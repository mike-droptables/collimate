"""Checked publication names, exact replay and coherent post-commit metadata."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture
def workspace(fresh_cwd, monkeypatch):
    import state
    import cell_metadata
    from models import AppState, Channel, Stack, Cell, VirtualFile
    from database import get_db_connection
    from persistence import initialize_files
    cell = Cell(name='Original', files=[VirtualFile(name='note.txt', content='acknowledged base')])
    channel = Channel(name='Durability fixture', main_stacks=[Stack(cells=[cell])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    conn = get_db_connection(channel.id)
    initialize_files(conn, str(cell.id), cell.files)
    conn.commit()
    conn.close()
    monkeypatch.setattr(state, 'state', AppState(channels=[channel], active_channel_id=channel.id))
    monkeypatch.setattr(state, '_persistence_writer', None)
    monkeypatch.setattr(state, 'broadcast_state_update', lambda: None)
    monkeypatch.setattr(cell_metadata, '_announce', lambda *args: None)
    state.persist_state()
    return SimpleNamespace(channel=channel, cell=cell, ch=str(channel.id), cid=str(cell.id))


def _global_rows(query, args=()):
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        return [dict(row) for row in conn.execute(query, args)]
    finally:
        conn.close()


def _layout():
    return json.loads(_global_rows("SELECT value FROM ui_state WHERE key='app_state'")[0]['value'])


def _rename(ws, name):
    from cell_metadata import publish_cell_metadata
    from cell_lineage import user_actor
    return publish_cell_metadata(ws.ch, ws.cid, {'name': name}, source_type='manual_edit', actor=user_actor())


def _fail_confirmation(monkeypatch, *, role, attempts=1):
    import database
    real = database.confirm_sql_durability
    observed = []

    def fail(conn):
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        matches = Path(path).name == 'app.db' if role == 'global' else Path(path).parent.name == 'channels'
        if matches and len(observed) < attempts:
            observed.append(path)
            raise database.DurabilityConfirmationError('injected checked directory EIO after commit')
        return real(conn)

    monkeypatch.setattr(database, 'confirm_sql_durability', fail)
    import cell_metadata
    monkeypatch.setattr(cell_metadata, 'confirm_sql_durability', fail)
    return observed


@pytest.mark.parametrize('role', ['channel', 'global'])
@pytest.mark.parametrize('mode', ['explicit', 'context'])
def test_recurring_wal_names_are_checked_after_every_commit(fresh_cwd, monkeypatch, role, mode):
    import database
    import cas
    identity = str(uuid4())
    opener = database.get_global_db_connection if role == 'global' else lambda: database.get_db_connection(identity)
    conn = opener()
    conn.execute('CREATE TABLE durability_probe (value INTEGER PRIMARY KEY)')
    conn.commit()
    conn.close()
    actual = cas.fsync_directory
    synced = []

    def observe(directory):
        synced.append(Path(directory))
        return actual(directory)

    monkeypatch.setattr(cas, 'fsync_directory', observe)
    for value in range(3):
        # Last close normally removes WAL/SHM; schema-cache hits do not exempt
        # recreated names from the checked commit boundary.
        conn = opener()
        try:
            assert [row[0] for row in conn.execute('SELECT value FROM durability_probe ORDER BY value')] == list(range(value))
            synced.clear()
            if mode == 'context':
                with conn:
                    conn.execute('INSERT INTO durability_probe VALUES (?)', (value,))
            else:
                conn.execute('INSERT INTO durability_probe VALUES (?)', (value,))
                conn.commit()
            path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
            assert synced[0] == path.parent
            assert Path.cwd() in synced
            assert Path.cwd() / '.collimate' in synced
        finally:
            conn.close()
    # An overlapping reader retains WAL while another writer confirms its names.
    reader, writer = opener(), opener()
    try:
        writer.execute('INSERT INTO durability_probe VALUES (3)')
        writer.commit()
        assert reader.execute('SELECT COUNT(*) FROM durability_probe').fetchone()[0] == 4
    finally:
        writer.close()
        reader.close()


def test_cas_confirms_fresh_ancestors_and_dedupe_retry(fresh_cwd, monkeypatch):
    import cas
    actual = cas.fsync_directory
    synced = []
    monkeypatch.setattr(cas, 'fsync_directory', lambda path: (synced.append(Path(path)), actual(path))[-1])
    body = b'\x00 valid UTF8 compatible binary with empty sibling'
    digest = cas.store_bytes(body)
    expected = [Path(cas.blob_path(digest)).absolute().parent, Path.cwd() / '.collimate/objects',
                Path.cwd() / '.collimate', Path.cwd()]
    assert synced == expected
    synced.clear()
    assert cas.store_bytes(body) == digest
    assert synced == expected
    assert cas.require_blob(digest) == body
    assert cas.require_blob(cas.store_bytes(b'')) == b''


@pytest.mark.parametrize('mode', ['explicit', 'context'])
def test_sql_confirmation_failure_is_committed_not_rollback(workspace, monkeypatch, mode):
    import database
    conn = database.get_db_connection(workspace.ch)
    _fail_confirmation(monkeypatch, role='channel')
    try:
        with pytest.raises(database.DurabilityConfirmationError):
            if mode == 'context':
                with conn:
                    conn.execute("INSERT INTO ui_state VALUES ('durable-row','accepted')")
            else:
                conn.execute("INSERT INTO ui_state VALUES ('durable-row','accepted')")
                conn.commit()
        assert not conn.in_transaction
        conn.rollback()
    finally:
        conn.close()
    reopened = database.get_db_connection(workspace.ch)
    try:
        assert reopened.execute("SELECT value FROM ui_state WHERE key='durable-row'").fetchone()[0] == 'accepted'
    finally:
        reopened.close()


def test_exact_file_replay_retries_failed_confirmation_without_new_revision(workspace, monkeypatch):
    import database
    import persistence
    operation = {'operation_id': str(uuid4()), 'mutations': [{'path': 'note.txt', 'base_revision': 1,
                 'writer_id': 'durability-writer', 'edit_seq': 1, 'kind': 'put', 'content_text': 'changed'}], 'resolves': []}
    failures = _fail_confirmation(monkeypatch, role='channel', attempts=2)
    with pytest.raises(database.DurabilityConfirmationError):
        persistence.mutate_files(workspace.ch, workspace.cid, operation)
    conn = database.get_db_connection(workspace.ch)
    try:
        expected = json.loads(conn.execute('SELECT result_json FROM file_operations WHERE operation_id=?',
                                          (operation['operation_id'],)).fetchone()[0])
    finally:
        conn.close()
    with pytest.raises(database.DurabilityConfirmationError):
        persistence.mutate_files(workspace.ch, workspace.cid, operation)
    assert len(failures) == 2
    assert persistence.mutate_files(workspace.ch, workspace.cid, operation) == expected
    assert persistence.read_file(workspace.ch, workspace.cid, 'note.txt')[1] == b'changed'
    conn = database.get_db_connection(workspace.ch)
    try:
        assert conn.execute('SELECT COUNT(*) FROM file_revisions WHERE cell_id=? AND path=?',
                            (workspace.cid, 'note.txt')).fetchone()[0] == 2
        assert conn.execute('SELECT last_edit_seq FROM file_writer_sequences WHERE cell_id=? AND path=? AND writer_id=?',
                            (workspace.cid, 'note.txt', 'durability-writer')).fetchone()[0] == 1
    finally:
        conn.close()


def test_user_rename_after_confirmation_error_repairs_identity_and_has_one_event(workspace, monkeypatch):
    import database
    import state
    import cas
    ws = workspace
    original_files = ws.cell.files
    _fail_confirmation(monkeypatch, role='global')
    # Surviving identities need no unrelated CAS reads during metadata recovery.
    monkeypatch.setattr(cas, 'read_blob', lambda *args: pytest.fail('metadata repair hydrated surviving bytes'))
    with pytest.raises(database.DurabilityConfirmationError):
        _rename(ws, 'Committed title')
    assert state.state.channels[0] is ws.channel
    assert ws.channel.main_stacks[0].cells[0] is ws.cell
    assert ws.cell.files is original_files
    assert ws.cell.name == 'Committed title'
    assert _rename(ws, 'Committed title')['name'] == 'Committed title'
    state._persist_state_sync()
    assert _layout()['channels'][0]['main_stacks'][0]['cells'][0]['name'] == 'Committed title'
    assert len(_global_rows('SELECT * FROM cell_rename_events')) == 1
    _rename(ws, 'Later title')
    assert ws.cell.name == 'Later title'
    assert len(_global_rows('SELECT * FROM cell_rename_events')) == 2


def test_failed_cache_repair_blocks_stale_layout_until_sql_recovery(workspace, monkeypatch):
    import database
    import state
    import cell_metadata
    ws = workspace
    real = cell_metadata._repair_layout_cache
    _fail_confirmation(monkeypatch, role='global')
    monkeypatch.setattr(cell_metadata, '_repair_layout_cache', lambda: (_ for _ in ()).throw(OSError('repair unavailable')))
    with pytest.raises(database.DurabilityConfirmationError):
        _rename(ws, 'SQL title')
    assert ws.cell.name == 'Original'
    with pytest.raises(OSError, match='repair unavailable'):
        state._persist_state_sync()
    assert _layout()['channels'][0]['main_stacks'][0]['cells'][0]['name'] == 'SQL title'
    monkeypatch.setattr(cell_metadata, '_repair_layout_cache', real)
    _rename(ws, 'Later title')
    assert ws.cell.name == 'Later title'
    state._persist_state_sync()
    assert _layout()['channels'][0]['main_stacks'][0]['cells'][0]['name'] == 'Later title'
    assert len(_global_rows('SELECT * FROM cell_rename_events')) == 2


def test_completed_run_name_replay_confirms_and_never_reapplies_old_name(workspace, monkeypatch):
    import database
    import state
    import cell_metadata
    import persistence
    ws = workspace
    operation = {'operation_id': str(uuid4()), 'mutations': [{'path': 'note.txt', 'base_revision': 1,
                 'writer_id': 'run-writer', 'edit_seq': 1, 'kind': 'put', 'content_text': 'run body'}], 'resolves': []}
    digest = hashlib.sha256(json.dumps(operation, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    args = (ws.ch, ws.cid, operation['operation_id'], digest, 'Run title')
    kwargs = {'source_type': 'generator', 'actor': {'kind': 'runner', 'runner_id': 'probe', 'runner_name': 'Probe'}}
    cell_metadata.prepare_cell_metadata(*args, **kwargs)
    persistence.mutate_files(ws.ch, ws.cid, operation, actor='host')
    _fail_confirmation(monkeypatch, role='global')
    with pytest.raises(database.DurabilityConfirmationError):
        cell_metadata.finish_cell_metadata(*args, **kwargs)
    expected = json.loads(_global_rows('SELECT result_json FROM cell_metadata_operations WHERE operation_id=?',
                                      (operation['operation_id'],))[0]['result_json'])
    assert ws.cell.name == 'Run title'
    _rename(ws, 'Later user title')
    _fail_confirmation(monkeypatch, role='global')
    with pytest.raises(database.DurabilityConfirmationError):
        cell_metadata.finish_cell_metadata(*args, **kwargs)
    assert cell_metadata.finish_cell_metadata(*args, **kwargs) == expected
    assert ws.cell.name == 'Later user title'
    state._persist_state_sync()
    assert _layout()['channels'][0]['main_stacks'][0]['cells'][0]['name'] == 'Later user title'
    assert len(_global_rows('SELECT * FROM cell_rename_events')) == 2


def test_deleted_channel_confirmation_failure_cannot_resurrect_layout(workspace, monkeypatch):
    import database
    import state
    import cell_metadata
    ws = workspace
    _fail_confirmation(monkeypatch, role='global')
    with pytest.raises(database.DurabilityConfirmationError):
        cell_metadata.remove_channel_layout(ws.ch)
    assert state.state.channels == []
    assert _layout()['channels'] == []
    state._persist_state_sync()
    assert _layout()['channels'] == []


@pytest.mark.parametrize('boundary', ['file_sync', 'shard_sync', 'objects_sync', 'collimate_sync',
                                     'data_root_sync', 'rename', 'sql_before_commit', 'sql_after_commit'])
def test_independent_admission_and_sql_errors_never_fake_rollback_or_ack(workspace, monkeypatch, boundary):
    import cas
    import database
    import persistence
    import sqlite3
    ws = workspace
    operation = {'operation_id': str(uuid4()), 'mutations': [{'path': 'note.txt', 'base_revision': 1,
                 'writer_id': 'error-writer', 'edit_seq': 1, 'kind': 'put',
                 'content_text': 'independent fault ' + boundary}], 'resolves': []}
    fired = []
    def fail_once():
        if not fired:
            fired.append(boundary)
            raise OSError('injected independent ' + boundary)
    if boundary == 'file_sync':
        real = cas.sync_cas_file
        def file_sync(path):
            fail_once()
            return real(path)
        monkeypatch.setattr(cas, 'sync_cas_file', file_sync)
    elif boundary.endswith('_sync'):
        digest = hashlib.sha256(operation['mutations'][0]['content_text'].encode()).hexdigest()
        paths = {'shard_sync': Path(cas.blob_path(digest)).absolute().parent,
                 'objects_sync': Path.cwd() / '.collimate/objects',
                 'collimate_sync': Path.cwd() / '.collimate', 'data_root_sync': Path.cwd()}
        real = cas.sync_cas_directory
        def directory_sync(path):
            if Path(path) == paths[boundary]:
                fail_once()
            return real(path)
        monkeypatch.setattr(cas, 'sync_cas_directory', directory_sync)
    elif boundary == 'rename':
        class FaultOS:
            def __getattr__(self, key):
                return getattr(__import__('os'), key)
            def replace(self, source, target):
                fail_once()
                return __import__('os').replace(source, target)
        monkeypatch.setattr(cas, 'os', FaultOS())
    else:
        real = persistence._commit
        def commit(conn):
            if boundary == 'sql_after_commit':
                real(conn)
            if not fired:
                fired.append(boundary)
                raise sqlite3.OperationalError('injected independent SQL commit failure')
            if boundary == 'sql_before_commit':
                return real(conn)
        monkeypatch.setattr(persistence, '_commit', commit)
    with pytest.raises((OSError, sqlite3.OperationalError)):
        persistence.mutate_files(ws.ch, ws.cid, operation)
    assert fired == [boundary]
    conn = database.get_db_connection(ws.ch)
    try:
        row = conn.execute('SELECT result_json FROM file_operations WHERE operation_id=?',
                           (operation['operation_id'],)).fetchone()
        if boundary == 'sql_after_commit':
            assert row is not None
            expected = json.loads(row[0])
        else:
            assert row is None
            assert conn.execute('SELECT last_edit_seq FROM file_writer_sequences WHERE writer_id=?',
                                ('error-writer',)).fetchone() is None
            assert persistence.read_file(ws.ch, ws.cid, 'note.txt')[1] == b'acknowledged base'
            expected = None
    finally:
        conn.close()
    receipt = persistence.mutate_files(ws.ch, ws.cid, operation)
    assert receipt['outcome'] == 'accepted'
    if expected:
        assert receipt == expected
    assert persistence.mutate_files(ws.ch, ws.cid, operation) == receipt
    assert persistence.read_file(ws.ch, ws.cid, 'note.txt')[1] == operation['mutations'][0]['content_text'].encode()
