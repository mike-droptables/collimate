# SPDX-License-Identifier: MIT
"""Unpaid pinned native TTY discovery; no prompt, tool execution or publication."""
import asyncio
from contextlib import asynccontextmanager

import gen
import pytest

from _agent_shared import cli_container
from _agent_shared.harnesses.codex import backend as codex_backend
from _agent_shared.harnesses.qwen_code import backend as qwen_code_backend
from tests_live.conftest import requires_docker, run_on_test_loop
from tests_live._host_terminal import _host_terminals, _type_line
from tests_live._host_terminal import _up_to_prompt

pytestmark = [pytest.mark.live, requires_docker, pytest.mark.usefixtures('one_loop_per_test')]


@pytest.mark.parametrize('harness', ['codex', 'qwen-code'])
@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
def test_pinned_native_terminal_discovers_own_leg_tools_D23(docker_env_bridge, monkeypatch, harness, commands):
    backend = codex_backend if harness == 'codex' else qwen_code_backend
    docker_env_bridge.permissions = {'docker': 'full', 'internet': 'full', 'commands': commands}
    docker_env_bridge.packages = ({'codex': '0.164.0', 'codex-acp': '2.2.2'}
                                 if harness == 'codex' else {'qwen-code': '0.26.1'})
    async def synthetic_key(provider):
        assert provider == 'openrouter'
        return 'unpaid-native-discovery-synthetic-key'
    monkeypatch.setattr(backend, 'account_api_key', synthetic_key)
    terms = _host_terminals(docker_env_bridge, monkeypatch)
    original_prepare = backend.prepare
    original_bridge = cli_container.leg_bridge
    prepared_values, opened, closed = [], [], []
    async def prepare(ctx, **kwargs):
        value = await original_prepare(ctx, **kwargs)
        prepared_values.append(value)
        return value
    @asynccontextmanager
    async def bridge(**kwargs):
        async with original_bridge(**kwargs) as value:
            assert value is not None, 'Own bridge failed to start'
            opened.append(value)
            try:
                yield value
            finally:
                closed.append(value)
    monkeypatch.setattr(backend, 'prepare', prepare)
    monkeypatch.setattr(cli_container, 'leg_bridge', bridge)
    async def run():
        ready = asyncio.Event()
        from _agent_shared import surface_runner
        runner = surface_runner('codex' if harness == 'codex' else 'qwen-code', 'terminal')
        leg = asyncio.create_task(runner({'backend': 'codex' if harness == 'codex' else 'qwen-code', 'provider': 'openrouter', 'auth': 'api_key',
            'model': 'deepseek/deepseek-v4-flash', 'commands': commands, 'websearch': False},
            pointer=gen.run_path('terminal/intake.terminal'),
            on_ready=lambda *args, **kwargs: ready.set()))
        waiter = asyncio.create_task(ready.wait())
        try:
            await asyncio.wait({leg, waiter}, timeout=180, return_when=asyncio.FIRST_COMPLETED)
            if leg.done():
                leg.result()
            assert ready.is_set() and len(opened) == 1
            prepared = prepared_values[0]
            executable = 'codex' if harness == 'codex' else 'qwen'
            rc, version = await asyncio.to_thread(prepared.env_obj.exec, [executable, '--version'])
            assert rc == 0 and ('0.164.0' if harness == 'codex' else '0.23.3') in version
            term = next(iter(terms.values()))
            if harness == 'codex':
                await asyncio.to_thread(term.wait_for, b'Ask Codex', b'context left', b'To get started', timeout=60)
            else:
                _, blocked = await asyncio.to_thread(_up_to_prompt, harness, term)
                assert not blocked
            # This is a native UI inventory command, not a model request.
            await asyncio.to_thread(_type_line, term, '/mcp verbose' if harness == 'codex' else '/mcp')
            await asyncio.to_thread(term.wait_for, b'collimate', timeout=60)
            if harness == 'qwen-code':
                # Pinned native dialog: select the only server, then View tools.
                term.send(b'\r')
                await asyncio.to_thread(term.wait_for, b'View tools', timeout=60)
                term.send(b'\r')
            await asyncio.to_thread(term.wait_for, b'list_handlers', timeout=60)
            if commands != 'deny':
                await asyncio.to_thread(term.wait_for, b'test_generator', timeout=60)
            else:
                # Allow native discovery to settle before checking the roster.
                await asyncio.sleep(2)
                assert 'test_generator' not in term.said()
        finally:
            waiter.cancel()
            # Ending the terminal's client ends the leg.
            for item in terms.values():
                await asyncio.to_thread(item.stop)
            await asyncio.wait_for(leg, timeout=180)
        assert closed == opened
    run_on_test_loop(run())
