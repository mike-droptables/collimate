"""Removed cells: record them on removal, list the Removed shelf, restore
one under its own id without copying its version history.

Every removed record is a shelf record and restores to the board, in a new
standard stack. Capability cells are never removed (they move off
Capabilities first), so a record never names the stack it came back to."""
from __future__ import annotations

from cell_placement import iter_stacks

from uuid import uuid4

from fastapi import HTTPException

import removed_cells
from run_folders import is_run_path, strip_run_paths
from time_wire import now_iso
from time_wire import iso
from versions import tip_file_map


def _resolve_channel(channel):
    from state import channel_db_name, require_channel
    ch = require_channel(channel)
    return ch, channel_db_name(ch)


def remember_removed_cells(channel, stack, cells, *, metadata_overrides=None):
    """Persist files and display metadata before unmounting; errors stop removal.

    ``metadata_overrides`` (a remove zone's resulting ``tags``) replace those
    keys in every record's ``metadata_json``; the live cells are untouched.

    Accepted current files are checkpointed before removal tombstones are written.
    Never-versioned cells receive a complete genesis row.
    The caller holds every cell's commit lock through this operation and
    memory publication, without holding state_lock during SQL. Each cell gets a
    ``removed_cells`` row ('removed', with the stack's size at removal).
    """
    from database import get_db_connection
    from cas import require_blob, CorruptBlobError
    from persistence import snapshot_files, restore_snapshot
    from versions import find_tip, synthesize_genesis_row, insert_version, version_file_entries

    stack_cell_count = len(stack.cells)
    removed_at = now_iso()
    conn = get_db_connection(channel)
    try:
        conn.execute("BEGIN IMMEDIATE")
        run_id = str(uuid4())
        conn.execute("INSERT INTO runs(id,prompt,status) VALUES(?, 'remove cell', 'completed')", (run_id,))
        for cell in cells:
            cell_id = str(cell.id)
            current = {entry['path']: entry for entry in snapshot_files(conn, cell_id)
                       if not is_run_path(entry['path'])}
            hashes = {}
            for name, entry in current.items():
                blob_hash = entry['body_hash']
                raw = require_blob(blob_hash)
                conn.execute("INSERT OR IGNORE INTO blobs(hash,size) VALUES(?,?)", (blob_hash, len(raw)))
                hashes[name] = blob_hash
            tip = find_tip(conn, cell_id)
            seq = conn.execute("SELECT COALESCE(MAX(sequence_num),0)+1 FROM cells WHERE run_id=?", (run_id,)).fetchone()[0]
            synthesize_genesis_row(conn, tip, run_id, seq, list(current.values()))
            before = tip_file_map(conn, tip)
            retained = {f['path']: (f['body_hash'], f['encoding'], f['language'])
                        for f in version_file_entries(conn, tip)}
            desired = {p: (f['body_hash'], f['encoding'], f['language']) for p, f in current.items()}
            if retained != desired:
                insert_version(conn, tip_id=tip, parent_files=before, owner_id=cell_id,
                               run_id=run_id, files=hashes, file_entries=current.values(),
                               version_name="Manual Edit", source_type="manual_edit",
                               generator_name="manual_edit")
            restore_snapshot(conn, cell_id, [], writer_id='host:remove')
            removed_cells.remember(
                conn, cell=cell, stack_name=stack.name, stack_cell_count=stack_cell_count, removed_at=removed_at, reason="removed",
                metadata_overrides=metadata_overrides,
            )
        conn.commit()
    except (OSError, CorruptBlobError) as exc:
        conn.rollback()
        raise HTTPException(409, "Stored removal content cannot be read") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


SORTS = ("created", "edited", "removed")
_SORT_KEYS = {"created": "created_at", "edited": "edited_at", "removed": "removed_at"}
DIRECTIONS = ("asc", "desc")
PREVIEW_BYTES = 4096
FILES_CAP = 200
# How many files a preview may open looking for text before giving up.
_PREVIEW_PROBES = 8


def list_removed_cells(channel, *, sort="edited", direction="desc", query=""):
    """The Removed shelf: one entry per ``removed_cells`` record whose cell is
    off the canvas, from the record, the cell's tip file map and its root
    version. Never walks other cells' versions or files.

    Each entry: ``cell_id, tip_version_id, name, icon, stack_name,
    stack_cell_count, reason, created_at, edited_at, removed_at, made_by
    {kind, runner_id, runner_name}, tags, files, file_count, preview {path,
    text}`` (``tags`` as recorded at removal; ``files`` sorted and capped at
    ``FILES_CAP``; the preview is the pinned file, else the first text file,
    at most ``PREVIEW_BYTES`` bytes; None when the cell has no text file).
    Every time is UTC ISO with an offset; ``removed_at`` is restamped on every
    removal, so it is the last-removed time. ``query`` terms must all match
    the name, the author, a tag, a file name or the preview text,
    case-insensitively. Sorted by ``sort`` ('created' | 'edited' | 'removed')
    in ``direction`` ('asc' | 'desc'), ties by cell id.
    """
    from cell_lineage import live_cells
    from channel_db import open_channel_db
    from state import state_lock
    from versions import find_tip

    if sort not in SORTS or direction not in DIRECTIONS:
        raise HTTPException(422, f"sort must be one of {SORTS} and dir one of {DIRECTIONS}")
    with state_lock:
        ch, _ = _resolve_channel(channel)
    live = live_cells(ch)
    conn = open_channel_db(channel)
    try:
        records = [r for r in removed_cells.list_removed(conn) if r.cell_id not in live]
        tips = {r.cell_id: find_tip(conn, r.cell_id) for r in records}
        made_by = _made_by(conn, [r.cell_id for r in records])
        times = _created_at(conn, [*tips.values(), *tips])
        terms = query.casefold().split()
        entries = []
        for record in records:
            tip = tips[record.cell_id]
            files = sorted(p for p in tip_file_map(conn, tip) if not is_run_path(p))
            maker = made_by[record.cell_id]
            preview = _preview(conn, tip, files, record.pinned)
            if terms:
                author = ((maker["runner_name"] or maker["runner_id"] or "")
                          if maker["kind"] == "runner" else "you")
                haystack = "\n".join([record.name, author, *record.tags, *files,
                                      preview["text"] if preview else ""]).casefold()
                if not all(term in haystack for term in terms):
                    continue
            entries.append({
                "cell_id": record.cell_id, "tip_version_id": tip if tip in times else None,
                "name": record.name, "icon": record.icon, "stack_name": record.stack_name,
                "stack_cell_count": record.stack_cell_count, "reason": record.reason,
                "created_at": record.created_at or times.get(record.cell_id),
                "edited_at": record.updated_at or times.get(tip),
                "removed_at": record.removed_at, "made_by": maker, "tags": record.tags,
                "files": files[:FILES_CAP], "file_count": len(files), "preview": preview,
            })
    finally:
        conn.close()
    key = _SORT_KEYS[sort]
    entries.sort(key=lambda e: e["cell_id"])
    entries.sort(key=lambda e: e[key] or "", reverse=direction == "desc")
    return entries


def _created_at(conn, version_ids):
    """``{version_id: created_at}`` (wire form) for the versions that exist."""
    ids = sorted(set(version_ids))
    out = {}
    for start in range(0, len(ids), 450):
        chunk = ids[start:start + 450]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT id, created_at FROM cells WHERE id IN ({marks})", chunk):
            out[row[0]] = iso(row[1])
    return out


def _made_by(conn, cell_ids):
    """Who made each cell: the runner of a run in a new cell that made it
    (``cell_moves`` op 'run_cell'), else its root version's ``source_type``
    — a generator root is its runner's, anything else the user's. Never
    decided by ``run_id`` alone: a never-versioned cell's genesis row is
    filed under the first run that started on it."""
    from cell_lineage import runner_actor, runners_of, user_actor

    ids = list(cell_ids)
    made_by_run = {}
    for start in range(0, len(ids), 450):
        chunk = ids[start:start + 450]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT to_cell, op_id FROM cell_moves WHERE op='run_cell' AND to_cell IN ({marks}) "
            f"ORDER BY id", chunk,
        ):
            made_by_run.setdefault(row[0], row[1])
        for row in conn.execute(
            f"SELECT v.version_id, c.source_type, c.run_id FROM cell_file_versions v "
            f"JOIN cells c ON c.id = v.root_id WHERE v.version_id IN ({marks})", chunk,
        ):
            if row[1] == "generator" and row[2]:
                made_by_run.setdefault(row[0], row[2])
    runners = runners_of(conn, made_by_run.values())
    return {cid: runner_actor(runners.get(made_by_run[cid])) if cid in made_by_run else user_actor()
            for cid in ids}


def _preview(conn, tip, files, pinned):
    """``{path, text}``: the head of the pinned file, else of the first file
    that reads as text, through the tip's file map. At most
    ``PREVIEW_BYTES`` bytes are read from a blob, a cut character is dropped."""
    from versions import version_file_entries

    order = ([pinned] if pinned in files else []) + [p for p in files if p != pinned]
    entries = {f['path']: f for f in version_file_entries(conn, tip)}
    order = [p for p in order if entries[p]['encoding'] == 'utf-8'][:_PREVIEW_PROBES]
    if not order:
        return None
    for path in order:
        text = _head_text(entries[path]['body_hash'])
        if text is not None:
            return {"path": path, "text": text}
    return None


def _head_text(blob_hash):
    import codecs
    from cas import blob_path, is_cas_hash
    if not blob_hash or not is_cas_hash(blob_hash):
        return None
    try:
        with open(blob_path(blob_hash), "rb") as fh:
            head = fh.read(PREVIEW_BYTES)
    except OSError:
        return None
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return None
    return None if "\x00" in text else text


def _restore_files(conn, cell_id, tip):
    from cas import CorruptBlobError
    from versions import build_virtual_files, version_file_entries
    try:
        return build_virtual_files(version_file_entries(conn, tip))
    except (OSError, CorruptBlobError, UnicodeDecodeError) as exc:
        raise HTTPException(409, "Stored restoration content cannot be read") from exc


def _reinstate(channel, ch, channel_uuid, conn, cell_id, record, files):
    """Commit the removed ``record``'s cell back under its own id with
    ``files``, in a new single-cell stack at the right end of the board, and
    select it. Metadata is the record's; the record is deleted. The caller
    holds the cell's commit lock. Returns the new stack."""
    from cell_display import write_display
    from models import Cell, Stack
    from persistence import publish_files, restore_snapshot
    from state import require_channel, state_lock

    metadata = {**record.metadata, "id": cell_id, "name": record.name, "files": files, "channel": channel_uuid}
    if is_run_path(metadata.get("pinned") or ""):
        metadata["pinned"] = None
    cell = Cell.model_validate(metadata)
    with state_lock:
        if require_channel(channel) is not ch:
            raise HTTPException(409, "Channel changed while restoring the cell; retry")
        if any(str(c.id) == cell_id for s in iter_stacks(ch) for c in s.cells):
            raise HTTPException(409, "Cell is already on the canvas")
    conn.execute("BEGIN IMMEDIATE")
    receipt = restore_snapshot(conn, cell_id, files)
    write_display(conn, cell_id, record.display)
    removed_cells.forget(conn, cell_id)
    conn.commit()
    with state_lock:
        stack = Stack(cells=[cell])
        ch.main_stacks.append(stack)
        ch.main_selection_id = stack.id
    publish_files(channel_uuid, cell_id, receipt, persist=False)
    return stack


def _removed_record(conn, ch, cell_id):
    """The removed record of ``cell_id`` with its retained files; 409 while
    it is on the canvas, 404 for no such record."""
    from cell_commits import sweep_dead_run_folders
    from cell_metadata import advance_cell_metadata_generations
    from state import channel_db_name, state_lock
    from versions import find_tip

    with state_lock:
        if any(str(c.id) == cell_id for s in iter_stacks(ch) for c in s.cells):
            raise HTTPException(409, "Cell is already on the canvas")
    record = removed_cells.get(conn, cell_id)
    if record is None:
        raise HTTPException(404, "No removed cell with this id in this channel")
    channel_uuid = channel_db_name(ch)
    advance_cell_metadata_generations(channel_uuid, [cell_id])
    sweep_dead_run_folders(channel_uuid, cell_id)
    return record, strip_run_paths(_restore_files(conn, cell_id, find_tip(conn, cell_id)))


def restore_cell(channel, cell_id):
    """Put the removed cell ``cell_id`` back on the canvas under its own id,
    in a new single-cell stack at the right end of the board (the shelf has
    no anchor), and select it.

    Files are its retained removal checkpoint, restored as new revisions;
    metadata (name, icon, settings, pinned, tags, created/updated times) is
    the record's. Dead run folders are swept first. 409 when the cell is in the
    layout, 404 when it has no removed record. The record is deleted. Emits
    ``lineage_changed``.
    """
    from cell_commits import cell_commit_lock
    from cell_lineage import announce_lineage
    from channel_db import open_channel_db
    from routes.layout import stripped_channel
    from state import flush_persist_state, persist_state, state_lock

    cell_id = str(cell_id)
    with state_lock:
        ch, channel_uuid = _resolve_channel(channel)
    conn = open_channel_db(channel)
    try:
        # Serialize concurrent restores and version writes of this cell while
        # keeping history and CAS reads outside the global lock.
        with cell_commit_lock(cell_id):
            record, files = _removed_record(conn, ch, cell_id)
            stack = _reinstate(channel, ch, channel_uuid, conn, cell_id, record, files)
    finally:
        conn.close()
    persist_state()
    flush_persist_state()
    announce_lineage(channel, [cell_id])
    return {"ok": True, "cell_id": cell_id, "stack_id": str(stack.id), "channel": stripped_channel(ch)}


def set_removed_cell_tags(channel, cell_id, tags):
    """Make ``tags`` the whole tag list of the shelf's removed cell
    ``cell_id`` (its record's ``metadata_json``, what restore gives back),
    normalized by ``cell_tags.normalize_tags``. 400 for an invalid list, 409
    while the cell is on the canvas, 404 without a record. Held under
    the cell's commit lock, as removal and restore are. Emits
    ``lineage_changed`` (the shelf's change event). Returns ``{cell_id,
    tags}``."""
    from cell_commits import cell_commit_lock
    from cell_lineage import announce_lineage
    from cell_tags import normalize_tags
    from channel_db import open_channel_db
    from state import state_lock

    try:
        tags = normalize_tags(tags)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cell_id = str(cell_id)
    with state_lock:
        ch, _ = _resolve_channel(channel)
    conn = open_channel_db(channel)
    try:
        with cell_commit_lock(cell_id):
            with state_lock:
                if any(str(c.id) == cell_id for s in iter_stacks(ch) for c in s.cells):
                    raise HTTPException(409, "Cell is on the canvas: edit its tags there")
            if not removed_cells.set_tags(conn, cell_id, tags):
                raise HTTPException(404, "No removed cell with this id in this channel")
            conn.commit()
    finally:
        conn.close()
    announce_lineage(channel, [cell_id])
    return {"cell_id": cell_id, "tags": tags}
