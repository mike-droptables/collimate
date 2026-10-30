# SPDX-License-Identifier: MIT
"""F1554/F1567/F1461: Cline owns cleanup beyond its terminal bridge lifetime."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import gen
import pytest

import _agent_shared as engine
from _agent_shared import cli_container, workspace_tools
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

from _agent_shared.harnesses.cline import backend as cline
from _agent_shared.harnesses import CommandsPolicy
from tests._commands_matrix import recording_runtime  # noqa: F401


async def _no_credentials(adapter, *, provider, plan):
    """The pipeline's credential step for a test whose renderer is a fake."""
    return _pipeline.Credentials(provider, plan, key="" if plan else "sk-test")


class NativeFailure(Exception):
    pass


@pytest.fixture
def leg(recording_runtime, monkeypatch):  # noqa: F811
    events, prepared, launches, ready, logs = [], [], [], [], []
    control = {'failure': '', 'grant': None, 'hub_running': True}
    main_thread = threading.get_ident()
    original_prepare = cline.prepare
    original_policy = cline.apply_commands_policy
    original_keys = cline.remove_key_settings
    original_rows = workspace_tools.tool_entries
    original_exec = recording_runtime.exec
    real_inject, real_remove = cli_container.inject_context_file, cli_container.remove_context_file

    def fail(stage):
        if stage in control['failure'].split(','):
            raise NativeFailure('native-' + stage)

    async def prepare(ctx, *, leg):
        events.append(('prepare', dict(ctx), leg))
        fail('prepare')
        value = await original_prepare(ctx, leg=leg)
        if control['grant'] is not None:
            from dataclasses import replace
            value = replace(value, commands=CommandsPolicy(control['grant'], {}))
        prepared.append(value)
        return value

    def inject(env, path, text):
        assert env is recording_runtime and path == '/workspace/AGENTS.md'
        events.append(('inject', text))
        fail('inject')

    def rows(**kwargs):
        events.append(('rows', kwargs))
        assert kwargs['command_context'].env_obj is recording_runtime
        fail('rows')
        return original_rows(**kwargs)

    @asynccontextmanager
    async def bridge(**kwargs):
        events.append('bridge-enter')
        fail('bridge-enter')
        try:
            yield SimpleNamespace(url_container='http://own/cline/mcp')
        finally:
            events.append('bridge-close')
            fail('bridge-close')

    def policy(env, level, **kwargs):
        assert threading.get_ident() != main_thread
        if prepared:
            stage = 'bind' if kwargs['mcp_servers'] else 'reset'
            events.append((stage, level, kwargs))
            fail(stage)
        return original_policy(env, level, **kwargs)

    def remove(env, path):
        assert env is recording_runtime and path == '/workspace/AGENTS.md'
        assert threading.get_ident() != main_thread
        events.append('remove')
        fail('remove')
        real_remove(env, path)

    def keys():
        assert threading.get_ident() != main_thread
        if prepared:
            events.append('keys')
            fail('keys')
        original_keys()

    def execute(argv, **kwargs):
        if argv[:2] == ['cline', 'hub']:
            assert threading.get_ident() != main_thread
            assert kwargs == {'env': prepared[0].env, 'timeout': 10}
            stage = 'hub-' + argv[-1]
            events.append(stage)
            fail(stage)
            return 0, json.dumps({'running': control['hub_running']} if argv[-1] == 'status' else {'stopped': True})
        return original_exec(argv, **kwargs)

    @asynccontextmanager
    async def terminal(container, argv, **kwargs):
        events.append('register')
        launches.append((container, argv, kwargs))
        fail('register')
        try:
            yield {'terminal_id': 'own-cline-terminal'}
        finally:
            events.append('terminal-close')

    @asynccontextmanager
    async def live(**kwargs):
        try:
            yield
        finally:
            events.append('live-close')

    async def wait(**kwargs):
        assert kwargs == {'terminal_id': 'own-cline-terminal', 'on_action': None}
        fail('wait')
        if control['failure'] == 'cancel':
            raise asyncio.CancelledError

    def on_ready(sid, **kwargs):
        ready.append((sid, kwargs))
        fail('ready')

    monkeypatch.setattr(cline, 'prepare', prepare)
    monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
    monkeypatch.setattr(cline, 'apply_commands_policy', policy)
    monkeypatch.setattr(cline, 'remove_key_settings', keys)
    monkeypatch.setattr(cline, '_secret_log', logs.append)
    monkeypatch.setattr(cli_container, 'inject_context_file', inject)
    monkeypatch.setattr(cli_container, 'remove_context_file', remove)
    monkeypatch.setattr(cli_container, 'leg_bridge', bridge)
    monkeypatch.setattr(workspace_tools, 'tool_entries', rows)
    monkeypatch.setattr(recording_runtime, 'exec', execute)
    monkeypatch.setattr(gen, 'serve_terminal', terminal)
    monkeypatch.setattr(gen, 'live_files', live)
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    async def run(*, resume='', text='', model='selected/model', commands='allow'):
        await engine.surface_runner('cline', 'terminal')(
            {'backend': 'cline', 'auth': 'api_key', 'provider': 'openrouter', 'model': model,
             'commands': commands, 'websearch': False},
            pointer='own.terminal', extra_system=text, resume_harness_session_id=resume,
            first_message='ignored-message', on_ready=on_ready)

    return SimpleNamespace(run=run, events=events, prepared=prepared, launches=launches,
        ready=ready, logs=logs, control=control, env=recording_runtime,
        real_inject=real_inject, real_remove=real_remove, policy=policy)


def stages(leg):
    return [x[0] if isinstance(x, tuple) else x for x in leg.events]


@pytest.mark.parametrize('resume', ['', 'ignored-native-session'])
@pytest.mark.parametrize('text', ['', ' \n\t ', '  exact native context\n'])
def test_cline_actual_entry_preserves_fresh_launch_and_cleanup_after_bridge_F1554_F1567(leg, resume, text):
    asyncio.run(leg.run(resume=resume, text=text))
    assert len(leg.prepared) == 1 and leg.events[0][2] == 'terminal'
    prepared = leg.prepared[0]
    container, argv, kwargs = leg.launches[0]
    assert container == prepared.env_obj.container_id and argv == list(prepared.argv)
    assert kwargs == {'cwd': '/workspace', 'env': {'TERM': 'xterm-256color', **prepared.env},
        'label': 'Cline · selected/model', 'pointer': 'own.terminal', 'keep_alive': True,
        'buttons': [], 'name': 'Agent · Cline'}
    assert leg.ready == [('', {})]
    assert leg.logs.count('cline is terminal session tier C — starting a fresh terminal') == bool(resume)
    assert 'cline has no websearch-off config seam — its built-in web tools stay available in the terminal' in leg.logs
    injects = [x for x in leg.events if isinstance(x, tuple) and x[0] == 'inject']
    assert injects == ([('inject', text)] if text.strip() else [])
    order = stages(leg)
    if text.strip():
        assert order.index('inject') < order.index('rows')
    assert order.index('rows') < order.index('bridge-enter') < order.index('bind') < order.index('register')
    assert order[-8:] == ['live-close', 'terminal-close', 'bridge-close', 'remove', 'keys', 'reset', 'hub-status', 'hub-stop']
    assert order.index('live-close') < order.index('terminal-close') < order.index('bridge-close')
    assert not (Path(cline.key_settings_host_dir()) / 'providers.json').exists()


@pytest.mark.parametrize('grant', ['deny', 'ask', 'allow'])
def test_cline_rows_bind_and_cleanup_use_authoritative_prepared_grant_F1554(leg, grant):
    leg.control['grant'] = grant
    asyncio.run(leg.run(commands='allow', model=''))
    rows = next(x[1] for x in leg.events if isinstance(x, tuple) and x[0] == 'rows')
    assert rows['commands'] == grant and rows['enable_file_tools'] is False
    assert rows['websearch'] is False
    bindings = [x for x in leg.events if isinstance(x, tuple) and x[0] in ('bind', 'reset')]
    assert bindings == [('bind', grant, {'terminal': True, 'mcp_servers': {'collimate': cline.mcp_server_entry('http://own/cline/mcp')}}),
                        ('reset', grant, {'terminal': True, 'mcp_servers': {}})]
    assert leg.launches[0][2]['label'] == 'Cline · default'


@pytest.mark.parametrize('failure', ['prepare', 'inject', 'rows', 'bridge-enter', 'bind', 'register',
                                    'ready', 'wait', 'cancel', 'bridge-close', 'keys', 'reset', 'hub-status', 'hub-stop'])
def test_cline_owned_cleanup_covers_entry_and_exit_failures_with_visible_errors_F1567(leg, failure):
    leg.control['failure'] = failure
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else NativeFailure,
                       match=None if failure == 'cancel' else 'native-' + failure):
        asyncio.run(leg.run(text='native context'))
    order = stages(leg)
    if failure == 'prepare':
        assert order == ['prepare']
        return
    for stage in ('remove', 'keys', 'reset', 'hub-status'):
        assert order.count(stage) == 1
    if 'bridge-close' in order:
        assert order.index('bridge-close') < order.index('remove')
    assert order.index('remove') < order.index('keys') < order.index('reset') < order.index('hub-status')
    assert order.count('hub-stop') == (0 if failure == 'hub-status' else 1)
    if failure in ('inject', 'rows', 'bridge-enter', 'bind'):
        assert not leg.launches and not leg.ready


def test_cline_only_context_removal_is_best_effort_F1567(leg):
    leg.control['failure'] = 'remove'
    asyncio.run(leg.run())
    assert stages(leg)[-5:] == ['remove', 'keys', 'reset', 'hub-status', 'hub-stop']


def test_cline_nested_cleanup_keeps_last_native_error_and_all_finally_steps_F1567(leg):
    leg.control['failure'] = 'keys,reset,hub-stop'
    with pytest.raises(NativeFailure, match='native-hub-stop') as caught:
        asyncio.run(leg.run())
    assert str(caught.value.__context__) == 'native-reset'
    assert str(caught.value.__context__.__context__) == 'native-keys'
    assert stages(leg)[-5:] == ['remove', 'keys', 'reset', 'hub-status', 'hub-stop']


@pytest.mark.parametrize('running', [False, None, 1, 'true'])
def test_cline_hub_stop_requires_verified_native_running_boolean_F1567(leg, running):
    leg.control['hub_running'] = running
    asyncio.run(leg.run())
    assert 'hub-status' in stages(leg) and 'hub-stop' not in stages(leg)


def test_cline_actual_context_read_write_are_off_loop_F1461(leg, monkeypatch):
    main_thread = threading.get_ident()
    calls = []
    original_read, original_write = leg.env.read_file, cli_container.write_container_file
    leg.env.files['/workspace/AGENTS.md'] = 'user-owned context\n'

    def read(path, **kwargs):
        assert threading.get_ident() != main_thread
        calls.append(('read', path))
        return original_read(path, **kwargs)

    def write(env, path, body, **kwargs):
        assert threading.get_ident() != main_thread
        calls.append(('write', path))
        return original_write(env, path, body, **kwargs)

    monkeypatch.setattr(leg.env, 'read_file', read)
    monkeypatch.setattr(cli_container, 'write_container_file', write)
    monkeypatch.setattr(cli_container, 'inject_context_file', leg.real_inject)
    asyncio.run(leg.run(text='  native context\n'))
    assert calls == [('read', '/workspace/AGENTS.md'), ('write', '/workspace/AGENTS.md')] * 2
    assert leg.env.files['/workspace/AGENTS.md'] == 'user-owned context\n'


@pytest.mark.parametrize('stage', ['inject', 'bind'])
def test_cline_cancelled_setup_worker_settles_before_bridge_and_policy_cleanup_F1461(leg, monkeypatch, stage):
    started, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    leg.env.files['/workspace/AGENTS.md'] = 'user-owned context\n'
    original_read, original_write = leg.env.read_file, cli_container.write_container_file

    def hold():
        assert threading.get_ident() != main_thread
        leg.events.append('worker-start')
        started.set()
        assert release.wait(3), 'bounded setup worker was not released'

    if stage == 'inject':
        def read(path, **kwargs):
            if not release.is_set():
                hold()
            return original_read(path, **kwargs)

        def write(env, path, body, **kwargs):
            original_write(env, path, body, **kwargs)
            if 'worker-done' not in leg.events:
                leg.events.append('worker-done')

        monkeypatch.setattr(leg.env, 'read_file', read)
        monkeypatch.setattr(cli_container, 'write_container_file', write)
        monkeypatch.setattr(cli_container, 'inject_context_file', leg.real_inject)
    else:
        def policy(env, level, **kwargs):
            if kwargs.get('mcp_servers'):
                hold()
                result = leg.policy(env, level, **kwargs)
                leg.events.append('worker-done')
                return result
            else:
                return leg.policy(env, level, **kwargs)

        monkeypatch.setattr(cline, 'apply_commands_policy', policy)

    async def run():
        task = asyncio.create_task(leg.run(text='native context'))
        try:
            assert await asyncio.to_thread(started.wait, 2), 'setup did not enter its worker'
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert 'remove' not in leg.events and 'bridge-close' not in leg.events
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    order = stages(leg)
    assert order.index('worker-done') < order.index('remove')
    if stage == 'bind':
        assert order.index('worker-done') < order.index('bridge-close') < order.index('remove')
    else:
        assert 'bridge-enter' not in order
        assert leg.env.files['/workspace/AGENTS.md'] == 'user-owned context\n'
    assert not leg.launches and not leg.ready
