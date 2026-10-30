# SPDX-License-Identifier: MIT
"""Actual pinned native UI rewrite; synthetic credential and no submitted turn."""
import json
from pathlib import Path
import stat

import pytest
from tests_live.conftest import requires_docker, run_on_test_loop
from tests_live._host_terminal import _HostTerminal
from tests_live._host_terminal import _up_to_prompt
from _agent_shared import harness_homes
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses.qwen_code import backend as qwen

pytestmark=[requires_docker,pytest.mark.usefixtures('one_loop_per_test')]


def test_native_qwen_startup_rewrite_keeps_selected_credential_private_P4F21(docker_env_bridge,monkeypatch):
    async def fake_key(provider):
        return 'qwen-native-rewrite-synthetic-not-a-real-key'
    monkeypatch.setattr(qwen,'account_api_key',fake_key)
    docker_env_bridge.permissions={'docker':'full','internet':'full','commands':'allow'}
    docker_env_bridge.packages={'qwen-code':'0.26.1'}
    result=run_on_test_loop(_pipeline.prepare(_HARNESS['qwen-code'], {'auth':'api_key','provider':'openrouter',
        'model':'deepseek/deepseek-v4-flash','commands':'allow','websearch':False,
        'system_prompt':''},leg='terminal'))
    path=Path(harness_homes.harness_home('qwen-code'))/'settings.json'
    initial=path.stat()
    assert stat.S_IMODE(initial.st_mode)==0o600
    term=_HostTerminal(result.env_obj.container_id,list(result.argv),'/workspace',
                       {'TERM':'xterm-256color',**result.env})
    try:
        _,blocked=_up_to_prompt('qwen-code',term)
        assert not blocked
        rewritten=path.stat()
        assert rewritten.st_ino!=initial.st_ino, 'Actual native startup did not rewrite its settings'
        data=json.loads(path.read_text())
        assert data['security']['auth']['apiKey']=='qwen-native-rewrite-synthetic-not-a-real-key'
        assert stat.S_IMODE(rewritten.st_mode)==0o600
        print('[native-rewrite] Qwen0.23.3 startup rewrote settings with selected synthetic key and mode0600; no prompt/provider turn')
    finally:
        term.stop()
