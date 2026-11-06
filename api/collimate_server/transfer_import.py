"""Import files as new stacks and cells, and expand uploaded zip archives.

Every imported body reaches the target channel's body store by streaming
(``BodyStore.store_stream``; a new channel's zips the staging store):
one upload per file through ``POST /api/blob/stream``, or every member of an
uploaded zip through :func:`expand_zip`. :func:`commit_import` then turns a
plan of stacks, cells and blob references into new layout stacks, published
atomically through :class:`import_plan.ImportPlan`. Text or binary is decided
here from the stored bytes (``cas.encoding_for``), never by the client. An
exported cell's provenance file (``provenance.EXPORT_FILE`` at the cell's
root) is dropped: an imported cell never inherits provenance.

A repository travels as its ``.git``: :func:`expand_zip` sets each
``<dir>/.git/`` aside under ``.collimate/imports/<token>/`` (never a cell
file), as ``POST /api/import/repository`` does for one uploaded ``.git``,
and a new cell adopts it by token (``ImportCell.repositories``):
:class:`Adoptions` reads it as the cell's repository state and lands its
objects in the channel's git store as the cells publish. What git would not
adopt is left out with git's words; the cell's files import either way.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collimate_server.git_harvest import Adopted
    from collimate_server.models import Cell
    from collimate_server.repository_state import RepositoryState

FOLDER_MARKER = ".collkeep"
CHUNK_BYTES = 64 * 1024

# Zip expansion caps. A member count or uncompressed total past these, or one
# member that inflates more than ZIP_MAX_RATIO times (above ZIP_RATIO_FLOOR,
# so tiny well-compressed text passes), refuses the whole archive with 413.
ZIP_MAX_MEMBERS = 50_000
# The members of the archive's ``.git`` folders, counted on their own: a
# repository's loose objects and refs are many small files.
ZIP_MAX_GIT_MEMBERS = 100_000
ZIP_MAX_TOTAL_BYTES = 16 * 1024 ** 3
ZIP_MAX_RATIO = 200
ZIP_RATIO_FLOOR = 1024 * 1024
# The uploaded archive itself: its members' bytes plus headers.
ZIP_MAX_UPLOAD_BYTES = ZIP_MAX_TOTAL_BYTES + 1024 ** 3

# Uploaded archives wait here (under the data dir) while they expand.
IMPORTS_DIR = os.path.join(".collimate", "imports")

_RESERVED_COMPONENTS = frozenset({".collrun", "_runs"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")
# Finder's resource-fork copies, not part of the user's tree.
_SKIPPED_PREFIX = "__MACOSX/"

GITDIR = ".git"
GITFILE_REASON = "submodule/worktree gitfile (not supported)"
# A ``.git``'s folders an import does not take: submodule gitdirs and
# linked worktrees are not kept (§2, D15).
_GITDIR_IGNORED = frozenset({"modules", "worktrees"})
_TOKEN = re.compile(r"[0-9a-f]{32}")


# ---------------------------------------------------------------------------
# Zip expansion
# ---------------------------------------------------------------------------

def _refuse(status: int, message: str, path: str | None = None):
    raise HTTPException(status, message if path is None else f"{message}: {path!r}")


def _member_name(info: zipfile.ZipInfo) -> str:
    """The member's relative path, or a 400 for a hostile name."""
    raw = info.filename
    name = raw[:-1] if info.is_dir() else raw
    if not name:
        _refuse(400, "Empty member name in archive")
    if "\\" in name:
        _refuse(400, "Backslash in archive path", raw)
    if name.startswith("/") or _DRIVE.match(name):
        _refuse(400, "Absolute path in archive", raw)
    parts = name.split("/")
    if ".." in parts:
        _refuse(400, "Parent reference in archive path", raw)
    if any(part in ("", ".") for part in parts) or _CONTROL.search(name):
        _refuse(400, "Invalid archive path", raw)
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        _refuse(400, "Symbolic link in archive", raw)
    if info.flag_bits & 0x1:
        _refuse(400, "Encrypted archive member", raw)
    return name


def _reserved(parts: list[str], raw: str) -> None:
    if any(part in _RESERVED_COMPONENTS for part in parts):
        _refuse(400, "Reserved path in archive", raw)


def _member_path(info: zipfile.ZipInfo) -> str:
    """The member's relative path, or a 400 for a hostile or reserved name."""
    name = _member_name(info)
    _reserved(name.split("/"), info.filename)
    return name


def _git_split(raw: str) -> tuple[str, list[str]] | None:
    """Where a member lies in a repository's ``.git``: the folder holding
    the first exact ``.git`` segment (``""`` the archive's root) and the
    segments after it; None outside every ``.git``."""
    parts = raw.rstrip("/").split("/")
    if GITDIR not in parts:
        return None
    at = parts.index(GITDIR)
    return "/".join(parts[:at]), parts[at + 1:]


def _check_caps(infos: list[zipfile.ZipInfo]) -> None:
    from collimate_server.cas import IMPORT_MAX_FILE_BYTES
    gitdir = sum(1 for info in infos if _git_split(info.filename) is not None)
    if len(infos) - gitdir > ZIP_MAX_MEMBERS:
        _refuse(413, f"Archive holds more than {ZIP_MAX_MEMBERS} members")
    if gitdir > ZIP_MAX_GIT_MEMBERS:
        _refuse(413, f"Archive holds more than {ZIP_MAX_GIT_MEMBERS} members under .git")
    if sum(info.file_size for info in infos) > ZIP_MAX_TOTAL_BYTES:
        _refuse(413, f"Archive expands past {ZIP_MAX_TOTAL_BYTES} bytes")
    for info in infos:
        if info.file_size > IMPORT_MAX_FILE_BYTES:
            _refuse(413, "Archive member exceeds the import file cap", info.filename)
        if info.file_size > ZIP_RATIO_FLOOR and info.file_size > ZIP_MAX_RATIO * max(info.compress_size, 1):
            _refuse(413, "Archive member compression ratio is too high", info.filename)


class _Gitdir:
    """One ``<dir>/.git`` of an archive: its folders and files, by their
    segments under the ``.git``."""

    def __init__(self) -> None:
        self.folders: list[list[str]] = []
        self.files: list[tuple[list[str], zipfile.ZipInfo]] = []


def expand_zip(zip_path: str, store, *, repository: bool = False) -> dict:
    """Store every file member of the archive at ``zip_path`` in the CAS and
    set each repository's ``.git`` aside for an import to adopt.

    Returns ``{entries: [{path, blob_hash, size, encoding}], dirs,
    repositories: [{dir, token}], skipped}``: ``dirs`` are the archive's
    empty folders, ``repositories`` each ``<dir>/.git/`` folder (by the
    folder holding it, ``""`` the root) written under
    ``.collimate/imports/<token>/.git`` (never a cell file; its ``modules/``
    and ``worktrees/`` left out), ``skipped`` the members left out with a
    reason (a ``.git`` that is a file: a gitfile). The whole archive is
    refused (400 for a structure, 413 for a cap) before any member is
    stored when a name or a declared size fails; a ``.git`` under
    ``.collrun`` or ``_runs`` is a 400 like any member there.

    ``repository``: the archive is one repository's ``.git`` and nothing
    else (``POST /api/import/repository``): ``{dir, token}``, 400 for a
    member outside it, a gitfile, or not exactly one ``.git``.
    """
    from collimate_server.cas import IMPORT_MAX_FILE_BYTES, BlobTooLarge, encoding_for
    store.remove_stale_uploads()
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise HTTPException(400, "Not a readable zip archive") from exc
    with archive:
        infos = archive.infolist()
        _check_caps(infos)
        files: list[tuple[str, zipfile.ZipInfo]] = []
        folders: set[str] = set()
        skipped: list[dict] = []
        seen: set[str] = set()
        gitdirs: dict[str, _Gitdir] = {}
        for info in infos:
            if info.filename.startswith(_SKIPPED_PREFIX):
                skipped.append({"path": info.filename, "reason": "macOS metadata"})
                continue
            split = _git_split(info.filename)
            path = _member_name(info) if split is not None else _member_path(info)
            if path in seen:
                _refuse(400, "Duplicate path in archive", path)
            seen.add(path)
            if split is not None:
                holder, inside = split
                _reserved(holder.split("/"), info.filename)
                if not inside and not info.is_dir():
                    if repository:
                        _refuse(400, f"A .git that is a file: {GITFILE_REASON}", path)
                    skipped.append({"path": path, "reason": GITFILE_REASON})
                elif not inside or inside[0] not in _GITDIR_IGNORED:
                    gitdir = gitdirs.setdefault(holder, _Gitdir())
                    if info.is_dir():
                        gitdir.folders.append(inside)
                    else:
                        gitdir.files.append((inside, info))
            elif info.is_dir():
                folders.add(path)
            elif repository:
                _refuse(400, "Not a repository's .git: a member outside it", path)
            else:
                files.append((path, info))
        if repository and len(gitdirs) != 1:
            _refuse(400, "Not one repository's .git")
        entries = []
        expanded = 0

        def chunks(source) -> Iterable[bytes]:
            nonlocal expanded
            while chunk := source.read(CHUNK_BYTES):
                expanded += len(chunk)
                if expanded > ZIP_MAX_TOTAL_BYTES:
                    raise BlobTooLarge(ZIP_MAX_TOTAL_BYTES)
                yield chunk

        for path, info in files:
            try:
                with archive.open(info) as source:
                    digest, size = store.store_stream(chunks(source), max_bytes=IMPORT_MAX_FILE_BYTES)
            except BlobTooLarge as exc:
                raise HTTPException(413, "Archive expands past its caps") from exc
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
                raise HTTPException(400, f"Archive member cannot be read: {path!r}") from exc
            entries.append({"path": path, "blob_hash": digest, "size": size,
                            "encoding": encoding_for(store.blob_path(digest))})
        repositories = []
        try:
            for holder, gitdir in sorted(gitdirs.items()):
                repositories.append({"dir": holder, "token": _set_aside(archive, gitdir, chunks)})
        except BaseException:
            for made in repositories:
                shutil.rmtree(os.path.join(IMPORTS_DIR, made["token"]), ignore_errors=True)
            raise
    if repository:
        return repositories[0]
    occupied = [path for path, _info in files] + sorted(folders)
    dirs = sorted(d for d in folders if not any(p.startswith(d + "/") for p in occupied))
    return {"entries": entries, "dirs": dirs, "repositories": repositories, "skipped": skipped}


def _set_aside(archive: zipfile.ZipFile, gitdir: _Gitdir, chunks) -> str:
    """Write one ``.git``'s members as ``.collimate/imports/<token>/.git``;
    the token. The token folder's mtime is the upload's, for the stale sweep
    (``cas.remove_stale_upload_dirs``)."""
    from collimate_server.cas import BlobTooLarge
    token = uuid.uuid4().hex
    root = os.path.join(IMPORTS_DIR, token, GITDIR)
    os.makedirs(root)
    try:
        for inside in gitdir.folders:
            try:
                os.makedirs(os.path.join(root, *inside), exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                _refuse(400, "Conflicting paths in archive", f"{GITDIR}/{'/'.join(inside)}")
        for inside, info in gitdir.files:
            target = os.path.join(root, *inside)
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with archive.open(info) as source, open(target, "xb") as out:
                    for chunk in chunks(source):
                        out.write(chunk)
            except BlobTooLarge as exc:
                raise HTTPException(413, "Archive expands past its caps") from exc
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
                raise HTTPException(400, f"Archive member cannot be read: {info.filename!r}") from exc
            except (FileExistsError, IsADirectoryError, NotADirectoryError):
                _refuse(400, "Conflicting paths in archive", info.filename)
    except BaseException:
        shutil.rmtree(os.path.dirname(root), ignore_errors=True)
        raise
    return token


def _upload_path() -> str:
    """A new private archive path under the data dir, after the archives,
    member files and set-aside ``.git`` folders a killed process left behind
    were swept."""
    from collimate_server.cas import remove_stale_upload_dirs, remove_stale_uploads
    remove_stale_uploads(IMPORTS_DIR, ".zip")
    remove_stale_upload_dirs(IMPORTS_DIR)
    os.makedirs(IMPORTS_DIR, exist_ok=True)
    return os.path.join(IMPORTS_DIR, f"{uuid.uuid4().hex}.zip")


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def receive_zip(chunks: Iterable[bytes]) -> str:
    """Write an uploaded archive to a private file under the data dir and
    return its path; the caller removes it. 413 past ``ZIP_MAX_UPLOAD_BYTES``.
    Archives, member files and set-aside ``.git`` folders a killed process left
    behind are swept first."""
    path = _upload_path()
    size = 0
    try:
        with open(path, "wb") as out:
            for chunk in chunks:
                size += len(chunk)
                if size > ZIP_MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Archive exceeds {ZIP_MAX_UPLOAD_BYTES} bytes")
                out.write(chunk)
    except BaseException:
        _unlink(path)
        raise
    return path


_BOUNDARY = re.compile(r'boundary=(?:"([^"]{1,70})"|([^";\s]{1,70}))', re.I)
_PART_NAME = re.compile(rb'(?:^|;)\s*name="([^"]*)"', re.I)
# A part's headers are short; anything longer is not a form a client sent.
_PART_HEADERS_MAX = 16 * 1024


def receive_form_file(chunks: Iterable[bytes], content_type: str, field: str = "file") -> str:
    """Write the ``field`` file part of a ``multipart/form-data`` upload
    (``content_type`` its header) to a private file under the data dir and
    return its path; the caller removes it. Other parts are read past. 400
    for a body that is not such a form or lacks the part, 413 past
    ``ZIP_MAX_UPLOAD_BYTES``."""
    match = _BOUNDARY.search(content_type)
    if not content_type.lower().startswith("multipart/form-data") or match is None:
        _refuse(400, "Expected a multipart/form-data upload")
    assert match is not None
    delimiter = b"\r\n--" + (match.group(1) or match.group(2)).encode("latin-1")
    path = _upload_path()
    out = None
    found = False
    # The body's first delimiter has no CRLF before it; this one stands in.
    buffer = bytearray(b"\r\n")
    phase = "preamble"
    size = 0
    try:
        for chunk in chunks:
            size += len(chunk)
            if size > ZIP_MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"Archive exceeds {ZIP_MAX_UPLOAD_BYTES} bytes")
            buffer += chunk
            while True:
                if phase == "headers":
                    end = buffer.find(b"\r\n\r\n")
                    if end < 0:
                        if len(buffer) > _PART_HEADERS_MAX:
                            _refuse(400, "Malformed multipart upload")
                        break
                    headers = bytes(buffer[:end])
                    del buffer[:end + 4]
                    names = [line for line in headers.split(b"\r\n")
                             if line.lower().startswith(b"content-disposition:")]
                    named = _PART_NAME.search(names[0].split(b":", 1)[1]) if names else None
                    if named is not None and named.group(1).decode("latin-1") == field and not found:
                        out = open(path, "wb")
                        found = True
                    phase = "body"
                    continue
                at = buffer.find(delimiter)
                if at < 0:
                    keep = len(delimiter) - 1
                    if phase == "body" and out is not None and len(buffer) > keep:
                        out.write(buffer[:-keep])
                    if len(buffer) > keep:
                        del buffer[:-keep]
                    break
                if phase == "body" and out is not None:
                    out.write(buffer[:at])
                    out.close()
                    out = None
                del buffer[:at]
                after = buffer[len(delimiter):len(delimiter) + 2]
                if len(after) < 2:
                    break
                if after == b"--":
                    phase = "done"
                    break
                if after != b"\r\n":
                    _refuse(400, "Malformed multipart upload")
                del buffer[:len(delimiter) + 2]
                phase = "headers"
            if phase == "done":
                break
        if phase != "done" or not found:
            _refuse(400, f"The upload has no {field!r} file")
    except BaseException:
        if out is not None:
            out.close()
        _unlink(path)
        raise
    return path


# ---------------------------------------------------------------------------
# Commit: new stacks and cells from uploaded blobs
# ---------------------------------------------------------------------------

class ImportFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    blob_hash: str


class ImportRepository(BaseModel):
    """A repository the cell adopts: its folder (``""`` the cell root) and
    the token of its uploaded ``.git``."""
    model_config = ConfigDict(extra="forbid")
    repo_path: str
    token: str = Field(min_length=1, max_length=128)


class ImportCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    files: list[ImportFile] = []
    dirs: list[str] = []
    repositories: list[ImportRepository] = []


class ImportStack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    cells: list[ImportCell] = []


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    insert_index: int | None = None
    stacks: list[ImportStack] = []


def _gitdir_path(repo_path: str) -> str:
    return f"{repo_path}/{GITDIR}" if repo_path else GITDIR


class Adoptions:
    """The repositories an import's new cells adopt (§7 Import), all in one
    channel: :meth:`add` reads each uploaded ``.git`` (by its token) as the
    state of its cell's repository (``git_harvest.adopt``), and
    :meth:`publishing` lands their objects in the channel's store and holds
    the ingest in flight while the caller publishes the rows. ``skipped``
    names what git would not adopt, ``{path: "<repo_path>/.git",
    reason}``; the cell's files import either way. Close it (or use
    ``with``) when the import is done."""

    def __init__(self, channel_uuid: str):
        self.channel = channel_uuid
        self.skipped: list[dict] = []
        self._ready: list[tuple[str, str, Adopted]] = []
        self._secrets: tuple[str, ...] | None = None
        self._stack = ExitStack()

    def add(self, cell: Cell, repositories: Iterable[ImportRepository]) -> None:
        """Read the new ``cell``'s uploaded repositories, at most the cell's
        ``MAX_REPOSITORIES_PER_CELL`` (shallowest first). 400 for a token
        that is not one, 409 for one whose ``.git`` is gone."""
        import collimate_server.git_harvest as git_harvest
        import collimate_server.git_host as git_host
        from collimate_server.repository_state import MAX_REPOSITORIES_PER_CELL, cap_repositories
        by_path = {repository.repo_path: repository for repository in repositories}
        if not by_path:
            return
        for repository in by_path.values():
            if not _TOKEN.fullmatch(repository.token):
                _refuse(400, "Invalid repository token", repository.token)
            if not os.path.isdir(os.path.join(IMPORTS_DIR, repository.token, GITDIR)):
                raise HTTPException(409, f"The uploaded .git of {_gitdir_path(repository.repo_path)!r} is gone; "
                                         "upload it again")
        if not git_host.capability().ok:
            self.skipped += [{"path": _gitdir_path(path), "reason": "git repository not adopted: git not available"}
                             for path in sorted(by_path)]
            return
        kept, _overflow = cap_repositories(by_path, ())
        for path in sorted(set(by_path) - set(kept)):
            self.skipped.append({"path": _gitdir_path(path),
                                 "reason": f"over the cell's {MAX_REPOSITORIES_PER_CELL} repositories"})
        if self._secrets is None:
            self._secrets = git_harvest.account_secret_forms()
        files = [item.name for item in cell.files]
        for path in kept:
            token = by_path[path].token
            try:
                adopted = git_harvest.adopt(self.channel, os.path.join(IMPORTS_DIR, token), path,
                                            cell_files=files, secrets=self._secrets)
            except git_harvest.NotAdopted as exc:
                self.skipped.append({"path": _gitdir_path(path), "reason": str(exc)})
                continue
            self._stack.enter_context(adopted)
            self._ready.append((str(cell.id), token, adopted))

    @contextmanager
    def publishing(self) -> Iterator[dict[str, dict[str, RepositoryState]]]:
        """Inside ``git_store.in_flight``, land every adopted repository's
        objects and yield the states that landed, ``{cell_id: {repo_path:
        state}}``, for the caller to publish as rows in the block; once it
        has, their uploads go."""
        import collimate_server.git_harvest as git_harvest
        import collimate_server.git_store as git_store
        with git_store.in_flight(self.channel):
            landed: dict[str, dict[str, RepositoryState]] = {}
            tokens = []
            for cell_id, token, adopted in self._ready:
                try:
                    adopted.land()
                except git_harvest.NotAdopted as exc:
                    self.skipped.append({"path": _gitdir_path(adopted.repo_path), "reason": str(exc)})
                    continue
                landed.setdefault(cell_id, {})[adopted.repo_path] = adopted.state
                tokens.append(token)
            yield landed
        for token in tokens:
            shutil.rmtree(os.path.join(IMPORTS_DIR, token), ignore_errors=True)

    def close(self) -> None:
        self._stack.close()

    def __enter__(self) -> Adoptions:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _cell_path(path: str) -> str:
    """A canonical cell-relative path outside ``_runs/`` and ``.collrun``, or 400."""
    from collimate.fs import relative_parts
    from collimate_server.reserved_paths import is_reserved_path
    from collimate_server.run_folders import is_run_path
    try:
        canonical = "/".join(relative_parts(path))
    except (PermissionError, TypeError, ValueError):
        canonical = None
    if canonical != path or _CONTROL.search(path):
        _refuse(400, "Invalid file path", path)
    if is_reserved_path(path) or is_run_path(path):
        _refuse(400, "Reserved file path", path)
    return path


def _cell(spec: ImportCell, channel_uuid: str):
    """An import cell's files from the channel's body store (where its
    uploads were streamed)."""
    from collimate_server.cas import channel_store, encoding_for, is_cas_hash
    from collimate_server.models import Cell, VirtualFile, language_for
    from collimate_server.provenance import EXPORT_FILE
    from collimate_server.reserved_paths import is_repo_path
    files: list = []
    paths: set[str] = set()
    for item in spec.files:
        if item.path == EXPORT_FILE:
            continue
        path = _cell_path(item.path)
        if path in paths:
            _refuse(400, "Duplicate file path", path)
        paths.add(path)
        if not is_cas_hash(item.blob_hash):
            _refuse(400, "Invalid blob hash", path)
        stored = channel_store(channel_uuid).blob_path(item.blob_hash)
        if not os.path.isfile(stored):
            raise HTTPException(409, f"Unknown blob hash for {path!r}; upload it again")
        if encoding_for(stored) == "utf-8":
            with open(stored, "rb") as body:
                text = body.read().decode("utf-8")
            files.append(VirtualFile(name=path, language=language_for(path), content=text))
        else:
            files.append(VirtualFile(name=path, language="binary", content="", blob_hash=item.blob_hash))
    adopted: set[str] = set()
    for repository in spec.repositories:
        if not is_repo_path(repository.repo_path):
            _refuse(400, "Invalid repository path", repository.repo_path)
        if repository.repo_path in adopted:
            _refuse(400, "Duplicate repository path", repository.repo_path)
        adopted.add(repository.repo_path)
    for folder in spec.dirs:
        folder = _cell_path(folder.rstrip("/"))
        marker = f"{folder}/{FOLDER_MARKER}"
        if marker in paths or any(p.startswith(folder + "/") for p in paths):
            continue
        paths.add(_cell_path(marker))
        files.append(VirtualFile(name=marker, language=language_for(marker), content=""))
    return Cell(name=spec.name.strip() or "Imported", files=files, channel=channel_uuid)


def commit_import(channel: str, request: ImportRequest) -> dict:
    """Publish the request's stacks and cells, all or nothing.

    New stacks land in ``main_stacks`` at ``insert_index`` (clamped; appended
    when None), in request order, and the first is selected. Each cell
    adopts its uploaded repositories (:class:`Adoptions`). Answers
    ``{stack_ids, cell_ids, skipped}`` after the layout is persisted and
    flushed: ``skipped`` the repositories git would not adopt.
    """
    from collimate_server.cas import CorruptBlobError
    from collimate_server.import_plan import ImportPlan
    from collimate_server.models import Stack
    from collimate_server.state import channel_db_name, flush_persist_state, persist_state, require_channel, state_lock

    ch = require_channel(channel)
    channel_uuid = channel_db_name(ch)
    if not request.stacks:
        _refuse(400, "stacks is required")
    stacks = []
    adopting: list[tuple[Cell, list[ImportRepository]]] = []
    for spec in request.stacks:
        if not spec.cells:
            _refuse(400, "A stack needs at least one cell", spec.name)
        cells = [_cell(cell, channel_uuid) for cell in spec.cells]
        adopting += list(zip(cells, (item.repositories for item in spec.cells)))
        stacks.append(Stack(name=spec.name.strip(), cells=cells))

    plan = ImportPlan(ch)
    plan.candidate.main_stacks.extend(stacks)
    with Adoptions(channel_uuid) as adoptions:
        for cell, repositories in adopting:
            adoptions.add(cell, repositories)
        try:
            with adoptions.publishing() as landed:
                plan.repositories = landed
                plan.publish()
        except (FileNotFoundError, CorruptBlobError) as exc:
            raise HTTPException(409, "Stored content is missing; upload it again") from exc

    # The plan appends new stacks; move them to the requested position before
    # the layout is persisted.
    with state_lock:
        firsts = [str(stack.cells[0].id) for stack in stacks]
        published = [next((s for s in ch.main_stacks if any(str(c.id) == first for c in s.cells)), None)
                     for first in firsts]
        published = [s for s in published if s is not None]
        for stack in published:
            ch.main_stacks.remove(stack)
        at = len(ch.main_stacks) if request.insert_index is None else max(0, min(request.insert_index, len(ch.main_stacks)))
        ch.main_stacks[at:at] = published
        if published:
            ch.main_selection_id = published[0].id
        stack_ids = [str(s.id) for s in published]
    persist_state()
    flush_persist_state()
    return {"stack_ids": stack_ids, "cell_ids": [str(c.id) for stack in stacks for c in stack.cells],
            "skipped": adoptions.skipped}
