"""The run side of the annotation context over IPC: poll, pull, post and
list on a real ProcessManager whose run row carries its context."""
from __future__ import annotations

import asyncio
import uuid

import pytest

import annotations as store
import test_pytest_run_folders as support
from test_pytest_run_folders import CHANNEL

client, cell, live = support.client, support.cell, support.live


@pytest.fixture
def bells(monkeypatch):
    """Every channel event and every all-channel event broadcast."""
    import events
    seen: dict = {"channel": [], "all": []}
    monkeypatch.setattr(events, "broadcast", lambda channel, event: seen["channel"].append(event))
    monkeypatch.setattr(events, "broadcast_all", lambda event: seen["all"].append(event))
    return seen


def call(run, op, **data):
    sent = []

    async def capture(body, request):
        sent.append({**body, "req_id": request["req_id"]})
    run._send_ack = capture
    asyncio.run(getattr(run, f"_op_{op}")({"op": op, "req_id": "r", **data}))
    (ack,) = sent
    assert ack["op"] == f"ack_{op}" and ack["req_id"] == "r"
    return ack


def user_post(run, body, *, mode="queued", held=False, parent=None, context_id=None):
    from database import get_db_connection
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("BEGIN IMMEDIATE")
        message, _ = store.post(conn, run.cell_id, context_id=context_id or run.run_id, body=body,
                                context_items=[], mode=mode, held=held, parent_id=parent,
                                operation_id=str(uuid.uuid4()), digest="d", live={run.run_id})
        conn.commit()
        return message
    finally:
        conn.close()


def context_of(run):
    from database import get_db_connection
    conn = get_db_connection(CHANNEL)
    try:
        return store.get_context(conn, run.cell_id, run.run_id)
    finally:
        conn.close()


def post_op(run, body, *, reply_to=None, op=None, message_id=None):
    return call(run, "annotations_post", operation_id=op or str(uuid.uuid4()),
                message_id=message_id or store.mint_id(), body=body, reply_to=reply_to)


def test_annotation_ops_are_request_ops_and_in_the_dispatch_table(cell, live):
    from collimate._host.ipc import ANNOTATION_OPS
    from process_manager.ipc import REQUEST_OPS
    run = live.start(cell)
    for op in ANNOTATION_OPS:
        assert op in REQUEST_OPS
        assert run._ipc_handlers[op] == getattr(run, f"_op_{op}")
    assert "comment" not in REQUEST_OPS and "comment" not in run._ipc_handlers


def test_first_poll_marks_listening_rings_and_broadcasts_runs_active_changed(cell, live, bells):
    run = live.start(cell)
    user_post(run, "hello")
    ack = call(run, "annotations_poll")
    assert ack == {"op": "ack_annotations_poll", "req_id": "r", "ok": True, "rev": 2, "unread": 1,
                   "interrupt_seq": 0, "listening": True}
    assert bells["channel"] == [{"type": "annotations_changed", "cell_id": cell, "context_id": run.run_id, "rev": 2}]
    assert [e["type"] for e in bells["all"]] == ["runs_active_changed"]
    assert context_of(run)["listening"] is True


def test_later_polls_do_not_write(cell, live, bells):
    run = live.start(cell)
    call(run, "annotations_poll")
    rev = context_of(run)["rev"]
    bells["channel"].clear()
    bells["all"].clear()
    for _ in range(3):
        assert call(run, "annotations_poll")["rev"] == rev
    assert context_of(run)["rev"] == rev and bells == {"channel": [], "all": []}


def test_pull_flushes_drafts_before_marking_read(cell, live, monkeypatch, bells):
    run = live.start(cell)
    user_post(run, "a")
    order = []
    original = store.pull

    def flush(include_tip):
        order.append(("flush", include_tip))

    def pull(*args, **kwargs):
        order.append(("pull", None))
        return original(*args, **kwargs)
    monkeypatch.setattr(run, "_flush_chat_files_to_workspace", flush)
    monkeypatch.setattr(store, "pull", pull)
    ack = call(run, "annotations_pull", operation_id="pull-1")
    assert ack["ok"] and [m["body"] for m in ack["messages"]] == ["a"]
    assert ack["messages"][0]["read_at"]
    assert order == [("flush", True), ("pull", None)]
    assert bells["channel"][-1]["type"] == "annotations_changed"


def test_pull_replays_by_operation_id_without_reflushing(cell, live, monkeypatch):
    run = live.start(cell)
    user_post(run, "a")
    flushes = []
    monkeypatch.setattr(run, "_flush_chat_files_to_workspace", lambda include_tip: flushes.append(include_tip))
    first = call(run, "annotations_pull", operation_id="pull-1")
    user_post(run, "b")
    again = call(run, "annotations_pull", operation_id="pull-1")
    assert again["messages"] == first["messages"] and len(flushes) == 1
    fresh = call(run, "annotations_pull", operation_id="pull-2")
    assert [m["body"] for m in fresh["messages"]] == ["b"]


def test_pull_answers_the_notice_it_left_and_a_replay_answers_the_same(cell, live, monkeypatch):
    """The pull ack carries the context's rev, unread and interrupt_seq as
    the pull left them (one transaction), so the run knows which interrupts
    its batch answers; a replay answers the remembered receipt."""
    run = live.start(cell)
    monkeypatch.setattr(run, "_flush_chat_files_to_workspace", lambda include_tip: None)
    monkeypatch.setattr(store, "PULL_MAX_MESSAGES", 2)
    user_post(run, "a")
    user_post(run, "b")
    user_post(run, "stop", mode="interrupt")
    ack = call(run, "annotations_pull", operation_id="pull-1")
    after = context_of(run)
    assert [m["body"] for m in ack["messages"]] == ["stop", "a"]
    assert (ack["rev"], ack["unread"], ack["interrupt_seq"]) == (after["rev"], 1, 1)
    user_post(run, "c")
    again = call(run, "annotations_pull", operation_id="pull-1")
    assert {k: again[k] for k in ("messages", "rev", "unread", "interrupt_seq")} == {
        k: ack[k] for k in ("messages", "rev", "unread", "interrupt_seq")}
    empty_run = live.start(cell)
    monkeypatch.setattr(empty_run, "_flush_chat_files_to_workspace", lambda include_tip: None)
    nothing = call(empty_run, "annotations_pull", operation_id="pull-2")
    assert nothing["messages"] == [] and (nothing["unread"], nothing["interrupt_seq"]) == (0, 0)
    assert nothing["rev"] == context_of(empty_run)["rev"]


def test_pull_receipts_keep_the_newest_64(cell, live, monkeypatch):
    from process_manager.ipc import ANNOTATION_PULL_RECEIPTS
    run = live.start(cell)
    monkeypatch.setattr(run, "_flush_chat_files_to_workspace", lambda include_tip: None)
    for i in range(ANNOTATION_PULL_RECEIPTS + 3):
        call(run, "annotations_pull", operation_id=f"pull-{i}")
    assert len(run._annotation_pulls) == ANNOTATION_PULL_RECEIPTS
    assert next(iter(run._annotation_pulls)) == "pull-3"


def test_pull_replays_a_remembered_operation_after_seal(cell, live, monkeypatch):
    run = live.start(cell)
    monkeypatch.setattr(run, "_flush_chat_files_to_workspace", lambda include_tip: None)
    user_post(run, "a")
    first = call(run, "annotations_pull", operation_id="pull-1")
    run.sealed = True
    assert call(run, "annotations_pull", operation_id="pull-1")["messages"] == first["messages"]
    assert call(run, "annotations_pull", operation_id="pull-2")["error"] == "sealed"


def test_post_is_run_authored_born_read_labelled_and_replays(cell, live, bells):
    run = live.start(cell)
    root = user_post(run, "question")
    message_id = store.mint_id()
    ack = post_op(run, "answer", reply_to=root["id"], op="post-1", message_id=message_id)
    assert ack == {"op": "ack_annotations_post", "req_id": "r", "ok": True, "operation_id": "post-1",
                   "id": message_id}
    reply = context_of(run)["messages"][1]
    assert reply["author"] == "run" and reply["author_label"] == run.label
    assert reply["read_at"] == reply["created_at"] and reply["parent_id"] == root["id"]
    rings = len(bells["channel"])
    assert post_op(run, "answer", reply_to=root["id"], op="post-1", message_id=message_id)["id"] == message_id
    assert len(bells["channel"]) == rings and len(context_of(run)["messages"]) == 2
    assert post_op(run, "changed", op="post-1", message_id=message_id)["error"] == "operation_reused"


def test_post_replays_a_remembered_operation_after_seal(cell, live):
    run = live.start(cell)
    message_id = store.mint_id()
    assert post_op(run, "note", op="post-1", message_id=message_id)["ok"]
    run.sealed = True
    assert post_op(run, "note", op="post-1", message_id=message_id)["id"] == message_id
    assert post_op(run, "late")["error"] == "sealed"


def test_post_reply_to_a_reply_is_reply_depth_and_unknown_is_unknown_message(cell, live):
    run = live.start(cell)
    root = user_post(run, "q")
    reply = user_post(run, "r", parent=root["id"])
    assert post_op(run, "deep", reply_to=reply["id"])["error"] == "reply_depth"
    assert post_op(run, "lost", reply_to="a_ffffffffffffffff")["error"] == "unknown_message"
    assert post_op(run, "bad", message_id="a_bad")["error"] == "invalid"
    assert call(run, "annotations_post", message_id=store.mint_id(), body="no op id")["error"] == "invalid"


def test_post_body_limits_empty_text_too_large(cell, live):
    run = live.start(cell)
    assert post_op(run, "   ")["error"] == "empty_text"
    assert post_op(run, "x" * (64 * 1024 + 1))["error"] == "too_large"


def test_list_returns_the_runs_context_without_held_messages(cell, live):
    from database import get_db_connection
    run = live.start(cell)
    user_post(run, "seen")
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("BEGIN IMMEDIATE")
        store.set_bar(conn, run.cell_id, run.run_id, on=True, live={run.run_id})
        conn.commit()
    finally:
        conn.close()
    user_post(run, "held", held=True)
    ack = call(run, "annotations_list")
    assert ack["ok"] and [m["body"] for m in ack["messages"]] == ["seen"]


@pytest.mark.parametrize("ending", ["sealed", "stop_requested", "final_status"])
def test_sealed_stop_requested_or_final_runs_refuse_with_sealed(cell, live, ending):
    run = live.start(cell)
    setattr(run, ending, "completed" if ending == "final_status" else True)
    assert call(run, "annotations_poll")["error"] == "sealed"
    assert call(run, "annotations_pull", operation_id="p")["error"] == "sealed"
    assert post_op(run, "late")["error"] == "sealed"
    assert call(run, "annotations_list")["ok"] is True


def test_a_run_only_touches_its_own_context(cell, live):
    first, second = live.start(cell), live.start(cell)
    theirs = user_post(first, "for the first run")
    assert post_op(second, "cross", reply_to=theirs["id"])["error"] == "unknown_message"
    assert [m["body"] for m in call(second, "annotations_list")["messages"]] == []
    assert call(second, "annotations_poll")["unread"] == 0
