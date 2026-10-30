# SPDX-License-Identifier: MIT
"""Measured Cline strict raw native ACP gate; not an app host gate.

Native tool prompt name follows installed3.0.65 createMcpTools server__tool.
ACP ordering/title/result representations follow the actual six-record witness
and pinned installed primary source. Execute only after independent review.
"""
import asyncio
import json
import os
import uuid

import gen
import pytest

from _agent_shared import acp_run
from tests_live.conftest import (bind_model_account, eng, has_key, model_account,
                                requires_docker, run_on_test_loop)
from tests_live.fixtures.acp_capture import Capture, observed_profile
from tests_live.test_live_opencode_generator_tool import install_forward_recording_ack
from tests_live._host_terminal import _leg_model

PINS = {'cline': '3.0.73', 'mcp-remote': '0.15.2'}
NATIVE_TOOL = 'collimate__test_generator'
pytestmark = [requires_docker, pytest.mark.usefixtures('one_loop_per_test'),
              pytest.mark.skipif(not has_key('openrouter'),
                                 reason='needs authorized OPENROUTER_API_KEY')]


def test_capture_actual_cline_native_generator_allow_D23(docker_env_bridge, monkeypatch, tmp_path):
    """Observe the prepared native adapter/tool once through production engine."""
    bridge = docker_env_bridge
    bridge.channel = 'p4-cline-native-acp-capture'
    bridge.cell_id = 'p4-cline-native-acp-capture-cell'
    bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': 'allow'}
    bridge.packages = dict(PINS)
    install_forward_recording_ack(bridge, monkeypatch)
    model = _leg_model('openrouter', {'openrouter':'deepseek/deepseek-v4-flash'})
    bind_model_account(model_account('openrouter', model=model, harness='cline'))
    capture = Capture(secrets=(os.environ['OPENROUTER_API_KEY'],))
    # Registry preparation invokes accepted cline.prepare; the observer only
    # replaces the actual profile's transport and preserves its process lifetime.
    original_profile = acp_run._PROFILES['cline']

    async def prepare(context):
        return observed_profile(await original_profile(context), capture)

    monkeypatch.setitem(acp_run._PROFILES, 'cline', prepare)
    marker = 'd23-cline-acp-native-' + uuid.uuid4().hex
    child_prompt = 'private-cline-capture-' + uuid.uuid4().hex + '-long-input-' * 35
    arguments = {'path': 'draft/probe.generator', 'settings': {}, 'prompt': child_prompt}
    gen.update_file('draft/probe.generator',
                    'id: native-probe\nname: Native probe\nsummary: D23 fixture\nentrypoint: main.py\n')
    gen.update_file('draft/main.py', 'import gen\nasync def run():\n'
        f'    gen.update_file("native-child.txt", {marker!r})\n'
        '    await gen.commit()\n'
        f'    print({marker!r})\n'
        f'    print({("complete-cline-child-log-" * 80)!r})\n'
        'gen.main(run)\n')

    async def turn():
        await gen.commit()
        return await asyncio.wait_for(eng.run_agent(
            prompt=f'Call {NATIVE_TOOL} exactly once with these exact arguments: '
                   + json.dumps(arguments)
                   + '. Do not read or change the draft, run a shell, or create output yourself. '
                     'After the tool returns, briefly report its result and stop. Do not retry.',
            backend='cline', auth='api_key', provider='openrouter', model=model,
            system_prompt='', enable_file_tools=False, websearch=False,
            commands='allow'), timeout=420)

    receipt = tmp_path / 'cline-raw-acp.json'
    try:
        run_on_test_loop(turn())
        proof, native_result = capture.completed_generator_result('cline',
            expected_input=arguments, marker=marker)
        assert native_result['result']['success'] is True
        assert proof['input'] == arguments
        assert gen.read_file('native-child.txt') == marker
        assert bridge.read_cell_files(paths=['native-child.txt']) == {'native-child.txt': marker}
        print('[D23-raw-capture] cline: raw native tool/input/result and end_turn captured; '
              'measured native result and SDK recording child commit; app gate owed')
    finally:
        # Retain incomplete native/dialog/error witnesses as well as success.
        capture.write_private(receipt)
