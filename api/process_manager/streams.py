"""Stream draining, microbatch writer, and SSE broadcast helpers."""
# pyright: reportAttributeAccessIssue=false
# (mixin: attributes like log_buffer/workspace_dir live on the composed
# ProcessManager in manager.py, not on the mixin class itself)

import asyncio
import logging
import re
import time

from database import get_db_connection

# Matches 7-bit C1 ANSI sequences.
ANSI_ESCAPE_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# asyncio StreamReader limit for run stdout/stderr (bytes per line). Both
# spawn paths (backends.py native, docker_runner.py docker) pass it.
STREAM_LINE_LIMIT = 1_048_576

# Stands in for a line longer than STREAM_LINE_LIMIT, which is dropped.
_OVERSIZE_LINE_MARKER = b"[log line over 1 MiB dropped]\n"


async def _discard_through_newline(stream: asyncio.StreamReader, consumed: int) -> None:
    """Drop an over-limit line: the ``consumed`` bytes readuntil left
    buffered, then everything through the next newline (or EOF), never
    holding more than the reader limit."""
    while True:
        await stream.readexactly(consumed)  # already buffered, returns at once
        try:
            await stream.readuntil(b"\n")
            return
        except asyncio.IncompleteReadError:
            return
        except asyncio.LimitOverrunError as e:
            consumed = e.consumed


def run_status_event(channel: str, run_id: str, status: str,
                     failure_reason: str | None = None) -> dict:
    """The ``run_status`` wire record of a run whose terminal status was
    written: ``status`` and ``failure_reason`` as the writer decided them,
    ``ended_at`` (UTC ISO with offset), ``note`` and ``outcome`` read from its
    row. A row that cannot be read sends those three as null."""
    from collimate._host.channel_events import RunStatus
    from time_wire import iso

    ended_at = note = outcome = None
    try:
        conn = get_db_connection(channel, create=False)
        try:
            row = conn.execute("SELECT ended_at, note, outcome FROM runs WHERE id=?", (run_id,)).fetchone()
        finally:
            conn.close()
        if row is not None:
            ended_at, note, outcome = iso(row[0]), row[1], row[2]
    except Exception:
        logging.getLogger(__name__).debug("run_status: run row %s unreadable", run_id, exc_info=True)
    return RunStatus(run_id, status, failure_reason, ended_at, note, outcome).to_dict()


class StreamsMixin:
    def _writer_should_continue(self) -> bool:
        """Keep flushing while the subprocess is alive. Don't key off
        ``_cleanup_task`` here — that task IS ``_wait_and_cleanup``, which
        itself awaits the writer; using cleanup_task.done() as a predicate
        would deadlock."""
        return self.process is not None and self.process.returncode is None

    # ------------------------------------------------------------------
    # Stream draining — stdout/stderr go straight to log buffer
    # ------------------------------------------------------------------

    async def _drain_stdout(self):
        """Forward stdout to the log buffer (human-readable logs only).

        Line-buffered: reads until each newline so long lines are never
        split across event rows (which used to corrupt indentation and
        break downstream grep-style log searches).
        """
        assert self.process and self.process.stdout
        await self._drain_stream(self.process.stdout, "stdout")

    async def _drain_stderr(self):
        """Forward stderr to the log buffer (line-buffered, like stdout)."""
        assert self.process and self.process.stderr
        await self._drain_stream(self.process.stderr, "stderr")

    async def _drain_stream(self, stream: asyncio.StreamReader, kind: str):
        # Line-buffered drain. The subprocess is spawned with a
        # STREAM_LINE_LIMIT StreamReader limit (backends.py native spawn,
        # docker_runner.py docker spawn), so lines up to that size stay
        # intact. A longer line is dropped whole and replaced by one
        # marker row; the lines around it are untouched.
        while True:
            try:
                chunk = await stream.readuntil(b"\n")
            except asyncio.IncompleteReadError as e:
                chunk = e.partial  # EOF: last unterminated line, b"" at clean EOF
            except asyncio.LimitOverrunError as e:
                await _discard_through_newline(stream, e.consumed)
                chunk = _OVERSIZE_LINE_MARKER
            if not chunk:
                break
            clean = ANSI_ESCAPE_REGEX.sub("", chunk.decode("utf-8", errors="replace"))
            if clean:
                self.log_buffer.append({"type": kind, "payload": clean})
                # First real output ⇒ the generator is doing work, not just
                # spinning up. Idempotent (no-ops once past spinning_up).
                self._maybe_auto_running()

    # ------------------------------------------------------------------
    # Microbatch SQLite writer
    # ------------------------------------------------------------------

    async def _microbatch_writer(self):
        """Writes logs to SQLite in batches every 500ms to prevent DB locks.

        Skips the thread-pool round-trip on idle ticks — when nothing has
        been appended since the last flush, there is nothing to write, so
        paying for a thread switch just to run an early-return is pure
        overhead (fires ~2x/sec per live run, multiplied by every run).

        After the worker exits we keep looping as long as the buffer is
        non-empty (e.g. the last _flush_log_buffer put items back because
        all three DB write attempts failed) so the retry eventually wins
        rather than silently dropping the tail.
        """
        while self._writer_should_continue():
            await asyncio.sleep(0.5)
            if not self.log_buffer:
                continue
            await asyncio.to_thread(self._flush_log_buffer)

        # Final flush after process exits. If the flush re-queues the batch
        # (write errors), back off and retry up to ~5s before giving up so
        # we don't spin forever when the DB is genuinely unreachable.
        await asyncio.sleep(0.1)
        for _ in range(10):
            if not self.log_buffer:
                break
            await asyncio.to_thread(self._flush_log_buffer)
            if self.log_buffer:
                await asyncio.sleep(0.5)

    def _flush_log_buffer(self):
        # One flusher at a time (microbatch writer, stop_run, end-of-run
        # cleanup all call this from worker threads): keeps pop order equal
        # to insert order and SSE order equal to id order.
        with self._log_flush_lock:
            if not self.log_buffer:
                return
            batch = []
            while True:
                try:
                    batch.append(self.log_buffer.popleft())
                except IndexError:
                    break
            if not batch:
                return
            rows = None
            for attempt in range(3):
                conn = None
                try:
                    conn = get_db_connection(self.channel)
                    conn.executemany(
                        "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, ?, ?)",
                        [(self.run_id, item["type"], item["payload"]) for item in batch],
                    )
                    # Read back the ids + created_at we just wrote so we can
                    # broadcast each event through the per-channel SSE stream
                    # (see _broadcast_run_events below — routed through the
                    # channel's single SSE to avoid per-run EventSource cap).
                    # Read inside the still-open write transaction, before
                    # commit: no other writer can interleave, so the newest
                    # len(batch) rows for the run are exactly ours.
                    rows = conn.execute(
                        "SELECT id, event_type, payload, created_at "
                        "FROM run_events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                        (self.run_id, len(batch)),
                    ).fetchall()
                    conn.commit()
                    break
                except Exception:
                    rows = None
                    if attempt < 2:
                        time.sleep(0.3)
                    # Last attempt failed — put items back so the next flush
                    # picks them up rather than losing log lines, and tag a
                    # single warning row so the user sees that the log stream
                    # had trouble persisting.
                    else:
                        self.log_buffer.extendleft(reversed(batch))
                        if not getattr(self, "_flush_warn_emitted", False):
                            self.log_buffer.append({
                                "type": "stderr",
                                "payload": "[logs] DB write failed 3x — retrying. Some log ordering may be delayed.\n",
                            })
                            self._flush_warn_emitted = True
                finally:
                    if conn is not None:
                        conn.close()  # an uncommitted transaction rolls back
            if rows is not None:
                try:
                    self._broadcast_run_events(reversed(rows))
                except Exception:
                    # The rows are persisted; reconnect/replay recovers them.
                    logging.getLogger(__name__).exception("run_events broadcast failed")

    def _broadcast_run_status(self, status: str, failure_reason: str | None = None) -> None:
        """Announce a run's terminal status on the channel SSE.

        Emitted by the explicit ``finish`` IPC handler (clean shutdown
        initiated by the generator), ``_finalize_run_status`` (process exit
        without a finish message) and the stop routes (control.py,
        teardown.py; status ``stopped``). The frontend uses this
        as the signal to close its in-memory run state and render the
        final status — a replacement for the old per-run SSE's synthetic
        ``stream_end`` event. ``failure_reason`` is the runs column: null
        for clean terminations and user stops. The run row's ``ended_at``,
        ``note`` and ``outcome`` ride along (``run_status_event``). Every
        client then hears ``runs_active_changed`` (the cross-channel running
        count).
        """
        from events import broadcast
        from routes.system import broadcast_runs_active_changed
        broadcast(self.channel, run_status_event(self.channel, self.run_id, status, failure_reason))
        broadcast_runs_active_changed(self.channel)

    def _broadcast_run_phase(self, phase: str, message: str = "") -> None:
        """Announce a run's lifecycle phase on the channel SSE.

        Parallels ``_broadcast_run_status`` but for the within-run phase
        (queued → spinning_up → running → terminal). The frontend drives the
        cell's loading/spinning-up vs actively-running indicator straight off
        this message — no log parsing — so it lands immediately, ahead of the
        eventual ``type=phase`` run_event (which carries the drawer log line).
        """
        from events import broadcast
        from collimate._host.channel_events import RunPhase
        broadcast(self.channel, RunPhase(self.run_id, phase, message).to_dict())

    def _apply_phase(self, phase: str, message: str = "") -> None:
        """Transition the run to ``phase`` — the single apply point shared by
        the backend-auto machine and the generator-driven path (gen.phase).

        Three idempotent side-effects behind a no-op guard:
          1. Persist the authoritative ``runs.phase`` column (so reconnect can
             recover it in one read — see queries.fetch_run_events_since),
             only when the phase changes: a new message for the same phase
             just logs and broadcasts.
          2. Append a ``type="phase"`` row to ``log_buffer`` — rides the
             existing microbatch flush, so it persists to run_events AND
             reaches the drawer's canonical log like any other event.
          3. Broadcast ``run_phase`` on the channel SSE for the indicator.

        Synchronous on purpose: the DB write is a single tiny UPDATE (same
        shape as _op_finish's status write) so it runs inline from the async
        IPC handlers. Phase is a *lifecycle* signal — fired a handful of
        times per run — not per-item progress (that's gen.progress), so the
        per-call connection open/close is well within budget.
        """
        from process_manager.phases import valid_phase
        if not valid_phase(phase):
            return
        # No-op guard: suppress duplicate rows when the auto-machine and a
        # generator converge on the same (phase, message) — every redundant
        # call would otherwise be a blocking sqlite write.
        if phase == self.current_phase and message == self.current_phase_message:
            return
        phase_changed = phase != self.current_phase
        self.current_phase = phase
        self.current_phase_message = message

        if phase_changed:
            from database import get_db_connection
            conn = None
            try:
                conn = get_db_connection(self.channel)
                conn.execute(
                    "UPDATE runs SET phase=? WHERE id=?", (phase, self.run_id),
                )
                conn.commit()
            except Exception:
                # A failed phase write is non-fatal — the run_event row (below)
                # still records the transition and drives the drawer; only the
                # single-read reconnect shortcut is degraded.
                pass
            finally:
                if conn is not None:
                    conn.close()

        import json
        self.log_buffer.append({
            "type": "phase",
            "payload": json.dumps({"phase": phase, "message": message}),
        })
        self._broadcast_run_phase(phase, message)


    def _maybe_auto_running(self) -> None:
        """Advance to ``running`` the first time the generator shows real
        activity (first update_cell or first stdout/stderr), unless it has
        already moved past spinning_up. Idempotent via _apply_phase's guard
        plus the PRE_RUNNING gate so an explicit gen.phase() isn't clobbered.
        """
        from process_manager.phases import RUNNING, PRE_RUNNING_PHASES
        if self.current_phase in PRE_RUNNING_PHASES:
            self._apply_phase(RUNNING)

    def _broadcast_run_events(self, rows) -> None:
        """Push freshly-persisted run_events rows through the channel SSE.

        Funnels every run's event stream through the single per-channel
        EventSource the frontend already maintains, so opening many chat
        cells doesn't exhaust the browser's per-host HTTP/1.1 connection
        cap (~6) with a persistent EventSource per run.
        """
        from events import broadcast
        from collimate._host.channel_events import RunEvents
        batch = []
        for row in rows:
            batch.append({
                    "id": row["id"],
                    "type": row["event_type"],
                    "payload": row["payload"],
                    "created_at": row["created_at"],
            })
            if len(batch) == 500:
                broadcast(self.channel, RunEvents(self.run_id, batch).to_dict())
                batch = []
        if batch:
            broadcast(self.channel, RunEvents(self.run_id, batch).to_dict())
