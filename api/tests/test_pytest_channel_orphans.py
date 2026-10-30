"""A channel database file that no channel owns — a creation that died
half-way, or a wipe that raced a running server — must never hold its name
hostage: a ghost (schema only) is removed, a file with data is parked under
channels/orphaned, at startup and when the name is created again.

But "no channel owns it" must mean the saved app state said so: a startup
that could not read the saved state refuses to start, and one whose saved
state does not validate keeps the row aside and leaves every channel
database where it is (main.restore_app_state)."""
from __future__ import annotations

import glob
import os
import sqlite3
import time
import uuid

import pytest

from database import channel_db_holds_data, get_db_connection, reconcile_orphan_channel_dbs


def _ghost(slug: str) -> str:
    """A physical orphan fixture; runtime channel lookup never resolves its label."""
    from migrations import upgrade_channel_db
    path = os.path.join('.collimate', 'channels', f'{slug}.db')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    upgrade_channel_db(conn)
    conn.close()
    return path


def _with_data(slug: str) -> str:
    path = _ghost(slug)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO cells (id, source_type) VALUES ('cell-1', 'user')")
    conn.commit()
    conn.close()
    return path


def test_creating_a_channel_after_schema_orphan_reconciliation_succeeds(client):
    from state import state
    path = _ghost("ghost")
    assert os.path.exists(path) and not channel_db_holds_data("ghost")
    reconcile_orphan_channel_dbs({str(ch.id) for ch in state.channels})
    assert not os.path.exists(path)
    r = client.post("/api/channels", params={"name": "Ghost"})
    assert r.status_code == 200, r.text
    assert any(c["name"] == "Ghost" for c in client.get("/api/state").json()["channels"])
    assert not glob.glob(".collimate/channels/orphaned/ghost-*.db"), "a ghost is dropped, not parked"


def test_recreated_channel_has_new_identity_after_unowned_data_is_parked(client):
    from state import state
    r = client.post("/api/channels", params={"name": "Parked"})
    assert r.status_code == 200, r.text
    original_id = r.json()['id']
    conn = get_db_connection(original_id)
    conn.execute("INSERT INTO cells (id, source_type) VALUES ('cell-1', 'user')")
    conn.commit()
    conn.close()
    assert channel_db_holds_data(original_id)
    # The registration is lost (a wipe raced the server); the file stays.
    state.channels = [c for c in state.channels if c.name != "Parked"]
    reconcile_orphan_channel_dbs({str(ch.id) for ch in state.channels})
    r = client.post("/api/channels", params={"name": "Parked"})
    assert r.status_code == 200, r.text
    assert r.json()['id'] != original_id
    parked = glob.glob(f".collimate/channels/orphaned/{original_id}-*.db")
    assert len(parked) == 1, "the data was moved aside, not deleted"
    import sqlite3
    old = sqlite3.connect(f"file:{os.path.abspath(parked[0])}?mode=ro", uri=True)
    assert old.execute("SELECT 1 FROM cells WHERE id='cell-1'").fetchone(), "the parked file keeps its rows"
    old.close()
    fresh = get_db_connection(r.json()['id'])
    assert fresh.execute("SELECT 1 FROM cells WHERE id='cell-1'").fetchone() is None, "the new channel does not inherit them"
    fresh.close()


def test_duplicate_display_labels_create_independent_uuid_databases(client):
    first = client.post("/api/channels", params={"name": "Twice"})
    second = client.post("/api/channels", params={"name": "Twice"})
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["name"] == second.json()["name"] == "Twice"
    assert all(os.path.isfile(f".collimate/channels/{response.json()['id']}.db") for response in (first, second))


def test_startup_reconcile_drops_ghosts_and_parks_data(client):
    from state import state
    ghost, data = _ghost("stale_ghost"), _with_data("stale_data")
    registered = {str(ch.id) for ch in state.channels}
    outcomes = reconcile_orphan_channel_dbs(registered)
    assert not os.path.exists(ghost) and not os.path.exists(data)
    assert len(glob.glob(".collimate/channels/orphaned/stale_data-*.db")) == 1
    assert any("removed the empty" in o and "stale_ghost" in o for o in outcomes)
    assert any("moved the unowned" in o and "stale_data" in o for o in outcomes)
    assert reconcile_orphan_channel_dbs(registered) == []


def test_an_open_without_create_never_makes_a_channel_db__F0030(fresh_cwd):
    """``get_db_connection(..., create=False)`` opens only a channel db file
    that is there; for any other name it raises FileNotFoundError and leaves
    no file behind."""
    absent, present = str(uuid.uuid4()), str(uuid.uuid4())
    with pytest.raises(FileNotFoundError):
        get_db_connection(absent, create=False)   # no channels folder yet
    _with_data(present)
    with pytest.raises(FileNotFoundError):
        get_db_connection(absent, create=False)
    assert not glob.glob(f".collimate/channels/{absent}.db*")
    conn = get_db_connection(present, create=False)
    try:
        assert [r["id"] for r in conn.execute("SELECT id FROM cells")] == ["cell-1"]
    finally:
        conn.close()


@pytest.mark.parametrize("move", ["rename", "unlink", "replace"])
def test_an_open_without_create_refuses_a_db_moved_while_it_opens__F0030(
    fresh_cwd, monkeypatch, move,
):
    """The connect holds the file it found, but the journal pragma opens the
    -wal and -shm beside the db by its name. A rename or unlink in between,
    or another file put under the name, is FileNotFoundError, never a
    connection whose -wal belongs to another file."""
    channel_id = str(uuid.uuid4())
    _with_data(channel_id)
    channels = os.path.join(".collimate", "channels")
    real_connect = sqlite3.connect

    def connect_then_move(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        for suffix in ("", "-wal", "-shm"):
            source = os.path.join(channels, f"{channel_id}.db{suffix}")
            if not os.path.exists(source):
                continue
            if move == "unlink":
                os.remove(source)
            else:
                os.rename(source, os.path.join(channels, f"renamed.db{suffix}"))
        if move == "replace":
            open(os.path.join(channels, f"{channel_id}.db"), "wb").close()
        return conn

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", connect_then_move)
        with pytest.raises(FileNotFoundError):
            get_db_connection(channel_id, create=False)

    if move == "replace":
        assert os.path.getsize(os.path.join(channels, f"{channel_id}.db")) == 0, \
            "the open wrote nothing into the file now under the name"
    else:
        assert not glob.glob(os.path.join(channels, f"{channel_id}.db*"))
    if move != "unlink":
        moved = sqlite3.connect(f"file:{os.path.abspath(os.path.join(channels, 'renamed.db'))}?mode=ro",
                                uri=True)
        try:
            assert moved.execute("SELECT id FROM cells").fetchall() == [("cell-1",)]
        finally:
            moved.close()


def _saved_state_row(key: str = "app_state") -> str | None:
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        row = conn.execute("SELECT value FROM ui_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def _quarantined() -> dict[str, str]:
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        return {
            r["key"]: r["value"] for r in conn.execute(
                "SELECT key, value FROM ui_state WHERE key GLOB 'app_state.invalid-*'")
        }
    finally:
        conn.close()


def _save_raw_state(raw: str) -> None:
    from database import get_global_db_connection
    conn = get_global_db_connection()
    conn.execute(
        "INSERT INTO ui_state (key, value) VALUES ('app_state', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (raw,))
    conn.commit()
    conn.close()


def _boot(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_startup_keeps_an_invalid_saved_state_aside_and_every_channel_db__F0001_F0053(api_app):
    """A saved state that no longer validates (an old row after a model
    change) boots the app empty — and nothing is lost: the row is kept under
    app_state.invalid-*, no channel database is parked or deleted, the first
    persist cannot destroy it, and later starts keep the databases in place
    until the row is repaired and its key deleted."""
    kept_data, kept_ghost = _with_data("work"), _ghost("empty_work")
    raw = '{"channels": "not a list", "active_channel_id": null}'
    _save_raw_state(raw)

    with _boot(api_app) as c:
        from state import state
        assert state.channels == []
        assert os.path.exists(kept_data) and os.path.exists(kept_ghost)
        assert not glob.glob(".collimate/channels/orphaned/*")
        assert list(_quarantined().values()) == [raw]
        # The first persist writes the empty state over app_state; the kept
        # copy is what survives it.
        assert c.post("/api/channels", params={"name": "Fresh"}).status_code == 200
    # persist_state writes from a worker thread; give it a moment to land.
    deadline = time.monotonic() + 5
    while "Fresh" not in (_saved_state_row() or "") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "Fresh" in _saved_state_row()
    assert list(_quarantined().values()) == [raw]

    # A later start restores the (now valid) state and still leaves the
    # databases alone while the unrestored row is kept.
    with _boot(api_app):
        from state import state
        assert [ch.name for ch in state.channels] == ["Fresh"]
        assert os.path.exists(kept_data) and os.path.exists(kept_ghost)
    assert len(_quarantined()) == 1

    # Repaired and its key deleted: the reconcile runs again.
    from database import get_global_db_connection
    conn = get_global_db_connection()
    conn.execute("DELETE FROM ui_state WHERE key GLOB 'app_state.invalid-*'")
    conn.commit()
    conn.close()
    with _boot(api_app):
        assert not os.path.exists(kept_data) and not os.path.exists(kept_ghost)
        assert len(glob.glob(".collimate/channels/orphaned/work-*.db")) == 1


def test_startup_keeps_one_copy_of_a_row_that_fails_twice__F0053(api_app):
    raw = "{not json"
    _save_raw_state(raw)
    with _boot(api_app):
        pass
    with _boot(api_app):  # nothing persisted in between: the same row fails again
        pass
    assert list(_quarantined().values()) == [raw]


def test_startup_retries_a_locked_app_db__F0001(api_app, monkeypatch):
    import json
    import genesis
    import main
    from models import AppState, Channel
    channel = Channel(name='Work')
    path = _with_data(str(channel.id))
    _save_raw_state(json.dumps(genesis.snapshot_app_state(AppState(channels=[channel]))))
    real = genesis.load_global_ui_state
    calls = []

    def locked_once():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real()

    monkeypatch.setattr(genesis, "load_global_ui_state", locked_once)
    monkeypatch.setattr(main, "_APP_DB_RETRY_SECONDS", 0)
    with _boot(api_app):
        from state import state
        assert [ch.name for ch in state.channels] == ["Work"]
    assert len(calls) == 2
    assert os.path.exists(path)
    with sqlite3.connect(path) as retained:
        assert retained.execute('SELECT id FROM cells').fetchall() == [('cell-1',)]


def test_startup_refuses_when_the_saved_state_cannot_be_read__F0001(api_app, monkeypatch):
    """A lock that outlasts the retries, or an unreadable app.db, stops the
    start before anything could persist the empty state or park a database."""
    import genesis
    import main
    data, ghost = _with_data("work"), _ghost("empty_work")
    from models import AppState, Channel
    saved = AppState(channels=[Channel(name="Work")]).model_dump_json()
    _save_raw_state(saved)

    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(genesis, "load_global_ui_state", locked)
    monkeypatch.setattr(main, "_APP_DB_RETRY_SECONDS", 0)
    with pytest.raises(main.StartupRefused, match="app.db"):
        with _boot(api_app):
            pass
    assert os.path.exists(data) and os.path.exists(ghost)
    assert not glob.glob(".collimate/channels/orphaned/*")
    assert _saved_state_row() == saved
    assert _quarantined() == {}

    def corrupt():
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(genesis, "load_global_ui_state", corrupt)
    with pytest.raises(main.StartupRefused, match="file is not a database"):
        with _boot(api_app):
            pass
    assert os.path.exists(data) and os.path.exists(ghost)
    # The refused start released the data dir: the next one may take it.
    fd = main.lock_data_dir()
    main.unlock_data_dir(fd)


def test_a_refused_start_leaves_no_reap_or_lease_sweep_running__F0001(api_app, monkeypatch):
    """The container reaps and the account lease sweep run in a worker
    thread; a start refused at the restore must not have scheduled them,
    or they would run on after its lifespan released the server lock."""
    import sys
    import accounts
    import genesis
    import main
    import sandbox
    ran = []
    monkeypatch.setitem(sys.modules, "docker", None)   # the docker reap: ImportError, skipped
    monkeypatch.setattr(sandbox, "reap_orphan_containers", lambda: ran.append("sandbox reap"))
    monkeypatch.setattr(accounts, "sweep_expired_account_leases", lambda: ran.append("lease sweep"))

    def unreadable():
        raise sqlite3.DatabaseError("file is not a database")

    real = genesis.load_global_ui_state
    monkeypatch.setattr(genesis, "load_global_ui_state", unreadable)
    with pytest.raises(main.StartupRefused):
        with _boot(api_app):
            pass
    time.sleep(0.3)
    assert ran == []
    # A start that goes through does schedule them.
    monkeypatch.setattr(genesis, "load_global_ui_state", real)
    with _boot(api_app):
        deadline = time.monotonic() + 5
        while len(ran) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert ran == ["sandbox reap", "lease sweep"]


def test_a_repeated_start_that_keeps_the_saved_state_aside_starts_empty__F0001_F0053(api_app):
    """In one process (tests, an embedded server) a later lifespan whose
    saved state does not validate must not keep the previous cycle's
    channels in state, which its first persist would write back."""
    with _boot(api_app) as c:
        assert c.post("/api/channels", params={"name": "First"}).status_code == 200
    deadline = time.monotonic() + 5
    while "First" not in (_saved_state_row() or "") and time.monotonic() < deadline:
        time.sleep(0.02)
    raw = '{"channels": "not a list"}'
    _save_raw_state(raw)
    with _boot(api_app):
        from state import state
        assert state.channels == [] and state.active_channel_id is None
    assert list(_quarantined().values()) == [raw]


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
    """The real genesis.load_global_ui_state, not a stand-in: its failing
    read must reach the startup restore as an error, never as "nothing
    saved" (which would boot empty, park every channel database and then
    persist the empty state over the saved one)."""
    import database
    import main
    data, ghost = _with_data("work"), _ghost("empty_work")
    from models import AppState, Channel
    saved = AppState(channels=[Channel(name="Work")]).model_dump_json()
    _save_raw_state(saved)
    real_connection = database.get_global_db_connection
    monkeypatch.setattr(database, "get_global_db_connection",
                        lambda: _AppStateSelectFails(real_connection()))
    monkeypatch.setattr(main, "_APP_DB_RETRY_SECONDS", 0)
    with pytest.raises(main.StartupRefused, match=r"app\.db: database is locked"):
        with _boot(api_app):
            pass
    monkeypatch.setattr(database, "get_global_db_connection", real_connection)
    assert os.path.exists(data) and os.path.exists(ghost)
    assert not glob.glob(".collimate/channels/orphaned/*")
    assert _saved_state_row() == saved
    assert _quarantined() == {}


def test_load_global_ui_state_answers_none_only_when_nothing_is_saved__F0001(fresh_cwd, monkeypatch):
    import database
    import genesis
    assert genesis.load_global_ui_state() is None
    _save_raw_state('{"channels": []}')
    assert genesis.load_global_ui_state() == '{"channels": []}'
    # A NULL value is a row that is there and does not parse, not "nothing".
    conn = database.get_global_db_connection()
    conn.execute("UPDATE ui_state SET value=NULL WHERE key='app_state'")
    conn.commit()
    conn.close()
    assert genesis.load_global_ui_state() == ""
    real_connection = database.get_global_db_connection
    monkeypatch.setattr(database, "get_global_db_connection",
                        lambda: _AppStateSelectFails(real_connection()))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        genesis.load_global_ui_state()


@pytest.mark.parametrize("raw", ["{}", '{"channel": []}', "[]", "null", ""])
def test_startup_keeps_a_row_without_a_channels_list_aside__F0001_F0053(api_app, raw):
    """A row that would validate only by defaulting ``channels`` to empty
    (``{}``, a renamed key) is not "no channels": read that way, the
    reconcile would park every channel database. It is kept aside like any
    row that does not validate."""
    kept = _with_data("work")
    _save_raw_state(raw)
    with _boot(api_app):
        from state import state
        assert state.channels == []
        assert list(_quarantined().values()) == [raw]
    assert os.path.exists(kept)
    assert not glob.glob(".collimate/channels/orphaned/*")


def test_new_channel_label_cannot_consume_a_quarantined_database__F0001_F0053(api_app):
    """New UUID identities preserve quarantined files even when labels match."""
    kept = _with_data("work")
    _save_raw_state('{"channels": "not a list"}')
    with _boot(api_app) as c:
        r = c.post("/api/channels", params={"name": "Work"})
        assert r.status_code == 200, r.text
        assert r.json()['id'] != 'work'
        assert _quarantined()
        assert os.path.exists(kept)
        assert not glob.glob(".collimate/channels/orphaned/*")
        assert c.post("/api/channels", params={"name": "Other"}).status_code == 200


def _referencing(slug: str, *hashes: str) -> str:
    """A cell in ``slug``'s database whose files are the CAS blobs ``hashes``."""
    path = os.path.join('.collimate', 'channels', f'{slug}.db')
    if not os.path.exists(path):
        _ghost(slug)
    conn = sqlite3.connect(path)
    cell_id = f"cell-{len(hashes)}-{hashes[0][:8]}"
    conn.execute("INSERT INTO cells (id, source_type) VALUES (?, 'user')", (cell_id,))
    for i, h in enumerate(hashes):
        conn.execute("INSERT OR IGNORE INTO blobs (hash, mime_type, size) VALUES (?, 'text/plain', 0)", (h,))
        conn.execute(
            "INSERT INTO cell_files (cell_id, filepath, blob_hash,encoding,language) VALUES (?, ?, ?,'utf-8','plaintext')",
            (cell_id, f"f{i}.txt", h))
    conn.commit()
    conn.close()
    return os.path.join(".collimate", "channels", f"{slug}.db")


def test_deleting_a_channel_keeps_the_blobs_a_kept_database_shares__F0001_F0053(api_app):
    """Booted empty with a saved state kept aside, the kept channel
    databases are in no state.channels. Deleting a channel created since
    must still not unlink a blob one of them references: the CAS dedups by
    content, so a seeded channel shares every seeded file with them, and the
    repaired state would reopen those cells with their bytes gone. Nor one a
    parked channels/orphaned database references. A blob only the deleted
    channel references is left to the guarded collector."""
    import database
    from cas import blob_path, hash_and_store_bytes
    shared = hash_and_store_bytes(b"a seeded file every channel carries")
    parked = hash_and_store_bytes(b"a file a parked database holds too")
    own = hash_and_store_bytes(b"a file only the deleted channel holds")
    kept = _referencing("work", shared)
    _referencing("recoverable", parked)
    database.set_aside_orphan_channel_db("recoverable")
    assert len(glob.glob(".collimate/channels/orphaned/recoverable-*.db")) == 1
    _save_raw_state('{"channels": "not a list"}')

    with _boot(api_app) as c:
        from state import state
        assert state.channels == [] and _quarantined()
        r = c.post("/api/channels", params={"name": "Other"})
        assert r.status_code == 200, r.text
        channel_id = r.json()['id']
        _referencing(channel_id, shared, parked, own)
        r = c.delete(f"/api/channels/{r.json()['id']}")
        assert r.status_code == 200, r.text
        assert not os.path.exists(os.path.join(".collimate", "channels", f"{channel_id}.db"))

    assert os.path.exists(blob_path(shared)), "the kept database's file lost its bytes"
    assert os.path.exists(blob_path(parked)), "the parked database's file lost its bytes"
    assert os.path.exists(blob_path(own)), "deletion leaves fresh blobs to guarded GC"
    assert os.path.exists(kept)


def test_deleting_a_channel_beside_an_unreadable_database_unlinks_no_blob__F0001_F0053(client):
    """A channel database that cannot be read may reference any blob: the
    delete leaves them all to the GC (which trashes before it deletes)
    rather than guess."""
    from cas import blob_path, hash_and_store_bytes
    own = hash_and_store_bytes(b"a file only the deleted channel holds")
    r = client.post("/api/channels", params={"name": "Doomed"})
    assert r.status_code == 200, r.text
    _referencing(r.json()['id'], own)
    with open(os.path.join(".collimate", "channels", "unreadable.db"), "wb") as fh:
        fh.write(b"\x00\xff not a sqlite database")
    assert client.delete(f"/api/channels/{r.json()['id']}").status_code == 200
    assert os.path.exists(blob_path(own))
