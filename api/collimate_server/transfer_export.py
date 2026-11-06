"""Export a cell, a stack, a set of stacks or tagged cells as plain folders.

The tree is built from a frozen :class:`lifecycle_snapshots.ExportSnapshot`,
one layout per :data:`Scope`:

- ``cell``: ``<cell>/<file>``; ``stack``: ``<stack>/<cell>/<file>``;
- ``all``: ``<channel>/<stack>/<cell>/<file>``, the Capabilities stack
  included as a folder, open or hidden;
- ``packages``: ``<channel>/Capabilities/<cell>/<file>``, the capability
  cells, so a new channel's packages import finds them;
- ``main``: ``<channel>/<stack>/<cell>/<file>`` over the standard main stacks;
- ``tag``: ``<tag>/<cell>/<file>``, the live tagged cells then the removed ones.

Every name is made safe on every OS (never a ``.git``-equivalent one) and
siblings are deduplicated as ``name (2)``. Run folders (``_runs/``) and run
staging (``.collrun``) are not exported, and a folder marker (``.collkeep``)
becomes the empty folder it keeps. A cell with provenance records carries
them in ``provenance.EXPORT_FILE`` beside its files. Nothing else (tags,
pins, display state, annotations) is written.

Each repository of a frozen cell (its tip's rows) is written as a real
``.git`` in its folder (§7 Export): :func:`stage_repositories` packs its
objects from the channel's store and builds the gitdir with
``git_materialize.write_gitdir(mode="export")`` under a staging directory
in ``.collimate/tmp``, index included, so ``git status`` in the exported
folder is clean when its files equal HEAD. Without usable host git the
``.git`` is left out, and the cell's provenance file says so, as it says
what else an export left behind (a stash, with local history omitted).

``export_manifest`` lists the tree for a client that writes the folders
itself (a staged ``.git`` file is fetched through the export's token:
:func:`hold_staging`); ``iter_zip`` streams it as a zip archive.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

from collimate_server.lifecycle_snapshots import ExportSnapshot
from collimate_server.repository_state import RepositoryState

_logger = logging.getLogger(__name__)

Scope = Literal["cell", "stack", "all", "packages", "main", "tag"]

FOLDER_MARKER = ".collkeep"
CHUNK_BYTES = 64 * 1024
# Zip pieces waiting for the client; the writer thread blocks beyond this.
QUEUE_PIECES = 16
# How long a folder export's staged files are served before they go.
STAGING_SECONDS = 3600.0
# The directories of ``.collimate/tmp`` an export stages its ``.git``s in.
STAGING_PREFIX = "export-"

_REGULAR_MODE = 0o100644
_EXECUTABLE_MODE = 0o100755

_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)])
_STACK_DEFAULTS = {"capabilities": "Capabilities"}


@dataclass(frozen=True)
class ExportEntry:
    """A cell file, read from the CAS; ``mode`` is its zip mode (HEAD's
    ``100755`` for an executable file of a repository)."""
    path: str
    body_hash: str
    size: int
    encoding: str
    mode: int = _REGULAR_MODE


@dataclass(frozen=True)
class StagedEntry:
    """A file the export wrote itself (a repository's ``.git``, a provenance
    file with notes): ``export_file`` is its name in the staging directory."""
    path: str
    export_file: str
    size: int


@dataclass(frozen=True)
class RepositoryExport:
    """A repository of a frozen cell to write as a ``.git``: its folder in
    the tree (``path``), its row (``repo_path``, ``state``), the cell files
    under it exported at their own names (``files``, cell path to body
    hash), the ones the export drops or renames (``excluded``, cell paths),
    the names the renamed ones are written under (``renamed``, relative to
    ``path``) and the cell's provenance file, whose notes say what was left
    behind."""
    path: str
    repo_path: str
    state: RepositoryState
    files: Mapping[str, str]
    excluded: frozenset[str]
    renamed: frozenset[str]
    provenance: str


@dataclass(frozen=True)
class ExportTree:
    """The tree of an export. ``repositories`` are still to be staged
    (:func:`stage_repositories`), after which ``staging`` names the
    directory holding the staged files and ``notes`` say what was left
    behind. ``provenance`` maps each cell's provenance file path to its
    records' CAS hash (None: a cell with repositories but no records,
    whose file is written only with notes); ``left_out`` maps one to the
    notes of the cell's repositories whose rows could not be read."""
    root: str
    entries: list[ExportEntry | StagedEntry]
    dirs: list[str]
    repositories: list[RepositoryExport] = field(default_factory=list)
    provenance: dict[str, str | None] = field(default_factory=dict)
    left_out: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    staging: str | None = None


def safe_name(name: str, fallback: str) -> str:
    """One path segment that every OS accepts: separators, reserved
    punctuation and control characters become ``_``, trailing dots and spaces
    go, a Windows device name gains a ``_`` after its stem, and a name git
    would take for ``.git`` (``.git.``, ``GIT~1``) gains a leading ``_``: the
    only ``.git`` in an export is a repository's own."""
    from collimate_server.reserved_paths import is_dotgit_segment
    cleaned = _UNSAFE.sub("_", name).strip().rstrip(". ")
    if not cleaned:
        cleaned = fallback
    stem, dot, ext = cleaned.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        cleaned = f"{stem}_{dot}{ext}"
    if is_dotgit_segment(cleaned):
        cleaned = f"_{cleaned}"
    return cleaned


class _Folder:
    """A folder of the output tree: its taken names (casefolded, because
    Windows and macOS compare names that way) and its named subfolders."""

    def __init__(self):
        self.taken: set[str] = set()
        self.subfolders: dict[str, tuple[str, _Folder]] = {}

    def claim(self, name: str, *, is_file: bool) -> str:
        candidate, number = name, 1
        stem, ext = os.path.splitext(name) if is_file else (name, "")
        if not stem:
            stem, ext = name, ""
        while candidate.casefold() in self.taken:
            number += 1
            candidate = f"{stem} ({number}){ext}"
        self.taken.add(candidate.casefold())
        return candidate

    def folder(self, original: str, fallback: str) -> tuple[str, _Folder]:
        """The output name and node of the subfolder named ``original``."""
        if original not in self.subfolders:
            self.subfolders[original] = (self.claim(safe_name(original, fallback), is_file=False), _Folder())
        return self.subfolders[original]


def _excluded(path: str) -> bool:
    from collimate_server.reserved_paths import is_reserved_path
    from collimate_server.run_folders import is_run_path
    return is_run_path(path) or is_reserved_path(path)


def _add_cell(tree: _Builder, base: str, folder: _Folder, files, records: str | None,
              repositories: Mapping[str, RepositoryState] | None = None,
              unreadable: Mapping[str, str] | None = None) -> None:
    """The cell's files under ``base``, then its provenance export file
    (``records``, its CAS hash) when it has one, then its repositories
    (``repositories``, by repo path), each resolved to its folder through
    the same nodes as the files; a row that could not be read
    (``unreadable``, its reason by repo path) is only a note."""
    from collimate_server.provenance import EXPORT_FILE
    tree.dirs.add(base)
    placed: dict[str, str] = {}
    for item in sorted(files, key=lambda f: f.name):
        if _excluded(item.name):
            continue
        parts = item.name.split("/")
        out, node = base, folder
        for part in parts[:-1]:
            name, node = node.folder(part, "folder")
            out = f"{out}/{name}"
        if parts[-1] == FOLDER_MARKER:
            tree.dirs.add(out)
            continue
        name = node.claim(safe_name(parts[-1], "file"), is_file=True)
        placed[item.name] = f"{out}/{name}"
        tree.entries.append(ExportEntry(f"{out}/{name}", item.body_hash,
                                        os.path.getsize(tree.store.blob_path(item.body_hash)), item.encoding))
    if records is None and not repositories and not unreadable:
        return
    provenance = f"{base}/{folder.claim(EXPORT_FILE, is_file=True)}"
    tree.provenance[provenance] = records
    if records is not None:
        tree.entries.append(ExportEntry(provenance, records, os.path.getsize(tree.store.blob_path(records)), "utf-8"))
    for repo_path, reason in sorted((unreadable or {}).items()):
        tree.left_out.setdefault(provenance, []).append(
            f"{_repository_folder(base, folder, repo_path)}/.git: not exported: {reason}")
    for repo_path, state in sorted((repositories or {}).items()):
        out = _repository_folder(base, folder, repo_path)
        tree.dirs.add(out)
        prefix = f"{repo_path}/" if repo_path else ""
        exported, excluded, renamed = {}, set(), set()
        for item in files:
            if not item.name.startswith(prefix):
                continue
            if placed.get(item.name) == f"{out}/{item.name[len(prefix):]}":
                exported[item.name] = item.body_hash
                continue
            excluded.add(item.name)
            if item.name in placed:
                renamed.add(placed[item.name][len(out) + 1:])
        if not repo_path and provenance != f"{base}/{EXPORT_FILE}":
            renamed.add(provenance[len(base) + 1:])
        tree.repositories.append(RepositoryExport(out, repo_path, state, exported, frozenset(excluded),
                                                  frozenset(renamed), provenance))


def _repository_folder(base: str, folder: _Folder, repo_path: str) -> str:
    """The output folder of the cell's repository at ``repo_path``, through
    the nodes its files took."""
    out, node = base, folder
    for part in repo_path.split("/") if repo_path else ():
        name, node = node.folder(part, "folder")
        out = f"{out}/{name}"
    return out


def _leaf_dirs(entries, dirs) -> list[str]:
    """Only folders nothing else creates: no file and no listed folder below."""
    occupied = [e.path for e in entries] + list(dirs)
    return sorted(d for d in set(dirs) if not any(p.startswith(d + "/") for p in occupied))


class _Builder:
    def __init__(self, store):
        # The snapshot's channel store, where every body of the tree is.
        self.store = store
        self.entries: list[ExportEntry | StagedEntry] = []
        self.dirs: set[str] = set()
        self.repositories: list[RepositoryExport] = []
        self.provenance: dict[str, str | None] = {}
        self.left_out: dict[str, list[str]] = {}

    def finish(self, root: str) -> ExportTree:
        return ExportTree(root, self.entries, _leaf_dirs(self.entries, self.dirs), self.repositories,
                          self.provenance, self.left_out)


def _stack_name(stack) -> tuple[str, str]:
    return stack.name, _STACK_DEFAULTS.get(stack.stack_type, "Stack")


def _frozen_files(snapshot: ExportSnapshot, cell) -> list:
    """The frozen files of a cell. A cell the freeze did not select joined the
    scope after its cells were chosen: ``LookupError``, so the export retries
    instead of writing that cell as an empty folder."""
    try:
        return snapshot.files_by_cell[str(cell.id)]
    except KeyError:
        raise LookupError(f"Cell {cell.id} joined the export scope") from None


def _add_cells(tree: _Builder, base: str, folder: _Folder, cells, snapshot: ExportSnapshot) -> None:
    """Each ``(cell, files, repositories, unreadable)`` as a sibling folder
    of ``base``."""
    tree.dirs.add(base)
    for cell, files, repositories, unreadable in cells:
        name = folder.claim(safe_name(cell.name, "Cell"), is_file=False)
        _add_cell(tree, f"{base}/{name}", _Folder(), files, snapshot.provenance.get(str(cell.id)), repositories,
                  unreadable)


def _live(snapshot: ExportSnapshot, cell) -> tuple:
    return (cell, _frozen_files(snapshot, cell), snapshot.repositories.get(str(cell.id), {}),
            snapshot.unreadable.get(str(cell.id), {}))


def _add_stack(tree: _Builder, base: str, folder: _Folder, stack, snapshot: ExportSnapshot) -> None:
    """The stack's cells; the app's built-in cards never travel."""
    _add_cells(tree, base, folder, [_live(snapshot, cell) for cell in stack.cells if not cell.builtin], snapshot)


def _add_stacks(tree: _Builder, root: str, stacks, snapshot: ExportSnapshot) -> ExportTree:
    """``<root>/<stack>/<cell>/…`` for each stack in order."""
    top = _Folder()
    tree.dirs.add(root)
    for stack in stacks:
        name = top.claim(safe_name(*_stack_name(stack)), is_file=False)
        _add_stack(tree, f"{root}/{name}", _Folder(), stack, snapshot)
    return tree.finish(root)


def build_tree(snapshot: ExportSnapshot, scope: Scope, *, cell_id: str | None = None,
               stack_id: str | None = None, tag: str | None = None) -> ExportTree:
    """The export tree of a frozen snapshot. Raises ``LookupError`` when the
    addressed cell or stack is not in the snapshot's layout, or a cell of
    the scope was not frozen. The ``tag`` scope writes the cells the freeze
    selected (``lifecycle_snapshots.freeze_tag_cells``) under ``tag``."""
    channel = snapshot.channel
    stacks = channel.main_stacks + channel.nav_stacks
    tree = _Builder(snapshot.store)
    if scope == "cell":
        cell = next((c for s in stacks for c in s.cells if str(c.id) == str(cell_id)), None)
        if cell is None:
            raise LookupError("Cell not found")
        root = safe_name(cell.name, "Cell")
        _cell, files, repositories, unreadable = _live(snapshot, cell)
        _add_cell(tree, root, _Folder(), files, snapshot.provenance.get(str(cell.id)), repositories, unreadable)
        return tree.finish(root)
    if scope == "stack":
        stack = next((s for s in stacks if str(s.id) == str(stack_id)), None)
        if stack is None:
            raise LookupError("Stack not found")
        root = safe_name(*_stack_name(stack))
        _add_stack(tree, root, _Folder(), stack, snapshot)
        return tree.finish(root)
    if scope == "tag":
        from collimate_server.cell_tags import normalize_tag
        root = safe_name(normalize_tag(tag) or "", "Tag")
        live = [_live(snapshot, cell) for stack in stacks for cell in stack.cells
                if str(cell.id) in snapshot.files_by_cell]
        removed = [(item.cell, item.files, item.repositories, item.unreadable) for item in snapshot.removed]
        _add_cells(tree, root, _Folder(), live + removed, snapshot)
        return tree.finish(root)
    root = safe_name(channel.name, "Channel")
    if scope == "packages":
        from collimate_server.cell_placement import capabilities_stack
        tree.dirs.add(root)
        _add_stack(tree, f"{root}/{_STACK_DEFAULTS['capabilities']}", _Folder(),
                   capabilities_stack(channel), snapshot)
        return tree.finish(root)
    if scope == "main":
        return _add_stacks(tree, root, [s for s in channel.main_stacks if s.stack_type == "standard"], snapshot)
    return _add_stacks(tree, root, stacks, snapshot)


# --- the repositories' .git ------------------------------------------------------------------------


def make_staging() -> tuple[str, str]:
    """A new staging directory ``.collimate/tmp/export-<id>``: its id and path."""
    import collimate_server.git_host as git_host
    token = uuid.uuid4().hex
    path = os.path.join(git_host.tmp_root(), f"{STAGING_PREFIX}{token}")
    os.mkdir(path, 0o700)
    return token, path


def _no_git_line(capability) -> str:
    return f"not exported: git on this machine is {capability.describe()}; install git 2.31 or later"


def stage_repositories(tree: ExportTree, snapshot: ExportSnapshot, staging: str, *,
                       omit_history: bool = False) -> ExportTree:
    """Write each repository of ``tree`` as a ``.git`` under ``staging`` and
    return the tree with their files (:class:`StagedEntry`), their folders,
    HEAD's exec bits on the files exported at their own names, and the
    notes, which also land in each cell's provenance file.

    Per repository (§7): ``export_state`` (``omit_history``: "Omit local
    git history (reflogs, stash)"), ``git_store.pack_for_export`` over its
    roots, then ``write_gitdir(mode="export")`` over a work tree holding
    the cell's files (links to their CAS bodies, for the filter guard),
    with the excluded paths skip-worktree, the names the renamed ones are
    written under excluded, and every Git account's secret forms redacted;
    what it warns of becomes a note. A repository that cannot be written
    (or whose row could not be read, ``tree.left_out``) is left out with a
    note; without usable host git every one is."""
    import collimate_server.git_host as git_host
    import collimate_server.git_materialize as git_materialize
    import collimate_server.git_store as git_store
    from collimate_server.repository_state import shown
    if not tree.repositories and not tree.left_out:
        return dataclasses.replace(tree, staging=staging)
    capability = git_host.capability()
    staged: list[ExportEntry | StagedEntry] = []
    dirs = set(tree.dirs)
    executable: set[str] = set()
    notes: dict[str, list[str]] = {path: list(said) for path, said in tree.left_out.items()}
    secrets: tuple[str, ...] = ()
    if capability.ok and tree.repositories:
        from collimate_server.git_harvest import account_secret_forms
        secrets = account_secret_forms()
    channel = _channel_uuid(snapshot)
    for n, job in enumerate(tree.repositories):
        said = notes.setdefault(job.provenance, [])
        if "refs/stash" in _refs(job.state) and omit_history:
            said.append(f"{job.path}: the stash was not exported (local git history omitted)")
        if not capability.ok:
            said.append(f"{job.path}/.git: {_no_git_line(capability)}")
            continue
        try:
            gitdir, modes, warnings = _write_repository(channel, job, n, staging, secrets=secrets,
                                                        omit_history=omit_history)
        except (git_materialize.MaterializeFailed, git_store.StoreFailed) as exc:
            said.append(f"{job.path}/.git: not exported: {exc.reason}")
            continue
        except (git_host.GitFailed, OSError) as exc:
            said.append(f"{job.path}/.git: not exported: {exc}")
            continue
        said += [f"{job.path}/.git: {warning.removeprefix(f'{shown(job.repo_path)}: ')}" for warning in warnings]
        executable |= {f"{job.path}/{path}" for path, mode in modes.items() if mode == "100755"}
        for root, folders, names in os.walk(gitdir):
            rel = os.path.relpath(root, staging).replace(os.sep, "/")
            inside = os.path.relpath(root, gitdir).replace(os.sep, "/")
            out = f"{job.path}/.git" if inside == "." else f"{job.path}/.git/{inside}"
            dirs.add(out)
            for name in sorted(names):
                staged.append(StagedEntry(f"{out}/{name}", f"{rel}/{name}", os.path.getsize(os.path.join(root, name))))
            folders.sort()
    entries: list[ExportEntry | StagedEntry] = [
        dataclasses.replace(e, mode=_EXECUTABLE_MODE) if isinstance(e, ExportEntry) and e.path in executable else e
        for e in tree.entries]
    lines = [line for said in notes.values() for line in said]
    for line in lines:
        _logger.info("export: %s", line)
    for k, (path, said) in enumerate(sorted(notes.items())):
        if said:
            entries = [e for e in entries if e.path != path]
            staged.append(_provenance_file(snapshot.store, staging, k, path, tree.provenance.get(path), said))
    entries += staged
    return dataclasses.replace(tree, entries=entries, dirs=_leaf_dirs(entries, dirs), notes=lines, staging=staging)


def _channel_uuid(snapshot: ExportSnapshot) -> str:
    from collimate_server.state import channel_db_name
    return channel_db_name(snapshot.channel)


def _refs(state: RepositoryState) -> dict[str, str]:
    from collimate_server.repository_state import parse_refs
    return parse_refs(state.part("refs") or b"")


def _write_repository(channel: str, job: RepositoryExport, n: int, staging: str, *, secrets: tuple[str, ...],
                      omit_history: bool) -> tuple[str, dict[str, str], tuple[str, ...]]:
    """Build ``job``'s ``.git`` as ``<staging>/git-<n>``: its path, the
    index's mode of each path (relative to the repository) and the build's
    warnings; its lines are logged."""
    import collimate_server.git_host as git_host
    import collimate_server.git_materialize as git_materialize
    import collimate_server.git_store as git_store
    from collimate_server.cas import channel_store
    store = channel_store(channel)
    state = git_materialize.export_state(job.state, omit_history=omit_history)
    work = os.path.join(staging, f"tree-{n}")
    packs = os.path.join(staging, f"pack-{n}")
    os.makedirs(work)
    os.makedirs(packs)
    try:
        prefix = len(job.repo_path) + 1 if job.repo_path else 0
        for cell_path, body_hash in job.files.items():
            _place(store.blob_path(body_hash), work, cell_path[prefix:])
        pack = git_store.pack_for_export(channel, state, packs)
        built = git_materialize.write_gitdir(
            work, state, git_store.objects_path(channel), repo_path=job.repo_path, hydrated=job.files.keys(),
            mode="export", excluded=job.excluded, renamed=job.renamed, pack=pack,
            staging=os.path.join(staging, str(n)), secrets=secrets)
        for line in (*built.lines, *built.warnings):
            _logger.info("export %s: %s", job.path, line)
        listed = git_host.run(["ls-files", "-s", "-z"], git_dir=os.path.join(work, ".git"), work_tree=work)
        modes = {}
        for record in listed.stdout.split(b"\0"):
            head, tab, path = record.partition(b"\t")
            if tab:
                modes[path.decode("utf-8", "replace")] = head.split(b" ")[0].decode("ascii")
        gitdir = os.path.join(staging, f"git-{n}")
        os.rename(os.path.join(work, ".git"), gitdir)
        return gitdir, modes, built.warnings
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(packs, ignore_errors=True)


def _place(source: str, work: str, rel: str) -> None:
    """The cell file ``rel`` in the work tree: a link to its CAS body (the
    export only reads it), else a copy."""
    target = os.path.join(work, *rel.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def _provenance_file(store, staging: str, k: int, path: str, records: str | None, notes: list[str]) -> StagedEntry:
    """The cell's provenance file with ``notes``, written into the staging."""
    from collimate_server.provenance import export_body
    held = json.loads(store.require_blob(records))["provenance"] if records is not None else []
    name = f"provenance-{k}.json"
    with open(os.path.join(staging, name), "wb") as fh:
        fh.write(export_body(held, notes))
    return StagedEntry(path, name, os.path.getsize(os.path.join(staging, name)))


# --- a folder export's staged files ----------------------------------------------------------------


@dataclass
class _Held:
    channel: str
    path: str
    timer: threading.Timer


_held: dict[str, _Held] = {}
_held_lock = threading.Lock()


def hold_staging(channel: str, token: str, staging: str) -> None:
    """Serve ``staging``'s files under ``token`` to the folder export of
    ``channel`` until :func:`release_staging`, or for :data:`STAGING_SECONDS`."""
    timer = threading.Timer(STAGING_SECONDS, release_staging, args=(token,))
    timer.daemon = True
    with _held_lock:
        _held[token] = _Held(channel, staging, timer)
    timer.start()


def staged_file(channel: str, token: str, export_file: str) -> str:
    """The path of the staged file ``export_file`` of ``channel``'s export
    ``token``; LookupError for an unknown token, a name outside the staging
    or anything but a regular file there."""
    import stat
    from collimate.fs import relative_parts
    with _held_lock:
        held = _held.get(token)
    if held is None or held.channel != channel:
        raise LookupError("No such export")
    try:
        parts = relative_parts(export_file)
    except (PermissionError, TypeError, ValueError):
        raise LookupError("No such file") from None
    path, at = held.path, held.path
    for part in parts:
        at = os.path.join(at, part)
        try:
            found = os.lstat(at)
        except OSError:
            raise LookupError("No such file") from None
        if stat.S_ISLNK(found.st_mode):
            raise LookupError("No such file")
    if at == path or not stat.S_ISREG(os.lstat(at).st_mode):
        raise LookupError("No such file")
    return at


def staging_channel(token: str) -> str | None:
    """The channel whose folder export holds ``token``, None when none does."""
    with _held_lock:
        held = _held.get(token)
    return None if held is None else held.channel


def held_stagings() -> list[str]:
    """The staging directories folder exports are serving now."""
    with _held_lock:
        return [held.path for held in _held.values()]


def release_staging(token: str) -> None:
    """Remove the staged files of the export ``token`` (idempotent)."""
    with _held_lock:
        held = _held.pop(token, None)
    if held is None:
        return
    held.timer.cancel()
    shutil.rmtree(held.path, ignore_errors=True)


def export_manifest(tree: ExportTree, token: str | None = None) -> dict:
    """The JSON shape of ``GET …/export/manifest``. Paths start with the
    ``root`` folder (``<cell>/…``, ``<stack>/…``, ``<tag>/…`` or
    ``<channel>/…``) and are relative to wherever the client puts that
    folder. A cell file names its CAS ``body_hash``; a staged one its
    ``export_file``, fetched through ``token`` (``GET
    …/export/{token}/file``); ``notes`` say what the export left behind."""
    return {
        "root": tree.root,
        "entries": [{"path": e.path, "export_file": e.export_file, "size": e.size} if isinstance(e, StagedEntry)
                    else {"path": e.path, "body_hash": e.body_hash, "size": e.size, "encoding": e.encoding}
                    for e in tree.entries],
        "dirs": list(tree.dirs),
        "token": token,
        "notes": list(tree.notes),
    }


class _Cancelled(Exception):
    pass


class QueueSink:
    """An unseekable binary sink handing the zip writer's output to a queue
    in pieces of about ``CHUNK_BYTES``. ``tell`` refuses, so ``zipfile``
    writes data descriptors and never seeks back."""

    def __init__(self, pieces: queue.Queue, cancelled: threading.Event):
        self._pieces = pieces
        self._cancelled = cancelled
        self._buffer = bytearray()

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        raise OSError("unseekable zip sink")

    def write(self, data) -> int:
        self._buffer += data
        while len(self._buffer) >= CHUNK_BYTES:
            self._put(bytes(self._buffer[:CHUNK_BYTES]))
            del self._buffer[:CHUNK_BYTES]
        return len(data)

    def flush(self) -> None:
        if self._buffer:
            self._put(bytes(self._buffer))
            self._buffer.clear()

    def _put(self, piece: bytes) -> None:
        while True:
            if self._cancelled.is_set():
                raise _Cancelled()
            try:
                self._pieces.put(piece, timeout=0.1)
                return
            except queue.Full:
                continue


class ZipStream:
    """A zip of ``tree`` written by a worker thread into a bounded queue.

    The worker owns the snapshot's pins and the tree's staging directory,
    and releases both when it ends: finished, failed or cancelled by
    ``close()``. ``next_chunk`` returns the next piece, None at the end, or
    raises the worker's error.
    """

    def __init__(self, tree: ExportTree, snapshot: ExportSnapshot):
        self._tree = tree
        self._snapshot = snapshot
        self._pieces: queue.Queue = queue.Queue(maxsize=QUEUE_PIECES)
        self._cancelled = threading.Event()
        self._finished = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._write, name="zip-export", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            snapshot.release()
            if tree.staging is not None:
                shutil.rmtree(tree.staging, ignore_errors=True)
            raise

    def _write(self) -> None:
        store = self._snapshot.store
        sink = QueueSink(self._pieces, self._cancelled)
        stamp = time.localtime()[:6]
        try:
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:  # type: ignore[arg-type]
                for directory in self._tree.dirs:
                    info = zipfile.ZipInfo(directory + "/", date_time=stamp)
                    info.external_attr = (0o40755 << 16) | 0x10
                    info.CRC = info.compress_size = info.file_size = 0
                    archive.mkdir(info)
                for entry in self._tree.entries:
                    info = zipfile.ZipInfo(entry.path, date_time=stamp)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    # A known size lets the writer pick ZIP64 headers up front.
                    info.file_size = entry.size
                    if isinstance(entry, StagedEntry):
                        assert self._tree.staging is not None
                        info.external_attr = _REGULAR_MODE << 16
                        # A pack is compressed already.
                        info.compress_level = 1 if entry.export_file.endswith(".pack") else 6
                        source_path = os.path.join(self._tree.staging, *entry.export_file.split("/"))
                        with open(source_path, "rb") as source, archive.open(info, "w", force_zip64=True) as target:
                            while chunk := source.read(CHUNK_BYTES):
                                target.write(chunk)
                        continue
                    info.external_attr = entry.mode << 16
                    # Binary bodies are mostly compressed already.
                    info.compress_level = 1 if entry.encoding == "binary" else 6
                    with open(store.blob_path(entry.body_hash), "rb") as source, archive.open(info, "w") as target:
                        while chunk := source.read(CHUNK_BYTES):
                            target.write(chunk)
            sink.flush()
        except _Cancelled:
            pass
        except BaseException as exc:
            self._error = exc
        finally:
            self._snapshot.release()
            if self._tree.staging is not None:
                shutil.rmtree(self._tree.staging, ignore_errors=True)
            self._finished.set()

    def next_chunk(self) -> bytes | None:
        while True:
            try:
                return self._pieces.get(timeout=0.1)
            except queue.Empty:
                if self._finished.is_set() and self._pieces.empty():
                    if self._error is not None:
                        raise self._error
                    return None

    def close(self) -> None:
        """Stop the worker without waiting for it; its pins release as it ends."""
        self._cancelled.set()
        while True:
            try:
                self._pieces.get_nowait()
            except queue.Empty:
                return

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


def iter_zip(tree: ExportTree, snapshot: ExportSnapshot) -> Iterator[bytes]:
    """The zip of ``tree`` in pieces of about 64 KiB; closing the iterator
    early cancels the writer, and the pins release either way."""
    stream = ZipStream(tree, snapshot)
    try:
        while (piece := stream.next_chunk()) is not None:
            yield piece
    finally:
        stream.close()
