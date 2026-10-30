"""Run finalization and cleanup after process exit.

Terminal status and logs belong to the run's artifact store. Cleanup seals
writes and the run's annotation context (recorded as its annotations.json
log), delegates run-folder sweeping to cell_commits, closes owned sessions
and environments, and releases account leases and the proven workspace.
Startup crash recovery uses the same persisted log projection.
"""
# pyright: reportAttributeAccessIssue=false
# (mixin: attributes like log_buffer/workspace_dir live on the composed
# ProcessManager in manager.py, not on the mixin class itself)

import asyncio
import json
import os
import logging
from datetime import datetime, timezone

from database import get_db_connection
from process_signals import signal_process_group
from run_ends import mark_run_ended

_logger = logging.getLogger("process_manager.finalization")

# Cap for a derived failure_reason (last stderr line / error message) so a
# runaway traceback doesn't bloat the runs row or the SSE payload.
_FAILURE_REASON_MAX_CHARS = 300

# Human labels for the terminal statuses, used by the run_finished log
# bookend. Mirrored by the frontend's live label tables (lib/runAttach /
# RunStatusIndicator) — keep the strings identical.
TERMINAL_STATUS_LABELS = {
    "completed": "Completed",
    "failed": "Failed",
    "timed_out": "Timed out",
    "interrupted": "Interrupted",
}


def _truncate_reason(text: str) -> str:
    text = text.strip()
    if len(text) > _FAILURE_REASON_MAX_CHARS:
        return text[: _FAILURE_REASON_MAX_CHARS - 1] + "…"
    return text


def _last_stderr_line(payload: str) -> str:
    """Last non-empty line of a (possibly multi-line) stderr payload."""
    for line in reversed(str(payload).splitlines()):
        if line.strip():
            return line.strip()
    return ""


# How long the run-end cleanup waits for stdout and stderr to reach EOF
# after the process exits before it kills the run's process group.
OUTPUT_DRAIN_TIMEOUT = 10.0


def reclaim_run_workspace(manager) -> bool:
    """Remove only this run's proven mkdtemp workspace after cleanup readers end."""
    import shutil
    workspace = os.path.abspath(getattr(manager, "workspace_dir", "") or "")
    root = os.path.realpath(os.path.join(".collimate", "tmp"))
    prefix = f"ws_{str(manager.run_id)[:8]}_"
    if (os.path.dirname(workspace) != root or not os.path.basename(workspace).startswith(prefix)
            or os.path.realpath(workspace) != workspace):
        return False
    shutil.rmtree(workspace, ignore_errors=True)
    return True


async def wait_process_exit(process) -> None:
    """Return once ``process`` has exited. ``Process.wait()`` resolves only
    after every stdio pipe has closed, so a background child still holding
    them keeps it pending after the process is gone; the return code is set
    as soon as the process is reaped, so it is checked every half second as
    well."""
    waiter = asyncio.ensure_future(process.wait())
    try:
        while process.returncode is None and not waiter.done():
            await asyncio.wait({waiter}, timeout=0.5)
        if waiter.done():
            waiter.result()
    finally:
        waiter.cancel()


class FinalizationMixin:
    def _finalize_run_status(self):
        """Mark run as completed/failed/timed_out based on process exit.

        Only updates if the run is still 'running' — if the generator already
        sent an IPC 'finish' message (or a /stop endpoint marked it 'stopped'),
        that status takes precedence over the process exit code. Performed as
        a single conditional UPDATE (``run_ends.mark_run_ended``, which also
        stamps ``ended_at``) so a concurrent stop can't race between the
        SELECT and UPDATE and have its 'stopped' row silently overwritten.
        """
        if self.timeout_reason:
            # The wallclock watchdog killed the run — a distinct terminal
            # status, not a bare non-zero-exit 'failed'.
            candidate = "timed_out"
        elif self.process and self.process.returncode == 0:
            candidate = "completed"
        else:
            candidate = "failed"

        conn = get_db_connection(self.channel)
        try:
            if candidate == "timed_out":
                failure_reason = _truncate_reason(self.timeout_reason)
            elif candidate == "failed":
                failure_reason = self._derive_failure_reason(conn)
            else:
                failure_reason = None
            mark_run_ended(conn, self.run_id, status=candidate, failure_reason=failure_reason)
            conn.commit()
            row = conn.execute(
                "SELECT status, failure_reason FROM runs WHERE id=?", (self.run_id,)
            ).fetchone()
        finally:
            conn.close()
        final_status = row["status"] if row else None
        final_reason = row["failure_reason"] if row else None
        # Kept for stop_run's already-finished re-broadcast and the phase
        # /runs/active reports for an ending run.
        self.final_status = final_status

        if final_status:
            self._broadcast_run_status(final_status, final_reason)
            # Mirror the terminal status onto the phase so the cell indicator
            # leaves "running"/"spinning up" and the reconnect column is
            # authoritative. Single chokepoint for completed/failed/
            # stopped/timed_out (this runs on every run end via
            # _wait_and_cleanup).
            self._apply_phase(final_status, self.timeout_reason or "")
        # If the watchdog killed the process, surface the reason as a
        # dedicated log row so the frontend drawer shows "Timed out"
        # instead of a bare non-zero exit code.
        if self.timeout_reason and final_status in ("timed_out", "failed"):
            try:
                conn = get_db_connection(self.channel)
                try:
                    conn.execute(
                        "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, 'timeout', ?)",
                        (self.run_id, self.timeout_reason),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                _logger.warning(
                    "run %s: failed to record timeout event (%r)",
                    self.run_id, self.timeout_reason, exc_info=True,
                )

        # Persist the buffered tail (the terminal phase row, output drained
        # after the microbatch writer's final flush, cleanup notices) before
        # baking logs.txt, so the saved log matches the live feed. After
        # _derive_failure_reason, which reads that unflushed tail.
        self._flush_log_buffer()

        # Project the run log into the run's own artifact store. Config was
        # captured at kickoff (create_run) — nothing about
        # the run is committed onto the cell at run end, so version rows
        # stay exactly what the generator emitted.
        self._commit_run_log()

    def _derive_failure_reason(self, conn) -> str | None:
        """Best human-readable cause for a run that failed WITHOUT a clean
        terminal write: the finish event's error_message if one was recorded,
        else the last non-empty stderr line (unflushed log_buffer tail first,
        then the persisted run_events rows)."""
        row = conn.execute(
            "SELECT payload FROM run_events "
            "WHERE run_id=? AND event_type='finish' ORDER BY id DESC LIMIT 1",
            (self.run_id,),
        ).fetchone()
        if row is not None:
            try:
                msg = str(json.loads(row["payload"]).get("error_message", "") or "")
            except Exception:
                msg = ""
            if msg.strip():
                return _truncate_reason(msg)
        for entry in reversed(getattr(self, "log_buffer", None) or []):
            if entry.get("type") == "stderr":
                line = _last_stderr_line(entry.get("payload", ""))
                if line:
                    return _truncate_reason(line)
        row = conn.execute(
            "SELECT payload FROM run_events "
            "WHERE run_id=? AND event_type='stderr' AND TRIM(payload) != '' "
            "ORDER BY id DESC LIMIT 1",
            (self.run_id,),
        ).fetchone()
        if row is not None:
            line = _last_stderr_line(row["payload"])
            if line:
                return _truncate_reason(line)
        return None

    def _commit_run_log(self):
        commit_run_log(self.channel, self.run_id)


    async def _wait_and_cleanup(self):
        """Wait for the process to exit, then finalize status and clean up.

        The run end, in order: process exit → output drains →
        ``_finalize_run_status`` → (``finally``) close the IPC writer and
        server → seal the manager (every later emit is refused) → wait for
        the emits still on worker threads → sweep the run folder (one
        version deleting ``_runs/<label>/``, its drafts dropped, its files
        kept in the run's Logs) → drop this run's chat and terminal sessions
        → environments, account leases. Everything after the
        finalize sits in the ``finally``, each step guarded on its own, so
        every run-end path — clean exit, SIGTERM, SIGKILL, a finalize crash,
        this task cancelled while it waits (a loop shutting down) — reaches
        the sweep and the unregister. Stop all and server shutdown wait for
        it, bounded (``teardown.wait_for_run_cleanups``).
        """
        try:
            await wait_process_exit(self.process)

            # Wait for the output streams to fully drain — but bounded. The
            # pipes only hit EOF when every writer closes them; a background
            # process the generator left behind (inheriting stdout/stderr)
            # would otherwise wedge this gather forever, skipping the entire
            # finally block below and never finalizing the run's status.
            if hasattr(self, '_stdout_task'):
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            self._stdout_task, self._stderr_task,
                            return_exceptions=True,
                        ),
                        timeout=OUTPUT_DRAIN_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # A straggler in the generator's process group still
                    # holds the pipe. The run is over — kill the group
                    # (the generator was its own session leader, so its
                    # pgid == its pid even after the leader was reaped)
                    # and stop draining so teardown can proceed.
                    self.push_log(
                        "[cleanup] stdout/stderr never reached EOF — a "
                        "leftover background process is holding the pipe; "
                        "killing the run's process group"
                    )
                    import signal as _sig
                    signal_process_group(self.process.pid, _sig.SIGKILL, known_group=True)
                    for _t in (self._stdout_task, self._stderr_task):
                        _t.cancel()

            # Give IPC a moment to drain any final messages
            await asyncio.sleep(0.2)

            # Wait for the microbatch writer to finish its final flush before
            # we touch the DB — otherwise _finalize_run_status races with the
            # writer's last INSERT and both hit "database is locked".
            if hasattr(self, '_writer_task'):
                await self._writer_task

            await asyncio.to_thread(self._finalize_run_status)
        finally:
            # Each step independently guarded so one failure can't skip the
            # next.

            if self._ipc_server:
                try:
                    # Sever any still-open generator connection FIRST. A
                    # force-stopped CONTAINERIZED run outlives its docker
                    # client — the container keeps its TCP IPC connection
                    # open — and py3.12+ ``Server.wait_closed()`` waits for
                    # every live handler, wedging this teardown until the
                    # container exits on its own: forever, for a daemon.
                    # Closing the writer drops the handler; the bounded wait
                    # is the belt-and-suspenders for a straggler connection.
                    w = getattr(self, "_ipc_writer", None)
                    if w is not None:
                        try:
                            w.close()
                        except Exception:
                            pass
                    self._ipc_server.close()
                    await asyncio.wait_for(
                        self._ipc_server.wait_closed(), timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    _logger.warning(
                        "run %s: _ipc_server.wait_closed timed out; proceeding with teardown",
                        self.run_id)
                except Exception:
                    _logger.warning("run %s: _ipc_server.close failed", self.run_id, exc_info=True)

            # No write lands after this point: a container that still holds
            # a connection, or an emit already on a worker thread, is refused
            # under the cell lock once sealed; the in-flight ones finish
            # first, so the sweep below sees their files.
            self.sealed = True
            inflight = list(getattr(self, "_inflight_emits", ()) or ())
            if inflight:
                try:
                    await asyncio.wait(inflight, timeout=30.0)
                except Exception:
                    _logger.warning("run %s: in-flight emit wait failed", self.run_id, exc_info=True)

            # The run's annotation context closes once its last write landed:
            # no pull or post is answered after this.
            try:
                bells = await asyncio.to_thread(commit_run_annotations, self.channel, self.run_id)
                self._ring(bells)
            except Exception:
                _logger.warning("run %s: annotations commit failed", self.run_id, exc_info=True)

            # The run's folder goes with the run — its card included — and
            # what it held stays in the run's Logs.
            if self.cell_id:
                try:
                    from cell_commits import sweep_run_folder
                    await asyncio.to_thread(
                        sweep_run_folder, self.channel, self.cell_id,
                        self.run_id, self.label, generator_name=self.generator_name,
                    )
                except Exception:
                    _logger.warning("run %s: run folder sweep failed", self.run_id, exc_info=True)

            # This run's live sessions close with it — its chat and inbox sockets
            # and its PTYs — never a sibling run's on the same cell.
            try:
                from sessions.chat import unregister_sessions_for_run
                unregister_sessions_for_run(self.run_id)
            except Exception:
                _logger.warning("run %s: chat session unregister failed", self.run_id, exc_info=True)
            try:
                from sessions.terminal import unregister_sessions_for_run as _unregister_terminals
                _unregister_terminals(self.run_id)
            except Exception:
                _logger.warning("run %s: terminal session unregister failed", self.run_id, exc_info=True)
            # A surface the run opened but never registered keeps its
            # starting claim (and a cell-scoped path's lock) until here.
            try:
                from served_surfaces import release_run
                release_run(self.run_id)
            except Exception:
                _logger.warning("run %s: surface release failed", self.run_id, exc_info=True)

            # Host-placed environments are run-bound: front-door
            # teardown here, the run-id-label docker reaper as backstop.
            service = getattr(self, "_environment_service", None)
            if service is not None:
                try:
                    await asyncio.to_thread(service.close_all)
                except Exception:
                    _logger.warning("run %s: environment close_all failed", self.run_id, exc_info=True)

            # A killed generator cannot release its credential leases. Wait
            # until its CLI environments stop writing before collecting them.
            try:
                from accounts import release_run_account_leases
                await asyncio.to_thread(release_run_account_leases, self.run_id)
            except Exception:
                _logger.warning("run %s: account credential cleanup failed", self.run_id, exc_info=True)
            finally:
                getattr(self, "host_bindings", {}).clear()

            # Ensure all remaining logs are flushed after IPC server shuts down
            try:
                await asyncio.to_thread(self._flush_log_buffer)
            except Exception:
                _logger.warning("run %s: _flush_log_buffer failed", self.run_id, exc_info=True)

            try:
                from state import flush_persist_state
                await asyncio.to_thread(flush_persist_state)
            except Exception:
                _logger.warning("run %s: final layout flush failed", self.run_id, exc_info=True)
            await asyncio.to_thread(reclaim_run_workspace, self)

            # The run is over — retire the timeout watchdog. Without this it
            # sleeps out the full wall-clock cap (up to 30 min) holding a
            # reference to the whole manager.
            tt = getattr(self, "_timeout_task", None)
            if tt is not None and not tt.done():
                tt.cancel()


def commit_run_log(channel: str, run_id: str) -> None:
    """Project the run's events into ``run_artifacts(run_id, 'log',
    'logs.txt')`` — the run node's log, keyed by run rather than by a
    version row, so a run that crashed before its first emit still keeps
    its log and can never clobber another run's.

    Module-level (channel/run_id, not manager state) so the startup
    reconciler can bake the same bookended log for runs it resolves to
    'interrupted' after a crash."""
    # Collect the canonical log from run_events. This is the SAME
    # format_log_line projection the live drawer renders (see
    # frontend/src/lib/runLogFormat.ts), over the SAME rows, so the saved
    # logs.txt matches what the user watched live — labeled, timestamped,
    # with the allow-list dropping internal plumbing rows. (Previously
    # this filtered to raw stdout/stderr only, which both lost the
    # status/phase/error metadata and diverged from the live view.)
    from process_manager.log_format import format_log_line

    conn = get_db_connection(channel)
    try:
        rows = conn.execute(
            "SELECT event_type, payload, created_at FROM run_events "
            "WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()

        log_lines = []
        stop_type = ""
        commit_count = 0
        for row in rows:
            line = format_log_line(row["event_type"], row["payload"], row["created_at"])
            if line is not None:
                log_lines.append(line + "\n")
            if row["event_type"] == "stop":
                try:
                    stop_type = str(json.loads(row["payload"]).get("stop_type", "") or "")
                except Exception:
                    pass
            elif row["event_type"] == "commit":
                commit_count += 1

        # Bookend the events with the `RUN started/finished` banners — the
        # same lines the live client fabricates around its feed. They carry
        # the runner (and final status) the bar no longer shows, so the
        # persisted log is self-describing about what ran and how it ended.
        run_row = conn.execute(
            "SELECT runner_id, status, created_at FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run_row is not None:
            runner_id = run_row["runner_id"] or ""
            started = format_log_line(
                "run_started",
                json.dumps({"runner": runner_id}),
                run_row["created_at"],
            )
            if started is not None:
                log_lines.insert(0, started + "\n")
            # Final status label — mirrors the live client's handleStatus
            # mapping so live and persisted bookends read identically.
            # timed_out/interrupted are checked before the stop branches,
            # in the same order handleStatus uses.
            status = str(run_row["status"] or "")
            if status == "completed" and not stop_type:
                label = TERMINAL_STATUS_LABELS["completed"]
                # A completed run that never committed a cell produced nothing
                # visible — say so, in the bookend AND as a machine-readable
                # run_event (the frontend live label table mirrors this).
                if commit_count == 0:
                    label = "Completed · no output"
                    try:
                        conn.execute(
                            "INSERT INTO run_events (run_id, event_type, payload) "
                            "VALUES (?, 'no_output', ?)",
                            (run_id, json.dumps({
                                "message": "Run completed without committing any output",
                            })),
                        )
                        # The row ended before this event existed: land its
                        # 'no output' note now (a note the run set is kept).
                        mark_run_ended(conn, run_id, status=status)
                    except Exception:
                        _logger.warning(
                            "run %s: failed to record no_output event", run_id,
                            exc_info=True,
                        )
            elif status in ("timed_out", "interrupted"):
                label = TERMINAL_STATUS_LABELS[status]
            elif stop_type == "force_stopped":
                label = "Stopped (forced)"
            elif stop_type == "gracefully_stopped":
                label = "Stopped (graceful)"
            elif status in TERMINAL_STATUS_LABELS:
                label = TERMINAL_STATUS_LABELS[status]
            else:
                label = status or "Unknown"
            finished_at = (
                rows[-1]["created_at"] if rows
                else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            )
            finished = format_log_line(
                "run_finished",
                json.dumps({"status": label, "runner": runner_id}),
                finished_at,
            )
            if finished is not None:
                log_lines.append(finished + "\n")

        if not log_lines:
            return

        log_text = "".join(log_lines)

        from run_artifacts import RUN_LOG_FILE, record_artifact_bytes

        record_artifact_bytes(
            conn, run_id, "log", RUN_LOG_FILE, log_text.encode("utf-8"),
        )
        conn.commit()
    finally:
        # close() without commit rolls back a pending write (the no_output
        # INSERT) so a failure here never leaves the channel DB locked.
        conn.close()

def commit_run_annotations(channel: str, run_id: str) -> list:
    """Seal the run's annotation context and, when it holds a message,
    record it as ``run_artifacts(run_id, 'log', 'annotations.json')`` in the
    same transaction, so no pull or post lands after it. Idempotent (a
    sealed context records nothing new). Returns the doorbells to ring."""
    import annotations
    from run_artifacts import RUN_ANNOTATIONS_FILE, record_artifact_bytes

    conn = get_db_connection(channel)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cell_id = annotations.run_context_cell(conn, run_id)
        if cell_id is None:
            conn.rollback()
            return []
        bells = annotations.seal(conn, cell_id, run_id)
        if bells and annotations.has_messages(conn, cell_id, run_id):
            record_artifact_bytes(conn, run_id, "log", RUN_ANNOTATIONS_FILE,
                                  annotations.dump(conn, cell_id, run_id))
        conn.commit()
        return bells
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_interrupted_runs() -> None:
    # 2.2 Crash recovery, AFTER the state restore: a run row left at
    # status='running' by a previous server process can never finish (its
    # manager died with the process), so without this sweep it stays
    # "running" forever in every listing.
    # Resolve each one to the terminal 'interrupted' status and bake its
    # persisted log (same bookended projection finalization writes), BEFORE
    # requests are served so no client ever observes the stale rows.
    try:
        import glob as _glob
        from database import get_db_connection as _get_channel_db
        from run_registry import active_managers as _live_runs
        import annotations as _annotations
        _interrupted_reason = "Server restarted while the run was active"
        for _db_path in sorted(_glob.glob(".collimate/channels/*.db")):
            _chan = os.path.splitext(os.path.basename(_db_path))[0]
            try:
                _conn = _get_channel_db(_chan)
                try:
                    # Contexts whose run died with the previous process.
                    _unsealed = [r for r in _annotations.unsealed_runs(_conn) if r not in _live_runs]
                    # Skip runs with a live manager: repeated lifespan cycles in
                    # one process (tests, embedded servers) don't orphan them.
                    _stale = [
                        r["id"] for r in _conn.execute(
                            "SELECT id FROM runs WHERE status='running'"
                        ).fetchall()
                        if r["id"] not in _live_runs
                    ]
                    _stale = [
                        _rid for _rid in _stale
                        if mark_run_ended(_conn, _rid, status="interrupted",
                                          failure_reason=_interrupted_reason)
                    ]
                    if _stale:
                        _conn.executemany(
                            "UPDATE runs SET phase='interrupted' WHERE id=?",
                            [(_rid,) for _rid in _stale],
                        )
                        _conn.commit()
                finally:
                    _conn.close()
                for _rid in _stale:
                    # Bookended logs.txt ending in "RUN finished · Interrupted".
                    commit_run_log(_chan, _rid)
                for _rid in _unsealed:
                    commit_run_annotations(_chan, _rid)
                if _stale:
                    _logger.info(
                        "crash recovery: marked %d stale running run(s) "
                        "interrupted in channel %r", len(_stale), _chan,
                    )
            except Exception:
                _logger.exception("crash recovery failed for channel db %s", _db_path)
    except Exception:
        _logger.exception("crash recovery sweep failed")

    # 2.3 The dead runs' folders: every ``_runs/<label>/`` no live run owns
    # is swept from its cell — tip, in-memory copy and drafts — in one
    # version per cell and one persist, so the next emit on that cell can't
    # re-commit an orphan from the restored state.
    try:
        from cell_commits import sweep_orphan_run_folders
        _swept = sweep_orphan_run_folders()
        if _swept:
            _logger.info("crash recovery: swept dead run folders from %d cell(s)", _swept)
    except Exception:
        _logger.exception("run folder sweep failed")
