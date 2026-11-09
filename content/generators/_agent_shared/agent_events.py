# SPDX-License-Identifier: MIT
"""Turn results, bounded events, attachments and prompt notes.

Extracted from the shared runtime without changing policy or event shapes.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log
import json
import re
from dataclasses import dataclass, field


@dataclass
class AgentResult:
    """One agent turn's outcome. File edits live in the workspace (returned to
    the caller verbatim); this carries the assistant text + the full-fidelity
    trace of the turn (thinking, tool activity, token usage) in the normalized
    event vocabulary defined at the trace-capture block below."""
    text: str = ""
    backend: str = ""
    trace: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class TurnRefused(RuntimeError):
    """The model service refused the turn's request outright (a 4xx error
    object the harness relayed as the reply text). Every later turn would be
    refused the same way, so the run ends failed rather than carrying on."""


def service_refusal(reply: str) -> str:
    """The refusal a turn's WHOLE reply states when it is a model service's
    structured 4xx error object — codex relays the service's
    ``{"type": "error", "status": 400, "error": {"type": ..., "message":
    ...}}`` as the reply text — or ``""`` for any other reply."""
    text = (reply or "").strip()
    if not (text.startswith("{") and text.endswith("}")):
        return ""
    try:
        obj = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(obj, dict) or obj.get("type", "error") != "error":
        return ""
    status, error = obj.get("status"), obj.get("error")
    if (not isinstance(status, int) or isinstance(status, bool)
            or not 400 <= status < 500 or not isinstance(error, dict)):
        return ""
    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        return ""
    kind = error.get("type")
    tag = f"{status} {kind}" if isinstance(kind, str) and kind else str(status)
    return f"the model service refused the turn ({tag}): {message.strip()}"


# A model call that never got a connection — what an HTTP client reports when
# the run's egress proxy refuses the CONNECT (httpx/openai: "Connection
# error.", requests: ProxyError / "Tunnel connection failed: 403").
_CONNECTION_FAILURE = re.compile(
    r"connection error|connecterror|proxyerror|tunnel connection failed|"
    r"cannot connect to proxy|connection refused",
    re.IGNORECASE,
)


def explain_turn_failure(error: "BaseException | str", *, backend: str, runtime: str,
                         cli_path: "str | None" = None,
                         internet: "str | None" = None) -> str:
    """The turn failure the user reads, with the real fix appended where the
    raw error points the wrong way:

    * A harness's own failure its ``explain_failure`` hook recognises
      (claude-code: Anthropic's per-model Claude Code version floor, whose own
      advice names the user's ``claude`` even when the turn ran the CLI
      bundled in Collimate's adapter). ``cli_path`` names the installed CLI
      when the adapter selected it.
    * A connection failure under a runner whose grant restricts ``internet``
      (``none``, or ``proxy`` with an allow list): the egress proxy refuses
      the model endpoint like any other host, and the bare "Connection error."
      reads like a provider outage rather than the grant that caused it."""
    text = str(error)
    if internet in ("none", "proxy") and _CONNECTION_FAILURE.search(text):
        if internet == "none":
            hint = ("this runner's grant has internet: none, so the agent cannot reach its model's "
                    "endpoint — give the runner internet access (full, or proxy with the model's host allowed)")
        else:
            hint = ("this runner's grant limits internet to its allow list — add the model endpoint's "
                    "host to internet_allow, or give the runner internet: full")
        return f"{text} ({hint})"
    from .harnesses import HARNESS, resolve
    row = HARNESS.get(str(backend or ""))
    hook = row.hooks.explain_failure if row else None
    if hook:
        hint = resolve(hook)(text, runtime=runtime, cli_path=cli_path)
        if hint:
            return f"{text} ({hint})"
    return text


# ──────────────────────────────────────────────────────────────────────
# Trace capture — the normalized full-fidelity record of one agent turn, and
# the durable raw record every surface persists (at TRACE_FILE below). Any
# processing of that record — mining, scoring, summarizing — is post-hoc and
# external: it never runs in-line with the run.
#
# Every backend collects far richer data than the reply text (thinking
# blocks, tool_use/tool_result, token usage); the trace retains it in one
# shared event vocabulary so downstream consumers are backend-agnostic:
#
#   {"type": "user",        "text": ...}
#   {"type": "text",        "text": ...}
#   {"type": "thinking",    "text": ...}
#   {"type": "tool_use",    "tool": ..., "input": ...}
#   {"type": "tool_result", "tool": ..., "output": ...,
#    "outcome": "failed"|"retried"}         outcome only for a call that went
#                                             wrong (pydantic-ai)
#   {"type": "system_note", "text": ...}
#
# The live ``on_event`` stream uses a smaller progress vocabulary:
#   {"type": "text_delta",     "text": ...}   reply chunks while streaming; an
#                                             ACP turn also emits the whole reply
#                                             once when stream=False
#   {"type": "thinking_delta", "text": ...}   thinking chunks from any backend
#                                             that streams thinking (streaming
#                                             only)
#   {"type": "tool", "id": ..., "name": ..., "status": "running"|"done"|"error",
#    "input": ..., "result": ...}             tool activity — a running event
#                                             with a bounded input summary,
#                                             then a completion with a bounded
#                                             result snippet (id pairs them;
#                                             backends without per-tool
#                                             structure emit bare heartbeats)
#   {"type": "text_reset", "reason": ...}     drop what streamed so far from
#                                             the reply: the turn is being
#                                             re-sent (an ACP transport
#                                             respawn), or what streamed was
#                                             the service's refusal, not a
#                                             reply (TurnRefused)
# ──────────────────────────────────────────────────────────────────────

# Where every surface persists that record — under _agent/ and written with
# category='log' (the log band), so callers reading a run's output files never
# pick up bookkeeping. A caller that wants the record to ride with its output
# cell copies it forward explicitly.
TRACE_FILE = "_agent/trace.json"

# Bounds for the live tool events' input/result summaries — the full payloads
# stay in the trace; the wire (and the transcript's display record downstream)
# carries a human-scale snippet.
TOOL_INPUT_CAP = 200
TOOL_RESULT_CAP = 500
ATTACH_BYTE_CAP = 20_000_000


def tool_summary(v, cap: int) -> str:
    """A bounded one-line summary of a tool input/result for the live event
    stream: JSON when it serializes (dicts render as their compact JSON),
    ``str()`` otherwise, truncated to ``cap`` with an ellipsis marker."""
    if v is None:
        return ""
    if isinstance(v, str):
        s = v
    else:
        try:
            s = json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            s = str(v)
    s = s.strip()
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _jsonable(v):
    """``v`` if it survives JSON round-tripping, else its ``str()`` form —
    trace events must always be serializable."""
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def trace_event(kind: str, **kw) -> dict:
    ev: dict = {"type": kind}
    for k, v in kw.items():
        if v is None or v == "":
            continue
        ev[k] = _jsonable(v)
    return ev


def clock_line() -> str:
    """One system-prompt line telling the model the real wall-clock time
    (RFC3339 UTC). Models have no clock, so anything they timestamp — a
    status comment, a log line, a dated filename — is otherwise an
    invented date (yesterday's, the skill example's) that renders as
    "1d ago" beside the user's real timestamps. Refreshed per turn by
    ``run_agent``; the terminal surface appends it via its extra-system
    assembly (``session.terminal``), and the ACP fold adds it per turn
    (``acp_run``)."""
    from datetime import datetime, timezone
    from .transcript_merge import CLOCK_HEAD
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return f"{CLOCK_HEAD}{now} (UTC)."


# ── Multimodal I/O helpers ────────────────────────────────────────────
#
# Input: caller ``attachments`` (``[{"data": bytes, "media_type": str,
# "name": str}]``) become native input parts per backend. Output: model file
# output (a generated image, etc.) is recovered back into AgentResult.files.
# ``iter_attachments`` is the one shared normalizer (skip-empty + oversize
# cap); each backend renders the usable entries into its own library's types.


def attachment_ref_lines(attachments) -> str:
    """The prompt block naming each attachment's CELL path (``attachments/
    <name>``, the folder the chat renderer uploads into) — ``""`` when none
    carries a workspace path. Shared wording with the ACP fold so the
    terminal-leg exporters can strip it (``transcript_merge``)."""
    lines: list = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        path = str(att.get("path") or "")
        if not path:
            continue
        mt = str(att.get("media_type") or "")
        lines.append(f"- {path} ({mt})" if mt else f"- {path}")
    if not lines:
        return ""
    from .transcript_merge import ATTACH_HDR
    return ATTACH_HDR + "\n" + "\n".join(lines)


def iter_attachments(attachments):
    """Yield ``(data: bytes, media_type: str, name: str)`` for each usable
    attachment. Entries that aren't dicts, or lack bytes / a media type, are
    skipped; an entry over ``ATTACH_BYTE_CAP`` is dropped (logged) so one bad
    file never sinks the whole turn."""
    for i, att in enumerate(attachments or []):
        if not isinstance(att, dict):
            continue
        data = att.get("data")
        mt = att.get("media_type")
        if not data or not mt:
            continue
        if isinstance(data, str):
            data = data.encode("utf-8")
        data = bytes(data)
        if len(data) > ATTACH_BYTE_CAP:
            safe_log(f"agent attachment #{i} dropped ({len(data)} bytes > cap)")
            continue
        name = str(att.get("name") or f"attachment-{i + 1}")
        yield data, str(mt), name


# Media types no provider can render as an IMAGE even though the media type
# says image/*: a vector document is not a raster frame, and every vision API
# behind this backend takes only raster bitmaps. It has to be named explicitly
# because the mappers disagree about it — the Responses mapper checks
# ``is_image`` and emits an input_image the API then rejects, while the
# openai-chat mapper sees ``+xml``, calls it text-like, and silently inlines
# the markup as a TEXT block (an image attachment that quietly stops being one).
















# ──────────────────────────────────────────────────────────────────────
# Shared logging helper
# ──────────────────────────────────────────────────────────────────────


def safe_log(msg: str) -> None:
    """``gen.log`` that never raises — ``gen.log`` needs a host and throws in
    dev/no-host, which must not turn best-effort wiring into a hard failure."""
    try:
        _secret_log(msg)
    except Exception:  # noqa: BLE001
        pass
