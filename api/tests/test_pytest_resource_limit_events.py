"""P6R03: pre-exec memory refusal reaches the parent's persisted run events."""
from __future__ import annotations

import asyncio
from collections import deque
import resource
import sqlite3
import sys
import threading
import uuid

import pytest

from process_manager.manager import ProcessManager
import process_manager.streams as streams


MESSAGE = "memory cap not applied on this platform\n"
CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:resource-limit-events:channel"))


async def _child_events(tmp_path, monkeypatch, preexec):
    """Real fork/exec and production drain/flush, with a local event database."""
    database = tmp_path / "events.sqlite"

    def connect(_channel):
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn

    conn = connect(CHANNEL)
    conn.execute("CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, "
                 "event_type TEXT, payload TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(streams, "get_db_connection", connect)
    manager = ProcessManager.__new__(ProcessManager)
    manager.channel = CHANNEL
    manager.run_id = "run"
    manager.log_buffer = deque()
    manager._log_flush_lock = threading.Lock()
    manager._maybe_auto_running = lambda: None
    broadcasted = []
    manager._broadcast_run_events = lambda rows: broadcasted.extend(
        (row["event_type"], row["payload"]) for row in rows)
    manager.process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "print('child executed')",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        preexec_fn=preexec,
    )
    await asyncio.wait_for(asyncio.gather(
        manager._drain_stdout(), manager._drain_stderr(), manager.process.wait()), 10)
    assert manager.process.returncode == 0
    manager._flush_log_buffer()
    manager._flush_log_buffer()  # Finalization may flush an already empty buffer.
    conn = connect(CHANNEL)
    try:
        rows = [(row["event_type"], row["payload"]) for row in conn.execute(
            "SELECT event_type, payload FROM run_events ORDER BY id")]
    finally:
        conn.close()
    assert broadcasted == rows
    assert ("stdout", "child executed\n") in rows
    return rows


@pytest.mark.asyncio
async def test_kernel_memory_refusal_persists_one_run_event__P6R03(tmp_path, monkeypatch):
    """A generous child-only hard cap still makes raising it a real refusal."""
    # A hard cap far above anything the child commits, so the only refusal
    # the kernel can make is of the request to raise it.
    hard_cap = 512 * 1024**3
    apply_limits = ProcessManager._build_preexec_fn({
        "rlimits": {"memory_bytes": 2 * hard_cap},
    })

    def refuse_requested_memory():
        resource.setrlimit(resource.RLIMIT_DATA, (hard_cap, hard_cap))
        apply_limits()

    rows = await _child_events(tmp_path, monkeypatch, refuse_requested_memory)
    assert [payload for kind, payload in rows if kind == "stderr"] == [MESSAGE]


@pytest.mark.asyncio
@pytest.mark.parametrize("case, warning", [
    ("oserror", True), ("valueerror", True), ("success", False),
    ("absent", False), ("unrequested", False), ("other_axis", False),
])
async def test_only_requested_memory_refusal_emits_event__P6R03(
    tmp_path, monkeypatch, case, warning,
):
    """Synthetic platform outcomes still traverse a real child stderr pipe."""
    def setrlimit(which, _limits):
        if which == 2 and case in {"oserror", "valueerror"}:
            raise (OSError if case == "oserror" else ValueError)("refused")
        if which != 2 and case == "other_axis":
            raise OSError("refused")

    fake_resource = type("Resource", (), {
        "RLIMIT_CPU": 0, "RLIMIT_NOFILE": 7,
        "setrlimit": staticmethod(setrlimit),
        **({} if case == "absent" else {"RLIMIT_DATA": 2}),
    })
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    if case == "unrequested":
        monkeypatch.setattr(ProcessManager, "_DEFAULT_RLIMIT_MEMORY_BYTES", None)
    rows = await _child_events(tmp_path, monkeypatch, ProcessManager._build_preexec_fn({}))
    assert [payload for kind, payload in rows if kind == "stderr"] == ([MESSAGE] if warning else [])


def test_unavailable_stderr_does_not_abort_limit_setup__P6R03(monkeypatch):
    import os

    attempted = []
    reported = []

    def setrlimit(which, _limits):
        attempted.append(which)
        if which == 2:
            raise OSError("memory refused")

    def unavailable(fd, message):
        reported.append((fd, message))
        raise OSError("stderr unavailable")

    fake_resource = type("Resource", (), {
        "RLIMIT_DATA": 2, "RLIMIT_CPU": 0, "RLIMIT_NOFILE": 7,
        "setrlimit": staticmethod(setrlimit),
    })
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "resource", fake_resource)
        patch.setattr(os, "write", unavailable)
        ProcessManager._build_preexec_fn({})()
    assert attempted == [2, 0, 7]
    assert reported == [(2, MESSAGE.encode())]
