"""Coverage for wal_checkpoint — periodic WAL truncation.

Two questions worth pinning:

  * Does ``checkpoint_all`` walk every channel DB and the global DB?
  * Does ``run_periodic`` survive a per-pass failure (corrupt DB,
    missing file) without crashing the whole task?

We don't try to verify SQLite's own behavior — that's its job. The
tests focus on the iteration + error-handling glue."""
from __future__ import annotations

import asyncio
import os
import sqlite3


def _make_db(path):
    """Create a small WAL-mode DB with one row written, one un-checkpointed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (k TEXT)")
    conn.execute("INSERT INTO t (k) VALUES ('x')")
    conn.commit()
    conn.close()


def test_checkpoint_all_walks_every_channel(fresh_cwd):
    """Both the global app.db and each channel/*.db should be visited."""
    _make_db(os.path.join(str(fresh_cwd), ".collimate", "app.db"))
    _make_db(os.path.join(str(fresh_cwd), ".collimate", "channels", "alpha.db"))
    _make_db(os.path.join(str(fresh_cwd), ".collimate", "channels", "beta.db"))

    import wal_checkpoint
    summary = wal_checkpoint.checkpoint_all()
    assert summary["global"] is not None  # app.db got checkpointed
    assert "alpha" in summary["channels"]
    assert "beta" in summary["channels"]


def test_checkpoint_skips_missing_global_db(fresh_cwd):
    """No app.db on disk — should return None for the global slot, not raise."""
    import wal_checkpoint
    summary = wal_checkpoint.checkpoint_all()
    assert summary["global"] is None


def test_checkpoint_tolerates_corrupt_db(fresh_cwd):
    """A garbage byte where a DB is expected should NOT crash the
    checkpoint pass — it just returns None for that DB."""
    bad = os.path.join(str(fresh_cwd), ".collimate", "channels", "broken.db")
    os.makedirs(os.path.dirname(bad), exist_ok=True)
    with open(bad, "wb") as f:
        f.write(b"\x00\xff garbage")

    _make_db(os.path.join(str(fresh_cwd), ".collimate", "channels", "good.db"))

    import wal_checkpoint
    summary = wal_checkpoint.checkpoint_all()
    assert summary["channels"]["broken"] is None
    assert summary["channels"]["good"] is not None


def test_run_periodic_invokes_checkpoint(fresh_cwd, monkeypatch):
    """The periodic task should call ``checkpoint_all`` at least once
    within a short interval. We bound the test to a few hundred ms by
    using a small interval and cancelling soon after."""
    import wal_checkpoint

    calls = []
    real = wal_checkpoint.checkpoint_all

    def _spy():
        calls.append(1)
        return real()

    monkeypatch.setattr(wal_checkpoint, "checkpoint_all", _spy)

    async def _scenario():
        # Use a tiny timeout-controlled wait: schedule the task with a
        # 1s interval, sleep a bit longer than that, then cancel.
        task = asyncio.create_task(wal_checkpoint.run_periodic(0.1))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_scenario())
    # At least one ordinary pass + the final cancellation pass.
    assert len(calls) >= 1


def test_shutdown_final_checkpoint_runs_after_run_teardown(api_app, monkeypatch):
    """The lifespan's final checkpoint must follow stop_all_runs and the
    wait for its runs to leave the registry, so the teardown's own commits
    (those of a run-end cleanup that ends during that wait included) are
    checkpointed too."""
    import routes.runs
    import routes.runs.teardown as teardown
    import wal_checkpoint
    from fastapi.testclient import TestClient

    order: list[str] = []

    async def _fake_stop_all(*a, **k):
        order.append("stop_all_runs")
        return {}

    async def _fake_wait_for_run_reaps(*a, **k):
        order.append("wait_for_run_reaps")
        return []

    monkeypatch.setattr(routes.runs, "stop_all_runs", _fake_stop_all)
    monkeypatch.setattr(teardown, "wait_for_run_reaps", _fake_wait_for_run_reaps)
    monkeypatch.setattr(wal_checkpoint, "checkpoint_all", lambda: order.append("checkpoint"))
    with TestClient(api_app):
        pass
    assert order.index("stop_all_runs") < order.index("wait_for_run_reaps")
    assert order[-1] == "checkpoint"


def test_production_connections_use_full(fresh_cwd):
    """Inspect actual channel and global stores, rather than a source default."""
    import uuid
    import database

    for opener in (lambda: database.get_db_connection(str(uuid.uuid4())),
                   database.get_global_db_connection):
        conn = opener()
        try:
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            conn.close()


def test_full_policy_survives_real_transaction_and_reopen(fresh_cwd):
    import uuid
    import database

    channel_id = str(uuid.uuid4())
    for opener in (lambda: database.get_db_connection(channel_id),
                   database.get_global_db_connection):
        conn = opener()
        try:
            with conn:
                conn.execute("INSERT INTO ui_state(key,value) VALUES ('policy-proof','committed')")
        finally:
            conn.close()
        reopened = opener()
        try:
            assert reopened.execute("PRAGMA synchronous").fetchone()[0] == 2
            assert reopened.execute("SELECT value FROM ui_state WHERE key='policy-proof'").fetchone()[0] == 'committed'
        finally:
            reopened.close()
