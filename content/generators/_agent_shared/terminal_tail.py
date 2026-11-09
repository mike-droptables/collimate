# SPDX-License-Identifier: MIT
"""Live terminal tail: a terminal run's conversation lands in its transcript
**while the TTY runs**, not only when it ends.

A terminal run hands the harness session to the harness's own TTY, so the
generator sees nothing of what is said there — until the leg's teardown
exports the harness's record and merges it into
:data:`transcript_merge.TERMINAL_TRANSCRIPT`, a run log artifact. A run
stopped hard never reaches that export, so the tail
keeps the record current: every ~3 s a per-harness :class:`TailReader`
re-reads the harness's CURRENT record, and the settled part of it is merged
into the transcript through the SAME append-only merge the teardown uses
(:func:`transcript_merge.write_merged_transcript`).

Two properties make the tail and the end-of-leg finalize safe together:

* **One merge, append-only and idempotent.** Both paths call
  ``write_merged_transcript``, which aligns the export against what the
  transcript already holds and appends only the tail. A message the tail
  already wrote is therefore never written twice — by the tail's own next
  poll, or by the finalize that runs after it.
* **Only SETTLED messages are tailed.** A record read mid-stream can hold a
  half-written assistant message (opencode/goose export a growing text part),
  and a truncated message appended now would still be there when the full one
  lands. So a poll merges only the prefix that is byte-identical to the
  previous poll's read, and never the record's last message while it is a
  reply (:func:`settled_prefix`): a reply that has held still for one poll
  can still be growing — opencode keeps one assistant message per turn and
  adds to it after every tool call — and is complete only once something
  follows it. The live mirror therefore lags the in-flight reply by one, and
  the leg's last reply is picked up by the finalize, which is the last
  reconcile either way.

Tier-C harnesses (qwen-code, cline, pi, vibe, copilot) have no readable record —
:func:`has_tail_reader` is False for them and the leg appends ONE
``system``-role marker at its end (:func:`append_opaque_marker`) instead of
pretending the conversation is mirrored.

The readers are deliberately built from the terminal runners' OWN export
helpers, so the tail and the finalize normalize the same record the same way:

===============  ====================================================
backend          reader
===============  ====================================================
claude-code      ``harnesses.claude_code.backend.pull_claude_transcript`` over the
                 Tier-A host session store (stat-gated re-read)
codex            ``harnesses.codex.backend._newest_codex_session`` +
                 ``trace_from_codex_session`` (a find, then the rollout
                 read whole)
opencode         ``opencode export`` of the leg's session (one exec,
                 read-only — never ``_export_chatlog``, which writes;
                 a fresh TTY's session found by ``opencode.harness_session_ids``)
goose            ``harnesses.goose.backend.goose_session_export`` +
                 ``trace_from_goose_session`` (an export to a file, read
                 whole; a fresh TTY's session found by
                 ``harnesses.goose.backend.harness_session_ids``)
antigravity      ``harnesses.antigravity.backend.messages_from_conversation_db`` over
                 the per-cell conversation store (stat-gated re-read of a
                 host-side SQLite file — no exec)
tier C           no reader (``None``) — the opaque marker (qwen-code,
                 cline, pi)
===============  ====================================================
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import asyncio
import json
import os

import gen
from .harnesses import HARNESS, resolve
from .harness_homes import harness_home

from gen import Message

from _agent_shared.transcript_merge import TERMINAL_TRANSCRIPT

# The poll interval (§9.1.2 "~3 s"). Every reader is one file read or one
# container exec, so this is cheap enough to hold for a whole leg.
TAIL_INTERVAL_S = 3.0

# The backends whose harness record the tail can read. Everything else is
# tier C for the purposes of the tail: opaque, marked once per leg.

# What a leg on a harness with no readable record leaves in the transcript.
OPAQUE_MARKER = ("Terminal session ({backend}) — its conversation is kept "
                 "in a record this transcript cannot read, so it is not "
                 "transcribed here.")


# ──────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested; no docker / gen).
# ──────────────────────────────────────────────────────────────────────


def has_tail_reader(backend: str) -> bool:
    """Whether this harness's live record can be tailed at all. False means
    the leg is opaque and gets the end-of-leg marker instead."""
    row = HARNESS.get(str(backend or ""))
    return bool(row and row.terminal and row.terminal.tail)


def _key(m) -> tuple:
    return (str(getattr(m, "role", "") or ""), str(getattr(m, "content", "") or ""))


def settled_prefix(prev: list, cur: list) -> list:
    """The part of ``cur`` that is SETTLED — its longest prefix that matches
    ``prev`` message for message, less the record's last message when that
    is a reply (the hold-back).

    Harness records grow at the end: a completed message never changes, while
    the one being streamed does. Merging only the unchanged prefix is what
    keeps a half-written assistant message out of the durable transcript (it
    would be appended in its truncated form and then again in full). Holding
    still is not enough for the record's LAST reply: a turn paused on a tool
    leaves it unchanged across polls, and opencode's one-message-per-turn
    record then grows it again — so it settles only once a later message
    follows it. The cost is one poll of latency, and the in-flight reply
    waits for the next message; the end-of-leg finalize is the last
    reconcile."""
    prev = list(prev or [])
    cur = list(cur or [])
    n = 0
    while n < len(prev) and n < len(cur) and _key(prev[n]) == _key(cur[n]):
        n += 1
    if n and n == len(cur) and str(getattr(cur[-1], "role", "") or "") != "user":
        n -= 1
    return cur[:n]


def messages_from_trace(trace: list) -> list:
    """The open-format transcript messages of a normalized trace — user +
    assistant TEXT only.

    The codex, goose and antigravity finalizers call this too, on purpose:
    the tail and the finalize must produce the SAME messages, or the merge's
    de-duplication would see two spellings of one turn."""
    return [
        Message(role="user" if ev.get("type") == "user" else "assistant",
                content=ev.get("text", ""))
        for ev in (trace or [])
        if isinstance(ev, dict) and ev.get("type") in ("user", "text") and ev.get("text")
    ]


def messages_from_opencode_export(data: dict) -> list:
    """The transcript messages of an ``opencode export`` document — the
    read-only half of ``harnesses.opencode.backend._export_chatlog``, which calls this before it
    WRITES, so the tail and the finalize produce identical messages."""
    from _agent_shared.harnesses.opencode.backend import text_from_parts

    out: list = []
    for m in (data or {}).get("messages") or []:
        if not isinstance(m, dict):
            continue
        info = m.get("info") if isinstance(m.get("info"), dict) else m
        role = str(info.get("role") or m.get("role") or "").lower()
        text = text_from_parts(m.get("parts")) or text_from_parts(info.get("parts")) \
            or (m.get("content") if isinstance(m.get("content"), str) else "")
        if role in ("user", "assistant", "system") and text and text.strip():
            out.append(Message(role=role, content=text.strip()))
    return out


def parse_opencode_export(raw: str) -> dict:
    """The JSON document out of an ``opencode export`` invocation's output
    (the CLI prints a banner ahead of it), or ``{}``."""
    start = (raw or "").find("{")
    if start < 0:
        return {}
    try:
        data = json.loads(raw[start:])
    except Exception:  # noqa: BLE001 — a partial/garbled export is "nothing new"
        return {}
    return data if isinstance(data, dict) else {}


# ──────────────────────────────────────────────────────────────────────
# The per-harness readers.
# ──────────────────────────────────────────────────────────────────────


class _ClaudeTail:
    """claude's session jsonl, read from the HOST store — this cell's harness
    home, which the terminal runner bind-mounts as ``CLAUDE_CONFIG_DIR`` under
    both auths, so the tail is a plain file read (no exec, no container).
    Re-parsed only when the file's (size, mtime) moved."""

    backend = "claude-code"

    def __init__(self, harness_session_id: str, store_dir: str, project: str) -> None:
        self.harness_session_id = harness_session_id
        self.store_dir = store_dir
        self.project = project
        self._stat: tuple = ()
        self._last: list = []

    def path(self) -> str:
        """The jsonl this tail stats, from the SAME candidate list the
        exporters read (``claude_session_paths``): the cwd slug first (where
        both legs file today), the reserved pinned name second. Deriving the
        path here instead would make the
        tail stat a file the export never reads — the leg would look dark
        while its transcript was landing one directory over. The first
        EXISTING candidate wins; with none on disk yet, candidate 0 is what
        the file will be called when the first turn is written."""
        from _agent_shared.harnesses.claude_code.backend import claude_session_paths
        paths = claude_session_paths(self.store_dir, self.project, self.harness_session_id)
        if not paths:
            return ""
        for path in paths:
            if os.path.exists(path):
                return path
        return paths[0]

    def read(self) -> list:
        from _agent_shared.harnesses.claude_code.backend import pull_claude_transcript
        try:
            # "" (nothing to look for) raises here too — a tail with no
            # readable path is "nothing new", never an error.
            st = os.stat(self.path())
        except OSError:
            return []
        key = (st.st_size, st.st_mtime_ns)
        if key == self._stat:
            return self._last
        rows, _raw = pull_claude_transcript(
            self.harness_session_id, store_dir=self.store_dir, project=self.project)
        self._stat = key
        self._last = [Message(role=r["role"], content=r["content"]) for r in rows]
        return self._last


class _CodexTail:
    """Host-side Codex rollout, stat-gated and parsed from the last byte offset."""

    backend = "codex"

    def __init__(self, store_dir: str) -> None:
        self.store_dir = store_dir
        self._key = None
        self._offset = 0
        self._pending = b""
        self._trace = []
        self._last = []
        self._anchor = b""

    def read(self) -> list:
        from collimate import fs
        from _agent_shared.harnesses.codex.backend import newest_codex_session_path, trace_from_codex_session

        path = newest_codex_session_path(self.store_dir)
        if not path:
            return []
        try:
            fd = fs.open_file(self.store_dir, os.path.relpath(path, self.store_dir), write=False)
        except FileNotFoundError:
            return []
        try:
            info = os.fstat(fd)
            identity = (path, info.st_dev, info.st_ino)
            key = (*identity, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if key == self._key:
                return self._last
            prefix_changed = (self._anchor and os.pread(fd, len(self._anchor),
                              self._offset - len(self._anchor)) != self._anchor)
            if (self._key is None or identity != self._key[:3]
                    or info.st_size <= self._offset or prefix_changed):
                self._offset = 0
                self._pending = b""
                self._trace = []
            os.lseek(fd, self._offset, os.SEEK_SET)
            pending = self._pending
            remaining = max(0, info.st_size - self._offset)
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                self._offset += len(chunk)
                pending += chunk
                complete, separator, pending = pending.rpartition(b"\n")
                if separator:
                    self._trace.extend(trace_from_codex_session(complete.decode("utf-8", "replace")))
            self._pending = pending
            anchor_size = min(64, self._offset)
            self._anchor = os.pread(fd, anchor_size, self._offset - anchor_size)
            self._key = key
            self._last = messages_from_trace(self._trace)
            return self._last
        finally:
            os.close(fd)


def _leg_session(harness_session_id: str, before, list_ids) -> str:
    """The session a tail follows: ``harness_session_id`` (the one the TTY runs
    on), else — a fresh TTY — the one session ``list_ids()`` holds that
    ``before`` (the store's ids when the leg started) lacks. ``""`` without
    either: the store's newest session may be an earlier run's."""
    if harness_session_id:
        return harness_session_id
    if before is None:
        return ""
    from _agent_shared import new_tty_session
    return new_tty_session(before, list_ids())


class _OpencodeTail:
    """opencode's export document (its sessions live in SQLite — the CLI is
    the only reader). One exec per poll (a fresh TTY's: one more, the
    listing that names its session, until that session appears)."""

    backend = "opencode"

    def __init__(self, env, harness_session_id: str = "",
                 before: "frozenset | None" = None) -> None:
        self.env = env
        self.harness_session_id = harness_session_id
        self.before = before

    def read(self) -> list:
        from _agent_shared.harnesses.opencode.backend import harness_session_ids, cli_output
        sid = _leg_session(self.harness_session_id, self.before,
                           lambda: harness_session_ids(self.env))
        if not sid:
            return []
        self.harness_session_id = sid
        raw = cli_output(self.env, ['export', sid])
        return messages_from_opencode_export(parse_opencode_export(raw))


class _GooseTail:
    """goose's ``session export --format json`` document (SQLite store since
    1.x), normalized through the same trace the teardown uses. A fresh TTY's
    session is found by listing the store until it appears."""

    backend = "goose"

    def __init__(self, env, harness_session_id: str = "",
                 before: "frozenset | None" = None) -> None:
        self.env = env
        self.harness_session_id = harness_session_id
        self.before = before

    def read(self) -> list:
        from _agent_shared.harnesses.goose.backend import (
            goose_session_export, harness_session_ids, trace_from_goose_session,
        )
        sid = _leg_session(self.harness_session_id, self.before,
                           lambda: harness_session_ids(self.env))
        if not sid:
            return []
        self.harness_session_id = sid
        raw = goose_session_export(self.env, sid)
        if not raw:
            return []
        return messages_from_trace(trace_from_goose_session(raw))


class _AntigravityTail:
    """agy's conversation database, read from the HOST store — this cell's
    harness home, which the terminal runner bind-mounts as the container's
    vendor state directory under both auths, so the tail is a plain SQLite read
    (no exec, no container). Re-parsed only when the file's (size, mtime)
    moved, exactly like :class:`_ClaudeTail`: agy touches the store on every
    step, and decoding a conversation's protobuf payloads is the one reader
    here that costs real CPU.

    agy's TTY names its own conversation, so the leg has no id to hand over:
    the tail follows the one conversation that appeared in the store after
    ``before`` (its ids when the leg started) (``new_tty_session``), so the
    live transcript mirrors the conversation the TTY wrote and the teardown
    export reads. An earlier conversation the per-cell
    home keeps is never mirrored, and while none or several have appeared
    there is nothing to read."""

    backend = "antigravity"

    def __init__(self, store_dir: str, before: frozenset) -> None:
        self.store_dir = store_dir
        self.before = frozenset(before)
        self._stat: tuple = ()
        self._last: list = []

    def path(self) -> str:
        """The database this tail stats — the TTY's conversation's (``""``
        while there is none to name)."""
        from _agent_shared import new_tty_session
        from _agent_shared.harnesses.antigravity.backend import (
            conversation_db_path, conversation_ids,
        )
        cid = new_tty_session(self.before, conversation_ids(self.store_dir))
        return conversation_db_path(self.store_dir, cid) if cid else ""

    def read(self) -> list:
        from _agent_shared.harnesses.antigravity.backend import messages_from_conversation_db
        path = self.path()
        try:
            # "" (nothing to look for) raises here too — a tail with no
            # readable path is "nothing new", never an error.
            st = os.stat(path)
        except OSError:
            return []
        # The WAL sibling moves ahead of the main file while a turn streams,
        # so it is part of the freshness key — without it a whole conversation
        # could land in one -wal and never be re-read.
        try:
            wal = os.stat(path + "-wal")
            wal_key = (wal.st_size, wal.st_mtime_ns)
        except OSError:
            wal_key = ()
        key = (path, st.st_size, st.st_mtime_ns, wal_key)
        if key == self._stat:
            return self._last
        self._stat = key
        self._last = messages_from_conversation_db(path)
        return self._last


class _HermesTail:
    """Hermes's SQLite session store, read from the HOST (this cell's harness
    home, the container's HERMES_HOME), re-read only when the database or its
    WAL moved. Hermes's TTY names its own conversation: the tail follows the
    one session that appeared after ``before``."""

    backend = "hermes"

    def __init__(self, home: str, before: frozenset) -> None:
        self.home = home
        self.before = frozenset(before)
        self._stat: tuple = ()
        self._last: list = []

    def read(self) -> list:
        from _agent_shared import new_tty_session
        from _agent_shared.harnesses.hermes.backend import (
            messages_from_state_db, session_ids, state_db_path,
        )
        path = state_db_path(self.home)
        try:
            st = os.stat(path)
        except OSError:
            return []
        try:
            wal = os.stat(path + "-wal")
            wal_key = (wal.st_size, wal.st_mtime_ns)
        except OSError:
            wal_key = ()
        key = (st.st_size, st.st_mtime_ns, wal_key)
        if key == self._stat:
            return self._last
        self._stat = key
        sid = new_tty_session(self.before, session_ids(self.home))
        self._last = messages_from_state_db(self.home, sid) if sid else []
        return self._last


def claude_tail(harness_session_id, before):
    if not harness_session_id:
        return None
    return _ClaudeTail(harness_session_id, harness_home("claude-code"), gen.workspace_path())


def codex_tail(harness_session_id, before):
    from .cli_container import cached_env
    return _CodexTail(harness_home("codex")) if cached_env("codex") is not None else None


def opencode_tail(harness_session_id, before):
    from .cli_container import cached_env
    env = cached_env("opencode")
    if env is None:
        return None
    return _OpencodeTail(env, harness_session_id, before)


def goose_tail(harness_session_id, before):
    from .cli_container import cached_env
    env = cached_env("goose")
    return _GooseTail(env, harness_session_id, before) if env is not None else None


def antigravity_tail(harness_session_id, before):
    from .harnesses.antigravity.backend import terminal_home
    return _AntigravityTail(terminal_home(), before or frozenset())


def hermes_tail(harness_session_id, before):
    return _HermesTail(harness_home("hermes"), before or frozenset())


def make_tail_reader(backend: str, *, harness_session_id: str = "",
                     before: "frozenset | None" = None):
    """Construct this harness's declared reader; failures never break the run."""
    row = HARNESS.get(str(backend or ""))
    if row is None or row.terminal is None or row.terminal.tail is None:
        return None
    try:
        return resolve(row.terminal.tail)(harness_session_id, before)
    except Exception as exc:  # noqa: BLE001 — a tail is never load-bearing
        _log(f"no live terminal tail for {backend}: {exc}")
        return None



# ──────────────────────────────────────────────────────────────────────
# The tail itself.
# ──────────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    try:
        _secret_log(msg)
    except Exception:  # noqa: BLE001 — bridge-free callers
        pass


class TranscriptTail:
    """One leg's tail: a reader, the previous read (for :func:`settled_prefix`)
    and the append-only merge into the durable transcript."""

    def __init__(self, reader, *, path: str = TERMINAL_TRANSCRIPT,
                 backend: str = "") -> None:
        self.reader = reader
        self.path = path
        self.backend = backend or str(getattr(reader, "backend", "") or "")
        self.added = 0
        self._prev: list = []
        self._merged: list = []
        self.failed = False

    def merge(self, messages: list) -> int:
        """Merge the settled prefix of one read; returns the number of
        messages APPENDED (0 whenever the record has not moved on)."""
        settled = settled_prefix(self._prev, messages)
        self._prev = list(messages or [])
        keys = [_key(m) for m in settled]
        if not settled or keys == self._merged:
            return 0
        from _agent_shared.transcript_merge import write_merged_transcript
        added = write_merged_transcript(settled, path=self.path)
        # Only once the write lands: a write that raised is retried by the
        # next poll over the same settled prefix.
        self._merged = keys
        if added <= 0:
            return 0
        self.added += added
        # One line per appended batch — a terminal leg can run for a long
        # time and the log is not the transcript.
        _log(f"{self.path}: {added} message(s) tailed live from the "
             f"{self.backend} terminal")
        return added

    async def poll(self) -> int:
        """Read off-loop, then merge the small settled prefix on this loop."""
        messages = await asyncio.to_thread(self.reader.read)
        return self.merge(messages)


async def tail_transcript(backend: str, *, stop_event=None,
                          interval: float = TAIL_INTERVAL_S,
                          harness_session_id: str = "",
                          before: "frozenset | None" = None,
                          reader=None, path: str = TERMINAL_TRANSCRIPT) -> int:
    """Tail the live terminal leg's conversation into the transcript until
    cancelled (or ``stop_event`` is set). Returns the number of messages
    appended over the leg. ``before`` is the store's session ids when the
    leg started (:func:`make_tail_reader`).

    Started by ``session.terminal.terminal_leg`` right after the runner's
    ``on_ready`` (the TTY is up, the harness's store exists) and cancelled
    when the leg ends. Never raises out: a tail that cannot read is a leg
    that reconciles only at its end, which is the pre-tail behaviour."""
    r = reader if reader is not None else make_tail_reader(
        backend, harness_session_id=harness_session_id, before=before)
    if r is None:
        return 0
    tail = TranscriptTail(r, path=path, backend=backend)
    while stop_event is None or not stop_event.is_set():
        await asyncio.sleep(interval)
        if stop_event is not None and stop_event.is_set():
            break
        try:
            await tail.poll()
        except Exception as exc:  # noqa: BLE001 — one report, then stay quiet
            if not tail.failed:
                tail.failed = True
                _log(f"terminal tail ({backend}) read failed: {exc}")
    return tail.added


# ──────────────────────────────────────────────────────────────────────
# End-of-leg bookkeeping.
# ──────────────────────────────────────────────────────────────────────


def append_opaque_marker(backend: str, *, path: str = TERMINAL_TRANSCRIPT) -> int:
    """The tier-C leg's one transcript entry (§9.1.2): the conversation stayed
    in the harness's own terminal and is not mirrored. Appended ONCE at the
    end of the leg — never per poll — and through the same idempotent merge,
    so a re-run over the same transcript adds nothing."""
    try:
        from _agent_shared.transcript_merge import write_merged_transcript
        return write_merged_transcript(
            [Message(role="system", content=OPAQUE_MARKER.format(backend=backend))],
            path=path)
    except Exception as exc:  # noqa: BLE001
        _log(f"terminal marker failed: {exc}")
        return 0


def end_of_leg(backend: str, *, ran: bool = True,
               path: str = TERMINAL_TRANSCRIPT) -> None:
    """What every leg does as it ends, tail or no tail: an opaque harness
    gets its one marker. ``ran`` is False for a leg whose TTY never came up
    (a runner that failed to start) — there was no terminal conversation, so
    no marker. (The leg's ``.terminal`` pointer is ``gen.serve_terminal``'s:
    written when the TTY registers, deleted when its block exits.)"""
    if ran and not has_tail_reader(backend):
        append_opaque_marker(backend, path=path)
