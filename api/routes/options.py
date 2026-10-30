"""Merge and Split: structural snapshots and channel-local identity restoration.

Validation reads accepted current files without writing anything. Cell locks
serialize file and lifecycle work; SQL runs outside the global state lock.
Provenance is the logical-cell lineage record (``cell_moves``): one 'merge'
row per input (``ord`` its position), one 'split' row per output. Inputs that
leave the canvas (Merge's, and Split's source without a remainder) go into
``removed_cells`` so the Removed shelf lists and restores them.
Merge's output carries the union of its inputs' tags; a Split output that
revives a removed cell's id takes that record's tags, a fresh one has none.
The SDK owns options parsing and path mapping; this module owns host policy.
"""
from __future__ import annotations

from cell_placement import is_capabilities_stack, iter_stacks

import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import removed_cells
from cas import read_blob, store_bytes, CorruptBlobError
from cell_lineage import announce_lineage
from cell_moves import record_move
from cell_tags import union_tags
from cell_commits import cell_commit_locks
from versions import insert_version, tip_file_map
from collimate.options_stack import (
    dedupe_folder,
    map_options_stack,
    parse_options_stack,
    read_options_stack,
    sanitize_folder,
)
from database import get_db_connection
from models import Cell, VirtualFile
from versions import find_tip, synthesize_genesis_row
from run_registry import running_cell_ids
from run_folders import is_run_path, strip_run_paths
from time_wire import now_iso

router = APIRouter()
logger = logging.getLogger(__name__)


class MergeRequest(BaseModel):
    stack_id: str


class SplitRequest(BaseModel):
    cell_id: str


@dataclass
class Snapshot:
    cell: Cell
    stamp: object
    tip: str
    revisions: dict
    hashes: dict[str, str]
    files: dict[str, VirtualFile]
    raw: dict[str, bytes]


def _candidates(conn, source: str) -> list[tuple[str, str, dict[str, str]]]:
    """Original logical inputs and their current accepted working files."""
    from persistence import snapshot_files
    return [(row[0], row[1], {f['path']: f['body_hash'] for f in snapshot_files(conn, row[0])
                             if not is_run_path(f['path'])})
            for row in conn.execute(
                "SELECT from_cell, from_version FROM cell_moves "
                "WHERE op='merge' AND to_cell=? AND from_version IS NOT NULL ORDER BY ord, id",
                (source,),
            )]


def _prepare_snapshot(conn, cell: Cell) -> Snapshot:
    """Lossless effective files, including tombstones; no CAS or SQL writes."""
    cid = str(cell.id)
    stamp = cell.updated_at
    from persistence import snapshot_files
    accepted=snapshot_files(conn,cid)
    revisions={f['path']:f['file_revision'] for f in snapshot_files(conn,cid,include_deleted=True)}
    files={}
    for entry in accepted:
        try:
            raw_body=read_blob(entry['body_hash'])
        except (OSError, CorruptBlobError) as exc:
            raise HTTPException(409, 'Unreadable accepted body') from exc
        if raw_body is None:
            raise HTTPException(409,'Missing accepted body')
        binary=entry['encoding']=='binary'
        files[entry['path']]=VirtualFile(name=entry['path'],blob_hash=entry['body_hash'] if binary else None,
                                       content='' if binary else raw_body.decode('utf-8'),language=entry['language'],
                                       encoding=entry['encoding'])
    hashes, raw = {}, {}
    for path, file in files.items():
        if file.blob_hash:
            content = read_blob(file.blob_hash)
            if content is None:
                raise HTTPException(409, f"stored content is missing for {path!r}")
            digest = hashlib.sha256(content).hexdigest()
            if digest != file.blob_hash:
                raise HTTPException(409, f"stored content is damaged for {path!r}")
        else:
            content = file.content.encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
        hashes[path] = digest
        raw[digest] = content
    return Snapshot(cell, stamp, find_tip(conn, cid), revisions, hashes, files, raw)


def _mapped_files(snapshot: Snapshot, mapping: dict[str, str]) -> list[VirtualFile]:
    return [snapshot.files[source].model_copy(update={"name": destination})
            for destination, source in mapping.items()]


def _entries(vfiles, hashes: dict[str, str]) -> list[dict]:
    return [{'path': f.name, 'body_hash': hashes[f.name],
             'encoding': 'binary' if f.blob_hash else 'utf-8', 'language': f.language} for f in vfiles]


def _insert_root(conn, *, cell_id: str, run_id: str, name: str,
                 files: dict[str, str], file_entries: list[dict], kind: str) -> str:
    seq = conn.execute("SELECT COALESCE(MAX(sequence_num),0)+1 FROM cells WHERE run_id=?", (run_id,)).fetchone()[0]
    conn.execute("INSERT INTO cells(id,run_id,generator_name,sequence_num,version_name,source_type) VALUES(?,?,?,?,?,?)",
                 (cell_id, run_id, kind, seq, name, kind))
    conn.executemany("INSERT INTO cell_files(cell_id,filepath,blob_hash,encoding,language) VALUES(?,?,?,?,?)",
                     [(cell_id, f['path'], f['body_hash'], f['encoding'], f['language']) for f in file_entries])
    from versions import initialize_file_snapshot
    initialize_file_snapshot(conn, cell_id)
    from persistence import initialize_files
    initialize_files(conn, cell_id, file_entries)
    conn.execute("INSERT INTO run_events(run_id,event_type,payload) VALUES(?,'commit',?)", (run_id, json.dumps({
        "cell_id": cell_id, "name": name, "sequence_num": seq, "file_count": len(files),
    })))
    return cell_id


def _materialize(conn, snapshot: Snapshot, run_id: str) -> str:
    """Materialize exactly the validated view, in the caller's transaction."""
    tip = snapshot.tip
    if conn.execute("SELECT 1 FROM cells WHERE id=?", (tip,)).fetchone() is None:
        seq = conn.execute("SELECT COALESCE(MAX(sequence_num),0)+1 FROM cells WHERE run_id=?", (run_id,)).fetchone()[0]
        synthesize_genesis_row(conn, tip, run_id, seq, _entries(snapshot.files.values(), snapshot.hashes))
    else:
        before = tip_file_map(conn, tip)
        from versions import version_file_entries
        retained = {f['path']: (f['body_hash'], f['encoding'], f['language'])
                    for f in version_file_entries(conn, tip)}
        entries = _entries(snapshot.files.values(), snapshot.hashes)
        desired = {f['path']: (f['body_hash'], f['encoding'], f['language']) for f in entries}
        if retained != desired:
            tip, _ = insert_version(conn, tip_id=tip, parent_files=before, owner_id=str(snapshot.cell.id),
                                    run_id=run_id, files=snapshot.hashes,
                                    file_entries=entries,
                                    version_name="Manual Edit", source_type="manual_edit",
                                    generator_name="manual_edit")
    return tip


def _guard(channel, ch, stack, snapshots: list[Snapshot], *, whole_stack: bool) -> None:
    from state import require_channel
    if require_channel(channel) is not ch or not any(s is stack for s in ch.main_stacks):
        raise HTTPException(409, "the canvas changed during the operation")
    if whole_stack and len(stack.cells) != len(snapshots):
        raise HTTPException(409, "the stack changed during the operation")
    for i, snapshot in enumerate(snapshots):
        cell = snapshot.cell
        live = stack.cells[i] if whole_stack else next((c for c in stack.cells if c.id == cell.id), None)
        if live is not cell or cell.updated_at != snapshot.stamp:
            raise HTTPException(409, "the cell changed during the operation")
    if any(str(s.cell.id) in running_cell_ids() for s in snapshots):
        raise HTTPException(409, "a run started on a cell during the operation")


def _begin(conn, snapshots: list[Snapshot]) -> None:
    # Some other writers apply memory under state_lock before committing. Never
    # wait for their SQLite lock while holding state_lock ourselves.
    conn.execute("PRAGMA busy_timeout=0")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise HTTPException(409, "the channel is being saved; retry the operation") from exc
        raise
    for snapshot in snapshots:
        cid = str(snapshot.cell.id)
        from persistence import snapshot_files
        if {f['path']:f['file_revision'] for f in snapshot_files(conn,cid,include_deleted=True)} != snapshot.revisions or find_tip(conn,cid) != snapshot.tip:
            raise HTTPException(409, "the cell changed during the operation")


def _guard_revisions(conn, snapshots, receipts=None):
    from persistence import snapshot_files
    for snapshot in snapshots:
        cell_id = str(snapshot.cell.id)
        expected = dict(snapshot.revisions)
        if receipts and cell_id in receipts:
            expected.update((f['path'], f['file_revision']) for f in receipts[cell_id]['files'])
        actual = {f['path']: f['file_revision'] for f in snapshot_files(conn, cell_id, include_deleted=True)}
        if actual != expected:
            raise HTTPException(409, "accepted files changed at the publication boundary")


def _remember_consumed(conn, snapshot: Snapshot, stack, stack_cell_count: int, reason: str) -> None:
    """Record a cell the operation takes off the canvas, in its transaction."""
    removed_cells.remember(
        conn, cell=snapshot.cell, stack_name=stack.name, stack_cell_count=stack_cell_count, removed_at=now_iso(), reason=reason,
    )


def _discard_records(conn, run_id: str, consumed: list[str],
                     revived: Sequence[removed_cells.RemovedCell] = ()) -> None:
    """The operation committed but did not land on the canvas: its inputs stay
    open and the cells it would have brought back stay removed, so drop the
    lineage rows and removal records it wrote and put back the ones it
    cleared. SQL runs outside state_lock."""
    from cell_display import write_display
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM cell_moves WHERE op_id=?", (run_id,))
        for cell_id in consumed:
            if record := removed_cells.get(conn, cell_id):
                write_display(conn, cell_id, record.display)
            removed_cells.forget(conn, cell_id)
        for record in revived:
            removed_cells.remember(
                conn, cell=Cell.model_validate(record.metadata), stack_name=record.stack_name,
                stack_cell_count=record.stack_cell_count,
                removed_at=record.removed_at, reason=record.reason,
                display=record.display,
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception("could not discard the records of operation %s", run_id)


def _store_snapshots(conn, snapshots: list[Snapshot], extra: bytes | None = None) -> None:
    raw = {h: value for snapshot in snapshots for h, value in snapshot.raw.items()}
    if extra is not None:
        raw[hashlib.sha256(extra).hexdigest()] = extra
    for digest, content in raw.items():
        if store_bytes(content) != digest:
            raise RuntimeError("CAS hash changed while storing snapshot")
        conn.execute("INSERT OR IGNORE INTO blobs(hash,size) VALUES(?,?)", (digest, len(content)))


def _open_channel(channel):
    from state import channel_db_name, require_channel
    ch = require_channel(channel)
    channel_uuid = channel_db_name(ch)
    try:
        conn = get_db_connection(channel_uuid, create=False)
    except FileNotFoundError as exc:
        raise HTTPException(404, "channel not found") from exc
    return ch, channel_uuid, conn


@router.get("/api/channels/{channel}/cells/{cell_id}/options-map")
def options_map(channel: str, cell_id: str, path: str,
                version_id: str | None = None) -> dict:
    """Read-only selected-definition mapping through the canonical SDK rules."""
    from cell_write_scope import cell_write_scope
    from package_approval import CellSnapshot, snapshot_cell
    from state import require_channel

    ch = require_channel(channel)
    with cell_write_scope(channel, [cell_id], hold_state=True):
        if version_id is None:
            snapshot = snapshot_cell(ch, cell_id)
        else:
            _, _, conn = _open_channel(channel)
            try:
                if not conn.execute("SELECT 1 FROM cells WHERE id=?", (version_id,)).fetchone():
                    raise HTTPException(404, "Cell snapshot not found")
                snapshot = CellSnapshot(cell_id, version_id,
                                        {p: ("blob", h) for p, h in tip_file_map(conn, version_id).items()}, {})
            finally:
                conn.close()
    if not path.endswith(".options_stack") or path not in snapshot.refs:
        raise HTTPException(404, "Options definition not found")
    selected = CellSnapshot(cell_id, snapshot.tip, {path: snapshot.refs[path]}, {})
    try:
        document = parse_options_stack(path, selected.bytes()[path])
        plan = map_options_stack(document, snapshot.refs,
                                 protected_paths={p for p in snapshot.refs if is_run_path(p)})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"options": plan.options, "kept": sorted(plan.kept),
            "retain_source": plan.retain_source}


@router.post("/api/channels/{channel}/merge")
def merge_stack(channel: str, body: MergeRequest) -> dict:
    from state import flush_persist_state, persist_state, state_lock
    from routes.layout import stripped_channel
    ch, channel_uuid, conn = _open_channel(channel)
    try:
        with state_lock:
            stack = next((s for s in ch.main_stacks if str(s.id) == body.stack_id), None)
            if stack is None:
                raise HTTPException(404, "stack not found")
            if is_capabilities_stack(stack):
                raise HTTPException(400, "Capabilities cells are not merged")
            cells = list(stack.cells)
            if len(cells) < 2:
                raise HTTPException(400, "merge needs a stack of 2 or more cells")
            if any(str(c.id) in running_cell_ids() for c in cells):
                raise HTTPException(409, "cannot merge a running cell")
        with cell_commit_locks(str(c.id) for c in cells):
            snapshots = [_prepare_snapshot(conn, cell) for cell in cells]
            folders, options, hashes, vfiles = {".options_stack"}, [], {}, []
            for snapshot in snapshots:
                cell = snapshot.cell
                folder = dedupe_folder(sanitize_folder(cell.name, f"cell-{str(cell.id)[:8]}"), folders)
                options.append({"name": cell.name, "root": folder, "files": ["**"]})
                mapping = {f"{folder}/{p}": p for p in strip_run_paths(snapshot.hashes)}
                hashes.update({p: snapshot.hashes[source] for p, source in mapping.items()})
                vfiles.extend(_mapped_files(snapshot, mapping))
            declaration = yaml.safe_dump({"options": options, "rest": "keep"}, sort_keys=False, allow_unicode=True)
            try:
                document = parse_options_stack(".options_stack", declaration)
                map_options_stack(document, [*hashes, ".options_stack"])
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            hashes[".options_stack"] = hashlib.sha256(declaration.encode()).hexdigest()
            vfiles.append(VirtualFile(name=".options_stack", content=declaration, language="yaml"))
            new_cell = Cell(name=stack.name or "Merged", files=vfiles, channel=channel_uuid,
                            tags=union_tags(s.cell.tags for s in snapshots))
            run_id = str(uuid.uuid4())
            with state_lock:
                _guard(channel, ch, stack, snapshots, whole_stack=True)
            from cell_metadata import advance_cell_metadata_generations
            advance_cell_metadata_generations(channel_uuid, [str(c.id) for c in cells])
            _begin(conn, snapshots)
            try:
                _store_snapshots(conn, snapshots, declaration.encode())
                conn.execute("INSERT INTO runs(id,prompt,status,generator_name) VALUES(?,?,'completed','merge')", (run_id, f"merge {len(cells)} cells"))
                tips = [_materialize(conn, snapshot, run_id) for snapshot in snapshots]
                merged = _insert_root(conn, cell_id=str(new_cell.id), run_id=run_id,
                                      name=new_cell.name, files=hashes,
                                      file_entries=_entries(vfiles, hashes), kind="merge")
                for i, (snapshot, tip) in enumerate(zip(snapshots, tips)):
                    record_move(conn, op="merge", op_id=run_id, from_cell=str(snapshot.cell.id),
                                from_version=tip, to_cell=merged, to_version=merged, ord=i)
                    _remember_consumed(conn, snapshot, stack, len(cells), "merged")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            consumed = [str(s.cell.id) for s in snapshots]
            try:
                _guard_revisions(conn, snapshots)
                with state_lock:
                    _guard(channel, ch, stack, snapshots, whole_stack=True)
                    stack.cells = [new_cell]
                    ch.main_selection_id = stack.id
            except HTTPException:
                _discard_records(conn, run_id, consumed)
                raise
            from persistence import snapshot_files, publish_files
            publish_files(channel_uuid, str(new_cell.id), {'operation_id': run_id, 'outcome': 'accepted',
                          'files': snapshot_files(conn, str(new_cell.id)), 'conflicts': []}, persist=False)
        persist_state()
        flush_persist_state()
        announce_lineage(channel, [*consumed, new_cell.id])
        return {"ok": True, "new_cell_id": str(new_cell.id), "new_stack_id": str(stack.id), "channel": stripped_channel(ch)}
    finally:
        conn.close()


@router.post("/api/channels/{channel}/split")
def split_cell(channel: str, body: SplitRequest) -> dict:
    from state import flush_persist_state, persist_state, state_lock
    from routes.layout import stripped_channel
    ch, channel_uuid, conn = _open_channel(channel)
    try:
        with state_lock:
            found = next(((s, c) for s in ch.main_stacks for c in s.cells if str(c.id) == body.cell_id), None)
            if found is None:
                raise HTTPException(404, "cell not found on the canvas")
            stack, source = found
            if is_capabilities_stack(stack):
                raise HTTPException(400, "Capabilities cells are not split")
            if body.cell_id in running_cell_ids():
                raise HTTPException(409, "cannot split a running cell")
        candidates = _candidates(conn, body.cell_id)
        with cell_commit_locks([body.cell_id, *(logical for logical, _, _ in candidates)]):
            snapshot = _prepare_snapshot(conn, source)
            protected = {p for p in snapshot.hashes if is_run_path(p)}
            try:
                document = read_options_stack(snapshot.hashes, lambda p: snapshot.raw[snapshot.hashes[p]], protected_paths=protected)
                if document is None:
                    raise ValueError("split requires an .options_stack file")
                plan = map_options_stack(document, snapshot.hashes, protected_paths=protected)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            option_hashes = [{p: snapshot.hashes[s] for p, s in option["files"].items()} for option in plan.options]
            outputs = [Cell(name=option["name"], files=_mapped_files(snapshot, option["files"]), channel=channel_uuid)
                       for option in plan.options]
            kept = {p: snapshot.hashes[p] for p in plan.kept}
            kept_files = _mapped_files(snapshot, {p: p for p in plan.kept})
            run_id = str(uuid.uuid4())
            with state_lock:
                _guard(channel, ch, stack, [snapshot], whole_stack=False)
                occupied = {str(c.id) for s in iter_stacks(ch) for c in s.cells}
            from cell_metadata import advance_cell_metadata_generations
            advance_cell_metadata_generations(channel_uuid, [body.cell_id, *(logical for logical, _, _ in candidates)])
            _begin(conn, [snapshot])
            receipts = {}
            revived = []
            try:
                claimed = set()
                restores = []
                from persistence import snapshot_files
                current_candidates = [(logical, {f['path']: f['body_hash'] for f in snapshot_files(conn, logical)
                                                   if not is_run_path(f['path'])})
                                      for logical, _, _ in candidates]
                for files in option_hashes:
                    choice = next(((logical, find_tip(conn, logical)) for logical, original in current_candidates
                                   if logical not in occupied and logical not in claimed and files == original), None)
                    restores.append(choice)
                    if choice:
                        claimed.add(choice[0])
                _store_snapshots(conn, [snapshot])
                conn.execute("INSERT INTO runs(id,prompt,status,generator_name) VALUES(?,?,'completed','split')", (run_id, f"split {len(outputs)} options"))
                source_tip = _materialize(conn, snapshot, run_id)
                from persistence import restore_snapshot, snapshot_files, publish_files
                from cell_display import write_display
                for i, (cell, files, restore) in enumerate(zip(outputs, option_hashes, restores)):
                    entries = _entries(cell.files, files)
                    if restore:
                        logical, tip = restore
                        version, _ = insert_version(conn, tip_id=tip, parent_files=tip_file_map(conn, tip), owner_id=logical,
                                                    run_id=run_id, files=files, file_entries=entries, version_name=cell.name,
                                                    source_type="split", generator_name="split")
                        cell.id = uuid.UUID(logical)
                        receipts[logical] = restore_snapshot(conn, logical, entries, writer_id='host:split')
                        if record := removed_cells.get(conn, logical):
                            cell.tags = record.tags
                            revived.append(record)
                            write_display(conn, logical, record.display)
                            removed_cells.forget(conn, logical)
                    else:
                        version = _insert_root(conn, cell_id=str(cell.id), run_id=run_id,
                                               name=cell.name, files=files, file_entries=entries, kind="split")
                        receipts[str(cell.id)] = {'operation_id': run_id, 'outcome': 'accepted',
                                                 'files': snapshot_files(conn, str(cell.id)), 'conflicts': []}
                    record_move(conn, op="split", op_id=run_id, from_cell=body.cell_id,
                                from_version=source_tip, to_cell=str(cell.id), to_version=version, ord=i)
                if plan.retain_source:
                    entries = _entries(kept_files, kept)
                    insert_version(conn, tip_id=source_tip, parent_files=snapshot.hashes, owner_id=str(snapshot.cell.id),
                                   run_id=run_id, files=kept, file_entries=entries, version_name="Split remainder",
                                   source_type="split", generator_name="split")
                    receipts[body.cell_id] = restore_snapshot(conn, body.cell_id, entries, writer_id='host:split')
                else:
                    _remember_consumed(conn, snapshot, stack, len(stack.cells), "split")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            consumed = [] if plan.retain_source else [body.cell_id]
            try:
                _guard_revisions(conn, [snapshot], receipts)
                with state_lock:
                    _guard(channel, ch, stack, [snapshot], whole_stack=False)
                    index = next(i for i, cell in enumerate(stack.cells) if cell is source)
                    if plan.retain_source:
                        from datetime import datetime
                        source.files = kept_files
                        source.updated_at = datetime.now()
                        if source.pinned not in kept:
                            source.pinned = None
                        stack.cells[index:index + 1] = [source, *outputs]
                    else:
                        stack.cells[index:index + 1] = outputs
                    ch.main_selection_id = stack.id
            except HTTPException:
                _discard_records(conn, run_id, consumed, revived)
                raise
            for cell_id, receipt in receipts.items():
                publish_files(channel_uuid, cell_id, receipt, persist=False)
        persist_state()
        flush_persist_state()
        announce_lineage(channel, [body.cell_id, *(c.id for c in outputs)])
        return {"ok": True, "new_cell_ids": [str(c.id) for c in outputs], "new_stack_id": str(stack.id), "channel": stripped_channel(ch)}
    finally:
        conn.close()
