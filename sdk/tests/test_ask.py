# SPDX-License-Identifier: MIT
"""``gen.ask`` / ``gen.can_ask`` — the SDK's one approval primitive — and the
host-free harness's scripted human (``approvals=``).

The card wire spells the option id ``option_id`` in the card, the answer and
the resolution; the run's phase is ``awaiting_approval`` while a card waits
and returns to the newest phase set when the last card closes. A card goes
through the run's inbox and is answered only while ``gen.serve_chat`` serves
it (the inbox carries the answers); it shows on the ``.chat`` surface or on
every viewer of a broadcast inbox, and a transport-only inbox without
broadcast refuses it at once."""
from __future__ import annotations

import asyncio

import pytest

from collimate import native
from collimate.testing import UNANSWERED, make_test_bridge, run_generator_callable


@pytest.fixture
def run(tmp_path, monkeypatch):
    """A host-free run serving its chat session, as inside a ``serve_chat``
    turn; ``run(approvals)`` installs one and returns its bridge."""
    made: list = []

    def _make(approvals=None):
        bridge = make_test_bridge(tmp_path, approvals=approvals)
        native.set_bridge(bridge)
        monkeypatch.setattr(native, "_serving_chat", True)
        # The inbox serve_chat registered can show the card (its .chat surface).
        monkeypatch.setattr(native, "_inbox_shows_cards", [True])
        made.append(bridge)
        return bridge

    yield _make
    native._clear_bridge()
    for bridge in made:
        bridge.close()


def _events(bridge, kind):
    return [r["event"] for r in bridge.stream_events if r["event"].get("type") == kind]


def test_an_answered_card_returns_the_chosen_option(run):
    bridge = run(["allow_once"])
    native.phase("running", "responding")
    got = asyncio.run(native.ask("Run a command", kind="execute", detail="ls -la"))
    assert got == "allow_once"
    [card] = _events(bridge, "permission_request")
    assert card["title"] == "Run a command" and card["kind"] == "execute"
    assert card["detail"] == "ls -la"
    assert card["options"] == [
        {"option_id": "allow_once", "name": "Allow", "kind": "allow_once"},
        {"option_id": "reject_once", "name": "Reject", "kind": "reject_once"},
    ]
    [resolution] = _events(bridge, "permission_resolved")
    assert resolution == {"type": "permission_resolved", "id": card["id"],
                          "option_id": "allow_once"}
    # awaiting_approval while the card waits, then the phase before it.
    assert bridge.phases == [("running", "responding"),
                             ("awaiting_approval", "awaiting approval: Run a command"),
                             ("running", "responding")]


def test_the_phase_before_the_card_is_restored_whatever_it_was(run):
    bridge = run(["a"])
    native.phase("spinning_up", "pulling image")
    asyncio.run(native.ask("x", kind="edit", options=[
        {"option_id": "a", "name": "A", "kind": "allow_once"}]))
    assert bridge.phases[-1] == ("spinning_up", "pulling image")


def test_a_run_that_set_no_phase_returns_to_running(run):
    bridge = run(["allow_once"])
    asyncio.run(native.ask("x", kind="edit"))
    assert [p for p, _ in bridge.phases] == ["awaiting_approval", "running"]


def test_a_dismissed_card_is_refused(run):
    bridge = run([None])
    assert asyncio.run(native.ask("x", kind="edit")) is None
    [resolution] = _events(bridge, "permission_resolved")
    assert resolution.get("cancelled") is True and "option_id" not in resolution


def test_an_answer_naming_an_option_never_offered_is_refused(run):
    bridge = run(["allow_always"])
    assert asyncio.run(native.ask("x", kind="edit")) is None
    assert _events(bridge, "permission_resolved")[0].get("cancelled") is True


def test_an_unanswered_card_times_out_refused_with_an_error_notice(run):
    bridge = run([UNANSWERED])
    assert asyncio.run(native.ask("x", kind="execute", timeout=0.2)) is None
    [resolution] = _events(bridge, "permission_resolved")
    assert resolution.get("cancelled") is True and resolution.get("timed_out") is True
    assert _events(bridge, "error")[0]["message"].startswith("approval request timed out")
    assert any("timed out" in line for line in bridge._rec_logs)


def test_nobody_to_answer_refuses_at_once_without_a_card(run):
    bridge = run(None)                    # nobody scripted: nobody can answer
    assert native.can_ask() is False
    assert asyncio.run(native.ask("x", kind="execute", timeout=30)) is None
    assert bridge.stream_events == [] and bridge.phases == []
    assert any("nobody can answer" in line for line in bridge._rec_logs)




def test_can_ask_needs_a_run():
    assert native.current_bridge() is None
    with pytest.raises(RuntimeError, match="outside a generator run"):
        native.can_ask()


def test_a_dev_run_without_a_host_cannot_ask(tmp_path):
    """The real bridge answers cards only with a host connected."""
    from collimate._host.bridge import HostBridge
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge._sock_wfile = None
    assert bridge.answers_cards() is False
    bridge._sock_wfile = object()
    assert bridge.answers_cards() is True


def test_a_stopped_run_refuses_the_waiting_card(run):
    bridge = run([UNANSWERED])

    async def flow():
        task = asyncio.ensure_future(native.ask("x", kind="execute", timeout=30))
        await asyncio.sleep(0.05)
        bridge._signal_stop()
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(flow()) is None
    assert _events(bridge, "permission_resolved")[0].get("cancelled") is True


def test_cancelling_the_ask_closes_the_card_refused(run):
    bridge = run([UNANSWERED])

    async def flow():
        native.phase("running", "responding")
        task = asyncio.ensure_future(native.ask("x", kind="execute", timeout=30))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(flow())
    assert _events(bridge, "permission_resolved")[0].get("cancelled") is True
    assert bridge.phases[-1] == ("running", "responding")


def test_the_run_stays_awaiting_approval_until_every_open_card_closes(run):
    answers = {"first": UNANSWERED, "second": UNANSWERED}
    bridge = run(lambda card: answers[card["title"]])

    async def flow():
        native.phase("running", "")
        first = asyncio.ensure_future(native.ask("first", kind="execute", timeout=30))
        second = asyncio.ensure_future(native.ask("second", kind="execute", timeout=30))
        await asyncio.sleep(0.05)
        ids = {c["title"]: c["id"] for c in _events(bridge, "permission_request")}
        native.deliver_permission_answer({"id": ids["first"], "option_id": "allow_once"})
        assert await first == "allow_once"
        # One card is still up: the run has not gone back to running.
        assert bridge.phases[-1][0] == "awaiting_approval"
        native.deliver_permission_answer({"id": ids["second"], "option_id": "reject_once"})
        assert await second == "reject_once"

    asyncio.run(flow())
    assert [p for p, _ in bridge.phases] == ["running", "awaiting_approval", "running"]


@pytest.mark.parametrize("options", [
    [],
    [{"name": "Allow", "kind": "allow_once"}],
    [{"option_id": "a", "kind": "allow_once"}, {"option_id": "a", "kind": "reject_once"}],
    ["allow_once"],
])
def test_malformed_options_are_a_caller_bug(run, options):
    run(["a"])
    with pytest.raises(ValueError):
        asyncio.run(native.ask("x", kind="edit", options=options))


def test_the_card_and_its_resolution_go_to_the_inbox(run):
    """The inbox is the run's one live session: neither the card nor its
    resolution names a surface, and neither belongs to a turn."""
    bridge = run(["allow_once"])
    asyncio.run(native.ask("x", kind="edit"))
    assert [r["event"]["type"] for r in bridge.stream_events] == [
        "permission_request", "permission_resolved"]
    assert all(set(r) == {"turn_id", "event"} and r["turn_id"] == ""
               for r in bridge.stream_events)


# ── fail-closed edges ──────────────────────────────────────────────────


@pytest.mark.parametrize("timeout", [None, float("inf"), float("nan"), 0, -1, "soon"])
def test_a_bad_timeout_is_a_caller_bug_raised_before_any_card(run, timeout):
    """The timeout is the only bound on a card's wait (the watchdog pauses
    while it waits), so it must be finite and positive — checked before the
    card is shown, leaving no card, open count or phase behind."""
    bridge = run(["allow_once"])
    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(native.ask("x", kind="execute", timeout=timeout))
    assert bridge.stream_events == [] and bridge.phases == []
    assert native._open_asks[0] == 0
    # The run is not left on awaiting_approval: a later card comes and goes.
    native.phase("running", "")
    assert asyncio.run(native.ask("y", kind="execute")) == "allow_once"
    assert [p for p, _ in bridge.phases] == ["running", "awaiting_approval", "running"]


def test_no_served_chat_refuses_at_once_without_a_card(run, monkeypatch):
    """A card's answer comes back through the chat session serve_chat
    serves; with none served, nobody's answer could arrive."""
    bridge = run(["allow_once"])
    monkeypatch.setattr(native, "_serving_chat", False)
    assert native.can_ask() is True
    assert asyncio.run(native.ask("x", kind="execute", timeout=30)) is None
    assert bridge.stream_events == [] and bridge.phases == []
    assert any("no chat session is being served" in line for line in bridge._rec_logs)


def test_a_card_is_refused_at_once_when_the_inbox_shows_none(run):
    """A transport-only inbox without broadcast has no viewer: a card would
    sit unseen until its timeout — so it is refused at once, with no card and
    no awaiting_approval."""
    bridge = run(["allow_once"])
    native._inbox_shows_cards[0] = False
    assert asyncio.run(native.ask("x", kind="execute", timeout=30)) is None
    assert bridge.stream_events == [] and bridge.phases == []
    assert any("nobody can see it" in line for line in bridge._rec_logs)


def test_a_card_is_refused_when_the_chat_session_ends_while_it_waits(run, monkeypatch):
    bridge = run([UNANSWERED])

    async def flow():
        task = asyncio.ensure_future(native.ask("x", kind="execute", timeout=30))
        await asyncio.sleep(0.05)
        monkeypatch.setattr(native, "_serving_chat", False)
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(flow()) is None
    assert _events(bridge, "permission_resolved")[0].get("cancelled") is True


def test_a_phase_set_while_a_card_waits_is_held_and_restored_after(run):
    """The host keeps showing awaiting_approval (its watchdog paused) while a
    card is open; what the run set meanwhile is shown once the card closes —
    the newest phase, not the one from before the card."""
    bridge = run([UNANSWERED])

    async def flow():
        native.phase("running", "responding")
        task = asyncio.ensure_future(native.ask("x", kind="execute", timeout=30))
        await asyncio.sleep(0.05)
        native.phase("running", "agent transport lost — reprovisioning")
        assert bridge.phases[-1][0] == "awaiting_approval"
        [card] = _events(bridge, "permission_request")
        native.deliver_permission_answer({"id": card["id"], "option_id": "allow_once"})
        return await task

    assert asyncio.run(flow()) == "allow_once"
    assert bridge.phases == [
        ("running", "responding"),
        ("awaiting_approval", "awaiting approval: x"),
        ("running", "agent transport lost — reprovisioning"),
    ]


def test_a_message_set_while_a_card_waits_is_held_and_restored_after(run):
    """A new message for the same phase never replaces the card's 'awaiting
    approval' message; the held message is shown with the restored phase
    once the card closes."""
    bridge = run([UNANSWERED])

    async def flow():
        native.phase("running", "responding")
        task = asyncio.ensure_future(native.ask("x", kind="execute", timeout=30))
        await asyncio.sleep(0.05)
        native.phase("running", "pulling image")
        assert ("running", "pulling image") not in bridge.phases
        assert bridge.phases[-1][0] == "awaiting_approval"
        [card] = _events(bridge, "permission_request")
        native.deliver_permission_answer({"id": card["id"], "option_id": "allow_once"})
        return await task

    assert asyncio.run(flow()) == "allow_once"
    assert bridge.phases[-1] == ("running", "pulling image")


def test_a_message_set_before_a_card_is_restored_after(run):
    bridge = run([UNANSWERED])

    async def flow():
        native.phase("running", "a")
        native.phase("running", "b")
        task = asyncio.ensure_future(native.ask("x", kind="execute", timeout=30))
        await asyncio.sleep(0.05)
        [card] = _events(bridge, "permission_request")
        native.deliver_permission_answer({"id": card["id"], "option_id": "allow_once"})
        return await task

    assert asyncio.run(flow()) == "allow_once"
    assert bridge.phases[-1] == ("running", "b")


# ── the harness's scripted human ───────────────────────────────────────


def _asks(*titles, serve: bool = True):
    """A generator that asks ``titles`` in turn — from a serve_chat turn,
    or (``serve=False``) straight from ``run()``, with no chat served."""
    async def run():
        import gen
        answers: list = []

        async def ask_all(turn=None):
            for title in titles:
                answers.append(await gen.ask(title, kind="execute", timeout=0.2))

        if serve:
            await gen.serve_chat(ask_all, initial_message="go", pointer_file=gen.run_path("chat/conversation.chat"), threads_dir=gen.run_path("chat"))
        else:
            await ask_all()
        gen.update_file("answers.txt", repr(answers))
        gen.update_file("can_ask.txt", repr(gen.can_ask()))
    return run


def _resolutions(res) -> list:
    return [c["event"] for c in res.tier_b_calls
            if c.get("op") == "stream_event"
            and c["event"].get("type") == "permission_resolved"]


def test_run_generator_without_approvals_has_nobody_to_ask():
    res = run_generator_callable(_asks("a"), messages=[])
    assert res.ok, res.error
    assert res.files["can_ask.txt"] == "False"
    assert res.files["answers.txt"] == "[None]"


def test_run_generator_answers_from_a_list_then_leaves_cards_waiting__P6F01():
    res = run_generator_callable(_asks("a", "b", "c"), messages=[],
                                 approvals=["allow_once", None])
    assert res.ok, res.error
    assert res.files["can_ask.txt"] == "True"
    assert res.files["answers.txt"] == "['allow_once', None, None]"
    assert [r.get("timed_out", False) for r in _resolutions(res)] == [False, False, True]


def test_run_generator_answers_from_a_callable():
    res = run_generator_callable(
        _asks("yes", "no"), messages=[],
        approvals=lambda card: "allow_once" if card["title"] == "yes" else "reject_once")
    assert res.ok, res.error
    assert res.files["answers.txt"] == "['allow_once', 'reject_once']"


def test_the_harness_refuses_a_card_production_could_not_answer():
    """A generator that asks without serving chat gets no card and no answer
    under the harness either, however the approvals are scripted — its tests
    fail the way the real run would, instead of passing on a scripted yes."""
    res = run_generator_callable(_asks("a", serve=False), approvals=["allow_once"])
    assert res.ok, res.error
    assert res.files["answers.txt"] == "[None]"
    assert not [c for c in res.tier_b_calls if c.get("op") == "stream_event"]


def _ask_beside_an_inbox(*, broadcast: bool):
    """A run serving a transport-only inbox whose ``on_ready`` task asks one
    card, writes the answer, and finishes the session."""
    async def run():
        import gen

        async def on_turn(turn):  # pragma: no cover — no turn is sent
            pass

        def on_ready():
            async def ask_once():
                answer = await gen.ask("Run ls", kind="execute", timeout=5)
                gen.update_file("answer.txt", repr(answer))
                gen.session.request_finish()
            asyncio.ensure_future(ask_once())

        await gen.serve_chat(on_turn, files=False, broadcast=broadcast, on_ready=on_ready,
                             pointer_file=gen.run_path("chat/conversation.chat"),
                             threads_dir=gen.run_path("chat"))
    return run


def test_a_broadcast_inbox_shows_cards_without_a_chat_surface():
    res = run_generator_callable(_ask_beside_an_inbox(broadcast=True),
                                 approvals=["allow_once"], messages=[], timeout=15)
    assert res.ok, res.error
    assert res.files["answer.txt"] == "'allow_once'"
    cards = [c for c in res.tier_b_calls if c.get("op") == "stream_event"
             and c["event"].get("type") == "permission_request"]
    assert len(cards) == 1 and "surface" not in cards[0]


def test_a_transport_only_inbox_without_broadcast_refuses_cards():
    res = run_generator_callable(_ask_beside_an_inbox(broadcast=False),
                                 approvals=["allow_once"], messages=[], timeout=15)
    assert res.ok, res.error
    assert res.files["answer.txt"] == "None"
    assert any("nobody can see it" in line for line in res.logs)
