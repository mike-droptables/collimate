# SPDX-License-Identifier: MIT
"""``gen.annotations`` — the run's annotation context — and the turn
interrupt (``gen.session.interrupt_turn``) and reserved log names that come with it.

Pinned here: dev mode and a forwarded child have no context; one poller
serves every subscription and fires ``on_new`` per revision and
``on_interrupt`` first when the interrupt counter advances; a pull takes
interrupt-mode messages first with their replies and marks them read, and a
cancelled pull keeps its batch; a post is validated before it is sent, tagged
with the run's label, born read and one level deep; the wire frames and the
refusal codes; and the harness's prompt seed. Synthetic fixtures only."""
from __future__ import annotations

import asyncio
import re
import threading

import pytest

from collimate import native

from collimate._host import ipc
from collimate._host.bridge import (
    AnnotationsSealed, HostBridge, IPCDisconnectedError, IPCError, IPCNakError,
)
from collimate._results import Annotation, AnnotationNotice, ContextItem, annotation_from_wire
from collimate.testing import make_test_bridge, run_generator_callable
from collimate.testing.forward import FORWARDED_OPS, LOCAL_OPS


@pytest.fixture
def installed(tmp_path):
    """``installed(**kwargs)`` makes a recording bridge and installs it."""
    made: list = []

    def install(**kwargs):
        workspace = tmp_path / f"run{len(made)}"
        workspace.mkdir()
        bridge = make_test_bridge(workspace, **kwargs)
        native.set_bridge(bridge)
        made.append(bridge)
        return bridge

    yield install
    native._clear_bridge()
    native.annotations.reset()
    for bridge in made:
        bridge.close()


def _wait_until(predicate, timeout: float = 3.0):
    async def wait():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                raise AssertionError("condition not reached")
            await asyncio.sleep(0.02)
    return wait()


# ── no context ─────────────────────────────────────────────────────────


def test_a_dev_run_has_no_annotation_context(installed):
    bridge = installed()
    bridge.annotations_available = lambda: False

    async def flow():
        assert native.annotations.available is False
        assert await native.annotations.pull() == []
        assert await native.annotations.list() == []
        with pytest.raises(LookupError):
            await native.annotations.post("x")
        subscription = native.annotations.subscribe(lambda notice: None)
        assert not subscription.closed and native.annotations._poller is None
        subscription.close()

    asyncio.run(flow())
    assert native.annotations.notice == AnnotationNotice(0, 0, 0)
    assert not [c for c in bridge._rec_tier_b if c["op"].startswith("annotations")]


def test_a_forwarded_child_has_no_annotation_context():
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge._sock_wfile = object()      # hosted, as far as the check is concerned
    bridge.ipc_ops = frozenset(FORWARDED_OPS) | LOCAL_OPS
    assert bridge.annotations_available() is False
    bridge.ipc_ops = None
    assert bridge.annotations_available() is True
    bridge.ipc_ops = frozenset(ipc.ANNOTATION_OPS) | {"update_cell"}
    assert bridge.annotations_available() is True
    bridge._sock_wfile = None
    assert bridge.annotations_available() is False


# ── subscribe ──────────────────────────────────────────────────────────


def test_subscribe_runs_one_poller_and_fires_on_new_per_revision(installed):
    bridge = installed(annotations=["one"])
    first, second = [], []

    async def flow():
        a = native.annotations.subscribe(first.append)
        poller = native.annotations._poller
        b = native.annotations.subscribe(second.append)
        assert native.annotations._poller is poller
        await _wait_until(lambda: first and second)
        assert first[0].unread == 1 and second[0].unread == 1
        await asyncio.sleep(0.6)               # the same revision fires once
        assert len(first) == 1
        bridge.add_annotation("two")
        await _wait_until(lambda: len(first) == 2)
        assert first[1].unread == 2
        a.close()
        b.close()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert poller.done()

    asyncio.run(flow())
    assert bridge.annotations_listening is True


def test_on_interrupt_fires_before_on_new_when_interrupt_seq_advances(installed):
    bridge = installed()
    order: list = []

    async def flow():
        with native.annotations.subscribe(lambda n: order.append("new"),
                                          on_interrupt=lambda n: order.append("interrupt")):
            await asyncio.sleep(0.3)
            bridge.add_annotation("stop now", mode="interrupt")
            await _wait_until(lambda: len(order) >= 2)

    asyncio.run(flow())
    assert order[:2] == ["interrupt", "new"]


def test_subscribe_refuses_coroutines_non_callables_and_a_missing_loop(installed):
    installed()

    async def coroutine_callback(notice):  # pragma: no cover — refused
        pass

    async def flow():
        with pytest.raises(TypeError, match="on_new"):
            native.annotations.subscribe(coroutine_callback)
        with pytest.raises(TypeError, match="on_new"):
            native.annotations.subscribe(None)
        with pytest.raises(TypeError, match="on_interrupt"):
            native.annotations.subscribe(lambda n: None, on_interrupt="not callable")
        assert native.annotations._subscriptions == []

    asyncio.run(flow())
    with pytest.raises(RuntimeError, match="event loop"):
        native.annotations.subscribe(lambda n: None)


def test_a_raising_callback_is_logged_and_watching_continues(installed, caplog):
    bridge = installed(annotations=["one"])
    seen: list = []

    def boom(notice):
        raise RuntimeError("callback bug")

    async def flow():
        with native.annotations.subscribe(boom), native.annotations.subscribe(seen.append):
            await _wait_until(lambda: seen)
            bridge.add_annotation("two")
            await _wait_until(lambda: len(seen) == 2)

    with caplog.at_level("ERROR"):
        asyncio.run(flow())
    assert any("on_new callback failed" in r.getMessage() for r in caplog.records)


def test_an_interrupt_a_pull_answered_never_fires_on_interrupt(installed):
    """The pull's notice says which interrupts its batch answers: a poller
    that sees one of those afterwards stays quiet, and a newer one fires."""
    bridge = installed()
    bridge.add_annotation("stop, do this", mode="interrupt")
    fired: list = []

    async def flow():
        [message] = await native.annotations.pull()
        assert message.mode == "interrupt"
        assert native.annotations.pull_notice.interrupt_seq == 1
        with native.annotations.subscribe(lambda n: None, on_interrupt=fired.append):
            await asyncio.sleep(0.6)
            assert fired == []
            bridge.add_annotation("and now this", mode="interrupt")
            await _wait_until(lambda: fired)
            await asyncio.sleep(0.3)

    asyncio.run(flow())
    assert [n.interrupt_seq for n in fired] == [2]


@pytest.mark.parametrize("after_the_pull", [False, True])
def test_no_interrupt_fires_while_a_pull_is_in_flight_and_its_answer_decides(
        installed, after_the_pull):
    """An interrupt the poller sees while a pull is in flight waits for the
    pull: when the pull answered it nothing fires; one that landed after the
    pull fires on the next poll."""
    bridge = installed()
    real_pull = bridge.annotations_pull
    taken, release = threading.Event(), threading.Event()

    def slow_pull():
        if not after_the_pull:
            bridge.add_annotation("stop", mode="interrupt")
        answer = real_pull()
        if after_the_pull:
            bridge.add_annotation("stop", mode="interrupt")
        taken.set()
        release.wait(5)
        return answer

    bridge.annotations_pull = slow_pull
    fired: list = []

    async def flow():
        with native.annotations.subscribe(lambda n: None, on_interrupt=fired.append):
            await asyncio.sleep(0.3)
            task = asyncio.ensure_future(native.annotations.pull())
            await _wait_until(taken.is_set)
            await asyncio.sleep(0.6)       # polls see interrupt_seq 1 meanwhile
            assert fired == []
            release.set()
            batch = await task
            await asyncio.sleep(0.6)
        return batch

    batch = asyncio.run(flow())
    assert [m.text for m in batch] == ([] if after_the_pull else ["stop"])
    assert native.annotations.pull_notice.interrupt_seq == (0 if after_the_pull else 1)
    assert [n.interrupt_seq for n in fired] == ([1] if after_the_pull else [])


def test_a_message_posted_while_a_pull_is_in_flight_stays_unread(installed):
    """A poll that lands between the host's pull and ``pull()`` resuming is
    newer than the pull: its unread message is kept, never zeroed."""
    bridge = installed(annotations=["first"])
    real_pull = bridge.annotations_pull
    taken, release = threading.Event(), threading.Event()

    def slow_pull():
        answer = real_pull()
        bridge.add_annotation("posted meanwhile")
        taken.set()
        release.wait(5)
        return answer

    bridge.annotations_pull = slow_pull
    seen: list = []

    async def flow():
        with native.annotations.subscribe(seen.append):
            await _wait_until(lambda: seen)
            task = asyncio.ensure_future(native.annotations.pull())
            await _wait_until(taken.is_set)
            await _wait_until(lambda: native.annotations.notice.unread == 1
                              and native.annotations.notice.rev > seen[0].rev)
            release.set()
            batch = await task
            await asyncio.sleep(0.3)
        return batch

    batch = asyncio.run(flow())
    assert [m.text for m in batch] == ["first"]
    assert native.annotations.notice.unread == 1
    assert native.annotations.notice.rev > native.annotations.pull_notice.rev
    assert native.annotations.pull_notice.unread == 0


def test_a_poll_answer_older_than_the_last_pull_is_dropped(installed):
    bridge = installed(annotations=["one"])
    stale = AnnotationNotice(rev=1, unread=1, interrupt_seq=0)
    fired: list = []

    async def flow():
        await native.annotations.pull()
        bridge.annotations_poll = lambda: stale
        with native.annotations.subscribe(fired.append):
            await asyncio.sleep(0.6)

    asyncio.run(flow())
    assert fired == []
    assert native.annotations.notice == native.annotations.pull_notice
    assert native.annotations.notice.rev > stale.rev and native.annotations.notice.unread == 0


def test_a_refused_poll_is_logged_once_and_retried(installed, monkeypatch):
    """A host refusal (a NAK, such as a locked database on the first poll's
    listening mark) never ends watching: it is logged once per streak and
    polled again, backing off."""
    monkeypatch.setattr(native, "_ANNOTATIONS_POLL_INTERVAL_S", 0.01)
    bridge = installed(annotations=["one"])
    real_poll = bridge.annotations_poll
    failures = iter([IPCNakError("database is locked", "annotations_poll")] * 3)

    def flaky_poll():
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return real_poll()

    bridge.annotations_poll = flaky_poll
    seen: list = []

    async def flow():
        with native.annotations.subscribe(seen.append):
            await _wait_until(lambda: seen)

    asyncio.run(flow())
    assert seen[0].unread == 1 and native.annotations._stopped is False
    logged = [line for line in bridge._rec_logs if line.startswith("annotations:")]
    assert logged == ["annotations: a poll failed (host NAK for 'annotations_poll': "
                      "database is locked); retrying"]


def test_a_transport_failure_stops_watching_for_good(installed):
    bridge = installed(annotations=["one"])
    polls: list = []

    def gone():
        polls.append(1)
        raise IPCDisconnectedError("the host closed the IPC socket")

    bridge.annotations_poll = gone
    fired: list = []

    async def flow():
        with native.annotations.subscribe(fired.append):
            await asyncio.sleep(0.6)
        assert native.annotations._stopped is True
        with native.annotations.subscribe(fired.append):
            assert native.annotations._poller is None

    asyncio.run(flow())
    assert polls == [1] and fired == []
    assert [line for line in bridge._rec_logs if line.startswith("annotations:")] == [
        "annotations: stopped watching (the host closed the IPC socket)"]


def test_close_then_subscribe_in_the_same_tick_starts_a_new_poller(installed):
    bridge = installed(annotations=["one"])
    first, second = [], []

    async def flow():
        a = native.annotations.subscribe(first.append)
        await _wait_until(lambda: first)
        old = native.annotations._poller
        a.close()
        assert native.annotations._poller is None
        b = native.annotations.subscribe(second.append)
        assert native.annotations._poller is not None and native.annotations._poller is not old
        bridge.add_annotation("two")
        await _wait_until(lambda: second)
        b.close()
        await asyncio.sleep(0)
        assert old.done()

    asyncio.run(flow())
    assert [n.unread for n in second] == [2]


def test_a_restarted_poller_carries_on_from_what_it_saw(installed):
    """A new poller after the last ``close()`` keeps the interrupt counter
    and revision the last one dispatched: an old interrupt does not fire
    again and an unchanged revision is not news."""
    bridge = installed()
    bridge.add_annotation("stop", mode="interrupt")
    interrupts, news = [], []

    async def flow():
        with native.annotations.subscribe(news.append, on_interrupt=interrupts.append):
            await _wait_until(lambda: interrupts and news)
        with native.annotations.subscribe(news.append, on_interrupt=interrupts.append):
            await asyncio.sleep(0.6)
            assert len(interrupts) == 1 and len(news) == 1
            bridge.add_annotation("again", mode="interrupt")
            await _wait_until(lambda: len(interrupts) == 2)

    asyncio.run(flow())
    assert [n.interrupt_seq for n in interrupts] == [1, 2]


# ── pull ───────────────────────────────────────────────────────────────


def test_pull_takes_interrupts_first_with_replies_and_marks_read(installed):
    bridge = installed()
    a = bridge.add_annotation("A")
    b = bridge.add_annotation("B", mode="interrupt")
    r = bridge.add_annotation("r", reply_to=a)

    async def flow():
        batch = await native.annotations.pull()
        again = await native.annotations.pull()
        return batch, again

    batch, again = asyncio.run(flow())
    assert [m.id for m in batch] == [b, a, r]
    assert all(m.read_at for m in batch) and batch[2].reply_to == a
    assert again == []
    [pull] = [c for c in bridge._rec_tier_b if c["op"] == "annotations_pull"]
    assert pull["ids"] == [b, a, r]
    assert native.annotations.notice.unread == 0


def test_a_reply_whose_root_was_read_is_pulled_on_its_own(installed):
    bridge = installed()
    root = bridge.add_annotation("root")

    async def flow():
        await native.annotations.pull()
        reply = bridge.add_annotation("follow-up", reply_to=root)
        return reply, await native.annotations.pull()

    reply, batch = asyncio.run(flow())
    assert [m.id for m in batch] == [reply]


def test_a_cancelled_pull_keeps_its_batch_for_the_next_pull(installed):
    bridge = installed()
    release = threading.Event()
    calls: list = []
    held = Annotation(id="a_" + "3" * 16, text="kept", author="user", author_label=None,
                      kind="message", reply_to=None, mode="queued",
                      created_at="2026-10-24 12:00:00", read_at="2026-10-24 12:00:01")

    def slow_pull():
        calls.append(1)
        release.wait(5)
        return [held], AnnotationNotice(2, 0, 0)

    bridge.annotations_pull = slow_pull

    async def flow():
        task = asyncio.ensure_future(native.annotations.pull())
        await _wait_until(lambda: calls)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await native.annotations.pull()

    assert asyncio.run(flow()) == [held]
    assert len(calls) == 1


# ── post ───────────────────────────────────────────────────────────────


def test_post_validates_text_and_reply_to_before_sending(installed):
    bridge = installed()

    async def flow():
        with pytest.raises(TypeError):
            await native.annotations.post(3)
        for text in ("", "  "):
            with pytest.raises(ValueError, match="non-empty"):
                await native.annotations.post(text)
        with pytest.raises(ValueError, match="64 KiB"):
            await native.annotations.post("x" * (ipc.ANNOTATION_BODY_MAX_BYTES + 1))
        with pytest.raises(ValueError, match="reply_to"):
            await native.annotations.post("ok", reply_to="")

    asyncio.run(flow())
    assert bridge.annotation_posts == []


def test_posts_are_tagged_born_read_and_one_level_deep(installed):
    bridge = installed(runner={"runner": "demo", "name": "Demo"})
    root = bridge.add_annotation("question")

    async def flow():
        top = await native.annotations.post("a note")
        reply = await native.annotations.post("an answer", reply_to=root)
        with pytest.raises(ValueError, match="reply_depth"):
            await native.annotations.post("deeper", reply_to=reply)
        with pytest.raises(LookupError, match="unknown_message"):
            await native.annotations.post("lost", reply_to="a_" + "f" * 16)
        return top, reply, await native.annotations.pull()

    top, reply, batch = asyncio.run(flow())
    assert [m.id for m in batch] == [root]          # the run's posts are never pulled
    posts = {a.id: a for a in bridge.annotation_context() if a.author == "run"}
    assert set(posts) == {top, reply}
    assert all(a.author_label == bridge.label and a.read_at for a in posts.values())
    assert posts[reply].reply_to == root and posts[top].reply_to is None
    assert all(re.fullmatch(ipc.ANNOTATION_ID_PATTERN, i) for i in posts)


def test_list_shows_the_prompt_messages_and_posts_in_context_order(installed):
    installed(prompt="Summarize", annotations=["a"])

    async def flow():
        await native.annotations.post("b")
        return await native.annotations.list()

    messages = asyncio.run(flow())
    assert [(m.kind, m.author, m.text) for m in messages] == [
        ("prompt", "user", "Summarize"), ("message", "user", "a"), ("message", "run", "b")]
    assert messages[0].read_at is not None and messages[1].read_at is None


def test_scripted_context_items_and_refs_reach_the_run():
    async def run():
        import gen

        [message] = await gen.annotations.pull()
        gen.update_file("seen.txt", f"{message.context[0].label}|{message.refs[0]}")

    item = ContextItem(path="site/index.html", renderer_id="html", label="index.html › h1",
                       text="<h1>Hi</h1>", anchor={"css": "h1"})
    res = run_generator_callable(run, annotations=[
        {"text": "fix this", "context": [item], "refs": ["site/index.html"]}])
    assert res.ok, res.error
    assert res.files["seen.txt"] == "index.html › h1|site/index.html"
    assert res.annotations[0].context == (item,)


# ── the wire ───────────────────────────────────────────────────────────


def _message(**changes) -> dict:
    message = {"id": "a_" + "4" * 16, "context_id": "run", "parent_id": None, "author": "user",
               "author_label": None, "kind": "message", "body": "hi",
               "context_items": [{"path": "a.md", "renderer_id": "md", "label": "a.md",
                                  "text": "x", "anchor": None}],
               "refs": ["a.md"], "mode": "queued", "position": 1.0, "held": False,
               "revision": 1, "created_at": "2026-10-24 12:00:00", "edited_at": None,
               "read_at": None}
    message.update(changes)
    return message


def test_annotation_from_wire_reads_the_host_message():
    annotation = annotation_from_wire(_message(parent_id="a_" + "5" * 16))
    assert annotation.text == "hi" and annotation.reply_to == "a_" + "5" * 16
    assert annotation.context == (ContextItem("a.md", "md", "a.md", "x", None),)
    assert annotation.refs == ("a.md",)


@pytest.mark.parametrize("changes", [
    {"author": "bot"}, {"mode": "later"}, {"kind": "note"}, {"context_items": "a.md"},
    {"id": ""}, {"body": None}, {"refs": [1]}, {"parent_id": ""},
    {"context_items": [{"path": "a.md", "renderer_id": "md", "label": "a", "text": "x",
                        "anchor": "css"}]},
])
def test_annotation_from_wire_refuses_malformed(changes):
    with pytest.raises(ValueError):
        annotation_from_wire(_message(**changes))
    missing = _message()
    del missing["id"]
    with pytest.raises(ValueError):
        annotation_from_wire(missing)


def _hosted(tmp_path, answer):
    """A recording bridge whose annotation ops go through HostBridge's real
    wire path, answered by ``answer(payload)``."""
    bridge = make_test_bridge(tmp_path)
    sent: list = []

    def emit_and_wait(payload, wait_op, **kw):
        sent.append((payload, wait_op))
        return answer(payload)

    bridge.emit_and_wait = emit_and_wait
    bridge._annotations_ack = lambda op, **fields: HostBridge._annotations_ack(bridge, op, **fields)
    return bridge, sent


def test_annotation_ops_put_exact_frames_on_the_wire(tmp_path):
    def answer(payload):
        op = payload["op"]
        if op == "annotations_poll":
            return {"ok": True, "rev": 2, "unread": 1, "interrupt_seq": 0, "listening": True}
        if op == "annotations_pull":
            return {"ok": True, "operation_id": payload["operation_id"], "messages": [],
                    "rev": 3, "unread": 0, "interrupt_seq": 0}
        if op == "annotations_post":
            return {"ok": True, "operation_id": payload["operation_id"], "id": payload["message_id"]}
        return {"ok": True, "messages": [_message()]}

    bridge, sent = _hosted(tmp_path, answer)
    try:
        assert bridge.annotations_poll() == AnnotationNotice(2, 1, 0)
        assert bridge.annotations_pull() == ([], AnnotationNotice(3, 0, 0))
        posted = bridge.annotations_post("top")
        bridge.annotations_post("reply", reply_to="a_" + "6" * 16)
        [listed] = bridge.annotations_list()
    finally:
        bridge.close()
    assert sent[0] == ({"op": "annotations_poll"}, "ack_annotations_poll")
    assert set(sent[1][0]) == {"op", "operation_id"} and sent[1][1] == "ack_annotations_pull"
    assert set(sent[2][0]) == {"op", "operation_id", "message_id", "body"}
    assert set(sent[3][0]) == {"op", "operation_id", "message_id", "body", "reply_to"}
    assert sent[2][0]["message_id"] == posted
    assert re.fullmatch(ipc.ANNOTATION_ID_PATTERN, posted)
    assert sent[4] == ({"op": "annotations_list"}, "ack_annotations_list")
    assert listed.text == "hi"


@pytest.mark.parametrize("op, ack, exc", [
    ("pull", {"ok": False, "error": "sealed"}, AnnotationsSealed),
    ("post", {"ok": False, "error": "empty_text"}, ValueError),
    ("post", {"ok": False, "error": "too_large"}, ValueError),
    ("post", {"ok": False, "error": "reply_depth"}, ValueError),
    ("post", {"ok": False, "error": "unknown_message"}, LookupError),
    ("post", {"ok": False, "error": "boom"}, RuntimeError),
    ("pull", {"ok": True, "operation_id": "another", "messages": []}, IPCError),
    ("post", {"ok": True, "operation_id": None, "id": "a_" + "0" * 16}, IPCError),
    ("poll", {"ok": True, "rev": -1, "unread": 0, "interrupt_seq": 0}, IPCError),
    ("poll", {"ok": True, "rev": True, "unread": 0, "interrupt_seq": 0}, IPCError),
    ("list", {"ok": True, "messages": [_message(author="bot")]}, IPCError),
])
def test_annotation_refusals_and_bad_receipts(tmp_path, op, ack, exc):
    bridge, _ = _hosted(tmp_path, lambda payload: dict(ack))
    calls = {"poll": bridge.annotations_poll, "pull": bridge.annotations_pull,
             "post": lambda: bridge.annotations_post("text"), "list": bridge.annotations_list}
    try:
        with pytest.raises(exc):
            calls[op]()
    finally:
        bridge.close()
    assert issubclass(AnnotationsSealed, RuntimeError)


@pytest.mark.parametrize("notice", [{}, {"rev": 1, "unread": 0},
                                    {"rev": 1, "unread": -1, "interrupt_seq": 0},
                                    {"rev": "1", "unread": 0, "interrupt_seq": 0}])
def test_a_pull_receipt_without_a_valid_notice_is_refused(tmp_path, notice):
    bridge, _ = _hosted(tmp_path, lambda payload: {
        "ok": True, "operation_id": payload["operation_id"], "messages": [], **notice})
    try:
        with pytest.raises(IPCError, match="invalid annotations_pull answer"):
            bridge.annotations_pull()
        assert bridge._persistence["pending_annotation_pull"] is not None
    finally:
        bridge.close()


# ── interrupt_turn ─────────────────────────────────────────────────────


def test_interrupt_turn_cancels_the_running_turn_and_is_false_when_idle():
    async def run():
        import gen

        results: list = []

        async def on_turn(turn):
            async def interrupter():
                await asyncio.sleep(0.1)
                results.append(gen.session.interrupt_turn())
                results.append(gen.session.interrupt_turn())      # already cancelled
            asyncio.ensure_future(interrupter())
            for _ in range(50):
                if turn.cancelled:
                    break
                await asyncio.sleep(0.02)
            results.append(turn.cancelled)
            gen.session.request_finish()

        await gen.serve_chat(on_turn, files=False, initial_message="go",
                             pointer_file=gen.run_path("chat/conversation.chat"),
                             threads_dir=gen.run_path("chat"))
        results.append(gen.session.interrupt_turn())              # nothing runs now
        gen.update_file("results.txt", repr(results))

    res = run_generator_callable(run, messages=[], timeout=10)
    assert res.ok, res.error
    assert res.files["results.txt"] == "[True, False, True, False]"


def test_interrupt_turn_marks_a_starting_turn():
    session = native._session
    session.reset()
    try:
        assert native.interrupt_turn() is False
        session.pending.add("starting")
        assert native.interrupt_turn() is True
        assert native.interrupt_turn() is False
        assert session.take_cancel_starting() is True
        assert session.take_cancel_starting() is False
    finally:
        session.reset()


# ── reserved log names ─────────────────────────────────────────────────


def test_reserved_log_names_are_refused_as_logs(installed):
    installed()
    for name in sorted(ipc.RESERVED_LOG_NAMES):
        with pytest.raises(ValueError, match="host's own run log"):
            native.update_file(name, "x", category="log")
        with pytest.raises(ValueError, match="host's own run log"):
            native.track_file(name, category="log")
        native.update_file(name, "x", category="file")
    native.update_file("trace/annotations.json", "x", category="log")
    assert ipc.RESERVED_LOG_NAMES == {"logs.txt", ipc.ANNOTATIONS_LOG}


def test_a_pull_takes_at_most_64_messages_and_leaves_the_rest_unread(installed):
    bridge = installed(annotations=[f"m{n}" for n in range(70)])

    async def flow():
        return await native.annotations.pull(), await native.annotations.pull()

    first, second = asyncio.run(flow())
    assert [m.text for m in first] == [f"m{n}" for n in range(64)]
    assert [m.text for m in second] == [f"m{n}" for n in range(64, 70)]
    assert bridge.annotations_poll().unread == 0
