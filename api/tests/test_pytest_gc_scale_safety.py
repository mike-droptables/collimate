"""Scheduled CAS collection must establish a complete mark set first."""
import os
import time
import pytest


def test_new_file_publication_does_not_inherit_source_age__F0041(fresh_cwd):
    import cas
    source = fresh_cwd / "old-source.bin"
    source.write_bytes(b"new CAS publication from old filesystem bytes")
    os.utime(source, (0, 0))
    started = time.time()
    digest = cas.hash_and_store_file(str(source))
    assert digest is not None
    assert os.stat(cas.blob_path(digest)).st_mtime >= started


def test_admitting_old_binary_reference_after_mark_preserves_bytes__P0F14(fresh_cwd, monkeypatch):
    import cas
    import cas_gc
    import state
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())
    digest = cas.store_bytes(b"old binary reference newly admitted")
    os.utime(cas.blob_path(digest), (0, 0))
    original = cas_gc._iter_cas_blobs
    def after_mark():
        cas.retain_blob(digest)
        yield from original()
    monkeypatch.setattr(cas_gc, "_iter_cas_blobs", after_mark)
    assert cas_gc.run_gc()["swept"] == 0
    assert os.path.exists(cas.blob_path(digest))
    with pytest.raises(FileNotFoundError):
        cas.retain_blob("f" * 64)
    with pytest.raises(ValueError):
        cas.retain_blob("../outside")


def test_unreadable_channel_aborts_sweep_and_purge__P0F14(fresh_cwd, monkeypatch):
    import cas
    import cas_gc
    import state
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())
    digest = cas.store_bytes(b"unreferenced but unknown database may own it")
    os.utime(cas.blob_path(digest), (0, 0))
    channels = fresh_cwd / ".collimate/channels"
    channels.mkdir()
    (channels / "broken.db").write_bytes(b"not SQLite")
    result = cas_gc.run_gc(grace_seconds=0)
    assert result.get("aborted") is True
    assert result["swept"] == result["purged_sweeps"] == 0
    assert os.path.exists(cas.blob_path(digest))


def test_accepted_current_files_survive_without_a_layout_file_projection(fresh_cwd, monkeypatch):
    import cas
    import cas_gc
    import database
    import state
    from models import AppState
    from persistence import initialize_files
    from uuid import uuid4
    monkeypatch.setattr(state, "state", AppState())
    digest = cas.store_bytes(b"accepted binary")
    conn = database.get_db_connection(str(uuid4()))
    try:
        initialize_files(conn, str(uuid4()), [{
            'path': 'current.bin', 'body_hash': digest, 'encoding': 'binary', 'language': 'binary',
        }])
        conn.commit()
    finally:
        conn.close()
    os.utime(cas.blob_path(digest), (0, 0))
    result = cas_gc.run_gc()
    assert result["swept"] == 0
    assert cas.require_blob(digest) == b"accepted binary"


def test_recent_store_and_dedupe_hit_have_mtime_floor__F0041(fresh_cwd, monkeypatch):
    import cas
    import cas_gc
    import state
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())
    digest = cas.store_bytes(b"inflight")
    os.utime(cas.blob_path(digest), (0, 0))
    assert cas.store_bytes(b"inflight") == digest
    result = cas_gc.run_gc()
    assert result["swept"] == 0
    assert os.path.exists(cas.blob_path(digest))


def test_creation_inflight_defers_every_gc_action__P0F14(fresh_cwd, monkeypatch):
    import cas
    import cas_gc
    import database
    import state
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())
    digest = cas.store_bytes(b"creation has not inserted its references")
    os.utime(cas.blob_path(digest), (0, 0))
    monkeypatch.setattr(database, "creating_channels", {"pending"})
    assert cas_gc.run_gc()["aborted"]
    assert os.path.exists(cas.blob_path(digest))


def test_channel_parked_after_listing_aborts_instead_of_losing_refs__P0F14(fresh_cwd, monkeypatch):
    import cas
    import cas_gc
    import database
    import state
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())
    digest = cas.store_bytes(b"parked database still owns these bytes")
    os.utime(cas.blob_path(digest), (0, 0))
    from uuid import uuid4
    channel_id = str(uuid4())
    conn = database.get_db_connection(channel_id)
    conn.execute("INSERT INTO cells(id, source_type) VALUES ('v', 'user')")
    conn.execute("INSERT INTO blobs(hash, mime_type, size) VALUES (?, 'text/plain', 42)", (digest,))
    conn.execute("INSERT INTO cell_files(cell_id, filepath, blob_hash, encoding, language) "
                 "VALUES ('v','file',?,'utf-8','text')", (digest,))
    from versions import initialize_file_snapshot
    initialize_file_snapshot(conn, 'v')
    conn.commit()
    conn.close()
    original = cas_gc._channel_dbs
    def list_then_move():
        names = original()
        database.set_aside_orphan_channel_db(channel_id)
        return names
    monkeypatch.setattr(cas_gc, "_channel_dbs", list_then_move)
    result = cas_gc.run_gc(grace_seconds=0)
    assert result["aborted"] and result["swept"] == result["purged_sweeps"] == 0
    assert os.path.exists(cas.blob_path(digest))


def test_dedupe_between_mark_and_sweep_is_not_trashed__F0041(fresh_cwd, monkeypatch):
    import cas
    import cas_gc
    import state
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())
    digest = cas.store_bytes(b"concurrent store")
    os.utime(cas.blob_path(digest), (0, 0))
    original = cas_gc._iter_cas_blobs
    def after_mark():
        cas.store_bytes(b"concurrent store")
        yield from original()
    monkeypatch.setattr(cas_gc, "_iter_cas_blobs", after_mark)
    assert cas_gc.run_gc()["swept"] == 0
    assert os.path.exists(cas.blob_path(digest))


async def test_daily_gc_dispatches_one_worker__F0083(monkeypatch):
    import asyncio
    import cas_gc
    calls = []
    monkeypatch.setattr(cas_gc, "run_gc", lambda: calls.append(1) or {})
    task = asyncio.create_task(cas_gc.run_periodic(interval=0.01))
    try:
        for _ in range(100):
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
