"""Channel layout and cell metadata publication, in the channel's own DB.

Each channel's ``channel.db`` holds its layout (``ui_state`` key ``layout``:
its stacks with their cells, without files, and the two selection ids) and
its cell metadata journal (``cell_metadata_heads``,
``cell_metadata_operations``, ``cell_rename_events``, channel migration 34),
which commit in one transaction with the layout row and the cell rows it
orders. ``app.db`` keeps only the active channel id (``genesis``).

The fields are name, icon, settings, pinned and tags. The metadata generation
(``cell_metadata_heads``) signs every field but tags: a tag edit bumps the
cell's ``updated_at`` and nothing else, so it never supersedes a run's
pending name intent, records no rename and announces no lineage change.

Lock order is cell → layout persistence → short state snapshot. Channel file
transactions must be closed before entering this module. Display metadata has
its own channel KV owner and never passes through this publisher.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException

from collimate_server.database import DurabilityConfirmationError, confirm_sql_durability, get_db_connection
from collimate_server.time_wire import now_iso

_FIELDS = ("name", "icon", "settings", "pinned", "tags")
# The fields the metadata generation signs; tags are publishable but unsigned.
_SIGNED_FIELDS = ("name", "icon", "settings", "pinned")

LAYOUT_KEY = "layout"
LAYOUT_FORMAT = "channel-layout-v1"

# Channels whose committed layout may be ahead of the in-memory cache (a
# post-commit durability confirmation failed): repaired before the next
# snapshot, publication refused until then.
_recovery_required: set[str] = set()
# The layout JSON last committed per channel.db path, so a persist writes only
# the channels whose layout changed.
_written: dict[str, str] = {}


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def layout_row(channel: dict) -> dict:
    """The ``layout`` row of one channel snapshot entry
    (``genesis.snapshot_channel``): everything but the name, which
    ``channel.json`` holds."""
    return {"format": LAYOUT_FORMAT, **{key: value for key, value in channel.items() if key != "name"}}


def read_layout(conn) -> dict | None:
    """The channel's committed layout row, or None when it has none.
    ValueError when the row is not a layout of this format."""
    row = conn.execute("SELECT value FROM ui_state WHERE key=?", (LAYOUT_KEY,)).fetchone()
    if row is None:
        return None
    layout = json.loads(row[0])
    if not isinstance(layout, dict) or layout.get("format") != LAYOUT_FORMAT:
        raise ValueError("its saved layout is not a channel-layout-v1 row")
    _written[_db_key(conn)] = _json(layout)
    layout = dict(layout)
    layout.pop("format")
    return layout


def _db_key(conn) -> str:
    path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    return os.path.abspath(path) if path else f"memory:{id(conn)}"


def _repair_layout_cache(channel_id: str) -> None:
    """Reconcile the exceptional committed layout of one channel with the
    live cache, keeping live objects."""
    import collimate_server.state as state_module
    from collimate_server.genesis import load_channel, reconcile_restored_cell_files
    with state_module.state_lock:
        live_channel = next((ch for ch in state_module.state.channels if str(ch.id) == channel_id), None)
        if live_channel is None:
            return
        existing_cell_ids = {cell.id for stack in (*live_channel.main_stacks, *live_channel.nav_stacks)
                             for cell in stack.cells}
    restored = load_channel(channel_id)
    # New identities need accepted files before becoming visible. Surviving
    # identities keep their existing accepted cache and physical object identity.
    fresh = restored.model_copy(update={kind: [stack.model_copy(update={'cells': [
        cell for cell in stack.cells if cell.id not in existing_cell_ids]}) for stack in getattr(restored, kind)]
        for kind in ('main_stacks', 'nav_stacks')})
    reconcile_restored_cell_files(fresh)
    fresh_cells = {cell.id: cell for stack in (*fresh.main_stacks, *fresh.nav_stacks) for cell in stack.cells}
    with state_module.state_lock:
        channel = live_channel
        stacks = {stack.id: stack for stack in (*channel.main_stacks, *channel.nav_stacks)}
        cells = {cell.id: cell for stack in stacks.values() for cell in stack.cells}
        cells.update({cell_id: cell for cell_id, cell in fresh_cells.items() if cell_id not in cells})
        for field in type(restored).model_fields:
            if field not in ('main_stacks', 'nav_stacks', 'name'):
                setattr(channel, field, copy.deepcopy(getattr(restored, field)))
        for placement in ('main_stacks', 'nav_stacks'):
            repaired_stacks = []
            for saved_stack in getattr(restored, placement):
                stack = stacks[saved_stack.id] if saved_stack.id in stacks else saved_stack
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


def ensure_layout_cache_coherent() -> None:
    """Repair before a later snapshot; failure leaves publication blocked."""
    import collimate_server.state as state_module
    with state_module._persist_lock:
        for channel_id in sorted(_recovery_required):
            _repair_layout_cache(channel_id)
            _recovery_required.discard(channel_id)


@contextmanager
def layout_publication(conn):
    """A failed post-commit confirmation must not leave a stale layout cache."""
    channel_id = conn.channel_id
    if channel_id in _recovery_required:
        raise RuntimeError("Layout cache recovery is required before publication")
    try:
        with conn:
            yield conn
    except DurabilityConfirmationError:
        _written.pop(_db_key(conn), None)
        _recovery_required.add(channel_id)
        try:
            ensure_layout_cache_coherent()
        except Exception:
            logging.getLogger(__name__).exception("Committed layout cache recovery failed")
        raise


def _signature(cell: dict | None) -> str:
    projection = None if cell is None else {field: cell.get(field) for field in _SIGNED_FIELDS}
    return hashlib.sha256(_json(projection).encode("utf-8")).hexdigest()


def _cells(channel: dict) -> dict[str, dict]:
    return {cell["id"]: cell for kind in ("main_stacks", "nav_stacks")
            for stack in channel[kind] for cell in stack["cells"]}


def write_layout(conn, channel: dict) -> str:
    """Stage the channel's layout and semantic generations on the caller's
    transaction in its channel.db; answers the row, for :func:`_remember`
    once the transaction committed."""
    if conn.channel_id in _recovery_required:
        raise RuntimeError("Layout cache recovery is required before publication")
    if channel["id"] != conn.channel_id:
        raise ValueError("A channel's layout belongs in its own database")
    current = _cells(channel)
    heads = {row["cell_id"]: row["signature"] for row in conn.execute(
        "SELECT cell_id,signature FROM cell_metadata_heads")}
    for cell_id in current.keys() | heads.keys():
        signature = _signature(current.get(cell_id))
        if cell_id not in heads:
            conn.execute("INSERT INTO cell_metadata_heads VALUES (?,1,?)", (cell_id, signature))
        elif heads[cell_id] != signature:
            conn.execute("UPDATE cell_metadata_heads SET generation=generation+1,signature=? "
                         "WHERE cell_id=?", (signature, cell_id))
    row = _json(layout_row(channel))
    conn.execute("INSERT INTO ui_state(key,value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (LAYOUT_KEY, row))
    return row


def _remember(conn, row: str | None) -> None:
    if row is not None:
        _written[_db_key(conn)] = row


def save_channel_layout(channel: dict) -> None:
    """Commit the channel's layout to its channel.db when it differs from
    the one last committed there."""
    conn = get_db_connection(channel["id"])
    try:
        key = _db_key(conn)
        if _written.get(key) == _json(layout_row(channel)):
            return
        with layout_publication(conn):
            conn.execute("BEGIN IMMEDIATE")
            row = write_layout(conn, channel)
        _remember(conn, row)
    finally:
        conn.close()


def _capture(channel: str, cell_id: str):
    import collimate_server.state as state_module
    from collimate_server.cell_placement import find_cell
    from collimate_server.genesis import snapshot_channel
    ensure_layout_cache_coherent()
    with state_module.state_lock:
        ch = state_module.require_channel(channel)
        _stack, cell = find_cell(ch, cell_id)
        return str(ch.id), cell, snapshot_channel(ch)


def _actor(source_type: str, actor: dict) -> str:
    expected = "runner" if source_type == "generator" else "user"
    if not source_type or not isinstance(actor, dict) or actor.get("kind") != expected:
        raise ValueError("Cell metadata requires an explicit actor matching its source type")
    return _json(actor)


def _stage(channel: dict, cell_id: str, updates: dict):
    target = _cells(channel).get(cell_id)
    if target is None:
        raise HTTPException(404, f"cell {cell_id} is not on a canvas")
    old_name = target["name"]
    changed = {key: value for key, value in updates.items() if target.get(key) != value}
    if changed:
        target.update(changed)
        target["updated_at"] = datetime.now().isoformat()
    return target, changed, old_name


def _event(conn, cell_id: str, old_name: str, new_name: str, source_type: str, actor_json: str) -> str | None:
    if old_name == new_name:
        return None
    event_id = str(uuid4())
    conn.execute("INSERT INTO cell_rename_events VALUES (?,?,?,?,?,?,?)",
                 (event_id, cell_id, now_iso(), old_name, new_name, source_type, actor_json))
    return event_id


def _apply(cell, target: dict, changed: dict) -> None:
    import collimate_server.state as state_module
    if not changed:
        return
    with state_module.state_lock:
        for field in changed:
            setattr(cell, field, target[field])
        cell.updated_at = datetime.fromisoformat(target["updated_at"])


def _announce(channel: str, cell_id: str, renamed: bool) -> None:
    from collimate_server.state import broadcast_state_update
    from collimate_server.user_validation import notify_user_validation
    notify_user_validation()
    broadcast_state_update()
    if renamed:
        from collimate_server.cell_lineage import announce_lineage
        announce_lineage(channel, [cell_id])


def _public(cell) -> dict:
    return {"id": str(cell.id), **{field: getattr(cell, field) for field in _FIELDS},
            "updated_at": cell.updated_at.isoformat()}


def publish_cell_metadata(channel: str, cell_id: str, updates: dict, *,
                          source_type: str, actor: dict) -> dict:
    """Commit requested metadata and its rename event before exposing either.

    ``tags`` is the cell's full list, normalized by ``cell_tags.normalize_tags``
    (ValueError when it is not a valid list)."""
    import collimate_server.state as state_module
    from collimate_server.cell_commits import cell_commit_lock
    from collimate_server.cell_tags import normalize_tags
    if not updates or updates.keys() - set(_FIELDS):
        raise ValueError("Expected explicit cell metadata fields")
    if "name" in updates and not isinstance(updates["name"], str):
        raise ValueError("Cell name must be a string")
    if "tags" in updates:
        updates = {**updates, "tags": normalize_tags(updates["tags"])}
    actor_json = _actor(source_type, actor)
    with cell_commit_lock(cell_id), state_module._persist_lock:
        channel_id, cell, snapshot = _capture(channel, cell_id)
        target, changed, old_name = _stage(snapshot, cell_id, updates)
        conn = get_db_connection(channel_id)
        try:
            with layout_publication(conn):
                conn.execute("BEGIN IMMEDIATE")
                row = write_layout(conn, snapshot)
                event_id = _event(conn, cell_id, old_name, target["name"], source_type, actor_json)
            _remember(conn, row)
        finally:
            conn.close()
        _apply(cell, target, changed)
        result = _public(cell)
    _announce(channel_id, cell_id, event_id is not None)
    return result


def advance_cell_metadata_generations(channel: str, ids, *, wait: bool = True) -> None:
    """Invalidate pending metadata intents before a serialized lifecycle change.

    The caller holds these cells' locks, but no state lock or channel writer.
    It retains the cell locks through publication. Failed lifecycle work may
    leave only this conservative generation increment. ``wait=False`` never
    waits for another writer of the channel's DB: a busy DB answers 409 retry
    (merge and split, which must not wait under their locks).
    """
    import sqlite3
    import collimate_server.state as state_module
    from collimate_server.genesis import snapshot_channel
    ids = set(map(str, ids))
    with state_module._persist_lock:
        with state_module.state_lock:
            ch = state_module.require_channel(channel)
            channel_id = str(ch.id)
            current = _cells(snapshot_channel(ch))
        conn = get_db_connection(channel_id)
        try:
            if not wait:
                conn.execute("PRAGMA busy_timeout=0")
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if not wait and getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise HTTPException(409, "the channel is being saved; retry the operation") from exc
                raise
            with conn:
                for cell_id in ids:
                    conn.execute("INSERT INTO cell_metadata_heads VALUES (?,1,?) "
                                 "ON CONFLICT(cell_id) DO UPDATE SET generation=generation+1",
                                 (cell_id, _signature(current.get(cell_id))))
        finally:
            conn.close()


def _validate_intent(row, cell_id, operation_id, digest, name, source_type, actor_json):
    expected = (cell_id, digest, name, source_type, actor_json)
    actual = tuple(row[key] for key in ("cell_id", "request_digest", "requested_name", "source_type", "actor_json"))
    if actual != expected:
        raise ValueError(f"Metadata operation {operation_id} was reused with different input")


def prepare_cell_metadata(channel: str, cell_id: str, operation_id: str, digest: str,
                          name: str, *, source_type: str, actor: dict) -> None:
    """Persist an immutable name intent before the corresponding byte operation."""
    import collimate_server.state as state_module
    from collimate_server.cell_commits import cell_commit_lock
    if not isinstance(name, str):
        raise ValueError("Cell name must be a string")
    actor_json = _actor(source_type, actor)
    with cell_commit_lock(cell_id), state_module._persist_lock:
        channel_id, _cell, snapshot = _capture(channel, cell_id)
        conn = get_db_connection(channel_id)
        try:
            with layout_publication(conn):
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM cell_metadata_operations WHERE operation_id=?",
                                   (operation_id,)).fetchone()
                if row is not None:
                    _validate_intent(row, cell_id, operation_id, digest, name, source_type, actor_json)
                    return
                if cell_id not in _cells(snapshot):
                    raise HTTPException(404, f"cell {cell_id} is not on a canvas")
                written = write_layout(conn, snapshot)
                generation = conn.execute("SELECT generation FROM cell_metadata_heads WHERE cell_id=?",
                                          (cell_id,)).fetchone()[0]
                conn.execute("INSERT INTO cell_metadata_operations VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                             (operation_id, cell_id, digest, name, source_type,
                              actor_json, generation, "pending", now_iso()))
            _remember(conn, written)
        finally:
            conn.close()


def finish_cell_metadata(channel: str, cell_id: str, operation_id: str, digest: str,
                         name: str, *, source_type: str, actor: dict) -> dict:
    """Finish or replay metadata only after verifying its exact durable file receipt."""
    import collimate_server.state as state_module
    from collimate_server.cell_commits import cell_commit_lock
    actor_json = _actor(source_type, actor)
    changed = {}
    event_id = None
    target = None
    with cell_commit_lock(cell_id), state_module._persist_lock:
        channel_id, cell, snapshot = _capture(channel, cell_id)
        conn = get_db_connection(channel_id)
        try:
            row = conn.execute("SELECT * FROM cell_metadata_operations WHERE operation_id=?",
                               (operation_id,)).fetchone()
            if row is None:
                raise ValueError("Named file operation has no prepared metadata intent")
            _validate_intent(row, cell_id, operation_id, digest, name, source_type, actor_json)
            if row["status"] != "pending":
                confirm_sql_durability(conn)
                return json.loads(row["result_json"])
            accepted = conn.execute("SELECT cell_id,request_digest,result_json FROM file_operations "
                                    "WHERE operation_id=?", (operation_id,)).fetchone()
            if accepted is None:
                raise ValueError("Metadata operation has no durable file receipt")
            if accepted["cell_id"] != cell_id or accepted["request_digest"] != digest:
                raise ValueError("Metadata intent does not match the durable file receipt")
            receipt = json.loads(accepted["result_json"])
            with layout_publication(conn):
                conn.execute("BEGIN IMMEDIATE")
                # Include unsaved semantic changes in generation comparison;
                # lifecycle ABA is additionally invalidated before channel SQL.
                written = write_layout(conn, snapshot)
                generation = conn.execute("SELECT generation FROM cell_metadata_heads WHERE cell_id=?",
                                          (cell_id,)).fetchone()
                latest = conn.execute("SELECT status,result_json FROM cell_metadata_operations "
                                      "WHERE operation_id=?", (operation_id,)).fetchone()
                if latest["status"] != "pending":
                    return json.loads(latest["result_json"])
                if receipt["outcome"] != "accepted":
                    status = "refused"
                elif cell is None or generation is None or generation[0] != row["expected_generation"]:
                    status = "superseded"
                else:
                    target, changed, old_name = _stage(snapshot, cell_id, {"name": name})
                    status = "applied" if changed else "unchanged"
                    if changed:
                        written = write_layout(conn, snapshot)
                        event_id = _event(conn, cell_id, old_name, name, source_type, actor_json)
                result = {"status": status, "event_id": event_id}
                conn.execute("UPDATE cell_metadata_operations SET status=?,result_json=? WHERE operation_id=?",
                             (status, _json(result), operation_id))
            _remember(conn, written)
        finally:
            conn.close()
        if target is not None:
            _apply(cell, target, changed)
    if changed:
        _announce(channel_id, cell_id, event_id is not None)
    return result


def unregister_channel(channel_id: str) -> None:
    """Take the channel out of the live catalog for its deletion, under every
    cell lock, and commit the new active channel id: the folder (with its
    layout and journal) goes after this."""
    import collimate_server.state as state_module
    from collimate_server.cell_commits import cell_commit_lock, cell_commit_locks
    from collimate_server.cell_placement import iter_stacks
    from collimate_server.genesis import save_active_channel_id, _saved_active
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
                active = state_module.state.active_channel_id
                if str(active) == channel_id:
                    from collimate_server.archive import successor
                    active = successor(state_module.state.channels, channel_id)
            save_active_channel_id(str(active) if active else None)
            _saved_active[os.getcwd()] = str(active) if active else None
            with state_module.state_lock:
                state_module.state.channels.remove(channel)
                state_module.state.active_channel_id = active
    from collimate_server.user_validation import notify_user_validation
    notify_user_validation()
    state_module.broadcast_state_update()


def forget_layout(channel_id: str) -> None:
    """Drop the channel's layout cache entries before its folder goes."""
    for key in [key for key in _written if os.path.basename(os.path.dirname(key)) in
                (channel_id, channel_id + ".moving")]:
        _written.pop(key, None)
    _recovery_required.discard(channel_id)


def recover_pending_cell_metadata() -> int:
    """Complete accepted run name intents after restoring the authoritative layouts."""
    import collimate_server.state as state_module
    with state_module.state_lock:
        channels = [str(ch.id) for ch in state_module.state.channels]
    recovered = 0
    for channel_id in channels:
        conn = get_db_connection(channel_id)
        try:
            pending = [dict(row) for row in conn.execute(
                "SELECT o.* FROM cell_metadata_operations o JOIN file_operations f USING (operation_id) "
                "WHERE o.status='pending' ORDER BY o.created_at,o.operation_id")]
        finally:
            conn.close()
        for row in pending:
            finish_cell_metadata(channel_id, row["cell_id"], row["operation_id"],
                                 row["request_digest"], row["requested_name"],
                                 source_type=row["source_type"], actor=json.loads(row["actor_json"]))
            recovered += 1
    return recovered
