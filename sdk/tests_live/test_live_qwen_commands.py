# SPDX-License-Identifier: MIT
"""Pinned native Qwen PTY grants and private selected credential file."""
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

pytestmark = [requires_docker, pytest.mark.usefixtures('one_loop_per_test'),
    pytest.mark.skipif(not has_key('openrouter'), reason='needs OPENROUTER_API_KEY')]


def direct_probe_prompt(command):
    return (_probe_prompt(command) + '\nUse only your direct run_shell_command tool. '
            'Do not delegate to an agent or use file tools to perform this probe. '
            'If the direct shell tool is unavailable or the request is rejected, reply NO-SHELL.')


def prepared(bridge, level):
    bridge.permissions={'docker':'full','internet':'full','commands':level}
    bridge.packages={'qwen-code':'0.26.1'}
    model=model_for('openrouter')
    bind_model_account(model_account('openrouter',model=model,harness='qwen-code'))
    return run_on_test_loop(_pipeline.prepare(_HARNESS['qwen-code'], {'auth':'api_key','provider':'openrouter','model':model,
        'commands':level,'websearch':False,'system_prompt':''},leg='terminal'))


def assert_private(result):
    key=os.environ['OPENROUTER_API_KEY']
    if key in json.dumps(result.env) or any(name.endswith('API_KEY') for name in result.env):
        pytest.fail('Selected credential exposed in native process environment')
    rc,output=result.env_obj.exec(['env'])
    if rc or key in output or any('API_KEY=' in row for row in output.splitlines()):
        pytest.fail('Selected credential exposed in native container environment')
    rc,output=result.env_obj.exec(['python3','-c',
        "import json,os,stat; p='/home/coder/.qwen/settings.json'; d=json.load(open(p)); "
        "print(bool(d['security']['auth']['apiKey'])); print(d['security']['auth']['selectedType']); "
        "print(oct(stat.S_IMODE(os.stat(p).st_mode)))"])
    assert rc==0 and output.splitlines()==['True','openai','0o600']


def test_qwen_native_private_key_file_and_container_environment(docker_env_bridge):
    assert_private(prepared(docker_env_bridge,'allow'))
    print('[credentials] Qwen0.23.3 native settings0600, selected key present, no API_KEY process/container variables')


@pytest.mark.parametrize('level',['deny','ask-reject','ask-allow','allow'])
def test_native_qwen_terminal_commands_grant(docker_env_bridge,level):
    commands,_,answer=level.partition('-')
    digest=_write_probe()
    result=prepared(docker_env_bridge,commands)
    env=result.env_obj
    home=Path(harness_homes.harness_home('qwen-code'))
    before=set(home.rglob('*.jsonl'))
    marker='/workspace/commands-probe.ran'
    env.exec(['rm','-f',marker])
    term=_HostTerminal(env.container_id,list(result.argv),'/workspace',
                       {'TERM':'xterm-256color',**result.env})
    observed=False
    cancelled=False
    probe_calls=set()
    try:
        _,blocked=_up_to_prompt('qwen-code',term)
        assert not blocked
        _type_line(term,direct_probe_prompt('sha256sum commands-probe.txt | tee '+marker))
        deadline=time.monotonic()+180
        responses=[]
        while time.monotonic()<deadline:
            screen=term.said().lower()
            if commands=='ask' and not observed and 'allow execution of:' in screen and 'yes, allow once' in screen:
                observed=True
                term.send(b'\x1b' if answer=='reject' else b'\r')
            responses=[]
            for path in set(home.rglob('*.jsonl'))-before:
                raw=path.read_text(errors='replace')
                if os.environ['OPENROUTER_API_KEY'] in raw:
                    pytest.fail('Selected credential exposed in native transcript')
                for line in raw.splitlines():
                    try:
                        entry=json.loads(line)
                    except ValueError:
                        continue
                    if entry.get('type')=='assistant':
                        parts=entry.get('message',{}).get('parts',[])
                        for part in parts:
                            call=part.get('functionCall',{})
                            command=call.get('args',{}).get('command','')
                            if (call.get('name')=='run_shell_command' and call.get('id')
                                    and 'sha256sum commands-probe.txt' in command and marker in command):
                                probe_calls.add(call['id'])
                        responses.extend(part['text'] for part in parts
                                         if isinstance(part,dict) and part.get('text') and not part.get('thought'))
                    if entry.get('type')=='tool_result':
                        for part in entry.get('message',{}).get('parts',[]):
                            response=part.get('functionResponse',{})
                            if (response.get('name')=='run_shell_command' and response.get('id') in probe_calls
                                    and response.get('response',{}).get('error')==
                                    '[Operation Cancelled] Reason: User did not allow tool call'):
                                cancelled=True
            if answer=='reject' and observed and cancelled:
                break
            if any(digest in response or 'NO-SHELL' in response for response in responses):
                break
            time.sleep(1)
        else:
            pytest.fail('Native Qwen no completed probe response: '+term.recent(1600).replace(os.environ['OPENROUTER_API_KEY'],'[redacted]'))
        rc,output=env.exec(['cat',marker])
        ran=rc==0 and digest in output
        assert observed is (commands=='ask')
        assert ran is (commands=='allow' or answer=='allow')
        if answer=='reject':
            assert cancelled, 'Native rejection must persist the direct shell cancellation result'
        elif not ran:
            assert any('NO-SHELL' in response for response in responses)
        assert_private(result)
        print(f'[commands-native-terminal] Qwen/{level}: permission={observed} shell_ran={ran}')
    finally:
        term.stop()
