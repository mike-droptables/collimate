# SPDX-License-Identifier: MIT
"""Genuine pinned Codex native PTY grant and credential-file gates."""
import json
import os
from pathlib import Path
import time

import pytest
from tests_live.conftest import (bind_model_account, has_key, model_account, model_for,
                                requires_docker, run_on_test_loop)
from tests_live.test_live_acp_conformance import _probe_prompt, _write_probe
from tests_live._host_terminal import _HostTerminal, _type_line
from _agent_shared import harness_homes
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses.codex import backend as codex

pytestmark = [requires_docker, pytest.mark.usefixtures('one_loop_per_test'),
    pytest.mark.skipif(not has_key('openrouter'), reason='needs OPENROUTER_API_KEY')]


def prepared(bridge, level):
    bridge.permissions = {'docker':'full','internet':'full','commands':level}
    bridge.packages = {'codex':'0.164.0','codex-acp':'2.2.2'}
    model = model_for('openrouter')
    bind_model_account(model_account('openrouter',model=model,harness='codex'))
    return run_on_test_loop(_pipeline.prepare(_HARNESS['codex'], {'auth':'api_key','provider':'openrouter','model':model,
        'commands':level,'websearch':False,'system_prompt':''},leg='terminal'))


def assert_private(result):
    key = os.environ['OPENROUTER_API_KEY']
    if key in json.dumps(result.env) or any(name.endswith('API_KEY') for name in result.env):
        pytest.fail('Selected credential exposed in native process environment')
    rc, output = result.env_obj.exec(['env'])
    if rc or key in output or any('API_KEY=' in row for row in output.splitlines()):
        pytest.fail('Selected credential exposed in native container environment')
    rc, output = result.env_obj.exec(['python3','-c',
        "import tomllib,os,stat; p='/home/coder/.codex-collimate/config.toml'; "
        "d=tomllib.load(open(p,'rb')); print(bool(d['model_providers']['collimate']['experimental_bearer_token'])); "
        "print('env_key' not in d['model_providers']['collimate']); print(oct(stat.S_IMODE(os.stat(p).st_mode)))"])
    assert rc == 0 and output.splitlines() == ['True','True','0o600']


def test_codex_native_private_key_file_and_container_environment(docker_env_bridge):
    assert_private(prepared(docker_env_bridge,'allow'))
    print('[credentials] Codex0.164.0: native config0600, selected token present, no env_key/API_KEY process or container variables')


@pytest.mark.parametrize('level',['deny','ask-reject','ask-allow','allow'])
def test_native_codex_terminal_commands_grant(docker_env_bridge, level):
    commands, _, answer = level.partition('-')
    digest = _write_probe()
    result = prepared(docker_env_bridge,commands)
    env = result.env_obj
    home = Path(harness_homes.harness_home('codex'))
    before = set((home/'sessions').rglob('*.jsonl'))
    # Under ask write outside all writable sandbox roots to require escalation.
    marker = '/home/coder/collimate-command-probe.ran' if commands == 'ask' else '/workspace/commands-probe.ran'
    env.exec(['rm','-f',marker])
    # Match Codex's native terminal update-screen override.
    argv = [result.argv[0], '-c', 'check_for_update_on_startup=false', *result.argv[1:]]
    term = _HostTerminal(env.container_id,argv,'/workspace',
                         {'TERM':'xterm-256color',**result.env})
    observed = False
    interrupted = False
    try:
        term.wait_for(b'context left',b'To get started',b'Ask Codex',timeout=60)
        _type_line(term,_probe_prompt('sha256sum commands-probe.txt > '+marker))
        deadline = time.monotonic()+180
        trace = []
        while time.monotonic()<deadline:
            screen = term.said().lower()
            if commands == 'ask' and not observed and ('would you like to run' in screen or 'approve this command' in screen):
                observed=True
                term.send(b'3\r' if answer=='reject' else b'1\r')
            paths=set((home/'sessions').rglob('*.jsonl'))-before
            complete=False
            for path in paths:
                raw=path.read_text(errors='replace')
                if os.environ['OPENROUTER_API_KEY'] in raw:
                    pytest.fail('Selected credential exposed in native rollout')
                trace=codex.trace_from_codex_session(raw)
                for line in raw.splitlines():
                    try:
                        entry=json.loads(line)
                    except ValueError:
                        continue
                    if entry.get('type')=='event_msg':
                        payload=entry.get('payload',{})
                        if payload.get('type') in ('task_complete','turn_complete'):
                            complete=True
                        if payload.get('type')=='turn_aborted' and payload.get('reason')=='interrupted':
                            interrupted=True
            if answer=='reject' and interrupted:
                break
            if complete:
                break
            time.sleep(1)
        else:
            recent=term.recent(1600).replace(os.environ['OPENROUTER_API_KEY'],'[redacted]')
            pytest.fail('Native Codex turn did not complete: '+recent)
        rc, output=env.exec(['cat',marker])
        ran=rc==0 and digest in output
        assert observed is (commands=='ask')
        assert ran is (commands=='allow' or answer=='allow')
        if answer=='reject':
            assert interrupted, 'Native rejected command must emit the typed interrupted turn event'
            assert 'you canceled the request' in term.said().lower()
        else:
            assert any(row.get('type')=='text' for row in trace), 'Native completed turn has no assistant response'
        assert_private(result)
        print(f'[commands-native-terminal] Codex/{level}: permission={observed} shell_ran={ran}')
    finally:
        term.stop()
