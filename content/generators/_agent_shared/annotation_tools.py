# SPDX-License-Identifier: MIT
"""The Annotation surface's tools: ``annotate`` posts to the run's annotation
context and ``list_annotations`` reads it back. They are rows of the
workspace registry (``workspace_tools.tool_entries(annotation_tools=True)``),
so the MCP bridge every harness reaches offers the pair.

The session's annotation leg records each message it presents
(:func:`note_messages`), so a reply naming a reply's id is posted in that
reply's thread, and it reads which posts a turn made (:func:`mark` /
:func:`posted_since`) to know whether the turn answered for itself."""
from __future__ import annotations

import json

import gen

# What gen.annotations.post accepts (UTF-8 bytes); a longer message belongs
# in a cell file the reply links.
_MAX_BYTES = 64 * 1024
# list_annotations' bounds: the newest messages, each text capped, the whole
# dump capped.
_LIST_MAX = 100
_LIST_TEXT_CAP = 4 * 1024
_LIST_DUMP_CAP = 64 * 1024

_ROOTS: dict[str, str] = {}   # message id -> its thread's root id
_POSTED: list[str] = []       # ids this run posted through annotate, in order


def note_messages(messages) -> None:
    """Remember each message's thread root, so a reply to a reply goes to
    its thread."""
    for m in messages:
        mid = getattr(m, "id", "") or ""
        if mid:
            _ROOTS[mid] = getattr(m, "reply_to", None) or mid


def mark() -> int:
    """A position in this run's annotate posts (see :func:`posted_since`)."""
    return len(_POSTED)


def posted_since(position: int) -> list[str]:
    """The ids ``annotate`` posted after ``position``."""
    return _POSTED[position:]


def clear() -> None:
    """Forget the run's threads and posts (run teardown)."""
    _ROOTS.clear()
    _POSTED.clear()


async def _ws_annotate(text: str, reply_to: str = "") -> str:
    """Post a message to this run's annotation context; the user reads it in the run's pane. reply_to names the thread you answer (a message's thread id; a reply's id is taken to its thread). Leave it empty for a new top-level note. Markdown; link cell files with relative links."""
    if not isinstance(text, str) or not text.strip():
        return "ERROR: annotate needs non-empty text"
    if len(text.encode("utf-8")) > _MAX_BYTES:
        return ("ERROR: the message is over 64 KiB; write the detail to a cell "
                "file and link it")
    target = _ROOTS.get(reply_to, reply_to) if reply_to else None
    try:
        mid = await gen.annotations.post(text, reply_to=target)
    except Exception as exc:  # noqa: BLE001 — the model reads why
        return f"ERROR: {exc}"
    _POSTED.append(mid)
    return f"posted {mid}" + (f" in thread {target}" if target else "")


def _capped(text: str) -> str:
    text = text or ""
    raw = text.encode("utf-8")
    if len(raw) <= _LIST_TEXT_CAP:
        return text
    return raw[:_LIST_TEXT_CAP].decode("utf-8", "ignore") + "…[truncated]"


def _entry(m) -> dict:
    return {
        "id": m.id,
        "thread": m.reply_to or m.id,
        "author": m.author,
        "author_label": m.author_label,
        "kind": m.kind,
        "mode": m.mode,
        "read": m.read_at is not None,
        "at": m.created_at,
        "text": _capped(m.text),
        "context": [{"path": c.path, "label": c.label, "text": _capped(c.text)}
                    for c in (m.context or ())],
        "refs": list(m.refs or ()),
    }


async def _ws_list_annotations() -> str:
    """Read this run's whole annotation context — the user's messages (read and unread), the run's prompt and every reply — oldest first, as JSON. Use it when a message refers back to an earlier one."""
    try:
        messages = list(await gen.annotations.list())
    except Exception as exc:  # noqa: BLE001 — the model reads why
        return f"ERROR: {exc}"
    note_messages(messages)
    omitted = max(len(messages) - _LIST_MAX, 0)
    entries = [_entry(m) for m in messages[omitted:]]
    while True:
        dump = json.dumps({"messages": entries, "omitted": omitted}, ensure_ascii=False)
        if len(dump.encode("utf-8")) <= _LIST_DUMP_CAP or not entries:
            return dump
        entries.pop(0)
        omitted += 1


def annotation_tool_entries() -> list:
    """The registry rows ``(name, gate, main_loop, twin_note, impl)``: both
    await ``gen.annotations``, so they run on the generator's loop."""
    return [
        ("annotate", "annotation", True, "gen.annotations.post()", _ws_annotate),
        ("list_annotations", "annotation", True, "gen.annotations.list()", _ws_list_annotations),
    ]
