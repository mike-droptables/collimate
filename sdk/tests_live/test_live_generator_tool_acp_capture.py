# SPDX-License-Identifier: MIT
"""Strict raw native D23 gate; SDK recording publication is not app proof.

Run only after fixture review in the coordinated native window. A capture
correlates actual ACP IDs/full payloads/completed prompt response and compares
the measured native result representations. App approval/publication needs its
own affected runtime evidence.
"""
import asyncio
import json
import os
import uuid

import gen
import pytest

from _agent_shared import acp_run
from tests_live.conftest import (bind_model_account, eng, has_key, model_account,
                                model_for, requires_docker, run_on_test_loop)
from tests_live.fixtures.acp_capture import Capture, observed_profile
from tests_live.test_live_opencode_generator_tool import install_forward_recording_ack


PINS = {
    'pi': {'pi-coding-agent': '1.4.0', 'pi-acp': '0.0.34', 'pi-mcp-sdk': '1.25.3'},
    'opencode': {'opencode': '1.19.4'},
}
pytestmark = [requires_docker, pytest.mark.usefixtures('one_loop_per_test'),
              pytest.mark.skipif(not has_key('openrouter'),
                                 reason='needs authorized OPENROUTER_API_KEY')]


@pytest.mark.parametrize('backend', ['pi', 'opencode'])
def test_capture_actual_native_generator_tool_allow_D23(docker_env_bridge, monkeypatch, tmp_path, backend):
    """Capture once through the actual engine, native adapter and MCP child."""
    bridge = docker_env_bridge
    bridge.channel = 'p4-native-acp-capture'
    bridge.cell_id = 'p4-native-acp-capture-cell'
    bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': 'allow'}
    bridge.packages = dict(PINS[backend])
    install_forward_recording_ack(bridge, monkeypatch)
    model = model_for('openrouter')
    bind_model_account(model_account('openrouter', model=model, harness=backend))
    capture = Capture(secrets=(os.environ['OPENROUTER_API_KEY'],))
    original_profile = acp_run._PROFILES[backend]

    async def prepare(context):
        return observed_profile(await original_profile(context), capture)

    monkeypatch.setitem(acp_run._PROFILES, backend, prepare)
    marker = 'd23-acp-native-' + uuid.uuid4().hex
    child_prompt = 'private-capture-' + uuid.uuid4().hex + '-long-input-' * 35
    arguments = {'path': 'draft/probe.generator', 'settings': {}, 'prompt': child_prompt}
    gen.update_file('draft/probe.generator',
                    'id: native-probe\nname: Native probe\nsummary: D23 fixture\nentrypoint: main.py\n')
    gen.update_file('draft/main.py', 'import gen\nasync def run():\n'
        f'    gen.update_file("native-child.txt", {marker!r})\n'
        '    await gen.commit()\n'
        f'    print({marker!r})\n'
        f'    print({("complete-child-log-" * 80)!r})\n'
        'gen.main(run)\n')

    async def turn():
        await gen.commit()
        return await asyncio.wait_for(eng.run_agent(
            prompt='Call collimate_test_generator exactly once with these exact arguments: '
                   + json.dumps(arguments)
                   + '. Do not read or change the draft, run a shell, or create output yourself. '
                     'After the tool returns, briefly report its result and stop. Do not retry.',
            backend=backend, auth='api_key', provider='openrouter', model=model,
            system_prompt='', enable_file_tools=False, websearch=False,
            commands='allow'), timeout=420)

    receipt = tmp_path / f'{backend}-raw-acp.json'
    try:
        run_on_test_loop(turn())
        proof, native_result = capture.completed_generator_result(
            backend, expected_input=arguments, marker=marker)
        assert native_result['result']['success'] is True
        # The native result gate and these recording-host assertions are SDK
        # fixture evidence; they do not establish actual app publication.
        assert proof['input'] == arguments
        assert gen.read_file('native-child.txt') == marker
        assert bridge.read_cell_files(paths=['native-child.txt']) == {'native-child.txt': marker}
        print(f'[D23-raw-capture] {backend}: one native tool/full input/result '
              'and end_turn captured; publication uses SDK recording host')
    finally:
        capture.write_private(receipt)
