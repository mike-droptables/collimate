"""Coverage for cas_gc — mark-and-sweep correctness.

Two correctness invariants we MUST maintain:

  * **Never delete a referenced blob.** A blob in cell_files of a
    non-deleted cell must survive the GC.
  * **Trash before delete.** Even orphans go to objects.trash/ first;
    the actual unlink only happens after the grace period. A bug in the
    mark phase shouldn't lose data permanently.

Tests run against a freshly-seeded DB + filesystem in a tmp_path so
they don't touch the developer's real ``.collimate``.
"""
from __future__ import annotations

import hashlib
import os
import uuid
import pytest


def test_abandoned_publisher_temp_cleanup_keeps_recent_foreign_and_symbolic_paths(fresh_cwd):
    from cas_gc import _reclaim_abandoned_cas_writes
    shard = fresh_cwd / '.collimate/objects/ab'
    shard.mkdir(parents=True)
    name = 'c' * 62 + '.' + 'd' * 32 + '.tmp'
    aged = shard / name
    aged.write_bytes(b'unpublished')
    os.utime(aged, (0, 0))
    recent = shard / ('e' * 62 + '.' + 'f' * 32 + '.tmp')
    recent.write_bytes(b'current publisher')
    foreign = shard / 'foreign.tmp'
    foreign.write_bytes(b'unowned')
    os.utime(foreign, (0, 0))
    target = fresh_cwd / 'unrelated'
    target.write_bytes(b'preserve')
    symbolic = shard / ('a' * 62 + '.' + 'b' * 32 + '.tmp')
    symbolic.symlink_to(target)
    elsewhere = fresh_cwd / 'elsewhere'
    elsewhere.mkdir()
    outside_temp = elsewhere / name
    outside_temp.write_bytes(b'outside shard')
    os.utime(outside_temp, (0, 0))
    (shard.parent / 'cd').symlink_to(elsewhere, target_is_directory=True)
    assert _reclaim_abandoned_cas_writes() == 1
    assert not aged.exists()
    assert recent.read_bytes() == b'current publisher'
    assert foreign.read_bytes() == b'unowned'
    assert symbolic.is_symlink() and target.read_bytes() == b'preserve'
    assert outside_temp.read_bytes() == b'outside shard'


def test_temp_cleanup_waits_for_live_publication_before_inspecting_candidates(fresh_cwd):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from cas import publication_lock
    from cas_gc import _reclaim_abandoned_cas_writes
    shard = fresh_cwd / '.collimate/objects/ab'
    shard.mkdir(parents=True)
    body = shard / ('c' * 62)
    temporary = shard / (body.name + '.' + 'd' * 32 + '.tmp')
    temporary.write_bytes(b'publisher owns these bytes')
    os.utime(temporary, (0, 0))
    entered = Event()
    def collect():
        entered.set()
        return _reclaim_abandoned_cas_writes()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with publication_lock:
            task = pool.submit(collect)
            assert entered.wait(1)
            with pytest.raises(TimeoutError):
                task.result(timeout=0.05)
            assert temporary.exists()
            temporary.rename(body)
        assert task.result(timeout=5) == 0
    assert body.read_bytes() == b'publisher owns these bytes'


@pytest.fixture(autouse=True)
def empty_layout(monkeypatch):
    import state
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())


def _write_blob(tmp_path, content: bytes) -> str:
    """Write ``content`` to .collimate/objects/<sha[:2]>/<sha[2:]>.
    Returns the hex hash for cell_files references."""
    h = hashlib.sha256(content).hexdigest()
    blob_dir = os.path.join(str(tmp_path), ".collimate", "objects", h[:2])
    os.makedirs(blob_dir, exist_ok=True)
    with open(os.path.join(blob_dir, h[2:]), "wb") as f:
        f.write(content)
    # These are old collection candidates, outside the new in-flight floor.
    os.utime(os.path.join(blob_dir, h[2:]), (0, 0))
    return h


def _seed_channel_with_cell(channel: str, cell_id: str, files: dict[str, str]):
    """Insert a cell + cell_files rows referencing the given hashes."""
    import database
    from models import Channel
    from state import state
    channel_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'cas-gc-fixture:' + channel))
    if not any(str(ch.id) == channel_id for ch in state.channels):
        state.channels.append(Channel(id=channel_id, name=channel))
    database._initialised_dbs.clear()
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
    from versions import initialize_file_snapshot
    initialize_file_snapshot(conn,cell_id)
    conn.commit()
    conn.close()
    return channel_id


def test_referenced_blob_survives_gc(fresh_cwd):
    """A blob attached to a non-deleted cell must NOT be moved to trash."""
    h = _write_blob(fresh_cwd, b"live content")
    _seed_channel_with_cell("test_channel", "cell-live", {"a.txt": h})

    import cas_gc
    result = cas_gc.run_gc()
    assert result["swept"] == 0
    blob_path = os.path.join(
        str(fresh_cwd), ".collimate", "objects", h[:2], h[2:]
    )
    assert os.path.isfile(blob_path), "live blob got swept"


def test_orphan_blob_moved_to_trash_not_deleted(fresh_cwd):
    """A blob with no live cell_files reference must move to trash, but
    the actual file must still exist (just under .objects.trash/)."""
    h = _write_blob(fresh_cwd, b"orphan")
    # No cell_files row for this blob.
    _seed_channel_with_cell("test_channel", "cell-other", {})

    import cas_gc
    result = cas_gc.run_gc()
    assert result["swept"] == 1

    # Original location empty.
    orig = os.path.join(str(fresh_cwd), ".collimate", "objects", h[:2], h[2:])
    assert not os.path.isfile(orig)

    # Found under trash.
    trash_root = os.path.join(str(fresh_cwd), ".collimate", "objects.trash")
    assert os.path.isdir(trash_root)
    found = False
    for dirpath, _dirs, files in os.walk(trash_root):
        if h[2:] in files:
            found = True
            break
    assert found, "orphan blob not found under trash"


def test_a_cell_off_the_canvas_keeps_its_blobs_O02(fresh_cwd):
    """Every version row is live: a cell in no layout (a removed cell, kept
    for the Removed shelf) still holds its blobs."""
    h = _write_blob(fresh_cwd, b"kept with its history")
    _seed_channel_with_cell("test_channel", "cell-off-canvas", {"a.txt": h})

    import cas_gc
    result = cas_gc.run_gc()
    assert result["swept"] == 0
    assert os.path.isfile(os.path.join(str(fresh_cwd), ".collimate", "objects", h[:2], h[2:]))


def test_in_flight_tmp_files_are_left_alone(fresh_cwd):
    """A file ending in .tmp under .collimate/objects/ is an in-flight
    write from cas._store_blob — never sweep it."""
    h = "a" * 64  # synthetic hash
    blob_dir = os.path.join(str(fresh_cwd), ".collimate", "objects", h[:2])
    os.makedirs(blob_dir, exist_ok=True)
    tmp_path = os.path.join(blob_dir, h[2:] + ".tmp")
    with open(tmp_path, "wb") as f:
        f.write(b"in-flight")

    import cas_gc
    result = cas_gc.run_gc()
    assert result["swept"] == 0
    assert os.path.isfile(tmp_path)


def test_expired_trash_is_purged(fresh_cwd):
    """A trash sweep older than the grace period is unlinked entirely.
    The grace defaults to 24h; tests pass ``grace_seconds=0`` to make
    every existing trash entry instantly expired."""
    # First, trash an orphan.
    _write_blob(fresh_cwd, b"trashed")
    _seed_channel_with_cell("test_channel", "cell-other", {})

    import cas_gc
    result1 = cas_gc.run_gc()
    assert result1["swept"] == 1

    trash_root = os.path.join(str(fresh_cwd), ".collimate", "objects.trash")
    assert os.path.isdir(trash_root)
    assert os.listdir(trash_root) != []

    # Backdate the sweep dir's mtime so it's "older than grace".
    for entry in os.listdir(trash_root):
        os.utime(os.path.join(trash_root, entry), (0, 0))

    # Re-run with grace=0 → the expired sweep gets purged.
    result2 = cas_gc.run_gc(grace_seconds=0)
    assert result2["purged_sweeps"] >= 1
    # Trash dir is now empty (or only contains fresh sweeps from this pass).
    remaining = os.listdir(trash_root) if os.path.isdir(trash_root) else []
    # Any remaining entries are from this pass and should be near-zero
    # mtime. The point is the old one is gone.
    assert all(
        os.path.getmtime(os.path.join(trash_root, e)) > 1000
        for e in remaining
    ), "old trash entry survived its grace period"


def test_disk_usage_reports_blob_count_and_bytes(fresh_cwd):
    _write_blob(fresh_cwd, b"hello world")
    _write_blob(fresh_cwd, b"second blob with more content")
    import cas_gc
    usage = cas_gc.disk_usage()
    assert usage["blob_count"] == 2
    assert usage["bytes"] == len(b"hello world") + len(b"second blob with more content")


def test_collect_live_blobs_unions_across_channels(fresh_cwd):
    """The mark phase walks every channel DB on disk and unions their
    live sets — a blob referenced by channel A must not be considered
    orphaned just because channel B doesn't see it."""
    h_a = _write_blob(fresh_cwd, b"used by channel A")
    h_b = _write_blob(fresh_cwd, b"used by channel B")

    _seed_channel_with_cell("channel_a", "cell-a", {"f.txt": h_a})
    _seed_channel_with_cell("channel_b", "cell-b", {"f.txt": h_b})

    import cas_gc
    live = cas_gc.collect_live_blobs()
    assert h_a in live
    assert h_b in live


def test_gc_refuses_incomplete_mark_from_corrupt_channel_db__P0F14(fresh_cwd):
    """An unreadable database prevents all reclamation."""
    h = _write_blob(fresh_cwd, b"healthy")
    _seed_channel_with_cell("good", "cell-good", {"a.txt": h})

    # Replace the bad channel's DB with random bytes.
    bad_path = os.path.join(
        str(fresh_cwd), ".collimate", "channels", "bad.db"
    )
    with open(bad_path, "wb") as f:
        f.write(b"\x00\xff\x00\xff garbage not a sqlite file ")

    import cas_gc
    with pytest.raises(cas_gc.IncompleteMark):
        cas_gc.collect_live_blobs()
    assert cas_gc.run_gc()["aborted"] is True
    assert os.path.exists(os.path.join(".collimate", "objects", h[:2], h[2:]))


def test_gc_keeps_blobs_of_parked_orphan_channel_db(fresh_cwd):
    """A channel DB parked under channels/orphaned/ stays recoverable, so
    the blobs only it references must survive the GC."""
    h = _write_blob(fresh_cwd, b"only the parked channel uses this")
    channel_id = _seed_channel_with_cell("parked_ch", "cell-p", {"a.txt": h})

    import database
    database.set_aside_orphan_channel_db(channel_id)
    orphaned = os.path.join(str(fresh_cwd), ".collimate", "channels", "orphaned")
    assert any(f.endswith(".db") for f in os.listdir(orphaned))
    assert not os.path.exists(
        os.path.join(str(fresh_cwd), ".collimate", "channels", channel_id + ".db")
    )

    import cas_gc
    result = cas_gc.run_gc()
    assert result["swept"] == 0
    assert os.path.exists(
        os.path.join(str(fresh_cwd), ".collimate", "objects", h[:2], h[2:])
    )


def test_an_unreadable_db_is_never_read_as_referencing_nothing__F0001_F0053(fresh_cwd, monkeypatch):
    """Channel deletion unlinks every blob of the deleted channel that no
    other DB references, so "cannot read this DB" must never come back as
    "it references nothing". Only a table or column a parked DB predates
    is skipped; any other read error (a lock held past the busy timeout)
    makes the walk report the DB as unreadable."""
    import sqlite3

    import pytest

    import cas_gc
    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    old.execute("CREATE TABLE run_artifacts (blob_hash TEXT)")
    run_hash = hashlib.sha256(b'run artifact').hexdigest()
    old.execute("INSERT INTO run_artifacts VALUES (?)", (run_hash,))
    assert cas_gc._live_blobs_in_channel(old) == {run_hash}

    class Locked:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            self._conn.close()

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        cas_gc._live_blobs_in_channel(Locked(old))

    h = _write_blob(fresh_cwd, b"held by a locked channel")
    busy = _seed_channel_with_cell("busy", "cell-busy", {"a.txt": h})
    calm = _seed_channel_with_cell("calm", "cell-calm", {"a.txt": h})
    real = cas_gc.get_db_connection
    monkeypatch.setattr(
        cas_gc, "get_db_connection",
        lambda slug, **kw: Locked(real(slug, **kw)) if slug == busy else real(slug, **kw))
    assert dict(cas_gc.iter_channel_blob_refs()) == {busy: None, calm: {h}}
    assert dict(cas_gc.iter_channel_blob_refs(skip_channel_id=calm)) == {busy: None}


def test_bundle_index_keeps_aged_executable_cache_objects__F0251_F0295(fresh_cwd):
    import database
    import cas_gc
    digest = _write_blob(fresh_cwd, b'export default () => null;')
    conn = database.get_global_db_connection()
    conn.execute('INSERT INTO renderer_bundles(source_hash, blob_hash) VALUES (?, ?)', ('a' * 64, digest))
    conn.commit()
    conn.close()
    result = cas_gc.run_gc()
    assert not result.get('aborted')
    assert result['swept'] == 0
    assert os.path.exists(os.path.join('.collimate', 'objects', digest[:2], digest[2:]))


def test_the_module_mirror_store_keeps_its_aged_objects(fresh_cwd):
    import database
    import cas_gc
    digest = _write_blob(fresh_cwd, b'export const mirrored = 1;')
    conn = database.get_global_db_connection()
    conn.execute('INSERT INTO esm_modules VALUES (?, ?, ?, ?, ?, ?, ?)',
                 ('https://esm.sh/a@1.0.0', digest, 'text/javascript', 26, digest, 'https://esm.sh/a@1.0.0', 0.0))
    conn.commit()
    conn.close()
    assert digest in cas_gc.collect_live_blobs()
    result = cas_gc.run_gc()
    assert not result.get('aborted')
    assert result['swept'] == 0
    assert os.path.exists(os.path.join('.collimate', 'objects', digest[:2], digest[2:]))


def test_unreadable_bundle_index_aborts_sweep__F0251_F0295(fresh_cwd):
    import database
    import cas_gc
    digest = _write_blob(fresh_cwd, b'old unowned object')
    conn = database.get_global_db_connection()
    conn.execute('DROP TABLE renderer_bundles')
    conn.commit()
    conn.close()
    result = cas_gc.run_gc()
    assert result['aborted'] is True
    assert result['swept'] == 0
    assert os.path.exists(os.path.join('.collimate', 'objects', digest[:2], digest[2:]))
