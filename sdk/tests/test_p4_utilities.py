"""P4 utilities use real shell writes and explicit teardown/translation seams."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from _agent_shared import cli_container as cc, mcp_bridge as mb
from _agent_shared.harnesses.antigravity import backend as ab


def test_bridge_stop_abandons_inflight_tools_before_join_F1583():
    server = SimpleNamespace(should_exit=False, force_exit=False)
    joins = []
    def join(timeout):
        joins.append((server.should_exit, server.force_exit, timeout))
    bridge = mb.McpBridge(1, 'container', 'host', [], server, SimpleNamespace(join=join))
    bridge.stop()
    bridge.stop()
    assert joins == [(True, True, 5)]


@pytest.mark.parametrize('fail', [False, True])
def test_gateway_discovery_closes_its_docker_client_F1583(monkeypatch, fail):
    closed = []
    def get(name):
        if fail:
            raise RuntimeError('daemon unavailable')
        return SimpleNamespace(attrs={'IPAM': {'Config': [{'Gateway': '172.18.0.1'}]}})
    client = SimpleNamespace(networks=SimpleNamespace(get=get), close=lambda: closed.append(True))
    monkeypatch.setitem(sys.modules, 'docker', SimpleNamespace(from_env=lambda: client))
    assert mb._docker_gateway_host() == ('172.17.0.1' if fail else '172.18.0.1')
    assert closed == [True]


class ShellContainer:
    """The production shell commands execute locally through the env plane."""
    def __init__(self, fail_append=False):
        self.calls = []
        self.fail_append = fail_append

    def exec(self, argv, user=None):
        self.calls.append((argv, user))
        if self.fail_append and '>>' in argv[-1]:
            return 1, 'append failed'
        result = subprocess.run(argv, capture_output=True)
        return result.returncode, (result.stdout + result.stderr).decode()


@pytest.mark.parametrize('kind', ['context', 'policy', 'root'])
def test_large_container_writes_use_bounded_commands_and_complete_bytes_F1505(tmp_path, kind):
    path = tmp_path / "nested space" / "file '$.txt"
    body = ('abcλ\n' * 250000)
    container = ShellContainer()
    env = container
    if kind == 'context':
        assert cc.write_container_file(env, str(path), body, mkdir=str(path.parent))
    elif kind == 'policy':
        cc.write_policy_file(env, str(path), body, mkdir=str(path.parent), what='large policy')
    else:
        cc.write_root_file(env, str(path), body, what='large root policy')
        assert path.stat().st_mode & 0o777 == 0o644
    assert path.read_bytes() == body.encode()
    assert all(len(arg.encode()) < 65536 for argv, _ in container.calls for arg in argv)
    assert len(container.calls) > 3
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


@pytest.mark.parametrize('kind', ['context', 'policy', 'root'])
def test_failed_large_append_preserves_destination_and_cleans_temps_F1505(tmp_path, kind):
    path = tmp_path / 'existing'
    path.write_text('preserved')
    container = ShellContainer(fail_append=True)
    env = container
    body = 'x' * 200000
    if kind == 'context':
        assert cc.write_container_file(env, str(path), body) is False
    else:
        writer = cc.write_policy_file if kind == 'policy' else cc.write_root_file
        with pytest.raises(RuntimeError, match='large write'):
            writer(env, str(path), body, what='large write')
    assert path.read_text() == 'preserved'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['existing']


def test_context_seam_failure_reports_real_exit_and_clears_stale_F1532(tmp_path):
    path = tmp_path / 'seam'
    path.write_text('old instruction')
    def execute(argv, **kwargs):
        if 'base64 -d' in argv[-1]:
            return 7, 'write refused'
        result = subprocess.run(argv, capture_output=True)
        return result.returncode, result.stdout.decode()
    env = SimpleNamespace(exec=execute)
    assert cc.write_context_seam(env, str(path), 'new instruction') is False
    assert not path.exists()


def test_terminal_mcp_config_is_only_its_bridge_F1474(monkeypatch):
    monkeypatch.setattr(mb, 'SERVER_NAME', 'selected-tools')
    url = 'http://host.docker.internal:9021/private/mcp'
    assert json.loads(ab.mcp_config_json(url)) == {'mcpServers': {
        'selected-tools': {'disabled': False, 'serverUrl': url}}}
    assert json.loads(ab.mcp_config_json(None)) == {'mcpServers': {}}


def test_large_root_write_refuses_directory_destination_F1505(tmp_path):
    path = tmp_path / 'directory'
    path.mkdir()
    marker = path / 'untouched'
    marker.write_text('preserved')
    env = ShellContainer()
    with pytest.raises(RuntimeError, match='root destination'):
        cc.write_root_file(env, str(path), 'x' * 200000, what='root destination')
    assert marker.read_text() == 'preserved'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['directory']
    assert sorted(p.name for p in path.iterdir()) == ['untouched']


def test_real_uvicorn_stop_abandons_a_tool_waiting_on_generator_loop_F1583():
    import asyncio
    import socket
    import threading
    import time
    import uvicorn

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = threading.Event()
        async def pending_tool():
            await asyncio.Event().wait()
        async def app(scope, receive, send):
            entered.set()
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(pending_tool(), loop))
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_level='critical'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        bridge = mb.McpBridge(sock.getsockname()[1], '', '', [], server, thread)
        thread.start()
        client = None
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(.01)
            assert server.started
            client = socket.create_connection(('127.0.0.1', bridge.port), timeout=2)
            client.sendall(b'GET /pending HTTP/1.1\r\nHost: localhost\r\n\r\n')
            assert await asyncio.to_thread(entered.wait, 2)
            started = time.monotonic()
            await asyncio.to_thread(bridge.stop)
            assert not thread.is_alive(), 'in-flight bounced tool held bridge shutdown open'
            assert time.monotonic() - started < 3
        finally:
            server.should_exit = server.force_exit = True
            if client is not None:
                client.close()
            await asyncio.to_thread(thread.join, 2)
            sock.close()
    asyncio.run(exercise())
