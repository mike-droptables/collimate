# SPDX-License-Identifier: MIT
"""Serve the agent's handler and selected workspace tools to its harness.

The stateless streamable-HTTP server runs on an owned background thread. ACP
sessions reuse a cached server; terminal legs own their server's lifetime.
Bind only the Docker bridge gateway or loopback, with an unguessable per-run
URL path. A startup failure is reported to the caller.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import asyncio
import socket
import threading
from dataclasses import dataclass, field


from .workspace_tools import tool_description, with_handler_tools

# Client declarations/configuration use this same name: the client config key
# determines its mcp__<name>__ tool prefix. serverInfo.name alone does not.
SERVER_NAME = "collimate"


@dataclass
class McpBridge:
    """A running MCP bridge. ``url_container`` is the endpoint a container
    reaches over the host gateway (``host.docker.internal``); ``url_host`` the
    same endpoint from a HOST process at the actual bind address;
    ``tool_names`` the fully-qualified ``mcp__<SERVER_NAME>__*`` names."""
    port: int
    url_container: str
    url_host: str
    tool_names: list
    _server: "object | None" = field(default=None, repr=False)
    _thread: "threading.Thread | None" = field(default=None, repr=False)
    _stopped: bool = field(default=False, repr=False)

    def stop(self) -> None:
        """Stop the server and join its thread. Idempotent, never raises —
        the run-teardown contract."""
        if self._stopped:
            return
        self._stopped = True
        try:
            if self._server is not None:
                self._server.should_exit = True
                self._server.force_exit = True
                # Python 3.13 also waits for active transports in server.wait_closed.
                # Bound that wait so uvicorn cancels tools still awaiting the run loop.
                config = getattr(self._server, "config", None)
                if config is not None and (config.timeout_graceful_shutdown is None
                                           or config.timeout_graceful_shutdown > 1):
                    config.timeout_graceful_shutdown = 1
            if self._thread is not None:
                self._thread.join(timeout=5)
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass


def _docker_gateway_host() -> str:
    """The Docker bridge-gateway IP the container reaches the host on (the
    address ``host.docker.internal`` → ``host-gateway`` resolves to on Linux).

    Bind only the gateway so the local network cannot reach the tool server.
    The unguessable URL additionally isolates this run from other containers."""
    try:
        import docker  # provided by the harness runtime
        client = docker.from_env()
        try:
            net = client.networks.get("bridge")
            cfg = (net.attrs.get("IPAM") or {}).get("Config") or []
            gw = (cfg[0].get("Gateway") if cfg else "") or ""
        finally:
            client.close()
        if gw:
            return gw
    except Exception:  # noqa: BLE001 — fall back to the Linux default bridge gateway
        pass
    return "172.17.0.1"


def _bind_free_port(host: str, start: int = 8765) -> socket.socket:
    """A socket already BOUND to the first free port from ``start`` on ``host``.

    The socket itself is handed to uvicorn: probing a port, closing it and
    letting uvicorn bind it later let two bridges started together (or any
    other process) take the same port in between, and the loser's server
    thread died on "address already in use" after start_bridge had returned
    its URL."""
    port = start
    while port < start + 2000:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
            return s
        except OSError:
            s.close()
            port += 1
    raise RuntimeError("no free port for the MCP bridge")


def start_bridge(
    *, loop,
    bind_host: "str | None" = None,
    extra_entries: "list | None" = None,
) -> "McpBridge | None":
    """Start the stateless streamable-http MCP bridge on a background thread.
    ``loop`` is the generator's running event loop (so the async ``main_loop``
    tools can be scheduled back onto it from the server thread). Returns
    ``None`` when the server stack is unavailable. Handler tools are always on.

    ``bind_host`` picks the interface. Default (``None``) binds the Docker
    bridge-gateway IP so a CONTAINER reaches it over ``host.docker.internal``.
    A HOST-process consumer (the claude host-locus adapter) passes
    ``"127.0.0.1"`` — it needs no docker and only this host can reach it (the
    unguessable slug path is still the capability check).

    ``extra_entries`` contains registry rows in the 5-tuple shape —
    ``(name, gate_label, main_loop, twin_note, impl)`` — for the workspace
    file / web tools; ``main_loop`` rows get the generator-loop bounce."""
    extra = with_handler_tools(extra_entries or [])
    try:
        import uvicorn
        from mcp.server.mcpserver import MCPServer
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception as e:  # noqa: BLE001
        _secret_log(f"mcp/uvicorn unavailable — tools not exposed to this surface ({e})")
        return None

    # The gateway bind limits reachability to the docker network — but every
    # container on that bridge (not just ours) can reach the gateway IP, so the
    # endpoint additionally lives under an unguessable per-run path: a
    # capability URL only this run's agent config ever sees. (A localhost bind
    # for a host-process consumer is already host-private; the slug still gates.)
    import secrets
    slug = secrets.token_urlsafe(16)

    try:
        bind_host = bind_host or _docker_gateway_host()
        sock = _bind_free_port(bind_host)
        port = sock.getsockname()[1]
    except (OSError, RuntimeError) as e:
        # e.g. the fallback gateway address isn't locally bindable (no docker
        # bridge on this host). Degrade cleanly per the module contract.
        _secret_log(f"MCP bridge unavailable — tools not exposed to this surface ({e})")
        return None
    # The SDK's streamable-http server ships DNS-rebinding protection that
    # validates the Host header against localhost only — which rejects every
    # in-container consumer (they reach us as ``host.docker.internal:<port>``,
    # and were refused with "Invalid Host header" until this). Allow exactly
    # the names this bridge is reachable as; the unguessable slug path stays
    # the actual capability check, and the bind address keeps the LAN out.
    security = TransportSecuritySettings(allowed_hosts=[
            f"host.docker.internal:{port}",
            f"{bind_host}:{port}",
            f"127.0.0.1:{port}",
            f"localhost:{port}",
    ])
    try:
        server = MCPServer(SERVER_NAME)
    except Exception:
        sock.close()
        raise
    names: list = []

    def _bounced(fn):
        """Wrap an impl that awaits a loop-affine ``gen.*`` coroutine so it runs
        on the generator's main loop (where the IPC bridge lives), not the
        server thread. The wrapper carries the impl's signature so MCPServer still
        derives the right input schema; ``__wrapped__`` is stripped so MCPServer
        can't unwrap past the bounce and call the raw impl on the wrong loop."""
        import functools
        import inspect

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            fut = asyncio.run_coroutine_threadsafe(fn(*args, **kwargs), loop)
            return await asyncio.wrap_future(fut)

        wrapper.__signature__ = inspect.signature(fn)
        del wrapper.__wrapped__
        return wrapper

    # The registry drives registration: each workspace file/web row (same 5-tuple shape), is served under
    # its name with its ``gen.*``-twin description; ``main_loop`` rows (they
    # await an async gen.*) get the main-loop bounce, the sync-bodied rows
    # register directly (their gen.* calls block on their own IPC lock,
    # thread-safe).
    for name, _gate, main_loop, gen_twin, fn in extra:
        handler = _bounced(fn) if main_loop else fn
        if not main_loop:
            import inspect
            if not inspect.iscoroutinefunction(fn):
                import functools

                def offloaded(impl):
                    @functools.wraps(impl)
                    async def handler(*args, **kwargs):
                        return await asyncio.to_thread(impl, *args, **kwargs)
                    handler.__signature__ = inspect.signature(impl)
                    del handler.__wrapped__
                    return handler
                handler = offloaded(fn)
        server.tool(name=name, description=tool_description(fn, gen_twin))(handler)
        names.append(f"mcp__{SERVER_NAME}__{name}")

    # We own the uvicorn server (rather than calling MCPServer.run) so the
    # bridge has a real stop: force_exit abandons in-flight tools and the thread joins.
    try:
        path = f"/{slug}/mcp"
        app = server.streamable_http_app(
            stateless_http=True, streamable_http_path=path, transport_security=security,
        )
        # The capability belongs on the actual mounted route. MCP v2's
        # constructor settings no longer contain transport configuration.
        if not any(getattr(route, "path", None) == path for route in app.routes):
            raise RuntimeError("MCP server did not mount its per-run capability path")
        config = uvicorn.Config(
            app, host=bind_host, port=port, log_level="warning",
        )
        userver = uvicorn.Server(config)
    except Exception as e:  # noqa: BLE001
        _secret_log(f"MCP bridge unavailable — tools not exposed to this surface ({e})")
        sock.close()
        return None

    def _serve() -> None:
        try:
            userver.run(sockets=[sock])
        except Exception as e:  # noqa: BLE001
            _secret_log(f"MCP bridge server stopped: {e}")

    thread = threading.Thread(
        target=_serve, name="collimate-mcp-bridge", daemon=True,
    )
    thread.start()
    path = f"/{slug}/mcp"
    url_container = f"http://host.docker.internal:{port}{path}"
    url_host = f"http://{bind_host}:{port}{path}"
    _secret_log(f"MCP bridge serving {len(names)} tool(s) on {bind_host}:{port} (per-run path)")
    return McpBridge(
        port=port, url_container=url_container, url_host=url_host,
        tool_names=names, _server=userver, _thread=thread,
    )
