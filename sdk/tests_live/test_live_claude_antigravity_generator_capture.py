# SPDX-License-Identifier: MIT
"""Actual Claude full native D23 gates.

Prepared ACP, SDK child execution/publication and private ACP evidence.
Product preparation and root-reviewed private account setup precede use.

The AGY rows were measured against Collimate's retired ACP shim (its
stream-json reader tap and its ``call_mcp_tool`` step shape); Google's own
``antigravity-acp`` server, which replaced it, reports other shapes and needs
its own acquisition.
"""
import asyncio
from copy import deepcopy
import json
import os
import uuid

import gen
import pytest

from _agent_shared import acp_run, generator_test
from _agent_shared.harnesses import HARNESS
from tests_live.conftest import bind_model_account, eng, model_account, run_on_test_loop
from tests_live.fixtures.acp_capture import Capture, observed_profile
from tests_live.test_live_codex_qwen_generator_tool import command_answer, serving_cards
from tests_live.test_live_opencode_generator_tool import install_forward_recording_ack


ROUTES = {
    'claude-code': {'provider': 'anthropic', 'model': 'claude-haiku-4-5-20251001',
        'key_env': 'ANTHROPIC_API_KEY', 'locus': 'host', 'native_version': '2.1.310',
        'pins': {'claude-agent-acp': '0.95.0'}, 'prepare_suffix': ':prepare'},
}
MAX_PRIVATE_BYTES = 32 * 1024 * 1024
pytestmark = pytest.mark.usefixtures('one_loop_per_test')


class AcquisitionCapture(Capture):
    """Retain all ACP envelopes beside the unchanged selective tool observer."""
    def __init__(self, **kwargs):
        self.secret_observed = False
        self.wire_records, self.wire_bytes = [], 0
        super().__init__(**kwargs)

    def _scrub(self, value):
        if isinstance(value, str) and any(secret in value for secret in self.secrets):
            self.secret_observed = True
        return super()._scrub(value)

    def observe(self, direction, message):
        value = {'direction': direction, 'message': self._scrub(deepcopy(message))}
        size = len(json.dumps(value, ensure_ascii=False).encode())
        if len(self.wire_records) >= self.max_records or self.wire_bytes + size > self.max_bytes:
            raise AssertionError('complete ACP wire capture exceeds bound')
        self.wire_records.append(value)
        self.wire_bytes += size
        return super().observe(direction, message)


def native_generator_gate(capture, backend, arguments, child_result, marker, level):
    """Use the measured strict decoder and compare the actual host result whole."""
    if level == 'ask-reject':
        proof, value = capture.completed_generator_value(backend, expected_input=arguments)
        assert value.get('error') == 'approval_refused'
    else:
        proof, value = capture.completed_generator_result(backend, expected_input=arguments, marker=marker)
    assert json.dumps(value, sort_keys=True, ensure_ascii=False) == json.dumps(
        child_result, sort_keys=True, ensure_ascii=False), 'Native complete result differs from actual SDK child boundary'
    return proof, value


def route_for(backend):
    if backend not in ROUTES:
        raise ValueError('unsupported explicit acquisition route')
    return deepcopy(ROUTES[backend])


def write_evidence(path, value, selected_key):
    # Preserve full data unless a selected credential appears; scrub and mark
    # refusal in that case, never put the credential into retained logs.
    body = json.dumps(value, ensure_ascii=False).encode()
    secret = selected_key.encode()
    leaked = bool(secret and secret in body)
    if leaked:
        body = body.replace(secret, b'[REDACTED]')
    if len(body) > MAX_PRIVATE_BYTES:
        raise AssertionError('acquisition evidence exceeds bound')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(body)
    if leaked:
        raise AssertionError('selected credential observed; scrubbed evidence retained')


def install_profile_capture(monkeypatch, backend, capture, profiles):
    route = route_for(backend)
    # The actual generic dispatcher survives both held migrations; the deleted
    # legacy owner is never a supported route for this acquisition.
    owner = HARNESS[backend].prepare
    assert owner.endswith(route['prepare_suffix']) and 'acp_run:' not in owner
    original = acp_run._PROFILES[backend]

    async def prepare(context):
        profile = await original(context)
        assert profile.locus == route['locus']
        assert profile.cwd == (gen.workspace_path() if route['locus'] == 'host' else '/workspace')
        profiles.append({'locus': profile.locus, 'cwd': profile.cwd, 'owner': owner})
        return observed_profile(profile, capture)

    monkeypatch.setitem(acp_run._PROFILES, backend, prepare)


def install_offered_inventory(monkeypatch, servers):
    """Observe actual bridge registration; do not invoke any registered tool."""
    from mcp.server.mcpserver import MCPServer
    original = MCPServer.add_tool

    def add(server, *args, **kwargs):
        result = original(server, *args, **kwargs)
        if not any(server is item for item in servers):
            servers.append(server)
        return result

    monkeypatch.setattr(MCPServer, 'add_tool', add)


async def offered_inventory(servers):
    return [[tool.model_dump(mode='json', by_alias=True) for tool in await server.list_tools()]
            for server in servers]


@pytest.mark.parametrize('backend,file_tools', [('claude-code', False)], ids=['claude-code'])
@pytest.mark.parametrize('level', ['allow', 'ask-reject', 'ask-allow'])
def test_actual_claude_antigravity_generator_native_D23(request, monkeypatch, tmp_path, backend, file_tools, level):
    route = route_for(backend)
    selected_key = os.environ.get(route['key_env'], '')
    if not selected_key:
        pytest.skip('needs selected ' + route['key_env'] + ' API account')
    if os.environ.get('COLLIMATE_LIVE_NATIVE_ACQUISITION') != '1':
        pytest.skip('needs explicit root-owned native acquisition release')
    if route['locus'] == 'container' and os.environ.get('COLLIMATE_LIVE_DOCKER') != '1':
        pytest.skip('needs authorized Docker fixture')
    bridge = request.getfixturevalue('native_bridge' if route['locus'] == 'host' else 'docker_env_bridge')
    bridge.channel = 'p4-d23-' + backend
    bridge.cell_id = 'p4-d23-' + backend + '-cell'
    commands = 'allow' if level == 'allow' else 'ask'
    bridge.permissions = {'docker': 'none' if route['locus'] == 'host' else 'full',
                          'internet': 'full', 'commands': commands}
    bridge.packages = dict(route['pins'])
    install_forward_recording_ack(bridge, monkeypatch)
    bind_model_account(model_account(route['provider'], model=route['model'], harness=backend))
    host = None
    if route['locus'] == 'host':
        from collimate.host_harness import probe
        found = probe(backend)
        assert found is not None and not found.blocker, 'selected machine Claude unavailable'
        assert found.version == route['native_version'], 'machine Claude version differs from approved acquisition'
        host = {'path': found.path, 'version': found.version, 'binary': found.binary}
    capture = AcquisitionCapture(secrets=(selected_key,))
    profiles, cards, child_results, executions, servers = [], [], [], [], []
    install_offered_inventory(monkeypatch, servers)
    install_profile_capture(monkeypatch, backend, capture, profiles)
    marker = 'd23-' + backend + '-' + uuid.uuid4().hex
    arguments = {'path': 'draft/probe.generator', 'settings': {},
                 'prompt': 'private-native-child-' + uuid.uuid4().hex + '-full-input-' * 35}
    gen.update_file(arguments['path'], 'id: native-probe\nname: Native probe\nsummary: D23 acquisition\nentrypoint: main.py\n')
    gen.update_file('draft/main.py', 'import gen\nasync def run():\n'
        f'    gen.update_file("native-child.txt", {marker!r})\n'
        '    await gen.commit()\n'
        f'    print({marker!r})\n'
        f'    print({("full-child-output-" * 80)!r})\n'
        'gen.main(run)\n')
    bridge.stub_approvals(command_answer(bridge, arguments,
        'reject_once' if level == 'ask-reject' else 'allow_once', cards))
    original_test = generator_test.run_test
    original_execute = generator_test._host_command if route['locus'] == 'host' else generator_test._container_command

    async def observed_test(context, **kwargs):
        assert kwargs == {'commands': commands, **arguments}
        assert context.locus == route['locus']
        result = await original_test(context, **kwargs)
        child_results.append(deepcopy(result))
        return result

    async def observed_execute(*args):
        child = args[-1]
        workspace = gen.workspace_path() if route['locus'] == 'host' else '/workspace'
        expected = workspace + '/draft'
        assert child.cwd == expected
        record = {'locus': route['locus'], 'cwd': child.cwd, 'returned': False}
        executions.append(record)
        result = await original_execute(*args)
        record.update(returned=True, exit_code=result[0], output=result[1])
        return result

    monkeypatch.setattr(generator_test, 'run_test', observed_test)
    monkeypatch.setattr(generator_test,
        '_host_command' if route['locus'] == 'host' else '_container_command', observed_execute)
    returned, reply, failure = False, None, None
    offered = []
    native_proof, native_value = None, None
    native_validated = False
    offered_validated, sessions_closed = False, False
    full_wire_written, raw_acp_written, permissions_written = False, False, False

    async def turn():
        await gen.commit()
        return await asyncio.wait_for(eng.run_agent(
            prompt='Call the collimate MCP test_generator tool exactly once with these exact arguments: '
                + json.dumps(arguments) + '. Do not read or change the draft, run a shell, or create output yourself. '
                'After the tool returns, briefly report its result and stop. If refused, do not retry.',
            backend=backend, auth='api_key', provider=route['provider'], model=route['model'],
            system_prompt='', enable_file_tools=file_tools, websearch=False, commands=commands,
            runtime='standard' if host else 'docker', host_harness=host), timeout=420)

    try:
        result = run_on_test_loop(serving_cards(turn))
        returned, reply = True, result.text
        assert not capture.secret_observed, 'selected credential appeared in ACP; scrubbed evidence retained'
        assert len(profiles) == len(capture.prompt_ids) == len(child_results) == 1
        assert len(cards) == (0 if commands == 'allow' else 1)
        if level == 'ask-reject':
            assert executions == [] and not gen.has_file('native-child.txt')
            assert bridge.read_cell_files(paths=['native-child.txt']) == {}
            assert child_results[0].get('error') == 'approval_refused'
        else:
            assert len(executions) == 1 and executions[0]['returned'] is True
            assert type(executions[0]['exit_code']) is int and executions[0]['exit_code'] == 0
            assert gen.read_file('native-child.txt') == marker
            assert bridge.read_cell_files(paths=['native-child.txt']) == {'native-child.txt': marker}
        native_proof, native_value = native_generator_gate(
            capture, backend, arguments, child_results[0], marker, level)
        native_validated = True
    except BaseException as error:
        failure = type(error).__name__
        raise
    finally:
        try:
            offered = run_on_test_loop(offered_inventory(servers))
            offered_validated = True
        finally:
            try:
                # Finish the actual adapter before the evidence is written.
                # Existing fixture still owns final run-home cleanup.
                run_on_test_loop(acp_run.close_acp_sessions())
                sessions_closed = True
            finally:
                try:
                    try:
                        write_evidence(tmp_path / (backend + '-full-acp-wire.json'), capture.wire_records, selected_key)
                        full_wire_written = True
                    finally:
                        write_evidence(tmp_path / (backend + '-raw-acp.json'), capture.records, selected_key)
                        raw_acp_written = True
                finally:
                    try:
                        write_evidence(tmp_path / (backend + '-raw-permissions.json'), capture.permission_records, selected_key)
                        permissions_written = True
                    finally:
                        gate_passed = (native_validated and failure is None
                            and offered_validated and sessions_closed and full_wire_written
                            and raw_acp_written and permissions_written)
                        write_evidence(tmp_path / (backend + '-acquisition.json'), {
                            'authority': ('native_full_input_result_normal_end'
                                if gate_passed
                                else 'incomplete_or_acquisition_held'),
                            'native_gate_passed': gate_passed,
                            'native_proof': native_proof, 'native_value': native_value,
                            'enable_file_tools': file_tools,
                            'backend': backend, 'route': {k: v for k, v in route.items() if k != 'key_env'},
                            'arguments': arguments, 'profiles': profiles, 'cards': cards,
                            'actual_bridge_offered_tools': offered,
                            'sdk_child_results': child_results, 'executions': executions,
                            'engine_returned': returned, 'normalized_reply_diagnostic': reply,
                            'exception_type': failure, 'selected_secret_observed': capture.secret_observed,
                        }, selected_key)
