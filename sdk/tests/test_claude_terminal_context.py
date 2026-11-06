# SPDX-License-Identifier: MIT
"""F1487/F1554/F1567: exact Claude native session handoff on this machine."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import threading

import gen
import pytest

import _agent_shared as engine
from _agent_shared import cli_host
from _agent_shared.harnesses.claude_code import backend as claude
from tests.test_backend_claude_terminal import _CLAUDE, terminal_run  # noqa: F401


class NativeFailure(Exception):
    pass


@pytest.fixture
def lifecycle(terminal_run, monkeypatch):  # noqa: F811
    events, ready, launches = [], [], []
    controls = {'failure': ''}
    main_thread = threading.get_ident()
    original_seed = claude._seed_claude_config_host
    original_unlink = cli_host.unlink_cell_dir

    def fail(stage):
        if controls['failure'] == stage:
            raise NativeFailure(stage)

    def seed(home, project, commands):
        assert threading.get_ident() != main_thread
        events.append(('seed', home, project, commands))
        fail('seed')
        return original_seed(home, project, commands)

    @asynccontextmanager
    async def terminal(container, argv, **kwargs):
        events.append('register')
        launches.append((container, argv, kwargs))
        fail('register')
        try:
            yield {'terminal_id': 'own-claude-terminal'}
        finally:
            events.append('terminal-close')

    @asynccontextmanager
    async def live(**kwargs):
        fail('live')
        try:
            yield
        finally:
            events.append('live-close')

    async def capture(sid, **kwargs):
        events.append(('capture', sid, kwargs))

    async def wait(**kwargs):
        assert kwargs['terminal_id'] == 'own-claude-terminal'
        await kwargs['on_action']['capture_chat']()
        fail('wait')
        if controls['failure'] == 'cancel':
            raise asyncio.CancelledError

    def pull(sid, **kwargs):
        assert threading.get_ident() != main_thread
        events.append(('read', sid, kwargs))
        fail('read')
        return [], 'exact own native session bytes'

    def chatlog(messages):
        assert threading.get_ident() != main_thread
        events.append(('chatlog', messages))
        fail('chatlog')

    def trace(raw):
        assert threading.get_ident() != main_thread
        events.append(('trace', raw))
        fail('trace')

    def unlink(link, target):
        assert threading.get_ident() != main_thread
        events.append('unlink')
        original_unlink(link, target)
        fail('unlink')

    def on_ready(sid):
        ready.append(sid)
        fail('ready')

    monkeypatch.setattr(claude, '_seed_claude_config_host', seed)
    monkeypatch.setattr(claude, '_capture_chat', capture)
    monkeypatch.setattr(claude, 'pull_claude_transcript', pull)
    monkeypatch.setattr(claude, '_write_chatlog', chatlog)
    monkeypatch.setattr(claude, '_export_trace', trace)
    monkeypatch.setattr(cli_host, 'unlink_cell_dir', unlink)
    monkeypatch.setattr(gen, 'serve_terminal', terminal)
    monkeypatch.setattr(gen, 'live_files', live)
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    async def run(*, resume='', auth='api_key', model='haiku', host=True):
        await engine.surface_runner('claude-code', 'terminal')(
            {'backend': 'claude-code', 'model': model, 'auth': auth, 'commands': 'ask',
             'host_harness': dict(_CLAUDE) if host else None},
            pointer='own.terminal', extra_system='  exact raw context\n',
            resume_harness_session_id=resume, first_message='  --literal opening turn  ',
            on_ready=on_ready)
        events.append('runner-returned')

    return {'run': run, 'events': events, 'ready': ready, 'launches': launches,
            'control': controls, 'account': terminal_run}


def order(leg):
    return [x[0] if isinstance(x, tuple) else x for x in leg['events']]


@pytest.mark.parametrize('resume', ['', 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'])
@pytest.mark.parametrize('auth', ['api_key', 'claude_code'])
def test_claude_actual_entry_preserves_native_session_capture_and_runner_return_F1487_F1567(lifecycle, resume, auth):
    import os
    leg = lifecycle
    asyncio.run(leg['run'](resume=resume, auth=auth))
    events = leg['events']
    project = gen.workspace_path()
    seeds = [x for x in events if isinstance(x, tuple) and x[0] == 'seed']
    assert len(seeds) == 1 and seeds[0][2] == project and seeds[0][3] == 'ask'
    assert len(leg['launches']) == 1
    container, argv, kwargs = leg['launches'][0]
    assert container is None and argv[0] == _CLAUDE['path']
    assert kwargs['cwd'] == project and kwargs['pointer'] == 'own.terminal'
    assert kwargs['name'] == 'Agent · Claude terminal' and kwargs['label'] == 'Claude · haiku'
    assert kwargs['env']['CLAUDE_CONFIG_DIR'] == seeds[0][1]
    assert kwargs['env']['HOME'] == os.environ['HOME']
    assert ('CLAUDE_CODE_OAUTH_TOKEN' in kwargs['env']) is (auth == 'claude_code')
    sid = argv[argv.index('--session-id') + 1]
    assert sid and (sid == resume if resume else len(sid) == 36)
    assert '--resume' not in argv
    assert argv[argv.index('--append-system-prompt') + 1] == '  exact raw context\n'
    assert argv[-2:] == ['--', '--literal opening turn']
    assert leg['ready'] == [sid]
    assert kwargs['buttons'] == [{'id': 'capture_chat', 'label': 'Capture Chat'}]
    native = [x for x in events if isinstance(x, tuple) and x[0] in ('capture', 'read')]
    assert len(native) == 2 and native[0][1:] == native[1][1:]
    assert native[1][1] == sid and native[1][2]['project'] == project
    from _agent_shared import harness_homes
    assert native[1][2]['store_dir'] == harness_homes.harness_home('claude-code')
    assert order(leg)[-7:] == ['live-close', 'terminal-close', 'read', 'chatlog', 'trace', 'unlink', 'runner-returned']


@pytest.mark.parametrize('failure', ['seed', 'register', 'ready', 'live', 'wait', 'cancel'])
def test_claude_leg_exports_and_unlinks_after_native_failures_F1567(lifecycle, failure):
    leg = lifecycle
    leg['control']['failure'] = failure
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else NativeFailure):
        asyncio.run(leg['run']())
    assert order(leg)[-4:] == ['read', 'chatlog', 'trace', 'unlink']
    assert 'runner-returned' not in order(leg)
    if failure == 'seed':
        assert not leg['launches'] and not leg['ready']


@pytest.mark.parametrize('failure', ['read', 'chatlog', 'trace'])
def test_claude_exports_are_best_effort_but_still_precede_the_unlink_F1567(lifecycle, failure):
    leg = lifecycle
    leg['control']['failure'] = failure
    asyncio.run(leg['run']())
    assert order(leg)[-5:] == ['read', 'chatlog', 'trace', 'unlink', 'runner-returned']


def test_claude_failed_unlink_remains_visible_before_the_runner_returns_F1567(lifecycle):
    leg = lifecycle
    leg['control']['failure'] = 'unlink'
    with pytest.raises(NativeFailure, match='unlink'):
        asyncio.run(leg['run']())
    assert order(leg)[-1] == 'unlink'
    assert 'runner-returned' not in order(leg)


@pytest.mark.parametrize('failure', ['invalid-model', 'missing-cli'])
def test_claude_invalid_native_inputs_refuse_before_any_launch_F1487(lifecycle, failure):
    leg = lifecycle
    with pytest.raises(RuntimeError):
        asyncio.run(leg['run'](model='not-supported' if failure == 'invalid-model' else 'haiku',
                               host=failure != 'missing-cli'))
    assert not leg['events'] and not leg['launches']


@pytest.mark.parametrize('commands', ['ask', 'allow'])
def test_claude_terminal_selected_helper_has_no_key_in_launch_and_survives_until_run_end_F1560(lifecycle, commands, monkeypatch):
    import json
    import os
    import shlex
    import subprocess
    from _agent_shared import harness_homes
    key = lifecycle['account']['key']
    async def run():
        await engine.surface_runner('claude-code', 'terminal')(
            {'backend': 'claude-code', 'model': 'haiku', 'auth': 'api_key', 'commands': commands,
             'host_harness': dict(_CLAUDE)},
            pointer='own.terminal')
    # The fixture's wait presses Capture Chat; this control only needs the
    # real prepare/context and the leg's own teardown.
    async def wait(*args, **kwargs):
        return None
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    asyncio.run(run())
    _, argv, tty = lifecycle['launches'][0]
    assert key not in repr((argv, tty))
    assert not {'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'} & set(tty['env'])
    home = Path(harness_homes.harness_home('claude-code'))
    secret, helper = home / claude._API_KEY_FILE, home / claude._API_KEY_HELPER
    assert secret.read_text() == key and secret.stat().st_mode & 0o777 == 0o600
    assert helper.stat().st_mode & 0o777 == 0o600
    settings = json.loads(argv[argv.index('--settings') + 1])
    expected_settings = {**claude.claude_command_settings(commands),
                         'apiKeyHelper': shlex.join([sys.executable, str(helper)])}
    if commands == 'allow':
        expected_settings['skipDangerousModePermissionPrompt'] = True
    assert settings == expected_settings
    assert ('--dangerously-skip-permissions' in argv) == (commands == 'allow')
    assert '--disallowedTools' not in argv
    # Execute only the selected interpreter with this synthetic private file.
    result = subprocess.run([sys.executable, str(helper)], capture_output=True, timeout=5, check=False)
    assert result.returncode == 0 and result.stdout == key.encode() and result.stderr == b''
    assert 'unlink' in order(lifecycle) and secret.exists()
    harness_homes.close_harness_homes()
    assert not home.exists()
    # Nothing the run end keeps — the cell's session cache, the entry's
    # per-account home — holds the key or its helper.
    cache = Path(os.environ['XDG_CACHE_HOME'])
    for retained in cache.rglob('*'):
        if retained.is_file():
            assert key.encode() not in retained.read_bytes()
            assert retained.name not in (claude._API_KEY_FILE, claude._API_KEY_HELPER)


@pytest.mark.parametrize('damage', ['missing', 'symlink', 'mode', 'fifo', 'empty', 'oversize', 'newline'])
def test_claude_native_helper_refuses_unusable_private_credential_without_disclosure(tmp_path, damage):
    import os
    import subprocess
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    key = 'synthetic-selected-api-key'
    claude._write_api_key(str(home), key)
    secret = home / claude._API_KEY_FILE
    if damage == 'missing':
        secret.unlink()
    elif damage == 'symlink':
        target = tmp_path / 'outside'
        target.write_text(key)
        secret.unlink()
        secret.symlink_to(target)
    elif damage == 'mode':
        secret.chmod(0o644)
    elif damage == 'fifo':
        secret.unlink()
        os.mkfifo(secret, 0o600)
    else:
        secret.write_text({'empty': '', 'oversize': 'x' * 16385, 'newline': 'first\nsecond'}[damage])
    result = subprocess.run([sys.executable, '-O', str(home / claude._API_KEY_HELPER)],
                            capture_output=True, timeout=5, check=False)
    assert result.returncode == 1 and result.stdout == b''
    assert result.stderr == b'Selected credential unavailable\n'


def test_claude_selected_auth_switch_clears_helper_and_failed_selection_never_reuses_it(tmp_path, monkeypatch):
    import shlex
    from _agent_shared import secret_hygiene
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    selected = {'key': 'synthetic-first-key'}
    remembered = []
    async def token(*, required=True):
        assert required
        return 'synthetic-static-setup-token'
    monkeypatch.setattr(claude, 'claude_subscription_token', token)
    monkeypatch.setattr(secret_hygiene, 'remember', remembered.append)
    secret, helper = home / claude._API_KEY_FILE, home / claude._API_KEY_HELPER
    env, command = asyncio.run(claude._native_auth('api_key', str(home), key=selected['key']))
    # Both auths bill the provider registry's endpoint.
    assert env == {'ANTHROPIC_BASE_URL': 'https://api.anthropic.com'}
    assert shlex.split(command) == [sys.executable, str(helper)]
    assert secret.read_text() == selected['key']
    assert remembered == [selected['key']]
    env, command = asyncio.run(claude._native_auth('claude_code', str(home)))
    assert env == {'ANTHROPIC_BASE_URL': 'https://api.anthropic.com',
                   'CLAUDE_CODE_OAUTH_TOKEN': 'synthetic-static-setup-token'} and command == ''
    assert not secret.exists() and not helper.exists()
    selected['key'] = 'synthetic-second-key'
    asyncio.run(claude._native_auth('api_key', str(home), key=selected['key']))
    selected['key'] = ''
    with pytest.raises(RuntimeError, match='select an Anthropic API key'):
        asyncio.run(claude._native_auth('api_key', str(home), key=selected['key']))
    assert not secret.exists() and not helper.exists()


def test_claude_cancelled_key_write_settles_then_removes_private_pair(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    entered, release = threading.Event(), threading.Event()
    cleanup_entered, cleanup_release = threading.Event(), threading.Event()
    events = []
    real = claude._write_api_key
    main = threading.get_ident()
    def write(where, key):
        assert threading.get_ident() != main
        if key:
            entered.set()
            assert release.wait(5), 'test did not release credential writer'
        if key is None and 'write' in events:
            cleanup_entered.set()
            assert cleanup_release.wait(5), 'test did not release credential cleanup'
        real(where, key)
        events.append('write' if key else 'clear')
    monkeypatch.setattr(claude, '_write_api_key', write)
    async def flow():
        task = asyncio.create_task(claude._native_auth('api_key', str(home),
                                                       key='synthetic-held-write'))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            assert not task.done()
            release.set()
            assert await asyncio.to_thread(cleanup_entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)  # Deliver the second cancellation while cleanup is held.
            assert not task.done(), 'Cancelled owner escaped before its cleanup worker settled'
        finally:
            release.set()
            cleanup_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(flow())
    assert events == ['clear', 'write', 'clear']
    assert not (home / claude._API_KEY_FILE).exists() and not (home / claude._API_KEY_HELPER).exists()


@pytest.fixture
def claude_tty_command_oracle():
    import ast
    import json
    import os
    import re

    source = Path(os.environ.get('CLAUDE_TTY_COMMAND_SOURCE',
        Path(__file__).resolve().parents[1] / 'tests_live/test_live_claude_tty_commands.py'))
    names = {'terminal_query_only', 'message_parts', 'native_rows', 'native_outcome'}
    constants = {'COMMAND', 'PROMPT', 'MODEL', 'REFUSAL'}
    nodes = [node for node in ast.parse(source.read_text()).body
        if (isinstance(node, ast.FunctionDef) and node.name in names)
        or (isinstance(node, ast.Assign) and all(isinstance(target, ast.Name)
            and target.id in constants for target in node.targets))]
    namespace = {'json': json, 're': re}
    exec(compile(ast.Module(nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


def _claude_tty_native_case(oracle, level):
    """Synthetic records use the three measured native branches, with fresh IDs."""
    import json

    sid, workspace, digest = 'own-session', '/own-cell', 'a' * 64
    base = {'isSidechain': False, 'sessionId': sid, 'cwd': workspace}
    rows = [{**base, 'type': 'user', 'uuid': 'prompt',
             'message': {'role': 'user', 'content': oracle['PROMPT']}}]
    answers = []
    marker = {'exists': False}
    rows.extend([
        {**base, 'type': 'assistant', 'uuid': 'call', 'message': {'role': 'assistant',
            'model': oracle['MODEL'], 'stop_reason': 'tool_use', 'content': [
                {'type': 'tool_use', 'id': 'own-call', 'name': 'Bash',
                 'input': {'command': oracle['COMMAND'], 'description': 'Compute own digest'}}]}},
        {**base, 'type': 'user', 'uuid': 'result', 'parentUuid': 'call',
            'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'own-call',
                 'content': digest, 'is_error': False}]},
            'toolUseResult': {'stdout': digest, 'stderr': '', 'interrupted': False,
                             'isImage': False, 'noOutputExpected': False}},
    ])
    if level.startswith('ask-'):
        answers = [{'kind': 'native-panel-answer', 'case': level,
                    'hex': '320d' if level == 'ask-reject' else '310d'}]
    if level == 'ask-reject':
        rows[-1]['message']['content'][0].update(content=oracle['REFUSAL'], is_error=True)
        rows[-1]['toolUseResult'] = 'User rejected tool use'
        rows.append({**base, 'type': 'user', 'uuid': 'interrupt', 'message': {'role': 'user',
            'content': [{'type': 'text', 'text': '[Request interrupted by user for tool use]'}]}})
    else:
        marker = {'exists': True, 'full_text': digest + '\n'}
    if level != 'ask-reject':
        rows.append({**base, 'type': 'assistant', 'uuid': 'final', 'message': {'role': 'assistant',
            'model': oracle['MODEL'], 'stop_reason': 'end_turn', 'content': [
                {'type': 'text', 'text': digest}]}})
    value = {'session_id': sid, 'probe_sha256': digest, 'marker': marker,
        'input_events': answers, 'answered_native_tool_id': 'own-call',
        'native_session_files': [{'full_jsonl': ''.join(json.dumps(row) + '\n' for row in rows)}]}
    return value, rows, workspace


@pytest.mark.parametrize('level,expected', [
    ('ask-reject', 'native_user_rejected_interrupted'),
    ('ask-allow', 'native_normal_command_completed'),
    ('allow', 'native_normal_command_completed'),
])
def test_claude_tty_full_native_outcome_contract(claude_tty_command_oracle, level, expected):
    value, _, workspace = _claude_tty_native_case(claude_tty_command_oracle, level)
    assert claude_tty_command_oracle['native_outcome'](value, workspace, level) == expected


@pytest.mark.parametrize('fault', ['foreign-session', 'foreign-cwd', 'sidechain', 'wrong-model',
    'wrong-command', 'extra-input', 'wrong-result-id', 'wrong-parent', 'result-suffix',
    'mirror-suffix', 'mirror-bool-int', 'result-bool-int', 'wrong-marker', 'final-suffix',
    'no-end-turn', 'extra-call', 'extra-result', 'wrong-answer-id', 'no-answer'])
def test_claude_tty_refuses_incomplete_or_mismatched_success(claude_tty_command_oracle, fault):
    import copy
    import json

    value, rows, workspace = _claude_tty_native_case(claude_tty_command_oracle, 'ask-allow')
    call, result = rows[1]['message']['content'][0], rows[2]['message']['content'][0]
    if fault == 'foreign-session': rows[1]['sessionId'] = 'foreign'
    elif fault == 'foreign-cwd': rows[1]['cwd'] = '/foreign'
    elif fault == 'sidechain': rows[1]['isSidechain'] = True
    elif fault == 'wrong-model': rows[-1]['message']['model'] = 'foreign'
    elif fault == 'wrong-command': call['input']['command'] += ' extra'
    elif fault == 'extra-input': call['input']['extra'] = True
    elif fault == 'wrong-result-id': result['tool_use_id'] = 'foreign'
    elif fault == 'wrong-parent': rows[2]['parentUuid'] = 'foreign'
    elif fault == 'result-suffix': result['content'] += ' extra'
    elif fault == 'mirror-suffix': rows[2]['toolUseResult']['stdout'] += ' extra'
    elif fault == 'mirror-bool-int': rows[2]['toolUseResult']['interrupted'] = 0
    elif fault == 'result-bool-int': result['is_error'] = 0
    elif fault == 'wrong-marker': value['marker']['full_text'] += ' extra'
    elif fault == 'final-suffix': rows[-1]['message']['content'][0]['text'] += ' extra'
    elif fault == 'no-end-turn': rows[-1]['message']['stop_reason'] = 'tool_use'
    elif fault == 'extra-call': rows.insert(2, copy.deepcopy(rows[1]))
    elif fault == 'extra-result': rows.insert(3, copy.deepcopy(rows[2]))
    elif fault == 'wrong-answer-id': value['answered_native_tool_id'] = 'foreign'
    elif fault == 'no-answer': value['input_events'] = []
    value['native_session_files'][0]['full_jsonl'] = ''.join(json.dumps(row) + '\n' for row in rows)
    try:
        outcome = claude_tty_command_oracle['native_outcome'](value, workspace, 'ask-allow')
    except AssertionError:
        outcome = ''
    assert outcome == ''


@pytest.mark.parametrize('fault', ['missing-interrupt', 'wrong-refusal', 'wrong-error-type',
                                   'marker-exists', 'invented-final'])
def test_claude_tty_user_rejection_is_only_measured_interruption(claude_tty_command_oracle, fault):
    import json

    value, rows, workspace = _claude_tty_native_case(claude_tty_command_oracle, 'ask-reject')
    if fault == 'missing-interrupt': rows.pop()
    elif fault == 'wrong-refusal': rows[2]['message']['content'][0]['content'] = 'cancelled'
    elif fault == 'wrong-error-type': rows[2]['message']['content'][0]['is_error'] = 1
    elif fault == 'marker-exists': value['marker'] = {'exists': True, 'full_text': 'a' * 64 + '\n'}
    elif fault == 'invented-final': rows.append(_claude_tty_native_case(claude_tty_command_oracle, 'allow')[1][-1])
    value['native_session_files'][0]['full_jsonl'] = ''.join(json.dumps(row) + '\n' for row in rows)
    assert claude_tty_command_oracle['native_outcome'](value, workspace, 'ask-reject') == ''
