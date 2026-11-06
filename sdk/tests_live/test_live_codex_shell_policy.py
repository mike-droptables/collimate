# SPDX-License-Identifier: MIT
"""OW-P4-01: actual installed Codex ACP forwards effective shell environment policy."""
import asyncio
import json
from pathlib import Path
import secrets
import shlex

import gen
import pytest

from tests_live.conftest import bind_model_account, has_key, model_account, model_for, run_on_test_loop
from _agent_shared.harnesses.codex import backend as codex
from collimate import host_harness
import _agent_shared as engine

pytestmark = [pytest.mark.usefixtures("one_loop_per_test"),
              pytest.mark.skipif(not has_key("openrouter"), reason="needs OPENROUTER_API_KEY")]


def test_actual_codex_adapter_forwards_shell_environment_policy_OW_P4_01(native_bridge, monkeypatch):
    found = host_harness.probe('codex')
    if found is None or not found.usable:
        pytest.skip('OW-P4-01 requires a usable native Codex CLI0.164.0 on normal host PATH')
    if found.version != '0.164.0':
        pytest.fail(f'OW-P4-01 requires native Codex CLI0.164.0; actual probed version is {found.version}')
    native_bridge.permissions = {'docker': 'none', 'internet': 'full', 'commands': 'allow'}
    native_bridge.packages = {'codex-acp': '2.2.2'}
    actual = codex.codex_acp_env
    nonce = secrets.token_hex(16)
    def policy(level):
        env = actual(level)
        cfg = json.loads(env['CODEX_CONFIG'])
        cfg['shell_environment_policy']['set']['COLLIMATE_POLICY_ALLOWED'] = nonce
        env['CODEX_CONFIG'] = json.dumps(cfg)
        env['COLLIMATE_POLICY_PARENT'] = 'synthetic-parent-value'
        return env
    monkeypatch.setattr(codex, 'codex_acp_env', policy)
    model = model_for('openrouter')
    bind_model_account(model_account('openrouter', model=model, harness='codex'))
    script = ("import json,os; from pathlib import Path; "
              "d={'allowed':os.getenv('COLLIMATE_POLICY_ALLOWED'), "
              "'parent_present':'COLLIMATE_POLICY_PARENT' in os.environ, "
              "'key_names':[k for k in os.environ if k.endswith('API_KEY')]}; "
              "Path('codex-shell-policy.json').write_text(json.dumps(d)); print(json.dumps(d))")
    command = '/usr/bin/python3 -c ' + shlex.quote(script)
    result = run_on_test_loop(asyncio.wait_for(engine.run_agent(
        backend='codex', auth='api_key', provider='openrouter', model=model,
        commands='allow', websearch=False, enable_file_tools=True,
        runtime='standard', host_harness={'path': found.path, 'version': found.version},
        prompt='Use your shell execution tool to run exactly this command, once. '
               'Do not write or edit files using any other tool. Return the command output.\n'+command), timeout=180))
    receipt = Path(gen.workspace_path()) / 'codex-shell-policy.json'
    assert receipt.is_file(), 'The actual native shell command did not write its observation'
    assert json.loads(receipt.read_text()) == {'allowed': nonce, 'parent_present': False, 'key_names': []}
    assert any(row.get('type') == 'tool_use' for row in result.trace)
    print(f'[OW-P4-01] actual codex-acp1.13.1 + codex{found.version}: configured shell set observed; parent/provider key names absent')
