# SPDX-License-Identifier: MIT
"""Chat transcript format — the open, provider-agnostic standard. Public.

A transcript is a single JSON document holding an OpenAI-compatible ``messages`` array — the
universal de-facto interchange shape every LLM provider and chat UI (including
assistant-ui, via ``convertMessage``) understands::

    {
      "messages": [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi — how can I help?"}
      ]
    }

One transcript file per chat **thread** (``<threads_dir>/<thread_id>.json``,
inside the serving run's folder — e.g. ``_runs/<label>/chat/main.json``); the
threads all share the cell's workspace files. ``role`` is one of
``user`` / ``assistant`` / ``system`` (case-insensitive; unknown roles — incl.
``tool`` — are dropped on read so a stray object can't smuggle in a bogus
message, and so parse/compose/render agree on exactly the visible roles).
The document is pretty-printed so it diffs cleanly in the cell's version
history. ``parse_transcript(serialize_transcript(m))`` round-trips. Assistant
messages may carry optional fields, each written only when non-empty/set and
preserved on read: ``"thinking"`` (the model's extended-thinking text),
``"tools"`` (a bounded display record of the turn's tool activity —
``{name, status, input?, result?}`` summaries; the full-fidelity record stays
in ``_agent/trace.json``), and ``"stopped"`` (the turn was interrupted and the
content is a partial).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

@dataclass
class Message:
    """A single chat message in the generator conversation."""
    role: str  # "user" | "assistant" | "system"
    content: str
    # The model's reasoning behind an assistant message (extended-thinking
    # text). Optional — empty for user/system messages and for backends/models
    # that expose no thinking. Persisted in the open transcript only when
    # non-empty, so plain transcripts stay plain.
    thinking: str = ""
    # File attachments on the message — each a cell-relative reference
    # ``{"path", "name", "media_type"}`` (the bytes live in the cell, not the
    # transcript). A user message carries files the person attached; an
    # assistant message carries files the model produced (e.g. a generated
    # image). Empty for plain text messages, so text-only transcripts stay
    # exactly the ``{role, content}`` shape.
    attachments: list = field(default_factory=list)
    # Bounded display record of the tool activity behind an assistant message —
    # ``{"name", "status", "input"?, "result"?}`` summaries (the full-fidelity
    # record lives in the run's trace, not the transcript). Empty for other
    # roles and for tool-less turns; persisted only when non-empty.
    tools: list = field(default_factory=list)
    # The turn behind an assistant message was interrupted (Stop) and the
    # content is a partial. Persisted only when True.
    stopped: bool = False


_ROLES = ("user", "assistant", "system")

# Bounds for the per-message tool display record — the transcript carries a
# human-scale summary (the trace holds the full record), so a runaway producer
# can't bloat the diff-friendly document.
_MAX_TOOLS = 50
_TOOL_TEXT_CAP = 2000
# The ACP-typed extras (agent-v2): ToolKind, file locations, and structured
# diff/terminal content blocks. Bounded like everything else — a diff block
# carries real file text, so it gets its own (larger) cap.
_MAX_TOOL_LOCATIONS = 20
_MAX_TOOL_BLOCKS = 8
_DIFF_TEXT_CAP = 16_000


def _clean_tool_locations(raw) -> list:
    """``[{path, line?}]`` — the ACP ToolCallLocation list, bounded."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list = []
    for loc in raw:
        if not isinstance(loc, dict):
            continue
        path = str(loc.get("path", "") or "")
        if not path:
            continue
        entry: dict = {"path": path[:500]}
        line = loc.get("line")
        if isinstance(line, int) and not isinstance(line, bool):
            entry["line"] = line
        out.append(entry)
        if len(out) >= _MAX_TOOL_LOCATIONS:
            break
    return out


def _clean_tool_content(raw) -> list:
    """The structured ``content`` blocks a tool entry may carry — ACP's
    ``diff`` (path + oldText/newText) and ``terminal`` (a terminal id
    reference) shapes. Anything else is dropped; text is capped."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "diff":
            entry: dict = {"type": "diff", "path": str(b.get("path", "") or "")[:500]}
            old = b.get("oldText")
            if old is not None:
                entry["oldText"] = str(old)[:_DIFF_TEXT_CAP]
            entry["newText"] = str(b.get("newText", "") or "")[:_DIFF_TEXT_CAP]
            out.append(entry)
        elif t == "terminal":
            tid = str(b.get("terminalId", "") or "")
            if tid:
                out.append({"type": "terminal", "terminalId": tid[:200]})
        if len(out) >= _MAX_TOOL_BLOCKS:
            break
    return out


def _clean_tools(raw) -> list:
    """Normalize a message's tool-activity record to the bounded display shape.

    Each valid entry is ``{"name", "status"}`` plus optional ``"input"`` /
    ``"result"`` string summaries and the ACP-typed extras — ``"kind"`` (the
    ToolKind), ``"locations"`` (file locations), and ``"content"``
    (structured diff / terminal blocks) — a non-empty ``name`` is required,
    ``status`` defaults to ``"done"``, and text fields are truncated to hard
    caps. Anything malformed is dropped; at most :data:`_MAX_TOOLS` entries
    survive.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name", "") or "")
        if not name:
            continue
        entry: dict = {
            "name": name[:200],
            "status": str(t.get("status", "") or "done"),
        }
        for k in ("input", "result"):
            v = t.get(k)
            if v is None or v == "":
                continue
            entry[k] = str(v)[:_TOOL_TEXT_CAP]
        kind = t.get("kind")
        if kind:
            entry["kind"] = str(kind)[:40]
        locations = _clean_tool_locations(t.get("locations"))
        if locations:
            entry["locations"] = locations
        content = _clean_tool_content(t.get("content"))
        if content:
            entry["content"] = content
        out.append(entry)
        if len(out) >= _MAX_TOOLS:
            break
    return out


def _clean_attachments(raw) -> list:
    """Normalize a message's attachments to a list of cell-file references.

    Each valid attachment is ``{"path", "name", "media_type"}`` with string
    values and a non-empty ``path`` (the bytes live in the cell). Anything else
    in the list is dropped, so a malformed entry can't smuggle non-string data
    into the transcript. Returns ``[]`` when there is nothing usable.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        path = str(a.get("path", "") or "")
        if not path:
            continue
        out.append({
            "path": path,
            "name": str(a.get("name", "") or path.rsplit("/", 1)[-1]),
            "media_type": str(a.get("media_type", "") or ""),
        })
    return out


def serialize_transcript(messages: list[Message]) -> str:
    """Render messages to the open transcript JSON (OpenAI-compatible).

    An assistant message's ``thinking`` (extended-thinking text) is written as
    an optional ``"thinking"`` field only when non-empty — plain transcripts
    stay exactly the two-key shape.
    A message's file ``attachments`` (cell-relative ``{path, name, media_type}``
    references) are written as an optional ``"attachments"`` field only when
    present, so text-only transcripts are unchanged.
    """
    out = []
    for m in messages:
        role = (m.role or "").lower()
        if role not in _ROLES:
            continue
        entry: dict = {"role": role, "content": m.content or ""}
        # Thinking / tools / stopped are assistant-message concepts; never
        # written for other roles.
        if role == "assistant" and getattr(m, "thinking", ""):
            entry["thinking"] = m.thinking
        if role == "assistant":
            tools = _clean_tools(getattr(m, "tools", None))
            if tools:
                entry["tools"] = tools
            if getattr(m, "stopped", False):
                entry["stopped"] = True
        attachments = _clean_attachments(getattr(m, "attachments", None))
        if attachments:
            entry["attachments"] = attachments
        out.append(entry)
    return json.dumps({"messages": out}, indent=2, ensure_ascii=False) + "\n"


def parse_transcript(text: str) -> list[Message]:
    """Parse the open transcript JSON back into messages.

    Tolerant: accepts either ``{"messages": [...]}`` or a bare top-level array,
    skips entries that aren't ``{role, content}`` objects or carry an unknown
    role, and returns ``[]`` for empty/unparseable input.
    """
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    messages: list[Message] = []
    for obj in items:
        if not isinstance(obj, dict):
            continue
        role = str(obj.get("role", "")).lower()
        if role not in _ROLES:
            continue
        raw = obj.get("content")    # an OpenAI tool_calls turn carries null
        messages.append(Message(
            role=role,
            content=("" if raw is None else str(raw)),
            thinking=(str(obj.get("thinking", "") or "")
                      if role == "assistant" else ""),
            attachments=_clean_attachments(obj.get("attachments")),
            tools=(_clean_tools(obj.get("tools"))
                   if role == "assistant" else []),
            stopped=(bool(obj.get("stopped"))
                     if role == "assistant" else False),
        ))
    return messages


__all__ = ["parse_transcript", "serialize_transcript"]
