"""D23 draft execution stays in the parent's declared command boundary."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from tests._commands_matrix import recording_runtime  # noqa: E402, F401


def module():
    return importlib.import_module('_agent_shared.generator_test')


def draft(bridge, source='print("child")\n'):
    root = Path(bridge.workspace)
    (root / 'draft').mkdir(exist_ok=True)
    (root / 'draft/main.generator').write_text('entrypoint: main.py\nmain_config: main.set_yaml\n')
    (root / 'draft/main.set_yaml').write_text('values:\n  answer: before\n')
    (root / 'draft/main.py').write_text(source)
    return 'draft/main.generator'


def test_denied_generator_command_is_absent_and_cannot_be_called_D23(monkeypatch):
    m = module()
    from _agent_shared.workspace_tools import tool_entries
    ctx = m.CommandContext('host')
    rows = tool_entries(enable_file_tools=False, websearch=False,
                                  commands='deny', command_context=ctx)
    assert 'test_generator' not in [row[0] for row in rows]
    async def must_not_execute(*args, **kwargs):
        raise AssertionError('denied command executed')
    monkeypatch.setattr(m, '_execute', must_not_execute)
    result = asyncio.run(m.run_test(ctx, commands='deny', path='draft/main.generator', settings={}, prompt=''))
    assert result['error'] == 'commands_denied'


def test_generator_ask_uses_real_command_card_before_execution_D23(monkeypatch):
    m = module()
    from _agent_shared.workspace_tools import tool_entries
    cards, executions = [], []
    choice = ['reject_once']
    async def ask(title, **kwargs):
        cards.append((title, kwargs))
        return choice[0]
    async def execute(context, **kwargs):
        executions.append((context, kwargs))
        return {'exit_code': 0}
    monkeypatch.setattr(m.gen, 'ask', ask)
    monkeypatch.setattr(m, '_execute', execute)
    rows = tool_entries(enable_file_tools=False, websearch=False,
        commands='ask', command_context=m.CommandContext('host'))
    row = next(row for row in rows if row[0] == 'test_generator')
    assert row[1:3] == ('command', True)
    result = asyncio.run(row[-1]('draft/main.generator', {'answer': 42}, 'prompt'))
    assert result['error'] == 'approval_refused' and executions == []
    assert cards[0][1]['kind'] == 'execute'
    assert all(value in cards[0][1]['detail'] for value in ('draft/main.generator', '42', 'prompt'))
    choice[0] = 'allow_once'
    assert asyncio.run(row[-1]('draft/main.generator', {}, '')) == {'exit_code': 0}
    assert len(executions) == 1


@pytest.mark.parametrize('locus', ['host', 'container'])
def test_acp_generator_tool_receives_actual_profile_environment_D23(monkeypatch, locus):
    m = module()
    from _agent_shared import acp_run, cli_container
    env = SimpleNamespace(mount='/workspace') if locus == 'container' else None
    profile = SimpleNamespace(locus=locus, env_obj=env, commands='allow', commands_notice='', bind_host=None)
    async def prepare(ctx):
        return profile
    monkeypatch.setitem(acp_run._PROFILES, 'test-d23', prepare)
    class Captured(Exception):
        pass
    async def execute(context, **kwargs):
        assert context.locus == locus and context.env_obj is env
        return {'placement': locus}
    monkeypatch.setattr(m, '_execute', execute)
    def bridge(**kwargs):
        row = next(row for row in kwargs['extra_entries'] if row[0] == 'test_generator')
        assert row[1:3] == ('command', True)
        captured.append(row[-1])
        raise Captured
    captured = []
    monkeypatch.setattr(cli_container, 'gateway_bridge', bridge)
    with pytest.raises(Captured):
        asyncio.run(acp_run._open_session(backend='test-d23', auth='api_key', provider='test', model='test',
            system_prompt='', mcp_decls=[], enable_file_tools=False, websearch=False,
            turn_skills=[], commands='allow'))
    assert asyncio.run(captured[0]('draft/main.generator', {}, '')) == {'placement': locus}


def test_container_generator_uses_env_exec_and_child_token_not_parent_process_D23(native_bridge, monkeypatch):
    m = module()
    path = draft(native_bridge, 'raise AssertionError("never in parent")\n')
    calls, relays = [], []
    class Relay:
        def __init__(self, **kwargs):
            self.bridge = native_bridge
            self.port, self.token = 19876, 'child-only-token'
            self.result = {'success': True}
            self.closed = False
            relays.append(self)
        def __enter__(self):
            return self
        def close(self):
            self.closed = True
        def wait_finished(self, timeout):
            return True
    class Environment:
        mount = '/workspace'
        def exec(self, argv, **kwargs):
            calls.append((argv, kwargs))
            if kwargs.get('detach'):
                private = Path(native_bridge.workspace) / kwargs['env']['COLLIMATE_RUN_DIR'].removeprefix('/workspace/')
                (private / 'command.ready').write_text('{"pid":0,"start":"0"}')
                (private / 'command.result').write_text('{"exit_code":0,"output":"stdout and stderr"}')
            return 0, 'stdout and stderr'
    monkeypatch.setattr(m, 'Forwarder', Relay)
    monkeypatch.setattr(m, 'docker_gateway', lambda: '172.17.0.1')
    result = asyncio.run(m.run_test(m.CommandContext('container', env_obj=Environment()),
        commands='allow', path=path, settings={'answer': 42}, prompt='private prompt'))
    argv, kwargs = calls[0]
    command = json.loads(argv[-1])
    assert 'uv' in command and '/workspace/draft/main.py' in command
    assert kwargs['cwd'] == '/workspace/draft'
    assert kwargs['env']['COLLIMATE_IPC_TOKEN'] == 'child-only-token'
    assert kwargs['env']['COLLIMATE_IPC_HOST'] == '172.17.0.1'
    assert kwargs['env']['COLLIMATE_RUN_DIR'].startswith('/workspace/.collrun/forward-')
    assert kwargs['user'] == f'{os.getuid()}:{os.getgid()}'
    assert relays[0].closed and 'child-only-token' not in json.dumps(result)
    assert result['output'] == 'stdout and stderr'
    assert not list(Path(native_bridge.workspace).glob('.collrun/forward-*'))


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
def test_generator_command_tool_membership_follows_the_grant_D23(commands):
    m = module()
    from _agent_shared import workspace_tools
    rows = workspace_tools.tool_entries(enable_file_tools=False, websearch=False,
        commands=commands, command_context=m.CommandContext('host'))
    assert ('test_generator' in [row[0] for row in rows]) == (commands != 'deny')


def test_cached_command_bridges_never_reuse_another_grant_or_environment_D23(monkeypatch):
    from _agent_shared import cli_container, mcp_bridge
    monkeypatch.setattr(cli_container, '_BRIDGES', {})
    monkeypatch.setattr(mcp_bridge, 'start_bridge', lambda **kwargs: object())
    async def check():
        same = {'extra_entries': [], 'bind_host': '127.0.0.1'}
        first = cli_container.gateway_bridge(**same, cache_scope=('allow', 'container', 1))
        assert first is cli_container.gateway_bridge(**same, cache_scope=('allow', 'container', 1))
        assert first is not cli_container.gateway_bridge(**same, cache_scope=('ask', 'container', 1))
        assert first is not cli_container.gateway_bridge(**same, cache_scope=('allow', 'container', 2))
    asyncio.run(check())


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
def test_existing_goose_terminal_bridge_offers_generator_command_by_grant_D23(
        recording_runtime, monkeypatch, commands):  # noqa: F811
    from contextlib import asynccontextmanager
    from _agent_shared import surface_runner, cli_container
    m = module()
    captured = []
    class Captured(Exception):
        pass
    @asynccontextmanager
    async def bridge(**kwargs):
        captured.extend(row for row in kwargs['extra_entries'] if row[0] == 'test_generator')
        raise Captured
        yield  # pragma: no cover
    async def execute(context, **kwargs):
        assert context.env_obj is recording_runtime and context.locus == 'container'
        return {'placement': 'goose container'}
    monkeypatch.setattr(cli_container, 'leg_bridge', bridge)
    monkeypatch.setattr(m, '_execute', execute)
    async def allow(*args, **kwargs):
        return 'allow_once'
    monkeypatch.setattr(m.gen, 'ask', allow)
    with pytest.raises(Captured):
        asyncio.run(surface_runner("goose", "terminal")(
            {'backend': 'goose', 'provider': 'openai', 'model': 'test-model', 'commands': commands, 'websearch': False},
            pointer='_runs/tester-0badc0de/terminal/test.terminal'))
    assert bool(captured) == (commands != 'deny')
    if captured:
        assert asyncio.run(captured[0][-1]('draft/main.generator', {}, '')) == {'placement': 'goose container'}


@pytest.mark.parametrize('ending', ['cancel', 'timeout'])
def test_host_child_stops_and_cleans_private_state_on_cancel_or_timeout_D23(
        native_bridge, monkeypatch, tmp_path, ending):
    m = module()
    from types import SimpleNamespace
    from collimate import shared as runtime
    if not shutil.which('uv'):
        pytest.skip('uv is not installed')
    monkeypatch.setattr(runtime, 'sdk_distribution', lambda: SimpleNamespace(requires=[]))
    monkeypatch.setenv('UV_CACHE_DIR', str(tmp_path / 'uv-cache'))
    path = draft(native_bridge, 'import signal, time\nfrom pathlib import Path\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        'while True:\n    Path("heartbeat").write_text(str(time.monotonic_ns()))\n    time.sleep(0.01)\n')
    heartbeat = Path(native_bridge.workspace) / 'draft/heartbeat'
    relays = []
    actual = m.Forwarder
    def relay(**kwargs):
        value = actual(**kwargs)
        relays.append(value)
        return value
    monkeypatch.setattr(m, 'Forwarder', relay)
    if ending == 'timeout':
        monkeypatch.setattr(m, 'TIMEOUT_S', 0.5)
    async def check():
        task = asyncio.create_task(m.run_test(m.CommandContext('host'), commands='allow', path=path,
                                             settings={}, prompt=''))
        for _ in range(100):
            if heartbeat.exists():
                break
            await asyncio.sleep(0.01)
        assert heartbeat.exists()
        if ending == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)['error'] == 'test_generator_timeout'
        assert relays[0].dispatch({'op': 'update_cell'})['reason'] == 'forward_refused'
        assert not list(Path(native_bridge.workspace).glob('.collrun/forward-*'))
        last = heartbeat.read_text()
        await asyncio.sleep(0.1)
        assert heartbeat.read_text() == last
    asyncio.run(check())


def test_container_cancel_closes_relay_before_killing_command_D23(native_bridge, monkeypatch):
    m = module()
    import threading
    path = draft(native_bridge)
    started, stopped = threading.Event(), threading.Event()
    relays = []
    class Relay:
        def __init__(self, **kwargs):
            self.bridge = native_bridge
            self.port, self.token = 19876, 'child-token'
            self.result = {'success': None}
            self.closed = False
            relays.append(self)
        def __enter__(self):
            return self
        def close(self):
            self.closed = True
    class Environment:
        mount = '/workspace'
        def exec(self, argv, **kwargs):
            if kwargs.get('detach'):
                private = Path(native_bridge.workspace) / kwargs['env']['COLLIMATE_RUN_DIR'].removeprefix('/workspace/')
                (private / 'command.ready').write_text('{"pid":0,"start":"0"}')
                started.set()
                return 0, ''
            assert relays[0].closed
            stopped.set()
            return 0, ''
    monkeypatch.setattr(m, 'Forwarder', Relay)
    monkeypatch.setattr(m, 'docker_gateway', lambda: '172.17.0.1')
    async def check():
        task = asyncio.create_task(m.run_test(m.CommandContext('container', env_obj=Environment()),
            commands='allow', path=path, settings={}, prompt=''))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(check())
    assert stopped.is_set()
    assert not list(Path(native_bridge.workspace).glob('.collrun/forward-*'))


def test_actual_uv_child_uses_private_config_fixed_relay_and_cleanup_D23(native_bridge, monkeypatch, tmp_path):
    m = module()
    if not shutil.which('uv'):
        pytest.skip('uv is not installed')
    from types import SimpleNamespace
    from collimate import shared as runtime
    monkeypatch.setattr(runtime, 'sdk_distribution', lambda: SimpleNamespace(requires=[]))
    monkeypatch.setenv('UV_CACHE_DIR', str(tmp_path / 'uv-cache'))
    source = '''import json, os, socket, sys
from pathlib import Path
run_dir = Path(os.environ['COLLIMATE_RUN_DIR'])
assert (run_dir / 'prompt.md').read_text() == 'child prompt'
assert 'answer: 42' in (run_dir / 'config/files/main.set_yaml').read_text()
with socket.create_connection((os.environ['COLLIMATE_IPC_HOST'], int(os.environ['COLLIMATE_IPC_PORT']))) as sock:
    sock.sendall((json.dumps({'op':'__auth__','token':os.environ['COLLIMATE_IPC_TOKEN']})+'\\n').encode())
    sock.sendall(b'{"op":"read_account","req_id":"refuse"}\\n')
    answer = json.loads(sock.makefile('rb').readline())
    assert answer['reason'] == 'forward_refused'
    sock.sendall(b'{"op":"finish","success":true,"req_id":"done"}\\n')
    assert json.loads(sock.makefile('rb').readline())['ok']
print('uv child stdout')
print('uv child stderr', file=sys.stderr)
'''
    path = draft(native_bridge, source)
    result = asyncio.run(m.run_test(m.CommandContext('host'), commands='allow', path=path,
                                   settings={'answer': 42}, prompt='child prompt'))
    assert result['exit_code'] == 0, result
    assert result['result']['success'] is True
    assert result['result']['refused']['op'] == 'read_account'
    assert 'uv child stdout' in result['output'] and 'uv child stderr' in result['output']
    assert not list(Path(native_bridge.workspace).glob('.collrun/forward-*'))


@pytest.mark.parametrize('finished', [True, False])
def test_windows_host_teardown_without_posix_process_api_D23(monkeypatch, finished):
    m = module()
    # Model the actual Windows os API boundary, not a Linux killpg pretending
    # to be Windows. No killpg/getuid/getgid attributes exist here.
    monkeypatch.setattr(m, 'os', SimpleNamespace(name='nt', environ={}, path=os.path))
    calls = []
    class Process:
        pid = 1234
        returncode = 0 if finished else None
        async def wait(self):
            self.returncode = 0
            return 0
        def kill(self):
            calls.append('fallback-kill')
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()
    monkeypatch.setattr(m.asyncio, 'create_subprocess_exec', spawn)
    asyncio.run(m._stop_host(Process()))
    if finished:
        assert calls == []
    else:
        assert calls[0][0][-4:] == ('/PID', '1234', '/T', '/F')


def test_windows_spawn_and_container_identity_boundary_D23(monkeypatch, tmp_path):
    m = module()
    monkeypatch.setattr(m, 'os', SimpleNamespace(name='nt', environ={}, path=os.path,
        open=os.open, fdopen=os.fdopen, fstat=os.fstat, O_RDONLY=os.O_RDONLY))
    calls = []
    class Process:
        pid, returncode = 1234, 0
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()
        async def wait(self):
            return 0
    async def spawn(*args, **kwargs):
        calls.append(kwargs)
        return Process()
    monkeypatch.setattr(m.asyncio, 'create_subprocess_exec', spawn)
    child = SimpleNamespace(argv=['uv', 'run'], cwd='workspace', env={}, run_dir='private',
                            host_run_dir=str(tmp_path))
    assert asyncio.run(m._host_command(child)) == (0, '')
    assert calls[0].get('creationflags') == 0x00000200
    assert not calls[0].get('start_new_session')
    class Environment:
        def exec(self, argv, **kwargs):
            assert kwargs['user'] is None  # Docker image identity on non-POSIX hosts
            if kwargs.get('detach'):
                (tmp_path / 'command.ready').write_text('{"pid":0,"start":"0"}')
                (tmp_path / 'command.result').write_text('{"exit_code":0,"output":""}')
            return 0, ''
    assert asyncio.run(m._container_command(Environment(), child)) == (0, '')


def test_host_draft_refuses_nonfilesystem_sdk_resource_before_relay_P4F33(
        native_bridge, monkeypatch, tmp_path):
    import importlib.resources
    import zipfile
    m = module()
    path = draft(native_bridge, 'raise AssertionError("must not execute")\n')
    def unexpected_relay(**kwargs):
        raise AssertionError('Relay started before SDK filesystem preflight')
    monkeypatch.setattr(m, 'Forwarder', unexpected_relay)
    with zipfile.ZipFile(tmp_path / 'sdk.zip', 'w') as archive:
        archive.writestr('collimate/__init__.py', '')
        monkeypatch.setattr(importlib.resources, 'files', lambda name: zipfile.Path(archive, 'collimate/'))
        result = asyncio.run(m.run_test(m.CommandContext('host'), commands='allow',
                                       path=path, settings={}, prompt=''))
    assert result == {'error': 'test_generator_failed',
                      'message': 'Host draft commands require a filesystem-backed collimate package'}
    assert not list(Path(native_bridge.workspace).glob('.collrun/forward-*'))
