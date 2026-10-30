"""Actual Claude TTY command policy, full native results and private file
carrier — the TTY on this machine (its CLI surface's locus), whose commands
level rides the CLI's own settings: ask (its prompt asks before each
command) and allow. deny never reaches it: placement refuses it."""
import asyncio
import json
import os
import re
import stat
import time
from pathlib import Path

import pytest

from tests_live.conftest import has_key, requires_claude_code

pytestmark = [pytest.mark.live, requires_claude_code,
    pytest.mark.usefixtures('one_loop_per_test'),
    pytest.mark.skipif(not has_key('anthropic'), reason='needs selected Anthropic API account')]
COMMAND = "sha256sum commands-probe.txt | cut -d ' ' -f1 | tee commands-probe.ran"
PROMPT = ('Use your Bash tool to run exactly this command: ' + COMMAND
    + '. If permission is required, wait for the human. Do not use another tool or command. '
      "After it runs, reply with only the command's output. If you cannot run it, reply NO-SHELL.")
MODEL = 'claude-haiku-4-5-20251001'


def terminal_query_only(data):
    """Only the responses already sent by the shared _HostTerminal pump."""
    assert data in (b'\x1b[?62;22c', b'\x1b[?0u', b'\x1b[1;1R') or re.fullmatch(
        rb'\x1b\[\?[0-9]+;2\$y', data), 'Unexpected terminal input'


def message_parts(row):
    content = row.get('message', {}).get('content', [])
    return [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []


def native_rows(value, sid, workspace):
    files = value['native_session_files']
    if not files:
        return []
    assert len(files) == 1
    raw = files[0]['full_jsonl']
    if not raw.endswith('\n'):
        return []
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    for row in rows:
        assert isinstance(row, dict)
        for field in ('sessionId', 'session_id'):
            if field in row:
                assert row[field] == sid
        if 'cwd' in row:
            assert row['cwd'] == workspace
    prompts = [row for row in rows if row.get('type') == 'user'
               and row.get('message', {}).get('content') == PROMPT]
    assert len(prompts) <= 1
    return rows if prompts else []


REFUSAL = ("The user doesn't want to proceed with this tool use. The tool use was rejected "
    "(eg. if it was a file edit, the new_string was NOT written to the file). "
    "STOP what you are doing and wait for the user to tell you how to proceed.")


def native_outcome(value, workspace, level):
    """Pinned actual native branches; user rejection is explicitly interrupted."""
    rows = native_rows(value, value['session_id'], workspace)
    messages = [(i, row) for i, row in enumerate(rows) if row.get('type') in ('user', 'assistant')]
    if not messages:
        return ''
    for _, row in messages:
        assert row.get('isSidechain') is False
        assert row.get('sessionId') == value['session_id'] and row.get('cwd') == workspace
        assert row.get('message', {}).get('role') == row['type']
        if row['type'] == 'assistant':
            assert row['message'].get('model') == MODEL
    calls = [(i, row, part) for i, row in messages for part in message_parts(row)
             if part.get('type') == 'tool_use']
    results = [(i, row, part) for i, row in messages for part in message_parts(row)
               if part.get('type') == 'tool_result']
    texts = [(i, row, part) for i, row in messages for part in message_parts(row)
             if row['type'] == 'assistant' and part.get('type') == 'text']
    finals = [entry for entry in texts if entry[1]['message'].get('stop_reason') == 'end_turn']
    answers = [event for event in value['input_events'] if event['kind'] == 'native-panel-answer']
    for _, row in messages:
        if row['type'] == 'user':
            content = row['message'].get('content')
            if (content != PROMPT and not (isinstance(content, list) and len(content) == 1
                    and content[0].get('type') in ('tool_result', 'text'))):
                return ''
            if (isinstance(content, list) and content[0].get('type') == 'text'
                    and content != [{'type': 'text', 'text': '[Request interrupted by user for tool use]'}]):
                return ''
    marker = value['marker']
    digest = value['probe_sha256']
    if len(calls) != 1 or len(results) != 1:
        return ''
    ci, call_row, call = calls[0]
    ri, result_row, result = results[0]
    argument = call.get('input')
    if (call_row['type'] != 'assistant' or result_row['type'] != 'user' or ri <= ci
            or call.get('name') != 'Bash' or not isinstance(call.get('id'), str) or not call['id']
            or not isinstance(argument, dict) or set(argument) != {'command', 'description'}
            or argument['command'] != COMMAND or not isinstance(argument['description'], str)
            or not argument['description'] or call_row['message'].get('stop_reason') != 'tool_use'
            or result.get('tool_use_id') != call['id'] or result_row.get('parentUuid') != call_row.get('uuid')):
        return ''
    if level.startswith('ask-'):
        expected = {'kind': 'native-panel-answer', 'case': level,
                    'hex': '320d' if level == 'ask-reject' else '310d'}
        if answers != [expected] or value.get('answered_native_tool_id') != call['id']:
            return ''
    elif level != 'allow' or answers:
        return ''
    if level == 'ask-reject':
        interrupts = [(i, row) for i, row in messages if row['type'] == 'user'
            and row['message'].get('content') == [
                {'type': 'text', 'text': '[Request interrupted by user for tool use]'}]]
        if (result.get('is_error') is not True or result != {'type': 'tool_result', 'content': REFUSAL, 'is_error': True,
                      'tool_use_id': call['id']}
                or result_row.get('toolUseResult') != 'User rejected tool use'
                or marker['exists'] or texts or len(interrupts) != 1 or interrupts[0][0] <= ri
                or any(row['message'].get('stop_reason') == 'end_turn' for _, row in messages)):
            return ''
        return 'native_user_rejected_interrupted'
    expected = {'stdout': digest, 'stderr': '', 'interrupted': False,
                'isImage': False, 'noOutputExpected': False}
    mirror = result_row.get('toolUseResult')
    if (result.get('is_error') is not False or result != {'tool_use_id': call['id'], 'type': 'tool_result',
                   'content': digest, 'is_error': False}
            or not isinstance(mirror, dict) or set(mirror) != set(expected)
            or any(type(mirror[k]) is not type(v) or mirror[k] != v for k, v in expected.items())
            or marker != {'exists': True, 'full_text': digest + '\n'}
            or len(texts) != 1 or len(finals) != 1 or finals[0][0] <= ri
            or finals[0][2] != {'type': 'text', 'text': digest}):
        return ''
    return 'native_normal_command_completed'


@pytest.mark.parametrize('level', ['ask-reject', 'ask-allow', 'allow'])
def test_actual_claude_tty_commands_and_private_file_carrier(native_bridge, monkeypatch, tmp_path, level):
    import gen
    from _agent_shared import acp_run, harness_homes, surface_runner
    from _agent_shared.cli_host import account_home
    from tests_live import _host_terminal as transport
    from tests_live import _claude_tty as witness
    from tests_live.test_live_acp_conformance import _claude_live_host, _write_probe

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    found = _claude_live_host(native_bridge)
    native_bridge.packages = dict(witness.PINS)
    grant = 'ask' if level.startswith('ask-') else level
    native_bridge.permissions = {'docker': 'none', 'internet': 'full', 'commands': grant}
    account = witness.model_account('anthropic', model=MODEL, harness='claude-code')
    account['id'] = 'live-claude-tty-' + level
    witness.bind_model_account(account)
    digest = _write_probe()
    selected = os.environ['ANTHROPIC_API_KEY']
    home = Path(harness_homes.harness_home('claude-code'))
    config_home = account_home('claude-code', account)
    workspace = gen.workspace_path()
    cfg = {'backend': 'claude-code', 'auth': 'api_key', 'provider': 'anthropic',
           'model': MODEL, 'commands': grant, 'websearch': False, 'host_harness': found}
    input_events, prepared, launch = [], [], []
    errors = []
    state = {'authority': 'NATIVE_TTY_GATE_PENDING', 'native_outcome': '',
             'model': MODEL, 'commands': grant, 'case': level, 'command': COMMAND, 'prompt': PROMPT,
             'probe_sha256': digest, 'session_id': '', 'input_events': input_events}
    original_send = transport._HostTerminal.send

    answer = b'1\r' if level == 'ask-allow' else b'2\r'
    answer_armed = [False]

    def send(self, data):
        if data == answer and answer_armed[0]:
            answer_armed[0] = False
            input_events.append({'kind': 'native-panel-answer', 'case': level, 'hex': data.hex()})
        else:
            terminal_query_only(data)
            input_events.append({'kind': 'terminal-query-response', 'hex': data.hex()})
        return original_send(self, data)

    monkeypatch.setattr(transport._HostTerminal, 'send', send)
    terms = transport._host_terminals(native_bridge, monkeypatch)
    original_start = native_bridge.start_host_terminal
    original_prepare = witness.claude.prepare

    def start_host_terminal(**values):
        argv = values['command']
        assert argv[0] == found['path'] and argv[-2:] == ['--', PROMPT]
        assert '-p' not in argv and '--print' not in argv
        assert ('--dangerously-skip-permissions' in argv) == (level == 'allow')
        assert not {'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'} & values['env'].keys()
        assert values['env']['CLAUDE_CONFIG_DIR'] == config_home
        if selected in json.dumps(values, default=str):
            raise AssertionError('Selected key reached terminal launch')
        launch.append({'argv': argv, 'cwd': values['cwd'], 'env_names': sorted(values['env'])})
        return original_start(**values)

    async def prepare(*args, **kwargs):
        result = await original_prepare(*args, **kwargs)
        if kwargs.get('leg') == 'terminal':
            prepared.append(result)
        return result

    monkeypatch.setattr(native_bridge, 'start_host_terminal', start_host_terminal)
    monkeypatch.setattr(witness.claude, 'prepare', prepare)

    def snapshot(label, *, retain=True):
        value = {**state, 'phase': label, 'launch': launch, 'native_session_files': []}
        for name in witness.claude.claude_session_paths(str(home), workspace, state['session_id']):
            path = Path(name)
            if not path.exists():
                continue
            assert not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)
            assert path.stat().st_size <= witness.MAX_TRANSCRIPT
            value['native_session_files'].append({'relative_path': str(path.relative_to(home)),
                                                  'full_jsonl': witness.claude.pull_claude_transcript(
                    state['session_id'], store_dir=str(home), project=workspace)[1]})
        if terms:
            term = next(iter(terms.values()))
            with term._lock:
                raw = bytes(term._out)
            assert len(raw) <= witness.MAX_TRANSCRIPT
            value.update(raw_pty_latin1=raw.decode('latin1'), client_exit=term.proc.poll())
        marker = Path(workspace) / 'commands-probe.ran'
        value['marker'] = {'exists': marker.exists()}
        if marker.exists():
            assert not marker.is_symlink() and stat.S_ISREG(marker.stat().st_mode)
            assert marker.stat().st_size <= 4096
            value['marker']['full_text'] = marker.read_text()
        if retain:
            witness.write_evidence(tmp_path / (label + '.json'), value, selected)
        return value

    async def capture():
        state['session_id'] = await acp_run.mint_acp_session(cfg)
        await acp_run.close_acp_sessions()
        harness_homes.record_resume_level('claude-code', state['session_id'], grant)
        ready = []
        task = asyncio.create_task(surface_runner('claude-code', 'terminal')(
            cfg, pointer=gen.run_path('terminal/claude-code.terminal'), first_message=PROMPT,
            resume_harness_session_id=state['session_id'], on_ready=ready.append))
        try:
            deadline = time.monotonic() + 180
            while not ready and not task.done() and time.monotonic() < deadline:
                await asyncio.sleep(.2)
            assert ready == [state['session_id']] and len(terms) == len(prepared) == len(launch) == 1
            item = prepared[0]
            assert item.commands.level == grant and item.model == MODEL
            assert item.env.get('ANTHROPIC_MODEL') == MODEL
            assert item.env_obj is None and launch[0]['cwd'] == workspace
            witness.private_home(home)
            assert (home / witness.claude._API_KEY_FILE).read_bytes() == selected.encode()
            if level == 'allow':
                # The environment a command the agent runs inherits is the
                # TTY's own: no held key in it.
                body = open(f'/proc/{next(iter(terms.values())).proc.pid}/environ', 'rb').read().decode()
                if any(key in body for key in transport._key_values().values()):
                    raise AssertionError('Held key reached the allow TTY environment')
                witness.write_evidence(tmp_path / 'literal-allow-env.json', {
                    'command': ['environ'], 'full_output': body.replace('\0', '\n'),
                    'private_selected_key_file_exact': True}, selected)
            # This acquisition has no previous command/answer in its fresh session.
            # Exact pending native identity prevents a historical screen authorizing a key.
            if level.startswith('ask-'):
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    assert not task.done() and next(iter(terms.values())).proc.poll() is None
                    pending = await asyncio.to_thread(snapshot, 'pending-latest', retain=False)
                    rows = native_rows(pending, state['session_id'], workspace)
                    calls = [(row, part) for row in rows for part in message_parts(row)
                             if part.get('type') == 'tool_use']
                    results = [part for row in rows for part in message_parts(row)
                               if part.get('type') == 'tool_result']
                    term = next(iter(terms.values()))
                    panel = ''.join(term.said().split())
                    exact = (len(calls) == 1 and calls[0][1].get('name') == 'Bash'
                        and calls[0][1].get('input', {}).get('command') == COMMAND
                        and isinstance(calls[0][1].get('id'), str) and calls[0][1]['id']
                        and not results and not pending['marker']['exists']
                        and not any(row.get('message', {}).get('stop_reason') == 'end_turn'
                                    for row in rows)
                        and all(token in panel for token in (
                            ''.join(COMMAND.split()),
                            ''.join(calls[0][1]['input'].get('description', '').split()), 'Bashcommand',
                            'PermissionruleBashrequiresconfirmationforthiscommand.',
                            'Doyouwanttoproceed?', '1.Yes', '2.No')))
                    if exact:
                        state['answered_native_tool_id'] = calls[0][1]['id']
                        snapshot('before-answer')
                        answer_armed[0] = True
                        term.send(answer)
                        assert not answer_armed[0]
                        break
                    await asyncio.sleep(.2)
                else:
                    raise AssertionError('Own exact two-choice pending Bash panel absent')
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                assert not task.done() and next(iter(terms.values())).proc.poll() is None
                candidate = await asyncio.to_thread(snapshot, 'candidate', retain=False)
                outcome = native_outcome(candidate, workspace, level)
                if outcome:
                    state['native_outcome'] = outcome
                    snapshot('native-outcome')
                    break
                await asyncio.sleep(.2)
            else:
                raise AssertionError('Complete own native command outcome absent')
            await asyncio.to_thread(witness.private_processes, next(iter(terms.values())), config_home)
            witness.private_home(home)
        finally:
            try:
                await asyncio.to_thread(snapshot, 'before-cleanup')
            finally:
                # Ending the terminal's client ends the leg.
                for term in terms.values():
                    await asyncio.to_thread(term.stop)
                try:
                    await asyncio.wait_for(task, 30)
                except BaseException:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise

    try:
        witness.run_on_test_loop(capture())
    except BaseException as error:  # noqa: BLE001 - retain gate/cleanup interruption evidence
        errors.append({'phase': 'capture', 'kind': type(error).__name__})
    finally:
        try:
            snapshot('after-terminal-cleanup')
        except BaseException as error:  # noqa: BLE001 - retain gate/cleanup interruption evidence
            errors.append({'phase': 'capture-after-terminal', 'kind': type(error).__name__})
        groups = [term.group() for term in terms.values()]
        for name, closer in [('acp', lambda: witness.run_on_test_loop(acp_run.close_acp_sessions())),
            *[(f'transport-{i}', term.stop) for i, term in enumerate(terms.values())],
            ('private-homes', harness_homes.close_harness_homes)]:
            try:
                closer()
            except BaseException as error:  # noqa: BLE001 - retain gate/cleanup interruption evidence
                errors.append({'phase': name, 'kind': type(error).__name__})
        left = [pid for group in groups for pid in group if os.path.exists(f'/proc/{pid}')]
        passed = bool(state['native_outcome'] and not errors and not left and not home.exists())
        witness.write_evidence(tmp_path / 'status.json', {**state, 'errors': errors,
            'process_group_left': left, 'private_home_removed': not home.exists(),
            'native_command_gate_passed': passed, 'input_events': input_events}, selected)
    assert passed, 'Native outcome/privacy/cleanup gate failed; retained full evidence and status'
