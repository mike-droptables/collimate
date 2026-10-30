"""Cell metadata publication, atomically with rename history in app.db.

The fields are name, icon, settings, pinned and tags. The metadata generation
(``cell_metadata_heads``) signs every field but tags: a tag edit bumps the
cell's ``updated_at`` and nothing else, so it never supersedes a run's
pending name intent, records no rename and announces no lineage change.

Lock order is cell → layout persistence → short state snapshot. Channel file
transactions must be closed before entering this module. Display metadata has
its own channel KV owner and never passes through this publisher.
"""
from __future__ import annotations

import hashlib
import json
import logging
import copy
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from database import DurabilityConfirmationError, confirm_sql_durability, get_db_connection, get_global_db_connection
from time_wire import now_iso

_FIELDS = ("name", "icon", "settings", "pinned", "tags")
# The fields the metadata generation signs; tags are publishable but unsigned.
_SIGNED_FIELDS = ("name", "icon", "settings", "pinned")
_recovery_required: set[str] = set()


def _store_identity() -> str:
    return str(Path('.collimate/app.db').absolute())


def _repair_layout_cache() -> None:
    """Reconcile the exceptional committed SQL head while retaining live objects."""
    import state as state_module
    from models import AppState
    from genesis import reconcile_restored_cell_files
    with state_module.state_lock:
        existing_cell_ids = {cell.id for ch in state_module.state.channels
                             for stack in (*ch.main_stacks, *ch.nav_stacks) for cell in stack.cells}
    conn = get_global_db_connection()
    try:
        row = conn.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()
        if row is None:
            raise RuntimeError("Committed layout is unavailable for cache recovery")
        snapshot = json.loads(row[0])
    finally:
        conn.close()
    if snapshot.get('storage_format') != 'sql-layout-v2':
        raise ValueError("Committed layout must be sql-layout-v2")
    restored = AppState.model_validate(snapshot)
    # New identities need accepted files before becoming visible. Surviving
    # identities keep their existing accepted cache and physical object identity.
    new_channels = []
    for channel in restored.channels:
        placements = {kind: [stack.model_copy(update={'cells': [cell for cell in stack.cells
                      if cell.id not in existing_cell_ids]}) for stack in getattr(channel, kind)]
                      for kind in ('main_stacks', 'nav_stacks')}
        if any(stack.cells for stacks in placements.values() for stack in stacks):
            new_channels.append(channel.model_copy(update=placements))
    reconcile_restored_cell_files(AppState(channels=new_channels))
    with state_module.state_lock:
        live = state_module.state
        channels = {ch.id: ch for ch in live.channels}
        stacks = {stack.id: stack for ch in live.channels
                  for stack in (*ch.main_stacks, *ch.nav_stacks)}
        cells = {cell.id: cell for stack in stacks.values() for cell in stack.cells}
        repaired_channels = []
        for saved_channel in restored.channels:
            channel = channels.get(saved_channel.id, saved_channel)
            for field in type(saved_channel).model_fields:
                if field not in ('main_stacks', 'nav_stacks'):
                    setattr(channel, field, copy.deepcopy(getattr(saved_channel, field)))
            for placement in ('main_stacks', 'nav_stacks'):
                repaired_stacks = []
                for saved_stack in getattr(saved_channel, placement):
                    stack = stacks.get(saved_stack.id)
                    if stack is None:
                        stack = saved_stack
                    for field in type(saved_stack).model_fields:
                        if field != 'cells':
                            setattr(stack, field, copy.deepcopy(getattr(saved_stack, field)))
                    repaired_cells = []
                    for saved_cell in saved_stack.cells:
                        cell = cells.get(saved_cell.id, saved_cell)
                        for field in type(saved_cell).model_fields:
                            if field != 'files':
                                setattr(cell, field, copy.deepcopy(getattr(saved_cell, field)))
                        repaired_cells.append(cell)
                    stack.cells = repaired_cells
                    repaired_stacks.append(stack)
                setattr(channel, placement, repaired_stacks)
            repaired_channels.append(channel)
        live.channels = repaired_channels
        live.active_channel_id = restored.active_channel_id


def ensure_layout_cache_coherent() -> None:
    """Repair before a later snapshot; failure leaves publication blocked."""
    import state as state_module
    with state_module._persist_lock:
        identity = _store_identity()
        if identity in _recovery_required:
            _repair_layout_cache()
            _recovery_required.remove(identity)


@contextmanager
def layout_publication(conn):
    """A failed post-commit confirmation must not leave a stale layout cache."""
    if _store_identity() in _recovery_required:
        raise RuntimeError("Layout cache recovery is required before publication")
    try:
        with conn:
            yield conn
    except DurabilityConfirmationError:
        _recovery_required.add(_store_identity())
        try:
            ensure_layout_cache_coherent()
        except Exception:
            logging.getLogger(__name__).exception("Committed layout cache recovery failed")
        raise


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _signature(cell: dict | None) -> str:
    projection = None if cell is None else {field: cell.get(field) for field in _SIGNED_FIELDS}
    return hashlib.sha256(_json(projection).encode("utf-8")).hexdigest()


def _cells(snapshot: dict) -> dict[tuple[str, str], dict]:
    return {(ch["id"], cell["id"]): cell
            for ch in snapshot["channels"] for kind in ("main_stacks", "nav_stacks")
            for stack in ch[kind] for cell in stack["cells"]}


def write_layout_state(conn, snapshot: dict) -> None:
    """Stage layout and semantic generations on the caller's global transaction."""
    if snapshot.get("storage_format") != "sql-layout-v2":
        raise ValueError("Expected a sql-layout-v2 snapshot")
    if _store_identity() in _recovery_required:
        raise RuntimeError("Layout cache recovery is required before publication")
    current = _cells(snapshot)
    heads = {(row["channel_id"], row["cell_id"]): row["signature"] for row in conn.execute(
        "SELECT channel_id,cell_id,signature FROM cell_metadata_heads")}
    for channel_id, cell_id in current.keys() | heads.keys():
        signature = _signature(current.get((channel_id, cell_id)))
        if (channel_id, cell_id) not in heads:
            conn.execute("INSERT INTO cell_metadata_heads VALUES (?,?,1,?)",
                         (channel_id, cell_id, signature))
        elif heads[channel_id, cell_id] != signature:
            conn.execute("UPDATE cell_metadata_heads SET generation=generation+1,signature=? "
                         "WHERE channel_id=? AND cell_id=?", (signature, channel_id, cell_id))
    conn.execute("INSERT INTO ui_state(key,value) VALUES ('app_state',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_json(snapshot),))


def _capture(channel: str, cell_id: str):
    import state as state_module
    from cell_placement import find_cell
    from genesis import snapshot_app_state
    ensure_layout_cache_coherent()
    with state_module.state_lock:
        ch = state_module.require_channel(channel)
        _stack, cell = find_cell(ch, cell_id)
        return str(ch.id), cell, snapshot_app_state(state_module.state)


def _actor(source_type: str, actor: dict) -> str:
    expected = "runner" if source_type == "generator" else "user"
    if not source_type or not isinstance(actor, dict) or actor.get("kind") != expected:
        raise ValueError("Cell metadata requires an explicit actor matching its source type")
    return _json(actor)


def _stage(snapshot: dict, channel_id: str, cell_id: str, updates: dict):
    target = _cells(snapshot).get((channel_id, cell_id))
    if target is None:
        raise HTTPException(404, f"cell {cell_id} is not on a canvas")
    old_name = target["name"]
    changed = {key: value for key, value in updates.items() if target.get(key) != value}
    if changed:
        target.update(changed)
        target["updated_at"] = datetime.now().isoformat()
    return target, changed, old_name


def _event(conn, channel_id: str, cell_id: str, old_name: str,
           new_name: str, source_type: str, actor_json: str) -> str | None:
    if old_name == new_name:
        return None
    event_id = str(uuid4())
    conn.execute("INSERT INTO cell_rename_events VALUES (?,?,?,?,?,?,?,?)",
                 (event_id, channel_id, cell_id, now_iso(), old_name, new_name,
                  source_type, actor_json))
    return event_id


def _apply(cell, target: dict, changed: dict) -> None:
    import state as state_module
    if not changed:
        return
    with state_module.state_lock:
        for field in changed:
            setattr(cell, field, target[field])
        cell.updated_at = datetime.fromisoformat(target["updated_at"])


def _announce(channel: str, cell_id: str, renamed: bool) -> None:
    from state import broadcast_state_update
    from user_validation import notify_user_validation
    notify_user_validation()
    broadcast_state_update()
    if renamed:
        from cell_lineage import announce_lineage
        announce_lineage(channel, [cell_id])


def _public(cell) -> dict:
    return {"id": str(cell.id), **{field: getattr(cell, field) for field in _FIELDS},
            "updated_at": cell.updated_at.isoformat()}


def publish_cell_metadata(channel: str, cell_id: str, updates: dict, *,
                          source_type: str, actor: dict) -> dict:
    """Commit requested metadata and its rename event before exposing either.

    ``tags`` is the cell's full list, normalized by ``cell_tags.normalize_tags``
    (ValueError when it is not a valid list)."""
    import state as state_module
    from cell_commits import cell_commit_lock
    from cell_tags import normalize_tags
    if not updates or updates.keys() - set(_FIELDS):
        raise ValueError("Expected explicit cell metadata fields")
    if "name" in updates and not isinstance(updates["name"], str):
        raise ValueError("Cell name must be a string")
    if "tags" in updates:
        updates = {**updates, "tags": normalize_tags(updates["tags"])}
    actor_json = _actor(source_type, actor)
    with cell_commit_lock(cell_id), state_module._persist_lock:
        channel_id, cell, snapshot = _capture(channel, cell_id)
        target, changed, old_name = _stage(snapshot, channel_id, cell_id, updates)
        conn = get_global_db_connection()
        try:
            with layout_publication(conn):
                conn.execute("BEGIN IMMEDIATE")
                write_layout_state(conn, snapshot)
                event_id = _event(conn, channel_id, cell_id, old_name,
                                  target["name"], source_type, actor_json)
        finally:
            conn.close()
        _apply(cell, target, changed)
        result = _public(cell)
    _announce(channel_id, cell_id, event_id is not None)
    return result


def advance_cell_metadata_generations(channel: str, ids) -> None:
    """Invalidate pending metadata intents before a serialized lifecycle change.

    The caller holds these cells' locks, but no state lock or channel writer.
    It retains the cell locks through publication. Failed lifecycle work may
    leave only this conservative generation increment.
    """
    import state as state_module
    from genesis import snapshot_app_state
    ids = set(map(str, ids))
    with state_module._persist_lock:
        with state_module.state_lock:
            channel_id = str(state_module.require_channel(channel).id)
            current = _cells(snapshot_app_state(state_module.state))
        _advance_heads(channel_id, ids, current)


def _advance_heads(channel_id: str, ids, current: dict) -> None:
    conn = get_global_db_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for cell_id in set(map(str, ids)):
                conn.execute("INSERT INTO cell_metadata_heads VALUES (?,?,1,?) "
                             "ON CONFLICT(channel_id,cell_id) DO UPDATE SET generation=generation+1",
                             (channel_id, cell_id, _signature(current.get((channel_id, cell_id)))))
    finally:
        conn.close()


def advance_snapshot_generations(snapshot: dict, channel_id: str, ids) -> None:
    """Offline reset invalidation using its loaded authoritative layout.

    The reset owns the data-directory lock and relevant cell locks, and calls
    this before its channel transaction. It need not install the layout into
    a server's in-memory registry merely to advance durable generations.
    """
    import state as state_module
    if snapshot.get("storage_format") != "sql-layout-v2":
        raise ValueError("Expected a sql-layout-v2 snapshot")
    with state_module._persist_lock:
        _advance_heads(channel_id, ids, _cells(snapshot))


def _validate_intent(row, channel_id, cell_id, operation_id, digest, name, source_type, actor_json):
    expected = (channel_id, cell_id, digest, name, source_type, actor_json)
    actual = tuple(row[key] for key in ("channel_id", "cell_id", "request_digest",
                                       "requested_name", "source_type", "actor_json"))
    if actual != expected:
        raise ValueError(f"Metadata operation {operation_id} was reused with different input")


def prepare_cell_metadata(channel: str, cell_id: str, operation_id: str, digest: str,
                          name: str, *, source_type: str, actor: dict) -> None:
    """Persist an immutable name intent before the corresponding byte operation."""
    import state as state_module
    from cell_commits import cell_commit_lock
    if not isinstance(name, str):
        raise ValueError("Cell name must be a string")
    actor_json = _actor(source_type, actor)
    with cell_commit_lock(cell_id), state_module._persist_lock:
        channel_id, _cell, snapshot = _capture(channel, cell_id)
        conn = get_global_db_connection()
        try:
            with layout_publication(conn):
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM cell_metadata_operations WHERE operation_id=?",
                                   (operation_id,)).fetchone()
                if row is not None:
                    _validate_intent(row, channel_id, cell_id, operation_id, digest, name,
                                     source_type, actor_json)
                    return
                if (channel_id, cell_id) not in _cells(snapshot):
                    raise HTTPException(404, f"cell {cell_id} is not on a canvas")
                write_layout_state(conn, snapshot)
                generation = conn.execute("SELECT generation FROM cell_metadata_heads "
                                          "WHERE channel_id=? AND cell_id=?", (channel_id, cell_id)).fetchone()[0]
                conn.execute("INSERT INTO cell_metadata_operations VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                             (operation_id, channel_id, cell_id, digest, name, source_type,
                              actor_json, generation, "pending", now_iso()))
        finally:
            conn.close()


def finish_cell_metadata(channel: str, cell_id: str, operation_id: str, digest: str,
                         name: str, *, source_type: str, actor: dict) -> dict:
    """Finish or replay metadata only after verifying its exact durable file receipt."""
    import state as state_module
    from cell_commits import cell_commit_lock
    actor_json = _actor(source_type, actor)
    changed = {}
    event_id = None
    target = None
    with cell_commit_lock(cell_id), state_module._persist_lock:
        channel_id, cell, snapshot = _capture(channel, cell_id)
        conn = get_global_db_connection()
        try:
            row = conn.execute("SELECT * FROM cell_metadata_operations WHERE operation_id=?",
                               (operation_id,)).fetchone()
            if row is None:
                raise ValueError("Named file operation has no prepared metadata intent")
            _validate_intent(row, channel_id, cell_id, operation_id, digest, name, source_type, actor_json)
            if row["status"] != "pending":
                confirm_sql_durability(conn)
                return json.loads(row["result_json"])
        finally:
            conn.close()
        files = get_db_connection(channel_id, create=False)
        try:
            accepted = files.execute("SELECT cell_id,request_digest,result_json FROM file_operations "
                                     "WHERE operation_id=?", (operation_id,)).fetchone()
            if accepted is None:
                raise ValueError("Metadata operation has no durable file receipt")
            if accepted["cell_id"] != cell_id or accepted["request_digest"] != digest:
                raise ValueError("Metadata intent does not match the durable file receipt")
            receipt = json.loads(accepted["result_json"])
        finally:
            files.close()
        conn = get_global_db_connection()
        try:
            with layout_publication(conn):
                conn.execute("BEGIN IMMEDIATE")
                # Include unsaved semantic changes in generation comparison;
                # lifecycle ABA is additionally invalidated before channel SQL.
                write_layout_state(conn, snapshot)
                generation = conn.execute("SELECT generation FROM cell_metadata_heads "
                                          "WHERE channel_id=? AND cell_id=?", (channel_id, cell_id)).fetchone()
                latest = conn.execute("SELECT status,result_json FROM cell_metadata_operations "
                                      "WHERE operation_id=?", (operation_id,)).fetchone()
                if latest["status"] != "pending":
                    return json.loads(latest["result_json"])
                if receipt["outcome"] != "accepted":
                    status = "refused"
                elif cell is None or generation is None or generation[0] != row["expected_generation"]:
                    status = "superseded"
                else:
                    target, changed, old_name = _stage(snapshot, channel_id, cell_id, {"name": name})
                    status = "applied" if changed else "unchanged"
                    if changed:
                        write_layout_state(conn, snapshot)
                        event_id = _event(conn, channel_id, cell_id, old_name, name,
                                          source_type, actor_json)
                result = {"status": status, "event_id": event_id}
                conn.execute("UPDATE cell_metadata_operations SET status=?,result_json=? WHERE operation_id=?",
                             (status, _json(result), operation_id))
        finally:
            conn.close()
        if target is not None:
            _apply(cell, target, changed)
    if changed:
        _announce(channel_id, cell_id, event_id is not None)
    return result


def delete_channel_metadata(conn, channel_id: str) -> None:
    """Explicit channel deletion only; caller owns the global transaction."""
    for table in ("cell_metadata_operations", "cell_rename_events", "cell_metadata_heads"):
        conn.execute(f"DELETE FROM {table} WHERE channel_id=?", (channel_id,))


def remove_channel_layout(channel_id: str) -> None:
    """Commit explicit channel unregistration and its global metadata together."""
    import state as state_module
    from cell_commits import cell_commit_lock, cell_commit_locks
    from cell_placement import iter_stacks
    from genesis import snapshot_app_state
    # An empty channel has no cell lock to serialize its final import with
    # deletion. Imports share this narrow structural publication barrier.
    with cell_commit_lock('channel-publication:' + channel_id):
        ensure_layout_cache_coherent()
        with state_module.state_lock:
            channel = next((ch for ch in state_module.state.channels if str(ch.id) == channel_id), None)
            if channel is None:
                raise HTTPException(404, "Channel not found")
            ids = [str(cell.id) for stack in iter_stacks(channel) for cell in stack.cells]
        with cell_commit_locks(ids), state_module._persist_lock:
            with state_module.state_lock:
                if channel not in state_module.state.channels:
                    raise HTTPException(404, "Channel not found")
                if {str(cell.id) for stack in iter_stacks(channel) for cell in stack.cells} - set(ids):
                    raise HTTPException(409, "Channel changed while it was being deleted; try again")
                snapshot = snapshot_app_state(state_module.state)
            snapshot["channels"] = [ch for ch in snapshot["channels"] if ch["id"] != channel_id]
            if snapshot["active_channel_id"] == channel_id:
                snapshot["active_channel_id"] = snapshot["channels"][0]["id"] if snapshot["channels"] else None
            conn = get_global_db_connection()
            try:
                with layout_publication(conn):
                    conn.execute("BEGIN IMMEDIATE")
                    write_layout_state(conn, snapshot)
                    delete_channel_metadata(conn, channel_id)
            finally:
                conn.close()
            with state_module.state_lock:
                state_module.state.channels.remove(channel)
                if str(state_module.state.active_channel_id) == channel_id:
                    state_module.state.active_channel_id = (
                        state_module.state.channels[0].id if state_module.state.channels else None)
    from user_validation import notify_user_validation
    notify_user_validation()
    state_module.broadcast_state_update()


def recover_pending_cell_metadata() -> int:
    """Complete accepted run name intents after restoring the authoritative layout."""
    import state as state_module
    conn = get_global_db_connection()
    try:
        pending = [dict(row) for row in conn.execute(
            "SELECT * FROM cell_metadata_operations WHERE status='pending' ORDER BY created_at,operation_id")]
    finally:
        conn.close()
    recovered = 0
    with state_module.state_lock:
        channels = {str(ch.id) for ch in state_module.state.channels}
    for row in pending:
        if row["channel_id"] not in channels:
            continue  # A quarantined layout cannot authorize metadata publication.
        files = get_db_connection(row["channel_id"], create=False)
        try:
            exists = files.execute("SELECT 1 FROM file_operations WHERE operation_id=?",
                                   (row["operation_id"],)).fetchone()
        finally:
            files.close()
        if exists is None:
            continue  # The byte transaction never committed; an exact retry may.
        finish_cell_metadata(row["channel_id"], row["cell_id"], row["operation_id"],
                             row["request_digest"], row["requested_name"],
                             source_type=row["source_type"], actor=json.loads(row["actor_json"]))
        recovered += 1
    return recovered
