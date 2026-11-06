"""ASGI bridge: HTTP requests (streaming, binary-safe) and WebSockets.

The page hands every same-origin ``/api/*`` request to :func:`http` and
every ``/api/*`` WebSocket to :func:`ws_connect`; both drive the real ASGI
app with ordinary ``(scope, receive, send)`` calls on the pyodide event
loop and report back through small duck-typed JS objects:

HTTP ``sink`` (what sw.js turns into a ``Response``):
    ``start(status, headers)``  headers as ``[[name, value], ...]``
    ``chunk(bytes)``            one body chunk (a ``Uint8Array``)
    ``end()``                   body complete
    ``error(message)``          page-side only: boot.js calls it when dispatch
                                itself throws (the bridge never does; its own
                                failures go through start/chunk/end)
A cancelled fetch (an EventSource closed, a navigated-away page) calls
``handle.cancel()`` on the returned :class:`HttpHandle`; the app then sees
``http.disconnect`` from ``receive()`` and its streaming generator exits.

WebSocket ``sock`` (the page-level ``WebSocket`` replacement in sw-gate.js):
    ``_accept(protocol)``       ``websocket.accept`` → the open event
    ``_message(data)``          a text (str) or binary (Uint8Array) frame
    ``_close(code, reason)``    the socket closed: the app's code and reason
                                when the app closed it, the page's own when
                                the page did (what a browser reports for a
                                close the server simply acknowledged)
The page sends frames through the returned :class:`WsHandle`'s ``send`` and
``close``. Bytes cross the boundary as ``Uint8Array`` in both directions
(``pyodide.ffi.to_js`` on the way out, ``.to_bytes()`` on the way in) —
never as strings.

:func:`request` is the in-process helper boot.py uses to seed the demo
through the app's own routes; it collects a whole response in memory.
"""
from __future__ import annotations

import asyncio
import json
import traceback
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

try:  # inside pyodide
    from pyodide.ffi import to_js as _to_js
except ImportError:  # native tests
    def _to_js(value, **_kw):
        return value


def _js_bytes(data: bytes):
    """Python bytes → a JS Uint8Array (a plain bytes object outside pyodide)."""
    return _to_js(data)


def _from_js_bytes(data) -> bytes:
    """A JS ``Uint8Array`` (or Python bytes) → bytes; ``null``/``undefined``
    (which pyodide hands over as ``JsNull``/``None``) → ``b""``."""
    if data is None:
        return b""
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    to_bytes = getattr(data, "to_bytes", None)
    if to_bytes is not None:
        return bytes(to_bytes())
    to_py = getattr(data, "to_py", None)
    if to_py is None:
        return b""  # JsNull and other non-buffer values
    value = to_py()
    return bytes(value) if value is not None else b""


def _headers(raw) -> list[tuple[bytes, bytes]]:
    """``[[name, value], ...]`` (JS or Python) → ASGI header pairs."""
    to_py = getattr(raw, "to_py", None)
    pairs = to_py() if to_py is not None else raw
    return [(str(k).lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in pairs]


def _scope_base(scheme: str, path: str, query: str, headers) -> dict:
    # ``path`` arrives percent-encoded (URL.pathname); ASGI wants it decoded
    # and keeps the wire form in ``raw_path`` — the same split uvicorn makes,
    # so a cell file path like ``_runs%2F<label>%2Fchat%2Fmain.json`` routes.
    return {
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": scheme,
        "path": unquote(path),
        "raw_path": path.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "root_path": "",
        "headers": _headers(headers),
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 0),
    }


ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class HttpHandle:
    """The page's grip on one in-flight request: ``cancel()`` delivers
    ``http.disconnect`` and stops the app task."""

    def __init__(self) -> None:
        self._disconnected = asyncio.Event()
        self.task: asyncio.Task | None = None

    def cancel(self) -> None:
        self._disconnected.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()


def http(app: ASGIApp, method: str, path: str, query: str, headers_json: str,
         body, sink) -> HttpHandle:
    """Dispatch one request to ``app``; responses stream into ``sink``."""
    handle = HttpHandle()
    body_bytes = _from_js_bytes(body)
    headers = json.loads(headers_json)
    scope = {"type": "http", "method": method.upper(), **_scope_base("http", path, query, headers)}
    handle.task = asyncio.ensure_future(_run_http(app, scope, body_bytes, sink, handle))
    return handle


async def _run_http(app: ASGIApp, scope: dict, body: bytes, sink, handle: HttpHandle) -> None:
    body_sent = False
    started = False
    ended = False

    async def receive() -> dict:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await handle._disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal started, ended
        if ended or handle._disconnected.is_set():
            return
        kind = message["type"]
        if kind == "http.response.start":
            started = True
            headers = [
                [k.decode("latin-1"), v.decode("latin-1")]
                for k, v in message.get("headers", [])
            ]
            sink.start(int(message["status"]), _to_js(headers))
        elif kind == "http.response.body":
            chunk = message.get("body", b"") or b""
            if chunk:
                sink.chunk(_js_bytes(bytes(chunk)))
            if not message.get("more_body", False):
                ended = True
                sink.end()

    try:
        await app(scope, receive, send)
        if started and not ended:
            ended = True
            sink.end()
        elif not started:
            _fail(sink, 500, "the app produced no response")
    except asyncio.CancelledError:
        # The fetch was cancelled by the page: nobody is listening for the
        # body any more, but the sink's end() is what closes the page's end
        # of the message port, so it still runs.
        if not ended:
            ended = True
            sink.end()
        return
    except Exception:  # noqa: BLE001 — surfaced to the page as a 500
        detail = traceback.format_exc()
        if not started:
            _fail(sink, 500, detail)
        elif not ended:
            ended = True
            sink.end()


def _fail(sink, status: int, detail: str) -> None:
    payload = json.dumps({"detail": detail}).encode("utf-8")
    sink.start(status, _to_js([["content-type", "application/json"]]))
    sink.chunk(_js_bytes(payload))
    sink.end()


# ---------------------------------------------------------------------------
# In-process requests (boot-time seeding)
# ---------------------------------------------------------------------------

class Response:
    def __init__(self) -> None:
        self.status = 0
        self.headers: list[list[str]] = []
        self.body = bytearray()

    def json(self) -> Any:
        return json.loads(bytes(self.body).decode("utf-8"))

    @property
    def text(self) -> str:
        return bytes(self.body).decode("utf-8", "replace")


class _Collector:
    def __init__(self) -> None:
        self.response = Response()
        self.done = asyncio.Event()

    def start(self, status, headers):
        self.response.status = status
        self.response.headers = list(headers)

    def chunk(self, data):
        self.response.body += bytes(data)

    def end(self):
        self.done.set()


async def request(app: ASGIApp, method: str, path: str, *, query: str = "",
                  json_body: Any = None, body: bytes = b"") -> Response:
    """Run one request against ``app`` in-process and collect the response."""
    headers = [["host", "localhost"]]
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers.append(["content-type", "application/json"])
    headers.append(["content-length", str(len(body))])
    collector = _Collector()
    handle = http(app, method, path, query, json.dumps(headers), body, collector)
    await handle.task
    await collector.done.wait()
    return collector.response


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

class WsHandle:
    """The page's end of one bridged socket."""

    def __init__(self, app: ASGIApp, url: str, protocols: list[str], sock) -> None:
        parts = urlsplit(url)
        self._sock = sock
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._page_close: tuple[int, str] | None = None
        scope = {
            "type": "websocket",
            "subprotocols": protocols,
            **_scope_base("ws", parts.path, parts.query, [["host", parts.netloc or "localhost"]]),
        }
        self.task = asyncio.ensure_future(self._run(app, scope))

    # ---- page → app ----
    def send(self, data) -> None:
        if self._closed:
            return
        if isinstance(data, str):
            self._inbox.put_nowait({"type": "websocket.receive", "text": data})
        else:
            self._inbox.put_nowait({"type": "websocket.receive", "bytes": _from_js_bytes(data)})

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        self._page_close = (int(code or 1000), str(reason or ""))
        self._inbox.put_nowait({"type": "websocket.disconnect", "code": self._page_close[0]})

    # ---- app → page ----
    def _emit_close(self, code: int, reason: str) -> None:
        self._closed = True
        self._sock._close(code, reason)

    async def _run(self, app: ASGIApp, scope: dict) -> None:
        connect_sent = False
        accepted = False
        closed_by_app = False

        async def receive() -> dict:
            nonlocal connect_sent
            if not connect_sent:
                connect_sent = True
                return {"type": "websocket.connect"}
            return await self._inbox.get()

        async def send(message: dict) -> None:
            nonlocal accepted, closed_by_app
            kind = message["type"]
            if kind == "websocket.accept":
                accepted = True
                self._sock._accept(message.get("subprotocol") or "")
            elif kind == "websocket.send":
                if "text" in message and message["text"] is not None:
                    self._sock._message(message["text"])
                elif message.get("bytes") is not None:
                    self._sock._message(_js_bytes(bytes(message["bytes"])))
            elif kind == "websocket.close":
                closed_by_app = True
                self._emit_close(int(message.get("code", 1000)), str(message.get("reason") or ""))
            elif kind == "websocket.http.response.start":
                # A handshake rejection (403) — no accept, so the page sees
                # an abnormal close, like a browser would.
                closed_by_app = True
                self._emit_close(1006, f"handshake rejected ({message.get('status')})")

        try:
            await app(scope, receive, send)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        if not closed_by_app:
            if self._page_close is not None:
                self._emit_close(*self._page_close)
            else:
                self._emit_close(1000 if accepted else 1006, "")


def ws_connect(app: ASGIApp, url: str, protocols_json: str, sock) -> WsHandle:
    protocols = [str(p) for p in json.loads(protocols_json or "[]")]
    return WsHandle(app, url, protocols, sock)
