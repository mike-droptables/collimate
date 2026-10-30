# SPDX-License-Identifier: MIT
"""The surface primitives: serve_chat's pointer and transcripts in the run
folder (written, revealed, the run's inbox — single-attach with files,
transport-only and optionally broadcast without), the generator-owned turn
queue (``gen.session.submit_turn`` + the turn queue), and ``emit_stream_event``
reaching the inbox.

All host-free against the recording bridge — synthetic fixtures only.
"""
from __future__ import annotations

import asyncio
import json

from collimate.testing import run_generator_callable


def _stream_events(bridge) -> list:
    return list(bridge.stream_events)


def _in_folder(bridge_hook, rel: str) -> str:
    return f"{bridge_hook['bridge'].folder}/{rel}"


# ---------------------------------------------------------------------------
# serve_chat's files: the run folder, the inbox, the reveal
# ---------------------------------------------------------------------------


def test_serve_chat_writes_and_reveals_its_pointer_in_the_run_folder(bridge_hook):
    """The declared pointer is ``<folder>/chat/conversation.chat`` and the
    transcripts live in ``<folder>/chat``; the pointer's session is the run's
    inbox (single-attach, not transport-only), the pointer is revealed once it
    is committed, and nothing is ever pinned."""
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "ok")

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=["hi"], timeout=10)
    assert res.ok, res.error
    pointer = _in_folder(bridge_hook, "chat/conversation.chat")
    assert pointer in res.files and _in_folder(bridge_hook, "chat/main.json") in res.files
    assert json.loads(res.files[pointer])["threads_dir"] == _in_folder(bridge_hook, "chat")
    [reg] = [c for c in res.tier_b_calls if c["op"] == "surface_register"]
    assert (reg["path"], reg["transport_only"], reg["broadcast"]) == (pointer, False, False)
    assert [c for c in res.tier_b_calls if c["op"] == "reveal_file"] == [
        {"op": "reveal_file", "path": pointer, "committed": True}]


def test_serve_chat_takes_paths_inside_the_run_folder(bridge_hook):
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "ok")

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("talk.chat"),
                             threads_dir=gen.run_path("threads"))

    res = run_generator_callable(run, messages=["hi"], timeout=10)
    assert res.ok, res.error
    assert _in_folder(bridge_hook, "talk.chat") in res.files
    assert _in_folder(bridge_hook, "threads/main.json") in res.files


def test_serve_chat_refuses_paths_outside_the_run_folder():
    import pytest

    async def run():
        import gen

        async def on_turn(turn):  # pragma: no cover — never served
            pass

        for kw in ({"pointer_file": "conversation.chat"},
                   {"threads_dir": "chat"},
                   {"pointer_file": "_runs/other-1a2b3c4d/chat/conversation.chat"},
                   {"threads_dir": gen.run_path("x") + "/../../elsewhere"}):
            with pytest.raises(ValueError, match="inside this run's folder"):
                await gen.serve_chat(on_turn, **{
                    "pointer_file": gen.run_path("chat/conversation.chat"),
                    "threads_dir": gen.run_path("chat"), **kw})

    res = run_generator_callable(run, messages=["hi"], timeout=10)
    assert res.ok, res.error
    assert not [c for c in res.tier_b_calls if c["op"] == "surface_register"]


def test_serve_chat_needs_its_ui_file_types_declared():
    import pytest

    async def run():
        import gen

        async def on_turn(turn):  # pragma: no cover — never served
            pass

        with pytest.raises(ValueError, match="ui_files"):
            await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=["hi"], ui_files=[".chat"], timeout=10)
    assert res.ok, res.error
    assert not [c for c in res.tier_b_calls if c["op"] == "surface_register"]


def test_serve_chat_writes_its_pointer_and_transcript_as_ui_files(bridge_hook):
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "hello")

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=["hi"], ui_files=[".chat", ".json"],
                                 timeout=10)
    assert res.ok, res.error
    assert _in_folder(bridge_hook, "chat/conversation.chat") in res.files
    assert _in_folder(bridge_hook, "chat/main.json") in res.files


def test_serve_chat_without_files_needs_no_ui_file_type():
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "hello")

        await gen.serve_chat(on_turn, files=False, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=["hi"], ui_files=[], timeout=10)
    assert res.ok, res.error
    assert not any("_runs/" in p for p in res.files)


def test_a_run_serves_one_inbox(bridge_hook):
    """The inbox is the run's: a second serve_chat in the same run is refused
    by the host (``inbox_exists``)."""
    import pytest

    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "ok")
            gen.session.request_finish()

        await gen.serve_chat(on_turn, files=False, initial_message="a", pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))
        with pytest.raises(RuntimeError, match="inbox_exists"):
            await gen.serve_chat(on_turn, initial_message="b", pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=[], timeout=10)
    assert res.ok, res.error


# ---------------------------------------------------------------------------
# submit_turn + the turn queue + the turn queue (§4.1, §4.3.7)
# ---------------------------------------------------------------------------


def test_submit_turn_queues_behind_the_inflight_turn(bridge_hook):
    """A turn submitted while another streams starts right after it — FIFO,
    never dropped, never cancelled; the handler sees its source label.
    (The recording bridge stops
    the run the moment its scripted queue drains, so delivery is gated by
    hand: the stop is released only after the queued turn ran.)"""
    flags = {"done": False}

    def setup(bridge):
        orig = bridge.chat_poll
        state = {"sent": False}

        def poll():
            if not state["sent"]:
                state["sent"] = True
                return {"kind": "user_msg", "content": "hi"}
            return orig() if flags["done"] else None

        bridge.chat_poll = poll

    bridge_hook["setup"] = setup

    async def run():
        import gen

        seen = []

        async def on_turn(turn):
            seen.append((turn.user_msg, turn.source))
            if turn.user_msg == "hi":
                gen.session.submit_turn("annotation batch", source="@annotations")
            gen.update_file("order.txt", ";".join(m for m, _ in seen))
            gen.update_file("flags.txt", repr(seen))
            if turn.user_msg == "annotation batch":
                flags["done"] = True

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=[], timeout=15)
    assert res.ok, res.error
    assert res.files["order.txt"] == "hi;annotation batch"
    assert res.files["flags.txt"] == repr([
        ("hi", ""), ("annotation batch", "@annotations")])


def test_user_message_defers_behind_a_queued_turn__F1233(bridge_hook):
    """A same-thread user message during a running turn must not
    cancel it: it queues behind it (busy-acknowledged with the queued
    wording) and runs after — the §4.1 rule that makes programmatic turns
    safe against a chatting user."""
    flags = {"in_protected": False, "sent": False, "done": False}

    def setup(bridge):
        orig = bridge.chat_poll

        def gated_poll():
            # Deliver the user's message only once the protected turn is
            # streaming, so it deterministically lands mid-turn; release the
            # bridge's drain-stop only after it ran.
            if flags["in_protected"] and not flags["sent"]:
                flags["sent"] = True
                return {"kind": "user_msg", "content": "user mid-turn",
                        "thread_id": "main"}
            return orig() if flags["done"] else None

        bridge.chat_poll = gated_poll

    bridge_hook["setup"] = setup

    async def run():
        import gen

        seen = []
        cancels = []

        async def on_turn(turn):
            seen.append(turn.user_msg)
            if turn.user_msg == "seed":
                gen.session.submit_turn("protected")
            if turn.user_msg == "protected":
                flags["in_protected"] = True
                # Outlive the gated delivery so the message lands mid-turn.
                for _ in range(60):
                    if flags["sent"]:
                        break
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.2)     # let the loop process it
                cancels.append(turn.cancelled)
            gen.update_file("order.txt", ";".join(seen))
            gen.update_file("cancels.txt", str(cancels))
            if turn.user_msg == "user mid-turn":
                flags["done"] = True

        await gen.serve_chat(on_turn, initial_message="seed", pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=[], timeout=15)
    assert res.ok, res.error
    assert res.files["order.txt"] == "seed;protected;user mid-turn"
    assert res.files["cancels.txt"] == "[False]"     # never cancelled
    busy = [e for e in _stream_events(bridge_hook["bridge"])
            if e["event"].get("type") == "busy"]
    assert busy and busy[0]["event"].get("queued") is True
    assert "queued" in busy[0]["event"]["message"].lower()
    assert busy[0]["event"].get("content") == "user mid-turn"


def test_serve_chat_on_ready_fires_before_the_first_turn(bridge_hook):
    """``on_ready`` marks the boot boundary: after the per-session reset (so
    nothing a companion does can be clobbered), before the seeded first
    turn."""
    async def run():
        import gen

        order = []

        async def on_turn(turn):
            order.append(f"turn:{turn.user_msg}")
            gen.update_file("order.txt", ";".join(order))

        await gen.serve_chat(on_turn, initial_message="seed",
                             on_ready=lambda: order.append("ready"), pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=[], timeout=10)
    assert res.ok, res.error
    assert res.files["order.txt"] == "ready;turn:seed"


# ---------------------------------------------------------------------------
# emit_stream_event: the run's inbox
# ---------------------------------------------------------------------------


def test_emit_stream_event_goes_to_the_inbox(bridge_hook):
    """A run's one live session is its inbox, so a stream event names no
    surface: it is recorded as ``{turn_id, event}``."""
    async def run():
        import gen

        gen.session.emit_stream_event("t1", {"type": "plan", "entries": []})
        gen.session.emit_stream_event("t1", {"type": "text_delta", "text": "x"})

    res = run_generator_callable(run, timeout=10)
    assert res.ok, res.error
    evs = _stream_events(bridge_hook["bridge"])
    assert evs == [{"turn_id": "t1", "event": {"type": "plan", "entries": []}},
                   {"turn_id": "t1", "event": {"type": "text_delta", "text": "x"}}]


# ---------------------------------------------------------------------------
# Approval cards — the answer channel (serve_chat → the waiting gen.ask)
# ---------------------------------------------------------------------------


def test_a_permission_answer_reaches_the_waiting_card_through_serve_chat(bridge_hook):
    """A card's answer arrives as an inbound ``permission_answer`` command on
    the chat session, and serve_chat routes it to the gen.ask waiting on that
    card — never a turn, never gated on busy: the answer is what unblocks the
    in-flight turn."""
    from collimate.testing import UNANSWERED

    sent: list = []

    def setup(bridge):
        orig = bridge.chat_poll

        def poll():
            cards = [r["event"] for r in bridge.stream_events
                     if r["event"].get("type") == "permission_request"]
            if cards and not sent:
                sent.append(cards[0]["id"])
                return {"kind": "permission_answer", "id": cards[0]["id"],
                        "option_id": "allow_once"}
            return orig()

        bridge.chat_poll = poll

    bridge_hook["setup"] = setup

    async def run():
        import gen

        async def on_turn(turn):
            chosen = await gen.ask("Run ls", kind="execute", timeout=10)
            gen.update_file("answer.txt", str(chosen))
            turn.append("assistant", "ok")

        await gen.serve_chat(on_turn, initial_message="go", pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    # The scripted human never answers itself: the answer comes only through
    # the session's command inbox.
    res = run_generator_callable(run, messages=[], approvals=[UNANSWERED], timeout=15)
    assert res.ok, res.error
    assert sent and res.files["answer.txt"] == "allow_once"


def test_queued_turn_for_another_thread_seeds_from_that_thread(bridge_hook):
    """A queued turn bound to a NON-active thread must seed from the TARGET
    thread's transcript — copying the active thread's history would overwrite
    the target's file with it on append (a review-confirmed corruption)."""
    flags = {"sent": False, "done": False}

    def setup(bridge):
        orig = bridge.chat_poll

        def poll():
            if not flags["sent"]:
                flags["sent"] = True
                return {"kind": "user_msg", "content": "hi"}
            return orig() if flags["done"] else None

        bridge.chat_poll = poll

    bridge_hook["setup"] = setup

    async def run():
        import gen
        from collimate.transcript import serialize_transcript
        from collimate.native import Message

        # A pre-existing side thread with its own history.
        gen.update_ui_file("chat/side.json", serialize_transcript([
            Message(role="user", content="side question"),
            Message(role="assistant", content="side answer"),
        ]))

        async def on_turn(turn):
            if turn.user_msg == "hi":
                gen.session.submit_turn("side follow-up", thread_id="side")
            # Record what this turn believed its history was.
            turn.append("assistant", f"seen:{len(turn.messages)}")
            if turn.user_msg == "side follow-up":
                flags["done"] = True

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=[], timeout=15)
    assert res.ok, res.error
    from collimate.transcript import parse_transcript
    side = parse_transcript(res.files[_in_folder(bridge_hook, "chat/side.json")])
    # The side thread kept its own two messages and gained the queued turn's
    # reply — the ACTIVE thread's history never bled in.
    contents = [m["content"] if isinstance(m, dict) else m.content for m in side]
    assert contents[0] == "side question"
    assert contents[1] == "side answer"
    assert any(c.startswith("seen:") for c in contents[2:])
    # And the queued turn saw the SIDE thread's 2 messages, not main's.
    assert "seen:2" in "".join(contents)
    main = parse_transcript(res.files[_in_folder(bridge_hook, "chat/main.json")])
    main_contents = [m["content"] if isinstance(m, dict) else m.content for m in main]
    assert "side question" not in main_contents


def test_a_queued_turn_never_starts_after_serve_chat_exits(bridge_hook):
    """When the poll loop dies by an exception (not Stop / finish), the
    finally cancels the in-flight turn — and its settle callback must NOT
    start the queued turns behind it: nothing polls the inbox any more."""
    flags = {"sent": False, "queued": False}

    def setup(bridge):
        def poll():
            if not flags["sent"]:
                flags["sent"] = True
                return {"kind": "user_msg", "content": "seed"}
            if flags["queued"]:
                raise RuntimeError("inbox gone")
            return None

        bridge.chat_poll = poll

    bridge_hook["setup"] = setup

    async def run():
        import gen
        import pytest

        seen: list = []

        async def on_turn(turn):
            seen.append(turn.user_msg)
            if turn.user_msg == "seed":
                gen.session.submit_turn("q1")
                gen.session.submit_turn("q2")
                flags["queued"] = True
                await asyncio.sleep(10)

        with pytest.raises(RuntimeError, match="inbox gone"):
            await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))
        await asyncio.sleep(0.5)
        gen.update_file("seen.json", json.dumps(seen))

    res = run_generator_callable(run, messages=[], timeout=20)
    assert res.ok, res.error
    assert json.loads(res.files["seen.json"]) == ["seed"]


def test_chat_busy_tracks_the_inflight_turn(bridge_hook):
    """``gen.session.chat_busy()`` is the busy/idle signal a companion gates on (the
    Agent pulls an annotation batch only on an idle session): False on a quiet chat,
    True while a turn's handler runs or a turn waits in the queue, False
    again once everything settled."""
    flags = {"done": False}

    def setup(bridge):
        orig = bridge.chat_poll
        state = {"sent": False}

        def poll():
            if not state["sent"]:
                state["sent"] = True
                return {"kind": "user_msg", "content": "hi"}
            return orig() if flags["done"] else None

        bridge.chat_poll = poll

    bridge_hook["setup"] = setup

    async def run():
        import gen

        seen = []

        async def on_turn(turn):
            seen.append(f"during={gen.session.chat_busy()}")
            if turn.user_msg == "hi":
                gen.session.submit_turn("queued")
                seen.append(f"queued={gen.session.chat_busy()}")
            if turn.user_msg == "queued":
                flags["done"] = True

        seen.append(f"before={gen.session.chat_busy()}")
        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))
        seen.append(f"after={gen.session.chat_busy()}")
        gen.update_file("busy.txt", ";".join(seen))

    res = run_generator_callable(run, messages=[], timeout=15)
    assert res.ok, res.error
    assert res.files["busy.txt"] == (
        "before=False;during=True;queued=True;during=True;after=False")


def test_chat_busy_counts_a_queued_turn_without_an_inflight_one():
    """The `_turn_queue` disjunct of chat_busy() on its own: a settled chat
    with an entry still queued (the window between a turn's settle callback
    and the queue drain starting the next) must still read busy — a batch
    pulled then would carry text a turn about to start makes stale."""
    from collimate import native

    assert native.chat_busy() is False
    native._turn_queue.append({"content": "queued", "thread_id": "main"})
    try:
        assert native._chat_inflight[0] == 0     # no in-flight turn…
        assert native.chat_busy() is True        # …the queue alone decides
    finally:
        native._turn_queue.clear()
    assert native.chat_busy() is False


def test_wire_thread_ids_are_clamped_to_the_conversation(bridge_hook):
    """One conversation per cell: the wire carries no thread controls, and a
    user_msg naming a foreign thread is clamped to the active conversation —
    no hidden thread is materialized, the reply lands in main. (submit_turn
    remains the programmatic side-transcript primitive; only the WIRE is
    single-conversation.)"""
    flags = {"done": False}

    def setup(bridge):
        orig = bridge.chat_poll
        state = {"step": 0}

        def poll():
            state["step"] += 1
            if state["step"] == 1:
                # A stale/foreign client naming a thread that doesn't exist.
                return {"kind": "user_msg", "content": "hello",
                        "thread_id": "ghost"}
            if state["step"] == 2:
                # The removed thread controls are unknown commands — ignored.
                return {"kind": "thread_new", "thread_id": "side"}
            return orig() if flags["done"] else None

        bridge.chat_poll = poll

    bridge_hook["setup"] = setup

    async def run():
        import gen

        async def on_turn(turn):
            turn.append("user", turn.user_msg)
            turn.append("assistant", f"on:{turn.thread_id}")
            flags["done"] = True

        await gen.serve_chat(on_turn, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=[], timeout=15)
    assert res.ok, res.error
    from collimate.transcript import parse_transcript
    main = parse_transcript(res.files[_in_folder(bridge_hook, "chat/main.json")])
    contents = [m["content"] if isinstance(m, dict) else m.content for m in main]
    assert "hello" in contents and "on:main" in contents
    # No ghost/side thread files were materialized.
    assert _in_folder(bridge_hook, "chat/ghost.json") not in res.files
    assert _in_folder(bridge_hook, "chat/side.json") not in res.files



# ---------------------------------------------------------------------------
# serve_chat(files=False) — the inbox with no chat surface
# ---------------------------------------------------------------------------


def test_serve_chat_without_files_registers_the_inbox_and_writes_nothing(bridge_hook):
    """``files=False`` keeps the chat session registered (the run's inbox)
    but writes no ``.chat`` pointer and no thread file, and reveals nothing;
    the transcript lives in memory — a turn reads the conversation so
    far, the in-memory history keeps it, and ``gen.history`` still
    answers after the session ends."""
    async def run():
        import gen

        seen = []

        async def on_turn(turn):
            seen.append([m.content for m in turn.messages])
            turn.append("user", turn.user_msg)
            turn.append("assistant", f"re: {turn.user_msg}")

        await gen.serve_chat(on_turn, files=False, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))
        gen.update_file("seen.json", json.dumps(seen))
        gen.update_file("history.json", json.dumps(
            [(m.role, m.content) for m in gen.history]))

    res = run_generator_callable(run, messages=["one", "two"], timeout=10)
    assert res.ok, res.error
    assert not any(p.startswith("_runs/") for p in res.files), sorted(res.files)
    assert json.loads(res.files["seen.json"]) == [[], ["one", "re: one"]]
    assert json.loads(res.files["history.json"]) == [
        ["user", "one"], ["assistant", "re: one"],
        ["user", "two"], ["assistant", "re: two"]]
    [reg] = [c for c in res.tier_b_calls if c["op"] == "surface_register"]
    assert (reg["path"], reg["transport_only"], reg["broadcast"]) == (
        _in_folder(bridge_hook, "chat/conversation.chat"), True, False)
    assert not [c for c in res.tier_b_calls if c["op"] == "reveal_file"]


def test_hidden_inbox_has_no_served_file_or_ui_type_requirement():
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "done")

        await gen.serve_chat(on_turn, files=False, pointer_file=gen.run_path("inbox.chat"),
                             threads_dir=gen.run_path("chat"))

    result = run_generator_callable(run, ui_files=[], messages=["start"])
    assert result.ok, result.error
    [call] = [call for call in result.tier_b_calls if call["op"] == "surface_register"]
    assert call["transport_only"] is True and call["broadcast"] is False
    assert not result.files


def test_serve_chat_broadcast_registers_a_transport_only_inbox(bridge_hook):
    """``files=False, broadcast=True`` registers the run's inbox for several
    viewers: transport-only, broadcast, and still no file."""
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("assistant", "ok")

        await gen.serve_chat(on_turn, files=False, broadcast=True,
                             pointer_file=gen.run_path("chat/conversation.chat"),
                             threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=["hi"], ui_files=[], timeout=10)
    assert res.ok, res.error
    [reg] = [c for c in res.tier_b_calls if c["op"] == "surface_register"]
    assert reg == {"op": "surface_register", "chat_id": "rec-surface-1",
                   "path": _in_folder(bridge_hook, "chat/conversation.chat"),
                   "label": "Chat", "transport_only": True, "broadcast": True}
    assert not res.files


def test_serve_chat_broadcast_needs_files_false():
    """The ``.chat`` surface is single-attach: ``broadcast=True`` with files
    raises before anything is registered."""
    import pytest

    async def run():
        import gen

        async def on_turn(turn):  # pragma: no cover — never served
            pass

        with pytest.raises(ValueError, match="broadcast"):
            await gen.serve_chat(on_turn, broadcast=True,
                                 pointer_file=gen.run_path("chat/conversation.chat"),
                                 threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=["hi"], timeout=10)
    assert res.ok, res.error
    assert not [c for c in res.tier_b_calls if c["op"] == "surface_register"]


def test_serve_chat_without_files_seeds_the_initial_message(bridge_hook):
    """The one-shot's seeded first turn still runs with no transcript file —
    the thread is virgin in memory."""
    async def run():
        import gen

        async def on_turn(turn):
            turn.append("user", turn.user_msg)
            turn.append("assistant", "the answer")
            gen.session.request_finish()

        await gen.serve_chat(on_turn, files=False, initial_message="do it", pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))
        gen.update_file("last.txt", gen.history[-1].content)

    res = run_generator_callable(run, messages=[], timeout=10)
    assert res.ok, res.error
    assert res.files["last.txt"] == "the answer"
    assert not any(p.startswith("_runs/") for p in res.files)


def _interrupt_run(frame: dict, bridge_hook, *, current_turn_id=None):
    """Serve one long turn and deliver ``frame`` (with ``turn_id`` filled
    from the running turn when it is ``...``) once it is in flight; returns
    what the turn saw."""
    started = asyncio.Event()
    holder: dict = {}

    def setup(bridge):
        orig = bridge.chat_poll
        state = {"sent": 0}

        def gated_poll():
            if started.is_set() and state["sent"] == 0:
                state["sent"] = 1
                cmd = dict(frame)
                if cmd.get("turn_id") is ...:
                    cmd["turn_id"] = holder["turn_id"]
                return cmd
            return orig()

        bridge.chat_poll = gated_poll

    bridge_hook["setup"] = setup

    async def run():
        import asyncio as aio

        import gen

        async def on_turn(turn):
            holder["turn_id"] = turn.turn_id
            started.set()
            for _ in range(15):          # ~1.5 s unless interrupted
                if turn.cancelled:
                    break
                await aio.sleep(0.1)
            gen.update_file("seen.txt", "cancelled" if turn.cancelled else "ran-out")
            gen.session.request_finish()

        await gen.serve_chat(on_turn, files=False, pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))

    res = run_generator_callable(run, messages=["go"], timeout=20)
    assert res.ok, res.error
    return res.files["seen.txt"]


def test_an_interrupt_naming_the_turn_cancels_it(bridge_hook):
    """A broadcast inbox viewer's Stop names the turn it saw start."""
    assert _interrupt_run({"kind": "interrupt", "turn_id": ...},
                          bridge_hook) == "cancelled"


def test_an_interrupt_naming_another_turn_is_ignored(bridge_hook):
    """A late click from another tab names a turn that already ended: the
    turn now in flight keeps running — even when the frame's thread matches."""
    assert _interrupt_run({"kind": "interrupt", "turn_id": "an-old-turn",
                           "thread_id": "main"}, bridge_hook) == "ran-out"


def test_an_interrupt_naming_only_the_thread_still_cancels(bridge_hook):
    """The chat composer's Stop names its thread."""
    assert _interrupt_run({"kind": "interrupt", "thread_id": "main"},
                          bridge_hook) == "cancelled"


def test_an_interrupt_naming_nothing_is_dropped(bridge_hook):
    assert _interrupt_run({"kind": "interrupt"}, bridge_hook) == "ran-out"


def test_unsupported_chat_commands_leave_history_intact_and_normal_turns_work(bridge_hook):
    commands = [
        {'kind': 'edit', 'index': 1, 'content': 'changed'},
        {'kind': 'delete', 'index': 0},
        {'kind': 'insert', 'index': 0, 'role': 'system', 'content': 'injected'},
        {'kind': 'move', 'index': 0, 'to': 1},
        {'kind': 'regenerate', 'index': 1},
        {'kind': 'unknown_command'},
    ]

    async def run():
        import gen

        async def on_turn(turn):
            turn.append('user', turn.user_msg)
            turn.append('assistant', f'reply to {turn.user_msg}')

        await gen.serve_chat(on_turn, initial_message='first',
                             pointer_file=gen.run_path('chat/conversation.chat'),
                             threads_dir=gen.run_path('chat'))

    result = run_generator_callable(run, messages=[*commands, 'second'], timeout=10)
    assert result.ok, result.error
    from collimate.transcript import parse_transcript
    messages = parse_transcript(result.files[_in_folder(bridge_hook, 'chat/main.json')])
    assert [(message.role, message.content) for message in messages] == [
        ('user', 'first'), ('assistant', 'reply to first'),
        ('user', 'second'), ('assistant', 'reply to second'),
    ]
    refusals = [item['event'] for item in _stream_events(bridge_hook['bridge'])
                if item['event'].get('type') == 'refused']
    assert [item['op'] for item in refusals] == [cmd['kind'] for cmd in commands]
    assert all(item['thread_id'] == 'main' and item['message'] == 'Unsupported chat command.'
               for item in refusals)
