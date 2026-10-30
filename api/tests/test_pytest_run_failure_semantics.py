"""Terminal-status semantics added with the failure_reason column.

Covers:
  * migration v6: fresh channel DBs carry runs.failure_reason
  * _finalize_run_status: timeout_reason → status 'timed_out' with
    failure_reason = the reason (plus the 'timeout' run_event and the
    "Timed out" run_finished bookend in logs.txt)
  * _finalize_run_status: non-zero exit → 'failed' with failure_reason
    derived from the finish event's error_message, else the last
    non-empty stderr line
  * zero-output completion: 'completed' with no commit events gets the
    "Completed · no output" bookend + a 'no_output' run_event
  * startup reconciler: a run left 'running' by a dead server is marked
    'interrupted' on the next lifespan startup, with the bookended log
  * queries: failure_reason exposed in list/detail/events_since; the
    terminal set includes timed_out/interrupted
  * phases: awaiting_input is active/valid; the wallclock watchdog does
    not charge time spent in awaiting_input against the budget
  * duplicate_stack names copied cells "{name} (Copy)"
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from database import get_db_connection

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:run-failure-semantics:watchdog"))


def _seed_run(channel_id: str, status: str = "running", **cols) -> str:
    conn = get_db_connection(channel_id)
    try:
        rid = str(uuid.uuid4())
        keys = ["id", "prompt", "status"] + list(cols)
        vals = [rid, "seed", status] + list(cols.values())
        conn.execute(
            f"INSERT INTO runs ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
            vals,
        )
        conn.commit()
        return rid
    finally:
        conn.close()


def _seed_event(channel_id: str, rid: str, event_type: str, payload: str) -> None:
    conn = get_db_connection(channel_id)
    try:
        conn.execute(
            "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, ?, ?)",
            (rid, event_type, payload),
        )
        conn.commit()
    finally:
        conn.close()


def _run_row(channel_id: str, rid: str):
    conn = get_db_connection(channel_id)
    try:
        return conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()


def _logs_txt(channel_id: str, rid: str) -> str:
    """Read the persisted logs.txt for a run out of the CAS."""
    conn = get_db_connection(channel_id)
    try:
        row = conn.execute(
            "SELECT blob_hash FROM run_artifacts "
            "WHERE run_id=? AND category='log' AND filepath='logs.txt'",
            (rid,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "no logs.txt artifact recorded"
    h = row["blob_hash"]
    with open(os.path.join(".collimate", "objects", h[:2], h[2:]), "rb") as f:
        return f.read().decode("utf-8")


class _FinalizeDummy:
    """Minimal stand-in composing FinalizationMixin for direct finalize
    calls (mirrors the pattern in test_pytest_runs_lifecycle.py)."""

    def __init__(self, channel_id, rid, returncode=0, timeout_reason=None, log_buffer=None):
        self.channel = channel_id
        self.run_id = rid
        self.process = type("P", (), {"returncode": returncode})()
        self.cell_id = None
        self.timeout_reason = timeout_reason
        self.log_buffer = log_buffer or []
        self.broadcasts = []

    def _broadcast_run_status(self, status, failure_reason=None):
        self.broadcasts.append((status, failure_reason))

    def _apply_phase(self, _p, _m=""):
        pass

    def _flush_log_buffer(self):
        pass


def _finalize(channel_id, rid, **kw):
    from process_manager.finalization import FinalizationMixin

    class _D(_FinalizeDummy, FinalizationMixin):
        pass

    d = _D(channel_id, rid, **kw)
    d._finalize_run_status()
    return d


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_fresh_channel_db_has_failure_reason_column(client):
    conn = get_db_connection(client.channel_id)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    finally:
        conn.close()
    assert "failure_reason" in cols


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------

def test_finalize_timeout_lands_timed_out_with_reason(client):
    rid = _seed_run(client.channel_id, "running")
    d = _finalize(client.channel_id, rid, returncode=1, timeout_reason="Exceeded 5s timeout")

    row = _run_row(client.channel_id, rid)
    assert row["status"] == "timed_out"
    assert row["failure_reason"] == "Exceeded 5s timeout"
    assert d.broadcasts == [("timed_out", "Exceeded 5s timeout")]

    conn = get_db_connection(client.channel_id)
    try:
        ev = conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? AND event_type='timeout'",
            (rid,),
        ).fetchone()
    finally:
        conn.close()
    assert ev is not None and ev["payload"] == "Exceeded 5s timeout"
    assert "Timed out" in _logs_txt(client.channel_id, rid)


def test_finalize_failed_uses_finish_error_message(client):
    rid = _seed_run(client.channel_id, "running")
    _seed_event(client.channel_id, rid, "finish", json.dumps(
        {"success": False, "error_message": "boom: bad manifest"}
    ))
    _seed_event(client.channel_id, rid, "stderr", "irrelevant later stderr\n")
    _finalize(client.channel_id, rid, returncode=1)

    row = _run_row(client.channel_id, rid)
    assert row["status"] == "failed"
    assert row["failure_reason"] == "boom: bad manifest"


def test_finalize_failed_falls_back_to_last_stderr_line(client):
    rid = _seed_run(client.channel_id, "running")
    _seed_event(client.channel_id, rid, "stderr", "first noise\n")
    _seed_event(client.channel_id, rid, "stderr", "Traceback (most recent call last):\nValueError: nope\n")
    _finalize(client.channel_id, rid, returncode=1)

    row = _run_row(client.channel_id, rid)
    assert row["status"] == "failed"
    assert row["failure_reason"] == "ValueError: nope"
    assert "Failed" in _logs_txt(client.channel_id, rid)


def test_finalize_failed_reason_truncated(client):
    rid = _seed_run(client.channel_id, "running")
    _seed_event(client.channel_id, rid, "stderr", "x" * 1000)
    _finalize(client.channel_id, rid, returncode=1)
    reason = _run_row(client.channel_id, rid)["failure_reason"]
    assert reason is not None and len(reason) <= 300


def test_finalize_bakes_the_unflushed_log_tail_into_logs_txt(client):
    """Rows still in log_buffer at finalize (output drained after the
    microbatch writer's last flush, the terminal phase row) must reach
    logs.txt, so the saved log matches the live feed."""
    import threading
    from collections import deque

    from process_manager.finalization import FinalizationMixin
    from process_manager.streams import StreamsMixin

    class _D(StreamsMixin, FinalizationMixin):
        def __init__(self, rid):
            self.channel = client.channel_id
            self.run_id = rid
            self.process = type("P", (), {"returncode": 1})()
            self.cell_id = None
            self.timeout_reason = None
            self.log_buffer = deque([{"type": "stderr", "payload": "Traceback tail\n"}])
            self._log_flush_lock = threading.Lock()

        def _broadcast_run_events(self, rows):
            pass

        def _broadcast_run_status(self, status, failure_reason=None):
            pass

        def _apply_phase(self, phase, message=""):
            self.log_buffer.append({
                "type": "phase",
                "payload": json.dumps({"phase": phase, "message": message}),
            })

    rid = _seed_run(client.channel_id, "running")
    d = _D(rid)
    d._finalize_run_status()

    assert not d.log_buffer
    logs = _logs_txt(client.channel_id, rid)
    assert "Traceback tail" in logs
    assert "PHASE" in logs


def test_finalize_completed_zero_commits_gets_no_output(client):
    rid = _seed_run(client.channel_id, "running")
    _seed_event(client.channel_id, rid, "stdout", "hello\n")
    _finalize(client.channel_id, rid, returncode=0)

    row = _run_row(client.channel_id, rid)
    assert row["status"] == "completed"
    assert row["failure_reason"] is None
    assert "Completed · no output" in _logs_txt(client.channel_id, rid)

    conn = get_db_connection(client.channel_id)
    try:
        ev = conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? AND event_type='no_output'",
            (rid,),
        ).fetchone()
    finally:
        conn.close()
    assert ev is not None


def test_finalize_completed_with_commit_stays_plain_completed(client):
    rid = _seed_run(client.channel_id, "running")
    _seed_event(client.channel_id, rid, "commit", json.dumps(
        {"cell_id": "c1", "name": "Out", "sequence_num": 1, "file_count": 1}
    ))
    _finalize(client.channel_id, rid, returncode=0)

    txt = _logs_txt(client.channel_id, rid)
    assert "Completed" in txt
    assert "Completed · no output" not in txt


def test_commit_run_log_failure_releases_the_write_lock(client, monkeypatch):
    """A raise after the no_output INSERT must not leak the connection's
    write transaction: the INSERT rolls back and other writers proceed."""
    import sqlite3

    import run_artifacts
    from process_manager.finalization import commit_run_log

    rid = _seed_run(client.channel_id, "completed")
    _seed_event(client.channel_id, rid, "stdout", "hello\n")

    def _boom(*_a, **_kw):
        raise RuntimeError("cas write failed")

    monkeypatch.setattr(run_artifacts, "record_artifact_bytes", _boom)
    with pytest.raises(RuntimeError):
        commit_run_log(client.channel_id, rid)

    from state import require_channel
    channel_id = require_channel(client.channel_id).id
    conn = sqlite3.connect(f".collimate/channels/{channel_id}.db", timeout=0.1)
    try:
        conn.execute("PRAGMA busy_timeout=100")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, 'stdout', 'x')",
            (rid,),
        )
        conn.rollback()
        n = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND event_type='no_output'",
            (rid,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_commit_run_log_timed_out_label_wins_over_stop_event(client):
    """Label order mirrors handleStatus: timed_out/interrupted come before
    the stop-type branches."""
    from process_manager.finalization import commit_run_log

    rid = _seed_run(client.channel_id, "timed_out")
    _seed_event(client.channel_id, rid, "stop", json.dumps({"stop_type": "gracefully_stopped"}))
    commit_run_log(client.channel_id, rid)

    last = _logs_txt(client.channel_id, rid).rstrip("\n").splitlines()[-1]
    assert "Timed out" in last
    assert "Stopped" not in last


def _finish(channel_id, rid, *, stop_requested=False, timeout_reason=None, **data):
    from process_manager.ipc import IPCMixin

    class _F(IPCMixin):
        pass

    f = _F()
    f.channel = channel_id
    f.run_id = rid
    f.log_buffer = []
    f.stop_requested = stop_requested
    f.timeout_reason = timeout_reason
    f.broadcasts = []
    f._broadcast_run_status = lambda st, reason=None: f.broadcasts.append((st, reason))
    asyncio.run(f._op_finish({"op": "finish", **data}))
    return f


def test_finish_marks_a_running_row_failed_with_its_reason(client):
    rid = _seed_run(client.channel_id, "running")
    f = _finish(client.channel_id, rid, success=False, error_message="boom")
    row = _run_row(client.channel_id, rid)
    assert (row["status"], row["failure_reason"]) == ("failed", "boom")
    assert f.broadcasts == [("failed", "boom")]


def test_finish_marks_a_running_row_completed(client):
    rid = _seed_run(client.channel_id, "running")
    f = _finish(client.channel_id, rid, success=True)
    assert _run_row(client.channel_id, rid)["status"] == "completed"
    assert f.broadcasts == [("completed", None)]


def test_finish_after_a_stop_keeps_stopped(client):
    rid = _seed_run(client.channel_id, "stopped")
    f = _finish(client.channel_id, rid, stop_requested=True, success=False, error_message="Stopped by user")
    row = _run_row(client.channel_id, rid)
    assert row["status"] == "stopped" and row["failure_reason"] is None
    assert f.broadcasts == []


def test_finish_on_a_watchdog_stop_leaves_timed_out_to_finalize(client):
    rid = _seed_run(client.channel_id, "running")
    reason = "Exceeded 5s timeout"
    _finish(client.channel_id, rid, stop_requested=True, timeout_reason=reason,
            success=False, error_message="Stopped by user")
    assert _run_row(client.channel_id, rid)["status"] == "running"
    _finalize(client.channel_id, rid, returncode=1, timeout_reason=reason)
    row = _run_row(client.channel_id, rid)
    assert (row["status"], row["failure_reason"]) == ("timed_out", reason)


def test_finish_closes_its_connection_when_the_write_fails(client, monkeypatch):
    import process_manager.ipc as ipc_mod
    closed = []

    class _Conn:
        def execute(self, *a, **k):
            raise RuntimeError("locked")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(ipc_mod, "get_db_connection", lambda _c: _Conn())
    with pytest.raises(RuntimeError):
        _finish(client.channel_id, _seed_run(client.channel_id, "running"), success=False, error_message="boom")
    assert closed == [True]


# ---------------------------------------------------------------------------
# Startup reconciler
# ---------------------------------------------------------------------------

def _serve_once(api_app, seed):
    """Run a server in this data dir long enough to create the channel and
    ``seed()`` rows into it, then stop it: one server owns a data dir at a
    time (main.lock_data_dir), so a restart starts after the stop."""
    from fastapi.testclient import TestClient

    with TestClient(api_app) as c:
        created = c.post("/api/channels", params={"name": "Starting"})
        assert created.status_code == 200, created.text
        channel_id = created.json()["id"]
        return channel_id, seed(channel_id)


def test_startup_reconciler_marks_stale_running_interrupted__F0056(api_app):
    from fastapi.testclient import TestClient

    channel_id, rid = _serve_once(api_app, lambda channel_id: _seed_run(channel_id, "running", phase="running"))
    # Start the lifespan again (same cwd, same DBs) — the server restarting
    # after a crash while the run was live.
    with TestClient(api_app):
        pass

    row = _run_row(channel_id, rid)
    assert row["status"] == "interrupted"
    assert row["failure_reason"] == "Server restarted while the run was active"
    assert row["phase"] == "interrupted"
    assert "Interrupted" in _logs_txt(channel_id, rid)


def test_startup_reconciler_leaves_terminal_rows_alone(api_app):
    from fastapi.testclient import TestClient

    channel_id, (done, stopped) = _serve_once(api_app, lambda channel_id: (
        _seed_run(channel_id, "completed"), _seed_run(channel_id, "stopped")))
    with TestClient(api_app):
        pass
    assert _run_row(channel_id, done)["status"] == "completed"
    assert _run_row(channel_id, stopped)["status"] == "stopped"


def test_startup_reconciler_closes_connection_when_query_fails(api_app, monkeypatch):
    """A failing sweep query must still close the channel connection."""
    import database
    from fastapi.testclient import TestClient

    real = database.get_db_connection
    failed: list = []

    class _FailingConn:
        def __init__(self, conn):
            self._conn = conn
            self.closed = False

        def execute(self, sql, *args):
            if sql == "SELECT id FROM runs WHERE status='running'":
                failed.append(self)
                raise RuntimeError("boom")
            return self._conn.execute(sql, *args)

        def close(self):
            self.closed = True
            self._conn.close()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def _get(chan, *a, **kw):
        return _FailingConn(real(chan, *a, **kw))

    _serve_once(api_app, lambda channel_id: _seed_run(channel_id, "running", phase="running"))
    monkeypatch.setattr(database, "get_db_connection", _get)
    with TestClient(api_app):
        pass
    assert failed and all(c.closed for c in failed)


# ---------------------------------------------------------------------------
# Query surface
# ---------------------------------------------------------------------------

def test_list_and_detail_expose_failure_reason(client):
    rid = _seed_run(client.channel_id, "timed_out", failure_reason="Exceeded 5s timeout")

    r = client.get(f"/api/channels/{client.channel_id}/runs")
    assert r.status_code == 200
    entry = next(x for x in r.json() if x["id"] == rid)
    assert entry["failure_reason"] == "Exceeded 5s timeout"

    r = client.get(f"/api/channels/{client.channel_id}/runs/{rid}")
    assert r.status_code == 200
    assert r.json()["run"]["failure_reason"] == "Exceeded 5s timeout"


def test_events_since_terminal_set_includes_new_statuses(client):
    for status in ("timed_out", "interrupted"):
        rid = _seed_run(client.channel_id, status, failure_reason="why")
        r = client.get(f"/api/channels/{client.channel_id}/runs/{rid}/events_since?since_id=0")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == status
        assert body["is_terminal"] is True
        assert body["failure_reason"] == "why"


# ---------------------------------------------------------------------------
# Phases + watchdog
# ---------------------------------------------------------------------------

def test_awaiting_input_phase_vocabulary():
    from process_manager import phases

    assert phases.AWAITING_INPUT == "awaiting_input"
    assert phases.AWAITING_INPUT in phases.ACTIVE_PHASES
    assert not phases.is_terminal(phases.AWAITING_INPUT)
    assert phases.valid_phase(phases.AWAITING_INPUT)
    for s in ("timed_out", "interrupted"):
        assert s in phases.TERMINAL_PHASES
        assert phases.is_terminal(s)


def test_watchdog_suspends_while_awaiting_input__F1916():
    from process_manager.manager import ProcessManager

    async def drive():
        mgr = _watchdog_mgr('awaiting_input')
        task = asyncio.create_task(ProcessManager._enforce_timeout(mgr, 1))
        await mgr.clock.tick(4)
        assert mgr.timeout_reason is None and not task.done()
        mgr.current_phase = 'running'
        await mgr.clock.tick()
        await asyncio.wait_for(task, 1)
        assert mgr.timeout_reason == 'Exceeded 1s timeout'
        assert mgr.clock.elapsed == 5

    asyncio.run(drive())


class _WatchdogClock:
    """Release real watchdog awaits one logical poll at a time."""
    def __init__(self):
        self.pending = asyncio.Queue()
        self.elapsed = 0.0

    async def sleep(self, seconds):
        released = asyncio.get_running_loop().create_future()
        self.pending.put_nowait((seconds, released))
        await released

    async def tick(self, count=1):
        for _ in range(count):
            seconds, released = await asyncio.wait_for(self.pending.get(), 1)
            self.elapsed += seconds
            released.set_result(None)
            await asyncio.sleep(0)


class _WatchdogProc:
    pid = 2**30  # nonexistent → kill attempts hit ProcessLookupError
    returncode = None

    async def wait(self):
        return 0


def _watchdog_mgr(phase: str, run_id: str = "watchdog-test"):
    from process_manager.manager import ProcessManager

    class _Mgr:
        channel = CHANNEL
        process = _WatchdogProc()
        timeout_reason = None
        log_buffer: list = []
        current_phase = phase
        cell_id = "watchdog-cell"
        _terminal_attached = ProcessManager._terminal_attached

    mgr = _Mgr()
    mgr.run_id = run_id
    mgr.clock = _WatchdogClock()
    mgr._watchdog_sleep = mgr.clock.sleep
    return mgr


def test_watchdog_budgets_each_chat_turn_not_the_whole_session__F1916():
    from process_manager.manager import ProcessManager

    async def turns(interactive):
        mgr = _watchdog_mgr('running')
        task = asyncio.create_task(ProcessManager._enforce_timeout(mgr, 3, interactive=interactive))
        await mgr.clock.tick(2)
        assert mgr.timeout_reason is None
        mgr.current_phase = 'awaiting_input'
        await mgr.clock.tick(3)
        assert mgr.timeout_reason is None
        mgr.current_phase = 'running'
        await mgr.clock.tick()
        if interactive:
            assert mgr.timeout_reason is None, 'a new chat turn retained the preceding turn budget'
            await mgr.clock.tick()
            assert mgr.timeout_reason is None
            await mgr.clock.tick()
        await asyncio.wait_for(task, 1)
        assert mgr.timeout_reason == 'Exceeded 3s timeout'

    asyncio.run(turns(True))
    asyncio.run(turns(False))


def test_awaiting_approval_phase_vocabulary():
    # gen.ask holds the run here while an approval card waits.
    from process_manager import phases

    assert phases.AWAITING_APPROVAL == "awaiting_approval"
    assert phases.AWAITING_APPROVAL in phases.ACTIVE_PHASES
    assert not phases.is_terminal(phases.AWAITING_APPROVAL)
    assert phases.valid_phase(phases.AWAITING_APPROVAL)


def test_generator_cannot_set_a_host_owned_phase():
    """gen.phase may set only the active phases the SDK documents. A
    terminal or queued phase over IPC is refused, not shown mid-run, and so
    is a frame that names no phase; a generator phase applies, and the same
    phase with a new message is how a status changes."""
    from collections import deque
    from process_manager.ipc import IPCMixin

    class _Stub(IPCMixin):
        pass

    inst = _Stub()
    inst.run_id = "phase-gate"
    inst.current_phase = "running"
    inst.log_buffer = deque()
    applied = []
    inst._apply_phase = lambda p, m="": applied.append((p, m))

    for bad in ("completed", "failed", "queued"):
        asyncio.run(inst._op_phase({"op": "phase", "phase": bad}))
    asyncio.run(inst._op_phase({"op": "phase", "message": "x"}))
    assert applied == []
    refusals = [e for e in inst.log_buffer if e["type"] == "ipc_error"]
    assert len(refusals) == 4 and "phase refused" in refusals[0]["payload"]
    assert "None is not a generator-settable phase" in refusals[3]["payload"]

    asyncio.run(inst._op_phase({"op": "phase", "phase": "finishing"}))
    asyncio.run(inst._op_phase({"op": "phase", "phase": "running", "message": "x"}))
    assert applied == [("finishing", ""), ("running", "x")]


@pytest.mark.parametrize("interactive", [False, True])
def test_watchdog_does_not_charge_an_approval_card__F1916(interactive):
    from process_manager.manager import ProcessManager

    async def drive():
        mgr = _watchdog_mgr('awaiting_approval')
        task = asyncio.create_task(ProcessManager._enforce_timeout(mgr, 1, interactive=interactive))
        await mgr.clock.tick(4)
        assert mgr.timeout_reason is None and not task.done()
        mgr.current_phase = 'running'
        await mgr.clock.tick()
        await asyncio.wait_for(task, 1)
        assert mgr.timeout_reason == 'Exceeded 1s timeout'

    asyncio.run(drive())


def test_watchdog_keeps_the_turn_budget_across_an_approval_card__F1916():
    from process_manager.manager import ProcessManager

    async def drive():
        mgr = _watchdog_mgr('running')
        task = asyncio.create_task(ProcessManager._enforce_timeout(mgr, 3, interactive=True))
        await mgr.clock.tick(2)
        mgr.current_phase = 'awaiting_approval'
        await mgr.clock.tick(4)
        assert mgr.timeout_reason is None and not task.done()
        mgr.current_phase = 'running'
        await mgr.clock.tick()
        await asyncio.wait_for(task, 1)
        assert mgr.timeout_reason == 'Exceeded 3s timeout'

    asyncio.run(drive())


def test_watchdog_does_not_charge_a_live_terminal_leg__F1916():
    from process_manager.manager import ProcessManager
    from sessions import terminal as terminal_sessions

    reg = terminal_sessions.register_session('watchdog-test', 'watchdog-cell', 'ctr', ['bash'], keep_alive=True)

    async def drive():
        mgr = _watchdog_mgr('running')
        task = asyncio.create_task(ProcessManager._enforce_timeout(mgr, 1, interactive=True))
        await mgr.clock.tick(4)
        assert mgr.timeout_reason is None and not task.done()
        terminal_sessions._sessions.pop(reg['terminal_id'], None)
        await mgr.clock.tick()
        await asyncio.wait_for(task, 1)
        assert mgr.timeout_reason == 'Exceeded 1s timeout'

    try:
        asyncio.run(drive())
    finally:
        terminal_sessions._sessions.pop(reg['terminal_id'], None)


def test_watchdog_charges_a_terminal_killed_by_its_detach__F1916(monkeypatch):
    from process_manager.manager import ProcessManager
    from sessions import terminal as terminal_sessions
    from routes import terminal

    async def fake_exec(*_args, **_kwargs):
        return _WatchdogProc()

    monkeypatch.setattr(terminal.asyncio, 'create_subprocess_exec', fake_exec)
    reg = terminal_sessions.register_session('watchdog-test', 'watchdog-cell', 'ctr', ['bash'])
    session = terminal_sessions._sessions[reg['terminal_id']]

    async def drive():
        await terminal._spawn_pty(session)
        assert terminal_sessions.has_live_session('watchdog-test')
        terminal_sessions.kill_session(session)
        assert not terminal_sessions.has_live_session('watchdog-test')
        mgr = _watchdog_mgr('running')
        task = asyncio.create_task(ProcessManager._enforce_timeout(mgr, 1, interactive=True))
        await mgr.clock.tick()
        await asyncio.wait_for(task, 1)
        assert mgr.timeout_reason == 'Exceeded 1s timeout'

    try:
        asyncio.run(drive())
    finally:
        terminal_sessions._sessions.pop(reg['terminal_id'], None)


def test_watchdog_charges_a_run_whose_sibling_serves_the_terminal__F1916():
    from process_manager.manager import ProcessManager
    from sessions import terminal as terminal_sessions

    reg = terminal_sessions.register_session('sibling-run', 'watchdog-cell', 'ctr', ['bash'], keep_alive=True)

    async def drive():
        mgr = _watchdog_mgr('running')
        task = asyncio.create_task(ProcessManager._enforce_timeout(mgr, 1, interactive=True))
        await mgr.clock.tick()
        await asyncio.wait_for(task, 1)
        assert mgr.timeout_reason == 'Exceeded 1s timeout'

    try:
        assert terminal_sessions.has_live_session('sibling-run')
        assert not terminal_sessions.has_live_session('watchdog-test')
        asyncio.run(drive())
    finally:
        terminal_sessions._sessions.pop(reg['terminal_id'], None)


def test_watchdog_cancellation_and_exited_process_do_not_timeout__F1916():
    from process_manager.manager import ProcessManager

    async def drive():
        mgr = _watchdog_mgr('running')
        task = asyncio.create_task(ProcessManager._enforce_timeout(mgr, 2))
        await mgr.clock.tick()
        task.cancel()
        await task
        assert mgr.timeout_reason is None and mgr.log_buffer == []
        for process in (None, _WatchdogProc()):
            if process is not None:
                process.returncode = 0
            mgr = _watchdog_mgr('running')
            mgr.process = process
            task = asyncio.create_task(ProcessManager._enforce_timeout(mgr, 1))
            await mgr.clock.tick()
            await asyncio.wait_for(task, 1)
            assert mgr.timeout_reason is None and mgr.log_buffer == []

    asyncio.run(drive())


# ---------------------------------------------------------------------------
# duplicate_stack naming
# ---------------------------------------------------------------------------

def test_duplicate_stack_names_copies(client):
    from models import Cell, Stack, VirtualFile
    from state import persist_state, state

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    stack = Stack(
        name="DupSource",
        cells=[
            Cell(name="Alpha", files=[
                VirtualFile(name="a.txt", content="a", language="text"),
            ]),
            Cell(name="Beta", files=[]),
        ],
    )
    ch.main_stacks.append(stack)
    persist_state()

    r = client.post(f"/api/stacks/{client.channel_id}/{stack.id}/duplicate_stack")
    assert r.status_code == 200
    body = r.json()
    idx = next(
        i for i, s in enumerate(body["main_stacks"]) if s["id"] == str(stack.id)
    )
    copy = body["main_stacks"][idx + 1]
    assert [c["name"] for c in copy["cells"]] == ["Alpha (Copy)", "Beta (Copy)"]
