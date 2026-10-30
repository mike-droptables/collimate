"""Persistence ordering, bounded work and durable recovery regressions."""
from __future__ import annotations

import json
import threading
import pytest


@pytest.fixture(autouse=True)
def empty_layout(fresh_cwd, monkeypatch):
    from models import AppState
    import state
    monkeypatch.setattr(state, 'state', AppState())


def _initialize(layout):
    from database import get_db_connection
    from persistence import initialize_files
    for channel in layout.channels:
        with get_db_connection(str(channel.id)) as conn:
            for stack in [*channel.main_stacks, *channel.nav_stacks]:
                for cell in stack.cells:
                    initialize_files(conn, str(cell.id), cell.files)


@pytest.fixture
def client(api_app, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import genesis
    empty = tmp_path / 'empty-definitions'
    empty.mkdir()
    monkeypatch.setattr(genesis, 'RENDERERS_DIR', str(empty))
    monkeypatch.setattr(genesis, 'GENERATORS_DIR', str(empty))
    with TestClient(api_app) as http:
        yield http


@pytest.mark.parametrize("operation", ["read", "write"])
def test_runtime_refuses_inline_layout_shape__F0009(fresh_cwd, operation):
    import genesis
    value = {"channels": [], "active_channel_id": None}
    with pytest.raises(ValueError):
        if operation == "read":
            genesis.parse_saved_app_state(json.dumps(value))
        else:
            genesis.save_global_ui_state(value)


def test_inline_layout_is_migrated_once_and_runtime_is_strict__F0009(fresh_cwd):
    import sqlite3
    import genesis
    import migrations
    from models import AppState, Cell, Channel, Stack, VirtualFile
    old = AppState(channels=[Channel(name="saved", main_stacks=[Stack(cells=[Cell(files=[
        VirtualFile(name="note.txt", content="preserve these bytes", language="custom")
    ])])])]).model_dump(mode="json")
    conn = sqlite3.connect(":memory:")
    migrations.apply(conn, migrations.GLOBAL_MIGRATIONS[:2])
    conn.execute("INSERT INTO ui_state(key,value) VALUES('app_state',?)", (json.dumps(old),))
    conn.commit()
    assert migrations.apply(conn, migrations.GLOBAL_MIGRATIONS) == [v for v, _, _ in migrations.GLOBAL_MIGRATIONS if v > 2]
    raw = conn.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()[0]
    assert "preserve these bytes" not in raw
    restored = genesis.parse_saved_app_state(raw)
    genesis.reconcile_restored_cell_files(restored)
    file = restored.channels[0].main_stacks[0].cells[0].files[0]
    assert (file.content, file.language) == ("preserve these bytes", "custom")
    assert migrations.upgrade_global_db(conn) == []
    with pytest.raises(ValueError):
        genesis.parse_saved_app_state(json.dumps(old))
    with pytest.raises(ValueError):
        genesis.save_global_ui_state(old)


def test_layout_migration_preserves_invalid_rows_and_rolls_back_cas_failure__F0009(fresh_cwd, monkeypatch):
    import sqlite3
    import cas
    import migrations
    from models import AppState, Cell, Channel, Stack, VirtualFile
    conn = sqlite3.connect(":memory:")
    migrations.apply(conn, migrations.GLOBAL_MIGRATIONS[:2])
    invalid = '{"channels": "invalid"}'
    conn.execute("INSERT INTO ui_state(key,value) VALUES('app_state',?)", (invalid,))
    conn.commit()
    assert migrations.apply(conn, migrations.GLOBAL_MIGRATIONS) == [v for v, _, _ in migrations.GLOBAL_MIGRATIONS if v > 2]
    assert conn.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()[0] == invalid
    conn.execute("DELETE FROM schema_migrations WHERE version=3")
    raw = AppState(channels=[Channel(name="old", main_stacks=[Stack(cells=[Cell(files=[
        VirtualFile(name="note", content="must survive")
    ])])])]).model_dump_json()
    conn.execute("UPDATE ui_state SET value=? WHERE key='app_state'", (raw,))
    conn.commit()
    def fail(_body):
        raise OSError("synthetic CAS failure")
    monkeypatch.setattr(cas, "store_bytes", fail)
    with pytest.raises(OSError, match="synthetic CAS"):
        migrations.upgrade_global_db(conn)
    assert conn.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()[0] == raw
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone() is None


def test_snapshot_save_order_cannot_restore_older_state__F0008_F0077(fresh_cwd, monkeypatch):
    import genesis
    import state
    from models import AppState, Channel

    channel = Channel(name="older")
    monkeypatch.setattr(state, "state", AppState(channels=[channel]))
    _initialize(state.state)
    old_in_save = threading.Event()
    release_old = threading.Event()
    new_in_save = threading.Event()
    new_saved = threading.Event()
    original = genesis.save_global_ui_state
    original(genesis.snapshot_app_state(state.state))

    def controlled_save(snapshot):
        if snapshot["channels"][0]["name"] == "older":
            old_in_save.set()
            assert release_old.wait(5)
        else:
            new_in_save.set()
        original(snapshot)
        if snapshot["channels"][0]["name"] == "newer":
            new_saved.set()

    monkeypatch.setattr(genesis, "save_global_ui_state", controlled_save)
    old = threading.Thread(target=state._persist_state_sync)
    newer = threading.Thread(target=state._persist_state_sync)
    old.start()
    assert old_in_save.wait(5)
    channel.name = "newer"
    newer.start()
    # Without ordering the new snapshot overtakes the old, then the old
    # snapshot overwrites it. With ordering, the new writer waits here.
    overtook = new_saved.wait(0.1)
    release_old.set()
    old.join(5)
    newer.join(5)
    assert not old.is_alive() and not newer.is_alive()
    saved = json.loads(genesis.load_global_ui_state())
    assert saved["channels"][0]["name"] == "newer"
    assert not overtook


def test_trailing_writer_coalesces_and_flush_is_durable__F0009_F0218(fresh_cwd, monkeypatch):
    import genesis
    import state
    from models import AppState, Channel

    channel = Channel(name="initial")
    monkeypatch.setattr(state, "state", AppState(channels=[channel]))
    _initialize(state.state)
    writes = []
    original = genesis.save_global_ui_state

    def save(snapshot):
        writes.append(snapshot)
        original(snapshot)

    monkeypatch.setattr(genesis, "save_global_ui_state", save)
    state.start_persistence_writer(delay=60)
    try:
        for index in range(100):
            channel.name = f"latest-{index}"
            state.persist_state()
        assert writes == []
        state.flush_persist_state()
        assert len(writes) == 1
        assert json.loads(genesis.load_global_ui_state())["channels"][0]["name"] == "latest-99"
    finally:
        state.stop_persistence_writer()


def test_writer_mutation_during_save_is_not_lost_at_stop__F0075_F0217(fresh_cwd, monkeypatch):
    import genesis
    import state
    from models import AppState, Channel

    channel = Channel(name="first")
    monkeypatch.setattr(state, "state", AppState(channels=[channel]))
    _initialize(state.state)
    original = genesis.save_global_ui_state
    entered, release = threading.Event(), threading.Event()

    def save(snapshot):
        if snapshot["channels"][0]["name"] == "first":
            entered.set()
            assert release.wait(5)
        original(snapshot)

    monkeypatch.setattr(genesis, "save_global_ui_state", save)
    state.start_persistence_writer(delay=0)
    state.persist_state()
    try:
        assert entered.wait(5)
        channel.name = "second"
        state.persist_state()
    finally:
        release.set()
        state.stop_persistence_writer()
    assert json.loads(genesis.load_global_ui_state())["channels"][0]["name"] == "second"


def test_explicit_flush_reports_database_failure_and_can_retry__P0F19(fresh_cwd, monkeypatch):
    import genesis
    import state
    original = genesis.save_global_ui_state
    state.start_persistence_writer(delay=60)
    state.persist_state()
    try:
        def refuse(_snapshot):
            raise OSError("disk full")
        monkeypatch.setattr(genesis, "save_global_ui_state", refuse)
        with pytest.raises(OSError, match="disk full"):
            state.flush_persist_state()
        monkeypatch.setattr(genesis, "save_global_ui_state", original)
        state.flush_persist_state()
        assert genesis.load_global_ui_state() is not None
    finally:
        monkeypatch.setattr(genesis, "save_global_ui_state", original)
        state.stop_persistence_writer()


def test_startup_rehydrates_accepted_current_despite_stale_layout_and_history__F0218(fresh_cwd, monkeypatch):
    import genesis
    import main
    import state
    import uuid
    from models import AppState, Cell, Channel, Stack, VirtualFile
    from persistence import mutate_files
    from cell_files import checkpoint_cells

    cell = Cell(files=[VirtualFile(name='a.txt', content='historical'),
                       VirtualFile(name='deleted.txt', content='historical')])
    channel = Channel(name='recover', main_stacks=[Stack(cells=[cell])])
    layout = AppState(channels=[channel], active_channel_id=channel.id)
    monkeypatch.setattr(state, 'state', layout)
    monkeypatch.setattr(main, 'state', layout)
    _initialize(layout)
    genesis.save_global_ui_state(genesis.snapshot_app_state(layout))
    checkpoint_cells(str(channel.id), [str(cell.id)], 'baseline')
    receipt = mutate_files(str(channel.id), str(cell.id), dict(operation_id=str(uuid.uuid4()), mutations=[
        dict(path='a.txt', kind='put', content_text='accepted current', base_revision=1,
             writer_id='startup', edit_seq=1),
        dict(path='new.txt', kind='put', content_text='new path', base_revision=0,
             writer_id='startup', edit_seq=1),
        dict(path='deleted.txt', kind='delete', base_revision=1, writer_id='startup', edit_seq=1),
    ]), actor='host')
    assert receipt['outcome'] == 'accepted'
    # Derived memory cannot become an authority when layout is saved/reloaded.
    cell.files = [VirtualFile(name='wrong.txt', content='wrong memory')]
    main.restore_app_state()
    restored = state.state.channels[0].main_stacks[0].cells[0]
    assert restored.id == cell.id
    assert {f.name: f.content for f in restored.files} == {'a.txt': 'accepted current', 'new.txt': 'new path'}
    assert {f.name: f.file_revision for f in restored.files} == {
        f['path']: f['file_revision'] for f in receipt['files'] if not f['deleted']}


def test_layout_row_has_no_file_text_and_rehydrates_unversioned_files__F0009(fresh_cwd, monkeypatch):
    import genesis
    import state
    from cas import store_bytes
    from models import AppState, Cell, Channel, Stack, VirtualFile

    binary_hash = store_bytes(b"explicit binary even though UTF8")
    original = [VirtualFile(name="custom.x", content="unique unversioned user text", language="custom"),
                VirtualFile(name="empty.txt", content=""),
                VirtualFile(name="binary.txt", blob_hash=binary_hash, language="binary")]
    cell = Cell(files=original)
    monkeypatch.setattr(state, "state", AppState(channels=[Channel(name="layout", main_stacks=[Stack(cells=[cell])])]))
    _initialize(state.state)
    state.persist_state()
    raw = genesis.load_global_ui_state()
    assert "unique unversioned user text" not in raw
    assert "explicit binary even though UTF8" not in raw
    saved = json.loads(raw)['channels'][0]['main_stacks'][0]['cells'][0]
    assert 'files' not in saved
    restored_layout = genesis.parse_saved_app_state(raw)
    genesis.reconcile_restored_cell_files(restored_layout)
    restored = restored_layout.channels[0].main_stacks[0].cells[0]
    assert restored.id == cell.id
    assert [(f.name, f.content, f.blob_hash, f.language) for f in restored.files] == sorted(
        (f.name, f.content, f.blob_hash, f.language) for f in original)


def test_repeated_layout_saves_do_not_hash_or_publish_derived_file_bodies__F0009_F0142(fresh_cwd, monkeypatch):
    import hashlib
    import state
    import genesis
    from persistence import read_file
    from models import AppState, Cell, Channel, Stack, VirtualFile

    original = 'unchanged' * 10000
    file = VirtualFile(name='large.txt', content=original)
    cell = Cell(files=[file])
    channel = Channel(name='cache', main_stacks=[Stack(cells=[cell])])
    layout = AppState(channels=[channel])
    monkeypatch.setattr(state, 'state', layout)
    _initialize(layout)
    state.persist_state()
    original_hash = hashlib.sha256
    def metadata_hash(data=b'', **kwargs):
        assert original.encode() not in data, 'layout save hashed file body'
        assert b'derived cache changed' not in data, 'layout save hashed derived file cache'
        return original_hash(data, **kwargs)
    monkeypatch.setattr(hashlib, 'sha256', metadata_hash)
    state.persist_state()
    file.content = 'derived cache changed'
    state.persist_state()
    saved = json.loads(genesis.load_global_ui_state())['channels'][0]['main_stacks'][0]['cells'][0]
    assert 'files' not in saved
    monkeypatch.setattr(hashlib, 'sha256', original_hash)
    assert read_file(str(channel.id), str(cell.id), 'large.txt')[1] == original.encode()


def test_channel_create_response_follows_actual_layout_commit__P0F19(client):
    import genesis
    response = client.post("/api/channels", params={"name": "durable new"})
    assert response.status_code == 200
    saved = genesis.parse_saved_app_state(genesis.load_global_ui_state())
    assert response.json()["id"] in {str(c.id) for c in saved.channels}


def test_missing_cas_layout_content_refuses_start_without_quarantine__F0009(fresh_cwd, monkeypatch):
    import os
    import cas
    import genesis
    import main
    import state
    from models import AppState, Channel, Stack, Cell, VirtualFile
    app = AppState(channels=[Channel(name="missing", main_stacks=[Stack(cells=[Cell(files=[VirtualFile(name="a.txt", content="keep me")])])])])
    monkeypatch.setattr(state, "state", app)
    monkeypatch.setattr(main, "state", app)
    _initialize(app)
    state.persist_state()
    raw = genesis.load_global_ui_state()
    from persistence import snapshot_files
    from database import get_db_connection
    channel = app.channels[0]
    with get_db_connection(str(channel.id)) as conn:
        stored = snapshot_files(conn, str(channel.main_stacks[0].cells[0].id))[0]
    assert 'files' not in json.loads(raw)['channels'][0]['main_stacks'][0]['cells'][0]
    os.unlink(cas.blob_path(stored["body_hash"]))
    with pytest.raises(main.StartupRefused, match="Missing accepted content a.txt"):
        main.restore_app_state()
    assert genesis.load_global_ui_state() == raw
    assert genesis.quarantined_app_states() == []
