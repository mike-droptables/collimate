# SPDX-License-Identifier: MIT
"""Raw ACP witnesses; measured native decoders and actual app proof are separate.

Existing terminal evidence keeps its own qualification. These cases use the
actual prepared native ACP leg and preserve its explicit prompt response.
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
    'codex': {'codex': '0.164.0', 'codex-acp': '2.2.2'},
    'qwen-code': {'qwen-code': '0.26.1'},
}
MAX_REPLY_DIAGNOSTIC_BYTES = 256 * 1024


def write_reply_diagnostic(path, *, engine_returned, text, exception_type, selected_key):
    """Normalized reply context only; never native tool or turn authority."""
    assert type(engine_returned) is bool
    assert text is None or isinstance(text, str)
    assert exception_type is None or isinstance(exception_type, str)
    assert isinstance(selected_key, str) and selected_key
    value = {'authority': 'normalized_diagnostic_only',
             'engine_returned': engine_returned, 'text': text,
             'exception_type': exception_type}
    # Scrub selected credentials from every stored string, without truncation.
    value = {key: item.replace(selected_key, '[REDACTED]') if isinstance(item, str)
             else item for key, item in value.items()}
    body = json.dumps(value, ensure_ascii=False, indent=2).encode()
    assert len(body) <= MAX_REPLY_DIAGNOSTIC_BYTES, 'Normalized reply diagnostic byte bound exceeded'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(body)


pytestmark = [requires_docker, pytest.mark.usefixtures('one_loop_per_test'),
              pytest.mark.skipif(not has_key('openrouter'),
                                 reason='needs authorized OPENROUTER_API_KEY')]


@pytest.mark.parametrize('backend', ['codex', 'qwen-code'])
def test_capture_actual_codex_qwen_native_generator_allow_D23(docker_env_bridge, monkeypatch, tmp_path, backend):
    """Capture one real native call without guessing its result encoding."""
    bridge = docker_env_bridge
    bridge.channel = 'p4-' + backend + '-native-acp-capture'
    bridge.cell_id = 'p4-' + backend + '-native-acp-capture-cell'
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
    marker = 'd23-' + backend + '-native-' + uuid.uuid4().hex
    child_prompt = 'private-native-capture-' + uuid.uuid4().hex + '-long-input-' * 35
    arguments = {'path': 'draft/probe.generator', 'settings': {}, 'prompt': child_prompt}
    gen.update_file('draft/probe.generator',
                    'id: native-probe\nname: Native probe\nsummary: D23 fixture\nentrypoint: main.py\n')
    gen.update_file('draft/main.py', 'import gen\nasync def run():\n'
        f'    gen.update_file("native-child.txt", {marker!r})\n'
        '    await gen.commit()\n'
        f'    print({marker!r})\n'
        f'    print({("complete-native-child-log-" * 80)!r})\n'
        'gen.main(run)\n')

    async def turn():
        await gen.commit()
        return await asyncio.wait_for(eng.run_agent(
            prompt='Call the collimate MCP test_generator tool exactly once with these exact arguments: '
                   + json.dumps(arguments)
                   + '. Do not read or change the draft, run a shell, or create output yourself. '
                     'After the tool returns, briefly report its result and stop. Do not retry.',
            backend=backend, auth='api_key', provider='openrouter', model=model,
            system_prompt='', enable_file_tools=False, websearch=False,
            commands='allow'), timeout=420)

    engine_returned, reply_text, exception_type = False, None, None
    try:
        result = run_on_test_loop(turn())
        engine_returned = True
        reply_text = result.text
        proof, _ = capture.completed_generator_result(backend, expected_input=arguments,
                                                     marker=marker)
        assert proof['input'] == arguments
        assert gen.read_file('native-child.txt') == marker
        assert bridge.read_cell_files(paths=['native-child.txt']) == {'native-child.txt': marker}
        print(f'[D23-first-raw-capture] {backend}: native identity/full input/result '
              'and normal ACP response captured; actual app proof remains owed')
    except BaseException as error:
        exception_type = type(error).__name__
        raise
    finally:
        try:
            capture.write_private(tmp_path / f'{backend}-raw-acp.json')
        finally:
            write_reply_diagnostic(tmp_path / f'{backend}-normalized-reply.json',
                                   engine_returned=engine_returned, text=reply_text,
                                   exception_type=exception_type,
                                   selected_key=os.environ['OPENROUTER_API_KEY'])
