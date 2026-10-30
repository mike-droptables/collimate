"""Body-free renderer membership distinguishes accepted current from history."""
import uuid

import pytest

from import_cells import import_cell


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from database import get_db_connection
    from state import state

    with TestClient(api_app) as http:
        channel = Channel(name='Starting')
        from genesis import ensure_capabilities_stack
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        yield http


def test_preview_paths_bind_accepted_current_and_immutable_version__D14(client, monkeypatch):
    from state import get_active_channel
    from database import get_db_connection
    from versions import find_tip

    channel = str(get_active_channel().id)
    created = import_cell(client, channel, [
        {'name': 'pkg/main.tsx', 'content': 'export default {}'},
        {'name': 'removed.txt', 'content': 'old'},
    ])
    assert created.status_code == 200, created.text
    cell = created.json()['cell_ids'][0]
    checkpoint = client.post(f'/api/channels/{channel}/cells/{cell}/checkpoint')
    assert checkpoint.status_code == 200, checkpoint.text
    with get_db_connection(channel) as conn:
        tip = find_tip(conn, cell)
        assert conn.execute('SELECT 1 FROM cells WHERE id=?', (tip,)).fetchone()
    changed = client.post(f'/api/channels/{channel}/cells/{cell}/files/mutate', json={
        'operation_id': str(uuid.uuid4()), 'mutations': [
            {'path': 'added.txt', 'kind': 'put', 'content_text': 'current', 'base_revision': 0,
             'writer_id': 'membership', 'edit_seq': 1},
            {'path': 'removed.txt', 'kind': 'delete', 'base_revision': 1,
             'writer_id': 'membership', 'edit_seq': 1},
        ],
    })
    assert changed.status_code == 200 and changed.json()['outcome'] == 'accepted', changed.text
    import cas

    def no_body(*args, **kwargs):
        raise AssertionError('Membership must not read CAS bodies')

    monkeypatch.setattr(cas, 'read_blob', no_body)
    path = f'/api/channels/{channel}/cells/{cell}/file-paths'
    current = client.get(path)
    assert current.status_code == 200, current.text
    assert current.json()['paths'] == ['added.txt', 'pkg/main.tsx']
    stored = client.get(path, params={'version_id': tip})
    assert stored.status_code == 200, stored.text
    assert stored.json()['paths'] == ['pkg/main.tsx', 'removed.txt']
