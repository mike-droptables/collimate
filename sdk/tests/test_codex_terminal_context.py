# SPDX-License-Identifier: MIT
"""F1554/F1567: actual Codex entry preserves marker/binding/native cleanup order."""
import asyncio
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace

import pytest

import _agent_shared as engine
from _agent_shared import cli_container
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

from _agent_shared.harnesses.codex import backend as codex
from collimate import fs
from tests._commands_matrix import recording_runtime  # noqa: F401
from tests._git_fixtures import SAFE_DIRECTORY


async def _no_credentials(adapter, *, provider, plan):
    """The pipeline's credential step for a test whose renderer is a fake."""
    return _pipeline.Credentials(provider, plan, key="" if plan else "sk-test")


class NativeFailure(Exception):
    pass


@pytest.fixture
def leg(recording_runtime, monkeypatch):  # noqa: F811
    events, launches, ready, prepared_values, logs = [], [], [], [], []
    control = {'failure': ''}
    main_thread = threading.get_ident()
    original_prepare, original_write, original_bind = codex.prepare, fs.write_file, codex.write_terminal_bridge
    def fail(stage):
        if control['failure'] == stage:
            raise NativeFailure('native-' + stage)
    async def prepare(ctx, *, leg):
        events.append(('prepare', dict(ctx), leg))
        value = await original_prepare(ctx, leg=leg)
        prepared_values.append(value)
        return value
    def write(root, leaf, body, **kwargs):
        if leaf == codex._START_MARKER:
            assert threading.get_ident() != main_thread
            events.append('marker')
            fail('marker')
        return original_write(root, leaf, body, **kwargs)
    @asynccontextmanager
    async def bridge(**kwargs):
        events.append(('bridge', kwargs))
        try:
            yield SimpleNamespace(url_container='http://own/codex/mcp')
        finally:
            events.append('bridge-close')
    def bind(*args):
        assert threading.get_ident() != main_thread
        events.append('bind')
        fail('bind')
        original_bind(*args)
    @asynccontextmanager
    async def terminal(container, argv, **kwargs):
        events.append('register')
        launches.append((container, argv, kwargs))
        fail('register')
        try:
            yield {'terminal_id': 'own-codex-terminal'}
        finally:
            events.append('terminal-close')
    @asynccontextmanager
    async def live(**kwargs):
        try:
            yield
        finally:
            events.append('live-close')
    async def wait(**kwargs):
        assert kwargs == {'terminal_id': 'own-codex-terminal', 'on_action': None}
        fail('wait')
        if control['failure'] == 'cancel':
            raise asyncio.CancelledError
    def finalize(env, home):
        assert env is recording_runtime and home == codex.CODEX_HOME
        assert threading.get_ident() != main_thread
        events.append('export')
        fail('finalize')
    def on_ready(sid, **kwargs):
        ready.append((sid, kwargs))
        fail('ready')
    monkeypatch.setattr(codex, 'prepare', prepare)
    monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
    monkeypatch.setattr(fs, 'write_file', write)
    monkeypatch.setattr(cli_container, 'leg_bridge', bridge)
    monkeypatch.setattr(codex, 'write_terminal_bridge', bind)
    monkeypatch.setattr(codex, '_finalize_codex_terminal', finalize)
    monkeypatch.setattr(codex, '_secret_log', logs.append)
    def touch_context(*args, **kwargs):
        events.append('workspace-context')
    monkeypatch.setattr(cli_container, 'remove_context_file', touch_context)
    monkeypatch.setattr(cli_container, 'inject_context_file', touch_context)
    monkeypatch.setattr(codex.gen, 'serve_terminal', terminal)
    monkeypatch.setattr(codex.gen, 'live_files', live)
    monkeypatch.setattr(engine, 'serve_tty_until', wait)
    async def run(*, resume='', text='', model='selected/model'):
        cfg = {'backend': 'codex', 'auth': 'api_key', 'provider': 'openrouter', 'model': model,
            'commands': 'allow', 'websearch': False, 'runtime': 'docker'}
        await engine.surface_runner('codex', 'terminal')(cfg, pointer='own.terminal',
            extra_system=text, resume_harness_session_id=resume, first_message='ignored-message',
            on_ready=on_ready)
    return SimpleNamespace(run=run, events=events, launches=launches, ready=ready,
        prepared=prepared_values, control=control, logs=logs, env=recording_runtime)


@pytest.mark.parametrize('resume', ['', 'own-thread'])
@pytest.mark.parametrize('text', ['', 'nonblank native context', ' \n\t '])
def test_codex_actual_entry_preserves_marker_resume_and_raw_context_cleanup_F1554_F1567(leg, resume, text):
    asyncio.run(leg.run(resume=resume, text=text))
    assert len(leg.prepared) == 1
    prepared = leg.prepared[0]
    assert leg.events[0][1]['system_prompt'] == text and leg.events[0][2] == 'terminal'
    bridge = next(x for x in leg.events if isinstance(x, tuple) and x[0] == 'bridge')
    assert leg.events.index('marker') < leg.events.index(bridge) < leg.events.index('bind')
    container, argv, kwargs = leg.launches[0]
    args = ['-c', 'check_for_update_on_startup=false', *prepared.argv[1:]]
    assert argv == (['codex', 'resume', *args, resume] if resume else ['codex', *args, '-m', prepared.model])
    assert ('-m' in argv) is (not bool(resume))
    assert kwargs['env'] == {'TERM': 'xterm-256color', **prepared.env, **SAFE_DIRECTORY}
    assert kwargs['label'] == 'Codex · selected/model' and kwargs['name'] == 'Agent · Codex'
    assert kwargs['pointer'] == 'own.terminal' and kwargs['cwd'] == '/workspace' and kwargs['keep_alive'] is True
    assert leg.ready == [(resume, {})]
    # The context rides $CODEX_HOME/AGENTS.md alone: the workspace file is never touched.
    assert 'workspace-context' not in leg.events
    assert leg.events.index('live-close') < leg.events.index('terminal-close') < leg.events.index('export') < leg.events.index('bridge-close')


def test_codex_fresh_blank_model_retains_native_default_label_without_override_F1554(leg):
    asyncio.run(leg.run(model=''))
    assert '-m' not in leg.launches[0][1]
    assert leg.launches[0][2]['label'] == 'Codex · default'


@pytest.mark.parametrize('failure', ['marker', 'bind', 'register', 'ready', 'wait', 'finalize', 'cancel'])
def test_codex_failure_boundaries_preserve_export_and_original_errors_F1554_F1567(leg, failure):
    leg.control['failure'] = failure
    if failure == 'finalize':
        asyncio.run(leg.run())
    else:
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else NativeFailure):
            asyncio.run(leg.run())
    assert leg.events.count('export') == (0 if failure in ('marker', 'bind') else 1)
    if failure == 'marker':
        assert not any(isinstance(x, tuple) and x[0] == 'bridge' for x in leg.events)
    else:
        assert leg.events[-1] == 'bridge-close'
    if failure == 'finalize':
        assert leg.logs == ['codex teardown failed: native-finalize']
    if failure in ('marker', 'bind'):
        assert leg.launches == [] and leg.ready == []
