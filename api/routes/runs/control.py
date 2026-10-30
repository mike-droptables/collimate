"""Control endpoints — stop, emit_renderer_action."""

import asyncio
import logging

from process_signals import signal_process_group
import signal as sig_module

from fastapi import APIRouter, HTTPException

from database import canonical_channel_id
from process_manager.finalization import wait_process_exit
from .models import RendererActionRequest
from run_registry import active_managers, channel_busy, lock_owner, mark_stop_requested
from .teardown import mark_run_stopped, record_stop_event

_logger = logging.getLogger("runs.control")

router = APIRouter()


@router.post("/api/channels/{channel}/cells/{cell_id}/actions")
async def emit_renderer_action(channel: str, cell_id: str, body: RendererActionRequest):
    """Enqueue a renderer-emitted action (a button click in a live renderer)
    for the run that owns the file it was emitted from.

    Several runs may be live on the cell, so the run is resolved from the
    action's ``source``, never from the cell alone: the live run serving that
    file, falling back to the path's lock owner. A source no live run owns is
    a 409. The generator consumes the queue via gen.wait_for_action().
    """
    from state import require_channel
    ch = str(require_channel(channel).id)
    from served_surfaces import get
    surface = get(cell_id, body.source)
    manager = active_managers.get(surface.run_id) if surface else lock_owner(cell_id, body.source)
    if manager is None or canonical_channel_id(manager.channel or "") != ch:
        raise HTTPException(409, f"no live run owns {body.source}")
    if not body.action_id:
        raise HTTPException(400, "action_id is required")
    entry = {"action_id": body.action_id, "source": body.source or "",
             "payload": body.payload if isinstance(body.payload, dict) else None}
    manager._pending_renderer_actions.append(entry)
    # Bound the queue: filtered waiters may leave non-matching entries behind,
    # and an unconsumed burst must not grow without limit.
    if len(manager._pending_renderer_actions) > 256:
        del manager._pending_renderer_actions[0]
    return {"ok": True}


def _broadcast_status(channel: str, run_id: str, status: str) -> None:
    """Broadcast a run's status to the channel, best-effort (the frontend's
    endRun is idempotent on the run id)."""
    try:
        from events import broadcast
        from process_manager.streams import run_status_event
        broadcast(channel, run_status_event(channel, run_id, status))
    except Exception:
        _logger.debug("stop_run: run_status broadcast failed for run %s", run_id, exc_info=True)


@router.delete("/api/channels/{channel}/runs/{run_id}")
async def stop_run(channel: str, run_id: str, force: bool = False):
    """Stop a run: mark its row stopped while the row is still 'running',
    then SIGTERM its process group (SIGKILL with ``force``).

    Stop never removes the run from ``active_managers``: its reaper
    (helpers._reap_process) does, once the run-end cleanup has finalized,
    swept the run folder and dropped its sessions. Until then the run holds
    its cell and its run folder like any live run (running_cell_ids,
    lock_owner), and /runs/active lists it as ``stopping``.

    The channel is resolved first (404 on an unknown one, so a stale tab
    never creates a stray channel db) and marked busy until the stop's last
    db write (registry.channel_busy): the reaper may remove the run between
    those writes, and deletion must wait for them."""
    from state import require_channel
    require_channel(channel)
    manager = active_managers.get(run_id)
    if manager is not None and canonical_channel_id(manager.channel) != canonical_channel_id(channel):
        raise HTTPException(404, "That run does not belong to this channel")
    with channel_busy(channel):
        return await _stop_run(channel, run_id, force)


async def _stop_run(channel: str, run_id: str, force: bool) -> dict:
    """stop_run's body, inside its channel's busy mark."""
    manager = active_managers.get(run_id)
    mark_stop_requested(manager)

    if manager is not None and manager.process is None:
        # Registered but not spawned yet: start_run is still inside
        # manager.start() (manifest, env build, backend spawn or image pull).
        # Nothing to signal, so tell it to abort its spawn, as stop_all_runs
        # does. start() then raises and start_run's unwind pops the registry
        # entry — popping it here would let a spawn already in flight go
        # live with nothing tracking it.
        abort = getattr(manager, "abort_spawn", None)
        if abort is not None:
            abort()
        await asyncio.to_thread(mark_run_stopped, channel, run_id, "aborted_spawn")
        try:
            manager._broadcast_run_status("stopped")
        except Exception:
            _logger.debug(
                "stop_run: run_status broadcast failed for aborted-spawn run %s",
                run_id, exc_info=True,
            )
        return {"status": "stopped", "stop_type": "aborted_spawn"}

    if manager is not None and manager.process.returncode is not None:
        # The process has exited and its run-end cleanup (finalize, seal, the
        # run-folder sweep) is under way. The cleanup records the run's
        # status from its exit, and the reaper removes the run from the
        # registry once the cleanup is done, so the run's cell stays
        # guarded until then.
        sig = sig_module.SIGKILL if force else sig_module.SIGTERM
        # Processes the generator left in its group can hold its stdio, which
        # keeps the cleanup in its bounded output drain; the stop reaches
        # them now.
        if signal_process_group(manager.process.pid, sig, known_group=True):
            return {
                "status": "stopped",
                "stop_type": "force_stopped" if force else "gracefully_stopped",
            }
        # Once the status is final, re-broadcast it: a missed run_status
        # (SSE backpressure, overflow, a brief disconnect) would leave the
        # frontend's run in stoppingRunIds, wedged on "Force Stop". Before
        # that, the finalize broadcasts it itself.
        final_status = manager.final_status
        if final_status:
            _broadcast_status(channel, run_id, final_status)
        return {"status": "stopped", "stop_type": "already_finished"}

    if manager is None:
        # A queued run (accepted, its spawn in flight) refuses at its last
        # gate whatever the timing of the row write below.
        from .create import stop_queued
        stop_queued(channel, run_id)
        # No live run: a row left 'running' becomes 'stopped' (one that
        # already ended keeps its status), and the broadcast carries whatever
        # status the row ends up with.
        row_status = await asyncio.to_thread(mark_run_stopped, channel, run_id)
        _broadcast_status(channel, run_id, row_status or "stopped")
        return {"status": "stopped", "stop_type": "already_finished"}

    pid = manager.process.pid
    stop_type = "force_stopped" if force else "gracefully_stopped"

    # Record the stop before signalling: the run's finalize only writes a row
    # still 'running', so a run that exits the instant it is signalled is
    # recorded 'stopped' all the same, and noted with its stop type, so
    # every run_status broadcast after the signal carries the note. A row
    # the generator's finish op already ended keeps its status. The stop
    # event follows the log flush below, so it lands after the run's last
    # output. The signal waits for this one-row write: at most the db's busy
    # timeout (5 s) while another writer holds the channel db, and a db that
    # refuses the write never keeps the run from being stopped.
    try:
        row_status = await asyncio.to_thread(mark_run_stopped, channel, run_id, stop_type, event=False)
    except Exception:
        row_status = None
        _logger.warning(
            "stop_run: could not mark run %s stopped before signalling it",
            run_id, exc_info=True,
        )

    def _kill_pg(sig):
        """Send signal to the process group, falling back to direct kill.
        Never once the process has been reaped: its pid may be reused."""
        if manager.process.returncode is not None:
            return
        signal_process_group(pid, sig)

    def _close_ipc_if_open():
        # On SIGKILL the _wait_and_cleanup task may never run, so the
        # IPC TCP server is only torn down when the manager object is
        # GC'd. Close it explicitly here so the port + socket fd are
        # released immediately.
        srv = manager._ipc_server
        if srv is not None:
            try:
                srv.close()
            except Exception:
                _logger.debug(
                    "stop_run: IPC server close failed for run %s (already closed?)",
                    run_id, exc_info=True,
                )

    if force:
        _kill_pg(sig_module.SIGKILL)
        _close_ipc_if_open()
        # Tear down the run's live chat and terminal sessions eagerly too
        # (mirrors _close_ipc_if_open). _wait_and_cleanup also unregisters
        # both, but only after the drain — until then the chat renderer keeps
        # its composer in the "assistant is replying" state, and a terminal's
        # PTY program keeps running, for a run the user just SIGKILLed.
        # Unregistering closes the WebSocket, which is the signal the
        # renderer clears that state on. Only THIS run's sessions: the cell's
        # other runs keep theirs. Idempotent on the cleanup path.
        try:
            from sessions.chat import unregister_sessions_for_run
            unregister_sessions_for_run(run_id)
        except Exception:
            _logger.debug(
                "stop_run: eager session unregister failed for run %s",
                run_id, exc_info=True,
            )
        try:
            from sessions.terminal import unregister_sessions_for_run as _unregister_terminals
            _unregister_terminals(run_id)
        except Exception:
            _logger.debug(
                "stop_run: eager terminal unregister failed for run %s",
                run_id, exc_info=True,
            )
        # Eager broadcast so the frontend flips out of "Stopping…" /
        # "Force Stop" the instant SIGKILL lands. _wait_and_cleanup
        # broadcasts run_status too, but it has to await the
        # microbatch writer's final flush first — when the log
        # buffer is large that finalization can take 5-10s, and the
        # cell's Force Stop button is wedged for that whole window.
        # Safe to fire twice — the frontend's endRun is idempotent on the
        # run_id key.
        try:
            manager._broadcast_run_status(row_status or "stopped")
        except Exception:
            _logger.debug(
                "stop_run: run_status broadcast failed for force-stopped run %s",
                run_id, exc_info=True,
            )
    else:
        _kill_pg(sig_module.SIGTERM)
        # Escalate to SIGKILL if the process doesn't exit within 15 seconds.
        # 15s is generous enough to let generator cleanup finish — e.g.
        # a Docker-using generator needs to call docker stop on its
        # container, which itself can take a few seconds. The previous 5s
        # budget was too short and routinely killed cleanup mid-flight,
        # leaking containers.
        async def _escalate_kill():
            try:
                await asyncio.wait_for(wait_process_exit(manager.process), timeout=15.0)
            except asyncio.TimeoutError:
                _kill_pg(sig_module.SIGKILL)
                # The parent ignored its grace — assume wedged children too.
            except Exception:
                _logger.warning(
                    "stop_run: escalate-kill wait failed for run %s (pid %s)",
                    run_id, pid, exc_info=True,
                )

        from task_utils import track_task
        track_task(_escalate_kill(), name=f"escalate_kill:{run_id}")

    # Drain whatever the microbatch writer has in flight before returning.
    # Without this, a client that POSTs /stop and immediately calls
    # /events_since can see a truncated log tail (last ~500 ms of stdout
    # still sitting in the in-memory buffer).
    try:
        await asyncio.to_thread(manager._flush_log_buffer)
    except Exception:
        _logger.warning(
            "stop_run: final log-buffer flush failed for run %s; "
            "log tail may be truncated", run_id, exc_info=True,
        )

    # The stop event, offloaded, when the row is stopped (by this stop or an
    # earlier one: a Force Stop after a graceful Stop is recorded as forced).
    if row_status == "stopped":
        await asyncio.to_thread(record_stop_event, channel, run_id, stop_type)

    return {"status": "stopped", "stop_type": stop_type}
