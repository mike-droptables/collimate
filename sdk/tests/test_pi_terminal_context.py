# SPDX-License-Identifier: MIT
"""F1554/F1567: Pi's actual entry retains fresh-session carrier ownership."""
import asyncio
from contextlib import asynccontextmanager
import json
import threading
from types import SimpleNamespace

import pytest
import gen

from tests.test_pi_prepare import prepared_runtime, ctx, native  # noqa: F401
from _agent_shared import cli_container
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

from _agent_shared.harnesses.pi import backend as pi
import _agent_shared as engine


async def _no_credentials(adapter, *, provider, plan):
    """The pipeline's credential step for a test whose renderer is a fake."""
    return _pipeline.Credentials(provider, plan, key="" if plan else "sk-test")


class NativeFailure(Exception):
    pass


@pytest.fixture
def leg(prepared_runtime, monkeypatch):  # noqa: F811
    events, prepared_values, launches, ready = [], [], [], []
    control = {'failure': ''}
    original_prepare, original_bind = pi.prepare, pi.bind_native_bridge
    def fail(stage):
        if control['failure'] == stage:
            raise NativeFailure('native-' + stage)
    async def prepare(config, *, leg):
        events.append(('prepare', dict(config), leg))
        fail('prepare')
        value = await original_prepare(config, leg=leg)
        prepared_values.append(value)
        return value
    async def bind(value):
        if value is None:
            events.append('clear-native-route')
        else:
            events.append('bind-native-route')
            fail('bind')
        await original_bind(value)
    @asynccontextmanager
    async def bridge(**kwargs):
        events.append(('bridge', kwargs))
        fail('bridge')
        try:
            yield SimpleNamespace(url_container='http://own/pi/mcp')
        finally:
            events.append('bridge-close')
    @asynccontextmanager
    async def terminal(container, argv, **kwargs):
        events.append('register')
        launches.append((container, argv, kwargs))
        assert json.loads(native('collimate-mcp.json').read_bytes()) == {'url': 'http://own/pi/mcp'}
        assert native('collimate-mcp.json').stat().st_mode & 0o777 == 0o600
        fail('register')
        try:
            yield {'terminal_id': 'own-pi-terminal'}
        finally:
            events.append('terminal-close')
    @asynccontextmanager
    async def live(**kwargs):
        events.append('live-open')
        fail('live')
        try:
            yield
        finally:
            events.append('live-close')
    async def wait(**kwargs):
        assert kwargs == {'terminal_id': 'own-pi-terminal', 'on_action': None}
        events.append('wait')
        fail('wait')
        if control['failure'] == 'cancel':
            raise asyncio.CancelledError
    def on_ready(sid, **kwargs):
        events.append('ready')
        ready.append((sid, kwargs))
        fail('ready')
    monkeypatch.setattr(pi, 'prepare', prepare)
    monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
    monkeypatch.setattr(pi, 'bind_native_bridge', bind)
    monkeypatch.setattr(cli_container, 'leg_bridge', bridge)
    monkeypatch.setattr(gen, 'serve_terminal', terminal)
    monkeypatch.setattr(gen, 'live_files', live)
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    monkeypatch.setattr(prepared_runtime, 'close', lambda: events.append('environment-close'), raising=False)
    async def run(*, commands='allow', resume='', text='exact selected native context', label=None):
        config = {'backend': 'pi', **ctx(commands=commands)}
        before = dict(config)
        options = {} if label is None else {'label': label}
        await engine.surface_runner('pi', 'terminal')(config, pointer='own.pi.terminal',
            extra_system=text, resume_harness_session_id=resume,
            first_message='ignored-first-message', on_ready=on_ready, **options)
        assert config == before
    return SimpleNamespace(run=run, events=events, prepared=prepared_values,
        launches=launches, ready=ready, control=control, env=prepared_runtime)


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
@pytest.mark.parametrize('resume', ['', 'ignored-native-session'])
def test_pi_actual_entry_owns_prepared_carrier_and_always_fresh_session_F1554_F1567(leg, commands, resume):
    asyncio.run(leg.run(commands=commands, resume=resume))
    assert len(leg.prepared) == 1
    prepared = leg.prepared[0]
    assert leg.events[0][1]['system_prompt'] == 'exact selected native context'
    assert leg.events[0][2] == 'terminal'
    bridge = next(event for event in leg.events if isinstance(event, tuple) and event[0] == 'bridge')
    assert leg.events.index('clear-native-route') < leg.events.index(bridge) < leg.events.index('bind-native-route') < leg.events.index('register')
    names = {row[0] for row in bridge[1]['extra_entries'] or []}
    assert ('test_generator' in names) is (commands != 'deny')
    container, argv, kwargs = leg.launches[0]
    assert container == leg.env.container_id and argv == list(prepared.argv)
    assert 'ignored-native-session' not in argv and 'ignored-first-message' not in argv
    assert kwargs['env'] == {'TERM': 'xterm-256color', **prepared.env}
    assert kwargs['env']['COLLIMATE_COMMANDS'] == commands
    assert not any(name.endswith('API_KEY') for name in kwargs['env'])
    assert kwargs['name'] == 'Agent · pi' and kwargs['label'] == 'pi · ' + prepared.model
    assert kwargs['keep_alive'] is True and kwargs['cwd'] == '/workspace' and kwargs['pointer'] == 'own.pi.terminal'
    assert kwargs['buttons'] == []
    assert leg.ready == [('', {})]
    assert leg.events.index('ready') < leg.events.index('live-open') < leg.events.index('wait') < leg.events.index('live-close') < leg.events.index('terminal-close') < leg.events.index('bridge-close')
    assert 'environment-close' not in leg.events


def test_pi_actual_entry_preserves_explicit_terminal_label_and_empty_context_F1554(leg):
    asyncio.run(leg.run(label='Own Pi terminal', text=''))
    assert leg.launches[0][2]['name'] == 'Own Pi terminal'
    assert '--append-system-prompt' not in leg.launches[0][1]


@pytest.mark.parametrize('failure', ['prepare', 'bridge', 'bind', 'register', 'ready', 'live', 'wait', 'cancel'])
def test_pi_actual_entry_failure_propagates_with_own_bridge_lifetime_F1554_F1567(leg, failure):
    leg.control['failure'] = failure
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else NativeFailure):
        asyncio.run(leg.run())
    if failure in ('prepare', 'bridge'):
        assert 'bridge-close' not in leg.events
    else:
        assert leg.events[-1] == 'bridge-close'
    if failure in ('prepare', 'bridge', 'bind'):
        assert leg.launches == [] and leg.ready == []
    assert 'environment-close' not in leg.events


@pytest.mark.parametrize('cancellations', [1, 2])
@pytest.mark.parametrize('writer_fails', [False, True])
def test_pi_actual_bind_settles_writer_before_bridge_cleanup_F1461_F1554_F1567(
        leg, monkeypatch, cancellations, writer_fails):
    started, release = threading.Event(), threading.Event()
    original_write = pi.write_native_file

    def write(home, path, content):
        if content is not None and path == 'agent/collimate-mcp.json':
            leg.events.append('write-started')
            started.set()
            assert release.wait(5), 'owned writer was not released'
            try:
                if writer_fails:
                    raise NativeFailure('native-write')
                return original_write(home, path, content)
            finally:
                leg.events.append('write-settled')
        return original_write(home, path, content)

    monkeypatch.setattr(pi, 'write_native_file', write)

    async def run():
        task = asyncio.create_task(leg.run())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            for _ in range(cancellations):
                task.cancel()
                # Give cancellation delivery and cleanup their actual loop turns.
                for _ in range(3):
                    await asyncio.sleep(0)
                assert not task.done(), 'cancellation escaped while native writer was held'
                assert 'bridge-close' not in leg.events
            release.set()
            with pytest.raises(NativeFailure if writer_fails else asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert leg.events.index('write-settled') < leg.events.index('bridge-close')
        assert leg.launches == [] and leg.ready == []
        assert 'environment-close' not in leg.events
        if not writer_fails:
            assert json.loads(native('collimate-mcp.json').read_bytes()) == {'url': 'http://own/pi/mcp'}

    asyncio.run(run())
