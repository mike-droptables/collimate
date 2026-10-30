# SPDX-License-Identifier: MIT
"""F1554/F1567: selected shared entry and real native-context resource ordering."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace

import pytest

import _agent_shared as engine
from _agent_shared import cli_container as cc, terminal_runner as runner
from _agent_shared.harnesses import CommandsPolicy, PreparedHarness  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

from _agent_shared.harnesses.opencode import backend as oc


async def _no_credentials(adapter, *, provider, plan):
    """The pipeline's credential step for a test whose renderer is a fake."""
    return _pipeline.Credentials(provider, plan, key="" if plan else "sk-test")

REAL_WAITER = engine.serve_tty_until
REAL_LEG_BRIDGE = cc.leg_bridge


class LegFailure(Exception):
    pass


@pytest.fixture
def recorded_leg(monkeypatch):
    events, ready, launches, preparations = [], [], [], []
    worker = {'bind_thread': None, 'export_thread': None}
    control = {'failure': '', 'before': frozenset({'older'}), 'wait': None, 'on_ready': None}
    class Env:
        container_id = 'prepared-container'
    env = Env()
    prepared = PreparedHarness(CommandsPolicy('deny', {}), None, env,
        ('actual-opencode', '--native-option'), {'NATIVE_ENV': 'prepared'}, 'native/model')
    def fail(stage):
        if control['failure'] == stage:
            raise LegFailure(stage)
    async def prepare(ctx, *, leg):
        events.append('prepare')
        preparations.append((dict(ctx), leg))
        fail('prepare')
        return prepared
    async def update(**kwargs):
        events.append(('update', kwargs))
    @asynccontextmanager
    async def bridge(**kwargs):
        events.append(('bridge-open', kwargs))
        fail('bridge')
        try:
            yield SimpleNamespace(url_container='http://own/secret/mcp')
        finally:
            events.append('bridge-close')
    def bind(environment, url):
        assert environment is env and url == 'http://own/secret/mcp'
        worker['bind_thread'] = threading.get_ident()
        events.append('bind')
        fail('bind')
    def listed(environment):
        assert environment is env
        events.append('snapshot')
        fail('snapshot')
        return control['before']
    def finalize(environment, **kwargs):
        assert environment is env
        worker['export_thread'] = threading.get_ident()
        events.append(('finalize', kwargs))
        fail('finalize')
    @asynccontextmanager
    async def terminal(container_id, argv, **kwargs):
        events.append('terminal-enter')
        launches.append((container_id, argv, kwargs))
        fail('register')
        try:
            yield {'terminal_id': 'registered-terminal'}
        finally:
            events.append('terminal-close')
    @asynccontextmanager
    async def live(**kwargs):
        events.append(('live-enter', kwargs))
        fail('live')
        try:
            yield
        finally:
            events.append('live-close')
    async def wait(**kwargs):
        assert kwargs == {'terminal_id': 'registered-terminal', 'on_action': None}
        events.append('wait')
        fail('wait')
        if control['wait']:
            await control['wait']()
    def on_ready(sid, **kwargs):
        events.append('ready')
        ready.append((sid, kwargs))
        fail('ready')
        if control['on_ready']:
            control['on_ready'](sid, **kwargs)
    monkeypatch.setattr(oc, 'prepare', prepare)
    monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
    monkeypatch.setattr(oc, '_write_terminal_bridge', bind)
    monkeypatch.setattr(oc, '_listed_before', listed)
    monkeypatch.setattr(oc, '_finalize', finalize)
    monkeypatch.setattr(cc, 'leg_bridge', bridge)
    monkeypatch.setattr(runner.gen, 'commit', update)
    monkeypatch.setattr(runner.gen, 'phase', lambda *args: events.append(('phase', args)))
    monkeypatch.setattr(runner.gen, 'serve_terminal', terminal)
    monkeypatch.setattr(runner.gen, 'live_files', live)
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    async def run(**kwargs):
        cfg = {'backend': 'opencode', 'commands': 'allow', 'websearch': False, 'system_prompt': 'superseded'}
        await engine.surface_runner('opencode', 'terminal')(cfg, pointer='own.terminal',
            extra_system='actual-system', first_message='ignored-opening', on_ready=on_ready, **kwargs)
        assert cfg['system_prompt'] == 'superseded'
    return SimpleNamespace(events=events, ready=ready, launches=launches, preparations=preparations,
        control=control, worker=worker, prepared=prepared, run=run)


@pytest.mark.parametrize('resume,before', [('', None), ('', frozenset()), ('', frozenset({'older'})), ('own-resume', None)])
def test_actual_selected_entry_prepares_once_and_preserves_native_launch_F1554_F1567(recorded_leg, resume, before):
    leg = recorded_leg
    leg.control['before'] = before
    main_thread = threading.get_ident()
    asyncio.run(leg.run(resume_harness_session_id=resume))
    assert len(leg.preparations) == 1
    ctx, kind = leg.preparations[0]
    assert kind == 'terminal' and ctx['system_prompt'] == 'actual-system'
    assert ctx['commands'] == 'allow'  # Native preparation's effective deny is authoritative for tools.
    rows = next(x[1]['extra_entries'] for x in leg.events if isinstance(x, tuple) and x[0] == 'bridge-open') or []
    assert all(row[0] != 'test_generator' for row in rows)
    container, argv, values = leg.launches[0]
    assert container == 'prepared-container'
    assert argv == ['actual-opencode', '--native-option'] + (['-s', resume] if resume else [])
    assert values['env'] == {'TERM': 'xterm-256color', 'NATIVE_ENV': 'prepared'}
    assert values['label'] == 'OpenCode · native/model'
    assert values['name'] == 'Agent · OpenCode'
    assert values['pointer'] == 'own.terminal' and values['cwd'] == '/workspace' and values['keep_alive'] is True
    assert leg.ready == ([(resume, {})] if resume or before is None else [('', {'before': before})])
    assert ('snapshot' in leg.events) is (not bool(resume))
    finalize = next(x for x in leg.events if isinstance(x, tuple) and x[0] == 'finalize')
    assert finalize[1] == {'harness_session_id': resume, 'before': None if resume else before}
    assert leg.worker['bind_thread'] != main_thread and leg.worker['export_thread'] != main_thread
    assert leg.events.index('live-close') < leg.events.index('terminal-close') < leg.events.index(finalize) < leg.events.index('bridge-close')
    assert len([x for x in leg.events if isinstance(x, tuple) and x[0] == 'finalize']) == 1
    assert leg.events.count('ready') == 1 and leg.events.count('bridge-close') == 1


@pytest.mark.parametrize('failure', ['prepare', 'bridge', 'bind', 'snapshot', 'register', 'ready', 'live', 'wait', 'finalize'])
def test_resource_failures_preserve_exact_native_export_arm_and_unwind_F1554_F1567(recorded_leg, failure):
    leg = recorded_leg
    leg.control['failure'] = failure
    with pytest.raises(LegFailure, match=failure):
        asyncio.run(leg.run())
    finalizers = [x for x in leg.events if isinstance(x, tuple) and x[0] == 'finalize']
    assert len(finalizers) == (0 if failure in ('prepare', 'bridge', 'bind') else 1)
    assert leg.events.count('bridge-close') == (0 if failure in ('prepare', 'bridge') else 1)
    assert bool(leg.ready) is (failure in ('ready', 'live', 'wait', 'finalize'))
    if finalizers:
        assert leg.events.index(finalizers[0]) < leg.events.index('bridge-close')
    if failure in ('prepare', 'bridge', 'bind', 'snapshot'):
        assert leg.launches == []


@pytest.mark.parametrize('ending', ['ended', 'cancel'])
def test_real_selected_entry_ended_and_cancel_lifetimes_F1554_F1567(recorded_leg, ending):
    """The leg unwinds once its waiter returns (the program exited or the
    run stopped) and when it is cancelled."""
    leg = recorded_leg
    async def check():
        ended = asyncio.Event()
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await ended.wait()
        leg.control['wait'] = wait
        task = asyncio.create_task(leg.run())
        await entered.wait()
        if ending == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            ended.set()
            await task
    asyncio.run(check())
    assert leg.events[-1] == 'bridge-close'
    assert leg.events.count('live-close') == 1 and leg.events.count('terminal-close') == 1
    assert len([x for x in leg.events if isinstance(x, tuple) and x[0] == 'finalize']) == 1


@pytest.mark.parametrize('ending', ['program', 'stop'])
def test_shared_entry_uses_existing_waiter_for_program_exit_and_stop_F1567(recorded_leg, monkeypatch, ending):
    leg = recorded_leg
    monkeypatch.setattr(engine, 'serve_tty_until', REAL_WAITER)
    monkeypatch.setitem(runner.gen.__dict__, 'cancelled', ending == 'stop')
    seen = []
    def status(terminal_id):
        seen.append(terminal_id)
        return {'ended': True, 'exit_code': 0}
    monkeypatch.setattr(runner.gen, 'terminal_status', status)
    monkeypatch.setattr(engine, '_secret_log', lambda *args: None)
    asyncio.run(leg.run())
    assert seen == (['registered-terminal'] if ending == 'program' else [])
    assert leg.events[-1] == 'bridge-close'


def test_explicit_label_is_retained_F1554(recorded_leg):
    leg = recorded_leg
    asyncio.run(leg.run(label='Actual caller label'))
    assert leg.launches[0][2]['name'] == 'Actual caller label'
    assert leg.events[0] == ('update', {'name': 'Actual caller label'})


@pytest.mark.parametrize('stage', ['prepare', 'bind', 'finalize'])
def test_cancel_during_acquisition_or_export_retains_actual_worker_ownership_F1567(recorded_leg, monkeypatch, stage):
    leg = recorded_leg
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    async def check():
        if stage == 'prepare':
            gate = asyncio.Event()
            async def prepare(*args, **kwargs):
                entered.set()
                await gate.wait()
            monkeypatch.setattr(oc, 'prepare', prepare)
            monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
        else:
            def blocking(*args, **kwargs):
                entered.set()
                try:
                    assert release.wait(5), 'test-owned worker release deadline exceeded'
                finally:
                    finished.set()
            monkeypatch.setattr(oc, '_write_terminal_bridge' if stage == 'bind' else '_finalize', blocking)
        task = asyncio.create_task(leg.run())
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            if stage != 'prepare':
                assert not finished.is_set()  # Cancelling the await never kills its worker.
                assert leg.events[-1] == 'bridge-close'
            if stage == 'bind':
                assert not any(isinstance(x, tuple) and x[0] == 'finalize' for x in leg.events)
                assert leg.launches == []
            if stage == 'prepare':
                assert leg.launches == [] and 'bridge-close' not in leg.events
        finally:
            release.set()
        if stage != 'prepare':
            assert await asyncio.to_thread(finished.wait, 5)
    asyncio.run(check())


def test_private_facet_without_carrier_opens_no_listener_F1554(recorded_leg, monkeypatch):
    from dataclasses import replace
    from _agent_shared.harnesses import HARNESS
    leg = recorded_leg
    row = HARNESS['opencode']
    monkeypatch.setitem(HARNESS, 'opencode', replace(row, terminal=replace(row.terminal, bridge_enabled=False)))
    bound = []
    monkeypatch.setattr(oc, '_write_terminal_bridge', lambda env, url: bound.append(url))
    asyncio.run(leg.run())
    assert bound == [None]
    assert not any(isinstance(x, tuple) and x[0] == 'bridge-open' for x in leg.events)
    assert 'bridge-close' not in leg.events
    assert leg.events[-1][0] == 'finalize'


def test_native_auxiliary_action_is_forwarded_F1567(recorded_leg, monkeypatch):
    from dataclasses import replace
    leg = recorded_leg
    original_context = oc.terminal_context
    handled = []
    async def capture():
        handled.append('capture')
        monkeypatch.setitem(runner.gen.__dict__, 'cancelled', True)
    @asynccontextmanager
    async def context(*args):
        async with original_context(*args) as launch:
            yield replace(launch, auxiliary_handlers={'capture_chat': capture})
    monkeypatch.setattr(oc, 'terminal_context', context)
    monkeypatch.setattr(engine, 'serve_tty_until', REAL_WAITER)
    monkeypatch.setitem(runner.gen.__dict__, 'cancelled', False)
    monkeypatch.setattr(runner.gen, 'terminal_status', lambda _: {})
    requested = []
    async def action(*, action_id, timeout):
        requested.append(action_id)
        return {'action_id': action_id}
    monkeypatch.setattr(runner.gen, 'wait_for_action_event', action)
    asyncio.run(leg.run())
    assert requested == ['capture_chat'] and handled == ['capture']
    assert leg.events[-1] == 'bridge-close'


@pytest.mark.parametrize('ending', ['normal', 'bind-failure', 'wait-cancel'])
def test_actual_leg_bridge_thread_stops_after_selected_native_entry_F1554_F1567(recorded_leg, monkeypatch, host_free_run, ending):
    from _agent_shared import mcp_bridge
    leg = recorded_leg
    monkeypatch.setattr(cc, 'leg_bridge', REAL_LEG_BRIDGE)
    monkeypatch.setattr(mcp_bridge, '_docker_gateway_host', lambda: '127.0.0.1')
    original_start = mcp_bridge.start_bridge
    bridges = []
    def start(**kwargs):
        bridge = original_start(**kwargs)
        assert bridge is not None, 'offline installed MCP stack required'
        bridges.append(bridge)
        return bridge
    monkeypatch.setattr(mcp_bridge, 'start_bridge', start)
    def bind(env, url):
        assert env is leg.prepared.env_obj
        assert url == bridges[-1].url_container
        assert bridges[-1]._thread.is_alive()
        if ending == 'bind-failure':
            raise LegFailure('bind')
    monkeypatch.setattr(oc, '_write_terminal_bridge', bind)
    async def cancel():
        raise asyncio.CancelledError
    if ending == 'wait-cancel':
        leg.control['wait'] = cancel
    def finalize(env, **kwargs):
        assert bridges[-1]._thread.is_alive(), 'bridge must span the native export'
        leg.events.append(('finalize', kwargs))
    monkeypatch.setattr(oc, '_finalize', finalize)
    async def run():
        for _ in range(2):
            if ending == 'normal':
                await leg.run()
            else:
                with pytest.raises(LegFailure if ending == 'bind-failure' else asyncio.CancelledError):
                    await leg.run()
            assert not bridges[-1]._thread.is_alive(), 'stop-called alone is not termination'
            assert bridges[-1]._stopped is True
    asyncio.run(run())
    assert len(bridges) == 2
    assert bridges[0].url_container != bridges[1].url_container


def test_a_launch_away_from_its_locus_is_refused_before_registration(recorded_leg, monkeypatch):
    """opencode's CLI runs in its container: a native context that launched
    it on this machine is a bug the runner refuses before any PTY."""
    from dataclasses import replace
    original_context = oc.terminal_context

    @asynccontextmanager
    async def context(*args):
        async with original_context(*args) as launch:
            yield replace(launch, container_id=None)
    monkeypatch.setattr(oc, 'terminal_context', context)
    with pytest.raises(RuntimeError, match='launched in the host where its locus is container'):
        asyncio.run(recorded_leg.run())
    assert recorded_leg.launches == []


def test_the_launch_says_its_locus():
    launch = runner.TerminalLaunch(None, ('claude',), '/ws', {}, 'Claude', [], 'ready', '')
    assert launch.locus == 'host'
    assert runner.TerminalLaunch('cid', ('agy',), '/workspace', {}, 'A', [], 'ready', '').locus == 'container'
