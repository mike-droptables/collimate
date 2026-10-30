"""Handler IPC binds catalog and verdicts to the run's safe current workspace."""
import asyncio
import json
import sys
import uuid
from collections import deque
from types import SimpleNamespace
import pytest
from process_manager.ipc import IPCMixin, REQUEST_OPS, OP_CAPABILITY
from runner_permissions import RunnerPermissions

class Writer:
    def __init__(self): self.lines = []
    def write(self, value): self.lines.append(json.loads(value))
    async def drain(self): pass

def manager(tmp_path):
    m = IPCMixin()
    m.channel, m.cell_id, m.label = str(uuid.uuid5(uuid.NAMESPACE_URL, 'handler-runtime')), 'own-cell', 'own-run'
    m.workspace_dir = str(tmp_path)
    m.permissions = RunnerPermissions.from_definition({})
    m.log_buffer, m._ipc_writer = deque(), Writer()
    return m

def test_handler_ops_correlated_ungated_bind_own_catalog__D09(tmp_path, monkeypatch):
    import state
    m, channel, calls = manager(tmp_path), object(), []
    monkeypatch.setattr(state, 'get_channel_by_id', lambda identity: channel if identity == m.channel else None)
    catalog = SimpleNamespace(public=lambda: [{'id':'own', 'name':'Own', 'description':'', 'types':[{'ext':'.txt'}]}], skills=lambda ext: {'own--edit/SKILL.md':ext})
    monkeypatch.setitem(sys.modules, 'handlers', SimpleNamespace(catalog=lambda selected: calls.append(selected) or catalog))
    for op in ('list_handlers', 'handler_skills'):
        assert op in REQUEST_OPS and op not in OP_CAPABILITY
        asyncio.run(m._handle_ipc(json.dumps({'op':op, 'req_id':op, 'ext_or_id':'.txt', 'channel':'foreign'})))
    assert calls == [channel, channel]
    assert m._ipc_writer.lines[0]['handlers'][0]['id'] == 'own'
    assert m._ipc_writer.lines[1]['skills'] == {'own--edit/SKILL.md':'.txt'}
    assert [f['req_id'] for f in m._ipc_writer.lines] == ['list_handlers','handler_skills']

def test_validation_current_package_and_bound_workspace__D09(tmp_path, monkeypatch):
    import state
    m, channel, calls = manager(tmp_path), object(), []
    (tmp_path/'pkg').mkdir()
    (tmp_path/'pkg/main.renderer').write_text('entrypoint: main.tsx')
    (tmp_path/'pkg/main.tsx').write_bytes(b'CURRENT sibling\r\n')
    (tmp_path/'other.txt').write_text('not selected')
    monkeypatch.setattr(state, 'get_channel_by_id', lambda _:channel)
    def validate(selected, cell_id, path, **kwargs):
        calls.append((selected, cell_id, path, kwargs))
        return {'valid':False,'error':'real verdict'}
    monkeypatch.setitem(sys.modules, 'verification', SimpleNamespace(validate_file=validate))
    monkeypatch.setitem(sys.modules, 'handlers', SimpleNamespace(catalog=lambda _:SimpleNamespace(for_path=lambda _:SimpleNamespace(accepted_files={'verify.ts':b'source'}))))
    assert 'validate_file' in REQUEST_OPS and 'validate_file' not in OP_CAPABILITY
    asyncio.run(m._handle_ipc(json.dumps({'op':'validate_file', 'req_id':'verify','path':'pkg/main.renderer','cell_id':'foreign'})))
    selected, cell_id, path, kwargs = calls[0]
    assert (selected, cell_id, path) == (channel,'own-cell','pkg/main.renderer')
    assert kwargs['workspace_files']['pkg/main.tsx'] == b'CURRENT sibling\r\n'
    assert 'other.txt' not in kwargs['workspace_files']
    assert kwargs['workspace_paths'] == ['other.txt','pkg/main.renderer','pkg/main.tsx']
    assert m._ipc_writer.lines[-1]['result'] == {'valid':False,'error':'real verdict'}

@pytest.mark.parametrize('path',['../secret.txt','/secret.txt','escape.txt','fifo'])
def test_validation_refuses_unsafe_workspace_before_verifier__D09(tmp_path, monkeypatch, path):
    import os
    m = manager(tmp_path)
    secret = tmp_path.parent/'secret.txt'
    secret.write_text('secret')
    (tmp_path/'escape.txt').symlink_to(secret)
    os.mkfifo(tmp_path/'fifo')
    called=[]
    monkeypatch.setitem(sys.modules,'verification',SimpleNamespace(validate_file=lambda *a,**k:called.append(True)))
    asyncio.run(m._handle_ipc(json.dumps({'op':'validate_file','req_id':'refuse','path':path})))
    assert called == []
    assert m._ipc_writer.lines[-1]['error']


def test_validation_no_verifier_skips_package_walk__D09(tmp_path, monkeypatch):
    import state
    import handler_runtime
    import verification
    m = manager(tmp_path)
    (tmp_path/'plain.txt').write_text('plain')
    monkeypatch.setattr(state, 'get_channel_by_id', lambda _:object())
    monkeypatch.setitem(sys.modules, 'handlers', SimpleNamespace(catalog=lambda _:SimpleNamespace(for_path=lambda _:None)))
    monkeypatch.setattr(handler_runtime, 'workspace_snapshot', lambda *a:pytest.fail('plain type walked package'))
    monkeypatch.setattr(verification, 'get_verifier_pool', lambda:pytest.fail('plain type started worker'))
    asyncio.run(m._handle_ipc(json.dumps({'op':'validate_file','req_id':'plain','path':'plain.txt'})))
    assert m._ipc_writer.lines[-1]['result'] == {'valid':True}


def test_validation_large_binary_metadata_and_explicit_verifier_bytes__D09(tmp_path):
    import hashlib
    from handler_runtime import workspace_snapshot
    m = manager(tmp_path)
    body = b'\xff' * 1_000_001
    (tmp_path/'large.dat').write_bytes(body)
    (tmp_path/'verify.ts').write_bytes(b'export default () => ({valid:true})')
    _, _, files, paths = workspace_snapshot(m, 'large.dat', 'verify.ts')
    assert files['large.dat'] == {'size':len(body), 'hash':hashlib.sha256(body).hexdigest()}
    assert files['verify.ts'] == b'export default () => ({valid:true})'
    assert paths == ['large.dat','verify.ts']


def test_ipc_current_sibling_verdict_uses_actual_verifier_pool__D09_D22(tmp_path, monkeypatch):
    """Compiler output is a fixture; evaluation and IPC path are production."""
    import hashlib
    import state
    import verification
    from verifier_pool import VerifierPool
    m = manager(tmp_path)
    (tmp_path/'pkg').mkdir()
    (tmp_path/'pkg/main.renderer').write_text('entrypoint: main.tsx')
    (tmp_path/'pkg/main.tsx').write_text('bad current sibling')
    monkeypatch.setattr(state, 'get_channel_by_id', lambda _:object())
    record = SimpleNamespace(accepted_files={'verify.ts':b'accepted source'},
                             manifest={'types': [{'ext': '.renderer'}]})
    monkeypatch.setitem(sys.modules, 'handlers', SimpleNamespace(catalog=lambda _:SimpleNamespace(for_path=lambda _:record)))
    code = 'var __collimate_verifier={default: input => ({valid:input.package["main.tsx"] === "good current sibling",error: "sibling verdict"})}'
    # This IPC/engine fixture has no persisted cell; baseline storage is tested
    # separately through the real user route and workspace validation.
    monkeypatch.setattr(verification, '_workspace_previous', lambda *args: None)
    monkeypatch.setattr(verification, 'compiled_verifier', lambda source, **k:(hashlib.sha256(code.encode()).hexdigest(),code))
    with VerifierPool(root=tmp_path/'workers',cap=1) as pool:
        monkeypatch.setattr(verification,'get_verifier_pool',lambda:pool)
        for text, valid in [('bad current sibling',False),('good current sibling',True)]:
            (tmp_path/'pkg/main.tsx').write_text(text)
            asyncio.run(m._handle_ipc(json.dumps({'op':'validate_file','req_id':text,'path':'pkg/main.renderer'})))
            response = m._ipc_writer.lines[-1]
            assert 'result' in response, response
            assert response['result']['valid'] is valid
        assert pool.worker_count == 1


def test_unselected_directory_swap_never_exposes_outside_names__D09(tmp_path, monkeypatch):
    import os
    from handler_runtime import workspace_snapshot
    root, outside = tmp_path/'workspace', tmp_path/'outside'
    root.mkdir()
    outside.mkdir()
    (root/'target').mkdir()
    (root/'target/document.txt').write_text('own')
    (root/'other').mkdir()
    (root/'other/local.txt').write_text('local')
    (outside/'outside-secret-name.txt').write_text('private')
    m = manager(root)
    original = os.scandir
    swapped = False
    def raced_scandir(path):
        nonlocal swapped
        actual = os.readlink(f'/proc/self/fd/{path}') if isinstance(path, int) else os.fspath(path)
        if actual == str(root/'other') and not swapped:
            swapped = True
            (root/'other').rename(root/'parked')
            (root/'other').symlink_to(outside, target_is_directory=True)
        return original(path)
    monkeypatch.setattr(os, 'scandir', raced_scandir)
    _, _, files, paths = workspace_snapshot(m, 'target/document.txt')
    assert swapped
    assert files == {'target/document.txt':b'own'}
    assert 'other/outside-secret-name.txt' not in paths


def test_workspace_metadata_preserves_own_run_binding__D09(tmp_path):
    from handler_runtime import workspace_snapshot
    m = manager(tmp_path)
    own, foreign = tmp_path/'_runs/own-run', tmp_path/'_runs/foreign'
    own.mkdir(parents=True)
    foreign.mkdir()
    (own/'state.panel').write_text('own state')
    (foreign/'private.txt').write_text('foreign state')
    _, _, files, paths = workspace_snapshot(m, '_runs/own-run/state.panel')
    assert files == {'_runs/own-run/state.panel':b'own state'}
    assert paths == ['_runs/own-run/state.panel']
