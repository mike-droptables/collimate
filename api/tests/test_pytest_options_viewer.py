"""D13 selected options viewer uses the exact SDK mapping without mutations."""
from test_pytest_merge_split import declaration, snapshot
from test_pytest_options_identity import CHANNEL, client as client, make_stack, seed_version, accept_file


def test_options_viewer_accepted_tombstones_and_literal_paths__D13(client, monkeypatch):
    from cas import read_blob
    import cas
    ch, stack, ids = make_stack([('Source', {
        'pick.options_stack': 'invalid',
        'choice/[literal].txt': 'hello', 'choice/deleted.txt': 'gone',
        '_runs/one/state': 'protected', 'outside.txt': 'kept',
        'choice/image.bin': b'\x00\xff',
    })])
    seed_version(CHANNEL, ids[0])
    accept_file(ids[0], 'pick.options_stack', declaration(files=['[literal].txt', '*.bin']))
    accept_file(ids[0], 'choice/deleted.txt')
    before = snapshot(ch)
    reads = []
    def tracked(digest):
        reads.append(digest)
        return read_blob(digest)
    monkeypatch.setattr(cas, 'read_blob', tracked)
    response = client.get(f'/api/channels/{CHANNEL}/cells/{ids[0]}/options-map', params={'path': 'pick.options_stack'})
    assert response.status_code == 200, response.text
    assert response.json() == {
        'options': [{'name': 'Candidate', 'files': {'[literal].txt': 'choice/[literal].txt', 'image.bin': 'choice/image.bin'}}],
        'kept': ['_runs/one/state', 'outside.txt'], 'retain_source': True,
    }
    from database import get_db_connection
    from persistence import snapshot_files
    with get_db_connection(CHANNEL) as conn:
        selected_hash = next(f['body_hash'] for f in snapshot_files(conn, ids[0])
                             if f['path'] == 'pick.options_stack')
    assert reads == [selected_hash]  # Read only the selected definition, never option bodies.
    assert snapshot(ch) == before


def test_options_viewer_metadata_cache_and_invalid_missing_controls__D13(client):
    from models import VirtualFile
    from database import get_db_connection
    from persistence import snapshot_files
    ch, stack, ids = make_stack([('Source', {'pick.options_stack': declaration(), 'choice/a.txt': 'A'})])
    seed_version(CHANNEL, ids[0])
    with get_db_connection(CHANNEL) as conn:
        stack.cells[0].files = [VirtualFile(name=f['path'], body_hash=f['body_hash'],
            file_revision=f['file_revision'], encoding=f['encoding'], language=f['language'])
            for f in snapshot_files(conn, ids[0])]
    base = f'/api/channels/{CHANNEL}/cells/{ids[0]}/options-map'
    assert client.get(base, params={'path': 'pick.options_stack'}).json()['options'][0]['files'] == {'a.txt': 'choice/a.txt'}
    accept_file(ids[0], 'pick.options_stack', 'options: []')
    before = snapshot(ch)
    assert client.get(base, params={'path': 'pick.options_stack'}).status_code == 409
    assert client.get(base, params={'path': 'missing.options_stack'}).status_code == 404
    assert client.get(base, params={'path': 'pick.options_stack', 'version_id': ids[0]}).status_code == 200
    assert client.get(base, params={'path': 'pick.options_stack', 'version_id': 'missing'}).status_code == 404
    assert snapshot(ch) == before
