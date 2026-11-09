# SPDX-License-Identifier: MIT
"""P4F23: actual installed Qwen policy/registry, synthetic auth, no model turn."""
import json

import pytest

from tests_live.conftest import requires_docker, run_on_test_loop
from tests_live._host_terminal import _HostTerminal
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

pytestmark = [requires_docker, pytest.mark.usefixtures('one_loop_per_test')]

_NATIVE_POLICY = r'''
const {realpathSync}=await import('node:fs');
const {dirname,join}=await import('node:path');
const root=join(dirname(realpathSync('/opt/collimate/qwen/native-cli')),'chunks/');
const {loadSettings}=await import(root+'chunk-7BXXIQG4.js');
const {parseArguments,loadCliConfig}=await import(root+'chunk-DYFOSJHU.js');
const input=JSON.parse(process.argv[1]);
process.argv=['node','qwen',...input.argv.slice(1),...(input.bare?['--bare']:[])];
const argv=await parseArguments();
const settings=loadSettings('/workspace');
const config=await loadCliConfig(settings.merged,argv,'/workspace');
const sourcePermissions=settings.merged.permissions;
const argvExclusions=argv.excludeTools;
const initialDeny=[...config.getPermissionsDeny()];
await config.initialize({skipLlmInitialization:true,skipMcpDiscovery:true});
const pm=config.getPermissionManager();
const names=config.getToolRegistry().getAllToolNames();
const result={deny:config.getPermissionsDeny(),ask:config.getPermissionsAsk(),
  agent:names.includes('agent'),shell:names.includes('run_shell_command'),
  agentPermissionEnabled:await pm.isToolEnabled('agent'),
  shellPermissionEnabled:await pm.isToolEnabled('run_shell_command'),
  mode:config.getApprovalMode(),bare:config.getBareMode(),stdinTTY:!!process.stdin.isTTY,
  sourcePermissions,argvExclusions,initialDeny};
console.log('NATIVE_POLICY='+JSON.stringify(result));
process.exit(0);
'''

_CASES = [(leg, level, bare) for leg in ('acp', 'terminal')
          for level, bare in (('deny', False), ('deny', True), ('ask', False), ('allow', False))]


@pytest.mark.parametrize('leg,level,bare', _CASES,
                         ids=[f'{leg}-{level}-{"bare" if bare else "native"}'
                              for leg, level, bare in _CASES])
def test_actual_native_agent_registry_holds_command_grant_P4F23(
        docker_env_bridge, monkeypatch, leg, level, bare):
    async def synthetic_key(provider):
        return 'qwen-P4F23-synthetic-not-a-real-provider-key'
    monkeypatch.setattr(_pipeline, 'account_api_key', synthetic_key)
    docker_env_bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': level}
    docker_env_bridge.packages = {'qwen-code': '0.26.1'}
    result = run_on_test_loop(_pipeline.prepare(_HARNESS['qwen-code'], {'auth': 'api_key', 'provider': 'openrouter',
        'model': 'deepseek/deepseek-v4-flash', 'commands': level,
        'websearch': False, 'system_prompt': ''}, leg=leg))
    command = ['node', '--input-type=module', '-e', _NATIVE_POLICY,
               json.dumps({'argv': list(result.argv), 'bare': bare})]
    if leg == 'terminal':
        term = _HostTerminal(result.env_obj.container_id, command, '/workspace',
                             {'TERM': 'xterm-256color', **result.env})
        try:
            rc = term.proc.wait(timeout=45)
        finally:
            term.stop()
        output = term.said()
    else:
        rc, output = result.env_obj.exec(command, env=result.env, timeout=45)
    assert rc == 0, output
    rows = [line.removeprefix('NATIVE_POLICY=') for line in output.splitlines()
            if line.startswith('NATIVE_POLICY=')]
    assert len(rows) == 1, output
    native = json.loads(rows[0])
    print('[native-effective-policy] '+json.dumps(native,sort_keys=True))
    assert native['bare'] is bare
    assert native['stdinTTY'] is (leg == 'terminal'), native
    assert native['agent'] is native['agentPermissionEnabled'], native
    assert native['shell'] is native['shellPermissionEnabled'], native
    assert native['agent'] is (level == 'allow'), native
    assert native['shell'] is (level != 'deny'), native
    if level == 'deny':
        assert set(native['deny']) >= {'agent', 'run_shell_command'}, native
    elif level == 'ask':
        assert 'agent' in native['deny'] and 'run_shell_command' in native['ask'], native
    else:
        assert 'agent' not in native['deny'] and 'run_shell_command' not in native['deny'], native
    print(f'[native-agent-policy] Qwen0.23.3 {leg}/{level}/bare={bare}: agent={native["agent"]} shell={native["shell"]}; no provider turn')
