# SPDX-License-Identifier: MIT
"""Codex native preparation and retained native trace regressions."""
import asyncio
import json
import stat
from pathlib import Path

import pytest
from _agent_shared import acp_run, harness_homes
from _agent_shared.harnesses.codex import backend as codex
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from tests._commands_matrix import recording_runtime  # noqa: F401


def context(**values):
    return {'auth':'api_key','provider':'openrouter','model':'deepseek/deepseek-v4-flash',
            'system_prompt':'','enable_file_tools':True,'websearch':True,
            'commands':'ask','runtime':'docker','host_harness':None,**values}


def test_codex_full_native_tool_output_is_retained_F1566():
    output = 'begin\n' + 'meaningful result\n'*400 + 'FINAL-SENTINEL'
    native = json.dumps({'type':'response_item','payload':{'type':'function_call_output','output':output}})
    assert codex.trace_from_codex_session(native) == [{'type':'tool_result','output':output}]


def test_codex_selected_key_is_private_native_file_before_spawn(recording_runtime):  # noqa: F811
    profile = asyncio.run(acp_run._PROFILES['codex'](context()))
    asyncio.run(profile.spawn())
    assert not any(name.endswith('API_KEY') for name in recording_runtime.spawn['env_vars'])
    config = Path(harness_homes.harness_home('codex')) / 'config.toml'
    import tomllib
    native = tomllib.loads(config.read_text())
    assert native['model_providers']['collimate']['experimental_bearer_token'] == 'matrix-fake-key'
    assert 'env_key' not in native['model_providers']['collimate']
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


@pytest.mark.parametrize('leg', ['acp','terminal'])
def test_codex_prepare_policy_is_actual_native_spawn_policy(recording_runtime, leg):  # noqa: F811
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['codex'], context(), leg=leg))
    assert prepared.commands.level == 'ask'
    assert prepared.commands.native['INITIAL_AGENT_MODE'] == 'read-only'
    policy = json.loads(prepared.commands.native['CODEX_CONFIG'])
    assert 'code_mode' not in policy['features']
    assert policy['features']['shell_snapshot'] is False
    assert policy['allow_login_shell'] is False
    assert policy['shell_environment_policy']['inherit'] == 'none'
    for name, value in prepared.commands.native.items():
        if leg == 'acp':
            assert prepared.env[name] == value
    assert prepared.argv[0] == ('codex-acp' if leg == 'acp' else 'codex')


@pytest.mark.parametrize('placement,relative', [
    ('docker','config.toml'), ('docker','auth.json'), ('docker','AGENTS.md'),
    ('standard','config.toml'), ('standard','auth.json'), ('standard','AGENTS.md'),
])
def test_repeated_codex_prepare_refuses_native_leaf_symlinks(recording_runtime, tmp_path, placement, relative):  # noqa: F811
    ctx = context(runtime=placement, system_prompt='initial context',
                  host_harness={'path':'/fake/codex','version':'0.157.0'})
    asyncio.run(_pipeline.prepare(_HARNESS['codex'], ctx))
    home = Path(harness_homes.harness_home('codex'))
    target = home / relative
    target.unlink()
    outside = tmp_path / 'unrelated'
    outside.write_text('unchanged outside')
    target.symlink_to(outside)
    with pytest.raises((OSError, ValueError, PermissionError)):
        asyncio.run(_pipeline.prepare(_HARNESS['codex'], {**ctx,'system_prompt':'new context'}))
    assert outside.read_text() == 'unchanged outside'
    assert target.is_symlink()


def test_codex_terminal_native_config_contains_effective_shell_policy(recording_runtime):  # noqa: F811
    import tomllib
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['codex'], context(), leg='terminal'))
    config = tomllib.loads(Path(harness_homes.harness_home('codex'), 'config.toml').read_text())
    effective = json.loads(prepared.commands.native['CODEX_CONFIG'])
    assert config['allow_login_shell'] is False
    assert config['features']['shell_snapshot'] is False
    assert config['shell_environment_policy'] == effective['shell_environment_policy']
    assert config['shell_environment_policy']['set']['HOME'] == '/home/coder'


@pytest.mark.parametrize('leg,placement,expected', [('terminal', 'docker', True),
    ('acp', 'docker', False), ('acp', 'standard', False)])
def test_codex_marker_is_owned_only_by_successful_terminal_prepare_F1554_F1567(recording_runtime, leg, placement, expected):  # noqa: F811
    ctx = context(runtime=placement, host_harness={'path': '/fake/codex', 'version': '0.157.0'})
    asyncio.run(_pipeline.prepare(_HARNESS['codex'], ctx, leg=leg))
    marker = Path(harness_homes.harness_home('codex'), codex._START_MARKER)
    assert marker.exists() is expected
    if expected:
        assert marker.read_text() == '' and marker.is_file() and not marker.is_symlink()


def test_codex_failed_terminal_prepare_never_advances_existing_start_marker_F1554_F1567(recording_runtime, monkeypatch):  # noqa: F811
    marker = Path(harness_homes.harness_home('codex'), codex._START_MARKER)
    marker.write_text('unchanged older marker')
    before = marker.stat().st_mtime_ns
    def broken(*args):
        raise RuntimeError('native policy preparation failed')
    monkeypatch.setattr(codex, 'write_codex_locks', broken)
    with pytest.raises(RuntimeError, match='native policy preparation failed'):
        asyncio.run(_pipeline.prepare(_HARNESS['codex'], context(), leg='terminal'))
    assert marker.read_text() == 'unchanged older marker' and marker.stat().st_mtime_ns == before


def test_codex_terminal_prepare_marker_refuses_symlink_without_touching_target_F1554_F1567(recording_runtime, monkeypatch, tmp_path):  # noqa: F811
    marker = Path(harness_homes.harness_home('codex'), codex._START_MARKER)
    outside = tmp_path / 'unrelated-marker'
    outside.write_text('unrelated bytes')
    marker.symlink_to(outside)
    with pytest.raises((OSError, ValueError, PermissionError)):
        asyncio.run(_pipeline.prepare(_HARNESS['codex'], context(), leg='terminal'))
    assert marker.is_symlink() and outside.read_text() == 'unrelated bytes'


def test_a_blank_codex_model_writes_no_model_and_codex_picks_its_own(recording_runtime):  # noqa: F811
    """The blank openai default is codex's own pick: the private config.toml
    names no model and the ACP leg applies none (codex_model_blank)."""
    import tomllib
    import _agent_shared as engine
    from collimate.permissions import Grant
    cfg = engine.parse_agent_config({"surface": "annotation", 'backend':'codex','provider':'openai'},
                                    grant=Grant.from_dict({}), can_ask=True)
    assert cfg['model'] == ''
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['codex'], context(provider='openai', model=cfg['model'])))
    config = tomllib.loads(Path(harness_homes.harness_home('codex'), 'config.toml').read_text())
    assert 'model' not in config
    assert prepared.profile.model_applied is False


def test_an_explicit_codex_model_is_written_verbatim(recording_runtime):  # noqa: F811
    import tomllib
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['codex'], context(provider='openai', model='gpt-5.5')))
    config = tomllib.loads(Path(harness_homes.harness_home('codex'), 'config.toml').read_text())
    assert config['model'] == 'gpt-5.5'
    assert prepared.profile.model_applied is True
