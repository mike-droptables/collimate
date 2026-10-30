"""Annotation contexts: the message stores of runs (context id = run id,
created with the run row) and cells (context id 'cell', created on first
write). Pure functions over an open channel connection; callers own the
transaction, the registry liveness snapshot (``live``) and the SSE
doorbells (``Bell``).

A message is top-level or a reply to a top-level message (one level).
Top-level messages are ordered by ``position``; a reply stores position 0
and sorts under its root by creation. A message is read once a run pulled
it, and a read message is locked: no edit, delete, reorder or mode change.
Run posts and the host-seeded prompt (``kind='prompt'``) are born read.
Held messages sit below the queue bar, after every unheld one, and a run
never sees them; held implies queued.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.parse
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from collimate.fs import relative_parts
from collimate._host.ipc import (
    ANNOTATION_BODY_MAX_BYTES,
    ANNOTATION_ID_PATTERN,
    ANNOTATION_MODES,
    new_annotation_id,
)

from reserved_paths import is_reserved_path
from run_folders import mint_run_label

CELL = "cell"
MODES: tuple[str, ...] = tuple(sorted(ANNOTATION_MODES, reverse=True))
BODY_MAX_BYTES = ANNOTATION_BODY_MAX_BYTES
CONTEXT_ITEMS_MAX = 16
ITEM_LABEL_MAX = 256
ITEM_TEXT_MAX = 16384
ANCHOR_MAX_CHARS = 4096
RENDERER_ID_MAX = 128
REFS_MAX = 64
CONTEXT_MAX_MESSAGES = 2000
PULL_MAX_MESSAGES = 64
PULL_MAX_BYTES = 4 * 1024 * 1024
ID_RE = re.compile(ANNOTATION_ID_PATTERN)
mint_id = new_annotation_id

_ITEM_KEYS = frozenset({"path", "renderer_id", "label", "text", "anchor"})

_STATUS: dict[str, int] = {
    "invalid": 422,
    "empty_text": 422,
    "too_large": 413,
    "not_found": 404,
    "unknown_message": 404,
    "unknown_context": 409,
    "sealed": 409,
    "locked": 409,
    "revision": 409,
    "parent_not_root": 409,
    "not_root": 409,
    "stale": 409,
    "bar_off": 409,
    "not_in_cell_context": 409,
    "run_not_live": 409,
    "operation_reused": 409,
    "duplicate_id": 409,
    "full": 409,
}

_DETAIL: dict[str, str] = {
    "invalid": "The request is not valid.",
    "empty_text": "Write a message first.",
    "too_large": "The message is over 64 KiB.",
    "not_found": "No such message.",
    "unknown_message": "The message you answered is gone.",
    "unknown_context": "No such annotation context.",
    "sealed": "This run no longer takes messages.",
    "locked": "A message a run has read cannot change.",
    "revision": "The message changed elsewhere; look again and retry.",
    "parent_not_root": "Replies answer top-level messages only.",
    "not_root": "Only top-level messages can interrupt.",
    "stale": "The queue changed elsewhere; look again and retry.",
    "bar_off": "Turn the queue bar on first.",
    "not_in_cell_context": "Only the cell's own messages can be sent to a run.",
    "run_not_live": "That run is no longer live.",
    "operation_reused": "That operation id was used for another request.",
    "duplicate_id": "A message with that id already exists.",
    "full": f"This context holds {CONTEXT_MAX_MESSAGES} messages; delete some first.",
}

_COLS = ("rowid AS seq, id, cell_id, context_id, parent_id, author, author_label, kind, body, "
         "context_json, refs_json, mode, position, held, revision, created_at, edited_at, read_at")

# The unread messages a run sees: unheld roots, and replies whose root is
# not held.
_DELIVERABLE = (
    "SELECT a.rowid AS seq, a.id, a.cell_id, a.context_id, a.parent_id, a.author, a.author_label, "
    "a.kind, a.body, a.context_json, a.refs_json, a.mode, a.position, a.held, a.revision, "
    "a.created_at, a.edited_at, a.read_at "
    "FROM annotations a LEFT JOIN annotations r ON r.id = a.parent_id "
    "WHERE a.cell_id = ? AND a.context_id = ? AND a.read_at IS NULL "
    "AND ((a.parent_id IS NULL AND a.held = 0) OR (a.parent_id IS NOT NULL AND r.held = 0))"
)


class AnnotationRefused(Exception):
    """A refused annotation write: ``code`` (the wire error), a sentence for
    people, and the HTTP status the code answers with."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or _DETAIL[code])
        self.code = code
        self.message = message or _DETAIL[code]
        self.status = _STATUS[code]


class _Crowded(Exception):
    """Two neighbouring positions have no float left between them."""


@dataclass(frozen=True)
class Bell:
    """One ``annotations_changed`` doorbell to ring after commit."""
    cell_id: str
    context_id: str
    rev: int


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def request_digest(value: dict) -> str:
    """The digest a receipt compares a retried request against."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_LINK_RE = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def _cell_path(path: str) -> str | None:
    """``path`` as a normalized cell-relative path, or None when it climbs
    out, is absolute or lies in the reserved staging band."""
    try:
        norm = "/".join(relative_parts(path))
    except (PermissionError, ValueError):
        return None
    return None if is_reserved_path(norm) else norm


def body_refs(body: str) -> list[str]:
    """The cell paths a markdown body links or embeds, in order, deduped:
    relative link and image targets only (no URL scheme, anchor or
    absolute path), query and fragment cut, percent-decoded."""
    refs: list[str] = []
    for match in _LINK_RE.finditer(body):
        target = match.group(1)
        if _SCHEME_RE.match(target) or target.startswith(("#", "/")):
            continue
        target = urllib.parse.unquote(re.split(r"[#?]", target, maxsplit=1)[0])
        path = _cell_path(target) if target else None
        if path is None or path in refs:
            continue
        refs.append(path)
        if len(refs) >= REFS_MAX:
            break
    return refs


def _check_body(body: Any) -> str:
    if not isinstance(body, str):
        raise AnnotationRefused("invalid", "The message body must be text.")
    if not body.strip():
        raise AnnotationRefused("empty_text")
    if len(body.encode("utf-8")) > BODY_MAX_BYTES:
        raise AnnotationRefused("too_large")
    return body


def _check_items(items: Any) -> list[dict]:
    """The context items a renderer answered, checked: at most 16, each
    exactly ``{path, renderer_id, label, text, anchor}`` with a normalized
    cell path; sizes are characters (the compact JSON of the anchor)."""
    if not isinstance(items, list) or len(items) > CONTEXT_ITEMS_MAX:
        raise AnnotationRefused("invalid", f"A message carries at most {CONTEXT_ITEMS_MAX} context items.")
    checked: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
            raise AnnotationRefused("invalid", "A context item is {path, renderer_id, label, text, anchor}.")
        path, renderer_id, label, text, anchor = (item[k] for k in ("path", "renderer_id", "label", "text", "anchor"))
        if not isinstance(path, str) or not path or _cell_path(path) != path:
            raise AnnotationRefused("invalid", "A context item names a cell path.")
        if not isinstance(renderer_id, str) or not 1 <= len(renderer_id) <= RENDERER_ID_MAX:
            raise AnnotationRefused("invalid", "A context item names its renderer.")
        if not isinstance(label, str) or len(label) > ITEM_LABEL_MAX:
            raise AnnotationRefused("invalid", f"A context item label is at most {ITEM_LABEL_MAX} characters.")
        if not isinstance(text, str) or len(text) > ITEM_TEXT_MAX:
            raise AnnotationRefused("invalid", f"A context item text is at most {ITEM_TEXT_MAX} characters.")
        if anchor is not None:
            if not isinstance(anchor, dict):
                raise AnnotationRefused("invalid", "A context item anchor is an object.")
            try:
                encoded = json.dumps(anchor, separators=(",", ":"), ensure_ascii=False)
            except (TypeError, ValueError):
                raise AnnotationRefused("invalid", "A context item anchor is plain JSON.") from None
            if len(encoded) > ANCHOR_MAX_CHARS:
                raise AnnotationRefused("invalid", f"A context item anchor is at most {ANCHOR_MAX_CHARS} characters of JSON.")
        checked.append({"path": path, "renderer_id": renderer_id, "label": label, "text": text, "anchor": anchor})
    return checked


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _context(conn: sqlite3.Connection, cell_id: str, context_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, cell_id, run_id, bar_on, listening, interrupt_seq, rev, sealed_at "
        "FROM annotation_contexts WHERE cell_id = ? AND id = ?", (cell_id, context_id)).fetchone()


def _open_context(conn: sqlite3.Connection, cell_id: str, context_id: str, live: Collection[str], *,
                  create_cell: bool = False) -> sqlite3.Row:
    """The context a write lands in: the cell context is created on first
    write; a run context refuses once sealed or when its run is not live."""
    row = _context(conn, cell_id, context_id)
    if row is None:
        if context_id != CELL or not create_cell:
            raise AnnotationRefused("unknown_context")
        conn.execute("INSERT INTO annotation_contexts (id, cell_id, run_id) VALUES (?, ?, NULL)", (CELL, cell_id))
        row = _context(conn, cell_id, context_id)
        assert row is not None
    if row["sealed_at"] is not None:
        raise AnnotationRefused("sealed")
    if row["run_id"] is not None and row["run_id"] not in live:
        raise AnnotationRefused("sealed")
    return row


def _bump(conn: sqlite3.Connection, cell_id: str, context_id: str, *, interrupt: bool = False) -> Bell:
    extra = ", interrupt_seq = interrupt_seq + 1" if interrupt else ""
    conn.execute(f"UPDATE annotation_contexts SET rev = rev + 1{extra} WHERE cell_id = ? AND id = ?",
                 (cell_id, context_id))
    rev = conn.execute("SELECT rev FROM annotation_contexts WHERE cell_id = ? AND id = ?",
                       (cell_id, context_id)).fetchone()[0]
    return Bell(cell_id, context_id, int(rev))


def _row(conn: sqlite3.Connection, cell_id: str, annotation_id: str) -> sqlite3.Row:
    row = conn.execute(f"SELECT {_COLS} FROM annotations WHERE id = ? AND cell_id = ?",
                       (annotation_id, cell_id)).fetchone()
    if row is None:
        raise AnnotationRefused("not_found")
    return row


def _tops(conn: sqlite3.Connection, cell_id: str, ctx: str) -> list[sqlite3.Row]:
    """The context's top-level messages in display order."""
    return conn.execute(
        "SELECT rowid AS seq, id, position, held, read_at, mode, author FROM annotations "
        "WHERE cell_id = ? AND context_id = ? AND parent_id IS NULL "
        "ORDER BY position, created_at, rowid", (cell_id, ctx)).fetchall()


def _count(conn: sqlite3.Connection, cell_id: str, ctx: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM annotations WHERE cell_id = ? AND context_id = ?",
                            (cell_id, ctx)).fetchone()[0])


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

def _spread(lo: float | None, hi: float | None, n: int) -> list[float]:
    """``n`` increasing positions strictly between ``lo`` and ``hi`` (an
    open end when None)."""
    if lo is None and hi is None:
        return [float(i + 1) for i in range(n)]
    if lo is None:
        assert hi is not None
        return [hi - (n - i) for i in range(n)]
    if hi is None:
        return [lo + i + 1 for i in range(n)]
    values = [lo + (hi - lo) * (i + 1) / (n + 1) for i in range(n)]
    previous = lo
    for value in values:
        if not previous < value < hi:
            raise _Crowded()
        previous = value
    return values


def _renumber(conn: sqlite3.Connection, cell_id: str, ctx: str) -> None:
    """Rewrite the top-level positions to 1..N in their current order."""
    for index, top in enumerate(_tops(conn, cell_id, ctx)):
        conn.execute("UPDATE annotations SET position = ? WHERE rowid = ?", (float(index + 1), top["seq"]))


def _bounds(tops: list[sqlite3.Row], where: str) -> tuple[float | None, float | None]:
    unheld = [t["position"] for t in tops if not t["held"]]
    held = [t["position"] for t in tops if t["held"]]
    if where == "head":
        waiting = [t["position"] for t in tops if not t["held"] and t["read_at"] is None]
        if waiting:
            hi = min(waiting)
            below = [t["position"] for t in tops if t["position"] < hi]
            return (max(below) if below else None), hi
        where = "tail"
    if where == "tail":
        return (max(unheld) if unheld else None), (min(held) if held else None)
    # 'end': after everything, where held messages go.
    every = unheld + held
    return (max(every) if every else None), None


def _place(conn: sqlite3.Connection, cell_id: str, ctx: str, where: str, *,
           exclude: Collection[str] = (), n: int = 1) -> list[float]:
    """Positions for ``n`` top-level messages: ``tail`` (after every unheld
    message, before every held one), ``end`` (after everything, for held
    messages) or ``head`` (before the first unread unheld message, after
    the read ones before it; ``tail`` when nothing waits)."""
    for attempt in range(2):
        tops = [t for t in _tops(conn, cell_id, ctx) if t["id"] not in exclude]
        try:
            return _spread(*_bounds(tops, where), n)
        except _Crowded:
            if attempt:
                raise
            _renumber(conn, cell_id, ctx)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Wire
# ---------------------------------------------------------------------------

def _message(row: sqlite3.Row, root_held: bool | None = None) -> dict:
    """The Message wire of a row; a reply's ``held`` mirrors its root's."""
    held = bool(root_held) if row["parent_id"] is not None else bool(row["held"])
    return {
        "id": row["id"],
        "context_id": row["context_id"],
        "parent_id": row["parent_id"],
        "author": row["author"],
        "author_label": row["author_label"],
        "kind": row["kind"],
        "body": row["body"],
        "context_items": json.loads(row["context_json"]),
        "refs": json.loads(row["refs_json"]),
        "mode": row["mode"],
        "position": row["position"],
        "held": held,
        "revision": row["revision"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
        "read_at": row["read_at"],
    }


def _messages(conn: sqlite3.Connection, cell_id: str, ctx: str, *, hide_held: bool = False) -> list[dict]:
    """Roots by position, each followed by its replies by creation; with
    ``hide_held`` the held roots and their replies are left out."""
    rows = conn.execute(f"SELECT {_COLS} FROM annotations WHERE cell_id = ? AND context_id = ?",
                        (cell_id, ctx)).fetchall()
    roots = sorted((r for r in rows if r["parent_id"] is None),
                   key=lambda r: (r["position"], r["created_at"], r["seq"]))
    replies: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["parent_id"] is not None:
            replies.setdefault(row["parent_id"], []).append(row)
    out: list[dict] = []
    for root in roots:
        if hide_held and root["held"]:
            continue
        out.append(_message(root))
        for reply in sorted(replies.get(root["id"], ()), key=lambda r: (r["created_at"], r["seq"])):
            out.append(_message(reply, bool(root["held"])))
    return out


def _label(conn: sqlite3.Connection, run_id: str | None) -> str | None:
    if run_id is None:
        return None
    row = conn.execute("SELECT runner_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    return None if row is None else mint_run_label(row[0], run_id)


def _wire(conn: sqlite3.Connection, row: sqlite3.Row, *, hide_held: bool = False) -> dict:
    """The Context wire of a context row."""
    return {
        "id": row["id"],
        "cell_id": row["cell_id"],
        "run_id": row["run_id"],
        "label": _label(conn, row["run_id"]),
        "bar_on": bool(row["bar_on"]),
        "listening": bool(row["listening"]),
        "interrupt_seq": int(row["interrupt_seq"]),
        "rev": int(row["rev"]),
        "sealed": row["sealed_at"] is not None,
        "messages": _messages(conn, row["cell_id"], row["id"], hide_held=hide_held),
    }


def _current(conn: sqlite3.Connection, cell_id: str, context_id: str) -> dict:
    """The Context wire of a context the caller just wrote."""
    row = _context(conn, cell_id, context_id)
    assert row is not None
    return _wire(conn, row)


def _empty_cell(cell_id: str) -> dict:
    """The cell context before its first write."""
    return {"id": CELL, "cell_id": cell_id, "run_id": None, "label": None, "bar_on": False,
            "listening": False, "interrupt_seq": 0, "rev": 0, "sealed": False, "messages": []}


def _receipt(conn: sqlite3.Connection, operation_id: str, cell_id: str, digest: str) -> dict | None:
    """The stored answer of a replayed post, or None for a new operation."""
    row = conn.execute("SELECT cell_id, request_digest, result_json FROM annotation_operations "
                       "WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        return None
    if row["cell_id"] != cell_id or row["request_digest"] != digest:
        raise AnnotationRefused("operation_reused")
    return json.loads(row["result_json"])


def _store_receipt(conn: sqlite3.Connection, operation_id: str, cell_id: str, digest: str, result: dict) -> None:
    conn.execute("INSERT INTO annotation_operations (operation_id, cell_id, request_digest, result_json) "
                 "VALUES (?, ?, ?, ?)", (operation_id, cell_id, digest, json.dumps(result, ensure_ascii=False)))


def _insert(conn: sqlite3.Connection, *, annotation_id: str, cell_id: str, context_id: str,
            parent_id: str | None, author: str, author_label: str | None, kind: str, body: str,
            items: list[dict], mode: str, position: float, held: bool, read: bool) -> sqlite3.Row:
    now = utc_now()
    conn.execute(
        "INSERT INTO annotations (id, cell_id, context_id, parent_id, author, author_label, kind, body, "
        "context_json, refs_json, mode, position, held, revision, created_at, edited_at, read_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?)",
        (annotation_id, cell_id, context_id, parent_id, author, author_label, kind, body,
         json.dumps(items, ensure_ascii=False), json.dumps(body_refs(body), ensure_ascii=False),
         mode, position, int(held), now, now if read else None))
    return _row(conn, cell_id, annotation_id)


def _fresh_id(conn: sqlite3.Connection) -> str:
    while True:
        candidate = mint_id()
        if conn.execute("SELECT 1 FROM annotations WHERE id = ?", (candidate,)).fetchone() is None:
            return candidate


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------

def create_run_context(conn: sqlite3.Connection, cell_id: str, run_id: str) -> None:
    """The run's context, created in the caller's run-row transaction."""
    conn.execute("INSERT INTO annotation_contexts (id, cell_id, run_id) VALUES (?, ?, ?)",
                 (run_id, cell_id, run_id))


def seed_prompt(conn: sqlite3.Connection, cell_id: str, run_id: str, prompt: str) -> None:
    """The run's prompt as its context's first message: user-authored,
    ``kind='prompt'``, born read (stored whole; the host wrote it)."""
    _insert(conn, annotation_id=_fresh_id(conn), cell_id=cell_id, context_id=run_id, parent_id=None,
            author="user", author_label=None, kind="prompt", body=prompt, items=[], mode="queued",
            position=1.0, held=False, read=True)


def get_context(conn: sqlite3.Connection, cell_id: str, context_id: str) -> dict | None:
    """A context of this cell in any state; the cell context is
    synthesized before its first write. None for an unknown context."""
    row = _context(conn, cell_id, context_id)
    if row is None:
        return _empty_cell(cell_id) if context_id == CELL else None
    return _wire(conn, row)


def list_contexts(conn: sqlite3.Connection, cell_id: str, run_ids: Iterable[str]) -> list[dict]:
    """The cell context, then each named run's context that lives on this
    cell, in the given order."""
    out = [get_context(conn, cell_id, CELL) or _empty_cell(cell_id)]
    for run_id in run_ids:
        row = _context(conn, cell_id, run_id)
        if row is not None and row["run_id"] is not None:
            out.append(_wire(conn, row))
    return out


def listening(conn: sqlite3.Connection, run_ids: Collection[str]) -> dict[str, bool]:
    """Whether each run has polled its context (runs without one are left out)."""
    ids = list(run_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT run_id, listening FROM annotation_contexts WHERE run_id IN ({marks})", ids)
    return {row[0]: bool(row[1]) for row in rows}


def unsealed_runs(conn: sqlite3.Connection) -> list[str]:
    """Run ids whose context is not sealed yet."""
    return [row[0] for row in conn.execute(
        "SELECT run_id FROM annotation_contexts WHERE run_id IS NOT NULL AND sealed_at IS NULL")]


def run_context_cell(conn: sqlite3.Connection, run_id: str) -> str | None:
    """The cell a run's context lives on, or None when the run has none."""
    row = conn.execute("SELECT cell_id FROM annotation_contexts WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# User writes
# ---------------------------------------------------------------------------

def post(conn: sqlite3.Connection, cell_id: str, *, context_id: str, body: Any, context_items: Any,
         mode: str, held: bool, parent_id: str | None, operation_id: str, digest: str,
         live: Collection[str]) -> tuple[dict, list[Bell]]:
    """A user message, top-level or a reply; the same operation id replays
    its stored answer before anything else is checked."""
    stored = _receipt(conn, operation_id, cell_id, digest)
    if stored is not None:
        return stored, []
    ctx = _open_context(conn, cell_id, context_id, live, create_cell=True)
    if _count(conn, cell_id, context_id) >= CONTEXT_MAX_MESSAGES:
        raise AnnotationRefused("full")
    body = _check_body(body)
    items = _check_items(context_items)
    if mode not in MODES:
        raise AnnotationRefused("invalid", "The mode is queued or interrupt.")
    root_held = False
    if parent_id is not None:
        parent = conn.execute(f"SELECT {_COLS} FROM annotations WHERE id = ?", (parent_id,)).fetchone()
        if parent is None or parent["cell_id"] != cell_id or parent["context_id"] != context_id:
            raise AnnotationRefused("unknown_message")
        if parent["parent_id"] is not None:
            raise AnnotationRefused("parent_not_root")
        if held:
            raise AnnotationRefused("invalid", "A reply follows its message across the bar.")
        root_held = bool(parent["held"])
        position = 0.0
    else:
        if held and mode == "interrupt":
            raise AnnotationRefused("invalid", "A held message is queued.")
        if held and not ctx["bar_on"]:
            raise AnnotationRefused("bar_off")
        where = "end" if held else ("head" if mode == "interrupt" else "tail")
        position = _place(conn, cell_id, context_id, where)[0]
    row = _insert(conn, annotation_id=_fresh_id(conn), cell_id=cell_id, context_id=context_id,
                  parent_id=parent_id, author="user", author_label=None, kind="message", body=body,
                  items=items, mode=mode, position=position, held=held, read=False)
    deliverable = not root_held if parent_id is not None else not held
    bell = _bump(conn, cell_id, context_id, interrupt=mode == "interrupt" and deliverable)
    message = _message(row, root_held)
    _store_receipt(conn, operation_id, cell_id, digest, message)
    return message, [bell]


def patch(conn: sqlite3.Connection, cell_id: str, annotation_id: str, *, expected_revision: int,
          body: Any = None, context_items: Any = None, mode: str | None = None,
          live: Collection[str]) -> tuple[dict, list[Bell]]:
    """Edit an unread message's body, context items or mode. Switching a
    top-level message to interrupt un-holds it and moves it to the head."""
    if body is None and context_items is None and mode is None:
        raise AnnotationRefused("invalid", "Name a field to change.")
    row = _row(conn, cell_id, annotation_id)
    _open_context(conn, cell_id, row["context_id"], live)
    if row["read_at"] is not None:
        raise AnnotationRefused("locked")
    if row["revision"] != expected_revision:
        raise AnnotationRefused("revision")
    context_id = row["context_id"]
    sets: dict[str, Any] = {}
    if body is not None:
        body = _check_body(body)
        if body != row["body"]:
            sets.update(body=body, refs_json=json.dumps(body_refs(body), ensure_ascii=False), edited_at=utc_now())
    if context_items is not None:
        encoded = json.dumps(_check_items(context_items), ensure_ascii=False)
        if encoded != row["context_json"]:
            sets["context_json"] = encoded
    interrupt = False
    root_held = False
    if row["parent_id"] is not None:
        root = conn.execute("SELECT held FROM annotations WHERE id = ?", (row["parent_id"],)).fetchone()
        root_held = bool(root and root["held"])
    if mode is not None:
        if mode not in MODES:
            raise AnnotationRefused("invalid", "The mode is queued or interrupt.")
        if mode != row["mode"]:
            sets["mode"] = mode
            if mode == "interrupt":
                if row["parent_id"] is None:
                    sets["held"] = 0
                    sets["position"] = _place(conn, cell_id, context_id, "head", exclude={annotation_id})[0]
                    interrupt = True
                else:
                    interrupt = not root_held
    if not sets:
        return _message(row, root_held), []
    sets["revision"] = row["revision"] + 1
    assignments = ", ".join(f"{column} = ?" for column in sets)
    conn.execute(f"UPDATE annotations SET {assignments} WHERE rowid = ?", (*sets.values(), row["seq"]))
    bell = _bump(conn, cell_id, context_id, interrupt=interrupt)
    return _message(_row(conn, cell_id, annotation_id), root_held), [bell]


def delete(conn: sqlite3.Connection, cell_id: str, annotation_id: str, *, expected_revision: int,
           live: Collection[str]) -> list[Bell]:
    """Delete an unread message; an unread root takes its unread replies.
    A root with any read reply is locked."""
    row = _row(conn, cell_id, annotation_id)
    _open_context(conn, cell_id, row["context_id"], live)
    if row["revision"] != expected_revision:
        raise AnnotationRefused("revision")
    if row["parent_id"] is None:
        read = conn.execute("SELECT 1 FROM annotations WHERE (id = ? OR parent_id = ?) AND read_at IS NOT NULL",
                            (annotation_id, annotation_id)).fetchone()
        if read is not None:
            raise AnnotationRefused("locked")
        conn.execute("DELETE FROM annotations WHERE id = ? OR parent_id = ?", (annotation_id, annotation_id))
    else:
        if row["read_at"] is not None:
            raise AnnotationRefused("locked")
        conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
    return [_bump(conn, cell_id, row["context_id"])]


def send(conn: sqlite3.Connection, cell_id: str, annotation_id: str, *, run_id: str,
         live: Collection[str]) -> tuple[dict, list[Bell]]:
    """Move a cell-context message and its replies into a live run's
    context: last queued, or first when its mode is interrupt."""
    row = _row(conn, cell_id, annotation_id)
    if row["context_id"] != CELL:
        raise AnnotationRefused("not_in_cell_context")
    if row["parent_id"] is not None:
        raise AnnotationRefused("parent_not_root")
    target = _context(conn, cell_id, run_id)
    if target is None or target["run_id"] is None or target["sealed_at"] is not None or run_id not in live:
        raise AnnotationRefused("run_not_live")
    moving = int(conn.execute("SELECT COUNT(*) FROM annotations WHERE id = ? OR parent_id = ?",
                              (annotation_id, annotation_id)).fetchone()[0])
    if _count(conn, cell_id, run_id) + moving > CONTEXT_MAX_MESSAGES:
        raise AnnotationRefused("full")
    position = _place(conn, cell_id, run_id, "head" if row["mode"] == "interrupt" else "tail")[0]
    conn.execute("UPDATE annotations SET context_id = ?, held = 0, revision = revision + 1 "
                 "WHERE cell_id = ? AND (id = ? OR parent_id = ?)", (run_id, cell_id, annotation_id, annotation_id))
    conn.execute("UPDATE annotations SET position = ? WHERE id = ?", (position, annotation_id))
    bells = [_bump(conn, cell_id, CELL), _bump(conn, cell_id, run_id, interrupt=row["mode"] == "interrupt")]
    return _message(_row(conn, cell_id, annotation_id)), bells


def reorder(conn: sqlite3.Connection, cell_id: str, context_id: str, *, ids: list[str],
            held_from: int | None, live: Collection[str]) -> tuple[dict, list[Bell]]:
    """Rewrite the order of the context's unread top-level messages, which
    ``ids`` names exactly; those from ``held_from`` on become held (and
    queued). Read messages keep their positions, before every unread one."""
    ctx = _open_context(conn, cell_id, context_id, live, create_cell=True)
    tops = _tops(conn, cell_id, context_id)
    read = {t["id"] for t in tops if t["read_at"] is not None}
    unread = {t["id"] for t in tops if t["read_at"] is None}
    if any(i in read for i in ids):
        raise AnnotationRefused("locked")
    if len(set(ids)) != len(ids) or set(ids) != unread:
        raise AnnotationRefused("stale")
    held_from = len(ids) if held_from is None else min(held_from, len(ids))
    if held_from < len(ids) and not ctx["bar_on"]:
        raise AnnotationRefused("bar_off")
    base = max((t["position"] for t in tops if t["read_at"] is not None), default=0.0)
    current = {r["id"]: r for r in (_row(conn, cell_id, i) for i in ids)}
    for index, annotation_id in enumerate(ids):
        row = current[annotation_id]
        held = index >= held_from
        mode = "queued" if held else row["mode"]
        changed = bool(row["held"]) != held or mode != row["mode"]
        conn.execute("UPDATE annotations SET position = ?, held = ?, mode = ?, revision = revision + ? "
                     "WHERE rowid = ?", (base + 1 + index, int(held), mode, int(changed), row["seq"]))
    bell = _bump(conn, cell_id, context_id)
    return _current(conn, cell_id, context_id), [bell]


def set_bar(conn: sqlite3.Connection, cell_id: str, context_id: str, *, on: bool,
            live: Collection[str]) -> tuple[dict, list[Bell]]:
    """Turn the queue bar on or off; off un-holds every held message,
    keeping their order."""
    ctx = _open_context(conn, cell_id, context_id, live, create_cell=True)
    changed = bool(ctx["bar_on"]) != on
    conn.execute("UPDATE annotation_contexts SET bar_on = ? WHERE cell_id = ? AND id = ?",
                 (int(on), cell_id, context_id))
    if not on:
        released = conn.execute("UPDATE annotations SET held = 0, revision = revision + 1 "
                                "WHERE cell_id = ? AND context_id = ? AND held = 1", (cell_id, context_id))
        changed = changed or released.rowcount > 0
    bells = [_bump(conn, cell_id, context_id)] if changed else []
    return _current(conn, cell_id, context_id), bells


def interrupt(conn: sqlite3.Connection, cell_id: str, context_id: str, *, ids: list[str],
              live: Collection[str]) -> tuple[dict, list[Bell]]:
    """Make unread top-level messages interrupts: they move to the head in
    the given order, un-held, and ``interrupt_seq`` steps once."""
    _open_context(conn, cell_id, context_id, live)
    ordered = list(dict.fromkeys(ids))
    rows = []
    for annotation_id in ordered:
        row = _row(conn, cell_id, annotation_id)
        if row["context_id"] != context_id:
            raise AnnotationRefused("not_found")
        if row["parent_id"] is not None:
            raise AnnotationRefused("not_root")
        if row["read_at"] is not None:
            raise AnnotationRefused("locked")
        rows.append(row)
    positions = _place(conn, cell_id, context_id, "head", exclude=set(ordered), n=len(ordered))
    for row, position in zip(rows, positions):
        changed = row["mode"] != "interrupt" or bool(row["held"])
        conn.execute("UPDATE annotations SET mode = 'interrupt', held = 0, position = ?, "
                     "revision = revision + ? WHERE rowid = ?", (position, int(changed), row["seq"]))
    bell = _bump(conn, cell_id, context_id, interrupt=True)
    return _current(conn, cell_id, context_id), [bell]


# ---------------------------------------------------------------------------
# Run side (IPC)
# ---------------------------------------------------------------------------

def notice(conn: sqlite3.Connection, cell_id: str, run_id: str) -> dict:
    """What a run's poll sees: ``{rev, unread, interrupt_seq, listening,
    sealed}``; ``unread`` counts the messages a pull would take."""
    row = _context(conn, cell_id, run_id)
    if row is None:
        raise AnnotationRefused("unknown_context")
    unread = conn.execute(f"SELECT COUNT(*) FROM ({_DELIVERABLE})", (cell_id, run_id)).fetchone()[0]
    return {"rev": int(row["rev"]), "unread": int(unread), "interrupt_seq": int(row["interrupt_seq"]),
            "listening": bool(row["listening"]), "sealed": row["sealed_at"] is not None}


def mark_listening(conn: sqlite3.Connection, cell_id: str, run_id: str) -> list[Bell]:
    """The run polled for the first time."""
    changed = conn.execute("UPDATE annotation_contexts SET listening = 1, rev = rev + 1 "
                           "WHERE cell_id = ? AND id = ? AND listening = 0", (cell_id, run_id)).rowcount
    if not changed:
        return []
    rev = conn.execute("SELECT rev FROM annotation_contexts WHERE cell_id = ? AND id = ?",
                       (cell_id, run_id)).fetchone()[0]
    return [Bell(cell_id, run_id, int(rev))]


def _size(row: sqlite3.Row) -> int:
    return len(row["body"].encode("utf-8")) + len(row["context_json"].encode("utf-8"))


def pull(conn: sqlite3.Connection, cell_id: str, run_id: str, *,
         live: Collection[str]) -> tuple[list[dict], list[Bell]]:
    """Mark the run's deliverable messages read and return them: units of
    an unread root with its unread replies (or a lone unread reply whose
    root is read), interrupts first, then by root position; one batch holds
    at most 64 messages and 4 MiB, and the rest stays unread."""
    _open_context(conn, cell_id, run_id, live)
    rows = conn.execute(_DELIVERABLE, (cell_id, run_id)).fetchall()
    if not rows:
        return [], []
    roots = {r["id"]: r for r in rows if r["parent_id"] is None}
    units: dict[str, list[sqlite3.Row]] = {root_id: [root] for root_id, root in roots.items()}
    for row in sorted((r for r in rows if r["parent_id"] is not None), key=lambda r: (r["created_at"], r["seq"])):
        if row["parent_id"] in roots:
            units[row["parent_id"]].append(row)
        else:
            units[row["id"]] = [row]
    positions = {r["id"]: r["position"] for r in conn.execute(
        "SELECT id, position FROM annotations WHERE cell_id = ? AND context_id = ? AND parent_id IS NULL",
        (cell_id, run_id))}

    def key(unit: list[sqlite3.Row]) -> tuple:
        lead = unit[0]
        root_id = lead["id"] if lead["parent_id"] is None else lead["parent_id"]
        return (lead["mode"] != "interrupt", positions.get(root_id, 0.0), lead["created_at"], lead["seq"])

    batch: list[sqlite3.Row] = []
    size = 0
    for unit in sorted(units.values(), key=key):
        unit_size = sum(_size(r) for r in unit)
        if batch and (len(batch) + len(unit) > PULL_MAX_MESSAGES or size + unit_size > PULL_MAX_BYTES):
            break
        batch.extend(unit)
        size += unit_size
    now = utc_now()
    conn.executemany("UPDATE annotations SET read_at = ? WHERE rowid = ?", [(now, r["seq"]) for r in batch])
    bell = _bump(conn, cell_id, run_id)
    return [_message(_row(conn, cell_id, r["id"]), False) for r in batch], [bell]


def run_post(conn: sqlite3.Connection, cell_id: str, run_id: str, *, label: str, message_id: Any,
             body: Any, reply_to: str | None, operation_id: str, digest: str,
             live: Collection[str]) -> tuple[dict, list[Bell]]:
    """A run's message into its own context: run-authored, labelled, born
    read; a reply answers a top-level message the run can see. A
    remembered operation replays before anything else is checked."""
    stored = _receipt(conn, operation_id, cell_id, digest)
    if stored is not None:
        return stored, []
    _open_context(conn, cell_id, run_id, live)
    if not isinstance(message_id, str) or not ID_RE.fullmatch(message_id):
        raise AnnotationRefused("invalid", "A message id is a_ and 16 hex digits.")
    if conn.execute("SELECT 1 FROM annotations WHERE id = ?", (message_id,)).fetchone() is not None:
        raise AnnotationRefused("duplicate_id")
    body = _check_body(body)
    if _count(conn, cell_id, run_id) >= CONTEXT_MAX_MESSAGES:
        raise AnnotationRefused("full")
    if reply_to is not None:
        parent = conn.execute(f"SELECT {_COLS} FROM annotations WHERE id = ?", (reply_to,)).fetchone()
        if parent is None or parent["cell_id"] != cell_id or parent["context_id"] != run_id:
            raise AnnotationRefused("unknown_message")
        if parent["parent_id"] is not None:
            raise AnnotationRefused("parent_not_root")
        if parent["held"]:
            raise AnnotationRefused("unknown_message")
        position = 0.0
    else:
        position = _place(conn, cell_id, run_id, "tail")[0]
    row = _insert(conn, annotation_id=message_id, cell_id=cell_id, context_id=run_id, parent_id=reply_to,
                  author="run", author_label=label, kind="message", body=body, items=[], mode="queued",
                  position=position, held=False, read=True)
    bell = _bump(conn, cell_id, run_id)
    message = _message(row, False)
    _store_receipt(conn, operation_id, cell_id, digest, message)
    return message, [bell]


def list_for_run(conn: sqlite3.Connection, cell_id: str, run_id: str) -> list[dict]:
    """The run's whole context as it sees it (held messages left out);
    readable after the seal too."""
    if _context(conn, cell_id, run_id) is None:
        raise AnnotationRefused("unknown_context")
    return _messages(conn, cell_id, run_id, hide_held=True)


# ---------------------------------------------------------------------------
# End of a run
# ---------------------------------------------------------------------------

def seal(conn: sqlite3.Connection, cell_id: str, run_id: str) -> list[Bell]:
    """Close the run's context to every write; idempotent."""
    changed = conn.execute("UPDATE annotation_contexts SET sealed_at = ?, rev = rev + 1 "
                           "WHERE cell_id = ? AND id = ? AND sealed_at IS NULL",
                           (utc_now(), cell_id, run_id)).rowcount
    if not changed:
        return []
    rev = conn.execute("SELECT rev FROM annotation_contexts WHERE cell_id = ? AND id = ?",
                       (cell_id, run_id)).fetchone()[0]
    return [Bell(cell_id, run_id, int(rev))]


def has_messages(conn: sqlite3.Connection, cell_id: str, run_id: str) -> bool:
    return _count(conn, cell_id, run_id) > 0


def dump(conn: sqlite3.Connection, cell_id: str, run_id: str) -> bytes:
    """The run's context as its log artifact: exactly what GET answers."""
    row = _context(conn, cell_id, run_id)
    if row is None:
        raise AnnotationRefused("unknown_context")
    return (json.dumps(_wire(conn, row), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
