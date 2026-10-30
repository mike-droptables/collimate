"""Coherent frozen snapshots of a channel's accepted files and settings.

``export_snapshot`` freezes accepted SQL descriptors under the selected cells'
commit locks, pins their CAS bodies before the locks release and returns an
explicit :class:`ExportSnapshot`. ``freeze_tag_cells`` freezes every cell
carrying one tag the same way: the live layout cells and the removed cells,
whose files are their version tip. The snapshot travels as an object (a
streaming response's sync iterator runs each step in a fresh threadpool
context, so context-local state would be lost) and its pins hold until
``release()``.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import NamedTuple

from models import Cell, Channel


@dataclass(frozen=True)
class ExportFile:
    """A frozen accepted descriptor, deliberately without a body reader."""
    name: str
    language: str
    encoding: str
    body_hash: str


class RemovedExport(NamedTuple):
    """A frozen removed cell: the layout cell rebuilt from its record (no
    files; a run-path pin cleared), its tip's files without run or reserved
    paths, and the name of the stack it left."""
    cell: Cell
    files: list[ExportFile]
    stack_name: str | None


@dataclass(eq=False)
class ExportSnapshot:
    """Frozen layout, settings and accepted descriptors with a pins lease.

    ``channel`` is a detached copy of the layout whose cells carry no files;
    ``files_by_cell`` holds the frozen descriptors of the selected layout
    cells only and ``removed`` the selected removed cells (empty unless the
    freeze selected some). ``provenance`` maps each selected cell that has
    provenance records to the CAS hash of their export file
    (``provenance.export_body``). Use it as a context manager or call
    ``release()`` (idempotent, thread-safe) once the bodies have been read.
    """
    channel: Channel
    settings: dict
    files_by_cell: dict[str, list[ExportFile]]
    _lease: ExitStack = field(repr=False)
    removed: list[RemovedExport] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)
    _released: bool = field(default=False, repr=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def hashes(self) -> set[str]:
        frozen = [*self.files_by_cell.values(), *(item.files for item in self.removed)]
        return {f.body_hash for files in frozen for f in files} | set(self.provenance.values())

    def release(self) -> None:
        with self._guard:
            if self._released:
                return
            self._released = True
        self._lease.close()

    def __enter__(self) -> ExportSnapshot:
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def _layout_cell_ids(channel: Channel) -> list[str]:
    return sorted(str(cell.id) for stack in channel.main_stacks + channel.nav_stacks for cell in stack.cells)


def _carries(tags, key: str) -> bool:
    from cell_tags import union_tags
    return any(tag.casefold() == key for tag in union_tags([tags]))


def _live_tagged_ids(channel: Channel, key: str) -> list[str]:
    return sorted(str(cell.id) for stack in channel.main_stacks + channel.nav_stacks
                  for cell in stack.cells if _carries(cell.tags, key))


def _removed_tagged_ids(conn, live_ids: set[str], key: str) -> list[str]:
    import removed_cells
    return sorted(record.cell_id for record in removed_cells.list_removed(conn)
                  if record.cell_id not in live_ids and _carries(record.tags, key))


def _frozen_removed(conn, cell_id: str, channel_uuid: str) -> RemovedExport:
    import removed_cells
    from reserved_paths import is_reserved_path
    from run_folders import is_run_path
    from versions import VersionChainTooLong, find_tip, version_file_entries
    record = removed_cells.get(conn, cell_id)
    if record is None:
        raise ValueError('Channel layout changed during export; retry')
    try:
        entries = version_file_entries(conn, find_tip(conn, cell_id))
    except VersionChainTooLong as exc:
        raise ValueError(f'Cell {cell_id} history cannot be read') from exc
    cell = Cell.model_validate({**record.metadata, "id": cell_id, "name": record.name,
                                "files": [], "channel": channel_uuid})
    if is_run_path(cell.pinned or ""):
        cell.pinned = None
    files = [ExportFile(name=item['path'], language=item['language'], encoding=item['encoding'],
                        body_hash=item['body_hash'])
             for item in entries if not is_run_path(item['path']) and not is_reserved_path(item['path'])]
    return RemovedExport(cell, files, record.stack_name)


def _freeze(channel: Channel, live_ids: list[str], removed_ids: list[str],
            holds: Callable[[Channel, object], bool]) -> ExportSnapshot:
    """Freeze ``live_ids`` (layout cells) and ``removed_ids`` (removed
    records) under all their commit locks in one read transaction, after
    ``holds(layout copy, conn)`` confirms the selection still stands."""
    from cas import pin_blobs, store_bytes, verify_blob
    from cell_commits import cell_commit_locks
    from channel_settings import load_channel_settings
    from database import channel_transaction
    from persistence import snapshot_files
    from provenance import export_body, read as read_provenance
    from state import state_lock, channel_db_name
    lease = ExitStack()
    try:
        with cell_commit_locks([*live_ids, *removed_ids]):
            with state_lock:
                candidate = channel.model_copy(deep=True)
            files_by_cell: dict[str, list[ExportFile]] = {}
            chosen = set(live_ids)
            with channel_transaction(channel_db_name(channel), read_only=True) as conn:
                if not holds(candidate, conn):
                    raise ValueError('Channel layout changed during export; retry')
                settings = load_channel_settings(channel_db_name(channel), conn=conn)
                for stack in candidate.main_stacks + candidate.nav_stacks:
                    for cell in stack.cells:
                        had_files = bool(cell.files)
                        cell.files = []
                        if str(cell.id) not in chosen:
                            continue
                        descriptors = snapshot_files(conn, str(cell.id))
                        if had_files and not descriptors:
                            raise ValueError(f'Cell {cell.id} has no accepted file snapshot')
                        files_by_cell[str(cell.id)] = [
                            ExportFile(name=item['path'], language=item['language'],
                                       encoding=item['encoding'], body_hash=item['body_hash'])
                            for item in descriptors]
                removed = [_frozen_removed(conn, cell_id, channel_db_name(channel)) for cell_id in removed_ids]
                provenance = {cell_id: store_bytes(export_body(records)) for cell_id, records
                              in read_provenance(conn, [*files_by_cell, *removed_ids]).items()}
            snapshot = ExportSnapshot(candidate, settings, files_by_cell, lease, removed, provenance)
            lease.enter_context(pin_blobs(snapshot.hashes, verify=False))
        for digest in snapshot.hashes:
            verify_blob(digest)
    except BaseException:
        lease.close()
        raise
    return snapshot


def export_snapshot(channel: Channel, cell_ids=None) -> ExportSnapshot:
    """Freeze ``cell_ids`` (every layout cell when None) of ``channel``.

    Only the selected cells' commit locks are held, and only while their
    descriptors are read. Raises ``ValueError`` when a selected cell left the
    layout (or, for the whole channel, the layout's membership changed) during
    the freeze, and ``OSError`` when a pinned body is missing or corrupt.
    """
    from state import state_lock
    whole = cell_ids is None
    if whole:
        with state_lock:
            selected = _layout_cell_ids(channel)
    else:
        selected = sorted({str(cell_id) for cell_id in cell_ids})

    def holds(candidate: Channel, _conn) -> bool:
        present = _layout_cell_ids(candidate)
        return present == selected if whole else set(selected) <= set(present)

    return _freeze(channel, selected, [], holds)


def freeze_tag_cells(channel: Channel, tag: str) -> ExportSnapshot:
    """Freeze every cell of ``channel`` carrying ``tag`` (casefold, after
    ``cell_tags.normalize_tag``; a refused tag matches nothing): the live
    layout cells into ``files_by_cell`` and the removed records off the
    canvas into ``removed``, exactly the cells ``cell_tags.channel_tags`` counts for it. The removed
    cells' commit locks are held too, so a restore cannot interleave. Raises
    ``ValueError`` when the tagged set changed during the freeze and
    ``OSError`` when a pinned body is missing or corrupt.
    """
    from cell_tags import normalize_tag
    from database import get_db_connection
    from state import channel_db_name, state_lock
    # A refused tag matches nothing: no kept tag casefolds to ''.
    key = (normalize_tag(tag) or "").casefold()
    with state_lock:
        live_ids = _live_tagged_ids(channel, key)
        layout = set(_layout_cell_ids(channel))
    conn = get_db_connection(channel_db_name(channel))
    try:
        removed_ids = _removed_tagged_ids(conn, layout, key)
    finally:
        conn.close()

    def holds(candidate: Channel, conn) -> bool:
        return (_live_tagged_ids(candidate, key) == live_ids
                and _removed_tagged_ids(conn, set(_layout_cell_ids(candidate)), key) == removed_ids)

    return _freeze(channel, live_ids, removed_ids, holds)
