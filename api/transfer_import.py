"""Import files as new stacks and cells, and expand uploaded zip archives.

Every imported body reaches the CAS by streaming (``cas.store_stream``):
one upload per file through ``POST /api/blob/stream``, or every member of an
uploaded zip through :func:`expand_zip`. :func:`commit_import` then turns a
plan of stacks, cells and blob references into new layout stacks, published
atomically through :class:`import_plan.ImportPlan`. Text or binary is decided
here from the stored bytes (``cas.encoding_for``), never by the client. An
exported cell's provenance file (``provenance.EXPORT_FILE`` at the cell's
root) is dropped: an imported cell never inherits provenance.
"""
from __future__ import annotations

import os
import re
import uuid
import zipfile
import zlib
from collections.abc import Iterable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

FOLDER_MARKER = ".collkeep"
CHUNK_BYTES = 64 * 1024

# Zip expansion caps. A member count or uncompressed total past these, or one
# member that inflates more than ZIP_MAX_RATIO times (above ZIP_RATIO_FLOOR,
# so tiny well-compressed text passes), refuses the whole archive with 413.
ZIP_MAX_MEMBERS = 50_000
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


# ---------------------------------------------------------------------------
# Zip expansion
# ---------------------------------------------------------------------------

def _refuse(status: int, message: str, path: str | None = None):
    raise HTTPException(status, message if path is None else f"{message}: {path!r}")


def _member_path(info: zipfile.ZipInfo) -> str:
    """The member's relative path, or a 400 for a hostile or reserved name."""
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
    if any(part in _RESERVED_COMPONENTS for part in parts):
        _refuse(400, "Reserved path in archive", raw)
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        _refuse(400, "Symbolic link in archive", raw)
    if info.flag_bits & 0x1:
        _refuse(400, "Encrypted archive member", raw)
    return name


def _check_caps(infos: list[zipfile.ZipInfo]) -> None:
    from cas import IMPORT_MAX_FILE_BYTES
    if len(infos) > ZIP_MAX_MEMBERS:
        _refuse(413, f"Archive holds more than {ZIP_MAX_MEMBERS} members")
    if sum(info.file_size for info in infos) > ZIP_MAX_TOTAL_BYTES:
        _refuse(413, f"Archive expands past {ZIP_MAX_TOTAL_BYTES} bytes")
    for info in infos:
        if info.file_size > IMPORT_MAX_FILE_BYTES:
            _refuse(413, "Archive member exceeds the import file cap", info.filename)
        if info.file_size > ZIP_RATIO_FLOOR and info.file_size > ZIP_MAX_RATIO * max(info.compress_size, 1):
            _refuse(413, "Archive member compression ratio is too high", info.filename)


def expand_zip(zip_path: str) -> dict:
    """Store every file member of the archive at ``zip_path`` in the CAS.

    Returns ``{entries: [{path, blob_hash, size, encoding}], dirs, skipped}``:
    ``dirs`` are the archive's empty folders, ``skipped`` the members left out
    with a reason. The whole archive is refused (400 for a structure, 413 for a
    cap) before any member is stored when a name or a declared size fails.
    """
    from cas import IMPORT_MAX_FILE_BYTES, BlobTooLarge, blob_path, encoding_for, store_stream
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
        for info in infos:
            if info.filename.startswith(_SKIPPED_PREFIX):
                skipped.append({"path": info.filename, "reason": "macOS metadata"})
                continue
            path = _member_path(info)
            if path in seen:
                _refuse(400, "Duplicate path in archive", path)
            seen.add(path)
            if info.is_dir():
                folders.add(path)
            else:
                files.append((path, info))
        entries = []
        expanded = 0
        for path, info in files:
            def chunks(source) -> Iterable[bytes]:
                nonlocal expanded
                while chunk := source.read(CHUNK_BYTES):
                    expanded += len(chunk)
                    if expanded > ZIP_MAX_TOTAL_BYTES:
                        raise BlobTooLarge(ZIP_MAX_TOTAL_BYTES)
                    yield chunk
            try:
                with archive.open(info) as source:
                    digest, size = store_stream(chunks(source), max_bytes=IMPORT_MAX_FILE_BYTES)
            except BlobTooLarge as exc:
                raise HTTPException(413, "Archive expands past its caps") from exc
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
                raise HTTPException(400, f"Archive member cannot be read: {path!r}") from exc
            entries.append({"path": path, "blob_hash": digest, "size": size,
                            "encoding": encoding_for(blob_path(digest))})
    occupied = [path for path, _info in files] + sorted(folders)
    dirs = sorted(d for d in folders if not any(p.startswith(d + "/") for p in occupied))
    return {"entries": entries, "dirs": dirs, "skipped": skipped}


def receive_zip(chunks: Iterable[bytes]) -> str:
    """Write an uploaded archive to a private file under the data dir and
    return its path; the caller removes it. 413 past ``ZIP_MAX_UPLOAD_BYTES``.
    Archives and member files a killed process left behind are swept first."""
    from cas import INCOMING_DIR, remove_stale_uploads
    remove_stale_uploads(IMPORTS_DIR, ".zip")
    remove_stale_uploads(INCOMING_DIR, ".part")
    os.makedirs(IMPORTS_DIR, exist_ok=True)
    path = os.path.join(IMPORTS_DIR, f"{uuid.uuid4().hex}.zip")
    size = 0
    try:
        with open(path, "wb") as out:
            for chunk in chunks:
                size += len(chunk)
                if size > ZIP_MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Archive exceeds {ZIP_MAX_UPLOAD_BYTES} bytes")
                out.write(chunk)
    except BaseException:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise
    return path


# ---------------------------------------------------------------------------
# Commit: new stacks and cells from uploaded blobs
# ---------------------------------------------------------------------------

class ImportFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    blob_hash: str


class ImportCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    files: list[ImportFile] = []
    dirs: list[str] = []


class ImportStack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    cells: list[ImportCell] = []


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    insert_index: int | None = None
    stacks: list[ImportStack] = []


def _cell_path(path: str) -> str:
    """A canonical cell-relative path outside ``_runs/`` and ``.collrun``, or 400."""
    from collimate.fs import relative_parts
    from reserved_paths import is_reserved_path
    from run_folders import is_run_path
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
    from cas import blob_path, encoding_for, is_cas_hash
    from models import Cell, VirtualFile, language_for
    from provenance import EXPORT_FILE
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
        stored = blob_path(item.blob_hash)
        if not os.path.isfile(stored):
            raise HTTPException(409, f"Unknown blob hash for {path!r}; upload it again")
        if encoding_for(stored) == "utf-8":
            with open(stored, "rb") as body:
                text = body.read().decode("utf-8")
            files.append(VirtualFile(name=path, language=language_for(path), content=text))
        else:
            files.append(VirtualFile(name=path, language="binary", content="", blob_hash=item.blob_hash))
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
    when None), in request order, and the first is selected. Answers
    ``{stack_ids, cell_ids}`` after the layout is persisted and flushed.
    """
    from cas import CorruptBlobError
    from import_plan import ImportPlan
    from models import Stack
    from state import channel_db_name, flush_persist_state, persist_state, require_channel, state_lock

    ch = require_channel(channel)
    channel_uuid = channel_db_name(ch)
    if not request.stacks:
        _refuse(400, "stacks is required")
    stacks = []
    for spec in request.stacks:
        if not spec.cells:
            _refuse(400, "A stack needs at least one cell", spec.name)
        stacks.append(Stack(name=spec.name.strip(), cells=[_cell(cell, channel_uuid) for cell in spec.cells]))

    plan = ImportPlan(ch)
    plan.candidate.main_stacks.extend(stacks)
    try:
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
    return {"stack_ids": stack_ids, "cell_ids": [str(c.id) for stack in stacks for c in stack.cells]}
