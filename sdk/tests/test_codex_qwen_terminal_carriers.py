"""D23 real terminal runner boundaries: native policy and bridge lifetime."""
import asyncio
from contextlib import asynccontextmanager
import copy
from types import SimpleNamespace
import tomllib

import pytest
import gen

import _agent_shared
from _agent_shared import (approval, cli_container, generator_test, harness_homes)
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses import CommandsPolicy, PreparedHarness  # noqa: E402
from _agent_shared.harnesses.codex import backend as codex_backend
from _agent_shared.harnesses.qwen_code import backend as qwen_code_backend


async def _no_credentials(adapter, *, provider, plan):
    """The pipeline's credential step for a test whose renderer is a fake."""
    return _pipeline.Credentials(provider, plan, key="" if plan else "sk-test")


class StartFailed(Exception):
    pass


@pytest.mark.parametrize('harness', ['codex', 'qwen'])
@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
@pytest.mark.parametrize('ending', ['normal', 'failed_start', 'cancel', 'unavailable'])
def test_prepared_native_terminal_bridge_grant_and_lifetime_D23(monkeypatch, tmp_path, harness, commands, ending):
    backend = codex_backend if harness == 'codex' else qwen_code_backend
    timeline, rows_seen, writes = [], [], []
    url = 'http://gateway/private-leg/mcp'
    home = tmp_path / 'home'
    home.mkdir()
    config = ('model = "synthetic/model"\nallow_login_shell = false\n'
              '[model_providers.synthetic]\nbase_url = "https://provider.invalid"\n'
              'experimental_bearer_token = "private-config-sentinel"\n'
              '[features]\nshell_snapshot = false\n')
    (home / 'config.toml').write_text(config)
    native = (copy.deepcopy(qwen_code_backend.system_settings(commands, websearch=False))
              if harness == 'qwen' else {'CODEX_CONFIG': 'unchanged-native-policy'})
    class Env:
        container_id = 'prepared-container-id'
    env = Env()
    argv = ('codex', *codex_backend.codex_tui_args(commands)) if harness == 'codex' else (
        'qwen', *qwen_code_backend.mode_flags(commands), '--approval-mode', qwen_code_backend.approval_mode(commands))
    prepared = PreparedHarness(CommandsPolicy(commands, native), None, env, argv,
        {'PRIVATE_NATIVE_SETTINGS': 'unchanged'}, 'synthetic/model')
    async def prepare(ctx, *, leg):
        assert leg == 'terminal' and ctx['commands'] == commands
        return prepared
    @asynccontextmanager
    async def bridge(*, extra_entries):
        rows_seen.append(extra_entries or [])
        timeline.append('open')
        try:
            yield None if ending == 'unavailable' else SimpleNamespace(url_container=url)
        finally:
            timeline.append('closed')
    def write_system(environment, settings):
        assert environment is env
        writes.append(copy.deepcopy(settings))
        return qwen_code_backend.SYSTEM_SETTINGS_PATH
    @asynccontextmanager
    async def terminal(container_id, command, **kwargs):
        assert timeline == ['open']
        assert container_id == env.container_id
        assert kwargs['env']['PRIVATE_NATIVE_SETTINGS'] == 'unchanged'
        if harness == 'codex':
            body = (home / 'config.toml').read_text()
            assert body.startswith(config)
            if ending == 'unavailable':
                assert body == config
            else:
                assert tomllib.loads(body)['mcp_servers']['collimate'] == {'url': url}
            assert command == ['codex', '-c', 'check_for_update_on_startup=false', *argv[1:], '-m', prepared.model]
        else:
            assert command == list(argv)
            assert writes == ([native] if ending == 'unavailable' else
                              [{**native, 'mcpServers': {'collimate': {'httpUrl': url}}}])
            assert native == qwen_code_backend.system_settings(commands, websearch=False)
        timeline.append('terminal')
        if ending == 'failed_start':
            raise StartFailed
        yield {'terminal_id': 'actual-terminal-id'}
    @asynccontextmanager
    async def live(**kwargs):
        yield
    async def wait(**kwargs):
        assert kwargs == {'terminal_id': 'actual-terminal-id', 'on_action': None}
        if ending == 'cancel':
            raise asyncio.CancelledError
        timeline.append(ending)
    async def execute(context, **kwargs):
        assert context.locus == 'container' and context.env_obj is env
        timeline.append('executed')
        return {'commands': commands}
    async def approve(*args, **kwargs):
        return None
    async def update(**kwargs):
        pass
    monkeypatch.setattr(backend, 'prepare', prepare)
    monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
    monkeypatch.setattr(cli_container, 'leg_bridge', bridge)
    monkeypatch.setattr(qwen_code_backend, 'write_system_settings', write_system)
    monkeypatch.setattr(harness_homes, 'harness_home', lambda _: str(home))
    monkeypatch.setattr(codex_backend, '_finalize_codex_terminal', lambda *args: timeline.append('finalized'))
    monkeypatch.setattr(cli_container, 'remove_context_file', lambda *args: None)
    monkeypatch.setattr(gen, 'commit', update)
    monkeypatch.setattr(gen, 'serve_terminal', terminal)
    monkeypatch.setattr(gen, 'live_files', live)
    for name in ('phase',):
        monkeypatch.setattr(gen, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(_agent_shared, 'serve_tty_until', wait)
    monkeypatch.setattr(generator_test, '_execute', execute)
    monkeypatch.setattr(approval, 'approve_command', approve)
    async def run():
        task = _agent_shared.surface_runner('codex' if harness == 'codex' else 'qwen-code', 'terminal')
        operation = task({'backend': 'codex' if harness == 'codex' else 'qwen-code', 'commands': commands, 'websearch': False}, pointer='fixture.terminal')
        if ending in ('failed_start', 'cancel'):
            with pytest.raises(StartFailed if ending == 'failed_start' else asyncio.CancelledError):
                await operation
        else:
            await operation
        assert timeline[-1] == 'closed'
        if harness == 'codex':
            assert timeline[-2] == 'finalized'
        assert len(rows_seen) == 1
        rows = [row for row in rows_seen[0] if row[0] == 'test_generator']
        assert bool(rows) == (commands != 'deny')
        if rows:
            assert await rows[0][-1]('draft/probe.generator', {}, '') == {'commands': commands}
            if commands == 'ask':
                executed = timeline.count('executed')
                async def refuse(*args):
                    return 'Rejected by fixture user'
                monkeypatch.setattr(approval, 'approve_command', refuse)
                result = await rows[0][-1]('draft/probe.generator', {}, '')
                assert result == {'error': 'approval_refused', 'message': 'Rejected by fixture user'}
                assert timeline.count('executed') == executed
    asyncio.run(run())


def test_codex_terminal_bridge_preserves_exact_private_native_bytes_D23(tmp_path):
    body = b'model = "keep"\n# provider secret stays private\n[mcp_servers.other]\nurl = "https://other.invalid/mcp"\n'
    path = tmp_path / 'config.toml'
    codex_backend.write_native_file(str(tmp_path), 'config.toml', body)
    codex_backend.write_terminal_bridge(str(tmp_path), 'http://gateway/private/mcp')
    assert path.read_bytes().startswith(body)
    assert path.stat().st_mode & 0o777 == 0o600
    assert tomllib.loads(path.read_text())['mcp_servers'] == {
        'other': {'url': 'https://other.invalid/mcp'},
        'collimate': {'url': 'http://gateway/private/mcp'}}
    before = path.read_bytes()
    with pytest.raises(ValueError, match='already contains'):
        codex_backend.write_terminal_bridge(str(tmp_path), 'http://replacement/mcp')
    assert path.read_bytes() == before


@pytest.mark.parametrize('body', [b'mcp_servers = []', b'[mcp_servers.collimate]\nurl = "old"', b'invalid TOML'])
def test_codex_terminal_bridge_rejects_ambiguous_config_without_write_D23(tmp_path, body):
    path = tmp_path / 'config.toml'
    path.write_bytes(body)
    with pytest.raises(ValueError):
        codex_backend.write_terminal_bridge(str(tmp_path), 'http://fresh/mcp')
    assert path.read_bytes() == body


def test_codex_terminal_bridge_does_not_follow_planted_leaf_D23(tmp_path):
    outside = tmp_path / 'outside.toml'
    outside.write_text('model = "untouched"\n')
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        codex_backend.write_terminal_bridge(str(home), 'http://fresh/mcp')
    assert outside.read_text() == 'model = "untouched"\n'


def test_codex_unavailable_bridge_leaves_fresh_config_unchanged_D23(tmp_path):
    body = b'model = "keep"\n'
    (tmp_path / 'config.toml').write_bytes(body)
    codex_backend.write_terminal_bridge(str(tmp_path), None)
    assert (tmp_path / 'config.toml').read_bytes() == body
