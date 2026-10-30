"""WebSocket reverse proxy (routes/proxy.py proxy_ws_to_generator)."""
import asyncio
import uuid
import gc
import logging
import socket
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.server import serve

import routes.proxy as proxy
import run_registry as runs


CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "proxy:primary"))
OTHER_CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "proxy:other"))


@pytest.fixture(autouse=True)
def registered_channels(fresh_cwd, monkeypatch):
    import state
    from models import AppState, Channel
    primary = Channel(id=CHANNEL, name="Primary")
    other = Channel(id=OTHER_CHANNEL, name="Other")
    monkeypatch.setattr(state, "state", AppState(channels=[primary, other], active_channel_id=primary.id))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def port():
    p = _free_port()
    runs.active_managers["run1"] = SimpleNamespace(channel=CHANNEL, bound_ports={p}, backend=None)
    yield p
    runs.active_managers.pop("run1", None)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(proxy.router)
    return TestClient(app)


def _start_upstream(port, handler):
    server = serve(
        handler, "127.0.0.1", port,
        # Pick the SECOND offered subprotocol, so a proxy that accepts the
        # client's first choice before dialing disagrees with the upstream.
        select_subprotocol=lambda conn, offered: offered[-1] if offered else None,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _echo(conn):
    for msg in conn:
        conn.send(msg)


def test_client_gets_the_subprotocol_the_upstream_picked(client, port):
    server = _start_upstream(port, _echo)
    try:
        with client.websocket_connect(
            f"/proxy/{CHANNEL}/run1/{port}/x", subprotocols=["a", "b"],
        ) as ws:
            assert ws.accepted_subprotocol == "b"
            ws.send_text("hi")
            assert ws.receive_text() == "hi"
    finally:
        server.shutdown()


def test_dead_upstream_refuses_the_handshake_and_logs(client, port, caplog):
    caplog.set_level(logging.WARNING, logger=proxy.logger.name)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/proxy/{CHANNEL}/run1/{port}/x"):
            pass
    assert any("upstream dial failed" in r.getMessage() for r in caplog.records)


def test_upstream_close_mid_stream_leaves_no_unretrieved_task_exception(client, port):
    def close_after_first(conn):
        conn.recv()
        conn.close()

    unretrieved = []
    real_handler = asyncio.BaseEventLoop.call_exception_handler

    def spy(loop, context):
        unretrieved.append(context)
        return real_handler(loop, context)

    server = _start_upstream(port, close_after_first)
    try:
        asyncio.BaseEventLoop.call_exception_handler = spy
        with client.websocket_connect(f"/proxy/{CHANNEL}/run1/{port}/x") as ws:
            ws.send_text("first")
            for _ in range(20):
                try:
                    ws.send_text("more")
                except Exception:
                    break
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        gc.collect()
    finally:
        asyncio.BaseEventLoop.call_exception_handler = real_handler
        server.shutdown()
    assert not [c for c in unretrieved if "never retrieved" in str(c.get("message", ""))]


def test_cookie_reaches_upstream_websocket__P0F08(client, port, monkeypatch):
    for key in ("no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    seen = []

    def handler(conn):
        seen.append(conn.request.headers.get("Cookie"))
        conn.send("ready")

    runs.active_managers["run1"].channel = CHANNEL
    server = _start_upstream(port, handler)
    try:
        with client.websocket_connect(
            f"/proxy/{CHANNEL}/run1/{port}/x", headers={"Cookie": "session=app; preference=dark"},
        ) as ws:
            assert ws.receive_text() == "ready"
        assert seen == ["session=app; preference=dark"]
    finally:
        server.shutdown()


def test_wrong_channel_websocket_refuses_before_dial__F0374(client, port, monkeypatch):
    seen = []

    async def dial(*args, **kwargs):
        seen.append(args)
        raise OSError("must not dial")

    runs.active_managers["run1"].channel = CHANNEL
    monkeypatch.setattr(proxy.websockets, "connect", dial)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/proxy/{OTHER_CHANNEL}/run1/{port}/x"):
            pass
    assert exc.value.code == 4410
    assert seen == []
