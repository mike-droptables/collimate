"""File import: the streamed blob upload, zip expansion and the import commit.

``POST /api/blob/stream`` writes a body of any size (up to the import cap) to
the CAS as it arrives; ``POST /api/import/zip`` expands an uploaded archive into
the CAS, refusing hostile structures and bombs; ``POST …/import`` publishes
new stacks and cells from blob references, all or nothing, deciding text or
binary from the stored bytes. An import into an existing cell is a file
mutation by blob hash.

Synthetic fixtures only — hand-made names, bodies and archives.
"""
from __future__ import annotations

import hashlib
import io
import os
import uuid
import zipfile

import pytest

from import_cells import import_cell, upload


@pytest.fixture
def client(api_app):
    """A real empty channel, without seeded SDK definitions."""
    from fastapi.testclient import TestClient
    from database import get_db_connection
    from models import Channel
    from state import state

    with TestClient(api_app) as http:
        channel = Channel(name='Import target')
        from genesis import ensure_capabilities_stack
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        http.channel_id = str(channel.id)
        yield http


def _layout(client) -> dict:
    state = client.get('/api/state').json()
    return next(c for c in state['channels'] if c['id'] == client.channel_id)


def _cells(client) -> dict:
    return {c['id']: c for s in _layout(client)['main_stacks'] for c in s['cells']}


def _objects() -> set[str]:
    root = os.path.join('.collimate', 'objects')
    return {shard + name for shard in os.listdir(root) if len(shard) == 2
            for name in os.listdir(os.path.join(root, shard)) if not name.endswith('.tmp')} if os.path.isdir(root) else set()


def _incoming() -> list[str]:
    from cas import INCOMING_DIR
    return os.listdir(INCOMING_DIR) if os.path.isdir(INCOMING_DIR) else []


def _commit(client, stacks, insert_index=None):
    return client.post(f'/api/channels/{client.channel_id}/import',
                       json={'insert_index': insert_index, 'stacks': stacks})


# ---------------------------------------------------------------------------
# POST /api/blob/stream
# ---------------------------------------------------------------------------

def test_stream_upload_above_the_buffered_limits_TR07(client):
    """A 70 MiB chunked body passes both the 50 MB blob cap and the 64 MB
    request cap: it is streamed to disk, hashed on the way and published."""
    block = os.urandom(64 * 1024)
    count = 70 * 16
    expected = hashlib.sha256()
    for _ in range(count):
        expected.update(block)

    def body():
        for _ in range(count):
            yield block

    response = client.post('/api/blob/stream', content=body())
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply == {'blob_hash': expected.hexdigest(), 'size': count * len(block), 'encoding': 'binary'}
    from cas import blob_path
    assert os.path.getsize(blob_path(reply['blob_hash'])) == count * len(block)
    assert _incoming() == []


def test_stream_upload_over_the_import_cap_is_413_and_stores_nothing_TR07(client, monkeypatch):
    import cas
    monkeypatch.setattr(cas, 'IMPORT_MAX_FILE_BYTES', 4096)
    before = _objects()

    def body():
        for _ in range(4):
            yield b'x' * 2048

    response = client.post('/api/blob/stream', content=body())
    assert response.status_code == 413, response.text
    assert _objects() == before
    assert _incoming() == []


@pytest.mark.parametrize('body,encoding', [
    (b'plain text\n', 'utf-8'),
    ('é unicode ✓'.encode('utf-8'), 'utf-8'),
    (b'', 'utf-8'),
    (b'abc\x00def', 'binary'),
    (b'\xff\xfe not utf-8', 'binary'),
    (b'a' * (8 * 1024) + b'\x00', 'utf-8'),
])
def test_stream_upload_sniffs_text_or_binary_TR07(client, body, encoding):
    """UTF-8 with no NUL in the first 8 KiB is text; anything else binary."""
    reply = upload(client, body)
    assert reply['encoding'] == encoding
    assert reply['size'] == len(body)
    assert reply['blob_hash'] == hashlib.sha256(body).hexdigest()


def test_text_above_the_text_threshold_is_binary_TR07(client, monkeypatch):
    import cas
    monkeypatch.setattr(cas, 'TEXT_IMPORT_MAX_BYTES', 16)
    assert upload(client, b'a' * 16)['encoding'] == 'utf-8'
    assert upload(client, b'a' * 17)['encoding'] == 'binary'


def test_store_stream_failure_leaves_no_private_file_TR07(fresh_cwd):
    import cas

    def broken():
        yield b'partial'
        raise OSError('source failed')

    with pytest.raises(OSError, match='source failed'):
        cas.store_stream(broken())
    assert _incoming() == []
    assert _objects() == set()


# ---------------------------------------------------------------------------
# POST /api/import/zip
# ---------------------------------------------------------------------------

def _zip(*members, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression) as archive:
        for member in members:
            info, data = member if isinstance(member, tuple) else (member, b'')
            if isinstance(info, str):
                info = zipfile.ZipInfo(info)
            info.compress_type = compression
            archive.writestr(info, data)
    return buffer.getvalue()


def _post_zip(client, raw):
    return client.post('/api/import/zip', content=raw)


def _imports_dir() -> list[str]:
    from transfer_import import IMPORTS_DIR
    return os.listdir(IMPORTS_DIR) if os.path.isdir(IMPORTS_DIR) else []


def _leftover(directory: str, name: str, *, age: float) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, 'wb') as out:
        out.write(b'abandoned')
    stamp = os.stat(path).st_mtime - age
    os.utime(path, (stamp, stamp))
    return path


def test_uploads_sweep_what_a_killed_process_left_TR07(client):
    """A process killed mid-upload leaves its private ``.part`` or archive
    behind; the next upload removes those past the stale age and leaves a
    live upload's fresh file alone."""
    from cas import INCOMING_DIR, STALE_UPLOAD_SECONDS
    from transfer_import import IMPORTS_DIR
    stale_part = _leftover(INCOMING_DIR, 'dead.part', age=STALE_UPLOAD_SECONDS + 60)
    fresh_part = _leftover(INCOMING_DIR, 'live.part', age=0)
    upload(client, b'next upload')
    assert not os.path.exists(stale_part) and os.path.exists(fresh_part)
    stale_zip = _leftover(IMPORTS_DIR, 'dead.zip', age=STALE_UPLOAD_SECONDS + 60)
    fresh_zip = _leftover(IMPORTS_DIR, 'live.zip', age=0)
    stale_part = _leftover(INCOMING_DIR, 'dead.part', age=STALE_UPLOAD_SECONDS + 60)
    assert _post_zip(client, _zip(('a.txt', b'a'))).status_code == 200
    assert not os.path.exists(stale_zip) and not os.path.exists(stale_part)
    assert os.path.exists(fresh_zip) and os.path.exists(fresh_part)


def test_zip_expands_entries_and_empty_dirs_TR09(client):
    raw = _zip(('Stack/Cell/notes.md', b'# notes\n'), ('Stack/Cell/img.bin', b'\x00\x01\x02'),
               'Stack/Cell/empty/', 'Stack/Cell/full/', ('Stack/Cell/full/a.txt', b'a'),
               ('__MACOSX/Stack/._notes.md', b'fork'))
    response = _post_zip(client, raw)
    assert response.status_code == 200, response.text
    reply = response.json()
    entries = {e['path']: e for e in reply['entries']}
    assert set(entries) == {'Stack/Cell/notes.md', 'Stack/Cell/img.bin', 'Stack/Cell/full/a.txt'}
    assert entries['Stack/Cell/notes.md'] == {
        'path': 'Stack/Cell/notes.md', 'blob_hash': hashlib.sha256(b'# notes\n').hexdigest(),
        'size': 8, 'encoding': 'utf-8'}
    assert entries['Stack/Cell/img.bin']['encoding'] == 'binary'
    assert reply['dirs'] == ['Stack/Cell/empty']
    assert reply['skipped'] == [{'path': '__MACOSX/Stack/._notes.md', 'reason': 'macOS metadata'}]
    from cas import require_blob
    assert require_blob(entries['Stack/Cell/img.bin']['blob_hash']) == b'\x00\x01\x02'
    assert _imports_dir() == []


def test_zip_expands_without_any_channel(client):
    """The expansion belongs to no channel: a new channel's sources expand
    before it exists, and nothing is created by expanding."""
    from state import state
    state.channels = []
    state.active_channel_id = None
    response = _post_zip(client, _zip(('Root/Generators/Tool/tool.generator', b'id: tool')))
    assert response.status_code == 200, response.text
    assert [e['path'] for e in response.json()['entries']] == ['Root/Generators/Tool/tool.generator']
    assert state.channels == []


def _symlink() -> zipfile.ZipInfo:
    info = zipfile.ZipInfo('Cell/link')
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    return info


@pytest.mark.parametrize('name', ['../escape.txt', 'Cell/../../escape.txt', '/etc/passwd', 'C:/x.txt',
                                  'Cell\\evil.txt', 'Cell//x.txt', '_runs/x/out.txt', 'Cell/_runs/out.txt',
                                  '.collrun/state', 'Stack/Cell/.collrun/x', 'symlink'])
def test_zip_refuses_hostile_or_reserved_structure_before_storing_TR09(client, name):
    member = (_symlink(), b'/etc/passwd') if name == 'symlink' else (name, b'payload')
    before = _objects()
    response = _post_zip(client, _zip(('Cell/fine.txt', b'stored only if accepted'), member))
    assert response.status_code == 400, response.text
    assert _objects() == before
    assert _imports_dir() == []


def test_zip_refuses_a_non_zip_body_TR09(client):
    response = _post_zip(client, b'not a zip at all')
    assert response.status_code == 400
    assert _imports_dir() == []


def test_zip_bomb_ratio_is_413_TR09(client):
    before = _objects()
    response = _post_zip(client, _zip(('bomb.txt', b'\x00' * (4 * 1024 * 1024))))
    assert response.status_code == 413, response.text
    assert _objects() == before


def test_zip_member_count_and_total_caps_are_413_TR09(client, monkeypatch):
    import transfer_import
    monkeypatch.setattr(transfer_import, 'ZIP_MAX_MEMBERS', 2)
    response = _post_zip(client, _zip(('a', b'1'), ('b', b'2'), ('c', b'3')))
    assert response.status_code == 413, response.text
    monkeypatch.setattr(transfer_import, 'ZIP_MAX_MEMBERS', 100)
    monkeypatch.setattr(transfer_import, 'ZIP_MAX_TOTAL_BYTES', 10)
    response = _post_zip(client, _zip(('a', b'123456'), ('b', b'123456'), compression=zipfile.ZIP_STORED))
    assert response.status_code == 413, response.text


# ---------------------------------------------------------------------------
# POST /api/channels/{channel}/import
# ---------------------------------------------------------------------------

def test_commit_creates_stacks_and_cells_at_the_insert_index_TR08(client):
    existing = import_cell(client, client.channel_id, [{'name': 'old.txt', 'content': 'old'}], name='Existing')
    assert existing.status_code == 200, existing.text
    text = upload(client, b'hello')['blob_hash']
    binary = upload(client, b'\x89PNG\x00\x01')['blob_hash']
    response = _commit(client, [
        {'name': 'First', 'cells': [
            {'name': 'One', 'files': [{'path': 'notes.md', 'blob_hash': text},
                                      {'path': 'img/logo.png', 'blob_hash': binary}],
             'dirs': ['empty', 'img', 'deep/er']},
            {'name': 'Two', 'files': [{'path': 'a.txt', 'blob_hash': text}], 'dirs': []}]},
        {'name': 'Second', 'cells': [{'name': 'Three', 'files': [], 'dirs': ['only']}]},
    ], insert_index=0)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert len(reply['stack_ids']) == 2 and len(reply['cell_ids']) == 3
    layout = _layout(client)
    stacks = layout['main_stacks']
    assert [s['id'] for s in stacks[:2]] == reply['stack_ids']
    assert stacks[2]['cells'][0]['name'] == 'Existing'
    assert layout['main_selection_id'] == reply['stack_ids'][0]
    assert [s['name'] for s in stacks[:2]] == ['First', 'Second']
    assert [[c['name'] for c in s['cells']] for s in stacks[:2]] == [['One', 'Two'], ['Three']]
    assert [c['id'] for s in stacks[:2] for c in s['cells']] == reply['cell_ids']
    one = {f['name']: f for f in stacks[0]['cells'][0]['files']}
    assert set(one) == {'notes.md', 'img/logo.png', 'empty/.collkeep', 'deep/er/.collkeep'}
    assert one['notes.md']['encoding'] == 'utf-8' and one['notes.md']['body_hash'] == text
    assert one['img/logo.png']['encoding'] == 'binary' and one['img/logo.png']['body_hash'] == binary
    assert [f['name'] for f in stacks[1]['cells'][0]['files']] == ['only/.collkeep']
    read = client.get(f"/api/channels/{client.channel_id}/cells/{reply['cell_ids'][0]}/files/img/logo.png")
    assert read.status_code == 200 and read.content == b'\x89PNG\x00\x01'


def test_commit_stamps_imported_roots_and_versions_TR08(client):
    """Each imported cell gets an ``import`` cells row whose immutable
    version entries match its current accepted files."""
    from database import get_db_connection
    from persistence import snapshot_files
    from versions import version_file_entries
    response = _commit(client, [{'name': 'S', 'cells': [
        {'name': f'Cell {i}', 'files': [{'path': 'note.txt', 'blob_hash': upload(client, f'imported {i}'.encode())['blob_hash']}]}
        for i in range(2)]}])
    assert response.status_code == 200, response.text
    cell_ids = response.json()['cell_ids']
    conn = get_db_connection(client.channel_id)
    try:
        rows = {r['id'] for r in conn.execute("SELECT id FROM cells WHERE source_type='import'").fetchall()}
        current = [snapshot_files(conn, cell_id) for cell_id in cell_ids]
        frozen = [version_file_entries(conn, cell_id) for cell_id in cell_ids]
    finally:
        conn.close()
    assert rows == set(cell_ids)
    assert [row[0]['body_hash'] for row in current] == [
        hashlib.sha256(f'imported {i}'.encode()).hexdigest() for i in range(2)]
    for rows_ in (current, frozen):
        assert all(len(row) == 1 and row[0]['path'] == 'note.txt' and row[0]['encoding'] == 'utf-8'
                   for row in rows_)


def test_commit_decides_encoding_on_the_server_TR08(client, monkeypatch):
    import cas
    big_text = upload(client, b'b' * 32)['blob_hash']
    monkeypatch.setattr(cas, 'TEXT_IMPORT_MAX_BYTES', 16)
    nul = upload(client, b'text\x00with nul')['blob_hash']
    response = import_cell(client, client.channel_id, [{'name': 'big.txt', 'blob_hash': big_text},
                                                       {'name': 'nul.txt', 'blob_hash': nul}])
    assert response.status_code == 200, response.text
    files = {f['name']: f for f in _cells(client)[response.json()['cell_ids'][0]]['files']}
    assert files['big.txt']['encoding'] == 'binary' and files['nul.txt']['encoding'] == 'binary'


def test_commit_appends_without_an_insert_index_TR08(client):
    import_cell(client, client.channel_id, [{'name': 'a.txt', 'content': 'a'}], name='A')
    response = import_cell(client, client.channel_id, [{'name': 'b.txt', 'content': 'b'}], name='B')
    assert _layout(client)['main_stacks'][-1]['id'] == response.json()['stack_ids'][0]
    clamped = import_cell(client, client.channel_id, [{'name': 'c.txt', 'content': 'c'}], name='C', insert_index=99)
    assert _layout(client)['main_stacks'][-1]['id'] == clamped.json()['stack_ids'][0]


@pytest.mark.parametrize('path', ['../escape.txt', '/abs.txt', 'a/../b.txt', 'a//b.txt', './a.txt', 'a\\b.txt',
                                  '_runs/x-1/out.txt', '_runs', '.collrun/stage', '', 'tab\there.txt'])
def test_commit_refuses_invalid_reserved_and_run_paths_atomically_TR08(client, path):
    """A bad path anywhere refuses the whole request; nothing is published."""
    good = upload(client, b'fine')['blob_hash']
    before = _layout(client)['main_stacks']
    response = _commit(client, [
        {'name': 'Fine', 'cells': [{'name': 'Fine', 'files': [{'path': 'ok.txt', 'blob_hash': good}]}]},
        {'name': 'Bad', 'cells': [{'name': 'Bad', 'files': [{'path': path, 'blob_hash': good}]}]},
    ])
    assert response.status_code == 400, response.text
    assert _layout(client)['main_stacks'] == before


def test_commit_refuses_a_bad_dir_TR08(client):
    response = _commit(client, [{'name': 'S', 'cells': [{'name': 'C', 'files': [], 'dirs': ['_runs/x']}]}])
    assert response.status_code == 400


def test_commit_with_an_unknown_blob_is_409_and_publishes_nothing_TR08(client):
    from database import get_db_connection
    before = _layout(client)['main_stacks']
    response = _commit(client, [{'name': 'S', 'cells': [
        {'name': 'C', 'files': [{'path': 'ok.txt', 'blob_hash': upload(client, b'ok')['blob_hash']},
                                {'path': 'missing.bin', 'blob_hash': 'a' * 64}]}]}])
    assert response.status_code == 409, response.text
    assert _layout(client)['main_stacks'] == before
    conn = get_db_connection(client.channel_id)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cells WHERE source_type='import'").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize('stacks', [
    [],
    [{'name': 'Empty', 'cells': []}],
])
def test_commit_refuses_no_stacks_or_an_empty_stack_TR08(client, stacks):
    response = _commit(client, stacks)
    assert response.status_code == 400, response.text


@pytest.mark.parametrize('digest', ['not-a-hash', '../../../../etc/passwd'])
def test_commit_refuses_a_malformed_hash_TR08(client, digest):
    response = _commit(client, [{'name': 'S', 'cells': [{'name': 'C', 'files': [{'path': 'x', 'blob_hash': digest}]}]}])
    assert response.status_code == 400


def test_commit_refuses_duplicate_paths_TR08(client):
    digest = upload(client, b'x')['blob_hash']
    response = _commit(client, [{'name': 'S', 'cells': [{'name': 'C', 'files': [
        {'path': 'x.txt', 'blob_hash': digest}, {'path': 'x.txt', 'blob_hash': digest}]}]}])
    assert response.status_code == 400


def test_commit_into_an_unknown_channel_is_404(client):
    response = client.post('/api/channels/no-such-channel/import', json={'stacks': [
        {'name': 'S', 'cells': [{'name': 'C', 'files': []}]}]})
    assert response.status_code == 404


def test_zip_preview_round_trips_into_a_commit_TR09(client):
    raw = _zip(('Stack/Cell/notes.md', b'# notes\n'), 'Stack/Cell/empty/')
    preview = _post_zip(client, raw).json()
    files = [{'path': e['path'].removeprefix('Stack/Cell/'), 'blob_hash': e['blob_hash']} for e in preview['entries']]
    dirs = [d.removeprefix('Stack/Cell/') for d in preview['dirs']]
    response = _commit(client, [{'name': 'Stack', 'cells': [{'name': 'Cell', 'files': files, 'dirs': dirs}]}])
    assert response.status_code == 200, response.text
    cell = _cells(client)[response.json()['cell_ids'][0]]
    assert sorted(f['name'] for f in cell['files']) == ['empty/.collkeep', 'notes.md']


# ---------------------------------------------------------------------------
# Import into an existing cell: the file mutation by blob hash
# ---------------------------------------------------------------------------

def _mutate(client, cell_id, **mutation):
    return client.post(f'/api/channels/{client.channel_id}/cells/{cell_id}/files/mutate', json={
        'operation_id': str(uuid.uuid4()), 'mutations': [
            {'writer_id': 'import-test', 'edit_seq': 1, 'base_revision': 0, 'kind': 'put', **mutation}]})


def test_existing_cell_takes_text_and_binary_by_hash_TR07(client):
    """A streamed text body joins a cell by hash with ``encoding: utf-8``,
    never inlined; a binary body likewise with ``binary``."""
    cell_id = import_cell(client, client.channel_id, [{'name': 'seed.txt', 'content': 'seed'}]).json()['cell_ids'][0]
    text = upload(client, 'é text body'.encode('utf-8'))
    binary = upload(client, b'\x00\xff binary')
    assert text['encoding'] == 'utf-8'
    accepted = _mutate(client, cell_id, path='docs/notes.md', blob_hash=text['blob_hash'], encoding='utf-8')
    assert accepted.status_code == 200 and accepted.json()['outcome'] == 'accepted', accepted.text
    accepted = _mutate(client, cell_id, path='bin/data.bin', blob_hash=binary['blob_hash'], encoding='binary')
    assert accepted.status_code == 200 and accepted.json()['outcome'] == 'accepted', accepted.text
    files = {f['name']: f for f in _cells(client)[cell_id]['files']}
    assert files['docs/notes.md']['encoding'] == 'utf-8'
    assert files['docs/notes.md']['body_hash'] == text['blob_hash']
    assert files['bin/data.bin']['encoding'] == 'binary'
    read = client.get(f'/api/channels/{client.channel_id}/cells/{cell_id}/files/docs/notes.md')
    assert read.content == 'é text body'.encode('utf-8') and read.headers['x-file-encoding'] == 'utf-8'


def test_existing_cell_refuses_non_utf8_bytes_declared_as_text_TR07(client):
    cell_id = import_cell(client, client.channel_id, [{'name': 'seed.txt', 'content': 'seed'}]).json()['cell_ids'][0]
    digest = upload(client, b'\xff\xfe broken')['blob_hash']
    response = _mutate(client, cell_id, path='bad.txt', blob_hash=digest, encoding='utf-8')
    assert response.status_code == 400, response.text


def test_existing_cell_by_hash_reads_the_body_in_bounded_chunks_TR07(client, monkeypatch):
    """A binary body joining a cell by hash is never read whole, neither
    while staging nor when the accepted mutation rebuilds the cell cache.
    The spies record rather than raise: publication swallows exceptions."""
    import cas
    cell_id = import_cell(client, client.channel_id, [{'name': 'seed.txt', 'content': 'seed'}]).json()['cell_ids'][0]
    binary = upload(client, os.urandom(300_000))['blob_hash']
    whole_reads: list[str] = []
    read_blob, require_blob = cas.read_blob, cas.require_blob

    def recording(original):
        def read(digest, *args, **kwargs):
            whole_reads.append(digest)
            return original(digest, *args, **kwargs)
        return read

    monkeypatch.setattr(cas, 'read_blob', recording(read_blob))
    monkeypatch.setattr(cas, 'require_blob', recording(require_blob))
    accepted = _mutate(client, cell_id, path='big.bin', blob_hash=binary, encoding='binary')
    assert accepted.status_code == 200 and accepted.json()['outcome'] == 'accepted', accepted.text
    assert binary not in whole_reads
    files = {f['name']: f for f in _cells(client)[cell_id]['files']}
    assert files['big.bin']['body_hash'] == binary and files['big.bin']['encoding'] == 'binary'


def test_existing_cell_refuses_text_by_hash_above_the_text_threshold_TR07(client, monkeypatch):
    """The layout cache decodes every text body, so a by-hash body may claim
    ``utf-8`` only within the import sniff's text size."""
    import cas
    cell_id = import_cell(client, client.channel_id, [{'name': 'seed.txt', 'content': 'seed'}]).json()['cell_ids'][0]
    monkeypatch.setattr(cas, 'TEXT_IMPORT_MAX_BYTES', 16)
    small = upload(client, b'a' * 16)['blob_hash']
    large = upload(client, b'a' * 17)['blob_hash']
    assert _mutate(client, cell_id, path='small.txt', blob_hash=small, encoding='utf-8').status_code == 200
    refused = _mutate(client, cell_id, path='large.txt', blob_hash=large, encoding='utf-8')
    assert refused.status_code == 400, refused.text
    assert 'large.txt' not in {f['name'] for f in _cells(client)[cell_id]['files']}
