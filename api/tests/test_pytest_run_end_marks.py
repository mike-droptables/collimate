# SPDX-License-Identifier: MIT
"""The marks a run's end leaves on its manager: every stop — the user's
Stop (graceful or forced, live or already exited), Stop all and the watchdog
— sets the sticky ``stop_requested`` before any signal, and finalization
keeps the run's final status for the stop route's re-broadcast and the
/runs/active phase. Synthetic stubs throughout."""
from __future__ import annotations

import asyncio
import types
from collections import deque

import pytest

from fake_manager import FakeManager


class _LiveProcess:
    pid = 2 ** 30   # no such process: the signals no-op
    returncode = None

    async def wait(self):
        return 0


class _DeadProcess(_LiveProcess):
    returncode = 0


class _Registry(dict):
    def items(self):
        return list(super().items())


def _seed_run(channel: str) -> str:
    import uuid
    from database import get_db_connection
    rid = str(uuid.uuid4())
    conn = get_db_connection(channel)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', 'running')",
                     (rid,))
        conn.commit()
    finally:
        conn.close()
    return rid


@pytest.mark.parametrize("process,force", [
    (_LiveProcess, False), (_LiveProcess, True), (_DeadProcess, False)])
def test_every_user_stop_marks_the_run_stopping(client, process, force):
    """A graceful stop signals first and marks ``stopped`` after, so a
    generator exiting 0 on SIGTERM can be finalized in between: the mark
    must be set before the signal, on every stop path."""
    from run_registry import active_managers
    rid = _seed_run(client.channel_id)
    mgr = FakeManager(
        channel=client.channel_id, run_id=rid, cell_id="cell-s",
        generator_name="g", process=process(), _ipc_server=None,
        _pending_renderer_actions=[],
        _flush_log_buffer=lambda: None,
        _broadcast_run_status=lambda _s: None)
    active_managers[rid] = mgr
    try:
        r = client.delete(f"/api/channels/{client.channel_id}/runs/{rid}"
                          + ("?force=true" if force else ""))
    finally:
        active_managers.pop(rid, None)
    assert r.status_code == 200, r.text
    assert mgr.stop_requested is True


def test_stop_all_marks_every_run_stopping(fresh_cwd, monkeypatch):
    from sessions import terminal as terminal_sessions
    import routes.runs.helpers as helpers
    from routes.runs import teardown
    mgrs = {f"r{i}": FakeManager(
        channel=None, process=None, abort_spawn=lambda: None, app_dir="",
        workspace_dir="", _broadcast_run_status=lambda _s: None) for i in range(2)}
    monkeypatch.setattr(teardown, "active_managers", _Registry(mgrs))
    monkeypatch.setattr(helpers, "_reap_run_containers", lambda _rid: None)
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)

    asyncio.run(teardown.stop_all_runs(grace_seconds=0))
    assert all(m.stop_requested is True for m in mgrs.values())


def test_the_watchdog_marks_the_run_stopping__F1916():
    """A run the watchdog ends is stopping like every other stop — even one
    whose finish op already marked it completed before it overran."""
    from process_manager.manager import ProcessManager
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    stub = types.SimpleNamespace(
        current_phase="running", process=_LiveProcess(), log_buffer=deque(),
        timeout_reason=None, _terminal_attached=lambda: False,
        _watchdog_sleep=sleep)
    asyncio.run(ProcessManager._enforce_timeout(stub, 0.01))
    assert sleeps == [0.01]
    assert stub.timeout_reason
    assert stub.stop_requested is True


def test_finalization_keeps_the_final_status(client):
    from process_manager.finalization import FinalizationMixin

    class _Dummy(FinalizationMixin):
        def __init__(self, run_id):
            self.channel = client.channel_id
            self.run_id = run_id
            self.process = types.SimpleNamespace(returncode=0)
            self.cell_id = None
            self.workspace_dir = "/nonexistent"
            self.timeout_reason = None
            self.final_status = None

        def _broadcast_run_status(self, _s, _r=None):
            pass

        def _apply_phase(self, _p, _m=""):
            pass

        def _flush_log_buffer(self):
            pass

    dummy = _Dummy(_seed_run(client.channel_id))
    dummy._finalize_run_status()
    assert dummy.final_status == "completed"
