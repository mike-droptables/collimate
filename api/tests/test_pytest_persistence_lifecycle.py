"""Durable body fidelity and fail-closed lifecycle publication."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi import HTTPException


def test_public_cas_resolvers_refuse_malformed_keys_before_io(fresh_cwd):
    from collimate_server.cas import channel_store
    store = channel_store(str(uuid4()))
    sentinel = fresh_cwd / "sentinel"
    sentinel.write_bytes(b"private scratch")
    malformed = "xx" + str(sentinel)
    with pytest.raises(ValueError):
        store.blob_path(malformed)
    assert store.read_blob(malformed) is None


def test_cas_dedupe_refuses_corrupt_existing_object(fresh_cwd):
    from collimate_server.cas import channel_store
    store = channel_store(str(uuid4()))
    digest = store.store_bytes(b"original")
    with open(store.blob_path(digest), "wb") as handle:
        handle.write(b"corrupted")
    with pytest.raises(OSError, match="integrity"):
        store.store_bytes(b"original")
    with pytest.raises(OSError, match="integrity"):
        store.require_blob(digest)


def test_required_cas_admission_failure_raises(fresh_cwd, monkeypatch):
    import collimate_server.cas as cas
    def refused(*args):
        raise OSError("injected ENOSPC")
    monkeypatch.setattr(cas, "_write_bytes", refused)
    with pytest.raises(OSError):
        cas.channel_store(str(uuid4())).hash_and_store_bytes(b"must remain pending")


def test_cas_publication_fsyncs_object_before_directory(fresh_cwd, monkeypatch):
    import collimate_server.cas as cas
    import stat
    synced = []
    actual = os.fsync
    def observe(fd):
        synced.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "object")
        return actual(fd)
    store = cas.channel_store(str(uuid4()))
    monkeypatch.setattr(os, "fsync", observe)
    store.store_bytes(b"durable bytes")
    assert "object" in synced and "directory" in synced
    assert synced.index("object") < synced.index("directory")


def _channel(name="source", files=None):
    from collimate_server.models import Channel, Stack, Cell, VirtualFile
    channel = Channel(name=name, main_stacks=[Stack(cells=[Cell(files=files or [VirtualFile(name="work.txt", content="user work")])])])
    from collimate_server.genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    from collimate_server.database import get_db_connection
    from collimate_server.persistence import initialize_files
    conn = get_db_connection(str(channel.id))
    initialize_files(conn, str(channel.main_stacks[0].cells[0].id), channel.main_stacks[0].cells[0].files)
    conn.commit()
    conn.close()
    return channel


def _register_destination(channel, monkeypatch):
    import collimate_server.state as state
    from collimate_server.models import AppState
    monkeypatch.setattr(state, 'state', AppState(channels=[channel], active_channel_id=channel.id))


def _import(destination, files):
    """Publish one new stack holding one new cell of ``files`` through the
    channel import publisher."""
    from collimate_server.import_plan import ImportPlan
    from collimate_server.models import Cell, Stack
    plan = ImportPlan(destination)
    plan.candidate.main_stacks.append(Stack(name="Imported", cells=[Cell(name="Imported", files=files)]))
    plan.publish()


def test_import_restart_preserves_complete_root_and_exact_bytes(fresh_cwd, monkeypatch):
    import collimate_server.genesis as genesis
    from collimate_server.cas import channel_store
    from collimate_server.cell_metadata import save_channel_layout
    from collimate_server.models import Channel, VirtualFile
    destination = Channel(name="destination")
    store = channel_store(str(destination.id))
    binary = store.store_bytes(b"\x00\xff")
    empty = store.store_bytes(b"")
    files = [VirtualFile(name="nested/work.txt", content="user work ☀"),
             VirtualFile(name="empty.txt", content=""),
             VirtualFile(name="binary.bin", blob_hash=binary, language="binary"),
             VirtualFile(name="empty.bin", blob_hash=empty, language="binary")]
    from collimate_server.genesis import ensure_capabilities_stack
    ensure_capabilities_stack(destination)
    _register_destination(destination, monkeypatch)
    _import(destination, [file.model_copy() for file in files])
    # Restart: the channel as its folder holds it (name in channel.json, layout in channel.db).
    save_channel_layout(genesis.snapshot_channel(destination))
    restored = genesis.load_channel(str(destination.id))
    genesis.reconcile_restored_cell_files(restored)
    recovered = {f.name: (f.content, f.blob_hash) for f in restored.main_stacks[0].cells[0].files}
    assert recovered == {f.name: (f.content, f.blob_hash) for f in files}


def test_export_missing_body_fails_without_publishing_blank_file(fresh_cwd):
    from collimate_server.lifecycle_snapshots import export_snapshot
    channel = _channel()
    import collimate_server.database as database
    import collimate_server.cas as cas
    conn = database.get_db_connection(str(channel.id))
    digest = conn.execute('SELECT body_hash FROM file_revisions').fetchone()[0]
    conn.close()
    os.unlink(cas.channel_store(str(channel.id)).blob_path(digest))
    with pytest.raises((OSError, ValueError)):
        with export_snapshot(channel):
            pass


def test_gc_preserves_explicit_reader_pin_after_mark(fresh_cwd, monkeypatch):
    import collimate_server.cas as cas
    import collimate_server.cas_gc as cas_gc
    channel_id = str(uuid4())
    store = cas.channel_store(channel_id)
    digest = store.store_bytes(b"export still owns this body")
    os.utime(store.blob_path(digest), (0, 0))
    with store.pin_blobs([digest]):
        assert cas_gc.collect_channel(channel_id)["swept"] == 0
        assert store.require_blob(digest) == b"export still owns this body"


def test_revision_candidates_are_gc_roots(fresh_cwd, monkeypatch):
    import collimate_server.cas as cas
    import collimate_server.cas_gc as cas_gc
    import collimate_server.database as database
    channel_id = str(uuid4())
    conn = database.get_db_connection(channel_id)
    store = cas.channel_store(channel_id)
    digest = store.store_bytes(b"retained conflict candidate")
    conn.execute("INSERT INTO file_revisions(cell_id,path,revision,base_revision,body_hash,deleted,language,encoding,writer_id,edit_seq,operation_id) VALUES(?,?,1,0,?,0,'plaintext','utf-8','test',1,'test')", (str(uuid4()), "candidate.txt", digest))
    row=conn.execute('SELECT cell_id FROM file_revisions').fetchone()
    conn.execute("INSERT INTO file_conflicts(conflict_id,operation_id,cell_id,path,base_revision,current_revision,candidate_revision) VALUES ('conflict','test',?,'candidate.txt',0,0,1)",(row[0],))
    conn.commit()
    conn.close()
    os.utime(store.blob_path(digest), (0, 0))
    assert cas_gc.collect_channel(channel_id)["swept"] == 0


def test_import_failure_rolls_back_the_candidate(fresh_cwd, monkeypatch):
    import collimate_server.import_plan as import_plan
    from collimate_server.models import Channel, VirtualFile
    from collimate_server.state import channel_db_name
    destination = Channel(name='destination')
    from collimate_server.genesis import ensure_capabilities_stack
    ensure_capabilities_stack(destination)
    _register_destination(destination, monkeypatch)
    real = import_plan.initialize_imported_cells
    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise OSError('injected ENOSPC')
    monkeypatch.setattr(import_plan, 'initialize_imported_cells', fail)
    with pytest.raises(OSError, match='ENOSPC'):
        _import(destination, [VirtualFile(name='work.txt', content='user work')])
    assert destination.main_stacks == []
    from collimate_server.database import get_db_connection
    with get_db_connection(channel_db_name(destination)) as conn:
        assert conn.execute('SELECT count(*) FROM working_files').fetchone()[0] == 0


def test_remove_restore_retains_body_and_rejects_pre_removal_revision(fresh_cwd, monkeypatch):
    import collimate_server.state as state
    from collimate_server.cell_catalog import remember_removed_cells, restore_cell
    from collimate_server.database import get_db_connection
    from collimate_server.models import AppState
    from collimate_server.persistence import snapshot_files, mutate_files, read_file
    channel = _channel()
    cell = channel.main_stacks[0].cells[0]
    stack = channel.main_stacks[0]
    monkeypatch.setattr(state, 'state', AppState(channels=[channel], active_channel_id=channel.id))
    import collimate_server.user_validation as user_validation
    monkeypatch.setattr(user_validation, 'notify_user_validation', lambda *args, **kwargs: None)
    remember_removed_cells(str(channel.id), stack, [cell])
    channel.main_stacks = []
    conn = get_db_connection(str(channel.id))
    assert snapshot_files(conn, str(cell.id)) == []
    assert snapshot_files(conn, str(cell.id), include_deleted=True)[0]['file_revision'] == 2
    conn.close()
    restore_cell(str(channel.id), str(cell.id))
    assert read_file(str(channel.id), str(cell.id), 'work.txt')[1] == b'user work'
    operation = {'operation_id': str(uuid4()), 'resolves': [], 'mutations': [
        {'path': 'work.txt', 'kind': 'put', 'base_revision': 1, 'writer_id': 'stale-browser',
         'edit_seq': 1, 'content_text': 'delayed pre-removal edit', 'language': 'plaintext'}]}
    assert mutate_files(str(channel.id), str(cell.id), operation)['outcome'] == 'conflict'
    assert read_file(str(channel.id), str(cell.id), 'work.txt')[1] == b'user work'


def test_channel_rename_keeps_database_and_capability_identity(fresh_cwd, monkeypatch):
    import asyncio
    import collimate_server.state as state
    from collimate_server.auth_middleware import cell_file_caps
    from collimate_server.database import get_db_connection, canonical_channel_id
    from collimate_server.models import AppState
    from collimate_server.routes.channels import _rename_channel
    channel = _channel(name='Before')
    cell = channel.main_stacks[0].cells[0]
    monkeypatch.setattr(state, 'state', AppState(channels=[channel], active_channel_id=channel.id))
    import collimate_server.routes.channels  # noqa: F401 (loads routes.channels)
    from collimate_server import routes
    monkeypatch.setattr(routes.channels, 'state', state.state)
    from collimate_server import channel_paths
    path = channel_paths.db_path(channel.id)
    inode = path.stat().st_ino
    capability = cell_file_caps.cap(str(channel.id), str(cell.id))
    asyncio.run(_rename_channel(channel.id, 'After'))
    assert path.stat().st_ino == inode
    assert canonical_channel_id(str(channel.id)) == str(channel.id)
    assert cell_file_caps.cap(str(channel.id), str(cell.id)) == capability
    for name in ('Before', 'After'):
        with pytest.raises(ValueError):
            canonical_channel_id(name)
        with pytest.raises(HTTPException):
            cell_file_caps.cap(name, str(cell.id))
    with pytest.raises(ValueError):
        get_db_connection('not-registered')
