# SPDX-License-Identifier: MIT
"""Source-derived native decoder controls, not fabricated live witnesses."""
import asyncio
from copy import deepcopy
import json
import stat
import threading

import pytest

from tests_live.test_live_codex_qwen_generator_tool import (
    QWEN_TOOL, command_answer, qwen_tool_result, serving_cards, successful_child_result,
    write_private_evidence,
    collect_native_witness,
)
from tests_live import test_live_codex_qwen_generator_tool as capture_fixture


def codex_shape():
    """Selected fields measured in Codex0.157.0 actual17-record MCP capture.

    IDs/data below are synthetic sensitivity controls. This deliberately has
    no invented completion event: the actual retained native turn was unfinished.
    """
    arguments, value, _ = shape()
    result = {'content': [{'type': 'text', 'text': json.dumps(value)}],
              'structuredContent': {'result': value}, 'isError': False}
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'thread-1', 'session_id': 'thread-1',
                                           'cli_version': '0.157.0'}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-1',
                                        'root_turn_id': 'turn-1'}},
        {'type': 'turn_context', 'payload': {'turn_id': 'turn-1', 'root_turn_id': 'turn-1'}},
        {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'test_generator',
            'namespace': 'mcp__collimate', 'call_id': 'call-1', 'arguments': json.dumps(arguments)}},
        {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': 'thread-1',
            'turn_id': 'turn-1', 'item': {'type': 'McpToolCall', 'id': 'call-1',
                'server': 'collimate', 'tool': 'test_generator', 'arguments': arguments,
                'status': 'completed', 'result': result}}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call-1',
            'output': 'Wall time: 1.6195 seconds\nOutput:\n' + json.dumps({'result': value})}},
    ]
    return arguments, value, rows


def codex_pending_confirmation():
    """Measured native pending layout/menu; synthetic identity and arguments."""
    _, _, rows = codex_shape()
    rows = rows[:4]
    arguments = {'path': 'draft/probe.generator', 'settings': {},
                 'prompt': 'native-child-' + 'a' * 32}
    rows[-1]['payload']['arguments'] = json.dumps(arguments)
    screen = ('Calling collimate.test_generator + Show details Field 1/1'
              'Allow the collimate MCP server to run tool "test_generator"?'
              'path:draft/probe.generator prompt:' + arguments['prompt'] + ' settings:{}'
              ' › 1. Allow Run the tool and continue'
              ' 2. Allow for this session Run the tool and remember this choice for this session'
              ' 3. Always allow Run the tool and remember this choice for future tool calls'
              ' 4. Cancel Cancel this tool call enter to submit | esc to cancel◦•')
    return arguments, rows, screen


@pytest.mark.parametrize('answer', ['allow', 'reject'])
def test_actual_codex_polling_answers_only_bound_one_time_native_question_D23(
        tmp_path, monkeypatch, answer):
    from types import SimpleNamespace
    from tests_live._host_terminal import _HostTerminal
    arguments, rows, screen = codex_pending_confirmation()
    term = object.__new__(_HostTerminal)
    term._lock = threading.Lock()
    term._out = screen.encode()
    sent = []
    term.send = sent.append
    monkeypatch.setattr(capture_fixture, 'native_records', lambda home, before: rows)
    bridge = SimpleNamespace(workspace=str(tmp_path), read_cell_files=lambda **kwargs: {})
    observed = {}
    found = asyncio.run(collect_native_witness('codex', term, tmp_path, set(),
        arguments, answer, 'ask', bridge, [], 'marker', observed,
        timeout=.025, poll_interval=.005))
    assert not found, 'A native permission response is not a completed tool witness'
    assert sent == [b'\r'], 'The exact pending question needs one one-time native Allow'
    assert observed['codex_native_confirmation'] == {
        'call_id': 'call-1', 'thread_id': 'thread-1', 'turn_id': 'turn-1',
        'choice': 'allow_once'}


@pytest.mark.parametrize('mutation', ['wrong_namespace', 'wrong_tool', 'wrong_args',
    'wrong_turn', 'wrong_session', 'missing_id', 'duplicate_call', 'completed_call',
    'completed_turn', 'other_tool', 'other_question', 'rendered_args', 'stale_question',
    'echo_only', 'echo_full_menu', 'duplicate_question', 'session_choice', 'missing_menu'])
def test_codex_native_confirmation_rejects_unbound_or_stale_question_D23(mutation):
    arguments, rows, screen = codex_pending_confirmation()
    call = rows[-1]['payload']
    if mutation == 'wrong_namespace':
        call['namespace'] = 'mcp__other'
    elif mutation == 'wrong_tool':
        call['name'] = 'run_bash'
    elif mutation == 'wrong_args':
        call['arguments'] = json.dumps({**arguments, 'prompt': 'native-child-' + 'b' * 32})
    elif mutation == 'wrong_turn':
        rows[2]['payload']['turn_id'] = 'other-turn'
    elif mutation == 'wrong_session':
        rows[0]['payload']['session_id'] = 'other-session'
    elif mutation == 'missing_id':
        call.pop('call_id')
    elif mutation == 'duplicate_call':
        rows.append(deepcopy(rows[-1]))
    elif mutation == 'completed_call':
        rows.append({'type': 'response_item', 'payload': {'type': 'function_call_output',
                    'call_id': 'call-1', 'output': '{}'}})
    elif mutation == 'completed_turn':
        rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn-1'}})
    elif mutation == 'other_tool':
        rows.append({'type': 'event_msg', 'payload': {'type': 'item_started',
                    'item': {'type': 'CommandExecution'}}})
    elif mutation == 'other_question':
        screen = screen.replace('"test_generator"?', '"run_bash"?')
    elif mutation == 'rendered_args':
        screen = screen.replace(arguments['prompt'], 'native-child-' + 'b' * 32)
    elif mutation == 'stale_question':
        screen += '› Ask Codex to do anything?'
    elif mutation == 'echo_only':
        screen = 'user says Allow the collimate MCP server to run tool "test_generator"?'
    elif mutation == 'echo_full_menu':
        rows.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': screen}]}})
    elif mutation == 'duplicate_question':
        screen += screen
    elif mutation == 'session_choice':
        screen = screen.replace('› 1. Allow', '1. Allow').replace('2. Allow for', '› 2. Allow for')
    elif mutation == 'missing_menu':
        screen = screen.split('4. Cancel')[0]
    assert capture_fixture.codex_native_confirmation(rows, arguments, screen) is None


@pytest.mark.parametrize('premature', ['card', 'write', 'commit', 'stage'])
def test_native_codex_permission_never_bypasses_sdk_gate_or_child_boundaries_D23(
        tmp_path, monkeypatch, premature):
    from types import SimpleNamespace
    arguments, rows, screen = codex_pending_confirmation()
    sent = []
    term = SimpleNamespace(said=lambda: screen, send=sent.append)
    cards = [object()] if premature == 'card' else []
    if premature == 'write':
        (tmp_path / 'native-child.txt').write_text('premature')
    if premature == 'stage':
        (tmp_path / '.collrun' / 'forward-premature').mkdir(parents=True)
    bridge = SimpleNamespace(workspace=str(tmp_path), read_cell_files=lambda **kwargs:
                             {'native-child.txt': 'premature'} if premature == 'commit' else {})
    monkeypatch.setattr(capture_fixture, 'native_records', lambda home, before: rows)
    with pytest.raises(AssertionError, match='before native permission'):
        asyncio.run(collect_native_witness('codex', term, tmp_path, set(), arguments,
            'allow', 'ask', bridge, cards, 'marker', {}, timeout=.025, poll_interval=.005))
    assert sent == []


def test_codex_actual_measured_mcp_shape_pairs_full_native_id_input_result_D23():
    arguments, value, rows = codex_shape()
    identity, decoded, turn = capture_fixture.codex_tool_result(rows, arguments)
    assert identity == 'call-1' and decoded == value and turn == 'turn-1'


@pytest.mark.parametrize('mutation', ['call_id', 'item_id', 'output_id', 'namespace', 'server',
    'tool', 'call_args', 'item_args', 'status', 'native_error', 'content', 'structured',
    'wrapper', 'thread', 'turn', 'context_turn', 'duplicate_call', 'duplicate_result',
    'missing_output', 'other_function', 'custom_mutating_tool', 'native_file_change',
    'native_command'])
def test_codex_measured_decoder_rejects_misattributed_or_inconsistent_native_result_D23(mutation):
    arguments, _, rows = codex_shape()
    call, event, output = rows[3]['payload'], rows[4]['payload'], rows[5]['payload']
    item = event['item']
    if mutation == 'call_id':
        call['call_id'] = ''
    elif mutation == 'item_id':
        item['id'] = 'other'
    elif mutation == 'output_id':
        output['call_id'] = 'other'
    elif mutation == 'namespace':
        call['namespace'] = 'mcp__other'
    elif mutation == 'server':
        item['server'] = 'other'
    elif mutation == 'tool':
        item['tool'] = 'write_file'
    elif mutation == 'call_args':
        call['arguments'] = json.dumps({**arguments, 'prompt': 'hidden changed prompt'})
    elif mutation == 'item_args':
        item['arguments'] = {**arguments, 'prompt': 'hidden changed prompt'}
    elif mutation == 'status':
        item['status'] = 'in_progress'
    elif mutation == 'native_error':
        item['result']['isError'] = True
    elif mutation == 'content':
        item['result']['content'][0]['text'] = json.dumps({'error': 'different'})
    elif mutation == 'structured':
        item['result']['structuredContent'] = {'result': {'error': 'different'}}
    elif mutation == 'wrapper':
        output['output'] = 'Wall time: 1 seconds\nOutput:\n{"result":{"error":"different"}}'
    elif mutation == 'thread':
        event['thread_id'] = 'other-thread'
    elif mutation == 'turn':
        event['turn_id'] = 'other-turn'
    elif mutation == 'context_turn':
        rows[2]['payload']['turn_id'] = 'other-turn'
    elif mutation == 'duplicate_call':
        rows.insert(3, deepcopy(rows[3]))
    elif mutation == 'duplicate_result':
        rows.insert(4, deepcopy(rows[4]))
    elif mutation == 'missing_output':
        rows.pop()
    elif mutation == 'other_function':
        rows.insert(4, {'type': 'response_item', 'payload': {
            'type': 'function_call', 'name': 'exec_command', 'namespace': 'functions',
            'call_id': 'another', 'arguments': '{"cmd":"echo marker"}'}})
    elif mutation == 'custom_mutating_tool':
        rows.insert(4, {'type': 'response_item', 'payload': {
            'type': 'custom_tool_call', 'name': 'apply_patch', 'call_id': 'another', 'input': 'patch'}})
    elif mutation in ('native_file_change', 'native_command'):
        rows.insert(4, {'type': 'event_msg', 'payload': {'type': 'item_completed',
            'thread_id': 'thread-1', 'turn_id': 'turn-1', 'item': {
                'type': 'FileChange' if mutation == 'native_file_change' else 'CommandExecution',
                'id': 'another'}}})
    with pytest.raises((AssertionError, ValueError)):
        capture_fixture.codex_tool_result(rows, arguments)


def test_codex_actual_collector_rejects_child_commit_without_completed_native_turn_D23(
        tmp_path, monkeypatch):
    from types import SimpleNamespace
    arguments, _, rows = codex_shape()
    monkeypatch.setattr(capture_fixture, 'native_records', lambda home, before: rows)
    observed = {}
    found = asyncio.run(collect_native_witness('codex', SimpleNamespace(said=lambda: ''),
        tmp_path, set(), arguments, '', 'allow',
        SimpleNamespace(read_cell_files=lambda **kwargs: {'native-child.txt': 'marker'}),
        [], 'marker', observed, timeout=.025, poll_interval=.005))
    assert not found, 'A committed child and exact tool result are not a completed native turn'
    assert observed['native_records'] == rows


@pytest.mark.parametrize('mutation', ['valid', 'missing_end', 'wrong_turn', 'missing_turn',
    'error', 'duplicate_end', 'premature_end', 'unproven_alias', 'aborted'])
def test_codex_source_schema_completion_requires_same_ended_turn_D23(mutation):
    """Generated exact0.157 schema controls; real affected replay remains owed."""
    arguments, value, rows = codex_shape()
    ending = {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn-1'}}
    rows.append(ending)
    if mutation == 'missing_end':
        rows.pop()
    elif mutation == 'wrong_turn':
        ending['payload']['turn_id'] = 'another-turn'
    elif mutation == 'missing_turn':
        ending['payload'].pop('turn_id')
    elif mutation == 'error':
        ending['payload']['error'] = {'message': 'native failed'}
    elif mutation == 'duplicate_end':
        rows.append(deepcopy(ending))
    elif mutation == 'premature_end':
        rows.pop()
        rows.insert(4, ending)
    elif mutation == 'unproven_alias':
        ending['payload']['type'] = 'turn_complete'
    elif mutation == 'aborted':
        rows.insert(6, {'type': 'event_msg', 'payload': {'type': 'turn_aborted', 'reason': 'interrupted'}})
    if mutation == 'valid':
        assert capture_fixture.codex_completed_result(rows, arguments) == ('call-1', value, 'turn-1')
    else:
        with pytest.raises(AssertionError):
            capture_fixture.codex_completed_result(rows, arguments)


@pytest.mark.parametrize('answer', ['', 'reject'])
def test_codex_actual_collector_requires_native_result_and_completion_before_success_D23(
        tmp_path, monkeypatch, answer):
    from types import SimpleNamespace
    arguments, value, rows = codex_shape()
    if answer == 'reject':
        value = {'error': 'approval_refused', 'message': (
            'REFUSED: the user did not approve this command (or no one was '
            'there to approve it) — do not retry it unchanged; ask the user '
            'or take another approach.')}
        result = rows[4]['payload']['item']['result']
        result['content'][0]['text'] = json.dumps(value)
        result['structuredContent'] = {'result': value}
        rows[5]['payload']['output'] = 'Wall time: 1 seconds\nOutput:\n' + json.dumps({'result': value})
    rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn-1'}})
    monkeypatch.setattr(capture_fixture, 'native_records', lambda home, before: rows)
    observed = {}
    found = asyncio.run(collect_native_witness('codex', SimpleNamespace(said=lambda: ''),
        tmp_path, set(), arguments, answer, 'ask' if answer else 'allow',
        SimpleNamespace(read_cell_files=lambda **kwargs: {} if answer else {'native-child.txt': 'marker'}),
        [object()] if answer else [], 'marker', observed, timeout=.025, poll_interval=.005))
    assert found
    assert observed['codex_native_validation'] == {'call_id': 'call-1', 'turn_id': 'turn-1',
                                                   'completed_turn': True}


def shape():
    arguments = {'path': 'draft/probe.generator', 'settings': {'overlay': 'x' * 300},
                 'prompt': 'full private prompt'}
    value = {'exit_code': 0, 'output': 'D23-CHILD marker\nD23-DEPENDENCY 6.0.3\n',
             'result': {'success': True}}
    rows = [
        {'type': 'assistant', 'message': {'parts': [{'functionCall': {
            'id': 'native-id', 'name': QWEN_TOOL, 'args': arguments}}]}},
        {'type': 'tool_result', 'message': {'parts': [{'functionResponse': {
            'id': 'native-id', 'name': QWEN_TOOL, 'response': {'output': json.dumps(value)}}}]}},
    ]
    return arguments, value, rows


def test_full_native_qwen_source_shape_preserves_id_arguments_and_json_D23():
    arguments, value, rows = shape()
    identity, result = qwen_tool_result(rows, arguments)
    assert identity == 'native-id' and result == value
    successful_child_result(result, 'marker')


@pytest.mark.parametrize('mutation', ['missing_call', 'missing_result', 'wrong_id', 'empty_id',
    'wrong_call_name', 'wrong_result_name', 'wrong_args', 'duplicate_call', 'duplicate_result',
    'native_error', 'plain_prose', 'marker_only', 'non_object', 'other_mutating_tool'])
def test_native_qwen_source_decoder_rejects_missing_or_misattributed_evidence_D23(mutation):
    arguments, _, rows = shape()
    call = rows[0]['message']['parts'][0]['functionCall']
    response = rows[1]['message']['parts'][0]['functionResponse']
    if mutation == 'missing_call':
        rows.pop(0)
    elif mutation == 'missing_result':
        rows.pop()
    elif mutation == 'wrong_id':
        response['id'] = 'another-native-id'
    elif mutation == 'empty_id':
        call['id'] = response['id'] = ''
    elif mutation == 'wrong_call_name':
        call['name'] = 'run_shell_command'
    elif mutation == 'wrong_result_name':
        response['name'] = 'run_shell_command'
    elif mutation == 'wrong_args':
        call['args'] = {**arguments, 'prompt': 'different prompt'}
    elif mutation == 'duplicate_call':
        rows.insert(0, deepcopy(rows[0]))
    elif mutation == 'duplicate_result':
        rows.append(deepcopy(rows[1]))
    elif mutation == 'native_error':
        response['response'] = {'error': 'not allowed'}
    elif mutation in ('plain_prose', 'marker_only'):
        response['response']['output'] = 'It worked' if mutation == 'plain_prose' else 'marker'
    elif mutation == 'non_object':
        response['response']['output'] = '[]'
    elif mutation == 'other_mutating_tool':
        rows[0]['message']['parts'].append({'functionCall': {
            'id': 'shell-id', 'name': 'run_shell_command', 'args': {'command': 'echo marker'}}})
    with pytest.raises((AssertionError, ValueError)):
        qwen_tool_result(rows, arguments)


@pytest.mark.parametrize('mutation', ['exit', 'boolean_exit', 'failed', 'missing_marker',
                                      'missing_dependency', 'prose', 'missing_result'])
def test_child_success_requires_actual_result_output_and_dependency_D23(mutation):
    _, value, _ = shape()
    if mutation == 'exit':
        value['exit_code'] = 1
    elif mutation == 'boolean_exit':
        value['exit_code'] = False
    elif mutation == 'failed':
        value['result']['success'] = False
    elif mutation == 'missing_marker':
        value['output'] = 'D23-DEPENDENCY 6.0.3'
    elif mutation == 'missing_dependency':
        value['output'] = 'D23-CHILD marker'
    elif mutation == 'prose':
        value = {'message': 'child succeeded'}
    else:
        value.pop('result')
    with pytest.raises(AssertionError):
        successful_child_result(value, 'marker')


@pytest.mark.parametrize('answer', ['allow_once', 'reject_once'])
def test_public_sdk_command_card_script_runs_in_actual_inbox_D23(native_bridge, answer):
    """No private _serving_chat monkeypatch; the real SDK delivers the answer."""
    from _agent_shared.approval import approve_command
    arguments, _, _ = shape()
    cards = []
    native_bridge.stub_approvals(command_answer(native_bridge, arguments, answer, cards))
    async def operation():
        return await approve_command('test_generator', json.dumps(arguments))
    refusal = asyncio.run(serving_cards(operation))
    assert (refusal is None) is (answer == 'allow_once')
    assert len(cards) == 1
    assert native_bridge.read_cell_files(paths=['native-child.txt']) == {}


def test_card_rejects_preexisting_child_commit_D23(native_bridge):
    import gen
    arguments, _, _ = shape()
    gen.update_file('native-child.txt', 'premature')
    native_bridge.update_cell(writes=['native-child.txt'])
    respond = command_answer(native_bridge, arguments, 'allow_once', [])
    with pytest.raises(AssertionError, match='before approval'):
        respond({'title': 'Run test_generator', 'kind': 'execute', 'detail': json.dumps(arguments)})


def test_inbox_propagates_scenario_failure_D23(native_bridge):
    async def operation():
        raise AssertionError('Actual native scenario failed')
    with pytest.raises(AssertionError, match='Actual native scenario failed'):
        asyncio.run(serving_cards(operation))


def test_capture_is_private_valid_json_and_scrubs_only_selected_key_D23(tmp_path, monkeypatch):
    key = 'selected-key-"quoted\\value'
    monkeypatch.setenv('OPENROUTER_API_KEY', key)
    path = write_private_evidence(tmp_path, 'codex', 'allow', {
        'native_records': [{'text': key + '\nordinary newline'}],
        'other': 'Unrelated text remains',
    })
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    value = json.loads(path.read_text())
    assert value == {'native_records': [{'text': '[selected-key-redacted]\nordinary newline'}],
                     'other': 'Unrelated text remains'}


def test_capture_exclusive_creation_preserves_existing_evidence_D23(tmp_path, monkeypatch):
    from tests_live import test_live_codex_qwen_generator_tool as fixture
    monkeypatch.setenv('OPENROUTER_API_KEY', 'selected-capture-key')
    class Nonce:
        hex = 'fixed'
    monkeypatch.setattr(fixture.uuid, 'uuid4', lambda: Nonce())
    path = write_private_evidence(tmp_path, 'codex', 'allow', {'first': True})
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_private_evidence(tmp_path, 'codex', 'allow', {'replacement': True})
    assert path.read_bytes() == original


@pytest.mark.parametrize('history,server,tool', [
    ('', 'collimate', 'test_generator'),
    ('Call collimate test_generator exactly once\n', 'collimate', 'write_file'),
    ('Call collimate test_generator exactly once\n', 'other-server', 'test_generator'),
    ('Confirm MCP Tool Execution\nMCP Server: collimate\nTool: test_generator\n'
     'Yes, allow once\n', 'collimate', 'write_file'),
    ('Confirm MCP Tool Execution\nMCP Server: collimate\nTool: test_generator\n'
     'Yes, allow once\n', 'other-server', 'write_file'),
])
def test_actual_capture_polling_never_answers_pending_native_dialog_D23(
        tmp_path, monkeypatch, history, server, tool):
    """Drive actual polling with the real terminal history representation.

    Frozen FIRST sent Enter for prompt-contaminated write_file and for stale
    intended-tool history. Pending capture must retain the exact dialog and
    empty native records, without sending any native approval bytes.
    """
    from tests_live import test_live_codex_qwen_generator_tool as fixture
    from tests_live._host_terminal import _HostTerminal
    term = object.__new__(_HostTerminal)
    term._lock = threading.Lock()
    screen = (history + 'Confirm MCP Tool Execution\nMCP Server: ' + server
              + '\nTool: ' + tool + '\nAllow execution of MCP tool "' + tool
              + '" from server "' + server + '"?\nYes, allow once\nNo\n')
    term._out = screen.encode()
    sent = []
    term.send = sent.append
    monkeypatch.setattr(fixture, 'native_records', lambda home, before: [])
    arguments, _, _ = shape()
    observed = {}
    found = asyncio.run(collect_native_witness('qwen-code', term, tmp_path, set(),
        arguments, '', 'allow', None, [], 'marker', observed,
        timeout=.025, poll_interval=.005))
    assert not found, 'A pending native question is not a result witness'
    assert sent == [], 'Initial capture must not answer a native approval'
    assert observed == {'native_records': [], 'native_ui': screen}


@pytest.mark.parametrize('layout', ['history_and_rollout', 'history_only', 'two_rollouts'])
def test_actual_codex_capture_reads_session_tree_not_history_index_D23(
        tmp_path, monkeypatch, layout):
    """Real polling and files; synthetic payloads establish selection only."""
    from types import SimpleNamespace
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-selection-key')
    sessions = tmp_path / 'sessions/2026/10/04'
    sessions.mkdir(parents=True)
    old = sessions / 'rollout-old.jsonl'
    old.write_text(json.dumps({'observer_only': 'old test_generator record'}) + '\n')
    before = {old}
    (tmp_path / 'history.jsonl').write_text(json.dumps({
        'session_id': 'synthetic-session', 'ts': 0,
        'text': 'Please call test_generator',
    }) + '\n')
    records = [{'observer_only': 'new test_generator record', 'sequence': 1}]
    if layout != 'history_only':
        (sessions / 'rollout-new.jsonl').write_text(json.dumps(records[0]) + '\n')
    if layout == 'two_rollouts':
        (sessions / 'rollout-another.jsonl').write_text(json.dumps(records[0]) + '\n')
    # A synthetic committed view lets the observer's capture-only condition
    # finish. This is not a child-execution or native decoder assertion.
    bridge = SimpleNamespace(read_cell_files=lambda **kwargs: {'native-child.txt': 'marker'})
    term = SimpleNamespace(said=lambda: 'synthetic terminal screen')
    observed = {}
    operation = collect_native_witness('codex', term, tmp_path, before, {}, '',
        'allow', bridge, [], 'marker', observed, timeout=.025, poll_interval=.005)
    if layout == 'two_rollouts':
        with pytest.raises(AssertionError, match='exactly one new native session'):
            asyncio.run(operation)
    else:
        found = asyncio.run(operation)
        assert not found, 'Synthetic selection records cannot satisfy a native result/completion gate'
        assert observed['native_records'] == (records if layout == 'history_and_rollout' else [])


def test_exact_native_refusal_matches_actual_sdk_rejected_command_D23(monkeypatch):
    from _agent_shared import approval, generator_test
    cards = []
    async def reject(**kwargs):
        cards.append(kwargs)
        return 'reject_once'
    async def forbidden_child(*args, **kwargs):
        raise AssertionError('Rejected command must not execute a child')
    monkeypatch.setattr(approval, 'ask_human', reject)
    monkeypatch.setattr(generator_test, '_execute', forbidden_child)
    value = asyncio.run(generator_test.run_test(generator_test.CommandContext('host'),
        commands='ask', path='draft/probe.generator', settings={}, prompt='synthetic-private'))
    capture_fixture.refused_child_result(value)
    assert len(cards) == 1 and cards[0]['title'] == 'Run test_generator'
    assert json.loads(cards[0]['raw_input']) == {
        'path': 'draft/probe.generator', 'settings': {}, 'prompt': 'synthetic-private'}


@pytest.mark.parametrize('damage', ['missing_message', 'wrong_message', 'empty_message',
    'nontext_message', 'wrong_error', 'extra_field', 'null', 'list'])
def test_native_completed_refusal_requires_exact_source_representation_D23(tmp_path, monkeypatch, damage):
    from types import SimpleNamespace
    arguments, _value, rows = codex_shape()
    value = {'error': 'approval_refused', 'message': capture_fixture.REJECTED_COMMAND_MESSAGE}
    if damage == 'missing_message':
        value.pop('message')
    elif damage == 'wrong_message':
        value['message'] = 'REFUSED: command too long'
    elif damage == 'empty_message':
        value['message'] = ''
    elif damage == 'nontext_message':
        value['message'] = {'text': capture_fixture.REJECTED_COMMAND_MESSAGE}
    elif damage == 'wrong_error':
        value['error'] = 'test_generator_failed'
    elif damage == 'extra_field':
        value['success'] = True
    elif damage == 'null':
        value = None
    else:
        value = ['approval_refused', capture_fixture.REJECTED_COMMAND_MESSAGE]
    # Mutate all native representations together: rejection must come from
    # the exact refusal predicate, not a content/structured/output mismatch.
    result = rows[4]['payload']['item']['result']
    result['content'][0]['text'] = json.dumps(value)
    result['structuredContent'] = {'result': value}
    rows[5]['payload']['output'] = 'Wall time: 1 seconds\nOutput:\n' + json.dumps({'result': value})
    rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn-1'}})
    monkeypatch.setattr(capture_fixture, 'native_records', lambda home, before: rows)
    def collect():
        return asyncio.run(collect_native_witness('codex', SimpleNamespace(said=lambda: ''),
            tmp_path, set(), arguments, 'reject', 'ask',
            SimpleNamespace(read_cell_files=lambda **kwargs: {}), [object()], 'unused', {},
            timeout=.01, poll_interval=.001))
    if damage in ('null', 'list'):
        # The existing native decoder rejects non-dicts before the refusal
        # predicate; the bounded collector consequently cannot find a witness.
        assert collect() is False
    else:
        with pytest.raises(AssertionError):
            collect()


@pytest.mark.parametrize('layout', ['chat_and_usage', 'usage_only', 'two_chats'])
def test_actual_qwen_capture_reads_owned_chat_tree_not_usage_D23(tmp_path, monkeypatch, layout):
    from types import SimpleNamespace
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-selection-key')
    chats = tmp_path / 'projects/-workspace/chats'
    chats.mkdir(parents=True)
    old = chats / 'old.jsonl'
    old.write_text(json.dumps({'observer_only': 'old record'}) + '\n')
    before = {old}
    usage = tmp_path / 'usage'
    usage.mkdir()
    (usage / 'token-usage-2026-10.jsonl').write_text(json.dumps({'usage': True}) + '\n')
    rows = [{'observer_only': 'new record'}]
    if layout != 'usage_only':
        (chats / 'new.jsonl').write_text(json.dumps(rows[0]) + '\n')
    if layout == 'two_chats':
        (chats / 'another.jsonl').write_text(json.dumps(rows[0]) + '\n')
    observed = {}
    operation = collect_native_witness('qwen-code', SimpleNamespace(said=lambda: ''),
        tmp_path, before, {}, '', 'allow', None, [], 'unused', observed,
        timeout=.025, poll_interval=.005)
    if layout == 'two_chats':
        with pytest.raises(AssertionError, match='exactly one new native session'):
            asyncio.run(operation)
    else:
        assert asyncio.run(operation) is False, 'Selection data is not native completion evidence'
        assert observed['native_records'] == (rows if layout == 'chat_and_usage' else [])


@pytest.mark.parametrize('linked', ['home', 'project', 'chat', 'file'])
def test_actual_qwen_capture_refuses_linked_owned_transcript_D23(tmp_path, monkeypatch, linked):
    from types import SimpleNamespace
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-selection-key')
    home = tmp_path / 'home'
    chats = home / 'projects/-workspace/chats'
    chats.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'sentinel.jsonl'
    target.write_text('{"observer_only":"outside"}\n')
    if linked == 'file':
        (chats / 'linked.jsonl').symlink_to(target)
    else:
        path = {'home': home, 'project': home / 'projects/-workspace', 'chat': chats}[linked]
        import shutil
        shutil.rmtree(path)
        path.symlink_to(outside, target_is_directory=True)
    operation = collect_native_witness('qwen-code', SimpleNamespace(said=lambda: ''),
        home, set(), {}, '', 'allow', None, [], 'unused', {}, timeout=.01, poll_interval=.001)
    with pytest.raises(AssertionError, match='linked'):
        asyncio.run(operation)
    assert target.read_text() == '{"observer_only":"outside"}\n'


@pytest.mark.parametrize('kind', ['fifo', 'socket'])
def test_qwen_native_nonregular_transcript_refuses_without_blocking_D23(tmp_path, kind):
    import os
    from pathlib import Path
    import socket
    import subprocess
    import sys
    home = tmp_path / 'owned'
    chats = home / 'projects/-workspace/chats'
    chats.mkdir(parents=True)
    leaf = chats / 'native.jsonl'
    endpoint = None
    if kind == 'fifo':
        os.mkfifo(leaf)
    else:
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        original_directory = os.getcwd()
        try:
            os.chdir(chats)
            endpoint.bind(leaf.name)
        finally:
            os.chdir(original_directory)
    # Execute the actual reader body, never a copied selection/parser oracle.
    script = """import ast,json,os,stat,sys
from pathlib import Path
source=Path(sys.argv[1]);tree=ast.parse(source.read_text())
node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='qwen_native_records')
exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'))
try:
 qwen_native_records(Path(sys.argv[2]),set())
except AssertionError:
 print('nonregular-refused')
else:
 raise AssertionError('Nonregular transcript was accepted')
"""
    try:
        result = subprocess.run([sys.executable, '-I', '-c', script,
            str(Path(capture_fixture.__file__).resolve()), str(home)],
            env={**os.environ, 'OPENROUTER_API_KEY': 'synthetic-selection-key'},
            capture_output=True, text=True, timeout=3)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'nonregular-refused'
    finally:
        if endpoint is not None:
            endpoint.close()
        leaf.unlink()



def qwen_pending_native_confirmation_shape():
    """Two retained0.23.3 pending captures; synthetic identity/data, no completion."""
    arguments = {'path': 'draft/probe.generator', 'settings': {}, 'prompt': 'native-child-' + 'a' * 32}
    submitted = ('Call the collimate test_generator tool exactly once with these full arguments: '
                 + json.dumps(arguments) + '. Do not run a shell, delegate, write files yourself, '
                 'or retry a refusal. After the tool returns, stop.')
    rows = []
    for i, kind in enumerate(('user', 'system', 'system', 'system', 'assistant')):
        rows.append({'uuid': 'row-' + str(i), 'parentUuid': 'row-' + str(i - 1) if i else None,
                     'sessionId': 'own-session', 'timestamp': '2026-10-04T21:00:00Z',
                     'type': kind, 'provenance': 'real_user' if i == 0 else 'assistant_output' if i == 4 else 'system',
                     'cwd': '/workspace', 'version': '0.23.3'})
    rows[0]['message'] = {'role': 'user', 'parts': [{'text': submitted}]}
    for row, subtype in zip(rows[1:4], ('attribution_snapshot', 'file_history_snapshot', 'ui_telemetry')):
        row['subtype'] = subtype
    rows[3]['systemPayload'] = {'uiEvent': {'event.name': 'qwen-code.api_response', 'status_code': 200,
        'prompt_id': 'own-session########0', 'model': 'selected-model'}}
    rows[4]['model'] = 'selected-model'
    rows[4]['message'] = {'role': 'model', 'parts': [{'functionCall': {
        'id': 'own-call', 'name': QWEN_TOOL, 'args': arguments}}]}
    screen = ('? test_generator (collimate MCP Server) ←\n'
              + json.dumps(arguments, separators=(',', ':'))
              + '\nMCP Server: collimate\nTool: test_generator\n'
              + 'Allow execution of MCP tool "test_generator" from server "collimate"?\n'
              + '› 1. Yes, allow once\n'
              + '2. Always allow mcp__collimate__test_generator in this project\n'
              + '3. Always allow mcp__collimate__test_generator for this user\n'
              + '4. No, suggest changes (esc)\n⠏ Waiting for user confirmation...\n')
    return arguments, rows, screen


@pytest.mark.parametrize('answer', ['allow', 'reject'])
def test_actual_qwen_polling_answers_only_bound_one_time_native_question_D23(tmp_path, monkeypatch, answer):
    from types import SimpleNamespace
    arguments, rows, screen = qwen_pending_native_confirmation_shape()
    sent = []
    term = SimpleNamespace(said=lambda: screen, send=sent.append)
    monkeypatch.setattr(capture_fixture, 'qwen_native_records', lambda home, before: rows)
    bridge = SimpleNamespace(workspace=str(tmp_path), read_cell_files=lambda **kwargs: {})
    observed = {}
    found = asyncio.run(collect_native_witness('qwen-code', term, tmp_path, set(), arguments,
        answer, 'ask', bridge, [], 'marker', observed, timeout=.02, poll_interval=.005))
    assert not found, 'Native permission alone is not a completed tool/turn witness'
    assert sent == [b'1'], 'Only the exactly bound one-time native Yes may reach the independent SDK card'


def qwen_completed_native_shape(*, commands='ask', refused=False):
    """Measured9-row TTY completion and policy footer; synthetic values only."""
    arguments, rows, _ = qwen_pending_native_confirmation_shape()
    rows[2]['systemPayload'] = {'snapshots': [{'promptId': 'own-session########0'}]}
    rows[3]['systemPayload']['uiEvent']['response_id'] = 'initial-response'
    marker = 'a' * 32
    value = ({'error': 'approval_refused', 'message': capture_fixture.REJECTED_COMMAND_MESSAGE}
             if refused else {'exit_code': 0,
                 'output': 'D23-CHILD ' + marker + '\nD23-DEPENDENCY 6.0.3\n',
                 'result': {'success': True, 'error_message': ''}})
    for index, kind in ((5, 'system'), (6, 'tool_result'), (7, 'system'), (8, 'assistant')):
        rows.append({'uuid': 'row-' + str(index), 'parentUuid': 'row-' + str(index - 1),
            'sessionId': 'own-session', 'timestamp': '2026-10-04T21:00:01Z', 'type': kind,
            'provenance': {'system': 'system', 'tool_result': 'tool_result', 'assistant': 'assistant_output'}[kind],
            'cwd': '/workspace', 'version': '0.23.3'})
    rows[5].update(subtype='ui_telemetry', systemPayload={'uiEvent': {
        'event.name': 'qwen-code.tool_call', 'call_id': 'own-call', 'function_name': QWEN_TOOL,
        'function_args': arguments, 'mcp_server_name': 'collimate', 'status': 'success',
        'execution_status': 'success', 'success': True, 'prompt_id': 'own-session########0',
        'decision': 'accept' if commands == 'ask' else 'auto_accept',
        'tool_type': 'mcp', 'response_id': 'initial-response'}})
    rows[6].update(message={'role': 'user', 'parts': [{'functionResponse': {
        'id': 'own-call', 'name': QWEN_TOOL, 'response': {'output': json.dumps(value)}}}]},
        toolCallResult={'callId': 'own-call', 'status': 'success', 'executionStatus': 'success',
                        'resultDisplay': json.dumps(value)})
    rows[7].update(subtype='ui_telemetry', systemPayload={'uiEvent': {
        'event.name': 'qwen-code.api_response', 'status_code': 200, 'response_id': 'final-response',
        'prompt_id': 'own-session########0', 'model': 'selected-model', 'response_text': 'Tool returned. Stopping.'}})
    rows[8].update(model='selected-model', message={'role': 'model', 'parts': [{'text': 'Tool returned. Stopping.'}]})
    footer = ('⏸ Ask permissions' if commands == 'ask' else 'YOLO mode') + ' (shift + tab to cycle)'
    prefix = '>' if commands == 'ask' else '*'
    screen = ('   ▄▄▄▄▄▄\n✓ test_generator (collimate MCP Server)\n◆︎ Tool returned. Stopping.\n'
        '────────────────────────\n' + prefix + ' Type your message or @path/to/file\n────────────────────────\n'
        '➜ workspace · selected-model · 1.0m Context\n' + footer)
    return arguments, value, rows, screen


@pytest.mark.parametrize('commands', ['ask', 'allow'])
def test_actual_qwen_polling_requires_completed_stream_and_current_finished_ui_D23(tmp_path, monkeypatch, commands):
    from types import SimpleNamespace
    arguments, _value, rows, screen = qwen_completed_native_shape(commands=commands)
    monkeypatch.setattr(capture_fixture, 'qwen_native_records', lambda *_: rows)
    observed = {}
    bridge = SimpleNamespace(workspace=str(tmp_path), read_cell_files=lambda **_: {'native-child.txt': 'a' * 32})
    term = SimpleNamespace(said=lambda: screen + '\nEnter to steer · Ctrl+Q to queue')
    found = asyncio.run(collect_native_witness('qwen-code', term, tmp_path, set(), arguments,
        'allow', commands, bridge, [object()] if commands == 'ask' else [], 'a' * 32,
        observed, timeout=.01, poll_interval=.001))
    assert not found, 'Completed tool/assistant alone must not bypass an active native response'
    assert 'qwen_native_validation' not in observed


@pytest.mark.parametrize('commands', ['ask', 'allow'])
@pytest.mark.parametrize('refused', [False, True])
def test_qwen_completed_native_stream_and_policy_footer_preserve_full_value_D23(commands, refused):
    arguments, value, rows, screen = qwen_completed_native_shape(commands=commands, refused=refused)
    identity, actual, proof = capture_fixture.qwen_completed_result(rows, arguments, screen, commands=commands)
    assert identity == 'own-call' and actual == value
    assert proof == {'session_id': 'own-session', 'prompt_id': 'own-session########0',
        'assistant_uuid': 'row-8', 'completion': 'native_completed_stream_and_idle_ui'}
    if refused:
        capture_fixture.refused_child_result(actual)
    else:
        successful_child_result(actual, 'a' * 32)
    with pytest.raises(AssertionError):
        capture_fixture.qwen_completed_result(rows, arguments, screen,
            commands='allow' if commands == 'ask' else 'ask')


@pytest.mark.parametrize('damage', ['missing_final', 'missing_api', 'api_error', 'wrong_text',
    'wrong_model', 'wrong_prompt', 'wrong_parent', 'wrong_session', 'status_bool',
    'responding', 'cancelled', 'truncated', 'stale_idle', 'extra_call'])
def test_qwen_native_completion_rejects_partial_misattributed_or_abnormal_turn_D23(damage):
    arguments, _value, rows, screen = qwen_completed_native_shape()
    if damage == 'missing_final':
        rows.pop()
    elif damage == 'missing_api':
        rows.pop(7)
    elif damage == 'api_error':
        rows[7]['systemPayload']['uiEvent']['event.name'] = 'qwen-code.api_error'
    elif damage == 'wrong_text':
        rows[7]['systemPayload']['uiEvent']['response_text'] += ' changed'
    elif damage == 'wrong_model':
        rows[8]['model'] = 'different-model'
    elif damage == 'wrong_prompt':
        rows[7]['systemPayload']['uiEvent']['prompt_id'] += '-different'
    elif damage == 'wrong_parent':
        rows[8]['parentUuid'] = rows[4]['uuid']
    elif damage == 'wrong_session':
        rows[8]['sessionId'] = 'foreign-session'
    elif damage == 'status_bool':
        rows[7]['systemPayload']['uiEvent']['status_code'] = True
    elif damage == 'responding':
        screen += '\nEnter to steer · Ctrl+Q to queue\n⏸ Ask permissions (shift + tab to cycle)'
    elif damage == 'cancelled':
        screen += '\nUser cancelled the request.\n⏸ Ask permissions (shift + tab to cycle)'
    elif damage == 'truncated':
        screen += '\nResponse truncated due to token limits.\n⏸ Ask permissions (shift + tab to cycle)'
    elif damage == 'stale_idle':
        screen += '   ▄▄▄▄▄▄\n> Type your message or @path/to/file\n⏸ Ask permissions (shift + tab to cycle)'
    else:
        rows[8]['message']['parts'].append({'functionCall': rows[4]['message']['parts'][0]['functionCall']})
    with pytest.raises(AssertionError):
        capture_fixture.qwen_completed_result(rows, arguments, screen, commands='ask')


@pytest.mark.parametrize('damage', ['arguments', 'session', 'parent', 'version', 'tool',
    'selection', 'stale_suffix', 'completed'])
def test_qwen_one_time_native_confirmation_refuses_unbound_or_stale_panel_D23(damage):
    arguments, rows, screen = qwen_pending_native_confirmation_shape()
    if damage == 'arguments':
        rows[4]['message']['parts'][0]['functionCall']['args'] = {**arguments, 'prompt': 'wrong'}
    elif damage == 'session':
        rows[4]['sessionId'] = 'foreign'
    elif damage == 'parent':
        rows[4]['parentUuid'] = 'foreign'
    elif damage == 'version':
        rows[4]['version'] = '0.24.7'
    elif damage == 'tool':
        rows[4]['message']['parts'][0]['functionCall']['name'] = 'write_file'
    elif damage == 'selection':
        screen = screen.replace('› 1.', '1.').replace('2. Always', '› 2. Always')
    elif damage == 'stale_suffix':
        screen += 'extra current content'
    else:
        rows.append({'type': 'tool_result'})
    assert capture_fixture.qwen_native_confirmation(rows, arguments, screen) is None


@pytest.mark.parametrize('commands', ['ask', 'allow'])
@pytest.mark.parametrize('refused', [False, True])
def test_actual_qwen_completed_polling_keeps_sdk_card_and_child_decision_independent_D23(
        tmp_path, monkeypatch, commands, refused):
    from types import SimpleNamespace
    arguments, _value, rows, screen = qwen_completed_native_shape(commands=commands, refused=refused)
    monkeypatch.setattr(capture_fixture, 'qwen_native_records', lambda *_: rows)
    committed = {} if refused else {'native-child.txt': 'a' * 32}
    bridge = SimpleNamespace(workspace=str(tmp_path), read_cell_files=lambda **_: committed)
    observed = {}
    sent = []
    found = asyncio.run(collect_native_witness('qwen-code', SimpleNamespace(said=lambda: screen, send=sent.append),
        tmp_path, set(), arguments, 'reject' if refused else 'allow', commands, bridge,
        [object()] if commands == 'ask' else [], 'a' * 32, observed, timeout=.01, poll_interval=.001))
    assert found and sent == []
    assert observed['qwen_native_validation']['completed_turn'] is True
    assert observed['qwen_native_validation']['completion'] == 'native_completed_stream_and_idle_ui'


@pytest.mark.parametrize('commands', ['ask', 'allow'])
def test_qwen_source_mode_decision_and_input_prefix_stay_bound_to_commands_D23(commands):
    arguments, value, rows, screen = qwen_completed_native_shape(commands=commands)
    assert capture_fixture.qwen_completed_result(rows, arguments, screen, commands=commands)[1] == value
    rows[5]['systemPayload']['uiEvent']['decision'] = 'auto_accept' if commands == 'ask' else 'accept'
    with pytest.raises(AssertionError):
        capture_fixture.qwen_completed_result(rows, arguments, screen, commands=commands)
    rows[5]['systemPayload']['uiEvent']['decision'] = 'accept' if commands == 'ask' else 'auto_accept'
    prefix, wrong = ('>', '*') if commands == 'ask' else ('*', '>')
    screen = screen.replace('\n' + prefix + ' Type your message', '\n' + wrong + ' Type your message')
    with pytest.raises(AssertionError):
        capture_fixture.qwen_completed_result(rows, arguments, screen, commands=commands)


@pytest.mark.parametrize('damage', ['exit_bool', 'success_number'])
def test_qwen_full_resultdisplay_json_pair_preserves_bool_and_number_types_D23(damage):
    arguments, value, rows, screen = qwen_completed_native_shape()
    mismatched = deepcopy(value)
    if damage == 'exit_bool':
        mismatched['exit_code'] = False
    else:
        mismatched['result']['success'] = 1
    rows[6]['toolCallResult']['resultDisplay'] = json.dumps(mismatched)
    # Primary MCP value still satisfies success: disagreement must be detected
    # across the complete native result pair, not by the child's value predicate.
    successful_child_result(value, 'a' * 32)
    with pytest.raises(AssertionError):
        capture_fixture.qwen_completed_result(rows, arguments, screen, commands='ask')


def qwen_ordinary_preamble_shape(*, completed=False, commands='ask'):
    if completed:
        arguments, value, rows, screen = qwen_completed_native_shape(commands=commands)
    else:
        arguments, rows, screen = qwen_pending_native_confirmation_shape()
        value = None
    text = "I'll call the collimate test_generator tool with the specified arguments.\n\n"
    rows[4]['message']['parts'].insert(0, {'text': text})
    rows[3]['systemPayload']['uiEvent']['response_text'] = text
    return arguments, value, rows, screen


@pytest.mark.parametrize('completed,commands', [(False, 'ask'), (True, 'ask'), (True, 'allow')])
def test_qwen_ordinary_model_preamble_matches_initial_native_response_text_D23(completed, commands):
    arguments, value, rows, screen = qwen_ordinary_preamble_shape(completed=completed, commands=commands)
    if completed:
        assert capture_fixture.qwen_completed_result(rows, arguments, screen, commands=commands)[1] == value
    else:
        assert capture_fixture.qwen_native_confirmation(rows, arguments, screen)['call_id'] == 'own-call'


@pytest.mark.parametrize('completed', [False, True])
@pytest.mark.parametrize('damage', ['mismatched_telemetry', 'missing_telemetry', 'nontext',
    'unknown_part', 'extra_part_field', 'false_thought', 'too_long'])
def test_qwen_ordinary_model_preamble_refuses_untyped_or_uncorrelated_parts_D23(completed, damage):
    arguments, _value, rows, screen = qwen_ordinary_preamble_shape(completed=completed)
    part = rows[4]['message']['parts'][0]
    if damage == 'mismatched_telemetry':
        rows[3]['systemPayload']['uiEvent']['response_text'] += ' changed'
    elif damage == 'missing_telemetry':
        rows[3]['systemPayload']['uiEvent'].pop('response_text')
    elif damage == 'nontext':
        part['text'] = True
    elif damage == 'unknown_part':
        rows[4]['message']['parts'][0] = {'functionResponse': {'id': 'foreign'}}
    elif damage == 'extra_part_field':
        part['unreviewed'] = 'extra'
    elif damage == 'false_thought':
        part['thought'] = False
    else:
        part['text'] = 'x' * 4096
        rows[3]['systemPayload']['uiEvent']['response_text'] = part['text']
    if completed:
        with pytest.raises(AssertionError):
            capture_fixture.qwen_completed_result(rows, arguments, screen, commands='ask')
    else:
        assert capture_fixture.qwen_native_confirmation(rows, arguments, screen) is None
