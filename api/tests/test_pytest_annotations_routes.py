"""The annotation HTTP routes: a cell's contexts, user writes and their
refusals and doorbells, a run's file touches and its broadcast inbox."""
from __future__ import annotations

import asyncio
import uuid

import pytest

import test_pytest_run_folders as support
from test_pytest_run_folders import CHANNEL, _emit, _mem

client, cell, live = support.client, support.cell, support.live


@pytest.fixture
def runs(live, monkeypatch):
    """``live`` with the run listing reading the same registry."""
    monkeypatch.setattr("routes.runs.queries.active_managers", live.runs)
    return live


@pytest.fixture
def rung(monkeypatch):
    import events
    seen: list = []
    monkeypatch.setattr(events, "broadcast", lambda channel, event: seen.append(event))
    return seen


def base(cell_id):
    return f"/api/channels/{CHANNEL}/cells/{cell_id}/annotations"


def send_post(client, cell_id, context_id, body="hello", **extra):
    payload = {"operation_id": str(uuid.uuid4()), "context_id": context_id, "body": body, "mode": "queued", **extra}
    return client.post(base(cell_id), json=payload)


def doorbells(seen):
    return [e for e in seen if e["type"] == "annotations_changed"]


def test_get_lists_the_synthesized_cell_context_then_live_and_stopping_run_contexts(client, cell, runs):
    first, second = runs.start(cell), runs.start(cell)
    other_cell = client.post(f"/api/stacks/{CHANNEL}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    runs.start(other_cell)
    second.stop_requested = True
    response = client.get(base(cell))
    assert response.status_code == 200, response.text
    contexts = response.json()["contexts"]
    assert [c["id"] for c in contexts] == ["cell", first.run_id, second.run_id]
    assert contexts[0] == {"id": "cell", "cell_id": cell, "run_id": None, "label": None, "bar_on": False,
                           "listening": False, "interrupt_seq": 0, "rev": 0, "sealed": False, "messages": []}
    assert contexts[1]["label"] == first.label and contexts[1]["run_id"] == first.run_id


def test_get_one_context_returns_sealed_ones_and_404s_unknown_and_other_cells(client, cell, runs):
    from process_manager.finalization import commit_run_annotations
    run = runs.start(cell)
    assert send_post(client, cell, run.run_id).status_code == 200
    commit_run_annotations(CHANNEL, run.run_id)
    runs.runs.pop(run.run_id)
    sealed = client.get(f"{base(cell)}/{run.run_id}")
    assert sealed.status_code == 200 and sealed.json()["sealed"] is True
    assert [m["body"] for m in sealed.json()["messages"]] == ["hello"]
    assert client.get(f"{base(cell)}/no-such-run").status_code == 404
    other_cell = client.post(f"/api/stacks/{CHANNEL}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    assert client.get(f"{base(other_cell)}/{run.run_id}").status_code == 404
    assert client.get(f"{base(cell)}/cell").json()["messages"] == []


def test_post_patch_delete_round_trip_and_each_rings_annotations_changed(client, cell, runs, rung):
    run = runs.start(cell)
    posted = send_post(client, cell, run.run_id, "first [a](a.md)",
                       context_items=[{"path": "page.html", "renderer_id": "html", "label": "page.html › p",
                                       "text": "Element p", "anchor": {"css": "p"}}])
    assert posted.status_code == 200, posted.text
    message = posted.json()
    assert message["author"] == "user" and message["refs"] == ["a.md"] and message["read_at"] is None
    assert message["context_items"][0]["anchor"] == {"css": "p"}
    patched = client.patch(f"{base(cell)}/{message['id']}", json={"expected_revision": 1, "body": "second"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["body"] == "second" and patched.json()["revision"] == 2
    deleted = client.delete(f"{base(cell)}/{message['id']}", params={"expected_revision": 2})
    assert deleted.status_code == 200 and deleted.json() == {"ok": True}
    assert doorbells(rung) == [
        {"type": "annotations_changed", "cell_id": cell, "context_id": run.run_id, "rev": rev} for rev in (1, 2, 3)]


def test_refusals_carry_error_and_detail(client, cell, runs):
    from database import get_db_connection
    run = runs.start(cell)
    message = send_post(client, cell, run.run_id).json()
    stale = client.patch(f"{base(cell)}/{message['id']}", json={"expected_revision": 9, "body": "x"})
    assert stale.status_code == 409 and stale.json()["error"] == "revision"
    assert isinstance(stale.json()["detail"], str) and stale.json()["detail"]
    reply = send_post(client, cell, run.run_id, "reply", parent_id=message["id"]).json()
    deep = send_post(client, cell, run.run_id, "deep", parent_id=reply["id"])
    assert (deep.status_code, deep.json()["error"]) == (409, "parent_not_root")
    assert send_post(client, cell, "no-such-run").json()["error"] == "unknown_context"
    assert client.patch(f"{base(cell)}/a_ffffffffffffffff", json={"expected_revision": 1, "body": "x"}).status_code == 404
    big = send_post(client, cell, run.run_id, "x" * (64 * 1024 + 1))
    assert (big.status_code, big.json()["error"]) == (413, "too_large")
    empty = send_post(client, cell, run.run_id, " ")
    assert (empty.status_code, empty.json()["error"]) == (422, "empty_text")
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("UPDATE annotations SET read_at = created_at WHERE id = ?", (message["id"],))
        conn.commit()
    finally:
        conn.close()
    locked = client.delete(f"{base(cell)}/{message['id']}", params={"expected_revision": 1})
    assert (locked.status_code, locked.json()["error"]) == (409, "locked")
    run.sealed = True
    sealed = send_post(client, cell, run.run_id)
    assert (sealed.status_code, sealed.json()["error"]) == (409, "sealed")


def _invalid(response) -> str:
    """The sentence of a 422 ``invalid`` refusal (and that it is one)."""
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"error", "detail"} and body["error"] == "invalid", body
    assert isinstance(body["detail"], str) and body["detail"].endswith("."), body
    return body["detail"]


def test_request_models_forbid_extra_fields_as_invalid_refusals(client, cell, runs):
    run = runs.start(cell)
    assert _invalid(send_post(client, cell, run.run_id, extra=1)).startswith("extra:")
    assert _invalid(send_post(client, cell, run.run_id, mode="later")).startswith("mode:")
    item = {"path": "a.md", "renderer_id": "md", "label": "a", "text": "t", "anchor": None, "more": 1}
    assert _invalid(send_post(client, cell, run.run_id, context_items=[item])).startswith(
        "context_items.0.more:")
    assert _invalid(client.put(f"{base(cell)}/{run.run_id}/bar", json={"on": True, "x": 1}))
    message = send_post(client, cell, run.run_id).json()
    assert _invalid(client.delete(f"{base(cell)}/{message['id']}")).startswith(
        "query.expected_revision:")
    assert _invalid(client.patch(f"{base(cell)}/{message['id']}", json={"expected_revision": 0}))
    assert _invalid(client.put(f"{base(cell)}/{run.run_id}/order", json={"ids": "x"}))
    assert _invalid(client.post(f"{base(cell)}/{message['id']}/send", json={}))
    broken = client.post(base(cell), content=b"{not json", headers={"content-type": "application/json"})
    assert _invalid(broken)


def test_seventeen_context_items_are_an_invalid_refusal_with_a_sentence(client, cell, runs):
    run = runs.start(cell)
    item = {"path": "a.md", "renderer_id": "md", "label": "a", "text": "t", "anchor": None}
    response = send_post(client, cell, run.run_id, context_items=[item] * 17)
    detail = _invalid(response)
    assert detail.startswith("context_items: ") and "16" in detail
    patched = client.patch(f"{base(cell)}/a_ffffffffffffffff",
                           json={"expected_revision": 1, "context_items": [item] * 17})
    assert _invalid(patched).startswith("context_items: ")


def test_a_cell_not_on_the_canvas_is_404(client):
    missing = str(uuid.uuid4())
    assert client.get(base(missing)).status_code == 404
    assert send_post(client, missing, "cell").status_code == 404


def test_writes_refuse_while_the_channel_is_being_deleted(client, cell, runs):
    import database
    database.changing_channels.add(CHANNEL)
    try:
        response = send_post(client, cell, "cell")
    finally:
        database.changing_channels.discard(CHANNEL)
    assert response.status_code == 409


def test_writes_hold_channel_busy(client, cell, runs, monkeypatch):
    import annotations
    import run_registry
    seen = []
    original = annotations.post

    def post(*args, **kwargs):
        seen.append(run_registry.busy_channels())
        return original(*args, **kwargs)
    monkeypatch.setattr(annotations, "post", post)
    assert send_post(client, cell, "cell").status_code == 200
    assert seen == [{CHANNEL}]
    assert run_registry.busy_channels() == set()


@pytest.mark.parametrize("ending", ["stop_requested", "final_status", "sealed", "gone"])
def test_post_into_a_stopping_finished_or_sealed_run_is_409_sealed(client, cell, runs, ending):
    run = runs.start(cell)
    if ending == "gone":
        runs.runs.pop(run.run_id)
    else:
        setattr(run, ending, "completed" if ending == "final_status" else True)
    response = send_post(client, cell, run.run_id)
    assert (response.status_code, response.json()["error"]) == (409, "sealed")


def test_send_route_moves_and_rings_both_contexts(client, cell, runs, rung):
    run = runs.start(cell)
    note = send_post(client, cell, "cell", "for later").json()
    rung.clear()
    sent = client.post(f"{base(cell)}/{note['id']}/send", json={"run_id": run.run_id})
    assert sent.status_code == 200, sent.text
    assert sent.json()["context_id"] == run.run_id
    assert [(e["context_id"]) for e in doorbells(rung)] == ["cell", run.run_id]
    run.stop_requested = True
    other = send_post(client, cell, "cell", "again").json()
    refused = client.post(f"{base(cell)}/{other['id']}/send", json={"run_id": run.run_id})
    assert (refused.status_code, refused.json()["error"]) == (409, "run_not_live")


def test_order_bar_and_interrupt_routes_answer_the_context(client, cell, runs):
    run = runs.start(cell)
    a = send_post(client, cell, run.run_id, "a").json()
    b = send_post(client, cell, run.run_id, "b").json()
    bar = client.put(f"{base(cell)}/{run.run_id}/bar", json={"on": True})
    assert bar.status_code == 200 and bar.json()["bar_on"] is True
    order = client.put(f"{base(cell)}/{run.run_id}/order", json={"ids": [b["id"], a["id"]], "held_from": 1})
    assert order.status_code == 200, order.text
    assert [(m["body"], m["held"]) for m in order.json()["messages"]] == [("b", False), ("a", True)]
    stale = client.put(f"{base(cell)}/{run.run_id}/order", json={"ids": [a["id"]]})
    assert stale.json()["error"] == "stale"
    interrupted = client.post(f"{base(cell)}/{run.run_id}/interrupt", json={"ids": [a["id"]]})
    assert interrupted.status_code == 200
    assert interrupted.json()["interrupt_seq"] == 1
    assert [(m["body"], m["mode"]) for m in interrupted.json()["messages"]] == [("a", "interrupt"), ("b", "queued")]
    assert _invalid(client.post(f"{base(cell)}/{run.run_id}/interrupt", json={"ids": []})).startswith("ids:")


def test_file_touches_flattens_commit_changes_oldest_first_and_404s_unknown_runs(client, cell, runs):
    run = runs.start(cell)
    first, _ = _emit(run, artifacts=[_mem("docs/a.md", "a"), _mem("b.md", "b")])
    second, _ = _emit(run, artifacts=[_mem("b.md", "b2")], deleted_files=["docs/a.md"])
    response = client.get(f"/api/channels/{CHANNEL}/runs/{run.run_id}/file_touches")
    assert response.status_code == 200, response.text
    touches = response.json()["touches"]
    assert [(t["path"], t["op"], t["folder"], t["version_id"]) for t in touches] == [
        ("docs", "create", True, first), ("b.md", "create", False, first), ("docs/a.md", "create", False, first),
        ("b.md", "edit", False, second), ("docs/a.md", "delete", False, second), ("docs", "delete", True, second),
    ]
    assert all(len(t["at"]) == 19 for t in touches)
    assert client.get(f"/api/channels/{CHANNEL}/runs/no-such-run/file_touches").status_code == 404


def test_inbox_answers_only_a_broadcast_inbox_of_a_live_run_on_this_channel(client, cell, runs, monkeypatch):
    import run_registry
    from models import Channel
    from state import state
    url = lambda channel, run_id: f"/api/channels/{channel}/runs/{run_id}/inbox"  # noqa: E731
    assert client.get(url(CHANNEL, "no-such-run")).status_code == 404
    single = runs.start(cell)
    single.ui_files = (".chat",)

    def register(run, path, **options):
        sent = []

        async def capture(body, _request):
            sent.append(body)
        run._send_ack = capture
        asyncio.run(run._op_surface_register({"path": path, "label": "Chat", "transport_only": False,
                                              "broadcast": False, **options}))
        assert sent[-1]["ok"], sent[-1]
        return sent[-1]
    register(single, f"{single.folder}/chat.chat")
    assert client.get(url(CHANNEL, single.run_id)).status_code == 404
    shared = runs.start(cell)
    ack = register(shared, f"{shared.folder}/inbox.chat", transport_only=True, broadcast=True)
    response = client.get(url(CHANNEL, shared.run_id))
    assert response.status_code == 200
    assert response.json() == {"chat_id": ack["chat_id"], "ws_path": ack["ws_path"], "token": ack["token"]}
    other = Channel(name="Elsewhere")
    state.channels.append(other)
    assert client.get(url(other.id, shared.run_id)).status_code == 404
    monkeypatch.setattr(run_registry, "active_managers", {})
    assert client.get(url(CHANNEL, shared.run_id)).status_code == 404
