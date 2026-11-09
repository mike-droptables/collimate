"""Remaining P0 shutdown paths obey process exit independently of pipe closure."""

from fake_manager import FakeManager
import asyncio
import signal
import uuid
from types import SimpleNamespace

import pytest

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:shutdown-completion:channel"))


async def test_stop_all_signals_exited_group_without_rewriting_completed_run__P0F18(fresh_cwd, monkeypatch):
    from collimate_server.sessions import terminal as terminal_sessions
    from collimate_server.database import get_db_connection
    from collimate_server.run_registry import active_managers
    from collimate_server.routes.runs import helpers, teardown
    import collimate_server.process_signals as process_signals

    conn = get_db_connection(CHANNEL)
    conn.execute("INSERT INTO runs (id, prompt, status) VALUES ('ended', 'x', 'completed')")
    conn.commit()
    conn.close()
    signals = []
    manager = FakeManager(
        channel=CHANNEL, run_id="ended", process=SimpleNamespace(pid=987654, returncode=0),
        final_status="completed", _broadcast_run_status=lambda status: None,
    )
    monkeypatch.setattr(process_signals.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(helpers, "_reap_run_containers", lambda run_id: None)
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)
    active_managers["ended"] = manager
    try:
        result = await teardown.stop_all_runs(grace_seconds=0)
    finally:
        active_managers.clear()
    assert signals == [(987654, signal.SIGTERM)]
    assert result["requested"] == 1 and result["killed"] == 0
    conn = get_db_connection(CHANNEL)
    try:
        assert conn.execute("SELECT status FROM runs WHERE id='ended'").fetchone()[0] == "completed"
        assert conn.execute("SELECT count(*) FROM run_events WHERE event_type='stop'").fetchone()[0] == 0
    finally:
        conn.close()


class _PipeHeldProcess:
    pid = 987655
    returncode = None

    async def wait(self):
        await asyncio.Future()


async def test_watchdog_returns_on_exit_even_when_wait_is_held_by_pipes__P0F18(monkeypatch):
    from collimate_server.process_manager import manager as module
    process = _PipeHeldProcess()
    signals = []
    def signal_group(pid, sig):
        signals.append(sig)
        process.returncode = -sig
    monkeypatch.setattr(module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(module.os, "killpg", signal_group)
    manager = FakeManager(process=process, run_id="watchdog", log_buffer=[], current_phase="running")
    await asyncio.wait_for(module.ProcessManager._enforce_timeout(manager, 0), timeout=1)
    assert signals == [signal.SIGTERM]
    assert manager.stop_requested


async def test_the_watchdog_turns_the_lease_releasing_before_it_signals(monkeypatch):
    """A run the watchdog ends is stopping like any other stop: its
    repository lease reads ``releasing`` at its SIGTERM, so a start on the
    cell waits for the final harvest instead of getting a read-only
    ``.git``."""
    import collimate_server.repository_leases as repository_leases
    from collimate_server.process_manager import manager as module
    process = _PipeHeldProcess()
    states = []
    def signal_group(pid, sig):
        states.append(repository_leases.holder(CHANNEL, "cell-watchdog").state)
        process.returncode = -sig
    monkeypatch.setattr(module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(module.os, "killpg", signal_group)
    manager = FakeManager(channel=CHANNEL, cell_id="cell-watchdog", process=process, run_id="watchdog",
                          log_buffer=[], current_phase="running")
    repository_leases.claim(CHANNEL, "cell-watchdog", "watchdog", "Agent")
    try:
        await asyncio.wait_for(module.ProcessManager._enforce_timeout(manager, 0), timeout=1)
    finally:
        repository_leases.release_run("watchdog")
    assert states == ["releasing"] and manager.stop_requested


@pytest.mark.parametrize("operation", ["probe", "build"])
async def test_image_command_uses_exit_code_without_waiting_for_transport__P0F18(tmp_path, monkeypatch, operation):
    import collimate_server.sandbox as sandbox
    process = _PipeHeldProcess()
    process.returncode = 0
    process.stdout = asyncio.StreamReader()
    process.stdout.feed_eof()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
    if operation == "probe":
        assert await asyncio.wait_for(sandbox._probe_docker_image("test-image"), 1) == ("present", "")
    else:
        (tmp_path / "Dockerfile").write_text("FROM scratch\n")
        await asyncio.wait_for(sandbox._build_image("test-image", str(tmp_path), lambda line: None), 1)
