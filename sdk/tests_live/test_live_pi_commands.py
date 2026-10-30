# SPDX-License-Identifier: MIT
"""Pinned native Pi ACP/PTY command grants and private native authentication."""
import json
import os
from pathlib import Path
import time

import pytest
from tests_live.conftest import (bind_model_account, has_key, model_account, model_for,
                                requires_docker, run_on_test_loop)
from tests_live.test_live_acp_conformance import _probe_prompt, _write_probe
from tests_live._host_terminal import _HostTerminal, _type_line
from tests_live._host_terminal import _up_to_prompt
from _agent_shared import harness_homes
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

PINS = {'pi-coding-agent': '1.4.0', 'pi-acp': '0.0.34'}
pytestmark = [requires_docker, pytest.mark.usefixtures('one_loop_per_test')]
requires_key = pytest.mark.skipif(not has_key('openrouter'), reason='needs OPENROUTER_API_KEY')


def prepared(bridge, level, *, synthetic=False):
    bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': level}
    bridge.packages = dict(PINS)
    model = 'deepseek/deepseek-v4-flash' if synthetic else model_for('openrouter')
    bind_model_account(model_account('openrouter', model=model, harness='pi'))
    return run_on_test_loop(_pipeline.prepare(_HARNESS['pi'], {'auth': 'api_key', 'provider': 'openrouter',
        'model': model, 'commands': level, 'system_prompt': ''}, leg='terminal'))


def assert_private(result):
    key = os.environ['OPENROUTER_API_KEY']
    if key in json.dumps(result.env) or any(name.endswith('API_KEY') for name in result.env):
        pytest.fail('Selected credential exposed in native process environment')
    path = Path(harness_homes.harness_home('pi')) / 'agent/auth.json'
    if json.loads(path.read_bytes()) != {'openrouter': {'type': 'api_key', 'key': key}}:
        pytest.fail('Native authentication does not contain exactly the selected key')
    rc, output = result.env_obj.exec(['env'])
    if rc or key in output or any('API_KEY=' in line for line in output.splitlines()):
        pytest.fail('Selected credential exposed in native container environment')
    # Actual installed native parser/resolver, no model call and no key output.
    script = """import {readFileSync,statSync} from 'node:fs';
import {ReadOnlyAuthStorage} from '/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/auth-storage.js';
const p='/home/coder/.pi/agent/auth.json'; const stored=JSON.parse(readFileSync(p,'utf8'));
const value=await new ReadOnlyAuthStorage(p).read('openrouter');
console.log(value?.type==='api_key' && value.key===stored.openrouter.key && Boolean(value.key));
console.log((statSync(p).mode & 511).toString(8));"""
    rc, output = result.env_obj.exec(['node', '--input-type=module', '-e', script])
    assert rc == 0 and output.splitlines() == ['true', '600']


def test_pi_installed_native_reads_selected_private_key_without_provider_turn(docker_env_bridge, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'pi-native-unpaid-synthetic-selected-key')
    assert_private(prepared(docker_env_bridge, 'deny', synthetic=True))


@requires_key
def test_pi_native_private_key_file_and_container_environment(docker_env_bridge):
    assert_private(prepared(docker_env_bridge, 'allow'))


def messages_since(home, before):
    messages = []
    for path in set(home.rglob('*.jsonl')) - before:
        raw = path.read_text(errors='replace')
        if os.environ['OPENROUTER_API_KEY'] in raw:
            pytest.fail('Selected credential exposed in native transcript')
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get('message')
            if isinstance(message, dict):
                messages.append(message)
    return messages


def terminal_turn_finished(messages, digest, *, rejected=False, command=''):
    """Require native completion after a matching rejected shell call."""
    if rejected:
        calls = [(index, part['id']) for index, row in enumerate(messages)
                 if row.get('role') == 'assistant'
                 for part in row.get('content', [])
                 if part.get('type') == 'toolCall' and part.get('id')
                 and part.get('name') in ('bash', 'powershell')
                 and part.get('arguments', {}).get('command') == command]
        if len(calls) != 1:
            return False
        call_index, identity = calls[0]
        for index in range(call_index + 1, len(messages)):
            row = messages[index]
            if (row.get('role') == 'toolResult' and row.get('toolCallId') == identity
                    and row.get('isError') is True
                    and any('the user did not approve' in part.get('text', '')
                            for part in row.get('content', []))):
                return any(final.get('role') == 'assistant'
                           and final.get('stopReason') == 'stop'
                           for final in messages[index + 1:])
        return False
    responses = [part['text'] for row in messages if row.get('role') == 'assistant'
                 for part in row.get('content', []) if part.get('type') == 'text']
    return any(digest in response or 'NO-SHELL' in response for response in responses)


@requires_key
@pytest.mark.parametrize('level', ['deny', 'ask-reject', 'ask-allow', 'allow'])
def test_native_pi_terminal_commands_grant(docker_env_bridge, level):
    commands, _, answer = level.partition('-')
    digest = _write_probe()
    result = prepared(docker_env_bridge, commands)
    env = result.env_obj
    home = Path(harness_homes.harness_home('pi'))
    before = set(home.rglob('*.jsonl'))
    marker = '/workspace/commands-probe.ran'
    env.exec(['rm', '-f', marker])
    term = _HostTerminal(env.container_id, list(result.argv), '/workspace',
                        {'TERM': 'xterm-256color', **result.env})
    observed = False
    messages = []
    try:
        _, blocked = _up_to_prompt('pi', term)
        assert not blocked
        _type_line(term, _probe_prompt('sha256sum commands-probe.txt | tee ' + marker))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if commands == 'ask' and not observed and 'collimate:command' in term.said():
                observed = True
                term.send(b'\x1b' if answer == 'reject' else b'\r')
            messages = messages_since(home, before)
            if terminal_turn_finished(messages, digest, rejected=answer == 'reject',
                    command='sha256sum commands-probe.txt | tee ' + marker):
                break
            time.sleep(1)
        else:
            pytest.fail('Native Pi turn did not complete: ' + term.recent(1600).replace(
                os.environ['OPENROUTER_API_KEY'], '[redacted]'))
        rc, output = env.exec(['cat', marker])
        ran = rc == 0 and digest in output
        assert observed is (commands == 'ask')
        assert ran is (commands == 'allow' or answer == 'allow')
        if answer == 'reject':
            calls = {part['id'] for row in messages if row.get('role') == 'assistant'
                     for part in row.get('content', []) if part.get('type') == 'toolCall'
                     and part.get('name') in ('bash', 'powershell') and part.get('id')
                     and 'commands-probe.txt' in part.get('arguments', {}).get('command', '')}
            assert any(row.get('role') == 'toolResult' and row.get('toolCallId') in calls
                and row.get('isError') is True and any('the user did not approve' in part.get('text', '')
                    for part in row.get('content', [])) for row in messages), 'Native rejection needs the matching typed shell result'
        assert_private(result)
    finally:
        term.stop()
