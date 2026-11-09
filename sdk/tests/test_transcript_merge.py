# SPDX-License-Identifier: MIT
"""The terminal-run transcript merge (``_agent_shared.transcript_merge``).

A terminal run's transcript (a run log, ``_agent/terminal/transcript.json``)
has two writers: the live tail, while the TTY runs, and the leg's teardown
exporter, as it closes. Both merge a read of the harness's own session
record, which stores the prompt text as the engine folded it, so the merge
must be APPEND-ONLY: the entries already on record (the tail's, or an
earlier export's) survive verbatim, the engine preamble is stripped from the
export's user text, and only the messages not yet on record are added.
"""
from __future__ import annotations



from _agent_shared import clock_line  # noqa: E402
from _agent_shared.transcript_merge import (  # noqa: E402
    FOLD_CLOSE,
    FOLD_OPEN,
    clean_exported,
    is_harness_noise,
    merge_exported_transcript,
    strip_prompt_fold,
    write_merged_transcript,
)
from collimate.transcript import Message  # noqa: E402


def _u(text, **kw):
    return Message(role="user", content=text, **kw)


def _a(text, **kw):
    return Message(role="assistant", content=text, **kw)


def test_strip_prompt_fold_removes_the_engine_preamble_only():
    folded = (f"{FOLD_OPEN}\n\nBe terse.\n\n{clock_line()}\n\n{FOLD_CLOSE}\n\n"
              "Read notes.md")
    assert strip_prompt_fold(folded) == "Read notes.md"
    # A stale-clock refresh: one line ahead of the prompt.
    assert strip_prompt_fold(f"{clock_line()}\n\nhello") == "hello"
    # The staged-attachment block.
    assert strip_prompt_fold(
        "The user attached these files (paths are relative to the workspace "
        "root):\n- attachments/a.png (image/png)\n\nlook") == "look"
    # A user's own '---' or timestamps inside the message are untouched.
    own = "first\n\n---\n\nThe current date and time is noon."
    assert strip_prompt_fold(own) == own


def test_harness_noise_is_dropped():
    assert is_harness_noise("<local-command-caveat>Caveat: …</local-command-caveat>")
    assert is_harness_noise("<command-name>/model</command-name>")
    assert is_harness_noise("<local-command-stdout>Set model</local-command-stdout>")
    assert not is_harness_noise("what is <b>bold</b>?")
    cleaned = clean_exported([_u("<command-name>/model</command-name>"),
                              _u(f"{clock_line()}\n\nhi"), _a("hello")])
    assert [(m.role, m.content) for m in cleaned] == [("user", "hi"),
                                                       ("assistant", "hello")]


def test_merge_keeps_the_records_own_entries_and_appends_the_new_turns():
    """What the tail or an earlier export already wrote stays exactly as it
    is (every field of it), the export's copies of those turns are skipped,
    and only the turns after them are added."""
    existing = [
        _u("Remember PLUM-42. Reply STORED."),
        _a("STORED", thinking="the user wants…", tools=[{"name": "Read", "status": "done"}]),
        _u("What is 2+2? One word."),
        _a("Four"),
    ]
    exported = [
        _u(f"{FOLD_OPEN}\n\nsys\n\n{FOLD_CLOSE}\n\nRemember PLUM-42. Reply STORED."),
        _a("STORED"),
        _u("What is 2+2? One word."),
        _a("Four."),                       # the harness's own copy of the reply
        _u("What is the codeword? One word."),
        _a("PLUM-42"),
    ]
    merged = merge_exported_transcript(existing, exported)
    assert merged[:4] == existing            # verbatim — every field intact
    assert [(m.role, m.content) for m in merged[4:]] == [
        ("user", "What is the codeword? One word."), ("assistant", "PLUM-42")]


def test_merge_keeps_the_exports_reply_when_the_transcript_ended_mid_turn():
    existing = [_u("hi"), _a("he", stopped=True), _u("go on")]
    exported = [_u("hi"), _a("hello"), _u("go on"), _a("…on and on")]
    merged = merge_exported_transcript(existing, exported)
    assert merged[:3] == existing
    assert [(m.role, m.content) for m in merged[3:]] == [("assistant", "…on and on")]


def test_merge_keeps_every_reply_of_a_tool_using_turn__F1447_F1569():
    """A tool-using turn records one assistant message per text block. Once
    the live tail has recorded the turn's first reply, the export's later
    replies of that turn are still the leg's: the merge skips only as many
    of the export's replies as the transcript already holds."""
    turn = [_u("q"), _a("first"), _a("second"), _a("third")]
    merged = merge_exported_transcript([_u("q"), _a("first")], turn)
    assert [(m.role, m.content) for m in merged] == [
        ("user", "q"), ("assistant", "first"), ("assistant", "second"),
        ("assistant", "third")]
    # Two of three on record: only the third is added.
    merged = merge_exported_transcript(turn[:3], turn)
    assert [m.content for m in merged] == ["q", "first", "second", "third"]
    # The turns after it land too, and a full transcript takes nothing more.
    later = turn + [_u("next"), _a("n1"), _a("n2")]
    merged = merge_exported_transcript([_u("q"), _a("first")], later)
    assert [m.content for m in merged] == [
        "q", "first", "second", "third", "next", "n1", "n2"]
    assert merge_exported_transcript(merged, later) == merged


def test_a_terminal_turn_with_three_replies_exports_three__F1447(native_bridge):
    """The exit test's shape: the tail recorded the turn's first reply while
    a tool ran, and the teardown export over the whole record adds the other
    two."""
    from collimate.transcript import parse_transcript

    import gen
    from _agent_shared.transcript_merge import TERMINAL_TRANSCRIPT

    assert write_merged_transcript([_u("q"), _a("one")]) == 2
    assert write_merged_transcript(
        [_u("q"), _a("one"), _a("two"), _a("three")]) == 2
    got = parse_transcript(gen.read_file(TERMINAL_TRANSCRIPT))
    assert [(m.role, m.content) for m in got] == [
        ("user", "q"), ("assistant", "one"), ("assistant", "two"),
        ("assistant", "three")]


def test_the_terminal_transcripts_writers_are_the_tail_and_the_exporters__F1447_F1569():
    """The count-aware merge compares how many replies the transcript holds
    with how many the export carries, which is sound only while every writer
    turns one harness record into the same messages: the live tail (and its
    tier-C marker) and the runners' teardown exporters, whose readers share
    helpers. A new writer must be checked against that before it joins."""
    import re

    from tests._workspace import WORKSPACE

    call = re.compile(r"(?<!def )\bwrite_merged_transcript\(")
    writers = sorted(
        str(p.relative_to(WORKSPACE)) for base in ("content/generators", "sdk/src")
        for p in (WORKSPACE / base).rglob("*.py")
        if call.search(p.read_text(encoding="utf-8")))
    assert writers == [
        "content/generators/_agent_shared/harnesses/antigravity/backend.py",
        "content/generators/_agent_shared/harnesses/claude_code/backend.py",
        "content/generators/_agent_shared/harnesses/codex/backend.py",
        "content/generators/_agent_shared/harnesses/goose/backend.py",
        "content/generators/_agent_shared/harnesses/hermes/backend.py",
        "content/generators/_agent_shared/harnesses/opencode/backend.py",
        "content/generators/_agent_shared/terminal_tail.py",
    ]


def test_merge_with_no_alignment_dedupes_and_appends():
    """An export that does not hold the record's user messages (another
    session's record) is appended after the record, never in place of it."""
    existing = [_u("chat question"), _a("chat answer")]
    exported = [_u("terminal question"), _a("terminal answer"),
                _u("chat question")]   # a coincidental duplicate is dropped
    merged = merge_exported_transcript(existing, exported)
    assert merged[:2] == existing
    assert [(m.role, m.content) for m in merged[2:]] == [
        ("user", "terminal question"), ("assistant", "terminal answer")]


def test_merge_edge_shapes():
    assert merge_exported_transcript([], [_u("a"), _a("b")]) == [_u("a"), _a("b")]
    kept = [_u("a"), _a("b")]
    assert merge_exported_transcript(kept, []) == kept
    # Nothing new in the export → the transcript is returned unchanged.
    assert merge_exported_transcript(kept, [_u("a"), _a("b")]) == kept


def test_write_merged_transcript_appends_to_the_run_log_transcript(native_bridge):
    """A terminal run's transcript is a run LOG artifact at
    ``_agent/terminal/transcript.json`` — never a cell file, never under the
    chat surface's folder — and the merge only ever appends to it."""
    import gen
    from collimate.transcript import parse_transcript, serialize_transcript

    from _agent_shared.transcript_merge import TERMINAL_TRANSCRIPT

    assert TERMINAL_TRANSCRIPT == "_agent/terminal/transcript.json"
    gen.update_file(TERMINAL_TRANSCRIPT, serialize_transcript([
        _u("first"), _a("one", thinking="hmm"),
    ]), category="log")
    added = write_merged_transcript([
        _u(f"{clock_line()}\n\nfirst"), _a("one"), _u("second"), _a("two"),
    ])
    assert added == 2
    assert TERMINAL_TRANSCRIPT in native_bridge.log_paths
    assert TERMINAL_TRANSCRIPT not in gen.list_files()
    assert not any(p.endswith("chat/main.json") for p in gen.list_files())
    got = parse_transcript(gen.read_file(TERMINAL_TRANSCRIPT))
    assert [(m.role, m.content) for m in got] == [
        ("user", "first"), ("assistant", "one"), ("user", "second"), ("assistant", "two")]
    assert got[1].thinking == "hmm"
    # Idempotent: the same export again adds nothing and leaves the file alone.
    assert write_merged_transcript([_u("first"), _a("one"), _u("second"), _a("two")]) == 0


def test_concurrent_writes_never_drop_the_fuller_export(native_bridge, monkeypatch):
    """The live tail and a runner's teardown export can merge at once (the
    teardown in a worker thread): the read -> merge -> write is serialized, so
    a shorter merge landing last cannot overwrite the fuller one."""
    import threading
    import time

    import gen
    import collimate.transcript as ct

    from _agent_shared.transcript_merge import TERMINAL_TRANSCRIPT

    gen.update_file(TERMINAL_TRANSCRIPT, ct.serialize_transcript([
        _u("first"), _a("one"),
    ]), category="log")
    real_parse = ct.parse_transcript

    def _slow_parse(text):
        # The shorter (tail) merge holds its read longest, so without the
        # lock it would write last.
        time.sleep(0.15 if threading.current_thread().name == "tail" else 0.02)
        return real_parse(text)

    monkeypatch.setattr(ct, "parse_transcript", _slow_parse)
    barrier = threading.Barrier(2)

    def _run(exported):
        barrier.wait()
        write_merged_transcript(exported)

    full = [_u("first"), _a("one"), _u("second"), _a("two")]
    threads = [
        threading.Thread(target=_run, args=(full[:3],), name="tail"),
        threading.Thread(target=_run, args=(full,), name="teardown"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = real_parse(gen.read_file(TERMINAL_TRANSCRIPT))
    assert [(m.role, m.content) for m in got] == [(m.role, m.content) for m in full]


def test_goose_turn_context_blocks_are_harness_noise():
    """goose stores its per-turn <turn-context> injection as a user entry:
    alone it is dropped, ahead of a prompt it is stripped."""
    block = ("<turn-context>\n<current-time>2026-08-30 05:27:00 +00:00</current-time>\n"
             "<working-directory>/workspace</working-directory>\n\nCurrent tasks and notes:\n"
             "Once given a task, immediately update your todo\n</turn-context>")
    assert is_harness_noise(block)
    assert strip_prompt_fold(block + "\n\nWhat is the codeword?") == "What is the codeword?"
    cleaned = clean_exported([_u(block), _u(block), _u(block + "\n\nq"), _a("PLUM-42")])
    assert [(m.role, m.content) for m in cleaned] == [("user", "q"), ("assistant", "PLUM-42")]
