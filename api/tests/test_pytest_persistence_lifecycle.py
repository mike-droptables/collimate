"""Durable body fidelity and fail-closed lifecycle publication."""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from fastapi import HTTPException


def test_public_cas_resolvers_refuse_malformed_keys_before_io(fresh_cwd):
    import cas
    sentinel = fresh_cwd / "sentinel"
    sentinel.write_bytes(b"private scratch")
    malformed = "xx" + str(sentinel)
    with pytest.raises(ValueError):
        cas.blob_path(malformed)
    assert cas.read_blob(malformed) is None


def test_cas_dedupe_refuses_corrupt_existing_object(fresh_cwd):
    import cas
    digest = cas.store_bytes(b"original")
    with open(cas.blob_path(digest), "wb") as handle:
        handle.write(b"corrupted")
    with pytest.raises(OSError, match="integrity"):
        cas.store_bytes(b"original")
    with pytest.raises(OSError, match="integrity"):
        cas.require_blob(digest)


def test_required_cas_admission_failure_raises(fresh_cwd, monkeypatch):
    import cas
    def refused(*args):
        raise OSError("injected ENOSPC")
    monkeypatch.setattr(cas, "_write_bytes", refused)
    with pytest.raises(OSError):
        cas.hash_and_store_bytes(b"must remain pending")


def test_cas_publication_fsyncs_object_before_directory(fresh_cwd, monkeypatch):
    import cas
    import stat
    synced = []
    actual = os.fsync
    def observe(fd):
        synced.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "object")
        return actual(fd)
    monkeypatch.setattr(os, "fsync", observe)
    cas.store_bytes(b"durable bytes")
    assert "object" in synced and "directory" in synced
    assert synced.index("object") < synced.index("directory")


def _channel(name="source", files=None):
    from models import Channel, Stack, Cell, VirtualFile
    channel = Channel(name=name, main_stacks=[Stack(cells=[Cell(files=files or [VirtualFile(name="work.txt", content="user work")])])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    from database import get_db_connection
    from persistence import initialize_files
    conn = get_db_connection(str(channel.id))
    initialize_files(conn, str(channel.main_stacks[0].cells[0].id), channel.main_stacks[0].cells[0].files)
    conn.commit()
    conn.close()
    return channel


def _register_destination(channel, monkeypatch):
    import state
    from models import AppState
    monkeypatch.setattr(state, 'state', AppState(channels=[channel], active_channel_id=channel.id))


def _import(destination, files):
    """Publish one new stack holding one new cell of ``files`` through the
    channel import publisher."""
    from import_plan import ImportPlan
    from models import Cell, Stack
    plan = ImportPlan(destination)
    plan.candidate.main_stacks.append(Stack(name="Imported", cells=[Cell(name="Imported", files=files)]))
    plan.publish()


def test_import_restart_preserves_complete_root_and_exact_bytes(fresh_cwd, monkeypatch):
    import cas
    import genesis
    from models import AppState, Channel, VirtualFile
    binary = cas.store_bytes(b"\x00\xff")
    empty = cas.store_bytes(b"")
    files = [VirtualFile(name="nested/work.txt", content="user work ☀"),
             VirtualFile(name="empty.txt", content=""),
             VirtualFile(name="binary.bin", blob_hash=binary, language="binary"),
             VirtualFile(name="empty.bin", blob_hash=empty, language="binary")]
    destination = Channel(name="destination")
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(destination)
    _register_destination(destination, monkeypatch)
    _import(destination, [file.model_copy() for file in files])
    restored = genesis.parse_saved_app_state(json.dumps(genesis.snapshot_app_state(AppState(channels=[destination]))))
    genesis.reconcile_restored_cell_files(restored)
    recovered = {f.name: (f.content, f.blob_hash) for f in restored.channels[0].main_stacks[0].cells[0].files}
    assert recovered == {f.name: (f.content, f.blob_hash) for f in files}


def test_export_missing_body_fails_without_publishing_blank_file(fresh_cwd):
    from lifecycle_snapshots import export_snapshot
    channel = _channel()
    import database
    import cas
    conn = database.get_db_connection(str(channel.id))
    digest = conn.execute('SELECT body_hash FROM file_revisions').fetchone()[0]
    conn.close()
    os.unlink(cas.blob_path(digest))
    with pytest.raises((OSError, ValueError)):
        with export_snapshot(channel):
            pass


def test_orphan_with_settings_is_parked(fresh_cwd):
    import database
    conn = database.get_db_connection(str(uuid4()))
    slug = os.path.basename(conn.execute("PRAGMA database_list").fetchone()[2])[:-3]
    conn.execute("INSERT INTO ui_state(key,value) VALUES ('channel_settings','{}')")
    conn.commit()
    conn.close()
    assert database.channel_db_holds_data(slug)
    result = database.set_aside_orphan_channel_db(slug)
    assert "moved" in result
    assert list((fresh_cwd / ".collimate/channels/orphaned").glob("*.db"))


def test_gc_preserves_explicit_reader_pin_after_mark(fresh_cwd, monkeypatch):
    import cas
    import cas_gc
    import state
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())
    digest = cas.store_bytes(b"export still owns this body")
    os.utime(cas.blob_path(digest), (0, 0))
    with cas.pin_blobs([digest]):
        assert cas_gc.run_gc()["swept"] == 0
        assert cas.require_blob(digest) == b"export still owns this body"


def test_revision_candidates_are_gc_roots(fresh_cwd, monkeypatch):
    import cas
    import cas_gc
    import database
    import state
    from models import AppState
    monkeypatch.setattr(state, "state", AppState())
    conn = database.get_db_connection(str(uuid4()))
    digest = cas.store_bytes(b"retained conflict candidate")
    conn.execute("INSERT INTO file_revisions(cell_id,path,revision,base_revision,body_hash,deleted,language,encoding,writer_id,edit_seq,operation_id) VALUES(?,?,1,0,?,0,'plaintext','utf-8','test',1,'test')", (str(uuid4()), "candidate.txt", digest))
    row=conn.execute('SELECT cell_id FROM file_revisions').fetchone()
    conn.execute("INSERT INTO file_conflicts(conflict_id,operation_id,cell_id,path,base_revision,current_revision,candidate_revision) VALUES ('conflict','test',?,'candidate.txt',0,0,1)",(row[0],))
    conn.commit()
    conn.close()
    os.utime(cas.blob_path(digest), (0, 0))
    assert cas_gc.run_gc()["swept"] == 0


def test_import_failure_rolls_back_the_candidate(fresh_cwd, monkeypatch):
    import import_plan
    from models import Channel, VirtualFile
    from state import channel_db_name
    destination = Channel(name='destination')
    from genesis import ensure_capabilities_stack
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
    from database import get_db_connection
    with get_db_connection(channel_db_name(destination)) as conn:
        assert conn.execute('SELECT count(*) FROM working_files').fetchone()[0] == 0


def test_reset_replaces_current_with_new_revisions_and_is_idempotent(fresh_cwd):
    from database import get_db_connection
    from models import VirtualFile
    from persistence import snapshot_files
    from reset_tool import load
    _commit_reset = load()._commit_reset
    channel = _channel()
    cell = channel.main_stacks[0].cells[0]
    conn = get_db_connection(str(channel.id))
    run_id = str(uuid4())
    conn.execute("INSERT INTO runs(id,prompt,status) VALUES(?, 'reset', 'completed')", (run_id,))
    replacement = [VirtualFile(name='work.txt', content='build replacement')]
    assert _commit_reset(conn, run_id, cell, replacement, None)
    conn.commit()
    current = snapshot_files(conn, str(cell.id))
    assert current[0]['file_revision'] == 2
    assert conn.execute('SELECT COUNT(*) FROM file_revisions').fetchone()[0] == 2
    assert not _commit_reset(conn, run_id, cell, replacement, None)
    conn.commit()
    assert snapshot_files(conn, str(cell.id))[0]['file_revision'] == 2
    conn.close()


def test_remove_restore_retains_body_and_rejects_pre_removal_revision(fresh_cwd, monkeypatch):
    import state
    from cell_catalog import remember_removed_cells, restore_cell
    from database import get_db_connection
    from models import AppState
    from persistence import snapshot_files, mutate_files, read_file
    channel = _channel()
    cell = channel.main_stacks[0].cells[0]
    stack = channel.main_stacks[0]
    monkeypatch.setattr(state, 'state', AppState(channels=[channel], active_channel_id=channel.id))
    import user_validation
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
    import state
    from auth_middleware import cell_file_caps
    from database import get_db_connection, canonical_channel_id
    from models import AppState
    from routes.channels import _rename_channel
    channel = _channel(name='Before')
    cell = channel.main_stacks[0].cells[0]
    monkeypatch.setattr(state, 'state', AppState(channels=[channel], active_channel_id=channel.id))
    import routes.channels
    monkeypatch.setattr(routes.channels, 'state', state.state)
    path = fresh_cwd / '.collimate/channels' / f'{channel.id}.db'
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
