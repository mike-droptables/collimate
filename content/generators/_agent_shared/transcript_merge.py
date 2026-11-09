# SPDX-License-Identifier: MIT
"""Merge a harness-native session export into a terminal run's transcript.

A terminal run's conversation happens in the harness's own TTY, so the only
record of it is the harness's: the live tail re-reads it while the TTY runs,
and the runner's teardown exports it (claude's session jsonl, codex's
rollout, goose's session export, ``opencode export``) as the TTY closes.
Both land in :data:`TERMINAL_TRANSCRIPT`, an open-format transcript written
as a run LOG artifact — the run's Logs section keeps it; the cell never
shows it.

The harness stores prompt text exactly as the engine folded it (the
session-context preamble, a wall-clock refresh, staged-attachment references
— never the user's bare words), so every export is cleaned first
(:func:`clean_exported`). And the export only ever APPENDS: the tail and the
teardown both merge into the same file, so :func:`merge_exported_transcript`
aligns the export's user messages to the transcript's (in order, after
stripping the engine preamble), keeps the transcript's own entries verbatim
and adds only the export's tail — counting the replies the transcript
already holds for its last user message, so a tool-using turn's later
replies (one assistant message per text block) still land after the tail
recorded its first. When nothing aligns (a foreign export) the export's
messages are appended after a de-duplication pass, so the record still gains
the new turns and never loses what it held.
"""
from __future__ import annotations
from _agent_shared.secret_hygiene import write_log as _write_log

import re
import threading

from gen import Message

# A terminal run's open-format transcript — a run LOG artifact beside the
# harness's raw exports under ``_agent/terminal/``, never a cell file of the
# run (a Capture Chat cell carries its own copy, under ``_agent/capture/``).
TERMINAL_TRANSCRIPT = "_agent/terminal/transcript.json"

# Serializes write_merged_transcript's read -> merge -> write: the live tail
# (on the loop) and a runner's teardown export (in a worker thread) both
# rewrite the transcript, and an unlocked interleave lets the later write
# drop the other's additions.
_WRITE_LOCK = threading.Lock()

# The session-context preamble markers the engine folds ahead of a NEW
# session's first prompt (acp_run._fold_prompt). Bracketed prose rather than
# markup: every harness shows the prompt back in its own TUI, and a labelled
# block reads as what it is there while staying trivially strippable here.
FOLD_OPEN = "[Session context — from Collimate, not the user's message]"
FOLD_CLOSE = "[End of session context]"

# The wall-clock line (_agent_shared.clock_line) as it appears at the head of
# a prompt when the engine refreshes a stale clock.
CLOCK_HEAD = "The current date and time is "
_CLOCK_RE = re.compile(
    r"\A" + re.escape(CLOCK_HEAD) + r"\S+ \(UTC\)\.[ \t]*(?:\n+|\Z)")
# The attachment reference block's header (``_agent_shared.attachment_ref_lines``
# builds the block; it imports this so the two can never drift).
ATTACH_HDR = ("The user attached these files (paths are relative to the "
              "workspace root):")

# claude stores its slash-command bookkeeping as user-role entries in the
# session jsonl (the `/model` injection the adapter uses for a model switch):
# harness noise, never something the user typed.
_HARNESS_NOISE_RE = re.compile(
    r"\A\s*<(?:local-command-caveat|command-name|command-message|command-args|"
    r"local-command-stdout|local-command-stderr)\b", re.IGNORECASE)


# goose injects a per-turn `<turn-context>` block (its own clock, cwd, and
# the task-list nudge) as a user-role entry ahead of every prompt in its
# session record; in the export it also appears alone (a turn that never
# got a prompt). Not something the user typed — stripped from the text,
# and an entry that was only that block is dropped.
_TURN_CONTEXT_RE = re.compile(r"<turn-context>.*?</turn-context>\s*", re.S | re.I)


def is_harness_noise(text: str) -> bool:
    """A user-role entry the HARNESS wrote for itself (claude's local
    slash-command records, goose's bare turn-context block) — dropped from
    the transcript."""
    t = text or ""
    if _HARNESS_NOISE_RE.match(t):
        return True
    return bool(t.strip()) and not _TURN_CONTEXT_RE.sub("", t).strip()


def strip_prompt_fold(text: str) -> str:
    """The user's own words from a harness-stored prompt: the folded session
    context block, a leading wall-clock line, and the staged-attachment
    reference block are the engine's, not the user's."""
    s = _TURN_CONTEXT_RE.sub("", text or "").lstrip("\n")
    if s.startswith(FOLD_OPEN):
        end = s.find(FOLD_CLOSE)
        if end >= 0:
            s = s[end + len(FOLD_CLOSE):].lstrip("\n")
    m = _CLOCK_RE.match(s)
    if m:
        s = s[m.end():]
    if s.startswith(ATTACH_HDR):
        # The block is the header plus one "- path (type)" line per file,
        # ended by the blank line before the prompt proper.
        cut = s.find("\n\n")
        s = s[cut + 2:] if cut >= 0 else ""
    return s.strip("\n")


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def clean_exported(messages: list) -> list[Message]:
    """The export's messages as transcript-ready ``Message``s: harness noise
    dropped, the engine preamble stripped from user text, blanks dropped."""
    out: list[Message] = []
    for m in messages or []:
        role = str(getattr(m, "role", "") or "").lower()
        content = str(getattr(m, "content", "") or "")
        if role == "user":
            if is_harness_noise(content):
                continue
            content = strip_prompt_fold(content)
        if role not in ("user", "assistant", "system") or not content.strip():
            continue
        out.append(Message(
            role=role, content=content,
            thinking=str(getattr(m, "thinking", "") or "") if role == "assistant" else "",
            attachments=list(getattr(m, "attachments", []) or []),
            tools=list(getattr(m, "tools", []) or []) if role == "assistant" else [],
            stopped=bool(getattr(m, "stopped", False)) if role == "assistant" else False,
        ))
    return out


def merge_exported_transcript(existing: list, exported: list) -> list[Message]:
    """The transcript after a terminal leg: ``existing`` verbatim, plus the
    turns in ``exported`` the transcript does not hold yet.

    Alignment walks the transcript's user messages in order, finding each
    (whitespace-normalized, preamble-stripped) in the export; the export's
    tail after the last aligned user message is the terminal leg's
    contribution, less as many of that message's replies as the transcript
    already holds after its own last user message. A turn is one assistant
    message per text block around its tool calls, and the live tail records
    the ones that have settled, so the count — not "any reply at all" — is
    what keeps the rest of the turn. The counts are comparable because a
    readable harness's transcript has two writers only, the live tail and
    the runner's teardown exporter, and both turn one harness record into
    the same messages through the same helpers (``terminal_tail``'s readers
    are built from the exporters'); a harness with no readable record gets
    only the leg's one opaque marker (``terminal_tail.append_opaque_marker``).
    A transcript whose user messages do not all appear in the export
    (another session's record, a prompt the harness stored in a form the
    preamble strip does not recover) gets the export's messages appended
    after a (role, text) de-duplication pass instead — never a rewrite."""
    existing = list(existing or [])
    cleaned = clean_exported(exported)
    if not existing:
        return cleaned
    if not cleaned:
        return existing

    j = 0
    last_user_at = -1
    aligned = True
    for m in existing:
        if m.role != "user":
            continue
        target = _norm(m.content)
        k = j
        while k < len(cleaned) and not (
                cleaned[k].role == "user" and _norm(cleaned[k].content) == target):
            k += 1
        if k >= len(cleaned):
            aligned = False
            break
        last_user_at = k
        j = k + 1

    # A prefix match only: every export user message up to the last aligned
    # one must be one of the transcript's — interleaved strangers mean this
    # export is another conversation that merely repeats a message.
    if aligned and last_user_at >= 0:
        n_existing_users = sum(1 for m in existing if m.role == "user")
        n_export_users = sum(1 for m in cleaned[:last_user_at + 1] if m.role == "user")
        aligned = n_existing_users == n_export_users
    if aligned and last_user_at >= 0:
        # The replies the transcript already holds to its last user message
        # are on record: skip that many of the export's copies, and append
        # the rest of the turn and every turn after it. A transcript that
        # ENDS on a user message holds none, so it takes the export's whole
        # reply.
        held = 0
        for m in reversed(existing):
            if m.role == "user":
                break
            held += 1
        start = last_user_at + 1
        while held and start < len(cleaned) and cleaned[start].role != "user":
            start += 1
            held -= 1
        return existing + cleaned[start:]

    known = {(m.role, _norm(m.content)) for m in existing}
    tail = [m for m in cleaned if (m.role, _norm(m.content)) not in known]
    return existing + tail


def write_merged_transcript(exported: list, *,
                            path: str = TERMINAL_TRANSCRIPT) -> int:
    """Merge ``exported`` into the transcript at ``path`` (a run log
    artifact) and write it back when anything was added. Returns the number
    of messages added (0 = the file was left untouched). Best-effort reads:
    an unreadable transcript is treated as empty, which still only ever
    appends."""
    import gen
    from collimate.transcript import parse_transcript, serialize_transcript

    with _WRITE_LOCK:
        from .secret_hygiene import refresh
        refresh()
        existing: list = []
        try:
            if gen.has_file(path):
                raw = gen.read_file(path)
                text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                existing = parse_transcript(text)
        except Exception:  # noqa: BLE001 — treat as empty; the merge only appends
            existing = []
        merged = merge_exported_transcript(existing, exported)
        added = len(merged) - len(existing)
        if added <= 0 and existing:
            from .secret_hygiene import scrub
            if scrub(serialize_transcript(existing)) == serialize_transcript(existing):
                return 0
        if not merged:
            return 0
        _write_log(path, serialize_transcript(merged), category="log")
        return added
