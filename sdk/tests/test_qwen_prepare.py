# SPDX-License-Identifier: MIT
"""Qwen preparation owns effective native policy and private selected auth."""
import asyncio
import json
import stat
from pathlib import Path

import pytest
from _agent_shared import surface_runner, acp_run, harness_homes
from _agent_shared.harnesses.qwen_code import backend as qwen
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from tests._commands_matrix import TerminalRecorded, recording_runtime  # noqa: F401


def context(**values):
    return {'auth':'api_key','provider':'openrouter','model':'deepseek/deepseek-v4-flash',
            'system_prompt':'','enable_file_tools':True,'websearch':True,
            'commands':'ask','runtime':'docker','host_harness':None,**values}


def test_qwen_selected_key_is_native_private_file_before_spawn(recording_runtime):  # noqa: F811
    profile=asyncio.run(acp_run._PROFILES['qwen-code'](context()))
    asyncio.run(profile.spawn())
    assert not any(name.endswith('API_KEY') for name in recording_runtime.spawn['env_vars'])
    path=Path(harness_homes.harness_home('qwen-code'))/'settings.json'
    data=json.loads(path.read_text())
    assert data['security']['auth']['apiKey']=='matrix-fake-key'
    assert data['security']['auth']['selectedType']=='openai'
    assert data['model']['name']=='deepseek/deepseek-v4-flash'
    assert stat.S_IMODE(path.stat().st_mode)==0o600


@pytest.mark.parametrize('leg',['acp','terminal'])
def test_qwen_prepare_exposes_effective_policy(recording_runtime,leg):  # noqa: F811
    prepared=asyncio.run(_pipeline.prepare(_HARNESS['qwen-code'], context(),leg=leg))
    assert prepared.commands.level=='ask'
    assert prepared.commands.native==qwen.system_settings('ask',websearch=True)
    assert prepared.env['QWEN_CODE_SYSTEM_SETTINGS_PATH']==qwen.SYSTEM_SETTINGS_PATH
    assert prepared.env['QWEN_CODE_SAFE_MODE']=='0'
    assert prepared.env['QWEN_CODE_SIMPLE']=='0'
    assert prepared.argv[-2:]==('--approval-mode','default')
    if leg=='acp':
        assert prepared.profile.one_session_per_process is True
        assert prepared.profile.session_mode=='default'


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
def test_qwen_acp_blocks_native_discovery_before_first_prompt_P4F35(recording_runtime, commands):  # noqa: F811
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['qwen-code'], context(commands=commands)))
    assert prepared.env == {
        'QWEN_CODE_SYSTEM_SETTINGS_PATH': qwen.SYSTEM_SETTINGS_PATH,
        'QWEN_CODE_SAFE_MODE': '0', 'QWEN_CODE_SIMPLE': '0',
        'QWEN_CODE_LEGACY_MCP_BLOCKING': '1',
    }
    assert prepared.profile.one_session_per_process is True
    assert prepared.profile.commands == commands
    asyncio.run(prepared.profile.spawn())
    assert recording_runtime.spawn['env_vars'] == prepared.env
    assert recording_runtime.spawn['argv'] == list(prepared.argv)
    assert recording_runtime.spawn['backend'] == 'qwen-code'


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
def test_qwen_terminal_keeps_native_discovery_environment_P4F35(recording_runtime, commands):  # noqa: F811
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['qwen-code'], context(commands=commands), leg='terminal'))
    assert prepared.env == {
        'QWEN_CODE_SYSTEM_SETTINGS_PATH': qwen.SYSTEM_SETTINGS_PATH,
        'QWEN_CODE_SAFE_MODE': '0', 'QWEN_CODE_SIMPLE': '0',
    }
    with pytest.raises(TerminalRecorded):
        asyncio.run(surface_runner("qwen-code", "terminal")({**context(commands=commands), "backend": "qwen-code"},
            pointer='_runs/matrix-12345678/terminal/session.terminal'))
    assert recording_runtime.terminal['env'] == {
        'TERM': 'xterm-256color', **prepared.env,
    }
    assert recording_runtime.terminal['argv'] == list(prepared.argv)
    assert recording_runtime.spawn == {}


@pytest.mark.parametrize('leg,relative', [('acp','settings.json'), ('acp','QWEN.md'),
    ('terminal','settings.json'), ('terminal','QWEN.md')])
def test_qwen_reused_home_refuses_planted_native_path(recording_runtime,tmp_path,leg,relative):  # noqa: F811
    asyncio.run(_pipeline.prepare(_HARNESS['qwen-code'], context(system_prompt='trusted context'),leg=leg))
    home=Path(harness_homes.harness_home('qwen-code'))
    outside=tmp_path/'outside'
    outside.mkdir()
    sentinel=outside/'sentinel'
    sentinel.write_text('outside unchanged')
    leaf=home/relative
    if leaf.is_dir():
        import shutil
        shutil.rmtree(leaf)
    elif leaf.exists():
        leaf.unlink()
    leaf.symlink_to(sentinel)
    with pytest.raises((PermissionError,OSError)):
        asyncio.run(_pipeline.prepare(_HARNESS['qwen-code'], context(system_prompt='replacement context'),leg=leg))
    assert sentinel.read_text()=='outside unchanged'
    assert sorted(path.name for path in outside.iterdir())==['sentinel']


@pytest.mark.parametrize('leg', ['acp', 'terminal'])
def test_qwen_deny_blocks_native_agent_and_bare_reload_P4F23(recording_runtime, leg):  # noqa: F811
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['qwen-code'], context(commands='deny'), leg=leg))
    assert prepared.commands.native['permissions']['deny'] == ['run_shell_command', 'agent']
    flags = list(prepared.argv)
    assert flags[flags.index('--exclude-tools') + 1] == 'run_shell_command,agent'
