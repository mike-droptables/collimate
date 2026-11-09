"""Startup restore and channel-database opens in the channel-folder layout.

Plan 1105 P1 removed what this file used to pin: channel databases that no
channel owns no longer exist (every channel is a folder with its own
``channel.json``; startup scans the folders, parks one whose ``channel.json``
does not open and marks a channel whose layout does not validate
unavailable, see test_pytest_channel_folders.py). So the orphan-database
reconcile (ghost removal, parking under channels/orphaned), the app_state
quarantine (``app_state.invalid-*``) and the label-vs-orphan name hostage
tests are gone. What stays is what still exists: a locked or unreadable
app.db at startup, a refused start running nothing, a channel database that
is opened and never created, and a delete that leaves another channel's
bodies alone."""
from __future__ import annotations

import glob
import os
import sqlite3
import time
import uuid

import pytest

from collimate_server.database import get_db_connection


def _boot(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def _create(client, name: str) -> str:
    r = client.post("/api/channels", params={"name": name})
    assert r.status_code == 200, r.text
    from collimate_server.state import flush_persist_state
    flush_persist_state()
    return r.json()["id"]


def _saved_state_row() -> str | None:
    from collimate_server.database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        row = conn.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def _folder(channel_id: str):
    import collimate_server.channel_paths as channel_paths
    return channel_paths.channel_dir(channel_id)


def test_duplicate_display_labels_create_independent_channel_folders(client):
    import collimate_server.channel_paths as channel_paths
    first = client.post("/api/channels", params={"name": "Twice"})
    second = client.post("/api/channels", params={"name": "Twice"})
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["name"] == second.json()["name"] == "Twice"
    assert all(os.path.isfile(channel_paths.db_path(response.json()["id"])) for response in (first, second))


@pytest.mark.real_channel_homes
def test_an_open_never_makes_a_channel_db__F0030(fresh_cwd):
    """``get_db_connection`` opens only a ``channel.db`` that is there: a
    channel with no folder is ChannelUnavailable, a folder whose database is
    gone FileNotFoundError, and neither leaves a file behind."""
    import collimate_server.channel_paths as channel_paths
    from conftest import channel_home
    absent = str(uuid.uuid4())
    with pytest.raises(channel_paths.ChannelUnavailable):
        get_db_connection(absent)
    assert not list(fresh_cwd.glob(f"**/{absent}*"))

    present = channel_home()
    conn = get_db_connection(present)
    conn.execute("INSERT INTO cells (id, source_type) VALUES ('cell-1', 'user')")
    conn.commit()
    conn.close()
    db = channel_paths.db_path(present)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(f"{db}{suffix}"):
            os.remove(f"{db}{suffix}")
    os.rename(db, f"{db}.moved")
    with pytest.raises(FileNotFoundError):
        get_db_connection(present)
    assert not os.path.exists(db), "the open created no database"
    os.rename(f"{db}.moved", db)
    conn = get_db_connection(present)
    try:
        assert [r["id"] for r in conn.execute("SELECT id FROM cells")] == ["cell-1"]
    finally:
        conn.close()


@pytest.mark.parametrize("move", ["rename", "unlink", "replace"])
def test_an_open_refuses_a_db_moved_while_it_opens__F0030(fresh_cwd, monkeypatch, move):
    """The connect holds the file it found, but the journal pragma opens the
    -wal and -shm beside the db by its name. A rename or unlink in between,
    or another file put under the name, is FileNotFoundError, never a
    connection whose -wal belongs to another file."""
    import collimate_server.channel_paths as channel_paths
    from conftest import channel_home
    channel_id = channel_home()
    conn = get_db_connection(channel_id)
    conn.execute("INSERT INTO cells (id, source_type) VALUES ('cell-1', 'user')")
    conn.commit()
    conn.close()
    db = str(channel_paths.db_path(channel_id))
    folder = os.path.dirname(db)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)
    real_connect = sqlite3.connect

    def connect_then_move(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        for suffix in ("", "-wal", "-shm"):
            source = db + suffix
            if not os.path.exists(source):
                continue
            if move == "unlink":
                os.remove(source)
            else:
                os.rename(source, os.path.join(folder, f"renamed.db{suffix}"))
        if move == "replace":
            open(db, "wb").close()
        return conn

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", connect_then_move)
        with pytest.raises(FileNotFoundError):
            get_db_connection(channel_id)

    if move == "replace":
        assert os.path.getsize(db) == 0, "the open wrote nothing into the file now under the name"
    else:
        assert not glob.glob(db + "*")
    if move != "unlink":
        moved = sqlite3.connect(f"file:{os.path.join(folder, 'renamed.db')}?mode=ro", uri=True)
        try:
            assert moved.execute("SELECT id FROM cells").fetchall() == [("cell-1",)]
        finally:
            moved.close()


def test_startup_retries_a_locked_app_db__F0001(api_app, monkeypatch):
    import collimate_server.genesis as genesis
    import collimate_server.main as main
    with _boot(api_app) as c:
        channel_id = _create(c, "Work")
    real = genesis.load_active_channel_id
    calls = []

    def locked_once():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real()

    monkeypatch.setattr(genesis, "load_active_channel_id", locked_once)
    monkeypatch.setattr(main, "_APP_DB_RETRY_SECONDS", 0)
    with _boot(api_app):
        from collimate_server.state import state
        assert [ch.name for ch in state.channels] == ["Work"]
    assert len(calls) == 2
    assert os.path.isfile(_folder(channel_id) / "channel.db")


def test_startup_refuses_when_the_active_channel_cannot_be_read__F0001(api_app, monkeypatch):
    """A lock that outlasts the retries, or an unreadable app.db, stops the
    start before anything could persist or change a channel folder."""
    import collimate_server.genesis as genesis
    import collimate_server.main as main
    with _boot(api_app) as c:
        channel_id = _create(c, "Work")
    folder = _folder(channel_id)
    manifest = (folder / "channel.json").read_bytes()
    saved = _saved_state_row()

    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(genesis, "load_active_channel_id", locked)
    monkeypatch.setattr(main, "_APP_DB_RETRY_SECONDS", 0)
    with pytest.raises(main.StartupRefused, match="app.db"):
        with _boot(api_app):
            pass
    assert (folder / "channel.db").exists()
    assert (folder / "channel.json").read_bytes() == manifest
    assert _saved_state_row() == saved

    def corrupt():
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(genesis, "load_active_channel_id", corrupt)
    with pytest.raises(main.StartupRefused, match="file is not a database"):
        with _boot(api_app):
            pass
    assert (folder / "channel.db").exists()
    assert (folder / "channel.json").read_bytes() == manifest
    # The refused start released the data dir: the next one may take it.
    fd = main.lock_data_dir()
    main.unlock_data_dir(fd)


def test_a_refused_start_leaves_no_reap_or_lease_sweep_running__F0001(api_app, monkeypatch):
    """The container reaps and the account lease sweep run in a worker
    thread; a start refused at the restore must not have scheduled them,
    or they would run on after its lifespan released the server lock."""
    import sys
    import collimate_server.accounts as accounts
    import collimate_server.genesis as genesis
    import collimate_server.main as main
    import collimate_server.sandbox as sandbox
    ran = []
    monkeypatch.setitem(sys.modules, "docker", None)   # the docker reap: ImportError, skipped
    monkeypatch.setattr(sandbox, "reap_orphan_containers", lambda: ran.append("sandbox reap"))
    monkeypatch.setattr(accounts, "sweep_expired_account_leases", lambda: ran.append("lease sweep"))

    def unreadable():
        raise sqlite3.DatabaseError("file is not a database")

    real = genesis.load_active_channel_id
    monkeypatch.setattr(genesis, "load_active_channel_id", unreadable)
    with pytest.raises(main.StartupRefused):
        with _boot(api_app):
            pass
    time.sleep(0.3)
    assert ran == []
    # A start that goes through does schedule them.
    monkeypatch.setattr(genesis, "load_active_channel_id", real)
    with _boot(api_app):
        deadline = time.monotonic() + 5
        while len(ran) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert ran == ["sandbox reap", "lease sweep"]


class _AppStateSelectFails:
    """A real app.db connection whose read of the saved app state fails the
    way a lock held past the busy timeout does; every other statement runs."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if "key='app_state'" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_startup_refuses_a_real_read_error_from_app_db__F0001(api_app, monkeypatch):
    """The real genesis.load_active_channel_id, not a stand-in: its failing
    read must reach the startup restore as an error, never as "nothing
    saved" (which would boot with no active channel and persist over it)."""
    import collimate_server.database as database
    import collimate_server.main as main
    with _boot(api_app) as c:
        channel_id = _create(c, "Work")
    saved = _saved_state_row()
    real_connection = database.get_global_db_connection
    monkeypatch.setattr(database, "get_global_db_connection",
                        lambda: _AppStateSelectFails(real_connection()))
    monkeypatch.setattr(main, "_APP_DB_RETRY_SECONDS", 0)
    with pytest.raises(main.StartupRefused, match=r"app\.db: database is locked"):
        with _boot(api_app):
            pass
    monkeypatch.setattr(database, "get_global_db_connection", real_connection)
    assert (_folder(channel_id) / "channel.db").exists()
    assert _saved_state_row() == saved


def test_load_active_channel_id_answers_none_only_when_nothing_is_saved__F0001(fresh_cwd, monkeypatch):
    import json
    import collimate_server.database as database
    import collimate_server.genesis as genesis
    assert genesis.load_active_channel_id() is None
    active = str(uuid.uuid4())
    genesis.save_active_channel_id(active)
    assert genesis.load_active_channel_id() == active
    assert json.loads(_saved_state_row()) == {"storage_format": genesis.APP_STATE_FORMAT, "active_channel_id": active}
    # A NULL value is a row that is there and names no channel.
    conn = database.get_global_db_connection()
    conn.execute("UPDATE ui_state SET value=NULL WHERE key='app_state'")
    conn.commit()
    conn.close()
    assert genesis.load_active_channel_id() is None
    real_connection = database.get_global_db_connection
    monkeypatch.setattr(database, "get_global_db_connection",
                        lambda: _AppStateSelectFails(real_connection()))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        genesis.load_active_channel_id()


def test_deleting_a_channel_keeps_another_channels_bodies__F0001_F0053(client):
    """Each channel's bodies live in its own folder, so deleting one removes
    only its own, even for identical content the other channel also holds."""
    from collimate_server.cas import channel_store
    keeper = _create(client, "Keeper")
    doomed = _create(client, "Doomed")
    raw = b"a seeded file every channel carries"
    digest = channel_store(keeper).hash_and_store_bytes(raw)
    assert channel_store(doomed).hash_and_store_bytes(raw) == digest
    doomed_folder = _folder(doomed)
    r = client.delete(f"/api/channels/{doomed}")
    assert r.status_code == 200, r.text
    assert not doomed_folder.exists()
    assert channel_store(keeper).require_blob(digest) == raw
