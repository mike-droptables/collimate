# SPDX-License-Identifier: MIT
"""The live terminal tail (``_agent_shared.terminal_tail`` + the leg that runs
it, ``_agent_shared/session/terminal.py``).

A terminal run's conversation happens in the harness's TTY; the tail polls
the harness's record WHILE the TTY runs and appends each settled turn to the
run's terminal transcript (a run log artifact) through the same append-only
merge the runner's teardown export uses, so a run stopped before that export
still has its conversation. The tests here pin the properties that make
that safe, with fake readers (no docker, no harness):

  * turns appear in the transcript while the leg is still running;
  * neither a repeated poll nor the end-of-leg finalize appends anything
    twice — both go through ``transcript_merge.write_merged_transcript``;
  * a half-written assistant message is held back (the settled-prefix rule,
    and the record's trailing reply until something follows it) rather than
    landing truncated and again in full;
  * every reply of a tool-using turn lands, however many the tail recorded
    before the finalize;
  * a harness with no readable record (tier C) gets exactly ONE marker at the
    end of the leg.

The leg serves its TTY at the run's own ``.terminal`` pointer (in its run
folder), handed to every backend's terminal runner as ``pointer=`` — no
backend spells a pointer path of its own.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest


from _agent_shared.session import terminal  # noqa: E402
from _agent_shared.harnesses.claude_code import backend as _claude_backend  # noqa: E402
from _agent_shared.session.consts import SessionPaths  # noqa: E402
from _agent_shared.harnesses import HARNESS
from _agent_shared import agent_config as shared_config  # noqa: E402
import _agent_shared as eng  # noqa: E402
from _agent_shared import harness_homes
from _agent_shared import terminal_tail as tt  # noqa: E402
from _agent_shared.transcript_merge import TERMINAL_TRANSCRIPT as TRANSCRIPT  # noqa: E402
from _agent_shared.transcript_merge import write_merged_transcript  # noqa: E402
from collimate.transcript import Message  # noqa: E402
from collimate.transcript import parse_transcript, serialize_transcript  # noqa: E402

from tests._workspace import GENERATORS


def _u(text):
    return Message(role="user", content=text)


def _a(text):
    return Message(role="assistant", content=text)


def _pairs(messages):
    return [(m.role, m.content) for m in messages]


def _transcript():
    import gen
    if not gen.has_file(TRANSCRIPT):
        return []
    return _pairs(parse_transcript(gen.read_file(TRANSCRIPT)))


class _ScriptedReader:
    """A harness record that grows read by read — the last frame repeats once
    the script runs out (a terminal sitting idle at its prompt)."""

    def __init__(self, script, backend="opencode"):
        self.backend = backend
        self.script = [list(f) for f in script]
        self.reads = 0

    def read(self):
        frame = self.script[min(self.reads, len(self.script) - 1)]
        self.reads += 1
        return list(frame)


# ── the settled-prefix rule (pure) ────────────────────────────────────

def test_settled_prefix_holds_back_the_message_still_being_written():
    prev = [_u("hi"), _a("hel")]
    cur = [_u("hi"), _a("hello there"), _u("next")]
    # Only the part that did not move between the two reads is settled: the
    # growing assistant message (and everything after it) waits.
    assert _pairs(tt.settled_prefix(prev, cur)) == [("user", "hi")]
    # An unchanged record that ends on a user message is settled end to end.
    assert _pairs(tt.settled_prefix(cur, cur)) == _pairs(cur)
    # The first read of a leg settles nothing (there is no previous read).
    assert tt.settled_prefix([], cur) == []
    # A record that shrank (a harness rewriting its own history) settles only
    # the common head.
    assert _pairs(tt.settled_prefix(cur, [_u("hi")])) == [("user", "hi")]


def test_settled_prefix_never_settles_the_records_trailing_reply__F1447():
    """The hold-back: a reply that held still across two reads can still be
    growing (a turn paused on a tool; opencode keeps adding to one message
    per turn), so the record's last message settles only once something
    follows it — a later reply, or the next user message."""
    stalled = [_u("hi"), _a("hel")]
    assert _pairs(tt.settled_prefix(stalled, stalled)) == [("user", "hi")]
    turn = [_u("hi"), _a("first"), _a("second")]
    assert _pairs(tt.settled_prefix(turn, turn)) == [
        ("user", "hi"), ("assistant", "first")]
    # The reply followed by a changed record: only the unchanged part counts.
    assert _pairs(tt.settled_prefix([_u("hi"), _a("first")], turn)) == [
        ("user", "hi"), ("assistant", "first")]
    assert tt.settled_prefix([_a("x")], [_a("x")]) == []


def test_messages_from_trace_matches_the_finalize_shape():
    """The tail must normalize a trace exactly as the runners' teardown does
    — two spellings of one turn would defeat the merge's de-duplication."""
    trace = [{"type": "user", "text": "q"}, {"type": "thinking", "text": "hmm"},
             {"type": "tool_use", "tool": "bash"}, {"type": "text", "text": "a"},
             {"type": "text", "text": ""}]
    assert _pairs(tt.messages_from_trace(trace)) == [("user", "q"), ("assistant", "a")]


def test_opencode_export_parsing_is_read_only():
    doc = tt.parse_opencode_export(
        'banner noise\n{"messages": [{"info": {"role": "user"}, '
        '"parts": [{"type": "text", "text": " hi "}]}, '
        '{"info": {"role": "assistant"}, "parts": ['
        '{"type": "reasoning", "text": "hmm"}, {"type": "text", "text": "yo"}]}]}')
    assert _pairs(tt.messages_from_opencode_export(doc)) == [
        ("user", "hi"), ("assistant", "yo")]
    assert tt.parse_opencode_export("not json") == {}


def test_reader_roster_matches_the_terminal_session_tiers():
    """Tier A/B harnesses have a record the tail can read; tier C does not
    (the leg marks itself opaque instead). The pointer map covers every
    backend that ships a terminal runner."""
    for backend in ("claude-code", "codex", "opencode", "goose", "antigravity"):
        assert tt.has_tail_reader(backend)
    # The tier-C harnesses, pi and cline included: both got a TTY with the
    # plan logins (their sign-ins are a TUI slash command / an interactive
    # `cline auth`), but neither exposes a session record in a format the tail
    # can merge, so their legs stay opaque and take the end-of-leg marker.
    for backend in ("qwen-code", "pi", "cline"):
        assert not tt.has_tail_reader(backend)
        assert tt.make_tail_reader(backend) is None
    # Every tier-C runner is one the roster really ships a terminal for.
    for backend in ("qwen-code", "pi", "cline"):
        assert HARNESS[backend].terminal, backend
        assert shared_config._BACKENDS[backend].terminal_session_tier == "C", backend


def test_every_terminal_runner_serves_the_pointer_it_is_handed():
    """Several runs can hold terminals in one cell, so the pointer is the
    RUN's (``<run folder>/terminal/<backend>.terminal``), handed to each
    backend's runner: every runner takes a required ``pointer`` and passes it
    to ``gen.serve_terminal`` — no backend module spells a pointer path."""
    import inspect
    import re

    runners = 0
    for backend, spec in shared_config._BACKENDS.items():
        if not HARNESS[backend].terminal:
            continue
        runner = eng.surface_runner(backend, "terminal")
        param = inspect.signature(runner).parameters.get("pointer")
        assert param is not None, backend
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, backend
        assert param.default is inspect.Parameter.empty, backend
        runners += 1
    assert runners >= 8
    shared = os.path.join(GENERATORS, "_agent_shared")
    for name in sorted(os.listdir(shared)):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(shared, name), encoding="utf-8").read()
        assert not re.findall(r'pointer="[^"]*"', src), name


# ── the tail against the durable transcript ───────────────────────────

def test_tail_appends_settled_turns_and_never_twice(native_bridge):
    """One poll at a time: the settled prefix lands, a repeat adds nothing,
    and the truncated form of a streaming message never reaches the file."""
    tail = tt.TranscriptTail(_ScriptedReader([]), backend="opencode")
    assert tail.merge([_u("hi"), _a("hel")]) == 0            # first read: nothing settled
    assert tail.merge([_u("hi"), _a("hello there")]) == 1    # the user turn settles
    assert _transcript() == [("user", "hi")]
    # The reply held still, but it is the record's last message: it waits…
    assert tail.merge([_u("hi"), _a("hello there")]) == 0
    # …until the next message follows it; that one settles on the next read.
    assert tail.merge([_u("hi"), _a("hello there"), _u("next")]) == 1
    assert tail.merge([_u("hi"), _a("hello there"), _u("next")]) == 1
    assert tail.merge([_u("hi"), _a("hello there"), _u("next")]) == 0   # …and stays put
    assert _transcript() == [("user", "hi"), ("assistant", "hello there"),
                             ("user", "next")]
    assert "hel" not in [c for _r, c in _transcript()]
    assert tail.added == 3


def test_tail_and_finalize_keep_every_reply_of_a_tool_using_turn__F1447_F1569(
        native_bridge):
    """A tool-using turn is one assistant message per text block, with the
    turn pausing on each tool for longer than a poll. The tail records the
    replies that something has followed, and the finalize over the whole
    record adds the rest: three replies in, three replies out."""
    tail = tt.TranscriptTail(_ScriptedReader([]), backend="claude-code")
    reads = [
        [_u("q")],
        [_u("q"), _a("one")],
        [_u("q"), _a("one")],                      # a tool runs for >1 poll
        [_u("q"), _a("one"), _a("two")],
        [_u("q"), _a("one"), _a("two")],           # another tool
    ]
    for read in reads:
        tail.merge(read)
    assert _transcript() == [("user", "q"), ("assistant", "one")]
    full = [_u("q"), _a("one"), _a("two"), _a("three")]
    assert write_merged_transcript(full, path=TRANSCRIPT) == 2
    assert _transcript() == _pairs(full)
    assert write_merged_transcript(full, path=TRANSCRIPT) == 0


def test_a_growing_opencode_reply_never_lands_truncated__F1447(native_bridge):
    """opencode keeps ONE assistant message per turn and grows it after every
    tool call: a copy that held still while a tool ran must not settle, or
    the finalize's full copy would be skipped as already on record."""
    tail = tt.TranscriptTail(_ScriptedReader([]), backend="opencode")
    for read in ([_u("q")], [_u("q"), _a("Looking")], [_u("q"), _a("Looking")],
                 [_u("q"), _a("Looking")]):
        tail.merge(read)
    assert _transcript() == [("user", "q")]
    full = [_u("q"), _a("Looking… found it: 42")]
    assert write_merged_transcript(full, path=TRANSCRIPT) == 1
    assert _transcript() == _pairs(full)


def test_tail_retries_a_batch_whose_write_failed(native_bridge, monkeypatch):
    """A write that raises is not recorded as merged: the next poll over the
    same settled prefix writes it."""
    import _agent_shared.transcript_merge as tm
    real = tm.write_merged_transcript
    calls = {"n": 0}

    def _flaky(exported, *, path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real(exported, path=path)

    monkeypatch.setattr(tm, "write_merged_transcript", _flaky)
    tail = tt.TranscriptTail(_ScriptedReader([]), backend="opencode")
    assert tail.merge([_u("hi")]) == 0                       # first read: nothing settled
    with pytest.raises(OSError):
        tail.merge([_u("hi")])
    assert tail.merge([_u("hi")]) == 1
    assert _transcript() == [("user", "hi")]


def test_tail_task_runs_live_and_the_finalize_adds_only_the_tail(native_bridge):
    """The whole leg shape: turns already on file, a terminal conversation
    growing under the tail, and the runner's end-of-leg export over the FULL
    record as the last reconcile — which adds only the reply the tail held
    back (the record's last message), nothing the tail already wrote."""
    import gen

    seeded = [_u("chat q"), _a("chat a")]
    gen.update_file(TRANSCRIPT, serialize_transcript(seeded))

    full = seeded + [_u("tty q"), _a("tty a")]
    reader = _ScriptedReader([
        seeded + [_u("tty q")],
        seeded + [_u("tty q"), _a("tty")],
        full,
        full,
    ])
    stop = asyncio.Event()

    async def leg():
        task = asyncio.ensure_future(
            tt.tail_transcript("opencode", stop_event=stop, interval=0.01,
                               reader=reader))
        # Long enough for the whole script to settle under the tail.
        for _ in range(60):
            await asyncio.sleep(0.01)
            if reader.reads > len(reader.script) + 1:
                break
        # …and the transcript grew WHILE the leg was still running.
        live = _transcript()
        stop.set()
        added = await task
        return live, added

    live, added = asyncio.run(leg())
    assert live == [("user", "chat q"), ("assistant", "chat a"),
                    ("user", "tty q")]
    assert added == 1
    # The runner's teardown export runs last, over the same full record.
    assert write_merged_transcript(full, path=TRANSCRIPT) == 1
    assert _transcript() == _pairs(full)
    # The transcript is a run log artifact, not a cell file.
    assert TRANSCRIPT == "_agent/terminal/transcript.json"
    assert TRANSCRIPT in native_bridge.log_paths


def test_tail_survives_a_reader_that_raises(native_bridge):
    class _Broken:
        backend = "goose"

        def __init__(self):
            self.calls = 0

        def read(self):
            self.calls += 1
            raise RuntimeError("container went away")

    broken = _Broken()
    stop = asyncio.Event()

    async def leg():
        task = asyncio.ensure_future(
            tt.tail_transcript("goose", stop_event=stop, interval=0.01,
                               reader=broken))
        await asyncio.sleep(0.08)
        stop.set()
        return await task

    assert asyncio.run(leg()) == 0
    assert broken.calls >= 2                       # it kept trying…
    logs = [line for line in native_bridge._rec_logs if "tail" in line]
    assert len(logs) == 1                          # …and reported once


def test_opaque_marker_is_appended_once(native_bridge):
    import gen
    gen.update_file(TRANSCRIPT, serialize_transcript([_u("hi"), _a("ok")]))
    assert tt.append_opaque_marker("qwen-code", path=TRANSCRIPT) == 1
    assert _transcript()[-1] == (
        "system", tt.OPAQUE_MARKER.format(backend="qwen-code"))
    # Idempotent through the same merge — a second leg (or a re-run of the
    # end-of-leg step) never doubles it.
    assert tt.append_opaque_marker("qwen-code", path=TRANSCRIPT) == 0
    assert len(_transcript()) == 3


# ── the leg that drives it ────────────────────────────────────────────

def _cfg(backend="opencode", commands="deny"):
    return {"backend": backend, "auth": "api_key", "model": "m",
            "system_prompt": "", "cell_skills": False, "commands": commands}


@pytest.fixture
def _leg_env(native_bridge, monkeypatch):
    """A terminal leg with the ACP plumbing stubbed out: the mint hands back
    ``ses_x`` (the session the TTY runs on), no adapters to close, and a
    fast tail."""
    async def _mint(cfg):
        return "ses_x"

    monkeypatch.setattr("_agent_shared.acp_run.mint_acp_session", _mint)
    monkeypatch.setattr(tt, "TAIL_INTERVAL_S", 0.01)
    monkeypatch.setattr(terminal, "with_terminal_skills",
                        lambda cfg, paths: "")
    # A key entry: an antigravity leg's agy store is this cell's harness home.
    native_bridge._rec_accounts["model"] = _KEY_ENTRY
    yield native_bridge


_KEY_ENTRY = {"id": "key-entry", "kind": "model", "service": "gemini", "protocol": "gemini",
              "auth_type": "api_key", "metadata": {"harness": "antigravity"}, "secrets": {"api_key": "k"}}
_GOOGLE_ENTRY = {"id": "google-entry", "kind": "model", "service": "gemini", "protocol": "cli",
                 "auth_type": "cli", "metadata": {"harness": "antigravity", "plan": "google"}, "secrets": {}}


def _paths(backend="opencode"):
    return SessionPaths.for_run(backend)


def _leg(cfg):
    return terminal.terminal_leg(cfg, paths=_paths(cfg["backend"]))


def _no_reader(backend, **kw):
    raise AssertionError(f"tier-C {backend} must not build a reader")


def _fake_runner(seen, *, script=None, finalize=None, ready=True):
    """A stand-in TTY runner: records the pointer it was handed, signals
    ready, serves for a moment, then runs the teardown export the real
    runners do."""
    async def runner(cfg, *, pointer, extra_system="", resume_harness_session_id="",
                     first_message="", on_ready=None):
        seen.append(pointer)
        if ready and on_ready is not None:
            on_ready(resume_harness_session_id)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if script is not None and script.reads > len(script.script):
                break
        if finalize is not None:
            write_merged_transcript(finalize, path=TRANSCRIPT)
    return runner


def test_leg_tails_live_on_the_runs_own_pointer(_leg_env, monkeypatch):
    """The end-to-end leg: the runner is handed the run's own pointer, the
    tail is armed at ``on_ready``, its turns are on the transcript before
    the runner returns, and the runner's finalize adds nothing on top."""
    import gen

    seeded = [_u("first q"), _a("first a")]
    gen.update_file(TRANSCRIPT, serialize_transcript(seeded), category="log")

    full = seeded + [_u("tty q"), _a("tty a")]
    reader = _ScriptedReader([seeded + [_u("tty q")],
                             seeded + [_u("tty q"), _a("tty")],
                             full, full])
    monkeypatch.setattr(tt, "make_tail_reader",
                        lambda backend, **kw: reader)
    seen: dict = {}

    async def runner(cfg, *, pointer, extra_system="", resume_harness_session_id="",
                     first_message="", on_ready=None):
        seen["resume"] = resume_harness_session_id
        seen["pointer"] = pointer
        on_ready(resume_harness_session_id)
        for _ in range(60):
            await asyncio.sleep(0.01)
            if reader.reads > len(reader.script) + 1:
                break
        seen["live"] = _transcript()
        # The runner's own teardown export — the last reconcile.
        seen["finalized"] = write_merged_transcript(full, path=TRANSCRIPT)

    monkeypatch.setattr(eng, "surface_runner", lambda backend, ui: runner)

    asyncio.run(_leg(_cfg()))

    assert seen["resume"] == "ses_x"
    folder = _leg_env.folder
    assert seen["pointer"] == f"{folder}/terminal/opencode.terminal"
    # Live: the terminal turn was on the transcript while the leg ran, less
    # its reply, the record's last message (held back)…
    assert seen["live"] == [("user", "first q"), ("assistant", "first a"),
                            ("user", "tty q")]
    # …which the finalize adds, and nothing else.
    assert seen["finalized"] == 1
    assert _transcript() == _pairs(full)
    assert not any(p.startswith(folder + "/chat/") for p in gen.list_files())


def test_the_leg_always_runs_the_tty_on_a_freshly_minted_session(_leg_env, monkeypatch):
    """A run never continues a previous run's harness session (its record is
    dropped at boot): a tier-A/B leg mints the session its TTY runs on, even
    when the cell carries a session record; a mint that fails runs a fresh
    TTY, tier C never mints, and neither does antigravity, whose TTY names
    its own conversation."""
    import gen

    gen.update_file("_agent/session.json",
                    json.dumps({"backend": "opencode", "acp_session_id": "ses_old"}))
    seen: list = []

    async def runner(cfg, *, pointer, resume_harness_session_id="", **kw):
        seen.append(resume_harness_session_id)

    async def _boom(cfg):
        raise RuntimeError("adapter unreachable")


    monkeypatch.setattr(tt, "make_tail_reader", _no_reader)
    monkeypatch.setattr(eng, "surface_runner", lambda backend, ui: runner)
    try:
        asyncio.run(_leg(_cfg()))
        asyncio.run(_leg(_cfg("qwen-code")))
        asyncio.run(_leg(_cfg("antigravity")))
        monkeypatch.setattr("_agent_shared.acp_run.mint_acp_session", _boom)
        asyncio.run(_leg(_cfg()))
    finally:
        harness_homes.close_harness_homes()    # the antigravity leg listed this run's home
    assert seen == ["ses_x", "", "", ""]


def test_the_leg_hands_the_antigravity_tail_its_stores_ids_at_the_start(
        _leg_env, monkeypatch):
    """agy's TTY names its own conversation, so the tail has no id to follow:
    it gets the store's conversation ids as the leg started — listed before
    the TTY came up — and follows only one that appears after them, so an
    earlier conversation this cell's home keeps is never mirrored as the
    TTY's."""
    from _agent_shared.harnesses.antigravity import backend as ab

    asked: dict = {}

    def reader(backend, **kw):
        asked.update(kw, backend=backend)
        return None

    async def runner(cfg, *, pointer, on_ready=None, **kw):
        # A conversation the TTY writes is not in the leg's listing.
        _agy_db(ab.conversation_db_path(harness_homes.harness_home("antigravity"),
                                        "conv-tty"), [(14, "q")])
        on_ready("")
        await asyncio.sleep(0.05)

    monkeypatch.setattr(tt, "make_tail_reader", reader)
    monkeypatch.setattr(eng, "surface_runner", lambda backend, ui: runner)
    harness_homes.close_harness_homes()
    try:
        _agy_db(ab.conversation_db_path(harness_homes.harness_home("antigravity"),
                                        "conv-old"), [(14, "last run")])
        asyncio.run(_leg(_cfg("antigravity")))
    finally:
        harness_homes.close_harness_homes()    # the leg's listing opened this run's home
    assert asked["backend"] == "antigravity" and asked["harness_session_id"] == ""
    assert asked["before"] == {"conv-old"}


def test_a_fresh_ttys_tail_follows_its_runners_store_snapshot__F1528(
        _leg_env, monkeypatch):
    """F1528: when the mint fails, the opencode TTY starts fresh; its runner
    lists the store before the TTY comes up and hands the ids to
    ``on_ready`` — the tail gets exactly those, so it follows only the one
    session that appears after them. A runner that could not list hands
    none, and the tail has nothing to follow (never the store's newest)."""
    asked: list = []

    def reader(backend, **kw):
        asked.append(dict(kw, backend=backend))
        return None

    def runner_with(snapshot):
        async def runner(cfg, *, pointer, on_ready=None, resume_harness_session_id="", **kw):
            if snapshot is None:
                on_ready(resume_harness_session_id)
            else:
                on_ready("", before=snapshot)
            await asyncio.sleep(0.05)
        return runner

    async def _boom(cfg):
        raise RuntimeError("mint failed")

    monkeypatch.setattr(tt, "make_tail_reader", reader)
    monkeypatch.setattr("_agent_shared.acp_run.mint_acp_session", _boom)
    monkeypatch.setattr(eng, "surface_runner",
                        lambda backend, ui: runner_with(frozenset({"ses_OLD"})))
    asyncio.run(_leg(_cfg()))
    monkeypatch.setattr(eng, "surface_runner", lambda backend, ui: runner_with(None))
    asyncio.run(_leg(_cfg()))
    assert asked == [
        {"backend": "opencode", "harness_session_id": "", "before": frozenset({"ses_OLD"})},
        {"backend": "opencode", "harness_session_id": "", "before": None}]


def test_tier_c_leg_marks_itself_opaque_once(_leg_env, monkeypatch):
    """qwen-code has no readable record: no tail task at all, one system marker
    at the end of the leg (never per poll)."""
    monkeypatch.setattr(tt, "make_tail_reader", _no_reader)
    pointers: list = []
    monkeypatch.setattr(eng, "surface_runner",
                        lambda backend, ui: _fake_runner(pointers))

    asyncio.run(_leg(_cfg("qwen-code")))
    assert _transcript() == [
        ("system", tt.OPAQUE_MARKER.format(backend="qwen-code"))]
    assert pointers == [f"{_leg_env.folder}/terminal/qwen-code.terminal"]

    # The marker goes through the transcript's idempotent merge: appending it
    # again over the same transcript adds nothing.
    tt.append_opaque_marker("qwen-code", path=TRANSCRIPT)
    assert sum(1 for role, _c in _transcript() if role == "system") == 1


def test_leg_whose_tty_never_started_leaves_no_marker(_leg_env, monkeypatch):
    """A runner that dies before the TTY is up had no terminal conversation —
    a "not transcribed here" marker there would be a lie. The failure raises
    out of the leg: the run ends on it."""
    async def runner(cfg, **kw):
        raise RuntimeError("image build failed")

    monkeypatch.setattr(eng, "surface_runner", lambda backend, ui: runner)
    with pytest.raises(RuntimeError, match="image build failed"):
        asyncio.run(_leg(_cfg("qwen-code")))
    assert _transcript() == []


# ── the per-harness readers (synthetic records, no docker) ────────────

class _FakeEnv:
    """The ``env`` shape the codex/goose export helpers take: ``exec`` →
    (rc, output), ``read_file`` → the whole file. The find/list probe and
    the record read are answered from one canned pair."""

    def __init__(self, record, listing=""):
        self.record = record
        self.listing = listing

    def exec(self, argv, **kw):
        cmd = " ".join(argv)
        if "find" in cmd:
            return 0, "/home/coder/.codex-collimate/sessions/2026/x.jsonl\n"
        if "session list" in cmd:
            return 0, self.listing
        return 0, ""

    def read_file(self, path, **kw):
        return self.record.encode("utf-8")


def test_codex_reader_normalizes_a_rollout(tmp_path):
    rollout = "\n".join(
        [
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "q1"}],
                    }
                }
            ),
            json.dumps({"payload": {"type": "reasoning", "summary": [{"text": "think"}]}}),
            json.dumps(
                {
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "a1"}],
                    }
                }
            ),
        ]
    )
    from _agent_shared.harnesses.codex.backend import _START_MARKER

    marker = tmp_path / _START_MARKER
    marker.write_text("")
    os.utime(marker, ns=(1, 1))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions/rollout.jsonl").write_text(rollout + "\n")
    assert _pairs(tt._CodexTail(str(tmp_path)).read()) == [("user", "q1"), ("assistant", "a1")]
    assert tt._CodexTail(str(tmp_path / "missing")).read() == []



def test_goose_reader_normalizes_an_export():
    doc = json.dumps({"messages": [
        {"role": "user", "content": [{"type": "text", "text": "gq"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ga"}]}]})
    assert _pairs(tt._GooseTail(_FakeEnv(doc), "sess-1").read()) == [
        ("user", "gq"), ("assistant", "ga")]


def test_opencode_reader_execs_export_and_never_writes(native_bridge):
    class _Container:
        def __init__(self):
            self.cmds = []

        def exec_run(self, argv, user=None):
            self.cmds.append(" ".join(argv))

            class _Res:
                exit_code = 0
                output = json.dumps({"messages": [
                    {"info": {"role": "user"},
                     "parts": [{"type": "text", "text": "oq"}]},
                    {"info": {"role": "assistant"},
                     "parts": [{"type": "text", "text": "oa"}]}]}).encode()
            return _Res()

        def exec(self, argv, **kw):
            if argv[0] == 'mktemp':
                return 0, '/tmp/opencode-fixture-output'
            result = self.exec_run(argv, **kw)
            self.captured_output = result.output
            return result.exit_code, result.output.decode()

        def read_file(self, path, **kw):
            return self.captured_output

    import gen
    container = _Container()
    assert _pairs(tt._OpencodeTail(container, "ses_1").read()) == [
        ("user", "oq"), ("assistant", "oa")]
    assert any("opencode export ses_1" in c for c in container.cmds)
    # Read-only: the reader touches no cell file (``_export_chatlog`` would
    # have written the transcript itself).
    assert not gen.has_file(TRANSCRIPT)


class _OpencodeStore:
    """An opencode container over a session store (``{id: export doc}``):
    ``session list`` names every id, ``export`` answers one document."""

    def __init__(self, sessions):
        self.sessions = dict(sessions)
        self.exported: list = []

    def exec_run(self, argv, user=None):
        cmd = argv[-1]

        class _Res:
            exit_code = 0
            output = b""
        if cmd.startswith("opencode session list"):
            _Res.output = "\n".join(f"{sid}  title" for sid in self.sessions).encode()
        elif cmd.startswith("opencode export"):
            sid = cmd.split()[2]
            self.exported.append(sid)
            _Res.output = json.dumps({"messages": [
                {"info": {"role": "assistant"},
                 "parts": [{"type": "text", "text": self.sessions[sid]}]}]}).encode()
        return _Res()

    def exec(self, argv, **kw):
        if argv[0] == 'mktemp':
            return 0, '/tmp/opencode-fixture-output'
        result = self.exec_run(argv, **kw)
        self.captured_output = result.output
        return result.exit_code, result.output.decode()

    def read_file(self, path, **kw):
        return self.captured_output


def test_opencode_reader_never_follows_an_earlier_runs_session__F1528():
    """F1528: without the session the TTY runs on, the tail follows only the
    one session that appeared after the leg's snapshot — never the store's
    newest, which can be an earlier run's conversation. No snapshot: nothing
    to follow."""
    store = _OpencodeStore({"ses_OLD": "earlier run"})
    assert tt._OpencodeTail(store, "", frozenset({"ses_OLD"})).read() == []
    assert tt._OpencodeTail(store, "").read() == []
    assert store.exported == []
    store.sessions["ses_NEW"] = "this run"
    assert _pairs(tt._OpencodeTail(store, "", frozenset({"ses_OLD"})).read()) == [
        ("assistant", "this run")]
    assert store.exported == ["ses_NEW"]


def test_goose_reader_never_follows_an_earlier_runs_session__F1528():
    """F1528, goose: the same rule over ``goose session list --format json``."""
    calls: list = []

    class _Store:
        sessions = {"20260901_1": "earlier run"}

        def exec(self, argv, **kw):
            cmd = argv[-1]
            if "session list" in cmd:
                return 0, json.dumps([{"id": sid} for sid in self.sessions])
            if argv[:2] == ["rm", "-f"]:
                return 0, ""
            sid = cmd.split("--session-id ", 1)[1].split()[0]
            calls.append(sid)
            self.exported = json.dumps({"messages": [
                {"role": "assistant", "content": [{"type": "text", "text": self.sessions[sid]}]}]})
            return 0, ""

        def read_file(self, path, **kw):   # the file the export went to
            return self.exported.encode()

    env = _Store()
    before = frozenset({"20260901_1"})
    assert tt._GooseTail(env, "", before).read() == []
    assert tt._GooseTail(env, "").read() == []
    assert calls == []
    env.sessions = {**env.sessions, "20260928_1": "this run"}
    assert _pairs(tt._GooseTail(env, "", before).read()) == [("assistant", "this run")]
    assert calls == ["20260928_1"]


def test_a_fresh_ttys_tail_lists_the_store_only_until_its_session_appears__F1528():
    """Once the one new session is found, the reader keeps to it: later polls
    export it without listing the store again."""
    class _Counting(_OpencodeStore):
        listed = 0

        def exec_run(self, argv, user=None):
            if argv[-1].startswith("opencode session list"):
                self.listed += 1
            return super().exec_run(argv, user=user)

    store = _Counting({"ses_OLD": "earlier run"})
    reader = tt._OpencodeTail(store, "", frozenset({"ses_OLD"}))
    assert reader.read() == [] and store.listed == 1
    store.sessions["ses_NEW"] = "this run"
    reader.read()
    reader.read()
    assert store.listed == 2 and store.exported == ["ses_NEW", "ses_NEW"]

    class _Goose:
        sessions = {"20260901_1": "earlier run"}
        listed = 0

        def exec(self, argv, **kw):
            cmd = argv[-1]
            if "session list" in cmd:
                self.listed += 1
                return 0, json.dumps([{"id": sid} for sid in self.sessions])
            if argv[:2] == ["rm", "-f"]:
                return 0, ""
            sid = cmd.split("--session-id ", 1)[1].split()[0]
            self.exported = json.dumps({"messages": [
                {"role": "assistant", "content": [{"type": "text", "text": self.sessions[sid]}]}]})
            return 0, ""

        def read_file(self, path, **kw):   # the file the export went to
            return self.exported.encode()

    env = _Goose()
    reader = tt._GooseTail(env, "", frozenset({"20260901_1"}))
    assert reader.read() == [] and env.listed == 1
    env.sessions = {**env.sessions, "20260928_1": "this run"}
    reader.read()
    assert _pairs(reader.read()) == [("assistant", "this run")] and env.listed == 2


def test_make_tail_reader_hands_a_fresh_ttys_snapshot_to_its_reader__F1528(monkeypatch):
    """F1528: the snapshot a fresh opencode or goose TTY's runner took reaches
    the reader ``make_tail_reader`` builds, so the live tail follows the one
    new session; without one there is none to follow."""
    from types import SimpleNamespace

    from _agent_shared import cli_container as cc

    monkeypatch.setattr(cc, "cached_env", lambda name: SimpleNamespace(container=object()))
    before = frozenset({"ses_OLD"})
    for backend in ("opencode", "goose"):
        assert tt.make_tail_reader(backend, harness_session_id="", before=before).before == before
        assert tt.make_tail_reader(backend, harness_session_id="").before is None


def test_claude_reader_reads_the_tier_a_host_store(tmp_path):
    """The Tier-A arrangement puts the session jsonl on the HOST — the tail is
    a plain file read at the project-slug path, re-parsed only when the file
    moves."""
    project = "/home/u/ws"
    store = tmp_path / "store"
    d = store / "projects" / "-home-u-ws"
    d.mkdir(parents=True)
    jsonl = d / "sid.jsonl"
    jsonl.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user",
                                                "content": [{"type": "text", "text": "cq"}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                     "content": [{"type": "text", "text": "ca"}]}}),
    ]) + "\n")

    reader = tt._ClaudeTail("sid", str(store), project)
    assert reader.path() == str(jsonl)
    assert _pairs(reader.read()) == [("user", "cq"), ("assistant", "ca")]
    # A second read of an unmoved file returns the cached parse.
    assert reader.read() is reader.read()
    # A missing session (never written yet) is "nothing new", not an error.
    assert tt._ClaudeTail("other", str(store), project).read() == []


def test_claude_reader_watches_the_cwd_slug_and_still_finds_a_pinned_dir(tmp_path):
    """The tail takes its candidates from the exporters' own list
    (``claude_code_backend.claude_session_paths``): read a different path than
    the export and the leg looks dark for its whole run while the transcript
    accumulates one directory over. Today both legs file under the cwd slug,
    so that is the path to WATCH before anything exists; a session a newer CLI
    filed under the reserved pinned name is still found once it is there."""
    from _agent_shared.harnesses.claude_code.backend import _claude_project_slug

    project = "/tmp/collimate-run-abc/ws"
    store = tmp_path / "store"
    slug_dir = store / "projects" / _claude_project_slug(project)
    pinned = store / "projects" / _claude_backend.CLAUDE_PROJECT_DIR_NAME
    slug_dir.mkdir(parents=True)
    pinned.mkdir(parents=True)

    # Nothing written yet: the path to WATCH is the slug candidate, and a read
    # of it is "nothing new".
    reader = tt._ClaudeTail("sid", str(store), project)
    assert reader.path() == str(slug_dir / "sid.jsonl")
    assert reader.read() == []

    # A transcript under the reserved pinned name is the one that exists, so
    # it wins — the candidate list, not a fixed path, decides.
    (pinned / "sid.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": "pinned"}}) + "\n")
    assert reader.path() == str(pinned / "sid.jsonl")
    assert [m.content for m in reader.read()] == ["pinned"]

# ── the antigravity reader (a real store, hand-encoded protobuf) ──────
#
# agy stores each conversation as a SQLite database whose step payloads are
# protobuf blobs with no published .proto. The fixture below writes those bytes
# BY HAND (wire format: key = field << 3 | wire type) so the reader is exercised
# against the real layout, not against its own encoder.


def _pb_varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb_v(num, value):
    return _pb_varint(num << 3) + _pb_varint(value)


def _pb_m(num, blob):
    return _pb_varint((num << 3) | 2) + _pb_varint(len(blob)) + blob


def _pb_s(num, text):
    return _pb_m(num, text.encode("utf-8"))


def _agy_db(path, steps):
    """A conversation database: ``steps`` is a list of ``(type, text)`` where
    type 14 is a user prompt and 15 a segment of agent text."""
    import sqlite3
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, "
                "status INTEGER, step_payload BLOB, step_format INTEGER)")
    for i, (step_type, text) in enumerate(steps):
        body = _pb_m(19, _pb_s(2, text)) if step_type == 14 \
            else _pb_m(20, _pb_s(1, text))
        con.execute("INSERT INTO steps (idx, step_type, step_payload) "
                    "VALUES (?, ?, ?)", (i, step_type, _pb_v(1, step_type) + body))
    con.commit()
    con.close()
    return path


def test_antigravity_reader_reads_the_per_cell_conversation_store(tmp_path):
    """No exec and no container: agy's store lives in this cell's harness home,
    which IS the container's ~/.gemini (the host side of the bind), so the tail
    is a plain SQLite read — and re-parses only when the file moved, because
    decoding the protobuf payloads is the one reader here that costs CPU."""
    store = tmp_path / "home"
    db = _agy_db(str(store / "antigravity-cli" / "conversations" / "conv-1.db"),
                 [(14, "aq"), (15, "a"), (15, "a1")])

    reader = tt._AntigravityTail(str(store), frozenset())
    assert reader.path() == db
    # Consecutive agent-text rows are segments of ONE assistant message.
    assert _pairs(reader.read()) == [("user", "aq"), ("assistant", "aa1")]
    # A second read of an unmoved store returns the cached parse.
    assert reader.read() is reader.read()


def test_antigravity_reader_follows_the_one_new_conversation(tmp_path):
    """The leg has no id to hand over (agy's TTY names its own conversation),
    so the reader follows the one conversation that appeared after the ids
    the store held when the leg started — the conversation the leg adopts.
    An earlier one is never read, however recent its file times; while none
    or several have appeared there is nothing to read, never an error."""
    import time

    store = tmp_path / "home"
    d = store / "antigravity-cli" / "conversations"
    old = _agy_db(str(d / "old.db"), [(14, "last run")])
    os.utime(old, (time.time() + 86400, time.time() + 86400))
    before = frozenset({"old"})

    assert tt._AntigravityTail(str(store), before).read() == []
    _agy_db(str(d / "new.db"), [(14, "this run")])
    assert _pairs(tt._AntigravityTail(str(store), before).read()) == \
        [("user", "this run")]
    _agy_db(str(d / "sub.db"), [(14, "a subagent")])
    assert tt._AntigravityTail(str(store), before).read() == []
    assert tt._AntigravityTail(str(tmp_path / "empty"), frozenset()).read() == []


def test_antigravity_reader_uses_the_terminals_own_agy_home(
        monkeypatch, tmp_path, native_bridge):
    """On a Gemini key the store is the per-cell harness home the terminal
    runner mounts as the container's ~/.gemini; on the Google plan it is the
    entry's per-account home's ~/.gemini, where this machine's agy keeps it;
    the user's own ~/.gemini is never read."""
    from _agent_shared.cli_host import account_home

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(native_bridge, "cell_id", "cell-tail")
    native_bridge._rec_accounts["model"] = _GOOGLE_ENTRY
    reader = tt.make_tail_reader("antigravity", harness_session_id="")
    assert reader.store_dir == os.path.join(account_home("antigravity", _GOOGLE_ENTRY), ".gemini")
    native_bridge._rec_accounts["model"] = _KEY_ENTRY
    home = harness_homes.harness_home("antigravity")
    reader = tt.make_tail_reader("antigravity", harness_session_id="conv-1")
    assert isinstance(reader, tt._AntigravityTail)
    assert reader.store_dir == home
    assert os.path.expanduser("~/.gemini") not in reader.path()
    # Unlike the container-exec readers, this one needs no cached env: it works
    # for a leg whose container the run never registered.
    assert tt.make_tail_reader("antigravity", harness_session_id="") is not None
    # The store's ids as the leg started: the reader follows only a newer one.
    assert tt.make_tail_reader("antigravity",
                               before=frozenset({"conv-0"})).before == {"conv-0"}


def test_claude_reader_uses_this_cells_harness_home(
        monkeypatch, tmp_path, native_bridge):
    """The store is the per-cell harness home mounted as CLAUDE_CONFIG_DIR,
    under every auth; the user's real ~/.claude is never read."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(native_bridge, "cell_id", "cell-tail")
    home = harness_homes.harness_home("claude-code")
    reader = tt.make_tail_reader("claude-code", harness_session_id="sid")
    assert isinstance(reader, tt._ClaudeTail)
    assert reader.store_dir == home
    assert os.path.expanduser("~/.claude") not in reader.path()
    # No resumed id: the runner minted its own and the tail has no file to
    # watch — the end-of-leg export still backfills.
    assert tt.make_tail_reader("claude-code", harness_session_id="") is None


def test_harness_tail_owns_conversation_identity__F0287(
        native_bridge, monkeypatch, tmp_path):
    import inspect

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(native_bridge, "cell_id", "cell-harness-proof")
    reader = tt.make_tail_reader("claude-code", harness_session_id="cli-conversation")
    assert reader.harness_session_id == "cli-conversation"
    assert "harness_session_id" in inspect.signature(tt.tail_transcript).parameters
    assert "harness_session_id" in inspect.signature(harness_homes.record_resume_level).parameters
    assert eng.backend_spec("antigravity").harness_session_ids.endswith(":harness_session_ids")
