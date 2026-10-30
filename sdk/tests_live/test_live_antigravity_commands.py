# SPDX-License-Identifier: MIT
"""Pinned actual AGY TTY command outcomes; decoded native result precedes cleanup."""
import asyncio
import os
from pathlib import Path
import time

import gen
import pytest

from _agent_shared import cli_container, harness_homes, surface_runner
from _agent_shared.harnesses.antigravity import backend as agy
from tests_live.conftest import bind_model_account, has_key, model_account, requires_docker, run_on_test_loop
from tests_live import _host_terminal as tty
from tests_live.test_live_acp_conformance import _write_probe
from tests_live.test_live_cline_commands import command_child_running
from tests_live.fixtures import agy_terminal_command as witness

PINS = {'agy': '1.4.7', 'mcp-remote': '0.15.2'}
pytestmark = [requires_docker, pytest.mark.usefixtures('one_loop_per_test'),
              pytest.mark.skipif(not has_key('gemini'), reason='needs authorized GEMINI_API_KEY')]


def native_snapshot(home, before, environment, term, checkpoint):
    ids = sorted(agy.conversation_ids(str(home)) - before)
    assert len(ids) <= 1, 'Additional native conversation'
    conversations = [{'conversation_id': cid,
        'database': witness.database_snapshot(home, agy.conversation_db_path(str(home), cid))} for cid in ids]
    with term._lock:
        raw = bytes(term._out)
    assert len(raw) <= 2 * 1024 * 1024
    path = Path(gen.workspace_path()) / 'commands-probe.ran'
    marker = {'exists': False}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        pass
    else:
        import stat
        with os.fdopen(fd, 'rb') as stream:
            assert stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
            body = stream.read(4097)
        marker = {'exists': True, 'retained_bytes': body[:4096], 'truncated': len(body) > 4096}
    return {'new_ids': ids, 'conversations': conversations, 'terminal_raw': raw,
            'checkpoint': checkpoint, 'marker': marker,
            'own_command_child_running': command_child_running(environment, witness.COMMAND)}


def assert_private(prepared, home):
    """AGY's measured key carrier is its private native process environment."""
    key = os.environ['GEMINI_API_KEY']
    if set(prepared.env) != {'GEMINI_API_KEY'} or prepared.env.get('GEMINI_API_KEY') != key:
        raise AssertionError('Native key carrier differs from selected private credential')
    assert prepared.model == witness.MODEL
    prepared.env_obj.container.reload()
    persisted = prepared.env_obj.container.attrs.get('Config', {}).get('Env', [])
    assert not any(value.startswith('GEMINI_API_KEY=') or key in value for value in persisted)
    assert key not in repr(prepared.argv)
    assert home.stat().st_mode & 0o777 == 0o700
    root = Path(gen.workspace_path())
    # Match the public/private split used by the actual workspace mirror.
    for path in root.rglob('*'):
        if '.collrun' in path.relative_to(root).parts or path.is_symlink() or not path.is_file():
            continue
        assert key.encode() not in path.read_bytes(), 'Selected key reached public workspace'


def native_idle(raw, checkpoint):
    """Only a new fully closed frame returning native input readiness."""
    start = raw.rfind(b'\x1b[?2026h')
    if start < checkpoint or not raw.endswith(b'\x1b[?2026l'):
        return False
    frame = raw[start:]
    return frame.count(b'? for shortcuts') == 1 and b'esc to cancel' not in frame


@pytest.mark.parametrize('level', ['deny', 'ask-reject', 'ask-allow', 'allow'])
def test_antigravity_actual_terminal_commands_and_private_scope(docker_env_bridge, monkeypatch, tmp_path, level):
    commands = level.partition('-')[0]
    docker_env_bridge.packages = dict(PINS)
    docker_env_bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': commands}
    bind_model_account(model_account('gemini', model=witness.MODEL, harness='antigravity'))
    digest = _write_probe()
    home = Path(harness_homes.harness_home('antigravity'))
    before = agy.conversation_ids(str(home))
    evidence = witness.Evidence(tmp_path / 'native-evidence', os.environ['GEMINI_API_KEY'])
    terms = tty._host_terminals(docker_env_bridge, monkeypatch)
    prepared = []
    original_prepare = agy.prepare

    async def prepare(*args, **kwargs):
        result = await original_prepare(*args, **kwargs)
        prepared.append(result)
        return result

    monkeypatch.setattr(agy, 'prepare', prepare)

    async def run():
        task = asyncio.create_task(surface_runner('antigravity', 'terminal')(
            {'backend': 'antigravity', 'auth': 'api_key', 'provider': 'gemini', 'model': witness.MODEL,
             'commands': commands, 'websearch': False}, pointer=gen.run_path('terminal/antigravity.terminal')))
        latest, term, checkpoint, responded = None, None, 0, {}
        try:
            until = time.monotonic() + 90
            while not terms and not task.done() and time.monotonic() < until:
                await asyncio.sleep(.1)
            assert len(terms) == len(prepared) == 1
            term = next(iter(terms.values()))
            await asyncio.to_thread(tty._agy_opens_at_its_prompt, term)
            assert_private(prepared[0], home)
            assert prepared[0].commands.level == commands
            assert prepared[0].argv == ('agy', '--model', witness.MODEL,
                *(['--dangerously-skip-permissions'] if commands == 'allow' else []))
            with term._lock:
                checkpoint = len(term._out)
            await asyncio.to_thread(tty._type_line, term, witness.PROMPT)
            until = time.monotonic() + 240
            while time.monotonic() < until:
                latest = await asyncio.to_thread(native_snapshot, home, before, prepared[0].env_obj, term, checkpoint)
                if level.startswith('ask-') and not responded:
                    try:
                        identity = witness.pending_identity(latest, agy._pb_fields)
                        witness.current_allow_panel(latest['terminal_raw'], checkpoint)
                    except (AssertionError, KeyError, TypeError, ValueError):
                        pass
                    else:
                        evidence.write('before-answer.json', latest)
                        assert not evidence.faults
                        if level == 'ask-reject':
                            initial_raw = latest['terminal_raw']
                            for number in range(1, 4):
                                term.send(b'\x1b[B')
                                await asyncio.sleep(.3)
                                latest = await asyncio.to_thread(native_snapshot, home, before, prepared[0].env_obj, term, checkpoint)
                                evidence.write(f'rejection-navigation-{number}.json', latest)
                                assert not evidence.faults
                                assert witness.pending_identity(latest, agy._pb_fields) == identity
                            witness.measured_rejection_transitions(latest['terminal_raw'], initial_raw)
                        term.send(b'\r')
                        responded[identity['call_id']] = level
                try:
                    if level == 'deny':
                        witness.completed_denial(latest, agy._pb_fields, responded=responded)
                    elif level == 'ask-reject':
                        witness.completed_rejection(latest, agy._pb_fields, responded=responded)
                    else:
                        witness.completed_success(latest, agy._pb_fields, digest=digest, level=level, responded=responded)
                except (AssertionError, KeyError, TypeError, ValueError):
                    pass
                else:
                    if native_idle(latest['terminal_raw'], checkpoint):
                        if level == 'ask-reject':
                            final_frame = latest['terminal_raw'][latest['terminal_raw'].rfind(b'\x1b[?2026h'):]
                            assert b'User declined the tool call' in final_frame
                            assert 'Interrupted · What should Antigravity CLI do instead?'.encode() in final_frame
                        latest['decoded_outcome'] = ('native_user_rejected_interrupted' if level == 'ask-reject'
                            else 'native_policy_denial_final_reply' if level == 'deny'
                            else 'native_command_success_final_reply')
                        evidence.write('completed-before-cleanup.json', latest)
                        assert not evidence.faults
                        assert_private(prepared[0], home)
                        return
                assert not task.done() and term.proc.poll() is None
                await asyncio.sleep(.2)
            raise AssertionError('No fully correlated native command outcome before cleanup')
        finally:
            errors = []
            try:
                if latest is not None:
                    evidence.write('last-before-cleanup.json', latest)
            except BaseException as error:
                errors.append(type(error).__name__)
            # Ending the terminal's client ends the leg: the runner sees its
            # terminal ended and returns.
            try:
                for item in terms.values():
                    await asyncio.to_thread(item.stop)
            except BaseException as error:
                errors.append(type(error).__name__)
            try:
                await asyncio.wait_for(task, 30)
            except BaseException as error:
                errors.append(type(error).__name__)
            for closer in [*[item.stop for item in terms.values()], cli_container.close_all,
                           harness_homes.close_harness_homes]:
                try:
                    await asyncio.to_thread(closer)
                except BaseException as error:
                    errors.append(type(error).__name__)
            evidence.write('cleanup.json', {'private_home_removed': not home.exists(), 'errors': errors})
            assert not home.exists() and not errors and not evidence.faults
    run_on_test_loop(run())
