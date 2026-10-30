"""Observer controls are simulated dictionaries, never claimed native proof."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import stat

import pytest

from tests_live.fixtures.acp_capture import (Capture, CaptureError, ObservedTransport,
                                           _cline_generator_title, observed_profile)


def permission_request(identity=11):
    """Source-shaped native RPC; synthetic full arguments, never native proof."""
    return {'jsonrpc': '2.0', 'id': identity, 'method': 'session/request_permission',
            'params': {'sessionId': 'native', 'toolCall': {'toolCallId': 'native-call',
                'status': 'pending', 'title': 'test_generator (collimate MCP Server)',
                'rawInput': {'path': 'draft/probe.generator', 'settings': {},
                             'prompt': 'complete-private-native-input-' * 80},
                '_meta': {'toolName': 'mcp__collimate__test_generator'}},
                'options': [{'optionId': 'allow', 'kind': 'allow_once', 'name': 'Allow'},
                            {'optionId': 'reject', 'kind': 'reject_once', 'name': 'Reject'}]}}


def permission_reply(identity=11, outcome='allow'):
    return {'jsonrpc': '2.0', 'id': identity, 'result': {'outcome': {
        'outcome': 'selected', 'optionId': outcome}}}


def test_private_permission_channel_preserves_full_rpc_order_and_legacy_decoder_D23():
    legacy = captured(raw_records())
    capture = Capture()
    rows = raw_records()
    for direction, message in rows[:2]:
        capture.observe(direction, message)
    request, reply = permission_request(), permission_reply()
    originals = deepcopy((request, reply))
    capture.observe('receive', request)
    capture.observe('send', reply)
    for direction, message in rows[2:]:
        capture.observe(direction, message)
    assert capture.records == legacy.records
    assert capture.completed_turn() == legacy.completed_turn()
    assert capture.permission_records == [
        {'direction': 'receive', 'message': request, 'after_record_count': 2},
        {'direction': 'send', 'message': reply, 'after_record_count': 2}]
    request['params']['toolCall']['rawInput']['prompt'] = 'mutated downstream'
    reply['result']['outcome']['optionId'] = 'mutated downstream'
    assert [row['message'] for row in capture.permission_records] == list(originals)
    assert capture.bytes == sum(len(json.dumps(row, ensure_ascii=False,
        separators=(',', ':')).encode()) for row in capture.records + capture.permission_records)


@pytest.mark.parametrize('outcome', ['allow', 'reject', 'cancel', 'error'])
def test_private_permission_channel_retains_reply_without_denial_inference_D23(outcome):
    capture = Capture()
    request = permission_request()
    reply = permission_reply(outcome=outcome)
    if outcome == 'cancel':
        reply['result'] = {'outcome': {'outcome': 'cancelled'}}
    elif outcome == 'error':
        reply.pop('result')
        reply['error'] = {'code': -32603, 'message': 'native permission error'}
    capture.observe('receive', request)
    capture.observe('send', reply)
    assert capture.permission_records == [
        {'direction': 'receive', 'message': request, 'after_record_count': 0},
        {'direction': 'send', 'message': reply, 'after_record_count': 0}]
    assert capture.records == []


def test_permission_rpc_ids_are_typed_and_direction_sensitive_D23():
    capture = Capture()
    capture.observe(*raw_records()[0])  # Local prompt id7 can equal peer request id7.
    capture.observe('receive', permission_request(7))
    capture.observe('receive', permission_request('7'))
    for identity in (True, 8, 'foreign'):
        capture.observe('send', permission_reply(identity))
    capture.observe('send', permission_reply('7'))
    capture.observe('send', permission_reply(7))
    capture.observe('receive', {'jsonrpc': '2.0', 'id': 7, 'result': {'stopReason': 'end_turn'}})
    assert [row['message']['id'] for row in capture.permission_records] == [7, '7', '7', 7]
    assert [row['direction'] for row in capture.permission_records] == ['receive', 'receive', 'send', 'send']
    assert len(capture.records) == 2 and capture.records[-1]['message']['id'] == 7


@pytest.mark.parametrize('identity', [None, True, False, 1.0, [], {}])
def test_permission_request_requires_exact_rpc_identity_D23(identity):
    capture = Capture()
    with pytest.raises(CaptureError, match='ID'):
        capture.observe('receive', permission_request(identity))
    assert capture.permission_records == [] and capture.bytes == 0


def test_permission_duplicate_requests_refuse_but_duplicate_replies_remain_evidence_D23():
    capture = Capture()
    capture.observe('receive', permission_request())
    before = deepcopy(capture.permission_records), capture.bytes
    with pytest.raises(CaptureError, match='duplicate'):
        capture.observe('receive', permission_request())
    assert (capture.permission_records, capture.bytes) == before
    capture.observe('send', permission_reply())
    capture.observe('send', permission_reply())
    assert len(capture.permission_records) == 3  # Never hide a duplicate reply.


@pytest.mark.parametrize('kind', ['legacy_after_permission', 'permission_after_legacy', 'reply'])
def test_permission_channel_shares_record_bound_and_fails_atomically_D23(kind):
    capture = Capture(max_records=1)
    if kind == 'legacy_after_permission':
        capture.observe('receive', permission_request())
        next_row = raw_records()[0]
    else:
        if kind == 'reply':
            capture.observe('receive', permission_request())
            next_row = ('send', permission_reply())
        else:
            capture.observe(*raw_records()[0])
            next_row = ('receive', permission_request())
    before = deepcopy(capture.records), deepcopy(capture.permission_records), capture.bytes
    with pytest.raises(CaptureError, match='bound'):
        capture.observe(*next_row)
    assert (capture.records, capture.permission_records, capture.bytes) == before


def test_permission_channel_shares_byte_bound_and_scrubs_full_payload_D23():
    secret = 'synthetic-selected-credential'
    request = permission_request()
    request['params']['toolCall']['rawInput'][secret] = secret * 100
    reply = permission_reply()
    reply['result'][secret] = [secret]
    capture = Capture(secrets=(secret,))
    capture.observe(*raw_records()[0])
    capture.observe('receive', request)
    capture.observe('send', reply)
    assert secret not in json.dumps(capture.permission_records)
    assert secret in json.dumps(request) and secret in json.dumps(reply)
    bounded = Capture(secrets=(secret,), max_bytes=capture.bytes - 1)
    bounded.observe(*raw_records()[0])
    bounded.observe('receive', request)
    before = deepcopy(bounded.permission_records), bounded.bytes
    with pytest.raises(CaptureError, match='bound'):
        bounded.observe('send', reply)
    assert (bounded.permission_records, bounded.bytes) == before


def test_permission_observed_transport_forwards_unchanged_and_records_before_mutation_D23():
    async def scenario():
        request, reply, calls = permission_request(), permission_reply(), []
        class Original:
            async def receive(self):
                return request
            async def send(self, message):
                calls.append(message)
                return 'sent'
            async def close(self):
                calls.append('close')
        capture = Capture()
        transport = ObservedTransport(Original(), capture)
        assert await transport.receive() is request
        assert await transport.send(reply) == 'sent' and calls == [reply]
        request['params']['toolCall']['rawInput']['prompt'] = 'later mutation'
        assert capture.permission_records[0]['message']['params']['toolCall']['rawInput']['prompt'] != 'later mutation'
        await transport.close()
        await transport.close()
        assert calls == [reply, 'close']
    asyncio.run(scenario())


def test_permission_order_indices_preserve_unpaired_and_cross_boundary_requests_D23():
    capture = Capture()
    capture.observe('receive', permission_request('first'))
    capture.observe(*raw_records()[0])
    capture.observe('send', permission_reply('first'))
    capture.observe(*raw_records()[1])
    capture.observe('receive', permission_request('second'))
    capture.observe(*raw_records()[2])
    capture.observe('send', permission_reply('second'))
    capture.observe('receive', permission_request('unpaired'))
    capture.observe(*raw_records()[3])
    assert [row['after_record_count'] for row in capture.permission_records] == [0, 1, 2, 3, 3]
    assert [row['message']['id'] for row in capture.permission_records] == [
        'first', 'first', 'second', 'second', 'unpaired']
    assert len(capture.records) == 4
    # No completion inference or hidden removal of an unpaired native request.
    assert capture.permission_records[-1]['direction'] == 'receive'


def test_permission_channel_never_fabricates_missing_qwen_progress_arguments_D23():
    rows, value, arguments = qwen_generator_records(ancillary=False)
    rows.pop(2)  # Native ASK suppresses the full-input in_progress update.
    capture = Capture()
    for direction, message in rows[:2]:
        capture.observe(direction, message)
    request = permission_request()
    request['params']['toolCall']['rawInput'] = deepcopy(arguments)
    capture.observe('receive', request)
    capture.observe('send', permission_reply())
    for direction, message in rows[2:]:
        capture.observe(direction, message)
    unchanged = deepcopy(capture.records)
    with pytest.raises(CaptureError):
        capture.completed_generator_value('qwen-code', expected_input=arguments)
    assert capture.records == unchanged and len(capture.records) == 4
    assert capture.permission_records[0]['message']['params']['toolCall']['rawInput'] == arguments


def test_permission_observer_ignores_nonselected_methods_and_wrong_direction_D23():
    capture = Capture()
    capture.observe('send', permission_request())
    capture.observe('receive', {'id': 11, 'result': {}})
    capture.observe('receive', {'id': 11, 'method': 'fs/read_text_file', 'params': {}})
    assert capture.permission_records == [] and capture.records == []
    capture.observe('receive', permission_request())
    capture.observe('send', {'id': 11, 'method': 'session/cancel', 'params': {}})
    assert len(capture.permission_records) == 1


def qwen_granted_permission_records(*, refused=True):
    """Actual four-legacy/two-permission layout; synthetic IDs/args/value."""
    rows, value, arguments = qwen_generator_records(ancillary=False)
    rows.pop(2)
    if refused:
        value = {'error': 'approval_refused', 'message': 'synthetic inner app refusal'}
        final = rows[2][1]['params']['update']
        final['rawOutput'] = json.dumps(value)
        final['content'][0]['content']['text'] = final['rawOutput']
    request = permission_request(0)
    request['params']['toolCall'] = {
        'toolCallId': rows[1][1]['params']['update']['toolCallId'], 'status': 'pending',
        'title': json.dumps(arguments, ensure_ascii=False, separators=(',', ':')),
        'content': [], 'locations': [], 'kind': 'other', 'rawInput': deepcopy(arguments),
        '_meta': {'toolName': 'mcp__collimate__test_generator'}}
    request['params']['options'] = [
        {'optionId': 'proceed_always_project', 'name': 'Always Allow in project: test_generator', 'kind': 'allow_always'},
        {'optionId': 'proceed_always_user', 'name': 'Always Allow for user: test_generator', 'kind': 'allow_always'},
        {'optionId': 'proceed_once', 'name': 'Allow', 'kind': 'allow_once'},
        {'optionId': 'cancel', 'name': 'Reject', 'kind': 'reject_once'}]
    for _, message in rows[1:]:
        message['jsonrpc'] = '2.0'
    permissions = [('receive', request), ('send', permission_reply(0, outcome='proceed_once'))]
    return [*rows[:2], *permissions, *rows[2:]], value, arguments


@pytest.mark.parametrize('refused', [True, False])
def test_qwen_granted_permission_full_native_arguments_without_progress_D23(refused):
    rows, value, arguments = qwen_granted_permission_records(refused=refused)
    capture = captured(rows)
    original = deepcopy((capture.records, capture.permission_records))
    proof, decoded = capture.completed_generator_value('qwen-code', expected_input=arguments)
    assert decoded == value and proof['input'] == arguments
    assert proof['input_source'] == 'native_permission_request'
    assert proof['records'] == original[0] and proof['permission_records'] == original[1]
    assert proof['ancillary_calls'] == [] and len(proof['final_updates']) == 1
    assert len(proof['records']) == 4 and len(proof['permission_records']) == 2
    assert (capture.records, capture.permission_records) == original
    with pytest.raises(CaptureError):
        capture.completed_turn(expected_input=arguments)  # Generic contract unchanged.
    if refused:
        with pytest.raises(CaptureError):
            capture.completed_generator_result('qwen-code', expected_input=arguments, marker='fresh-child-marker')
    else:
        assert capture.completed_generator_result('qwen-code', expected_input=arguments,
            marker='fresh-child-marker')[1] == value


@pytest.mark.parametrize('damage', [
    'no_permission', 'extra_permission', 'reverse_permission', 'request_index', 'reply_index',
    'index_type', 'request_direction', 'reply_direction', 'request_id_bool', 'reply_id_type',
    'reply_id', 'rpc_version', 'request_method', 'request_extra', 'params_extra', 'session',
    'tool_id', 'tool_name', 'tool_extra', 'tool_status', 'tool_kind', 'tool_locations',
    'full_input', 'missing_input', 'extra_input', 'description', 'description_prefix',
    'options_order', 'options_missing', 'option_kind', 'option_name', 'option_extra',
    'reply_error', 'reply_cancel', 'reply_reject', 'reply_always', 'reply_extra',
    'initial_name', 'initial_id', 'initial_input', 'initial_meta', 'update_session',
    'final_id', 'final_status', 'final_input', 'final_content', 'final_partial', 'final_nonobject',
    'prompt_bool', 'prompt_session', 'end_id', 'end_error', 'end_reason', 'end_direction',
    'extra_tool',
])
def test_qwen_granted_permission_requires_all_native_identity_input_reply_result_gates_D23(damage):
    rows, _, arguments = qwen_granted_permission_records()
    capture = captured(rows)
    request, reply = capture.permission_records
    rpc, answer = request['message'], reply['message']
    tool = rpc['params']['toolCall']
    initial = capture.records[1]['message']['params']['update']
    final = capture.records[2]['message']['params']['update']
    end = capture.records[-1]['message']
    if damage == 'no_permission': capture.permission_records = []
    elif damage == 'extra_permission': capture.permission_records.append(deepcopy(reply))
    elif damage == 'reverse_permission': capture.permission_records.reverse()
    elif damage == 'request_index': request['after_record_count'] = 1
    elif damage == 'reply_index': reply['after_record_count'] = 3
    elif damage == 'index_type': request['after_record_count'] = 2.0
    elif damage == 'request_direction': request['direction'] = 'send'
    elif damage == 'reply_direction': reply['direction'] = 'receive'
    elif damage == 'request_id_bool': rpc['id'] = answer['id'] = False
    elif damage == 'reply_id_type': answer['id'] = '0'
    elif damage == 'reply_id': answer['id'] = 1
    elif damage == 'rpc_version': rpc['jsonrpc'] = '1.0'
    elif damage == 'request_method': rpc['method'] = 'other'
    elif damage == 'request_extra': rpc['extra'] = True
    elif damage == 'params_extra': rpc['params']['extra'] = True
    elif damage == 'session': rpc['params']['sessionId'] = 'foreign'
    elif damage == 'tool_id': tool['toolCallId'] = 'foreign'
    elif damage == 'tool_name': tool['_meta']['toolName'] = 'shell'
    elif damage == 'tool_extra': tool['extra'] = True
    elif damage == 'tool_status': tool['status'] = 'in_progress'
    elif damage == 'tool_kind': tool['kind'] = 'execute'
    elif damage == 'tool_locations': tool['locations'] = [{'path': '/foreign'}]
    elif damage == 'full_input': tool['rawInput']['prompt'] = 'foreign'
    elif damage == 'missing_input': tool.pop('rawInput')
    elif damage == 'extra_input': tool['rawInput']['extra'] = 'hidden'
    elif damage == 'description': tool['title'] = '{}'
    elif damage == 'description_prefix': tool['title'] = 'test_generator: ' + tool['title']
    elif damage == 'options_order': rpc['params']['options'].reverse()
    elif damage == 'options_missing': rpc['params']['options'].pop()
    elif damage == 'option_kind': rpc['params']['options'][2]['kind'] = 'reject_once'
    elif damage == 'option_name': rpc['params']['options'][2]['name'] = 'Other'
    elif damage == 'option_extra': rpc['params']['options'][2]['extra'] = True
    elif damage == 'reply_error': answer['error'] = {'message': 'error'}
    elif damage == 'reply_cancel': answer['result']['outcome'] = {'outcome': 'cancelled'}
    elif damage == 'reply_reject': answer['result']['outcome']['optionId'] = 'cancel'
    elif damage == 'reply_always': answer['result']['outcome']['optionId'] = 'proceed_always_project'
    elif damage == 'reply_extra': answer['result']['outcome']['extra'] = True
    elif damage == 'initial_name': initial['title'] = 'shell'
    elif damage == 'initial_id': initial['toolCallId'] = 'foreign'
    elif damage == 'initial_input': initial['rawInput'] = deepcopy(arguments)
    elif damage == 'initial_meta': initial['_meta']['serverId'] = 'foreign'
    elif damage == 'update_session': capture.records[2]['message']['params']['sessionId'] = 'foreign'
    elif damage == 'final_id': final['toolCallId'] = 'foreign'
    elif damage == 'final_status': final['status'] = 'failed'
    elif damage == 'final_input': final['rawInput'] = deepcopy(arguments)
    elif damage == 'final_content': final['content'][0]['content']['text'] = '{}'
    elif damage == 'final_partial': final['rawOutput'] = final['content'][0]['content']['text'] = '{'
    elif damage == 'final_nonobject': final['rawOutput'] = final['content'][0]['content']['text'] = '[]'
    elif damage == 'prompt_bool': capture.records[0]['id'] = True
    elif damage == 'prompt_session': capture.records[0]['sessionId'] = ''
    elif damage == 'end_id': end['id'] = '7'
    elif damage == 'end_error': end['error'] = {'message': 'error'}
    elif damage == 'end_reason': end['result']['stopReason'] = 'cancelled'
    elif damage == 'end_direction': capture.records[-1]['direction'] = 'send'
    else: capture.records.insert(2, deepcopy(capture.records[1]))
    with pytest.raises((CaptureError, ValueError)):
        capture.completed_generator_value('qwen-code', expected_input=arguments)


@pytest.mark.parametrize('ancillary', [False, True])
def test_qwen_progress_shape_never_ignores_unexplained_permission_traffic_D23(ancillary):
    rows, _, arguments = qwen_generator_records(ancillary=ancillary)
    capture = captured(rows)
    capture.permission_records = [{'direction': 'receive', 'message': permission_request(), 'after_record_count': 2}]
    with pytest.raises(CaptureError, match='unexplained permission'):
        capture.completed_generator_value('qwen-code', expected_input=arguments)


def test_qwen_permission_description_uses_captured_argument_property_order_D23():
    rows, value, arguments = qwen_granted_permission_records()
    request = rows[2][1]['params']['toolCall']
    request['rawInput'] = dict(reversed(list(request['rawInput'].items())))
    request['title'] = json.dumps(request['rawInput'], separators=(',', ':'), ensure_ascii=False)
    assert captured(rows).completed_generator_value('qwen-code', expected_input=arguments)[1] == value


def test_qwen_permission_retains_source_completion_metadata_and_typed_rpc_ids_D23():
    rows, value, arguments = qwen_granted_permission_records()
    # Pinned Session.prompt adds branch point metadata when available, without
    # changing stopReason. This opaque metadata is retained in the full proof.
    metadata = {'qwen.branchPoint': {'assistantRecordUuid': 'synthetic-assistant-uuid',
                                    'checkpointUuid': 'synthetic-checkpoint-uuid'}}
    rows[-1][1]['result']['_meta'] = metadata
    for identity in (7, '7'):
        rows[2][1]['id'] = rows[3][1]['id'] = identity
        proof, decoded = captured(rows).completed_generator_value('qwen-code', expected_input=arguments)
        assert decoded == value and proof['prompt_response']['result']['_meta'] == metadata
        assert proof['permission_records'][0]['message']['id'] == identity


@pytest.mark.parametrize('damage', ['metadata', 'extra_result'])
def test_qwen_permission_normal_completion_rejects_wrong_metadata_container_D23(damage):
    rows, _, arguments = qwen_granted_permission_records()
    rows[-1][1]['result']['_meta' if damage == 'metadata' else 'extra'] = 'foreign'
    with pytest.raises(CaptureError):
        captured(rows).completed_generator_value('qwen-code', expected_input=arguments)


def raw_records():
    args = {'path': 'draft/probe.generator', 'settings': {'marker': 'x' * 500},
            'prompt': 'private-child'}
    return [
        ('send', {'id': 7, 'method': 'session/prompt', 'params': {'sessionId': 'native',
                   'prompt': [{'text': 'never retain prompt body'}]}}),
        ('receive', {'method': 'session/update', 'params': {'sessionId': 'native', 'update': {
            'sessionUpdate': 'tool_call', 'toolCallId': 'native-call',
            'title': 'collimate_test_generator', 'status': 'in_progress', 'rawInput': args}}}),
        ('receive', {'method': 'session/update', 'params': {'sessionId': 'native', 'update': {
            'sessionUpdate': 'tool_call_update', 'toolCallId': 'native-call',
            'status': 'completed', 'rawOutput': {'content': [{'type': 'text',
                'text': json.dumps({'exit_code': 0, 'output': 'log' * 1000,
                                    'result': {'success': True}})}], 'details': {}}}}}),
        ('receive', {'id': 7, 'result': {'stopReason': 'end_turn'}}),
    ]


def generator_records(backend):
    """Actual five-record Pi/OpenCode layout with synthetic full result data."""
    rows = raw_records()
    value = {'exit_code': 0, 'output': 'fresh-child-marker\n' + 'native-child-log-' * 80,
             'result': {'success': True, 'error_message': ''}}
    text = json.dumps(value)
    final = rows[2][1]['params']['update']
    final['content'] = [{'type': 'content', 'content': {'type': 'text', 'text': text}}]
    final['rawOutput'] = ({'content': [{'type': 'text', 'text': text}], 'details': {'result': value}}
                         if backend == 'pi' else {'output': text, 'metadata': {'truncated': False}})
    progress = deepcopy(rows[1])
    progress[1]['params']['update']['sessionUpdate'] = 'tool_call_update'
    if backend == 'pi':
        progress[1]['params']['update'].pop('title')
    else:
        rows[1][1]['params']['update']['rawInput'] = {}
    rows.insert(2, progress)
    return rows, value


def cline_generator_records():
    """Measured six-record3.0.65 layout; synthetic arguments/ID/result only."""
    rows, value = generator_records('pi')
    arguments = {'path': 'draft/probe.generator', 'settings': {},
                 'prompt': 'private-cline-input-' * 35}
    pending = rows[1][1]['params']['update']
    pending.update(sessionUpdate='tool_call_update', status='pending', rawInput=arguments,
                   title='collimate__test_generator: {"path":"draft/probe.generator","settings":{},"prompt":"p...')
    rows[2][1]['params']['update'] = {'sessionUpdate': 'tool_call_update',
        'toolCallId': 'native-call', 'status': 'in_progress'}
    initial = deepcopy(rows[1])
    initial[1]['params']['update']['sessionUpdate'] = 'tool_call'
    final = rows[3][1]['params']['update']
    final.pop('content')
    final['rawOutput'] = {'content': [{'type': 'text', 'text': json.dumps(value)}],
                         'isError': False, 'structuredContent': {'result': value}}
    rows.insert(3, initial)
    return rows, value, arguments


def codex_generator_records():
    """Measured four-record1.13.1 ACP layout, synthetic values only."""
    rows = raw_records()
    arguments = {'path': 'draft/probe.generator', 'settings': {},
                 'prompt': 'private-codex-input-' * 35}
    wrapper = {'server': 'collimate', 'tool': 'test_generator', 'arguments': arguments}
    initial = rows[1][1]['params']['update']
    initial.update(title='mcp.collimate.test_generator', rawInput=deepcopy(wrapper),
                   kind='execute', _meta={'is_mcp_tool_call': True})
    value = {'exit_code': 0, 'output': 'fresh-child-marker\n' + 'native-child-log-' * 80,
             'result': {'success': True, 'error_message': ''}}
    final = rows[2][1]['params']['update']
    final['rawInput'] = deepcopy(wrapper)
    final['rawOutput'] = {'result': {'content': [{'type': 'text', 'text': json.dumps(value)}],
                                   'structuredContent': {'result': deepcopy(value)}, '_meta': None},
                          'error': None}
    return rows, value, arguments


def test_measured_codex_acp_full_result_wrapper_and_semantic_input_D23():
    rows, value, arguments = codex_generator_records()
    capture = captured(rows)
    original = deepcopy(capture.records)
    native_input = {'server': 'collimate', 'tool': 'test_generator', 'arguments': arguments}
    assert capture.completed_turn(expected_input=native_input)['input'] == native_input
    proof, actual = capture.completed_generator_result('codex', expected_input=arguments,
                                                       marker='fresh-child-marker')
    assert actual == value and len(actual['output']) > 1000
    assert proof['input'] == arguments
    assert proof['native_input'] == {'server': 'collimate', 'tool': 'test_generator',
                                     'arguments': arguments}
    assert proof['tool_call_id'] == 'native-call' and len(proof['records']) == 4
    assert capture.records == original


def test_normal_prompt_without_native_tool_cannot_be_generator_proof_D23():
    rows = raw_records()
    capture = captured([rows[0], rows[-1]])
    with pytest.raises(CaptureError, match='identified native tool'):
        capture.completed_turn(expected_input={})
    with pytest.raises(CaptureError):
        capture.completed_generator_value('qwen-code', expected_input={
            'path': 'draft/probe.generator', 'settings': {}, 'prompt': 'private-child'})


@pytest.mark.parametrize('refused', [False, True])
def test_codex_app_exact_inputless_progress_preserves_full_native_value_D23(refused):
    rows, value, arguments = codex_generator_records()
    rows.insert(2, ('receive', {'method': 'session/update', 'params': {'sessionId': 'native',
        'update': {'sessionUpdate': 'tool_call_update', 'toolCallId': 'native-call',
                   'status': 'in_progress'}}}))
    if refused:
        value = {'error': 'approval_refused', 'message': 'synthetic full approval refusal'}
        result = rows[-2][1]['params']['update']['rawOutput']['result']
        result['content'][0]['text'] = json.dumps(value)
        result['structuredContent']['result'] = deepcopy(value)
    capture = captured(rows)
    original = deepcopy(capture.records)
    proof, actual = capture.completed_generator_value('codex', expected_input=arguments)
    assert actual == value and proof['input'] == arguments
    assert proof['native_input']['arguments'] == arguments
    assert capture.records == original
    if refused:
        with pytest.raises(CaptureError, match='successful execution'):
            capture.completed_generator_result('codex', expected_input=arguments,
                                               marker='fresh-child-marker')


@pytest.mark.parametrize('damage', ['title', 'input', 'output', 'content', 'kind', 'meta',
                                   'id', 'session', 'status', 'type', 'duplicate',
                                   'late', 'initial_args', 'final_args', 'final_title', 'no_end'])
def test_codex_app_progress_cannot_hide_native_identity_arguments_or_result_D23(damage):
    rows, _, arguments = codex_generator_records()
    progress = {'sessionUpdate': 'tool_call_update', 'toolCallId': 'native-call', 'status': 'in_progress'}
    row = ('receive', {'method': 'session/update', 'params': {'sessionId': 'native', 'update': progress}})
    rows.insert(2, row)
    if damage == 'title': progress['title'] = 'mcp.collimate.test_generator'
    elif damage == 'input': progress['rawInput'] = deepcopy(rows[1][1]['params']['update']['rawInput'])
    elif damage == 'output': progress['rawOutput'] = {}
    elif damage == 'content': progress['content'] = []
    elif damage == 'kind': progress['kind'] = 'execute'
    elif damage == 'meta': progress['_meta'] = {}
    elif damage == 'id': progress['toolCallId'] = 'other'
    elif damage == 'session': row[1]['params']['sessionId'] = 'other'
    elif damage == 'status': progress['status'] = 'pending'
    elif damage == 'type': progress['sessionUpdate'] = 'tool_call'
    elif damage == 'duplicate': rows.insert(3, deepcopy(row))
    elif damage == 'late': rows[2], rows[3] = rows[3], rows[2]
    elif damage == 'initial_args': rows[1][1]['params']['update']['rawInput']['arguments']['prompt'] = 'other'
    elif damage == 'final_args': rows[-2][1]['params']['update']['rawInput']['arguments']['prompt'] = 'other'
    elif damage == 'final_title': rows[-2][1]['params']['update']['title'] = 'mcp.collimate.test_generator'
    elif damage == 'no_end': rows.pop()
    with pytest.raises(CaptureError):
        captured(rows).completed_generator_value('codex', expected_input=arguments)


@pytest.mark.parametrize('damage', [
    'id', 'session', 'prompt_id', 'prompt_id_type', 'no_end', 'cancelled_end', 'error_end',
    'duplicate_end', 'outside_turn', 'no_initial', 'duplicate_initial', 'extra_tool',
    'order', 'initial_status', 'final_status', 'title', 'final_title', 'kind', 'mcp_meta', 'null_meta',
    'server', 'tool', 'wrapper_extra', 'initial_args', 'final_args', 'no_initial_args',
    'no_final_args', 'raw_error', 'missing_error', 'result_error', 'missing_structured',
    'structured_extra', 'text_mismatch', 'text_type_mismatch', 'partial_text', 'multiple_text', 'extra_acp_content',
    'child_exit', 'child_bool_exit', 'child_failure', 'child_error', 'missing_marker',
])
def test_codex_acp_exact_native_identity_arguments_result_and_turn_required_D23(damage):
    rows, value, arguments = codex_generator_records()
    initial = rows[1][1]['params']['update']
    final = rows[2][1]['params']['update']
    raw = final['rawOutput']
    if damage == 'id': final['toolCallId'] = 'other'
    elif damage == 'session': rows[2][1]['params']['sessionId'] = 'other'
    elif damage == 'prompt_id': rows[-1][1]['id'] = 8
    elif damage == 'prompt_id_type': rows[-1][1]['id'] = '7'
    elif damage == 'no_end': rows.pop()
    elif damage == 'cancelled_end': rows[-1][1]['result']['stopReason'] = 'cancelled'
    elif damage == 'error_end': rows[-1][1]['error'] = {'code': -1}
    elif damage == 'duplicate_end': rows.append(deepcopy(rows[-1]))
    elif damage == 'outside_turn': rows[2], rows[3] = rows[3], rows[2]
    elif damage == 'no_initial': initial['sessionUpdate'] = 'tool_call_update'
    elif damage == 'duplicate_initial': rows.insert(2, deepcopy(rows[1]))
    elif damage == 'extra_tool':
        extra = deepcopy(rows[1])
        extra[1]['params']['update']['toolCallId'] = 'other'
        rows.insert(2, extra)
    elif damage == 'order': rows[1], rows[2] = rows[2], rows[1]
    elif damage == 'initial_status': initial['status'] = 'pending'
    elif damage == 'final_status': final['status'] = 'failed'
    elif damage == 'title': initial['title'] = 'mcp.other.test_generator'
    elif damage == 'final_title': final['title'] = 'mcp.other.test_generator'
    elif damage == 'kind': initial['kind'] = 'read'
    elif damage == 'mcp_meta': initial['_meta']['is_mcp_tool_call'] = False
    elif damage == 'null_meta': initial['_meta'] = None
    elif damage in ('server', 'tool'): final['rawInput'][damage] = 'other'
    elif damage == 'wrapper_extra': final['rawInput']['other'] = 'ignored'
    elif damage == 'initial_args': initial['rawInput']['arguments']['prompt'] = 'other'
    elif damage == 'final_args': final['rawInput']['arguments']['prompt'] = 'other'
    elif damage == 'no_initial_args': initial.pop('rawInput')
    elif damage == 'no_final_args': final.pop('rawInput')
    elif damage == 'raw_error': raw['error'] = {'message': 'native failure'}
    elif damage == 'missing_error': raw.pop('error')
    elif damage == 'result_error': raw['result']['isError'] = True
    elif damage == 'missing_structured': raw['result'].pop('structuredContent')
    elif damage == 'structured_extra': raw['result']['structuredContent']['error'] = 'ignored'
    elif damage == 'text_mismatch': raw['result']['content'][0]['text'] = '{}'
    elif damage == 'text_type_mismatch':
        raw['result']['content'][0]['text'] = json.dumps({**value, 'exit_code': False})
    elif damage == 'partial_text': raw['result']['content'][0]['text'] = '{'
    elif damage == 'multiple_text': raw['result']['content'].append({'type': 'text', 'text': '{}'})
    elif damage == 'extra_acp_content': final['content'] = [{'type': 'text', 'text': '{}'}]
    else:
        if damage == 'child_exit': value['exit_code'] = 1
        elif damage == 'child_bool_exit': value['exit_code'] = False
        elif damage == 'child_failure': value['result']['success'] = False
        elif damage == 'child_error': value['result']['error_message'] = 'failed'
        elif damage == 'missing_marker': value['output'] = 'different-marker'
        raw['result']['structuredContent']['result'] = value
        raw['result']['content'][0]['text'] = json.dumps(value)
    with pytest.raises((CaptureError, json.JSONDecodeError)):
        captured(rows).completed_generator_result('codex', expected_input=arguments,
                                                  marker='fresh-child-marker')


def test_measured_cline_native_full_result_and_order_D23():
    rows, value, arguments = cline_generator_records()
    proof, actual = captured(rows).completed_generator_result('cline',
        expected_input=arguments, marker='fresh-child-marker')
    assert actual == value and len(actual['output']) > 1000
    assert proof['tool_call_id'] == 'native-call' and len(proof['records']) == 6


APP_SETTINGS = {'overlay': 'child-value', 'marker': 'accepted-' + 'a' * 32,
                'parent_run_id': 'run-id', 'cell_id': 'cell-id',
                'expected_prompt': 'private-child-' + 'b' * 32, 'commands': 'ask',
                'expected_mount': '/workspace'}
APP_TITLE = 'collimate__test_generator: {"path":"draft/probe.generator","settings":{"overlay":"ch...'


def test_cline_exact_app_ascii_settings_title_and_full_input_D23():
    rows, value, arguments = cline_generator_records()
    arguments = {**arguments, 'settings': deepcopy(APP_SETTINGS)}
    for index in (1, 3):
        rows[index][1]['params']['update'].update(rawInput=arguments, title=APP_TITLE)
    assert _cline_generator_title(arguments) == APP_TITLE
    assert captured(rows).completed_generator_value('cline', expected_input=arguments)[1] == value
    changed = deepcopy(arguments)
    changed['settings']['expected_mount'] = '/unexpected'
    # This late field is outside the native title cap; full args remain owed.
    assert _cline_generator_title(changed) == APP_TITLE
    with pytest.raises(CaptureError):
        captured(rows).completed_generator_value('cline', expected_input=changed)


@pytest.mark.parametrize('damage', ['number', 'nested', 'missing_key', 'extra_key',
                                   'unicode_value', 'unicode_prompt', 'unicode_path'])
def test_cline_app_title_refuses_unsupported_string_settings_D23(damage):
    arguments = {'path': 'draft/probe.generator', 'settings': deepcopy(APP_SETTINGS),
                 'prompt': 'private-child'}
    if damage == 'number':
        arguments['settings']['marker'] = 1
    elif damage == 'nested':
        arguments['settings']['marker'] = {'text': 'a'}
    elif damage == 'missing_key':
        arguments['settings'].pop('cell_id')
    elif damage == 'extra_key':
        arguments['settings']['unreviewed'] = 'a'
    elif damage == 'unicode_value':
        arguments['settings']['marker'] = 'unreviewed-\u00e9'
    else:
        arguments['prompt' if damage == 'unicode_prompt' else 'path'] = 'unreviewed-\u00e9'
    with pytest.raises(CaptureError):
        _cline_generator_title(arguments)


@pytest.mark.parametrize('damage', ['pending_title', 'initial_title', 'prefix_only',
    'title_json', 'wrong_input', 'initial_input', 'pending_id', 'native_error', 'raw_content',
    'structured', 'structured_extra', 'missing_raw', 'partial_json', 'conflicting_acp',
    'order', 'missing_progress', 'extra_update', 'failed_child', 'missing_marker', 'missing_end',
    'missing_pending_title', 'missing_initial_title'])
def test_cline_measured_native_gate_refuses_misattribution_and_incomplete_result_D23(damage):
    rows, _, arguments = cline_generator_records()
    pending, initial, final = [rows[i][1]['params']['update'] for i in (1, 3, 4)]
    if damage in ('pending_title', 'initial_title'):
        (pending if damage == 'pending_title' else initial)['title'] = 'run_commands: echo wrong'
    elif damage in ('missing_pending_title', 'missing_initial_title'):
        (pending if damage == 'missing_pending_title' else initial).pop('title')
    elif damage == 'prefix_only':
        pending['title'] = 'collimate__test_generator: arbitrary'
    elif damage == 'title_json':
        pending['title'] = pending['title'].replace('"settings":{}', '"settings":[]')
    elif damage in ('wrong_input', 'initial_input'):
        (pending if damage == 'wrong_input' else initial)['rawInput'] = {**arguments, 'prompt': 'different'}
    elif damage == 'pending_id':
        pending['toolCallId'] = 'other'
    elif damage == 'native_error':
        final['rawOutput']['isError'] = True
    elif damage == 'raw_content':
        final['rawOutput']['content'][0]['text'] = '{}'
    elif damage == 'structured':
        final['rawOutput']['structuredContent']['result'] = {'error': 'changed'}
    elif damage == 'structured_extra':
        final['rawOutput']['structuredContent']['extra'] = 'changed'
    elif damage == 'missing_raw':
        final.pop('rawOutput')
    elif damage == 'partial_json':
        final['rawOutput']['content'][0]['text'] = 'partial…'
    elif damage == 'conflicting_acp':
        final['content'] = [{'type': 'content', 'content': {'type': 'text', 'text': '{}'}}]
    elif damage == 'order':
        rows[1], rows[3] = rows[3], rows[1]
    elif damage == 'missing_progress':
        rows.pop(2)
    elif damage == 'extra_update':
        rows.insert(3, deepcopy(rows[2]))
    elif damage in ('failed_child', 'missing_marker'):
        value = deepcopy(final['rawOutput']['structuredContent']['result'])
        if damage == 'failed_child':
            value['result']['success'] = False
        else:
            value['output'] = 'no child marker'
        final['rawOutput']['structuredContent']['result'] = value
        final['rawOutput']['content'][0]['text'] = json.dumps(value)
    elif damage == 'missing_end':
        rows.pop()
    with pytest.raises((CaptureError, ValueError)):
        captured(rows).completed_generator_result('cline', expected_input=arguments,
                                                  marker='fresh-child-marker')


@pytest.mark.parametrize('backend', ['pi', 'opencode'])
def test_measured_native_generator_results_agree_beyond_display_caps_D23(backend):
    rows, value = generator_records(backend)
    expected = rows[2][1]['params']['update']['rawInput']
    proof, result = captured(rows).completed_generator_result(backend, expected_input=expected,
                                                             marker='fresh-child-marker')
    assert result == value
    assert proof['tool_call_id'] == 'native-call'
    assert len(result['output']) > 1000


@pytest.mark.parametrize('backend', ['pi', 'opencode'])
def test_complete_native_value_decode_does_not_claim_child_success_D23(backend):
    rows, value = generator_records(backend)
    value['result']['success'] = False
    text = json.dumps(value)
    final = rows[3][1]['params']['update']
    final['content'][0]['content']['text'] = text
    final['rawOutput'] = ({'content': [{'type': 'text', 'text': text}], 'details': {'result': value}}
                         if backend == 'pi' else {'output': text, 'metadata': {'truncated': False}})
    capture = captured(rows)
    expected = rows[2][1]['params']['update']['rawInput']
    assert capture.completed_generator_value(backend, expected_input=expected)[1] == value
    with pytest.raises(CaptureError):
        capture.completed_generator_result(backend, expected_input=expected,
                                           marker='fresh-child-marker')


@pytest.mark.parametrize('backend', ['pi', 'opencode'])
@pytest.mark.parametrize('mutation', ['wrong_name', 'progress_name', 'acp_content', 'raw_result',
    'missing_raw', 'truncated', 'failed_child', 'exit', 'missing_marker', 'missing_end',
    'early_progress', 'duplicate_final'])
def test_measured_native_generator_gate_rejects_wrong_or_incomplete_result_D23(backend, mutation):
    rows, _ = generator_records(backend)
    expected = deepcopy(rows[2][1]['params']['update']['rawInput'])
    final = rows[3][1]['params']['update']
    if mutation == 'wrong_name':
        rows[1][1]['params']['update']['title'] = 'collimate_run_bash'
    elif mutation == 'progress_name':
        rows[2][1]['params']['update']['title'] = 'collimate_run_bash'
    elif mutation == 'acp_content':
        final['content'][0]['content']['text'] = json.dumps({'error': 'changed'})
    elif mutation == 'raw_result':
        if backend == 'pi':
            final['rawOutput']['details']['result'] = {'error': 'changed'}
        else:
            final['rawOutput']['output'] = json.dumps({'error': 'changed'})
    elif mutation == 'missing_raw':
        final.pop('rawOutput')
    elif mutation == 'truncated':
        if backend == 'pi':
            final['rawOutput']['content'][0]['text'] = 'partial…'
        else:
            final['rawOutput']['metadata']['truncated'] = True
    elif mutation in ('failed_child', 'exit', 'missing_marker'):
        value = json.loads(final['content'][0]['content']['text'])
        if mutation == 'failed_child':
            value['result']['success'] = False
        elif mutation == 'exit':
            value['exit_code'] = 1
        else:
            value['output'] = 'unrelated output'
        text = json.dumps(value)
        final['content'][0]['content']['text'] = text
        if backend == 'pi':
            final['rawOutput'] = {'content': [{'type': 'text', 'text': text}], 'details': {'result': value}}
        else:
            final['rawOutput']['output'] = text
    elif mutation == 'missing_end':
        rows.pop()
    elif mutation == 'early_progress':
        rows[1], rows[2] = rows[2], rows[1]
    elif mutation == 'duplicate_final':
        rows.insert(4, deepcopy(rows[3]))
    with pytest.raises((CaptureError, ValueError)):
        captured(rows).completed_generator_result(backend, expected_input=expected,
                                                  marker='fresh-child-marker')


def captured(records=None, **kwargs):
    capture = Capture(**kwargs)
    for direction, message in records or raw_records():
        capture.observe(direction, message)
    return capture


def test_raw_full_identity_and_titleless_result_are_retained_D23():
    records = raw_records()
    capture = captured(records)
    records[1][1]['params']['update']['rawInput']['prompt'] = 'library mutation'
    proof = capture.completed_turn()
    assert proof['input']['prompt'] == 'private-child'
    assert len(proof['input']['settings']['marker']) == 500
    assert len(proof['final_updates'][0]['rawOutput']['content'][0]['text']) > 3000
    assert proof['tool_call_id'] == 'native-call'
    assert proof['prompt_id'] == 7
    assert 'never retain' not in json.dumps(capture.records)


@pytest.mark.parametrize('mutation', ['wrong_id', 'extra_id', 'wrong_session', 'hidden_input',
                                     'missing_end', 'error_end', 'cancel_end', 'partial_result',
                                     'missing_input', 'missing_name', 'duplicate_prompt',
                                     'late_tool', 'early_tool', 'bool_response_id'])
def test_capture_refuses_unpaired_or_incomplete_native_records_D23(mutation):
    records = raw_records()
    initial = records[1][1]['params']['update']
    final = records[2][1]['params']['update']
    if mutation == 'wrong_id':
        final['toolCallId'] = 'another'
    elif mutation == 'extra_id':
        extra = deepcopy(records[1])
        extra[1]['params']['update']['toolCallId'] = 'another'
        records.insert(2, extra)
    elif mutation == 'wrong_session':
        records[2][1]['params']['sessionId'] = 'another'
    elif mutation == 'hidden_input':
        final['rawInput'] = {**initial['rawInput'], 'prompt': 'changed'}
    elif mutation == 'missing_end':
        records.pop()
    elif mutation == 'error_end':
        records[-1] = ('receive', {'id': 7, 'error': {'code': -1}})
    elif mutation == 'cancel_end':
        records[-1][1]['result']['stopReason'] = 'cancelled'
    elif mutation == 'partial_result':
        final['status'] = 'in_progress'
    elif mutation == 'missing_input':
        initial.pop('rawInput')
    elif mutation == 'missing_name':
        initial.pop('title')
    elif mutation == 'duplicate_prompt':
        records.append(deepcopy(records[0]))
    elif mutation == 'late_tool':
        records[2], records[3] = records[3], records[2]
    elif mutation == 'early_tool':
        records[0], records[1] = records[1], records[0]
    elif mutation == 'bool_response_id':
        records[0][1]['id'] = 1
        records[-1][1]['id'] = True
    with pytest.raises(CaptureError):
        captured(records).completed_turn(expected_input=raw_records()[1][1]['params']['update']['rawInput'])


def test_evolving_raw_inputs_preserved_with_exact_final_input_D23():
    records = raw_records()
    expected = deepcopy(records[1][1]['params']['update']['rawInput'])
    records[1][1]['params']['update']['rawInput'] = {'path': 'draft/'}
    records[2][1]['params']['update']['rawInput'] = expected
    proof = captured(records).completed_turn(expected_input=expected)
    assert proof['input'] == expected
    assert proof['records'][1]['message']['params']['update']['rawInput'] == {'path': 'draft/'}


@pytest.mark.parametrize('bound', ['records', 'bytes'])
def test_capture_bounds_fail_loudly_without_truncation_D23(bound):
    kwargs = {'max_records': 1} if bound == 'records' else {'max_bytes': 1}
    with pytest.raises(CaptureError, match='bound'):
        captured(**kwargs)


def test_capture_selects_structured_methods_and_scrubs_only_evidence_D23(tmp_path):
    records = raw_records()
    records[2][1]['params']['update']['rawOutput']['details'] = {'credential': 'synthetic-secret'}
    capture = Capture(secrets=('synthetic-secret',))
    capture.observe('receive', {'id': 100, 'result': {'stopReason': 'end_turn'}})
    capture.observe('receive', {'method': 'unrelated', 'params': {'text': 'session/update'}})
    capture.observe('send', {'id': 2, 'method': 'authenticate', 'params': {'key': 'synthetic-secret'}})
    for direction, message in records:
        capture.observe(direction, message)
    assert len(capture.records) == 4
    assert 'synthetic-secret' not in json.dumps(capture.records)
    assert records[2][1]['params']['update']['rawOutput']['details']['credential'] == 'synthetic-secret'
    assert capture.completed_turn()['input'] == records[1][1]['params']['update']['rawInput']
    path = tmp_path / 'capture.json'
    capture.write_private(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == capture.records
    with pytest.raises(FileExistsError):
        capture.write_private(path)


class Transport:
    def __init__(self, message, failure=None):
        self.message, self.failure = message, failure
        self.sent, self.received, self.closed = [], 0, 0

    async def send(self, message):
        self.sent.append(message)
        if self.failure:
            raise self.failure
        return 'sent'

    async def receive(self):
        self.received += 1
        if self.failure:
            raise self.failure
        return self.message

    async def close(self):
        self.closed += 1
        if self.failure:
            raise self.failure
        return 'closed'


def test_transport_preserves_original_objects_values_and_close_once_D23():
    async def run():
        message = raw_records()[1][1]
        original = Transport(message)
        observed = ObservedTransport(original, Capture())
        outgoing = raw_records()[0][1]
        assert await observed.send(outgoing) == 'sent'
        assert original.sent[0] is outgoing
        assert await observed.receive() is message
        assert original.received == 1
        assert await observed.close() == 'closed'
        assert await observed.close() is None
        assert original.closed == 1
    asyncio.run(run())


def test_receive_capture_failure_is_loud_and_original_close_still_runs_D23():
    async def run():
        original = Transport(raw_records()[1][1])
        observed = ObservedTransport(original, Capture(max_bytes=1))
        with pytest.raises(CaptureError, match='bound'):
            await observed.receive()
        assert original.received == 1
        assert await observed.close() == 'closed'
        await observed.close()
        assert original.closed == 1
    asyncio.run(run())


@pytest.mark.parametrize('failure', [RuntimeError('transport failed'), asyncio.CancelledError()])
@pytest.mark.parametrize('operation', ['send', 'receive', 'close'])
def test_transport_failure_and_cancel_pass_through_without_masking_D23(failure, operation):
    async def run():
        original = Transport(None, failure)
        observed = ObservedTransport(original, Capture())
        with pytest.raises(type(failure)) as raised:
            await (observed.send({'method': 'unrelated'}) if operation == 'send'
                   else getattr(observed, operation)())
        assert raised.value is failure
        if operation == 'close':
            await observed.close()
            assert original.closed == 1
    asyncio.run(run())


def test_observed_profile_preserves_real_process_kill_and_profile_D23():
    @dataclass
    class Process:
        transport: object
        kill: object
        proc: object
    @dataclass
    class Profile:
        spawn: object
        commands: str
        env_obj: object
    async def run():
        transport, kill, proc, env = Transport(None), object(), object(), object()
        calls = []
        async def spawn():
            calls.append(True)
            return Process(transport, kill, proc)
        profile = Profile(spawn, 'allow', env)
        observed = observed_profile(profile, Capture())
        process = await observed.spawn()
        assert calls == [True]
        assert process.transport.original is transport
        assert process.kill is kill and process.proc is proc
        assert observed.env_obj is env and observed.commands == 'allow'
        assert profile.spawn is spawn
    asyncio.run(run())


def qwen_generator_records(*, ancillary=True, resource_uri='collimate://test_generator'):
    """Measured0.23.3 eight-record layout, synthetic IDs/input/result bytes."""
    arguments = {'path': 'draft/probe.generator', 'settings': {},
                 'prompt': 'private-qwen-input-' * 35}
    value = {'exit_code': 0, 'output': 'fresh-child-marker\n' + 'native-child-log-' * 80,
             'result': {'success': True, 'error_message': ''}}
    meta = {'toolName': 'mcp__collimate__test_generator', 'provenance': 'mcp',
            'serverId': 'collimate'}
    resource_meta = {'toolName': 'read_mcp_resource', 'provenance': 'builtin'}
    title = 'test_generator (collimate MCP Server)'

    def start(call_id, name, kind, args, metadata, preparing):
        return {'sessionUpdate': 'tool_call' if preparing else 'tool_call_update',
                'toolCallId': call_id, 'status': 'pending' if preparing else 'in_progress',
                'title': name, 'content': [], 'locations': [], 'kind': kind,
                'rawInput': args, '_meta': {**metadata, **({'phase': 'preparing'} if preparing else {})}}

    updates = []
    if ancillary:
        updates = [start('resource-call', 'ReadMcpResource', 'fetch', {}, resource_meta, True),
                   start('resource-call', 'ReadMcpResource: collimate:' + resource_uri,
                         'fetch', {'server_name': 'collimate', 'uri': resource_uri},
                         resource_meta, False),
                   {'sessionUpdate': 'tool_call_update', 'toolCallId': 'resource-call',
                    'status': 'failed', '_meta': resource_meta, 'content': [
                        {'type': 'content', 'content': {'type': 'text',
                         'text': 'Unknown resource: ' + resource_uri}}]}]
    text = json.dumps(value, indent=2)
    updates += [start('target-call', title, 'other', {}, meta, True),
                start('target-call', title + ': ' + json.dumps(arguments, separators=(',', ':')),
                      'other', deepcopy(arguments), meta, False),
                {'sessionUpdate': 'tool_call_update', 'toolCallId': 'target-call',
                 'status': 'completed', '_meta': meta, 'rawOutput': text,
                 'content': [{'type': 'content', 'content': {'type': 'text', 'text': text}}]}]
    rows = [raw_records()[0]] + [
        ('receive', {'method': 'session/update', 'params': {'sessionId': 'native', 'update': row}})
        for row in updates] + [raw_records()[-1]]
    return rows, value, arguments


@pytest.mark.parametrize('ancillary', [False, True])
def test_qwen_complete_target_and_explicit_failed_resource_accounting_D23(ancillary):
    rows, value, arguments = qwen_generator_records(ancillary=ancillary)
    capture = captured(rows)
    original = deepcopy(capture.records)
    proof, actual = capture.completed_generator_result('qwen-code', expected_input=arguments,
                                                       marker='fresh-child-marker')
    assert actual == value and len(actual['output']) > 1000
    assert proof['tool_call_id'] == 'target-call'
    assert proof['title'] == 'test_generator (collimate MCP Server)'
    assert proof['input'] == arguments
    assert proof['records'] is capture.records and capture.records == original
    assert proof['prompt_id'] == 7 and proof['session_id'] == 'native'
    assert proof['final_updates'] == [capture.records[-2]['message']['params']['update']]
    assert proof['prompt_response'] == capture.records[-1]['message']
    if ancillary:
        assert proof['ancillary_calls'] == [{
            'tool_call_id': 'resource-call', 'title': 'ReadMcpResource',
            'classification': 'failed_test_generator_resource_lookup',
            'input': {'server_name': 'collimate', 'uri': 'collimate://test_generator'},
            'updates': [row['message']['params']['update'] for row in capture.records[1:4]]}]
        with pytest.raises(CaptureError, match='identified native tool'):
            capture.completed_turn(expected_input=arguments)
    else:
        assert proof['ancillary_calls'] == []


@pytest.mark.parametrize('uri', ['test_generator', 'collimate://test_generator', 'opaque-resource', 'a' * 4096])
def test_qwen_ancillary_uri_drives_exact_native_description_and_error_D23(uri):
    rows, value, arguments = qwen_generator_records(resource_uri=uri)
    capture = captured(rows)
    original = deepcopy(capture.records)
    proof, actual = capture.completed_generator_result('qwen-code', expected_input=arguments,
                                                      marker='fresh-child-marker')
    assert actual == value and capture.records == original
    assert proof['records'] is capture.records
    assert proof['ancillary_calls'][0]['input'] == {'server_name': 'collimate', 'uri': uri}
    assert proof['ancillary_calls'][0]['classification'] == 'failed_test_generator_resource_lookup'


@pytest.mark.parametrize('damage', ['title', 'error', 'server', 'extra', 'missing', 'empty',
                                   'long', 'number', 'nested', 'unicode'])
def test_qwen_ancillary_uri_shape_and_title_error_agreement_required_D23(damage):
    rows, _, arguments = qwen_generator_records(resource_uri='test_generator')
    progress, final = rows[2][1]['params']['update'], rows[3][1]['params']['update']
    if damage == 'title': progress['title'] += '-other'
    elif damage == 'error': final['content'][0]['content']['text'] += '-other'
    elif damage == 'server': progress['rawInput']['server_name'] = 'foreign'
    elif damage == 'extra': progress['rawInput']['hidden'] = 'ignored'
    elif damage == 'missing': progress['rawInput'].pop('uri')
    else:
        progress['rawInput']['uri'] = {'empty': '', 'long': 'a' * 4097, 'number': 1,
                                      'nested': {'uri': 'test_generator'}, 'unicode': 'é'}[damage]
    with pytest.raises((CaptureError, ValueError)):
        captured(rows).completed_generator_result('qwen-code', expected_input=arguments,
                                                  marker='fresh-child-marker')


@pytest.mark.parametrize('reordered', ['arguments', 'settings'])
def test_qwen_progress_title_uses_verified_native_argument_order_D23(reordered):
    rows, value, arguments = qwen_generator_records()
    arguments['settings'] = deepcopy(APP_SETTINGS)
    native = deepcopy(arguments)
    if reordered == 'arguments':
        native = dict(reversed(list(native.items())))
    else:
        native['settings'] = dict(reversed(list(native['settings'].items())))
    progress = rows[-3][1]['params']['update']
    progress['rawInput'] = native
    progress['title'] = 'test_generator (collimate MCP Server): ' + json.dumps(native, separators=(',', ':'))
    capture = captured(rows)
    original = deepcopy(capture.records)
    proof, actual = capture.completed_generator_result('qwen-code', expected_input=arguments,
                                                      marker='fresh-child-marker')
    assert actual == value and proof['input'] == arguments and capture.records == original


@pytest.mark.parametrize('refused', [False, True])
def test_qwen_reviewed_app_arguments_and_unsuccessful_value_are_complete_D23(refused):
    rows, _, arguments = qwen_generator_records()
    arguments['settings'] = deepcopy(APP_SETTINGS)
    progress = rows[-3][1]['params']['update']
    progress['rawInput'] = deepcopy(arguments)
    progress['title'] = 'test_generator (collimate MCP Server): ' + json.dumps(arguments, separators=(',', ':'))
    value = ({'error': 'approval_refused', 'message': 'synthetic full refusal'} if refused else
             {'exit_code': 0, 'output': 'fresh-child-marker\n',
              'result': {'success': False, 'error_message': 'synthetic full child failure'}})
    final = rows[-2][1]['params']['update']
    final['rawOutput'] = json.dumps(value)
    final['content'][0]['content']['text'] = final['rawOutput']
    proof, actual = captured(rows).completed_generator_value('qwen-code', expected_input=arguments)
    assert actual == value and proof['input'] == arguments
    with pytest.raises(CaptureError, match='successful execution'):
        captured(rows).completed_generator_result('qwen-code', expected_input=arguments,
                                                  marker='fresh-child-marker')


@pytest.mark.parametrize('damage', [
    'target_id', 'resource_id', 'alias_id', 'target_session', 'resource_session',
    'prompt_id_type', 'bool_prompt', 'no_end', 'error_end', 'cancel_end', 'duplicate_end',
    'outside_prompt', 'extra_target', 'extra_tool', 'interleave', 'resource_after_target',
    'target_order', 'resource_order', 'initial_name', 'progress_name', 'prefix_only',
    'initial_kind', 'progress_kind', 'initial_meta', 'progress_meta', 'final_meta',
    'initial_phase', 'initial_args', 'progress_args', 'extra_args', 'final_args', 'hidden_output',
    'initial_content', 'progress_locations', 'initial_status', 'progress_status', 'final_status',
    'resource_name', 'resource_server', 'resource_uri', 'resource_success', 'resource_result',
    'resource_extra_result', 'resource_progress_meta', 'resource_final_id',
    'no_raw', 'raw_dict', 'partial_json', 'nonobject_json', 'conflicting_content',
    'type_conflict', 'multiple_content', 'final_title', 'failed_child', 'exit_bool', 'missing_marker',
])
def test_qwen_exact_ids_input_all_representations_and_ancillary_order_required_D23(damage):
    rows, value, arguments = qwen_generator_records()
    r0, r1, r2, initial, progress, final = [row[1]['params']['update'] for row in rows[1:-1]]
    if damage == 'target_id': final['toolCallId'] = 'other'
    elif damage == 'resource_id': r0['toolCallId'] = ''
    elif damage == 'alias_id':
        for row in (r0, r1, r2): row['toolCallId'] = 'target-call'
    elif damage in ('target_session', 'resource_session'):
        rows[-2 if damage == 'target_session' else 2][1]['params']['sessionId'] = 'other'
    elif damage == 'prompt_id_type': rows[-1][1]['id'] = '7'
    elif damage == 'bool_prompt': rows[0][1]['id'] = True
    elif damage == 'no_end': rows.pop()
    elif damage == 'error_end': rows[-1][1]['error'] = {'code': -1}
    elif damage == 'cancel_end': rows[-1][1]['result']['stopReason'] = 'cancelled'
    elif damage == 'duplicate_end': rows.append(deepcopy(rows[-1]))
    elif damage == 'outside_prompt': rows[-2], rows[-1] = rows[-1], rows[-2]
    elif damage in ('extra_target', 'extra_tool'):
        extra = deepcopy(rows[-4])
        if damage == 'extra_tool': extra[1]['params']['update']['toolCallId'] = 'arbitrary-tool'
        rows.insert(4, extra)
    elif damage == 'interleave': rows[3], rows[4] = rows[4], rows[3]
    elif damage == 'resource_after_target': rows[1:-1] = rows[4:7] + rows[1:4]
    elif damage == 'target_order': rows[4], rows[5] = rows[5], rows[4]
    elif damage == 'resource_order': rows[1], rows[2] = rows[2], rows[1]
    elif damage == 'initial_name': initial['title'] = 'ReadMcpResource'
    elif damage in ('progress_name', 'prefix_only'): progress['title'] = 'test_generator (collimate MCP Server)'
    elif damage == 'initial_kind': initial['kind'] = 'execute'
    elif damage == 'progress_kind': progress['kind'] = 'fetch'
    elif damage in ('initial_meta', 'progress_meta', 'final_meta'):
        {'initial_meta': initial, 'progress_meta': progress, 'final_meta': final}[damage]['_meta']['serverId'] = 'other'
    elif damage == 'initial_phase': initial['_meta'].pop('phase')
    elif damage == 'initial_args': initial['rawInput'] = deepcopy(arguments)
    elif damage == 'progress_args': progress['rawInput']['prompt'] = 'other'
    elif damage == 'extra_args': progress['rawInput']['extra'] = 'ignored'
    elif damage == 'final_args': final['rawInput'] = deepcopy(arguments)
    elif damage == 'hidden_output': progress['rawOutput'] = 'ignored'
    elif damage == 'initial_content': initial['content'] = [{'type': 'text', 'text': 'ignored'}]
    elif damage == 'progress_locations': progress['locations'] = [{'path': 'unreviewed'}]
    elif damage == 'initial_status': initial['status'] = 'in_progress'
    elif damage == 'progress_status': progress['status'] = 'pending'
    elif damage == 'final_status': final['status'] = 'failed'
    elif damage == 'resource_name': r0['title'] = 'Shell'
    elif damage == 'resource_server': r1['rawInput']['server_name'] = 'other'
    elif damage == 'resource_uri': r1['rawInput']['uri'] = 'draft/main.py'
    elif damage == 'resource_success': r2['status'] = 'completed'
    elif damage == 'resource_result': r2['content'][0]['content']['text'] = 'contents of draft'
    elif damage == 'resource_extra_result': r2['rawOutput'] = 'ignored'
    elif damage == 'resource_progress_meta': r1['_meta']['provenance'] = 'mcp'
    elif damage == 'resource_final_id': r2['toolCallId'] = 'other'
    elif damage == 'no_raw': final.pop('rawOutput')
    elif damage == 'raw_dict': final['rawOutput'] = deepcopy(value)
    elif damage == 'partial_json':
        final['rawOutput'] = '{'
        final['content'][0]['content']['text'] = '{'
    elif damage == 'nonobject_json':
        final['rawOutput'] = '[]'
        final['content'][0]['content']['text'] = '[]'
    elif damage in ('conflicting_content', 'type_conflict'):
        final['content'][0]['content']['text'] = json.dumps({**value, 'exit_code': False})
    elif damage == 'multiple_content': final['content'].append(deepcopy(final['content'][0]))
    elif damage == 'final_title': final['title'] = initial['title']
    else:
        if damage == 'failed_child': value['result']['success'] = False
        elif damage == 'exit_bool': value['exit_code'] = False
        elif damage == 'missing_marker': value['output'] = 'other'
        final['rawOutput'] = json.dumps(value)
        final['content'][0]['content']['text'] = final['rawOutput']
    with pytest.raises((CaptureError, ValueError)):
        captured(rows).completed_generator_result('qwen-code', expected_input=arguments,
                                                  marker='fresh-child-marker')


@pytest.mark.parametrize('damage', ['numeric', 'nested', 'extra', 'missing', 'unicode'])
def test_qwen_json_title_refuses_unreviewed_js_input_shapes_D23(damage):
    rows, _, arguments = qwen_generator_records()
    arguments['settings'] = deepcopy(APP_SETTINGS)
    if damage == 'numeric': arguments['settings']['marker'] = 1
    elif damage == 'nested': arguments['settings']['marker'] = {'text': 'ignored'}
    elif damage == 'extra': arguments['settings']['extra'] = 'ignored'
    elif damage == 'missing': arguments['settings'].pop('marker')
    elif damage == 'unicode': arguments['prompt'] = 'unreviewed-é'
    with pytest.raises(CaptureError, match='ASCII'):
        captured(rows).completed_generator_value('qwen-code', expected_input=arguments)
