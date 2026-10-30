"""Accepted shipped renderer handler reaches real native compilation and QuickJS."""
import time
import uuid

import pytest

from import_cells import import_cell



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


def _accept(channel, cell):
    from persistence import initialize_files
    from database import get_db_connection
    with get_db_connection(str(channel.id)) as conn:
        initialize_files(conn, str(cell.id), cell.files)


def _put(client, base, path, text, revision=1):
    response = client.post(base + '/files/mutate', json={
        'operation_id': str(uuid.uuid4()), 'mutations': [{
            'path': path, 'content_text': text, 'base_revision': revision,
            'writer_id': 'verification-test', 'edit_seq': 1, 'kind': 'put',
        }],
    })
    assert response.status_code == 200 and response.json()['outcome'] == 'accepted', response.text
    return response.json()

def test_actual_shipped_renderer_handler_catalog_to_native_verdict_without_node__D09_D10_D12_F1280_F1984(api_app, monkeypatch):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        registered = client.post('/api/channels', params={'name': 'Native verifier'},
                                 json={'suggested': {'id': 'standard', 'tutorial': False}})
        assert registered.status_code == 200, registered.text
        channel_id = registered.json()['id']
        entries = client.get('/api/handlers').json()
        assert next(row for row in entries if row['id'] == 'renderer')['accepted'] is True
        created = import_cell(client, channel_id, name='Authored package', files=[
                {'name': 'custom/sample.renderer', 'content': 'id: sample\nname: Sample\nsummary: Test renderer\nentrypoint: main.tsx\nembed: native\nextensions: [.sample]\n'},
                {'name': 'custom/main.tsx', 'content': "import {defineRenderer} from '/api/sdk/react.js'; import {value} from './sibling'; export default defineRenderer({component:()=>value});"},
                {'name': 'custom/sibling.ts', 'content': "export const value='native verifier';"},
            ])
        assert created.status_code == 200, created.text
        cell_id = created.json()['cell_ids'][0]
        url = f'/api/channels/{channel_id}/cells/{cell_id}/validate'
        monkeypatch.setenv('PATH', '')
        valid = client.post(url, json={'path': 'custom/sample.renderer'})
        assert valid.status_code == 200
        assert valid.json() == {'valid': True}, valid.text
        # The same route reads the accepted current source, not its history checkpoint.
        _put(client, f'/api/channels/{channel_id}/cells/{cell_id}', 'custom/main.tsx',
             "import '/etc/passwd'; export default {mount(){}};")
        invalid = client.post(url, json={'path': 'custom/sample.renderer'})
        assert invalid.json()['valid'] is False
        assert 'import' in invalid.json()['error'].lower()


def test_verifier_input_package_scope_metadata_and_current_paths__D10__P6F01():
    from verification import verifier_input
    value = verifier_input('pkg/choice.renderer', {
        'pkg/choice.renderer': b'id: x', 'pkg/main.tsx': b'code',
        'pkg/image.png': b'\x00\xff', 'pkg/large.txt': b'x' * 1_000_001,
        'outside.txt': b'not exposed',
    }, ['pkg/choice.renderer', 'pkg/main.tsx', 'pkg/image.png', 'pkg/large.txt', 'outside.txt'])
    assert value['package']['main.tsx'] == 'code'
    assert value['package']['image.png']['size'] == 2
    assert value['package']['large.txt']['size'] == 1_000_001
    assert 'outside.txt' not in value['package']
    assert 'outside.txt' in value['cell_paths']
    root = verifier_input('root.txt', {'root.txt': b'x', 'folder/other.txt': b'y'}, ['root.txt', 'folder/other.txt'])
    assert root['package'] == {'root.txt': 'x'}
    assert verifier_input('_runs/a/status.panel', {'_runs/a/status.panel': b'{}'}, ['_runs/a/status.panel'])['text'] == '{}'


def test_a_handler_examples_root_file_sees_the_whole_example_as_its_package():
    """A handler example's folder is itself the package folder: its root
    definition sees the nested files (its own examples), relative to it,
    while a nested package inside the example still sees only its subtree."""
    from verification import verifier_input
    example = {'sample.handler': b'id: s', 'examples/simple/a.note': b'n', 'pkg/x.renderer': b'id: x',
               'pkg/main.tsx': b'code'}
    root = verifier_input('sample.handler', example, example, root_is_package=True)
    assert root['package'] == {'sample.handler': 'id: s', 'examples/simple/a.note': 'n',
                               'pkg/x.renderer': 'id: x', 'pkg/main.tsx': 'code'}
    nested = verifier_input('pkg/x.renderer', example, example, root_is_package=True)
    assert nested['package'] == {'x.renderer': 'id: x', 'main.tsx': 'code'}


def test_plain_type_bypasses_source_reads_and_worker__D10(client, monkeypatch):
    from verification import validate_file
    from state import find_active_channel
    from models import Cell, Stack, VirtualFile
    channel = find_active_channel()
    cell = Cell(files=[VirtualFile(name='plain.unknown', content='plain')])
    channel.main_stacks.append(Stack(cells=[cell]))
    _accept(channel, cell)
    monkeypatch.setattr('verification._cell_files', lambda *_: pytest.fail('plain path reads package'))
    monkeypatch.setattr('verification.get_verifier_pool', lambda: pytest.fail('plain path starts worker'))
    assert validate_file(channel, str(cell.id), 'plain.unknown') == {'valid': True}
    assert validate_file(channel, str(cell.id), 'missing.unknown')['valid'] is False
    assert validate_file(channel, 'missing-cell', 'plain.unknown')['valid'] is False


def test_verifier_input_preserves_canonical_colon_paths__D10_F1280():
    from verification import verifier_input
    path = 'pkg/view:main.tsx'
    assert verifier_input(path, {path: b'hello'}, [path])['package'] == {'view:main.tsx': 'hello'}
    with pytest.raises(ValueError):
        verifier_input('C:/escape.tsx', {'C:/escape.tsx': b'no'}, [])


def test_warm_verdict_cache_respects_preparation_deadline__D10(monkeypatch):
    import verification
    import hashlib
    import json
    value = {'path': 'input.x'}
    bundle_hash = 'deadline-test'
    key = hashlib.sha256(json.dumps([bundle_hash, value], sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    monkeypatch.setitem(verification._verdicts, key, {'valid': True})
    now = [100.0]
    monkeypatch.setattr(verification.time, 'monotonic', lambda: now[0])
    def compile_after_budget(*args, **kwargs):
        now[0] = 101.0
        return bundle_hash, 'unused cached code'
    monkeypatch.setattr(verification, 'compiled_verifier', compile_after_budget)
    monkeypatch.setattr(verification, 'get_verifier_pool', lambda: pytest.fail('expired preparation starts worker'))
    assert verification.verify_source(b'unused', value, deadline=100.5) == {'valid': False, 'error': 'verifier budget exceeded'}


def test_real_verdict_cache_includes_package_and_cell_paths__D10(fresh_cwd, monkeypatch):
    import verification
    from verifier_pool import close_verifier_pool
    source = b'export default input => ({valid:input.package["sibling"]==="good" && input.cell_paths.includes("required") && input.previous_text === "stored"});'
    value = {'path': 'input.x', 'text': 'same', 'package': {'sibling': 'good'}, 'cell_paths': ['required'], 'previous_text': 'stored'}
    try:
        assert verification.verify_source(source, value) == {'valid': True}
        original = verification.get_verifier_pool
        monkeypatch.setattr(verification, 'get_verifier_pool', lambda: pytest.fail('cached verdict reruns worker'))
        assert verification.verify_source(source, value) == {'valid': True}
        monkeypatch.setattr(verification, 'get_verifier_pool', original)
        assert verification.verify_source(source, {**value, 'cell_paths': []})['valid'] is False
        assert verification.verify_source(source, {**value, 'package': {'sibling': 'bad'}})['valid'] is False
        assert verification.verify_source(source, {**value, 'previous_text': 'changed baseline'})['valid'] is False
        assert verification.verify_source(source, value, deadline=time.monotonic()-1)['valid'] is False
    finally:
        close_verifier_pool()


def test_verification_reads_only_selected_package_and_large_cas_metadata__D10(client, monkeypatch):
    from cas import store_bytes
    from models import Cell, Stack, VirtualFile
    from state import find_active_channel
    from verification import _cell_files
    import cas
    channel = find_active_channel()
    large = store_bytes(b'x' * 1_000_001)
    outside = store_bytes(b'unrelated private content')
    cell = Cell(files=[VirtualFile(name='pkg/definition.x', content='definition'),
                       VirtualFile(name='pkg/large.bin', blob_hash=large),
                       VirtualFile(name='outside.bin', blob_hash=outside)])
    channel.main_stacks.append(Stack(cells=[cell]))
    _accept(channel, cell)
    actual_read = cas.read_blob
    reads = []
    def selected_read(digest):
        assert digest not in (large, outside), 'unneeded whole CAS read'
        reads.append(digest)
        return actual_read(digest)
    monkeypatch.setattr(cas, 'read_blob', selected_read)
    values, paths, previous = _cell_files(channel, str(cell.id), 'pkg/definition.x', None)
    assert values == {'pkg/definition.x': b'definition', 'pkg/large.bin': {'size': 1_000_001, 'hash': large}}
    assert paths == ['outside.bin', 'pkg/definition.x', 'pkg/large.bin']
    assert previous == 'definition'
    assert reads


def test_user_and_workspace_validation_receive_accepted_previous_content__D10_D12_P4F11(client, monkeypatch):
    from state import find_active_channel
    from verification import validate_file
    channel = find_active_channel()
    created = import_cell(client, channel.id, name='Stored baseline', files=[
        {'name': 'pkg/document.check', 'content': 'checkpoint document'},
        {'name': 'pkg/verify.ts', 'content': 'export default x=>({valid:true})'},
    ])
    assert created.status_code == 200, created.text
    cell_id = created.json()['cell_ids'][0]
    base = f'/api/channels/{channel.id}/cells/{cell_id}'
    _put(client, base, 'pkg/document.check', 'accepted current document')
    values = []
    from verification import verify_source as actual_verify
    def capture(source, value, *, deadline=None):
        if value['path'].endswith('.check'):
            values.append(value)
            return {'valid': True}
        return actual_verify(source, value, deadline=deadline)
    monkeypatch.setattr('verification.verify_source', capture)
    assert client.post(base+'/validate', json={'path':'pkg/document.check','verifier':'pkg/verify.ts'}).json() == {'valid':True}
    assert values[-1]['text'] == 'accepted current document'
    assert values[-1]['previous_text'] == 'accepted current document'
    assert validate_file(find_active_channel(), cell_id, 'pkg/document.check', verifier='pkg/verify.ts',
                         workspace_files={'pkg/document.check':b'workspace edit','pkg/verify.ts':b'export default x=>({valid:true})'}) == {'valid':True}
    assert values[-1]['text'] == 'workspace edit'
    assert values[-1]['previous_text'] == 'accepted current document'
    _put(client, base, 'pkg/new.check', 'brand new', revision=0)
    assert client.post(base+'/validate', json={'path':'pkg/new.check','verifier':'pkg/verify.ts'}).json() == {'valid':True}
    assert values[-1]['previous_text'] == 'brand new'
    deleted = client.post(base + '/files/mutate', json={
        'operation_id': str(uuid.uuid4()), 'mutations': [{
            'path': 'pkg/document.check', 'kind': 'delete', 'base_revision': 2,
            'writer_id': 'verification-test', 'edit_seq': 2,
        }],
    })
    assert deleted.status_code == 200 and deleted.json()['outcome'] == 'accepted', deleted.text
    assert validate_file(find_active_channel(), cell_id, 'pkg/document.check', verifier='pkg/verify.ts',
                         workspace_files={'pkg/document.check': b'recreated candidate',
                                          'pkg/verify.ts': b'export default x=>({valid:true})'}) == {'valid': True}
    assert not {'previous_text', 'previous_bytes'} & values[-1].keys()


def test_previous_binary_and_large_content_are_bounded_metadata__D10_D12_P4F11(fresh_cwd, monkeypatch):
    import cas
    from verification import _previous_content
    binary = b'\x00binary'
    digest = cas.store_bytes(binary)
    assert _previous_content(digest) == {'size':len(binary),'hash':digest}
    large = cas.store_bytes(b'x' * 1_000_001)
    monkeypatch.setattr(cas, 'read_blob', lambda *_: pytest.fail('large baseline body read'))
    assert _previous_content(large) == {'size':1_000_001,'hash':large}
    assert _previous_content(None) is None
