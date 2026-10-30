"""Pins for measured server startup/checkpoint work."""
import sqlite3
import time
import pytest


def test_checkpoint_does_not_wait_for_pinned_reader__F0032(fresh_cwd):
    import wal_checkpoint
    path = fresh_cwd / "probe.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t(value)")
    writer.execute("INSERT INTO t VALUES (1)")
    writer.commit()
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t").fetchall()
    writer.execute("INSERT INTO t VALUES (2)")
    writer.commit()
    try:
        started = time.monotonic()
        result = wal_checkpoint._checkpoint_one(str(path))
        elapsed = time.monotonic() - started
        assert result[0] == 0
        assert elapsed < 0.5
        writer.execute("INSERT INTO t VALUES (3)")
        writer.commit()
    finally:
        reader.close()
        writer.close()


def test_readiness_reuses_informational_startup_work__F0078(fresh_cwd, monkeypatch):
    import bundled_integrity
    import cas_gc
    import main
    calls = []
    monkeypatch.setattr(cas_gc, "disk_usage", lambda: (calls.append("cas"), {"blob_count": 0, "bytes": 0})[1])
    monkeypatch.setattr(bundled_integrity, "verify_bundled", lambda: (calls.append("bundled"), {"status": "ok"})[1])
    if hasattr(main, "_load_readiness_diagnostics"):
        main._load_readiness_diagnostics()
    main.ready()
    main.ready()
    assert calls.count("cas") == 1
    assert calls.count("bundled") == 1


def test_noop_startup_sweep_queries_once_per_channel__F0079(fresh_cwd, monkeypatch):
    import cell_commits
    import state
    from database import get_db_connection
    from models import AppState, Cell, Channel, Stack
    ch = Channel(name="sweep", main_stacks=[Stack(cells=[Cell() for _ in range(100)])])
    monkeypatch.setattr(state, "state", AppState(channels=[ch]))
    conn = get_db_connection(str(ch.id))
    conn.close()
    calls = []
    def connect(*args, **kwargs):
        calls.append(1)
        return get_db_connection(*args, **kwargs)
    monkeypatch.setattr(cell_commits, "get_db_connection", connect)
    assert cell_commits.sweep_orphan_run_folders() == 0
    assert len(calls) == 1


@pytest.mark.parametrize("declared", [True, False])
async def test_blob_body_limit_is_cas_limit_before_buffering__F0288(monkeypatch, declared):
    import cas
    from main import BodySizeLimitMiddleware
    monkeypatch.setattr(cas, "MAX_BLOB_BYTES", 4)
    sent = []
    async def receive():
        return {"type": "http.request", "body": b"12345", "more_body": False}
    async def send(message):
        sent.append(message)
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    headers = [(b"content-length", b"5")] if declared else []
    await BodySizeLimitMiddleware(app, limit=8)({"type": "http", "path": "/api/blob", "headers": headers}, receive, send)
    assert sent[0]["status"] == 413


async def test_channel_delete_moves_database_work_off_loop__F0334(fresh_cwd, monkeypatch):
    import threading
    import cas_gc
    import state
    from database import get_db_connection
    from models import AppState, Channel
    from routes import channels
    ch = Channel(name="deleting")
    app = AppState(channels=[ch], active_channel_id=ch.id)
    monkeypatch.setattr(state, "state", app)
    monkeypatch.setattr(channels, "state", app)
    get_db_connection(str(ch.id)).close()
    threads = []
    monkeypatch.setattr(channels, "_checkpoint_and_release", lambda _identity: threads.append(threading.get_ident()))
    loop_thread = threading.get_ident()
    result = await channels.delete_channel(ch.id)
    await cas_gc.wait_for_collection()
    assert result["status"] == "deleted"
    assert threads and all(thread != loop_thread for thread in threads)
@pytest.mark.parametrize("operation", ["rename", "delete"])
async def test_channel_cancellation_keeps_reservation_until_durable_finish__F0334_F0372(fresh_cwd, monkeypatch, operation):
    import asyncio
    import threading
    import genesis
    import state
    import database
    from models import AppState, Channel
    from routes import channels
    channel = Channel(name="Old")
    current = AppState(channels=[channel], active_channel_id=channel.id)
    monkeypatch.setattr(state, "state", current)
    monkeypatch.setattr(channels, "state", current)
    identity = str(channel.id)
    database.get_db_connection(identity).close()
    state.start_persistence_writer(delay=60)
    entered, release = threading.Event(), threading.Event()
    if operation == "rename":
        original = state.flush_persist_state

        def blocked():
            entered.set()
            assert release.wait(5)
            return original()

        monkeypatch.setattr(state, "flush_persist_state", blocked)
    else:
        original = channels._safe_remove

        def blocked(path):
            if path.endswith(f"{identity}.db"):
                entered.set()
                assert release.wait(5)
            return original(path)

        monkeypatch.setattr(channels, "_safe_remove", blocked)
    import cas_gc
    monkeypatch.setattr(cas_gc, "request_collection", lambda: None)
    task = asyncio.create_task(channels.rename_channel(channel.id, "New") if operation == "rename" else channels.delete_channel(channel.id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        if operation == "delete":
            assert identity in database.changing_channels
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        saved = genesis.parse_saved_app_state(genesis.load_global_ui_state())
        assert not database.changing_channels
        if operation == "rename":
            assert channel.name == saved.channels[0].name == "New"
            assert (fresh_cwd / f".collimate/channels/{identity}.db").exists()
            assert str(saved.channels[0].id) == identity
        else:
            assert not current.channels and not saved.channels
            assert not (fresh_cwd / f".collimate/channels/{identity}.db").exists()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(state.stop_persistence_writer)


@pytest.mark.parametrize("scope", ["global", "channel"])
def test_per_call_connections_have_independent_lifetimes_and_transactions__F0086(fresh_cwd, scope):
    """Retained behavior: callers own fresh connections, not a shared cache."""
    from database import get_db_connection, get_global_db_connection
    from uuid import uuid4
    identity = str(uuid4())
    connect = get_global_db_connection if scope == "global" else lambda: get_db_connection(identity)
    first, second = connect(), connect()
    try:
        assert first is not second
        first.execute("CREATE TEMP TABLE caller_private(value)")
        first.execute("INSERT INTO caller_private VALUES ('pending')")
        assert first.in_transaction and not second.in_transaction
        assert second.execute("SELECT count(*) FROM sqlite_temp_master WHERE name='caller_private'").fetchone()[0] == 0
        first.close()
        assert second.execute("SELECT 1").fetchone()[0] == 1
        assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert second.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        first.close()
        second.close()
