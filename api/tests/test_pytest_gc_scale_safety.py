"""Scheduled CAS collection must establish a complete mark set first."""
import os
import time
import pytest


@pytest.fixture
def channel(fresh_cwd, monkeypatch):
    """One channel with its folder, in a live state that holds only it."""
    import collimate_server.state as state
    from conftest import channel_home
    from collimate_server.models import AppState, Channel
    from uuid import uuid4
    monkeypatch.setattr(state, "state", AppState())
    chan = Channel(id=str(uuid4()), name="gc")
    channel_home(chan)
    state.state.channels.append(chan)
    return str(chan.id)


def _aged(store, raw: bytes) -> str:
    digest = store.store_bytes(raw)
    os.utime(store.blob_path(digest), (0, 0))
    return digest


def test_new_file_publication_does_not_inherit_source_age__F0041(fresh_cwd, channel):
    from collimate_server.cas import channel_store
    store = channel_store(channel)
    source = fresh_cwd / "old-source.bin"
    source.write_bytes(b"new CAS publication from old filesystem bytes")
    os.utime(source, (0, 0))
    started = time.time()
    digest = store.hash_and_store_file(str(source))
    assert digest is not None
    assert os.stat(store.blob_path(digest)).st_mtime >= started


def test_admitting_old_binary_reference_after_mark_preserves_bytes__P0F14(channel, monkeypatch):
    from collimate_server.cas import channel_store
    import collimate_server.cas_gc as cas_gc
    store = channel_store(channel)
    digest = store.store_bytes(b"old binary reference newly admitted")
    os.utime(store.blob_path(digest), (0, 0))
    original = cas_gc._iter_store_blobs
    def after_mark(which):
        store.retain_blob(digest)
        yield from original(which)
    monkeypatch.setattr(cas_gc, "_iter_store_blobs", after_mark)
    assert cas_gc.collect_channel(channel)["swept"] == 0
    assert os.path.exists(store.blob_path(digest))
    with pytest.raises(FileNotFoundError):
        store.retain_blob("f" * 64)
    with pytest.raises(ValueError):
        store.retain_blob("../outside")


def test_unreadable_channel_aborts_its_sweep_and_purge_only__P0F14(channel, monkeypatch):
    """An unreadable channel.db stops that channel's collection (sweep and
    trash purge); another channel's store is collected as usual."""
    from collimate_server.cas import channel_store
    import collimate_server.cas_gc as cas_gc
    import collimate_server.channel_paths as channel_paths
    from collimate_server.models import Channel
    from conftest import channel_home
    from uuid import uuid4
    import collimate_server.state as state
    store = channel_store(channel)
    digest = _aged(store, b"unreferenced but the unreadable database may own it")
    other = Channel(id=str(uuid4()), name="other")
    channel_home(other)
    state.state.channels.append(other)
    other_store = channel_store(str(other.id))
    other_digest = _aged(other_store, b"unreferenced in a readable channel")
    channel_paths.db_path(channel).write_bytes(b"not SQLite")
    result = cas_gc.run_gc(grace_seconds=0)
    mine = next(row for row in result["channels"] if row["id"] == channel)
    assert mine.get("aborted") is True
    assert mine["swept"] == mine["purged_sweeps"] == 0
    assert os.path.exists(store.blob_path(digest))
    theirs = next(row for row in result["channels"] if row["id"] == str(other.id))
    assert not theirs.get("aborted") and theirs["swept"] == 1
    assert not os.path.exists(other_store.blob_path(other_digest))


def test_accepted_current_files_survive_without_a_layout_file_projection(channel):
    from collimate_server.cas import channel_store
    import collimate_server.cas_gc as cas_gc
    import collimate_server.database as database
    from collimate_server.persistence import initialize_files
    from uuid import uuid4
    store = channel_store(channel)
    digest = store.store_bytes(b"accepted binary")
    conn = database.get_db_connection(channel)
    try:
        initialize_files(conn, str(uuid4()), [{
            'path': 'current.bin', 'body_hash': digest, 'encoding': 'binary', 'language': 'binary',
        }])
        conn.commit()
    finally:
        conn.close()
    os.utime(store.blob_path(digest), (0, 0))
    result = cas_gc.collect_channel(channel)
    assert result["swept"] == 0
    assert store.require_blob(digest) == b"accepted binary"


def test_pinned_body_survives_collection(channel):
    from collimate_server.cas import channel_store
    import collimate_server.cas_gc as cas_gc
    store = channel_store(channel)
    digest = _aged(store, b"an export is streaming this")
    with store.pin_blobs([digest]):
        assert cas_gc.collect_channel(channel)["swept"] == 0
        assert os.path.exists(store.blob_path(digest))
    assert cas_gc.collect_channel(channel)["swept"] == 1
    assert not os.path.exists(store.blob_path(digest))


def test_recent_store_and_dedupe_hit_have_mtime_floor__F0041(channel):
    from collimate_server.cas import channel_store
    import collimate_server.cas_gc as cas_gc
    store = channel_store(channel)
    digest = store.store_bytes(b"inflight")
    os.utime(store.blob_path(digest), (0, 0))
    assert store.store_bytes(b"inflight") == digest
    result = cas_gc.collect_channel(channel)
    assert result["swept"] == 0
    assert os.path.exists(store.blob_path(digest))


def test_creation_inflight_defers_the_channels_collection__P0F14(channel, monkeypatch):
    from collimate_server.cas import channel_store
    import collimate_server.cas_gc as cas_gc
    import collimate_server.database as database
    store = channel_store(channel)
    digest = _aged(store, b"creation has not inserted its references")
    monkeypatch.setattr(database, "creating_channels", {channel})
    assert cas_gc.collect_channel(channel)["aborted"]
    assert os.path.exists(store.blob_path(digest))


def test_channel_being_moved_or_deleted_defers_its_collection__P0F14(channel, monkeypatch):
    """Replaces the old "channel parked after listing" race: a channel in
    ``changing_channels`` (delete / move in flight) is skipped whole."""
    from collimate_server.cas import channel_store
    import collimate_server.cas_gc as cas_gc
    import collimate_server.database as database
    store = channel_store(channel)
    digest = _aged(store, b"the folder is about to move")
    monkeypatch.setattr(database, "changing_channels", {channel})
    result = cas_gc.collect_channel(channel, grace_seconds=0)
    assert result["aborted"] and result["swept"] == result["purged_sweeps"] == 0
    assert os.path.exists(store.blob_path(digest))


def test_dedupe_between_mark_and_sweep_is_not_trashed__F0041(channel, monkeypatch):
    from collimate_server.cas import channel_store
    import collimate_server.cas_gc as cas_gc
    store = channel_store(channel)
    digest = store.store_bytes(b"concurrent store")
    os.utime(store.blob_path(digest), (0, 0))
    original = cas_gc._iter_store_blobs
    def after_mark(which):
        store.store_bytes(b"concurrent store")
        yield from original(which)
    monkeypatch.setattr(cas_gc, "_iter_store_blobs", after_mark)
    assert cas_gc.collect_channel(channel)["swept"] == 0
    assert os.path.exists(store.blob_path(digest))


async def test_daily_gc_dispatches_one_worker__F0083(fresh_cwd, monkeypatch):
    import asyncio
    import collimate_server.cas_gc as cas_gc
    calls = []
    monkeypatch.setattr(cas_gc, "run_gc", lambda: calls.append(1) or {})
    task = asyncio.create_task(cas_gc.run_periodic(interval=0.01))
    try:
        # The first dispatch waits for a worker thread to open and migrate a
        # fresh app.db (seconds_until_due): a few seconds, not 0.5 s, under a
        # loaded parallel suite.
        for _ in range(1000):
            if calls:
                break
            await asyncio.sleep(0.005)
        assert calls
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await cas_gc.wait_for_collection()
