"""The demo's own terminal sessions and their WebSocket route.

The app's ``/api/terminals/{id}`` spawns a docker PTY on attach, which does
not exist here, so a timeline's ``demo.terminal(label)`` is served by this
module instead: ``/api/demo/terminals/{terminal_id}`` speaks the terminal
renderer's wire protocol exactly (binary frames both ways, ``{"t":
"resize", "cols", "rows"}`` control JSON, the token on the query string,
4404 / 4401 / 4409 close codes, single attach). The pieces of that protocol
the app already defines — its route's accept-then-close rejection and the
session service's replay ring cap — are imported from their owners rather
than copied. A bounded ring of everything written replays on attach, so a
renderer that mounts after the script started still sees the whole session.

``routes.terminal`` imports unix-only modules, so it is imported lazily —
after ``modules.install()`` and ``import main`` have run.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from typing import Callable, Optional

from fastapi import Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

_sessions: dict[str, "DemoTerminal"] = {}


class DemoTerminal:
    """One scripted PTY. Timelines write to it; the attached renderer's
    keystrokes reach ``on_input``'s callback as bytes."""

    def __init__(self, run_id: str) -> None:
        from sessions.terminal import KEEP_ALIVE_RING_CAP
        self._ring_cap = KEEP_ALIVE_RING_CAP
        self.run_id = run_id
        self.terminal_id = f"demo_{uuid.uuid4().hex[:16]}"
        self.token = secrets.token_urlsafe(24)
        self.ws_path = f"/api/demo/terminals/{self.terminal_id}"
        self.pointer_path: str = ""
        self.cols = 80
        self.rows = 24
        self.attached = False
        self.closed = False
        self._ring = bytearray()
        self._queue: Optional[asyncio.Queue] = None
        self._on_input: Optional[Callable] = None
        self._on_resize: Optional[Callable] = None

    def pointer_session(self) -> dict:
        return {"terminal_id": self.terminal_id, "ws_path": self.ws_path, "token": self.token}

    async def write(self, data) -> None:
        """Append output (str is utf-8 encoded); yields once so an attached
        socket can flush the frame before the timeline continues."""
        if self.closed:
            return
        chunk = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        if not chunk:
            return
        self._ring += chunk
        if len(self._ring) > self._ring_cap:
            del self._ring[: len(self._ring) - self._ring_cap]
        if self._queue is not None:
            self._queue.put_nowait(chunk)
        await asyncio.sleep(0)

    def on_input(self, callback: Callable) -> None:
        """``callback(bytes)`` for every binary frame the renderer sends. A
        returned awaitable is scheduled, not awaited."""
        self._on_input = callback

    def on_resize(self, callback: Callable) -> None:
        """``callback(cols, rows)`` on every resize control frame."""
        self._on_resize = callback

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._queue is not None:
            self._queue.put_nowait(None)
        _sessions.pop(self.terminal_id, None)

    def _deliver_input(self, data: bytes) -> None:
        cb = self._on_input
        if cb is None:
            return
        try:
            result = cb(data)
        except Exception:  # noqa: BLE001 — a timeline bug must not kill the socket
            return
        if asyncio.iscoroutine(result):
            asyncio.ensure_future(result)


def create_session(run_id: str) -> DemoTerminal:
    term = DemoTerminal(run_id)
    _sessions[term.terminal_id] = term
    return term


def close_for_run(run_id: str) -> None:
    for term in list(_sessions.values()):
        if term.run_id == run_id:
            term.close()


async def demo_terminal_ws(websocket: WebSocket, terminal_id: str,
                           token: str = Query(default="")) -> None:
    from terminal_dimensions import clamp_dim
    from websocket_errors import reject_websocket
    term = _sessions.get(terminal_id)
    if term is None or term.closed:
        await reject_websocket(websocket, 4404, "no such session")
        return
    if not token or not secrets.compare_digest(token, term.token):
        await reject_websocket(websocket, 4401, "bad token")
        return
    if term.attached:
        await reject_websocket(websocket, 4409, "session already attached")
        return

    await websocket.accept()
    term.attached = True
    queue: asyncio.Queue = asyncio.Queue()
    term._queue = queue
    if term._ring:
        try:
            await websocket.send_bytes(bytes(term._ring))
        except Exception:  # noqa: BLE001
            term.attached = False
            term._queue = None
            return

    async def pump_to_ws() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            try:
                await websocket.send_bytes(item)
            except Exception:  # noqa: BLE001
                return

    async def pump_from_ws() -> None:
        while True:
            try:
                msg = await websocket.receive()
            except (WebSocketDisconnect, Exception):  # noqa: BLE001
                return
            if msg.get("type") == "websocket.disconnect":
                return
            text = msg.get("text")
            data = msg.get("bytes")
            if text is not None:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("t") == "resize":
                    term.cols = clamp_dim(payload.get("cols"), 80)
                    term.rows = clamp_dim(payload.get("rows"), 24)
                    if term._on_resize is not None:
                        try:
                            term._on_resize(term.cols, term.rows)
                        except Exception:  # noqa: BLE001
                            pass
            elif data is not None:
                term._deliver_input(bytes(data))

    to_task = asyncio.create_task(pump_to_ws())
    from_task = asyncio.create_task(pump_from_ws())
    try:
        _done, pending = await asyncio.wait({to_task, from_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        term.attached = False
        term._queue = None
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass


def register_route(app) -> None:
    app.add_api_websocket_route("/api/demo/terminals/{terminal_id}", demo_terminal_ws)
