# SPDX-License-Identifier: MIT
"""Cline native bridge delivery and selected credential confinement."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _agent_shared import acp_run as ar, cli_container as cc  # noqa: E402
from _agent_shared.harnesses.cline import backend as cb
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from tests.test_backend_cline import selected_account_runtime, stub_terminal, _run_terminal, _PolicyEnv  # noqa: E402,F401
from _agent_shared.harnesses import HARNESS, resolve  # noqa: E402


@pytest.mark.parametrize('level', ['deny', 'ask', 'allow'])
def test_native_acp_binding_delivers_own_server_before_spawn_D23_F1403_F1404(monkeypatch, cache_root, level):  # noqa: F811
    env = _PolicyEnv()
    async def ensure(*args, **kwargs):
        assert 'env_vars' not in kwargs
        return env
    requested_providers = []
    async def key(provider):
        requested_providers.append(provider)
        return 'synthetic-cline-selected-key'
    monkeypatch.setattr(cc, 'ensure_env', ensure)
    monkeypatch.setattr(_pipeline, 'account_api_key', key)
    context = {'auth': 'api_key', 'provider': 'openrouter', 'model': 'deepseek/deepseek-v4-flash',
               'commands': level, 'websearch': True, 'enable_file_tools': True, 'system_prompt': ''}
    profile = asyncio.run(ar._PROFILES['cline'](context))
    assert requested_providers == ['openrouter']
    assert profile.bind_bridge is not None
    asyncio.run(profile.bind_bridge(SimpleNamespace(url_container='http://127.0.0.1:8765/owned-leg/mcp')))
    server = json.loads(env.files[cb.CLINE_MCP_POLICY_SETTINGS])['mcpServers']['collimate']
    assert server['transport'] == {'type': 'stdio', 'command': 'mcp-remote',
                                    'args': ['http://127.0.0.1:8765/owned-leg/mcp',
                                             '--allow-http', '--transport', 'http-only']}
    assert profile.commands == level
    assert profile.verify_model is True


def test_terminal_key_parent_link_cannot_write_outside_home_F1403(cache_root, tmp_path):  # noqa: F811
    parent = Path(cb.key_settings_host_dir())
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'providers.json'
    sentinel.write_text('preserved')
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, PermissionError)):
        cb.write_key_settings('openrouter', 'synthetic-selected-key', 'deepseek/deepseek-v4-flash')
    assert sentinel.read_text() == 'preserved'


@pytest.mark.parametrize('leg', ['acp', 'terminal'])
@pytest.mark.parametrize('level', ['deny', 'ask', 'allow'])
def test_selected_key_native_route_and_complete_policy_F1404_F1560(monkeypatch, cache_root, leg, level):  # noqa: F811
    key_value = 'synthetic-selected-cline-key'
    env = _PolicyEnv()
    placements, spawns = [], []
    async def ensure(name, **kwargs):
        placements.append((name, kwargs))
        return env
    async def key(provider):
        assert provider == 'openrouter'
        return key_value
    async def spawn(name, target, argv, *, env_vars):
        assert target is env
        spawns.append((name, argv, env_vars))
    monkeypatch.setattr(cc, 'ensure_env', ensure)
    monkeypatch.setattr(_pipeline, 'account_api_key', key)
    monkeypatch.setattr(cb, 'spawn_container_adapter', spawn)
    result = asyncio.run(_pipeline.prepare(_HARNESS['cline'], {'auth': 'api_key', 'provider': 'openrouter',
        'model': 'deepseek/deepseek-v4-flash', 'commands': level}, leg=leg))
    assert result.commands.level == level
    assert key_value not in json.dumps(placements)
    assert key_value not in json.dumps(env.root)
    assert key_value not in json.dumps(result.argv)
    assert json.loads(env.files[cb.CLINE_MCP_POLICY_SETTINGS]) == {'mcpServers': {}}
    if level == 'allow':
        assert cb.CLINE_POLICY_SETTINGS not in env.files
    else:
        assert json.loads(env.files[cb.CLINE_POLICY_SETTINGS]) == cb.cline_policy_settings(level)
    native = Path(cb.key_settings_host_dir()) / 'providers.json'
    if leg == 'acp':
        # The ACP leg's key stays in its env; its providers.json (Collimate's
        # per-cell file, not the plan login store) holds only the endpoint.
        from collimate.providers import STANDARD_MODEL_ENDPOINTS
        assert result.env['CLINE_PROVIDER_SETTINGS_PATH'] == cb.CLINE_KEY_SETTINGS_FILE
        assert json.loads(native.read_text())['providers']['openrouter']['settings'] == {
            'provider': 'openrouter', 'baseUrl': STANDARD_MODEL_ENDPOINTS['openrouter']}
        assert key_value not in native.read_text()
        asyncio.run(result.profile.spawn())
        assert spawns[0][2]['CLINE_API_KEY'] == key_value
        assert spawns[0][2]['CLINE_PROVIDER_SETTINGS_PATH'] == cb.CLINE_KEY_SETTINGS_FILE
        assert result.profile.verify_model is True
    else:
        assert 'CLINE_API_KEY' not in result.env
        assert result.env['CLINE_PROVIDER_SETTINGS_PATH'] == cb.CLINE_KEY_SETTINGS_FILE
        assert json.loads(native.read_text())['providers']['openrouter']['settings']['apiKey'] == key_value
        assert native.stat().st_mode & 0o777 == 0o600
        cb.remove_key_settings()
        assert not native.exists()


def test_cleanup_refuses_linked_native_rewrite_stage_F1403(cache_root, tmp_path):  # noqa: F811
    cb.write_key_settings('openrouter', 'synthetic-selected-key')
    outside = tmp_path / 'outside-secret'
    outside.write_text('unchanged')
    stage = Path(cb.key_settings_host_dir()) / 'providers.json.123.tmp'
    stage.symlink_to(outside)
    with pytest.raises(PermissionError):
        cb.remove_key_settings()
    assert outside.read_text() == 'unchanged'
    assert not (stage.parent / 'providers.json').exists()


def test_registry_uses_actual_cline_prepare_F1403():
    assert resolve(HARNESS['cline'].prepare) is cb.prepare


@pytest.mark.parametrize('level', ['deny', 'ask', 'allow'])
def test_failed_terminal_start_closes_bridge_and_clears_native_binding_D23(stub_terminal, monkeypatch, level):  # noqa: F811
    from contextlib import asynccontextmanager
    events = []
    env = _PolicyEnv()
    env.read_file = lambda path: env.files.get(path, '').encode()
    stub_terminal['env'] = env
    @asynccontextmanager
    async def bridge(**kwargs):
        events.append(('opened', kwargs))
        try:
            yield SimpleNamespace(url_container='http://127.0.0.1:8765/terminal-own/mcp')
        finally:
            events.append(('closed', kwargs))
    monkeypatch.setattr(cc, 'leg_bridge', bridge)
    _run_terminal(auth='api_key', provider='openrouter', model='deepseek/deepseek-v4-flash', commands=level)
    assert [event[0] for event in events] == ['opened', 'closed']
    names = {row[0] for row in events[0][1]['extra_entries'] or []}
    assert ('test_generator' in names) is (level != 'deny')
    assert json.loads(env.files[cb.CLINE_MCP_POLICY_SETTINGS]) == {'mcpServers': {}}
    assert not (Path(cb.key_settings_host_dir()) / 'providers.json').exists()


def test_stale_native_discovery_never_signals_a_process_F1403():
    calls = []
    class Env:
        def exec(self, argv, **kwargs):
            calls.append(argv)
            return 0, json.dumps({'running': False, 'pid': 123})
    cb._stop_owned_hub(Env(), {})
    assert calls == [['cline', 'hub', 'status']]


@pytest.mark.parametrize('failure', ['key', 'policy'])
def test_failed_cleanup_still_gracefully_stops_owned_native_hub_F1403(stub_terminal, monkeypatch, failure):  # noqa: F811
    env = _PolicyEnv()
    env.read_file = lambda path: env.files.get(path, '').encode()
    stub_terminal['env'] = env
    original_exec = env.exec
    commands = []
    def execute(argv, **kwargs):
        if argv[:2] == ['cline', 'hub']:
            commands.append((argv, kwargs))
            return 0, json.dumps({'running': True} if argv[-1] == 'status' else {'stopped': True})
        return original_exec(argv, **kwargs)
    env.exec = execute
    name = 'remove_key_settings' if failure == 'key' else 'apply_commands_policy'
    original = getattr(cb, name)
    calls = 0
    def fail_cleanup(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == (2 if failure == 'key' else 3):
            raise PermissionError('synthetic cleanup refusal')
        return original(*args, **kwargs)
    monkeypatch.setattr(cb, name, fail_cleanup)
    with pytest.raises(PermissionError, match='synthetic cleanup refusal'):
        _run_terminal(auth='api_key', provider='openrouter', model='deepseek/deepseek-v4-flash', commands='deny')
    assert [argv for argv, _ in commands] == [['cline', 'hub', 'status'], ['cline', 'hub', 'stop']]
    assert all(kwargs['timeout'] == 10 for _, kwargs in commands)
    assert all('CLINE_API_KEY' not in kwargs['env'] for _, kwargs in commands)
