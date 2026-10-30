"""Worker observation controls; simulated transport is never native evidence."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests_live import test_live_generator_tool as live

SDK = Path(__file__).resolve().parents[2] / 'sdk'
GENERATORS = Path(__file__).resolve().parents[2] / 'content' / 'generators'
BUILTINS = Path(__file__).resolve().parents[2] / 'builtins'
WORKER = live.FIXTURES / 'generator_tool_observed_agent.py'
# The accepted agent generator's definition, as _actual_sources places it:
# D23 observes the package the live suite publishes (OBSERVED_GENERATOR).
DEFINITION = 'agent/agent.generator'


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker():
    return load(WORKER, 'd23_test_worker')


def observer():
    return load(SDK / 'tests_live/fixtures/acp_capture.py', 'd23_test_capture')


def messages(number=1):
    return [
        {'id': number, 'method': 'session/prompt', 'params': {'sessionId': 'native', 'prompt': 'not retained'}},
        {'method': 'session/update', 'params': {'sessionId': 'native', 'update': {
            'sessionUpdate': 'tool_call', 'toolCallId': f'call-{number}',
            'title': 'collimate_test_generator', 'rawInput': {'prompt': 'selected-secret'}}}},
        {'id': number, 'result': {'stopReason': 'end_turn'}},
    ]


@pytest.mark.parametrize('ending', ['normal', 'error', 'cancel'])
def test_cached_native_transport_uses_disjoint_turn_capture_and_unwinds_D23(ending):
    async def scenario():
        module, capture = worker(), observer()
        logs = []
        original = []
        router = module.TurnObserver(capture, backend='pi', provenance={'sources': {}},
            identity=lambda: {'run_id': 'run', 'cell_id': 'cell'},
            scrub=lambda value: json.loads(json.dumps(value).replace('selected-secret', '[redacted]')),
            write_log=lambda path, body, **kw: logs.append((path, json.loads(body), kw)))
        async def turn(*args, **kwargs):
            original.append((args, kwargs))
            rows = messages(len(original))
            router.observe('send', rows[0])
            router.observe('receive', rows[1])
            router.observe('receive', rows[2])
            assert rows[1]['params']['update']['rawInput']['prompt'] == 'selected-secret'
            if len(original) == 1 and ending == 'error':
                raise ValueError('synthetic failure')
            if len(original) == 1 and ending == 'cancel':
                raise asyncio.CancelledError()
            return 'original-result'
        wrapped = router.wrap(turn)
        if ending == 'normal':
            assert await wrapped(1, backend='pi') == 'original-result'
        else:
            with pytest.raises(ValueError if ending == 'error' else asyncio.CancelledError):
                await wrapped(1, backend='pi')
        assert router.current is None
        assert await wrapped(2, backend='pi') == 'original-result'
        assert original == [((1,), {'backend': 'pi'}), ((2,), {'backend': 'pi'})]
        assert [row[1]['ordinal'] for row in logs] == [1, 2]
        assert [row[1]['records'][0]['id'] for row in logs] == [1, 2]
        assert all(row[2] == {'category': 'log'} for row in logs)
        assert 'selected-secret' not in json.dumps(logs)
        assert logs[0][1]['outcome'] == ending
        assert logs[1][1]['outcome'] == 'normal'
        assert logs[1][1]['run_id'] == 'run'
    asyncio.run(scenario())


def test_observer_does_no_log_io_inside_transport_callback_and_rejects_overlap_D23():
    async def scenario():
        module, capture = worker(), observer()
        logs = []
        router = module.TurnObserver(capture, backend='pi', provenance={}, identity=lambda: {},
            scrub=deepcopy, write_log=lambda *a, **k: logs.append(a))
        async def inner():
            raise AssertionError('overlapping original must not run')
        async def turn(**kwargs):
            rows = messages()
            router.observe('send', rows[0])
            assert logs == []
            with pytest.raises(RuntimeError, match='overlapping'):
                await router.wrap(inner)(backend='pi')
        await router.wrap(turn)(backend='pi')
        assert len(logs) == 1
    asyncio.run(scenario())


def test_copied_package_preserves_accepted_bytes_and_pep_header_D23():
    accepted = {
        'nested/observed.generator': 'id: observed\nname: Observed\nentrypoint: observed.py\nmain_config: main.set_yaml\n',
        'nested/observed.py': '# /// script\n# dependencies = ["pyyaml"]\n# ///\nasync def run(): pass\n',
        'nested/main.set_yaml': 'fields: []\nvalues: {}\n',
        '_agent_shared/owned.py': 'unchanged = True\n',
    }
    built, proof = live._observed_package(accepted, 'nested/observed.generator', 'synthetic-agent',
                                        (SDK / 'tests_live/fixtures/acp_capture.py').read_text())
    for path, text in accepted.items():
        if path != 'nested/observed.generator':
            assert built[proof['accepted_to_runtime'][path]] == text
    assert built['synthetic-agent/d23_observed_agent.py'].startswith(accepted['nested/observed.py'].split('async def')[0])
    module = worker()
    assert proof['sources']['synthetic-agent/_agent_shared/owned.py'] == module.digest(accepted['_agent_shared/owned.py'].encode())
    assert proof['entrypoint'] == 'synthetic-agent/observed.py'
    assert proof['root_depth'] == 1


@pytest.mark.parametrize('ending', ['normal', 'error', 'cancel'])
def test_cached_profile_preserves_disposer_and_restores_hooks_D23(ending):
    from dataclasses import dataclass
    @dataclass
    class Process:
        transport: object
        kill: object
        proc: object
        dispose: object
    @dataclass
    class Profile:
        spawn: object
    async def scenario():
        module, capture = worker(), observer()
        logs, sent, received, closed = [], [], [], []
        class Transport:
            async def send(self, value):
                sent.append(value)
                return 'sent'
            async def receive(self):
                return received.pop(0)
            async def close(self):
                closed.append(True)
        process = Process(Transport(), object(), object(), object())
        async def spawn():
            return process
        async def prepare(context):
            assert context == {'original': True}
            return Profile(spawn)
        acp = SimpleNamespace(_PROFILES={'pi': prepare})
        cached = None
        async def turn(**kwargs):
            nonlocal cached
            if cached is None:
                cached = await (await acp._PROFILES['pi']({'original': True})).spawn()
                assert cached.kill is process.kill and cached.proc is process.proc
                assert cached.dispose is process.dispose
            rows = messages(len(sent) + 1)
            assert await cached.transport.send(rows[0]) == 'sent'
            received.extend(rows[1:])
            assert await cached.transport.receive() is rows[1]
            assert await cached.transport.receive() is rows[2]
        async def run(*, here):
            assert here == '/accepted/agent'
            try:
                await session.entry.run_agent(backend='pi')
                await session.entry.run_agent(backend='pi')
                if ending == 'error':
                    raise OSError('original')
                if ending == 'cancel':
                    raise asyncio.CancelledError()
                return 'original-result'
            finally:
                await cached.transport.close()
        session = SimpleNamespace(run=run, entry=SimpleNamespace(run_agent=turn))
        call = module.run_observed(session, '/accepted/agent', acp, capture, {'backend': 'pi'},
            identity=lambda: {}, scrub=deepcopy,
            write_log=lambda path, body, **kw: logs.append(json.loads(body)))
        if ending == 'normal':
            assert await call == 'original-result'
        else:
            with pytest.raises(OSError if ending == 'error' else asyncio.CancelledError):
                await call
        assert session.entry.run_agent is turn and acp._PROFILES['pi'] is prepare
        assert closed == [True] and [row['records'][0]['id'] for row in logs] == [1, 2]
        assert asyncio.all_tasks() == {asyncio.current_task()}
    asyncio.run(scenario())


def test_capture_bound_failure_is_loud_and_unwinds_D23():
    async def scenario():
        module, capture = worker(), observer()
        class Tiny(capture.Capture):
            def __init__(self):
                super().__init__(max_records=1)
        logs = []
        router = module.TurnObserver(SimpleNamespace(Capture=Tiny), backend='pi', provenance={},
            identity=lambda: {}, scrub=deepcopy, write_log=lambda path, body, **kw: logs.append(json.loads(body)))
        async def turn(**kw):
            router.observe('send', messages()[0])
            router.observe('receive', messages()[1])
        with pytest.raises(capture.CaptureError, match='bound'):
            await router.wrap(turn)(backend='pi')
        assert router.current is None and logs[0]['outcome'] == 'error'
    asyncio.run(scenario())


def _actual_sources():
    """The agent generator's accepted sources as the Agent cell holds them:
    the agent/ package plus the cell's shared _* folders (the session runtime
    included)."""
    source, selected = GENERATORS, {}
    for folder in ('agent/agent', '_agent_shared', '_env_shared'):
        for path in (source / folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
                name = path.relative_to(source).as_posix()
                selected[name[len('agent/'):] if name.startswith('agent/') else name] = path.read_text()
    return selected


@pytest.mark.parametrize('backend', ['pi', 'cline', 'codex', 'qwen-code'])
def test_actual_package_selection_staging_and_isolated_import_D23(tmp_path, monkeypatch, backend):
    import os
    import subprocess
    import sys
    from models import Cell, VirtualFile
    import run_staging
    from package_approval import CellSnapshot, package_snapshot
    accepted = _actual_sources()
    built, proof = live._observed_package(accepted, DEFINITION, 'observed-test',
        (SDK / 'tests_live/fixtures/acp_capture.py').read_text(), backend=backend)
    snapshot = CellSnapshot('source', 'tip', {p: ('text', v) for p, v in built.items()}, {}, set())
    package, selected = package_snapshot(snapshot, proof['definition'])
    assert len(selected) == len(built), 'Add must retain all shared sources'
    cell = Cell(name='Synthetic observed source', files=[VirtualFile(name=p, content=v) for p, v in built.items()])
    monkeypatch.setattr(run_staging, 'resolve_generator_cell', lambda *a, **k: (cell, package.prefix + '/'))
    staged = run_staging.materialize_generator_source(
        run_staging.snapshot_generator_source('observed-test', 'synthetic'), str(tmp_path / 'stage'))
    # The worker runs as the package's script, so its folder leads sys.path.
    script = '''import importlib.util,json,pathlib,sys
here=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(here))
spec=importlib.util.spec_from_file_location('observer_worker',here/'d23_observed_agent.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
p=json.loads((here/'d23-observation.json').read_text());m.verify_sources(here.parent,p)
a=m.load_module(here.parent/p['entrypoint'],'_d23_accepted_agent')
import _agent_shared.acp_run as acp
import _agent_shared.session as session
assert pathlib.Path(acp.__file__).resolve().is_relative_to(here)
assert pathlib.Path(session.__file__).resolve().is_relative_to(here)
assert pathlib.Path(a.__file__).resolve()==here/'agent.py'
assert a.session is session
assert callable(session.run) and callable(session.entry.run_agent)
print(json.dumps({'loaded':True,'source_count':len(p['sources'])}))
'''
    result = subprocess.run([sys.executable, '-I', '-c', script, staged],
        env={key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR') if key in os.environ},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'loaded': True, 'source_count': len(proof['sources'])}
    for source, destination in proof['accepted_to_runtime'].items():
        if source != DEFINITION:
            assert (Path(staged).parent / destination).read_text() == accepted[source]
    target = Path(staged) / '_agent_shared/acp_run.py'
    target.write_text(target.read_text() + '\n# altered\n')
    with pytest.raises(RuntimeError, match='hash differs'):
        worker().verify_sources(Path(staged).parent, proof)


def native_envelope(backend):
    value = {'exit_code': 0, 'output': 'full-native-output-' * 100, 'result': {'success': True}}
    arguments = {'path': live.CHILD_PATH, 'settings': {'marker': 'unique'}, 'prompt': 'private'}
    text = json.dumps(value)
    raw = {'content': [{'type': 'text', 'text': text}], 'details': {'result': value}} if backend == 'pi' else {
        'output': text, 'metadata': {'truncated': False}}
    rows = [
        {'direction': 'send', 'id': 7, 'method': 'session/prompt', 'sessionId': 'actual-acp'},
        {'direction': 'receive', 'message': {'method': 'session/update', 'params': {'sessionId': 'actual-acp',
            'update': {'sessionUpdate': 'tool_call', 'toolCallId': 'actual-call',
                'title': 'collimate_test_generator', 'rawInput': arguments}}}},
        {'direction': 'receive', 'message': {'method': 'session/update', 'params': {'sessionId': 'actual-acp',
            'update': {'sessionUpdate': 'tool_call_update', 'toolCallId': 'actual-call', 'status': 'completed',
                'rawOutput': raw, 'content': [{'type': 'content', 'content': {'type': 'text', 'text': text}}]}}}},
        {'direction': 'receive', 'message': {'id': 7, 'result': {'stopReason': 'end_turn'}}},
    ]
    envelope = {'run_id': 'run', 'cell_id': 'cell', 'backend': backend, 'ordinal': 1,
                'outcome': 'normal', 'provenance': {'sources': {'owned': 'exact'}}, 'records': rows}
    frames = [{'t': 'tool', 'id': 'actual-call', 'name': 'collimate_test_generator', 'status': 'running'},
              {'t': 'tool', 'id': 'actual-call', 'status': 'done'}]
    return envelope, frames, arguments, value


def cline_app_envelope():
    envelope, _, arguments, value = native_envelope('pi')
    settings = {'overlay': 'child-value', 'marker': 'unique', 'parent_run_id': 'run',
                'cell_id': 'cell', 'expected_prompt': 'private', 'commands': 'ask',
                'expected_mount': '/workspace'}
    arguments = {**arguments, 'settings': settings}
    title = 'collimate__test_generator: {"path":"draft/probe.generator","settings":{"overlay":"ch...'
    rows = envelope['records']
    pending = rows[1]['message']['params']['update']
    pending.update(sessionUpdate='tool_call_update', status='pending', rawInput=arguments, title=title)
    initial = deepcopy(rows[1])
    initial['message']['params']['update']['sessionUpdate'] = 'tool_call'
    progress = deepcopy(rows[1])
    progress['message']['params']['update'] = {'sessionUpdate': 'tool_call_update',
        'toolCallId': 'actual-call', 'status': 'in_progress'}
    final = rows[2]['message']['params']['update']
    final.pop('content')
    final['rawOutput'] = {'content': [{'type': 'text', 'text': json.dumps(value)}],
        'isError': False, 'structuredContent': {'result': value}}
    rows[2:2] = [progress, initial]
    envelope['backend'] = 'cline'
    return envelope, arguments, value


def codex_app_envelope():
    envelope, _, _, value = native_envelope('pi')
    _, arguments, _ = cline_app_envelope()
    value['result']['error_message'] = ''
    wrapper = {'server': 'collimate', 'tool': 'test_generator', 'arguments': arguments}
    rows = envelope['records']
    initial = rows[1]['message']['params']['update']
    initial.update(title='mcp.collimate.test_generator', status='in_progress', kind='execute',
                   rawInput=deepcopy(wrapper), _meta={'is_mcp_tool_call': True})
    final = rows[2]['message']['params']['update']
    final.pop('content')
    final['rawInput'] = deepcopy(wrapper)
    final['rawOutput'] = {'result': {'content': [{'type': 'text', 'text': json.dumps(value)}],
                                   'structuredContent': {'result': deepcopy(value)}, '_meta': None},
                          'error': None}
    envelope['backend'] = 'codex'
    return envelope, arguments, value


def qwen_app_envelope(*, ancillary=True):
    """Measured0.23.3 pending/progress/full-result layout, synthetic app data."""
    envelope, arguments, value = cline_app_envelope()
    value['result']['error_message'] = ''
    meta = {'toolName': 'mcp__collimate__test_generator', 'provenance': 'mcp', 'serverId': 'collimate'}
    def start(identity, title, kind, inputs, metadata, preparing):
        return {'sessionUpdate': 'tool_call' if preparing else 'tool_call_update',
            'toolCallId': identity, 'status': 'pending' if preparing else 'in_progress',
            'title': title, 'content': [], 'locations': [], 'kind': kind, 'rawInput': inputs,
            '_meta': {**metadata, **({'phase': 'preparing'} if preparing else {})}}
    updates = []
    if ancillary:
        resource_meta = {'toolName': 'read_mcp_resource', 'provenance': 'builtin'}
        updates = [start('resource-call', 'ReadMcpResource', 'fetch', {}, resource_meta, True),
            start('resource-call', 'ReadMcpResource: collimate:collimate://test_generator', 'fetch',
                  {'server_name': 'collimate', 'uri': 'collimate://test_generator'}, resource_meta, False),
            {'sessionUpdate': 'tool_call_update', 'toolCallId': 'resource-call', 'status': 'failed',
             '_meta': resource_meta, 'content': [{'type': 'content', 'content': {'type': 'text',
                 'text': 'Unknown resource: collimate://test_generator'}}]}]
    title = 'test_generator (collimate MCP Server)'
    text = json.dumps(value)
    updates += [start('actual-call', title, 'other', {}, meta, True),
        start('actual-call', title + ': ' + json.dumps(arguments, separators=(',', ':')), 'other',
              deepcopy(arguments), meta, False),
        {'sessionUpdate': 'tool_call_update', 'toolCallId': 'actual-call', 'status': 'completed',
         '_meta': meta, 'rawOutput': text,
         'content': [{'type': 'content', 'content': {'type': 'text', 'text': text}}]}]
    envelope['records'] = [envelope['records'][0], *[
        {'direction': 'receive', 'message': {'method': 'session/update',
         'params': {'sessionId': 'actual-acp', 'update': update}}} for update in updates],
        envelope['records'][-1]]
    envelope['backend'] = 'qwen-code'
    return envelope, arguments, value


def qwen_granted_app_envelope(*, refused=True):
    """Measured permission-granted four-record layout; synthetic app data."""
    envelope, arguments, value = qwen_app_envelope(ancillary=False)
    envelope['records'].pop(2)
    if refused:
        value = {'error': 'approval_refused', 'message': 'synthetic inner app refusal'}
        final = envelope['records'][2]['message']['params']['update']
        final['rawOutput'] = json.dumps(value)
        final['content'][0]['content']['text'] = final['rawOutput']
    for row in envelope['records'][1:]:
        row['message']['jsonrpc'] = '2.0'
    request = {'jsonrpc': '2.0', 'id': 0, 'method': 'session/request_permission',
        'params': {'sessionId': 'actual-acp', 'toolCall': {'toolCallId': 'actual-call',
            'status': 'pending', 'title': json.dumps(arguments, separators=(',', ':')),
            'content': [], 'locations': [], 'kind': 'other', 'rawInput': deepcopy(arguments),
            '_meta': {'toolName': 'mcp__collimate__test_generator'}}, 'options': [
                {'optionId': 'proceed_always_project', 'name': 'Always Allow in project: test_generator', 'kind': 'allow_always'},
                {'optionId': 'proceed_always_user', 'name': 'Always Allow for user: test_generator', 'kind': 'allow_always'},
                {'optionId': 'proceed_once', 'name': 'Allow', 'kind': 'allow_once'},
                {'optionId': 'cancel', 'name': 'Reject', 'kind': 'reject_once'}]}}
    reply = {'jsonrpc': '2.0', 'id': 0, 'result': {
        'outcome': {'optionId': 'proceed_once', 'outcome': 'selected'}}}
    envelope['permission_records'] = [
        {'direction': 'receive', 'message': request, 'after_record_count': 2},
        {'direction': 'send', 'message': reply, 'after_record_count': 2}]
    return envelope, arguments, value


@pytest.mark.parametrize('refused', [True, False])
def test_actual_qwen_granted_permission_native_frames_and_complete_value_D23(monkeypatch, refused):
    envelope, arguments, value = qwen_granted_app_envelope(refused=refused)
    original = deepcopy(envelope)
    frames = actual_native_frames(monkeypatch, envelope)
    assert len(frames) == 2 and frames[0]['name'] == frames[1]['name'] == 'test_generator (collimate MCP Server)'
    assert live._native_tool_value(frames, arguments['settings'], arguments['prompt'], envelope,
        run='run', cell='cell', backend='qwen-code', ordinal=1, provenance=envelope['provenance']) == value
    assert envelope == original


@pytest.mark.parametrize('damage', ['no_permission', 'permission_session', 'permission_id',
    'permission_input', 'reply', 'order', 'extra_permission', 'display_id', 'display_name',
    'display_status', 'extra_display', 'missing_display', 'provenance', 'run', 'cell', 'end'])
def test_actual_qwen_granted_permission_cannot_borrow_other_evidence_D23(monkeypatch, damage):
    envelope, arguments, _ = qwen_granted_app_envelope()
    provenance = deepcopy(envelope['provenance'])
    frames = actual_native_frames(monkeypatch, envelope)
    request, reply = envelope['permission_records']
    if damage == 'no_permission': envelope.pop('permission_records')
    elif damage == 'permission_session': request['message']['params']['sessionId'] = 'foreign'
    elif damage == 'permission_id': request['message']['params']['toolCall']['toolCallId'] = 'foreign'
    elif damage == 'permission_input': request['message']['params']['toolCall']['rawInput']['prompt'] = 'foreign'
    elif damage == 'reply': reply['message']['result']['outcome']['optionId'] = 'cancel'
    elif damage == 'order': reply['after_record_count'] = 3
    elif damage == 'extra_permission': envelope['permission_records'].append(deepcopy(reply))
    elif damage == 'display_id': frames[-1]['id'] = 'foreign'
    elif damage == 'display_name': frames[-1]['name'] = 'Run test_generator'
    elif damage == 'display_status': frames[-1]['status'] = 'error'
    elif damage == 'extra_display': frames.append(deepcopy(frames[-1]))
    elif damage == 'missing_display': frames.pop(0)
    elif damage == 'provenance': envelope['provenance']['sources']['owned'] = 'altered'
    elif damage in ('run', 'cell'): envelope[damage + '_id'] = 'foreign'
    else: envelope['records'][-1]['message']['result']['stopReason'] = 'cancelled'
    with pytest.raises((AssertionError, RuntimeError)):
        live._native_tool_value(frames, arguments['settings'], arguments['prompt'], envelope,
            run='run', cell='cell', backend='qwen-code', ordinal=1, provenance=provenance)


def actual_native_frames(monkeypatch, envelope):
    import importlib
    from collimate.native import ChatTurn
    from routes.chat_live import _to_frame
    monkeypatch.setenv('COLLIMATE_LIVE_SDK_SOURCE', str(SDK))
    monkeypatch.syspath_prepend(str(GENERATORS))
    normalizer = importlib.import_module('_agent_shared.acp_client')
    frames = []
    turn = SimpleNamespace(emit=frames.append)
    def emit(event):
        extra = {key: item for key, item in event.items()
                 if key not in ('type', 'name', 'status', 'thread_id')}
        ChatTurn.tool(turn, event.get('name', ''), event.get('status') or 'running', **extra)
    session = normalizer.AcpSession(backend=envelope['backend'],
        profile=SimpleNamespace(cwd='/workspace'), mcp_servers_for=lambda caps: ([], []))
    session._sink = normalizer._TurnSink(emit=emit, stream=True)
    for row in envelope['records']:
        if row.get('message', {}).get('method') != 'session/update':
            continue
        session._on_update(row['message']['params'])
    assert session._sink.open_tools == {}
    return [_to_frame('actual-turn', event) for event in frames]


def test_actual_cline_normalizer_frames_pair_with_full_app_native_arguments_D23(monkeypatch):
    envelope, arguments, value = cline_app_envelope()
    frames = actual_native_frames(monkeypatch, envelope)
    assert live._native_tool_value(frames, arguments['settings'], arguments['prompt'], envelope,
        run='run', cell='cell', backend='cline', ordinal=1, provenance=envelope['provenance']) == value


def test_actual_codex_normalizer_frames_pair_with_full_app_native_arguments_D23(monkeypatch):
    envelope, arguments, value = codex_app_envelope()
    original = deepcopy(envelope['records'])
    frames = actual_native_frames(monkeypatch, envelope)
    assert live._native_tool_value(frames, arguments['settings'], arguments['prompt'], envelope,
        run='run', cell='cell', backend='codex', ordinal=1, provenance=envelope['provenance']) == value
    assert envelope['records'] == original


def test_observed_package_admits_codex_exact_pins_and_source_bytes_D23():
    accepted = _actual_sources()
    built, proof = live._observed_package(accepted, DEFINITION, 'observed-test',
        (SDK / 'tests_live/fixtures/acp_capture.py').read_text(), backend='codex')
    assert live.NATIVE_PINS['codex'] == {'codex': '0.157.0', 'codex-acp': '1.13.1'}
    assert proof['backend'] == 'codex'
    for original, destination in proof['accepted_to_runtime'].items():
        if original != DEFINITION:
            assert built[destination] == accepted[original]


def test_observed_package_admits_cline_exact_pin_and_source_bytes_D23():
    accepted = _actual_sources()
    built, proof = live._observed_package(accepted, DEFINITION, 'observed-test',
        (SDK / 'tests_live/fixtures/acp_capture.py').read_text(), backend='cline')
    assert live.NATIVE_PINS['cline'] == {'cline': '3.0.65', 'mcp-remote': '0.2.4'}
    assert proof['backend'] == 'cline'
    for original, destination in proof['accepted_to_runtime'].items():
        if original != DEFINITION:
            assert built[destination] == accepted[original]


def test_observed_package_admits_qwen_exact_pin_and_source_bytes_D23():
    accepted = _actual_sources()
    built, proof = live._observed_package(accepted, DEFINITION, 'observed-test',
        (SDK / 'tests_live/fixtures/acp_capture.py').read_text(), backend='qwen-code')
    assert live.NATIVE_PINS['qwen-code'] == {'qwen-code': '0.23.3'}
    assert proof['backend'] == 'qwen-code'
    for original, destination in proof['accepted_to_runtime'].items():
        if original != DEFINITION:
            assert built[destination] == accepted[original]


@pytest.mark.parametrize('ancillary', [False, True])
def test_actual_qwen_session_frames_carry_latest_native_title_D23(monkeypatch, ancillary):
    envelope, arguments, value = qwen_app_envelope(ancillary=ancillary)
    original = deepcopy(envelope['records'])
    frames = actual_native_frames(monkeypatch, envelope)
    updates = [row['message']['params']['update'] for row in envelope['records'][1:-1]]
    assert 'title' not in updates[-1]
    assert frames[-1]['name'] == updates[-2]['title']
    if ancillary:
        assert frames[2]['name'] == updates[1]['title']
    assert live._native_tool_value(frames, arguments['settings'], arguments['prompt'], envelope,
        run='run', cell='cell', backend='qwen-code', ordinal=1, provenance=envelope['provenance']) == value
    assert envelope['records'] == original


@pytest.mark.parametrize('damage', ['target_id', 'target_name', 'target_status', 'resource_id',
                                   'resource_name', 'resource_status', 'missing_resource', 'duplicate_resource',
                                   'target_final_name', 'resource_final_name', 'target_base_name',
                                   'resource_base_name', 'borrow_resource_name'])
def test_qwen_app_display_cannot_borrow_target_or_ignore_ancillary_D23(monkeypatch, damage):
    envelope, arguments, _ = qwen_app_envelope()
    frames = actual_native_frames(monkeypatch, envelope)
    if damage == 'missing_resource':
        frames = frames[3:]
    elif damage == 'duplicate_resource':
        frames.insert(1, deepcopy(frames[0]))
    elif damage in ('target_final_name', 'resource_final_name'):
        frames[5 if damage.startswith('target') else 2]['name'] = ''
    elif damage in ('target_base_name', 'resource_base_name'):
        frames[5 if damage.startswith('target') else 2]['name'] = frames[
            3 if damage.startswith('target') else 0]['name']
    elif damage == 'borrow_resource_name':
        frames[5]['name'] = frames[2]['name']
    else:
        identity, field = damage.split('_')
        frame = frames[4 if identity == 'target' else 1]
        frame[{'id': 'id', 'name': 'name', 'status': 'status'}[field]] = 'foreign'
    with pytest.raises((AssertionError, RuntimeError)):
        live._native_tool_value(frames, arguments['settings'], arguments['prompt'], envelope,
            run='run', cell='cell', backend='qwen-code', ordinal=1, provenance=envelope['provenance'])


@pytest.mark.parametrize('backend', ['pi', 'opencode', 'cline', 'codex', 'qwen-code'])
@pytest.mark.parametrize('damage', ['', 'run', 'cell', 'backend', 'ordinal', 'outcome', 'hash',
                                   'display_id', 'extra_display', 'name', 'native_id', 'end', 'input'])
def test_native_artifact_cannot_borrow_other_turn_run_or_display_D23(monkeypatch, backend, damage):
    monkeypatch.setenv('COLLIMATE_LIVE_SDK_SOURCE', str(SDK))
    if backend in ('cline', 'codex', 'qwen-code'):
        envelope, arguments, value = (cline_app_envelope() if backend == 'cline'
                                      else codex_app_envelope() if backend == 'codex'
                                      else qwen_app_envelope())
        frames = actual_native_frames(monkeypatch, envelope)
    else:
        envelope, frames, arguments, value = native_envelope(backend)
    expected_provenance = deepcopy(envelope['provenance'])
    if damage in ('run', 'cell'):
        envelope[damage + '_id'] = 'other'
    elif damage in ('backend', 'ordinal', 'outcome'):
        envelope[damage] = 'other'
    elif damage == 'hash':
        envelope['provenance']['sources']['owned'] = 'altered'
    elif damage == 'display_id':
        frames[1]['id'] = 'other'
    elif damage == 'extra_display':
        frames.append({**frames[0], 'id': 'other'})
    elif damage == 'name':
        frames[0]['name'] = 'run_bash'
    elif damage == 'native_id':
        envelope['records'][-2]['message']['params']['update']['toolCallId'] = 'other'
    elif damage == 'end':
        envelope['records'].pop()
    elif damage == 'input':
        arguments = {**arguments, 'prompt': 'different'}
    def check():
        return live._native_tool_value(frames, arguments['settings'], arguments['prompt'], envelope,
            run='run', cell='cell', backend=backend, ordinal=1, provenance=expected_provenance)
    if damage:
        with pytest.raises((AssertionError, RuntimeError)):
            check()
    else:
        assert check() == value


@pytest.mark.parametrize('backend', ['pi', 'cline', 'codex', 'qwen-code'])
def test_actual_reserved_verifier_admits_complete_observed_package_D23(tmp_path, monkeypatch, backend):
    from package_approval import admit_package
    from verifier_pool import close_verifier_pool
    monkeypatch.chdir(tmp_path)
    built, proof = live._observed_package(_actual_sources(), DEFINITION, 'observed-test',
        (SDK / 'tests_live/fixtures/acp_capture.py').read_text(), backend=backend)
    files = {path.split('/', 1)[1]: text.encode() for path, text in built.items()}
    import builtin_packages
    source = (BUILTINS / 'definitions/generator/derived/verify.ts').read_bytes()
    monkeypatch.setattr(builtin_packages, 'verifier_for', lambda path: source)
    try:
        result = admit_package(None, proof['definition'], files)
        assert result['id'] == 'observed-test'
        damaged = {**files, 'agent.generator': files['agent.generator'].replace(
            b'entrypoint: d23_observed_agent.py', b'entrypoint: absent.py')}
        with pytest.raises(ValueError, match='entrypoint|missing|exist'):
            admit_package(None, proof['definition'], damaged)
    finally:
        close_verifier_pool()


def test_real_selected_registry_log_staging_is_private_and_not_cell_output_D23(tmp_path, monkeypatch):
    import importlib
    import stat
    import gen
    from collimate.testing import run_generator_callable
    monkeypatch.syspath_prepend(str(GENERATORS))
    hygiene = importlib.import_module('_agent_shared.secret_hygiene')
    # This bridge has no credential accounts. Only synthetic selected material
    # is registered; no owner home, environment key or account file is read.
    async def entry():
        hygiene.remember('synthetic-selected-credential')
        router = worker().TurnObserver(observer(), backend='pi', provenance={},
            identity=lambda: {'run_id': gen.run_info().run_id},
            scrub=hygiene.scrub_object, write_log=worker().private_log)
        async def turn(**kwargs):
            rows = messages()
            rows[1]['params']['update']['rawInput']['prompt'] = 'synthetic-selected-credential'
            for direction, row in zip(('send', 'receive', 'receive'), rows):
                router.observe(direction, row)
        await router.wrap(turn)(backend='pi')
        await gen.commit()
    try:
        result = run_generator_callable(entry, workspace=tmp_path, run_id='isolated-observer')
    finally:
        hygiene.clear()
    assert result.ok, result.error
    path = '_agent/d23-native/turn-0001.json'
    assert path in result.log_files and path not in result.files
    body = (tmp_path / path).read_text()
    assert 'synthetic-selected-credential' not in body and '[redacted]' in body
    assert stat.S_IMODE((tmp_path / path).stat().st_mode) == 0o600


@pytest.mark.parametrize('linked', ['parent', 'leaf'])
def test_private_artifact_writer_refuses_planted_links_D23(tmp_path, monkeypatch, linked):
    from collimate.testing import run_generator_callable
    monkeypatch.syspath_prepend(str(GENERATORS))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'turn-0001.json'
    sentinel.write_text('untouched')
    (workspace / '_agent').mkdir()
    folder = workspace / '_agent/d23-native'
    if linked == 'parent':
        folder.symlink_to(outside, target_is_directory=True)
    else:
        folder.mkdir()
        (folder / sentinel.name).symlink_to(sentinel)
    async def entry():
        worker().private_log('_agent/d23-native/turn-0001.json', '{}')
    result = run_generator_callable(entry, workspace=workspace)
    assert not result.ok
    assert sentinel.read_text() == 'untouched'
    assert result.log_files == {}


@pytest.mark.parametrize('title', ['collimate_test_generator', ''])
def test_actual_native_normalizer_and_chat_tool_preserve_call_identity_D23(monkeypatch, title):
    import importlib
    from collimate.native import ChatTurn
    monkeypatch.syspath_prepend(str(GENERATORS))
    normalizer = importlib.import_module('_agent_shared.acp_client')
    update = {'sessionUpdate': 'tool_call_update', 'toolCallId': 'actual-native-id',
              'status': 'completed', 'rawOutput': {'result': 'full'}}
    if title:
        update['title'] = title
    events = normalizer.normalize_update({'sessionId': 'actual-acp', 'update': update})['events']
    assert len(events) == 1
    event = events[0]
    emitted = []
    turn = SimpleNamespace(emit=emitted.append)
    extra = {key: value for key, value in event.items() if key not in ('type', 'name', 'status', 'thread_id')}
    ChatTurn.tool(turn, event.get('name', ''), event.get('status') or 'running', **extra)
    assert emitted[0]['id'] == 'actual-native-id' and emitted[0]['status'] == 'done'


def _actual_generator_probe_prompt(harness, settings, private_prompt):
    """Execute the real live consumer's nested prompt with synthetic inputs."""
    import ast
    source = ast.parse(Path(live.__file__).read_text())
    consumer = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'test_real_agent_generator_command_publication_and_approval_D23')
    prompt = next(node for node in ast.walk(consumer) if isinstance(node, ast.FunctionDef)
                  and node.name == 'prompt')
    namespace = {**vars(live), 'harness': harness, 'settings': settings,
                 'expected_private': private_prompt}
    exec(compile(ast.Module(body=[prompt], type_ignores=[]), live.__file__, 'exec'), namespace)
    return namespace['prompt']()


@pytest.mark.parametrize('harness,name', [
    ('qwen-code', 'mcp__collimate__test_generator'),
    ('cline', 'collimate__test_generator'),
    ('codex', 'test_generator'), ('pi', 'test_generator'),
    ('pydantic-ai', 'test_generator'),
])
def test_actual_live_generator_prompt_uses_native_name_and_full_input_D23(harness, name):
    settings = {'overlay': 'quoted "value"', 'commands': 'ask',
                'expected_prompt': 'synthetic-child\nprivate'}
    private_prompt = 'synthetic-child\nprivate'
    text = _actual_generator_probe_prompt(harness, settings, private_prompt)
    expected = (f'Call the {name} tool exactly once with the following exact arguments: '
                + json.dumps({'path': live.CHILD_PATH, 'settings': settings, 'prompt': private_prompt})
                + '. Do not read or change the draft, run a shell, or create any output yourself. '
                  'This is a test of that tool. After it returns, briefly report its result and stop. '
                  'If refused, do not retry or use another tool.')
    assert text == expected
    assert settings == {'overlay': 'quoted "value"', 'commands': 'ask',
                        'expected_prompt': 'synthetic-child\nprivate'}


def test_actual_live_generator_prompt_consumes_shared_helper_D23(monkeypatch):
    seen = []
    original = live._generator_tool_prompt
    def record(harness, settings, private_prompt):
        seen.append((harness, settings, private_prompt))
        return original(harness, settings, private_prompt)
    monkeypatch.setattr(live, '_generator_tool_prompt', record)
    settings = {'commands': 'ask'}
    result = _actual_generator_probe_prompt('qwen-code', settings, 'synthetic-private')
    assert result == original('qwen-code', settings, 'synthetic-private')
    assert seen == [('qwen-code', settings, 'synthetic-private')]
