"""Source-only host fixture extension; no native/provider/publication claims."""
import ast
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

SOURCE = Path(__file__).resolve().parents[1] / 'tests_live'


def load(name):
    spec = importlib.util.spec_from_file_location('d23_host_' + name.replace('.', '_'), SOURCE / 'fixtures' / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child_runtime(monkeypatch, tmp_path, docker):
    from importlib.resources import files
    sdk = str(files('collimate').resolve().parent)
    workspace = tmp_path / 'workspace'
    private = workspace / '.collrun/forward-synthetic'
    private.mkdir(parents=True)
    (private / 'prompt.md').write_text('private synthetic prompt')
    settings = {'keep': 'base-value', 'overlay': 'child-value', 'marker': 'synthetic',
        'parent_run_id': 'run', 'cell_id': 'cell', 'expected_prompt': 'private synthetic prompt',
        'commands': 'ask', 'expected_mount': str(workspace), 'execution_locus': 'host',
        'expected_sdk': sdk}
    written = {}
    def refused(*args, **kwargs):
        raise RuntimeError('forward_refused')
    async def flush():
        if 'child-invalid.options_stack' in written:
            raise PermissionError('child-invalid.options_stack: handler_invalid')
    gen = SimpleNamespace(config=lambda _: settings,
        run_info=lambda: SimpleNamespace(run_id='run', cell_id='cell', folder='_runs/own'),
        prompt='private synthetic prompt', grant=lambda: SimpleNamespace(commands='ask', docker=docker),
        workspace_path=lambda: str(workspace), list_files=lambda: [], account=refused,
        annotations=SimpleNamespace(available=False), update_file=lambda p, v: written.update({p: v}),
        delete_file=lambda p: written.pop(p), commit=flush, main=lambda _: None)
    monkeypatch.setitem(sys.modules, 'gen', gen)
    monkeypatch.setenv('COLLIMATE_RUN_DIR', str(private))
    monkeypatch.setenv('PYTHONPATH', sdk)
    return gen, settings, written


@pytest.mark.parametrize('docker', ['none', 'full'])
def test_actual_child_fixture_requires_true_host_grant_and_filesystem_sdk_D23(monkeypatch, tmp_path, docker):
    gen, settings, written = child_runtime(monkeypatch, tmp_path, docker)
    child = load('generator_tool_child.py')
    if docker != 'none':
        with pytest.raises(AssertionError):
            asyncio.run(child.run())
        return
    asyncio.run(child.run())
    report = json.loads(written['child-proof.json'])
    assert report['docker'] == 'none' and report['workspace'] == settings['expected_mount']
    assert report['execution_locus'] == 'host' and report['pid'] == os.getpid()
    assert report['sdk_forward'] == str(Path(settings['expected_sdk'], 'collimate/testing/forward.py'))
    assert report['verifier_refused'] is True and report['refused'] == ['account']
    assert report['private_prompt_hash'] == hashlib.sha256(gen.prompt.encode()).hexdigest()


def test_actual_original_host_admission_fails_closed_until_explicit_extension_D23():
    source = ast.parse((SOURCE / 'test_live_generator_tool.py').read_text())
    selected = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == '_observed_package']
    ns = {'yaml': yaml, 'Path': Path, 'hashlib': hashlib, 'json': json, 'FIXTURES': SOURCE / 'fixtures',
        'NATIVE_PINS': {'pi': {}}, 'HOST_NATIVE_PINS': {'claude-code': {}}}
    exec(compile(ast.Module(selected, type_ignores=[]), 'actual-host-admission', 'exec'), ns)
    accepted = {'observed/observed.generator': 'id: observed\nname: Observed\nentrypoint: main.py\n',
        'observed/main.py': '# /// script\n# dependencies = []\n# ///\npass\n'}
    built, proof = ns['_observed_package'](accepted, 'observed/observed.generator', 'own', 'pass', backend='claude-code')
    assert proof['backend'] == 'claude-code' and built[proof['accepted_to_runtime']['observed/main.py']] == accepted['observed/main.py']


@pytest.mark.parametrize('damage', [None, 'auth', 'provider', 'runtime', 'model', 'version', 'adapter', 'locus', 'cwd'])
def test_host_observer_preserves_actual_profile_boundary_and_refuses_foreign_facts_D23(monkeypatch, tmp_path, damage):
    # New observation mechanism has no legacy implementation; this test does
    # not label an absent class as before failure.
    monkeypatch.setitem(sys.modules, 'gen', SimpleNamespace(workspace_path=lambda: str(tmp_path)))
    worker = load('generator_tool_observed_agent.py')
    logs = []
    observer = worker.HostChildObserver(lambda: {'run_id': 'run', 'cell_id': 'cell'},
        lambda p, v, **kw: logs.append((p, json.loads(v))), {'backend': 'claude-code'})
    context = {'auth': 'api_key', 'provider': 'anthropic', 'runtime': 'standard',
        'model': 'claude-haiku-4-5-20251001',
        'host_harness': {'path': '/owned/claude', 'binary': 'claude', 'version': '2.1.281'}}
    profile = SimpleNamespace(locus='host', cwd=str(tmp_path), env_obj=None, adapter_version='0.81.1')
    if damage in ('auth', 'provider', 'runtime', 'model'):
        context[damage] = 'foreign'
    elif damage == 'version':
        context['host_harness']['version'] = '2.1.280'
    elif damage in ('adapter', 'locus', 'cwd'):
        setattr(profile, {'adapter': 'adapter_version'}.get(damage, damage), 'foreign')
    if damage:
        with pytest.raises(AssertionError):
            observer.prepared(context, profile)
        assert logs == []
    else:
        observer.prepared(context, profile)
        assert logs[0][1]['binding']['workspace'] == str(tmp_path)
        assert logs[0][1]['executions'] == []
        assert 'env' not in json.dumps(logs) and 'token' not in json.dumps(logs)


@pytest.mark.parametrize('ending', ['normal', 'error', 'cancel'])
def test_host_wrapper_preserves_own_argv_pid_return_and_restores_on_failure_D23(monkeypatch, tmp_path, ending):
    worker = load('generator_tool_observed_agent.py')
    logs = []
    observer = worker.HostChildObserver(lambda: {'run_id': 'run', 'cell_id': 'cell'},
        lambda p, v, **kw: logs.append(json.loads(v)), {'backend': 'claude-code'})
    observer.binding = {'workspace': str(tmp_path), 'sdk': '/synthetic/sdk'}
    argv = ['uv', 'run', '--no-project', '--with', 'PyYAML==6.0.3', str(tmp_path / 'draft/main.py')]
    child = SimpleNamespace(argv=argv, cwd=str(tmp_path / 'draft'),
        env={'PYTHONPATH': '/synthetic/sdk', 'COLLIMATE_IPC_TOKEN': 'never-retain-this'},
        run_dir=str(tmp_path / '.collrun/forward-own'), host_run_dir=str(tmp_path / '.collrun/forward-own'))
    calls = []
    async def recorded_spawn(*args, **kwargs):
        calls.append((args, kwargs))
        # Recording boundary, not a UV/native spawn. Public stat identity is
        # read for this exact returned test process only.
        return SimpleNamespace(pid=os.getpid())
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', recorded_spawn)
    expected = (0, 'full synthetic child output')
    async def original(actual):
        assert actual is child
        await asyncio.create_subprocess_exec(*actual.argv, cwd=actual.cwd, start_new_session=True)
        if ending == 'error':
            raise ValueError('original failure')
        if ending == 'cancel':
            raise asyncio.CancelledError()
        return expected
    async def run():
        wrapped = observer.wrap(original)
        if ending == 'normal':
            assert await wrapped(child) is expected
        else:
            with pytest.raises(ValueError if ending == 'error' else asyncio.CancelledError):
                await wrapped(child)
    asyncio.run(run())
    assert asyncio.create_subprocess_exec is recorded_spawn
    assert calls == [(tuple(argv), {'cwd': child.cwd, 'start_new_session': True})]
    record = observer.executions[0]
    assert record['spawn']['pid'] == os.getpid() and record['spawn']['start'].isdigit()
    assert record['returned'] is (ending == 'normal')
    assert 'never-retain-this' not in json.dumps(logs) and 'COLLIMATE_IPC_TOKEN' not in json.dumps(logs)




def test_acquisition_source_keeps_native_decoder_dependency_and_original_container_assertions_D23():
    source = (SOURCE / 'test_live_generator_tool.py').read_text()
    assert "backend='claude-code'" in source
    assert 'Acquisition-only Claude host gate needs reviewed native decoder' not in source
    assert 'test_claude_host_generator_publication_and_approval_D23' in source
    assert 'native_gate_passed' in source


def host_consumers():
    source = ast.parse((SOURCE / 'test_live_generator_tool.py').read_text())
    names = {'_host_observation', '_no_host_child', '_host_child_return'}
    nodes = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {'Path': Path, '_json': json.loads, 'PROOF': 'child-proof.json'}
    exec(compile(ast.Module(nodes, type_ignores=[]), 'actual-host-consumers', 'exec'), namespace)
    return namespace


def consumer_runtime(tmp_path):
    from importlib.resources import files
    run, cell = '0123456789abcdef', 'own-cell'
    root = tmp_path / ('ws_' + run[:8] + '_own')
    root.mkdir()
    sdk = str(files('collimate').resolve().parent)
    proof = {'backend': 'claude-code', 'sources': {'accepted/main.py': 'public-sha'}}
    binding = {'locus': 'host', 'adapter_version': '0.81.1',
        'model': 'claude-haiku-4-5-20251001', 'workspace': str(root), 'sdk': sdk,
        'host_harness': {'path': '/selected/claude', 'binary': 'claude', 'version': '2.1.281'}}
    body = {'run_id': run, 'cell_id': cell, 'provenance': proof, 'binding': binding, 'executions': []}
    native = root / '_agent/d23-host/observation.json'
    native.parent.mkdir(parents=True)
    def stage(*args):
        native.write_text(json.dumps(body))
        return json.dumps(body)
    api = SimpleNamespace(wait_until=lambda call, *a, **kw: call(),
        run_log=stage, cell_paths=lambda cell: [])
    return api, run, cell, proof, body, root


@pytest.mark.parametrize('damage', [None, 'run', 'cell', 'backend', 'model', 'adapter', 'version', 'locus', 'workspace', 'sdk'])
def test_actual_host_consumer_requires_own_provenance_workspace_and_source_D23(tmp_path, damage):
    ns = host_consumers()
    api, run, cell, proof, body, root = consumer_runtime(tmp_path)
    if damage in ('run', 'cell'):
        body[damage + '_id'] = 'foreign'
    elif damage == 'backend':
        body['provenance'] = {**proof, 'backend': 'unknown'}
    elif damage == 'version':
        body['binding']['host_harness']['version'] = 'unmeasured'
    elif damage in ('model', 'adapter', 'locus', 'workspace', 'sdk'):
        body['binding'][{'adapter': 'adapter_version'}.get(damage, damage)] = 'foreign'
    if damage:
        with pytest.raises(AssertionError):
            ns['_host_observation'](api, run, cell, proof)
    else:
        assert ns['_host_observation'](api, run, cell, proof) == (body, root)


@pytest.mark.parametrize('damage', [None, 'boundary', 'private', 'physical-proof', 'published-proof'])
def test_actual_host_precard_consumer_refuses_any_child_boundary_or_artifact_D23(tmp_path, damage):
    ns = host_consumers()
    api, run, cell, proof, body, root = consumer_runtime(tmp_path)
    if damage == 'boundary':
        body['executions'].append({'returned': False})
    elif damage == 'private':
        (root / '.collrun/forward-own').mkdir(parents=True)
    elif damage == 'physical-proof':
        (root / 'child-proof.json').write_text('{}')
    elif damage == 'published-proof':
        api.cell_paths = lambda cell: ['child-proof.json']
    if damage:
        with pytest.raises(AssertionError):
            ns['_no_host_child'](api, cell, run, proof)
    else:
        assert ns['_no_host_child'](api, cell, run, proof) == body


@pytest.mark.parametrize('damage', [None, 'not-returned', 'exit-bool', 'foreign-cwd', 'foreign-argv',
    'foreign-sdk', 'private', 'pid-bool', 'birth', 'report-workspace', 'report-pgrp', 'report-birth', 'alive'])
def test_actual_host_return_consumer_correlates_own_uv_group_and_child_report_D23(monkeypatch, tmp_path, damage):
    ns = host_consumers()
    api, run, cell, proof, body, root = consumer_runtime(tmp_path)
    command = {'argv': ['uv', 'run', '--no-project', '--with', 'PyYAML==6.0.3', str(root / 'draft/main.py')],
        'cwd': str(root / 'draft'), 'run_dir': str(root / '.collrun/forward-own'),
        'sdk': body['binding']['sdk'], 'returned': True, 'exit_code': 0,
        'spawn': {'pid': 123456789, 'start': '100'}}
    body['executions'] = [command]
    report = {'workspace': str(root), 'execution_locus': 'host',
        'sdk_forward': str(Path(command['sdk'], 'collimate/testing/forward.py').resolve()),
        'pid': 123456790, 'pgrp': 123456789, 'host_start': '101'}
    # Recorded public stat census only; not a native/UV child launch.
    original_glob = Path.glob
    stat = tmp_path / 'stat'
    stat.write_text('123456790 (synthetic) S 1 123456789 0 0')
    monkeypatch.setattr(Path, 'glob', lambda self, pattern: iter([stat]) if self == Path('/proc') and damage == 'alive'
        else iter([]) if self == Path('/proc') else original_glob(self, pattern))
    if damage == 'not-returned': command['returned'] = False
    elif damage == 'exit-bool': command['exit_code'] = False
    elif damage == 'foreign-cwd': command['cwd'] = '/foreign'
    elif damage == 'foreign-argv': command['argv'][-1] = '/foreign/main.py'
    elif damage == 'foreign-sdk': command['sdk'] = '/foreign'
    elif damage == 'private': Path(command['run_dir']).mkdir(parents=True)
    elif damage == 'pid-bool': command['spawn']['pid'] = True
    elif damage == 'birth': command['spawn']['start'] = 'unknown'
    elif damage == 'report-workspace': report['workspace'] = '/foreign'
    elif damage == 'report-pgrp': report['pgrp'] += 1
    elif damage == 'report-birth': report['host_start'] = None
    if damage:
        with pytest.raises(AssertionError):
            ns['_host_child_return'](api, cell, run, proof, report)
    else:
        assert ns['_host_child_return'](api, cell, run, proof, report) == (body, root)


@pytest.mark.parametrize('damage', [None, 'missing-stat', 'wrong-group', 'duplicate', 'write-error'])
def test_real_host_command_owner_always_reaps_before_observation_gate_D23(monkeypatch, tmp_path, damage):
    import contextlib
    import subprocess
    from importlib.resources import files
    worker = load('generator_tool_observed_agent.py')
    writes = []
    def retain(*args, **kwargs):
        writes.append(args)
        if damage == 'write-error' and len(writes) == 2:
            raise RuntimeError('controlled writer failure')
    observer = worker.HostChildObserver(lambda: {'run_id': 'run', 'cell_id': 'cell'},
        retain, {'backend': 'claude-code'})
    observer.binding = {'workspace': str(tmp_path), 'sdk': '/synthetic/sdk'}
    argv = ['uv', 'run', '--no-project', str(tmp_path / 'draft/main.py')]
    child = SimpleNamespace(argv=argv, cwd=str(tmp_path / 'draft'),
        env={'PYTHONPATH': '/synthetic/sdk'}, run_dir=str(tmp_path / '.collrun/forward-own'),
        host_run_dir=str(tmp_path / '.collrun/forward-own'))
    events = []
    class Output:
        async def read(self, count):
            return b''
    class Process:
        pid = os.getpid()
        stdout = Output()
        async def wait(self):
            events.append('wait')
            return 0
    async def spawn(*args, **kwargs):
        assert args == tuple(argv) and kwargs['cwd'] == child.cwd
        assert kwargs['env'] == child.env
        events.append('spawn')
        return Process()
    async def stop(process):
        assert process.pid == os.getpid()
        events.append('reap')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    native = Path(os.environ.get('COLLIMATE_LIVE_SDK_SOURCE', files('collimate').parent.parent)).parent / 'content/generators/_agent_shared/generator_test.py'
    tree = ast.parse(native.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_host_command')
    ns = {'asyncio': asyncio, 'contextlib': contextlib, 'os': os, 'subprocess': subprocess,
        '_host_environment': lambda child: child.env, '_stop_host': stop, 'OUTPUT_BYTES': 1000}
    exec(compile(ast.Module([node], type_ignores=[]), str(native), 'exec'), ns)
    original_read = Path.read_text
    if damage == 'missing-stat':
        def read(path, *a, **kw):
            if path == Path(f'/proc/{os.getpid()}/stat'):
                raise FileNotFoundError('controlled missing public identity')
            return original_read(path, *a, **kw)
        monkeypatch.setattr(Path, 'read_text', read)
    owner = ns['_host_command']
    if damage == 'wrong-group':
        # The process still reaches the actual owner; observed API mismatch
        # must not steal the process from the owner responsible for cleanup.
        actual_spawn = asyncio.create_subprocess_exec
        async def wrong(*args, **kwargs):
            return await actual_spawn(*args, **kwargs)
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', wrong)
        original_owner = owner
        async def owner(child):
            observed_spawn = asyncio.create_subprocess_exec
            async def altered(*args, **kwargs):
                kwargs['start_new_session'] = False
                return await observed_spawn(*args, **kwargs)
            asyncio.create_subprocess_exec = altered
            try:
                return await original_owner(child)
            finally:
                asyncio.create_subprocess_exec = observed_spawn
    elif damage == 'duplicate':
        actual_owner = owner
        async def owner(child):
            await asyncio.create_subprocess_exec(*child.argv, cwd=child.cwd, env=child.env, start_new_session=True)
            # The actual host owner remains responsible for this controlled
            # recording-only second process boundary.
            return await actual_owner(child)
    async def run():
        wrapped = observer.wrap(owner)
        if damage:
            with pytest.raises(AssertionError):
                await wrapped(child)
        else:
            assert await wrapped(child) == (0, '')
    asyncio.run(run())
    assert events[-1] == 'reap'
    assert 'wait' in events and observer.executions[0]['returned'] is (damage is None)
    assert asyncio.create_subprocess_exec is (wrong if damage == 'wrong-group' else spawn)


@pytest.mark.parametrize('damage', ['stale', 'missing', 'symlink'])
def test_actual_host_consumer_requires_physical_current_observation_D23(tmp_path, damage):
    ns = host_consumers()
    api, run, cell, proof, body, root = consumer_runtime(tmp_path)
    native = root / '_agent/d23-host/observation.json'
    if damage == 'stale':
        native.write_text(json.dumps({**body, 'executions': [{'returned': False}]}))
    elif damage == 'symlink':
        target = tmp_path / 'foreign-observation.json'
        target.write_text(json.dumps(body))
        native.symlink_to(target)
    api.run_log = lambda *args: json.dumps(body)
    with pytest.raises(AssertionError):
        ns['_host_observation'](api, run, cell, proof)


@pytest.mark.parametrize('opt_in,key,selected', [
    ('1', None, False), ('1', '', False), (None, 'synthetic-selected-api', False),
    ('0', 'synthetic-selected-api', False), ('1', 'synthetic-selected-api', True),
])
def test_host_api_admission_precedes_any_fallback_capable_fixture_D23(opt_in, key, selected):
    source = SOURCE / 'test_live_generator_tool.py'
    node = next(n for n in ast.parse(source.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == 'claude_host_runner')
    node.decorator_list = []
    environment = {}
    if opt_in is not None:
        environment['COLLIMATE_LIVE_NATIVE_ACQUISITION'] = opt_in
    if key is not None:
        environment['ANTHROPIC_API_KEY'] = key
    reached = []
    class SelectedFixtureBoundary(Exception):
        pass
    class PrerequisiteRefusal(Exception):
        pass
    def fixture(name):
        reached.append((name, environment.get('ANTHROPIC_API_KEY')))
        raise SelectedFixtureBoundary()
    def skip(message):
        assert message
        raise PrerequisiteRefusal()
    namespace = {'os': SimpleNamespace(environ=environment), 'pytest': SimpleNamespace(skip=skip)}
    exec(compile(ast.Module([node], type_ignores=[]), str(source), 'exec'), namespace)
    with pytest.raises(SelectedFixtureBoundary if selected else PrerequisiteRefusal):
        next(namespace['claude_host_runner'](object(), SimpleNamespace(getfixturevalue=fixture)))
    assert reached == ([('claude_code', key)] if selected else [])
    assert environment.get('ANTHROPIC_API_KEY') == key


def test_host_fixture_selects_exact_account_from_actual_list_endpoint_envelope_D23(monkeypatch):
    """Actual list_accounts and fixture source; only stored rows/next boundary record."""
    from contextlib import nullcontext
    selected = {'id': 'selected-api', 'name': 'Selected', 'service': 'anthropic', 'auth_type': 'api_key'}
    other = {'id': 'other-api', 'name': 'Other', 'service': 'gemini', 'auth_type': 'api_key'}
    # Execute the real endpoint's response construction without reading accounts
    # or keystore state. Its exact accounts wrapper is authoritative here.
    account_source = Path(__file__).resolve().parents[1] / 'accounts.py'
    account_node = next(n for n in ast.parse(account_source.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == 'list_accounts')
    monkeypatch.setitem(sys.modules, 'account_catalog', SimpleNamespace(service_catalog=lambda: []))
    monkeypatch.setitem(sys.modules, 'plan_logins', SimpleNamespace(plan_login_catalog=lambda: []))
    namespace = {'keystore': SimpleNamespace(available=lambda: True), 'CONFIG_PATH': 'controlled-accounts',
        '_locked': nullcontext, '_read': lambda: {'accounts': {'other': other, 'selected': selected}},
        '_public': lambda record, doc: dict(record), 'harness_catalog': lambda: {}}
    exec(compile(ast.Module([account_node], type_ignores=[]), str(account_source), 'exec'), namespace)
    envelope = namespace['list_accounts']()
    assert set(envelope) == {'accounts', 'services', 'plan_logins', 'status'}
    assert envelope['accounts'] == [other, selected]
    fixture_source = SOURCE / 'test_live_generator_tool.py'
    fixture_node = next(n for n in ast.parse(fixture_source.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == 'claude_host_runner')
    fixture_node.decorator_list = []
    class ModelBoundary(Exception):
        pass
    reached = []
    def model_entry(api, owner, name, model, harness):
        reached.append((owner, model, harness))
        raise ModelBoundary()
    def request(method, path):
        assert (method, path) == ('GET', '/api/accounts')
        return envelope
    namespace = {'os': SimpleNamespace(environ={'COLLIMATE_LIVE_NATIVE_ACQUISITION': '1',
        'ANTHROPIC_API_KEY': 'synthetic-selected'}), 'model_entry': model_entry,
        'uuid': SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='controlled'))}
    exec(compile(ast.Module([fixture_node], type_ignores=[]), str(fixture_source), 'exec'), namespace)
    with pytest.raises(ModelBoundary):
        next(namespace['claude_host_runner'](SimpleNamespace(request=request),
            SimpleNamespace(getfixturevalue=lambda name: {'credential_account_id': 'selected-api'})))
    assert reached == [(selected, 'claude-haiku-4-5-20251001', 'claude-code')]


def test_host_fixture_starts_exact_persisted_catalog_revision_D23(monkeypatch):
    """Actual catalog/fixture/start source; recorded storage and HTTP only."""
    import re
    import urllib.parse
    app = Path(__file__).resolve().parents[1]
    fixture_source = SOURCE / 'test_live_generator_tool.py'
    fixture_node = next(n for n in ast.parse(fixture_source.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == 'claude_host_runner')
    fixture_node.decorator_list = []
    catalog_node = next(n for n in ast.parse((app / 'runners.py').read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == 'build_catalog')
    live_node = next(n for n in ast.parse((app / 'tests_live/conftest.py').read_text()).body
        if isinstance(n, ast.ClassDef) and n.name == 'LiveApi')
    start_node = next(n for n in live_node.body if isinstance(n, ast.FunctionDef) and n.name == 'start_runner')
    start_node.returns = None
    for argument in start_node.args.args + start_node.args.kwonlyargs:
        argument.annotation = None
    persisted = {}
    owner = {'id': 'selected-api', 'service': 'anthropic', 'auth_type': 'api_key'}
    cell = SimpleNamespace(id='runner-home', icon=None)
    file = SimpleNamespace(name='saved.runner', content='recorded-definition')
    gi = {'manifest': {}, 'fields': [], 'defaults': {}, 'main_config': 'agent-config.set_yaml'}
    monkeypatch.setitem(sys.modules, 'accounts', SimpleNamespace(AccountError=ValueError,
        resolve_public_accounts=lambda bindings, requirements: dict(bindings)))
    catalog_ns = {'generator_index': lambda cells: {'observed': gi},
        'capabilities_stack_cells': lambda channel: [cell], 'definition_files': lambda c, kind: [(None, file)],
        '_yaml': lambda text, name: dict(persisted), 'SLUG': re.compile(r'[a-z0-9-]+'),
        'RunnerError': RuntimeError, 'runner_files': lambda c, identity: ['persisted-file'],
        'validate_definition': lambda *args: ([], [], None),
        '_account_settings': lambda main, fields, defaults, settings, public: settings,
        'selected_file_gate': lambda manifest, fields, values: False,
        'revision': lambda runner, files: 'persisted-exact-revision'}
    exec(compile(ast.Module([catalog_node], type_ignores=[]), 'actual-runner-catalog', 'exec'), catalog_ns)
    calls = []
    class Api:
        chq = 'owned-channel'
        def __init__(self):
            self.folders = {}
            self.started_runs = []
        def request(self, method, path, body=None):
            calls.append((method, path, body))
            if (method, path) == ('GET', '/api/accounts'):
                return {'accounts': [owner]}
            if path == '/api/runner_generators/observed/configs/default':
                return {'runner': {'settings': {'agent-config.set_yaml': {}}, 'prompt': 'template'},
                    'files': ['persisted-file']}
            if (method, path) == ('POST', '/api/runners'):
                persisted.update(body['runner'])
                assert 'revision' not in persisted
                return {'runner': dict(persisted), 'revision': 'persisted-exact-revision',
                    'files': body['files'], 'cell_id': cell.id, 'generator': {}}
            if (method, path) == ('GET', '/api/runners'):
                return catalog_ns['build_catalog']('owned-channel')
            if path == '/api/channels/owned-channel/runs':
                assert body == {'runner': persisted['id'], 'runner_revision': 'persisted-exact-revision',
                    'cell_id': 'owned-cell'}
                return {'run_id': 'recorded-run', 'folder': '_runs/recorded'}
            assert method == 'DELETE' and path == '/api/runners/' + persisted['id']
        def stop_started_runs(self):
            calls.append(('cleanup', tuple(self.started_runs)))
    namespace = {'os': SimpleNamespace(environ={'COLLIMATE_LIVE_NATIVE_ACQUISITION': '1',
        'ANTHROPIC_API_KEY': 'synthetic-selected'}), 'model_entry': lambda *args: {'id': 'selected-model'},
        '_publish_observed_agent': lambda api, harness: ('observed', {'backend': harness}),
        'uuid': SimpleNamespace(uuid4=lambda: SimpleNamespace(hex='controlled')),
        'HOST_NATIVE_PINS': {'claude-code': {'claude-code': '2.1.281', 'claude-agent-acp': '0.81.1'}},
        'urllib': urllib, 'delete_accounts': lambda api, identity: calls.append(('account-cleanup', identity))}
    exec(compile(ast.Module([fixture_node, start_node], type_ignores=[]), str(fixture_source), 'exec'), namespace)
    api = Api()
    fixture = namespace['claude_host_runner'](api,
        SimpleNamespace(getfixturevalue=lambda name: {'credential_account_id': owner['id']}))
    create = next(fixture)
    try:
        runner, provenance = create('ask')
        assert runner['accounts'] == {'model': 'selected-model'}
        assert runner['permissions'] == {'docker': 'none', 'internet': 'full', 'commands': 'ask'}
        assert provenance == {'backend': 'claude-code'}
        assert namespace['start_runner'](api, 'owned-cell', runner) == 'recorded-run'
        assert api.started_runs == ['recorded-run'] and api.folders == {'recorded-run': '_runs/recorded'}
    finally:
        fixture.close()
    assert ('DELETE', '/api/runners/' + persisted['id'], None) in calls
    assert ('account-cleanup', 'selected-model') in calls


def claude_app_consumers():
    source = SOURCE / 'test_live_generator_tool.py'
    names = {'_native_tool_value', '_tool_call', '_generator_tool_prompt', '_json'}
    nodes = [node for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {'json': json, 'ast': ast, 'CHILD_PATH': 'draft/probe.generator'}
    exec(compile(ast.Module(nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('damage', [None, 'wrong-id', 'missing-update', 'wrong-title',
    'wrong-status', 'changed-trace-input', 'wrong-wrapper'])
def test_claude_app_measured_diagnostic_and_every_native_sse_update_agree(damage):
    import copy
    # Synthetic controlled values follow the retained actual APP wrapper/SSE;
    # no model/provider, observer or public host execution is substituted.
    namespace = claude_app_consumers()
    settings = {'opaque': {'nested': [True, 2]}, 'expected_sdk': '/exact/sdk/src'}
    prompt = 'synthetic-private'
    arguments = {'path': 'draft/probe.generator', 'settings': settings, 'prompt': prompt}
    value = {'exit_code': 0, 'output': 'full child output', 'result': {'success': True,
        'refused': {'op': 'surface_register', 'reason': 'forward_refused'}, 'error_message': ''}}
    identity, title = 'toolu_' + 'a' * 24, 'mcp__collimate__test_generator'
    updates = [{'toolCallId': identity, 'title': title, 'status': 'pending'},
        {'toolCallId': identity, 'title': title}, {'toolCallId': identity},
        {'toolCallId': identity, 'status': 'completed'}]
    proof = {'records': [{'message': {'method': 'session/update', 'params': {'update': row}}}
        for row in updates], 'tool_call_id': identity, 'title': title, 'ancillary_calls': []}
    class RecordedCapture:
        def completed_generator_value(self, backend, *, expected_input):
            assert backend == 'claude-code' and expected_input == arguments
            assert self.records == ['owned-complete-records'] and self.permission_records == ['owned-permission']
            return proof, copy.deepcopy(value)
    namespace['_capture_module'] = lambda: (SimpleNamespace(Capture=RecordedCapture), '')
    frames = [{'t': 'tool', 'id': identity, 'name': title, 'status': state}
        for state in ('running', 'running', 'running', 'done')]
    frames[-2]['input'] = copy.deepcopy(arguments)
    trace = [{'type': 'tool_use', 'tool': title, 'input': {}},
        {'type': 'tool_result', 'output': json.dumps({'result': value})}]
    if damage == 'wrong-id': frames[1]['id'] = 'foreign'
    elif damage == 'missing-update': frames.pop(1)
    elif damage == 'wrong-title': frames[1]['name'] = 'foreign'
    elif damage == 'wrong-status': frames[-1]['status'] = 'error'
    elif damage == 'changed-trace-input': trace[0]['input'] = {'settings': {'expected_sdk': '/guessed/sdk/src'}}
    elif damage == 'wrong-wrapper': trace[-1]['output'] = json.dumps({'result': value, 'extra': True})
    envelope = {'run_id': 'own-run', 'cell_id': 'own-cell', 'backend': 'claude-code', 'ordinal': 1,
        'outcome': 'normal', 'provenance': {'own': True}, 'records': ['owned-complete-records'],
        'permission_records': ['owned-permission']}
    def consume():
        diagnostic = namespace['_tool_call'](frames, settings, prompt, trace, 1)
        native = namespace['_native_tool_value'](frames, settings, prompt, envelope, run='own-run',
            cell='own-cell', backend='claude-code', ordinal=1, provenance={'own': True})
        assert json.dumps(diagnostic, sort_keys=True) == json.dumps(native, sort_keys=True)
        return native
    if damage:
        with pytest.raises(AssertionError): consume()
    else:
        assert consume() == value


def test_claude_app_opaque_prompt_preserves_every_input_byte_and_other_harness_prompts():
    namespace = claude_app_consumers()
    settings = {'expected_sdk': '/exact/sdk/src', 'nested': {'bool': True, 'array': [2, None]}}
    prompt = namespace['_generator_tool_prompt']('claude-code', settings, 'synthetic-private')
    assert json.dumps({'path': 'draft/probe.generator', 'settings': settings, 'prompt': 'synthetic-private'}) in prompt
    assert 'opaque exact object' in prompt and 'omit every unlisted key' in prompt
    assert 'Copy every string value verbatim' in prompt and 'do not correct, rewrite' in prompt
    for backend in ('pydantic-ai', 'goose', 'pi', 'cline', 'codex', 'qwen-code'):
        assert 'opaque exact object' not in namespace['_generator_tool_prompt'](backend, settings, 'synthetic-private')


@pytest.mark.parametrize('failure', [None, 'artifact', 'wire-close', 'stop-run', 'wait-finished'])
def test_claude_app_acceptance_receipt_requires_successful_artifacts_and_cleanup(failure):
    source = SOURCE / 'test_live_generator_tool.py'
    function = next(node for node in ast.parse(source.read_text()).body if isinstance(node, ast.FunctionDef)
        and node.name in ('test_claude_host_generator_publication_and_approval_D23',
                         'test_claude_host_generator_publication_acquisition_only_D23'))
    outer = next(node for node in function.body if isinstance(node, ast.Try))
    # Execute its actual finally and trailing receipt, after the reviewed
    # publication/native body. Recording fault injection tests finalization only.
    statements = [ast.Try(body=[ast.Pass()], handlers=[], orelse=[], finalbody=outer.finalbody),
        *function.body[function.body.index(outer) + 1:]]
    module = ast.fix_missing_locations(ast.Module(statements, type_ignores=[]))
    writes = [{'native_gate_passed': False}]
    def action(stage):
        if failure == stage: raise RuntimeError(stage)
    def write(path, value):
        action('artifact')
        writes.append(value)
    api = SimpleNamespace(run_events=lambda run: [], run_log=lambda *args: '{}',
        stop_run=lambda run: action('stop-run'), wait_finished=lambda *args, **kw: action('wait-finished'))
    namespace = {'evidence': SimpleNamespace(text=write), 'tag': 'own', 'wire': SimpleNamespace(frames=[],
        close=lambda: action('wire-close')), 'api': api, 'run': 'own-run', 'commands': 'allow',
        'report': {'own': True}, 'observation': {'own': True}, 'version': 'version-2',
        'partial': 'version-1', 'cards': []}
    if failure:
        with pytest.raises(RuntimeError, match=failure): exec(compile(module, str(source), 'exec'), namespace)
        assert not any(isinstance(row, dict) and row.get('native_gate_passed') is True for row in writes)
    else:
        exec(compile(module, str(source), 'exec'), namespace)
        assert writes[-1]['native_gate_passed'] is True


@pytest.mark.parametrize('flush_failure', [False, True])
def test_host_profile_publishes_real_zero_child_snapshot_before_permission_D23(monkeypatch, tmp_path, flush_failure):
    """Actual observer/profile/consumer source with bounded staged-log driver."""
    from importlib.resources import files
    source = SOURCE / 'fixtures/generator_tool_observed_agent.py'
    spec = importlib.util.spec_from_file_location('host_prepermission_observer_control', source)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    run, cell = '0123456789abcdef', 'own-cell'
    workspace = tmp_path / ('ws_' + run[:8] + '_own')
    workspace.mkdir()
    staged, published, events = {}, {}, []
    async def flush():
        events.append('flush')
        if flush_failure: raise RuntimeError('controlled flush failure')
        published.update(staged)
        staged.clear()
    gen = SimpleNamespace(workspace_path=lambda: str(workspace), commit=flush)
    monkeypatch.setitem(sys.modules, 'gen', gen)
    def write_log(path, body, **kwargs):
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        staged[path] = body
    context = {'auth': 'api_key', 'provider': 'anthropic', 'runtime': 'standard',
        'model': 'claude-haiku-4-5-20251001',
        'host_harness': {'path': '/owned/claude', 'binary': 'claude', 'version': '2.1.281'}}
    profile = SimpleNamespace(locus='host', cwd=str(workspace), env_obj=None, adapter_version='0.81.1')
    async def original_profile(actual):
        assert actual is context
        return profile
    async def original_command(child):
        raise AssertionError('No command may run before approval')
    child_owner = SimpleNamespace(_host_command=original_command)
    actual_import = worker.importlib.import_module
    monkeypatch.setattr(worker.importlib, 'import_module',
        lambda name: child_owner if name == '_agent_shared.generator_test' else actual_import(name))
    acp = SimpleNamespace(_PROFILES={'claude-code': original_profile})
    provenance = {'backend': 'claude-code', 'sources': {'accepted/main.py': 'public-sha'}}
    consumer_source = SOURCE / 'test_live_generator_tool.py'
    names = {'_host_observation', '_no_host_child', '_json'}
    nodes = [node for node in ast.parse(consumer_source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name in names]
    consumers = {'json': json, 'Path': Path, 'PROOF': 'child-proof.json'}
    exec(compile(ast.Module(nodes, type_ignores=[]), str(consumer_source), 'exec'), consumers)
    def wait_until(call, *args, **kwargs):
        value = call()
        assert value, 'Initial real zero-child observation was not published'
        return value
    api = SimpleNamespace(wait_until=wait_until, run_log=lambda run, path: published.get(path),
        cell_paths=lambda cell: [])
    async def original_turn(*args, **kwargs):
        raise AssertionError('No native turn in offline staged-log control')
    async def session_run(*, here):
        assert here == str(tmp_path / 'accepted')
        offered = await acp._PROFILES['claude-code'](context)
        assert offered is profile
        events.append('permission-boundary')
        body = consumers['_no_host_child'](api, cell, run, provenance)
        assert body['executions'] == []
        assert body['binding']['sdk'] == str(files('collimate').resolve().parent)
        return 'recorded-boundary'
    session = SimpleNamespace(run=session_run, entry=SimpleNamespace(run_agent=original_turn))
    capture = SimpleNamespace(Capture=object, observed_profile=lambda item, observer: item)
    async def drive():
        return await asyncio.wait_for(worker.run_observed(
            session, str(tmp_path / 'accepted'), acp, capture, provenance,
            identity=lambda: {'run_id': run, 'cell_id': cell}, scrub=lambda value: value,
            write_log=write_log), timeout=2)
    if flush_failure:
        with pytest.raises(RuntimeError, match='controlled flush failure'): asyncio.run(drive())
        assert events == ['flush'] and not published
    else:
        assert asyncio.run(drive()) == 'recorded-boundary'
        assert events == ['flush', 'permission-boundary']
    assert session.entry.run_agent is original_turn and acp._PROFILES['claude-code'] is original_profile
    assert child_owner._host_command is original_command
