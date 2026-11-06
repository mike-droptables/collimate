# SPDX-License-Identifier: MIT
"""The Agent cell's shared session runtime (``generators/_agent_shared/session``)
behind its one generator: one surface per run, the one its ``surface``
setting picks.

These lock in:

  * the run's surface — annotation (no UI file, a broadcast inbox), the chat
    or the terminal, at the file the manifest's ``opens`` names — and the
    exclusive surface files: an annotation run writes no chat file;
  * the one-shot: its opening turn presents its prompt message, its answer
    is posted under it, and it ends once the turn and its corrections settle;
  * the selected file as part of the first turn (``selected_file_first``) on
    each surface; the run's prompt as chat's opening message and claude's
    TTY's first message;
  * the terminal surface: the TTY on the minted session (tiers A/B); a TTY
    that exits or fails ends the run;
  * the run folder: every surface file lives in the run's own
    ``_runs/<label>/`` (its ``SessionPaths``), the directives name those
    paths and who the run is, and nothing touches another run's files;
  * the run end: surface files logged and cleared;
  * the chat ``on_turn`` handler (tool events, stopped/cancelled/failed
    turns, usage), a refused turn ending the run, and the pure helpers.

The annotation surface's batches, replies and corrections are
``tests/test_agent_annotation.py``. Everything runs against a live bridge
(``native_bridge``) with ``run_agent`` / ``serve_chat`` monkeypatched — no
LLM, no host. Synthetic fixtures only.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os

import pytest

from tests._workspace import GENERATORS



pytestmark = pytest.mark.usefixtures("no_machine_harness_probe")


import _agent_shared as eng  # noqa: E402 — the engine's own flag tables
from _agent_shared import harness_homes, session  # noqa: E402
from _agent_shared.session import annotation, consts, entry, media, run_end, terminal  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The Agent cell's generator package, which ``session.run`` reads its
# manifest and main-config schema from.
_PACKAGE = os.path.join(GENERATORS, "agent", "agent")


def _paths(backend: str = "pydantic-ai"):
    """The installed run's surface paths — all in its run folder."""
    return consts.SessionPaths.for_run(backend)


def _boot_history(kw: dict) -> None:
    """What ``serve_chat`` does before its first turn, for a stubbed one:
    bind the transcript to the thread folder the run passed (its run
    folder's ``chat/``)."""
    entry.gen.history.persist = kw.get("files", True)
    entry.gen.history.switch_thread("main", threads_dir=kw["threads_dir"], load=False)


@pytest.fixture(autouse=True)
def _reset_surface_state(native_bridge):
    """The surface primitives keep module-level state (the turn queue, the
    inbox's card slot, the finish flag). Individual tests reset what they
    touch in their own ``finally``, but a missed slot leaks into whichever
    test file runs NEXT in the process — this resets all of it after every
    test, unconditionally."""
    yield
    from collimate import native as n
    n._turn_queue.clear()
    n._finish_requested[0] = False
    n._inbox_shows_cards[0] = False
    # The transcript singleton too: a real serve_chat resets it at boot
    # (switch_thread), but these tests stub serve_chat out — without this a
    # test that appends leaks its messages into whichever test runs next.
    n.history.persist = True
    n.history.switch_thread("main", threads_dir="chat", load=False)


# A minimal 1x1 PNG — a real media byte payload for attachment/file tests.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ── synthetic fixtures ────────────────────────────────────────────────


class _Result:
    """The shape ``run_agent`` returns (an ``AgentResult`` stand-in)."""

    def __init__(self, *, text="", trace=None, usage=None, files=None,
                 backend="pydantic-ai"):
        self.text = text
        self.trace = trace if trace is not None else []
        self.usage = usage if usage is not None else {}
        self.files = files if files is not None else []
        self.backend = backend


class _Msg:
    """A transcript message — the ``.role`` / ``.content`` shape
    ``turn.messages`` yields."""

    def __init__(self, role, content):
        self.role = role
        self.content = content


class FakeChatTurn:
    """A stand-in for ``gen.ChatTurn`` that records everything the handler does.

    ``append`` also grows ``messages`` so the handler sees the user bubble it
    just added (the transcript the renderer scopes per thread).
    """

    def __init__(self, user_msg, *, messages=None, attachments=None):
        self.user_msg = user_msg
        self.attachments = list(attachments or [])
        self.cancelled = False
        self._messages = list(messages or [])
        self.deltas: list = []
        self.thinking_deltas: list = []
        self.tool_calls: list = []   # (name, status, kw)
        self.emitted: list = []      # turn.emit(...) payloads
        self.appended: list = []     # append(...) as dicts

    @property
    def messages(self):
        return list(self._messages)

    def append(self, role, content, *, thinking="", attachments=None,
               tools=None, stopped=False):
        self._messages.append(_Msg(role, content))
        self.appended.append({
            "role": role, "content": content, "thinking": thinking,
            "attachments": list(attachments or []),
            "tools": list(tools or []), "stopped": bool(stopped),
        })

    def delta(self, text):
        self.deltas.append(text)

    def thinking_delta(self, text):
        self.thinking_deltas.append(text)

    def tool(self, name, status="running", **kw):
        self.tool_calls.append((name, status, kw))

    def emit(self, event):
        self.emitted.append(event)


class _NullCM:
    """A no-op async context manager standing in for ``gen.live_files()``."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def flush(self):
        pass


def _base_cfg(**over):
    """A complete parsed-config dict — every key the entrypoint subscripts,
    at inert defaults. Synthetic (no shipped set_yaml), overridable per test.
    ``opens`` is the shipped manifest's declaration resolved for these
    values, as ``session.run`` sets it."""
    cfg = {
        "session": "interactive", "surface": "chat",
        "backend": "pydantic-ai", "auth": "api_key", "provider": "anthropic",
        "model": "test-model-x", "system_prompt": "",
        "enable_file_tools": True, "exec_enabled": False,
        "websearch": True,
        "runtime": "docker", "host_harness": None,
        "mcp": "off",
        "approvals": "auto", "commands": "deny",
        "warnings": [],
    }
    cfg.update(over)
    if "opens" not in over:
        cfg["opens"] = _opens(cfg)
    return cfg


def _opens(cfg):
    """The surface files the shipped manifest opens for ``cfg``'s values."""
    return session.surface_opens(_PACKAGE, {
        k: cfg[k] for k in ("surface", "backend", "session") if k in cfg})


def _run_on_turn(cfg, turn, monkeypatch):
    """Drive ``run_session`` with ``serve_chat`` stubbed to capture the inner
    ``on_turn`` handler, then run one turn through it. Returns the holder
    (``on_turn`` / ``serve_kwargs``). ``entry.run_agent`` must be patched by the
    caller before this runs.

    The turn runs INSIDE the stubbed ``serve_chat``, where a real one runs it:
    ``run_session``'s ``finally`` is the run end (agent-v2 §9.1.8) and clears
    the session's live interaction files, so a turn dispatched after it
    returned would see a cell the run had already torn down."""
    holder: dict = {}

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        holder["on_turn"] = on_turn
        holder["serve_kwargs"] = kw
        await on_turn(turn)

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())

    asyncio.run(entry.run_session(cfg))
    return holder


# ── _upsert_tool (pure) ────────────────────────────────────────────────


def test_upsert_tool_pairs_completions_by_id():
    """A completion patches its running entry in place (status/result); a
    nameless completion never blanks the name; a no-id event appends fresh."""
    acc: list = []
    entry._upsert_tool(acc, {"id": "a", "name": "read", "status": "running",
                             "input": {"p": "x"}})
    assert acc == [{"id": "a", "name": "read", "status": "running",
                    "input": {"p": "x"}}]
    # done pairs by id: status + result patched, existing input kept.
    entry._upsert_tool(acc, {"id": "a", "status": "done", "result": "ok"})
    assert acc == [{"id": "a", "name": "read", "status": "done",
                    "input": {"p": "x"}, "result": "ok"}]
    # a nameless completion for the same id must not blank the name.
    entry._upsert_tool(acc, {"id": "a", "status": "done"})
    assert acc[0]["name"] == "read"
    # a no-id (heartbeat) event just appends.
    entry._upsert_tool(acc, {"name": "beat", "status": "running"})
    assert len(acc) == 2 and acc[1]["id"] == "" and acc[1]["name"] == "beat"


def test_upsert_tool_caps_at_max_events():
    """The in-memory accumulation is bounded at ``_MAX_TOOL_EVENTS``."""
    acc: list = []
    for i in range(entry._MAX_TOOL_EVENTS + 25):
        entry._upsert_tool(acc, {"name": f"t{i}", "status": "running"})
    assert len(acc) == entry._MAX_TOOL_EVENTS


# ── the one-shot session ───────────────────────────────────────────────


class _QueuedTurn(FakeChatTurn):
    """A turn the stubbed serve_chat drains off the session's queue. Like a
    real ``ChatTurn``, its ``append`` also keeps the shared transcript
    coherent — that transcript is what the run's answer is read out of."""

    def __init__(self, user_msg, *, source=""):
        super().__init__(user_msg)
        self.source = source

    def append(self, role, content, **kw):
        super().append(role, content, **kw)
        entry.gen.history.append(role, content)


def _oneshot_cfg(**over):
    return _base_cfg(session="oneshot", surface="annotation", **over)


def _seed_prompt(bridge, text: str) -> str:
    """What the host does before a run with a prompt starts: the prompt is
    the run's first annotation message, user-authored and already read.
    Returns its id."""
    bridge.prompt = text
    with bridge._annotation_store_lock:
        return bridge._store_annotation(author="user", kind="prompt", body=text,
                                        born_read=True)["id"]


class _LiveFiles:
    """``gen.live_files`` stand-in that records the run's callbacks."""

    def __init__(self, holder: dict, kwargs: dict):
        holder.update(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def flush(self):
        pass


def _drive_session(monkeypatch, cfg, *, on_start=None, mirror=None) -> tuple:
    """Run a session with ``serve_chat`` stubbed: boot, ``on_ready``, then
    drain the session's own turn queue through ``on_turn`` — every turn the
    run submits (an opening, a correction) runs, as a real serve_chat runs
    them. ``on_start(turn)`` sees each turn before it runs; ``mirror`` (a
    dict) receives the live mirror's callbacks as the run opens it. Returns
    the ``AgentSession``, the turns run and the mirror's callbacks."""
    from collimate import native as n

    sessions: list = []
    turns: list = []
    mirror = {} if mirror is None else mirror
    real_session = entry.AgentSession

    def capture(*a, **kw):
        st = real_session(*a, **kw)
        sessions.append(st)
        return st

    monkeypatch.setattr(entry, "AgentSession", capture)
    monkeypatch.setattr(entry.gen, "live_files", lambda **kw: _LiveFiles(mirror, kw))

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        mirror["serve_kwargs"] = kw
        kw["on_ready"]()
        await asyncio.sleep(0)
        while n._turn_queue:
            queued = n._turn_queue.popleft()
            turn = _QueuedTurn(queued["content"], source=queued["source"])
            turns.append(turn)
            if on_start is not None:
                on_start(turn)
            await on_turn(turn)

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    try:
        asyncio.run(entry.run_session(cfg))
    finally:
        n._turn_queue.clear()
    return (sessions[0] if sessions else None), turns, mirror


def _drive_oneshot(native_bridge, monkeypatch, prompt="do the thing", *, cfg=None,
                   on_start=None) -> tuple:
    """A one-shot over ``prompt`` (seeded as the host seeds it). Returns
    ``(session, turns, mirror, prompt_id)``."""
    prompt_id = _seed_prompt(native_bridge, prompt)
    st, turns, mirror = _drive_session(monkeypatch, cfg or _oneshot_cfg(),
                                       on_start=on_start)
    return st, turns, mirror, prompt_id


def test_a_oneshot_finishes_after_its_opening_turn(native_bridge, monkeypatch):
    """The pass ends itself once its opening turn settles: the answer is
    posted under the prompt message and ``request_finish`` is asked for
    exactly once."""
    native_bridge.runner = {"runner": "probe-runner"}
    finishes: list = []
    monkeypatch.setattr(entry.gen.session, "request_finish", lambda: finishes.append(1))

    async def fake_run_agent(**kw):
        return _Result(text="the answer")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    st, turns, _, prompt_id = _drive_oneshot(native_bridge, monkeypatch)

    assert st.oneshot is True and st.finish_requested is True
    assert [t.source for t in turns] == [consts.Sources.OPENING]
    assert [(p["text"], p["reply_to"]) for p in native_bridge.annotation_posts] == [
        ("the answer", prompt_id)]
    assert finishes == [1]


def test_an_empty_opening_reply_posts_nothing_and_finishes(native_bridge, monkeypatch):
    """An opening turn with no prose leaves the transcript's empty-response
    placeholder — display text, never an answer: nothing is posted, and the
    run still finishes."""
    native_bridge.runner = {"runner": "probe-runner"}
    monkeypatch.setattr(entry.gen.session, "request_finish", lambda: None)

    async def fake_run_agent(**kw):
        return _Result(text="")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    st, _, _, _ = _drive_oneshot(native_bridge, monkeypatch)

    assert st.finish_requested is True
    assert native_bridge.annotation_posts == []


@pytest.mark.parametrize("commands", ["ask", "allow"])
def test_every_turn_runs_at_the_configs_commands_level_F1585_F1598(
        native_bridge, monkeypatch, commands):
    """The parsed commands level reaches run_agent on every turn — its own
    default is deny, so a dropped argument would take every runner's shell
    away without a word."""
    seen: list = []

    async def fake_run_agent(**kw):
        seen.append(kw["commands"])
        return _Result(text="ok")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _drive_oneshot(native_bridge, monkeypatch, cfg=_oneshot_cfg(commands=commands))
    assert seen and set(seen) == {commands}


def _opening(native_bridge, monkeypatch, cfg) -> dict:
    """What a run opens with: serve_chat's files and broadcast flags, and the
    turns queued once it booted."""
    from collimate import native as n

    seen: dict = {}

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        seen.update(files=kw["files"], broadcast=kw["broadcast"],
                    initial="initial_message" in kw,
                    queued=[(e["source"], e["content"]) for e in n._turn_queue])

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())
    try:
        asyncio.run(entry.run_session(cfg))
    finally:
        _clear_session_state()
    return seen


def test_an_interactive_annotation_run_submits_no_opening_turn(native_bridge,
                                                               monkeypatch):
    """The Prompt belongs to the one-shot (the schema hides it for an
    interactive session), so an interactive annotation run opens on nothing:
    it waits for the user's first message — never a prompt left behind by an
    earlier shape. Its inbox is broadcast, and it writes no chat file."""
    native_bridge.prompt = "a stale one-shot task"
    seen = _opening(native_bridge, monkeypatch, _base_cfg(surface="annotation"))
    assert seen == {"files": False, "broadcast": True, "initial": False, "queued": []}


def test_a_chats_prompt_is_its_opening_message(native_bridge, monkeypatch):
    """A6: the chat surface's Prompt is an optional opening message — the
    user's first composer turn, queued once the session boots; never a
    seeded initial message."""
    native_bridge.prompt = "  what changed since?  "
    seen = _opening(native_bridge, monkeypatch, _base_cfg(surface="chat"))
    assert seen == {"files": True, "broadcast": False, "initial": False,
                    "queued": [(consts.Sources.COMPOSER, "what changed since?")]}
    native_bridge.prompt = ""
    seen = _opening(native_bridge, monkeypatch, _base_cfg(surface="chat"))
    assert seen["queued"] == []


def test_a_oneshot_opening_is_one_marker_turn(native_bridge, monkeypatch):
    """A one-shot queues exactly one opening turn at boot — a marker its turn
    body replaces with the prompt message it presents."""
    _seed_prompt(native_bridge, "do the thing")
    seen = _opening(native_bridge, monkeypatch, _oneshot_cfg())
    assert seen["queued"] == [(consts.Sources.OPENING, consts.Sources.OPENING)]
    assert seen["files"] is False and seen["broadcast"] is True


def test_the_opening_answer_is_posted_as_a_reply_to_the_prompt_message(
        native_bridge, monkeypatch):
    """The pass answers the prompt once, under the prompt message; a
    correction turn behind it only repairs a refused write, and its
    housekeeping reply is never posted as the answer."""
    import gen

    native_bridge.runner = {"runner": "probe-runner"}
    replies = ["the answer", "I fixed the file"]
    mirror: dict = {}

    async def fake_run_agent(**kw):
        if len(replies) == 2:
            # The live mirror reports a refused write during the opening.
            mirror["on_refused"]("notes.md", "the verifier refused it")
        return _Result(text=replies.pop(0))

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    prompt_id = _seed_prompt(native_bridge, "do the thing")
    st, turns, _ = _drive_session(monkeypatch, _oneshot_cfg(), mirror=mirror)

    assert [t.source for t in turns] == [consts.Sources.OPENING, consts.Sources.CORRECTION]
    assert st.finish_requested is True
    assert [(p["text"], p["reply_to"]) for p in native_bridge.annotation_posts] == [
        ("the answer", prompt_id)]
    # …and the correction really did run (the transcript keeps both replies).
    assert [m.content for m in gen.history][-1] == "I fixed the file"


def test_a_stopped_opening_turn_ends_the_run_stopped_and_posts_nothing(
        native_bridge, monkeypatch):
    """Stop on the opening turn ends the pass with no answer posted — and it
    DOES end: no correction turn resumes the stopped work, the run asks to
    finish once, and it reports the user's stop (not success)."""
    native_bridge.runner = {"runner": "probe-runner"}
    finishes: list = []
    monkeypatch.setattr(entry.gen.session, "request_finish", lambda: finishes.append(1))
    calls: list = []
    running: list = []

    async def fake_run_agent(**kw):
        calls.append(kw["prompt"])
        running[-1].cancelled = True
        return _Result(text="half an answer")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    submitted = _spy_submits(monkeypatch)
    with pytest.raises(entry.gen.Cancelled):
        _drive_oneshot(native_bridge, monkeypatch, on_start=running.append)

    assert finishes == [1]
    assert len(calls) == 1 and "do the thing" in calls[0]
    assert submitted == [consts.Sources.OPENING], \
        "no correction turn may resume the stopped work"
    assert native_bridge.annotation_posts == []


def _seed_main_config(native_bridge, values: dict) -> None:
    """Seed the run's main-config draft the way the dispatcher does, so
    ``session.run`` parses the generator's SHIPPED ``agent-config.set_yaml``
    — schema defaults included — rather than a synthetic cfg."""
    cfg_dir = os.path.join(native_bridge.workspace, ".collrun", "config", "files")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "agent-config.set_yaml"), "w",
              encoding="utf-8") as f:
        json.dump({"values": values}, f)   # JSON is valid YAML


# ── chat on_turn (via _run_chat's captured handler) ────────────────────


def test_on_turn_tool_events_reach_turn_and_emit_usage(native_bridge, monkeypatch):
    """A tool event carrying no text still reaches ``turn.tool`` and accumulates
    into the durable ``turn.append`` tool list (completions pair by id, id
    stripped); a completed turn emits an allow-listed usage event."""
    import gen  # noqa: F401 — native bridge already active

    turn = FakeChatTurn("hello")

    def driver(kw):
        emit = kw["on_event"]
        # A running tool + a NAMELESS completion — neither carries "text".
        emit({"type": "tool", "id": "t1", "name": "read_file",
              "status": "running", "input": {"path": "a.txt"}})
        emit({"type": "tool", "id": "t1", "status": "done", "result": "ok"})
        emit({"type": "text_delta", "text": "answer"})

    result = _Result(
        text="answer", trace=[{"type": "text", "text": "answer"}],
        usage={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12,
               "total_cost_usd": 0.0011, "model": "x", "junk": 9},
    )

    async def fake_run_agent(**kw):
        driver(kw)
        return result

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    holder = _run_on_turn(_base_cfg(), turn, monkeypatch)

    # both tool events (no "text" key) reached turn.tool — the running one AND
    # the nameless completion (regression: a blanket text guard swallowed these).
    assert [(n, s) for n, s, _ in turn.tool_calls] == [
        ("read_file", "running"), ("", "done"),
    ]
    # the assistant record folds them into one paired, id-stripped tool entry.
    assistant = turn.appended[-1]
    assert assistant["content"] == "answer"
    assert assistant["stopped"] is False
    assert assistant["tools"] == [{
        "name": "read_file", "status": "done",
        "input": {"path": "a.txt"}, "result": "ok",
    }]
    # one allow-listed usage event — non-numeric / non-listed keys dropped.
    usage = [e for e in turn.emitted if e.get("type") == "usage"]
    assert usage == [{"type": "usage", "input_tokens": 5, "output_tokens": 7,
                      "total_tokens": 12, "total_cost_usd": 0.0011}]
    # The chat seeds no initial message: its opening turn is queued.
    assert "initial_message" not in holder["serve_kwargs"]


def test_text_after_a_tool_call_starts_a_new_paragraph(native_bridge, monkeypatch):
    """The raw delta stream carries no separator across a tool boundary, so
    the joined reply used to glue the sentences around it ("…writing the
    report.The tests pass…"). Text resuming after a tool call now opens
    a fresh paragraph — in the durable record and the live delta stream."""
    turn = FakeChatTurn("hello")

    def driver(kw):
        emit = kw["on_event"]
        emit({"type": "text_delta", "text": "Writing the report."})
        emit({"type": "tool", "id": "t1", "name": "write_file",
              "status": "running"})
        emit({"type": "tool", "id": "t1", "status": "done"})
        emit({"type": "text_delta", "text": "The tests pass."})

    async def fake_run_agent(**kw):
        driver(kw)
        return _Result(text="", trace=[])

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _run_on_turn(_base_cfg(), turn, monkeypatch)

    assert turn.appended[-1]["content"] == (
        "Writing the report.\n\nThe tests pass.")
    # The live stream got the same break, so the bubble matches the record.
    assert "".join(turn.deltas) == "Writing the report.\n\nThe tests pass."


def test_a_transport_respawn_reply_replaces_the_dead_attempts_partial(native_bridge, monkeypatch):
    """F54: run_acp_turn re-sends the prompt after a transport death and
    signals text_reset first — the durable reply is the retry's answer
    alone, not the dead attempt's fragment glued in front of it."""
    turn = FakeChatTurn("hello")

    def driver(kw):
        emit = kw["on_event"]
        emit({"type": "text_delta", "text": "Half an ans"})
        emit({"type": "text_reset", "reason": "agent transport lost: exit 1"})
        emit({"type": "text_delta", "text": "The whole answer."})

    async def fake_run_agent(**kw):
        driver(kw)
        return _Result(text="The whole answer.", trace=[])

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _run_on_turn(_base_cfg(), turn, monkeypatch)

    assert turn.appended[-1]["content"] == "The whole answer."


def test_no_paragraph_break_when_the_turn_opens_with_a_tool(native_bridge, monkeypatch):
    """The separator marks a text→tool→text boundary only: a turn whose first
    text follows an opening tool call (nothing streamed before it) starts
    clean, and text already ending in a newline is topped up to exactly ONE
    blank line — a single trailing newline is a soft break in markdown (it
    renders as a space), so the sentences would still glue without it."""
    turn = FakeChatTurn("hello")

    def driver(kw):
        emit = kw["on_event"]
        emit({"type": "tool", "id": "t1", "name": "read_file", "status": "running"})
        emit({"type": "text_delta", "text": "First line.\n"})
        emit({"type": "tool", "id": "t2", "name": "read_file", "status": "running"})
        emit({"type": "text_delta", "text": "Second line."})

    async def fake_run_agent(**kw):
        driver(kw)
        return _Result(text="", trace=[])

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _run_on_turn(_base_cfg(), turn, monkeypatch)

    assert turn.appended[-1]["content"] == "First line.\n\nSecond line."


def test_on_turn_stopped_keeps_only_streamed(native_bridge, monkeypatch):
    """When ``turn.cancelled`` flips mid-turn: only pre-cancel chunks persist,
    ``stopped`` rides the append, tools keep only pre-cancel events, no usage
    event fires, and the interrupt note is deduped in the trace."""
    import gen

    turn = FakeChatTurn("hello")

    def driver(kw):
        emit = kw["on_event"]
        emit({"type": "text_delta", "text": "par"})
        emit({"type": "text_delta", "text": "tial"})
        emit({"type": "tool", "id": "x", "name": "search", "status": "running"})
        turn.cancelled = True                       # user interrupts here
        emit({"type": "text_delta", "text": " MORE"})            # swallowed
        emit({"type": "tool", "id": "y", "name": "post_cancel",  # swallowed
              "status": "running"})

    result = _Result(
        text="the full background answer",
        # the watchdog already recorded an interrupt note on the partial trace.
        trace=[{"type": "text", "text": "full answer"},
               {"type": "system_note", "text": "run interrupted by user"}],
        usage={"input_tokens": 9},
    )

    async def fake_run_agent(**kw):
        driver(kw)
        return result

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _run_on_turn(_base_cfg(), turn, monkeypatch)

    assistant = turn.appended[-1]
    assert assistant["content"] == "partial"        # only what streamed pre-cancel
    assert assistant["stopped"] is True
    assert assistant["tools"] == [{"name": "search", "status": "running"}]  # pre-cancel only
    # no usage event on a stopped turn.
    assert not any(e.get("type") == "usage" for e in turn.emitted)
    # the interrupt note isn't doubled — the watchdog's note is the only one.
    persisted = json.loads(gen.read_file(entry.TRACE_FILE))
    notes = [ev for ev in persisted
             if ev.get("type") == "system_note" and "interrupted" in ev.get("text", "")]
    assert len(notes) == 1
    assert not any("turn interrupted by user" == ev.get("text") for ev in persisted)


def test_on_turn_cancelled_turn_stays_in_trace(native_bridge, monkeypatch):
    """A per-turn ``gen.Cancelled`` (an interrupted run
    the turn awaited was cancelled, run NOT stopping)
    pairs the turn AND keeps it in the trace record — the user message, what
    streamed, and ONE interrupt note (never a turn missing from the trace)."""
    import gen

    turn = FakeChatTurn("draw me a cat")

    async def fake_run_agent(**kw):
        kw["on_event"]({"type": "text_delta", "text": "par"})
        raise entry.gen.Cancelled()

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _run_on_turn(_base_cfg(), turn, monkeypatch)

    # the turn is still paired in the transcript (its existing semantics).
    assistant = turn.appended[-1]
    assert assistant["content"] == "par"

    persisted = json.loads(gen.read_file(entry.TRACE_FILE))
    assert persisted[0] == {"type": "user", "text": "draw me a cat"}
    assert {"type": "text", "text": "par"} in persisted
    notes = [ev for ev in persisted if ev.get("type") == "system_note"]
    assert len(notes) == 1 and "interrupted" in notes[0]["text"]


def test_on_turn_cancelled_and_stopped_notes_once(native_bridge, monkeypatch):
    """The cancel path's note doubles as the interrupt note: a turn the user
    also stopped records one, not two."""
    import gen

    turn = FakeChatTurn("hello")

    async def fake_run_agent(**kw):
        turn.cancelled = True
        raise entry.gen.Cancelled()

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _run_on_turn(_base_cfg(), turn, monkeypatch)

    assert turn.appended[-1]["stopped"] is True
    persisted = json.loads(gen.read_file(entry.TRACE_FILE))
    assert [ev["type"] for ev in persisted] == ["user", "system_note"]


def test_on_turn_trace_rides_the_turns_own_flush(native_bridge, monkeypatch):
    """The turn's trace is staged before the turn's flush, so a run killed
    between turns still has the last turn's record committed."""
    turn = FakeChatTurn("hello")
    staged_after_turn: list = []

    async def fake_run_agent(**kw):
        return _Result(text="hi", trace=[{"type": "text", "text": "hi"}])

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        await on_turn(turn)
        staged_after_turn.append(entry.TRACE_FILE in native_bridge.tracked_writes)

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())
    asyncio.run(entry.run_session(_base_cfg()))
    assert staged_after_turn == [False]


def test_tools_from_trace_pairs_fifo_by_name():
    """Backends with trace-only tools (pydantic-ai, end-parsed CLI turns) get
    a display record folded post-turn: use→entry, result→the earliest waiting
    entry of the same tool (any name when that tool has none waiting)."""
    acc: list = []
    entry._tools_from_trace(acc, [
        {"type": "user", "text": "q"},
        {"type": "tool_use", "tool": "web_search", "input": {"q": "x"}},
        {"type": "tool_result", "output": "3 hits"},
        {"type": "tool_use", "tool": "read_file", "input": "a.py"},
        {"type": "text", "text": "done"},
    ])
    assert acc == [
        {"id": "", "name": "web_search", "status": "done",
         "input": '{"q": "x"}', "result": "3 hits"},
        {"id": "", "name": "read_file", "status": "done", "input": "a.py"},
    ]


def test_tools_from_trace_pairs_parallel_calls_by_name():
    """Two calls in flight at once: results that come back in the reverse
    order still land on their own call, and a result with no call waiting
    becomes an entry of its own."""
    acc: list = []
    entry._tools_from_trace(acc, [
        {"type": "tool_use", "tool": "read_file", "input": "a.py"},
        {"type": "tool_use", "tool": "web_search", "input": {"q": "x"}},
        {"type": "tool_result", "tool": "web_search", "output": "3 hits"},
        {"type": "tool_result", "tool": "read_file", "output": "a.py body"},
        {"type": "tool_result", "tool": "write_file", "output": "wrote b.py"},
    ])
    assert acc == [
        {"id": "", "name": "read_file", "status": "done",
         "input": "a.py", "result": "a.py body"},
        {"id": "", "name": "web_search", "status": "done",
         "input": '{"q": "x"}', "result": "3 hits"},
        {"id": "", "name": "write_file", "status": "done", "result": "wrote b.py"},
    ]


# ── terminal.with_terminal_skills ────────────────────────────────


def test_with_terminal_skills_appends_index_F1585_F1598(native_bridge, monkeypatch):
    """A terminal surface's system prompt gains the runner's skills index
    when a skill is present in the _skills config folder (every backend's
    spec takes skills); the index points the harness's file tools at the
    container-visible folder path (staged under .collrun). No skills → no
    index. A run's cell caps are read once, so the no-skills case is a fresh
    run (cache reset)."""
    import os
    import shutil

    folder = os.path.join(
        native_bridge.workspace, ".collrun", "config", "folders", "_skills",
    )
    os.makedirs(os.path.join(folder, "pirate"), exist_ok=True)
    with open(os.path.join(folder, "pirate", "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: pirate\ndescription: Arr.\n---\ntalk like a pirate")

    cfg = _base_cfg(system_prompt="base prompt")
    out = terminal.with_terminal_skills(cfg, _paths())
    assert out.startswith("base prompt")
    assert "The current date and time is" in out   # the wall clock rides every arm
    assert "pirate — Arr." in out
    assert "[.collrun/config/folders/_skills/pirate/SKILL.md]" in out
    # No skills provided (a fresh run) → clock only, no index, even when on.
    shutil.rmtree(folder)
    monkeypatch.setattr(eng, "_RUN_CELL_CAPS", {})
    none = terminal.with_terminal_skills(_base_cfg(
        system_prompt="base prompt"), _paths())
    assert none.startswith("base prompt")
    assert "The current date and time is" in none
    assert "SKILL.md" not in none


def test_the_terminal_skills_index_comes_from_the_runs_cell_caps(native_bridge,
                                                                  monkeypatch):
    """The terminal reads the same cell capabilities as every other leg of
    the run (``discover_cell_caps``), not a fresh read of the config
    folders."""
    from _agent_shared.cell_caps import CellCaps, Skill

    caps = CellCaps(skills=[Skill(name="pirate", description="Arr.", body="",
                                  dir="_skills/pirate")])
    monkeypatch.setattr(eng, "discover_cell_caps", lambda: caps)
    out = terminal.with_terminal_skills(_base_cfg(system_prompt="base"), _paths())
    assert "pirate — Arr." in out


def test_read_turn_attachments_carry_the_cell_path(native_bridge):
    """The turn's attachment inputs name the file's CELL location
    (attachments/<name>) so the engine references it instead of staging a
    copy elsewhere."""
    import gen

    gen.update_file("attachments/pic.png", b"\x89PNG")
    turn = FakeChatTurn("look", attachments=[
        {"path": "attachments/pic.png", "name": "pic.png", "media_type": "image/png"},
        {"path": "attachments/missing.png", "name": "missing.png", "media_type": "image/png"},
    ])
    out = media._read_turn_attachments(turn)
    assert [(a["path"], a["media_type"], a["data"]) for a in out] == [
        ("attachments/pic.png", "image/png", b"\x89PNG")]


# ── run end: live interaction files are tools ──────────────────────────


# The run's surface files, relative to its run folder.
_LIVE_NAMES = (
    "chat/conversation.chat",
    "chat/main.json",
)


def _live() -> list:
    """The installed run's surface files (cell-relative)."""
    folder = _paths().folder
    return [f"{folder}/{name}" for name in _LIVE_NAMES]


def _seed_live_files():
    """A cell mid-session: every live interaction file of this run, plus the
    artifacts that must SURVIVE the run end."""
    import gen

    gen.update_ui_file("chat/conversation.chat", '{"chat_id": "s1"}')
    gen.update_ui_file("chat/main.json", '{"messages": []}')
    # what the agent produced: untouched by the run end
    gen.update_file("report.md", "# the work\n")
    gen.update_file("notes.llm_summary", "# where things stand\n")


# ── workspace media on the one-shot's opening turn ─────────────────────


def test_workspace_media_inputs_filter_and_shape_F1585_F1598(native_bridge):
    """Media-typed extensions only, the ``.jpeg`` special case, ``_agent/``
    and runs' folders skipped, empty skipped, non-media skipped, stable
    sorted order, engine input shape."""
    import gen

    gen.update_file("photo.png", _PNG)
    gen.update_file("clip.jpeg", _PNG)
    gen.update_file("notes.txt", "hi")
    gen.update_file("_agent/keep.png", _PNG)
    native_bridge.add_foreign_run("peer-1234abcd", {"shot.png": _PNG})
    gen.update_file("empty.png", b"")

    out = media._workspace_media_inputs()
    assert [a["name"] for a in out] == ["clip.jpeg", "photo.png"]
    by_name = {a["name"]: a for a in out}
    assert by_name["clip.jpeg"]["media_type"] == "image/jpeg"
    assert by_name["photo.png"]["media_type"] == "image/png"
    assert by_name["photo.png"]["data"] == _PNG
    assert set(out[0].keys()) == {"data", "media_type", "name", "path"}


def test_workspace_media_inputs_cap_and_skip_oversized(native_bridge, monkeypatch):
    import gen

    monkeypatch.setattr(media, "_WORKSPACE_MEDIA_MAX", 2)
    monkeypatch.setattr(media, "_WORKSPACE_MEDIA_BYTE_CAP", 4)
    gen.update_file("big.png", b"toolarge")
    for i in range(5):
        gen.update_file(f"s{i}.png", b"ok")
    real_read = gen.read_file
    read: list = []

    def spy_read(path):
        read.append(path)
        return real_read(path)

    monkeypatch.setattr(gen, "read_file", spy_read)
    assert [a["name"] for a in media._workspace_media_inputs()] == ["s0.png", "s1.png"]
    # An oversized file is skipped on its size alone, never read whole.
    assert "big.png" not in read and "s0.png" in read


def test_the_opening_turn_carries_the_workspace_media(native_bridge, monkeypatch):
    """A user drops files into the run workspace; the one-shot's opening
    turn feeds the media among them to the model."""
    import gen

    gen.update_file("photo.png", _PNG)
    gen.update_file("notes.md", "text")
    seen: list = []

    async def fake_run_agent(**kw):
        seen.append([a["path"] for a in (kw.get("attachments") or [])])
        return _Result(text="ok")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _drive_oneshot(native_bridge, monkeypatch, "describe the picture")
    assert seen == [["photo.png"]]


def test_interactive_turns_carry_only_their_own_attachments(native_bridge, monkeypatch):
    import gen

    gen.update_file("photo.png", _PNG)
    seen: list = []

    async def fake_run_agent(**kw):
        seen.append([a["path"] for a in (kw.get("attachments") or [])])
        return _Result(text="ok")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _run_on_turn(_base_cfg(session="interactive", surface="chat"), FakeChatTurn("hi"), monkeypatch)
    assert seen == [[]]


# ── failed and stopped turns (the turn boundary must know) ─────────────


def _clear_session_state():
    from collimate import native as n
    n._turn_queue.clear()
    n._finish_requested[0] = False


def _spy_submits(monkeypatch) -> list:
    """Record the source of every turn the session queues, as it is queued
    (a real serve_chat drains the queue as it runs the turns)."""
    submitted: list = []
    real_submit = entry.gen.session.submit_turn
    monkeypatch.setattr(entry.gen.session, "submit_turn",
                        lambda text, **kw: (submitted.append(kw.get("source")),
                                            real_submit(text, **kw)))
    return submitted


def test_a_failed_opening_turn_ends_the_run_failed_with_a_note_under_the_prompt(
        native_bridge, monkeypatch):
    """An opening turn whose run_agent raised is not an answer: the run ends
    FAILED naming the error (so a caller gets ok=False), posts the failure
    under the prompt message, runs no correction turn against a provider
    that just refused it, and the run log names the real cause."""
    native_bridge.runner = {"runner": "probe-runner"}
    finishes: list = []
    logs: list = []
    monkeypatch.setattr(entry.gen.session, "request_finish", lambda: finishes.append(1))
    monkeypatch.setattr(entry.gen, "log", lambda line: logs.append(line))
    calls: list = []

    async def fake_run_agent(**kw):
        calls.append(kw["prompt"])
        raise RuntimeError("401 Unauthorized: invalid x-api-key")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    submitted = _spy_submits(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="401 Unauthorized"):
            _drive_oneshot(native_bridge, monkeypatch)
        assert len(calls) == 1 and "do the thing" in calls[0]
        assert submitted == [consts.Sources.OPENING], "a failed turn must queue no correction"
        assert finishes == [1]
        (note,) = native_bridge.annotation_posts
        assert note["text"].startswith("_(This turn failed:") and "401" in note["text"]
        prompt = next(m for m in native_bridge.annotation_context() if m.kind == "prompt")
        assert note["reply_to"] == prompt.id
        assert any(line.startswith("turn failed:") and "401" in line for line in logs)
    finally:
        _clear_session_state()


def test_a_failed_batch_turn_reaches_the_log_and_its_thread(native_bridge, monkeypatch):
    """An annotation run has no transcript: a batch turn that fails says so
    in the thread it was answering (and keeps its messages open), the run
    log names the cause, and no correction turn is thrown at the same
    failure."""
    logs: list = []
    monkeypatch.setattr(entry.gen, "log", lambda line: logs.append(line))

    async def fake_run_agent(*, on_event, **kw):
        raise RuntimeError("Connection error.")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(annotation, "start", lambda sess: None)
    mid = native_bridge.add_annotation("do the thing")
    submitted = _spy_submits(monkeypatch)
    turn = FakeChatTurn(consts.Sources.ANNOTATIONS)
    turn.source = consts.Sources.ANNOTATIONS
    try:
        _run_on_turn(_base_cfg(surface="annotation"), turn, monkeypatch)
        (note,) = native_bridge.annotation_posts
        assert note["reply_to"] == mid and "Connection error." in note["text"]
        assert "come back with your next message" in note["text"]
        assert any(line.startswith("turn failed:") and "Connection error." in line
                   for line in logs)
        assert submitted == []
    finally:
        _clear_session_state()


def test_failed_turn_reply_and_trace_carry_the_explained_failure(
        native_bridge, monkeypatch):
    """explain_turn_failure's hint reaches the transcript reply and the
    trace note — a Claude Code version floor names the CLI bundled in
    Collimate, not a `claude update` that would not help."""
    import gen

    async def fake_run_agent(**kw):
        raise RuntimeError(
            "API Error: 400 Claude Code 2.1.232 does not support this model; "
            "version 2.1.280 or newer is required.")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    turn = FakeChatTurn("hi")
    _run_on_turn(_base_cfg(backend="claude-code", runtime="docker"), turn,
                 monkeypatch)
    reply = turn.appended[-1]["content"]
    assert reply.startswith("_(error: ") and "bundled in Collimate" in reply
    trace = json.loads(gen.read_file(entry.TRACE_FILE))
    notes = [ev["text"] for ev in trace if ev.get("type") == "system_note"]
    assert any(t.startswith("turn failed: API Error") for t in notes)


# ── host refusals versus every other PermissionError ──────────────────


def test_a_permission_error_that_is_no_host_refusal_fails_the_oneshot(
        native_bridge, monkeypatch):
    """Harness preparation that hits EACCES is not a write the host
    refused: the one-shot ends FAILED with that message, posts the failure
    under the prompt (never a placeholder answer) and queues no
    correction."""
    native_bridge.runner = {"runner": "probe-runner"}
    monkeypatch.setattr(entry.gen.session, "request_finish", lambda: None)

    async def fake_run_agent(**kw):
        raise PermissionError(13, "Permission denied", "/home/u/.codex/config.toml")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    submitted = _spy_submits(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="Permission denied"):
            _drive_oneshot(native_bridge, monkeypatch)
        assert submitted == [consts.Sources.OPENING]
        (note,) = native_bridge.annotation_posts
        assert note["text"].startswith("_(This turn failed:") and "Permission denied" in note["text"]
        assert "write refused" not in note["text"]
    finally:
        _clear_session_state()


def test_a_permission_error_that_is_no_host_refusal_fails_a_chat_turn(
        native_bridge, monkeypatch):
    async def fake_run_agent(**kw):
        raise PermissionError("Harness native config contains a linked path")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    submitted = _spy_submits(monkeypatch)
    turn = FakeChatTurn("hi")
    try:
        _run_on_turn(_base_cfg(surface="chat"), turn, monkeypatch)
        reply = turn.appended[-1]["content"]
        assert reply.startswith("_(error: ") and "linked path" in reply
        assert submitted == []
    finally:
        _clear_session_state()


def test_a_host_refusal_inside_the_turn_queues_a_correction_and_posts_no_placeholder(
        native_bridge, monkeypatch):
    """``gen.PartialRefusal`` is the one PermissionError a correction can
    fix: the correction turn follows, and the transcript placeholder of the
    refused turn is never posted as the answer."""
    from collimate._host.bridge import PartialRefusal

    native_bridge.runner = {"runner": "probe-runner"}
    monkeypatch.setattr(entry.gen.session, "request_finish", lambda: None)
    replies = []

    async def fake_run_agent(**kw):
        if not replies:
            replies.append(1)
            raise PartialRefusal("notes.md refused", paths=["notes.md"],
                                 refused=[{"path": "notes.md", "error": "bad front matter"}])
        return _Result(text="I fixed notes.md")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    st, turns, _, _ = _drive_oneshot(native_bridge, monkeypatch)
    assert [t.source for t in turns] == [consts.Sources.OPENING, consts.Sources.CORRECTION]
    assert "bad front matter" in turns[1].user_msg
    assert turns[0].appended[-1]["content"] == "_(write refused; a correction turn follows)_"
    assert native_bridge.annotation_posts == []
    assert st.finish_requested and not st.oneshot_error


@pytest.mark.parametrize("kind", ["nak", "refusal"])
def test_a_batch_turn_that_fails_before_its_reply_never_posts_an_earlier_answer(
        native_bridge, monkeypatch, kind):
    """A flush that raises before the engine runs leaves the turn with no
    reply of its own: the batch stays open with a failure note, and the
    previous turn's answer is never posted again. A host refusal also
    queues its correction; any other PermissionError does not."""
    from collimate._host.bridge import PartialRefusal

    monkeypatch.setattr(annotation, "start", lambda sess: None)

    async def fake_run_agent(**kw):
        return _Result(text="the first answer")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    error = (PermissionError("permission denied for 'update_cell'") if kind == "nak"
             else PartialRefusal("x.md refused", paths=["x.md"]))
    fail = [False]
    real_update = entry.gen.commit

    async def update(**kw):
        if fail[0]:
            fail[0] = False
            raise error
        return await real_update(**kw)

    monkeypatch.setattr(entry.gen, "commit", update)
    submitted = _spy_submits(monkeypatch)
    sessions: list = []
    real_session = entry.AgentSession
    monkeypatch.setattr(entry, "AgentSession",
                        lambda *a, **k: sessions.append(real_session(*a, **k)) or sessions[-1])
    first = native_bridge.add_annotation("first question")
    raised: list = []

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        turn = _QueuedTurn(consts.Sources.ANNOTATIONS, source=consts.Sources.ANNOTATIONS)
        await on_turn(turn)
        native_bridge.add_annotation("second question")
        fail[0] = True
        turn = _QueuedTurn(consts.Sources.ANNOTATIONS, source=consts.Sources.ANNOTATIONS)
        try:
            await on_turn(turn)
        except Exception as exc:  # noqa: BLE001 — serve_chat logs a raising turn
            raised.append(exc)

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda **kw: _NullCM())
    try:
        asyncio.run(entry.run_session(_base_cfg(surface="annotation")))
        posts = native_bridge.annotation_posts
        second = next(m for m in native_bridge.annotation_context() if m.text == "second question")
        assert (posts[0]["text"], posts[0]["reply_to"]) == ("the first answer", first)
        assert len(posts) == 2 and posts[1]["reply_to"] == second.id
        assert posts[1]["text"].startswith("_(This turn failed:")
        assert [m.id for m in sessions[0].annotations.open] == [second.id]
        assert submitted == ([] if kind == "nak" else [consts.Sources.CORRECTION])
        assert raised == ([error] if kind == "nak" else [])
    finally:
        _clear_session_state()


# ── the comment ledger: nothing dispatched is silently lost ────────────


# ── comments from the person and from other runs ─────────────────────

_PEER = "peer-1234abcd"


def test_every_turn_says_which_run_it_is(native_bridge, monkeypatch):
    prompts: list = []

    async def fake_run_agent(**kw):
        prompts.append(kw["system_prompt"])
        return _Result(text="ok")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _run_on_turn(_base_cfg(surface="chat", system_prompt="base"),
                 FakeChatTurn("hi"), monkeypatch)
    assert prompts[0].startswith("base")
    assert consts.identity_directive(_paths()) in prompts[0]
    assert f"You are run {native_bridge.label}." in prompts[0]


# ── the run's config and cell label ────────────────────────────────────


@pytest.mark.parametrize("surface, label", [("chat", "chat · pydantic-ai"),
                                            ("annotation", "annotation · pydantic-ai")])
def test_the_cell_label_is_sent_only_when_it_changes(native_bridge, monkeypatch,
                                                     surface, label):
    """A run names its cell after its surface and backend. The host takes
    a sent name over the current one, so re-sending the label every turn
    would undo a rename the user made mid-session."""
    names: list = []
    real_update = entry.gen.commit

    async def spy_update(**kw):
        names.append(kw.get("name"))
        await real_update(**kw)

    async def fake_run_agent(**kw):
        return _Result(text="ok")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(entry.gen, "commit", spy_update)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())

    # Each surface's own turns: an annotation run's batches, chat's messages.
    source = consts.Sources.ANNOTATIONS if surface == "annotation" else ""
    for text in ("one", "two", "three"):
        native_bridge.add_annotation(text) if surface == "annotation" else None
    monkeypatch.setattr(annotation, "start", lambda sess: None)

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        for text in ("one", "two", "three"):
            turn = FakeChatTurn(text)
            turn.source = source
            await on_turn(turn)

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    try:
        asyncio.run(entry.run_session(_base_cfg(surface=surface)))
    finally:
        _clear_session_state()
    labels = [n for n in names if n]
    assert labels == [label]


def test_a_cancelled_turn_with_nothing_streamed_says_cancelled(native_bridge,
                                                               monkeypatch):
    """A per-turn cancel pairs the turn with a neutral placeholder — the
    API-key dialog the old "(no API key provided)" text described is gone."""
    async def fake_run_agent(**kw):
        raise entry.gen.Cancelled()

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    turn = FakeChatTurn("hi")
    _run_on_turn(_base_cfg(), turn, monkeypatch)
    assert turn.appended[-1]["content"] == "_(turn cancelled)_"


def test_the_run_closes_its_acp_sessions_on_its_own_loop_first_F1583(native_bridge, monkeypatch):
    """The run's teardown awaits the ACP sessions' close on the run's own
    loop — the adapter children exit and their subprocess transports close
    while the loop still runs — and only then tears down the containers and
    bridges, even when the session itself failed."""
    import _agent_shared.acp_run as acp_run
    import _agent_shared.cli_container as cli_container

    native_bridge._rec_accounts["model"] = {
        "service": "openai", "protocol": "openai", "model": "gpt-4o",
        "auth_type": "api_key", "secrets": {"api_key": "test-key"}}
    _seed_main_config(native_bridge, {"session": "oneshot"})
    order: list = []

    async def close_sessions():
        order.append(("sessions", asyncio.get_running_loop().is_running()))

    async def failing_session(cfg, **kw):
        raise RuntimeError("the session failed")

    monkeypatch.setattr(acp_run, "close_acp_sessions", close_sessions)
    def close_containers():
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        order.append(("close_all", on_loop))
    monkeypatch.setattr(cli_container, "close_all", close_containers)
    monkeypatch.setattr(entry, "run_session", failing_session)
    monkeypatch.setattr(session, "stage_builtin_skills", lambda source, **kw: [])
    with pytest.raises(RuntimeError, match="the session failed"):
        asyncio.run(session.run(here=_PACKAGE))
    assert order == [("sessions", True), ("close_all", False)]


# ── the configured surface ─────────────────────────────────────────────


_OPENAI_KEY = {"service": "openai", "protocol": "openai", "model": "gpt-4o",
               "auth_type": "api_key", "secrets": {"api_key": "test-key"}}


@pytest.mark.parametrize("values, files, broadcast, skills", [
    ({"surface": "annotation", "session": "interactive"}, False, True,
     ("annotations", "llm-summary")),
    ({"surface": "chat"}, True, False, ("llm-summary",)),
    ({"surface": "terminal", "backend": "opencode"}, False, False, ("llm-summary",)),
])
def test_each_configured_surface_runs_through_session_run_GN02(native_bridge, monkeypatch,
                                                               values, files, broadcast,
                                                               skills):
    """Through ``session.run``, as the entrypoint calls it: the generator's
    schema parses the runner's settings, the run opens on the configured
    surface (only chat serves its chat files, only annotation a broadcast
    inbox) at the file the manifest's ``opens`` names — none for annotation
    — and the surface's own skills are staged from the runtime's skills
    folder."""
    import gen
    from collimate.config_values import resolve_fields
    from collimate.surfaces import resolve_opens
    native_bridge._rec_accounts["model"] = dict(_OPENAI_KEY)
    native_bridge.permissions = {"docker": "full", "internet": "full"}
    _seed_main_config(native_bridge, values)
    sessions: list = []
    served: list = []
    staged: list = []
    real = entry.AgentSession

    def capture(*a, **kw):
        st = real(*a, **kw)
        sessions.append(st)
        return st

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        served.append((kw["files"], kw["broadcast"], kw["pointer_file"]))
        kw["on_ready"]()
        await asyncio.sleep(0.05)

    async def no_tty(cfg, *, paths, on_ready=None, selected=None):
        return None

    monkeypatch.setattr(entry, "AgentSession", capture)
    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())
    monkeypatch.setattr(terminal, "terminal_leg", no_tty)
    monkeypatch.setattr(session, "stage_builtin_skills",
                        lambda source, *, names=None: staged.append((source, names)) or [])
    try:
        asyncio.run(session.run(here=_PACKAGE))
    finally:
        _clear_session_state()
    (st,) = sessions
    surface = values["surface"]
    assert st.surface == surface
    fields = session.config_fields(_PACKAGE)
    opens = resolve_opens(session.manifest(_PACKAGE), fields, resolve_fields(fields, values),
                          folder=gen.run_info().folder)
    if surface == "annotation":
        assert opens == []
    else:
        surface_file = st.paths.chat_pointer if surface == "chat" else st.paths.terminal_pointer
        assert [surface_file] == opens
    assert served == [(files, broadcast, st.paths.chat_pointer)]
    assert staged == [(session.SKILLS_DIR, skills)]
    for name in skills:
        assert os.path.isfile(os.path.join(session.SKILLS_DIR, name, "SKILL.md"))


@pytest.mark.parametrize("docker", ["none", "limited"])
def test_a_terminal_run_without_docker_full_is_refused_before_work(
        native_bridge, monkeypatch, docker):
    """A terminal run is a harness's TTY in its container: without docker:
    full the run refuses at start — no skill staged, no session opened."""
    native_bridge._rec_accounts["model"] = dict(_OPENAI_KEY)
    native_bridge.permissions = {"docker": docker, "internet": "full"}
    _seed_main_config(native_bridge, {"surface": "terminal", "backend": "opencode"})
    monkeypatch.setattr(session, "stage_builtin_skills",
                        lambda *a, **k: pytest.fail("staged before refusal"))
    monkeypatch.setattr(entry, "run_session",
                        lambda *a, **k: pytest.fail("session before refusal"))
    with pytest.raises(ValueError, match="the terminal surface needs docker: full"):
        asyncio.run(session.run(here=_PACKAGE))


_SURFACE_SCOPES = [({"surface": "chat"}, "chat", ["{run}/chat/conversation.chat"]),
                   ({"surface": "terminal", "backend": "claude-code"}, "terminal",
                    ["{run}/terminal/claude-code.terminal"]),
                   ({"surface": "terminal", "backend": "codex"}, "terminal",
                    ["{run}/terminal/codex.terminal"]),
                   ({"surface": "annotation"}, "annotation", []),
                   ({"surface": "annotation", "session": "oneshot"}, "annotation", [])]


@pytest.mark.parametrize("values, surface, expected", _SURFACE_SCOPES)
def test_session_paths_are_the_manifests_opens_GN03(native_bridge, values, surface, expected):
    """The host creates the file the manifest ``opens`` as the run's starting
    placeholder; the run serves the same file. ``session.surface_opens``
    resolves the shipped declaration with the host's resolver for every
    surface, and ``SessionPaths`` takes the surface file from it; an
    annotation run opens none and keeps the default paths (its inbox
    registers at the chat pointer path)."""
    import gen
    from collimate.config_values import resolve_fields
    from collimate.surfaces import resolve_opens
    folder = gen.run_info().folder
    fields = session.config_fields(_PACKAGE)
    opens = resolve_opens(session.manifest(_PACKAGE), fields, resolve_fields(fields, values),
                          folder=folder)
    assert opens == session.surface_opens(_PACKAGE, values) == [
        e.replace("{run}", folder) for e in expected]
    cfg = {**_base_cfg(surface=surface), "opens": opens}
    backend = resolve_fields(fields, values)["backend"]
    paths = consts.SessionPaths.for_run(backend, cfg=cfg)
    defaults = consts.SessionPaths.for_run(backend)
    if surface == "chat":
        assert paths.chat_pointer == opens[0]
    elif surface == "terminal":
        assert paths.terminal_pointer == opens[0]
    else:
        assert (paths.chat_pointer, paths.terminal_pointer) == (
            defaults.chat_pointer, defaults.terminal_pointer)


def test_session_paths_refuse_an_empty_opens_and_an_annotation_opens_GN03(native_bridge):
    folder = _paths().folder
    with pytest.raises(ValueError, match="names no file"):
        consts.SessionPaths.for_run("pydantic-ai", cfg={**_base_cfg(surface="chat"), "opens": []})
    with pytest.raises(ValueError, match="opens no file"):
        consts.SessionPaths.for_run("pydantic-ai", cfg={
            **_base_cfg(surface="annotation"), "opens": [f"{folder}/notes.view"]})


# ── the run's paths and directives ────────────────────────────────────


_SYNTHETIC = consts.SessionPaths(
    label="tester-0badc0de", folder="_runs/tester-0badc0de", backend="codex")


def test_the_session_paths_live_in_the_run_folder():
    p = _SYNTHETIC
    assert p.chat_pointer == "_runs/tester-0badc0de/chat/conversation.chat"
    assert p.chat_threads == "_runs/tester-0badc0de/chat"
    assert p.terminal_pointer == "_runs/tester-0badc0de/terminal/codex.terminal"
    assert p.run_card == "_runs/tester-0badc0de/tester-0badc0de.run"
    assert not hasattr(p, "file_target")


def test_the_annotation_directive_names_the_annotate_tool_and_the_oneshot_end():
    """The user sees the annotation context and the files, not this
    conversation; replies go through ``annotate`` in their thread; a visual
    answer is a self-contained HTML file; a one-shot is told how it ends."""
    d = consts.annotation_directive(oneshot=False)
    assert "does not see this conversation" in d
    assert "annotate(text, reply_to=<thread>)" in d
    assert "<message>" in d and "<context>" in d and "<file>" in d
    assert "self-contained .html" in d
    assert "annotations skill" in d
    assert "one-shot" not in d
    o = consts.annotation_directive(oneshot=True)
    assert o.startswith(d) and "one-shot run" in o and "ends once" in o


def test_every_run_is_told_who_it_is_among_the_cells_runs():
    """The identity line: the run's label, other runs' folders read-only, the
    llm-summary skill for a summary or a handoff, and the instructions'
    paths over any a skill names — and nothing that makes the run folder a
    place for work files."""
    line = consts.identity_directive(_SYNTHETIC)
    assert line.startswith("You are run tester-0badc0de.")
    assert "Everything under _runs/ that is not your own folder" in line
    assert "read-only to you" in line
    assert "llm-summary skill" in line
    assert "override any path a skill names" in line
    assert "comment" not in line and "status-" not in line
    assert "write your work" not in line and "work files there" not in line


def test_an_annotation_run_writes_no_ui_file_and_serves_a_broadcast_inbox(
        native_bridge, monkeypatch):
    """An annotation run's turns carry the annotation directive and the
    annotation tools; its inbox is served transport-only and broadcast, so
    every viewer of the run's status pane gets its approval cards, and the
    run writes no UI file. A chat run carries neither."""
    import gen

    seen: list = []

    async def fake_run_agent(**kw):
        seen.append((kw["system_prompt"], kw["annotation_tools"]))
        return _Result(text="ok")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(annotation, "start", lambda sess: None)
    native_bridge.add_annotation("look at this")
    turn = FakeChatTurn(consts.Sources.ANNOTATIONS)
    turn.source = consts.Sources.ANNOTATIONS
    holder = _run_on_turn(_base_cfg(surface="annotation"), turn, monkeypatch)
    kw = holder["serve_kwargs"]
    assert (kw["files"], kw["broadcast"]) == (False, True)
    assert "initial_message" not in kw
    (prompt, tools), = seen
    assert consts.annotation_directive(oneshot=False) in prompt and tools is True
    assert not any(p.startswith(_paths().folder + "/")
                   for p in list(gen.list_files()) if not p.endswith(".run"))
    seen.clear()
    _run_on_turn(_base_cfg(surface="chat"), FakeChatTurn("hi"), monkeypatch)
    (prompt, tools), = seen
    assert "annotation" not in prompt and tools is False


# ── the terminal's first message ───────────────────────────────────────


def test_the_terminal_opens_on_the_selected_file_and_the_prompt(
        native_bridge, monkeypatch):
    """Every terminal runner takes the same kwargs: ``first_message`` is the
    selected file, then the run's prompt (A6: shown for claude-code, the TTY
    that takes a first message); else "" (a TUI that takes no opening
    message ignores it)."""
    from _agent_shared import terminal_tail
    from _agent_shared.session.selected import SelectedFile

    import _agent_shared.acp_run as acp_run

    async def mint(cfg):
        return "sid"

    monkeypatch.setattr(acp_run, "mint_acp_session", mint)
    monkeypatch.setattr(terminal, "with_terminal_skills", lambda cfg, paths: "")
    monkeypatch.setattr(terminal_tail, "has_tail_reader", lambda backend: False)
    seen: list = []

    async def runner(cfg, *, pointer, extra_system="", resume_harness_session_id="",
                     first_message="", on_ready=None):
        seen.append(first_message)

    monkeypatch.setattr(eng, "surface_runner", lambda backend, ui: runner)

    def leg(selected=None):
        asyncio.run(terminal.terminal_leg(
            _base_cfg(backend="codex"), paths=_paths("codex"), selected=selected))

    leg()
    native_bridge.prompt = " fix the build "
    leg()
    leg(SelectedFile("notes.md", "the selected file"))
    assert seen == ["", "fix the build", "the selected file\n\nfix the build"]


# ── run end (J7) ───────────────────────────────────────────────────────


def _card(bridge) -> str:
    """The host's run card, as its hydration puts it in the workspace."""
    card = f"{bridge.folder}/{bridge.label}.run"
    full = os.path.join(bridge.workspace, card)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write('{"v": 1}')
    return card


def test_run_end_logs_every_live_file_and_clears_it_from_the_cell(native_bridge):
    """The run's surface files — pointers (a terminal's included) and the
    chat transcript — are written to the run's LOG under the same path and
    deleted from the cell in one flush; what the agent produced (a summary
    included), the host's run card and every other run's files stay."""
    import gen

    _seed_live_files()
    gen.update_ui_file("terminal/codex.terminal", '{"terminal_id": "s3"}')
    card = _card(native_bridge)
    native_bridge.add_foreign_run("peer-1234abcd", {"chat/main.json": "{}"})
    asyncio.run(gen.commit())
    deleted: list = []
    orig = native_bridge._commit_update

    def spy(**kw):
        deleted.extend(kw.get("deleted_files") or [])
        return orig(**kw)

    native_bridge._commit_update = spy
    st = entry.AgentSession(_base_cfg(surface="chat"))
    asyncio.run(run_end.finish(st))

    live = _live() + [f"{native_bridge.folder}/terminal/codex.terminal"]
    for path in live:
        assert path in native_bridge.log_files, f"{path} was not logged"
        assert path in deleted, f"{path} was not deleted from the cell"
        assert path not in gen.list_files(), f"{path} survived in the cell"
    assert native_bridge.log_files[f"{native_bridge.folder}/chat/main.json"] == \
        '{"messages": []}'
    for keep in ("report.md", "notes.llm_summary", card,
                 "_runs/peer-1234abcd/chat/main.json"):
        assert gen.has_file(keep), f"{keep} must survive the run end"
        assert keep not in native_bridge.log_files and keep not in deleted
    assert native_bridge.revealed == []


def test_run_end_is_a_noop_without_live_files(native_bridge):
    import gen

    gen.update_file("report.md", "x")
    before = len(native_bridge.updates)
    asyncio.run(run_end.finish(entry.AgentSession(_base_cfg(surface="chat"))))
    assert native_bridge.log_files == {}
    assert len(native_bridge.updates) == before
    assert native_bridge.revealed == []


def test_hard_stop_reaches_the_run_end(native_bridge, monkeypatch):
    """Stop: serve_chat unwinds with Cancelled and ``run_session``'s finally
    still logs-and-clears the live files. The flush is explicit because
    gen.main's auto-flush never runs on that path."""
    import gen

    async def stopped_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        _seed_live_files()
        raise gen.Cancelled("stopped by user")

    monkeypatch.setattr(entry.gen, "serve_chat", stopped_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())
    with pytest.raises(gen.Cancelled):
        asyncio.run(entry.run_session(_base_cfg(surface="chat")))
    for path in _live():
        assert path in native_bridge.log_files
        assert path not in gen.list_files()


def test_the_run_end_does_not_wait_past_the_hosts_stop_grace(native_bridge, monkeypatch):
    """The host kills a stopping run 15 s after SIGTERM: the run end waits
    for the surface loop only ``RUN_END_TASK_WAIT_S`` (8 s) before clearing
    the live files, even when the loop will not unwind."""
    import gen

    assert consts.RUN_END_TASK_WAIT_S == 8.0
    monkeypatch.setattr(entry, "RUN_END_TASK_WAIT_S", 0.2)

    async def stubborn(self, ready):
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(entry.AgentSession, "surface_loop", stubborn)

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        _seed_live_files()

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())

    async def _flow():
        t0 = asyncio.get_running_loop().time()
        await entry.run_session(_base_cfg(surface="chat"))
        return asyncio.get_running_loop().time() - t0

    loop = asyncio.new_event_loop()
    try:
        elapsed = loop.run_until_complete(_flow())
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.close()
    assert elapsed < 3
    chat = f"{native_bridge.folder}/chat/conversation.chat"
    assert chat in native_bridge.log_files
    assert chat not in gen.list_files()


# ── the terminal run (J2) ──────────────────────────────────────────────


def _terminal_session(backend="codex"):
    return entry.AgentSession(_base_cfg(surface="terminal", backend=backend))


def _stub_terminal_leg(monkeypatch, runner):
    import _agent_shared.acp_run as acp_run
    from _agent_shared import terminal_tail

    async def mint(cfg):
        return "sid-mint"

    monkeypatch.setattr(acp_run, "mint_acp_session", mint)
    monkeypatch.setattr(terminal, "with_terminal_skills", lambda cfg, paths: "")
    monkeypatch.setattr(terminal_tail, "has_tail_reader", lambda backend: False)
    monkeypatch.setattr(eng, "surface_runner", lambda backend, ui: runner)


def _stop_race(native_bridge, monkeypatch, *, teardown_s: float,
               bound: "float | None" = None) -> dict:
    """A terminal run Stopped the way that races: the runner's own poll sees
    the Stop and starts its teardown (off the loop, as every runner's is)
    BEFORE serve_chat returns. Returns the teardown's and the run end's
    times after the Stop, and whether the export had finished at the run
    end."""
    import threading
    import time

    import gen

    if bound is not None:
        monkeypatch.setattr(entry, "RUN_END_TASK_WAIT_S", bound)
    release = threading.Event()
    marks: dict = {}
    t0: list = []

    def _export() -> None:                          # the exporter backfill
        release.wait(teardown_s)
        marks["exported"] = time.monotonic() - t0[0]

    async def runner(cfg, *, on_ready=None, resume_harness_session_id="", **kw):
        on_ready(resume_harness_session_id)
        try:
            while not gen.cancelled:                # serve_tty_until's poll
                await asyncio.sleep(0.02)
        finally:
            marks["teardown_start"] = time.monotonic() - t0[0]
            await asyncio.to_thread(_export)

    _stub_terminal_leg(monkeypatch, runner)

    async def stopped_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        await asyncio.sleep(0.2)                    # the TTY is up
        t0.append(time.monotonic())
        native_bridge._signal_stop()
        while "teardown_start" not in marks:        # the runner saw it first
            await asyncio.sleep(0.01)
        raise gen.Cancelled("stopped by user")

    real_finish = run_end.finish

    async def finish(sess):
        marks["run_end"] = time.monotonic() - t0[0]
        marks["exported_at_run_end"] = "exported" in marks
        await real_finish(sess)

    monkeypatch.setattr(run_end, "finish", finish)
    monkeypatch.setattr(entry.gen, "serve_chat", stopped_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())

    async def _flow():
        try:
            await entry.run_session(_base_cfg(surface="terminal", backend="codex"))
        except gen.Cancelled:
            pass
        finally:
            release.set()

    try:
        asyncio.run(_flow())
    finally:
        _clear_session_state()
    return marks


def test_a_stopped_terminal_runs_its_teardown_to_the_end_first(
        native_bridge, monkeypatch):
    """A Stop the runner's poll sees first: its teardown (the export against
    the container) runs to the end before the run end — and so before
    session.run's close_all removes the container. The run end's cancel never
    lands inside it."""
    marks = _stop_race(native_bridge, monkeypatch, teardown_s=1.0)
    assert marks["exported_at_run_end"], marks
    assert marks["exported"] <= marks["run_end"]


def test_a_stopped_terminals_teardown_is_still_cut_at_the_bound(
        native_bridge, monkeypatch):
    """A teardown that outlasts the bound does not hold the run end past it:
    the host's stop grace is 15 s."""
    marks = _stop_race(native_bridge, monkeypatch, teardown_s=30.0, bound=0.4)
    assert not marks["exported_at_run_end"], marks
    assert marks["run_end"] < 1.5


def test_a_terminal_stopped_before_its_tty_is_up_is_cancelled_at_once(
        native_bridge, monkeypatch):
    """Before the TTY is up the runner is in its setup, which does not watch
    the Stop: the run end cancels it at once rather than waiting out the
    bound."""
    import time

    import gen

    async def runner(cfg, **kw):
        await asyncio.sleep(30)                     # an image still building

    _stub_terminal_leg(monkeypatch, runner)

    async def stopped_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        await asyncio.sleep(0.2)
        native_bridge._signal_stop()
        raise gen.Cancelled("stopped by user")

    monkeypatch.setattr(entry.gen, "serve_chat", stopped_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())

    async def _flow():
        t0 = time.monotonic()
        with pytest.raises(gen.Cancelled):
            await entry.run_session(_base_cfg(surface="terminal", backend="codex"))
        return time.monotonic() - t0

    try:
        assert asyncio.run(_flow()) < 2.0
    finally:
        _clear_session_state()


def test_a_terminal_whose_program_exits_ends_the_run(native_bridge, monkeypatch):
    """The TTY's program quit on its own: the run finishes with it."""
    from collimate import native as n

    async def exits(cfg, **kw):
        return

    _stub_terminal_leg(monkeypatch, exits)
    st = _terminal_session("codex")
    try:
        asyncio.run(terminal.run_terminal(st))
        assert st.finish_requested and n._finish_requested[0]
        assert list(n._turn_queue) == []
    finally:
        _clear_session_state()


def test_a_terminal_that_cannot_start_ends_the_run_failed(native_bridge, monkeypatch):
    """A failed TTY ends the run — no chat to fall back to: error.md says
    why and the run reports the failure."""
    import gen

    async def broken(cfg, **kw):
        raise RuntimeError("image build failed")

    _stub_terminal_leg(monkeypatch, broken)

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if entry.gen.has_file("error.md"):
                break

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())
    try:
        with pytest.raises(RuntimeError, match="the codex terminal failed"):
            asyncio.run(entry.run_session(
                _base_cfg(surface="terminal", backend="codex")))
        assert "image build failed" in gen.read_file("error.md")
        # Flushed to the cell: a failed run gets no auto-flush.
        flushed = [c for c in native_bridge.updates if "error.md" in c["files"]]
        assert flushed, native_bridge.updates
    finally:
        _clear_session_state()


def test_a_terminal_run_mints_the_session_the_tty_runs_on(native_bridge, monkeypatch):
    """Through ``session.run``, as the entrypoint calls it on the terminal
    surface: a
    tier-A/B terminal run mints the ACP session first and hands its id to
    the TTY (the live tail follows it); a mint failure
    degrades to a fresh TTY; tier C never mints. The chat session serves no
    files."""
    import _agent_shared.acp_run as acp_run

    native_bridge._rec_accounts["model"] = {
        "service": "openai", "protocol": "openai", "model": "gpt-4o",
        "auth_type": "api_key", "secrets": {"api_key": "test-key"}}
    calls: dict = {}

    async def fake_mint(cfg):
        calls["minted"] = cfg["backend"]
        return "sid-mint-1"

    async def fake_runner(cfg, *, extra_system="", resume_harness_session_id="", **kw):
        calls["resume"] = resume_harness_session_id

    served: list = []

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        served.append(kw["files"])
        kw["on_ready"]()
        await asyncio.sleep(0.2)

    monkeypatch.setattr(acp_run, "mint_acp_session", fake_mint)
    monkeypatch.setattr(eng, "surface_runner", lambda b, ui: fake_runner)
    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())
    _seed_main_config(native_bridge, {"surface": "terminal", "backend": "opencode"})
    here = _PACKAGE
    try:
        asyncio.run(session.run(here=here))
        assert calls == {"minted": "opencode", "resume": "sid-mint-1"}
        assert served == [False]

        async def boom(cfg):
            raise RuntimeError("adapter unreachable")

        calls.clear()
        monkeypatch.setattr(acp_run, "mint_acp_session", boom)
        asyncio.run(session.run(here=here))
        assert calls == {"resume": ""}

        calls.clear()
        monkeypatch.setattr(acp_run, "mint_acp_session", fake_mint)
        _seed_main_config(native_bridge, {"surface": "terminal", "backend": "qwen-code"})
        asyncio.run(session.run(here=here))
        assert calls == {"resume": ""}
    finally:
        _clear_session_state()


def test_a_terminal_leg_records_the_level_its_session_runs_at(native_bridge, monkeypatch):
    """The TTY runs at the leg's level on the minted session and can write
    the whole store, so the leg records that level for the id before the
    runner starts. A record that cannot be kept stops the TTY."""
    from _agent_shared import terminal_tail

    monkeypatch.setattr(terminal, "with_terminal_skills", lambda cfg, paths: "")
    monkeypatch.setattr(terminal_tail, "has_tail_reader", lambda backend: False)
    seen: list = []

    async def runner(cfg, *, resume_harness_session_id="", **kw):
        seen.append((cfg["commands"], resume_harness_session_id,
                     dict(harness_homes._resume_levels("goose"))))

    async def mint(cfg):
        return "S1"

    import _agent_shared.acp_run as acp_run
    monkeypatch.setattr(acp_run, "mint_acp_session", mint)
    monkeypatch.setattr(eng, "surface_runner", lambda backend, ui: runner)

    def leg(commands):
        return asyncio.run(terminal.terminal_leg(
            _base_cfg(backend="goose", commands=commands), paths=_paths("goose")))

    leg("ask")
    assert seen == [("ask", "S1", {"S1": "ask"})]

    def unkeepable(*_a, **_k):
        raise RuntimeError("the goose resume record could neither be updated nor removed")

    monkeypatch.setattr(harness_homes, "record_resume_level", unkeepable)
    with pytest.raises(RuntimeError, match="neither be updated nor removed"):
        leg("allow")
    assert len(seen) == 1


def test_config_warnings_are_said_on_the_runs_own_surface(native_bridge, monkeypatch):
    """A config the run could not honour as chosen is said where the user
    is looking: a chat run's transcript, an annotation run's context (a
    top-level note of the run) — never a chat message in an annotation
    run."""
    import gen

    warn = "No usable machine CLI found — using the bundled CLI"
    seen: dict = {}
    monkeypatch.setattr(annotation, "start", lambda sess: None)

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        kw["on_ready"]()
        await asyncio.sleep(0.05)       # the note's post lands
        seen["messages"] = [(m.role, m.content) for m in gen.history]

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())
    try:
        asyncio.run(entry.run_session(_base_cfg(surface="chat", warnings=[warn])))
        assert ("system", f"Config: {warn}") in seen["messages"]
        assert native_bridge.annotation_posts == []

        entry.gen.history.switch_thread("main", threads_dir="chat", load=False)
        asyncio.run(entry.run_session(_base_cfg(surface="annotation", warnings=[warn])))
        assert seen["messages"] == []
        assert [(p["text"], p["reply_to"]) for p in native_bridge.annotation_posts] == [
            (f"Config: {warn}", None)]
    finally:
        _clear_session_state()


# ── fail-safe edges ────────────────────────────────────────────────────


def test_a_stop_that_surfaces_as_an_error_writes_no_error_md(native_bridge, monkeypatch):
    """Stop signals the whole process group, so a runner can fail with an
    ordinary exception before any checkpoint: with the run cancelled that is
    the Stop unwinding, not a failed terminal — no error.md, no failure."""
    import gen

    async def killed(cfg, **kw):
        native_bridge._signal_stop()
        raise RuntimeError("docker exec: signal: terminated")

    _stub_terminal_leg(monkeypatch, killed)
    st = _terminal_session("codex")
    try:
        asyncio.run(terminal.run_terminal(st))
        assert gen.cancelled
        assert not gen.has_file("error.md")
        assert st.terminal_error == ""
    finally:
        _clear_session_state()


def test_a_run_never_touches_another_runs_files(native_bridge, monkeypatch):
    """Several runs share a cell: a run starts on its own (empty) folder and
    ends by clearing only that — a sibling's files, and a dead run's folder
    (the host's to sweep), are neither loaded, logged nor deleted."""
    import gen

    native_bridge.add_foreign_run("peer-1234abcd", {
        "chat/conversation.chat": '{"chat_id": "s1"}',
        "chat/main.json": '{"messages": [{"role": "user", "content": "hi"}]}',
        "terminal/codex.terminal": "{}"})
    native_bridge.add_foreign_run("gone-deadbeef", {"chat/main.json": "{}"})
    theirs = [p for paths in native_bridge.foreign_runs.values() for p in paths]
    seen: dict = {}

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        seen["kw"] = kw
        kw["on_ready"]()

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())
    try:
        asyncio.run(entry.run_session(_base_cfg(surface="chat")))
        folder = native_bridge.folder
        assert seen["kw"]["pointer_file"] == f"{folder}/chat/conversation.chat"
        assert seen["kw"]["threads_dir"] == f"{folder}/chat"
        for path in theirs:
            assert gen.has_file(path), path
            assert path not in native_bridge.log_files, path
        assert not any(p.startswith("previous-run/") for p in native_bridge.log_files)
    finally:
        _clear_session_state()


# ── a turn the model service refused ends the run failed ─────────────

_REFUSAL_REPLY = ('{"type":"error","status":400,"error":{"type":"invalid_request_error",'
                  '"message":"The \'gpt-5\' model is not supported when using Codex with a ChatGPT account."}}')


def test_a_refused_turn_ends_the_run_failed_and_sends_no_later_turn(native_bridge, monkeypatch):
    """codex relays the service's 400 as its reply (the ACP client raises
    TurnRefused): the run ends failed with the refusal as its error and in
    its log, and no later turn — an annotation batch, a correction, a queued
    message — is sent to be refused again."""
    from _agent_shared import TurnRefused, service_refusal

    refusal = service_refusal(_REFUSAL_REPLY)
    sent: list = []

    async def fake_run_agent(**kw):
        sent.append(kw["prompt"])
        kw["on_event"]({"type": "text_delta", "text": _REFUSAL_REPLY})
        kw["on_event"]({"type": "text_reset", "reason": refusal})
        raise TurnRefused(refusal)

    finishes: list = []
    logged: list = []
    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(entry.gen.session, "request_finish", lambda: finishes.append(1))
    monkeypatch.setattr(entry, "_secret_log", logged.append)
    monkeypatch.setattr(entry.gen, "live_files", lambda *a, **k: _NullCM())

    class _Sourced(FakeChatTurn):
        def __init__(self, msg, source):
            super().__init__(msg)
            self.source = source

    first = FakeChatTurn("hello")
    later = [_Sourced(consts.Sources.ANNOTATIONS, consts.Sources.ANNOTATIONS),
             _Sourced("fix the write", consts.Sources.CORRECTION),
             FakeChatTurn("still there?")]

    async def fake_serve_chat(on_turn, **kw):
        _boot_history(kw)
        for turn in [first, *later]:
            await on_turn(turn)

    monkeypatch.setattr(entry.gen, "serve_chat", fake_serve_chat)
    with pytest.raises(RuntimeError) as err:
        asyncio.run(entry.run_session(_base_cfg(backend="codex", provider="openai", model="")))

    assert str(err.value) == refusal
    assert len(sent) == 1
    assert first.appended[-1]["content"] == f"_(error: {refusal})_"
    assert _REFUSAL_REPLY not in json.dumps(first.appended)
    assert all(not t.appended for t in later)
    assert finishes == [1]
    assert f"turn failed: {refusal}" in logged


# ── the selected file as the first message, and a host-created document ─


def _select(native_bridge, path: str, content) -> None:
    import gen
    gen.update_file(path, content)
    native_bridge.selected_file = path


def test_chat_submits_the_selected_file_before_the_prompt_GN02(
        native_bridge, monkeypatch):
    _select(native_bridge, "notes/brief.md", "Ship the parser first.")
    native_bridge.prompt = "Plan the week."
    seen = _opening(native_bridge, monkeypatch, _base_cfg(surface="chat", selected_file_first=True))
    assert seen["initial"] is False
    sources = [source for source, _ in seen["queued"]]
    assert sources == [consts.Sources.SELECTED_FILE, consts.Sources.COMPOSER]
    selected = seen["queued"][0][1]
    assert '"notes/brief.md"' in selected and "Ship the parser first." in selected
    assert seen["queued"][1][1] == "Plan the week."


def test_the_selected_file_waits_for_the_setting_GN02(native_bridge, monkeypatch):
    _select(native_bridge, "notes/brief.md", "Ship the parser first.")
    seen = _opening(native_bridge, monkeypatch, _base_cfg(surface="chat"))
    assert seen["queued"] == []


def test_a_selected_text_file_is_capped_GN02(native_bridge):
    from _agent_shared.session import selected as sel
    _select(native_bridge, "big.md", "q" * (sel.TEXT_CAP_BYTES + 10))
    chosen = sel.read_selected({"selected_file_first": True})
    assert chosen.message.count("q") == sel.TEXT_CAP_BYTES
    assert f"the first {sel.TEXT_CAP_BYTES} of {sel.TEXT_CAP_BYTES + 10} bytes" in chosen.message
    assert chosen.media == []


def test_a_selected_image_rides_the_chat_turn_as_media_GN02(native_bridge, monkeypatch):
    """A media file cannot ride in the text: its bytes are the input of the
    turn that carries it (once), and the bubble shows it as an attachment."""
    _select(native_bridge, "shots/screen.png", _PNG)
    seen: list = []

    async def fake_run_agent(**kw):
        seen.append(kw["attachments"])
        return _Result(text="ok")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    turn = FakeChatTurn("The user started this run from the selected file")
    turn.source = consts.Sources.SELECTED_FILE
    _run_on_turn(_base_cfg(surface="chat", selected_file_first=True), turn, monkeypatch)
    ((media,),) = seen
    assert (media["path"], media["media_type"], media["data"]) == ("shots/screen.png", "image/png", _PNG)
    assert turn.appended[0]["attachments"] == [
        {"path": "shots/screen.png", "name": "screen.png", "media_type": "image/png"}]


def test_an_interactive_annotation_run_folds_the_selected_file_into_its_first_batch_GN02(
        native_bridge, monkeypatch):
    """An interactive annotation run takes the selected file into its first
    batch turn — the text in the batch's header, a media file as that turn's
    input — never a turn of its own, and posts nothing about it."""
    _select(native_bridge, "shots/screen.png", _PNG)
    native_bridge.add_annotation("what is on this screen?")
    seen: list = []

    async def fake_run_agent(**kw):
        seen.append((kw["prompt"], kw["attachments"]))
        return _Result(text="")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    monkeypatch.setattr(annotation, "start", lambda sess: None)
    turn = FakeChatTurn(consts.Sources.ANNOTATIONS)
    turn.source = consts.Sources.ANNOTATIONS
    _run_on_turn(_base_cfg(surface="annotation", selected_file_first=True), turn, monkeypatch)
    ((prompt, attachments),) = seen
    header, rest = prompt.split("<message", 1)
    assert 'selected file "shots/screen.png"' in header and "It is attached" in header
    assert "what is on this screen?" in rest
    assert [a["path"] for a in attachments] == ["shots/screen.png"]
    assert native_bridge.annotation_posts == []


def test_a_oneshot_opening_turn_carries_the_selected_file_before_its_prompt_GN02(
        native_bridge, monkeypatch):
    _select(native_bridge, "notes/brief.md", "Ship the parser first.")
    seen: list = []

    async def fake_run_agent(**kw):
        seen.append(kw["prompt"])
        return _Result(text="done")

    monkeypatch.setattr(entry, "run_agent", fake_run_agent)
    _drive_oneshot(native_bridge, monkeypatch, "Summarize it.",
                   cfg=_oneshot_cfg(selected_file_first=True))
    (prompt,) = seen
    selected = prompt.index('"notes/brief.md"')
    assert "Ship the parser first." in prompt
    assert selected < prompt.index("Summarize it.")


def test_the_terminal_takes_the_selected_file_before_the_prompt_GN02(native_bridge):
    """claude-code's TTY takes one first message: the selected file, then
    the runner's prompt."""
    from _agent_shared.session.selected import read_selected

    _select(native_bridge, "notes/brief.md", "Ship the parser first.")
    native_bridge.prompt = "Go."
    cfg = _base_cfg(surface="terminal", backend="claude-code", selected_file_first=True)
    message = terminal.opening_message(cfg, read_selected(cfg))
    parts = message.split("\n\n")
    assert parts[0].startswith('The user started this run from the selected file "notes/brief.md"')
    assert "Ship the parser first." in message and parts[-1] == "Go."
    image = {"selected_file_first": True}
    _select(native_bridge, "shots/screen.png", _PNG)
    assert 'selected file "shots/screen.png"; read it.' in terminal.opening_message(
        cfg, read_selected(image))
