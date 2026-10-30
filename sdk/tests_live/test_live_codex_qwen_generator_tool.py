# SPDX-License-Identifier: MIT
"""D23 native evidence: measured Codex result and source-grounded completion.

The Qwen decoder is pinned to 0.23.3's persisted parts and MCP result
conversion. Codex result matches an actual0.157.0 native capture; its completed
turn predicate uses that binary's generated RolloutLine schema and still needs
an affected actual replay. Qwen completion correlates its completed native
stream telemetry and finished TUI, without inventing a persisted STOP reason. This module never
uses the application's normalized/capped display trace as native evidence.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

import gen
import pytest

from tests_live.conftest import (bind_model_account, has_key, model_account,
                                model_for, requires_docker, run_on_test_loop)
from _agent_shared import cli_container, harness_homes
from _agent_shared.harnesses.codex import backend as codex_backend
from _agent_shared.harnesses.qwen_code import backend as qwen_code_backend
from tests_live.test_live_opencode_generator_tool import (
    dead, install_forward_recording_ack, tool_names,
)
from tests_live._host_terminal import _host_terminals, _type_line
from tests_live._host_terminal import _up_to_prompt

pytestmark = [pytest.mark.live, requires_docker,
              pytest.mark.usefixtures('one_loop_per_test')]
CHILD_PATH = 'draft/probe.generator'
QWEN_TOOL = 'mcp__collimate__test_generator'
REJECTED_COMMAND_MESSAGE = (
    'REFUSED: the user did not approve this command (or no one was '
    'there to approve it) — do not retry it unchanged; ask the user '
    'or take another approach.')


def codex_tool_result(records, arguments):
    """Decode the actual pinned0.157.0 MCP witness, without turn-end inference.

    Raw response_item and McpToolCall records independently carry the same
    call, full arguments and MCP result; the native output wrapper agrees with
    both content and structured result. No display trace is involved.
    """
    headers = [(i, row['payload']) for i, row in enumerate(records)
               if row.get('type') == 'session_meta']
    starts = [(i, row['payload']) for i, row in enumerate(records)
              if row.get('type') == 'event_msg'
              and row.get('payload', {}).get('type') == 'task_started']
    contexts = [(i, row['payload']) for i, row in enumerate(records)
                if row.get('type') == 'turn_context']
    calls, results, outputs = [], [], []
    for index, row in enumerate(records):
        payload = row.get('payload', {})
        if row.get('type') == 'response_item':
            kind = payload.get('type', '')
            # The fixture authorizes one MCP call. Other native function or
            # custom tool calls cannot be credited with the child's publication.
            assert not (kind.endswith('_call') and kind != 'function_call'), payload
            if kind == 'function_call':
                calls.append((index, payload))
            elif kind == 'function_call_output':
                outputs.append((index, payload))
        if (row.get('type') == 'event_msg' and payload.get('type') == 'item_completed'
                and payload.get('item', {}).get('type') == 'McpToolCall'):
            results.append((index, payload))
        if row.get('type') == 'event_msg' and payload.get('type') in ('item_started', 'item_completed'):
            # Generated0.157 TurnItem enumerates native command/file/delegation
            # items independently of response_item. Keep the fixture's one-tool
            # claim strict on both native representations.
            assert payload.get('item', {}).get('type') in {
                'UserMessage', 'Reasoning', 'AgentMessage', 'Plan',
                'ContextCompaction', 'McpToolCall'}, payload.get('item', {}).get('type')
    assert len(headers) == len(starts) == len(contexts) == len(calls) == len(results) == len(outputs) == 1
    hi, header = headers[0]
    si, start = starts[0]
    ci, context = contexts[0]
    fi, call = calls[0]
    ri, event = results[0]
    oi, output = outputs[0]
    assert hi < si < ci < fi < ri < oi
    assert header.get('cli_version') == '0.157.0'
    thread = header.get('id')
    turn = start.get('turn_id')
    assert isinstance(thread, str) and thread and header.get('session_id') == thread
    assert isinstance(turn, str) and turn and start.get('root_turn_id') == turn
    assert context.get('turn_id') == context.get('root_turn_id') == turn
    assert event.get('thread_id') == thread and event.get('turn_id') == turn
    identity = call.get('call_id')
    assert isinstance(identity, str) and identity
    assert call.get('namespace') == 'mcp__collimate' and call.get('name') == 'test_generator'
    assert isinstance(call.get('arguments'), str) and json.loads(call['arguments']) == arguments
    item = event['item']
    assert item.get('id') == identity and item.get('server') == 'collimate'
    assert item.get('tool') == 'test_generator' and item.get('status') == 'completed'
    assert item.get('arguments') == arguments
    result = item.get('result')
    assert isinstance(result, dict) and result.get('isError') is False
    structured = result.get('structuredContent')
    assert isinstance(structured, dict) and set(structured) == {'result'}
    value = structured['result']
    assert isinstance(value, dict)
    content = result.get('content')
    assert isinstance(content, list) and len(content) == 1
    assert content[0].get('type') == 'text' and isinstance(content[0].get('text'), str)
    assert json.loads(content[0]['text']) == value
    assert output.get('call_id') == identity and isinstance(output.get('output'), str)
    wrapped = re.fullmatch(r'Wall time: [0-9]+(?:\.[0-9]+)? seconds\nOutput:\n(.*)',
                           output['output'], re.DOTALL)
    assert wrapped is not None
    assert json.loads(wrapped.group(1)) == structured
    return identity, value, turn


def codex_completed_result(records, arguments):
    """Strict pinned-native correlation; completion is not a child commit.

    Exact0.157.0 generated RolloutLine.json TaskCompleteEventMsg requires
    type=task_complete and turn_id, with optional terminal error. Its serialized
    tag is task_complete; no guessed completion alias is accepted here.
    """
    identity, value, turn = codex_tool_result(records, arguments)
    endings = [(index, row['payload']) for index, row in enumerate(records)
               if row.get('type') == 'event_msg'
               and row.get('payload', {}).get('type') == 'task_complete']
    assert len(endings) == 1
    index, ending = endings[0]
    output_index = next(i for i, row in enumerate(records)
                        if row.get('type') == 'response_item'
                        and row.get('payload', {}).get('type') == 'function_call_output')
    assert index > output_index
    assert ending.get('turn_id') == turn and ending.get('error') is None
    assert not any(row.get('type') == 'event_msg'
                   and row.get('payload', {}).get('type') in ('turn_aborted', 'error')
                   for row in records)
    return identity, value, turn


def qwen_tool_result(records, arguments):
    """Exact native ID/name/input/result pairing, with no completion inference.

    Response.output is Qwen's pinned MCP content-to-text conversion. These
    tests establish decoder sensitivity; the first actual MCP witness must
    still validate that source-derived shape before it becomes a live gate.
    """
    calls, responses = [], []
    for row in records:
        parts = row.get('message', {}).get('parts', [])
        if row.get('type') == 'assistant':
            native_calls = [p['functionCall'] for p in parts if 'functionCall' in p]
            assert all(call.get('name') in {QWEN_TOOL, 'tool_search'} for call in native_calls), native_calls
            calls.extend(p['functionCall'] for p in parts if 'functionCall' in p
                         and p['functionCall'].get('name') == QWEN_TOOL)
        if row.get('type') == 'tool_result':
            responses.extend(p['functionResponse'] for p in parts if 'functionResponse' in p
                             and p['functionResponse'].get('name') == QWEN_TOOL)
    assert len(calls) == len(responses) == 1, (calls, responses)
    call, response = calls[0], responses[0]
    assert isinstance(call.get('id'), str) and call['id'], call
    assert response.get('id') == call['id'], response
    assert call.get('args') == arguments, call
    value = response.get('response')
    assert isinstance(value, dict) and set(value) == {'output'}, response
    result = json.loads(value['output'])
    assert isinstance(result, dict), result
    return call['id'], result


def qwen_native_confirmation(records, arguments, screen):
    if (set(arguments) != {'path', 'prompt', 'settings'}
            or arguments['path'] != 'draft/probe.generator' or arguments['settings'] != {}
            or not isinstance(arguments['prompt'], str)
            or re.fullmatch(r'native-child-[0-9a-f]{32}', arguments['prompt']) is None
            or not isinstance(screen, str) or len(records) != 5):
        return None
    ids = [r.get('uuid') for r in records]
    session = records[0].get('sessionId')
    if (not isinstance(session, str) or not session
            or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != 5):
        return None
    for i, row in enumerate(records):
        if (row.get('sessionId') != session or row.get('version') != '0.23.3'
                or row.get('cwd') != '/workspace'
                or row.get('parentUuid') != (ids[i-1] if i else None)
                or not isinstance(row.get('timestamp'), str) or not row['timestamp']):
            return None
    user, attribution, history, api, assistant = records
    if (user.get('type') != 'user' or user.get('provenance') != 'real_user'
            or user.get('message', {}).get('role') != 'user'
            or assistant.get('type') != 'assistant' or assistant.get('provenance') != 'assistant_output'
            or assistant.get('message', {}).get('role') != 'model'):
        return None
    submitted = ('Call the collimate test_generator tool exactly once with these full arguments: '
                 + json.dumps(arguments) + '. Do not run a shell, delegate, write files yourself, '
                 'or retry a refusal. After the tool returns, stop.')
    if user['message'].get('parts') != [{'text': submitted}]:
        return None
    for row, subtype in ((attribution, 'attribution_snapshot'), (history, 'file_history_snapshot'), (api, 'ui_telemetry')):
        if row.get('type') != 'system' or row.get('provenance') != 'system' or row.get('subtype') != subtype:
            return None
    event = api.get('systemPayload', {}).get('uiEvent', {})
    if (event.get('event.name') != 'qwen-code.api_response'
            or type(event.get('status_code')) is not int or event['status_code'] != 200
            or event.get('prompt_id') != session + '########0'
            or event.get('model') != assistant.get('model')):
        return None
    parts = assistant['message'].get('parts', [])
    calls = [p['functionCall'] for p in parts if isinstance(p, dict) and 'functionCall' in p]
    if len(calls) != 1 or any(not isinstance(p, dict) or set(p) not in ({'functionCall'}, {'text', 'thought'}, {'text'}) for p in parts):
        return None
    if any('text' in p and (not isinstance(p['text'], str)
            or (set(p) == {'text', 'thought'} and p.get('thought') is not True) or 'Allow execution of MCP tool \"test_generator\" from server \"collimate\"?' in p['text']) for p in parts):
        return None
    visible_preamble = ''.join(p['text'] for p in parts if set(p) == {'text'})
    if any(set(p) == {'text'} for p in parts) and (
            len(visible_preamble) > 4096 - len('...[truncated]')
            or event.get('response_text') != visible_preamble):
        return None
    call = calls[0]
    if (set(call) != {'id', 'name', 'args'} or call.get('name') != QWEN_TOOL
            or call.get('args') != arguments or not isinstance(call.get('id'), str) or not call['id']):
        return None
    compact = re.sub(r'\s+', '', screen)
    panel = ('?test_generator(collimateMCPServer)←'
             + json.dumps(call['args'], separators=(',', ':'), ensure_ascii=False)
             + 'MCPServer:collimateTool:test_generator'
             + re.sub(r'\s+', '', 'Allow execution of MCP tool \"test_generator\" from server \"collimate\"?')
             + '›1.Yes,allowonce'
             + '2.Alwaysallowmcp__collimate__test_generatorinthisproject'
             + '3.Alwaysallowmcp__collimate__test_generatorforthisuser'
             + '4.No,suggestchanges(esc)'
             + '⠏Waitingforuserconfirmation...')
    if not compact.endswith(panel):
        return None
    return {'call_id': call['id'], 'session_id': session, 'assistant_uuid': assistant['uuid'],
            'prompt_id': session + '########0'}


def qwen_completed_result(records, arguments, screen, *, commands):
    assert len(records) == 9
    assert [r.get('type') for r in records] == [
        'user', 'system', 'system', 'system', 'assistant',
        'system', 'tool_result', 'system', 'assistant']
    ids = [r.get('uuid') for r in records]
    assert all(isinstance(i, str) and i for i in ids) and len(set(ids)) == 9
    session = records[0].get('sessionId')
    assert isinstance(session, str) and session
    for index, row in enumerate(records):
        assert row.get('sessionId') == session and row.get('cwd') == '/workspace'
        assert row.get('version') == '0.23.3'
        assert row.get('parentUuid') == (ids[index-1] if index else None)
        assert isinstance(row.get('timestamp'), str) and row['timestamp']
        assert row.get('provenance') == {
            'user': 'real_user', 'system': 'system', 'assistant': 'assistant_output',
            'tool_result': 'tool_result'}[row['type']]
    submitted = ('Call the collimate test_generator tool exactly once with these full arguments: '
        + json.dumps(arguments) + '. Do not run a shell, delegate, write files yourself, '
        'or retry a refusal. After the tool returns, stop.')
    assert records[0].get('message') == {'role': 'user', 'parts': [{'text': submitted}]}
    assert records[1].get('subtype') == 'attribution_snapshot'
    assert records[2].get('subtype') == 'file_history_snapshot'
    assert records[2].get('systemPayload', {}).get('snapshots', [{}])[0].get('promptId') == session + '########0'
    identity, value = qwen_tool_result(records, arguments)
    call_parts = records[4].get('message', {}).get('parts', [])
    calls = [p.get('functionCall') for p in call_parts if 'functionCall' in p]
    assert len(calls) == 1 and calls[0] == {'id': identity, 'name': QWEN_TOOL, 'args': arguments}
    assert records[4].get('message', {}).get('role') == 'model'
    assert all(set(p) == {'functionCall'} or (set(p) in ({'text'}, {'text', 'thought'})
        and isinstance(p['text'], str)
        and (set(p) == {'text'} or p['thought'] is True)) for p in call_parts)
    visible_preamble = ''.join(p['text'] for p in call_parts if set(p) == {'text'})
    if any(set(p) == {'text'} for p in call_parts):
        assert len(visible_preamble) <= 4096 - len('...[truncated]')
        assert records[3]['systemPayload']['uiEvent'].get('response_text') == visible_preamble
    assert records[6].get('message', {}).get('role') == 'user'
    assert len(records[6]['message'].get('parts', [])) == 1
    result = records[6].get('toolCallResult', {})
    assert result.get('callId') == identity
    assert result.get('status') == result.get('executionStatus') == 'success'
    assert isinstance(result.get('resultDisplay'), str)
    # Canonical JSON comparison retains bool/number types across the full pair.
    assert json.dumps(json.loads(result['resultDisplay']), sort_keys=True, allow_nan=False) == json.dumps(
        value, sort_keys=True, allow_nan=False)
    assert records[5].get('subtype') == 'ui_telemetry'
    event = records[5].get('systemPayload', {}).get('uiEvent', {})
    assert event.get('event.name') == 'qwen-code.tool_call'
    assert event.get('call_id') == identity and event.get('function_name') == QWEN_TOOL
    assert event.get('function_args') == arguments and event.get('mcp_server_name') == 'collimate'
    assert event.get('status') == event.get('execution_status') == 'success' and event.get('success') is True
    assert event.get('prompt_id') == session + '########0'
    assert commands in {'ask', 'allow'}
    assert event.get('decision') == ('accept' if commands == 'ask' else 'auto_accept')
    assert event.get('tool_type') == 'mcp'
    final = records[8]
    assert final.get('message', {}).get('role') == 'model'
    parts = final['message'].get('parts', [])
    assert parts and all(set(p) == {'text'} and isinstance(p['text'], str) and p['text'] for p in parts)
    final_text = ''.join(p['text'] for p in parts)
    assert len(final_text) <= 4096
    for index, assistant in ((3, records[4]), (7, final)):
        assert records[index].get('subtype') == 'ui_telemetry'
        api = records[index].get('systemPayload', {}).get('uiEvent', {})
        assert api.get('event.name') == 'qwen-code.api_response'
        assert type(api.get('status_code')) is int and api['status_code'] == 200
        assert api.get('prompt_id') == session + '########0'
        assert api.get('model') == assistant.get('model') == records[4].get('model')
        assert isinstance(api.get('response_id'), str) and api['response_id']
        if index == 7:
            assert api.get('response_text') == final_text
            assert api['response_id'] != records[3]['systemPayload']['uiEvent']['response_id']
    assert event.get('response_id') == records[3]['systemPayload']['uiEvent']['response_id']
    assert isinstance(screen, str) and '   ▄▄▄▄▄▄' in screen
    frame = screen[screen.rfind('   ▄▄▄▄▄▄'):]
    # Responding adds Enter-to-steer before this same AutoAcceptIndicator.
    assert commands in {'ask', 'allow'}
    footer = ('⏸ Ask permissions' if commands == 'ask' else 'YOLO mode') + ' (shift + tab to cycle)'
    assert frame.rstrip().endswith(footer)
    prefix = '>' if commands == 'ask' else '*'
    assert '────────────────' in frame and re.search(r'\n' + re.escape(prefix) + r'[^\n]*\n', frame)
    assert '➜ workspace · ' + final['model'] in frame
    assert '✓ test_generator (collimate MCP Server)' in frame and '◆︎ ' in frame
    assert not re.search(r'[⠁-⣿]|Enter to steer|Waiting for user confirmation|esc to cancel', frame)
    assert not re.search(r'User cancelled the request\.|Press Ctrl\+Y to retry|Response (?:truncated|stopped)|Retrying in|Maximum number of session turns|Session token limit', screen)
    return identity, value, {'session_id': session, 'prompt_id': session + '########0',
        'assistant_uuid': final['uuid'], 'completion': 'native_completed_stream_and_idle_ui'}


def successful_child_result(value, marker):
    assert set(value) == {'exit_code', 'output', 'result'}, value
    assert type(value['exit_code']) is int and value['exit_code'] == 0, value
    assert isinstance(value['result'], dict) and value['result'].get('success') is True, value
    assert isinstance(value['output'], str), value
    assert 'D23-CHILD ' + marker in value['output'], value
    assert 'D23-DEPENDENCY 6.0.3' in value['output'], value


def refused_child_result(value):
    """Exact accepted SDK user-rejection value, measured in native Codex."""
    assert value == {'error': 'approval_refused', 'message': REJECTED_COMMAND_MESSAGE}, value


def command_answer(bridge, arguments, answer, cards):
    """Public scripted human; require the whole real card before answering."""
    def respond(card):
        assert card['title'] == 'Run test_generator' and card['kind'] == 'execute', card
        assert json.loads(card['detail']) == arguments, card
        assert bridge.read_cell_files(paths=['native-child.txt']) == {}, 'Child committed before approval'
        assert not (Path(bridge.workspace) / 'native-child.txt').exists(), 'Child wrote before approval'
        assert not list(Path(bridge.workspace).glob('.collrun/forward-*')), 'Child staged before approval'
        cards.append(deepcopy(card))
        return answer
    return respond


async def serving_cards(operation):
    """Use the real SDK inbox/card lifecycle; propagate seeded-turn failures."""
    errors, values = [], []
    async def on_turn(*_args, **_kwargs):
        try:
            values.append(await operation())
        except BaseException as exc:
            errors.append(exc)
    await gen.serve_chat(on_turn=on_turn, files=True,
                         threads_dir=gen.run_path('proof/threads'),
                         pointer_file=gen.run_path('proof/inbox.chat'),
                         initial_message='Capture one native tool witness')
    if errors:
        raise errors[0]
    assert len(values) == 1, values
    return values[0]


def native_records(home, before):
    """Read only this fixture's newly-created active native home files."""
    paths = set(home.rglob('*.jsonl')) - before
    if not paths:
        return []
    assert len(paths) == 1, 'Expected exactly one new native session'
    raw = next(iter(paths)).read_text(encoding='utf-8')
    key = os.environ['OPENROUTER_API_KEY']
    if key in raw or json.dumps(key)[1:-1] in raw:
        raise AssertionError('Selected credential exposed in native session')
    # A concurrently appended final line may be incomplete; earlier complete
    # lines remain authoritative. Never discard malformed complete records.
    lines = raw.splitlines()
    if raw and not raw.endswith('\n'):
        lines = lines[:-1]
    return [json.loads(line) for line in lines if line.strip()]



def qwen_native_records(home, before):
    """Pinned /workspace native chat tree; usage logs are not conversations."""
    home = Path(home).absolute()
    assert not home.is_symlink(), 'Qwen native home is linked'
    if not home.exists():
        return []
    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in ('projects', '-workspace', 'chats'):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                return []
            except OSError as exc:
                raise AssertionError('Qwen native chat directory is linked or unreadable') from exc
            os.close(directory)
            directory = child
        root = home / 'projects/-workspace/chats'
        paths = {root / name for name in os.listdir(directory) if name.endswith('.jsonl')} - before
        if not paths:
            return []
        assert len(paths) == 1, 'Expected exactly one new native session'
        path = next(iter(paths))
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError as exc:
            raise AssertionError('Qwen native chat file is linked or unreadable') from exc
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise AssertionError('Qwen native chat is not a regular file')
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            raw = stream.read()
    finally:
        os.close(directory)
    key = os.environ['OPENROUTER_API_KEY']
    if key in raw or json.dumps(key)[1:-1] in raw:
        raise AssertionError('Selected credential exposed in native session')
    lines = raw.splitlines()
    if raw and not raw.endswith('\n'):
        lines = lines[:-1]
    return [json.loads(line) for line in lines if line.strip()]


def write_private_evidence(directory, harness, level, value):
    """Exclusive private captures; no credentials or general-home discovery."""
    path = directory / f'{harness}-{level}-{uuid.uuid4().hex}.json'
    key = os.environ['OPENROUTER_API_KEY']
    def scrub(item):
        if isinstance(item, str):
            return item.replace(key, '[selected-key-redacted]')
        if isinstance(item, list):
            return [scrub(part) for part in item]
        if isinstance(item, dict):
            return {scrub(name): scrub(part) for name, part in item.items()}
        return item
    payload = json.dumps(scrub(value), ensure_ascii=False, indent=2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(payload)
    return path


def codex_native_confirmation(records, arguments, screen):
    """Bind the observed0.157 menu suffix to one sole unfinished native call.

    HostTerminal.said is cumulative stripped output, not a screen emulator.
    Accept only the complete measured menu at its current suffix, followed by
    observed spinner glyphs alone. Earlier, duplicate or echoed menus with
    additional text cannot authorize a response. This bounded fixture's args
    have no whitespace/control characters or arbitrary nested settings.
    """
    if (set(arguments) != {'path', 'prompt', 'settings'}
            or arguments['path'] != CHILD_PATH or arguments['settings'] != {}
            or not isinstance(arguments['prompt'], str)
            or re.fullmatch(r'native-child-[0-9a-f]{32}', arguments['prompt']) is None):
        return None
    question = 'AllowthecollimateMCPservertoruntool"test_generator"?'
    headers = [(i, row.get('payload', {})) for i, row in enumerate(records)
               if row.get('type') == 'session_meta']
    starts = [(i, row.get('payload', {})) for i, row in enumerate(records)
              if row.get('type') == 'event_msg' and row.get('payload', {}).get('type') == 'task_started']
    contexts = [(i, row.get('payload', {})) for i, row in enumerate(records)
                if row.get('type') == 'turn_context']
    calls = []
    for index, row in enumerate(records):
        payload = row.get('payload', {})
        kind = payload.get('type', '')
        if row.get('type') == 'response_item':
            if kind == 'message':
                # An echoed menu in model/user text is not a native dialog.
                for part in payload.get('content', []):
                    if isinstance(part, dict) and question in re.sub(r'\s+', '', str(part.get('text', ''))):
                        return None
            if kind.endswith('_call'):
                if kind != 'function_call':
                    return None
                calls.append((index, payload))
            if kind == 'function_call_output':
                return None
        if row.get('type') == 'event_msg':
            if kind in ('task_complete', 'turn_complete', 'turn_aborted', 'error'):
                return None
            if kind in ('item_started', 'item_completed') and payload.get('item', {}).get('type') not in {
                    'UserMessage', 'Reasoning', 'AgentMessage', 'Plan', 'ContextCompaction'}:
                return None
    if not (len(headers) == len(starts) == len(contexts) == len(calls) == 1):
        return None
    hi, header = headers[0]
    si, start = starts[0]
    ci, context = contexts[0]
    fi, call = calls[0]
    thread, turn, identity = header.get('id'), start.get('turn_id'), call.get('call_id')
    if (not hi < si < ci < fi or header.get('cli_version') != '0.157.0'
            or not isinstance(thread, str) or not thread or header.get('session_id') != thread
            or not isinstance(turn, str) or not turn or start.get('root_turn_id') != turn
            or context.get('turn_id') != turn or context.get('root_turn_id') != turn
            or not isinstance(identity, str) or not identity
            or call.get('namespace') != 'mcp__collimate' or call.get('name') != 'test_generator'
            or not isinstance(call.get('arguments'), str)):
        return None
    try:
        if json.loads(call['arguments']) != arguments:
            return None
    except ValueError:
        return None
    # Core mcp_tool_call.rs constructs this question and one-time choice;
    # the exact native0.157 captured TTY supplies rendered field/menu ordering.
    compact = re.sub(r'\s+', '', screen)
    menu = ('Callingcollimate.test_generator+ShowdetailsField1/1' + question
            + 'path:' + arguments['path'] + 'prompt:' + arguments['prompt'] + 'settings:{}'
            + '›1.AllowRunthetoolandcontinue'
            + '2.AllowforthissessionRunthetoolandrememberthischoiceforthissession'
            + '3.AlwaysallowRunthetoolandrememberthischoiceforfuturetoolcalls'
            + '4.CancelCancelthistoolcallentertosubmit|esctocancel')
    if compact.count(question) != 1 or re.search(re.escape(menu) + r'[◦•]*\Z', compact) is None:
        return None
    return {'call_id': identity, 'thread_id': thread, 'turn_id': turn}


async def collect_native_witness(harness, term, home, before, arguments, answer,
                                 commands, bridge, cards, marker, observed,
                                 *, timeout=240, poll_interval=.5):
    """Observe exact completion; answer only the measured bound Codex question.

    Native one-time permission lets the tool reach the independent SDK command
    card. That real card still decides child allow/reject. Native finished
    telemetry and current UI must agree before Qwen completion is credited.
    """
    # Codex's home history.jsonl is a prompt index; its native exporter reads
    # session transcripts only below sessions/. Keep the one-new-file check.
    transcript_home = home / 'sessions' if harness == 'codex' else home
    deadline = time.monotonic() + timeout
    native_confirmed = None
    while time.monotonic() < deadline:
        records = (qwen_native_records(home, before) if harness == 'qwen-code'
                   else native_records(transcript_home, before))
        observed.update(native_records=records, native_ui=term.said())
        if harness == 'qwen-code':
            if commands == 'ask' and native_confirmed is None:
                pending = qwen_native_confirmation(records, arguments, observed['native_ui'])
                if pending is not None:
                    assert not cards, 'SDK card arrived before native permission'
                    assert bridge.read_cell_files(paths=['native-child.txt']) == {}, 'Child committed before native permission'
                    assert not (Path(bridge.workspace) / 'native-child.txt').exists(), 'Child wrote before native permission'
                    assert not list(Path(bridge.workspace).glob('.collrun/forward-*')), 'Child staged before native permission'
                    native_confirmed = pending
                    observed['qwen_native_confirmation'] = {**pending, 'choice': 'proceed_once', 'sent_hex': '31'}
                    term.send(b'1')
            try:
                identity, value, completion = qwen_completed_result(
                    records, arguments, observed['native_ui'], commands=commands)
            except AssertionError:
                pass
            else:
                assert len(cards) == (1 if commands == 'ask' else 0), cards
                if answer == 'reject':
                    assert value.get('error') == 'approval_refused', value
                    refused_child_result(value)
                    assert bridge.read_cell_files(paths=['native-child.txt']) == {}
                else:
                    successful_child_result(value, marker)
                    assert bridge.read_cell_files(paths=['native-child.txt']) == {'native-child.txt': marker}
                observed['qwen_native_validation'] = {'call_id': identity, **completion,
                                                     'completed_turn': True}
                return True
        else:
            assert harness == 'codex'
            if commands == 'ask' and native_confirmed is None:
                pending = codex_native_confirmation(records, arguments, observed['native_ui'])
                if pending is not None:
                    assert not cards, 'SDK card arrived before native permission'
                    assert bridge.read_cell_files(paths=['native-child.txt']) == {}, 'Child committed before native permission'
                    assert not (Path(bridge.workspace) / 'native-child.txt').exists(), 'Child wrote before native permission'
                    assert not list(Path(bridge.workspace).glob('.collrun/forward-*')), 'Child staged before native permission'
                    native_confirmed = pending
                    observed['codex_native_confirmation'] = {**pending, 'choice': 'allow_once'}
                    term.send(b'\r')
            try:
                identity, value, turn = codex_completed_result(records, arguments)
            except AssertionError:
                pass
            else:
                assert len(cards) == (1 if commands == 'ask' else 0), cards
                if answer == 'reject':
                    refused_child_result(value)
                    assert bridge.read_cell_files(paths=['native-child.txt']) == {}
                else:
                    successful_child_result(value, marker)
                    assert bridge.read_cell_files(paths=['native-child.txt']) == {'native-child.txt': marker}
                observed['codex_native_validation'] = {'call_id': identity, 'turn_id': turn,
                                                       'completed_turn': True}
                return True
        await asyncio.sleep(poll_interval)
    return False


@pytest.mark.parametrize('harness', ['codex', 'qwen-code'])
@pytest.mark.parametrize('level', ['allow', 'ask-allow', 'ask-reject'])
def test_native_terminal_generator_witness_capture_D23(docker_env_bridge, monkeypatch, harness, level):
    """Opt-in measured Codex result/completion gate or qualified Qwen capture."""
    if os.environ.get('COLLIMATE_LIVE_D23_CAPTURE') != '1':
        pytest.skip('Explicit paid witness capture opt-in required')
    if not has_key('openrouter'):
        pytest.skip('Needs authorized OPENROUTER_API_KEY')
    directory = Path(os.environ['COLLIMATE_LIVE_D23_EVIDENCE']).resolve(strict=True)
    assert directory.is_dir()
    backend = codex_backend if harness == 'codex' else qwen_code_backend
    commands, _, answer = level.partition('-')
    bridge = docker_env_bridge
    bridge.channel, bridge.cell_id = 'p4-native-' + harness, 'p4-native-' + harness + '-cell'
    install_forward_recording_ack(bridge, monkeypatch)
    bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': commands}
    bridge.packages = ({'codex': '0.164.0', 'codex-acp': '2.2.2'} if harness == 'codex'
                       else {'qwen-code': '0.26.1'})
    model = model_for('openrouter')
    bind_model_account(model_account('openrouter', model=model, harness=harness))
    terms = _host_terminals(bridge, monkeypatch)
    marker = uuid.uuid4().hex
    arguments = {'path': CHILD_PATH, 'settings': {}, 'prompt': 'native-child-' + marker}
    gen.update_file(CHILD_PATH, 'id: native-probe\nname: Native probe\nsummary: D23 witness\nentrypoint: main.py\n')
    gen.update_file('draft/main.py', '# /// script\n# requires-python = ">=3.13"\n'
        '# dependencies = ["PyYAML==6.0.3"]\n# ///\nimport gen\nimport yaml\nasync def run():\n'
        f'    assert gen.prompt == {arguments["prompt"]!r}\n'
        f'    gen.update_file("native-child.txt", {marker!r})\n    await gen.commit()\n'
        f'    print("D23-CHILD " + {marker!r})\n    print("D23-DEPENDENCY " + yaml.__version__)\ngen.main(run)\n')
    cards = []
    bridge.stub_approvals(command_answer(bridge, arguments,
                                       'reject_once' if answer == 'reject' else 'allow_once', cards))
    original_prepare, original_bridge = backend.prepare, cli_container.leg_bridge
    prepared_values, opened, closed = [], [], []
    async def prepare(ctx, **kwargs):
        value = await original_prepare(ctx, **kwargs)
        prepared_values.append(value)
        return value
    @asynccontextmanager
    async def leg_bridge(**kwargs):
        async with original_bridge(**kwargs) as value:
            assert value is not None
            opened.append(value)
            try:
                yield value
            finally:
                closed.append(value)
    monkeypatch.setattr(backend, 'prepare', prepare)
    monkeypatch.setattr(cli_container, 'leg_bridge', leg_bridge)
    async def capture():
        await gen.commit()
        home = Path(harness_homes.harness_home(harness))
        before = set(home.rglob('*.jsonl'))
        ready = asyncio.Event()
        from _agent_shared import surface_runner
        runner = surface_runner('codex' if harness == 'codex' else 'qwen-code', 'terminal')
        leg = asyncio.create_task(runner({'backend': 'codex' if harness == 'codex' else 'qwen-code', 'provider': 'openrouter', 'auth': 'api_key',
            'model': model, 'commands': commands, 'websearch': False},
            pointer=gen.run_path('terminal/witness.terminal'),
            on_ready=lambda *args, **kwargs: ready.set()))
        waiter = asyncio.create_task(ready.wait())
        observed = {'native_records': [], 'native_ui': ''}
        try:
            await asyncio.wait({leg, waiter}, timeout=180, return_when=asyncio.FIRST_COMPLETED)
            if leg.done():
                leg.result()
            assert ready.is_set() and len(opened) == 1 and len(prepared_values) == 1
            prepared = prepared_values[0]
            key = os.environ['OPENROUTER_API_KEY']
            if any('API_KEY' in name or key in value for name, value in prepared.env.items()):
                raise AssertionError('Selected credential entered native process environment')
            private_config = home / ('config.toml' if harness == 'codex' else 'settings.json')
            assert stat.S_IMODE(private_config.stat().st_mode) == 0o600
            assert 'test_generator' in await tool_names(opened[0].url_host)
            term = next(iter(terms.values()))
            if harness == 'codex':
                await asyncio.to_thread(term.wait_for, b'Ask Codex', b'context left', b'To get started', timeout=60)
            else:
                _, blocked = await asyncio.to_thread(_up_to_prompt, harness, term)
                assert not blocked
            await asyncio.to_thread(_type_line, term,
                'Call the collimate test_generator tool exactly once with these full arguments: '
                + json.dumps(arguments) + '. Do not run a shell, delegate, write files yourself, '
                'or retry a refusal. After the tool returns, stop.')
            found = await collect_native_witness(harness, term, home, before,
                arguments, answer, commands, bridge, cards, marker, observed)
            assert found, 'No native result witness; inspect private capture, do not retry blindly'
            assert len(cards) == (1 if commands == 'ask' else 0), cards
            assert stat.S_IMODE(private_config.stat().st_mode) == 0o600
        finally:
            # Retain failures as well as captures, including native pending UI.
            try:
                codex_complete = harness == 'codex' and 'codex_native_validation' in observed
                native_complete = codex_complete or (harness == 'qwen-code' and 'qwen_native_validation' in observed)
                write_private_evidence(directory, harness, level,
                    {'capture_only': not native_complete, 'arguments': arguments, **observed, 'cards': cards,
                     'committed': bridge.read_cell_files(paths=['native-child.txt']),
                     'completed_turn_validation': 'passed' if native_complete
                     else 'awaiting actual native shape review'})
            finally:
                waiter.cancel()
                # Ending the terminal's client ends the leg.
                for term in terms.values():
                    await asyncio.to_thread(term.stop)
                await asyncio.wait_for(leg, timeout=180)
        assert closed == opened
        await dead(opened[0].url_host)
        assert not list(Path(bridge.workspace).glob('.collrun/forward-*'))
    try:
        run_on_test_loop(serving_cards(capture))
    finally:
        for term in terms.values():
            term.stop()
