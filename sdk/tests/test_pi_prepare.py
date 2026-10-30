# SPDX-License-Identifier: MIT
"""Actual Pi setup boundaries and retained native home protections."""
import asyncio
import json
from pathlib import Path

import pytest

from _agent_shared import acp_run as ar, cli_container as cc  # noqa: E402
from _agent_shared.harnesses.pi import backend as pb
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import HARNESS, resolve  # noqa: E402
from tests.test_backend_pi import (  # noqa: E402,F401
    selected_account_runtime, stub_terminal, _run_terminal, _RootEnv,
)

KEY = 'pi-synthetic-selected-provider-native-key'
MODEL = 'deepseek/deepseek-v4-flash'


@pytest.fixture
def prepared_runtime(monkeypatch, cache_root):  # noqa: F811
    env = _RootEnv()
    env.ensure = []
    env.spawn = []
    async def ensure(name, **kwargs):
        env.ensure.append((name, kwargs))
        return env
    async def key(provider, **kwargs):
        assert provider in ('openrouter', 'gemini')
        return KEY
    async def spawn(backend, target, argv, *, env_vars, cwd='/workspace'):
        assert target is env
        env.spawn.append((backend, list(argv), dict(env_vars), cwd))
        return object()
    monkeypatch.setattr(cc, 'ensure_env', ensure)
    monkeypatch.setattr(_pipeline, 'account_api_key', key)
    monkeypatch.setattr(pb, 'spawn_container_adapter', spawn)
    return env


def ctx(**over):
    return {'auth': 'api_key', 'provider': 'openrouter', 'model': MODEL,
            'commands': 'deny', 'system_prompt': '', **over}


def native(relative):
    return Path(pb.harness_home('pi')) / 'agent' / relative


def test_terminal_selected_native_auth_without_key_environment(stub_terminal):  # noqa: F811
    _run_terminal(auth='api_key', provider='openrouter', model=MODEL, commands='deny')
    assert json.loads(stub_terminal['files'][pb.PI_AUTH_PATH]) == {
        'openrouter': {'type': 'api_key', 'key': 'key-1'}}
    assert not any(name.endswith('API_KEY') for name in stub_terminal['serve']['env'])


def test_acp_selected_native_auth_without_key_environment(prepared_runtime):
    profile = asyncio.run(ar._PROFILES['pi'](ctx()))
    assert json.loads(native('auth.json').read_bytes()) == {
        'openrouter': {'type': 'api_key', 'key': KEY}}
    asyncio.run(profile.spawn())
    assert prepared_runtime.spawn[-1] == ('pi', ['pi-acp'], {
        'COLLIMATE_COMMANDS': 'deny', 'PI_ACP_PI_COMMAND': pb.PI_WRAPPER,
        'COLLIMATE_PI_STDERR': '1', 'COLLIMATE_PI_CONTEXT': f'{pb.PI_AGENT_DIR}/{pb.CONTEXT_FILE}'}, '/workspace')
    assert profile.model_applied is False
    assert profile.model_value == 'openrouter/' + MODEL


@pytest.mark.parametrize('leg', ['acp', 'terminal'])
@pytest.mark.parametrize('level', ['deny', 'ask', 'allow'])
def test_actual_prepare_preserves_wrapper_command_level(prepared_runtime, leg, level):
    result = asyncio.run(_pipeline.prepare(_HARNESS['pi'], ctx(commands=level, system_prompt='session context'), leg=leg))
    assert result.commands.level == level
    assert result.env['COLLIMATE_COMMANDS'] == level
    assert not any(name.endswith('API_KEY') for name in result.env)
    assert KEY not in json.dumps(result.env)
    assert KEY not in json.dumps(result.argv)
    assert result.env_obj is prepared_runtime
    if leg == 'terminal':
        assert result.argv == (pb.PI_WRAPPER, '--model', 'openrouter/' + MODEL,
                               '--append-system-prompt', 'session context')
    else:
        assert result.profile.commands == level
        asyncio.run(result.profile.spawn())
        assert prepared_runtime.spawn[-1][2]['COLLIMATE_COMMANDS'] == level
    assert prepared_runtime.root[-1] == f'rm -f {pb.PI_MODELS_PATH} {pb.PI_AUTH_PATH}'


@pytest.mark.parametrize('leg', ['acp', 'terminal'])
def test_reprepare_replaces_stale_executable_auth_and_repairs_permissions(prepared_runtime, leg):
    asyncio.run(_pipeline.prepare(_HARNESS['pi'], ctx(), leg=leg))
    native('models.json').write_text('{"providers":{"openrouter":{"apiKey":"!touch outside"}}}')
    native('auth.json').write_text('{"openrouter":{"type":"api_key","key":"!touch outside"}}')
    native('auth.json').chmod(0o644)
    native('settings.json').chmod(0o666)
    asyncio.run(_pipeline.prepare(_HARNESS['pi'], ctx(provider='gemini', model='gemini-test'), leg=leg))
    # The stale `!command` apiKey is gone: models.json now holds ONLY the
    # registry endpoint as the provider's baseUrl.
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    assert json.loads(native('models.json').read_bytes()) == {
        'providers': {'google': {'baseUrl': STANDARD_MODEL_ENDPOINTS['gemini']}}}
    assert '!' not in native('models.json').read_text() and KEY not in native('models.json').read_text()
    assert json.loads(native('auth.json').read_bytes()) == {'google': {'type': 'api_key', 'key': KEY}}
    assert json.loads(native('settings.json').read_bytes()) == {'quietStartup': True}
    assert native('auth.json').stat().st_mode & 0o777 == 0o600
    assert native('settings.json').stat().st_mode & 0o777 == 0o600
    assert KEY not in json.dumps(prepared_runtime.ensure)


@pytest.mark.parametrize('relative', ['agent', 'agent/auth.json', 'agent/settings.json',
                                       'agent/models.json'])
def test_reused_home_rejects_planted_native_paths(prepared_runtime, tmp_path, relative):
    asyncio.run(_pipeline.prepare(_HARNESS['pi'], ctx(), leg='terminal'))
    root = Path(pb.harness_home('pi'))
    target = root / relative
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_text('unchanged')
    import shutil
    if target.is_dir():
        shutil.rmtree(target)
        target.symlink_to(outside, target_is_directory=True)
    else:
        target.unlink(missing_ok=True)
        target.symlink_to(sentinel)
    with pytest.raises((OSError, PermissionError)):
        asyncio.run(_pipeline.prepare(_HARNESS['pi'], ctx(), leg='terminal'))
    assert sentinel.read_text() == 'unchanged'
    assert sorted(p.name for p in outside.iterdir()) == ['sentinel']
    assert prepared_runtime.spawn == []


def test_pi_registry_dispatch_is_actual_prepare():
    assert resolve(HARNESS['pi'].prepare) is pb.prepare
    assert not hasattr(ar, '_profile_pi')


def test_missing_plan_fails_before_environment_or_key(prepared_runtime):
    with pytest.raises(RuntimeError, match='Key manager'):
        asyncio.run(_pipeline.prepare(_HARNESS['pi'], ctx(auth='chatgpt')))
    assert prepared_runtime.ensure == []


@pytest.mark.parametrize('leg', ['acp', 'terminal'])
def test_direct_blank_chatgpt_model_has_same_plan_default_F1555(stub_terminal, leg):  # noqa: F811
    # Skip config parsing deliberately; a stale provider dropdown cannot pick
    # an API-family default for the selected ChatGPT plan.
    if leg == 'acp':
        profile = asyncio.run(ar._PROFILES['pi'](ctx(auth='chatgpt', provider='anthropic', model='')))
        assert profile.model_applied is False
        assert profile.model_value == 'openai-codex/gpt-5.5'
    else:
        _run_terminal(auth='chatgpt', provider='anthropic', model='')
        assert stub_terminal['serve']['command'][:3] == [pb.PI_WRAPPER, '--model', 'openai-codex/gpt-5.5']
    assert json.loads(stub_terminal['files'][pb.PI_SETTINGS_PATH]) == {
        'quietStartup': True, 'defaultProvider': 'openai-codex', 'defaultModel': 'gpt-5.5'}


@pytest.mark.parametrize('provider,pi_id,suffix_dropped', [
    ('openrouter', 'openrouter', False), ('anthropic', 'anthropic', True),
    ('mistral', 'mistral', True), ('openai', 'openai', False)])
def test_models_json_holds_only_the_registry_base_url(provider, pi_id, suffix_dropped):
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    url = STANDARD_MODEL_ENDPOINTS[provider]
    want = url.removesuffix('/v1') if suffix_dropped else url
    assert pb.models_json(pi_id, provider) == {'providers': {pi_id: {'baseUrl': want}}}
    assert pb.models_json(pi_id, 'copilot') is None


@pytest.mark.parametrize('model,path', [
    # pi's Anthropic Messages client appends /v1/messages to the bare base.
    ('anthropic/claude-sonnet-5.5', '/api/v1/messages'),
    ('anthropic/claude-opus-5', '/api/v1/messages'),
    # A batch variant and every other model go over chat completions.
    ('anthropic/claude-sonnet-5.5:batch', '/api/v1/chat/completions'),
    ('openai/gpt-4.1-mini', '/api/v1/chat/completions'),
])
def test_an_openrouter_model_is_billed_at_the_registry_endpoint_whatever_client_pi_uses(model, path):
    """The models.json override is provider-wide: the URL pi's client for the
    selected model builds from it is the registry endpoint's own path, never
    a doubled ``/v1``."""
    from urllib.parse import urlsplit
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    base = pb.models_json('openrouter', 'openrouter', model)['providers']['openrouter']['baseUrl']
    anthropic_client = model.startswith('anthropic/claude-') and ':' not in model
    built = base.rstrip('/') + ('/v1/messages' if anthropic_client else '/chat/completions')
    assert urlsplit(built).path == path
    assert built.startswith(STANDARD_MODEL_ENDPOINTS['openrouter'].removesuffix('/v1'))
