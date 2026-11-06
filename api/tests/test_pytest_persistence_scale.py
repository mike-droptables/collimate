"""Persistence ordering, bounded work and durable recovery regressions."""
from __future__ import annotations

import json
import threading
import pytest


@pytest.fixture(autouse=True)
def empty_layout(fresh_cwd, monkeypatch):
    from collimate_server.models import AppState
    import collimate_server.state as state
    monkeypatch.setattr(state, 'state', AppState())


def _initialize(layout):
    from collimate_server.database import get_db_connection
    from collimate_server.persistence import initialize_files
    for channel in layout.channels:
        with get_db_connection(str(channel.id)) as conn:
            for stack in [*channel.main_stacks, *channel.nav_stacks]:
                for cell in stack.cells:
                    initialize_files(conn, str(cell.id), cell.files)


@pytest.fixture
def client(api_app, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import collimate_server.genesis as genesis
    empty = tmp_path / 'empty-definitions'
    empty.mkdir()
    monkeypatch.setattr(genesis, 'RENDERERS_DIR', str(empty))
    monkeypatch.setattr(genesis, 'GENERATORS_DIR', str(empty))
    with TestClient(api_app) as http:
        yield http


def _layout_text(channel_id) -> str:
    """The raw ``layout`` row of the channel's own channel.db."""
    from collimate_server.database import get_db_connection
    with get_db_connection(str(channel_id)) as conn:
        return conn.execute("SELECT value FROM ui_state WHERE key='layout'").fetchone()[0]


def _saved_layout(channel_id) -> dict:
    return json.loads(_layout_text(channel_id))


def test_runtime_refuses_inline_layout_shape__F0009(fresh_cwd):
    """Only a channel-layout-v1 row is a layout: an inline app-state shape in
    a channel.db is refused (the channel is then unavailable at start)."""
    import uuid
    import collimate_server.genesis as genesis
    from collimate_server.cell_metadata import read_layout
    from collimate_server.database import get_db_connection
    channel_id = str(uuid.uuid4())
    with get_db_connection(channel_id) as conn:
        assert read_layout(conn) is None
        with pytest.raises(ValueError):
            genesis.load_channel(channel_id)
        value = {"channels": [], "active_channel_id": None}
        conn.execute("INSERT INTO ui_state(key,value) VALUES('layout',?)", (json.dumps(value),))
        conn.commit()
        with pytest.raises(ValueError):
            read_layout(conn)
    with pytest.raises(ValueError):
        genesis.load_channel(channel_id)


def test_snapshot_save_order_cannot_restore_older_state__F0008_F0077(fresh_cwd, monkeypatch):
    import collimate_server.genesis as genesis
    import collimate_server.state as state
    from collimate_server.models import AppState, Channel, Stack

    # The layout carries the stack name (the channel's own name lives in channel.json).
    channel = Channel(name="saved", main_stacks=[Stack(name="older")])
    monkeypatch.setattr(state, "state", AppState(channels=[channel]))
    _initialize(state.state)
    old_in_save = threading.Event()
    release_old = threading.Event()
    new_in_save = threading.Event()
    new_saved = threading.Event()
    original = genesis.save_app_state
    original(genesis.snapshot_app_state(state.state))

    def controlled_save(snapshot):
        if snapshot["channels"][0]["main_stacks"][0]["name"] == "older":
            old_in_save.set()
            assert release_old.wait(5)
        else:
            new_in_save.set()
        original(snapshot)
        if snapshot["channels"][0]["main_stacks"][0]["name"] == "newer":
            new_saved.set()

    monkeypatch.setattr(genesis, "save_app_state", controlled_save)
    old = threading.Thread(target=state._persist_state_sync)
    newer = threading.Thread(target=state._persist_state_sync)
    old.start()
    assert old_in_save.wait(5)
    channel.main_stacks[0].name = "newer"
    newer.start()
    # Without ordering the new snapshot overtakes the old, then the old
    # snapshot overwrites it. With ordering, the new writer waits here.
    overtook = new_saved.wait(0.1)
    release_old.set()
    old.join(5)
    newer.join(5)
    assert not old.is_alive() and not newer.is_alive()
    assert _saved_layout(channel.id)["main_stacks"][0]["name"] == "newer"
    assert not overtook


def test_trailing_writer_coalesces_and_flush_is_durable__F0009_F0218(fresh_cwd, monkeypatch):
    import collimate_server.genesis as genesis
    import collimate_server.state as state
    from collimate_server.models import AppState, Channel, Stack

    channel = Channel(name="initial", main_stacks=[Stack(name="initial")])
    monkeypatch.setattr(state, "state", AppState(channels=[channel]))
    _initialize(state.state)
    writes = []
    original = genesis.save_app_state

    def save(snapshot):
        writes.append(snapshot)
        original(snapshot)

    monkeypatch.setattr(genesis, "save_app_state", save)
    state.start_persistence_writer(delay=60)
    try:
        for index in range(100):
            channel.main_stacks[0].name = f"latest-{index}"
            state.persist_state()
        assert writes == []
        state.flush_persist_state()
        assert len(writes) == 1
        assert _saved_layout(channel.id)["main_stacks"][0]["name"] == "latest-99"
    finally:
        state.stop_persistence_writer()


def test_writer_mutation_during_save_is_not_lost_at_stop__F0075_F0217(fresh_cwd, monkeypatch):
    import collimate_server.genesis as genesis
    import collimate_server.state as state
    from collimate_server.models import AppState, Channel, Stack

    channel = Channel(name="saved", main_stacks=[Stack(name="first")])
    monkeypatch.setattr(state, "state", AppState(channels=[channel]))
    _initialize(state.state)
    original = genesis.save_app_state
    entered, release = threading.Event(), threading.Event()

    def save(snapshot):
        if snapshot["channels"][0]["main_stacks"][0]["name"] == "first":
            entered.set()
            assert release.wait(5)
        original(snapshot)

    monkeypatch.setattr(genesis, "save_app_state", save)
    state.start_persistence_writer(delay=0)
    state.persist_state()
    try:
        assert entered.wait(5)
        channel.main_stacks[0].name = "second"
        state.persist_state()
    finally:
        release.set()
        state.stop_persistence_writer()
    assert _saved_layout(channel.id)["main_stacks"][0]["name"] == "second"


def test_explicit_flush_reports_database_failure_and_can_retry__P0F19(fresh_cwd, monkeypatch):
    import collimate_server.genesis as genesis
    import collimate_server.state as state
    from collimate_server.models import AppState, Channel, Stack
    channel = Channel(name="flush", main_stacks=[Stack(name="before")])
    monkeypatch.setattr(state, "state", AppState(channels=[channel]))
    _initialize(state.state)
    original = genesis.save_app_state
    state.start_persistence_writer(delay=60)
    channel.main_stacks[0].name = "after"
    state.persist_state()
    try:
        def refuse(_snapshot):
            raise OSError("disk full")
        monkeypatch.setattr(genesis, "save_app_state", refuse)
        with pytest.raises(OSError, match="disk full"):
            state.flush_persist_state()
        monkeypatch.setattr(genesis, "save_app_state", original)
        state.flush_persist_state()
        assert _saved_layout(channel.id)["main_stacks"][0]["name"] == "after"
    finally:
        monkeypatch.setattr(genesis, "save_app_state", original)
        state.stop_persistence_writer()


def test_startup_rehydrates_accepted_current_despite_stale_layout_and_history__F0218(fresh_cwd, monkeypatch):
    import collimate_server.genesis as genesis
    import collimate_server.main as main
    import collimate_server.state as state
    import uuid
    from collimate_server.models import AppState, Cell, Channel, Stack, VirtualFile
    from collimate_server.persistence import mutate_files
    from collimate_server.cell_files import checkpoint_cells

    cell = Cell(files=[VirtualFile(name='a.txt', content='historical'),
                       VirtualFile(name='deleted.txt', content='historical')])
    channel = Channel(name='recover', main_stacks=[Stack(cells=[cell])])
    layout = AppState(channels=[channel], active_channel_id=channel.id)
    monkeypatch.setattr(state, 'state', layout)
    monkeypatch.setattr(main, 'state', layout)
    _initialize(layout)
    genesis.save_app_state(genesis.snapshot_app_state(layout))
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
    import collimate_server.genesis as genesis
    import collimate_server.state as state
    from collimate_server.cas import channel_store
    from collimate_server.models import AppState, Cell, Channel, Stack, VirtualFile

    channel = Channel(name="layout")
    binary_hash = channel_store(str(channel.id)).store_bytes(b"explicit binary even though UTF8")
    original = [VirtualFile(name="custom.x", content="unique unversioned user text", language="custom"),
                VirtualFile(name="empty.txt", content=""),
                VirtualFile(name="binary.txt", blob_hash=binary_hash, language="binary")]
    cell = Cell(files=original)
    channel.main_stacks = [Stack(cells=[cell])]
    monkeypatch.setattr(state, "state", AppState(channels=[channel]))
    _initialize(state.state)
    state.persist_state()
    raw = _layout_text(channel.id)
    assert "unique unversioned user text" not in raw
    assert "explicit binary even though UTF8" not in raw
    saved = json.loads(raw)['main_stacks'][0]['cells'][0]
    assert 'files' not in saved
    restored_channel = genesis.load_channel(str(channel.id))
    genesis.reconcile_restored_cell_files(restored_channel)
    restored = restored_channel.main_stacks[0].cells[0]
    assert restored.id == cell.id
    assert [(f.name, f.content, f.blob_hash, f.language) for f in restored.files] == sorted(
        (f.name, f.content, f.blob_hash, f.language) for f in original)


def test_repeated_layout_saves_do_not_hash_or_publish_derived_file_bodies__F0009_F0142(fresh_cwd, monkeypatch):
    import hashlib
    import collimate_server.state as state
    from collimate_server.persistence import read_file
    from collimate_server.models import AppState, Cell, Channel, Stack, VirtualFile

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
    saved = _saved_layout(channel.id)['main_stacks'][0]['cells'][0]
    assert 'files' not in saved
    monkeypatch.setattr(hashlib, 'sha256', original_hash)
    assert read_file(str(channel.id), str(cell.id), 'large.txt')[1] == original.encode()


def test_channel_create_response_follows_actual_layout_commit__P0F19(client):
    import collimate_server.genesis as genesis
    response = client.post("/api/channels", params={"name": "durable new"})
    assert response.status_code == 200
    # The response follows the commit: the new channel's folder already holds its layout.
    assert str(genesis.load_channel(response.json()["id"]).id) == response.json()["id"]


def test_missing_cas_content_leaves_only_that_channel_unavailable__F0009(fresh_cwd, monkeypatch):
    import os
    import collimate_server.cas as cas
    import collimate_server.channel_paths as channel_paths
    import collimate_server.main as main
    import collimate_server.state as state
    from collimate_server.models import AppState, Channel, Stack, Cell, VirtualFile
    app = AppState(channels=[Channel(name="missing", main_stacks=[Stack(cells=[Cell(files=[VirtualFile(name="a.txt", content="keep me")])])])])
    monkeypatch.setattr(state, "state", app)
    monkeypatch.setattr(main, "state", app)
    _initialize(app)
    state.persist_state()
    channel = app.channels[0]
    raw = _layout_text(channel.id)
    from collimate_server.persistence import snapshot_files
    from collimate_server.database import get_db_connection
    with get_db_connection(str(channel.id)) as conn:
        stored = snapshot_files(conn, str(channel.main_stacks[0].cells[0].id))[0]
    assert 'files' not in json.loads(raw)['main_stacks'][0]['cells'][0]
    os.unlink(cas.channel_store(str(channel.id)).blob_path(stored["body_hash"]))
    main.restore_app_state()
    # The start goes on: the channel is listed unavailable with the reason, its data untouched.
    assert state.state.channels == []
    [found] = channel_paths.unavailable_channels()
    assert found.channel_id == str(channel.id) and "Missing accepted content a.txt" in found.reason
    import sqlite3
    from pathlib import Path
    path = Path(os.environ["COLLIMATE_CHANNELS"]) / str(channel.id) / "channel.db"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM ui_state WHERE key='layout'").fetchone()[0] == raw
