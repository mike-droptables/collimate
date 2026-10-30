from __future__ import annotations

import logging
import os
import threading
import time
from uuid import UUID
from fastapi import HTTPException
from database import canonical_channel_id
from models import AppState, Channel

logger = logging.getLogger(__name__)

state = AppState()

current_dir = os.path.dirname(os.path.abspath(__file__))


# Serializes any mutate-then-broadcast sequence against concurrent
# writers. Several ProcessManagers commit to cells from worker threads (two
# runs on one cell, or live chats in several cells); without a lock one
# run's mutation of state.channels can interleave with another's model_dump
# and capture a half-and-half state, which then gets persisted and broadcast
# — cells flickering or reverting in the UI.
#
# Lock order: a cell's commit lock (cell_commits.cell_commit_lock) is taken
# BEFORE this lock and never while holding it, for every cell type.
#
# Reentrant because call sites that hold the lock for a mutation block call
# helpers that take it again.
#
# Rule for canvas-layout writes: a mutation of a channel's main_stacks /
# nav_stacks / stack.cells / main_selection_id holds state_lock across its
# lookup and mutation (so an index found under it is still valid when it is
# used), and persist_state() is called after releasing it. The layout
# handlers in routes/stacks.py and routes/cells.py follow it; some older
# paths (channel bootstrap, package and cell imports) do not yet.
state_lock = threading.RLock()

# Dump and save are one ordered operation. Lifespan writers may take state_lock
# inside this lock: mutations only mark dirty, and explicit barriers must run
# outside state locks. Synchronous publishers take a short state snapshot too.
_persist_lock = threading.RLock()
_persistence_writer: _PersistenceWriter | None = None


class _PersistenceWriter:
    """One trailing writer for the server lifespan, with an explicit barrier."""

    def __init__(self, delay: float):
        self.delay = delay
        self.condition = threading.Condition()
        self.dirty = 0
        self.saved = 0
        self.deadline: float | None = None
        self.closed = False
        self.thread = threading.Thread(target=self.run, name="app-state-writer", daemon=True)
        self.thread.start()

    def mark(self):
        with self.condition:
            if self.closed:
                return False
            self.dirty += 1
            self.deadline = time.monotonic() + self.delay
            self.condition.notify_all()
            return True

    def flush(self):
        # Only the worker and explicit barriers acquire this lock. A normal
        # mutation merely marks dirty, including callers holding state_lock.
        with _persist_lock:
            with self.condition:
                target = self.dirty
                if target <= self.saved:
                    return
            _persist_state_sync(broadcast=False)
            with self.condition:
                self.saved = target
                if self.dirty == target:
                    self.deadline = None
                self.condition.notify_all()

    def run(self):
        while True:
            with self.condition:
                while not self.closed:
                    if self.deadline is None:
                        self.condition.wait()
                    elif (remaining := self.deadline - time.monotonic()) > 0:
                        self.condition.wait(remaining)
                    else:
                        self.deadline = None
                        break
                if self.closed:
                    return
            try:
                self.flush()
            except Exception:
                # Dirty remains unsaved. A new mutation or explicit flush
                # retries, and a durability barrier propagates the failure.
                logger.exception("app state background save failed")


def start_persistence_writer(*, delay: float = 0.25) -> None:
    """Start after startup recovery; direct non-server users remain synchronous."""
    global _persistence_writer
    if _persistence_writer is None:
        _persistence_writer = _PersistenceWriter(delay)


def flush_persist_state() -> None:
    """Durability barrier. Call outside cell/state locks; async callers offload it."""
    from user_validation import notify_user_validation
    notify_user_validation()
    writer = _persistence_writer
    if writer is None:
        _persist_state_sync()
    else:
        writer.flush()


def stop_persistence_writer() -> None:
    """Flush final mutations and join the writer before releasing the data dir."""
    global _persistence_writer
    writer = _persistence_writer
    if writer is None:
        return
    try:
        while True:
            writer.flush()
            with writer.condition:
                if writer.saved == writer.dirty:
                    writer.closed = True
                    break
    finally:
        with writer.condition:
            writer.closed = True
            writer.condition.notify_all()
        writer.thread.join()
        _persistence_writer = None


def find_active_channel() -> Channel | None:
    """Return the active channel, or None when no current channel exists."""
    return next((channel for channel in state.channels if channel.id == state.active_channel_id), None)


def get_active_channel() -> Channel:
    if not state.active_channel_id:
        raise HTTPException(status_code=404, detail="No active channel")
    ch = find_active_channel()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")
    return ch

def channel_db_name(channel: Channel) -> str:
    """Return this channel's immutable UUID database identity."""
    return canonical_channel_id(channel.id)


def validate_channel_name(name: str) -> str:
    """Validate presentation metadata independently of database addressing."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(400, "Channel name cannot be empty")
    return cleaned


def get_channel_by_id(channel: str | UUID) -> Channel | None:
    """Nullable catalog lookup by canonical UUID only."""
    try:
        identity = canonical_channel_id(channel)
    except ValueError:
        return None
    channels = state.channels
    return next((item for item in channels if str(item.id) == identity), None)


def require_channel(channel: str | UUID) -> Channel:
    """Admit a live registered UUID; filenames alone confer no admission."""
    from database import changing_channels
    owner = get_channel_by_id(channel)
    if owner is None:
        raise HTTPException(404, f"No such channel: {channel}")
    if str(owner.id) in changing_channels:
        raise HTTPException(409, "Channel is being deleted")
    return owner


def persist_state():
    """Mark layout dirty and notify clients; explicit barriers provide durability."""
    from user_validation import notify_user_validation
    notify_user_validation()
    writer = _persistence_writer
    if writer is None:
        _persist_state_sync()
    else:
        if writer.mark():
            broadcast_state_update()
        else:
            _persist_state_sync()


def _persist_state_sync(*, broadcast: bool = True):
    """Order the snapshot and durable write as one persistence operation."""
    from genesis import save_global_ui_state, snapshot_app_state
    from cell_metadata import ensure_layout_cache_coherent
    with _persist_lock:
        ensure_layout_cache_coherent()
        with state_lock:
            state_dict = snapshot_app_state(state)
        save_global_ui_state(state_dict)
    if broadcast:
        broadcast_state_update()


def file_metadata_state(state_dict: dict) -> dict:
    """One body-free descriptor shape for every file size. A built-in card
    lists the build's files under its overlay rows
    (``builtin_packages.projection``), and its name and icon are the
    build's."""
    from builtin_packages import builtin_cell_id, builtin_cells, projection
    from database import get_db_connection
    from persistence import snapshot_files
    for ch in state_dict.get("channels", []):
        cards = {builtin_cell_id(ch["id"], spec.spec_id): spec for spec in builtin_cells()}
        conn = get_db_connection(ch["id"], create=False)
        try:
            for kind in ("main_stacks", "nav_stacks"):
                for stack in ch.get(kind, []):
                    for cell in stack.get("cells", []):
                        rows = snapshot_files(conn, cell["id"])
                        card = cards.get(cell["id"]) if cell.get("builtin") else None
                        if card is not None:
                            rows = projection(card, rows)
                            cell["name"], cell["icon"] = card.name, card.icon
                        cell["files"] = [{"name": f.pop("path"), **f} for f in rows]
        finally:
            conn.close()
    return state_dict


def _state_wire_snapshot() -> dict:
    from genesis import snapshot_app_state
    with state_lock:
        snapshot = snapshot_app_state(state)
    snapshot.pop("storage_format", None)
    return file_metadata_state(snapshot)


def scope_state_snapshot(snapshot: dict, channel: str | None = None) -> dict:
    """One full channel and metadata for others; absence still means removal."""
    if channel is None:
        scope_id = snapshot.get("active_channel_id")
    else:
        from database import canonical_channel_id
        channel_id = canonical_channel_id(channel)
        scope_id = next((c["id"] for c in snapshot["channels"]
                         if c["id"] == channel_id), None)
    return {"active_channel_id": snapshot.get("active_channel_id"),
            "channel_scope": scope_id,
            "channels": [c if c["id"] == scope_id else {"id": c["id"], "name": c["name"]}
                         for c in snapshot["channels"]]}


def build_state_snapshot() -> dict:
    """Complete active-channel state for HTTP bootstrap/manual refresh."""
    return scope_state_snapshot(_state_wire_snapshot())


def _build_channel_state_events(channels: list[str]) -> dict:
    snapshot = _state_wire_snapshot()
    return {channel: scope_state_snapshot(snapshot, channel) for channel in channels}


def state_patches(previous, current, path=None) -> list[dict]:
    """Diff immutable wire snapshots by logical identity, without copying files.

    Array offsets alone turn a one-file deletion into every later file changing.
    Tree/file members use stable selectors; only actual insertions/moves carry
    positions. The snapshots stay untouched while the worker builds patches.
    """
    path = [] if path is None else path
    if previous == current:
        return []
    patches = []
    if isinstance(previous, dict) and isinstance(current, dict):
        for key in previous.keys() - current.keys():
            patches.append({"op": "remove", "path": [*path, key]})
        for key, value in current.items():
            if key not in previous:
                patches.append({"op": "set", "path": [*path, key], "value": value})
            else:
                patches.extend(state_patches(previous[key], value, [*path, key]))
        return patches
    field = path[-1] if path and isinstance(path[-1], str) else None
    identity = "name" if field == "files" else "id" if field in {"channels", "main_stacks", "nav_stacks", "cells"} else None
    if identity and isinstance(previous, list) and isinstance(current, list):
        before = {item[identity]: item for item in previous}
        after = {item[identity]: item for item in current}
        if len(before) != len(previous) or len(after) != len(current):
            raise ValueError(f"duplicate {identity} in state array")
        def selector(key):
            return {"key": identity, "value": key}
        for key in before.keys() - after.keys():
            patches.append({"op": "remove", "path": [*path, selector(key)]})
        order = [key for key in before if key in after]
        # SQL-backed file maps can normalize an initially unsorted file list.
        # Encode that exact existing-member permutation once, rather than one
        # move per unchanged file. Other arrays and arbitrary orders keep moves.
        target_existing = [key for key in after if key in before]
        if field == "files" and order != target_existing and target_existing == sorted(order):
            patches.append({"op": "sort", "path": path})
            order.sort()
        for index, (key, item) in enumerate(after.items()):
            member = [*path, selector(key)]
            if key not in before:
                patches.append({"op": "insert", "path": member, "index": index, "value": item})
                order.insert(index, key)
            else:
                if order[index] != key:
                    order.remove(key)
                    order.insert(index, key)
                    patches.append({"op": "move", "path": member, "index": index})
                if field == "files" and before[key] != item:
                    patches.append({"op": "set", "path": member, "value": item})
                else:
                    patches.extend(state_patches(before[key], item, member))
        return patches
    return [{"op": "set", "path": path, "value": current}]


def broadcast_state_update():
    """Schedule one shared snapshot diff for the subscribed channel streams."""
    try:
        from events import broadcast_coalesced
        broadcast_coalesced(_build_channel_state_events)
    except Exception:
        logger.exception("state broadcast scheduling failed")
