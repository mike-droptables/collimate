# SPDX-License-Identifier: MIT
"""D23: real app publication, command card and child isolation.

No model response, bridge or ACK is replaced. Select reviewed native backends
with COLLIMATE_LIVE_GENERATOR_HARNESSES (default goose; pydantic-ai runs its
commands on this machine, so it has no container to observe). Every paid
case requires explicit Docker/provider gates. The deterministic deny parent is
separate: it executes byte-identical accepted tool code under the app's actual
SDK, without claiming native model discovery. Fixtures freeze before live runs.
"""
from __future__ import annotations

import hashlib
import ast
import importlib.util
import json
import os
from pathlib import Path
import time
import threading
import copy
import urllib.parse
import uuid

import pytest
import yaml

from .conftest import CHAT_POINTER, DOCKER_GATE, ApiError, delete_accounts, model_entry

pytestmark = pytest.mark.live
FIXTURES = Path(__file__).with_name('fixtures')
HARNESSES = tuple(value.strip() for value in os.environ.get(
    'COLLIMATE_LIVE_GENERATOR_HARNESSES', 'goose').split(',') if value.strip())
CHILD_PATH = 'draft/probe.generator'
PROOF = 'child-proof.json'
NATIVE_PINS = {
    'pi': {'pi-coding-agent': '0.87.1', 'pi-acp': '0.0.34', 'pi-mcp-sdk': '1.25.3'},
    'opencode': {'opencode': '1.18.30'},
    'cline': {'cline': '3.0.65', 'mcp-remote': '0.2.4'},
    'codex': {'codex': '0.157.0', 'codex-acp': '1.13.1'},
    'qwen-code': {'qwen-code': '0.23.3'},
}


HOST_NATIVE_PINS = {'claude-code': {'claude-code': '2.1.281', 'claude-agent-acp': '0.81.1'}}


def _capture_module():
    source = Path(os.environ['COLLIMATE_LIVE_SDK_SOURCE']) / 'tests_live/fixtures/acp_capture.py'
    spec = importlib.util.spec_from_file_location('d23_app_native_capture', source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, source.read_text()


def _observed_package(accepted, definition, identity, observer_source, *, backend='pi'):
    """Self-contained test placement, with exact accepted-to-runtime mapping."""
    assert backend in {*NATIVE_PINS, *HOST_NATIVE_PINS}
    manifest = yaml.safe_load(accepted[definition])
    folder = Path(definition).parent
    accepted_entry = (folder / manifest['entrypoint']).as_posix()
    original = accepted[accepted_entry]
    assert original.startswith('# /// script\n')
    pep = original[:original.index('# ///', len('# ///')) + len('# ///')] + '\n'
    prefix = folder.as_posix() + '/'
    mapping = {path: identity + '/' + (path[len(prefix):] if path.startswith(prefix) else path)
               for path in accepted}
    assert len(set(mapping.values())) == len(mapping), 'Source placement collision'
    entry = mapping[accepted_entry]
    wrapper = identity + '/d23_observed_agent.py'
    observer = identity + '/d23_acp_capture.py'
    metadata = identity + '/d23-observation.json'
    built = {mapping[path]: text for path, text in accepted.items()}
    assert not {wrapper, observer, metadata}.intersection(built)
    built[mapping[definition]] = yaml.safe_dump({**manifest, 'id': identity,
        'name': 'D23 observed accepted agent', 'entrypoint': 'd23_observed_agent.py'}, sort_keys=False)
    built[wrapper] = pep + (FIXTURES / 'generator_tool_observed_agent.py').read_text()
    built[observer] = observer_source
    def hashes(mapping):
        return {path: hashlib.sha256(text.encode()).hexdigest() for path, text in mapping.items()}
    provenance = {'backend': backend, 'entrypoint': entry, 'root_depth': 1,
                  'accepted_to_runtime': mapping, 'definition': mapping[definition],
                  'accepted_sources': hashes(accepted), 'sources': hashes(built)}
    built[metadata] = json.dumps(provenance, sort_keys=True)
    return built, provenance


#: The Agent cell generator the observed package copies. D23 drives its turns
#: and command cards over the chat wire (``ChatWire``), so its runners set
#: the ``chat`` surface.
OBSERVED_GENERATOR = 'agent'


def _add_to_capabilities(api, source_cell, definition):
    """Add the package at ``definition`` of ``source_cell`` to Capabilities,
    in a new cell, as the Files panel's Add to Capabilities does."""
    selection = {'source_cell_id': source_cell, 'definition_path': definition}
    preview = api.request('POST', f'/api/channels/{api.chq}/packages/preview', selection)
    api.request('POST', f'/api/channels/{api.chq}/packages/add', {
        **selection, 'expected_content_hash': preview['content_hash'],
        'expected_target_revision': preview['target_revision']})


def _publish_observed_agent(api, harness):
    """Publish an observed copy of the Agent cell's generator: its own
    package folder plus the cell's shared ``_*`` folders."""
    cell = next(row['cell_id'] for row in api.request('GET', '/api/generators')
                if row['name'] == OBSERVED_GENERATOR)
    accepted = {row['path']: api.cell_file(cell, row['path'])
                for row in api.cell_files(cell)
                if not row['deleted'] and (row['path'].startswith(OBSERVED_GENERATOR + '/')
                or row['path'].split('/', 1)[0].startswith('_'))}
    assert all(isinstance(text, str) for text in accepted.values())
    definitions = [path for path, text in accepted.items() if path.endswith('.generator')
                   and yaml.safe_load(text).get('id') == OBSERVED_GENERATOR]
    assert len(definitions) == 1
    definition = definitions[0]
    identity = 'd23-observed-' + uuid.uuid4().hex[:10]
    _, observer_source = _capture_module()
    files, provenance = _observed_package(accepted, definition, identity, observer_source, backend=harness)
    source = api.new_cell('D23 observed accepted package',
                          [{'name': path, 'content': text} for path, text in files.items()])
    _add_to_capabilities(api, source, provenance['definition'])
    return identity, provenance


def _generator_tool_prompt(harness, settings, private_prompt):
    tool_name = {'cline': 'collimate__test_generator',
                 'qwen-code': 'mcp__collimate__test_generator'}.get(harness, 'test_generator')
    return (f'Call the {tool_name} tool exactly once with the following exact arguments: '
            + json.dumps({'path': CHILD_PATH, 'settings': settings, 'prompt': private_prompt})
            + ('. Treat settings as an opaque exact object: include every provided key and value, '
               'omit every unlisted key, and do not infer or add defaults'
               if harness == 'claude-code' else '')
            + ('. Copy every string value verbatim, including expected_sdk and expected_mount paths: '
               'do not correct, rewrite, abbreviate, or infer any path'
               if harness == 'claude-code' else '')
            + '. Do not read or change the draft, run a shell, or create any output yourself. '
              'This is a test of that tool. After it returns, briefly report its result and stop. '
              'If refused, do not retry or use another tool.')


def _native_tool_value(frames, settings, prompt, envelope, *, run, cell, backend, ordinal, provenance):
    assert {key: envelope.get(key) for key in ('run_id', 'cell_id', 'backend', 'ordinal', 'outcome')} == {
        'run_id': run, 'cell_id': cell, 'backend': backend, 'ordinal': ordinal, 'outcome': 'normal'}
    assert envelope['provenance'] == provenance
    capture_module, _ = _capture_module()
    capture = capture_module.Capture()
    capture.records = envelope['records']
    capture.permission_records = envelope.get('permission_records', [])
    proof, value = capture.completed_generator_value(backend,
        expected_input={'path': CHILD_PATH, 'settings': settings, 'prompt': prompt})
    calls = [frame for frame in frames if frame.get('t') == 'tool' and frame.get('id')]
    if backend in ('qwen-code', 'claude-code'):
        # The strict decoder retains Qwen's measured failed resource lookup.
        # Corroborate every update's ID/title/status;
        # neither a foreign display call nor an ignored ancillary ID is proof.
        updates = [row['message']['params']['update'] for row in proof['records']
                   if row.get('message', {}).get('method') == 'session/update']
        states = {'pending': 'running', 'in_progress': 'running',
                  'completed': 'done', 'failed': 'error'}
        # AcpSession._on_update carries the latest named event for the same
        # open ID into a titleless completion, then removes that ID.
        open_titles = {}
        expected_calls = []
        for row in updates:
            identity = row['toolCallId']
            title = row.get('title', '')
            if title:
                open_titles[identity] = title
            else:
                title = open_titles.get(identity, '')
            state = states[row['status']] if row.get('status') else 'running'
            expected_calls.append((identity, title, state))
            if state in ('done', 'error'):
                open_titles.pop(identity, None)
        assert [(row['id'], row.get('name', ''), row.get('status')) for row in calls] == expected_calls, calls
        assert {row['id'] for row in calls} == {
            proof['tool_call_id'], *[row['tool_call_id'] for row in proof['ancillary_calls']]}, calls
        target = [row for row in calls if row['id'] == proof['tool_call_id']]
        assert target and target[-1]['status'] == 'done', target
        assert isinstance(value, dict)
        return value
    # acp_client.normalize_update -> agent.emit -> ChatTurn.tool forwards the
    # native string ID unchanged, including titleless completed updates.
    assert {row['id'] for row in calls} == {proof['tool_call_id']}, calls
    expected_name = proof['title'] if backend in ('cline', 'codex') else 'collimate_test_generator'
    assert {row['name'] for row in calls if row.get('name')} == {expected_name}, calls
    finals = [row for row in calls if row.get('status') in ('done', 'error')]
    assert finals and finals[-1]['status'] == 'done', calls
    assert isinstance(value, dict)
    return value


def _app_tool_value(api, run, cell, harness, frames, settings, prompt, ordinal, provenance):
    if harness not in NATIVE_PINS:
        return _tool_call(frames, settings, prompt, _full_trace(api, run, ordinal), ordinal)
    envelope = api.wait_until(lambda: api.run_log(run, f'_agent/d23-native/turn-{ordinal:04d}.json'),
                              30, 'raw native completed turn artifact', run_id=run)
    return _native_tool_value(frames, settings, prompt, _json(envelope), run=run, cell=cell,
                              backend=harness, ordinal=ordinal, provenance=provenance)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _history(api, cell):
    return api.request('GET', f'/api/channels/{api.chq}/cells/{cell}/history')


def _committed_proof(api, cell, run, path, expected):
    """Read an immutable host-minted version belonging to this parent run."""
    history = _history(api, cell)
    for node in history['nodes']:
        if node['kind'] != 'run' or node['run']['run_id'] != run:
            continue
        for version in reversed(node['versions']):
            identity = version['cell_id']
            try:
                content = api.request('GET', f'/api/channels/{api.chq}/versions/{identity}/files/'
                                      + urllib.parse.quote(path, safe='/'))
            except ApiError as exc:
                if exc.code == 404:
                    continue
                raise
            if _json(content) == expected:
                return identity
    raise AssertionError(f'No immutable version of {path} attributed to parent run {run}')


def _run_containers(run):
    import docker
    client = docker.from_env(timeout=2)
    try:
        return [{'id': row.id, 'hostname': row.attrs['Config']['Hostname'],
                 'kind': row.labels.get('com.collimate.kind'),
                 'mounts': [{'source': m['Source'], 'target': m['Destination']}
                            for m in row.attrs.get('Mounts', []) if m.get('Type') == 'bind']}
                for row in client.containers.list(filters={'label': f'com.collimate.run_id={run}'}, ignore_removed=True)]
    finally:
        client.close()


def _observed_turn(run, operation, *, interval=.05, timeout=430):
    """Retain real Docker metadata while a transient command resource lives."""
    observed, failures = {}, []
    stop = threading.Event()
    deadline = time.monotonic() + timeout

    def sample():
        for row in _run_containers(run):
            observed[row['id']] = copy.deepcopy(row)

    def watch():
        try:
            while not stop.wait(interval):
                if time.monotonic() >= deadline:
                    raise TimeoutError('Command container observation expired')
                sample()
        except Exception as exc:
            failures.append(exc)

    sample()
    thread = threading.Thread(target=watch, name='D23-container-observer')
    thread.start()
    try:
        result = operation()
    finally:
        stop.set()
        thread.join(timeout=15)
        assert not thread.is_alive(), 'Docker observation thread did not stop'
        if failures:
            raise AssertionError('Actual command container observation failed') from failures[0]
    return result, list(observed.values())


def _observed_container(containers, report, expected_kind):
    matching = [row for row in containers if row['hostname'] == report['hostname']]
    assert len(matching) == 1 and matching[0]['kind'] == expected_kind, containers
    row = matching[0]
    assert isinstance(row['id'], str) and row['id'], row
    assert any(mount['target'] == report['workspace'] for mount in row['mounts']), row
    return row


def _workspace(containers):
    roots = {mount['source'] for row in containers for mount in row['mounts']
             if mount['target'] in ('/workspace', '/work')}
    assert len(roots) == 1, containers
    return Path(roots.pop())


def _no_child(api, cell, run):
    assert PROOF not in api.cell_paths(cell)
    containers = _run_containers(run)
    if containers:
        root = _workspace(containers)
        assert not list((root / '.collrun').glob('forward-*'))
        assert not (root / PROOF).exists()
    return containers


def _host_observation(api, run, cell, provenance):
    body = _json(api.wait_until(lambda: api.run_log(run, '_agent/d23-host/observation.json'),
                              30, 'own host command observation', run_id=run))
    assert body['run_id'] == run and body['cell_id'] == cell
    assert body['provenance'] == provenance and provenance['backend'] == 'claude-code'
    binding = body['binding']
    assert binding['locus'] == 'host' and binding['adapter_version'] == '0.81.1'
    assert binding['model'] == 'claude-haiku-4-5-20251001'
    assert binding['host_harness']['version'] == '2.1.281'
    assert binding['host_harness']['binary'] == 'claude'
    root = Path(binding['workspace'])
    assert root.is_absolute() and root.is_dir() and not root.is_symlink()
    assert root.name.startswith('ws_' + run[:8] + '_')
    assert Path(binding['sdk'], 'collimate/testing/forward.py').is_file()
    assert isinstance(body['executions'], list)
    native = root / '_agent/d23-host/observation.json'
    assert native.is_file() and not native.is_symlink()
    assert _json(native.read_text()) == body, 'Staged host observation is stale'
    return body, root


def _no_host_child(api, cell, run, provenance):
    body, root = _host_observation(api, run, cell, provenance)
    assert body['executions'] == [], 'Host child boundary reached before approval'
    assert not list((root / '.collrun').glob('forward-*'))
    assert not (root / PROOF).exists() and PROOF not in api.cell_paths(cell)
    return body


def _host_child_return(api, cell, run, provenance, report):
    body, root = _host_observation(api, run, cell, provenance)
    assert len(body['executions']) == 1
    command = body['executions'][0]
    assert command['returned'] is True and type(command['exit_code']) is int and command['exit_code'] == 0
    assert command['cwd'] == str(root / 'draft')
    assert command['argv'][:3] == ['uv', 'run', '--no-project']
    assert command['argv'][-1] == str(root / 'draft/main.py')
    assert command['sdk'] == body['binding']['sdk']
    assert Path(command['run_dir']).parent == root / '.collrun'
    assert Path(command['run_dir']).name.startswith('forward-') and not Path(command['run_dir']).exists()
    spawn = command['spawn']
    assert type(spawn['pid']) is int and spawn['pid'] > 0
    assert isinstance(spawn['start'], str) and spawn['start'].isdigit()
    assert report['workspace'] == str(root) and report['execution_locus'] == 'host'
    assert report['sdk_forward'] == str(Path(command['sdk'], 'collimate/testing/forward.py').resolve())
    assert type(report['pid']) is int and report['pid'] > 0
    assert type(report['pgrp']) is int and report['pgrp'] == spawn['pid']
    assert isinstance(report['host_start'], str) and report['host_start'].isdigit()
    # Native host commands start a separate process group. Check every member,
    # including zombies, using public stat identity only (no argv/environ).
    for leaf in Path('/proc').glob('[0-9]*/stat'):
        try:
            parts = leaf.read_text().rsplit(') ', 1)[1].split()
        except FileNotFoundError:
            continue
        assert int(parts[2]) != spawn['pid'], 'Owned host command group survived completion'
    assert not list((root / '.collrun').glob('forward-*'))
    return body, root


@pytest.fixture
def claude_host_runner(api, request):
    """Explicitly released real host gate under the selected API account."""
    if os.environ.get('COLLIMATE_LIVE_NATIVE_ACQUISITION') != '1':
        pytest.skip('Claude D23 host gate needs explicit root release')
    if not os.environ.get('ANTHROPIC_API_KEY'):
        pytest.skip('Claude D23 host gate needs selected ANTHROPIC_API_KEY')
    selected = request.getfixturevalue('claude_code')
    owner = next(row for row in api.request('GET', '/api/accounts')['accounts']
                 if row['id'] == selected['credential_account_id'])
    assert owner['service'] == 'anthropic' and owner['auth_type'] == 'api_key'
    model = model_entry(api, owner, 'D23 Claude host ' + uuid.uuid4().hex[:6],
                        'claude-haiku-4-5-20251001', 'claude-code')
    saved = []
    def create(commands):
        generator, provenance = _publish_observed_agent(api, 'claude-code')
        template = api.request('GET', f'/api/runner_generators/{generator}/configs/default')
        runner = {**template['runner'], 'id': 'd23-host-' + uuid.uuid4().hex[:10],
            'name': 'D23 Claude host', 'generator': generator, 'prompt': '',
            'accounts': {'model': model['id']},
            'permissions': {'docker': 'none', 'internet': 'full', 'commands': commands},
            'packages': HOST_NATIVE_PINS['claude-code']}
        runner['settings'] = {**runner['settings'], 'agent-config.set_yaml': {
            **runner['settings']['agent-config.set_yaml'], 'surface': 'chat', 'backend': 'claude-code',
            'approvals': 'auto'}}
        api.request('POST', '/api/runners', {'runner': runner, 'files': template['files']})
        saved.append(runner['id'])
        actual = next(row for row in api.request('GET', '/api/runners') if row['id'] == runner['id'])
        assert not actual.get('invalid'), actual
        return actual, provenance
    try:
        yield create
    finally:
        api.stop_started_runs()
        for identity in saved:
            api.request('DELETE', '/api/runners/' + urllib.parse.quote(identity))
        delete_accounts(api, model['id'])


@pytest.mark.parametrize('commands', ['ask', 'allow'])
def test_claude_host_generator_publication_and_approval_D23(api, claude_host_runner, commands, evidence):
    """Actual host child/publication and measured full native proof for both turns."""
    runner, provenance = claude_host_runner(commands)
    files = _draft_files()
    config = next(row for row in files if row['name'] == 'draft/main.set_yaml')
    config['content'] = yaml.safe_dump({'fields': [*yaml.safe_load(config['content'])['fields'],
        {'key': 'execution_locus', 'type': 'text', 'default': 'host'},
        {'key': 'expected_sdk', 'type': 'text', 'default': ''}], 'values': {}})
    cell = api.new_cell('D23 Claude host', files)
    run = api.start_runner(cell, runner)
    wire = ChatWire(api, cell, run)
    tag = 'd23-claude-host-' + commands + '-' + run[:8]
    cards = []
    try:
        api.wait_idle(run, timeout=180)
        location = _json(api.wait_until(lambda: api.run_log(run, '_agent/d23-host/location.json'),
                                       30, 'own host location', run_id=run))
        assert location['run_id'] == run and location['cell_id'] == cell and location['provenance'] == provenance
        root = Path(location['workspace'])
        assert root.name.startswith('ws_' + run[:8] + '_') and root.is_dir()
        private = 'private-host-child-' + uuid.uuid4().hex
        settings = {'overlay': 'child-value', 'marker': 'host-' + uuid.uuid4().hex,
            'parent_run_id': run, 'cell_id': cell, 'expected_prompt': private, 'commands': commands,
            'expected_mount': str(root), 'expected_sdk': location['sdk'], 'execution_locus': 'host'}
        def answer(frame, choice):
            assert frame['title'] == 'Run test_generator' and frame['kind'] == 'execute'
            assert _json(frame['detail']) == {'path': CHILD_PATH, 'settings': settings, 'prompt': private}
            _no_host_child(api, cell, run, provenance)
            assert choice in {row['option_id'] for row in frame['options']}
            cards.append(choice)
            return choice
        if commands == 'ask':
            rejected = wire.turn(_generator_tool_prompt('claude-code', settings, private),
                permission=lambda frame: answer(frame, 'reject_once'))
            # Diagnostic app trace is separate from raw/native completion.
            refusal = _tool_call(rejected, settings, private, _full_trace(api, run, 1), 1)
            rejected_envelope = api.wait_until(
                lambda: api.run_log(run, '_agent/d23-native/turn-0001.json'),
                30, 'own rejected full native turn', run_id=run)
            native_refusal = _native_tool_value(rejected, settings, private, _json(rejected_envelope),
                run=run, cell=cell, backend='claude-code', ordinal=1, provenance=provenance)
            assert json.dumps(native_refusal, sort_keys=True, allow_nan=False) == json.dumps(
                refusal, sort_keys=True, allow_nan=False)
            assert refusal['error'] == 'approval_refused'
            _no_host_child(api, cell, run, provenance)
            api.wait_idle(run, timeout=120)
        prompt = _generator_tool_prompt('claude-code', settings, private)
        if commands == 'ask':
            prompt = 'This is a NEW authorized attempt; I will approve its new card. ' + prompt
        accepted = wire.turn(prompt, permission=(lambda frame: answer(frame, 'allow_once')) if commands == 'ask' else None)
        ordinal = 2 if commands == 'ask' else 1
        diagnostic = _tool_call(accepted, settings, private, _full_trace(api, run, ordinal), ordinal)
        envelope = _json(api.wait_until(
            lambda: api.run_log(run, f'_agent/d23-native/turn-{ordinal:04d}.json'),
            30, 'own accepted full native turn', run_id=run))
        native_value = _native_tool_value(accepted, settings, private, envelope, run=run, cell=cell,
            backend='claude-code', ordinal=ordinal, provenance=provenance)
        assert json.dumps(native_value, sort_keys=True, allow_nan=False) == json.dumps(
            diagnostic, sort_keys=True, allow_nan=False)
        assert diagnostic['exit_code'] == 0 and diagnostic['result']['success'] is True
        assert 'D23-CHILD-COMMITTED ' + settings['marker'] in diagnostic['output']
        api.wait_file(cell, PROOF, timeout=30, run_id=run)
        api.wait_idle(run, timeout=120)
        report = _json(api.cell_file(cell, PROOF))
        observation, root = _host_child_return(api, cell, run, provenance, report)
        assert report['marker'] == settings['marker'] and report['run_id'] == run and report['cell_id'] == cell
        assert report['commands'] == commands and report['docker'] == 'none'
        assert report['keep'] == 'base-value' and report['overlay'] == 'child-value'
        assert report['private_prompt_hash'] == hashlib.sha256(private.encode()).hexdigest()
        assert report['refused'] == ['account'] and report['dependency'] == '6.0.3'
        assert report['verifier_refused'] is True and 'child-invalid.options_stack' not in api.cell_paths(cell)
        version = _committed_proof(api, cell, run, PROOF, report)
        partial = _committed_proof(api, cell, run, PROOF, {**report, 'verifier_refused': False})
        assert version != partial
        assert any(event.get('type') == 'ipc_error' and 'child-invalid.options_stack: handler_invalid' in str(event.get('payload'))
                   for event in api.run_events(run).get('events', []))
        parent = yaml.safe_load((root / '.collrun/config/files/agent-config.set_yaml').read_text())
        assert parent['values']['backend'] == 'claude-code' and 'overlay' not in parent['values']
        parent_prompt = root / '.collrun/prompt.md'
        assert not parent_prompt.exists() or parent_prompt.read_text() == ''
        for row in files:
            assert api.cell_file(cell, row['name']) == row['content']
        assert api.run_state(run)['is_active']
        assert cards == (['reject_once', 'allow_once'] if commands == 'ask' else [])
        evidence.text(tag + '-host-publication.json', {'report': report, 'observation': observation,
            'version': version, 'partial_version': partial, 'cards': cards, 'native_gate_passed': False})
    finally:
        try:
            evidence.text(tag + '-wire.json', wire.frames)
            evidence.text(tag + '-events.json', api.run_events(run))
            evidence.text(tag + '-trace.json', api.run_log(run, '_agent/trace.json'))
            evidence.text(tag + '-host-observation.json', api.run_log(run, '_agent/d23-host/observation.json'))
            for ordinal in (1, 2) if commands == 'ask' else (1,):
                evidence.text(tag + f'-native-{ordinal}.json', api.run_log(run, f'_agent/d23-native/turn-{ordinal:04d}.json'))
        finally:
            try:
                wire.close()
            finally:
                api.stop_run(run)
                api.wait_finished(run, timeout=180)
    # Persist acceptance only after all publication/native/artifact and close
    # stages return successfully. Failed paths retain the earlier false receipt.
    evidence.text(tag + '-host-publication.json', {'report': report, 'observation': observation,
        'version': version, 'partial_version': partial, 'cards': cards, 'native_gate_passed': True})


class ChatWire:
    """Actual renderer protocol, with exact command-card answers recorded."""
    def __init__(self, api, cell, run):
        from websockets.sync.client import connect
        info = api.wait_until(lambda: api.surface(cell, api.run_path(run, CHAT_POINTER)),
                              180, 'agent chat registration', run_id=run)
        url = api.base.replace('http://', 'ws://', 1) + info['ws_path']
        self.socket = connect(url + '?token=' + urllib.parse.quote(info['token']),
                              proxy=None, open_timeout=20, max_size=4 * 1024 * 1024)
        self.frames = []

    def close(self):
        self.socket.close()

    def turn(self, prompt, *, permission=None, timeout=420):
        self.socket.send(json.dumps({'t': 'user_msg', 'content': prompt, 'thread_id': 'main'}))
        deadline, started = time.monotonic() + timeout, False
        frames = []
        while time.monotonic() < deadline:
            frame = json.loads(self.socket.recv(timeout=max(.01, deadline - time.monotonic())))
            frames.append(frame)
            self.frames.append(frame)
            kind = frame.get('t')
            if kind == 'turn_start':
                started = True
            if kind == 'permission_request':
                assert permission is not None, f'Unexpected approval request: {frame}'
                option = permission(frame)
                self.socket.send(json.dumps({'t': 'permission_answer', 'id': frame['id'],
                                             'option_id': option}))
            if kind == 'error':
                raise AssertionError(frame)
            if kind == 'turn_end' and started:
                return frames
        raise AssertionError('The actual model turn did not finish')


def _tool_call(frames, settings, prompt, trace, ordinal):
    """Pair native IDs with full trace data; live display previews are capped."""
    calls = [frame for frame in frames if frame.get('t') == 'tool' and frame.get('id')]
    assert len({frame['id'] for frame in calls}) == 1, frames
    names = {frame['name'] for frame in calls if frame.get('name')}
    assert len(names) == 1, calls
    name = names.pop()
    # PAI carries the function name. Goose 1.52 appends the exact path
    # argument to its ACP title; keep that target part of the attribution.
    assert name in {'test_generator', f'collimate: test generator · {CHILD_PATH}',
                    'mcp__collimate__test_generator'}, name
    results = [frame for frame in calls if frame.get('status') in ('done', 'error')]
    assert results and results[-1]['status'] == 'done', calls
    events = [row for row in trace if row.get('type') in ('tool_use', 'tool_result')]
    assert [row['type'] for row in events] == ['tool_use', 'tool_result'] * ordinal, events
    use, result = events[-2:]
    assert use['tool'] == name, (use, calls)
    if 'tool' in result:
        assert result['tool'] == name, (result, calls)
    expected_input = {'path': CHILD_PATH, 'settings': settings, 'prompt': prompt}
    if name == 'mcp__collimate__test_generator':
        # Claude's measured trace opens with empty input; display inputs are
        # capped previews. The strict raw native decoder binds every argument.
        assert _json(use['input']) == {}, use
    else:
        assert _json(use['input']) == expected_input, use
    raw = result['output']
    try:
        output = _json(raw)
    except ValueError:
        # PAI records str(dict); literal_eval parses data without executing it.
        output = ast.literal_eval(raw)
    assert isinstance(output, dict), output
    if name == 'mcp__collimate__test_generator':
        assert set(output) == {'result'} and isinstance(output['result'], dict), output
        output = output['result']
    return output


def _full_trace(api, run, ordinal):
    def complete():
        trace = _json(api.run_log(run, '_agent/trace.json') or [])
        count = sum(row.get('type') in ('tool_use', 'tool_result') for row in trace)
        return trace if count >= ordinal * 2 else None
    return api.wait_until(complete, 30, 'full native tool trace', run_id=run)


def _draft_files():
    definition = {'id': 'd23-draft', 'name': 'D23 draft', 'summary': 'D23 child fixture', 'entrypoint': 'main.py',
                  'main_config': 'main.set_yaml', 'draft_files': ['main.set_yaml'],
                  'ui_files': ['.chat'], 'live_files_mode': 'manual'}
    fields = [{'key': key, 'type': 'text', 'default': value} for key, value in {
        'keep': 'base-value', 'overlay': 'base-value', 'marker': '', 'parent_run_id': '',
        'cell_id': '', 'expected_prompt': '', 'commands': '', 'expected_mount': ''}.items()]
    return [
        {'name': CHILD_PATH, 'content': yaml.safe_dump(definition, sort_keys=False)},
        {'name': 'draft/main.py', 'content': (FIXTURES / 'generator_tool_child.py').read_text()},
        {'name': 'draft/main.set_yaml', 'content': yaml.safe_dump({'fields': fields, 'values': {}})},
    ]


@pytest.fixture(params=HARNESSES)
def generator_runner(api, provider_model, request):
    if not DOCKER_GATE:
        pytest.skip('D23 command-placement proof requires COLLIMATE_LIVE_DOCKER=1')
    harness = request.param
    assert harness in {'goose', *NATIVE_PINS}, 'Only reviewed container harnesses belong to this fixture'
    model = model_entry(api, {'id': provider_model['credential_account_id']},
                        f'D23 {harness} {uuid.uuid4().hex[:6]}', provider_model['model'], harness)
    saved = []
    observed = {}
    def create(commands):
        generator = OBSERVED_GENERATOR
        if harness in NATIVE_PINS:
            generator, provenance = _publish_observed_agent(api, harness)
            observed[generator] = provenance
        template = api.request('GET', f'/api/runner_generators/{generator}/configs/default')
        runner = {**template['runner'], 'id': 'd23-' + uuid.uuid4().hex[:10],
                  'name': f'D23 {harness} {commands}', 'generator': generator,
                  'prompt': '',
                  'accounts': {'model': model['id']},
                  'permissions': {'docker': 'full', 'internet': 'full', 'commands': commands},
                  'packages': {'goose': '1.52.0'} if harness == 'goose' else NATIVE_PINS.get(harness, {})}
        runner['settings'] = {**runner['settings'], 'agent-config.set_yaml': {
            **runner['settings']['agent-config.set_yaml'], 'surface': 'chat', 'backend': harness,
            'approvals': 'auto'}}
        api.request('POST', '/api/runners', {'runner': runner, 'files': template['files']})
        saved.append(runner['id'])
        actual = next(row for row in api.request('GET', '/api/runners') if row['id'] == runner['id'])
        assert not actual.get('invalid'), actual
        actual['_d23_observation'] = observed.get(generator)
        return actual
    try:
        yield harness, create
    finally:
        api.stop_started_runs()
        for identity in saved:
            api.request('DELETE', '/api/runners/' + urllib.parse.quote(identity))
        delete_accounts(api, model['id'])


@pytest.mark.parametrize('commands', ['ask', 'allow'])
def test_real_agent_generator_command_publication_and_approval_D23(api, generator_runner, commands, evidence):
    harness, create = generator_runner
    runner = create(commands)
    files = _draft_files()
    cell = api.new_cell(f'D23 {harness} {commands}', files)
    run = api.start_runner(cell, runner)
    wire = ChatWire(api, cell, run)
    tag = f'd23-{harness}-{commands}-{run[:8]}'
    cards = []
    try:
        api.wait_idle(run, timeout=180)
        for row in files:
            verdict = api.request('POST', f'/api/channels/{api.chq}/cells/{cell}/validate',
                                  {'path': row['name']})
            assert verdict.get('valid') is True, verdict
        expected_private = 'private-child-' + uuid.uuid4().hex
        settings = {'overlay': 'child-value', 'marker': 'accepted-' + uuid.uuid4().hex,
                    'parent_run_id': run, 'cell_id': cell, 'expected_prompt': expected_private,
                    'commands': commands,
                    'expected_mount': '/workspace'}
        def prompt():
            return _generator_tool_prompt(harness, settings, expected_private)
        def answer(frame, choice):
            assert frame.get('title') == 'Run test_generator' and frame.get('kind') == 'execute', frame
            detail = _json(frame.get('detail'))
            assert detail == {'path': CHILD_PATH, 'settings': settings, 'prompt': expected_private}, frame
            _no_child(api, cell, run)
            choices = {row['option_id'] for row in frame['options']}
            assert choice in choices
            cards.append({'id': frame['id'], 'choice': choice, 'child_absent_before_answer': True})
            return choice
        if commands == 'ask':
            rejected = wire.turn(prompt(), permission=lambda frame: answer(frame, 'reject_once'))
            result = _app_tool_value(api, run, cell, harness, rejected, settings, expected_private,
                                     1, runner.get('_d23_observation'))
            assert result.get('error') == 'approval_refused', result
            _no_child(api, cell, run)
            assert len(cards) == 1 and cards[0]['choice'] == 'reject_once', cards
            api.wait_idle(run, timeout=120)
        next_prompt = prompt()
        if commands == 'ask':
            next_prompt = ('The previous attempt was rejected. This is a NEW authorized test attempt; '
                           'I will approve its new command card. ' + next_prompt)
        accepted, containers = _observed_turn(run, lambda: wire.turn(
            next_prompt, permission=(lambda frame: answer(frame, 'allow_once'))
            if commands == 'ask' else None))
        ordinal = 2 if commands == 'ask' else 1
        completed = _app_tool_value(api, run, cell, harness, accepted, settings, expected_private,
                                    ordinal, runner.get('_d23_observation'))
        assert completed['exit_code'] == 0 and completed['result']['success'] is True, completed
        assert 'D23-CHILD-COMMITTED ' + settings['marker'] in completed['output'], completed
        api.wait_file(cell, PROOF, timeout=30, run_id=run)
        api.wait_idle(run, timeout=120)
        report = _json(api.cell_file(cell, PROOF))
        assert report['marker'] == settings['marker'] and report['run_id'] == run and report['cell_id'] == cell
        assert report['commands'] == commands and report['docker'] == 'full'
        assert report['overlay'] == 'child-value' and report['keep'] == 'base-value'
        assert report['private_prompt_hash'] == hashlib.sha256(expected_private.encode()).hexdigest()
        assert report['refused'] == ['account'] and report['dependency'] == '6.0.3'
        assert report['verifier_refused'] is True
        assert 'child-invalid.options_stack' not in api.cell_paths(cell)
        version = _committed_proof(api, cell, run, PROOF, report)
        partial_version = _committed_proof(api, cell, run, PROOF, {**report, 'verifier_refused': False})
        assert partial_version != version
        events = api.run_events(run)
        assert any(event.get('type') == 'ipc_error'
                   and 'child-invalid.options_stack: handler_invalid' in str(event.get('payload'))
                   for event in events.get('events', [])), events
        container = _observed_container(containers, report, harness)
        root = _workspace(containers)
        assert not list((root / '.collrun').glob('forward-*'))
        parent_config = yaml.safe_load((root / '.collrun/config/files/agent-config.set_yaml').read_text())
        assert parent_config['values']['backend'] == harness
        assert 'overlay' not in parent_config['values']
        parent_prompt = root / '.collrun/prompt.md'
        assert not parent_prompt.exists() or parent_prompt.read_text() == ''
        for row in files:
            assert api.cell_file(cell, row['name']) == row['content'], 'The model changed the tested draft'
        assert api.run_state(run)['is_active'], 'Child finish must not finish the parent run'
        assert [card['choice'] for card in cards] == (['reject_once', 'allow_once'] if commands == 'ask' else [])
        evidence.text(tag + '-proof.json', {'report': report, 'version': version,
                                          'partial_version': partial_version, 'cards': cards,
                                          'container_id': container['id']})
    finally:
        # Surface credentials never enter evidence. Permission payloads contain
        # only synthetic fixture inputs; provider secrets remain host-owned.
        try:
            evidence.text(tag + '-wire.json', wire.frames)
            evidence.text(tag + '-events.json', api.run_events(run))
            evidence.text(tag + '-trace.json', api.run_log(run, '_agent/trace.json'))
            if harness in NATIVE_PINS:
                for ordinal in (1, 2) if commands == 'ask' else (1,):
                    evidence.text(tag + f'-native-{ordinal}.json',
                                  api.run_log(run, f'_agent/d23-native/turn-{ordinal:04d}.json'))
        finally:
            try:
                wire.close()
            finally:
                api.stop_run(run)
                api.wait_finished(run, timeout=180)


def test_real_app_denied_tool_is_omitted_and_direct_call_refused_D23(api, evidence):
    """Deterministic worker control, explicitly not a native model-intake claim."""
    agent = next(row for row in api.request('GET', '/api/generators') if row['name'] == 'agent')
    source_path = next(row['path'] for row in api.cell_files(agent['cell_id'])
                       if row['path'] == '_agent_shared/generator_test.py' and not row['deleted'])
    tool_source = api.cell_file(agent['cell_id'], source_path)
    assert isinstance(tool_source, str)
    identity = 'd23-deny-' + uuid.uuid4().hex[:10]
    package = [
        {'name': f'{identity}/{identity}.generator', 'content': yaml.safe_dump({
            'id': identity, 'name': 'D23 denied control', 'summary': 'D23 denied command control', 'entrypoint': 'main.py',
            'main_config': 'main.set_yaml', 'draft_files': ['main.set_yaml'],
            'enforces': ['commands']})},
        {'name': f'{identity}/main.py',
         'content': (FIXTURES / 'generator_tool_deny_parent.py').read_text()},
        {'name': f'{identity}/generator_test.py', 'content': tool_source},
        {'name': f'{identity}/main.set_yaml', 'content': 'fields: []\nvalues: {}\n'},
    ]
    source_cell = api.new_cell('D23 deterministic parent package', package)
    _add_to_capabilities(api, source_cell, f'{identity}/{identity}.generator')
    template = api.request('GET', f'/api/runner_generators/{identity}/configs/default')
    runner = {**template['runner'], 'id': identity, 'name': 'D23 denied control',
              'generator': identity, 'permissions': {'commands': 'deny', 'docker': 'none', 'internet': 'full'}}
    api.request('POST', '/api/runners', {'runner': runner, 'files': template['files']})
    saved = next(row for row in api.request('GET', '/api/runners') if row['id'] == identity)
    assert not saved.get('invalid'), saved
    cell = api.new_cell('D23 denied target', [
        {'name': 'draft/probe.generator', 'content': 'entrypoint: main.py\n'},
        {'name': 'draft/main.py', 'content': 'from pathlib import Path\nPath("child-was-run.txt").write_text("bad")\n'},
    ])
    try:
        run = api.start_runner(cell, saved)
        api.wait_finished(run, timeout=180)
        state = api.run_state(run)
        assert state['status'] == 'completed', state
        proof = _json(api.cell_file(cell, 'denied-proof.json'))
        assert proof == {
            'run_id': run, 'commands': 'deny', 'tool_names': [],
            'direct_result': {'error': 'commands_denied'}, 'private_children': [],
            'source_hash': hashlib.sha256(tool_source.encode()).hexdigest(),
        }
        assert 'child-was-run.txt' not in api.cell_paths(cell)
        version = _committed_proof(api, cell, run, 'denied-proof.json', proof)
        evidence.text('d23-denied-control.json', {'proof': proof, 'version': version})
    finally:
        api.stop_started_runs()
        api.request('DELETE', '/api/runners/' + urllib.parse.quote(identity))


def test_real_app_exact_container_child_public_sdk_permissions_P4F30_P4F31_D23(api, evidence):
    """No provider: real worker/container/uv/IPC and immutable host publication."""
    if not DOCKER_GATE:
        pytest.skip('Requires explicit COLLIMATE_LIVE_DOCKER=1')
    sdk = Path(os.environ['COLLIMATE_LIVE_SDK_SOURCE'])
    agent = next(row for row in api.request('GET', '/api/generators') if row['name'] == 'agent')
    accepted = {}
    for row in api.cell_files(agent['cell_id']):
        if row['deleted']:
            continue
        path = row['path']
        if path.startswith(('_agent_shared/', '_env_shared/')):
            text = api.cell_file(agent['cell_id'], path)
            assert isinstance(text, str), path
            accepted[path] = text
    assert '_agent_shared/generator_test.py' in accepted
    # Pre-vendor candidates are explicit; every other shared file remains
    # byte-identical to its accepted version. Both candidate hashes are checked
    # in the actual parent result together with the complete accepted mapping.
    candidates = ('_env_shared/base/Dockerfile', '_env_shared/__init__.py',
                  '_agent_shared/generator_test.py')
    candidate_hashes = {}
    for path in candidates:
        accepted[path] = (sdk.parent / 'content/generators' / path).read_text()
        candidate_hashes[path] = hashlib.sha256(accepted[path].encode()).hexdigest()
    source_hashes = {path: hashlib.sha256(text.encode()).hexdigest()
                     for path, text in accepted.items()}
    # The thin agent entrypoint carries the Agent generator's PEP 723 header.
    agent_main = api.cell_file(agent['cell_id'], 'agent/agent.py')
    pep = agent_main[:agent_main.index('# ///', len('# ///')) + len('# ///')]
    identity = 'd23-container-' + uuid.uuid4().hex[:10]
    manifest = {'id': identity, 'name': 'D23 container control',
                'summary': 'Unpaid exact child real-host publication', 'entrypoint': 'main.py',
                'main_config': 'main.set_yaml', 'draft_files': ['main.set_yaml'],
                'enforces': ['commands'], 'live_files_mode': 'manual'}
    source_files = {**accepted, f'{identity}.generator': yaml.safe_dump(manifest),
                    'main.py': pep + '\n' + (FIXTURES / 'generator_tool_container_parent.py').read_text(),
                    'main.set_yaml': 'fields: []\nvalues: {}\n'}
    package = [{'name': f'{identity}/{path}', 'content': text} for path, text in source_files.items()]
    source_cell = api.new_cell('D23 exact container parent', package)
    _add_to_capabilities(api, source_cell, f'{identity}/{identity}.generator')
    template = api.request('GET', f'/api/runner_generators/{identity}/configs/default')
    runner = {**template['runner'], 'id': identity, 'name': 'D23 exact container control',
              'generator': identity, 'permissions': {'commands': 'allow', 'docker': 'full', 'internet': 'full'}}
    api.request('POST', '/api/runners', {'runner': runner, 'files': template['files']})
    saved = next(row for row in api.request('GET', '/api/runners') if row['id'] == identity)
    assert not saved.get('invalid'), saved
    files = _draft_files()
    cell = api.new_cell('D23 exact container target', files)
    run = api.start_runner(cell, saved)
    try:
        api.wait_file(cell, 'parent-result.json', timeout=600, run_id=run)
        parent = _json(api.cell_file(cell, 'parent-result.json'))
        evidence.text('d23-unpaid-parent.json', parent)
        assert parent['source_hashes'] == source_hashes
        assert parent['remaining_child_processes'] == [], parent
        assert parent['import_proof']['exit_code'] == 0, parent
        imported = _json(parent['import_proof']['output'])
        assert imported == {'uid': parent['uid'], 'main': True, 'owner': 0,
                            'writable': False, 'directory_writable': False}
        completed = parent['result']
        assert completed['exit_code'] == 0 and completed['result']['success'] is True, completed
        settings = parent['settings']
        assert 'D23-CHILD-COMMITTED ' + settings['marker'] in completed['output']
        report = _json(api.cell_file(cell, PROOF))
        assert report['marker'] == settings['marker'] and report['run_id'] == run and report['cell_id'] == cell
        assert report['commands'] == 'allow' and report['docker'] == 'full'
        assert report['overlay'] == 'child-value' and report['keep'] == 'base-value'
        assert report['private_prompt_hash'] == hashlib.sha256(settings['expected_prompt'].encode()).hexdigest()
        assert report['refused'] == ['account'] and report['dependency'] == '6.0.3'
        assert report['verifier_refused'] is True
        assert 'child-invalid.options_stack' not in api.cell_paths(cell)
        version = _committed_proof(api, cell, run, PROOF, report)
        partial = _committed_proof(api, cell, run, PROOF, {**report, 'verifier_refused': False})
        assert version != partial
        events = api.run_events(run)
        assert any(event.get('type') == 'ipc_error'
                   and 'child-invalid.options_stack: handler_invalid' in str(event.get('payload'))
                   for event in events.get('events', [])), events
        containers = _run_containers(run)
        matching = [row for row in containers if row['hostname'] == report['hostname']]
        assert len(matching) == 1 and matching[0]['kind'] == 'generator_test', containers
        assert any(m['target'] == report['workspace'] for m in matching[0]['mounts']), matching
        assert report['workspace'] == '/work'
        root = _workspace(matching)
        assert not list((root / '.collrun').glob('forward-*'))
        assert (root / '.collrun/config/files/main.set_yaml').read_text() == source_files['main.set_yaml']
        prompt = root / '.collrun/prompt.md'
        assert not prompt.exists() or prompt.read_text() == ''
        for row in files:
            assert api.cell_file(cell, row['name']) == row['content']
        assert api.run_state(run)['is_active']
        evidence.text('d23-unpaid-publication.json', {'version': version, 'partial_version': partial,
            'report': report, 'container_id': matching[0]['id'], 'source_hashes': source_hashes,
            'candidate_source_hashes': candidate_hashes})
    finally:
        try:
            evidence.text('d23-unpaid-events.json', api.run_events(run))
        finally:
            api.stop_run(run)
            api.wait_finished(run, timeout=180)
            api.request('DELETE', '/api/runners/' + urllib.parse.quote(identity))


@pytest.mark.skipif(not DOCKER_GATE, reason='Unpaid witness requires COLLIMATE_LIVE_DOCKER=1')
def test_unpaid_short_lived_container_observation_D23(tmp_path):
    """Real Docker identity/bind captured before explicit own-container removal."""
    import docker
    from docker.errors import NotFound
    client = docker.from_env(timeout=5)
    run = 'd23-observation-' + uuid.uuid4().hex
    containers = []
    report_path = tmp_path / 'actual-container.json'
    image = 'collimate-linux-small:0f66c6c76b1f'
    script = ('import json,pathlib,socket,time; '
              'pathlib.Path("/work/actual-container.json").write_text('
              'json.dumps({"hostname":socket.gethostname(),"workspace":"/work"})); '
              'time.sleep(.3)')
    def operation():
        container = client.containers.run(image, ['python3', '-c', script], detach=True,
            user=f'{os.getuid()}:{os.getgid()}',
            volumes={str(tmp_path): {'bind': '/work', 'mode': 'rw'}},
            labels={'com.collimate.run_id': run, 'com.collimate.kind': 'observation_probe'})
        containers.append(container)
        result = container.wait(timeout=15)
        assert result['StatusCode'] == 0
        container.remove()
        return json.loads(report_path.read_text())
    try:
        report, observed = _observed_turn(run, operation)
        container = _observed_container(observed, report, 'observation_probe')
        assert container['id'] == containers[0].id
        assert _workspace(observed) == tmp_path
        assert _run_containers(run) == [], 'Own short-lived container must already be gone'
    finally:
        for container in containers:
            try:
                container.remove(force=True)
            except NotFound:
                pass
        client.close()
