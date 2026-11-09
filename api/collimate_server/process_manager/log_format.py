"""Canonical run-log line formatter.

The ONE format shared by the live drawer (frontend/src/lib/runLogFormat.ts)
and the persisted run log (finalization → run_artifacts 'logs.txt'). The two MUST stay
byte-identical — a reconnecting client rebuilds the drawer purely by replaying
run_events through this format, and the saved file is the same rows formatted
the same way, so live == saved. Both suites read the same golden vectors,
``api/tests/fixtures/run-log-vectors.json``.

Each run_events row → one line::

    [HH:MM:SS] TAG    text        (LOCAL wall-clock)

``TAG`` is left-justified in a fixed width. The set of event types that render
is an explicit ALLOW-LIST: the if-chain in ``_summarize``. Any type it does
not handle (stream_event, chat_*, port_bound, dag_commit, …
internal plumbing) returns ``None`` and is dropped on BOTH sides.

``gen.log()`` is NOT a distinct event type — it is ``[LEVEL]``-prefixed
**stderr** (the SDK wires the generator logger with
``logging.Formatter("[%(levelname)s] %(message)s")``). So stderr rows whose
payload opens with a known level frame are re-tagged (INFO/DEBUG→LOG,
WARNING→WARN, ERROR/CRITICAL→ERR) and the frame is stripped. Bare stderr
(subprocess noise) is tagged ``STDERR``, deliberately NOT ``ERR``: half the
tools a generator shells out to (pip, npm, git, curl) narrate normal progress
on stderr, and painting every installer line as an error made real failures
invisible. ``ERR`` is reserved for lines a logger explicitly leveled ERROR.

``run_started`` / ``run_finished`` are host-fabricated bookends (tag ``RUN``),
not real run_events rows: the live client seeds/appends them around the feed
and finalization bakes the same lines into ``logs.txt`` — they carry the
runner (and final status) the bar itself no longer shows.
"""
import json
from datetime import datetime, timezone

TAG_WIDTH = 6

# Python logging levelname frames emitted on stderr by gen.log()/the logger.
LEVEL_TAGS = {
    "INFO": "LOG",
    "DEBUG": "LOG",
    "WARNING": "WARN",
    "ERROR": "ERR",
    "CRITICAL": "ERR",
}


def _hhmmss(created_at) -> str:
    """``HH:MM:SS`` in LOCAL time from a SQLite ``CURRENT_TIMESTAMP`` string
    (``YYYY-MM-DD HH:MM:SS``, UTC, no offset) or an ISO string. The
    offset-less string is explicitly pinned to UTC before conversion; the JS
    mirror does the same (``+ 'Z'`` then local getters). The server runs on
    the user's machine, so server-local == the wall clock the user reads.
    Unparseable → raw slice."""
    s = str(created_at or "")
    if len(s) >= 19 and s[10] in (" ", "T"):
        try:
            dt = datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).astimezone().strftime("%H:%M:%S")
        except ValueError:
            return s[11:19]
    return ""


def _as_dict(payload) -> dict:
    try:
        v = json.loads(payload)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _strip_level(text: str):
    """If ``text`` opens with a ``[LEVEL] `` frame, return ``(tag, stripped)``;
    else ``(None, text)``."""
    if text.startswith("["):
        close = text.find("] ")
        if close != -1:
            tag = LEVEL_TAGS.get(text[1:close])
            if tag:
                return tag, text[close + 2:]
    return None, text


def _summarize(event_type: str, payload):
    """Return ``(tag, text)`` for a rendered event, or ``(None, "")`` to drop."""
    if event_type == "stdout":
        return "OUT", str(payload).rstrip("\n")
    if event_type == "stderr":
        # Bare stderr = subprocess narration, not an error (see header).
        tag, body = _strip_level(str(payload).rstrip("\n"))
        return (tag or "STDERR"), body
    if event_type == "log":
        # Host-side notices (OwnCellWritesMixin.push_log); the generator's own gen.log
        # rides [INFO]-stderr and is re-tagged LOG above.
        return "LOG", str(payload).rstrip("\n")
    if event_type in ("ipc_error", "timeout"):
        return ("IPCERR" if event_type == "ipc_error" else "TIMOUT"), str(payload).rstrip("\n")

    d = _as_dict(payload)
    if event_type == "phase":
        p = str(d.get("phase", "") or "")
        m = str(d.get("message", "") or "")
        return "PHASE", (f"{p} — {m}" if m else p)
    if event_type == "progress":
        cur = d.get("current")
        tot = d.get("total")
        return "PROG", f"{'?' if cur is None else cur}/{'?' if tot is None else tot}"
    if event_type == "finish":
        err = str(d.get("error_message", "") or "")
        base = "ok" if d.get("success", True) else "failed"
        return "DONE", (f"{base}: {err}" if err else base)
    if event_type == "stop":
        return "STOP", str(d.get("stop_type", "stopped") or "stopped")
    if event_type == "commit":
        name = str(d.get("name", "") or "")
        return "COMMIT", name
    if event_type == "egress_denied":
        host = str(d.get("host", "") or "")
        port = d.get("port")
        port = "" if port is None else port
        method = str(d.get("method", "") or "")
        return "EGRESS", f"blocked: {method} {host}:{port}".rstrip(": ")
    if event_type == "run_started":
        parts = ["started", str(d.get("runner", "") or "")]
        return "RUN", " · ".join(p for p in parts if p)
    if event_type == "run_finished":
        parts = [
            "finished",
            str(d.get("status", "") or ""),
            str(d.get("runner", "") or ""),
        ]
        return "RUN", " · ".join(p for p in parts if p)
    return None, ""


def format_log_line(event_type: str, payload, created_at=None):
    """Format one run_events row into the canonical line, or ``None`` if the
    event type is not part of the user-facing log (allow-list miss)."""
    tag, text = _summarize(event_type, payload if payload is not None else "")
    if tag is None:
        return None
    ts = _hhmmss(created_at)
    prefix = f"[{ts}] " if ts else ""
    return f"{prefix}{tag:<{TAG_WIDTH}} {text}"
