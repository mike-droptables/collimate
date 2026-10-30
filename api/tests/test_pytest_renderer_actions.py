# SPDX-License-Identifier: MIT
"""Tagged renderer actions (agent-v2 §4.3.4).

The renderer-action channel carries ``{action_id, source, payload}`` dicts
now — ``source`` names the emitting surface's file and ``payload`` an
optional structured value — and ``poll_renderer_action`` pops the FIRST
entry matching the caller's filters, so several concurrent generator-side
waiters (a chat card, a terminal button loop) share the queue
without stealing each other's entries. These tests drive the route handler's
queue discipline and the IPC op's filter semantics directly against a stub
manager — synthetic throughout.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest
from process_manager.ipc import IPCMixin
from fake_manager import FakeManager


class _StubIPC:
    """Just enough of the IPC handler for _op_poll_renderer_action: the
    pending queue plus an ack recorder."""

    def __init__(self):
        self._pending_renderer_actions: list = []
        self.acks: list = []

    async def _send_ack(self, body: dict, _request: dict) -> None:
        self.acks.append(body)


def _poll(stub: _StubIPC, **filters) -> dict:
    op = types.MethodType(IPCMixin._op_poll_renderer_action, stub)
    asyncio.run(op(dict(filters)))
    return stub.acks.pop()


def test_poll_pops_fifo_without_filters():
    stub = _StubIPC()
    stub._pending_renderer_actions += [
        {"action_id": "a", "source": "_runs/synth-aaaaaaaa/panel/status.panel", "payload": None},
        {"action_id": "b", "source": "_runs/synth-aaaaaaaa/chat/conversation.chat", "payload": None},
    ]
    ack = _poll(stub)
    assert ack["action_id"] == "a" and ack["source"] == "_runs/synth-aaaaaaaa/panel/status.panel"
    assert "payload" not in ack          # None payloads never ride the ack
    ack = _poll(stub)
    assert ack["action_id"] == "b"
    assert _poll(stub)["action_id"] is None     # drained


def test_poll_filters_by_source_and_leaves_others_queued():
    """A source-filtered waiter takes ONLY its surface's entries; entries for
    other surfaces stay queued for their own waiter — the reason the bare
    FIFO had to become a keyed queue."""
    stub = _StubIPC()
    stub._pending_renderer_actions += [
        {"action_id": "chat-click", "source": "_runs/synth-aaaaaaaa/chat/conversation.chat", "payload": None},
        {"action_id": "panel-answer", "source": "_runs/synth-aaaaaaaa/panel/status.panel",
         "payload": {"value": "yes"}},
    ]
    ack = _poll(stub, source="_runs/synth-aaaaaaaa/panel/status.panel")
    assert ack["action_id"] == "panel-answer"
    assert ack["payload"] == {"value": "yes"}
    # The chat entry was not stolen.
    assert [e["action_id"] for e in stub._pending_renderer_actions] == ["chat-click"]
    # A non-matching filter comes back empty without draining anything.
    assert _poll(stub, source="_runs/synth-aaaaaaaa/panel/status.panel")["action_id"] is None
    assert len(stub._pending_renderer_actions) == 1


def test_poll_filters_by_action_id():
    stub = _StubIPC()
    stub._pending_renderer_actions += [
        {"action_id": "copy", "source": "session.terminal", "payload": None},
        {"action_id": "capture", "source": "session.terminal", "payload": None},
    ]
    ack = _poll(stub, action_id="capture")
    assert ack["action_id"] == "capture"
    assert [e["action_id"] for e in stub._pending_renderer_actions] == ["copy"]


CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "actions:primary"))
OTHER_CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "actions:other"))


def _mgr(run_id="r1", *, channel=CHANNEL, cell_id="cell-1", label="synth-aaaaaaaa"):
    return FakeManager(
        run_id=run_id, channel=channel, cell_id=cell_id, label=label,
        _pending_renderer_actions=[])


@pytest.fixture
def live(fresh_cwd, monkeypatch):
    """Install managers as the live runs the actions route resolves from."""
    import state
    from database import get_db_connection
    from models import AppState, Channel
    primary = Channel(id=CHANNEL, name="My Project")
    other = Channel(id=OTHER_CHANNEL, name="Other Project")
    monkeypatch.setattr(state, "state", AppState(channels=[primary, other], active_channel_id=primary.id))
    get_db_connection(CHANNEL).close()
    get_db_connection(OTHER_CHANNEL).close()
    import routes.runs.control as control
    import run_registry as registry
    runs: dict = {}
    monkeypatch.setattr(control, "active_managers", runs)
    monkeypatch.setattr(registry, "active_managers", runs)

    def add(*mgrs):
        for m in mgrs:
            runs[m.run_id] = m
        return mgrs[0] if len(mgrs) == 1 else mgrs
    return add


def _emit(channel, cell, **body):
    from routes.runs.control import emit_renderer_action
    from routes.runs.models import RendererActionRequest
    return asyncio.run(emit_renderer_action(channel, cell, RendererActionRequest(**body)))


def test_action_route_shapes_and_bounds_the_queue(live):
    """The POST route appends the tagged dict and bounds the queue at 256
    (drop-oldest) so unmatched entries can't grow without limit."""
    mgr = live(_mgr())
    source = "_runs/synth-aaaaaaaa/panel/status.panel"
    _emit(CHANNEL, "cell-1", action_id="go", source=source, payload={"n": 1})
    assert mgr._pending_renderer_actions == [
        {"action_id": "go", "source": source, "payload": {"n": 1}}]
    # A payload that isn't a dict is dropped to None, never trusted.
    _emit(CHANNEL, "cell-1", action_id="raw", source=source, payload=None)
    assert mgr._pending_renderer_actions[-1] == {
        "action_id": "raw", "source": source, "payload": None}
    for i in range(300):
        _emit(CHANNEL, "cell-1", action_id=f"a{i}", source=source)
    assert len(mgr._pending_renderer_actions) == 256
    assert mgr._pending_renderer_actions[-1]["action_id"] == "a299"


def test_a_renderer_action_goes_to_the_run_owning_its_file(live):
    """Two runs on one cell: a renderer's action lands in the queue of the
    run whose folder holds the renderer's file, never the other's."""
    a, b = live(_mgr("ra", label="synth-aaaaaaaa"), _mgr("rb", label="synth-bbbbbbbb"))
    _emit(CHANNEL, "cell-1", action_id="go", source="_runs/synth-bbbbbbbb/tab-1.webview")
    assert a._pending_renderer_actions == []
    assert [e["action_id"] for e in b._pending_renderer_actions] == ["go"]


@pytest.mark.parametrize("source", [
    "notes.md",                               # an ordinary file: no run owns it
    "_runs/synth-dddddddd/tab-1.webview",     # a dead run's folder
    "@run",                                   # no reserved source: unowned like any path
    "",
])
def test_a_renderer_action_nobody_owns_is_a_409(live, source):
    from fastapi import HTTPException
    live(_mgr())
    with pytest.raises(HTTPException) as exc:
        _emit(CHANNEL, "cell-1", action_id="go", source=source)
    assert exc.value.status_code == 409
    assert "no live run owns" in exc.value.detail


def test_an_action_naming_a_run_is_a_422(client):
    """The run is resolved from the action's source alone: a request
    carrying any other field, such as a run id, is refused at the edge."""
    from state import find_active_channel
    r = client.post(f"/api/channels/{find_active_channel().id}/cells/cell-1/actions", json={
        "action_id": "go", "source": "_runs/synth-aaaaaaaa/tab-1.webview",
        "run_id": "ra"})
    assert r.status_code == 422, r.text


def test_action_route_uses_registered_identity_after_label_change(live):
    from state import state
    mgr = live(_mgr())
    state.channels[0].name = "Renamed Project"
    _emit(CHANNEL, "cell-1", action_id="go", source="_runs/synth-aaaaaaaa/x.panel")
    assert [entry["action_id"] for entry in mgr._pending_renderer_actions] == ["go"]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _emit(OTHER_CHANNEL, "cell-1", action_id="wrong", source="_runs/synth-aaaaaaaa/x.panel")
    assert exc.value.status_code == 409
    assert [entry["action_id"] for entry in mgr._pending_renderer_actions] == ["go"]


def test_active_runs_query_is_scoped_by_uuid_after_label_change(live, monkeypatch):
    from routes.runs import queries
    from state import state
    mgr = live(_mgr())
    monkeypatch.setattr(queries, "active_managers", {"r1": mgr})
    state.channels[0].name = "Renamed Project"
    result = asyncio.run(queries.list_active_runs(CHANNEL))
    assert [row["run_id"] for row in result] == ["r1"]
    assert result[0]["cell_id"] == "cell-1"
    assert asyncio.run(queries.list_active_runs(OTHER_CHANNEL)) == []
