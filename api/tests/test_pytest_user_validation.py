"""Accepted user mutations trigger coalesced advisory validation."""
import threading
import time
import uuid

import pytest


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state
    from database import get_db_connection
    with TestClient(api_app) as http:
        channel = Channel(name='Starting')
        from genesis import ensure_capabilities_stack
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        yield http


def _cell(files=None):
    from models import Cell, Stack, VirtualFile
    from state import find_active_channel
    from test_pytest_handlers import install_handler
    from persistence import initialize_files
    from database import get_db_connection
    ch = find_active_channel()
    cell = Cell(files=[VirtualFile(name=path, content=body) for path, body in
                       (files or {'report.review_doc': 'initial'}).items()])
    ch.main_stacks.append(Stack(cells=[cell]))
    handler, _, _ = install_handler(ch, identity='review-document', ext='.review_doc')
    conn = get_db_connection(str(ch.id))
    try:
        initialize_files(conn, str(cell.id), cell.files)
        initialize_files(conn, str(handler.id), handler.files)
        conn.commit()
    finally:
        conn.close()
    return ch, cell, handler


def _mutate(client, cell, body=None, path='report.review_doc', *, delete=False):
    from database import get_db_connection
    from persistence import current_file
    from state import find_active_channel
    channel = str(find_active_channel().id)
    conn = get_db_connection(channel)
    try:
        current = current_file(conn, str(cell.id), path)
    finally:
        conn.close()
    mutation = dict(path=path, writer_id='test-user', edit_seq=(current['revision'] + 1 if current else 1),
                    base_revision=current['revision'] if current else 0, kind='delete' if delete else 'put')
    if not delete:
        mutation['content_text'] = body
    return client.post(f'/api/channels/{channel}/cells/{cell.id}/files/mutate',
                       json=dict(operation_id=str(uuid.uuid4()), mutations=[mutation], resolves=[]))


def _base(ch, cell):
    return f'/api/channels/{ch.id}/cells/{cell.id}'


def _wait(predicate):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('Background validation did not finish')


def _result(client, base):
    def ready():
        response = client.get(base + '/validation')
        assert response.status_code == 200, response.text
        value = response.json()
        return value if not value['pending'] else None
    return _wait(ready)


def test_user_mutation_runs_validation_without_blocking_or_refusing_save(client, monkeypatch):
    import verification
    ch, cell, _ = _cell()
    entered, release = threading.Event(), threading.Event()
    def validate(channel, cid, path, **kwargs):
        assert cid == str(cell.id)
        entered.set()
        assert release.wait(8)
        return {'valid': False, 'error': 'synthetic document rejected'}
    monkeypatch.setattr(verification, 'validate_file', validate)
    base = _base(ch, cell)
    try:
        response = _mutate(client, cell, 'invalid document')
        assert response.status_code == 200, response.text
        assert response.json()['outcome'] == 'accepted'
        assert entered.wait(5), 'Accepted mutation never scheduled validation'
        assert client.get(base + '/files/report.review_doc').content == b'invalid document'
        pending = client.get(base + '/validation')
        assert pending.status_code == 200, pending.text
        assert pending.json()['pending'] and pending.json()['verdicts'] == {}
    finally:
        release.set()
    assert _result(client, base)['verdicts'] == {
        'report.review_doc': {'valid': False, 'error': 'synthetic document rejected'}}


def test_same_second_edit_discards_inflight_verdict_and_delete_clears_it(client, monkeypatch):
    import verification
    ch, cell, _ = _cell()
    base = _base(ch, cell)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def validate(channel, cid, path, **kwargs):
        files, _, _ = verification._cell_files(channel, cid, path, None)
        text = files[path].decode()
        calls.append(text)
        if text == 'old':
            entered.set()
            assert release.wait(8)
        return {'valid': text == 'new', **({'error': 'old failure'} if text == 'old' else {})}
    monkeypatch.setattr(verification, 'validate_file', validate)
    try:
        assert _mutate(client, cell, 'old').status_code == 200
        assert entered.wait(5)
        first = client.get(base + '/validation').json()
        assert _mutate(client, cell, 'new').status_code == 200
        second = client.get(base + '/validation').json()
        assert first['source_revision'] != second['source_revision']
        assert second['pending'] and second['verdicts'] == {}
    finally:
        release.set()
    assert _result(client, base)['verdicts'] == {'report.review_doc': {'valid': True}}
    assert 'old' in calls and 'new' in calls
    assert _mutate(client, cell, delete=True).status_code == 200
    assert _result(client, base)['verdicts'] == {}


def test_sibling_mutation_revalidates_dependent_document(client, monkeypatch):
    import verification
    ch, cell, _ = _cell({'pkg/view.review_doc': 'declaration', 'pkg/main.tsx': 'old'})
    calls = []
    def validate(channel, cid, path, **kwargs):
        files, _, _ = verification._cell_files(channel, cid, path, None)
        calls.append(files['pkg/main.tsx'])
        return {'valid': files['pkg/main.tsx'] == b'new'}
    monkeypatch.setattr(verification, 'validate_file', validate)
    response = _mutate(client, cell, 'new', 'pkg/main.tsx')
    assert response.status_code == 200, response.text
    _wait(lambda: b'new' in calls)
    assert _result(client, _base(ch, cell))['verdicts']['pkg/view.review_doc']['valid']


def test_user_checkpoint_has_tip_identity_and_revalidates_after_catalog_change(client, monkeypatch):
    import verification
    from state import persist_state
    from database import get_db_connection
    from versions import find_tip
    ch, cell, handler = _cell()
    calls = []
    def validate(channel, cid, path, **kwargs):
        calls.append(path)
        return {'valid': True}
    monkeypatch.setattr(verification, 'validate_file', validate)
    assert _mutate(client, cell, 'committed').status_code == 200
    base = _base(ch, cell)
    assert client.post(base + '/checkpoint').status_code == 200
    conn = get_db_connection(str(ch.id))
    try:
        version = find_tip(conn, str(cell.id))
    finally:
        conn.close()
    _wait(lambda: calls)
    first = _result(client, base)
    assert first['source_version_id'] == version
    assert first['verdicts']['report.review_doc']['valid']
    from cell_placement import capabilities_stack
    capabilities_stack(ch).cells.remove(handler)
    persist_state()
    second = _result(client, base)
    assert second['source_revision'] != first['source_revision']
    assert second['verdicts'] == {}


def test_accepted_revision_is_transactional_and_plain_files_never_invoke_verifier(client, monkeypatch):
    from database import get_db_connection
    import verification
    ch, cell, _ = _cell({'plain.txt': 'x' * 1000000})
    base = _base(ch, cell)
    def refuse_plain(*args, **kwargs):
        raise AssertionError('Plain cell must not invoke a verifier')
    monkeypatch.setattr(verification, 'validate_file', refuse_plain)
    first = _result(client, base)
    conn = get_db_connection(str(ch.id))
    try:
        conn.execute('DELETE FROM working_files WHERE cell_id=?', (str(cell.id),))
        conn.rollback()
    finally:
        conn.close()
    assert _result(client, base)['source_revision'] == first['source_revision']
    assert _mutate(client, cell, 'kept', 'plain.txt').status_code == 200
    last = _result(client, base)
    assert last['source_revision'] != first['source_revision']
    assert last['verdicts'] == {}


def test_validation_shutdown_joins_active_work_and_drops_results(client, monkeypatch):
    import verification
    import user_validation
    _ch, cell, _ = _cell()
    entered, release = threading.Event(), threading.Event()
    def validate(channel, cid, path, **kwargs):
        entered.set()
        assert release.wait(8)
        return {'valid': True}
    monkeypatch.setattr(verification, 'validate_file', validate)
    service = user_validation._service
    assert _mutate(client, cell, 'pending').status_code == 200
    assert entered.wait(5)
    closing = threading.Thread(target=user_validation.close_user_validation)
    closing.start()
    try:
        _wait(lambda: service.stopping)
        assert closing.is_alive() and service.thread.is_alive()
    finally:
        release.set()
        closing.join(8)
    assert not closing.is_alive() and not service.thread.is_alive()
    assert not service.results


def test_burst_mutations_coalesce_without_read_route(client, monkeypatch):
    import verification
    import user_validation
    ch, cell, _ = _cell()
    monkeypatch.setattr(user_validation, 'DEBOUNCE_SECONDS', 1.0)
    calls = []
    def validate(channel, cid, path, **kwargs):
        files, _, _ = verification._cell_files(channel, cid, path, None)
        calls.append(files[path])
        assert time.monotonic() < kwargs['deadline'] <= time.monotonic() + 30
        return {'valid': True}
    monkeypatch.setattr(verification, 'validate_file', validate)
    for i in range(8):
        response = _mutate(client, cell, f'value {i}')
        assert response.status_code == 200, response.text
    _wait(lambda: b'value 7' in calls)
    assert len(calls) < 8
    assert _result(client, _base(ch, cell))['verdicts']['report.review_doc']['valid']
