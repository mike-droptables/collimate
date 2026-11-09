"""Channel lifecycle, settings and state-stream HTTP routes."""

import asyncio
import json
import logging
from uuid import UUID

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, model_validator

from collimate_server.cell_placement import iter_stacks
from collimate_server.channel_sources import ConfigSelection
from collimate_server.models import Channel
from collimate_server.state import (
    state, persist_state, channel_db_name, validate_channel_name,
    find_active_channel, require_channel,
)
from collimate_server.suggested import SuggestedSelection
from collimate_server.user_catalog import SavedSelection

router = APIRouter()


class CreateChannelBody(BaseModel):
    """What a new channel starts from: a suggestion of the app's, a saved
    suggestion (``user_catalog``) or a config selection. None (or no body) is
    an empty config: a Capabilities stack holding the app's built-in cards,
    default settings and no main cells."""
    model_config = ConfigDict(extra="forbid")
    suggested: SuggestedSelection | None = None
    saved: SavedSelection | None = None
    config: ConfigSelection | None = None

    @model_validator(mode="after")
    def _one_source(self):
        if sum(part is not None for part in (self.suggested, self.saved, self.config)) > 1:
            raise ValueError("Choose a suggestion, a saved suggestion or a config, not more than one")
        return self


logger = logging.getLogger("routes.system")
from collimate_server.database import creating_channels, changing_channels


@router.post("/api/channels")
async def create_channel(name: str, body: CreateChannelBody | None = Body(None)):
    """Create S2: the folder appears as ``<default>/<uuid>.moving`` with its
    channel.json, is seeded there (DB, stores, the initial layout), then is
    renamed to ``<uuid>`` and registered. Any failure removes the folder and
    leaves the catalog as it was; a crash leaves a ``.moving`` the next
    start removes. 409 ``no_default_location`` when new channels have
    nowhere to go."""
    import collimate_server.channel_paths as channel_paths
    channel = Channel(name=validate_channel_name(name))
    db_name = channel_db_name(channel)
    if db_name in creating_channels or any(c.id == channel.id for c in state.channels):
        raise HTTPException(409, "Channel identity already exists")
    creating_channels.add(db_name)
    previous_active = state.active_channel_id
    try:
        await asyncio.to_thread(channel_paths.begin_create, db_name, channel.name)
        try:
            result = await _create_channel(channel, body or CreateChannelBody())
            from collimate_server.state import flush_persist_state
            await asyncio.to_thread(flush_persist_state)
            await asyncio.to_thread(_size_new_channel, db_name)
            return result
        except BaseException:
            from collimate_server.state import state_lock
            from collimate_server.cell_metadata import forget_layout
            with state_lock:
                state.channels = [c for c in state.channels if channel_db_name(c) != db_name]
                if not any(c.id == state.active_channel_id for c in state.channels):
                    state.active_channel_id = previous_active
            forget_layout(db_name)
            await asyncio.to_thread(channel_paths.abandon_create, db_name)
            persist_state()
            raise
    finally:
        creating_channels.discard(db_name)


def _size_new_channel(channel_id: str) -> None:
    """The storage survey's entry for a channel just created; a failure to
    measure it leaves the entry to the next survey."""
    import collimate_server.storage_survey as storage_survey
    try:
        storage_survey.update_channel(channel_id)
    except Exception:
        logger.warning("could not size the new channel %s", channel_id, exc_info=True)


async def _create_channel(ch: Channel, body: CreateChannelBody):
    """Seed the unregistered channel ``ch`` in its staging folder, commit its
    initial layout there, rename the folder into place, then register it and
    make it active; answers the channel, the held packages and the
    repositories left behind (``skipped``: copied ones not copied, uploaded
    ones git would not adopt)."""
    import collimate_server.channel_paths as channel_paths
    from collimate_server.cell_metadata import save_channel_layout
    from collimate_server.channel_sources import seed_config
    from collimate_server.database import get_db_connection
    from collimate_server.genesis import ensure_capabilities_stack, snapshot_channel
    from collimate_server.suggested import seed_suggested
    if any(c.id == ch.id for c in state.channels):
        raise HTTPException(409, "Channel identity already exists")
    channel_id = channel_db_name(ch)
    # The DB first, so every seeder opens an existing one.
    await asyncio.to_thread(lambda: get_db_connection(channel_id).close())
    left_behind: list[dict] = []
    if body.suggested is not None:
        skipped = await asyncio.to_thread(seed_suggested, ch, body.suggested)
    elif body.saved is not None:
        from collimate_server.user_catalog import seed_saved
        skipped = await asyncio.to_thread(seed_saved, ch, body.saved)
    else:
        skipped, left_behind = await asyncio.to_thread(seed_config, ch, body.config or ConfigSelection())
    ensure_capabilities_stack(ch)
    ch.main_selection_id = ch.main_stacks[0].id if ch.main_stacks else None

    def commit_folder():
        save_channel_layout(snapshot_channel(ch))
        _checkpoint_and_release(channel_id)
        from collimate_server.cell_metadata import forget_layout
        forget_layout(channel_id)
        channel_paths.finish_create(channel_id)
    await asyncio.to_thread(commit_folder)
    state.channels.append(ch)
    state.active_channel_id = ch.id
    from collimate_server.archive import note_selected
    note_selected(ch.id)
    persist_state()
    return ch.model_dump(mode="json") | {"package_skips": skipped, "skipped": left_behind}


# ---------------------------------------------------------------------------
# Per-channel settings (the channel-settings document — see channel_settings.py)
# ---------------------------------------------------------------------------

@router.get("/api/channels/{channel}/settings")
async def get_channel_settings(channel: str):
    """The channel's full settings doc (fresh defaults if never written)."""
    from collimate_server.channel_settings import load_channel_settings
    channel_uuid = channel_db_name(require_channel(channel))
    return await asyncio.to_thread(load_channel_settings, channel_uuid)


@router.put("/api/channels/{channel}/settings")
async def put_channel_settings(channel: str, partial: dict = Body(...)):
    """Merge one or more sections (section-wholesale) into the channel's
    settings doc; returns the merged document."""
    from collimate_server.channel_settings import merge_channel_settings
    channel_uuid = channel_db_name(require_channel(channel))
    try:
        return await asyncio.to_thread(merge_channel_settings, channel_uuid, partial)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/channels")
async def list_channels() -> dict:
    """The light channel catalog: every available channel's id and name
    plus the active one. The desktop shell polls this on each saved server
    to list its channels in the window's channel menu; ``GET /api/state``
    would carry the whole active channel for it. Archived channels are not
    in it; every channel, archived and unavailable ones too, with its
    state, location and size, is ``GET /api/channels/manage``'s."""
    from collimate_server.archive import is_archived
    active = find_active_channel()
    return {
        "channels": [{"id": str(c.id), "name": c.name} for c in list(state.channels) if not is_archived(c.id)],
        "active_channel_id": str(active.id) if active else None,
    }


# ---------------------------------------------------------------------------
# The New / manage channels page's listing and a channel's size (plan 1105 P6)
# ---------------------------------------------------------------------------

@router.get("/api/channels/manage")
async def manage_channels() -> dict:
    """Every channel of this server's owned locations, archived ones
    included (the only listing that has them): ``{channels: [{id, name,
    archived, archived_at, location_id, path, state, reason, bytes, files,
    computed_at, active}]}``. ``state`` is ``ok``, ``moving`` (being copied
    to another location) or ``unavailable`` (its folder is gone or did not
    open; ``reason`` says why). ``bytes`` and ``files`` are the storage
    survey's (null before it sized the folder), as of ``computed_at``."""
    import collimate_server.storage_survey as storage_survey
    return {"channels": await asyncio.to_thread(storage_survey.manage_listing)}


@router.get("/api/channels/{channel}/size")
async def channel_size(channel: str) -> dict:
    """The channel folder's size, measured now (the delete confirmation):
    ``{bytes, files, parts: {channel_db, git, objects, objects_trash,
    cell_state, other}, computed_at}``, allocated bytes with each inode
    once. Archived and unavailable channels are measured too; 404 for no
    channel folder, 409 when it is gone."""
    import collimate_server.storage_survey as storage_survey
    entry = await asyncio.to_thread(storage_survey.update_channel, channel)
    return {key: entry[key] for key in ("bytes", "files", "parts", "computed_at")}


@router.post("/api/channels/{channel_id}/select")
async def select_channel(channel_id: str):
    """Make the channel active; an archived one opens read-only."""
    from collimate_server.archive import note_selected
    ch = require_channel(channel_id)
    state.active_channel_id = ch.id
    note_selected(ch.id)
    persist_state()
    return {"status": "selected"}


def _checkpoint_and_release(channel_uuid: str) -> None:
    """Flush WAL into the main db and close the connection.

    The lifecycle reservation excludes new writers while the worker drains
    the WAL before renaming or removing the channel's folder.
    """
    try:
        from collimate_server.database import get_db_connection
        conn = get_db_connection(channel_uuid)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except Exception:
        pass


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
    from collimate_server.run_registry import active_managers, busy_channels, held_cell_ids

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
    """Rewrite the channel's ``channel.json`` with the new name (under the
    channel files lock), then the live catalog."""
    import collimate_server.channel_paths as channel_paths
    ch = next((c for c in state.channels if c.id == channel_id), None)
    if not ch:
        raise HTTPException(404, "Channel not found")
    new_name = validate_channel_name(name)
    if new_name == ch.name:
        return ch
    await asyncio.to_thread(channel_paths.rename, channel_db_name(ch), new_name)
    from collimate_server.state import state_lock
    with state_lock:
        ch.name = new_name
    persist_state()
    return ch


async def _delete_channel(channel_id: UUID):
    """Delete S2: refuse while runs are live or an export holds pins in the
    channel's store; take it out of the catalog; under the channel files
    lock and the store lock checkpoint and close its DB and rename its
    folder to ``.deleting-<uuid>-<rand>``; then remove that folder. The
    space comes back at once; a removal that fails is retried by the next
    start's reconcile."""
    import collimate_server.channel_paths as channel_paths
    from collimate_server.cas import channel_store
    ch = next((c for c in state.channels if c.id == channel_id), None)
    if not ch:
        raise HTTPException(404, "Channel not found")
    # A live run still writes the db and references the blobs below.
    _refuse_while_runs_live(ch, "deleting")
    channel_uuid = channel_db_name(ch)
    if channel_store(channel_uuid).holds_pins():
        raise HTTPException(409, "An export of this channel is in progress. Try again when it is done.")
    # Deleting the last channel is allowed: state falls back to zero channels
    # and the frontend shows the channel-creation view.

    from collimate_server.database import channel_files_lock, forget_db
    from collimate_server.cell_metadata import forget_layout, unregister_channel
    import collimate_server.git_store as git_store
    changing_channels.add(channel_uuid)
    try:
        await asyncio.to_thread(unregister_channel, channel_uuid)
    except Exception:
        changing_channels.discard(channel_uuid)
        raise

    def retire_folder():
        # The store lock covers the folder's rename, so no store write sees
        # half a channel.
        with channel_files_lock, git_store.store_lock(channel_uuid):
            _checkpoint_and_release(channel_uuid)
            forget_db(channel_uuid)
            forget_layout(channel_uuid)
            return channel_paths.retire(channel_uuid)
    try:
        retired = await asyncio.to_thread(retire_folder)
    finally:
        changing_channels.discard(channel_uuid)
    await asyncio.to_thread(channel_paths.purge, retired)
    import collimate_server.storage_survey as storage_survey
    await asyncio.to_thread(storage_survey.forget_channel, channel_uuid)
    return {
        "status": "deleted",
        "active_channel_id": (
            str(state.active_channel_id) if state.active_channel_id else None
        ),
    }


@router.post("/api/channels/{channel_id}/archive")
async def archive_channel(channel_id: str) -> dict:
    """Archive the channel (S6): ``channel.json`` says so, it leaves every
    channel list and opens read-only, every write to it answers 409
    ``channel_archived``. Refused (409) while runs are live on it. Its pick
    sessions go; when it is the active channel the most recently selected
    channel that is not archived becomes active (None shows the channels
    page). Archiving an archived channel changes nothing. Answers its
    manage-listing row."""
    return await _finish_channel_operation(_archive_channel(require_channel(channel_id)))


@router.post("/api/channels/{channel_id}/unarchive")
async def unarchive_channel(channel_id: str) -> dict:
    """Unarchive the channel: listed and writable again (it stays where it
    is, active or not). Answers its manage-listing row."""
    return await _finish_channel_operation(_unarchive_channel(require_channel(channel_id)))


async def _archive_channel(ch: Channel) -> dict:
    import collimate_server.archive as archive
    from collimate_server.routes.pick_sessions import drop_pick_sessions
    from collimate_server.state import state_lock
    channel_uuid = channel_db_name(ch)
    if archive.is_archived(channel_uuid):
        return archive.manage_entry(channel_uuid)
    _refuse_while_runs_live(ch, "archiving")
    # Archived to every reader from here, with no await since the check: a
    # run start marks the channel busy before it reads the flag.
    with archive.archiving(channel_uuid):
        await asyncio.to_thread(archive.archive, channel_uuid)
    await asyncio.to_thread(drop_pick_sessions, channel_uuid)
    with state_lock:
        if state.active_channel_id == ch.id:
            state.active_channel_id = archive.successor(state.channels, ch.id)
    # The broadcast's patch carries ``archived: true`` to the channel's
    # streams, and drops the channel from the other windows' lists.
    persist_state()
    return archive.manage_entry(channel_uuid)


async def _unarchive_channel(ch: Channel) -> dict:
    import collimate_server.archive as archive
    channel_uuid = channel_db_name(ch)
    if archive.is_archived(channel_uuid):
        await asyncio.to_thread(archive.unarchive, channel_uuid)
        persist_state()
    return archive.manage_entry(channel_uuid)


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
    from collimate_server.events import subscribe, unsubscribe, OVERFLOW_SENTINEL

    async def event_gen():
        # Subscribe inside the generator: a client gone before the body
        # is first pulled never starts it, and only the finally below
        # unsubscribes.
        q = subscribe(channel)
        from collimate_server.state import broadcast_state_update
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
