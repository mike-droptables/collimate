"""Coverage for cas_gc: mark-and-sweep correctness, one body store at a time.

Two correctness invariants we MUST maintain:

  * **Never delete a referenced blob.** A blob in cell_files of a
    non-deleted cell must survive the collection of its channel.
  * **Trash before delete.** Even orphans go to the store's ``.trash``
    first; the actual unlink only happens after the grace period. A bug in
    the mark phase shouldn't lose data permanently.

Each channel's store (``<channel folder>/objects``) is collected on its own
(``collect_channel``); the app store (``.collimate/app-objects``) marks with
app.db's bundle / handler build / module mirror tables. An unreadable source
aborts only that store's sweep.

Gone with the global store (and so not tested any more): the parked orphan
channel DBs (``channels/orphaned``) that kept a blob alive, and the union of
every channel's live set over one shared store.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest


def _channel_store(channel_id):
    from collimate_server.cas import channel_store
    return channel_store(channel_id)


def _tmp_name(a, b):
    return a * 62 + '.' + b * 32 + '.tmp'


@pytest.fixture(autouse=True)
def empty_layout(monkeypatch):
    import collimate_server.state as state
    from collimate_server.models import AppState
    monkeypatch.setattr(state, "state", AppState())


def _new_channel(name="test_channel") -> str:
    """A channel with its folder, registered in the live state (so run_gc sees it)."""
    from conftest import channel_home
    from collimate_server.models import Channel
    from collimate_server.state import state
    channel = Channel(id=str(uuid.uuid4()), name=name)
    channel_home(channel)
    state.channels.append(channel)
    return str(channel.id)


def _write_blob(store, content: bytes) -> str:
    """Store ``content`` in ``store``, aged past the publication floor (an old
    collection candidate)."""
    h = store.store_bytes(content)
    os.utime(store.blob_path(h), (0, 0))
    return h


def _seed_cell(channel_id: str, cell_id: str, files: dict[str, str]) -> None:
    """Insert a cell + cell_files rows referencing the given hashes."""
    import collimate_server.database as database
    conn = database.get_db_connection(channel_id)
    conn.execute("INSERT OR IGNORE INTO cells (id, generator_name, sequence_num) VALUES (?, ?, ?)",
                 (cell_id, "test", 1))
    for filepath, blob_hash in files.items():
        conn.execute(
            "INSERT OR IGNORE INTO blobs (hash, mime_type, size) VALUES (?, ?, ?)",
            (blob_hash, "text/plain", 0),
        )
        conn.execute(
            "INSERT OR IGNORE INTO cell_files (cell_id, filepath, blob_hash,encoding,language) VALUES (?, ?, ?,'utf-8','text')",
            (cell_id, filepath, blob_hash),
        )
    from collimate_server.versions import initialize_file_snapshot
    initialize_file_snapshot(conn, cell_id)
    conn.commit()
    conn.close()


def _seed_channel_with_cell(name: str, cell_id: str, files: dict[str, str]) -> str:
    channel_id = _new_channel(name)
    _seed_cell(channel_id, cell_id, files)
    return channel_id


def _result_for(result: dict, channel_id: str) -> dict:
    return next(row for row in result["channels"] if row["id"] == channel_id)


def test_abandoned_publisher_temp_cleanup_keeps_recent_foreign_and_symbolic_paths(fresh_cwd):
    from collimate_server.cas_gc import _reclaim_abandoned_writes
    store = _channel_store(_new_channel())
    shard = Path(store.root) / 'ab'
    shard.mkdir(parents=True)
    name = _tmp_name('c', 'd')
    aged = shard / name
    aged.write_bytes(b'unpublished')
    os.utime(aged, (0, 0))
    recent = shard / _tmp_name('e', 'f')
    recent.write_bytes(b'current publisher')
    foreign = shard / 'foreign.tmp'
    foreign.write_bytes(b'unowned')
    os.utime(foreign, (0, 0))
    target = fresh_cwd / 'unrelated'
    target.write_bytes(b'preserve')
    symbolic = shard / _tmp_name('a', 'b')
    symbolic.symlink_to(target)
    elsewhere = fresh_cwd / 'elsewhere'
    elsewhere.mkdir()
    outside_temp = elsewhere / name
    outside_temp.write_bytes(b'outside shard')
    os.utime(outside_temp, (0, 0))
    (shard.parent / 'cd').symlink_to(elsewhere, target_is_directory=True)
    assert _reclaim_abandoned_writes(store) == 1
    assert not aged.exists()
    assert recent.read_bytes() == b'current publisher'
    assert foreign.read_bytes() == b'unowned'
    assert symbolic.is_symlink() and target.read_bytes() == b'preserve'
    assert outside_temp.read_bytes() == b'outside shard'


def test_temp_cleanup_waits_for_live_publication_before_inspecting_candidates(fresh_cwd):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from collimate_server.cas_gc import _reclaim_abandoned_writes
    store = _channel_store(_new_channel())
    shard = Path(store.root) / 'ab'
    shard.mkdir(parents=True)
    body = shard / ('c' * 62)
    temporary = shard / (body.name + '.' + 'd' * 32 + '.tmp')
    temporary.write_bytes(b'publisher owns these bytes')
    os.utime(temporary, (0, 0))
    entered = Event()

    def collect():
        entered.set()
        return _reclaim_abandoned_writes(store)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.publication_lock:
            task = pool.submit(collect)
            assert entered.wait(1)
            with pytest.raises(TimeoutError):
                task.result(timeout=0.05)
            assert temporary.exists()
            temporary.rename(body)
        assert task.result(timeout=5) == 0
    assert body.read_bytes() == b'publisher owns these bytes'


def test_referenced_blob_survives_gc(fresh_cwd):
    """A blob attached to a non-deleted cell must NOT be moved to trash."""
    channel_id = _new_channel()
    h = _write_blob(_channel_store(channel_id), b"live content")
    _seed_cell(channel_id, "cell-live", {"a.txt": h})

    import collimate_server.cas_gc as cas_gc
    result = cas_gc.collect_channel(channel_id)
    assert result["swept"] == 0
    assert os.path.isfile(_channel_store(channel_id).blob_path(h)), "live blob got swept"


def test_orphan_blob_moved_to_trash_not_deleted(fresh_cwd):
    """A blob with no live cell_files reference must move to the store's
    trash, but the actual file must still exist."""
    channel_id = _seed_channel_with_cell("test_channel", "cell-other", {})
    store = _channel_store(channel_id)
    h = _write_blob(store, b"orphan")

    import collimate_server.cas_gc as cas_gc
    result = cas_gc.run_gc()
    assert _result_for(result, channel_id)["swept"] == 1

    assert not os.path.isfile(store.blob_path(h))
    assert os.path.isdir(store.trash)
    found = any(h[2:] in files for _dirpath, _dirs, files in os.walk(store.trash))
    assert found, "orphan blob not found under trash"


def test_a_cell_off_the_canvas_keeps_its_blobs_O02(fresh_cwd):
    """Every version row is live: a cell in no layout (a removed cell, kept
    for the Removed shelf) still holds its blobs."""
    channel_id = _new_channel()
    store = _channel_store(channel_id)
    h = _write_blob(store, b"kept with its history")
    _seed_cell(channel_id, "cell-off-canvas", {"a.txt": h})

    import collimate_server.cas_gc as cas_gc
    result = cas_gc.run_gc()
    assert _result_for(result, channel_id)["swept"] == 0
    assert os.path.isfile(store.blob_path(h))


def test_in_flight_tmp_files_are_left_alone(fresh_cwd):
    """A file ending in .tmp under a store is an in-flight write from
    BodyStore._store_blob: never sweep it."""
    channel_id = _new_channel()
    store = _channel_store(channel_id)
    h = "a" * 64  # synthetic hash
    blob_dir = os.path.join(store.root, h[:2])
    os.makedirs(blob_dir, exist_ok=True)
    tmp_path = os.path.join(blob_dir, h[2:] + ".tmp")
    with open(tmp_path, "wb") as f:
        f.write(b"in-flight")

    import collimate_server.cas_gc as cas_gc
    result = cas_gc.collect_channel(channel_id)
    assert result["swept"] == 0
    assert os.path.isfile(tmp_path)


def test_expired_trash_is_purged(fresh_cwd):
    """A trash sweep older than the grace period is unlinked entirely.
    The grace defaults to 24h; tests pass ``grace_seconds=0`` to make
    every existing trash entry instantly expired."""
    channel_id = _seed_channel_with_cell("test_channel", "cell-other", {})
    store = _channel_store(channel_id)
    _write_blob(store, b"trashed")

    import collimate_server.cas_gc as cas_gc
    result1 = cas_gc.collect_channel(channel_id)
    assert result1["swept"] == 1

    trash_root = store.trash
    assert os.path.isdir(trash_root)
    assert os.listdir(trash_root) != []

    # Backdate the sweep dir's mtime so it's "older than grace".
    for entry in os.listdir(trash_root):
        os.utime(os.path.join(trash_root, entry), (0, 0))

    result2 = cas_gc.collect_channel(channel_id, grace_seconds=0)
    assert result2["purged_sweeps"] >= 1
    remaining = os.listdir(trash_root) if os.path.isdir(trash_root) else []
    assert all(
        os.path.getmtime(os.path.join(trash_root, e)) > 1000
        for e in remaining
    ), "old trash entry survived its grace period"



def test_a_channel_collection_never_touches_another_channels_store(fresh_cwd):
    """Each channel is marked against its own DB and swept in its own store:
    a body unreferenced in channel B is trashed there even though channel A
    stores the same content and references it."""
    shared = b"stored by both channels"
    channel_a = _new_channel("channel_a")
    channel_b = _new_channel("channel_b")
    h_a = _write_blob(_channel_store(channel_a), shared)
    h_b = _write_blob(_channel_store(channel_b), shared)
    assert h_a == h_b
    _seed_cell(channel_a, "cell-a", {"f.txt": h_a})

    import collimate_server.cas_gc as cas_gc
    result = cas_gc.run_gc()
    assert _result_for(result, channel_a)["swept"] == 0
    assert _result_for(result, channel_b)["swept"] == 1
    assert os.path.isfile(_channel_store(channel_a).blob_path(h_a))
    assert not os.path.isfile(_channel_store(channel_b).blob_path(h_b))


def test_channel_live_blobs_keeps_a_user_skins_scene_picture(fresh_cwd):
    """A picture a user skin's scene names (channel settings
    ``theme.userThemes[].scene.image``) is referenced by that row alone, so
    the mark phase reads it there; a DB with no settings row contributes
    nothing for it, and a settings row that is not JSON is an incomplete
    mark, never "references nothing"."""
    import json

    import collimate_server.cas_gc as cas_gc
    import collimate_server.database as database
    from collimate_server.channel_settings import CHANNEL_SETTINGS_KEY
    channel_id = _new_channel("channel_pic")
    store = _channel_store(channel_id)
    picture = _write_blob(store, b"\x89PNG the ground")
    other = _write_blob(store, b"a file")
    _seed_cell(channel_id, "cell-p", {"f.txt": other})
    conn = database.get_db_connection(channel_id)
    doc = {"theme": {"activeThemeId": "user.a", "userThemes": [
        {"id": "user.a", "vars": {}, "scene": {"image": picture}},
        {"id": "user.b", "vars": {}, "scene": {"image": None}},
    ]}}
    conn.execute("INSERT OR REPLACE INTO ui_state (key, value) VALUES (?, ?)", (CHANNEL_SETTINGS_KEY, json.dumps(doc)))
    conn.commit()
    conn.close()
    live = cas_gc.channel_live_blobs(channel_id)
    assert picture in live and other in live
    assert cas_gc.collect_channel(channel_id)["swept"] == 0
    assert os.path.isfile(store.blob_path(picture))

    # A channel DB with no settings row contributes nothing for pictures; a
    # settings row that is not JSON, or names a non-hash, is an incomplete mark.
    conn = database.get_db_connection(channel_id)
    try:
        conn.execute("DELETE FROM ui_state WHERE key = ?", (CHANNEL_SETTINGS_KEY,))
        assert picture not in cas_gc._live_blobs_in_channel(conn)
        conn.execute("INSERT INTO ui_state VALUES (?, ?)", (CHANNEL_SETTINGS_KEY, json.dumps(doc)))
        assert picture in cas_gc._live_blobs_in_channel(conn)
        conn.execute("UPDATE ui_state SET value = ? WHERE key = ?", ("{not json", CHANNEL_SETTINGS_KEY))
        with pytest.raises(cas_gc.IncompleteMark):
            cas_gc._live_blobs_in_channel(conn)
        conn.execute("UPDATE ui_state SET value = ? WHERE key = ?", (json.dumps(
            {"theme": {"userThemes": [{"id": "user.z", "scene": {"image": "nope"}}]}}), CHANNEL_SETTINGS_KEY))
        with pytest.raises(cas_gc.IncompleteMark):
            cas_gc._live_blobs_in_channel(conn)
        conn.rollback()
    finally:
        conn.close()
    assert cas_gc.collect_channel(channel_id)["swept"] == 0
    # An unreadable settings row never reaches the sweep as "references nothing".
    conn = database.get_db_connection(channel_id)
    conn.execute("UPDATE ui_state SET value = ? WHERE key = ?", ("{not json", CHANNEL_SETTINGS_KEY))
    conn.commit()
    conn.close()
    assert cas_gc.collect_channel(channel_id)["aborted"] is True
    assert os.path.isfile(store.blob_path(picture))


def test_gc_refuses_incomplete_mark_from_corrupt_channel_db__P0F14(fresh_cwd):
    """An unreadable channel database prevents reclamation in that channel's
    store only; a healthy channel is still collected."""
    import collimate_server.channel_paths as channel_paths
    import collimate_server.cas_gc as cas_gc
    good = _seed_channel_with_cell("good", "cell-good", {})
    good_orphan = _write_blob(_channel_store(good), b"healthy channel orphan")
    bad = _new_channel("bad")
    bad_store = _channel_store(bad)
    h = _write_blob(bad_store, b"unreferenced but the broken database may own it")

    with open(channel_paths.db_path(bad), "wb") as f:
        f.write(b"\x00\xff\x00\xff garbage not a sqlite file ")

    result = cas_gc.run_gc(grace_seconds=0)
    assert _result_for(result, bad)["aborted"] is True
    assert os.path.exists(bad_store.blob_path(h))
    assert not _result_for(result, good).get("aborted")
    assert _result_for(result, good)["swept"] == 1
    assert not os.path.exists(_channel_store(good).blob_path(good_orphan))


def test_an_unreadable_db_is_never_read_as_referencing_nothing__F0001_F0053(fresh_cwd, monkeypatch):
    """Collecting a channel trashes every body its DB does not name, so
    "cannot read this DB" must never come back as "it references nothing":
    any read error (a lock held past the busy timeout) aborts that channel's
    sweep."""
    import sqlite3

    import collimate_server.cas_gc as cas_gc
    import collimate_server.database as database
    run_channel = _new_channel("run_artifacts")
    run_hash = hashlib.sha256(b'run artifact').hexdigest()
    conn = database.get_db_connection(run_channel)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO run_artifacts (run_id, category, filepath, blob_hash) VALUES ('r', 'config', 'c.json', ?)", (run_hash,))
    assert cas_gc._live_blobs_in_channel(conn) == {run_hash}
    old = conn

    class Locked:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            self._conn.close()

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        cas_gc._live_blobs_in_channel(Locked(old))

    busy = _new_channel("busy")
    calm = _new_channel("calm")
    h_busy = _write_blob(_channel_store(busy), b"held by a locked channel")
    h_calm = _write_blob(_channel_store(calm), b"unreferenced in a calm channel")
    real = cas_gc.get_db_connection
    monkeypatch.setattr(
        cas_gc, "get_db_connection",
        lambda slug, **kw: Locked(real(slug, **kw)) if slug == busy else real(slug, **kw))
    result = cas_gc.run_gc(grace_seconds=0)
    assert _result_for(result, busy)["aborted"] is True
    assert "locked" in _result_for(result, busy)["reason"]
    assert os.path.exists(_channel_store(busy).blob_path(h_busy))
    assert _result_for(result, calm)["swept"] == 1
    assert not os.path.exists(_channel_store(calm).blob_path(h_calm))


def test_bundle_index_keeps_aged_executable_cache_objects__F0251_F0295(fresh_cwd):
    import collimate_server.cas as cas
    import collimate_server.database as database
    import collimate_server.cas_gc as cas_gc
    store = cas.app_store()
    digest = _write_blob(store, b'export default () => null;')
    conn = database.get_global_db_connection()
    conn.execute('INSERT INTO renderer_bundles(source_hash, blob_hash) VALUES (?, ?)', ('a' * 64, digest))
    conn.commit()
    conn.close()
    result = cas_gc.collect_app_store()
    assert not result.get('aborted')
    assert result['swept'] == 0
    assert os.path.exists(store.blob_path(digest))


def test_accepted_handler_builds_keep_their_aged_app_store_objects(fresh_cwd):
    import collimate_server.cas as cas
    import collimate_server.database as database
    import collimate_server.cas_gc as cas_gc
    store = cas.app_store()
    digest = _write_blob(store, b'accepted derived file')
    orphan = _write_blob(store, b'nothing names this')
    conn = database.get_global_db_connection()
    columns = [row[1] for row in conn.execute('PRAGMA table_info(handler_derived_files)')]
    values = {column: 'x' for column in columns}
    values['blob_hash'] = digest
    conn.execute(f'INSERT INTO handler_derived_files({",".join(values)}) VALUES ({",".join("?" * len(values))})',
                 tuple(values.values()))
    conn.commit()
    conn.close()
    assert digest in cas_gc.app_store_live_blobs()
    result = cas_gc.collect_app_store()
    assert not result.get('aborted')
    assert result['swept'] == 1
    assert os.path.exists(store.blob_path(digest))
    assert not os.path.exists(store.blob_path(orphan))


def test_the_module_mirror_store_keeps_its_aged_objects(fresh_cwd):
    import collimate_server.cas as cas
    import collimate_server.database as database
    import collimate_server.cas_gc as cas_gc
    store = cas.app_store()
    digest = _write_blob(store, b'export const mirrored = 1;')
    conn = database.get_global_db_connection()
    conn.execute('INSERT INTO esm_modules VALUES (?, ?, ?, ?, ?, ?, ?)',
                 ('https://esm.sh/a@1.0.0', digest, 'text/javascript', 26, digest, 'https://esm.sh/a@1.0.0', 0.0))
    conn.commit()
    conn.close()
    assert digest in cas_gc.app_store_live_blobs()
    result = cas_gc.collect_app_store()
    assert not result.get('aborted')
    assert result['swept'] == 0
    assert os.path.exists(store.blob_path(digest))


def test_unreadable_bundle_index_aborts_the_app_store_sweep__F0251_F0295(fresh_cwd):
    import collimate_server.cas as cas
    import collimate_server.database as database
    import collimate_server.cas_gc as cas_gc
    store = cas.app_store()
    digest = _write_blob(store, b'old unowned object')
    channel_id = _seed_channel_with_cell("unaffected", "cell-u", {})
    channel_orphan = _write_blob(_channel_store(channel_id), b'a channel orphan')
    conn = database.get_global_db_connection()
    conn.execute('DROP TABLE renderer_bundles')
    conn.commit()
    conn.close()
    result = cas_gc.run_gc()
    assert result['app_store']['aborted'] is True
    assert result['app_store']['swept'] == 0
    assert os.path.exists(store.blob_path(digest))
    assert _result_for(result, channel_id)['swept'] == 1
    assert not os.path.exists(_channel_store(channel_id).blob_path(channel_orphan))
