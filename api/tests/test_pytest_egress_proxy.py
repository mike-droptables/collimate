"""Tests for api/egress_proxy.py — the per-run CONNECT/HTTP forward proxy that
bounds a containerized (docker: limited) run's egress to an allowlist.

Two layers:
  * unit — allowlist suffix matching, the per-run credential, the
    absolute-form header rewrite, and a real CONNECT tunnel and a denied
    CONNECT against local servers (no docker);
  * live — a real container on a masquerade-off bridge network proving the
    demonstrated enforcement: allowed host passes THROUGH the proxy, denied host
    gets 403, direct (non-proxy) egress is black-holed. Gated on docker being
    reachable (mirrors test_sandbox.py's docker dependency, made a hard skip).
"""
from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
import uuid

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.dirname(THIS_DIR)
SDK_SRC = os.path.join(os.path.dirname(API_DIR), "sdk", "src")
for _p in (API_DIR, SDK_SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import egress_proxy  # noqa: E402
from egress_proxy import EgressProxy, addr_blocked, host_allowed  # noqa: E402
from process_manager.docker_runner import (  # noqa: E402
    default_runner_image, runner_image_build_args, runner_image_dir,
)


# ---------------------------------------------------------------------------
# docker availability gate (hard skip when the daemon isn't reachable)
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


DOCKER_OK = _docker_available()
requires_docker = pytest.mark.skipif(not DOCKER_OK, reason="docker daemon not reachable")

RUNNER_IMAGE = default_runner_image()


# ---------------------------------------------------------------------------
# Unit — allowlist matching
# ---------------------------------------------------------------------------

def test_host_allowed_suffix_matching():
    allow = ("anthropic.com", "example.org")
    assert host_allowed("anthropic.com", allow)
    assert host_allowed("api.anthropic.com", allow)          # label-boundary suffix
    assert host_allowed("a.b.example.org", allow)
    assert not host_allowed("notanthropic.com", allow)       # substring, not a suffix
    assert not host_allowed("anthropic.com.evil.net", allow)
    assert not host_allowed("example.com", allow)


def test_host_allowed_empty_denies_all():
    # The internet: none equivalence — an empty allowlist is a deny-all proxy.
    assert not host_allowed("anything.com", ())
    assert not host_allowed("localhost", ())


def test_host_allowed_normalizes_case_dot_and_port():
    assert host_allowed("API.Anthropic.COM", ("anthropic.com",))
    assert host_allowed("api.anthropic.com.", ("anthropic.com",))   # trailing dot
    assert host_allowed("api.anthropic.com:443", ("anthropic.com",))  # :port stripped
    assert host_allowed("host", (".host",))                          # leading-dot entry
    assert host_allowed("api.anthropic.com", (" Anthropic.COM. ",))  # entry's one spelling
    assert not host_allowed("", ("anything",))


def test_host_allowed_reads_entries_the_way_the_grant_does__F2041():
    """The proxy and the grant share one grammar: an entry the grant keeps
    matches here exactly as ``intersect`` treats it, and the spellings the
    linter warns about (wildcard, scheme, port) never match a host."""
    from runner_permissions import normalize_internet_allow

    written = [".GitHub.com.", "*.example.com", "https://api.example.com", "api.example.com:443"]
    grant = normalize_internet_allow(written)
    for host in ("github.com", "api.github.com", "api.example.com", "x.example.com"):
        assert host_allowed(host, tuple(written)) == host_allowed(host, grant), host
    assert host_allowed("api.github.com", grant)
    assert not host_allowed("api.example.com", grant)
    assert not host_allowed("x.example.com", grant)


# ---------------------------------------------------------------------------
# The per-run credential
# ---------------------------------------------------------------------------

def _auth_header(proxy: EgressProxy) -> str:
    """The ``Proxy-Authorization`` line a client derives from ``proxy.url``."""
    userinfo = proxy.url("h").split("//", 1)[1].rsplit("@", 1)[0]
    return f"Proxy-Authorization: Basic {base64.b64encode(userinfo.encode()).decode()}\r\n"


# ---------------------------------------------------------------------------
# Unit — a real CONNECT tunnel + a denied CONNECT (local echo server)
# ---------------------------------------------------------------------------

async def _start_echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader, writer):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(b"echo:" + data)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _connect_via_proxy(
    proxy: EgressProxy, target_host: str, target_port: int, *, auth: str | None = None,
):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    auth = _auth_header(proxy) if auth is None else auth
    writer.write(f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n{auth}\r\n".encode())
    await writer.drain()
    status_line = await reader.readline()
    # Drain the rest of the proxy's response headers.
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    return reader, writer, status_line


async def test_connect_tunnel_allows_and_relays(monkeypatch):
    # The only reachable test upstream is on loopback, which the proxy's
    # address check refuses — lift that check to exercise the relay itself.
    monkeypatch.setattr(egress_proxy, "addr_blocked", lambda ip: False)
    echo, echo_port = await _start_echo_server()
    proxy = EgressProxy("run-t", ("127.0.0.1",), bind_host="127.0.0.1")
    await proxy.start()
    try:
        reader, writer, status = await _connect_via_proxy(proxy, "127.0.0.1", echo_port)
        assert b"200" in status, status
        writer.write(b"ping")
        await writer.drain()
        got = await asyncio.wait_for(reader.read(64), timeout=5)
        assert got == b"echo:ping", got
        writer.close()
    finally:
        await proxy.stop()
        echo.close()


async def test_connect_tunnel_denies_and_hooks():
    echo, echo_port = await _start_echo_server()
    denied: list[tuple] = []
    proxy = EgressProxy(
        "run-d", ("example.com",), bind_host="127.0.0.1",
        on_deny=lambda h, p, m: denied.append((h, p, m)),
    )
    await proxy.start()
    try:
        reader, writer, status = await _connect_via_proxy(proxy, "127.0.0.1", echo_port)
        assert b"403" in status, status
        writer.close()
        # on_deny fired with the blocked target.
        assert denied == [("127.0.0.1", echo_port, "CONNECT")], denied
    finally:
        await proxy.stop()
        echo.close()


def test_proxy_and_agent_address_policy_match_shared_cases__F0459_F1341_F1351():
    import ast
    import ipaddress
    import json
    from pathlib import Path

    from collimate.shared import shared_path

    # Execute the deployed generator's predicate without importing its model
    # engines. These are separate deployment units and intentionally keep
    # local predicates; the fixture pins their actual behavior together.
    source = (Path(__file__).resolve().parents[2] / "content" /
              "generators/_agent_shared/web_tools.py")
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "ip_blocked")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    for case in json.loads(shared_path("egress-addresses.json").read_text()):
        ip = ipaddress.ip_address(case["address"])
        assert addr_blocked(ip) is case["blocked"], case["address"]
        assert namespace["ip_blocked"](ip) is case["blocked"], case["address"]


async def _start_counting_server() -> tuple[asyncio.AbstractServer, int, list]:
    """A loopback server that records every connection it accepts."""
    hits: list = []

    async def handle(reader, writer):
        hits.append("connected")  # on accept: a tunnel may never send bytes
        try:
            await asyncio.wait_for(reader.read(4096), timeout=2)
        except asyncio.TimeoutError:
            pass
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\nsecret")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], hits


async def test_connect_refuses_an_allowlisted_name_that_resolves_to_loopback():
    # The proxy connects from the HOST namespace: an allowlisted name that
    # answers 127.0.0.1 must not reach host-loopback services through it.
    server, port, hits = await _start_counting_server()
    denied: list[tuple] = []
    proxy = EgressProxy(
        "run-l", ("localhost",), bind_host="127.0.0.1",
        on_deny=lambda h, p, m: denied.append((h, p, m)),
    )
    await proxy.start()
    try:
        reader, writer, status = await asyncio.wait_for(
            _connect_via_proxy(proxy, "localhost", port), timeout=10,
        )
        assert b"403" in status, status
        writer.close()
        await asyncio.sleep(0.1)
        assert hits == []
        assert denied == [("localhost", port, "CONNECT")], denied
    finally:
        await proxy.stop()
        server.close()


async def test_absolute_form_refuses_an_allowlisted_name_that_resolves_to_loopback():
    server, port, hits = await _start_counting_server()
    proxy = EgressProxy("run-a", ("localhost",), bind_host="127.0.0.1")
    pport = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        writer.write(
            f"GET http://localhost:{port}/ HTTP/1.1\r\n"
            f"Host: localhost:{port}\r\n{_auth_header(proxy)}\r\n".encode()
        )
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(4096), timeout=5)
        assert resp.startswith(b"HTTP/1.1 403"), resp
        assert b"secret" not in resp
        writer.close()
        await asyncio.sleep(0.1)
        assert hits == []
    finally:
        await proxy.stop()
        server.close()


async def test_a_request_without_the_credential_gets_407_and_no_deny_event__F0405():
    """The proxy listens on every interface, so a network peer can reach it:
    without the run's credential it gets 407 before anything is routed — no
    upstream connect, no allowlist decision, no ``egress_denied`` event."""
    server, port, hits = await _start_counting_server()
    denied: list[tuple] = []
    proxy = EgressProxy(
        "run-c", ("example.com",), bind_host="127.0.0.1",
        on_deny=lambda h, p, m: denied.append((h, p, m)),
    )
    await proxy.start()
    wrong = base64.b64encode(b"collimate:not-the-token").decode()
    try:
        for auth in ("", f"Proxy-Authorization: Basic {wrong}\r\n",
                     "Proxy-Authorization: Bearer x\r\n"):
            reader, writer, status = await _connect_via_proxy(
                proxy, "127.0.0.1", port, auth=auth)
            assert status.startswith(b"HTTP/1.1 407"), (auth, status)
            writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(4096), timeout=5)
        assert resp.startswith(b"HTTP/1.1 407"), resp
        assert b'Proxy-Authenticate: Basic realm="collimate"' in resp
        writer.close()
        await asyncio.sleep(0.1)
        assert hits == [] and denied == []
    finally:
        await proxy.stop()
        server.close()


async def test_the_credential_is_per_proxy_and_rides_the_proxy_url__F0405():
    a = EgressProxy("run-1", (), bind_host="127.0.0.1")
    b = EgressProxy("run-2", (), bind_host="127.0.0.1")
    with pytest.raises(RuntimeError):
        a.url("host.docker.internal")
    await a.start()
    await b.start()
    try:
        url = a.url("host.docker.internal")
        assert url.startswith("http://collimate:") and url.endswith(f"@host.docker.internal:{a.port}")
        assert url.split("@")[0] != b.url("host.docker.internal").split("@")[0]
        # b's credential does not open a.
        reader, writer, status = await _connect_via_proxy(a, "example.com", 443, auth=_auth_header(b))
        assert status.startswith(b"HTTP/1.1 407"), status
        writer.close()
    finally:
        await a.stop()
        await b.stop()


async def _start_keepalive_server() -> tuple[asyncio.AbstractServer, int, list]:
    """A loopback HTTP server that answers every request on a connection
    until the request asks it to close; records each request head."""
    heads: list[bytes] = []

    async def handle(reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                heads.append(head)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
                if b"connection: close" in head.lower():
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], heads


async def test_absolute_form_strips_hop_headers_and_closes_the_upstream__F0404(monkeypatch):
    """A plain-HTTP request goes upstream in origin form without the proxy's
    credential or any hop-by-hop header, with ``Host`` from the vetted URL and
    ``Connection: close``: the upstream answers once and closes, so a second
    request on the client's keep-alive connection can never ride the first
    request's vetted upstream to another host."""
    monkeypatch.setattr(egress_proxy, "addr_blocked", lambda ip: False)
    server, port, heads = await _start_keepalive_server()
    proxy = EgressProxy("run-k", ("127.0.0.1",), bind_host="127.0.0.1")
    await proxy.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    try:
        writer.write(
            f"GET http://127.0.0.1:{port}/a?q=1 HTTP/1.1\r\n"
            f"Host: elsewhere.example\r\n"
            f"Proxy-Connection: keep-alive\r\n"
            f"Connection: keep-alive, X-Hop\r\n"
            f"Keep-Alive: timeout=5\r\n"
            f"X-Hop: 1\r\n"
            f"X-End: 2\r\n"
            f"{_auth_header(proxy)}\r\n".encode()
        )
        await writer.drain()
        resp = await asyncio.wait_for(reader.readuntil(b"ok"), timeout=5)
        assert resp.startswith(b"HTTP/1.1 200"), resp
        # The same client connection, now for another host: never forwarded.
        writer.write(
            f"GET http://other.example/ HTTP/1.1\r\n{_auth_header(proxy)}\r\n".encode())
        try:
            await writer.drain()
        except ConnectionError:
            pass
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        assert len(heads) == 1, heads
        head = heads[0].decode("latin-1")
        lines = head.split("\r\n")
        assert lines[0] == "GET /a?q=1 HTTP/1.1"
        assert f"Host: 127.0.0.1:{port}" in lines
        assert "X-End: 2" in lines and "Connection: close" in lines
        lowered = head.lower()
        for gone in ("proxy-authorization", "proxy-connection", "keep-alive", "x-hop",
                     "elsewhere.example"):
            assert gone not in lowered, (gone, head)
    finally:
        # Closing the client unwinds any relay still open, so stop() (which
        # waits for open connections) returns even when an assertion failed.
        writer.close()
        await asyncio.wait_for(proxy.stop(), timeout=5)
        server.close()


async def test_a_malformed_head_is_refused_with_400__F0404():
    proxy = EgressProxy("run-m", ("127.0.0.1",), bind_host="127.0.0.1")
    await proxy.start()
    try:
        for head in (
            b"GET http://127.0.0.1/ HTTP/1.1\r\nX-A: 1\r\n folded\r\n\r\n",
            b"GET http://127.0.0.1/ HTTP/1.1\r\nno colon here\r\n\r\n",
            b"GET http://127.0.0.1/ HTTP/1.1\r\nX-A: 1\rHost: evil\r\n\r\n",
            b"GET\rX http://127.0.0.1/ HTTP/1.1\r\n\r\n",
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(head)
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert resp.startswith(b"HTTP/1.1 400"), (head, resp)
            writer.close()
    finally:
        await proxy.stop()


async def test_oversized_request_head_is_refused_with_431():
    """The head is read from the untrusted run, so its size is capped: an
    endless stream of header lines is cut off, not buffered."""
    proxy = EgressProxy("run-h", ("127.0.0.1",), bind_host="127.0.0.1")
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        request_line = b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n"
        writer.write(request_line)
        # Stop at the line that crosses the cap, so the proxy consumes every
        # byte sent and the close cannot race the response with a reset.
        line = b"X-A: " + b"a" * 40 + b"\r\n"
        n = (egress_proxy._MAX_HEAD_BYTES - len(request_line)) // len(line) + 1
        for _ in range(n):
            writer.write(line)
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(4096), timeout=5)
        assert resp.startswith(b"HTTP/1.1 431"), resp
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        writer.close()
    finally:
        await proxy.stop()


async def test_idle_client_is_dropped_after_the_head_timeout(monkeypatch):
    monkeypatch.setattr(egress_proxy, "_HEAD_TIMEOUT", 0.2)
    proxy = EgressProxy("run-i", (), bind_host="127.0.0.1")
    pport = await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        assert await asyncio.wait_for(reader.read(), timeout=2) == b""
        writer.close()
    finally:
        await proxy.stop()


async def test_stop_is_idempotent():
    proxy = EgressProxy("run-x", (), bind_host="127.0.0.1")
    await proxy.start()
    await proxy.stop()
    await proxy.stop()  # no raise


# ---------------------------------------------------------------------------
# Live — real container, masquerade-off network, end-to-end enforcement
# ---------------------------------------------------------------------------

def _ensure_runner_image() -> bool:
    if subprocess.run(
        ["docker", "image", "inspect", RUNNER_IMAGE],
        capture_output=True,
    ).returncode == 0:
        return True
    command = ['docker', 'build']
    for key, value in sorted(runner_image_build_args().items()):
        command.extend(['--build-arg', f'{key}={value}'])
    command.extend(['-t', RUNNER_IMAGE, runner_image_dir()])
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, (
        f'runner image build failed (exit {result.returncode}) for {RUNNER_IMAGE}\n'
        f'{result.stdout}\n{result.stderr}'
    )
    return True


def test_cold_runner_image_build_uses_canonical_python_and_identity__F0227_F0180_F0197(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if command[1] == 'image' else 0,
                                           stdout=b'build output', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)
    assert _ensure_runner_image()
    build = calls[1]
    assert '--build-arg' in build
    supplied = [build[i + 1] for i, arg in enumerate(build) if arg == '--build-arg']
    assert supplied == [f'{key}={value}' for key, value in sorted(runner_image_build_args().items())]
    assert calls[0] == ['docker', 'image', 'inspect', default_runner_image()]
    assert build[build.index('-t') + 1] == default_runner_image()
    assert build[-1] == runner_image_dir()
    assert len(calls) == 2


def test_existing_canonical_runner_image_is_reused__F0227(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command == ['docker', 'image', 'inspect', default_runner_image()]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, 'run', run)
    assert _ensure_runner_image()
    assert len(calls) == 1


def test_runner_image_build_failure_retains_diagnostics__F0180(monkeypatch):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1 if command[1] == 'image' else 23,
                                           stdout='builder context', stderr='builder refusal')

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(AssertionError, match='(?s)exit 23.*builder context.*builder refusal'):
        _ensure_runner_image()


@requires_docker
def test_live_egress_enforcement_in_container():
    """The core deliverable, demonstrated: a container on a masquerade-off
    bridge with HTTP(S)_PROXY pointed at a live EgressProxy —
      * an ALLOWED host reaches the internet THROUGH the proxy (200);
      * a DENIED host is refused at CONNECT (403 tunnel failure);
      * DIRECT (non-proxy) egress is black-holed by the masquerade-off net.
    """
    assert _ensure_runner_image(), "runner image build failed"

    net = f"collimate-egtest-{uuid.uuid4().hex[:8]}"
    results: dict[str, str] = {}

    async def _run():
        denies: list[tuple] = []
        proxy = EgressProxy(
            "run-live", ("example.com",),
            on_deny=lambda h, p, m: denies.append((h, p, m)),
        )
        await proxy.start()
        # Masquerade-off bridge: no working route to the public internet, but
        # the on-link host gateway (proxy + IPC) stays reachable.
        create = await asyncio.create_subprocess_exec(
            "docker", "network", "create", "--driver", "bridge",
            "--opt", "com.docker.network.bridge.enable_ip_masquerade=false", net,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, cerr = await create.communicate()
        assert create.returncode == 0, cerr.decode()

        proxy_url = proxy.url("host.docker.internal")

        async def _probe(script: str) -> str:
            proc = await asyncio.create_subprocess_exec(
                "docker", "run", "--rm", "-i", "--network", net,
                "--add-host", "host.docker.internal:host-gateway",
                "-e", f"HTTP_PROXY={proxy_url}", "-e", f"HTTPS_PROXY={proxy_url}",
                "-e", "NO_PROXY=host.docker.internal",
                RUNNER_IMAGE, "python", "-c", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            return out.decode(errors="replace").strip()

        try:
            results["allowed"] = await _probe(
                "import urllib.request as u\n"
                "try:\n"
                "    r=u.urlopen('https://example.com/', timeout=20); print('OK', r.status)\n"
                "except Exception as e: print('FAIL', type(e).__name__)\n"
            )
            results["denied"] = await _probe(
                "import urllib.request as u, urllib.error as e\n"
                "try:\n"
                "    u.urlopen('https://www.google.com/', timeout=20); print('REACHED')\n"
                "except Exception as ex: print('BLOCKED', '403' in str(ex))\n"
            )
            results["direct"] = await _probe(
                "import socket\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1',443), timeout=8); print('REACHED')\n"
                "except Exception: print('BLOCKED')\n"
            )
            results["denies"] = str(len(denies))
        finally:
            await proxy.stop()
            rm = await asyncio.create_subprocess_exec(
                "docker", "network", "rm", net,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await rm.wait()

    asyncio.run(_run())

    assert results["allowed"].startswith("OK"), f"allowed host should pass: {results}"
    assert results["denied"] == "BLOCKED True", f"denied host should 403: {results}"
    assert results["direct"] == "BLOCKED", f"direct egress should be black-holed: {results}"
    assert int(results["denies"]) >= 1, "the deny hook should have fired"
