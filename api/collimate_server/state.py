from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Collection
from functools import partial
from uuid import UUID
from fastapi import HTTPException
from collimate_server.database import canonical_channel_id
from collimate_server.models import AppState, Channel

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
    from collimate_server.user_validation import notify_user_validation
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
    """Admit a live registered UUID; filenames alone confer no admission. An
    indexed channel that is unavailable (its layout did not validate, its
    folder went) raises ``channel_paths.ChannelUnavailable`` (409)."""
    from collimate_server.database import changing_channels
    owner = get_channel_by_id(channel)
    if owner is None:
        import collimate_server.channel_paths as channel_paths
        found = None
        try:
            found = channel_paths.home(channel)
        except ValueError:
            pass
        if found is not None and found.state == "unavailable":
            raise channel_paths.ChannelUnavailable(found.channel_id, found.reason or "unavailable")
        raise HTTPException(404, f"No such channel: {channel}")
    if str(owner.id) in changing_channels:
        from collimate_server.channel_guard import is_moving
        # A moving channel is read from its source until the switch; the
        # guard (channel_guard) refuses its writes.
        if not is_moving(str(owner.id)):
            raise HTTPException(409, "Channel is being deleted")
    return owner


def persist_state():
    """Mark layout dirty and notify clients; explicit barriers provide durability."""
    from collimate_server.user_validation import notify_user_validation
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
    from collimate_server.genesis import save_app_state, snapshot_app_state
    from collimate_server.cell_metadata import ensure_layout_cache_coherent
    with _persist_lock:
        ensure_layout_cache_coherent()
        with state_lock:
            state_dict = snapshot_app_state(state)
        save_app_state(state_dict)
    if broadcast:
        broadcast_state_update()


def file_metadata_state(state_dict: dict, *, measured: Collection[str] = ()) -> dict:
    """One body-free descriptor shape for every file size. A built-in card
    lists the build's files under its overlay rows
    (``builtin_packages.projection``), and its name and icon are the
    build's. Every cell lists its tip's repositories
    (``repository_rows.snapshot_entries``: ``[{path, branch, upstream,
    ahead, behind, detached, error?}]``, a rescue's ``error`` from
    ``git_store.rescues_of``, a failed materialize's from
    ``git_harvest.materialize_errors``), and ``repositories_overflow`` counts those
    past the per-cell cap when there are any.

    ``ahead``/``behind`` come only from ``git_store``'s cache: the channels
    in ``measured`` (those sent in full; the caller is off the event loop)
    count a missing entry in the channel store, the others read it as it is."""
    import collimate_server.git_harvest as git_harvest
    import collimate_server.git_store as git_store
    from collimate_server.builtin_packages import builtin_cell_id, builtin_cells, projection
    from collimate_server.database import get_db_connection
    from collimate_server.persistence import snapshot_files
    from collimate_server.repository_rows import snapshot_entries
    from collimate_server.versions import find_tip
    for ch in state_dict.get("channels", []):
        cards = {builtin_cell_id(ch["id"], spec.spec_id): spec for spec in builtin_cells()}
        counts = partial(git_store.ahead_behind if ch["id"] in measured else git_store.cached_ahead_behind, ch["id"])
        rescued = git_store.rescued_cells(ch["id"])
        conn = get_db_connection(ch["id"])
        try:
            # Most channels hold no repository: one probe spares every
            # cell its tip walk.
            held = bool(rescued) or conn.execute("SELECT 1 FROM cell_repositories LIMIT 1").fetchone() is not None
            for kind in ("main_stacks", "nav_stacks"):
                for stack in ch.get(kind, []):
                    for cell in stack.get("cells", []):
                        rows = snapshot_files(conn, cell["id"])
                        card = cards.get(cell["id"]) if cell.get("builtin") else None
                        if card is not None:
                            rows = projection(card, rows)
                            cell["name"], cell["icon"] = card.name, card.icon
                        cell["files"] = [{"name": f.pop("path"), **f} for f in rows]
                        repositories, overflow = ([], 0)
                        if held:
                            repositories, overflow = snapshot_entries(
                                conn, find_tip(conn, cell["id"]), counts=counts,
                                rescued=git_store.rescues_of(ch["id"], cell["id"]) if cell["id"] in rescued else None,
                                failed=git_harvest.materialize_errors(ch["id"], cell["id"]))
                        cell["repositories"] = repositories
                        if overflow:
                            cell["repositories_overflow"] = overflow
        finally:
            conn.close()
    return state_dict


def _state_wire_snapshot(measured: Collection[str] = ()) -> dict:
    """The wire snapshot of every channel; ``measured`` names the channels
    it is built to send in full (``file_metadata_state``)."""
    from collimate_server.genesis import snapshot_app_state
    with state_lock:
        snapshot = snapshot_app_state(state)
    snapshot.pop("storage_format", None)
    return file_metadata_state(snapshot, measured=measured)


def scope_state_snapshot(snapshot: dict, channel: str | None = None) -> dict:
    """One full channel and metadata for others; absence still means removal.

    Archived channels are left out, except the scope channel and the active
    one, which carry ``archived: true`` (the window opens them read-only; a
    stream on a channel just archived learns it from that patch)."""
    from collimate_server.archive import is_archived
    active_id = snapshot.get("active_channel_id")
    if channel is None:
        scope_id = active_id
    else:
        from collimate_server.database import canonical_channel_id
        channel_id = canonical_channel_id(channel)
        scope_id = next((c["id"] for c in snapshot["channels"]
                         if c["id"] == channel_id), None)
    channels = []
    for c in snapshot["channels"]:
        archived = is_archived(c["id"])
        if archived and c["id"] not in (scope_id, active_id):
            continue
        entry = c if c["id"] == scope_id else {"id": c["id"], "name": c["name"]}
        channels.append({**entry, "archived": True} if archived else entry)
    return {"active_channel_id": active_id, "channel_scope": scope_id, "channels": channels}


def build_state_snapshot() -> dict:
    """Complete active-channel state for HTTP bootstrap/manual refresh."""
    with state_lock:
        active = state.active_channel_id
    return scope_state_snapshot(_state_wire_snapshot({str(active)} if active else ()))


def _build_channel_state_events(channels: list[str]) -> dict:
    snapshot = _state_wire_snapshot({canonical_channel_id(channel) for channel in channels})
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
        from collimate_server.events import broadcast_coalesced
        broadcast_coalesced(_build_channel_state_events)
    except Exception:
        logger.exception("state broadcast scheduling failed")
