# SPDX-License-Identifier: MIT
"""F1554/F1567: Qwen selected entry owns fresh launch and native bridge lifetime."""
import asyncio
import copy
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace

import pytest
import gen

import _agent_shared as engine
from _agent_shared import cli_container
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

from _agent_shared.harnesses.qwen_code import backend as qwen
from tests._commands_matrix import recording_runtime  # noqa: F401


async def _no_credentials(adapter, *, provider, plan):
    """The pipeline's credential step for a test whose renderer is a fake."""
    return _pipeline.Credentials(provider, plan, key="" if plan else "sk-test")

REAL_BRIDGE = cli_container.leg_bridge


class NativeFailure(Exception):
    pass


@pytest.fixture
def leg(recording_runtime, monkeypatch):  # noqa: F811
    events, launches, ready, values, rows_seen, settings_seen = [], [], [], [], [], []
    control = {'failure': '', 'commands': 'ask', 'bridge': True, 'preparing': False}
    main_thread = threading.get_ident()
    original_prepare, original_settings = qwen.prepare, qwen.write_system_settings
    original_policy = qwen.system_settings
    def fail(stage):
        if control['failure'] == stage:
            raise NativeFailure('native-' + stage)
    def policy(*args, **kwargs):
        # The native builder remains real; an existing unrelated server must survive binding.
        kwargs['mcp_servers'] = {'other': {'httpUrl': 'http://other.invalid/mcp'}}
        return original_policy(*args, **kwargs)
    async def prepare(cfg, *, leg):
        events.append(('prepare', dict(cfg), leg))
        fail('prepare')
        control['preparing'] = True
        try:
            value = await original_prepare({**cfg, 'commands': control['commands']}, leg=leg)
        finally:
            control['preparing'] = False
        values.append(value)
        return value
    def write(env, settings):
        assert env is recording_runtime and threading.get_ident() != main_thread
        settings_seen.append(copy.deepcopy(settings))
        if not control['preparing']:
            events.append('bind')
            fail('bind')
        return original_settings(env, settings)
    @asynccontextmanager
    async def bridge(**kwargs):
        rows_seen.append(kwargs['extra_entries'] or [])
        events.append('bridge-open')
        fail('bridge')
        try:
            yield SimpleNamespace(url_container='http://own/leg/mcp') if control['bridge'] else None
        finally:
            events.append('bridge-close')
    @asynccontextmanager
    async def terminal(container, argv, **kwargs):
        events.append('register')
        launches.append((container, argv, kwargs))
        fail('register')
        try:
            yield {'terminal_id': 'own-qwen-terminal'}
        finally:
            events.append('terminal-close')
    @asynccontextmanager
    async def live(**kwargs):
        events.append('live')
        fail('live')
        try:
            yield
        finally:
            events.append('live-close')
    async def wait(**kwargs):
        assert kwargs == {'terminal_id': 'own-qwen-terminal', 'on_action': None}
        events.append('wait')
        fail('wait')
        if control['failure'] == 'cancel':
            raise asyncio.CancelledError
    def on_ready(sid, **kwargs):
        ready.append((sid, kwargs))
        fail('ready')
    monkeypatch.setattr(qwen, 'prepare', prepare)
    monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
    monkeypatch.setattr(qwen, 'system_settings', policy)
    monkeypatch.setattr(qwen, 'write_system_settings', write)
    monkeypatch.setattr(cli_container, 'leg_bridge', bridge)
    monkeypatch.setattr(qwen, '_secret_log', lambda *args: None)
    monkeypatch.setattr(gen, 'serve_terminal', terminal)
    monkeypatch.setattr(gen, 'live_files', live)
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    async def run(resume='old-thread'):
        cfg = {'backend': 'qwen-code', 'auth': 'api_key', 'provider': 'openrouter',
            'model': 'deepseek/deepseek-v4-flash', 'commands': 'allow', 'websearch': False,
            'runtime': 'docker', 'system_prompt': 'original'}
        await engine.surface_runner('qwen-code', 'terminal')(cfg, pointer='own.terminal',
            extra_system='native context', resume_harness_session_id=resume,
            first_message='ignored-message', on_ready=on_ready)
        assert cfg['system_prompt'] == 'original' and cfg['commands'] == 'allow'
    return SimpleNamespace(run=run, control=control, events=events, launches=launches,
        ready=ready, values=values, rows=rows_seen, settings=settings_seen)


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
@pytest.mark.parametrize('bridge', [False, True])
@pytest.mark.parametrize('resume', ['', 'old-thread'])
def test_qwen_actual_entry_preserves_native_policy_effective_grant_and_fresh_session_F1554_F1567(leg, commands, bridge, resume):
    leg.control.update(commands=commands, bridge=bridge)
    asyncio.run(leg.run(resume))
    assert len(leg.values) == 1
    prepared = leg.values[0]
    assert leg.events[0][1]['system_prompt'] == 'native context' and leg.events[0][2] == 'terminal'
    assert len(leg.rows) == 1
    assert any(row[0] == 'test_generator' for row in leg.rows[0]) is (commands != 'deny')
    assert len(leg.settings) == 2
    expected = copy.deepcopy(prepared.commands.native)
    if bridge:
        expected['mcpServers']['collimate'] = {'httpUrl': 'http://own/leg/mcp'}
    assert leg.settings[-1] == expected
    assert prepared.commands.native == leg.settings[0]
    assert prepared.commands.native['mcpServers'] == {'other': {'httpUrl': 'http://other.invalid/mcp'}}
    container, argv, kwargs = leg.launches[0]
    assert container == prepared.env_obj.container_id and argv == list(prepared.argv)
    assert resume not in argv or not resume
    assert 'ignored-message' not in argv
    assert kwargs['env'] == {'TERM': 'xterm-256color', **prepared.env}
    assert kwargs['label'] == 'Qwen Code · deepseek/deepseek-v4-flash'
    assert kwargs['name'] == 'Agent · Qwen Code' and kwargs['pointer'] == 'own.terminal'
    assert kwargs['cwd'] == '/workspace' and kwargs['keep_alive'] is True
    assert kwargs['buttons'] == []
    assert leg.ready == [('', {})]
    assert leg.events.index('bridge-open') < leg.events.index('bind') < leg.events.index('register')
    assert leg.events.index('live-close') < leg.events.index('terminal-close') < leg.events.index('bridge-close')


@pytest.mark.parametrize('failure', ['prepare', 'bridge', 'bind', 'register', 'ready', 'live', 'wait', 'cancel'])
def test_qwen_actual_entry_keeps_native_acquisition_and_cleanup_boundaries_F1554_F1567(leg, failure):
    leg.control['failure'] = failure
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else NativeFailure):
        asyncio.run(leg.run())
    if failure == 'prepare':
        assert leg.values == [] and 'bridge-open' not in leg.events
    elif failure == 'bridge':
        assert 'bind' not in leg.events and 'bridge-close' not in leg.events
    else:
        assert leg.events[-1] == 'bridge-close'
    if failure in ('prepare', 'bridge', 'bind'):
        assert leg.launches == [] and leg.ready == []
    if failure in ('ready', 'live', 'wait', 'cancel'):
        assert 'terminal-close' in leg.events
    if failure in ('wait', 'cancel'):
        assert leg.events.index('live-close') < leg.events.index('terminal-close')


@pytest.mark.parametrize('ending', ['normal', 'bind', 'cancel'])
def test_qwen_actual_leg_bridge_thread_spans_native_entry_then_stops_F1554_F1567(leg, monkeypatch, ending):
    from _agent_shared import mcp_bridge
    monkeypatch.setattr(cli_container, 'leg_bridge', REAL_BRIDGE)
    monkeypatch.setattr(mcp_bridge, '_docker_gateway_host', lambda: '127.0.0.1')
    bridges = []
    original_start = mcp_bridge.start_bridge
    original_write = qwen.write_system_settings
    def start(**kwargs):
        bridge = original_start(**kwargs)
        assert bridge is not None
        bridges.append(bridge)
        return bridge
    def write(env, settings):
        if 'collimate' in settings.get('mcpServers', {}):
            assert bridges[-1]._thread.is_alive()
            assert settings['mcpServers']['collimate'] == {'httpUrl': bridges[-1].url_container}
        return original_write(env, settings)
    async def wait(**kwargs):
        assert bridges[-1]._thread.is_alive()
        assert kwargs == {'terminal_id': 'own-qwen-terminal', 'on_action': None}
        if ending == 'cancel':
            raise asyncio.CancelledError
    monkeypatch.setattr(mcp_bridge, 'start_bridge', start)
    monkeypatch.setattr(qwen, 'write_system_settings', write)
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    leg.control['failure'] = 'bind' if ending == 'bind' else ''
    async def run():
        for _ in range(2):
            if ending == 'normal':
                await leg.run()
            else:
                with pytest.raises(NativeFailure if ending == 'bind' else asyncio.CancelledError):
                    await leg.run()
            assert not bridges[-1]._thread.is_alive() and bridges[-1]._stopped is True
    asyncio.run(run())
    assert len(bridges) == 2 and bridges[0].url_container != bridges[1].url_container
