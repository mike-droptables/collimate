"""Channel lifecycle, settings and state-stream HTTP routes."""

import asyncio
import json
import logging
import os
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, model_validator

from cell_placement import iter_stacks
from channel_sources import ConfigSelection
from models import Channel
from state import (
    state, persist_state, channel_db_name, validate_channel_name,
    find_active_channel, require_channel,
)
from suggested import SuggestedSelection

router = APIRouter()


class CreateChannelBody(BaseModel):
    """What a new channel starts from: a suggestion or a config selection.
    Neither (or no body) is an empty config: a Capabilities stack holding
    the app's built-in cards, default settings and no main cells."""
    model_config = ConfigDict(extra="forbid")
    suggested: SuggestedSelection | None = None
    config: ConfigSelection | None = None

    @model_validator(mode="after")
    def _one_source(self):
        if self.suggested is not None and self.config is not None:
            raise ValueError("Choose a suggestion or a config, not both")
        return self


logger = logging.getLogger("routes.system")
from database import creating_channels, changing_channels


@router.post("/api/channels")
async def create_channel(name: str, body: CreateChannelBody | None = Body(None)):
    from pathlib import Path
    from database import forget_db
    channel = Channel(name=validate_channel_name(name))
    db_name = channel_db_name(channel)
    reservation = db_name
    path = Path(".collimate/channels") / f"{db_name}.db"
    if reservation in creating_channels or any(c.id == channel.id for c in state.channels):
        raise HTTPException(409, "Channel identity already exists")
    creating_channels.add(reservation)
    previous_active = state.active_channel_id
    try:
        result = await _create_channel(channel, body or CreateChannelBody())
        from state import flush_persist_state
        await asyncio.to_thread(flush_persist_state)
        return result
    except Exception:
        from state import state_lock
        with state_lock:
            state.channels = [c for c in state.channels if channel_db_name(c) != db_name]
            if not any(c.id == state.active_channel_id for c in state.channels):
                state.active_channel_id = previous_active
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        forget_db(db_name)
        persist_state()
        raise
    finally:
        creating_channels.discard(reservation)


async def _create_channel(ch: Channel, body: CreateChannelBody):
    """Seed the unregistered channel ``ch``, then register it and make it
    active; answers the channel and the held packages."""
    from channel_sources import seed_config
    from genesis import ensure_capabilities_stack
    from suggested import seed_suggested
    if any(c.id == ch.id for c in state.channels):
        raise HTTPException(409, "Channel identity already exists")
    if body.suggested is not None:
        skipped = await asyncio.to_thread(seed_suggested, ch, body.suggested)
    else:
        skipped = await asyncio.to_thread(seed_config, ch, body.config or ConfigSelection())
    ensure_capabilities_stack(ch)
    ch.main_selection_id = ch.main_stacks[0].id if ch.main_stacks else None
    state.channels.append(ch)
    state.active_channel_id = ch.id
    persist_state()
    return ch.model_dump(mode="json") | {"package_skips": skipped}


# ---------------------------------------------------------------------------
# Per-channel settings (the channel-settings document — see channel_settings.py)
# ---------------------------------------------------------------------------

@router.get("/api/channels/{channel}/settings")
async def get_channel_settings(channel: str):
    """The channel's full settings doc (fresh defaults if never written)."""
    from channel_settings import load_channel_settings
    channel_uuid = channel_db_name(require_channel(channel))
    return await asyncio.to_thread(load_channel_settings, channel_uuid)


@router.put("/api/channels/{channel}/settings")
async def put_channel_settings(channel: str, partial: dict = Body(...)):
    """Merge one or more sections (section-wholesale) into the channel's
    settings doc; returns the merged document."""
    from channel_settings import merge_channel_settings
    channel_uuid = channel_db_name(require_channel(channel))
    try:
        return await asyncio.to_thread(merge_channel_settings, channel_uuid, partial)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/channels")
async def list_channels() -> dict:
    """The light channel catalog: every channel's id and name plus the
    active one. The desktop shell polls this on each saved server to list
    its channels in the window's channel menu; ``GET /api/state`` would
    carry the whole active channel for it."""
    active = find_active_channel()
    return {
        "channels": [{"id": str(c.id), "name": c.name} for c in list(state.channels)],
        "active_channel_id": str(active.id) if active else None,
    }


@router.post("/api/channels/{channel_id}/select")
async def select_channel(channel_id: str):
    ch = require_channel(channel_id)
    state.active_channel_id = ch.id
    persist_state()
    return {"status": "selected"}


def _checkpoint_and_release(channel_uuid: str) -> None:
    """Flush WAL into the main db and close the connection.

    The lifecycle reservation excludes new writers while the worker drains
    the WAL before moving or removing the database file bundle.
    """
    try:
        from database import get_db_connection
        conn = get_db_connection(channel_uuid)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except Exception:
        pass



def _safe_remove(path: str, retries: int = 5) -> None:
    """Remove on the worker, with a bounded retry for transient refusal."""
    import time
    for attempt in range(retries):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(0.05 * (2 ** attempt))
            else:
                raise


def _refuse_while_runs_live(ch: Channel, doing: str) -> None:
    """409 while a run is live on ``ch``: a start under way (it writes the
    channel db before it registers), a registered run writing its channel
    db until its run-end cleanup is done (including a stopping run), a stop still writing a run's status or stop event, or a
    start holding one of its cells. A run retains its UUID database
    identity, so unlinking that database during a run would have its
    next write recreate an empty db no channel owns, and its log, versions
    and final status would land there. Checked with no await before the
    file operations. A start and a per-run Stop mark the channel before
    their first await (registry.channel_busy), and Stop all marks each
    channel for its status write one step after its first await, while the
    runs it writes are still registered, so none can begin in between."""
    from run_registry import active_managers, busy_channels, held_cell_ids

    channel_uuid = channel_db_name(ch)
    live = channel_uuid in busy_channels() or any(
        getattr(m, "channel", None) == channel_uuid
        for m in active_managers.values()
    )
    if not live:
        held = held_cell_ids()
        live = bool(held) and any(
            str(cell.id) in held
            for stack in iter_stacks(ch) for cell in stack.cells
        )
    if live:
        raise HTTPException(
            409, f"Runs in '{ch.name}' are still running or finishing. Stop them "
                 f"and let them finish before {doing} the channel.")


async def _finish_channel_operation(operation):
    """Cancellation waits for file moves and durable layout to agree."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@router.put("/api/channels/{channel_id}/rename")
async def rename_channel(channel_id: str, name: str):
    return await _finish_channel_operation(_rename_channel(require_channel(channel_id).id, name))


@router.delete("/api/channels/{channel_id}")
async def delete_channel(channel_id: str):
    return await _finish_channel_operation(_delete_channel(require_channel(channel_id).id))


async def _rename_channel(channel_id: UUID, name: str):
    ch = next((c for c in state.channels if c.id == channel_id), None)
    if not ch:
        raise HTTPException(404, "Channel not found")
    new_name = validate_channel_name(name)
    if new_name == ch.name:
        return ch

    from state import state_lock, flush_persist_state
    with state_lock:
        previous_name = ch.name
        ch.name = new_name
    try:
        persist_state()
        await asyncio.to_thread(flush_persist_state)
    except BaseException:
        with state_lock:
            ch.name = previous_name
        persist_state()
        raise
    return ch


async def _delete_channel(channel_id: UUID):
    ch = next((c for c in state.channels if c.id == channel_id), None)
    if not ch:
        raise HTTPException(404, "Channel not found")
    # A live run still writes the db and references the blobs below.
    _refuse_while_runs_live(ch, "deleting")
    # Deleting the last channel is allowed: state falls back to zero channels
    # and the frontend shows the channel-creation view.

    channel_uuid = channel_db_name(ch)
    channels_dir = os.path.join(".collimate", "channels")

    from database import channel_files_lock, forget_db
    from cell_metadata import remove_channel_layout
    changing_channels.add(channel_uuid)
    try:
        await asyncio.to_thread(remove_channel_layout, channel_uuid)
    except Exception:
        changing_channels.discard(channel_uuid)
        raise

    def remove_database():
        with channel_files_lock:
            _checkpoint_and_release(channel_uuid)
            for suffix in ("", "-wal", "-shm"):
                _safe_remove(os.path.join(channels_dir, f"{channel_uuid}.db{suffix}"))
            forget_db(channel_uuid)
    try:
        await asyncio.to_thread(remove_database)
    finally:
        changing_channels.discard(channel_uuid)
    # One retention policy: deletion never unlinks CAS objects directly.
    from cas_gc import request_collection
    request_collection()
    return {
        "status": "deleted",
        "active_channel_id": (
            str(state.active_channel_id) if state.active_channel_id else None
        ),
    }


@router.get("/api/channels/{channel}/state/stream")
async def stream_channel_state(channel: str):
    """SSE stream for real-time channel state updates (DAG commits, status changes).

    On overflow (slow client, suspended tab, etc.) the broadcaster
    enqueues an overflow sentinel; this handler emits a final
    ``{"type": "overflow"}`` event and closes the stream. The
    EventSource client reconnects and receives a fresh subscribed
    canonical snapshot on that new stream before accepting more deltas — see frontend/useChannelSSE.ts.
    """
    require_channel(channel)
    from events import subscribe, unsubscribe, OVERFLOW_SENTINEL

    async def event_gen():
        # Subscribe inside the generator: a client gone before the body
        # is first pulled never starts it, and only the finally below
        # unsubscribes.
        q = subscribe(channel)
        from state import broadcast_state_update
        broadcast_state_update()
        try:
            # The coalescer gives this queue a complete snapshot before deltas.
            # Send initial keepalive
            from collimate._host.channel_events import Connected, Overflow
            yield f"data: {json.dumps(Connected().to_dict())}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Send keepalive to prevent connection timeout
                    yield ": keepalive\n\n"
                    continue
                if event is OVERFLOW_SENTINEL:
                    # Tell the client why the stream is closing so it
                    # can reconnect for a full revisioned snapshot. The
                    # leading newline + retry: hint nudges the browser
                    # to reconnect quickly.
                    yield f"data: {json.dumps(Overflow().to_dict())}\n\n"
                    yield "retry: 100\n\n"
                    return
                yield event.sse_data if hasattr(event, "sse_data") else f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe(channel, q)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
