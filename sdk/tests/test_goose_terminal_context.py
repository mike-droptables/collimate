# SPDX-License-Identifier: MIT
"""F1554/F1567: Goose native snapshot/export ownership through selected dispatch."""
import asyncio
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace

import pytest

import _agent_shared as engine
from _agent_shared import cli_container
from _agent_shared.harnesses import CommandsPolicy, PreparedHarness  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

from _agent_shared.harnesses.goose import backend as goose


async def _no_credentials(adapter, *, provider, plan):
    """The pipeline's credential step for a test whose renderer is a fake."""
    return _pipeline.Credentials(provider, plan, key="" if plan else "sk-test")


class NativeFailure(Exception):
    pass


@pytest.fixture
def leg(monkeypatch):
    timeline, calls, ready, prepare_calls, logs = [], [], [], [], []
    control = {'before': frozenset({'old'}), 'failure': '', 'bridge': True}
    main_thread = threading.get_ident()
    env = SimpleNamespace(container_id='prepared-goose')
    prepared = PreparedHarness(CommandsPolicy('deny', {}), None, env, ('goose', 'session', '--native-option'),
        {'NATIVE_ENV': 'actual'}, 'selected/native-model')
    def fail(name):
        if control['failure'] == name:
            raise NativeFailure('native-' + name)
    async def prepare(cfg, *, leg):
        prepare_calls.append((cfg, leg))
        return prepared
    async def update(**kwargs):
        timeline.append(('update', kwargs))
    @asynccontextmanager
    async def bridge(**kwargs):
        timeline.append(('bridge', kwargs))
        try:
            yield SimpleNamespace(url_container='http://own/leg/mcp') if control['bridge'] else None
        finally:
            timeline.append('bridge-close')
    def snapshot(target):
        assert target is env and threading.get_ident() != main_thread
        timeline.append('snapshot')
        fail('snapshot')
        return control['before']
    @asynccontextmanager
    async def terminal(container, argv, **kwargs):
        calls.append((container, argv, kwargs))
        timeline.append('register')
        fail('register')
        try:
            yield {'terminal_id': 'own-goose-terminal'}
        finally:
            timeline.append('terminal-close')
    @asynccontextmanager
    async def live(**kwargs):
        timeline.append('live')
        fail('live')
        try:
            yield
        finally:
            timeline.append('live-close')
    async def wait(**kwargs):
        assert kwargs == {'terminal_id': 'own-goose-terminal', 'on_action': None}
        timeline.append('wait')
        fail('wait')
        if control['failure'] == 'cancel':
            raise asyncio.CancelledError
    def finalized(target, **kwargs):
        assert target is env and threading.get_ident() != main_thread
        timeline.append(('finalize', kwargs))
        fail('finalize')
    def on_ready(sid, **kwargs):
        ready.append((sid, kwargs))
        fail('ready')
    monkeypatch.setattr(goose, 'prepare', prepare)
    monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
    monkeypatch.setattr(goose, '_listed_before', snapshot)
    monkeypatch.setattr(goose, '_finalize_goose_terminal', finalized)
    monkeypatch.setattr(goose, '_secret_log', logs.append)
    monkeypatch.setattr(cli_container, 'leg_bridge', bridge)
    monkeypatch.setattr(goose.gen, 'commit', update)
    monkeypatch.setattr(goose.gen, 'phase', lambda *args: None)
    monkeypatch.setattr(goose.gen, 'serve_terminal', terminal)
    monkeypatch.setattr(goose.gen, 'live_files', live)
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    async def run(resume=''):
        cfg = {'backend': 'goose', 'commands': 'allow', 'websearch': False, 'system_prompt': 'old'}
        await engine.surface_runner('goose', 'terminal')(cfg, pointer='own.terminal',
            extra_system='native-context', resume_harness_session_id=resume,
            first_message='ignored-initial-message', on_ready=on_ready)
        assert cfg['system_prompt'] == 'old'
    return SimpleNamespace(run=run, control=control, timeline=timeline, calls=calls, ready=ready,
        prepare_calls=prepare_calls, logs=logs)


@pytest.mark.parametrize('resume,before,bridge', [('', None, True), ('', frozenset(), True),
    ('', frozenset({'old'}), False), ('own-resume', None, True)])
def test_goose_actual_entry_preserves_prepared_launch_session_and_effective_grant_F1554_F1567(leg, resume, before, bridge):
    leg.control.update(before=before, bridge=bridge)
    asyncio.run(leg.run(resume))
    assert len(leg.prepare_calls) == 1
    cfg, kind = leg.prepare_calls[0]
    assert kind == 'terminal' and cfg['system_prompt'] == 'native-context'
    assert leg.timeline[0] == ('update', {'name': 'Agent · Goose'})
    rows = next(x[1]['extra_entries'] for x in leg.timeline if isinstance(x, tuple) and x[0] == 'bridge') or []
    assert all(row[0] != 'test_generator' for row in rows)
    container, argv, kwargs = leg.calls[0]
    assert container == 'prepared-goose'
    assert argv == ['goose', 'session', '--native-option'] + (['--resume', '--session-id', resume] if resume else []) + (['--with-streamable-http-extension', 'http://own/leg/mcp'] if bridge else [])
    assert kwargs['env'] == {'TERM': 'xterm-256color', 'NATIVE_ENV': 'actual'}
    assert kwargs['label'] == 'Goose · selected/native-model' and kwargs['name'] == 'Agent · Goose'
    assert kwargs['pointer'] == 'own.terminal' and kwargs['cwd'] == '/workspace' and kwargs['keep_alive'] is True
    assert leg.ready == ([(resume, {})] if resume or before is None else [('', {'before': before})])
    assert ('snapshot' in leg.timeline) is (not bool(resume))
    final = next(x for x in leg.timeline if isinstance(x, tuple) and x[0] == 'finalize')
    assert final[1] == {'harness_session_id': resume, 'before': None if resume else before}
    assert leg.timeline.index('live-close') < leg.timeline.index('terminal-close') < leg.timeline.index(final) < leg.timeline.index('bridge-close')


@pytest.mark.parametrize('failure', ['snapshot', 'register', 'ready', 'live', 'wait', 'cancel', 'finalize'])
def test_goose_snapshot_precedes_export_arm_and_native_whole_finalizer_is_best_effort_F1554_F1567(leg, failure):
    leg.control['failure'] = failure
    if failure == 'finalize':
        asyncio.run(leg.run())
        assert leg.logs == ['goose finalize failed: native-finalize']
    else:
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else NativeFailure):
            asyncio.run(leg.run())
    finals = [x for x in leg.timeline if isinstance(x, tuple) and x[0] == 'finalize']
    assert len(finals) == (0 if failure == 'snapshot' else 1)
    assert leg.timeline[-1] == 'bridge-close'
    if failure == 'snapshot':
        assert leg.calls == [] and leg.ready == []
    if finals:
        assert leg.timeline.index(finals[0]) < leg.timeline.index('bridge-close')
