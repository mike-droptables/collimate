"""The execution-backend seam (process_manager/backends.py).

select_backend is the one place the docker-axis branch exists; each
backend's IPC endpoint carries the bind/auth shape manager.start() used
to build inline. Byte-identical behavior is otherwise pinned by the
generator/docker suites — these tests pin the seam itself.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def _perms(docker: str):
    from runner_permissions import RunnerPermissions
    return RunnerPermissions.from_definition({"docker": docker})


def test_docker_limited_selects_local_docker():
    from process_manager.backends import LocalDockerBackend, select_backend
    assert isinstance(select_backend(_perms("limited")), LocalDockerBackend)


@pytest.mark.parametrize("docker", ["none", "full"])
def test_other_grants_select_native(docker):
    from process_manager.backends import NativeSubprocessBackend, select_backend
    assert isinstance(select_backend(_perms(docker)), NativeSubprocessBackend)


def test_native_ipc_endpoint_is_loopback_behind_a_per_run_token__F0128():
    """Loopback is shared with every local process and user, and the port
    serves the run's account secrets: a native run's endpoint takes the same
    per-run token as a container's, never the bare connection handler."""
    from process_manager.backends import NativeSubprocessBackend

    class FakeManager:
        def _handle_ipc_connection(self):  # pragma: no cover - identity only
            pass

    manager = FakeManager()
    endpoint = NativeSubprocessBackend().ipc_endpoint(manager)
    assert endpoint.bind_host == "127.0.0.1"
    assert set(endpoint.env) == {"COLLIMATE_IPC_TOKEN"}
    token = endpoint.env["COLLIMATE_IPC_TOKEN"]
    assert isinstance(token, str) and len(token) >= 32
    assert endpoint.handler != manager._handle_ipc_connection
    # Fresh token per run.
    assert NativeSubprocessBackend().ipc_endpoint(manager).env["COLLIMATE_IPC_TOKEN"] != token


def test_docker_ipc_endpoint_is_widened_with_token():
    from process_manager.backends import LocalDockerBackend
    from process_manager.docker_runner import HOST_GATEWAY_ALIAS

    class FakeManager:
        pass

    endpoint = LocalDockerBackend().ipc_endpoint(FakeManager())
    assert endpoint.bind_host == "0.0.0.0"
    assert endpoint.env["COLLIMATE_IPC_HOST"] == HOST_GATEWAY_ALIAS
    token = endpoint.env["COLLIMATE_IPC_TOKEN"]
    assert isinstance(token, str) and len(token) >= 32
    # Fresh token per run.
    other = LocalDockerBackend().ipc_endpoint(FakeManager())
    assert other.env["COLLIMATE_IPC_TOKEN"] != token


class _FakeWriter:
    def __init__(self):
        self.closed = False
    def write(self, _d): pass
    async def drain(self): pass
    def close(self): self.closed = True
    async def wait_closed(self): pass


def _reader_with(*lines: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    for ln in lines:
        r.feed_data(ln)
    r.feed_eof()
    return r


class _RecordingManager:
    run_id = "r"

    def __init__(self):
        self.called = False
        self.rest = b""

    async def _handle_ipc_connection(self, reader, writer):
        self.called = True
        self.rest = await reader.read()


async def test_authed_handler_accepts_valid_token_then_delegates():
    from process_manager.backends import make_authed_ipc_handler

    mgr = _RecordingManager()
    handler = make_authed_ipc_handler(mgr, "tok-123")
    reader = _reader_with(
        json.dumps({"op": "__auth__", "token": "tok-123"}).encode() + b"\n",
        b'{"op":"finish"}\n',
    )
    await handler(reader, _FakeWriter())
    assert mgr.called is True
    # The auth frame is consumed up to its newline and no further: the next
    # frame reaches the IPC loop whole.
    assert mgr.rest == b'{"op":"finish"}\n'


async def test_authed_handler_refuses_an_oversized_first_frame_unbuffered__F0128():
    """An unauthenticated peer cannot make the host buffer a protocol-sized
    line: the first frame is read at most ~1 KiB before it is refused."""
    from process_manager.backends import make_authed_ipc_handler

    mgr = _RecordingManager()
    handler = make_authed_ipc_handler(mgr, "tok-123")
    reader = _reader_with(b"x" * 4_000_000 + b"\n")
    writer = _FakeWriter()
    await handler(reader, writer)
    assert mgr.called is False
    assert writer.closed is True
    assert len(await reader.read()) > 4_000_000 - 2048


async def test_authed_handler_rejects_bad_token_and_closes():
    from process_manager.backends import make_authed_ipc_handler

    mgr = _RecordingManager()
    handler = make_authed_ipc_handler(mgr, "tok-123")
    reader = _reader_with(json.dumps({"op": "__auth__", "token": "WRONG"}).encode() + b"\n")
    writer = _FakeWriter()
    await handler(reader, writer)
    assert mgr.called is False
    assert writer.closed is True


async def test_authed_handler_rejects_non_auth_first_frame():
    from process_manager.backends import make_authed_ipc_handler

    mgr = _RecordingManager()
    handler = make_authed_ipc_handler(mgr, "tok-123")
    writer = _FakeWriter()
    await handler(_reader_with(b'{"op":"update_cell"}\n'), writer)
    assert mgr.called is False
    assert writer.closed is True


async def test_native_ipc_port_answers_only_the_runs_own_connection__F0128_F0100():
    """Over a real loopback socket: a local process without the run's token
    reads nothing from the port (the account op serves secrets there) and
    never takes the run's writer slot, so the run's own connection keeps
    receiving its acks."""
    from process_manager.backends import NativeSubprocessBackend
    from process_manager.ipc import IPCMixin

    class _Run(IPCMixin):
        pass

    run = _Run()
    run.run_id = "run-f0128"
    run._ipc_writer = None
    run.log_buffer = []
    handled: list = []

    async def _handle_ipc(line):
        request = json.loads(line)
        handled.append(request)
        await run._send_ack({"op": "ack_" + request["op"]}, request)

    run._handle_ipc = _handle_ipc
    endpoint = NativeSubprocessBackend().ipc_endpoint(run)
    server = await asyncio.start_server(endpoint.handler, endpoint.bind_host, 0)
    port = server.sockets[0].getsockname()[1]
    own_w = stray_w = None
    try:
        own_r, own_w = await asyncio.open_connection("127.0.0.1", port)
        own_w.write(json.dumps({"op": "__auth__", "token": endpoint.env["COLLIMATE_IPC_TOKEN"]}).encode() + b"\n")
        own_w.write(b'{"op":"first","req_id":"first-request"}\n')
        await own_w.drain()
        assert json.loads(await asyncio.wait_for(own_r.readline(), 5)) == {"op": "ack_first", "req_id": "first-request"}
        own_writer_slot = run._ipc_writer
        assert own_writer_slot is not None

        stray_r, stray_w = await asyncio.open_connection("127.0.0.1", port)
        stray_w.write(b'{"op":"account","role":"model","req_id":"stray-request"}\n')
        await stray_w.drain()
        assert await asyncio.wait_for(stray_r.read(), 5) == b""
        stray_w.close()

        assert run._ipc_writer is own_writer_slot
        own_w.write(b'{"op":"second","req_id":"second-request"}\n')
        await own_w.drain()
        assert json.loads(await asyncio.wait_for(own_r.readline(), 5)) == {"op": "ack_second", "req_id": "second-request"}
        assert [f["op"] for f in handled] == ["first", "second"]
    finally:
        # Close both client ends first and bound the teardown: wait_closed
        # waits for every open connection, so a failed assertion above must
        # not leave the test hanging on the run's own connection.
        for w in (own_w, stray_w):
            if w is not None:
                w.close()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)


def test_port_contract_is_loopback_for_local_backends():
    from process_manager.backends import LocalDockerBackend, NativeSubprocessBackend
    assert NativeSubprocessBackend().port_target(8080) == ("127.0.0.1", 8080)
    assert LocalDockerBackend().port_target(8080) == ("127.0.0.1", 8080)


def test_native_run_serves_only_ports_its_own_group_listens_on():
    """A native run shares the host's loopback: a port it serves is one a
    process of its group listens on, never another listener on the host.
    A docker:limited container publishes nothing, so it serves no port."""
    import socket
    import subprocess
    import sys
    from types import SimpleNamespace

    from process_manager.backends import LocalDockerBackend, NativeSubprocessBackend

    listener = (
        "import socket, sys, time\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "print(s.getsockname()[1], flush=True); time.sleep(30)\n"
    )
    generator = subprocess.Popen([sys.executable, "-c", listener], stdout=subprocess.PIPE,
                                 text=True, start_new_session=True)
    elsewhere = socket.socket()
    try:
        served = int(generator.stdout.readline())
        elsewhere.bind(("127.0.0.1", 0))
        elsewhere.listen()
        run = SimpleNamespace(process=SimpleNamespace(pid=generator.pid, returncode=None))

        assert NativeSubprocessBackend().serves_port(run, served)
        assert not NativeSubprocessBackend().serves_port(run, elsewhere.getsockname()[1])
        assert not LocalDockerBackend().serves_port(run, served)
    finally:
        elsewhere.close()
        generator.kill()
        generator.wait()


def test_runs_on_local_machine_refuses_containerized_backend():
    from process_manager.backends import (
        LocalDockerBackend,
        NativeSubprocessBackend,
        RunsOnViolation,
        enforce_runs_on,
    )

    manifest = {"runs_on": "local-machine"}
    # Native placement honors the assumption.
    enforce_runs_on(manifest, NativeSubprocessBackend())
    # Containerized placement is a configuration contradiction.
    with pytest.raises(RunsOnViolation, match="local-machine"):
        enforce_runs_on(manifest, LocalDockerBackend())


@pytest.mark.parametrize("manifest", [None, {}, {"runs_on": "anywhere"}])
def test_runs_on_default_runs_under_any_backend(manifest):
    from process_manager.backends import (
        LocalDockerBackend,
        NativeSubprocessBackend,
        enforce_runs_on,
    )

    enforce_runs_on(manifest, NativeSubprocessBackend())
    enforce_runs_on(manifest, LocalDockerBackend())
