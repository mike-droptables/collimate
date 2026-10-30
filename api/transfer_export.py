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

Every name is made safe on every OS and siblings are deduplicated as
``name (2)``. Run folders (``_runs/``) and run staging (``.collrun``) are not
exported, and a folder marker (``.collkeep``) becomes the empty folder it
keeps. A cell with provenance records carries them in
``provenance.EXPORT_FILE`` beside its files. Nothing else (tags, pins,
display state, annotations) is written.

``export_manifest`` lists the tree for a client that writes the folders
itself; ``iter_zip`` streams it as a zip archive.
"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from lifecycle_snapshots import ExportSnapshot

Scope = Literal["cell", "stack", "all", "packages", "main", "tag"]

FOLDER_MARKER = ".collkeep"
CHUNK_BYTES = 64 * 1024
# Zip pieces waiting for the client; the writer thread blocks beyond this.
QUEUE_PIECES = 16

_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)])
_STACK_DEFAULTS = {"capabilities": "Capabilities"}


@dataclass(frozen=True)
class ExportEntry:
    path: str
    body_hash: str
    size: int
    encoding: str


@dataclass(frozen=True)
class ExportTree:
    root: str
    entries: list[ExportEntry]
    dirs: list[str]


def safe_name(name: str, fallback: str) -> str:
    """One path segment that every OS accepts: separators, reserved
    punctuation and control characters become ``_``, trailing dots and spaces
    go, and a Windows device name gains a ``_`` after its stem."""
    cleaned = _UNSAFE.sub("_", name).strip().rstrip(". ")
    if not cleaned:
        cleaned = fallback
    stem, dot, ext = cleaned.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        cleaned = f"{stem}_{dot}{ext}"
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
    from reserved_paths import is_reserved_path
    from run_folders import is_run_path
    return is_run_path(path) or is_reserved_path(path)


def _add_cell(tree: _Builder, base: str, folder: _Folder, files, records: str | None) -> None:
    """The cell's files under ``base``, then its provenance export file
    (``records``, its CAS hash) when it has one."""
    from cas import blob_path
    from provenance import EXPORT_FILE
    tree.dirs.add(base)
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
        tree.entries.append(ExportEntry(f"{out}/{name}", item.body_hash,
                                        os.path.getsize(blob_path(item.body_hash)), item.encoding))
    if records is not None:
        name = folder.claim(EXPORT_FILE, is_file=True)
        tree.entries.append(ExportEntry(f"{base}/{name}", records, os.path.getsize(blob_path(records)), "utf-8"))


class _Builder:
    def __init__(self):
        self.entries: list[ExportEntry] = []
        self.dirs: set[str] = set()

    def finish(self, root: str) -> ExportTree:
        # Only folders nothing else creates: no file and no listed folder below.
        occupied = [e.path for e in self.entries] + list(self.dirs)
        dirs = sorted(d for d in self.dirs if not any(p.startswith(d + "/") for p in occupied))
        return ExportTree(root, self.entries, dirs)


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
    """Each ``(cell, files)`` as a sibling folder of ``base``."""
    tree.dirs.add(base)
    for cell, files in cells:
        name = folder.claim(safe_name(cell.name, "Cell"), is_file=False)
        _add_cell(tree, f"{base}/{name}", _Folder(), files, snapshot.provenance.get(str(cell.id)))


def _add_stack(tree: _Builder, base: str, folder: _Folder, stack, snapshot: ExportSnapshot) -> None:
    """The stack's cells; the app's built-in cards never travel."""
    _add_cells(tree, base, folder, [(cell, _frozen_files(snapshot, cell)) for cell in stack.cells
                                    if not cell.builtin], snapshot)


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
    tree = _Builder()
    if scope == "cell":
        cell = next((c for s in stacks for c in s.cells if str(c.id) == str(cell_id)), None)
        if cell is None:
            raise LookupError("Cell not found")
        root = safe_name(cell.name, "Cell")
        _add_cell(tree, root, _Folder(), _frozen_files(snapshot, cell), snapshot.provenance.get(str(cell.id)))
        return tree.finish(root)
    if scope == "stack":
        stack = next((s for s in stacks if str(s.id) == str(stack_id)), None)
        if stack is None:
            raise LookupError("Stack not found")
        root = safe_name(*_stack_name(stack))
        _add_stack(tree, root, _Folder(), stack, snapshot)
        return tree.finish(root)
    if scope == "tag":
        from cell_tags import normalize_tag
        root = safe_name(normalize_tag(tag) or "", "Tag")
        live = [(cell, snapshot.files_by_cell[str(cell.id)]) for stack in stacks for cell in stack.cells
                if str(cell.id) in snapshot.files_by_cell]
        _add_cells(tree, root, _Folder(), live + [(item.cell, item.files) for item in snapshot.removed], snapshot)
        return tree.finish(root)
    root = safe_name(channel.name, "Channel")
    if scope == "packages":
        from cell_placement import capabilities_stack
        tree.dirs.add(root)
        _add_stack(tree, f"{root}/{_STACK_DEFAULTS['capabilities']}", _Folder(),
                   capabilities_stack(channel), snapshot)
        return tree.finish(root)
    if scope == "main":
        return _add_stacks(tree, root, [s for s in channel.main_stacks if s.stack_type == "standard"], snapshot)
    return _add_stacks(tree, root, stacks, snapshot)


def export_manifest(tree: ExportTree) -> dict:
    """The JSON shape of ``GET …/export/manifest``. Paths start with the
    ``root`` folder (``<cell>/…``, ``<stack>/…``, ``<tag>/…`` or
    ``<channel>/…``) and are relative to wherever the client puts that
    folder."""
    return {
        "root": tree.root,
        "entries": [{"path": e.path, "body_hash": e.body_hash, "size": e.size, "encoding": e.encoding}
                    for e in tree.entries],
        "dirs": list(tree.dirs),
    }


class _Cancelled(Exception):
    pass


class _QueueSink:
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

    The worker owns the snapshot's pins and releases them when it ends:
    finished, failed or cancelled by ``close()``. ``next_chunk`` returns the
    next piece, None at the end, or raises the worker's error.
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
            raise

    def _write(self) -> None:
        from cas import blob_path
        sink = _QueueSink(self._pieces, self._cancelled)
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
                    info.external_attr = 0o100644 << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    # Binary bodies are mostly compressed already.
                    info.compress_level = 1 if entry.encoding == "binary" else 6
                    # A known size lets the writer pick ZIP64 headers up front.
                    info.file_size = entry.size
                    with open(blob_path(entry.body_hash), "rb") as source, archive.open(info, "w") as target:
                        while chunk := source.read(CHUNK_BYTES):
                            target.write(chunk)
            sink.flush()
        except _Cancelled:
            pass
        except BaseException as exc:
            self._error = exc
        finally:
            self._snapshot.release()
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
