# SPDX-License-Identifier: MIT
"""The Agent's Annotation surface (``_agent_shared.session.annotation``), its
corrections (``session.corrections``), its tools (``annotation_tools``) and
its skills.

These lock in:

  * a batch turn pulls at its start and renders each message — id, thread,
    author, mode, its context items and the cell files it links (inlined,
    attached or named within the batch's budget); interrupt messages lead,
    then the messages a cut turn left open, then the queued ones;
  * after a turn: the final answer posted under the last thread when the
    turn posted nothing itself; a stopped, interrupted or failed turn keeps
    its batch open (an interrupt resumes at once; a failure posts a note);
    a run Stop leaves the batch read and unanswered;
  * the watcher: a batch only when the run is idle and something new waits;
    outside edits become a note, never a turn; an idle session parks;
  * host-refusal corrections: at most two in a row per path, one per refused
    path of a partial refusal, one for the same refusal reported twice,
    wired for annotation and chat and never for a terminal;
  * the annotate and list_annotations tools, and that only annotation turns
    get them;
  * the one-shot: its prompt message presented, never re-posted, the
    messages posted while it started, no subscription, a blank prompt
    refused, and its final push acknowledged before it finishes (D07);
  * the generator's own skills, staged per surface.

Unit tests substitute ``gen.annotations`` with :class:`FakeAnnotations`; one
end-to-end test runs the real SDK harness (``run_generator_callable``).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("no_machine_harness_probe")


import gen  # noqa: E402
from _agent_shared import annotation_tools, session, workspace_tools  # noqa: E402
from _agent_shared.session import annotation, consts, corrections, entry  # noqa: E402
from _agent_shared.session.selected import SelectedFile  # noqa: E402
from collimate._host.bridge import PartialRefusal  # noqa: E402

from tests.test_agent_entrypoint import (  # noqa: E402
    FakeChatTurn, _NullCM, _PNG, _Result, _base_cfg, _boot_history, _drive_session,
    _oneshot_cfg, _seed_prompt,
)

Sources = consts.Sources


@pytest.fixture(autouse=True)
def _reset(native_bridge):
    yield
    from collimate import native as n
    n._turn_queue.clear()
    n._finish_requested[0] = False
    n._inbox_shows_cards[0] = False
    n.history.persist = True
    n.history.switch_thread("main", threads_dir="chat", load=False)
    annotation_tools.clear()


# ── fixtures ───────────────────────────────────────────────────────────


def msg(mid, text="hello", *, reply_to=None, mode="queued", refs=(), context=(),
        author="user", label=None, kind="message", created_at="2026-10-24 10:00:00"):
    return gen.Annotation(id=mid, text=text, author=author, author_label=label, kind=kind,
                          reply_to=reply_to, mode=mode, created_at=created_at,
                          read_at=None, context=tuple(context), refs=tuple(refs))


class _Sub:
    def __init__(self, on_new, on_interrupt):
        self.on_new, self.on_interrupt = on_new, on_interrupt
        self.closed = False

    def close(self):
        self.closed = True


class FakeAnnotations:
    """``gen.annotations`` as the session uses it, scripted: ``waiting`` is
    what the next pull returns, ``context`` what ``list()`` returns, and
    ``posts`` records every post."""

    def __init__(self, *, available=True):
        self.available = available
        self.notice = gen.AnnotationNotice(rev=0, unread=0, interrupt_seq=0)
        self.pull_notice = gen.AnnotationNotice(rev=0, unread=0, interrupt_seq=0)
        self.waiting: list = []
        self.context: list = []
        self.posts: list = []
        self.subscriptions: list = []
        self.pulls = 0
        self.pull_error = None
        self.post_error = None
        self.post_gate = None

    async def pull(self):
        self.pulls += 1
        if self.pull_error is not None:
            raise self.pull_error
        batch, self.waiting = self.waiting, []
        self.notice = self.pull_notice = dataclasses.replace(
            self.notice, rev=self.notice.rev + bool(batch), unread=0)
        return batch

    async def post(self, text, *, reply_to=None):
        if self.post_error is not None:
            raise self.post_error
        if self.post_gate is not None:
            await self.post_gate()
        mid = f"a_{len(self.posts):016x}"
        self.posts.append((text, reply_to))
        return mid

    async def list(self):
        return list(self.context)

    def subscribe(self, on_new, *, on_interrupt=None):
        sub = _Sub(on_new, on_interrupt)
        self.subscriptions.append(sub)
        return sub

    def waits(self, *messages, rev=1):
        """Messages arrive: the next pull returns them, the notice shows them
        (an interrupt-mode one advances ``interrupt_seq``)."""
        self.waiting += list(messages)
        interrupts = sum(m.mode == "interrupt" for m in messages)
        self.notice = dataclasses.replace(self.notice, rev=rev, unread=len(self.waiting),
                                          interrupt_seq=self.notice.interrupt_seq + interrupts)


@pytest.fixture
def fake(monkeypatch):
    f = FakeAnnotations()
    monkeypatch.setattr(gen, "annotations", f)
    return f


def _session(**over):
    """An interactive annotation run's session with its leg open."""
    st = entry.AgentSession(_base_cfg(surface="annotation", **over))
    asyncio.run(annotation.open_annotations(st))
    return st


def _turn(source=Sources.ANNOTATIONS):
    return SimpleNamespace(source=source)


def _answer(st, text: str) -> None:
    """The turn's engine replied ``text``, as the entrypoint records it."""
    annotation.answered(st, text)


async def _after(st, source=Sources.ANNOTATIONS, *, stopped=False, failed=""):
    await annotation.after_turn(st, _turn(source), stopped=stopped, failed=failed)


# ── a batch: pulled at the turn's start, rendered message by message ──


def test_a_batch_turn_pulls_at_its_start_and_renders_each_message(native_bridge, fake):
    st = _session()
    item = gen.ContextItem(path="page.html", renderer_id="html", label="page.html › h1.title",
                           text="Element h1.title > Q3 results", anchor={"css": "h1.title"})
    fake.waits(msg("a_1", "Make the title blue.", context=[item]),
               msg("a_2", "And add a subtitle."))
    assert fake.pulls == 0
    batch = asyncio.run(annotation.begin_batch(st))
    assert fake.pulls == 1
    text = batch.text
    assert text.startswith("@annotations — 2 message(s) for you.")
    assert ('<message id="a_1" thread="a_1" author="user" mode="queued" '
            'at="2026-10-24 10:00:00">') in text
    assert "Make the title blue." in text
    assert ('<context path="page.html" label="page.html › h1.title" renderer="html">\n'
            "Element h1.title > Q3 results\n</context>") in text
    assert text.index("a_1") < text.index("a_2")
    assert 'annotate(text, reply_to="<thread>")' in text
    assert [m.id for m in st.annotations.flight.messages] == ["a_1", "a_2"]


def test_a_reply_message_names_its_thread_root(native_bridge, fake):
    st = _session()
    fake.waits(msg("a_r", "one more thing", reply_to="a_root"),
               msg("a_x", "from a peer", author="run", label="peer-1234abcd"))
    text = asyncio.run(annotation.begin_batch(st)).text
    assert '<message id="a_r" thread="a_root" author="user"' in text
    assert '<message id="a_x" thread="a_x" author="run peer-1234abcd"' in text


def test_refs_inline_small_text_attach_media_and_name_the_rest(native_bridge, fake, monkeypatch):
    st = _session()
    gen.update_file("notes.md", "the notes")
    gen.update_file("big.md", "x" * (annotation.REF_TEXT_FILE_BYTES + 100))
    gen.update_file("shot.png", _PNG)
    gen.update_file("second.png", _PNG)
    gen.update_file("data.bin", b"\xff\xfe\x00binary")
    native_bridge.add_foreign_run("peer-1234abcd", {"chat/main.json": "{}"})
    monkeypatch.setattr(annotation, "MAX_REF_MEDIA", 1)
    fake.waits(msg("a_1", "see these", refs=[
        "notes.md", "big.md", "shot.png", "second.png", "data.bin",
        "_runs/peer-1234abcd/chat/main.json", "gone.md", "notes.md"]))
    batch = asyncio.run(annotation.begin_batch(st))
    text = batch.text
    assert '<file path="notes.md">\nthe notes\n</file>' in text
    assert (f"[Truncated: the first {annotation.REF_TEXT_FILE_BYTES} of "
            f"{annotation.REF_TEXT_FILE_BYTES + 100} bytes. Read the file for the rest.]") in text
    assert '<file path="shot.png" media="image/png">attached</file>' in text
    assert ('<file path="second.png" media="image/png">attached files are capped; read it '
            "with your file tools</file>") in text
    assert '<file path="data.bin">a binary file of 9 bytes; read it with your file tools</file>' in text
    assert ('<file path="_runs/peer-1234abcd/chat/main.json">a run\'s interface file; '
            "read it with your file tools</file>") in text
    assert '<file path="gone.md">not in the workspace</file>' in text
    assert text.count('<file path="notes.md">') == 2
    assert '<file path="notes.md">(shown above)</file>' in text
    assert [(m["path"], m["media_type"]) for m in batch.media] == [("shot.png", "image/png")]


def test_the_batch_text_budget_is_shared_by_its_messages(native_bridge, fake, monkeypatch):
    st = _session()
    monkeypatch.setattr(annotation, "REF_TEXT_TOTAL_BYTES", 10)
    gen.update_file("a.md", "0123456789")
    gen.update_file("b.md", "more")
    fake.waits(msg("a_1", "first", refs=["a.md"]), msg("a_2", "second", refs=["b.md"]))
    text = asyncio.run(annotation.begin_batch(st)).text
    assert '<file path="a.md">\n0123456789\n</file>' in text
    assert "not inlined: this batch's inline budget is spent" in text


def test_interrupt_messages_lead_the_batch_before_open_and_queued(native_bridge, fake):
    st = _session()
    st.annotations.open = [msg("a_open", "the cut turn's message")]
    # The pull's own order: interrupt-led units first, a root's replies after it.
    fake.waits(msg("a_int", "stop, do this", mode="interrupt"),
               msg("a_int_r", "and this", reply_to="a_int"),
               msg("a_q", "later"))
    asyncio.run(annotation.begin_batch(st))
    assert [m.id for m in st.annotations.flight.messages] == ["a_int", "a_int_r", "a_open", "a_q"]
    assert st.annotations.open == []


def test_an_empty_pull_with_nothing_open_runs_no_turn(native_bridge, fake):
    st = _session()
    assert asyncio.run(annotation.begin_batch(st)) is None
    assert st.annotations.flight is None


def test_a_turn_already_cancelled_before_its_pull_pulls_nothing(native_bridge, fake, monkeypatch):
    """An interrupt that lands while a batch turn is starting cuts it before
    it pulls: nothing is read, no engine turn runs, no batch is in flight,
    and the watcher looks again."""
    calls: list = []

    async def fake_run_agent(**kw):
        calls.append(kw)
        return _Result(text="never")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(annotation, "start", lambda sess: None)
    fake.waits(msg("a_1"))
    sessions: list = []

    def on_start(turn):
        turn.cancelled = True

    def capture(*a, **kw):
        st = real(*a, **kw)
        sessions.append(st)
        return st

    real = entry.AgentSession
    monkeypatch.setattr(entry, "AgentSession", capture)

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        turn = FakeChatTurn(Sources.ANNOTATIONS)
        turn.source = Sources.ANNOTATIONS
        on_start(turn)
        await on_turn(turn)

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda **kw: _NullCM())
    asyncio.run(entry.run_session(_base_cfg(surface="annotation")))
    assert fake.pulls == 0 and calls == []
    leg = sessions[0].annotations
    assert leg.flight is None and leg.wake.is_set()


# ── after a turn ───────────────────────────────────────────────────────


def test_a_completed_turn_without_annotate_posts_its_final_text_under_the_last_thread(
        native_bridge, fake):
    st = _session()
    fake.waits(msg("a_1"), msg("a_2"), msg("a_2r", reply_to="a_2"))
    asyncio.run(annotation.begin_batch(st))
    _answer(st, "Done: the title is blue.")
    asyncio.run(_after(st))
    assert fake.posts == [("Done: the title is blue.", "a_2")]
    assert st.annotations.flight is None


def test_a_turn_that_called_annotate_posts_nothing_more(native_bridge, fake):
    st = _session()
    fake.waits(msg("a_1"))
    asyncio.run(annotation.begin_batch(st))
    assert asyncio.run(annotation_tools._ws_annotate("On it.", reply_to="a_1")).startswith("posted ")
    _answer(st, "All done.")
    asyncio.run(_after(st))
    assert fake.posts == [("On it.", "a_1")]


def test_an_empty_final_text_posts_nothing(native_bridge, fake):
    st = _session()
    fake.waits(msg("a_1"))
    asyncio.run(annotation.begin_batch(st))
    _answer(st, consts.EMPTY_REPLY)
    asyncio.run(_after(st))
    assert fake.posts == []


def test_a_long_final_text_is_capped_when_posted(native_bridge, fake):
    st = _session()
    fake.waits(msg("a_1"))
    asyncio.run(annotation.begin_batch(st))
    _answer(st, "y" * (annotation.POST_CAP_BYTES + 500))
    asyncio.run(_after(st))
    ((text, reply_to),) = fake.posts
    assert reply_to == "a_1"
    assert text.endswith("\n\n[truncated]")
    assert len(text.encode()) <= annotation.POST_CAP_BYTES + len("\n\n[truncated]")


def test_a_stopped_turn_keeps_its_batch_open_with_the_cut_short_note_and_no_resume(
        native_bridge, fake):
    st = _session()
    fake.waits(msg("a_1"))
    asyncio.run(annotation.begin_batch(st))
    asyncio.run(_after(st, stopped=True))
    leg = st.annotations
    assert [m.id for m in leg.open] == ["a_1"]
    assert leg.cut_short == "stopped" and leg.resume is False
    assert fake.posts == []
    fake.waits(msg("a_2"), rev=2)
    text = asyncio.run(annotation.begin_batch(st)).text
    assert text.splitlines()[2].startswith("Your previous turn was stopped before it finished.")
    assert [m.id for m in leg.flight.messages] == ["a_1", "a_2"]


def test_an_interrupt_cancels_the_running_turn_and_its_batch_resumes_at_once(
        native_bridge, fake, monkeypatch):
    st = _session()
    cut: list = []
    monkeypatch.setattr(gen.session, "interrupt_turn", lambda: cut.append(1) or True)
    fake.waits(msg("a_1"))
    asyncio.run(annotation.begin_batch(st))
    leg = st.annotations
    flight = leg.flight
    fake.waits(msg("a_int", "stop, do this", mode="interrupt"), rev=3)
    annotation._on_interrupt(st, fake.notice)
    assert cut == [1] and flight.interrupted is True and leg.wake.is_set()
    asyncio.run(_after(st, stopped=True))
    assert leg.resume is True and leg.cut_short == "interrupted by a newer message"
    assert leg.flight is None
    # The watcher resubmits at once.
    submitted = _watch_once(st, monkeypatch)
    assert submitted == [(annotation.BATCH_MARK, Sources.ANNOTATIONS)]
    text = asyncio.run(annotation.begin_batch(st)).text
    assert "interrupted by a newer message before it finished" in text
    assert [m.id for m in leg.flight.messages] == ["a_int", "a_1"]
    assert leg.pulled_seq == 1 and leg.flight.interrupted is False


def test_an_interrupt_with_no_turn_running_cuts_nothing(native_bridge, fake, monkeypatch):
    st = _session()
    monkeypatch.setattr(gen.session, "interrupt_turn", lambda: False)
    annotation._on_interrupt(st, gen.AnnotationNotice(rev=1, unread=1, interrupt_seq=1))
    assert st.annotations.flight is None and st.annotations.wake.is_set()


def test_a_failed_turn_posts_a_failure_note_and_keeps_its_batch_open_without_resuming(
        native_bridge, fake):
    st = _session()
    fake.waits(msg("a_1"))
    asyncio.run(annotation.begin_batch(st))
    asyncio.run(_after(st, failed="Connection error."))
    leg = st.annotations
    assert [m.id for m in leg.open] == ["a_1"] and leg.resume is False
    assert leg.cut_short == "cut short: Connection error."
    assert fake.posts == [("_(This turn failed: Connection error. These messages come back "
                           "with your next message.)_", "a_1")]


def test_a_run_stop_leaves_the_batch_read_and_unanswered(native_bridge, fake):
    st = _session()
    fake.waits(msg("a_1"))
    asyncio.run(annotation.begin_batch(st))
    native_bridge._signal_stop()
    _answer(st, "half")
    asyncio.run(_after(st, stopped=True))
    assert fake.posts == [] and st.annotations.open == []


def test_a_post_that_fails_never_fails_the_turn(native_bridge, fake):
    st = _session()
    fake.waits(msg("a_1"))
    asyncio.run(annotation.begin_batch(st))
    fake.post_error = RuntimeError("sealed")
    _answer(st, "done")
    asyncio.run(_after(st))      # logged, not raised
    assert fake.posts == []


# ── the watcher ────────────────────────────────────────────────────────


def _watch_once(st, monkeypatch, *, busy=False, ticks=3) -> list:
    """Run the watcher for a few short polls; return what it submitted."""
    submitted: list = []
    monkeypatch.setattr(annotation, "POLL_S", 0.01)
    # A submitted turn keeps the session busy, as serve_chat's queue does.
    monkeypatch.setattr(gen.session, "chat_busy", lambda: busy or bool(submitted))
    monkeypatch.setattr(gen.session, "submit_turn",
                        lambda text, *, source="", thread_id="": submitted.append((text, source)))

    async def run():
        # The doorbell belongs to the loop the watcher runs on (each call is
        # its own asyncio.run).
        rung = st.annotations.wake.is_set()
        st.annotations.wake = asyncio.Event()
        if rung:
            st.annotations.wake.set()
        task = asyncio.ensure_future(annotation.watcher(st))
        await asyncio.sleep(0.01 * ticks + 0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    return submitted


def test_the_watcher_submits_only_when_idle(native_bridge, fake, monkeypatch):
    st = _session()
    fake.waits(msg("a_1"))
    assert _watch_once(st, monkeypatch, busy=True) == []
    assert _watch_once(st, monkeypatch, busy=False, ticks=1) == [
        (annotation.BATCH_MARK, Sources.ANNOTATIONS)]


def test_the_watcher_waits_while_the_run_is_finishing(native_bridge, fake, monkeypatch):
    st = _session()
    fake.waits(msg("a_1"))
    st.finish_requested = True
    assert _watch_once(st, monkeypatch) == []


def test_a_failed_pull_leaves_its_messages_for_a_retry_after_a_backoff(
        native_bridge, fake, monkeypatch):
    """A pull the host refused (a locked database) read nothing: the
    messages stay unread, the watcher waits out the backoff and the next
    batch pulls them."""
    st = _session()
    leg = st.annotations
    fake.waits(msg("a_1"), rev=5)
    fake.pull_error = RuntimeError("annotations_pull refused: database is locked")
    assert asyncio.run(annotation.begin_batch(st)) is None
    assert leg.pull_failures == 1 and leg.retry_at > 0 and leg.flight is None
    assert fake.notice.unread == 1
    assert _watch_once(st, monkeypatch) == []          # within the backoff
    leg.retry_at = 0.0
    assert _watch_once(st, monkeypatch, ticks=1) == [(annotation.BATCH_MARK, Sources.ANNOTATIONS)]
    fake.pull_error = None
    asyncio.run(annotation.begin_batch(st))
    assert [m.id for m in leg.flight.messages] == ["a_1"] and leg.pull_failures == 0


def test_failed_pulls_back_off_doubling_up_to_the_cap(native_bridge, fake, monkeypatch):
    import time as _time

    monkeypatch.setattr(annotation, "PULL_RETRY_S", 1.0)
    monkeypatch.setattr(annotation, "PULL_RETRY_MAX_S", 4.0)
    st = _session()
    fake.waits(msg("a_1"))
    fake.pull_error = RuntimeError("busy")
    waits = []
    for _ in range(4):
        asyncio.run(annotation.begin_batch(st))
        waits.append(round(st.annotations.retry_at - _time.monotonic()))
    assert waits == [1, 2, 4, 4]


def test_the_watcher_starts_a_batch_whenever_unread_messages_wait(native_bridge, fake,
                                                                   monkeypatch):
    """The SDK's notice never goes back to an older revision, so unread
    messages on it are waiting: a capped pull's remainder starts the next
    batch, and nothing waiting starts none."""
    st = _session()
    fake.waits(msg("a_1"), rev=5)
    asyncio.run(annotation.begin_batch(st))
    assert _watch_once(st, monkeypatch) == []
    fake.notice = dataclasses.replace(fake.notice, rev=6, unread=6)
    assert _watch_once(st, monkeypatch, ticks=1) == [(annotation.BATCH_MARK, Sources.ANNOTATIONS)]


def test_an_interrupt_the_last_pull_answered_cuts_nothing(native_bridge, fake, monkeypatch):
    """An interrupt that landed just before the pull is in the batch the
    turn is answering: its notice cuts nothing. A newer one cuts."""
    st = _session()
    cut: list = []
    monkeypatch.setattr(gen.session, "interrupt_turn", lambda: cut.append(1) or True)
    fake.waits(msg("a_int", "stop", mode="interrupt"), msg("a_q", "later"), rev=2)
    asyncio.run(annotation.begin_batch(st))
    leg = st.annotations
    assert leg.pulled_seq == 1
    annotation._on_interrupt(st, gen.AnnotationNotice(rev=3, unread=0, interrupt_seq=1))
    assert cut == [] and leg.flight.interrupted is False
    annotation._on_interrupt(st, gen.AnnotationNotice(rev=4, unread=1, interrupt_seq=2))
    assert cut == [1] and leg.flight.interrupted is True


def test_an_interrupt_while_the_answer_is_posted_does_not_leak_into_the_next_batch(
        native_bridge, fake, monkeypatch):
    """The cut belongs to the batch in flight: an interrupt that arrives
    once ``after_turn`` took it (while it posts the answer) marks nothing, so
    a later plain stop reads as a stop and does not resume."""
    st = _session()
    monkeypatch.setattr(gen.session, "interrupt_turn", lambda: True)
    fake.waits(msg("a_1"))
    asyncio.run(annotation.begin_batch(st))
    _answer(st, "done")

    async def interrupt_meanwhile():
        annotation._on_interrupt(st, gen.AnnotationNotice(rev=9, unread=1, interrupt_seq=1))

    fake.post_gate = interrupt_meanwhile
    asyncio.run(_after(st))
    fake.post_gate = None
    assert fake.posts == [("done", "a_1")] and st.annotations.flight is None
    fake.waits(msg("a_2"), rev=10)
    asyncio.run(annotation.begin_batch(st))
    asyncio.run(_after(st, stopped=True))
    leg = st.annotations
    assert leg.cut_short == "stopped" and leg.resume is False


def test_outside_edits_fold_into_the_next_batch_as_a_note_and_never_start_a_turn(
        native_bridge, fake, monkeypatch):
    st = _session()
    annotation.changed(st, "_runs/peer-1234abcd/chat/main.json")
    annotation.changed(st, ".collrun/x")
    for i in range(annotation.CHANGED_PATHS_MAX + 5):
        annotation.changed(st, f"f{i:03d}.md")
    assert _watch_once(st, monkeypatch) == []
    fake.waits(msg("a_1"))
    text = asyncio.run(annotation.begin_batch(st)).text
    note = next(line for line in text.splitlines() if line.startswith("Files changed outside"))
    assert "f000.md" in note and f"f{annotation.CHANGED_PATHS_MAX - 1:03d}.md" in note
    assert "and 5 more" in note and "_runs/" not in note and ".collrun" not in note
    assert st.annotations.changed == {}
    fake.waits(msg("a_2"), rev=2)
    assert "Files changed outside" not in asyncio.run(annotation.begin_batch(st)).text


def test_an_idle_session_parks_its_acp_sessions_after_15_minutes(native_bridge, fake, monkeypatch):
    import _agent_shared.acp_run as acp_run
    import _agent_shared.cli_container as cli_container

    assert annotation.IDLE_PARK_S == 15 * 60
    order: list = []

    async def close_sessions():
        order.append("sessions")

    monkeypatch.setattr(acp_run, "close_acp_sessions", close_sessions)
    monkeypatch.setattr(cli_container, "close_all", lambda: order.append("close_all"))
    monkeypatch.setattr(annotation, "IDLE_PARK_S", 0)
    st = _session()
    _watch_once(st, monkeypatch, ticks=4)
    assert order == ["sessions", "close_all"] and st.annotations.parked is True
    asyncio.run(_after(st, Sources.CORRECTION))
    assert st.annotations.parked is False


# ── host-refusal corrections ───────────────────────────────────────────


@pytest.fixture
def queued(monkeypatch) -> list:
    out: list = []
    monkeypatch.setattr(gen.session, "submit_turn",
                        lambda text, *, source="", thread_id="": out.append((text, source)))
    return out


def test_host_refusals_queue_at_most_two_corrections_per_path(native_bridge, queued):
    c = corrections.Corrections()
    gen.update_file("a.md", "v1")
    assert c.refused("a.md", "bad v1") is True
    c.after_turn(Sources.CORRECTION, stopped=False, failed="")
    gen.update_file("a.md", "v2")
    assert c.refused("a.md", "bad v2") is True
    c.after_turn(Sources.CORRECTION, stopped=False, failed="")
    gen.update_file("a.md", "v3")
    assert c.refused("a.md", "bad v3") is False
    assert [source for _, source in queued] == [Sources.CORRECTION] * 2
    assert "The host refused your write to a.md: bad v1" in queued[0][0]
    assert "validate_file" in queued[0][0]
    # A completed turn with nothing pending clears the count.
    c.after_turn(Sources.ANNOTATIONS, stopped=False, failed="")
    gen.update_file("a.md", "v4")
    assert c.refused("a.md", "bad v4") is True


def test_a_partial_refusal_queues_one_correction_per_refused_path(native_bridge, queued):
    c = corrections.Corrections(oneshot=True)
    exc = PartialRefusal("2 refused", paths=["a.md", "b.md"],
                         refused=[{"path": "a.md", "error": "a is bad"},
                                  {"path": "b.md", "error": "b is bad"}])
    assert c.refused_exc(exc) is True
    assert [text.splitlines()[0] for text, _ in queued] == [
        "The host refused your write to a.md: a is bad",
        "The host refused your write to b.md: b is bad"]
    assert all(text.endswith("The run retries its final push when this correction ends.")
               for text, _ in queued)
    assert c.pending
    c.after_turn(Sources.CORRECTION, stopped=False, failed="")
    assert c.pending
    c.after_turn(Sources.CORRECTION, stopped=False, failed="")
    assert not c.pending
    assert c.refused_exc(PartialRefusal("refused")) is True
    assert queued[-1][0].startswith("The host refused a write of your last turn: refused")


def test_the_same_refusal_reported_twice_queues_one_correction(native_bridge, queued):
    c = corrections.Corrections()
    gen.update_file("a.md", "same bytes")
    assert c.refused("a.md", "bad") is True
    assert c.refused("a.md", "bad") is False
    assert len(queued) == 1


def test_a_stopping_run_queues_no_correction(native_bridge, queued):
    native_bridge._signal_stop()
    assert corrections.Corrections().refused("a.md", "bad") is False
    assert queued == []


@pytest.mark.parametrize("surface, wired", [("annotation", True), ("chat", True),
                                            ("terminal", False)])
def test_corrections_are_wired_for_chat_and_never_for_terminal(native_bridge, monkeypatch,
                                                               surface, wired):
    seen: dict = {}

    class _Mirror(_NullCM):
        def __init__(self, kw):
            seen.update(kw)

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)

    async def no_tty(sess):
        return None

    monkeypatch.setattr(entry.gen, "live_files", lambda **kw: _Mirror(kw))
    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(annotation, "start", lambda sess: None)
    monkeypatch.setattr(session.terminal, "run_terminal", no_tty)
    asyncio.run(entry.run_session(_base_cfg(surface=surface, backend="codex")))
    assert (seen["on_refused"] is not None) is wired
    assert (seen["on_changed"] is not None) is (surface == "annotation")


# ── the annotate and list_annotations tools ───────────────────────────


def test_the_annotate_tool_posts_and_re_roots_a_reply_id_to_its_thread(native_bridge, fake):
    annotation_tools.note_messages([msg("a_root"), msg("a_reply", reply_to="a_root")])
    out = asyncio.run(annotation_tools._ws_annotate("Fixed.", reply_to="a_reply"))
    assert out.startswith("posted ") and out.endswith(" in thread a_root")
    assert asyncio.run(annotation_tools._ws_annotate("A note.")).startswith("posted ")
    assert fake.posts == [("Fixed.", "a_root"), ("A note.", None)]
    assert asyncio.run(annotation_tools._ws_annotate("  ")) == "ERROR: annotate needs non-empty text"
    big = asyncio.run(annotation_tools._ws_annotate("z" * (64 * 1024 + 1)))
    assert big.startswith("ERROR: the message is over 64 KiB")
    fake.post_error = LookupError("unknown_message")
    assert asyncio.run(annotation_tools._ws_annotate("x", reply_to="a_gone")) == \
        "ERROR: unknown_message"
    assert len(annotation_tools.posted_since(0)) == 2


def test_list_annotations_returns_the_context_as_capped_json(native_bridge, fake):
    item = gen.ContextItem(path="page.html", renderer_id="html", label="h1", text="t" * 5000)
    fake.context = [msg("a_p", "the task", kind="prompt"),
                    msg("a_1", "long " * 2000, context=[item], refs=["notes.md"]),
                    msg("a_2", "a reply", reply_to="a_1", author="run", label="me-1234abcd")]
    out = json.loads(asyncio.run(annotation_tools._ws_list_annotations()))
    assert out["omitted"] == 0
    ids = [m["id"] for m in out["messages"]]
    assert ids == ["a_p", "a_1", "a_2"]
    first, second, third = out["messages"]
    assert first["kind"] == "prompt" and first["thread"] == "a_p"
    assert second["text"].endswith("…[truncated]")
    assert len(second["text"].encode()) <= 4 * 1024 + len("…[truncated]".encode())
    assert second["context"][0]["text"].endswith("…[truncated]")
    assert second["refs"] == ["notes.md"]
    assert third == {**third, "thread": "a_1", "author": "run", "author_label": "me-1234abcd"}
    # It also learns the threads, for a reply to a reply.
    asyncio.run(annotation_tools._ws_annotate("ok", reply_to="a_2"))
    assert fake.posts[-1] == ("ok", "a_1")
    fake.context = [msg(f"a_{i}", "m") for i in range(150)]
    out = json.loads(asyncio.run(annotation_tools._ws_list_annotations()))
    assert out["omitted"] == 50 and out["messages"][0]["id"] == "a_50"


def test_annotation_tools_ride_only_annotation_turns(native_bridge, monkeypatch):
    names = lambda **kw: [row[0] for row in workspace_tools.tool_entries(  # noqa: E731
        enable_file_tools=False, websearch=False, **kw)]
    assert {"annotate", "list_annotations"} <= set(names(annotation_tools=True))
    assert not {"annotate", "list_annotations"} & set(names())
    rows = {r[0]: r for r in workspace_tools.tool_entries(
        enable_file_tools=True, websearch=False, annotation_tools=True)}
    assert rows["annotate"][1:3] == ("annotation", True)
    # run_agent threads the flag to the ACP turn…
    import _agent_shared as eng
    import _agent_shared.acp_run as acp_run
    seen: list = []

    async def run_acp_turn(**kw):
        seen.append(("turn", kw["annotation_tools"]))
        return "ok", [], {}

    with monkeypatch.context() as patched:
        patched.setattr(acp_run, "run_acp_turn", run_acp_turn)
        for flag in (True, False):
            asyncio.run(eng.run_agent(prompt="hi", annotation_tools=flag))
    # …and the ACP session's tool server.

    async def open_session(**kw):
        seen.append(("acp", kw["annotation_tools"]))
        raise RuntimeError("stop here")

    monkeypatch.setattr(acp_run, "_open_session", open_session)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(acp_run.run_acp_turn(
            backend="codex", prompt="hi", auth="api_key", provider="openai", model="m",
            system_prompt="", mcp_decls=[], stream=False, emit=lambda ev: None,
            annotation_tools=True))
    assert seen == [("turn", True), ("turn", False), ("acp", True)]


def test_a_direct_message_on_an_annotation_run_runs_no_turn(native_bridge, fake, monkeypatch):
    """An annotation run reads its messages, not its inbox: a message sent
    to the inbox directly is logged and skipped."""
    calls: list = []

    async def fake_run_agent(**kw):
        calls.append(kw)
        return _Result(text="no")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(annotation, "start", lambda sess: None)
    turn = FakeChatTurn("hello from the inbox")

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        await on_turn(turn)

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda **kw: _NullCM())
    asyncio.run(entry.run_session(_base_cfg(surface="annotation")))
    assert calls == [] and turn.appended == [] and fake.pulls == 0


def test_an_annotation_run_end_to_end_reads_answers_and_stops(monkeypatch):
    """The real SDK harness: a scripted message waits, the run subscribes,
    one batch turn reads it and its answer is posted in its thread; the run
    then idles and the harness stops it."""
    from collimate.testing import run_generator_callable

    prompts: list = []

    async def fake_run_agent(**kw):
        prompts.append(kw["prompt"])
        gen.update_file("hello.md", "# hello\n")
        return _Result(text="I wrote hello.md.")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)

    async def run():
        await entry.run_session(_base_cfg(surface="annotation"))

    res = run_generator_callable(run, annotations=["Write a hello file."],
                                 ui_files=[".chat", ".json", ".terminal"], timeout=30)
    assert res.ok, res.error
    assert len(prompts) == 1 and "Write a hello file." in prompts[0]
    user, reply = res.annotations
    assert user.author == "user" and user.read_at is not None
    assert (reply.author, reply.reply_to, reply.text) == ("run", user.id, "I wrote hello.md.")
    assert res.files["hello.md"] == "# hello\n"


# ── the one-shot ───────────────────────────────────────────────────────


def test_the_oneshot_lists_its_prompt_message_and_never_reposts_it(native_bridge, monkeypatch):
    prompts: list = []

    async def fake_run_agent(**kw):
        prompts.append(kw["prompt"])
        return _Result(text="the answer")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    prompt_id = _seed_prompt(native_bridge, "Summarize the notes.")
    _drive_session(monkeypatch, _oneshot_cfg())
    (prompt,) = prompts
    assert prompt.startswith("@annotations — the run's opening message (one-shot")
    assert f'<message id="{prompt_id}" thread="{prompt_id}" author="user"' in prompt
    assert "Summarize the notes." in prompt
    assert f'annotate(text, reply_to="{prompt_id}")' in prompt
    assert [p["text"] for p in native_bridge.annotation_posts] == ["the answer"]
    assert all(p["text"] != "Summarize the notes." for p in native_bridge.annotation_posts)


def test_a_oneshot_opening_pulls_messages_posted_while_it_started(native_bridge, monkeypatch):
    prompts: list = []

    async def fake_run_agent(**kw):
        prompts.append(kw["prompt"])
        return _Result(text="done")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _seed_prompt(native_bridge, "Do the task.")
    early = native_bridge.add_annotation("Also check the footer.")
    _drive_session(monkeypatch, _oneshot_cfg())
    (prompt,) = prompts
    assert prompt.index("Do the task.") < prompt.index("Also check the footer.")
    assert f'id="{early}"' in prompt
    assert "Messages the user added while the run was starting follow it" in prompt
    assert all(m.read_at for m in native_bridge.annotation_context())


def test_a_oneshot_never_subscribes(native_bridge, monkeypatch):
    async def fake_run_agent(**kw):
        return _Result(text="done")

    subscribed: list = []
    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(gen.annotations, "subscribe",
                        lambda *a, **k: subscribed.append(1))
    _seed_prompt(native_bridge, "Do the task.")
    st, _, _ = _drive_session(monkeypatch, _oneshot_cfg())
    assert subscribed == [] and native_bridge.annotations_listening is False
    assert st.annotations.watch is None


def test_a_oneshot_without_a_prompt_is_refused_before_work(native_bridge, monkeypatch):
    monkeypatch.setattr(entry.gen, "live_files",
                        lambda **kw: pytest.fail("the mirror opened before the refusal"))
    monkeypatch.setattr(entry.gen, "serve_chat",
                        lambda *a, **kw: pytest.fail("serve_chat before the refusal"))
    native_bridge.prompt = "   "
    with pytest.raises(ValueError, match="a one-shot run needs a prompt"):
        asyncio.run(entry.run_session(_oneshot_cfg()))


def test_a_oneshot_without_a_host_context_answers_its_prompt_top_level(native_bridge,
                                                                        monkeypatch):
    """No annotation context (dev mode): the prompt is presented from
    ``gen.prompt`` with no id, and the answer is posted top level — which
    fails without a context, logged, never the run."""
    f = FakeAnnotations(available=False)
    monkeypatch.setattr(gen, "annotations", f)
    prompts: list = []

    async def fake_run_agent(**kw):
        prompts.append(kw["prompt"])
        return _Result(text="done")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    native_bridge.prompt = "Do the task."
    st, _, _ = _drive_session(monkeypatch, _oneshot_cfg())
    (prompt,) = prompts
    assert '<message author="user" mode="queued">' in prompt and "Do the task." in prompt
    assert "reply with annotate(text)" in prompt
    assert st.finish_requested and f.pulls == 0


# ── the one-shot's final push (D07) ────────────────────────────────────


def _oneshot_session(monkeypatch):
    st = entry.AgentSession(_oneshot_cfg())
    finished, queued, updates = [], [], []
    monkeypatch.setattr(entry.gen.session, "request_finish", lambda: finished.append(True))
    monkeypatch.setattr(entry.gen.session, "submit_turn",
                        lambda text, *, source="", thread_id="": queued.append((text, source)))

    async def update():
        updates.append(True)

    monkeypatch.setattr(entry.gen, "commit", update)
    return st, finished, queued, updates


def test_a_oneshot_stays_open_until_its_final_push_is_acknowledged(native_bridge, monkeypatch):
    st, finished, queued, updates = _oneshot_session(monkeypatch)

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def ack():
            started.set()
            await release.wait()

        st.flush_output = ack
        task = asyncio.create_task(st.on_turn_end(_turn(Sources.OPENING), stopped=False))
        await asyncio.sleep(0)
        assert started.is_set() and not finished
        release.set()
        await task
        assert finished == [True] and not queued and updates == [True]
        assert not st.oneshot_error

    asyncio.run(run())


def test_a_oneshot_final_push_refusal_queues_a_correction_then_finishes(native_bridge,
                                                                        monkeypatch):
    st, finished, queued, _ = _oneshot_session(monkeypatch)
    attempts = 0

    async def ack():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PartialRefusal("candidate/result.md refused", paths=["candidate/result.md"])

    st.flush_output = ack

    async def run():
        await st.on_turn_end(_turn(Sources.OPENING), stopped=False)
        assert not finished and len(queued) == 1
        assert "candidate/result.md" in queued[0][0] and queued[0][1] == Sources.CORRECTION
        await st.on_turn_end(_turn(Sources.CORRECTION), stopped=False)
        assert finished == [True] and not st.oneshot_error

    asyncio.run(run())


def test_a_oneshot_whose_corrections_run_out_ends_failed_and_a_cancelled_push_never_finishes(
        native_bridge, monkeypatch):
    st, finished, queued, _ = _oneshot_session(monkeypatch)
    pushes = 0

    async def refused():
        nonlocal pushes
        pushes += 1
        gen.update_file("blocked.txt", f"try {pushes}")
        raise PartialRefusal("blocked.txt refused", paths=["blocked.txt"])

    st.flush_output = refused

    async def run():
        await st.on_turn_end(_turn(Sources.OPENING), stopped=False)
        assert not finished and len(queued) == 1
        await st.on_turn_end(_turn(Sources.CORRECTION), stopped=False)
        assert not finished and len(queued) == 2
        await st.on_turn_end(_turn(Sources.CORRECTION), stopped=False)
        assert finished == [True] and "blocked.txt" in st.oneshot_error
        assert len(queued) == 2

    asyncio.run(run())

    st, finished, queued, _ = _oneshot_session(monkeypatch)

    async def cancelled():
        raise asyncio.CancelledError()

    st.flush_output = cancelled

    async def stop():
        with pytest.raises(asyncio.CancelledError):
            await st.on_turn_end(_turn(Sources.OPENING), stopped=False)
        assert not queued and not finished

    asyncio.run(stop())


@pytest.mark.parametrize("stopped", [False, True])
def test_a_failed_or_stopped_correction_cannot_finish_the_oneshot_successfully(
        native_bridge, monkeypatch, stopped):
    st, finished, queued, _ = _oneshot_session(monkeypatch)

    async def ack():
        raise PartialRefusal("blocked.txt refused", paths=["blocked.txt"])

    st.flush_output = ack

    async def run():
        await st.on_turn_end(_turn(Sources.OPENING), stopped=False)
        assert len(queued) == 1
        await st.on_turn_end(_turn(Sources.CORRECTION), stopped=stopped,
                             failed="" if stopped else "repair failed")
        assert len(queued) == 1 and finished == [True]
        if stopped:
            assert st.oneshot_stopped
        else:
            assert "blocked.txt" in st.oneshot_error

    asyncio.run(run())


# ── the generator's own skills ────────────────────────────────────────


def _frontmatter(path: str) -> dict:
    import yaml
    text = open(path, encoding="utf-8").read()
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


def test_the_generator_owned_skills_are_annotations_and_llm_summary():
    names = sorted(e for e in os.listdir(session.SKILLS_DIR)
                   if os.path.isfile(os.path.join(session.SKILLS_DIR, e, "SKILL.md")))
    assert names == ["annotations", "llm-summary"]
    for name in names:
        meta = _frontmatter(os.path.join(session.SKILLS_DIR, name, "SKILL.md"))
        assert meta["name"] == name and meta["description"].strip()


def test_an_annotation_run_stages_both_skills_and_chat_and_terminal_stage_llm_summary(
        native_bridge):
    assert session.surface_skills("annotation") == ("annotations", "llm-summary")
    assert session.surface_skills("chat") == session.surface_skills("terminal") == ("llm-summary",)
    import _agent_shared as eng
    staged = eng.stage_builtin_skills(session.SKILLS_DIR, names=session.surface_skills("chat"))
    assert staged == ["llm-summary"]
    root = gen.config_folder_path("_skills")
    assert sorted(os.listdir(root)) == ["llm-summary"]
    assert eng.stage_builtin_skills(session.SKILLS_DIR, names=("annotations", "llm-summary")) == [
        "annotations"]


def test_the_annotations_skill_names_both_tools_and_the_html_answer():
    text = open(os.path.join(session.SKILLS_DIR, "annotations", "SKILL.md"), encoding="utf-8").read()
    assert 'annotate(text, reply_to="<thread>")' in text and "list_annotations()" in text
    assert "`<message>`" in text and "`<context" in text and "`<file" in text
    assert "interrupt" in text and "self-contained `.html`" in text
    assert "no external scripts" in text


def test_the_llm_summary_skill_names_the_type_the_cell_root_and_the_read_side():
    text = open(os.path.join(session.SKILLS_DIR, "llm-summary", "SKILL.md"), encoding="utf-8").read()
    assert "`<name>.llm_summary` at the cell root (never under `_runs/`)" in text
    assert "## Reading one" in text and "read the" in text and "Never delete or" in text
    assert "nothing writes one automatically" in text


def test_the_selected_file_rides_the_first_batch_only(native_bridge, fake):
    st = _session()
    st.selected = SelectedFile("notes.md", 'The user started this run from the selected file "notes.md".')
    st.annotations.selected_pending = True
    fake.waits(msg("a_1"))
    first = asyncio.run(annotation.begin_batch(st)).text
    assert first.splitlines()[2] == st.selected.message
    fake.waits(msg("a_2"), rev=2)
    assert "selected file" not in asyncio.run(annotation.begin_batch(st)).text
