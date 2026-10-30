"""StreamsMixin._flush_log_buffer: the microbatch flush persists each batch
exactly once and broadcasts exactly the rows it wrote, even when another
writer inserts run_events for the same run or a step after the INSERT
fails."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from collections import deque

import pytest

import process_manager.streams as streams
from database import get_db_connection

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:log-flush:channel"))


class _Conn:
    """Delegating proxy over a real channel connection with hooks."""

    def __init__(self, real, *, after_commit=None, fail_select=False):
        self._real = real
        self._after_commit = after_commit
        self._fail_select = fail_select

    def execute(self, sql, *args):
        if self._fail_select and sql.lstrip().upper().startswith("SELECT"):
            raise sqlite3.OperationalError("simulated readback failure")
        return self._real.execute(sql, *args)

    def commit(self):
        self._real.commit()
        if self._after_commit:
            self._after_commit()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _manager():
    run_id = str(uuid.uuid4())
    conn = get_db_connection(CHANNEL)
    conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, '', 'running')", (run_id,))
    conn.commit()
    conn.close()

    class _M(streams.StreamsMixin):
        pass

    m = _M()
    m.channel = CHANNEL
    m.run_id = run_id
    m._log_flush_lock = threading.Lock()
    m.log_buffer = deque(
        {"type": "stdout", "payload": f"line {i}\n"} for i in range(3))
    m.broadcasted = []
    m._broadcast_run_events = lambda rows: m.broadcasted.extend(r["payload"] for r in rows)
    return m


def _stored(run_id):
    conn = get_db_connection(CHANNEL)
    try:
        return [r["payload"] for r in conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? ORDER BY id", (run_id,))]
    finally:
        conn.close()


BATCH = ["line 0\n", "line 1\n", "line 2\n"]


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(streams.time, "sleep", lambda _s: None)


def test_flush_broadcasts_only_its_own_rows(client, monkeypatch):
    """A row another writer commits right after the flush's commit must not
    replace one of the batch's rows in the broadcast."""
    m = _manager()

    def _foreign():
        other = get_db_connection(CHANNEL)
        other.execute(
            "INSERT INTO run_events (run_id, event_type, payload) VALUES (?, 'commit', 'foreign')",
            (m.run_id,))
        other.commit()
        other.close()

    monkeypatch.setattr(
        streams, "get_db_connection",
        lambda ch: _Conn(get_db_connection(ch), after_commit=_foreign))
    m._flush_log_buffer()

    assert m.broadcasted == BATCH
    assert _stored(m.run_id) == BATCH + ["foreign"]


def test_flush_readback_failure_does_not_duplicate_rows(client, monkeypatch, no_sleep):
    """A failure after the INSERT retries without persisting the batch
    twice."""
    m = _manager()
    calls = []

    def _get(ch):
        calls.append(ch)
        return _Conn(get_db_connection(ch), fail_select=len(calls) == 1)

    monkeypatch.setattr(streams, "get_db_connection", _get)
    m._flush_log_buffer()

    assert _stored(m.run_id) == BATCH
    assert m.broadcasted == BATCH
    assert not m.log_buffer


def test_flush_broadcast_failure_keeps_rows_once(client):
    """A broadcast error neither re-inserts nor re-queues persisted rows."""
    m = _manager()

    def _boom(rows):
        raise RuntimeError("broadcast down")

    m._broadcast_run_events = _boom
    m._flush_log_buffer()

    assert _stored(m.run_id) == BATCH
    assert not m.log_buffer


@pytest.mark.asyncio
async def test_failed_close_sdk_log_persists_without_ipc__P4F47__P6R01(client, tmp_path, monkeypatch):
    """SDK diagnostics use stderr, so failed close transport cannot hide its run event."""
    import asyncio
    import io
    import logging

    from collimate import native
    from collimate.native import HostBridge

    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    bridge = HostBridge()
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    monkeypatch.setattr(bridge.log, "handlers", [handler])
    monkeypatch.setattr(native, "_bridge", bridge)
    native.log("[environment close] remote removal refused")

    manager = _manager()
    manager.log_buffer.clear()
    manager._maybe_auto_running = lambda: None
    stream = asyncio.StreamReader()
    stream.feed_data(captured.getvalue().encode())
    stream.feed_eof()
    await manager._drain_stream(stream, "stderr")
    manager._flush_log_buffer()
    assert _stored(manager.run_id) == ["[INFO] [environment close] remote removal refused\n"]
    assert manager.broadcasted == _stored(manager.run_id)
