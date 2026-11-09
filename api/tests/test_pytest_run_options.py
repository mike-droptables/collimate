# SPDX-License-Identifier: MIT
"""A run's live options: the set it publishes over IPC (``publish_options``)
lives on its manager and its /runs/active row under a revision, and every
publish rings ``runs_active_changed``; ``PUT /runs/{run_id}/options``
queues a change the set takes, which the run polls (``poll_option_change``),
oldest first, bounded. Manager stand-ins and synthetic options throughout:
nothing spawns."""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest

from collimate._host.ipc import RUN_OPTION_CHANGES_MAX
from fake_manager import FakeManager
from collimate_server.process_manager.ipc import IPCMixin

SET = [
    {"id": "speed", "name": "Speed", "category": "model", "type": "select",
     "currentValue": "slow", "options": [{"value": "slow", "name": "Slow"},
                                         {"value": "fast", "name": "Fast", "description": "Quick"}]},
    {"id": "loud", "name": "Loud", "type": "boolean", "currentValue": False},
]


class _StubIPC(FakeManager):
    """Just enough of the IPC handler for the option ops: the manager state,
    an ack recorder and the run log."""

    def __init__(self, **over):
        super().__init__(log_buffer=[], acks=[], **over)

    async def _send_ack(self, body: dict, _request: dict) -> None:
        self.acks.append(body)

    log_refusal = IPCMixin.log_refusal


def _op(stub, name: str, data: dict):
    asyncio.run(types.MethodType(getattr(IPCMixin, f"_op_{name}"), stub)(data))


@pytest.fixture
def bells(monkeypatch):
    import collimate_server.events as events
    heard: list = []
    monkeypatch.setattr(events, "broadcast_all", heard.append)
    return heard


def test_a_published_set_is_kept_normalized_under_a_new_revision_and_rings(bells):
    stub = _StubIPC(channel=str(uuid.uuid4()))
    _op(stub, "publish_options", {"options": [{**SET[0], "extra": 1}, SET[1]]})
    assert stub.run_options == SET
    assert stub.run_options_rev == 1
    assert [b["type"] for b in bells] == ["runs_active_changed"]
    # The same set again is a new publish: a new revision, another ring, so
    # a client waiting on its change hears the run's answer.
    _op(stub, "publish_options", {"options": SET})
    assert stub.run_options_rev == 2 and len(bells) == 2
    _op(stub, "publish_options", {"options": []})
    assert stub.run_options == [] and stub.run_options_rev == 3


@pytest.mark.parametrize("bad", [
    "speed",
    [{"id": "", "name": "X", "type": "boolean", "currentValue": True}],
    [{"id": "x", "name": "X", "type": "toggle", "currentValue": True}],
    [{"id": "x", "name": "X", "type": "boolean", "currentValue": "on"}],
    [{"id": "x", "name": "X", "type": "select", "currentValue": "a", "options": []}],
    [{"id": "x", "name": "X", "type": "select", "currentValue": "a", "options": [{"value": "a"}]}],
    [SET[1], SET[1]],
])
def test_a_malformed_set_is_refused_into_the_run_log_and_the_last_stands(bells, bad):
    stub = _StubIPC(run_options=SET, run_options_rev=4)
    _op(stub, "publish_options", {"options": bad})
    assert stub.run_options == SET and stub.run_options_rev == 4
    assert bells == []
    assert "publish_options refused" in stub.log_buffer[-1]["payload"]


def test_a_new_set_drops_the_queued_changes_it_no_longer_takes(bells):
    """The run only ever polls a change to the set it published last: one
    to an option withdrawn, or to a value no longer offered, goes."""
    stub = _StubIPC(run_options=SET)
    stub.option_changes.extend([{"id": "speed", "value": "fast"}, {"id": "loud", "value": True},
                                {"id": "speed", "value": "slow"}])
    _op(stub, "publish_options", {"options": [{**SET[0], "options": SET[0]["options"][:1]}]})
    assert list(stub.option_changes) == [{"id": "speed", "value": "slow"}]
    assert stub.option_changes.maxlen == RUN_OPTION_CHANGES_MAX


def test_poll_pops_the_oldest_change_then_none():
    stub = _StubIPC()
    stub.option_changes.extend([{"id": "speed", "value": "fast"}, {"id": "loud", "value": True}])
    _op(stub, "poll_option_change", {})
    _op(stub, "poll_option_change", {})
    _op(stub, "poll_option_change", {})
    assert stub.acks == [
        {"op": "ack_poll_option_change", "id": "speed", "value": "fast"},
        {"op": "ack_poll_option_change", "id": "loud", "value": True},
        {"op": "ack_poll_option_change", "id": None},
    ]


@pytest.fixture
def live_run(client):
    """A live run on the starting channel that published SET."""
    from collimate_server.run_registry import active_managers
    from collimate_server.state import find_active_channel
    channel = str(find_active_channel().id)
    run_id = str(uuid.uuid4())
    manager = FakeManager(channel=channel, run_id=run_id, cell_id="cell-1",
                          run_options=SET, run_options_rev=1)
    active_managers[run_id] = manager
    try:
        yield channel, run_id, manager
    finally:
        active_managers.pop(run_id, None)


def test_put_queues_a_change_the_set_takes(client, live_run):
    channel, run_id, manager = live_run
    url = f"/api/channels/{channel}/runs/{run_id}/options"
    assert client.put(url, json={"id": "speed", "value": "fast"}).json() == {"ok": True}
    assert client.put(url, json={"id": "loud", "value": True}).status_code == 200
    assert list(manager.option_changes) == [{"id": "speed", "value": "fast"},
                                            {"id": "loud", "value": True}]
    # The published set is the run's to change: the PUT leaves it alone.
    assert manager.run_options == SET


@pytest.mark.parametrize("change,detail", [
    ({"id": "volume", "value": "x"}, "the run offers no option 'volume'"),
    ({"id": "speed", "value": "warp"}, "Speed offers no value 'warp'"),
    ({"id": "speed", "value": True}, "Speed offers no value True"),
    ({"id": "loud", "value": "true"}, "Loud takes true or false"),
])
def test_put_refuses_a_change_the_set_does_not_take(client, live_run, change, detail):
    channel, run_id, manager = live_run
    r = client.put(f"/api/channels/{channel}/runs/{run_id}/options", json=change)
    assert r.status_code == 409 and r.json()["detail"] == detail
    assert not manager.option_changes


def test_put_refuses_an_unknown_field(client, live_run):
    channel, run_id, _manager = live_run
    r = client.put(f"/api/channels/{channel}/runs/{run_id}/options",
                   json={"id": "loud", "value": True, "extra": 1})
    assert r.status_code == 422


def test_put_refuses_a_run_that_is_not_live_here_or_is_ending(client, live_run):
    channel, run_id, manager = live_run
    other = client.post("/api/channels", params={"name": "Other"}).json()["id"]
    body = {"id": "loud", "value": True}
    assert client.put(f"/api/channels/{channel}/runs/{uuid.uuid4()}/options", json=body).status_code == 404
    assert client.put(f"/api/channels/{other}/runs/{run_id}/options", json=body).status_code == 404
    manager.stop_requested = True
    r = client.put(f"/api/channels/{channel}/runs/{run_id}/options", json=body)
    assert r.status_code == 409 and r.json()["detail"] == "That run is ending"
    assert not manager.option_changes


def test_the_queue_keeps_the_newest_changes(client, live_run):
    channel, run_id, manager = live_run
    url = f"/api/channels/{channel}/runs/{run_id}/options"
    for i in range(RUN_OPTION_CHANGES_MAX + 3):
        client.put(url, json={"id": "loud", "value": i % 2 == 0})
    assert len(manager.option_changes) == RUN_OPTION_CHANGES_MAX
    assert manager.option_changes[0] == {"id": "loud", "value": (3 % 2 == 0)}


def test_the_active_row_carries_the_set_and_its_revision(client, live_run):
    channel, run_id, _manager = live_run
    [row] = [r for r in client.get(f"/api/channels/{channel}/runs/active").json()
             if r["run_id"] == run_id]
    assert row["options"] == SET and row["options_rev"] == 1
