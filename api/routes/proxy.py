"""Reverse proxy routes — forwards HTTP and WebSocket traffic to generator
live-preview ports."""
import asyncio
import logging
from urllib.parse import quote
import httpx
import websockets
from fastapi import APIRouter, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

router = APIRouter()
logger = logging.getLogger(__name__)


class _SharedTransport(httpx.AsyncBaseTransport):
    """Request clients borrow one transport; server teardown owns its close."""

    def __init__(self):
        self.pool = httpx.AsyncHTTPTransport(
            trust_env=False, limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def handle_async_request(self, request):
        return await self.pool.handle_async_request(request)

    async def aclose(self):
        pass


_transport: _SharedTransport | None = None


def _request_transport() -> _SharedTransport:
    global _transport
    if _transport is None:
        _transport = _SharedTransport()
    return _transport


async def close_proxy_transport() -> None:
    global _transport
    transport, _transport = _transport, None
    if transport is not None:
        await transport.pool.aclose()

# Headers that must not be forwarded
_HOP_BY_HOP = {"host", "content-length", "transfer-encoding", "connection",
               "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "upgrade"}

# Upstream response headers that act on the app's whole origin, which a
# proxied app shares: upstream ``Service-Worker-Allowed`` is replaced with
# this run and port's scope below; ``Clear-Site-Data``
# would wipe the app's own storage, and ``NEL`` with ``Report-To`` or
# ``Reporting-Endpoints`` would install a reporting policy for the origin that
# outlives the run and sends the URLs of later app requests (capability URLs
# among them) to an endpoint of the app's choosing. Those policies are dropped.
_ORIGIN_WIDE_RESPONSE_HEADERS = {
    "service-worker-allowed", "clear-site-data",
    "nel", "report-to", "reporting-endpoints",
}


def _strip_server_credentials(headers: dict) -> dict:
    """Drop this server's own credential from headers bound for a generator.

    The desktop shell injects ``Authorization: Bearer <token>`` on every
    same-origin request (proxy iframes included); it may not reach
    generator-controlled code (the same invariant the subprocess env
    scrubbing pins). A proxied app's own Authorization passes through
    untouched, and so do cookies: the server authenticates by the bearer
    header alone."""
    from auth_middleware import auth
    for key in list(headers):
        if key.lower() == "authorization":
            scheme, _, credential = headers[key].partition(" ")
            if scheme.lower() == "bearer" and auth.matches(credential.strip()):
                del headers[key]
    return headers


def _run_owns_port(channel: str, run_id: str, port: int) -> bool:
    """Whether ``run_id`` is a currently-active run that bound ``port``.

    The proxy path carries both ``run_id`` and ``port``, but ports are
    recycled the instant a process exits (a daemon's port scan always
    restarts from the same base), so forwarding by port alone lets a
    stopped cell's renderer — which reconnects forever to its baked-in
    proxy URL — reach whatever new run reused that port. Gating on the
    run being live AND owning the port severs that: a dead run_id is
    rejected, and a reused port only answers for the run that now holds
    it (its run_id differs, so the stale URL still misses). A run whose
    process has exited is dead here too, although it stays registered
    through its run-end cleanup: its ports are free for the next run."""
    # Imported lazily — routes.runs pulls in a chunk of the app and this
    # module is imported early during route registration.
    from run_registry import active_managers
    from state import get_channel_by_id
    owner = get_channel_by_id(channel)
    manager = active_managers.get(run_id)
    if owner is None or manager is None or manager.channel != str(owner.id):
        return False
    proc = getattr(manager, "process", None)
    if proc is not None and proc.returncode is not None:
        return False
    return port in getattr(manager, "bound_ports", set())


def _dial_target(run_id: str, port: int) -> tuple[str, int]:
    """Where to dial for a run's bound port — the backend's port contract.

    Local backends (native, local docker) publish on loopback; a remote
    backend reports the forwarded host:port its runner exposed. Falls
    back to loopback when the manager or backend is gone (the ownership
    gate has already passed by the time this is called)."""
    from run_registry import active_managers
    manager = active_managers.get(run_id)
    backend = getattr(manager, "backend", None)
    if backend is None:
        return ("127.0.0.1", port)
    return backend.port_target(port)


async def proxy_to_generator(
    channel: str, run_id: str, port: int, path: str, request: Request
):
    """Transparently forward requests to a generator's bound port."""
    if not _run_owns_port(channel, run_id, port):
        # Run stopped (or never owned this port). 410 rather than 502 so
        # the client can tell "this run is gone" from "upstream hiccup".
        raise HTTPException(410, "This generator run is no longer active")
    dial_host, dial_port = _dial_target(run_id, port)
    target_url = f"http://{dial_host}:{dial_port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    _strip_server_credentials(forward_headers)
    # Force identity encoding upstream. httpx only ships gzip/deflate
    # decoders, so if upstream picks brotli or zstd we'd get raw compressed
    # bytes back and have no way to decompress them. Asking for identity
    # sidesteps the whole problem at the cost of not compressing the
    # localhost-to-localhost hop (negligible).
    forward_headers["accept-encoding"] = "identity"

    # Stream the response instead of buffering it. Proxied dev servers hold
    # bodies open indefinitely — code-server pushes Server-Sent-Events for the
    # whole life of a session, and a dev server's rebuild stream can idle well
    # past a minute — which the old buffered read with a 30s cap truncated.
    # read=None lifts the per-read timeout so a slow stream isn't cut off,
    # while a connect timeout still fails fast on a dead port.
    # follow_redirects=False so 3xx responses pass through to the browser
    # unchanged — code-server uses a 302 to push query params (e.g.
    # ?folder=/home/coder/project) into the URL, which a server-side redirect
    # would swallow.
    client = httpx.AsyncClient(
        transport=_request_transport(),
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0),
    )
    try:
        upstream_req = client.build_request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            content=await request.body(),
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        await client.aclose()
        raise HTTPException(502, f"Generator on port {port} is not reachable")
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(504, "Gateway timeout proxying to generator")
    except httpx.HTTPError:
        # Any other transport failure (RemoteProtocolError / ReadError
        # while a dev server restarts mid-request) is still an upstream
        # fault: 502, and the client is closed rather than leaked.
        await client.aclose()
        raise HTTPException(502, f"Generator on port {port} failed: proxy error")
    except BaseException:
        # E.g. a client disconnect while reading request.body().
        await client.aclose()
        raise

    # aiter_bytes() decodes any content-encoding, so dropping that header keeps
    # the bytes and the metadata consistent; strip transfer-encoding/
    # content-length too because Starlette frames the streamed body itself.
    # Set-Cookie is re-added one header per cookie below: items() comma-joins
    # repeated keys, and browsers read a joined Set-Cookie as one cookie. The
    # origin-wide headers are dropped (``_ORIGIN_WIDE_RESPONSE_HEADERS``).
    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in {"transfer-encoding", "connection", "content-length",
                             "content-encoding", "set-cookie",
                             *_ORIGIN_WIDE_RESPONSE_HEADERS}
    }
    # Workers may serve this run's app, including nested script locations,
    # but cannot control the host UI or another run/port. The frontend removes
    # matching registrations on run end and reconciles them on reconnect.
    response_headers["service-worker-allowed"] = (
        f"/proxy/{quote(channel, safe='')}/{quote(run_id, safe='')}/{port}/"
    )

    async def _body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    resp = StreamingResponse(
        _body(),
        status_code=upstream.status_code,
        headers=response_headers,
    )
    for k, v in upstream.headers.multi_items():
        if k.lower() == "set-cookie":
            resp.headers.append("set-cookie", v)
    return resp


# Registered once per verb rather than as one ``api_route`` carrying the
# whole methods list. FastAPI derives a route's OpenAPI operation id from the
# route name, the path and its FIRST method only — so a seven-method route
# publishes seven operations that all share one id, and every schema build
# (each /openapi.json request, each test client) logs a "Duplicate Operation
# ID" warning. One route per verb gives each operation its own id; the
# handler, the path and the match order are unchanged, and the drift
# snapshot (paths + methods, never ids) does not move.
_PROXY_PATH = "/proxy/{channel}/{run_id}/{port}/{path:path}"
for _method in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
    router.add_api_route(
        _PROXY_PATH, proxy_to_generator, methods=[_method],
        name="proxy_to_generator",
        operation_id=f"proxy_to_generator_{_method.lower()}",
    )


@router.websocket("/proxy/{channel}/{run_id}/{port}/{path:path}")
async def proxy_ws_to_generator(
    websocket: WebSocket, channel: str, run_id: str, port: int, path: str
):
    """Transparently forward WebSocket traffic to a generator's bound port.

    Required for code-server, Jupyter, and any other generator that uses a
    workbench-to-server WebSocket for live state.
    """
    if not _run_owns_port(channel, run_id, port):
        # Stale connection to a stopped run (or a port it no longer owns).
        # Close before the handshake so the renderer's reconnect loop sees
        # a hard failure and stops painting a live stream from a dead cell.
        await websocket.close(code=4410)
        return
    requested_subprotocols = list(websocket.scope.get("subprotocols") or [])

    dial_host, dial_port = _dial_target(run_id, port)
    target = f"ws://{dial_host}:{dial_port}/{path}"
    qs = websocket.scope.get("query_string") or b""
    if qs:
        target += "?" + qs.decode("latin-1")

    # Dial upstream BEFORE accepting the browser: the client then gets the
    # subprotocol the upstream actually picked, and a dead upstream refuses
    # the handshake instead of looking like a normal open-then-close (the
    # renderers count failed opens to decide a run is gone).
    try:
        upstream = await websockets.connect(
            target,
            proxy=None,
            additional_headers={"Cookie": websocket.headers["cookie"]} if "cookie" in websocket.headers else None,
            subprotocols=requested_subprotocols or None,
            open_timeout=10,
            max_size=None,  # code-server sends large frames; don't cap
            ping_interval=None,  # let the upstream drive keepalives
        )
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        logger.warning(
            "WS proxy: upstream dial failed for run %s port %s path /%s: %r",
            run_id, port, path, exc,
        )
        await websocket.close(code=1011)
        return

    try:
        await websocket.accept(subprotocol=upstream.subprotocol)

        async def client_to_upstream():
            try:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    data_b = msg.get("bytes")
                    data_t = msg.get("text")
                    if data_b is not None:
                        await upstream.send(data_b)
                    elif data_t is not None:
                        await upstream.send(data_t)
            except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                pass

        async def upstream_to_client():
            try:
                async for msg in upstream:
                    if isinstance(msg, bytes):
                        await websocket.send_bytes(msg)
                    else:
                        await websocket.send_text(msg)
            except (websockets.exceptions.ConnectionClosed, WebSocketDisconnect,
                    RuntimeError):
                # RuntimeError: Starlette refusing a send on a closed socket.
                pass

        tasks = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancel AND await both pumps, also when this handler is itself
            # cancelled (asyncio.wait never cancels its children), so none
            # outlives the handler or leaves an unretrieved exception.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, websockets.exceptions.WebSocketException):
        pass
    finally:
        await upstream.close()
        try:
            await websocket.close()
        except Exception:
            pass
