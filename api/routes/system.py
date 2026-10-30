
import asyncio
import logging
import os
from datetime import datetime
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from state import (
    get_active_channel, channel_db_name,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System endpoints
# ---------------------------------------------------------------------------

@router.get("/api/status")
async def status_check():
    return {"status": "ok", "time": datetime.now()}


class ClientHandshake(BaseModel):
    """What a client posts to identify itself. Capability names are the
    shell's camelCase contextBridge names; the registry snake-cases them."""
    model_config = ConfigDict(extra="forbid")
    client: Literal["electron", "browser"]
    shellVersion: str | None = None
    bridge: int | None = None
    capabilities: dict[str, bool] = {}


@router.post("/api/system/client")
async def set_client(handshake: ClientHandshake) -> dict:
    """Record which client is driving this server, replacing whatever the
    ``COLLIMATE_CLIENT`` env seed (or an earlier handshake) said.

    Last writer wins on purpose: a browser tab opened against a server the
    desktop shell spawned really is a browser, and telling a generator
    otherwise would have it reach for a native guest view that isn't there."""
    from client_caps import set_client as record_client
    return record_client(
        handshake.client,
        capabilities=handshake.capabilities,
        shell_version=handshake.shellVersion,
        bridge=handshake.bridge,
    )


def broadcast_runs_active_changed(channel: str) -> None:
    """Tell every client, on whichever channel stream it follows, that
    ``channel``'s live runs changed: a run started or reached a terminal
    status. The client refetches ``GET /api/runs/active`` (the top bar's
    running count) on it instead of polling. Best-effort, like the run
    broadcasts it follows."""
    import events as _events
    from collimate._host.channel_events import RunsActiveChanged
    from database import canonical_channel_id
    try:
        _events.broadcast_all(RunsActiveChanged(canonical_channel_id(channel)).to_dict())
    except Exception:
        logger.debug("runs_active_changed broadcast failed for %s", channel, exc_info=True)


@router.get("/api/system/instance")
async def system_instance(request: Request) -> dict:
    """Who this server is: its pid, absolute data dir and instance id, with
    the ``/health`` version and bridge. The desktop shell adopts a server it
    left running only when this names its own profile's data dir, so a
    recycled port or pid never passes for it. Guarded like every route;
    ``/health`` stays the minimal public handshake.

    The version and bridge come from the app's own ``/health`` handler:
    importing ``main`` here would build a second app when the server runs
    as ``python main.py`` (there the app lives in ``__main__``)."""
    from instance import instance_id
    health = next(r for r in request.app.router.routes
                  if isinstance(r, APIRoute) and r.path == "/health")
    handshake = await health.endpoint()
    return {
        "pid": os.getpid(),
        "data_dir": os.path.abspath(os.getcwd()),
        "instance": instance_id(),
        "version": handshake["version"],
        "bridge": handshake["bridge"],
    }


@router.post("/api/system/stop_all")
async def stop_all() -> dict:
    """Stop every running cell + sandbox container **without** shutting the
    server (or the app window) down. Generators get a grace window to exit
    cleanly — Docker-using ones use it to ``docker stop`` their container —
    then anything still alive is force-killed. The UI is left intact and
    empty of live work. Blocks until teardown finishes, then reports it."""
    from routes.runs import stop_all_runs
    summary = await stop_all_runs()
    return {"status": "stopped", **summary}


@router.post("/api/system/shutdown")
async def shutdown_server() -> dict:
    """Fully shut the server down: broadcast a ``shutdown`` event to every
    SSE subscriber, then (in the background) stop all running cells +
    sandboxes and signal uvicorn to exit. The HTTP response returns
    immediately so the client can show a progress UI while teardown runs.

    This is the full-quit path; the runs menu's Stop all is wired to
    /stop_all instead, which leaves the server running."""
    import events as _events
    from collimate._host.channel_events import Shutdown
    _events.broadcast_all(Shutdown().to_dict())
    # The actual teardown runs as a background task so this handler can
    # return a response before uvicorn starts refusing new connections.
    from task_utils import track_task
    track_task(_run_shutdown_sequence(), name="shutdown_sequence")
    return {"status": "shutting_down"}


async def _run_shutdown_sequence() -> None:
    """Stop all running cells + sandboxes (graceful → force) then send
    SIGINT to ourselves so uvicorn runs its lifespan teardown and exits
    cleanly. The stop ladder lives in routes/runs/teardown so this path
    and the lifespan path can't diverge; the lifespan's own Stop all then
    stops only what this one did not reach, and never waits again for a
    run-end cleanup this one waited out (teardown.wait_for_run_cleanups)."""
    import signal
    from run_registry import mark_shutting_down
    # Refuse starts even during the response-flush pause: no new run may
    # enter behind this sequence's Stop all sweep.
    mark_shutting_down()
    # Give the SSE broadcast + HTTP response a moment to flush before
    # uvicorn starts refusing new connections.
    await asyncio.sleep(0.3)

    try:
        from routes.runs import stop_all_runs
        await stop_all_runs()
    except Exception:
        # Best-effort: a failure to stop a manager shouldn't prevent
        # the server itself from exiting.
        logger.exception("shutdown: stop_all_runs failed; exiting anyway")

    # Tell uvicorn to shut down. SIGINT triggers the same lifespan
    # teardown path as Ctrl-C; uvicorn waits for in-flight requests
    # (briefly) then exits. The CLI also maps a supervisor's SIGTERM to
    # interruption, so both paths close the loop and await pending cleanup.
    os.kill(os.getpid(), signal.SIGINT)


@router.post("/api/maintenance/gc")
async def maintenance_gc(grace_seconds: int | None = None):
    """Run one CAS mark-and-sweep cycle on demand.

    Use cases: an operator wants to reclaim disk before a large import,
    or to verify the same collector used by daily and deletion tasks.
    Pass ``grace_seconds`` to override the 24h trash retention for
    testing — production use should leave it at the default.
    """
    import cas_gc
    grace = float(grace_seconds) if grace_seconds is not None else cas_gc.DEFAULT_GRACE_SECONDS
    if grace < 0:
        raise HTTPException(status_code=400, detail="grace_seconds must be >= 0")
    # Run in a thread — sweeps and unlinks can stall on slow disks and
    # we don't want to block the event loop.
    result = await asyncio.to_thread(cas_gc.run_gc, grace_seconds=grace)
    return result


@router.get("/api/maintenance/disk_usage")
async def maintenance_disk_usage():
    """Report current CAS + trash byte counts. Useful for dashboards
    or for an operator sanity-checking before triggering a GC."""
    import cas_gc
    return await asyncio.to_thread(cas_gc.disk_usage)


@router.get("/api/state")
async def get_app_state():
    # Pydantic model_dump on the event loop is CPU-bound and scales
    # with channel/cell/file count — for a busy workspace it's tens of
    # ms per call, blocking every other handler for that duration.
    # Offload so the loop stays responsive even when the frontend
    # polls /api/state at the same time generators are mutating state.
    def _dump():
        from state import build_state_snapshot
        return build_state_snapshot()
    return await asyncio.to_thread(_dump)


@router.get("/api/renderer_preferences")
async def get_renderer_preferences():
    """Get renderer preferences for the active channel (extension -> renderer name).
    The authoritative store lives in channel_config (also composed into the
    settings doc's ``renderer_preferences`` section)."""
    from channel_config import read_renderer_preferences
    ch = get_active_channel()
    db_name = channel_db_name(ch)
    return await asyncio.to_thread(read_renderer_preferences, db_name)


@router.put("/api/renderer_preferences")
async def set_renderer_preferences(prefs: dict[str, str]):
    """Set renderer preferences for the active channel (extension -> renderer
    name). 400 for one that moves a surface extension (``.run``, ``.chat``,
    …) off its built-in renderer."""
    from channel_config import write_renderer_preferences
    ch = get_active_channel()
    db_name = channel_db_name(ch)
    try:
        await asyncio.to_thread(write_renderer_preferences, db_name, prefs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


# --- Renderer availability (file-manager "management" view) -----------------
# Channel-scoped per-renderer on/off flag managed by the renderer cards.
# Storage + flag-resolution semantics live in channel_config.py.

class EnabledPatch(BaseModel):
    """On/off patch for a per-id renderer availability flag."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.put("/api/renderer_availability/{renderer_id}")
async def set_renderer_availability_endpoint(
    renderer_id: str, patch: EnabledPatch,
):
    """Set one renderer's enabled flag (per-id upsert). 409 for a built-in
    renderer: the app's own are always available."""
    from builtin_packages import builtin_renderers
    from channel_config import set_renderer_availability
    if renderer_id in builtin_renderers():
        raise HTTPException(409, "Built-in renderers are always available")
    ch = get_active_channel()
    db_name = channel_db_name(ch)
    data = await asyncio.to_thread(
        set_renderer_availability, db_name, renderer_id, patch.enabled,
    )
    return {"id": renderer_id, "enabled": data.get(renderer_id, True)}
