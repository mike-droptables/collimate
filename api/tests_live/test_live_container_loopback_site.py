# SPDX-License-Identifier: MIT
"""LIVE (docker): a site a container serves on its own 127.0.0.1 is relayed.

A real container on this machine's dockerd runs an HTTP server bound to
127.0.0.1 inside it (the default of most dev servers), which the host cannot
reach at the container's bridge address. The run's ``EnvironmentService``
(around a stand-in manager: only its ``bound_ports`` and log matter here)
finds the listener in the container's TCP tables, relays a loopback host
port to it with one ``docker exec`` of the image's ``python3`` per
connection, and carries two GETs on one keep-alive connection, a GET on a
new one and a POST with a body. Each connection's exec ends with it, and
the environment's close takes the host port and the container away.

Gate: ``COLLIMATE_LIVE_DOCKER=1`` and a dockerd that answers; the image
(``python:3.13-slim``) is pulled when this machine does not have it.
"""
from __future__ import annotations

import http.client
import socket
import time
import types
import uuid

import pytest

from tests_live.conftest import DOCKER_GATE

pytestmark = pytest.mark.live

IMAGE = "python:3.13-slim"
PORT = 5001

# Answers GET with its path and POST with its body, over HTTP/1.1 keep-alive.
_SERVER = f"""\
import http.server
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def reply(self, body):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self.reply(b"get " + self.path.encode())
    def do_POST(self):
        self.reply(b"post " + self.rfile.read(int(self.headers["Content-Length"])))
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", {PORT}), Handler).serve_forever()
"""


@pytest.fixture
def docker_client():
    if not DOCKER_GATE:
        pytest.skip("set COLLIMATE_LIVE_DOCKER=1 to run the container scenarios")
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no dockerd answers: {e}")
    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        try:
            client.images.pull(IMAGE)
        except docker.errors.DockerException as e:
            pytest.skip(f"could not pull {IMAGE}: {e}")
    return client


@pytest.fixture
def loopback_container(docker_client):
    container = docker_client.containers.run(
        IMAGE, ["python3", "-c", _SERVER], detach=True,
        name=f"collimate-live-loopback-{uuid.uuid4().hex[:8]}",
        labels={"com.collimate.kind": "live-test"},
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            code, _ = container.exec_run(
                ["python3", "-c", f"import socket; socket.create_connection(('127.0.0.1', {PORT}), 1)"])
            if code == 0:
                break
            time.sleep(0.2)
        else:
            pytest.fail("the container's server never listened")
        yield container
    finally:
        try:
            container.remove(force=True)
        except Exception:  # noqa: BLE001 — the environment's close removed it
            pass


def _relays(container) -> list[str]:
    """The relay processes running in ``container``."""
    container.reload()
    if container.status != "running":
        return []
    return [row[-1] for row in container.top()["Processes"]
            if "socket.create_connection((sys.argv" in row[-1]]


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def test_a_server_bound_to_the_containers_loopback_is_relayed_by_exec(docker_client, loopback_container):
    from collimate_server.process_manager.environments import EnvironmentService, PlacedEnvironment, _container_ip

    container = loopback_container
    manager = types.SimpleNamespace(bound_ports=set(), log_buffer=[])
    service = EnvironmentService(manager, client_factory=lambda: docker_client)
    service._envs["env-1"] = PlacedEnvironment("env-1", container)

    # The bridge refuses: the server listens on the container's own loopback.
    with pytest.raises(OSError):
        socket.create_connection((_container_ip(container), PORT), timeout=2).close()

    listener = service.find_listener(PORT)
    assert listener is not None
    assert (listener.env_id, listener.bridge, listener.address, listener.relay) == (
        "env-1", False, "127.0.0.1", "python3")

    host_port = service.expose_listener(listener, serve=True)
    placed = service._envs["env-1"]
    assert placed.relayed == {PORT: host_port}
    assert host_port in placed.host_ports and host_port in manager.bound_ports
    assert service.serves_port(host_port)

    kept = http.client.HTTPConnection("127.0.0.1", host_port, timeout=10)
    for path in ("/one", "/two"):
        kept.request("GET", path)
        response = kept.getresponse()
        assert (response.status, response.read()) == (200, b"get " + path.encode())
    body = uuid.uuid4().hex.encode() * 4096
    kept.request("POST", "/form", body=body, headers={"Content-Type": "application/octet-stream"})
    response = kept.getresponse()
    assert (response.status, response.read()) == (200, b"post " + body)
    # One keep-alive connection, one exec.
    assert len(_relays(container)) == 1
    kept.close()

    fresh = http.client.HTTPConnection("127.0.0.1", host_port, timeout=10)
    fresh.request("GET", "/fresh")
    response = fresh.getresponse()
    assert (response.status, response.read()) == (200, b"get /fresh")
    fresh.close()

    # A closed connection's relay ends with it.
    assert _wait(lambda: not _relays(container)), _relays(container)

    # The same container port keeps its relay.
    assert service.expose_listener(service.find_listener(PORT), serve=True) == host_port
    assert len(placed.forwarders) == 1

    # A connection still open at close is ended, and the port refuses.
    held = socket.create_connection(("127.0.0.1", host_port), timeout=10)
    held.sendall(b"GET /held HTTP/1.1\r\nHost: x\r\n\r\n")
    held.settimeout(10)
    assert held.recv(65536).startswith(b"HTTP/1.")
    assert len(_relays(container)) == 1
    service.close("env-1")
    assert held.recv(65536) == b""
    held.close()
    assert not manager.bound_ports and not placed.relayed and not placed.forwarders
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", host_port), timeout=2).close()
    import docker
    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(container.id)
