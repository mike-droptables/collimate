"""Accepted-file metadata, atomic mutations and explicit byte-read regressions."""
import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest

from import_cells import import_cell


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from database import get_db_connection
    from models import Channel
    from state import state

    with TestClient(api_app) as http:
        channel = Channel(name='Starting')
        from genesis import ensure_capabilities_stack
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        yield http


def _cell(client, content='original', name='note.txt'):
    from state import get_active_channel
    channel = get_active_channel()
    response = import_cell(client, channel.id, [{'name': name, 'content': content}], name='P2 synthetic cell')
    assert response.status_code == 200, response.text
    cell = next(c for s in channel.main_stacks for c in s.cells
                if str(c.id) == response.json()['cell_ids'][0])
    return channel, cell


def _write(client, channel, cell, *mutations):
    return client.post(f'/api/channels/{channel.id}/cells/{cell.id}/files/mutate', json={
        'operation_id': str(uuid.uuid4()), 'mutations': [
            {'writer_id': 'route-test', 'edit_seq': 1, **mutation} for mutation in mutations],
    })


def _put(path='note.txt', content='new body', base=1, **extra):
    return dict(path=path, kind='put', content_text=content, base_revision=base, **extra)


def _snapshot(channel, cell, *, deleted=False):
    from database import get_db_connection
    from persistence import snapshot_files
    with get_db_connection(str(channel.id), create=False) as conn:
        return snapshot_files(conn, str(cell.id), include_deleted=deleted)


def test_history_tree_is_metadata_only_and_full_transfer_is_explicit__F0299(client):
    channel, cell = _cell(client, 'x' * 60000)
    response = _write(client, channel, cell, _put(content='y' * 60000))
    assert response.status_code == 200 and response.json()['outcome'] == 'accepted', response.text
    checkpoint = client.post(f'/api/channels/{channel.id}/cells/{cell.id}/checkpoint')
    assert checkpoint.status_code == 200, checkpoint.text
    from database import get_db_connection
    from versions import find_tip
    with get_db_connection(str(channel.id)) as conn:
        tip = find_tip(conn, str(cell.id))
    tree = client.get(f'/api/channels/{channel.id}/cells/{tip}/files-tree')
    assert tree.status_code == 200, tree.text
    descriptor = tree.json()['files'][0]
    assert descriptor['body_hash'] == hashlib.sha256(b'y' * 60000).hexdigest()
    assert descriptor['encoding'] == 'utf-8'
    assert not {'content', 'content_hash', 'is_loaded', 'blob_hash'} & descriptor.keys()
    full = client.get(f'/api/channels/{channel.id}/versions/{tip}/files/note.txt')
    assert full.status_code == 200 and full.content == b'y' * 60000


def test_changed_large_file_gets_monotonic_revision_and_metadata_receipt__P0F10(client):
    channel, cell = _cell(client, 'x' * 60000)
    before = _snapshot(channel, cell)[0]
    response = _write(client, channel, cell, _put(content='y' * 60000))
    assert response.status_code == 200 and response.json()['outcome'] == 'accepted', response.text
    descriptor = response.json()['files'][0]
    assert descriptor['file_revision'] > before['file_revision']
    assert descriptor['body_hash'] == hashlib.sha256(b'y' * 60000).hexdigest()
    assert not {'content', 'content_hash', 'is_loaded', 'timestamp'} & descriptor.keys()
    current = client.get('/api/state').json()['channels'][0]['main_stacks'][-1]['cells'][0]['files'][0]
    assert current['body_hash'] == descriptor['body_hash']
    assert 'content' not in current
    assert client.get(f'/api/channels/{channel.id}/cells/{cell.id}/files/note.txt',
                      params={'revision': descriptor['file_revision']}).content == b'y' * 60000


def test_explicit_empty_text_clears_shrunken_body__F0530(client):
    channel, cell = _cell(client, 'x' * 60000)
    first = _write(client, channel, cell, _put(content='small current body')).json()
    assert first['outcome'] == 'accepted'
    second = _write(client, channel, cell, _put(content='', base=first['files'][0]['file_revision'], edit_seq=2))
    assert second.status_code == 200 and second.json()['outcome'] == 'accepted', second.text
    assert client.get(f'/api/channels/{channel.id}/cells/{cell.id}/files/note.txt').content == b''
    assert second.json()['files'][0]['body_hash'] == hashlib.sha256(b'').hexdigest()


def test_layout_rename_response_has_no_file_bodies__F0305_F0257(client):
    channel, cell = _cell(client, 'private body' * 5000)
    stack = next(s for s in channel.main_stacks if cell in s.cells)
    from fastapi.encoders import jsonable_encoder
    from routes.stacks import rename_stack
    body = jsonable_encoder(asyncio.run(rename_stack(str(channel.id), stack.id, 'Renamed')))
    assert body['main_stacks'][-1]['name'] == 'Renamed'
    assert all('files' not in c for key in ['main_stacks', 'nav_stacks'] for s in body[key] for c in s['cells'])


@pytest.mark.parametrize('kind', ['put', 'delete'])
def test_missing_cell_refuses_mutation_without_orphan_rows__P0F12(client, kind):
    channel, _ = _cell(client)
    from models import Cell
    missing = Cell(id=uuid.uuid4())
    mutation = dict(path='note.txt', kind=kind, base_revision=0)
    if kind == 'put':
        mutation['content_text'] = 'late bytes'
    assert _write(client, channel, missing, mutation).status_code == 404
    from database import get_db_connection
    with get_db_connection(str(channel.id)) as conn:
        assert conn.execute('SELECT count(*) FROM file_revisions WHERE cell_id=?', (str(missing.id),)).fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM working_files WHERE cell_id=?', (str(missing.id),)).fetchone()[0] == 0


def test_read_routes_never_recreate_missing_database__P0F09(client):
    from state import get_active_channel
    from database import get_db_connection
    channel = str(get_active_channel().id)
    conn = get_db_connection(channel)
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    conn.close()
    original = Path(f'.collimate/channels/{channel}.db')
    original.rename(original.with_name('parked.db'))
    response = client.get(f'/api/channels/{channel}/runs')
    assert response.status_code == 404, response.text
    assert not original.exists()


@pytest.mark.parametrize('suffix', ['runs', 'runs/missing', 'runs/missing/artifacts', 'runs/missing/events_since',
                                   'cells/missing/files', 'cells/missing/history', 'cells/missing/files-tree',
                                   'cells/missing/files/note.txt', 'versions/missing/files/note.txt', 'cells/removed',
                                   'lineage', 'journal'])
def test_read_opens_pin_mode_rw__P0F09(client, monkeypatch, suffix):
    import database
    from state import get_active_channel
    channel = str(get_active_channel().id)
    original = database._connect
    opens = []

    def record(target, **kwargs):
        if f'{channel}.db' in target:
            opens.append((target, kwargs))
        return original(target, **kwargs)

    monkeypatch.setattr(database, '_connect', record)
    response = client.get(f'/api/channels/{channel}/{suffix}')
    assert response.status_code in (200, 404), response.text
    assert opens
    assert all('?mode=rw' in target and options.get('uri') is True for target, options in opens)


def test_grouped_put_and_delete_land_together__F0535_F0485(client):
    channel, cell = _cell(client)
    response = _write(client, channel, cell, _put('new.txt', base=0),
                      dict(path='note.txt', kind='delete', base_revision=1))
    assert response.status_code == 200 and response.json()['outcome'] == 'accepted', response.text
    assert [(f['path'], f['deleted']) for f in _snapshot(channel, cell, deleted=True)] == [
        ('new.txt', False), ('note.txt', True)]
    assert client.get(f'/api/channels/{channel.id}/cells/{cell.id}/files/new.txt').content == b'new body'
    assert client.get(f'/api/channels/{channel.id}/cells/{cell.id}/files/note.txt').status_code == 404


def test_failed_group_commit_preserves_current_bytes_and_metadata__F0535_F0485(client, monkeypatch):
    import persistence
    channel, cell = _cell(client)
    before = _snapshot(channel, cell, deleted=True)
    monkeypatch.setattr(persistence, '_commit', lambda conn: (_ for _ in ()).throw(OSError('injected commit failure')))
    with pytest.raises(OSError, match='injected commit failure'):
        _write(client, channel, cell, _put('new.txt', base=0), dict(path='note.txt', kind='delete', base_revision=1))
    assert _snapshot(channel, cell, deleted=True) == before
    assert client.get(f'/api/channels/{channel.id}/cells/{cell.id}/files/note.txt').content == b'original'
    assert client.get(f'/api/channels/{channel.id}/cells/{cell.id}/files/new.txt').status_code == 404
    from database import get_db_connection
    with get_db_connection(str(channel.id)) as conn:
        assert conn.execute('SELECT count(*) FROM file_operations WHERE cell_id=?', (str(cell.id),)).fetchone()[0] == 0


def test_blob_and_accepted_media_support_range__F0288(client):
    from cas import store_bytes
    digest = store_bytes(b'0123456789')
    channel, cell = _cell(client)
    response = _write(client, channel, cell, dict(path='media.bin', kind='put', base_revision=0,
                      blob_hash=digest, encoding='binary'))
    assert response.status_code == 200 and response.json()['outcome'] == 'accepted', response.text
    for route in [f'/api/blob/{digest}', f'/api/channels/{channel.id}/cells/{cell.id}/files/media.bin']:
        ranged = client.get(route, headers={'Range': 'bytes=2-5'})
        assert ranged.status_code == 206 and ranged.content == b'2345'
        assert ranged.headers['content-range'] == 'bytes 2-5/10'


def test_mutation_preserves_explicit_language(client):
    channel, cell = _cell(client)
    response = _write(client, channel, cell, _put(language='custom-language'))
    assert response.status_code == 200 and response.json()['outcome'] == 'accepted', response.text
    assert _snapshot(channel, cell)[0]['language'] == 'custom-language'
    assert client.get(f'/api/channels/{channel.id}/cells/{cell.id}/files/note.txt').content == b'new body'


def test_binary_put_preserves_explicit_encoding_for_utf8_bytes(client):
    from cas import store_bytes
    channel, cell = _cell(client)
    digest = store_bytes(b'binary represented bytes')
    response = _write(client, channel, cell, dict(path='note.bin', kind='put', base_revision=0,
                       blob_hash=digest, encoding='binary', language='binary'))
    assert response.status_code == 200 and response.json()['outcome'] == 'accepted', response.text
    metadata = next(f for f in _snapshot(channel, cell) if f['path'] == 'note.bin')
    assert metadata['body_hash'] == digest and metadata['encoding'] == 'binary'
    assert metadata['language'] == 'binary'
    assert client.get(f'/api/channels/{channel.id}/cells/{cell.id}/files/note.bin').content == b'binary represented bytes'


@pytest.mark.parametrize('admission', ['create', 'single', 'group'])
def test_binary_admission_survives_gc_after_mark__P0F14(client, monkeypatch, admission):
    import os
    import cas
    import cas_gc
    channel, cell = _cell(client)
    digest = cas.store_bytes(b'orphan bytes referenced again by a client')
    os.utime(cas.blob_path(digest), (0, 0))
    original = cas_gc._iter_cas_blobs

    def publish_after_mark():
        if admission == 'create':
            response = import_cell(client, channel.id, [{'name': 'media.bin', 'blob_hash': digest}])
        else:
            mutations = [dict(path='media.bin', kind='put', base_revision=0, blob_hash=digest, encoding='binary')]
            if admission == 'group':
                mutations.append(_put(content='new sibling'))
            response = _write(client, channel, cell, *mutations)
            assert response.json()['outcome'] == 'accepted'
        assert response.status_code == 200, response.text
        yield from original()

    monkeypatch.setattr(cas_gc, '_iter_cas_blobs', publish_after_mark)
    result = cas_gc.run_gc()
    assert not result.get('aborted', False)
    assert Path(cas.blob_path(digest)).exists(), 'GC reclaimed a newly admitted binary reference'


@pytest.mark.parametrize('admission', ['create', 'single', 'group'])
def test_missing_binary_admission_publishes_nothing__P0F14(client, admission):
    channel, cell = _cell(client)
    before = _snapshot(channel, cell, deleted=True)
    digest = 'a' * 64
    if admission == 'create':
        stacks = len(channel.main_stacks)
        response = import_cell(client, channel.id, [{'name': 'missing.bin', 'blob_hash': digest}])
        assert response.status_code == 409, response.text
        assert len(channel.main_stacks) == stacks
    else:
        mutations = [dict(path='missing.bin', kind='put', base_revision=0, blob_hash=digest, encoding='binary')]
        if admission == 'group':
            mutations.append(_put(content='must not land'))
        response = _write(client, channel, cell, *mutations)
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'body_unavailable'
        assert response.json()['detail']['body_hash'] == digest
    assert _snapshot(channel, cell, deleted=True) == before
