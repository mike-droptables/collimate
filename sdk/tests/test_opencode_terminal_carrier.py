"""D23 OpenCode terminal: effective grant, native config and per-leg lifetime."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest

from _agent_shared.harnesses.opencode import backend as oc
from _agent_shared.harnesses import CommandsPolicy, PreparedHarness  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

from _agent_shared import cli_container, generator_test, approval
import _agent_shared


async def _no_credentials(adapter, *, provider, plan):
    """The pipeline's credential step for a test whose renderer is a fake."""
    return _pipeline.Credentials(provider, plan, key="" if plan else "sk-test")


class StartFailed(Exception):
    pass


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
@pytest.mark.parametrize('ending', ['normal', 'failed_start', 'cancel'])
def test_terminal_native_config_and_command_lifetime_D23(monkeypatch, commands, ending):
    original = {'permission': {'bash': commands}, 'snapshot': False,
                'instructions': ['private-context.md'], 'model': 'synthetic/model',
                'command': {'review': {'template': 'keep'}},
                'mcp': {'other': {'type': 'remote', 'url': 'http://other/mcp'}}}
    timeline, bridges = [], []
    class Env:
        container_id = 'actual-prepared-container'
        config = json.dumps(original)
        def read_file(self, path, **kwargs):
            assert path == oc.OPENCODE_CONFIG_PATH and kwargs == {'user': 'root'}
            return self.config.encode()
    env = Env()
    prepared = PreparedHarness(CommandsPolicy(commands, {}), None, env, ('opencode',), {},
                               'synthetic/model')
    def write(environment, path, text, **kwargs):
        assert environment is env and path == oc.OPENCODE_CONFIG_PATH
        env.config = text
        timeline.append('config')
    @asynccontextmanager
    async def bridge(**kwargs):
        timeline.append('open')
        rows = kwargs['extra_entries'] or []
        bridges.append(rows)
        try:
            yield SimpleNamespace(url_container='http://gateway/leg-secret/mcp')
        finally:
            timeline.append('closed')
    @asynccontextmanager
    async def terminal(*args, **kwargs):
        config = json.loads(env.config)
        assert config['mcp']['collimate'] == {'type': 'remote', 'url': 'http://gateway/leg-secret/mcp', 'enabled': True}
        assert {k: v for k, v in config.items() if k != 'mcp'} == {k: v for k, v in original.items() if k != 'mcp'}
        assert config['mcp']['other'] == original['mcp']['other']
        timeline.append('terminal')
        if ending == 'failed_start':
            raise StartFailed
        yield {'terminal_id': 'terminal-id'}
    @asynccontextmanager
    async def live(**kwargs):
        yield
    async def wait(**kwargs):
        assert kwargs == {'terminal_id': 'terminal-id', 'on_action': None}
        if ending == 'cancel':
            raise asyncio.CancelledError
        timeline.append(ending)
    async def prepare(ctx, *, leg):
        assert leg == 'terminal'
        return prepared
    async def update(**kwargs):
        return None
    monkeypatch.setattr(oc, 'prepare', prepare)
    monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
    monkeypatch.setattr(oc.gen, 'commit', update)
    async def execute(context, **kwargs):
        assert context.locus == 'container' and context.env_obj is env
        return {'effective': commands}
    async def approve(*args, **kwargs):
        return None
    monkeypatch.setattr(cli_container, 'write_root_file', write)
    monkeypatch.setattr(cli_container, 'leg_bridge', bridge)
    monkeypatch.setattr(oc, '_listed_before', lambda _: frozenset())
    monkeypatch.setattr(oc, '_finalize', lambda *args, **kwargs: timeline.append('finalized'))
    monkeypatch.setattr(oc.gen, 'serve_terminal', terminal)
    monkeypatch.setattr(oc.gen, 'live_files', live)
    for name in ('phase',):
        monkeypatch.setattr(oc.gen, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(_agent_shared, 'serve_tty_until', wait)
    monkeypatch.setattr(generator_test, '_execute', execute)
    monkeypatch.setattr(approval, 'approve_command', approve)
    async def run():
        task = _agent_shared.surface_runner('opencode', 'terminal')(
            {'backend': 'opencode', 'commands': 'allow'}, label='label',
            pointer='fixture.terminal')
        if ending in ('failed_start', 'cancel'):
            with pytest.raises(StartFailed if ending == 'failed_start' else asyncio.CancelledError):
                await task
        else:
            await task
        assert timeline[-2:] == ['finalized', 'closed']
        assert len(bridges) == 1
        command_rows = [row for row in bridges[0] if row[0] == 'test_generator']
        assert bool(command_rows) == (commands != 'deny')
        if command_rows:
            assert await command_rows[0][-1]('draft/probe.generator', {}, '') == {'effective': commands}
    asyncio.run(run())


def test_unavailable_bridge_removes_expired_own_url_preserves_native_policy_D23(monkeypatch):
    original = {'permission': {'bash': 'deny'}, 'model': 'keep', 'mcp': {
        'collimate': {'type': 'remote', 'url': 'expired'},
        'other': {'type': 'remote', 'url': 'keep'}}}
    class Env:
        def read_file(self, *args, **kwargs):
            return json.dumps(original).encode()
    writes = []
    monkeypatch.setattr(cli_container, 'write_root_file', lambda env, path, text, **kwargs: writes.append(json.loads(text)))
    oc._write_terminal_bridge(Env(), None)
    assert writes == [{**original, 'mcp': {'other': original['mcp']['other']}}]


@pytest.mark.parametrize('body', ['[]', '{"mcp": []}', '{broken'])
def test_malformed_prepared_config_is_never_replaced_D23(monkeypatch, body):
    class Env:
        def read_file(self, *args, **kwargs):
            return body.encode()
    writes = []
    monkeypatch.setattr(cli_container, 'write_root_file', lambda *args, **kwargs: writes.append(args))
    with pytest.raises(ValueError):
        oc._write_terminal_bridge(Env(), 'new-url')
    assert writes == []
