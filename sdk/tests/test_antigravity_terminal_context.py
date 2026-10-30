# SPDX-License-Identifier: MIT
"""F1461/F1475/F1554/F1567: actual Antigravity entry retains native store/cleanup."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import threading
from types import SimpleNamespace

import gen
import pytest

import _agent_shared as engine
from _agent_shared import cli_container, harness_homes
from _agent_shared.harnesses.antigravity import backend as agy
from tests._commands_matrix import recording_runtime  # noqa: F401

_ACTUAL_LEG_BRIDGE = cli_container.leg_bridge


class NativeFailure(Exception):
    pass


@pytest.fixture
def leg(recording_runtime, monkeypatch):  # noqa: F811
    events, ready, launches, exports, logs = [], [], [], [], []
    control = {'failure': '', 'seam': True, 'thread_check': ''}
    main_thread = threading.get_ident()
    originals = {name: getattr(agy, name) for name in ['write_settings', 'conversation_ids', 'write_mcp_config', 'write_context_seam']}
    old_inject, old_remove = cli_container.inject_context_file, cli_container.remove_context_file
    def fail(name):
        if control['failure'] == name:
            raise NativeFailure('native-' + name)
    def event(name):
        events.append(name)
        if control['thread_check'] == name:
            assert threading.get_ident() != main_thread, 'blocking native terminal setup ran on event loop thread'
        fail(name)
    def settings(*args, **kwargs):
        event('settings')
        return originals['write_settings'](*args, **kwargs)
    def snapshot(home):
        event('snapshot')
        return originals['conversation_ids'](home)
    def mcp(env, url):
        assert env is recording_runtime and url == "http://host.docker.internal:9876/owned-capability"
        event('mcp')
        return originals['write_mcp_config'](env, url)
    def seam(env, text):
        assert env is recording_runtime and text == 'native context'
        event('seam')
        originals['write_context_seam'](env, text)
        return control['seam']
    def inject(env, path, text):
        assert env is recording_runtime and path == '/workspace/GEMINI.md' and text == 'native context'
        event('inject')
        return old_inject(env, path, text)
    def export(home, **kwargs):
        assert threading.get_ident() != main_thread
        event('export')
        exports.append((home, kwargs))
    def remove(env, path):
        assert env is recording_runtime and path == '/workspace/GEMINI.md'
        assert threading.get_ident() != main_thread
        event('remove')
        return old_remove(env, path)
    @asynccontextmanager
    async def own_bridge(**kwargs):
        events.append('bridge-open')
        try:
            yield SimpleNamespace(url_container='http://host.docker.internal:9876/owned-capability')
        finally:
            events.append('bridge-close')
    @asynccontextmanager
    async def terminal(container, argv, **kwargs):
        events.append('register')
        launches.append((container, argv, kwargs))
        fail('register')
        try:
            yield {'terminal_id': 'own-agy-terminal'}
        finally:
            events.append('terminal-close')
    @asynccontextmanager
    async def live(**kwargs):
        event('live')
        try:
            yield
        finally:
            events.append('live-close')
    async def wait(**kwargs):
        assert kwargs == {'terminal_id': 'own-agy-terminal', 'on_action': None}
        event('wait')
        if control['failure'] == 'cancel':
            raise asyncio.CancelledError
    def on_ready(sid, **kwargs):
        ready.append((sid, kwargs))
        event('ready')
    monkeypatch.setattr(agy, 'write_settings', settings)
    monkeypatch.setattr(agy, 'conversation_ids', snapshot)
    monkeypatch.setattr(agy, 'write_mcp_config', mcp)
    monkeypatch.setattr(agy, 'write_context_seam', seam)
    monkeypatch.setattr(agy, '_finalize_antigravity_terminal', export)
    monkeypatch.setattr(agy, '_secret_log', logs.append)
    monkeypatch.setattr(cli_container, 'inject_context_file', inject)
    monkeypatch.setattr(cli_container, 'remove_context_file', remove)
    monkeypatch.setattr(cli_container, 'leg_bridge', own_bridge)
    monkeypatch.setattr(gen, 'serve_terminal', terminal)
    monkeypatch.setattr(gen, 'live_files', live)
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    home = Path(harness_homes.harness_home('antigravity'))
    old = Path(agy.conversation_db_path(str(home), 'earlier-conversation'))
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b'earlier-store-record')
    recording_runtime.files['/workspace/GEMINI.md'] = 'user context\n'
    async def run(resume='ignored-old-thread', model='default'):
        cfg = {'backend': 'antigravity', 'auth': 'api_key', 'provider': 'gemini',
            'model': model, 'commands': 'ask', 'websearch': False, 'enable_file_tools': True,
            'runtime': 'docker', 'system_prompt': 'original'}
        await engine.surface_runner('antigravity', 'terminal')(cfg, pointer='own.terminal',
            extra_system='native context', resume_harness_session_id=resume,
            first_message='ignored-opening-message', on_ready=on_ready)
        assert cfg['system_prompt'] == 'original'
    return SimpleNamespace(run=run, control=control, events=events, ready=ready, launches=launches,
        exports=exports, logs=logs, home=str(home), env=recording_runtime,
        actual_mcp=originals["write_mcp_config"])


@pytest.mark.parametrize('resume', ['', 'ignored-old-thread'])
@pytest.mark.parametrize('model', ['default', 'gemini-3.5-flash-lite'])
@pytest.mark.parametrize('seam', [False, True])
def test_antigravity_actual_entry_preserves_named_conversation_carried_launch_and_cleanup_F1554_F1567(leg, resume, model, seam):
    leg.control['seam'] = seam
    asyncio.run(leg.run(resume, model))
    assert leg.events.count('settings') == 1 and len(leg.env.ensures) == 1
    container, argv, kwargs = leg.launches[0]
    assert container == leg.env.container_id
    assert argv == ['agy', *(['--model', 'collimate:model:' + model] if model != 'default' else [])]
    assert kwargs['env'] == {'TERM': 'xterm-256color', 'GEMINI_API_KEY': 'matrix-fake-key'}
    assert kwargs['name'] == 'Agent · Antigravity' and kwargs['label'] == f'Antigravity · {model}'
    assert kwargs['cwd'] == '/workspace' and kwargs['pointer'] == 'own.terminal' and kwargs['keep_alive'] is True
    assert kwargs['buttons'] == []
    assert leg.ready == [('', {})] and leg.exports == [(leg.home, {'before': frozenset({'earlier-conversation'})})]
    assert leg.events.index('snapshot') < leg.events.index('mcp') < leg.events.index('seam') < leg.events.index('register')
    assert leg.events.index('live-close') < leg.events.index('terminal-close') < leg.events.index('export')
    assert ('inject' in leg.events) is (not seam) and ('remove' in leg.events) is (not seam)
    assert leg.env.files['/workspace/GEMINI.md'] == 'user context\n'
    assert leg.events.index('bridge-open') < leg.events.index('snapshot')
    assert leg.events.index('export') < leg.events.index('bridge-close')


@pytest.mark.parametrize('failure', ['settings', 'snapshot', 'mcp', 'seam', 'inject', 'register', 'ready', 'live', 'wait', 'cancel', 'export', 'remove'])
def test_antigravity_actual_entry_preserves_cleanup_arm_and_error_order_F1475_F1567(leg, failure):
    leg.control.update(failure=failure, seam=False)
    if failure in ('export', 'remove'):
        asyncio.run(leg.run())
    else:
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else NativeFailure):
            asyncio.run(leg.run())
    assert leg.events.count('export') == (0 if failure in ('settings', 'snapshot') else 1)
    assert leg.events.count('remove') == (0 if failure in ('settings', 'snapshot') else 1)
    if failure in ('settings', 'snapshot', 'mcp', 'seam', 'inject'):
        assert leg.launches == [] and leg.ready == []
    if failure in ('ready', 'live', 'wait', 'cancel'):
        assert 'terminal-close' in leg.events
    if failure == 'export':
        assert leg.logs == ['antigravity teardown failed: native-export']
        assert leg.events[-2:] == ['remove', 'bridge-close']


@pytest.mark.parametrize('operation', ['snapshot', 'mcp', 'seam', 'inject'])
def test_antigravity_actual_entry_offloads_native_terminal_setup_F1461(leg, operation):
    leg.control.update(seam=False, thread_check=operation)
    asyncio.run(leg.run())
    assert leg.events.count(operation) == 1


def test_antigravity_actual_common_entry_stops_real_bridge_thread_after_native_cleanup_F1475(leg, monkeypatch):
    from _agent_shared import mcp_bridge
    bridges = []
    actual_start = mcp_bridge.start_bridge
    def start(**kwargs):
        bridge = actual_start(**kwargs)
        assert bridge is not None and bridge._thread.is_alive()
        bridges.append(bridge)
        return bridge
    monkeypatch.setattr(mcp_bridge, '_docker_gateway_host', lambda: '127.0.0.1')
    monkeypatch.setattr(mcp_bridge, 'start_bridge', start)
    monkeypatch.setattr(cli_container, 'leg_bridge', _ACTUAL_LEG_BRIDGE)
    def write_actual(env, url):
        assert env is leg.env and len(bridges) == 1 and url == bridges[0].url_container
        assert bridges[0]._thread.is_alive()
        leg.actual_mcp(env, url)
    monkeypatch.setattr(agy, 'write_mcp_config', write_actual)
    asyncio.run(leg.run())
    assert len(bridges) == 1 and bridges[0]._stopped and not bridges[0]._thread.is_alive()
    import json
    assert json.loads(leg.env.files[agy.MCP_CONFIG_PATH]) == {'mcpServers': {
        'collimate': {'disabled': False, 'serverUrl': bridges[0].url_container}}}
    assert leg.events.index('terminal-close') < leg.events.index('export')


def test_antigravity_actual_common_bridge_failure_clears_stale_native_config_F1475(leg, monkeypatch):
    from _agent_shared import mcp_bridge
    def failed(**kwargs):
        raise OSError('bounded unavailable native bridge')
    monkeypatch.setattr(mcp_bridge, 'start_bridge', failed)
    monkeypatch.setattr(cli_container, 'leg_bridge', _ACTUAL_LEG_BRIDGE)
    leg.env.files[agy.MCP_CONFIG_PATH] = '{"mcpServers":{"stale":{"serverUrl":"http://foreign"}}}'
    def write_actual(env, url):
        assert url is None
        leg.actual_mcp(env, url)
    monkeypatch.setattr(agy, 'write_mcp_config', write_actual)
    asyncio.run(leg.run())
    import json
    assert json.loads(leg.env.files[agy.MCP_CONFIG_PATH]) == {'mcpServers': {}}
    assert len(leg.launches) == 1 and len(leg.exports) == 1


@pytest.mark.parametrize('operation', ['mcp', 'seam', 'inject', 'teardown'])
@pytest.mark.parametrize('worker_error', [False, True])
def test_antigravity_actual_native_writer_settles_before_owner_cleanup_on_repeated_cancel_F1461_F1475(leg, monkeypatch, operation, worker_error):
    """Hold the real helper at its actual environment write, never a fake binder."""
    import base64

    leg.control['seam'] = operation not in ('inject', 'teardown')
    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    actual_exec = leg.env.exec
    path = {'mcp': agy.MCP_CONFIG_PATH, 'seam': agy.CONTEXT_SEAM_PATH,
            'inject': '/workspace/GEMINI.md', 'teardown': '/workspace/GEMINI.md'}[operation]
    injected_text = base64.b64encode(('user context\n\n' + cli_container.CONTEXT_BEGIN
        + '\nnative context\n' + cli_container.CONTEXT_END + '\n').encode()).decode()

    def held_exec(argv, **kwargs):
        script = argv[-1]
        target = path in script
        if operation == 'teardown':
            target = target and injected_text not in script
        if target and not entered.is_set():
            leg.events.append('actual-writer-held')
            entered.set()
            try:
                assert release.wait(5), 'bounded writer was not released'
                result = actual_exec(argv, **kwargs)
                leg.events.append('actual-write-landed')
                if worker_error:
                    raise NativeFailure('actual-native-writer-error')
                return result
            finally:
                leg.events.append('actual-writer-settled')
                settled.set()
        return actual_exec(argv, **kwargs)

    monkeypatch.setattr(leg.env, 'exec', held_exec)

    async def exercise():
        task = asyncio.create_task(leg.run())
        observation = {}
        outcome = None
        try:
            assert await asyncio.to_thread(entered.wait, 2), 'actual write boundary not reached'
            assert not settled.is_set()
            for _ in range(2):
                task.cancel()
                # Let cancellation propagate while the actual writer remains held.
                await asyncio.wait({task}, timeout=0.03)
            observation = {'returned': task.done(), 'bridge_closed': 'bridge-close' in leg.events,
                           'exported': 'export' in leg.events, 'removed': 'remove' in leg.events}
        finally:
            release.set()
            try:
                await task
            except BaseException as exc:
                outcome = exc
            assert await asyncio.to_thread(settled.wait, 2)
        assert not observation['returned'] and not observation['bridge_closed'], observation
        if operation != 'teardown':
            assert not observation['exported'] and not observation['removed'], observation
            assert leg.launches == []
        expected = NativeFailure if worker_error and operation != 'teardown' else asyncio.CancelledError
        assert isinstance(outcome, expected), repr(outcome)
        assert leg.events.index('actual-writer-settled') < leg.events.index('bridge-close')
        if operation != 'teardown':
            assert leg.events.index('actual-writer-settled') < leg.events.index('export')
        if operation == 'seam' and not worker_error:
            assert 'remove' not in leg.events, 'settled native seam result must own cleanup choice'
        if not worker_error:
            assert leg.env.files['/workspace/GEMINI.md'] == 'user context\n'

    asyncio.run(exercise())
