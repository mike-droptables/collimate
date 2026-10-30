"""Unit tests for the live session registry (routes/chat_live.py).

Covers the pieces the IPC ops + WebSocket route lean on: session
registration per (run, path) with one inbox per run, generator→renderer
event routing to the run's inbox (with in-flight-turn text tracking for
reconnect replay), renderer→generator command queueing onto the run's
inbox, and run-cleanup teardown — all by RUN, so two runs on one cell never
touch each other's sessions. Synthetic run folders throughout.
"""

from sessions import chat as chat_sessions

import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketState

import routes.chat_live as cl


@pytest.fixture(autouse=True)
def _clean_registry():
    chat_sessions._sessions.clear()
    yield
    chat_sessions._sessions.clear()


CHAT = "_runs/synth-aaaaaaaa/chat/conversation.chat"
INBOX = "_runs/synth-aaaaaaaa/inbox.chat"
PANEL = "_runs/synth-aaaaaaaa/panel/status.panel"


def _chat(run_id: str, cell_id: str, surface: str = CHAT) -> dict:
    """The run's chat inbox, as serve_chat registers it."""
    return chat_sessions.register_surface_session(run_id, cell_id, surface, inbox=True, broadcast=False, answer_route="inbox")


def _broadcast(run_id: str, cell_id: str, surface: str = INBOX) -> dict:
    """The run's transport-only broadcast inbox, as serve_chat(files=False,
    broadcast=True) registers it."""
    return chat_sessions.register_surface_session(run_id, cell_id, surface, inbox=True, broadcast=True, answer_route="inbox")


def _panel(run_id: str, cell_id: str, surface: str = PANEL) -> dict:
    """A broadcast session that is not the run's inbox but routes its
    answers there."""
    return chat_sessions.register_surface_session(run_id, cell_id, surface, broadcast=True, answer_route="inbox")


def _session(info: dict) -> chat_sessions.ChatSession:
    return chat_sessions._sessions[info["chat_id"]]


def _live_sessions(run_id: str) -> list:
    return [s for s in chat_sessions._sessions.values() if s.run_id == run_id and not s.closed]


def _app() -> FastAPI:
    """Minimal app exposing just the chat WS route — the reconnect replay
    path is integration-shaped, so we drive it through a real socket (mirrors
    test_pytest_ws_close_codes._app)."""
    app = FastAPI()
    app.include_router(cl.router)
    return app


def test_register_returns_pointer_shape():
    info = _chat("run-1", "cell-1")
    assert info["chat_id"].startswith("chat_")
    assert info["ws_path"] == f"/api/chats/{info['chat_id']}"
    assert info["token"] and len(info["token"]) >= 16
    sess = chat_sessions.get_inbox("run-1")
    assert sess is not None and sess.run_id == "run-1"


def test_surface_policy_is_declared_without_suffix_rules__P1_SDK6():
    custom = chat_sessions.register_surface_session("run-1", "cell", "_runs/r/views/custom.data",
                                         broadcast=True, answer_route="")
    same = chat_sessions.register_surface_session("run-1", "cell", "_runs/r/views/custom.data",
                                       broadcast=True, answer_route="")
    assert same == custom
    changed = chat_sessions.register_surface_session("run-1", "cell", "_runs/r/views/custom.data",
                                          broadcast=True, answer_route="inbox")
    assert changed != custom
    assert chat_sessions._sessions[changed["chat_id"]].answer_route == "inbox"
    single = chat_sessions.register_surface_session("run-1", "cell", "_runs/r/status.panel",
                                         broadcast=False, answer_route="")
    assert not chat_sessions._sessions[single["chat_id"]].broadcast


@pytest.mark.parametrize("answer_route", ["", "inbox"])
@pytest.mark.parametrize("inbox_surface", [False, True])
async def test_surface_answers_follow_the_declared_route__P1_SDK6(answer_route, inbox_surface):
    chat_sessions.register_surface_session("run-1", "cell", "_runs/r/inbox.data", inbox=True,
                                broadcast=False, answer_route=answer_route if inbox_surface else "inbox")
    if inbox_surface:
        session = chat_sessions.get_inbox("run-1")
    else:
        info = chat_sessions.register_surface_session("run-1", "cell", "_runs/r/answers.data",
                                           broadcast=True, answer_route=answer_route)
        session = chat_sessions._sessions[info["chat_id"]]
    chat_sessions.register_surface_session("other-run", "cell", "_runs/other/inbox.data", inbox=True,
                                broadcast=False, answer_route="inbox")
    ws = _FakeWS()
    for frame in ({"t": "permission_answer", "id": "req", "option_id": "yes"},
                  {"t": "config_set", "config_id": "choice", "value": "one"},
                  {"t": "interrupt", "turn_id": "turn"}):
        ws._inbound.put_nowait({"type": "websocket.receive", "text": json.dumps(frame)})
    ws.disconnect()
    await asyncio.wait_for(cl.chat_ws(ws, session.chat_id, session.token), 2)
    received = []
    while command := chat_sessions.pop_command("run-1"):
        received.append(command["kind"])
    assert received == (["permission_answer", "config_set", "interrupt"] if answer_route else [])
    assert chat_sessions.pop_command("other-run") is None


@pytest.mark.parametrize("broadcast,answer_route,inbox", [(None, "", False), ("yes", "", False),
                                                         (False, None, False), (False, "other", False),
                                                         (False, "", "yes")])
def test_malformed_surface_declarations_are_refused__P1_SDK6(broadcast, answer_route, inbox):
    with pytest.raises(ValueError, match="declaration"):
        chat_sessions.register_surface_session("run", "cell", "_runs/r/custom", broadcast=broadcast,
                                    answer_route=answer_route, inbox=inbox)
    assert not chat_sessions._sessions


def test_two_runs_on_one_cell_keep_their_own_inboxes__F0139():
    """Several runs may be live on a cell: each registers its own inbox and
    neither registration evicts the other's session."""
    first = _chat("run-1", "cell-1")
    second = _chat("run-2", "cell-1", "_runs/synth-bbbbbbbb/chat/conversation.chat")
    assert first["chat_id"] != second["chat_id"]
    assert first["chat_id"] in chat_sessions._sessions and second["chat_id"] in chat_sessions._sessions
    assert chat_sessions.get_inbox("run-1").chat_id == first["chat_id"]
    assert chat_sessions.get_inbox("run-2").chat_id == second["chat_id"]


def test_a_run_has_at_most_one_inbox():
    """A second inbox at another path is refused; re-registering the same
    inbox surface replaces only its predecessor."""
    first = _chat("run-1", "cell-1")
    with pytest.raises(chat_sessions.InboxExists):
        _chat("run-1", "cell-1", "_runs/synth-aaaaaaaa/chat/other.chat")
    again = _chat("run-1", "cell-1")
    assert again["chat_id"] != first["chat_id"]
    assert first["chat_id"] not in chat_sessions._sessions
    assert chat_sessions.get_inbox("run-1").chat_id == again["chat_id"]


def test_push_event_tracks_in_progress_text_for_reconnect():
    _chat("run-1", "cell-1")
    sess = chat_sessions.get_inbox("run-1")

    chat_sessions.push_event("run-1", "turn-1", {"type": "turn_start"})
    chat_sessions.push_event("run-1", "turn-1", {"type": "text_delta", "text": "Hel"})
    chat_sessions.push_event("run-1", "turn-1", {"type": "text_delta", "text": "lo"})
    assert sess.in_progress_text == "Hello"
    assert sess.in_progress_turn_id == "turn-1"

    chat_sessions.push_event("run-1", "turn-1", {"type": "turn_end"})
    assert sess.in_progress_text == ""
    assert sess.in_progress_turn_id == ""


def test_push_event_tracks_in_progress_tools_for_reconnect():
    _chat("run-1", "cell-1")
    sess = chat_sessions.get_inbox("run-1")

    chat_sessions.push_event("run-1", "turn-1", {"type": "turn_start"})
    # A tool's running event and its completion carry the same id "a" — they
    # collapse into ONE entry: the completion's status/result patch the entry
    # while its blank name leaves the running name intact, and the running
    # input survives.
    chat_sessions.push_event("run-1", "turn-1", {
        "type": "tool", "id": "a", "name": "Read",
        "status": "running", "input": "path=x",
    })
    chat_sessions.push_event("run-1", "turn-1", {
        "type": "tool", "id": "a", "name": "", "status": "done",
        "result": "42 lines",
    })
    assert len(sess.in_progress_tools) == 1
    entry = sess.in_progress_tools[0]
    assert entry["status"] == "done"
    assert entry["name"] == "Read"  # blank completion name didn't clobber it
    assert entry["input"] == "path=x"
    assert entry["result"] == "42 lines"

    # A no-id tool event can't pair with anything, so it appends.
    chat_sessions.push_event("run-1", "turn-1", {
        "type": "tool", "name": "Bash", "status": "running",
    })
    assert len(sess.in_progress_tools) == 2

    # turn_end clears the tool list alongside the text/thinking.
    chat_sessions.push_event("run-1", "turn-1", {"type": "turn_end"})
    assert sess.in_progress_tools == []


def test_push_event_replay_keeps_the_acp_typed_tool_extras():
    """The ACP-typed tool extras — kind, locations, structured diff/terminal
    content — must survive into the reconnect-replay record (agent-v2: a
    reload must not strip the kind-typed display back to a generic wrench).
    They land on the initial running event AND patch in from a completion."""
    _chat("run-1", "cell-x")
    sess = chat_sessions.get_inbox("run-1")

    chat_sessions.push_event("run-1", "turn-1", {
        "type": "tool", "id": "t1", "name": "Edit", "status": "running",
        "kind": "edit", "locations": [{"path": "notes/a.md", "line": 3}],
    })
    entry = sess.in_progress_tools[0]
    assert entry["kind"] == "edit"
    assert entry["locations"] == [{"path": "notes/a.md", "line": 3}]

    # The completion carries the diff content the running event didn't have.
    chat_sessions.push_event("run-1", "turn-1", {
        "type": "tool", "id": "t1", "status": "done",
        "content": [{"type": "diff", "path": "notes/a.md",
                     "oldText": "x", "newText": "y"}],
    })
    assert len(sess.in_progress_tools) == 1
    entry = sess.in_progress_tools[0]
    assert entry["status"] == "done"
    assert entry["content"] == [{"type": "diff", "path": "notes/a.md",
                                 "oldText": "x", "newText": "y"}]
    chat_sessions.unregister_sessions_for_run("run-1")


def test_to_frame_maps_tool_and_usage_events():
    # `tool` gets its own frame type carrying the turn id; every payload key
    # (id/name/status/input/result/thread_id) rides through unchanged.
    tool = cl._to_frame("turn-1", {
        "type": "tool", "id": "a", "name": "Read", "status": "done",
        "input": "path=x", "result": "42 lines", "thread_id": "th-1",
    })
    assert tool == {
        "t": "tool", "turn_id": "turn-1", "id": "a", "name": "Read",
        "status": "done", "input": "path=x", "result": "42 lines",
        "thread_id": "th-1",
    }

    # `usage` is unknown to _to_frame, so it takes the generic passthrough:
    # its own type name as `t`, the turn id, and the numbers preserved.
    usage = cl._to_frame("turn-1", {
        "type": "usage", "input_tokens": 10, "output_tokens": 5,
        "total_tokens": 15, "total_cost_usd": 0.0003, "thread_id": "th-1",
    })
    assert usage == {
        "t": "usage", "turn_id": "turn-1", "input_tokens": 10,
        "output_tokens": 5, "total_tokens": 15, "total_cost_usd": 0.0003,
        "thread_id": "th-1",
    }


def test_chat_ws_history_replay_includes_tools():
    """A renderer reattaching mid-turn gets the accumulated tool state in the
    replayed `history` frame, alongside the partial text/thinking."""
    info = _chat("run-1", "cell-1")
    # Put a turn in flight (detached — nothing buffers on the outbox; the
    # partial replays from the in_progress_* slots on reattach).
    chat_sessions.push_event("run-1", "turn-1", {"type": "turn_start", "thread_id": "th-1"})
    chat_sessions.push_event("run-1", "turn-1", {"type": "text_delta", "text": "Hi"})
    chat_sessions.push_event("run-1", "turn-1", {
        "type": "tool", "id": "a", "name": "Read",
        "status": "running", "input": "path=x",
    })

    client = TestClient(_app())
    with client.websocket_connect(f"{info['ws_path']}?token={info['token']}") as ws:
        frame = ws.receive_json()
    assert frame["t"] == "history"
    assert frame["turn_id"] == "turn-1"
    assert frame["text"] == "Hi"
    assert frame["tools"] == [
        {"id": "a", "name": "Read", "status": "running", "input": "path=x"},
    ]


def test_push_event_only_buffers_when_attached():
    _chat("run-1", "cell-1")
    sess = chat_sessions.get_inbox("run-1")

    # Detached: deltas update the partial but don't pile on the outbox
    # (a reconnect replays in_progress_text instead).
    chat_sessions.push_event("run-1", "t", {"type": "text_delta", "text": "x"})
    assert sess.outbox.qsize() == 0

    sess.attached = True
    chat_sessions.push_event("run-1", "t", {"type": "text_delta", "text": "y"})
    assert sess.outbox.qsize() == 1
    item = sess.outbox.get_nowait()
    assert item["event"]["text"] == "y"


def test_pop_command_drains_inbox():
    _chat("run-1", "cell-1")
    sess = chat_sessions.get_inbox("run-1")
    assert chat_sessions.pop_command("run-1") is None

    sess.commands.put_nowait({"kind": "user_msg", "content": "hi"})
    sess.commands.put_nowait({"kind": "interrupt"})
    assert chat_sessions.pop_command("run-1") == {"kind": "user_msg", "content": "hi"}
    assert chat_sessions.pop_command("run-1") == {"kind": "interrupt"}
    assert chat_sessions.pop_command("run-1") is None


def test_unregister_for_run_closes_session():
    _chat("run-1", "cell-1")
    sess = chat_sessions.get_inbox("run-1")
    chat_sessions.unregister_sessions_for_run("run-1")
    assert sess.closed is True
    assert chat_sessions.get_inbox("run-1") is None
    # A push after teardown is a no-op (no such session).
    chat_sessions.push_event("run-1", "t", {"type": "text_delta", "text": "z"})


def test_push_and_pop_unknown_cell_are_safe():
    assert chat_sessions.pop_command("nope") is None
    chat_sessions.push_event("run-1", "t", {"type": "text_delta", "text": "z"})  # no raise


# ---------------------------------------------------------------------------
# Session registry — one session per (run, path), the inbox as the only push
# target, broadcast buffering, pending-request replay state.
# ---------------------------------------------------------------------------


def test_surface_register_is_one_per_run_and_surface():
    """Registering a path evicts only ITS OWN predecessor — a second
    session's registration never kills the chat socket, and another run's
    registration never touches this run's."""
    chat = _chat("run-1", "cell-s")
    panel = _panel("run-1", "cell-s")
    assert chat["chat_id"] in chat_sessions._sessions      # chat survived
    assert panel["chat_id"] in chat_sessions._sessions
    assert chat_sessions.get_inbox("run-1").chat_id == chat["chat_id"]

    # The SAME run re-registering a broadcast session keeps its live session.
    same = _panel("run-1", "cell-s")
    assert same == panel
    # Another run on the cell registers its own; this run's survives.
    other = _panel("run-2", "cell-s", "_runs/synth-bbbbbbbb/panel/status.panel")
    assert panel["chat_id"] in chat_sessions._sessions and other["chat_id"] in chat_sessions._sessions
    # The chat inbox (single-attach) always re-mints.
    chat2 = _chat("run-1", "cell-s")
    assert chat2["chat_id"] != chat["chat_id"]
    assert chat["chat_id"] not in chat_sessions._sessions

    assert _session(panel).broadcast is True
    assert chat_sessions.get_inbox("run-1").broadcast is False
    chat_sessions.unregister_sessions_for_run("run-1")


def test_a_broadcast_inbox_re_registered_keeps_its_session():
    first = _broadcast("run-1", "cell-r")
    assert _broadcast("run-1", "cell-r") == first
    assert chat_sessions.get_inbox("run-1").broadcast is True
    chat_sessions.unregister_sessions_for_run("run-1")


def test_push_event_reaches_only_the_runs_inbox():
    """A stream event goes to THE RUN's inbox: neither another session of
    the run nor a sibling run's inbox on the same cell gets it."""
    _chat("run-1", "cell-f")
    panel = _session(_panel("run-1", "cell-f"))
    chat = chat_sessions.get_inbox("run-1")
    chat.attached = True
    sibling = _session(_broadcast("run-2", "cell-f", "_runs/synth-bbbbbbbb/inbox.chat"))

    chat_sessions.push_event("run-1", "t1", {"type": "text_delta", "text": "a"})
    chat_sessions.push_event("run-1", "t1", {"type": "plan", "entries": []})
    assert chat.outbox.qsize() == 2
    assert len(panel.backlog) == 0 and len(sibling.backlog) == 0
    chat_sessions.unregister_sessions_for_run("run-1")
    chat_sessions.unregister_sessions_for_run("run-2")


def test_broadcast_backlog_keeps_only_the_newest_events():
    """A broadcast inbox's detached backlog is bounded: past the cap the
    oldest events fall off and the newest stay, in order."""
    sess = _session(_broadcast("run-1", "cell-bl"))
    for i in range(chat_sessions.OUTBOX_MAX + 5):
        chat_sessions.push_event("run-1", "t1", {"type": "context", "used": i})
    assert len(sess.backlog) == chat_sessions.OUTBOX_MAX
    assert sess.backlog[0]["event"]["used"] == 5
    assert sess.backlog[-1]["event"]["used"] == chat_sessions.OUTBOX_MAX + 4
    chat_sessions.unregister_sessions_for_run("run-1")


def test_broadcast_inbox_buffers_detached_and_multi_attaches():
    """A broadcast inbox buffers events with NO viewer attached (bounded
    backlog), replays the backlog to each attaching socket, and takes a
    second socket instead of 4409."""
    info = _broadcast("run-1", "cell-b")
    sess = _session(info)

    # Detached: events land in the backlog (unlike the chat inbox).
    chat_sessions.push_event("run-1", "t1", {"type": "plan", "entries": [{"content": "x"}]})
    assert len(sess.backlog) == 1

    client = TestClient(_app())
    url = f"{info['ws_path']}?token={info['token']}"
    with client.websocket_connect(url) as ws1:
        frame = ws1.receive_json()          # the backlog replay
        assert frame["t"] == "plan"
        with client.websocket_connect(url) as ws2:   # no 4409
            frame2 = ws2.receive_json()
            assert frame2["t"] == "plan"    # same backlog replay
            assert len(sess.subscribers) == 2
            # A live push reaches BOTH sockets.
            chat_sessions.push_event("run-1", "t2", {"type": "context", "used": 5})
            assert ws1.receive_json()["t"] == "context"
            assert ws2.receive_json()["t"] == "context"
    chat_sessions.unregister_sessions_for_run("run-1")


def test_pending_requests_track_and_replay():
    """A permission_request event joins the session's pending set until its
    resolution event clears it, and an attaching socket receives the pending
    set in the history frame — the generator is BLOCKED on the answer, so a
    reload must not lose the card."""
    info = _chat("run-1", "cell-p")
    sess = chat_sessions.get_inbox("run-1")
    chat_sessions.push_event("run-1", "t1", {
        "type": "permission_request", "id": "req-1",
        "title": "Run ls?", "kind": "execute",
        "options": [{"option_id": "y", "name": "Allow", "kind": "allow_once"}],
    })
    assert [r["id"] for r in sess.pending_requests] == ["req-1"]

    client = TestClient(_app())
    with client.websocket_connect(
            f"{info['ws_path']}?token={info['token']}") as ws:
        frame = ws.receive_json()
    assert frame["t"] == "history"
    assert [r["id"] for r in frame["pending_requests"]] == ["req-1"]

    chat_sessions.push_event("run-1", "t1", {"type": "permission_resolved", "id": "req-1",
                                   "option_id": "y"})
    assert sess.pending_requests == []
    chat_sessions.unregister_sessions_for_run("run-1")


def test_unregister_for_run_drops_every_session_of_that_run_only():
    _chat("run-1", "cell-u")
    _panel("run-1", "cell-u")
    _chat("run-2", "cell-u", "_runs/synth-bbbbbbbb/chat/conversation.chat")
    assert len(_live_sessions("run-1")) == 2
    chat_sessions.unregister_sessions_for_run("run-1")
    assert _live_sessions("run-1") == []
    assert len(_live_sessions("run-2")) == 1


def test_ws_inbound_permission_answer_frame_reaches_the_generator():
    """An approval card's answer (gen.ask) — the chosen ``option_id`` or
    ``cancelled: true``, never both — lands on the chat session's inbox as a
    command of the same shape for serve_chat's dispatch."""
    info = _chat("run-1", "cell-pa")
    client = TestClient(_app())
    with client.websocket_connect(
            f"{info['ws_path']}?token={info['token']}") as ws:
        ws.send_json({"t": "permission_answer", "id": "req-1",
                      "option_id": "allow-1"})
        ws.send_json({"t": "permission_answer", "id": "req-2",
                      "cancelled": True})
        # A round-trip nudge so the server processed the frames.
        ws.send_json({"t": "not-a-real-frame"})
    assert chat_sessions.pop_command("run-1") == {
        "kind": "permission_answer", "id": "req-1", "option_id": "allow-1"}
    assert chat_sessions.pop_command("run-1") == {
        "kind": "permission_answer", "id": "req-2", "cancelled": True}
    chat_sessions.unregister_sessions_for_run("run-1")


@pytest.mark.parametrize("frame", [
    {"id": "req-1", "option_id": "y", "cancelled": False},
    {"id": "req-1", "option_id": "", "cancelled": True},
    {"id": "req-1", "optionId": "y"},
    {"id": "req-1", "option_id": ""},
    {"id": "req-1"},
    {"id": "req-1", "cancelled": "true"},
    {"id": "", "option_id": "y"},
    {"option_id": "y"},
], ids=["cancelled-false", "cancelled-with-option", "camel-case", "empty-option",
        "no-answer", "cancelled-not-bool", "empty-id", "no-id"])
def test_a_malformed_permission_answer_never_reaches_the_generator(frame):
    """Only the wire's two shapes are answers. Anything else is a renderer
    bug, dropped rather than read as a refusal, so the card stays open."""
    info = _chat("run-1", "cell-bad")
    client = TestClient(_app())
    with client.websocket_connect(
            f"{info['ws_path']}?token={info['token']}") as ws:
        ws.send_json({"t": "permission_answer", **frame})
        # A frame that lands on the same inbox, sent after the bad one: once
        # it arrives, the bad one was already handled.
        ws.send_json({"t": "permission_answer", "id": "req-ok", "option_id": "y"})
        inbox = chat_sessions.get_inbox("run-1").commands
        for _ in range(200):
            if not inbox.empty():
                break
            time.sleep(0.01)
    assert chat_sessions.pop_command("run-1") == {
        "kind": "permission_answer", "id": "req-ok", "option_id": "y"}
    assert chat_sessions.pop_command("run-1") is None
    chat_sessions.unregister_sessions_for_run("run-1")


def test_config_options_track_and_replay():
    """A config_option event wholesale-replaces the session's retained
    option set (agent-v2 §2.1's Phase-4 configOptions UI), a current_mode
    event patches the mode select's current value, and an attaching socket
    receives the set in the history frame — the switcher must not blank on
    a reload."""
    info = _chat("run-1", "cell-co")
    sess = chat_sessions.get_inbox("run-1")
    chat_sessions.push_event("run-1", "", {
        "type": "config_option", "update": {"configOptions": [
            {"id": "mode", "category": "mode", "currentValue": "auto",
             "options": [{"value": "auto"}, {"value": "yolo"}]},
            {"id": "model", "category": "model", "currentValue": "m1",
             "options": [{"value": "m1"}]},
        ]},
    })
    assert [o["id"] for o in sess.config_options] == ["mode", "model"]

    chat_sessions.push_event("run-1", "", {"type": "current_mode", "modeId": "yolo"})
    assert sess.config_options[0]["currentValue"] == "yolo"
    assert sess.config_options[1]["currentValue"] == "m1"

    client = TestClient(_app())
    with client.websocket_connect(
            f"{info['ws_path']}?token={info['token']}") as ws:
        frame = ws.receive_json()
    assert frame["t"] == "history"
    assert [o["id"] for o in frame["config_options"]] == ["mode", "model"]

    # The next full set REPLACES (an option the agent withdraws disappears).
    chat_sessions.push_event("run-1", "", {
        "type": "config_option",
        "update": {"configOptions": [{"id": "model", "category": "model"}]},
    })
    assert [o["id"] for o in sess.config_options] == ["model"]
    chat_sessions.unregister_sessions_for_run("run-1")


def test_ws_inbound_config_set_frame_reaches_the_generator():
    """The config_set frame (the configOptions UI's mid-session change)
    joins the inbound allowlist and lands on the session inbox as a typed
    command for serve_chat's dispatch."""
    info = _chat("run-1", "cell-cs")
    client = TestClient(_app())
    with client.websocket_connect(
            f"{info['ws_path']}?token={info['token']}") as ws:
        ws.send_json({"t": "config_set", "config_id": "model",
                      "value": "claude-haiku-4-5"})
        ws.send_json({"t": "not-a-real-frame"})
    assert chat_sessions.pop_command("run-1") == {
        "kind": "config_set", "config_id": "model",
        "value": "claude-haiku-4-5"}
    chat_sessions.unregister_sessions_for_run("run-1")


def test_permission_answer_on_the_broadcast_inbox_reaches_the_generator():
    """An approval card answered from a status-pane drilldown attached to
    the run's broadcast inbox lands on that inbox's queue, which the
    generator drains (chat_poll → pop_command)."""
    inbox = _broadcast("run-1", "cell-bi")
    client = TestClient(_app())
    with client.websocket_connect(
            f"{inbox['ws_path']}?token={inbox['token']}") as ws:
        ws.send_json({"t": "permission_answer", "id": "req-9",
                      "option_id": "allow-1"})
        ws.send_json({"t": "not-a-real-frame"})
    assert chat_sessions.pop_command("run-1") == {
        "kind": "permission_answer", "id": "req-9", "option_id": "allow-1"}
    chat_sessions.unregister_sessions_for_run("run-1")


def test_permission_answer_from_another_session_reaches_the_runs_inbox():
    """A card answered on a session that routes its answers to the inbox is
    delivered to the run's INBOX — the generator drains only that one — or
    the waiting ask loop never sees the answer."""
    _chat("run-1", "cell-ts")
    panel = _panel("run-1", "cell-ts")
    client = TestClient(_app())
    with client.websocket_connect(
            f"{panel['ws_path']}?token={panel['token']}") as ws:
        ws.send_json({"t": "permission_answer", "id": "req-9",
                      "option_id": "allow-1"})
        ws.send_json({"t": "not-a-real-frame"})
    # It landed on the run's inbox, not the panel session's queue.
    assert chat_sessions.pop_command("run-1") == {
        "kind": "permission_answer", "id": "req-9", "option_id": "allow-1"}
    assert _session(panel).commands.empty()
    chat_sessions.unregister_sessions_for_run("run-1")


@pytest.mark.parametrize("op", ["edit", "delete", "insert", "move", "regenerate"])
def test_transcript_mutations_are_refused_without_reaching_the_harness(op):
    info = _chat("run-1", "cell-1")
    with TestClient(_app()) as client, client.websocket_connect(
            f"{info['ws_path']}?token={info['token']}") as ws:
        ws.send_json({"t": op, "thread_id": "main", "index": 0,
                      "to": 1, "content": "rewritten", "role": "user"})
        assert ws.receive_json() == {
            "t": "refused", "op": op, "thread_id": "main",
            "message": "Unsupported chat command.",
        }
        assert chat_sessions.pop_command("run-1") is None
        # A refusal leaves the connection usable for a normal new turn.
        ws.send_json({"t": "user_msg", "thread_id": "main", "content": "next"})
        ws.send_json({"t": "unsupported"})
        assert ws.receive_json()["t"] == "refused"
        assert chat_sessions.pop_command("run-1") == {
            "kind": "user_msg", "thread_id": "main", "content": "next",
            "attachments": [],
        }
        assert chat_sessions.pop_command("run-1") is None


def test_permission_answer_without_an_inbox_is_dropped():
    """gen.ask shows a card only while serve_chat serves the run's inbox, so
    a run with no inbox has no card waiting: an answer sent from another
    session is dropped, never parked on that session's queue — nor handed
    to another run's inbox on the cell."""
    panel = _panel("run-1", "cell-nc")
    _chat("run-2", "cell-nc", "_runs/synth-bbbbbbbb/chat/conversation.chat")
    client = TestClient(_app())
    with client.websocket_connect(
            f"{panel['ws_path']}?token={panel['token']}") as ws:
        ws.send_json({"t": "permission_answer", "id": "req-1", "cancelled": True})
        ws.send_json({"t": "interrupt", "turn_id": "turn-1"})
        ws.send_json({"t": "delete", "index": 0})
    # The pump handles the frames in order before the disconnect, and its
    # finally then drops the subscriber: once it is gone, all were handled.
    sess = _session(panel)
    for _ in range(200):
        if not sess.subscribers:
            break
        time.sleep(0.01)
    assert not sess.subscribers
    # Nothing parked on the panel session's queue (nothing would drain it).
    assert sess.commands.empty()
    assert chat_sessions.pop_command("run-2") is None
    chat_sessions.unregister_sessions_for_run("run-1")


def test_interrupt_from_any_session_reaches_its_runs_inbox_with_its_turn_id():
    """The generator reads commands only from its run's inbox — so an
    interrupt from another of the run's sessions is delivered there,
    carrying the ``turn_id`` it names (and a ``thread_id`` when the renderer
    sends one); that session's own queue stays empty, and so does a sibling
    run's inbox on the same cell."""
    _chat("run-1", "cell-it")
    _chat("run-2", "cell-it", "_runs/synth-bbbbbbbb/chat/conversation.chat")
    panel = _panel("run-1", "cell-it")
    client = TestClient(_app())
    with client.websocket_connect(
            f"{panel['ws_path']}?token={panel['token']}") as ws:
        ws.send_json({"t": "interrupt", "turn_id": "turn-7"})
        ws.send_json({"t": "interrupt", "turn_id": "turn-8", "thread_id": "main"})
    chat = chat_sessions.get_inbox("run-1")
    for _ in range(200):
        if chat.commands.qsize() >= 2:
            break
        time.sleep(0.01)
    assert chat_sessions.pop_command("run-1") == {
        "kind": "interrupt", "turn_id": "turn-7", "thread_id": ""}
    assert chat_sessions.pop_command("run-1") == {
        "kind": "interrupt", "turn_id": "turn-8", "thread_id": "main"}
    assert _session(panel).commands.empty()
    assert chat_sessions.pop_command("run-2") is None
    chat_sessions.unregister_sessions_for_run("run-1")


def test_a_broadcast_inbox_tracks_the_turn_in_flight_for_its_history_frame():
    """A reloaded drilldown must still see the turn in flight: the broadcast
    inbox tracks the turn id from the turn_start/turn_end it is sent, and the
    history frame carries it."""
    info = _broadcast("run-1", "cell-tt")
    sess = _session(info)
    chat_sessions.push_event("run-1", "turn-3", {"type": "turn_start", "thread_id": "main"})
    chat_sessions.push_event("run-1", "turn-3", {"type": "text_delta", "text": "hi"})
    assert sess.in_progress_turn_id == "turn-3"
    assert sess.in_progress_thread_id == "main"
    client = TestClient(_app())
    with client.websocket_connect(
            f"{info['ws_path']}?token={info['token']}") as ws:
        history = ws.receive_json()
        assert history["t"] == "history"
        assert history["turn_id"] == "turn-3"
        assert history["thread_id"] == "main"
        assert set(history) == {"t", "turn_id", "thread_id", "text", "thinking", "tools",
                                "pending_requests", "config_options"}
    chat_sessions.push_event("run-1", "turn-3", {"type": "turn_end"})
    assert sess.in_progress_turn_id == "" and sess.in_progress_thread_id == ""
    chat_sessions.unregister_sessions_for_run("run-1")


def test_unknown_event_types_pass_through_without_retention():
    """An event type the session does not know is forwarded live and never
    retained: it leaves no replay state, so an attach gets no history
    frame for it."""
    info = _chat("run-1", "cell-sw")
    sess = chat_sessions.get_inbox("run-1")
    chat_sessions.push_event("run-1", "", {"type": "custom_note", "text": "hello"})
    assert not (sess.in_progress_turn_id or sess.pending_requests
                or sess.config_options)

    # Nothing in flight and nothing retained → no history frame; the socket
    # just goes live (closing without a frame is the assertion).
    client = TestClient(_app())
    with client.websocket_connect(
            f"{info['ws_path']}?token={info['token']}"):
        pass
    chat_sessions.unregister_sessions_for_run("run-1")


def test_a_non_run_frame_on_a_non_inbox_session_is_dropped():
    """Only the inbox's command queue is drained (chat_poll), so a
    user_msg sent on another session is dropped rather than parked on a
    queue nothing reads."""
    panel = _panel("run-1", "cell-um")
    client = TestClient(_app())
    with client.websocket_connect(
            f"{panel['ws_path']}?token={panel['token']}") as ws:
        ws.send_json({"t": "user_msg", "content": "x"})
    sess = _session(panel)
    for _ in range(200):
        if not sess.subscribers:
            break
        time.sleep(0.01)
    assert not sess.subscribers
    assert sess.commands.qsize() == 0
    assert chat_sessions.pop_command("run-1") is None
    chat_sessions.unregister_sessions_for_run("run-1")


# ---------------------------------------------------------------------------
# The attach window — chat_ws driven directly with a fake socket, so a test
# can hold the upgrade inside accept() or the history send.
# ---------------------------------------------------------------------------


class _FakeWS:
    """The slice of starlette's WebSocket chat_ws uses. ``accept_gate`` /
    ``history_gate`` (asyncio.Events) hold accept() / the history frame's
    send until set; ``entered`` fires once the held call is reached."""

    def __init__(self, accept_gate=None, history_gate=None):
        self.client_state = WebSocketState.CONNECTED
        self.sent: list = []
        self.closed_with = None
        self.entered = asyncio.Event()
        self._accept_gate = accept_gate
        self._history_gate = history_gate
        self._inbound: asyncio.Queue = asyncio.Queue()

    async def accept(self):
        if self._accept_gate is not None:
            self.entered.set()
            await self._accept_gate.wait()

    async def send_text(self, text):
        frame = json.loads(text)
        if self._history_gate is not None and frame.get("t") == "history":
            self.entered.set()
            await self._history_gate.wait()
        self.sent.append(frame)

    async def close(self, code=1000, reason=""):
        if self.closed_with is None:
            self.closed_with = code
        self.client_state = WebSocketState.DISCONNECTED
        self._inbound.put_nowait({"type": "websocket.disconnect"})

    async def receive(self):
        return await self._inbound.get()

    def disconnect(self):
        self._inbound.put_nowait({"type": "websocket.disconnect"})


async def test_chat_ws_second_upgrade_during_accept_gets_4409():
    """The single-attach claim is taken before the first await: a second
    upgrade racing the first one's accept() is refused with 4409 rather than
    splitting the outbox between two sockets."""
    info = _chat("run-1", "cell-race")
    sid = info["chat_id"]
    gate = asyncio.Event()
    ws1 = _FakeWS(accept_gate=gate)
    first = asyncio.create_task(cl.chat_ws(ws1, sid, info["token"]))
    await asyncio.wait_for(ws1.entered.wait(), 2)
    ws2 = _FakeWS()
    second = asyncio.create_task(cl.chat_ws(ws2, sid, info["token"]))
    try:
        await asyncio.wait_for(asyncio.shield(second), 2)
    except asyncio.TimeoutError:
        pass
    closed_with = ws2.closed_with
    gate.set()
    ws1.disconnect()
    ws2.disconnect()
    await asyncio.wait_for(asyncio.gather(first, second), 2)
    assert closed_with == 4409
    assert chat_sessions.get_inbox("run-1").attached is False


async def test_chat_ws_delta_during_the_history_send_follows_it():
    """A text delta pushed while the history frame is being sent missed the
    snapshot, so it must flow as a live frame after it, not vanish."""
    info = _chat("run-1", "cell-gap")
    chat_sessions.push_event("run-1", "turn-1", {"type": "turn_start"})
    chat_sessions.push_event("run-1", "turn-1", {"type": "text_delta", "text": "He"})
    gate = asyncio.Event()
    ws = _FakeWS(history_gate=gate)
    task = asyncio.create_task(cl.chat_ws(ws, info["chat_id"], info["token"]))
    await asyncio.wait_for(ws.entered.wait(), 2)
    chat_sessions.push_event("run-1", "turn-1", {"type": "text_delta", "text": "llo"})
    gate.set()
    for _ in range(200):
        if len(ws.sent) >= 2:
            break
        await asyncio.sleep(0.01)
    ws.disconnect()
    await asyncio.wait_for(task, 2)
    assert ws.sent[0]["t"] == "history" and ws.sent[0]["text"] == "He"
    assert ws.sent[1:] == [{"t": "delta", "turn_id": "turn-1", "text": "llo"}]


@pytest.mark.parametrize("surface", ["chat", "broadcast"])
async def test_chat_ws_unregistered_during_accept_closes_4404(surface):
    """A session unregistered while its upgrade sat in accept() already sent
    its close sentinel; the socket closes with 4404 instead of idling on a
    dead session."""
    info = _chat("run-1", "cell-dead") if surface == "chat" else _broadcast("run-1", "cell-dead")
    gate = asyncio.Event()
    ws = _FakeWS(accept_gate=gate)
    task = asyncio.create_task(cl.chat_ws(ws, info["chat_id"], info["token"]))
    await asyncio.wait_for(ws.entered.wait(), 2)
    chat_sessions.unregister_chat_session(info["chat_id"])
    gate.set()
    try:
        await asyncio.wait_for(asyncio.shield(task), 2)
    except asyncio.TimeoutError:
        ws.disconnect()
        await asyncio.wait_for(task, 2)
    assert ws.closed_with == 4404
