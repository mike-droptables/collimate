# SPDX-License-Identifier: MIT
"""Real socket/HTTP layer teardown; no daemon, native process or provider."""
import asyncio
import gc
import http.client
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest
import requests
import urllib3
from docker.api.client import APIClient

from _agent_shared import acp_client as ac, acp_run as ar  # noqa: E402
from tests_live.fixtures.acp_capture import Capture, observed_profile  # noqa: E402


def attached_http_socket():
    left, right = socket.socketpair()
    right.sendall(b'HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n')
    native = http.client.HTTPResponse(left)
    native.begin()
    response = requests.Response()
    response.status_code = 101
    response.raw = urllib3.response.HTTPResponse(
        body=native, original_response=native, preload_content=False)
    client = APIClient.__new__(APIClient)
    client.base_url = 'http+docker://localhost'
    return left, right, response, client._get_raw_response_socket(response)


@pytest.mark.parametrize('close_mode', ['sync', 'async_failed_connection'])
@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('kill_fails', [False, True])
def test_real_sync_cache_discard_closes_owned_response_even_when_wrapped_or_kill_fails_P4F34(
        monkeypatch, wrapped, kill_fails, close_mode):
    left, right, response, attached = attached_http_socket()
    monkeypatch.setattr(ar, '_ACP_SESSIONS', {})
    client_closes = []
    client = SimpleNamespace(close=lambda: client_closes.append(True))
    monkeypatch.setattr(ac, '_attached_exec', lambda *args, **kwargs: (client, 'local', attached))
    monkeypatch.setattr(ac, 'register_adapter_log', lambda backend: 'unused')
    monkeypatch.setattr(ac, '_adapter_log_writer', lambda path: lambda data: None)
    calls = []
    def execute(argv):
        calls.append(argv)
        right.shutdown(socket.SHUT_RDWR)
        if kill_fails:
            raise RuntimeError('synthetic owned child kill failure')
    environment = SimpleNamespace(container_id='no-daemon', exec=execute)
    errors = []
    monkeypatch.setattr(sys, 'unraisablehook', lambda error: errors.append(error.exc_value))

    async def scenario():
        async def spawn():
            return await ac.spawn_container_adapter('synthetic', environment, ['synthetic'], cwd='/workspace',
                                                     env_vars={})
        profile = ac.SpawnProfile(locus='container', cwd='/workspace', spawn=spawn, commands='deny')
        if wrapped:
            profile = observed_profile(profile, Capture())
        process = await profile.spawn()
        transport = process.transport.original if wrapped else process.transport
        session = ac.AcpSession.__new__(ac.AcpSession)
        async def failed_close():
            raise OSError('synthetic protocol close failure')
        session.loop = asyncio.get_running_loop()
        session._terminals, session.conn, session.adapter = {}, SimpleNamespace(close=failed_close), process
        ar._ACP_SESSIONS[('synthetic', '')] = session
        try:
            if close_mode == 'sync':
                ar.close_acp_sessions_sync()
            else:
                await ar.close_acp_sessions()
            assert session.adapter is None and not ar._ACP_SESSIONS
            assert transport._closed, 'Sync discard lost ownership of the attached transport'
            assert attached.closed and response.raw.closed
            await ar.close_acp_sessions()
            session.discard()
            await process.transport.close()
            assert len(calls) == 1
            assert client_closes == [True], 'Attached exec client lost its cleanup owner'
        finally:
            # The failing baseline must still release its real local descriptors.
            await transport.close()
            right.close()
            left.close()
            transport._thread.join(timeout=1)
            assert not transport._thread.is_alive()
    asyncio.run(scenario())
    gc.collect()
    assert not errors


def test_real_async_response_close_is_idempotent_and_retains_current_layer_order_P4F34(monkeypatch):
    left, right, response, attached = attached_http_socket()
    errors = []
    monkeypatch.setattr(sys, 'unraisablehook', lambda error: errors.append(error.exc_value))
    async def scenario():
        transport = ac.DockerExecTransport(attached, loop=asyncio.get_running_loop(), on_stderr=lambda data: None)
        try:
            await transport.close()
            await transport.close()
            assert transport._closed and attached.closed and response.raw.closed
        finally:
            right.close()
            left.close()
            transport._thread.join(timeout=1)
            assert not transport._thread.is_alive()
    asyncio.run(scenario())
    gc.collect()
    assert not errors


@pytest.mark.parametrize('phase', ['create','start'])
def test_real_attached_exec_closes_owned_client_on_admission_error_P4F34(monkeypatch, phase):
    import docker
    closed = []
    def create(*args, **kwargs):
        if phase == 'create':
            raise OSError('synthetic exec admission failure')
        return {'Id':'local'}
    def start(*args, **kwargs):
        raise OSError('synthetic attach admission failure')
    client=SimpleNamespace(api=SimpleNamespace(exec_create=create,exec_start=start),
                           close=lambda:closed.append(True))
    monkeypatch.setattr(docker,'from_env',lambda:client)
    with pytest.raises(OSError):
        ac._attached_exec('synthetic',['synthetic'],'/tmp/owned.pid',stdin=True,
                         environment={},workdir='/workspace')
    assert closed == [True]


@pytest.mark.parametrize('inspect_fails', [False,True])
def test_real_terminal_eof_closes_attached_client_and_response_P4F34(monkeypatch, inspect_fails):
    left,right,response,attached=attached_http_socket()
    closed=[]
    def inspect(exec_id):
        if inspect_fails:
            raise OSError('synthetic inspect failure')
        return {'ExitCode':0}
    client=SimpleNamespace(api=SimpleNamespace(exec_inspect=inspect),close=lambda:closed.append(True))
    monkeypatch.setattr(ac,'_attached_exec',lambda *args,**kwargs:(client,'local',attached))
    term=ac._CommandTerminal('local',byte_limit=None)
    session=SimpleNamespace(profile=SimpleNamespace(env_obj=SimpleNamespace(container_id='synthetic')),
                            agent_cwd='/workspace')
    async def scenario():
        try:
            await ac._spawn_container_terminal(session,term,['synthetic'],{},'/workspace')
            right.shutdown(socket.SHUT_WR)
            await asyncio.wait_for(term.exited.wait(),timeout=1)
            deadline=asyncio.get_running_loop().time()+1
            while not closed and asyncio.get_running_loop().time()<deadline:
                await asyncio.sleep(.01)
            assert attached.closed and response.raw.closed
            assert closed == [True]
        finally:
            ac._close_exec_socket(attached)
            left.close()
            right.close()
    asyncio.run(scenario())


def test_real_live_fixture_closes_async_sessions_before_cached_container_teardown_P4F34(
        monkeypatch, tmp_path):
    import ast
    import uuid
    from _agent_shared import cli_container
    source=Path(__file__).resolve().parents[1]/'tests_live/conftest.py'
    function=next(node for node in ast.parse(source.read_text()).body
                  if isinstance(node,ast.FunctionDef) and node.name=='docker_env_bridge')
    function.decorator_list=[]
    events=[]
    container=SimpleNamespace(id='local',remove=lambda **kwargs:events.append('container_removed'))
    client=SimpleNamespace(containers=SimpleNamespace(run=lambda *args,**kwargs:container))
    bridge=SimpleNamespace(stub_env_place=lambda value:setattr(bridge,'place',value),
                           stub_env_exec=lambda value:None,
                           env_close=lambda env_id: {"ok": True})
    async def close_sessions():
        await asyncio.sleep(0)
        events.append('async_sessions_closed')
    monkeypatch.setattr(ar,'close_acp_sessions',close_sessions)
    monkeypatch.setattr(cli_container,'close_all',lambda:events.append('sync_cache_closed'))
    namespace={'docker_client':lambda:client,'ensure_image':lambda *args:None,'uuid':uuid,
               'live_container_labels':lambda:{}}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
    with asyncio.Runner() as runner:
        lifetime=namespace['docker_env_bridge'](bridge,runner,monkeypatch)
        assert next(lifetime) is bridge
        bridge.place({'image':'synthetic','workspace':str(tmp_path)})
        with pytest.raises(StopIteration):
            next(lifetime)
    assert events==['async_sessions_closed','sync_cache_closed','container_removed']
