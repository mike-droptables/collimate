"""WebSocket rejection close codes + SSE broadcast coalescing.

Regression tests for the frozen-UI incident:

1. The chat + terminal WebSocket routes rejected bad/stale/duplicate
   sessions by closing BEFORE accepting the upgrade. Starlette delivers a
   pre-accept close as a plain HTTP 403 handshake rejection, which strips
   the close code — so renderers could never see 4404 ("session gone,
   stop retrying") and re-dialed dead sessions forever. The routes must
   accept, then close with the app code.

2. Every DAG commit shipped a full serialized app state over SSE — twice
   (a "belt" re-broadcast after the core already broadcast). state_snapshot
   is now coalesced per channel and the double broadcast is gone.
"""

from sessions import chat as chat_sessions
from sessions import terminal as terminal_sessions

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.chat_live as cl
import routes.terminal as tm


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(cl.router)
    app.include_router(tm.router)
    return app


def _expect_close(client: TestClient, path: str, code: int):
    """The handshake must be ACCEPTED, then closed with ``code``.

    A pre-accept rejection raises during ``websocket_connect`` (handshake
    403) — exactly the regression this asserts against.
    """
    with client.websocket_connect(path) as ws:
        msg = ws.receive()
    assert msg["type"] == "websocket.close", f"expected close frame, got {msg}"
    assert msg["code"] == code, f"expected close code {code}, got {msg['code']}"


@pytest.fixture(autouse=True)
def _clean_registries():
    chat_sessions._sessions.clear()
    terminal_sessions._sessions.clear()
    yield
    chat_sessions._sessions.clear()
    terminal_sessions._sessions.clear()


CHAT = "_runs/synth-aaaaaaaa/chat/conversation.chat"

# ---------------------------------------------------------------------------
# chat WS
# ---------------------------------------------------------------------------

def test_chat_ws_unknown_session_closes_4404__F0363():
    client = TestClient(_app())
    _expect_close(client, "/api/chats/chat_nope?token=whatever", 4404)


def test_chat_ws_bad_token_closes_4401__F0363():
    info = chat_sessions.register_surface_session("run-1", "cell-1", CHAT, inbox=True, broadcast=False, answer_route="inbox")
    client = TestClient(_app())
    _expect_close(client, f"{info['ws_path']}?token=wrong", 4401)


def test_chat_ws_already_attached_closes_4409__F0363():
    info = chat_sessions.register_surface_session("run-1", "cell-1", CHAT, inbox=True, broadcast=False, answer_route="inbox")
    chat_sessions._sessions[info["chat_id"]].attached = True
    client = TestClient(_app())
    _expect_close(client, f"{info['ws_path']}?token={info['token']}", 4409)


def test_chat_ws_valid_token_attaches_and_routes_commands():
    info = chat_sessions.register_surface_session("run-1", "cell-1", CHAT, inbox=True, broadcast=False, answer_route="inbox")
    client = TestClient(_app())
    with client.websocket_connect(f"{info['ws_path']}?token={info['token']}") as ws:
        ws.send_json({"t": "user_msg", "content": "hi"})
        # The inbox put happens on the app's loop; poll briefly.
        cmd = None
        for _ in range(50):
            cmd = chat_sessions.pop_command("run-1")
            if cmd:
                break
            import time
            time.sleep(0.02)
        # thread_id rides along (empty here — the frame didn't name one);
        # serve_chat treats a non-empty value as the authoritative target.
        # attachments default to [] when the frame carries none.
        assert cmd == {"kind": "user_msg", "content": "hi", "thread_id": "",
                       "attachments": []}
    # Detach on close so the next view can attach.
    for _ in range(50):
        if not chat_sessions._sessions[info["chat_id"]].attached:
            break
        import time
        time.sleep(0.02)
    assert not chat_sessions._sessions[info["chat_id"]].attached


def test_chat_ws_user_msg_carries_attachments():
    """A user_msg frame's file attachments (cell-relative refs) ride into the
    turn command, normalized; malformed entries are dropped."""
    info = chat_sessions.register_surface_session("run-att", "cell-att", CHAT, inbox=True, broadcast=False, answer_route="inbox")
    client = TestClient(_app())
    with client.websocket_connect(f"{info['ws_path']}?token={info['token']}") as ws:
        ws.send_json({"t": "user_msg", "content": "make this happier", "attachments": [
            {"path": "attachments/photo.png", "name": "photo.png", "media_type": "image/png"},
            "nope", {"no": "path"},
        ]})
        cmd = None
        for _ in range(50):
            cmd = chat_sessions.pop_command("run-att")
            if cmd:
                break
            import time
            time.sleep(0.02)
        assert cmd["kind"] == "user_msg"
        assert cmd["attachments"] == [
            {"path": "attachments/photo.png", "name": "photo.png", "media_type": "image/png"}]


# ---------------------------------------------------------------------------
# terminal WS (rejection paths only — a valid attach would spawn docker)
# ---------------------------------------------------------------------------

def test_terminal_ws_unknown_session_closes_4404__F0363():
    client = TestClient(_app())
    _expect_close(client, "/api/terminals/tmnl_nope?token=whatever", 4404)


def test_terminal_ws_bad_token_closes_4401__F0363():
    info = terminal_sessions.register_session(
        run_id="run-1", cell_id="cell-1", container_id="fake", command=["bash"])
    client = TestClient(_app())
    _expect_close(client, f"{info['ws_path']}?token=wrong", 4401)


def test_terminal_ws_already_attached_closes_4409__F0363():
    info = terminal_sessions.register_session(
        run_id="run-1", cell_id="cell-1", container_id="fake", command=["bash"])
    terminal_sessions._sessions[info["terminal_id"]].attached = True
    client = TestClient(_app())
    _expect_close(client, f"{info['ws_path']}?token={info['token']}", 4409)


@pytest.mark.parametrize("route,path", [
    (cl, "/api/chats/chat_nope?token=whatever"),
    (tm, "/api/terminals/tmnl_nope?token=whatever"),
], ids=["chat", "terminal"])
def test_routes_use_shared_rejection_owner__F0363(monkeypatch, route, path):
    from websocket_errors import reject_websocket

    assert route.reject_websocket is reject_websocket
    assert "_reject" not in vars(route)
    calls = []

    async def observed(websocket, code, reason):
        calls.append((code, reason))
        await reject_websocket(websocket, code, reason)

    monkeypatch.setattr(route, "reject_websocket", observed)
    _expect_close(TestClient(_app()), path, 4404)
    assert calls == [(4404, "no such session")]


@pytest.mark.parametrize("route", [cl, tm], ids=["chat", "terminal"])
@pytest.mark.parametrize("code,reason", [
    (4404, "no such session"),
    (4401, "bad token"),
    (4409, "session already attached"),
])
async def test_rejection_accepts_before_exact_close__F0363(route, code, reason):
    calls = []

    class Socket:
        async def accept(self):
            calls.append(("accept",))

        async def close(self, *, code, reason):
            calls.append(("close", code, reason))

    await route.reject_websocket(Socket(), code, reason)
    assert calls == [("accept",), ("close", code, reason)]


# ---------------------------------------------------------------------------
# SSE coalescing
# ---------------------------------------------------------------------------

async def test_broadcast_coalesced_absorbs_burst():
    import events

    events._main_loop = None
    events._subscribers.clear()
    events._pending_coalesced.clear()
    events.capture_loop()
    channel = str(uuid.uuid4())
    q = events.subscribe(channel)

    builds = []

    def build(channels):
        builds.append(1)
        return {channel: {"channel_scope": channel, "seq": len(builds)} for channel in channels}

    for _ in range(10):
        events.broadcast_coalesced(build, delay=0.05)
    await asyncio.sleep(0.3)

    items = []
    while not q.empty():
        items.append(q.get_nowait())
    events.unsubscribe(channel, q)

    assert len(builds) == 1, f"burst should build once, built {len(builds)}x"
    assert len(items) == 1, f"burst should broadcast once, got {len(items)}"


async def test_broadcast_coalesced_skips_without_subscribers():
    import events

    events._main_loop = None
    events._subscribers.clear()
    events._pending_coalesced.clear()
    events.capture_loop()

    builds = []
    events.broadcast_coalesced(lambda _channels: builds.append(1), delay=0.02)
    await asyncio.sleep(0.1)
    assert not builds, "no subscribers → no build"


async def test_broadcast_coalesced_none_result_skips_broadcast():
    import events

    events._main_loop = None
    events._subscribers.clear()
    events._pending_coalesced.clear()
    events.capture_loop()
    channel = str(uuid.uuid4())
    q = events.subscribe(channel)

    events.broadcast_coalesced(lambda _channels: None, delay=0.02)
    await asyncio.sleep(0.15)
    assert q.empty()
    events.unsubscribe(channel, q)


# ---------------------------------------------------------------------------
# update_cell → exactly one dag_commit (the "belt" used to re-broadcast
# after _emit_partial had already broadcast, doubling SSE traffic on
# every successful commit) and exactly one coalesced state_snapshot.
# ---------------------------------------------------------------------------

async def test_update_cell_broadcasts_dag_commit_once(fresh_cwd, monkeypatch):
    import json
    import uuid

    import events
    from database import get_db_connection
    from models import Cell, Channel, Stack
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED
    from state import state as app_state

    events._main_loop = None
    events._subscribers.clear()
    events._pending_coalesced.clear()
    events.capture_loop()

    ch = Channel(name="Test Single Bcast")
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(ch)
    monkeypatch.setattr(app_state, "channels", [ch])
    monkeypatch.setattr(app_state, "active_channel_id", ch.id)
    channel_name = str(ch.id)
    run_id = str(uuid.uuid4())
    placeholder_id = str(uuid.uuid4())

    conn = get_db_connection(channel_name)
    conn.execute(
        "INSERT INTO runs (id, prompt, status) VALUES (?, '', 'running')",
        (run_id,),
    )
    conn.commit()
    conn.close()

    stk = Stack(cells=[Cell(id=placeholder_id, name="Generating...")])
    ch.main_stacks.append(stk)

    ws = fresh_cwd / "ws"
    app_dir = fresh_cwd / "app"
    ws.mkdir()
    app_dir.mkdir()
    (ws / "out.md").write_text("hello")

    manager = ProcessManager(
        channel=channel_name,
        run_id=run_id,
        app_dir=str(app_dir),
        workspace_dir=str(ws),
        entrypoint="gen.py",
        generator_name="synthetic",
        cell_id=placeholder_id,
        permissions=UNRESTRICTED,
    )

    q = events.subscribe(channel_name)
    try:
        await manager._handle_ipc(json.dumps({"req_id": "test-request",
            "op": "update_cell",
            "operation_id": str(uuid.uuid4()), "writer_id": str(uuid.uuid4()),
            "edit_seq": 1, "base_revisions": {"out.md": 0},
            "artifacts": [{
                "type": "workspace",
                "cell_path": "out.md",
                "local_path": "out.md",
            }],
            "metadata": {"name": "Out"},
        }))
        # Wait out the state_snapshot coalescing window (+ executor hop).
        await asyncio.sleep(0.5)

        drained = []
        while not q.empty():
            drained.append(q.get_nowait())

        dag_commits = [e for e in drained if e.get("type") == "dag_commit"]
        state_snapshots = [e for e in drained if e.get("type") == "state_snapshot"]
        assert len(dag_commits) == 1, (
            f"one commit must broadcast exactly one dag_commit, got "
            f"{len(dag_commits)}"
        )
        assert len(state_snapshots) == 1, (
            f"one commit burst must coalesce to one state_snapshot, got "
            f"{len(state_snapshots)}"
        )
    finally:
        events.unsubscribe(channel_name, q)
