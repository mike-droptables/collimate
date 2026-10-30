"""The annotation store (annotations.py) over a real channel database:
contexts, placement, the queue bar, interrupts, edits, replies, receipts,
send, the run side (notice, pull, post, list) and the seal."""
from __future__ import annotations

import json
import uuid

import pytest

import annotations as store
from annotations import CELL, AnnotationRefused

CELL_ID = "cell-aaaa"
RUN = "11111111-0000-4000-8000-000000000001"
OTHER_RUN = "22222222-0000-4000-8000-000000000002"


@pytest.fixture
def db(fresh_cwd):
    import database
    conn = database.get_db_connection(str(uuid.uuid4()))
    yield conn
    conn.close()


def txn(conn, fn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = fn()
        conn.commit()
        return out
    except BaseException:
        conn.rollback()
        raise


def seed(conn, run_id=RUN, cell_id=CELL_ID, runner="agent", prompt=""):
    def work():
        conn.execute("INSERT INTO runs (id, prompt, status, runner_id) VALUES (?, ?, 'running', ?)",
                     (run_id, prompt, runner))
        store.create_run_context(conn, cell_id, run_id)
        if prompt:
            store.seed_prompt(conn, cell_id, run_id, prompt)
    txn(conn, work)


def post(conn, body="m", *, ctx=RUN, mode="queued", held=False, parent=None, items=(), live=(RUN,),
         op=None, cell_id=CELL_ID):
    message, _bells = post_bells(conn, body, ctx=ctx, mode=mode, held=held, parent=parent, items=items,
                                 live=live, op=op, cell_id=cell_id)
    return message


def post_bells(conn, body="m", *, ctx=RUN, mode="queued", held=False, parent=None, items=(), live=(RUN,),
               op=None, cell_id=CELL_ID):
    op = op or str(uuid.uuid4())
    digest = store.request_digest({"ctx": ctx, "body": body, "mode": mode, "held": held, "parent": parent})
    return txn(conn, lambda: store.post(conn, cell_id, context_id=ctx, body=body, context_items=list(items),
                                        mode=mode, held=held, parent_id=parent, operation_id=op,
                                        digest=digest, live=set(live)))


def refused(code, fn):
    with pytest.raises(AnnotationRefused) as caught:
        fn()
    assert caught.value.code == code, caught.value.code
    return caught.value


def context(conn, ctx=RUN):
    return store.get_context(conn, CELL_ID, ctx)


def bodies(conn, ctx=RUN):
    return [m["body"] for m in context(conn, ctx)["messages"]]


def pull(conn, run_id=RUN):
    return txn(conn, lambda: store.pull(conn, CELL_ID, run_id, live={run_id}))


def run_post(conn, body, *, reply_to=None, message_id=None, op=None, run_id=RUN, label="agent-11111111"):
    message_id = message_id or store.mint_id()
    op = op or str(uuid.uuid4())
    digest = store.request_digest({"message_id": message_id, "body": body, "reply_to": reply_to})
    return txn(conn, lambda: store.run_post(conn, CELL_ID, run_id, label=label, message_id=message_id,
                                            body=body, reply_to=reply_to, operation_id=op, digest=digest,
                                            live={run_id}))


def item(path="page.html", **over):
    return {"path": path, "renderer_id": "html", "label": "page.html › div", "text": "Element div",
            "anchor": {"css": "div"}, **over}


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def test_ids_are_a_underscore_sixteen_hex(db):
    from collimate._host.ipc import ANNOTATION_ID_PATTERN
    seed(db)
    assert store.ID_RE.pattern == ANNOTATION_ID_PATTERN
    assert store.ID_RE.fullmatch(store.mint_id())
    assert store.ID_RE.fullmatch(post(db)["id"])


def test_modes_limits_and_log_names_are_the_sdks():
    from collimate._host.ipc import ANNOTATION_BODY_MAX_BYTES, ANNOTATION_MODES, RESERVED_LOG_NAMES
    from run_artifacts import HOST_LOG_FILES, RUN_ANNOTATIONS_FILE, RUN_LOG_FILE
    assert store.MODES == ("queued", "interrupt") and set(store.MODES) == ANNOTATION_MODES
    assert store.BODY_MAX_BYTES == ANNOTATION_BODY_MAX_BYTES
    assert HOST_LOG_FILES == RESERVED_LOG_NAMES == {RUN_LOG_FILE, RUN_ANNOTATIONS_FILE}


def test_run_context_and_prompt_live_in_the_callers_transaction(db):
    db.execute("BEGIN IMMEDIATE")
    store.create_run_context(db, CELL_ID, RUN)
    store.seed_prompt(db, CELL_ID, RUN, "Do it")
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM annotation_contexts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0


def test_the_prompt_is_born_read_kind_prompt_and_locked(db):
    seed(db, prompt="Summarize [notes](notes.md)")
    (prompt,) = context(db)["messages"]
    assert prompt["kind"] == "prompt" and prompt["author"] == "user"
    assert prompt["read_at"] == prompt["created_at"] and prompt["refs"] == ["notes.md"]
    assert context(db)["rev"] == 0
    refused("locked", lambda: txn(db, lambda: store.patch(db, CELL_ID, prompt["id"], expected_revision=1,
                                                         body="x", live={RUN})))
    refused("locked", lambda: txn(db, lambda: store.delete(db, CELL_ID, prompt["id"], expected_revision=1,
                                                          live={RUN})))
    assert store.notice(db, CELL_ID, RUN)["unread"] == 0


def test_the_cell_context_is_synthesized_on_read_and_created_on_first_write(db):
    before = store.get_context(db, CELL_ID, CELL)
    assert before == {"id": CELL, "cell_id": CELL_ID, "run_id": None, "label": None, "bar_on": False,
                      "listening": False, "interrupt_seq": 0, "rev": 0, "sealed": False, "messages": []}
    assert db.execute("SELECT COUNT(*) FROM annotation_contexts").fetchone()[0] == 0
    post(db, "note", ctx=CELL, live=())
    after = store.get_context(db, CELL_ID, CELL)
    assert after["rev"] == 1 and [m["body"] for m in after["messages"]] == ["note"]
    assert store.get_context(db, CELL_ID, "unknown-run") is None


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

def _bar(conn, on, ctx=RUN):
    return txn(conn, lambda: store.set_bar(conn, CELL_ID, ctx, on=on, live={RUN}))


def test_queued_messages_go_after_unheld_and_before_held(db):
    seed(db)
    _bar(db, True)
    post(db, "q1")
    post(db, "h1", held=True)
    post(db, "q2")
    post(db, "h2", held=True)
    messages = context(db)["messages"]
    assert [m["body"] for m in messages] == ["q1", "q2", "h1", "h2"]
    assert [m["held"] for m in messages] == [False, False, True, True]
    positions = [m["position"] for m in messages]
    assert positions == sorted(positions)


def test_interrupt_goes_before_the_unread_and_after_the_read(db):
    seed(db)
    post(db, "q0")
    pull(db)
    post(db, "q1")
    post(db, "i1", mode="interrupt")
    assert bodies(db) == ["q0", "i1", "q1"]
    post(db, "i2", mode="interrupt")
    assert bodies(db) == ["q0", "i2", "i1", "q1"]


def test_held_needs_the_bar_and_cannot_interrupt(db):
    seed(db)
    refused("bar_off", lambda: post(db, "h", held=True))
    _bar(db, True)
    error = refused("invalid", lambda: post(db, "h", held=True, mode="interrupt"))
    assert error.status == 422


def test_bar_off_unholds_all_keeping_order_and_bumps_revisions(db):
    seed(db)
    _bar(db, True)
    post(db, "q1")
    post(db, "h1", held=True)
    post(db, "h2", held=True)
    wire, bells = _bar(db, False)
    assert len(bells) == 1 and not wire["bar_on"]
    assert [(m["body"], m["held"], m["revision"]) for m in wire["messages"]] == [
        ("q1", False, 1), ("h1", False, 2), ("h2", False, 2)]
    _wire, again = _bar(db, False)
    assert again == []


def test_crowded_positions_renumber_the_context(db):
    seed(db)
    post(db, "read")
    pull(db)
    post(db, "tail")
    for i in range(60):
        post(db, f"i{i}", mode="interrupt")
    messages = context(db)["messages"]
    assert [m["body"] for m in messages] == ["read", *[f"i{i}" for i in reversed(range(60))], "tail"]
    positions = [m["position"] for m in messages]
    assert all(a < b for a, b in zip(positions, positions[1:]))


# ---------------------------------------------------------------------------
# Order and interrupt
# ---------------------------------------------------------------------------

def _order(conn, ids, held_from=None, ctx=RUN):
    return txn(conn, lambda: store.reorder(conn, CELL_ID, ctx, ids=ids, held_from=held_from, live={RUN}))


def test_order_rewrites_unread_positions_after_the_read_and_holds_from_held_from(db):
    seed(db)
    read = post(db, "read")
    pull(db)
    a, b, c = post(db, "a"), post(db, "b"), post(db, "c")
    _bar(db, True)
    wire, bells = _order(db, [c["id"], a["id"], b["id"]], held_from=2)
    assert len(bells) == 1
    assert [(m["body"], m["held"]) for m in wire["messages"]] == [
        ("read", False), ("c", False), ("a", False), ("b", True)]
    by_body = {m["body"]: m for m in wire["messages"]}
    assert by_body["read"]["position"] == read["position"]
    assert by_body["c"]["position"] > read["position"]
    assert by_body["b"]["revision"] == 2 and by_body["c"]["revision"] == 1


def test_order_resets_mode_of_held_messages_to_queued(db):
    seed(db)
    _bar(db, True)
    a = post(db, "a")
    i = post(db, "i", mode="interrupt")
    wire, _ = _order(db, [a["id"], i["id"]], held_from=1)
    held = next(m for m in wire["messages"] if m["id"] == i["id"])
    assert held["held"] and held["mode"] == "queued" and held["revision"] == 2


def test_order_refuses_read_ids_locked_and_mismatched_sets_stale_and_held_from_with_bar_off(db):
    seed(db)
    read = post(db, "read")
    pull(db)
    a, b = post(db, "a"), post(db, "b")
    refused("locked", lambda: _order(db, [read["id"], a["id"], b["id"]]))
    refused("stale", lambda: _order(db, [a["id"]]))
    refused("stale", lambda: _order(db, [a["id"], a["id"], b["id"]]))
    refused("bar_off", lambda: _order(db, [a["id"], b["id"]], held_from=1))
    wire, _ = _order(db, [b["id"], a["id"]])
    assert [m["body"] for m in wire["messages"]] == ["read", "b", "a"]


def _interrupt(conn, ids, ctx=RUN):
    return txn(conn, lambda: store.interrupt(conn, CELL_ID, ctx, ids=ids, live={RUN}))


def test_interrupt_moves_to_head_unholds_sets_mode_and_bumps_interrupt_seq_once(db):
    seed(db)
    _bar(db, True)
    a, b = post(db, "a"), post(db, "b")
    h = post(db, "h", held=True)
    wire, bells = _interrupt(db, [h["id"], b["id"]])
    assert len(bells) == 1 and wire["interrupt_seq"] == 1
    assert [(m["body"], m["mode"], m["held"]) for m in wire["messages"]] == [
        ("h", "interrupt", False), ("b", "interrupt", False), ("a", "queued", False)]
    assert a["id"] == wire["messages"][2]["id"]


def test_interrupt_refuses_replies_not_root_and_read_locked(db):
    seed(db)
    root = post(db, "root")
    reply = post(db, "reply", parent=root["id"])
    refused("not_root", lambda: _interrupt(db, [reply["id"]]))
    pull(db)
    refused("locked", lambda: _interrupt(db, [root["id"]]))
    refused("not_found", lambda: _interrupt(db, ["a_0000000000000000"]))


# ---------------------------------------------------------------------------
# Edit and delete
# ---------------------------------------------------------------------------

def _patch(conn, message, live=(RUN,), **fields):
    revision = fields.pop("revision", message["revision"])
    return txn(conn, lambda: store.patch(conn, CELL_ID, message["id"], expected_revision=revision,
                                         live=set(live), **fields))


def test_patch_checks_revision_lock_and_recomputes_refs_and_edited_at(db):
    seed(db)
    m = post(db, "see [a](a.md)")
    assert m["refs"] == ["a.md"] and m["edited_at"] is None
    edited, bells = _patch(db, m, body="now ![b](img/b.png)", context_items=[item()])
    assert len(bells) == 1
    assert edited["refs"] == ["img/b.png"] and edited["edited_at"] is not None
    assert edited["revision"] == 2 and edited["context_items"] == [item()]
    refused("revision", lambda: _patch(db, m, body="stale"))
    pull(db)
    refused("locked", lambda: _patch(db, edited, body="late"))


def test_patch_to_interrupt_moves_a_root_to_the_head(db):
    seed(db)
    post(db, "a")
    b = post(db, "b")
    wire, _ = _patch(db, b, mode="interrupt")
    assert wire["mode"] == "interrupt" and wire["revision"] == 2
    assert bodies(db) == ["b", "a"] and context(db)["interrupt_seq"] == 1


def test_patch_with_no_field_is_invalid(db):
    seed(db)
    m = post(db, "a")
    refused("invalid", lambda: _patch(db, m))


def _delete(conn, message, revision=None):
    return txn(conn, lambda: store.delete(conn, CELL_ID, message["id"],
                                          expected_revision=revision or message["revision"], live={RUN}))


def test_delete_cascades_unread_replies_and_refuses_when_root_or_any_reply_is_read(db):
    seed(db)
    root = post(db, "root")
    post(db, "reply", parent=root["id"])
    assert len(_delete(db, root)) == 1
    assert bodies(db) == []
    kept = post(db, "kept")
    pull(db)
    late = post(db, "late reply", parent=kept["id"])
    refused("locked", lambda: _delete(db, kept))
    _delete(db, late)
    assert bodies(db) == ["kept"]
    other = post(db, "other")
    reply = run_post(db, "answer", reply_to=other["id"])
    assert reply[0]["read_at"]
    refused("locked", lambda: _delete(db, other))


def test_delete_checks_revision(db):
    seed(db)
    m = post(db, "a")
    refused("revision", lambda: _delete(db, m, revision=5))


# ---------------------------------------------------------------------------
# Replies and receipts
# ---------------------------------------------------------------------------

def test_replies_are_one_level_parent_not_root_unknown_message_and_held_follows_root_on_the_wire(db):
    seed(db)
    _bar(db, True)
    root = post(db, "root", held=True)
    reply = post(db, "reply", parent=root["id"])
    assert reply["held"] is True and reply["position"] == 0.0
    refused("parent_not_root", lambda: post(db, "deep", parent=reply["id"]))
    refused("unknown_message", lambda: post(db, "lost", parent="a_ffffffffffffffff"))
    refused("invalid", lambda: post(db, "held reply", parent=root["id"], held=True))
    seed(db, run_id=OTHER_RUN)
    refused("unknown_message", lambda: post(db, "cross", parent=root["id"], ctx=OTHER_RUN, live=(OTHER_RUN,)))
    messages = context(db)["messages"]
    assert [(m["body"], m["held"]) for m in messages] == [("root", True), ("reply", True)]


def test_post_replays_by_operation_id_and_refuses_reuse_with_other_input(db):
    seed(db)
    first, bells = post_bells(db, "once", op="op-1")
    again, replay_bells = post_bells(db, "once", op="op-1")
    assert again == first and len(bells) == 1 and replay_bells == []
    assert bodies(db) == ["once"]
    refused("operation_reused", lambda: post(db, "different", op="op-1"))
    refused("operation_reused", lambda: post(db, "once", op="op-1", cell_id="cell-other", ctx=CELL, live=()))


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def _send(conn, message, run_id=RUN, live=(RUN,)):
    return txn(conn, lambda: store.send(conn, CELL_ID, message["id"], run_id=run_id, live=set(live)))


def test_send_moves_a_cell_message_and_replies_into_a_live_run_as_last_queued(db):
    seed(db)
    post(db, "existing")
    note = post(db, "note", ctx=CELL, live=())
    post(db, "note reply", ctx=CELL, parent=note["id"], live=())
    moved, bells = _send(db, note)
    assert moved["context_id"] == RUN and moved["revision"] == 2
    assert [(b.context_id) for b in bells] == [CELL, RUN]
    assert bodies(db) == ["existing", "note", "note reply"]
    assert bodies(db, CELL) == []


def test_send_of_an_interrupt_message_goes_first_and_bumps_interrupt_seq(db):
    seed(db)
    post(db, "existing")
    urgent = post(db, "urgent", ctx=CELL, mode="interrupt", live=())
    _send(db, urgent)
    assert bodies(db) == ["urgent", "existing"] and context(db)["interrupt_seq"] == 1


def test_send_refuses_non_cell_messages_replies_and_runs_not_in_live_or_sealed(db):
    seed(db)
    in_run = post(db, "in run")
    refused("not_in_cell_context", lambda: _send(db, in_run))
    note = post(db, "note", ctx=CELL, live=())
    reply = post(db, "reply", ctx=CELL, parent=note["id"], live=())
    refused("parent_not_root", lambda: _send(db, reply))
    refused("run_not_live", lambda: _send(db, note, live=()))
    refused("run_not_live", lambda: _send(db, note, run_id="no-such-run"))
    txn(db, lambda: store.seal(db, CELL_ID, RUN))
    refused("run_not_live", lambda: _send(db, note))


# ---------------------------------------------------------------------------
# Sealing and limits
# ---------------------------------------------------------------------------

def test_run_context_writes_refuse_sealed_when_not_in_live_or_sealed_at_set(db):
    seed(db)
    m = post(db, "a")
    refused("sealed", lambda: post(db, "b", live=()))
    refused("sealed", lambda: _patch(db, m, body="x", live=()))
    txn(db, lambda: store.seal(db, CELL_ID, RUN))
    refused("sealed", lambda: post(db, "c"))
    refused("sealed", lambda: pull(db))
    refused("sealed", lambda: run_post(db, "late"))
    refused("unknown_context", lambda: post(db, "x", ctx="no-such-run"))


def test_body_limits_empty_text_too_large_and_full_context(db):
    seed(db)
    assert refused("empty_text", lambda: post(db, "  \n")).status == 422
    assert refused("too_large", lambda: post(db, "é" * (32 * 1024 + 1))).status == 413
    post(db, "x" * (64 * 1024))
    rows = [(store.mint_id(), CELL_ID, RUN, "user", "m", float(i + 10), "2026-10-24 00:00:00")
            for i in range(store.CONTEXT_MAX_MESSAGES - 1)]
    txn(db, lambda: db.executemany(
        "INSERT INTO annotations (id, cell_id, context_id, author, body, position, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows))
    assert refused("full", lambda: post(db, "one more")).status == 409


def test_context_items_validation_paths_sizes_anchor(db):
    seed(db)
    assert post(db, "ok", items=[item(), item("notes/a.md", anchor=None)])["context_items"][1]["anchor"] is None
    for bad in (
        [item("../escape.md")], [item("/abs.md")], [item("a/./b.md")], [item(".collrun/x")],
        [item(renderer_id="")], [item(label="l" * 257)], [item(text="t" * 16385)],
        [item(anchor=["not", "an", "object"])], [item(anchor={"k": "v" * 4100})],
        [{**item(), "extra": 1}], [item()] * 17,
    ):
        refused("invalid", lambda bad=bad: post(db, "bad", items=bad))
    assert post(db, "label ok", items=[item(label="é" * 256, text="ü" * 16384)])
    assert post(db, "anchor ok", items=[item(anchor={"k": "é" * 4000})])


def test_body_refs_extracts_cell_relative_links_and_images_only():
    body = ("[a](a.md) ![img](img/b.png \"t\") [web](https://x.test/a) [mail](mailto:x@y) "
            "[frag](#top) [abs](/etc/passwd) [up](../out.md) [q](c.md?x=1#h) [sp](d%20e.md) "
            "[again](a.md) [angle](<f g.md>) [reserved](.collrun/x) [dot](./h/../i.md)")
    assert store.body_refs(body) == ["a.md", "img/b.png", "c.md", "d e.md", "i.md"]
    assert store.body_refs("no links") == []


# ---------------------------------------------------------------------------
# Run-side reads and pulls
# ---------------------------------------------------------------------------

def test_notice_counts_only_deliverable_unread_including_replies_to_read_roots(db):
    seed(db, prompt="go")
    _bar(db, True)
    root = post(db, "root")
    post(db, "held", held=True)
    assert store.notice(db, CELL_ID, RUN)["unread"] == 1
    pull(db)
    post(db, "late reply", parent=root["id"])
    run_post(db, "run note")
    notice = store.notice(db, CELL_ID, RUN)
    assert notice["unread"] == 1 and notice["listening"] is False and notice["sealed"] is False
    refused("unknown_context", lambda: store.notice(db, CELL_ID, "no-such-run"))


def test_pull_takes_interrupts_first_then_position_with_unread_replies_and_marks_read(db):
    seed(db)
    q1 = post(db, "q1")
    post(db, "q2")
    post(db, "q1 reply", parent=q1["id"])
    post(db, "i1", mode="interrupt")
    batch, bells = pull(db)
    assert [m["body"] for m in batch] == ["i1", "q1", "q1 reply", "q2"]
    assert all(m["read_at"] for m in batch) and len(bells) == 1
    assert store.notice(db, CELL_ID, RUN)["unread"] == 0
    post(db, "q1 second reply", parent=q1["id"])
    post(db, "q3")
    batch, _ = pull(db)
    assert [m["body"] for m in batch] == ["q1 second reply", "q3"]


def test_pull_never_returns_held_run_authored_or_prompt_messages(db):
    seed(db, prompt="the prompt")
    _bar(db, True)
    held = post(db, "held", held=True)
    post(db, "held reply", parent=held["id"])
    run_post(db, "from the run")
    assert pull(db) == ([], [])
    _bar(db, False)
    batch, _ = pull(db)
    assert [m["body"] for m in batch] == ["held", "held reply"]


def test_pull_is_bounded_by_count_and_bytes_and_leaves_the_rest_unread(db, monkeypatch):
    seed(db)
    for i in range(70):
        post(db, f"m{i:02}")
    first, _ = pull(db)
    assert len(first) == store.PULL_MAX_MESSAGES
    assert store.notice(db, CELL_ID, RUN)["unread"] == 6
    second, _ = pull(db)
    assert [m["body"] for m in second] == [f"m{i:02}" for i in range(64, 70)]
    monkeypatch.setattr(store, "PULL_MAX_BYTES", 10)
    big = post(db, "x" * 50)
    post(db, "small")
    batch, _ = pull(db)
    assert [m["id"] for m in batch] == [big["id"]]
    assert store.notice(db, CELL_ID, RUN)["unread"] == 1


def test_empty_pull_rings_nothing(db):
    seed(db)
    assert pull(db) == ([], [])
    assert context(db)["rev"] == 0


def test_run_post_is_run_authored_born_read_with_label_and_reply_rules(db):
    seed(db)
    root = post(db, "question")
    reply, bells = run_post(db, "answer", reply_to=root["id"])
    assert reply["author"] == "run" and reply["author_label"] == "agent-11111111"
    assert reply["read_at"] == reply["created_at"] and reply["parent_id"] == root["id"]
    assert reply["kind"] == "message" and reply["mode"] == "queued" and len(bells) == 1
    note, _ = run_post(db, "a note of its own")
    refused("parent_not_root", lambda: run_post(db, "deep", reply_to=reply["id"]))
    refused("unknown_message", lambda: run_post(db, "lost", reply_to="a_ffffffffffffffff"))
    _bar(db, True)
    held = post(db, "held", held=True)
    refused("unknown_message", lambda: run_post(db, "unseen", reply_to=held["id"]))
    seed(db, run_id=OTHER_RUN)
    refused("unknown_message", lambda: run_post(db, "cross", reply_to=root["id"], run_id=OTHER_RUN))
    assert note["position"] > root["position"]


def test_run_post_replays_and_refuses_bad_or_duplicate_ids(db):
    seed(db)
    first, bells = run_post(db, "once", message_id="a_0123456789abcdef", op="op-x")
    again, replay = run_post(db, "once", message_id="a_0123456789abcdef", op="op-x")
    assert again == first and replay == [] and len(bells) == 1
    refused("operation_reused", lambda: run_post(db, "other", message_id="a_0123456789abcdef", op="op-x"))
    refused("duplicate_id", lambda: run_post(db, "dup", message_id="a_0123456789abcdef"))
    for bad in ("a_XYZ", "b_0123456789abcdef", "a_0123456789abcdef0", 7):
        refused("invalid", lambda bad=bad: run_post(db, "bad", message_id=bad))
    refused("empty_text", lambda: run_post(db, " "))


def test_list_for_run_excludes_held_roots_and_their_replies(db):
    seed(db, prompt="go")
    _bar(db, True)
    post(db, "seen")
    held = post(db, "held", held=True)
    post(db, "held reply", parent=held["id"])
    assert [m["body"] for m in store.list_for_run(db, CELL_ID, RUN)] == ["go", "seen"]
    txn(db, lambda: store.seal(db, CELL_ID, RUN))
    assert [m["body"] for m in store.list_for_run(db, CELL_ID, RUN)] == ["go", "seen"]
    refused("unknown_context", lambda: store.list_for_run(db, CELL_ID, "no-such-run"))


# ---------------------------------------------------------------------------
# rev, seal, dump
# ---------------------------------------------------------------------------

def test_every_write_bumps_rev_once_per_touched_context(db):
    seed(db)
    _bar(db, True)
    revs = [context(db)["rev"]]

    def step(bells, expected):
        assert [b.context_id for b in bells] == expected
        for bell in bells:
            assert bell.rev == store.get_context(db, CELL_ID, bell.context_id)["rev"]

    m, bells = post_bells(db, "a")
    step(bells, [RUN])
    edited, bells = _patch(db, m, body="b")
    step(bells, [RUN])
    wire, bells = _order(db, [m["id"]], held_from=0)
    step(bells, [RUN])
    wire, bells = _interrupt(db, [m["id"]])
    step(bells, [RUN])
    note, bells = post_bells(db, "n", ctx=CELL, live=())
    step(bells, [CELL])
    moved, bells = _send(db, note)
    step(bells, [CELL, RUN])
    _batch, bells = pull(db)
    step(bells, [RUN])
    _reply, bells = run_post(db, "r")
    step(bells, [RUN])
    revs.append(context(db)["rev"])
    assert revs[1] - revs[0] == 7


def test_mark_listening_flips_once(db):
    seed(db)
    bells = txn(db, lambda: store.mark_listening(db, CELL_ID, RUN))
    assert len(bells) == 1 and context(db)["listening"] is True
    assert txn(db, lambda: store.mark_listening(db, CELL_ID, RUN)) == []
    assert store.listening(db, [RUN, OTHER_RUN]) == {RUN: True}


def test_seal_is_idempotent_and_dump_equals_get_context(db):
    seed(db, prompt="go")
    post(db, "a")
    assert store.unsealed_runs(db) == [RUN] and store.run_context_cell(db, RUN) == CELL_ID
    bells = txn(db, lambda: store.seal(db, CELL_ID, RUN))
    assert len(bells) == 1
    assert txn(db, lambda: store.seal(db, CELL_ID, RUN)) == []
    assert store.unsealed_runs(db) == []
    dumped = json.loads(store.dump(db, CELL_ID, RUN))
    assert dumped == context(db) and dumped["sealed"] is True
    assert dumped["label"] == "agent-11111111"
    assert store.has_messages(db, CELL_ID, RUN)
