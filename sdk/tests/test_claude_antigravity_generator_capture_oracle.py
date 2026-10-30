# SPDX-License-Identifier: MIT
"""Raw acquisition/gate contracts; native decoder witnesses have separate controls."""
import ast
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests_live import test_live_claude_antigravity_generator_capture as subject


def test_explicit_account_routes_do_not_alias_openrouter_or_the_retired_antigravity_shim():
    claude = subject.route_for('claude-code')
    assert (claude['provider'], claude['model'], claude['locus']) == (
        'anthropic', 'claude-haiku-4-5-20251001', 'host')
    claude['pins']['claude-agent-acp'] = 'not-shared'
    assert subject.route_for('claude-code')['pins']['claude-agent-acp'] == '0.95.0'
    for unsupported in ('openrouter', 'antigravity'):
        with pytest.raises(ValueError):
            subject.route_for(unsupported)


def test_acquisition_writer_keeps_full_channels_private_and_flags_scrub(tmp_path):
    value = {'raw_acp': [{'full': 'text' * 500}], 'raw_permissions': [{'id': 'own'}],
             'exception_type': 'TimeoutError', 'normalized_reply_diagnostic': None}
    path = tmp_path / 'capture.json'
    subject.write_evidence(path, value, 'synthetic-key')
    assert json.loads(path.read_text()) == value and path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        subject.write_evidence(path, value, 'synthetic-key')
    escaped = tmp_path / 'scrubbed.json'
    with pytest.raises(AssertionError, match='credential observed'):
        subject.write_evidence(escaped, {'raw_acp': ['synthetic-key']}, 'synthetic-key')
    assert 'synthetic-key' not in escaped.read_text()


@pytest.mark.parametrize('backend,locus', [('claude-code', 'host')])
def test_profile_observation_uses_actual_dispatch_preserving_process_owner(monkeypatch, backend, locus):
    from dataclasses import replace
    from _agent_shared.acp_client import SpawnProfile, AdapterProcess
    from tests_live.fixtures.acp_capture import Capture
    from _agent_shared.harnesses import HARNESS
    row = HARNESS[backend]
    owner = 'harnesses.claude_code.backend:prepare'
    monkeypatch.setitem(HARNESS, backend, replace(row, prepare=owner))
    monkeypatch.setattr(subject.gen, 'workspace_path', lambda: '/actual-host-workspace')
    observed, calls = [], []
    transport = SimpleNamespace()
    killed = []
    process = AdapterProcess(transport, lambda: killed.append('own-kill'))
    async def spawn():
        return process
    profile = SpawnProfile(locus=locus, cwd='/actual-host-workspace' if locus == 'host' else '/workspace', spawn=spawn, commands='allow')
    async def prepare(context):
        calls.append(context)
        return profile
    monkeypatch.setitem(subject.acp_run._PROFILES, backend, prepare)
    subject.install_profile_capture(monkeypatch, backend, Capture(), observed)
    wrapped = asyncio.run(subject.acp_run._PROFILES[backend]({'exact': 'context'}))
    assert calls == [{'exact': 'context'}]
    assert wrapped.locus == profile.locus and wrapped.cwd == profile.cwd
    assert observed == [{'locus': locus, 'cwd': profile.cwd, 'owner': owner}]

    actual = asyncio.run(wrapped.spawn())
    assert actual.transport.original is transport and actual.kill is process.kill
    actual.kill()
    assert killed == ['own-kill']



def test_acquisition_keeps_full_prompt_and_unknown_acp_envelopes_without_decoder_acceptance():
    from tests_live.fixtures.acp_capture import CaptureError
    capture = subject.AcquisitionCapture(secrets=('synthetic-secret',))
    original = {'jsonrpc': '2.0', 'method': 'unmeasured/event', 'params': {'text': 'full original'}}
    capture.observe('receive', original)
    prompt = {'jsonrpc': '2.0', 'id': 'own', 'method': 'session/prompt',
              'params': {'sessionId': 'session', 'prompt': [{'type': 'text', 'text': 'full prompt'}]}}
    capture.observe('send', prompt)
    capture.observe('receive', {'method': 'unmeasured/event', 'params': {'text': 'synthetic-secret'}})
    assert capture.wire_records[:2] == [{'direction': 'receive', 'message': original},
                                       {'direction': 'send', 'message': prompt}]
    assert capture.secret_observed and 'synthetic-secret' not in json.dumps(capture.wire_records)
    assert original['params']['text'] == 'full original'
    with pytest.raises(CaptureError, match='normal completed response'):
        capture.completed_generator_value('claude-code', expected_input={})
    bounded = subject.AcquisitionCapture(max_records=1, max_bytes=1000)
    bounded.observe('receive', original)
    with pytest.raises(AssertionError, match='wire capture'):
        bounded.observe('receive', original)
    assert len(bounded.wire_records) == 1


def test_offered_inventory_uses_actual_registered_full_schema_without_invocation(monkeypatch):
    from mcp.server.mcpserver import MCPServer
    servers, calls = [], []
    subject.install_offered_inventory(monkeypatch, servers)
    server = MCPServer('collimate')
    def sample(path: str, options: dict) -> str:
        calls.append('must-not-run')
        return path
    server.add_tool(sample, name='sample', description='Exact offered declaration')
    values = asyncio.run(subject.offered_inventory(servers))
    assert len(values) == 1 and len(values[0]) == 1
    tool = values[0][0]
    assert tool['name'] == 'sample' and tool['description'] == 'Exact offered declaration'
    assert set(tool['inputSchema']['properties']) == {'path', 'options'}
    assert set(tool['inputSchema']['required']) == {'path', 'options'}
    assert calls == []



@pytest.mark.parametrize('stage', [None, 'inventory', 'close', 'full-acp-wire', 'raw-acp', 'raw-permissions'])
def test_actual_finalization_never_persists_acceptance_after_failed_stage_D23(tmp_path, stage):
    """Execute actual finally source with real private files; no native launch."""
    backend = 'claude-code'
    source = ast.parse(Path(subject.__file__).read_text())
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'test_actual_claude_antigravity_generator_native_D23')
    finalization = next(node for node in reversed(function.body) if isinstance(node, ast.Try))

    async def inventory(servers):
        if stage == 'inventory':
            raise RuntimeError('controlled inventory failure')
        return []

    async def close():
        if stage == 'close':
            raise RuntimeError('controlled close failure')

    def writer(path, value, selected_key):
        if path.name == backend + '-' + str(stage) + '.json':
            raise RuntimeError('controlled earlier private artifact failure')
        subject.write_evidence(path, value, selected_key)

    namespace = {**vars(subject), 'tmp_path': tmp_path, 'backend': backend,
        'selected_key': 'synthetic-secret', 'capture': subject.AcquisitionCapture(),
        'native_validated': True, 'offered_validated': False, 'sessions_closed': False,
        'full_wire_written': False, 'raw_acp_written': False, 'permissions_written': False,
        'failure': None, 'offered': [], 'offered_inventory': inventory, 'servers': [],
        'run_on_test_loop': asyncio.run, 'acp_run': SimpleNamespace(close_acp_sessions=close),
        'write_evidence': writer, 'native_proof': {'synthetic': 'previous stage'},
        'native_value': {}, 'file_tools': False, 'route': subject.route_for(backend),
        'arguments': {}, 'profiles': [], 'cards': [], 'child_results': [], 'executions': [],
        'returned': True, 'reply': None}
    tree = ast.fix_missing_locations(ast.Module(body=[ast.Try(body=[ast.Pass()], handlers=[],
        orelse=[], finalbody=finalization.finalbody)], type_ignores=[]))
    code = compile(tree, str(subject.__file__), 'exec')
    if stage is None:
        exec(code, namespace)
    else:
        with pytest.raises((RuntimeError, AssertionError)):
            exec(code, namespace)
    retained = json.loads((tmp_path / (backend + '-acquisition.json')).read_text())
    assert retained['native_gate_passed'] is (stage is None)
    assert retained['authority'] == ('native_full_input_result_normal_end' if stage is None
                                     else 'incomplete_or_acquisition_held')


def claude_measured_shape(refused=False):
    """Controlled2.1.281/0.81.1 shape; actual paid replay is separate evidence."""
    arguments = {'path': 'draft/probe.generator', 'settings': {}, 'prompt': 'full-input-' * 90}
    value = ({'error': 'approval_refused', 'message':
        'REFUSED: the user did not approve this command (or no one was there to approve it) '
        '— do not retry it unchanged; ask the user or take another approach.'} if refused else {
        'exit_code': 0, 'output': 'own-marker\n' + 'full-output-' * 90,
        'result': {'success': True, 'error_message': ''}})
    session, identity, name = 'own-session', 'toolu_' + 'a' * 24, 'mcp__collimate__test_generator'
    meta = {'claudeCode': {'toolName': name}}
    common = {'_meta': meta, 'toolCallId': identity, 'sessionUpdate': 'tool_call_update'}
    initial = {**common, 'sessionUpdate': 'tool_call', 'name': name, 'title': name,
               'kind': 'other', 'rawInput': {}, 'status': 'pending', 'content': []}
    updates = [initial]
    for keys in [('path',), ('path', 'settings'), ('path', 'settings', 'prompt')]:
        update = {**common, 'rawInput': {key: arguments[key] for key in keys},
                  'title': name, 'kind': 'other'}
        if len(keys) == 3:
            update['content'] = []
        updates.append(update)
    raw = json.dumps({'result': value}, ensure_ascii=False)
    updates.append({**common, '_meta': {'claudeCode': {'toolName': name, 'toolResponse': raw}}})
    updates.append({**common, 'status': 'completed', 'rawOutput': raw,
                    'content': [{'type': 'content', 'content': {'type': 'text', 'text': raw}}]})
    rows = [{'direction': 'send', 'id': 2, 'method': 'session/prompt', 'sessionId': session}]
    for update in updates:
        rows.append({'direction': 'receive', 'message': {'jsonrpc': '2.0', 'method': 'session/update',
                     'params': {'sessionId': session, 'update': update}}})
    counts = {'cachedInputTokens': 10, 'cachedWriteTokens': 20, 'inputTokens': 3,
              'outputTokens': 4, 'reasoningOutputTokens': 0, 'totalTokens': 37}
    rows.append({'direction': 'receive', 'message': {'jsonrpc': '2.0', 'id': 2, 'result': {
        'stopReason': 'end_turn', 'usage': {'cachedReadTokens': 10, 'cachedWriteTokens': 20,
            'inputTokens': 3, 'outputTokens': 4, 'totalTokens': 37},
        '_meta': {'quota': {'token_count': counts, 'model_usage': [
            {'model': 'claude-haiku-4-5-20251001', 'token_count': counts}]}}}}})
    tool = {**initial, 'rawInput': arguments, '_meta': {'claudeCode': {
        'toolName': name, 'mcpServer': {'name': 'collimate', 'source': 'dynamic'}}}}
    tool.pop('sessionUpdate')
    permissions = [
        {'direction': 'receive', 'after_record_count': 5, 'message': {'jsonrpc': '2.0', 'id': 0,
            'method': 'session/request_permission', 'params': {'sessionId': session, 'toolCall': tool,
                '_meta': {'permission': {'title': name, 'version': 1}}, 'options': [
                    {'kind': 'allow_once', 'name': 'Yes', 'optionId': 'allow-once'},
                    {'kind': 'allow_always', 'name': "Yes, and don't ask again for Test Generator commands",
                     'optionId': 'allow-with-updates'},
                    {'kind': 'reject_once', 'name': 'No', 'optionId': 'reject'}]}}},
        {'direction': 'send', 'after_record_count': 5, 'message': {'jsonrpc': '2.0', 'id': 0,
            'result': {'outcome': {'optionId': 'allow-once', 'outcome': 'selected'}}}},
    ]
    # Actual observer copies every envelope; avoid aliases hiding mutations.
    return tuple(deepcopy(item) for item in (arguments, value, rows, permissions))


@pytest.mark.parametrize('refused', [False, True])
def test_measured_claude_full_input_result_normal_end_and_native_permission(refused):
    from tests_live.fixtures.acp_capture import Capture, CaptureError
    arguments, value, rows, permissions = claude_measured_shape(refused)
    capture = Capture()
    capture.records, capture.permission_records = rows, permissions
    proof, actual = capture.completed_generator_value('claude-code', expected_input=arguments)
    assert actual == value and proof['input'] == arguments and proof['records'] == rows
    assert proof['native_permission'] == permissions
    if refused:
        with pytest.raises(CaptureError, match='successful execution'):
            capture.completed_generator_result('claude-code', expected_input=arguments, marker='own-marker')
    else:
        assert capture.completed_generator_result(
            'claude-code', expected_input=arguments, marker='own-marker')[1] == value


@pytest.mark.parametrize('level', ['allow', 'ask-allow', 'ask-reject'])
def test_actual_gate_pairs_whole_native_result_with_sdk_boundary_D23(level):
    from tests_live.fixtures.acp_capture import Capture, CaptureError
    arguments, value, rows, permissions = claude_measured_shape(level == 'ask-reject')
    capture = Capture()
    capture.records, capture.permission_records = rows, permissions
    proof, actual = subject.native_generator_gate(capture, 'claude-code', arguments, value, 'own-marker', level)
    assert actual == value and proof['input'] == arguments
    other = deepcopy(value)
    other['message' if level == 'ask-reject' else 'output'] += ' foreign-tail'
    with pytest.raises(AssertionError, match='complete result differs'):
        subject.native_generator_gate(capture, 'claude-code', arguments, other, 'own-marker', level)
    if level != 'ask-reject':
        with pytest.raises(CaptureError, match='successful execution'):
            subject.native_generator_gate(capture, 'claude-code', arguments, value, 'foreign-marker', level)


@pytest.mark.parametrize('damage', [
    'foreign-session', 'cross-call', 'wrong-name', 'wrong-meta', 'partial-input', 'typed-input',
    'changed-prefix', 'extra-call', 'missing-final', 'duplicate-final', 'truncated-output',
    'unequal-meta', 'unequal-text', 'extra-output-block', 'bad-wrapper', 'bool-exit',
    'missing-child-field', 'unknown-refusal', 'nonfinite-result', 'duplicate-json-key',
    'nonfinite-input', 'missing-end', 'cancelled-end', 'wrong-prompt', 'bool-prompt',
    'late-end', 'extra-end-field', 'bool-usage', 'wrong-quota', 'wrong-model',
    'no-permission', 'extra-permission', 'foreign-permission', 'wrong-permission-input',
    'wrong-server', 'wrong-permission-answer', 'wrong-permission-id', 'wrong-permission-position',
])
def test_claude_decoder_refuses_partial_cross_call_changed_or_nonfinite_evidence(damage):
    from tests_live.fixtures.acp_capture import Capture, CaptureError
    arguments, value, rows, permissions = claude_measured_shape()
    opened = rows[1]['message']['params']['update']
    full = rows[4]['message']['params']['update']
    metadata = rows[5]['message']['params']['update']
    final = rows[6]['message']['params']['update']
    end = rows[-1]['message']
    if damage == 'foreign-session': rows[2]['message']['params']['sessionId'] = 'foreign'
    elif damage == 'cross-call': final['toolCallId'] = 'toolu_' + 'b' * 24
    elif damage == 'wrong-name': opened['name'] = 'other'
    elif damage == 'wrong-meta': full['_meta']['claudeCode']['toolName'] = 'other'
    elif damage == 'partial-input': full['rawInput']['prompt'] = '...'
    elif damage == 'typed-input':
        arguments['settings']['typed'] = 1
        full['rawInput']['settings']['typed'] = True
    elif damage == 'changed-prefix': rows[2]['message']['params']['update']['rawInput']['path'] = 'other'
    elif damage == 'extra-call': rows.insert(2, deepcopy(rows[1]))
    elif damage == 'missing-final': rows.pop(6)
    elif damage == 'duplicate-final': rows.insert(7, deepcopy(rows[6]))
    elif damage == 'truncated-output': final['rawOutput'] = '{...}'
    elif damage == 'unequal-meta': metadata['_meta']['claudeCode']['toolResponse'] += ' '
    elif damage == 'unequal-text': final['content'][0]['content']['text'] += ' '
    elif damage == 'extra-output-block': final['content'].append(deepcopy(final['content'][0]))
    elif damage in ('bad-wrapper', 'bool-exit', 'missing-child-field', 'unknown-refusal',
                    'nonfinite-result', 'duplicate-json-key'):
        if damage == 'bad-wrapper': wrapped = value
        else:
            if damage == 'bool-exit': value['exit_code'] = False
            elif damage == 'missing-child-field': value['result'].pop('error_message')
            elif damage == 'unknown-refusal': value = {'error': 'approval_refused', 'message': 'other'}
            elif damage == 'nonfinite-result': value['exit_code'] = float('nan')
            wrapped = {'result': value}
        raw = json.dumps(wrapped)
        if damage == 'duplicate-json-key': raw = '{"result":{},"result":' + json.dumps(value) + '}'
        final['rawOutput'] = final['content'][0]['content']['text'] = raw
        metadata['_meta']['claudeCode']['toolResponse'] = raw
    elif damage == 'nonfinite-input':
        arguments['settings']['nonfinite'] = float('inf')
        for row in rows[3:5]:
            row['message']['params']['update']['rawInput']['settings']['nonfinite'] = float('inf')
        permissions[0]['message']['params']['toolCall']['rawInput']['settings']['nonfinite'] = float('inf')
    elif damage == 'missing-end': rows.pop()
    elif damage == 'cancelled-end': end['result']['stopReason'] = 'cancelled'
    elif damage == 'wrong-prompt': end['id'] = 3
    elif damage == 'bool-prompt': rows[0]['id'] = end['id'] = True
    elif damage == 'late-end': rows[6], rows[7] = rows[7], rows[6]
    elif damage == 'extra-end-field': end['result']['ignored'] = True
    elif damage == 'bool-usage': end['result']['usage']['inputTokens'] = True
    elif damage == 'wrong-quota': end['result']['_meta']['quota']['token_count']['inputTokens'] = 4
    elif damage == 'wrong-model': end['result']['_meta']['quota']['model_usage'][0]['model'] = 'other'
    elif damage == 'no-permission': permissions = []
    elif damage == 'extra-permission': permissions.append(deepcopy(permissions[1]))
    elif damage == 'foreign-permission': permissions[0]['message']['params']['sessionId'] = 'foreign'
    elif damage == 'wrong-permission-input': permissions[0]['message']['params']['toolCall']['rawInput']['prompt'] = '...'
    elif damage == 'wrong-server': permissions[0]['message']['params']['toolCall']['_meta']['claudeCode']['mcpServer']['name'] = 'other'
    elif damage == 'wrong-permission-answer': permissions[1]['message']['result']['outcome']['optionId'] = 'reject'
    elif damage == 'wrong-permission-id': permissions[1]['message']['id'] = True
    elif damage == 'wrong-permission-position': permissions[0]['after_record_count'] = 4
    capture = Capture()
    capture.records, capture.permission_records = rows, permissions
    with pytest.raises(CaptureError):
        capture.completed_generator_value('claude-code', expected_input=arguments)


def claude_prefix_variation(prefixes):
    arguments, value, rows, permissions = claude_measured_shape()
    template = rows[2]
    streamed = []
    for keys in prefixes:
        record = deepcopy(template)
        record['message']['params']['update']['rawInput'] = {
            key: arguments[key] for key in keys}
        streamed.append(record)
    rows[2:4] = streamed
    for permission in permissions:
        permission['after_record_count'] = len(rows) - 3
    return arguments, value, rows, permissions


@pytest.mark.parametrize('prefixes', [(), (('path',),), (('settings',),), (('prompt',),),
    (('prompt',), ('prompt', 'path')), (('settings',), ('settings', 'path')),
    (('path',), ('path', 'settings'))])
def test_claude_pinned_adapter_optional_prefixes_preserve_one_exact_full_input(prefixes):
    from tests_live.fixtures.acp_capture import Capture
    arguments, value, rows, permissions = claude_prefix_variation(prefixes)
    capture = Capture()
    capture.records, capture.permission_records = rows, permissions
    proof, actual = capture.completed_generator_result(
        'claude-code', expected_input=arguments, marker='own-marker')
    assert actual == value and proof['records'] == rows
    assert proof['input'] == arguments and proof['native_permission'] == permissions


@pytest.mark.parametrize('damage', ['duplicate-prefix', 'empty-prefix', 'regressed-prefix',
    'unknown-prefix-key', 'full-as-prefix', 'prefix-value', 'full-content-missing',
    'stale-permission-offset', 'prefix-after-full'])
def test_claude_optional_prefixes_cannot_hide_changed_input_or_permission_boundary(damage):
    from tests_live.fixtures.acp_capture import Capture, CaptureError
    arguments, _, rows, permissions = claude_prefix_variation((('path',), ('path', 'settings')))
    first = rows[2]['message']['params']['update']
    second = rows[3]['message']['params']['update']
    if damage == 'duplicate-prefix': second['rawInput'] = deepcopy(first['rawInput'])
    elif damage == 'empty-prefix': first['rawInput'] = {}
    elif damage == 'regressed-prefix':
        first['rawInput'] = {'path': arguments['path'], 'settings': arguments['settings']}
        second['rawInput'] = {'path': arguments['path']}
    elif damage == 'unknown-prefix-key': first['rawInput']['ignored'] = True
    elif damage == 'full-as-prefix': second['rawInput'] = deepcopy(arguments)
    elif damage == 'prefix-value': first['rawInput']['path'] = 'foreign'
    elif damage == 'full-content-missing': rows[4]['message']['params']['update'].pop('content')
    elif damage == 'stale-permission-offset':
        rows.pop(2)
        # Deliberately retain the original offset instead of following full input.
    elif damage == 'prefix-after-full': rows[3], rows[4] = rows[4], rows[3]
    capture = Capture()
    capture.records, capture.permission_records = rows, permissions
    with pytest.raises(CaptureError):
        capture.completed_generator_value('claude-code', expected_input=arguments)


@pytest.mark.parametrize('damage', [None, 'wrong-op', 'wrong-reason', 'extra-key',
    'false-success', 'nonzero-exit', 'error-message', 'mirror-difference'])
def test_claude_measured_app_refused_result_is_exact_and_full_mirrored(damage):
    from tests_live.fixtures.acp_capture import Capture, CaptureError
    arguments, value, rows, permissions = claude_measured_shape()
    value['result']['refused'] = {'op': 'surface_register', 'reason': 'forward_refused'}
    if damage == 'wrong-op': value['result']['refused']['op'] = 'account'
    elif damage == 'wrong-reason': value['result']['refused']['reason'] = 'other'
    elif damage == 'extra-key': value['result']['refused']['extra'] = True
    elif damage == 'false-success': value['result']['success'] = False
    elif damage == 'nonzero-exit': value['exit_code'] = 1
    elif damage == 'error-message': value['result']['error_message'] = 'error'
    raw = json.dumps({'result': value}, ensure_ascii=False)
    rows[-3]['message']['params']['update']['_meta']['claudeCode']['toolResponse'] = raw
    final = rows[-2]['message']['params']['update']
    final['rawOutput'] = raw
    final['content'][0]['content']['text'] = raw + (' ' if damage == 'mirror-difference' else '')
    capture = Capture()
    capture.records, capture.permission_records = rows, permissions
    if damage:
        with pytest.raises(CaptureError):
            capture.completed_generator_value('claude-code', expected_input=arguments)
    else:
        proof, actual = capture.completed_generator_value('claude-code', expected_input=arguments)
        assert actual == value and proof['records'] == rows


@pytest.mark.parametrize('damage', [None, 'bool', 'negative', 'fraction', 'missing',
    'extra', 'wrong-total', 'reasoning', 'extra-model'])
def test_claude_model_quota_uses_its_own_native_scope(damage):
    """Actual APP reply has wider model accounting than main-loop usage."""
    from tests_live.fixtures.acp_capture import Capture, CaptureError
    arguments, value, rows, permissions = claude_measured_shape()
    result = rows[-1]['message']['result']
    result['usage'] = {'inputTokens': 18, 'outputTokens': 597, 'cachedReadTokens': 46356,
                       'cachedWriteTokens': 1098, 'totalTokens': 48069}
    quota = result['_meta']['quota']
    quota['token_count'] = {'inputTokens': 18, 'outputTokens': 597, 'cachedInputTokens': 46356,
                           'cachedWriteTokens': 1098, 'reasoningOutputTokens': 0, 'totalTokens': 48069}
    wider = {**quota['token_count'], 'inputTokens': 1218, 'outputTokens': 609, 'totalTokens': 49281}
    quota['model_usage'][0]['token_count'] = wider
    if damage == 'bool': wider['inputTokens'] = True
    elif damage == 'negative': wider['cachedInputTokens'] = -1
    elif damage == 'fraction': wider['outputTokens'] = 609.5
    elif damage == 'missing': wider.pop('cachedWriteTokens')
    elif damage == 'extra': wider['extra'] = 0
    elif damage == 'wrong-total': wider['totalTokens'] += 1
    elif damage == 'reasoning': wider['reasoningOutputTokens'] = 1
    elif damage == 'extra-model': quota['model_usage'].append(deepcopy(quota['model_usage'][0]))
    capture = Capture()
    capture.records, capture.permission_records = rows, permissions
    if damage:
        with pytest.raises(CaptureError):
            capture.completed_generator_value('claude-code', expected_input=arguments)
    else:
        proof, actual = capture.completed_generator_value('claude-code', expected_input=arguments)
        assert actual == value and proof['prompt_response']['result'] == result
        assert result['usage']['totalTokens'] != wider['totalTokens']
