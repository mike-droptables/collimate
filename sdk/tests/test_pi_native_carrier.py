"""Pi private native-tool binding uses the prepared environment and policy."""
import asyncio
import json
from types import SimpleNamespace

import pytest
import gen

from tests.test_pi_prepare import prepared_runtime, ctx, native  # noqa: F401
from _agent_shared.harnesses.pi import backend as pb
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
def test_pi_acp_native_bridge_bound_before_spawn_D23(prepared_runtime, commands):  # noqa: F811
    async def run():
        result = await _pipeline.prepare(_HARNESS['pi'], ctx(commands=commands))
        assert result.profile.bind_bridge is not None
        await result.profile.bind_bridge(SimpleNamespace(url_container='http://gateway/private/mcp'))
        assert json.loads(native('collimate-mcp.json').read_text()) == {'url': 'http://gateway/private/mcp'}
        assert native('collimate-mcp.json').stat().st_mode & 0o777 == 0o600
        await result.profile.spawn()
        assert prepared_runtime.spawn[-1][2]['COLLIMATE_COMMANDS'] == commands
        assert prepared_runtime.spawn[-1][2]['PI_ACP_PI_COMMAND'] == pb.PI_WRAPPER
    asyncio.run(run())


def test_shared_acp_binding_runs_before_spawn_without_backend_branch_D23(monkeypatch, native_bridge):
    from _agent_shared import acp_run as ar, cli_container as cc, workspace_tools
    from _agent_shared.acp_client import SpawnProfile
    timeline = []
    bridge = SimpleNamespace(url_container='http://gateway/private/mcp')
    async def bind(value):
        assert value is bridge
        timeline.append('bind')
    async def spawn():
        timeline.append('spawn')
    async def prepare(context):
        return SpawnProfile(locus='container', cwd='/workspace', commands='deny',
                            spawn=spawn, bind_bridge=bind)
    class Session:
        def __init__(self, *, profile, **kwargs):
            self.profile = profile
            self.loaded = False
        async def start(self, **kwargs):
            await self.profile.spawn()
    monkeypatch.setitem(ar._PROFILES, 'synthetic-native-carrier', prepare)
    monkeypatch.setattr(ar, 'AcpSession', Session)
    monkeypatch.setattr(cc, 'gateway_bridge', lambda **kwargs: bridge)
    monkeypatch.setattr(workspace_tools, 'tool_entries', lambda **kwargs: [])
    asyncio.run(ar._open_session(backend='synthetic-native-carrier', auth='api_key',
        provider='openrouter', model='synthetic', system_prompt='', mcp_decls=[],
        enable_file_tools=False, websearch=False, turn_skills=[]))
    assert timeline == ['bind', 'spawn']


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
@pytest.mark.parametrize('ending', ['normal', 'failed_start', 'cancel'])
def test_terminal_bridge_uses_effective_context_and_closes_D23(monkeypatch, prepared_runtime, native_bridge, commands, ending):  # noqa: F811
    from contextlib import asynccontextmanager
    from _agent_shared import cli_container as cc
    import _agent_shared
    timeline = []
    prepared_runtime.container_id = 'prepared-pi-container'
    @asynccontextmanager
    async def bridge(**kwargs):
        rows = kwargs['extra_entries'] or []
        assert ('test_generator' in {row[0] for row in rows}) is (commands != 'deny')
        timeline.append('bridge-open')
        try:
            yield SimpleNamespace(url_container='http://gateway/private/mcp')
        finally:
            timeline.append('bridge-closed')
    @asynccontextmanager
    async def terminal(container_id, argv, **kwargs):
        assert container_id == prepared_runtime.container_id
        assert kwargs['env']['COLLIMATE_COMMANDS'] == commands
        assert json.loads(native('collimate-mcp.json').read_text())['url'] == 'http://gateway/private/mcp'
        timeline.append('terminal-start')
        if ending == 'failed_start':
            raise RuntimeError('synthetic failed start')
        yield {'terminal_id': 'pi-terminal'}
    @asynccontextmanager
    async def live(**kwargs):
        yield
    async def wait(*args, **kwargs):
        timeline.append('terminal-finish')
        if ending == 'cancel':
            raise asyncio.CancelledError
    monkeypatch.setattr(cc, 'leg_bridge', bridge)
    monkeypatch.setattr(gen, 'serve_terminal', terminal)
    monkeypatch.setattr(gen, 'live_files', live)
    monkeypatch.setattr(_agent_shared, 'serve_tty_until', wait)
    async def run():
        await _agent_shared.surface_runner('pi', 'terminal')({'backend': 'pi', **ctx(commands=commands)}, pointer='unused.terminal')
    if ending == 'failed_start':
        with pytest.raises(RuntimeError, match='synthetic failed start'):
            asyncio.run(run())
    elif ending == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert timeline[0] == 'bridge-open' and timeline[-1] == 'bridge-closed'
