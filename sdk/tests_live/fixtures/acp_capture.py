"""Test-only raw ACP observation; no normalized events or synthetic native proof."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path


class CaptureError(RuntimeError):
    """A private capture is incomplete, misattributed, or over its explicit bound."""


def _same_id(left, right):
    return type(left) is type(right) and left == right


def _cline_generator_title(arguments):
    """Pinned3.0.65 title V→NC→f: compact JSON,60 UTF16 units, literal '...'.

    Admit only empty settings or the exact seven string-valued app fields,
    with ASCII keys/values/path/prompt. This is the native JSON.stringify
    object/string representation; general JS numbers/objects are not guessed.
    """
    if (not isinstance(arguments, dict) or set(arguments) != {'path', 'settings', 'prompt'}
            or not isinstance(arguments['settings'], dict) or not isinstance(arguments['path'], str)
            or not isinstance(arguments['prompt'], str)):
        raise CaptureError('Cline title decoder requires the measured fixture input shape')
    settings = arguments['settings']
    app_keys = {'overlay', 'marker', 'parent_run_id', 'cell_id', 'expected_prompt',
                'commands', 'expected_mount'}
    if settings and set(settings) != app_keys:
        raise CaptureError('Cline title decoder requires exactly the reviewed app settings')
    if not all(isinstance(value, str) and value.isascii()
               for value in [arguments['path'], arguments['prompt'], *settings.values()]):
        raise CaptureError('Cline title decoder requires reviewed ASCII string inputs')
    text = json.dumps(arguments, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    encoded = text.encode('utf-16-le', errors='surrogatepass')
    if len(encoded) > 120:
        text = encoded[:114].decode('utf-16-le', errors='surrogatepass') + '...'
    return 'collimate__test_generator: ' + text


class Capture:
    def __init__(self, *, secrets=(), max_records=1024, max_bytes=16 * 1024 * 1024):
        if max_records < 1 or max_bytes < 1:
            raise ValueError('capture bounds must be positive')
        self.secrets = tuple(value for value in secrets if value)
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.records = []
        self.permission_records = []
        self.bytes = 0
        self.prompt_ids = []
        self.permission_ids = []

    def _scrub(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, '[selected-key-redacted]')
            return value
        if isinstance(value, list):
            return [self._scrub(item) for item in value]
        if isinstance(value, dict):
            return {self._scrub(key): self._scrub(item) for key, item in value.items()}
        return value

    def observe(self, direction, message):
        if not isinstance(message, dict):
            return
        method = message.get('method')
        record = None
        permission = False
        if direction == 'send' and method == 'session/prompt':
            identity = message.get('id')
            if isinstance(identity, bool) or not isinstance(identity, (int, str)):
                raise CaptureError('native prompt request has no exact JSON-RPC ID')
            if any(_same_id(identity, value) for value in self.prompt_ids):
                raise CaptureError('duplicate native prompt request ID')
            # Prompt text, attachments and startup/auth/config declarations are
            # outside this observer. Session identity alone correlates tools.
            params = message.get('params') or {}
            record = {'direction': direction, 'id': identity, 'method': method,
                      'sessionId': params.get('sessionId')}
        elif direction == 'receive' and method == 'session/update':
            params = message.get('params')
            update = params.get('update') if isinstance(params, dict) else None
            if isinstance(update, dict) and update.get('sessionUpdate') in (
                    'tool_call', 'tool_call_update'):
                record = {'direction': direction, 'message': deepcopy(message)}
        elif (direction == 'receive' and method is None
              and any(_same_id(message.get('id'), value) for value in self.prompt_ids)):
            record = {'direction': direction, 'message': deepcopy(message)}
        elif direction == 'receive' and method == 'session/request_permission':
            identity = message.get('id')
            if isinstance(identity, bool) or not isinstance(identity, (int, str)):
                raise CaptureError('native permission request has no exact JSON-RPC ID')
            if any(_same_id(identity, value) for value in self.permission_ids):
                raise CaptureError('duplicate native permission request ID')
            permission = True
            record = {'direction': direction, 'message': deepcopy(message),
                      'after_record_count': len(self.records)}
        elif (direction == 'send' and method is None
              and any(_same_id(message.get('id'), value) for value in self.permission_ids)):
            permission = True
            record = {'direction': direction, 'message': deepcopy(message),
                      'after_record_count': len(self.records)}
        if record is None:
            return
        # Copy before downstream library mutation and preserve complete payloads.
        # Only selected credential strings change in this private evidence copy.
        record = self._scrub(deepcopy(record))
        size = len(json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode())
        if (len(self.records) + len(self.permission_records) >= self.max_records
                or self.bytes + size > self.max_bytes):
            raise CaptureError('raw ACP capture exceeded its record/byte bound')
        # Permission traffic is a separate evidence channel. Legacy records
        # and all decoders retain their exact format and semantics. The index
        # locates this wire message between unchanged legacy records; list
        # order preserves multiple requests/replies at the same boundary.
        (self.permission_records if permission else self.records).append(record)
        self.bytes += size
        if direction == 'send' and method == 'session/prompt':
            self.prompt_ids.append(message['id'])
        elif permission and direction == 'receive':
            self.permission_ids.append(message['id'])

    def completed_turn(self, *, expected_input=None):
        """Correlate raw records without guessing a backend's result encoding."""
        requests = [row for row in self.records if row['direction'] == 'send']
        if len(requests) != 1 or not requests[0].get('sessionId'):
            raise CaptureError('capture requires exactly one native prompt/session')
        request = requests[0]
        updates, responses = [], []
        prompt_sent = False
        prompt_ended = False
        for record in self.records:
            if record['direction'] == 'send':
                prompt_sent = True
                continue
            message = record.get('message', {})
            if message.get('method') == 'session/update':
                if not prompt_sent or prompt_ended:
                    raise CaptureError('native tool arrived outside the captured prompt')
                params = message.get('params', {})
                if params.get('sessionId') != request['sessionId']:
                    raise CaptureError('tool belongs to another native session')
                updates.append(params['update'])
            elif record['direction'] == 'receive':
                if not _same_id(message.get('id'), request['id']):
                    raise CaptureError('response belongs to another native prompt')
                responses.append(message)
                prompt_ended = True
        if (len(responses) != 1 or 'error' in responses[0]
                or responses[0].get('result', {}).get('stopReason') != 'end_turn'):
            raise CaptureError('native prompt has no unique normal completed response')
        ids = {row.get('toolCallId') for row in updates}
        if len(ids) != 1 or not all(isinstance(value, str) and value for value in ids):
            raise CaptureError('capture requires exactly one identified native tool')
        initial = [row for row in updates if row.get('sessionUpdate') == 'tool_call']
        if len(initial) != 1 or not initial[0].get('title'):
            raise CaptureError('native tool has no unique initial name')
        arguments = [row['rawInput'] for row in updates if row.get('rawInput') is not None]
        if not arguments:
            raise CaptureError('native full arguments are absent')
        # Native streaming updates may evolve their rawInput. Preserve all of
        # them, and compare the last supplied arguments to the requested input.
        if expected_input is not None and arguments[-1] != expected_input:
            raise CaptureError('native final arguments differ from the requested input')
        finals = [row for row in updates if row.get('status') in ('completed', 'failed')]
        if (not finals or finals[-1].get('status') != 'completed'
                or not any(row.get('rawOutput') is not None or row.get('content')
                           for row in finals)):
            raise CaptureError('native full completed result is absent')
        # A final title may differ (native descriptive result titles); preserve
        # raw records and never infer another backend's tool name from it.
        return {'prompt_id': request['id'], 'session_id': request['sessionId'],
                'tool_call_id': next(iter(ids)), 'title': initial[0]['title'],
                'input': arguments[-1], 'final_updates': finals,
                'prompt_response': responses[0], 'records': self.records}

    def completed_generator_value(self, backend, *, expected_input):
        """Decode complete native values without a fixture-specific success claim."""
        if backend == 'claude-code':
            return self._completed_claude_generator_value(expected_input=expected_input)
        if backend == 'qwen-code':
            return self._completed_qwen_generator_value(expected_input=expected_input)
        if backend not in ('pi', 'opencode', 'cline', 'codex'):
            raise CaptureError('unsupported native generator result encoding')
        native_input = ({'server': 'collimate', 'tool': 'test_generator',
                         'arguments': expected_input} if backend == 'codex' else expected_input)
        proof = self.completed_turn(expected_input=native_input)
        updates = [row['message']['params']['update'] for row in self.records
                   if row.get('message', {}).get('method') == 'session/update']
        if backend == 'cline':
            order = [(row.get('sessionUpdate'), row.get('status')) for row in updates]
            if order != [('tool_call_update', 'pending'), ('tool_call_update', 'in_progress'),
                         ('tool_call', 'pending'), ('tool_call_update', 'completed')]:
                raise CaptureError('native Cline generator update order differs from its witness')
            if updates[0].get('rawInput') != expected_input:
                raise CaptureError('native Cline permission input differs from requested arguments')
            if any(row.get('title') != _cline_generator_title(row.get('rawInput'))
                   for row in (updates[0], updates[2])):
                raise CaptureError('native Cline permission/call title is absent or differs')
            for row in updates:
                if ('title' in row and row['title'] != _cline_generator_title(row.get('rawInput'))):
                    raise CaptureError('native Cline tool title differs from its exact input rendering')
                if 'rawInput' in row and row['rawInput'] != expected_input:
                    raise CaptureError('native Cline full arguments differ between updates')
        elif backend == 'codex':
            order = [(row.get('sessionUpdate'), row.get('status')) for row in updates]
            measured = [('tool_call', 'in_progress'), ('tool_call_update', 'completed')]
            with_progress = [measured[0], ('tool_call_update', 'in_progress'), measured[1]]
            if order not in (measured, with_progress):
                raise CaptureError('native Codex ACP generator order differs from its witness')
            if (order == with_progress and set(updates[1]) != {
                    'sessionUpdate', 'toolCallId', 'status'}):
                raise CaptureError('native Codex ACP status progress hides additional fields')
            initial = updates[0]
            if (initial.get('title') != 'mcp.collimate.test_generator'
                    or initial.get('kind') != 'execute'
                    or not isinstance(initial.get('_meta'), dict)
                    or initial['_meta'].get('is_mcp_tool_call') is not True
                    or 'title' in updates[-1]):
                raise CaptureError('native Codex ACP MCP identity differs from its witness')
            if any(row.get('rawInput') != native_input for row in (updates[0], updates[-1])):
                raise CaptureError('native Codex ACP full wrapper arguments differ')
            # Generic proof retains native wrapper semantics. Only this strict
            # backend proof exposes semantic tool input under input, with the
            # actual wrapper separately named. Raw records remain untouched.
            proof = {**proof, 'native_input': proof['input'],
                     'input': proof['input']['arguments']}
        else:
            if updates[0].get('sessionUpdate') != 'tool_call':
                raise CaptureError('native generator update precedes its initial call')
            if any(row.get('title') != 'collimate_test_generator'
                   for row in updates if 'title' in row):
                raise CaptureError('native tool name differs from the measured MCP tool')
        finals = proof['final_updates']
        if len(finals) != 1 or updates[-1] is not finals[0]:
            raise CaptureError('native generator requires a sole final completed update')
        final = finals[0]

        def text_value(blocks, *, acp=False):
            if not isinstance(blocks, list) or len(blocks) != 1:
                raise CaptureError('native result requires one complete text block')
            block = blocks[0]
            if acp:
                if not isinstance(block, dict) or block.get('type') != 'content':
                    raise CaptureError('native ACP result content is absent')
                block = block.get('content')
            if (not isinstance(block, dict) or block.get('type') != 'text'
                    or not isinstance(block.get('text'), str)):
                raise CaptureError('native result text is absent')
            return json.loads(block['text'])

        content = (text_value(final.get('content'), acp=True)
                   if backend in ('pi', 'opencode') else None)
        raw = final.get('rawOutput')
        if not isinstance(raw, dict):
            raise CaptureError('native full raw result is absent')
        if backend == 'pi':
            details = raw.get('details')
            if not isinstance(details, dict) or not isinstance(details.get('result'), dict):
                raise CaptureError('native Pi structured result is absent')
            value = details['result']
            if text_value(raw.get('content')) != value:
                raise CaptureError('native Pi result representations disagree')
        elif backend == 'opencode':
            metadata = raw.get('metadata')
            if (not isinstance(metadata, dict) or metadata.get('truncated') is not False
                    or not isinstance(raw.get('output'), str)):
                raise CaptureError('native OpenCode full untruncated result is absent')
            value = json.loads(raw['output'])
        elif backend == 'codex':
            result = raw.get('result')
            if (set(raw) != {'result', 'error'} or raw['error'] is not None
                    or not isinstance(result, dict)
                    or set(result) != {'content', 'structuredContent', '_meta'}
                    or result['_meta'] is not None or 'content' in final):
                raise CaptureError('native Codex ACP complete non-error result is absent')
            structured = result['structuredContent']
            if (not isinstance(structured, dict) or set(structured) != {'result'}
                    or not isinstance(structured['result'], dict)):
                raise CaptureError('native Codex ACP structured result is absent')
            value = structured['result']
            if (json.dumps(text_value(result['content']), sort_keys=True, ensure_ascii=False)
                    != json.dumps(value, sort_keys=True, ensure_ascii=False)):
                raise CaptureError('native Codex ACP result representations disagree')
        else:
            structured = raw.get('structuredContent')
            if (raw.get('isError') is not False or not isinstance(structured, dict)
                    or set(structured) != {'result'} or not isinstance(structured['result'], dict)):
                raise CaptureError('native Cline structured non-error result is absent')
            value = structured['result']
            if text_value(raw.get('content')) != value:
                raise CaptureError('native Cline result representations disagree')
            # The actual ACP final has rawOutput only, no separate content.
            # If additional content is supplied it must never disagree.
            if 'content' in final and text_value(final['content'], acp=True) != value:
                raise CaptureError('native Cline additional ACP result disagrees')
        if backend in ('pi', 'opencode') and content != value:
            raise CaptureError('native ACP and raw result representations disagree')
        return proof, value

    def _completed_claude_generator_value(self, *, expected_input):
        """Pinned2.1.281/0.81.1 measured SDK stream and native permission pair."""
        proof = self.completed_turn(expected_input=expected_input)

        def same(actual, expected):
            try:
                return json.dumps(actual, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
                    expected, sort_keys=True, ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError) as error:
                raise CaptureError('Claude complete finite JSON is required') from error

        if (len(self.records) < 6 or not isinstance(expected_input, dict)
                or set(expected_input) != {'path', 'settings', 'prompt'}
                or not isinstance(expected_input['path'], str)
                or not isinstance(expected_input['settings'], dict)
                or not isinstance(expected_input['prompt'], str)
                or not isinstance(proof['session_id'], str)):
            raise CaptureError('Claude measured full input and stream inventory differs')
        request = self.records[0]
        if (set(request) != {'direction', 'id', 'method', 'sessionId'}
                or type(request['id']) is not int or request['id'] < 0):
            raise CaptureError('Claude own prompt request differs')
        identity, session = proof['tool_call_id'], proof['session_id']
        suffix = identity.removeprefix('toolu_')
        if not identity.startswith('toolu_') or len(suffix) != 24 or not suffix.isascii() or not suffix.isalnum():
            raise CaptureError('Claude measured native tool identity differs')
        updates = []
        for row in self.records[1:-1]:
            message = row.get('message', {})
            if (set(row) != {'direction', 'message'} or row['direction'] != 'receive'
                    or set(message) != {'jsonrpc', 'method', 'params'} or message['jsonrpc'] != '2.0'
                    or message['method'] != 'session/update'
                    or set(message['params']) != {'sessionId', 'update'}
                    or message['params']['sessionId'] != session):
                raise CaptureError('Claude complete native update envelope differs')
            updates.append(message['params']['update'])
        name = 'mcp__collimate__test_generator'
        meta = {'claudeCode': {'toolName': name}}
        common = {'_meta': meta, 'toolCallId': identity, 'sessionUpdate': 'tool_call_update'}
        initial = {**common, 'sessionUpdate': 'tool_call', 'name': name, 'title': name,
                   'kind': 'other', 'rawInput': {}, 'status': 'pending', 'content': []}
        full = {**common, 'rawInput': expected_input, 'title': name, 'kind': 'other', 'content': []}
        if not same(updates[0], initial) or not same(updates[-3], full):
            raise CaptureError('Claude exact initial and full native input differs')
        previous = set()
        for partial in updates[1:-3]:
            arguments = partial.get('rawInput')
            if (not isinstance(arguments, dict) or not previous < set(arguments) < set(expected_input)
                    or not same(arguments, {key: expected_input[key] for key in arguments})
                    or not same(partial, {**common, 'rawInput': arguments, 'title': name, 'kind': 'other'})):
                raise CaptureError('Claude exact monotone native input prefix differs')
            previous = set(arguments)
        response_meta, final = updates[-2:]
        permission_position = len(self.records) - 3
        if (set(final) != {'_meta', 'toolCallId', 'sessionUpdate', 'status', 'rawOutput', 'content'}
                or not isinstance(final.get('rawOutput'), str)):
            raise CaptureError('Claude complete raw native result is absent')
        raw = final['rawOutput']
        expected_final = {**common, 'status': 'completed', 'rawOutput': raw,
                          'content': [{'type': 'content', 'content': {'type': 'text', 'text': raw}}]}
        expected_response_meta = {**common, '_meta': {'claudeCode': {'toolName': name, 'toolResponse': raw}}}
        if not same(final, expected_final) or not same(response_meta, expected_response_meta):
            raise CaptureError('Claude complete metadata/raw/text result representations disagree')

        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError('duplicate JSON key')
                result[key] = value
            return result

        def nonfinite(value):
            raise ValueError('nonfinite JSON constant')

        try:
            wrapped = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
        except (ValueError, TypeError) as error:
            raise CaptureError('Claude complete finite result JSON is absent') from error
        if not isinstance(wrapped, dict) or set(wrapped) != {'result'}:
            raise CaptureError('Claude measured full result wrapper differs')
        value = wrapped['result']
        child = (isinstance(value, dict) and set(value) == {'exit_code', 'output', 'result'}
                 and type(value['exit_code']) is int and isinstance(value['output'], str)
                 and isinstance(value['result'], dict)
                 and set(value['result']) in ({'success', 'error_message'}, {'success', 'error_message', 'refused'})
                 and type(value['result']['success']) is bool
                 and isinstance(value['result']['error_message'], str)
                 and ('refused' not in value['result'] or (
                     value['exit_code'] == 0 and value['result']['success'] is True
                     and value['result']['error_message'] == ''
                     and same(value['result']['refused'], {'op': 'surface_register', 'reason': 'forward_refused'}))))
        refused = value == {'error': 'approval_refused', 'message':
            'REFUSED: the user did not approve this command (or no one was there to approve it) '
            '— do not retry it unchanged; ask the user or take another approach.'}
        if not (child or refused):
            raise CaptureError('Claude full measured child or refusal result shape differs')
        if len(self.permission_records) != 2:
            raise CaptureError('Claude measured native permission pair is absent')
        asked, answered = self.permission_records
        permission_id = asked.get('message', {}).get('id')
        if type(permission_id) is not int or permission_id < 0:
            raise CaptureError('Claude native permission ID differs')
        tool = {**initial, 'rawInput': expected_input,
                '_meta': {'claudeCode': {'toolName': name,
                                       'mcpServer': {'name': 'collimate', 'source': 'dynamic'}}}}
        tool.pop('sessionUpdate')
        expected_asked = {'direction': 'receive', 'after_record_count': permission_position, 'message': {
            'jsonrpc': '2.0', 'id': permission_id, 'method': 'session/request_permission',
            'params': {'sessionId': session, 'toolCall': tool,
                '_meta': {'permission': {'title': name, 'version': 1}},
                'options': [{'kind': 'allow_once', 'name': 'Yes', 'optionId': 'allow-once'},
                    {'kind': 'allow_always', 'name': "Yes, and don't ask again for Test Generator commands",
                     'optionId': 'allow-with-updates'},
                    {'kind': 'reject_once', 'name': 'No', 'optionId': 'reject'}]}}}
        expected_answered = {'direction': 'send', 'after_record_count': permission_position, 'message': {
            'jsonrpc': '2.0', 'id': permission_id,
            'result': {'outcome': {'outcome': 'selected', 'optionId': 'allow-once'}}}}
        if not same(asked, expected_asked) or not same(answered, expected_answered):
            raise CaptureError('Claude own complete native permission exchange differs')
        response = self.records[-1]
        message = response.get('message', {})
        result = message.get('result', {})
        usage = result.get('usage', {})
        usage_keys = {'cachedReadTokens', 'cachedWriteTokens', 'inputTokens', 'outputTokens', 'totalTokens'}
        if (not isinstance(usage, dict) or set(usage) != usage_keys
                or not all(type(number) is int and number >= 0 for number in usage.values())
                or usage['totalTokens'] != sum(number for key, number in usage.items() if key != 'totalTokens')):
            raise CaptureError('Claude measured full completion usage differs')
        counts = {'cachedInputTokens': usage['cachedReadTokens'], 'cachedWriteTokens': usage['cachedWriteTokens'],
                  'inputTokens': usage['inputTokens'], 'outputTokens': usage['outputTokens'],
                  'reasoningOutputTokens': 0, 'totalTokens': usage['totalTokens']}
        # Native quota has two scopes: top-level usage is the main loop;
        # model_usage also counts native internal calls/sidechains. Its own
        # typed total is authoritative, not equality with the main-loop tally.
        models = result.get('_meta', {}).get('quota', {}).get('model_usage')
        if not isinstance(models, list) or len(models) != 1 or not isinstance(models[0], dict):
            raise CaptureError('Claude measured model usage row differs')
        model_counts = models[0].get('token_count')
        if (not isinstance(model_counts, dict) or set(model_counts) != set(counts)
                or not all(type(number) is int and number >= 0 for number in model_counts.values())
                or model_counts['reasoningOutputTokens'] != 0
                or model_counts['totalTokens'] != sum(model_counts[key] for key in (
                    'inputTokens', 'outputTokens', 'cachedInputTokens', 'cachedWriteTokens'))):
            raise CaptureError('Claude measured model usage tally differs')
        expected_response = {'direction': 'receive', 'message': {'jsonrpc': '2.0', 'id': request['id'],
            'result': {'stopReason': 'end_turn', 'usage': usage, '_meta': {'quota': {
                'token_count': counts, 'model_usage': [{'model': 'claude-haiku-4-5-20251001',
                                                     'token_count': model_counts}]}}}}}
        if not same(response, expected_response):
            raise CaptureError('Claude measured own normal full prompt completion differs')
        return {**proof, 'native_input': expected_input, 'native_permission': self.permission_records,
                'ancillary_calls': []}, value

    def _completed_qwen_generator_value(self, *, expected_input):
        """Pinned0.23.3 witness: one target, optional failed resource discovery.

        The ancillary call is explicitly accounted for, never treated as a
        test_generator call or discarded from the returned native evidence.
        Generic completed_turn keeps its exactly-one-native-ID contract.
        """
        app_keys = {'overlay', 'marker', 'parent_run_id', 'cell_id', 'expected_prompt',
                    'commands', 'expected_mount'}
        if (not isinstance(expected_input, dict)
                or set(expected_input) != {'path', 'settings', 'prompt'}
                or not isinstance(expected_input['settings'], dict)
                or (expected_input['settings'] and set(expected_input['settings']) != app_keys)
                or not all(isinstance(value, str) and value.isascii() for value in
                           [expected_input['path'], expected_input['prompt'],
                            *expected_input['settings'].values()])):
            raise CaptureError('Qwen title decoder requires reviewed ASCII string inputs')
        if len(self.records) == 4:
            return self._completed_qwen_permission_generator_value(expected_input=expected_input)
        if self.permission_records:
            raise CaptureError('native Qwen progress capture has unexplained permission traffic')
        if len(self.records) not in (5, 8):
            raise CaptureError('native Qwen requires one target and only its measured ancillary call')
        request, response = self.records[0], self.records[-1]
        if (request.get('direction') != 'send' or request.get('method') != 'session/prompt'
                or response.get('direction') != 'receive'):
            raise CaptureError('native Qwen tools arrived outside the captured prompt')
        updates = []
        for record in self.records[1:-1]:
            message = record.get('message', {})
            params = message.get('params', {})
            if (record.get('direction') != 'receive'
                    or message.get('method') != 'session/update'
                    or params.get('sessionId') != request.get('sessionId')
                    or not isinstance(params.get('update'), dict)):
                raise CaptureError('native Qwen update belongs to another prompt/session')
            updates.append(params['update'])
        target = updates[-3:]
        target_id = target[0].get('toolCallId')
        if (not isinstance(target_id, str) or not target_id
                or any(row.get('toolCallId') != target_id for row in target)):
            raise CaptureError('native Qwen target IDs differ or are absent')
        start_keys = {'sessionUpdate', 'toolCallId', 'status', 'title', 'content',
                      'locations', 'kind', 'rawInput', '_meta'}
        final_keys = {'sessionUpdate', 'toolCallId', 'status', 'content', '_meta'}
        title = 'test_generator (collimate MCP Server)'
        meta = {'toolName': 'mcp__collimate__test_generator', 'provenance': 'mcp',
                'serverId': 'collimate'}
        native_input = target[1].get('rawInput')
        if native_input != expected_input:
            raise CaptureError('native Qwen full arguments differ from requested input')
        # JSON.stringify uses the captured object's order. Semantic argument
        # equality stays exact even when its property order differs from ours.
        compact = json.dumps(native_input, ensure_ascii=False, separators=(',', ':'),
                             allow_nan=False)

        def require_start(row, *, call_id, name, kind, arguments, metadata, preparing):
            expected = {'sessionUpdate': 'tool_call' if preparing else 'tool_call_update',
                        'toolCallId': call_id, 'status': 'pending' if preparing else 'in_progress',
                        'title': name, 'content': [], 'locations': [], 'kind': kind,
                        'rawInput': arguments, '_meta': metadata}
            if set(row) != start_keys or row != expected:
                raise CaptureError('native Qwen call identity/full arguments/order differ')

        require_start(target[0], call_id=target_id, name=title, kind='other', arguments={},
                      metadata={**meta, 'phase': 'preparing'}, preparing=True)
        require_start(target[1], call_id=target_id, name=title + ': ' + compact, kind='other',
                      arguments=expected_input, metadata=meta, preparing=False)
        final = target[-1]
        raw = final.get('rawOutput')
        if (set(final) != final_keys | {'rawOutput'} or final.get('sessionUpdate') != 'tool_call_update'
                or final.get('status') != 'completed' or final.get('_meta') != meta
                or not isinstance(raw, str)
                or final.get('content') != [{'type': 'content',
                                             'content': {'type': 'text', 'text': raw}}]):
            raise CaptureError('native Qwen complete raw/ACP result representations disagree')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise CaptureError('native Qwen generator result is not a complete object')
        ancillary = []
        if len(updates) == 6:
            resource = updates[:3]
            resource_id = resource[0].get('toolCallId')
            if not isinstance(resource_id, str) or not resource_id or resource_id == target_id:
                raise CaptureError('native Qwen ancillary resource ID is absent or aliases target')
            resource_meta = {'toolName': 'read_mcp_resource', 'provenance': 'builtin'}
            resource_input = resource[1].get('rawInput')
            if (not isinstance(resource_input, dict)
                    or set(resource_input) != {'server_name', 'uri'}
                    or resource_input.get('server_name') != 'collimate'
                    or not isinstance(resource_input.get('uri'), str)
                    or not 1 <= len(resource_input['uri']) <= 4096
                    or not resource_input['uri'].isascii()):
                raise CaptureError('native Qwen ancillary resource input differs from its schema')
            # Pinned ReadMcpResource describes server_name:uri; its schema
            # bounds URI length at4096. The failed server read reports that
            # same URI verbatim. Correlate these fields, not literal variants.
            resource_uri = resource_input['uri']
            require_start(resource[0], call_id=resource_id, name='ReadMcpResource', kind='fetch',
                          arguments={}, metadata={**resource_meta, 'phase': 'preparing'}, preparing=True)
            require_start(resource[1], call_id=resource_id,
                          name='ReadMcpResource: collimate:' + resource_uri, kind='fetch',
                          arguments=resource_input, metadata=resource_meta, preparing=False)
            if resource[2] != {'sessionUpdate': 'tool_call_update', 'toolCallId': resource_id,
                               'status': 'failed', '_meta': resource_meta, 'content': [
                                   {'type': 'content', 'content': {'type': 'text',
                                    'text': 'Unknown resource: ' + resource_uri}}]}:
                raise CaptureError('native Qwen ancillary resource is not the measured failed lookup')
            ancillary.append({'tool_call_id': resource_id, 'title': 'ReadMcpResource',
                              'input': resource_input, 'updates': resource,
                              'classification': 'failed_test_generator_resource_lookup'})
        # Reuse correlation on the fully checked target, then return ALL records.
        selected = Capture()
        selected.records = [request, *self.records[-4:-1], response]
        proof = selected.completed_turn(expected_input=expected_input)
        return {**proof, 'records': self.records, 'ancillary_calls': ancillary}, value

    def _completed_qwen_permission_generator_value(self, *, expected_input):
        """Pinned granted permission supplies full input when progress is suppressed.

        Native grant and the inner application's value are separate facts.
        Return its complete JSON object; child success/refusal stays with the
        existing caller gates. Preserve both channels without synthetic updates.
        """
        if not all(isinstance(row, dict) for row in self.records):
            raise CaptureError('native Qwen permission records are not complete objects')
        request, initial_record, final_record, response_record = self.records
        identity = request.get('id')
        session = request.get('sessionId')
        if (set(request) != {'direction', 'id', 'method', 'sessionId'}
                or request.get('direction') != 'send' or request.get('method') != 'session/prompt'
                or isinstance(identity, bool) or not isinstance(identity, (int, str))
                or not isinstance(session, str) or not session):
            raise CaptureError('native Qwen permission turn has no exact prompt/session')
        updates = []
        for record in (initial_record, final_record):
            message = record.get('message', {})
            params = message.get('params', {}) if isinstance(message, dict) else None
            if (set(record) != {'direction', 'message'} or record.get('direction') != 'receive'
                    or not isinstance(message, dict) or not isinstance(params, dict)
                    or set(message) != {'jsonrpc', 'method', 'params'}
                    or message.get('jsonrpc') != '2.0' or message.get('method') != 'session/update'
                    or set(params) != {'sessionId', 'update'} or params.get('sessionId') != session
                    or not isinstance(params.get('update'), dict)):
                raise CaptureError('native Qwen permission tool belongs to another turn/session')
            updates.append(params['update'])
        initial, final = updates
        tool_id = initial.get('toolCallId')
        title = 'test_generator (collimate MCP Server)'
        meta = {'toolName': 'mcp__collimate__test_generator', 'provenance': 'mcp',
                'serverId': 'collimate'}
        if (not isinstance(tool_id, str) or not tool_id or initial != {
                'sessionUpdate': 'tool_call', 'toolCallId': tool_id, 'status': 'pending',
                'title': title, 'content': [], 'locations': [], 'kind': 'other', 'rawInput': {},
                '_meta': {**meta, 'phase': 'preparing'}}):
            raise CaptureError('native Qwen permission target identity differs')
        if not isinstance(self.permission_records, list) or len(self.permission_records) != 2:
            raise CaptureError('native Qwen requires exactly one full permission request/reply')
        permission, reply = self.permission_records
        for row, direction in ((permission, 'receive'), (reply, 'send')):
            if (not isinstance(row, dict) or set(row) != {'direction', 'message', 'after_record_count'}
                    or row.get('direction') != direction
                    or type(row.get('after_record_count')) is not int or row['after_record_count'] != 2
                    or not isinstance(row.get('message'), dict)):
                raise CaptureError('native Qwen permission request/reply ordering differs')
        message, answer = permission['message'], reply['message']
        permission_id = message.get('id')
        params = message.get('params')
        if (set(message) != {'jsonrpc', 'id', 'method', 'params'}
                or message.get('jsonrpc') != '2.0' or message.get('method') != 'session/request_permission'
                or isinstance(permission_id, bool) or not isinstance(permission_id, (int, str))
                or not isinstance(params, dict) or set(params) != {'sessionId', 'toolCall', 'options'}
                or params.get('sessionId') != session or not isinstance(params.get('toolCall'), dict)):
            raise CaptureError('native Qwen full permission request identity/session differs')
        tool = params['toolCall']
        native_input = tool.get('rawInput')
        if native_input != expected_input:
            raise CaptureError('native Qwen permission full arguments differ from requested input')
        # DiscoveredMCPToolInvocation.getDescription uses safeJsonStringify
        # of captured params (native property order), without a tool prefix.
        compact = json.dumps(native_input, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if tool != {'toolCallId': tool_id, 'status': 'pending', 'title': compact,
                    'content': [], 'locations': [], 'kind': 'other', 'rawInput': native_input,
                    '_meta': {'toolName': 'mcp__collimate__test_generator'}}:
            raise CaptureError('native Qwen permission tool/input/description differs')
        options = [
            {'optionId': 'proceed_always_project', 'name': 'Always Allow in project: test_generator', 'kind': 'allow_always'},
            {'optionId': 'proceed_always_user', 'name': 'Always Allow for user: test_generator', 'kind': 'allow_always'},
            {'optionId': 'proceed_once', 'name': 'Allow', 'kind': 'allow_once'},
            {'optionId': 'cancel', 'name': 'Reject', 'kind': 'reject_once'}]
        if (params['options'] != options or set(answer) != {'jsonrpc', 'id', 'result'}
                or answer.get('jsonrpc') != '2.0' or not _same_id(answer.get('id'), permission_id)
                or answer.get('result') != {'outcome': {'optionId': 'proceed_once', 'outcome': 'selected'}}):
            raise CaptureError('native Qwen offered options/once grant reply differ')
        raw = final.get('rawOutput')
        if (not isinstance(raw, str) or final != {'sessionUpdate': 'tool_call_update',
                'toolCallId': tool_id, 'status': 'completed', '_meta': meta, 'rawOutput': raw,
                'content': [{'type': 'content', 'content': {'type': 'text', 'text': raw}}]}):
            raise CaptureError('native Qwen permission raw/ACP completed result representations disagree')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise CaptureError('native Qwen permission result is not a complete object')
        response = response_record.get('message', {})
        if (set(response_record) != {'direction', 'message'} or response_record.get('direction') != 'receive'
                or not isinstance(response, dict)
                or set(response) != {'jsonrpc', 'id', 'result'} or response.get('jsonrpc') != '2.0'
                or not _same_id(response.get('id'), identity)
                or not isinstance(response.get('result'), dict)
                or not set(response['result']) <= {'stopReason', '_meta'}
                or response['result'].get('stopReason') != 'end_turn'
                or ('_meta' in response['result'] and not isinstance(response['result']['_meta'], dict))):
            raise CaptureError('native Qwen permission prompt has no exact normal completion')
        return {'prompt_id': identity, 'session_id': session, 'tool_call_id': tool_id,
                'title': title, 'input': native_input, 'input_source': 'native_permission_request',
                'final_updates': [final], 'prompt_response': response, 'records': self.records,
                'permission_records': self.permission_records, 'ancillary_calls': []}, value

    def completed_generator_result(self, backend, *, expected_input, marker):
        """Require the measured native value to prove this fixture's child success."""
        if not isinstance(marker, str) or not marker:
            raise CaptureError('a fresh child output marker is required')
        proof, value = self.completed_generator_value(backend, expected_input=expected_input)
        if (not isinstance(value, dict) or set(value) != {'exit_code', 'output', 'result'}
                or type(value['exit_code']) is not int or value['exit_code'] != 0
                or not isinstance(value['output'], str)
                or marker not in value['output'].splitlines()
                or not isinstance(value['result'], dict)
                or value['result'].get('success') is not True
                or value['result'].get('error_message') != ''):
            raise CaptureError('native child result does not prove successful execution')
        return proof, value

    def write_private(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(self.records, ensure_ascii=False, indent=2).encode()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(body)


class ObservedTransport:
    def __init__(self, original, capture):
        self.original = original
        self.capture = capture
        self.closed = False

    async def send(self, message):
        self.capture.observe('send', message)
        return await self.original.send(message)

    async def receive(self):
        message = await self.original.receive()
        self.capture.observe('receive', message)
        return message

    async def close(self):
        if not self.closed:
            self.closed = True
            return await self.original.close()


def observed_profile(profile, capture):
    """Reuse the actual prepared process, kill function and transport lifetime."""
    async def spawn():
        process = await profile.spawn()
        return replace(process, transport=ObservedTransport(process.transport, capture))
    return replace(profile, spawn=spawn)
