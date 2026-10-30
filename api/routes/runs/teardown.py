"""Shared full-stop teardown.

Stops every running generator, gracefully first
(SIGTERM, and let Docker-using generators run their own ``docker stop``),
then forcibly (SIGKILL) for anything that ignores the grace window.

Every "stop everything" runs this one ladder, ``stop_all_runs``, so its
semantics can't drift between them:

  * the in-app **Stop all** button  (``POST /api/system/stop_all``) — the
    server keeps running, the UI just empties of live work.
  * **server shutdown**: the ``main.py`` lifespan teardown, which uvicorn
    runs on SIGINT (Ctrl-C) and on SIGTERM. The desktop shell quits with
    ``POST /api/system/shutdown``, whose sequence
    (``routes/system._run_shutdown_sequence``) runs one Stop all and then
    SIGINTs the server, so the lifespan's pass comes second there; the
    shell SIGTERMs the process group only when the server is still up 30 s
    after its POST (``desktop/src/main/server.ts``).

``stop_all_runs`` blocks until every generator has actually exited
(or been SIGKILLed), each run's run-end cleanup has swept its run folder
or run out its bounded wait (a cleanup is waited for once, however many
passes run), and container cleanup has finished, so the endpoint can
honestly report "done". Server shutdown then calls ``wait_for_run_reaps``,
which waits, bounded, for every run to leave the registry before the
lifespan ends.
"""

import asyncio
import json
import logging
import os

from process_signals import signal_process_group
import signal as sig_module
import weakref

from database import get_db_connection
from run_ends import mark_run_ended

from run_registry import active_managers, cancel_holds, channel_busy, mark_stop_requested

_logger = logging.getLogger("runs.teardown")


# Grace each generator gets to exit on SIGTERM before we SIGKILL it.
# Generous because a Docker-using generator spends this window calling
# ``docker stop`` on its own container, which itself waits ~10s for the
# container's PID 1 to exit. Deliberately shorter than the per-run /stop
# ladder's 15 s (control.py), for the desktop shell's quit, which SIGTERMs
# the server's process group if it is still up 30 s after the shell's
# POST /api/system/shutdown and SIGKILLs it 5 s later
# (desktop/src/main/server.ts). That quit waits 0.3 s (the shutdown
# sequence), then its Stop all's status pass (at most one 5 s busy
# timeout), this grace and CLEANUP_WAIT_SECONDS: 27.3 s at most before that
# Stop all's container teardown. The lifespan's Stop all that
# follows waits again only for a run started since the first one's
# snapshot, never for a cleanup the first waited out (_waited_out), and
# REAP_WAIT_SECONDS adds 2 s: 29.3 s in all. The rest of the 30 s must hold
# the container teardown and, at the loop close, what a late
# cleanup has left to do: a SIGTERM that lands before the process has
# exited ends it without them (see wait_for_run_reaps).
# Override per-deployment.
DEFAULT_GRACE_SECONDS = float(
    os.environ.get("COLLIMATE_STOP_ALL_GRACE_SECONDS") or 12.0
)

# How long each run's run-end cleanup (ipc._wait_and_cleanup: finalize, seal,
# sweep the run folder into its Logs, drop its sessions) gets once its process
# is gone. Every run waits concurrently, so the whole wait is bounded by this
# too. A cleanup that outlasts it is logged and keeps going, and no later
# Stop all waits for it again.
CLEANUP_WAIT_SECONDS = 10.0

# The run-end cleanup tasks a Stop all waited CLEANUP_WAIT_SECONDS for without
# seeing them end. Every later pass leaves them out of its wait, so a cleanup
# is waited for once however many passes run: the desktop shell's quit runs
# two (see DEFAULT_GRACE_SECONDS), and its budget has no room for a second
# full wait. Weak, so it keeps no task alive.
_waited_out: weakref.WeakSet = weakref.WeakSet()

# Server shutdown only: how long, once stop_all_runs has returned, its runs
# get to leave the registry (wait_for_run_reaps, which only waits).
REAP_WAIT_SECONDS = 2.0


def _stop_row(conn, run_id: str, stop_type: str | None, *, event: bool = True) -> str | None:
    """End ``run_id``'s row 'stopped' (with ``ended_at``) while it is still
    'running' and, with a ``stop_type``, note the stopped row with it and
    (``event``) record the ``stop`` run event carrying it, when the row is
    stopped (by this stop or an earlier one: a Force Stop after a graceful
    Stop is recorded as forced). A row that already ended (completed,
    failed, ...) keeps its status, its note and gets no stop event. Returns
    the row's status after the write, None when there is no row. Leaves the
    commit to the caller."""
    mark_run_ended(conn, run_id, status="stopped")
    row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    status = row["status"] if row else None
    if stop_type is not None and status == "stopped":
        _note_stop(conn, run_id, stop_type)
        if event:
            _insert_stop_event(conn, run_id, stop_type)
    return status


def _note_stop(conn, run_id: str, stop_type: str) -> None:
    """The stopped row's note from its stop_type ('force stopped', ...),
    replacing any earlier one: a Force Stop after a graceful Stop is noted
    forced, as its log bookend reads. A stopped row's note is always the
    stop's (``_op_finish`` writes nothing while ``stop_requested`` is set)."""
    conn.execute(
        "UPDATE runs SET note=? WHERE id=? AND status='stopped'",
        (stop_type.replace("_", " "), run_id),
    )


def _insert_stop_event(conn, run_id: str, stop_type: str) -> None:
    conn.execute(
        "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, 'stop', ?)",
        (run_id, json.dumps({"stop_type": stop_type})),
    )


def mark_run_stopped(channel: str, run_id: str, stop_type: str | None = None, *,
                     event: bool = True) -> str | None:
    """``_stop_row`` for one run. A stop that signals a run calls it BEFORE
    the signal, with its ``stop_type`` and ``event=False``: the run's own
    finalize (and its finish op) only writes a row still 'running', so a run
    the stop reached is recorded 'stopped' however fast it then exits, one
    that ended before the stop keeps its status, and every ``run_status``
    broadcast after the signal carries the stop's note; its ``stop`` event
    follows the log flush (``record_stop_event``). Blocking: callers on the
    loop run it via ``asyncio.to_thread``."""
    conn = get_db_connection(channel)
    try:
        status = _stop_row(conn, run_id, stop_type, event=event)
        conn.commit()
        return status
    finally:
        conn.close()


def record_stop_event(channel: str, run_id: str, stop_type: str) -> None:
    """Record a ``stop`` run event carrying ``stop_type``: the per-run Stop
    writes it after flushing the run's log tail, once ``mark_run_stopped``
    has found the row stopped and noted it. Blocking, like
    ``mark_run_stopped``."""
    conn = get_db_connection(channel)
    try:
        _insert_stop_event(conn, run_id, stop_type)
        conn.commit()
    finally:
        conn.close()


async def _mark_runs_stopped(by_channel: dict[str, list[str]],
                             stop_type: str) -> dict[str, str | None]:
    """``_stop_row`` for the runs of every channel in ``by_channel``, so Stop
    all records its runs in one pass before it signals any of them. One
    connection per channel, the channels written concurrently, so a channel
    db held by another writer delays the pass by its busy timeout, not by
    one timeout per channel. Each channel is marked busy until its write is
    done (registry.channel_busy), so deletion never removes the database
    under it. Returns ``{run_id: status}`` for the runs it could write; a
    channel whose db fails is logged and left out."""
    def write(channel: str, run_ids: list[str]) -> dict[str, str | None]:
        conn = get_db_connection(channel)
        try:
            out = {run_id: _stop_row(conn, run_id, stop_type) for run_id in run_ids}
            conn.commit()
            return out
        finally:
            conn.close()

    async def one(channel: str, run_ids: list[str]) -> dict[str, str | None]:
        with channel_busy(channel):
            try:
                return await asyncio.to_thread(write, channel, run_ids)
            except Exception:
                _logger.warning(
                    "stop_all: failed to mark runs %s stopped in channel %s",
                    ", ".join(run_ids), channel, exc_info=True,
                )
                return {}

    statuses: dict[str, str | None] = {}
    for part in await asyncio.gather(
            *(one(channel, run_ids) for channel, run_ids in by_channel.items())):
        statuses.update(part)
    return statuses


async def wait_for_run_cleanups(snapshot, timeout: float | None = None) -> list[str]:
    """Wait, concurrently and at most ``timeout`` seconds
    (``CLEANUP_WAIT_SECONDS`` by default), for the run-end cleanup task
    (``manager._cleanup_task``) of every ``(run_id, manager)`` in
    ``snapshot`` except one an earlier call already waited out
    (``_waited_out``), and return the run ids whose cleanup is still going
    when it returns, a waited-out one included. The tasks are never
    cancelled here: one that outlasts the bound keeps going, and its run
    stays registered until its reaper removes it (at server shutdown, see
    ``wait_for_run_reaps``)."""
    going = {
        task: run_id for run_id, manager in snapshot
        if (task := manager._cleanup_task) is not None and not task.done()
    }
    pending = [task for task in going if task not in _waited_out]
    if pending:
        if timeout is None:
            timeout = CLEANUP_WAIT_SECONDS
        _done, still = await asyncio.wait(pending, timeout=timeout)
        _waited_out.update(still)
        if still:
            _logger.warning(
                "stop_all: run-end cleanup of %s did not finish within %gs; it "
                "keeps going and its run stays registered until it ends",
                ", ".join(sorted(going[task] for task in still)), timeout)
    return sorted(run_id for task, run_id in going.items() if not task.done())


async def wait_for_run_reaps(timeout: float | None = None) -> list[str]:
    """Server shutdown's last run step, after ``stop_all_runs``: wait at
    most ``timeout`` seconds (``REAP_WAIT_SECONDS`` by default) for every
    spawn in flight to unwind (create.wait_for_spawns) and every
    run to leave the registry through its own remover (a run's reaper,
    helpers._reap_process, once its run-end cleanup is over; a start's unwind), and
    return the run ids still registered then (logged; normally none). So a
    later lifespan in the same process (tests, an embedded server) normally
    finds no previous one's runs, whose rows and run folders its startup
    sweeps would pass over as live.

    It only waits: it never cancels a run-end cleanup and never removes a
    run. A cleanup still going at the bound keeps going after the lifespan
    ends (the data dir's server lock released by then), and the signal
    that stopped the server decides how it ends. After a SIGINT (Ctrl-C,
    the desktop shell's quit inside its 30 s), the loop close (asyncio.run's,
    under uvicorn) cancels it once and waits for it: a cancel that lands
    before the cleanup's ``finally`` lets that ``finally`` run all its
    teardown steps (ipc._wait_and_cleanup), one that lands inside it ends it
    at the step it is on. After a SIGTERM (the shell's escalation, kill, a
    service manager), uvicorn re-raises the signal with its default action
    once the lifespan returns: the process ends at once, and the cleanup
    gets no cancel and runs none of its ``finally``. The next start's crash
    recovery and run folder and account lease sweeps clear what that
    leaves, but the folder sweep keeps only the version tip's files in the
    run's Logs: the folder's drafts and in-memory-only files are dropped,
    and log lines the run still buffered are lost."""
    if timeout is None:
        timeout = REAP_WAIT_SECONDS
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # Runs accepted before the shutdown whose spawn is still in flight: their
    # holds are cancelled (stop_all_runs), so each unwinds as stopped at its
    # last gate. Wait for that before the persistence writer stops.
    from routes.runs.create import wait_for_spawns
    await wait_for_spawns(timeout)
    while left := active_managers.items():
        if loop.time() >= deadline:
            ids = sorted(run_id for run_id, _m in left)
            _logger.warning(
                "shutdown: %s did not leave the registry within %gs",
                ", ".join(ids), timeout)
            return ids
        await asyncio.sleep(0.05)
    return []


async def stop_all_runs(grace_seconds: float = DEFAULT_GRACE_SECONDS) -> dict:
    """Stop every active run and tear down run-owned containers.

    Returns ``{"requested", "killed", "terminals"}``. Blocks until every
    generator has exited (or been force-killed) and container teardown has
    completed.
    """
    # Cancel every starting hold in the same synchronous step as the
    # snapshot, so a start either registered before it (and is stopped
    # below) or unwinds at its last gate.
    cancel_holds()
    snapshot = active_managers.items()  # list snapshot — safe to iterate
    requested = len(snapshot)
    for _run_id, manager in snapshot:
        mark_stop_requested(manager)

    # The runs this sweep stops: every one still running, or registered and
    # not spawned yet. A run whose process already exited is ending on its
    # own; its run-end cleanup records its status from the exit, as for a
    # per-run Stop (control.stop_run), and it stays registered until then.
    reached = [
        (run_id, m) for run_id, m in snapshot
        if getattr(m, "process", None) is None or m.process.returncode is None
    ]

    # Phase 1: record every reached run stopped in its DB BEFORE any
    # signal. A run that exits promptly on SIGTERM is finalized from its
    # exit a moment later, and that write only touches a row still
    # 'running'; recording first means every run this sweep reached reads
    # 'stopped', however fast it exits and however many runs there are. A
    # row that already ended (the generator's finish op) keeps its status.
    # One pass, off the loop, the channels in parallel: the signals wait
    # at most one busy timeout (5 s) for a channel db another writer holds.
    by_channel: dict[str, list[str]] = {}
    for run_id, m in reached:
        if getattr(m, "channel", None):
            by_channel.setdefault(m.channel, []).append(run_id)
    statuses = await _mark_runs_stopped(by_channel, "stopped_all")

    # Phase 2: signal everything at once so the per-generator Docker stops
    # run in parallel, not serially within the grace window. A manager
    # registered but not yet spawned (process=None) has nothing to signal,
    # so it is told to abort its spawn instead (ProcessManager.abort_spawn).
    # Then tell the UI, so cells flip out of "running" now rather than only
    # once the process finally dies: a reached run with the status its row
    # ended with, an exited one with its final status once it has one (its
    # finalize broadcasts it otherwise).
    live = []  # (run_id, manager, pid)
    for run_id, manager in snapshot:
        proc = manager.process
        if proc is None:
            abort = getattr(manager, "abort_spawn", None)
            if abort is not None:
                abort()
        elif proc.returncode is None:
            signal_process_group(proc.pid, sig_module.SIGTERM)
            live.append((run_id, manager, proc.pid))
        else:
            # The leader may be gone while descendants still own its pipes.
            # Signal its session group without changing the completed run row.
            signal_process_group(proc.pid, sig_module.SIGTERM, known_group=True)
    reached_ids = {run_id for run_id, _m in reached}
    for run_id, manager in snapshot:
        status = ((statuses.get(run_id) or "stopped") if run_id in reached_ids
                  else manager.final_status)
        if not status:
            continue
        try:
            manager._broadcast_run_status(status)
        except Exception:
            _logger.debug(
                "stop_all: run_status broadcast failed for run %s",
                run_id, exc_info=True,
            )

    # Phase 3: give the grace window for graceful exit; early-out when all
    # processes have gone.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_seconds
    while live and loop.time() < deadline:
        if all(m.process.returncode is not None for _, m, _ in live):
            break
        await asyncio.sleep(0.2)

    # Phase 4: SIGKILL stragglers and release their IPC sockets (on a clean
    # exit the manager's own teardown closes these; on SIGKILL it never
    # runs, so do it here to free the port + fd immediately).
    killed = 0
    for run_id, manager, pid in live:
        if manager.process.returncode is None:
            signal_process_group(pid, sig_module.SIGKILL)
            killed += 1
        srv = manager._ipc_server
        if srv is not None:
            try:
                srv.close()
            except Exception:
                _logger.debug(
                    "stop_all: IPC server close failed for run %s (already closed?)",
                    run_id, exc_info=True,
                )

    # Phase 4.2: wait, bounded, for every run's run-end cleanup, but not
    # again for one an earlier pass already waited out. It sweeps the run's
    # folder (the card included, its files kept in the run's Logs) and drops
    # its drafts and sessions; at server shutdown the loop closes soon after
    # this returns (the lifespan's reap wait, wait_for_run_reaps, is 2 s), so
    # without this wait a folder would be left to the next start's sweep.
    await wait_for_run_cleanups(snapshot)

    # Phase 4.5: reap each run's labeled Docker containers. Graceful exits
    # already removed theirs (this finds nothing); a SIGKILLed straggler's
    # `finally: env.close()` never ran, and when the caller is the lifespan
    # hook its _reap_process backstop comes after the run has left the
    # registry, which is all the lifespan waits for (wait_for_run_reaps),
    # so the loop's close can cut it off — so reap here, synchronously,
    # before the process exits. Idempotent and cheap when there's nothing
    # to reap.
    if live:
        from .helpers import _reap_run_containers
        try:
            await asyncio.gather(
                *(asyncio.to_thread(_reap_run_containers, run_id)
                  for run_id, _m, _pid in live),
            )
        except Exception:
            _logger.warning(
                "stop_all: per-run container reap failed", exc_info=True,
            )

    # Phase 4.6: reclaim each stopped run's temp app tree, for the same reason
    # as 4.5 — when the caller is the lifespan hook, a run's reaper reaches
    # its own reclaim only after the run has left the registry, and the
    # loop's close can cut it off there — and no startup sweep reclaims
    # .collimate/tmp, so every run of the session could leave its
    # generator-tree copy behind. That includes a run whose
    # cleanup outlasted the bounded wait above: its process is gone, and no
    # run-end cleanup step reads the tree (finalize, the sweep, the session,
    # environment and lease steps work from the DB, the cell and
    # the workspace). The reaper's own reclaim later finds nothing.
    # Workspaces (``ws_*``) are untouched here as everywhere else.
    if snapshot:
        from .helpers import _reclaim_run_app_dir
        for _rid, manager in snapshot:
            await asyncio.to_thread(_reclaim_run_app_dir, manager)

    # Phase 4.7: drop any live terminal PTY sessions. Their `docker exec`
    # clients run in their own sessions (start_new_session=True), so they
    # are NOT hung up on when this server exits — without an explicit
    # sweep they linger as orphans on the host.
    terminals = 0
    try:
        from sessions.terminal import shutdown_all_sessions
        terminals = shutdown_all_sessions()
    except Exception:
        _logger.warning(
            "stop_all: terminal session sweep failed", exc_info=True,
        )

    from state import flush_persist_state
    await asyncio.to_thread(flush_persist_state)
    return {
        "requested": requested, "killed": killed,
        "terminals": terminals,
    }
