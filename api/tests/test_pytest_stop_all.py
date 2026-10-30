"""Focused coverage for full-stop teardown and reap.

The seams that "stop everything" leans on, each exercised without a real
subprocess, signal, or docker invocation — every OS/subprocess boundary is
monkeypatched:

  * routes/runs/teardown.stop_all_runs          — the SIGTERM → grace → SIGKILL
    ladder, DB status flip + broadcast, and the return dict.
  * routes/runs/teardown.wait_for_run_cleanups  — its bounded wait for the
    run-end cleanups, once per cleanup however many passes run.
  * routes/runs/teardown.wait_for_run_reaps     — server shutdown's wait for
    every run to leave the registry, the lifespan that calls it, and the
    desktop shell's quit around both (routes/system._run_shutdown_sequence,
    then the lifespan, then the loop close).
  * routes/runs/helpers._reap_run_containers      — the run-labelled Docker
    container reaper.

Stub managers are injected into ``active_managers`` the same way
test_pytest_runs_lifecycle.py does; the async tests use ``fresh_cwd`` (like
test_pytest_ws_close_codes' async cases) and build their DB rows directly.
"""
from __future__ import annotations


from fake_manager import FakeManager
import asyncio
import signal as _signal
import types
import uuid

import pytest

from database import get_db_connection


CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:stop-all:channel"))


# ---------------------------------------------------------------------------
# stop_all_runs
# ---------------------------------------------------------------------------

class _FakeProcess:
    """A process object stop_all_runs polls via ``.returncode``.

    ``exit_on_term`` models a generator that exits cleanly on SIGTERM (the
    faked ``signal_process_group`` sets its returncode); ``False`` models a straggler
    that ignores SIGTERM and must be SIGKILLed in phase 4."""

    def __init__(self, pid: int, exit_on_term: bool):
        self.pid = pid
        self.returncode = None
        self.exit_on_term = exit_on_term


class _StubManager(FakeManager):
    """Minimal stand-in for a ProcessManager — walks like one for the fields
    stop_all_runs reaches: ``channel``, ``run_id``, ``process``,
    ``_ipc_server`` and a recording ``_broadcast_run_status``."""

    def __init__(self, channel, run_id, process):
        super().__init__()
        self.channel = channel
        self.run_id = run_id
        self.process = process
        self._ipc_server = None
        self.broadcasts: list = []

    def _broadcast_run_status(self, status):
        self.broadcasts.append(status)


class _UnspawnedManager(_StubManager):
    """A manager create_run has registered whose ``start()`` has not reached
    the spawn yet — ``process`` is None and only ``abort_spawn`` can stop it."""

    def __init__(self, channel, run_id):
        super().__init__(channel, run_id, process=None)
        self.aborted = False

    def abort_spawn(self):
        self.aborted = True


async def test_stop_all_runs_signals_marks_and_force_kills(fresh_cwd, monkeypatch):
    """Two live runs: one exits on SIGTERM, one ignores it. The ladder
    SIGTERMs both, marks both stopped (DB row + broadcast), gives the grace
    window, then SIGKILLs only the straggler — and returns the summary dict."""
    from sessions import terminal as terminal_sessions
    from routes.runs import stop_all_runs
    from run_registry import active_managers
    import routes.runs.teardown as teardown
    import routes.runs.helpers as helpers

    graceful_id = str(uuid.uuid4())
    straggler_id = str(uuid.uuid4())
    conn = get_db_connection(CHANNEL)
    try:
        for rid in (graceful_id, straggler_id):
            conn.execute(
                "INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')",
                (rid,),
            )
        conn.commit()
    finally:
        conn.close()

    graceful = _StubManager(CHANNEL, graceful_id,
                            _FakeProcess(pid=1001, exit_on_term=True))
    straggler = _StubManager(CHANNEL, straggler_id,
                             _FakeProcess(pid=1002, exit_on_term=False))

    # Record every process-group signal; a graceful proc "exits" on SIGTERM so
    # the ladder never has to SIGKILL it.
    signals: list = []

    def fake_signal_process_group(pid, sig, *, known_group=False):
        signals.append((pid, sig))
        for m in (graceful, straggler):
            if m.process.pid == pid and sig == _signal.SIGTERM and m.process.exit_on_term:
                m.process.returncode = -_signal.SIGTERM
    monkeypatch.setattr(teardown, "signal_process_group", fake_signal_process_group)

    # No real docker / sandbox / terminal teardown.
    reaped: list = []
    monkeypatch.setattr(helpers, "_reap_run_containers",
                        lambda rid: reaped.append(rid))
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)


    active_managers.clear()
    active_managers[graceful_id] = graceful
    active_managers[straggler_id] = straggler
    try:
        # Small grace so the straggler-wait loop runs but doesn't stall.
        result = await stop_all_runs(grace_seconds=0.3)
    finally:
        active_managers.clear()

    # Return shape (from teardown.stop_all_runs).
    assert set(result) == {"requested", "killed", "terminals"}
    assert result["requested"] == 2

    assert result["terminals"] == 0
    assert result["killed"] == 1             # only the straggler

    # Phase 1 SIGTERM'd both; phase 4 SIGKILLed only the straggler.
    assert (1001, _signal.SIGTERM) in signals
    assert (1002, _signal.SIGTERM) in signals
    assert (1002, _signal.SIGKILL) in signals
    assert (1001, _signal.SIGKILL) not in signals

    # Phase 2 broadcast "stopped" on every manager...
    assert graceful.broadcasts == ["stopped"]
    assert straggler.broadcasts == ["stopped"]

    # ...and flipped every run row to 'stopped' with a stop event.
    conn = get_db_connection(CHANNEL)
    try:
        for rid in (graceful_id, straggler_id):
            row = conn.execute(
                "SELECT status FROM runs WHERE id=?", (rid,),
            ).fetchone()
            assert row and row["status"] == "stopped"
        stop_events = {
            e["run_id"]
            for e in conn.execute(
                "SELECT run_id FROM run_events WHERE event_type='stop'",
            ).fetchall()
        }
    finally:
        conn.close()
    assert {graceful_id, straggler_id} <= stop_events

    # Both natives' labelled containers were reaped (docker seam faked).
    assert set(reaped) == {graceful_id, straggler_id}


async def test_stop_all_runs_aborts_a_manager_that_has_not_spawned__F0376(fresh_cwd, monkeypatch):
    """A manager mid-start (process=None) has no process group to signal.
    stop_all_runs must flip its abort flag so ``start()`` refuses the spawn —
    otherwise the generator would come up in its own session after the server
    has finished tearing down, with nothing left to kill it — and still mark
    the run stopped like any other."""
    from sessions import terminal as terminal_sessions
    from routes.runs import stop_all_runs
    from run_registry import active_managers
    import routes.runs.teardown as teardown

    run_id = str(uuid.uuid4())
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')", (run_id,))
        conn.commit()
    finally:
        conn.close()

    pending = _UnspawnedManager(CHANNEL, run_id)
    signals: list = []
    monkeypatch.setattr(teardown, "signal_process_group", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)


    active_managers[run_id] = pending
    try:
        summary = await stop_all_runs(grace_seconds=0.1)
    finally:
        active_managers.clear()

    assert pending.aborted is True
    assert signals == []                         # nothing to signal
    assert summary["requested"] == 1 and summary["killed"] == 0
    assert pending.broadcasts == ["stopped"]
    conn = get_db_connection(CHANNEL)
    try:
        assert conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] == "stopped"
    finally:
        conn.close()


async def test_stop_all_runs_leaves_every_registry_removal_to_the_reaper__F0321(fresh_cwd, monkeypatch):
    """Stop all never drops a run from the registry: a stopped run leaves
    only when its reaper has seen its run-end cleanup through (until then
    its cell and run folder stay guarded), and a run that registered after
    the snapshot (Stop all leaves new runs legal) was never signalled."""
    from sessions import terminal as terminal_sessions
    from routes.runs import stop_all_runs
    from run_registry import active_managers
    import routes.runs.teardown as teardown
    import routes.runs.helpers as helpers

    run_id = str(uuid.uuid4())
    late_id = "late-run"
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')", (run_id,))
        conn.commit()
    finally:
        conn.close()

    stopped = _StubManager(CHANNEL, run_id, _FakeProcess(pid=2001, exit_on_term=True))
    late = _StubManager(CHANNEL, late_id, _FakeProcess(pid=2002, exit_on_term=True))
    signals: list = []

    def fake_signal_process_group(pid, sig, *, known_group=False):
        signals.append((pid, sig))
        if pid == stopped.process.pid and sig == _signal.SIGTERM:
            stopped.process.returncode = -_signal.SIGTERM
            # A Run click lands while the teardown is under way.
            active_managers[late_id] = late
    monkeypatch.setattr(teardown, "signal_process_group", fake_signal_process_group)
    monkeypatch.setattr(helpers, "_reap_run_containers", lambda rid: None)
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)


    active_managers.clear()
    active_managers[run_id] = stopped
    try:
        await stop_all_runs(grace_seconds=0.1)
        assert active_managers.get(late_id) is late
        assert active_managers.get(run_id) is stopped
    finally:
        active_managers.clear()

    assert all(pid != late.process.pid for pid, _sig in signals)
    assert late.broadcasts == []


async def test_manager_start_refuses_to_spawn_after_abort(fresh_cwd, tmp_path, monkeypatch):
    """``ProcessManager.abort_spawn()`` before the spawn makes ``start()``
    raise without ever asking the backend for a process; an abort that lands
    while the backend is spawning kills the fresh process group."""
    import asyncio
    import process_manager.manager as manager_mod
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED
    from process_manager.backends import NativeSubprocessBackend

    from pathlib import Path
    root = Path(".collimate/tmp").resolve()
    root.mkdir(parents=True, exist_ok=True)

    def make():
        run_id = str(uuid.uuid4())
        app = root / f"app_{run_id[:8]}_probe"
        app.mkdir()
        (app / "choice.generator").write_text("id: probe\nname: probe\nentrypoint: probe.py\n")
        (app / "probe.py").write_text("import gen\n")
        ws = root / f"ws_{run_id[:8]}_probe"
        ws.mkdir()
        mgr = ProcessManager(
            channel=CHANNEL, run_id=run_id, app_dir=str(app),
            workspace_dir=str(ws), entrypoint="probe.py",
            permissions=UNRESTRICTED,
        )
        mgr._build_subprocess_env = lambda *_a, **_k: {}  # type: ignore[method-assign]
        return mgr

    spawned: list = []

    class _Proc:
        pid = 424242
        returncode = None

    async def fake_spawn(self, manager, **kw):
        spawned.append(manager.run_id)
        return _Proc()
    monkeypatch.setattr(NativeSubprocessBackend, "spawn", fake_spawn)

    # Abort before start(): the backend is never asked to spawn.
    early = make()
    early.abort_spawn()
    with pytest.raises(RuntimeError, match="before the generator was spawned"):
        await early.start()
    assert spawned == []
    assert early._ipc_server is None

    # Abort during the spawn: the process that came back is killed.
    late = make()
    killed: list = []
    monkeypatch.setattr(manager_mod.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(manager_mod.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    async def racing_spawn(self, manager, **kw):
        manager.abort_spawn()
        spawned.append(manager.run_id)
        return _Proc()
    monkeypatch.setattr(NativeSubprocessBackend, "spawn", racing_spawn)
    with pytest.raises(RuntimeError, match="while the generator was spawning"):
        await late.start()
    assert spawned == [late.run_id]
    assert killed and killed[0][0] == _Proc.pid
    assert late._ipc_server is None

    # The backend's spawn raising: start() re-raises and closes the server.
    failed = make()

    async def failing_spawn(self, manager, **kw):
        raise OSError("no such interpreter")
    monkeypatch.setattr(NativeSubprocessBackend, "spawn", failing_spawn)
    with pytest.raises(OSError, match="no such interpreter"):
        await failed.start()
    assert failed._ipc_server is None
    await asyncio.sleep(0)


def _quiet_teardown(monkeypatch, managers):
    """Fake every OS seam stop_all_runs reaches; SIGTERM ends each process."""
    from sessions import terminal as terminal_sessions
    import routes.runs.helpers as helpers
    import routes.runs.control as control
    import routes.runs.teardown as teardown

    # These managers use synthetic PIDs; never signal a host group.
    monkeypatch.setattr(control, "signal_process_group", lambda pid, sig, **kwargs: False)

    def fake_signal_process_group(pid, sig, *, known_group=False):
        for m in managers:
            if m.process.pid == pid:
                m.process.returncode = -sig
    monkeypatch.setattr(teardown, "signal_process_group", fake_signal_process_group)
    monkeypatch.setattr(helpers, "_reap_run_containers", lambda rid: None)
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)


def _seed_runs(*run_ids):
    conn = get_db_connection(CHANNEL)
    try:
        for rid in run_ids:
            conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')", (rid,))
        conn.commit()
    finally:
        conn.close()


async def test_stop_all_runs_waits_for_every_run_end_sweep(fresh_cwd, monkeypatch):
    """Stop all (and the shutdown lifespan, which calls it) returns only
    after each run's run-end cleanup has swept its folder: the cleanup waits
    for its process, then sweeps, as ipc._wait_and_cleanup does."""
    import asyncio
    from routes.runs import stop_all_runs
    from run_registry import active_managers

    swept: list = []
    managers = []
    for pid in (2001, 2002):
        run_id = str(uuid.uuid4())
        manager = _StubManager(CHANNEL, run_id, _FakeProcess(pid=pid, exit_on_term=True))

        async def cleanup(m=manager):
            while m.process.returncode is None:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)          # the sweep's commit
            swept.append(m.run_id)
        manager._cleanup_task = asyncio.ensure_future(cleanup())
        managers.append(manager)
    _seed_runs(*(m.run_id for m in managers))
    _quiet_teardown(monkeypatch, managers)

    active_managers.clear()
    for m in managers:
        active_managers[m.run_id] = m
    try:
        await stop_all_runs(grace_seconds=1.0)
    finally:
        active_managers.clear()
    assert sorted(swept) == sorted(m.run_id for m in managers)


async def test_a_hung_run_cleanup_is_waited_for_only_until_the_bound(fresh_cwd, monkeypatch):
    """A cleanup that never finishes holds Stop all for the bound, no more;
    it is logged, not cancelled, and a sibling's cleanup still completes."""
    import asyncio
    import logging
    import time
    import routes.runs.teardown as teardown
    from routes.runs import stop_all_runs
    from run_registry import active_managers

    monkeypatch.setattr(teardown, "CLEANUP_WAIT_SECONDS", 0.3)
    hung = _StubManager(CHANNEL, str(uuid.uuid4()), _FakeProcess(pid=2101, exit_on_term=True))
    quick = _StubManager(CHANNEL, str(uuid.uuid4()), _FakeProcess(pid=2102, exit_on_term=True))
    forever = asyncio.Event()
    swept: list = []

    async def hang():
        await forever.wait()

    async def sweep():
        while quick.process.returncode is None:
            await asyncio.sleep(0.01)
        swept.append(quick.run_id)
    hung._cleanup_task = asyncio.ensure_future(hang())
    quick._cleanup_task = asyncio.ensure_future(sweep())
    _seed_runs(hung.run_id, quick.run_id)
    _quiet_teardown(monkeypatch, [hung, quick])

    active_managers.clear()
    active_managers[hung.run_id] = hung
    active_managers[quick.run_id] = quick
    warnings: list = []
    handler = logging.Handler()
    handler.emit = lambda record: warnings.append(record.getMessage())
    teardown._logger.addHandler(handler)
    started = time.monotonic()
    try:
        await stop_all_runs(grace_seconds=1.0)
    finally:
        teardown._logger.removeHandler(handler)
        active_managers.clear()
    try:
        assert time.monotonic() - started < 3.0
        assert swept == [quick.run_id]
        assert not hung._cleanup_task.done(), "the bound never cancels a cleanup"
        assert any(hung.run_id in w and "did not finish" in w for w in warnings)
    finally:
        forever.set()
        await hung._cleanup_task


async def test_stop_all_runs_keeps_the_status_of_a_run_that_already_ended__F0390(fresh_cwd, monkeypatch):
    """Stop all only records the runs it stops. A run that already exited
    (it is only still registered for its run-end cleanup) keeps whatever its
    finalize records, finalized or not yet, and is re-broadcast only once it
    has a final status. A live run whose finish op already ended its row
    keeps that status. A still-running sibling is stopped as before."""
    from routes.runs import stop_all_runs
    from run_registry import active_managers

    done_id, ending_id, finished_id, live_id = (str(uuid.uuid4()) for _ in range(4))
    conn = get_db_connection(CHANNEL)
    try:
        for rid, status in ((done_id, "completed"), (ending_id, "running"),
                            (finished_id, "completed"), (live_id, "running")):
            conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'x', ?)", (rid, status))
        conn.commit()
    finally:
        conn.close()

    def exited(pid):
        proc = _FakeProcess(pid=pid, exit_on_term=True)
        proc.returncode = 0
        return proc
    done = _StubManager(CHANNEL, done_id, exited(2201))
    done.final_status = "completed"
    ending = _StubManager(CHANNEL, ending_id, exited(2203))
    ending.final_status = None
    finished = _StubManager(CHANNEL, finished_id, _FakeProcess(pid=2204, exit_on_term=True))
    live = _StubManager(CHANNEL, live_id, _FakeProcess(pid=2202, exit_on_term=True))
    _quiet_teardown(monkeypatch, [done, ending, finished, live])

    active_managers.clear()
    for m in (done, ending, finished, live):
        active_managers[m.run_id] = m
    try:
        await stop_all_runs(grace_seconds=0.2)
    finally:
        active_managers.clear()

    conn = get_db_connection(CHANNEL)
    try:
        status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM runs")}
        stops = {r["run_id"] for r in conn.execute(
            "SELECT run_id FROM run_events WHERE event_type='stop'")}
    finally:
        conn.close()
    assert status[done_id] == "completed"
    assert status[ending_id] == "running"      # its finalize records it
    assert status[finished_id] == "completed"
    assert status[live_id] == "stopped"
    assert stops == {live_id}
    assert done.broadcasts == ["completed"]
    assert ending.broadcasts == []
    assert finished.broadcasts == ["completed"]
    assert live.broadcasts == ["stopped"]


def _finalize_exit(channel: str, run_id: str, returncode: int) -> str | None:
    """The run's own finalize (FinalizationMixin._finalize_run_status, the
    real status write) for a process that exited with ``returncode``;
    returns the status it read back."""
    from process_manager.finalization import FinalizationMixin

    class _Exited(FinalizationMixin):
        timeout_reason = None
        final_status = None
        log_buffer: list = []

        def _broadcast_run_status(self, *_a):
            pass

        def _apply_phase(self, *_a):
            pass

        def _flush_log_buffer(self):
            pass

    run = _Exited()
    run.channel, run.run_id = channel, run_id
    run.process = types.SimpleNamespace(returncode=returncode)
    run._finalize_run_status()
    return run.final_status


async def test_stop_all_records_stopped_before_a_prompt_exit_is_finalized__F0390(fresh_cwd, monkeypatch):
    """A generator that exits 0 the moment it is SIGTERMed is finalized from
    its exit by its real finalize, which would write 'completed', shortly
    after. Stop all records 'stopped' before it signals, so that write finds
    nothing to do and the run the user stopped reads 'stopped', with one
    stop event, however many runs the sweep has to record."""
    from routes.runs import stop_all_runs
    from run_registry import active_managers
    import routes.runs.teardown as teardown

    run_ids = [str(uuid.uuid4()) for _ in range(3)]
    _seed_runs(*run_ids)
    managers = [_StubManager(CHANNEL, rid, _FakeProcess(pid=2300 + i, exit_on_term=True))
                for i, rid in enumerate(run_ids)]
    _quiet_teardown(monkeypatch, managers)
    finalized: list = []

    def exit_and_finalize(pid, sig):
        # The run exits on the signal and its finalize writes the row before
        # the sweep gets any further.
        m = next(m for m in managers if m.process.pid == pid)
        m.process.returncode = 0
        finalized.append(_finalize_exit(CHANNEL, m.run_id, 0))
    monkeypatch.setattr(teardown, "signal_process_group", exit_and_finalize)

    active_managers.clear()
    for m in managers:
        active_managers[m.run_id] = m
    try:
        await stop_all_runs(grace_seconds=0.2)
    finally:
        active_managers.clear()

    conn = get_db_connection(CHANNEL)
    try:
        status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM runs")}
        stops = [r["run_id"] for r in conn.execute(
            "SELECT run_id FROM run_events WHERE event_type='stop'")]
    finally:
        conn.close()
    assert finalized == ["stopped"] * len(run_ids)
    assert all(status[rid] == "stopped" for rid in run_ids), status
    assert sorted(stops) == sorted(run_ids)
    assert all(m.broadcasts == ["stopped"] for m in managers)


def _seed_channel_runs(channel: str, *run_ids) -> None:
    conn = get_db_connection(channel)
    try:
        for rid in run_ids:
            conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')", (rid,))
        conn.commit()
    finally:
        conn.close()


def _statuses(channel: str) -> dict:
    conn = get_db_connection(channel)
    try:
        return {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM runs")}
    finally:
        conn.close()


async def test_stop_all_records_the_other_channels_when_one_db_fails__F0390(fresh_cwd, monkeypatch):
    """A channel whose db refuses Stop all's status write is logged and left
    out: every other channel's runs are still recorded 'stopped', and every
    run is still signalled and told 'stopped'."""
    from routes.runs import stop_all_runs
    from run_registry import active_managers
    import routes.runs.teardown as teardown

    broken_channel = str(uuid.uuid4())
    ok_id, broken_id = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_channel_runs(CHANNEL, ok_id)
    _seed_channel_runs(broken_channel, broken_id)
    ok = _StubManager(CHANNEL, ok_id, _FakeProcess(pid=2401, exit_on_term=True))
    broken = _StubManager(broken_channel, broken_id, _FakeProcess(pid=2402, exit_on_term=True))
    _quiet_teardown(monkeypatch, [ok, broken])
    real_connect = teardown.get_db_connection

    def connect(channel):
        if channel == broken_channel:
            raise RuntimeError("database is locked")
        return real_connect(channel)
    monkeypatch.setattr(teardown, "get_db_connection", connect)

    active_managers.clear()
    active_managers[ok_id] = ok
    active_managers[broken_id] = broken
    try:
        await stop_all_runs(grace_seconds=0.2)
    finally:
        active_managers.clear()

    assert _statuses(CHANNEL)[ok_id] == "stopped"
    assert _statuses(broken_channel)[broken_id] == "running"   # its finalize records it
    assert ok.process.returncode is not None and broken.process.returncode is not None
    assert ok.broadcasts == ["stopped"] and broken.broadcasts == ["stopped"]


async def test_stop_all_writes_its_channels_concurrently_and_holds_them__F0390_F0318(
        fresh_cwd, monkeypatch):
    """Stop all's status pass writes every channel at once, so a channel db
    another writer holds delays the signals by one busy timeout, not by one
    per channel; and each channel is marked busy while it is written, so a
    rename or delete never moves the db under the write. Here each channel's
    write waits for the other's to start: written one after the other, the
    first would give up and leave its run 'running'."""
    import threading
    from routes.runs import stop_all_runs
    from run_registry import active_managers
    from run_registry import busy_channels
    import routes.runs.teardown as teardown

    channel_a, channel_b = str(uuid.uuid4()), str(uuid.uuid4())
    a_id, b_id = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_channel_runs(channel_a, a_id)
    _seed_channel_runs(channel_b, b_id)
    a = _StubManager(channel_a, a_id, _FakeProcess(pid=2501, exit_on_term=True))
    b = _StubManager(channel_b, b_id, _FakeProcess(pid=2502, exit_on_term=True))
    _quiet_teardown(monkeypatch, [a, b])
    both_writing = threading.Barrier(2, timeout=5)
    busy_while_written: list = []
    real_connect = teardown.get_db_connection

    def connect(channel):
        both_writing.wait()
        busy_while_written.append((channel, channel in busy_channels()))
        return real_connect(channel)
    monkeypatch.setattr(teardown, "get_db_connection", connect)

    active_managers.clear()
    active_managers[a_id] = a
    active_managers[b_id] = b
    try:
        await stop_all_runs(grace_seconds=0.2)
    finally:
        active_managers.clear()

    assert sorted(busy_while_written) == sorted([(channel_a, True), (channel_b, True)])
    assert busy_channels() == set()
    assert _statuses(channel_a)[a_id] == "stopped"
    assert _statuses(channel_b)[b_id] == "stopped"


async def test_stop_all_runs_empty_registry_is_a_clean_noop(fresh_cwd, monkeypatch):
    """With nothing active, the ladder reports zero work and touches no
    subprocess/sandbox seam beyond the (faked) sandbox sweep."""
    from sessions import terminal as terminal_sessions
    from routes.runs import stop_all_runs
    from run_registry import active_managers
    import routes.runs.teardown as teardown

    monkeypatch.setattr(teardown, "signal_process_group",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no signal expected")))
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)


    active_managers.clear()
    result = await stop_all_runs(grace_seconds=0.0)
    assert result == {"requested": 0, "killed": 0, "terminals": 0}


# ---------------------------------------------------------------------------
# Server shutdown: wait_for_run_reaps, and the lifespan that calls it
# ---------------------------------------------------------------------------

def _record_removals(monkeypatch) -> list:
    """``(run_id, remover)`` for every run that leaves the registry, the
    remover being the function that called ``pop``."""
    import sys
    import run_registry as registry
    removals: list = []
    real_pop = registry.ActiveManagersRegistry.pop

    def pop(self, run_id, default=None):
        if run_id in self:
            removals.append((run_id, sys._getframe(1).f_code.co_name))
        return real_pop(self, run_id, default)
    monkeypatch.setattr(registry.ActiveManagersRegistry, "pop", pop)
    return removals


def _stuck_cleanup(manager) -> asyncio.Task:
    """A run-end cleanup that waits for the run's process to exit and is
    then stuck (a sweep held on a locked db, say): it never ends on its
    own."""
    async def cleanup():
        while manager.process.returncode is None:
            await asyncio.sleep(0.01)
        await asyncio.Event().wait()
    return asyncio.ensure_future(cleanup())


def _late_cleanup(manager, gate: asyncio.Event, steps: list) -> asyncio.Task:
    """A run-end cleanup that ends on its own, once ``gate`` opens: the test
    opens it after the wait it must outlast is over, so it is late by
    construction. It waits for the run's process to exit and then for the
    gate (the output drain, the finalize); then its ``finally`` awaits a
    step before recording it, as ipc._wait_and_cleanup's does (the in-flight
    emit wait, the folder sweep). A cancel anywhere in it leaves a step out
    of ``steps``."""
    async def cleanup():
        try:
            while manager.process.returncode is None:
                await asyncio.sleep(0.01)
            await gate.wait()
            steps.append("drain")
        finally:
            await asyncio.sleep(0.1)
            steps.append("sweep")
    return asyncio.ensure_future(cleanup())


def _ended(run_id: str, pid: int) -> _StubManager:
    """A registered run whose process has exited."""
    manager = _StubManager(CHANNEL, run_id, _FakeProcess(pid=pid, exit_on_term=True))
    manager.process.returncode = 0
    return manager


async def test_shutdown_waits_for_a_late_cleanup_and_the_reaper_removes_the_run__F0321(
        fresh_cwd, monkeypatch):
    """Server shutdown's two run steps, stop_all_runs then
    wait_for_run_reaps, with a run-end cleanup that outlasts Stop all's
    bounded wait. The reap wait only waits: the cleanup is never cancelled,
    every step of it runs, the awaited one in its ``finally`` included, and
    the run's own reaper drops it from the registry. wait_for_run_reaps
    removes nothing itself."""
    import routes.runs.helpers as helpers
    import routes.runs.teardown as teardown
    from routes.runs import stop_all_runs
    from run_registry import active_managers

    monkeypatch.setattr(teardown, "CLEANUP_WAIT_SECONDS", 0.2)
    removals = _record_removals(monkeypatch)
    run = _StubManager(CHANNEL, str(uuid.uuid4()), _FakeProcess(pid=2601, exit_on_term=True))
    steps: list = []
    gate = asyncio.Event()
    run._cleanup_task = _late_cleanup(run, gate, steps)
    _seed_runs(run.run_id)
    _quiet_teardown(monkeypatch, [run])
    active_managers.clear()
    active_managers[run.run_id] = run
    reaper = asyncio.ensure_future(helpers._reap_process(run.run_id, run))
    try:
        await asyncio.wait_for(stop_all_runs(grace_seconds=1.0), timeout=5)
        # Late: Stop all's wait is over and the cleanup is still going.
        assert not run._cleanup_task.done() and active_managers.get(run.run_id) is run
        asyncio.get_running_loop().call_later(0.3, gate.set)   # it ends during the reap wait
        assert await asyncio.wait_for(teardown.wait_for_run_reaps(timeout=2.0), timeout=5) == []
    finally:
        active_managers.clear()
    await asyncio.wait({reaper}, timeout=5)   # bounded: a regression fails, never hangs
    assert reaper.done()
    assert not run._cleanup_task.cancelled()
    assert steps == ["drain", "sweep"]
    assert removals == [(run.run_id, "_reap_process")]


async def test_shutdown_leaves_a_run_still_registered_at_the_bound_as_it_is__F0321(
        fresh_cwd, monkeypatch):
    """A run that has not left the registry when the bound is out is
    returned and logged, and left as it is: still registered, its cleanup
    neither cancelled nor ended. So it goes for a cleanup stuck after its
    process exited, a run whose process is still alive and one that never
    spawned. wait_for_run_reaps never removes a run itself."""
    import logging
    import routes.runs.teardown as teardown
    from run_registry import active_managers

    removals = _record_removals(monkeypatch)
    stuck = _ended("stuck-run", pid=2701)
    stuck._cleanup_task = _stuck_cleanup(stuck)
    alive = _StubManager(CHANNEL, "alive-run", _FakeProcess(pid=2702, exit_on_term=True))
    alive._cleanup_task = _stuck_cleanup(alive)
    unspawned = _UnspawnedManager(CHANNEL, "unspawned-run")
    runs = (stuck, alive, unspawned)
    warnings: list = []
    handler = logging.Handler()
    handler.emit = lambda record: warnings.append(record.getMessage())
    teardown._logger.addHandler(handler)
    active_managers.clear()
    for m in runs:
        active_managers[m.run_id] = m
    try:
        # Bounded: a reap wait that ignored its bound fails here, never hangs.
        left = await asyncio.wait_for(teardown.wait_for_run_reaps(timeout=0.3), timeout=5)
        assert left == ["alive-run", "stuck-run", "unspawned-run"]
        await asyncio.sleep(0.05)          # a cancel requested at the bound would land now
        assert all(active_managers.get(m.run_id) is m for m in runs)
        assert not stuck._cleanup_task.done() and not alive._cleanup_task.done()
    finally:
        teardown._logger.removeHandler(handler)
        active_managers.clear()
        stuck._cleanup_task.cancel()
        alive._cleanup_task.cancel()
    assert removals == []
    assert any("stuck-run" in w and "did not leave the registry" in w for w in warnings)


async def test_a_later_lifespan_in_the_process_starts_with_no_run_registered__F0321(
        api_app, monkeypatch):
    """Two server lifespans on one loop in one process (tests, an embedded
    server). The first shuts down with two runs whose run-end cleanups
    outlast Stop all's wait and end on their own during the reap wait: one
    still running, which Stop all signals, and one whose process had already
    exited and whose finalize failed, its row still 'running'. Nothing
    cancels them. The first lifespan ends with the registry empty, each run
    dropped by its own reaper, and the second starts with none registered,
    so its crash recovery resolves the unfinalized row instead of passing it
    over as a live run's."""
    import sys
    import accounts
    import main
    import routes.runs.helpers as helpers
    import routes.runs.teardown as teardown
    import sandbox
    from models import Channel
    from run_registry import active_managers
    from state import persist_state, state
    from task_utils import track_task

    monkeypatch.setitem(sys.modules, "docker", None)   # the startup container reap: skipped
    monkeypatch.setattr(sandbox, "reap_orphan_containers", lambda: None)
    monkeypatch.setattr(accounts, "sweep_expired_account_leases", lambda: None)
    monkeypatch.setattr(teardown, "CLEANUP_WAIT_SECONDS", 0.2)
    removals = _record_removals(monkeypatch)
    live_id, ended_id = str(uuid.uuid4()), str(uuid.uuid4())
    live = _StubManager(CHANNEL, live_id, _FakeProcess(pid=3001, exit_on_term=True))
    ended = _ended(ended_id, pid=3002)
    steps: dict = {live_id: [], ended_id: []}
    gates: dict = {}
    _quiet_teardown(monkeypatch, [live, ended])
    real_wait_for_run_reaps = teardown.wait_for_run_reaps
    going_at_the_reap_wait: list = []

    async def wait_for_run_reaps(*a, **k):
        going_at_the_reap_wait.extend(sorted(
            run_id for run_id, m in active_managers.items() if not m._cleanup_task.done()))
        for gate in gates.values():            # both end during the reap wait
            asyncio.get_running_loop().call_later(0.3, gate.set)
        return await real_wait_for_run_reaps(*a, **k)
    monkeypatch.setattr(teardown, "wait_for_run_reaps", wait_for_run_reaps)

    active_managers.clear()
    try:
        async with main.lifespan(api_app):
            state.channels.append(Channel(id=uuid.UUID(CHANNEL), name="Starting"))
            await asyncio.to_thread(persist_state)
            _seed_runs(live_id, ended_id)
            for run in (live, ended):
                gates[run.run_id] = asyncio.Event()
                run._cleanup_task = _late_cleanup(run, gates[run.run_id], steps[run.run_id])
                active_managers[run.run_id] = run
                track_task(helpers._reap_process(run.run_id, run),
                           name=f"reap_process:{run.run_id}")
        left_by_the_first = [run_id for run_id, _m in active_managers.items()]
        async with main.lifespan(api_app):
            at_the_second = [run_id for run_id, _m in active_managers.items()]
            statuses = _statuses(CHANNEL)
    finally:
        active_managers.clear()

    assert going_at_the_reap_wait == sorted([live_id, ended_id])   # both outlasted Stop all's wait
    assert left_by_the_first == [] and at_the_second == []
    assert steps == {live_id: ["drain", "sweep"], ended_id: ["drain", "sweep"]}
    assert sorted(removals) == sorted([(live_id, "_reap_process"), (ended_id, "_reap_process")])
    assert live.process.returncode == -_signal.SIGTERM
    assert statuses[live_id] == "stopped"         # Stop all recorded it before its signal
    assert statuses[ended_id] == "interrupted"    # the second start's crash recovery


async def test_a_later_stop_all_never_waits_again_for_a_cleanup_it_waited_out__F0321(
        fresh_cwd, monkeypatch):
    """Stop all waits for a run-end cleanup once. A second pass that finds
    the same stuck cleanup (the lifespan's, after the desktop shell's quit
    ran one) returns at once instead of waiting the bound out again, which
    the shell's 30 s budget has no room for. Neither pass cancels the
    cleanup or removes its run, and the late cleanup is logged once."""
    import logging
    import time
    import routes.runs.teardown as teardown
    from routes.runs import stop_all_runs
    from run_registry import active_managers

    monkeypatch.setattr(teardown, "CLEANUP_WAIT_SECONDS", 0.5)
    removals = _record_removals(monkeypatch)
    run = _StubManager(CHANNEL, str(uuid.uuid4()), _FakeProcess(pid=2901, exit_on_term=True))
    run._cleanup_task = _stuck_cleanup(run)
    _seed_runs(run.run_id)
    _quiet_teardown(monkeypatch, [run])
    warnings: list = []
    handler = logging.Handler()
    handler.emit = lambda record: warnings.append(record.getMessage())
    teardown._logger.addHandler(handler)
    active_managers.clear()
    active_managers[run.run_id] = run
    try:
        started = time.monotonic()
        await asyncio.wait_for(stop_all_runs(grace_seconds=1.0), timeout=5)
        first = time.monotonic() - started
        started = time.monotonic()
        await asyncio.wait_for(stop_all_runs(grace_seconds=1.0), timeout=5)
        second = time.monotonic() - started
        await asyncio.sleep(0.05)          # a cancel either pass requested would land now
        assert active_managers.get(run.run_id) is run
        assert not run._cleanup_task.done()
    finally:
        teardown._logger.removeHandler(handler)
        active_managers.clear()
        run._cleanup_task.cancel()
    assert first >= 0.45, f"the first pass waited only {first:.2f}s for the stuck cleanup"
    assert second < 0.25, f"the second pass waited {second:.2f}s for the same cleanup"
    assert removals == []
    assert [w for w in warnings if "did not finish" in w] == [
        f"stop_all: run-end cleanup of {run.run_id} did not finish within 0.5s; it keeps "
        "going and its run stays registered until it ends"]


async def test_a_later_pass_still_waits_for_a_cleanup_no_pass_waited_out__F0321(fresh_cwd):
    """The skip is per cleanup, not per pass: a later pass that also finds
    a cleanup no earlier pass waited for (a run started since) waits for
    that one as ever, only not for the one already waited out, which it
    still reports as going."""
    import time
    import routes.runs.teardown as teardown

    stuck = _ended("stuck-run", pid=2911)
    stuck._cleanup_task = _stuck_cleanup(stuck)
    fresh = _ended("fresh-run", pid=2912)
    try:
        assert await asyncio.wait_for(teardown.wait_for_run_cleanups(
            [("stuck-run", stuck)], timeout=0.3), timeout=5) == ["stuck-run"]
        fresh._cleanup_task = asyncio.ensure_future(asyncio.sleep(0.2))
        started = time.monotonic()
        left = await asyncio.wait_for(teardown.wait_for_run_cleanups(
            [("stuck-run", stuck), ("fresh-run", fresh)], timeout=0.6), timeout=5)
        took = time.monotonic() - started
        assert left == ["stuck-run"] and fresh._cleanup_task.done()
        assert not stuck._cleanup_task.done()
    finally:
        stuck._cleanup_task.cancel()
    assert 0.15 <= took < 0.5, f"the second pass returned after {took:.2f}s"


async def test_the_lifespan_leaves_a_run_still_registered_at_its_end_as_it_is__F0321(
        api_app, monkeypatch):
    """Ctrl-C: the lifespan's own Stop all and reap wait, with a run whose
    run-end cleanup is stuck past both bounds. The lifespan cancels nothing
    and is no second remover beside the run's own: when it ends the run is
    still registered as the same object and its cleanup still going, left
    for the loop close's one cancel."""
    import sys
    import accounts
    import main
    import run_registry as registry
    import routes.runs.teardown as teardown
    import sandbox
    from run_registry import active_managers

    monkeypatch.setitem(sys.modules, "docker", None)   # the startup container reap: skipped
    monkeypatch.setattr(sandbox, "reap_orphan_containers", lambda: None)
    monkeypatch.setattr(accounts, "sweep_expired_account_leases", lambda: None)
    monkeypatch.setattr(teardown, "CLEANUP_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(teardown, "REAP_WAIT_SECONDS", 0.3)
    # The lifespan's shutdown closes the start gate; reopen it afterwards.
    monkeypatch.setattr(registry, "_shutting_down", registry.is_shutting_down())
    removals = _record_removals(monkeypatch)
    stuck = _ended("stuck-run", pid=3101)
    stuck._cleanup_task = _stuck_cleanup(stuck)
    _quiet_teardown(monkeypatch, [stuck])
    active_managers.clear()
    try:
        async with main.lifespan(api_app):
            active_managers[stuck.run_id] = stuck
        await asyncio.sleep(0.05)          # a cancel requested at the lifespan's end would land now
        assert active_managers.get(stuck.run_id) is stuck
        assert not stuck._cleanup_task.done()
        assert removals == []
    finally:
        active_managers.clear()
        stuck._cleanup_task.cancel()


class _WaitableProcess(_FakeProcess):
    """A _FakeProcess the real run-end cleanup can wait on."""

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


def _ipc_cleanup_run(run_id: str, pid: int, steps: list):
    """A run whose run-end cleanup is the real ipc._wait_and_cleanup, the
    teardown steps it reaches recorded in ``steps`` (the sweep and the lease
    release by the test's patches). Its log writer's final flush never ends
    (a channel db another writer holds, say), so the cleanup stays in its
    try part until a cancel ends that wait."""
    from process_manager.finalization import FinalizationMixin

    class _Run(_StubManager):
        _wait_and_cleanup = FinalizationMixin._wait_and_cleanup

        def _finalize_run_status(self):
            steps.append("finalize")

        def _flush_log_buffer(self):
            steps.append("log flush")

    run = _Run(CHANNEL, run_id, _WaitableProcess(pid=pid, exit_on_term=True))
    run.cell_id, run.label, run.generator_name = "cell-q", "quit", "gen"
    run._writer_task = asyncio.ensure_future(asyncio.Event().wait())
    run._cleanup_task = asyncio.ensure_future(run._wait_and_cleanup())
    return run


def test_the_desktop_quit_ends_its_lifespan_inside_the_shell_budget__F0321(api_app, monkeypatch):
    """The desktop shell's quit with its bounds scaled 1/10, as uvicorn runs
    it: POST /api/system/shutdown's sequence (Stop all, then SIGINT), the
    lifespan's own Stop all and reap wait, then the loop close. The run
    takes 0.5 s of its 1.2 s grace to exit (a docker stop), and its run-end
    cleanup, the real ipc._wait_and_cleanup, is still in its try part when
    both waits are out: its log writer's final flush is stuck. The
    lifespan's Stop all does not wait for that cleanup again, so the
    lifespan ends inside the shell's budget, 3 s here: a SIGTERM from the
    shell before then would be re-raised by uvicorn the moment the lifespan
    returned, ending the process with the cleanup's finally not run. The
    lifespan leaves the run registered and its cleanup going; the loop close
    cancels the cleanup once, every step of its finally runs (the folder
    sweep, the account lease release, the log flush), and the run's own
    reaper removes it."""
    from sessions import terminal as terminal_sessions
    import os
    import signal
    import sys
    import time
    import accounts
    import cell_commits
    import main
    import routes.runs.helpers as helpers
    import run_registry as registry
    import routes.runs.teardown as teardown
    import routes.system as system
    import sandbox
    from models import Channel
    from run_registry import active_managers
    from state import persist_state, state
    from task_utils import track_task

    budget = 3.0                                                   # the shell's 30 s
    monkeypatch.setattr(teardown.stop_all_runs, "__defaults__", (1.2,))   # the 12 s grace
    # The lifespan's shutdown closes the start gate; reopen it afterwards.
    monkeypatch.setattr(registry, "_shutting_down", registry.is_shutting_down())
    monkeypatch.setattr(teardown, "CLEANUP_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(teardown, "REAP_WAIT_SECONDS", 0.2)
    monkeypatch.setitem(sys.modules, "docker", None)   # the startup container reap: skipped
    monkeypatch.setattr(sandbox, "reap_orphan_containers", lambda: None)
    monkeypatch.setattr(accounts, "sweep_expired_account_leases", lambda: None)
    steps: list = []
    monkeypatch.setattr(cell_commits, "sweep_run_folder", lambda *a, **k: steps.append("sweep"))
    monkeypatch.setattr(accounts, "release_run_account_leases",
                        lambda _run_id: steps.append("lease release"))
    monkeypatch.setattr(helpers, "_reap_run_containers", lambda _rid: None)
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)

    removals = _record_removals(monkeypatch)
    runs: list = []
    marks: dict = {}
    sigint: list = []

    def signal_pg(pid, sig):
        # The run exits 0.5 s after its SIGTERM, at once on a SIGKILL.
        for run in runs:
            if run.process.pid == pid:
                def exit_(run=run, code=-sig):
                    run.process.returncode = code
                if sig == signal.SIGTERM:
                    asyncio.get_running_loop().call_later(0.5, exit_)
                else:
                    exit_()
    monkeypatch.setattr(teardown, "signal_process_group", signal_pg)
    real_kill = os.kill

    def kill(pid, sig):
        # The sequence's SIGINT to the server: uvicorn's shutdown begins.
        if pid == os.getpid() and sig == signal.SIGINT:
            sigint[0].set()
            return
        real_kill(pid, sig)
    monkeypatch.setattr(os, "kill", kill)

    async def serve():
        sigint.append(asyncio.Event())
        async with main.lifespan(api_app):
            state.channels.append(Channel(id=uuid.UUID(CHANNEL), name="Starting"))
            await asyncio.to_thread(persist_state)
            run = _ipc_cleanup_run(str(uuid.uuid4()), 3201, steps)
            runs.append(run)
            _seed_runs(run.run_id)
            active_managers[run.run_id] = run
            track_task(helpers._reap_process(run.run_id, run), name=f"reap_process:{run.run_id}")
            marks["post"] = time.monotonic()
            await system.shutdown_server()                         # the shell's POST
            await asyncio.wait_for(sigint[0].wait(), timeout=10)
            marks["sigint"] = time.monotonic()
        marks["lifespan end"] = time.monotonic()
        # What the loop close finds.
        marks["left"] = (active_managers.get(run.run_id) is run, run._cleanup_task.done(),
                         list(removals), list(steps))

    runner = asyncio.Runner()
    try:
        runner.run(serve())
    finally:
        runner.close()          # the loop close: one cancel for what is left, then a wait
        active_managers.clear()
    run = runs[0]
    quit_took = marks["lifespan end"] - marks["post"]
    lifespan_took = marks["lifespan end"] - marks["sigint"]
    assert lifespan_took < 0.2 + 0.5, f"the lifespan's teardown took {lifespan_took:.2f}s"
    assert quit_took < budget, f"the lifespan ended {quit_took:.2f}s after the POST"
    assert marks["left"] == (True, False, [], [])      # registered, in its try part, untouched
    assert run._cleanup_task.cancelled()
    assert steps == ["sweep", "lease release", "log flush"]
    assert removals == [(run.run_id, "_reap_process")]


# ---------------------------------------------------------------------------
