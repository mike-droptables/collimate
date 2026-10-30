"""How a run ended: ``runs.ended_at``, ``note`` and ``outcome``.

:func:`mark_run_ended` is the one terminal write of a run row. It is a pure
SQL helper: no commit, no broadcast, no log line; the caller owns those.
"""
from __future__ import annotations

import json
import sqlite3

from collimate._host.ipc import FINISH_OUTCOMES, finish_note
from time_wire import now_iso


def _fallback_note(conn: sqlite3.Connection, run_id: str, failure_reason: str | None) -> str | None:
    """failure_reason, else the stop event's stop_type, else 'no output'."""
    if note := finish_note(failure_reason):
        return note
    row = conn.execute(
        "SELECT payload FROM run_events WHERE run_id=? AND event_type='stop' ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is not None:
        try:
            stop_type = json.loads(row[0]).get("stop_type")
        except (TypeError, ValueError, AttributeError):
            stop_type = None
        if isinstance(stop_type, str) and stop_type.strip():
            return finish_note(stop_type.replace("_", " "))
    if conn.execute(
        "SELECT 1 FROM run_events WHERE run_id=? AND event_type='no_output' LIMIT 1", (run_id,),
    ).fetchone():
        return "no output"
    return None


def mark_run_ended(conn: sqlite3.Connection, run_id: str, *, status: str,
                   failure_reason: str | None = None, note: str | None = None,
                   outcome: str | None = None) -> bool:
    """End ``run_id`` with ``status``; returns True when this call ended it.

    A row still 'running' gets ``status``, ``failure_reason``, ``ended_at``
    (now, UTC ISO with offset), ``outcome`` and ``note`` in one guarded
    UPDATE. ``note`` is one line capped like a generator's ``finish`` note
    (``collimate._host.ipc.finish_note``, the SDK's wire contract); without one it
    falls back to ``failure_reason``, then the run's ``stop`` event's
    ``stop_type`` ('force stopped', ...), then 'no output' when the run has
    a ``no_output`` event.

    A row that already ended keeps its status, failure_reason, ended_at and
    outcome; only a NULL note is filled (from ``note`` or the fallback). So
    a caller that writes a ``stop`` or ``no_output`` event after the status
    calls again to land the note.

    Raises ValueError for an ``outcome`` other than 'pass' / 'fail' (callers
    taking it from a generator check it first). Does not commit.
    """
    if outcome is not None and (not isinstance(outcome, str) or outcome not in FINISH_OUTCOMES):
        raise ValueError(f"run outcome must be 'pass' or 'fail', not {outcome!r}")
    run_id = str(run_id)
    explicit = finish_note(note)
    ended = conn.execute(
        "UPDATE runs SET status=?, failure_reason=?, ended_at=?, outcome=?, note=? "
        "WHERE id=? AND status='running'",
        (status, failure_reason, now_iso(), outcome,
         explicit or _fallback_note(conn, run_id, failure_reason), run_id),
    ).rowcount == 1
    if not ended:
        row = conn.execute("SELECT note, failure_reason FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is not None and row[0] is None:
            filled = explicit or _fallback_note(conn, run_id, row[1])
            if filled:
                conn.execute("UPDATE runs SET note=? WHERE id=? AND note IS NULL", (filled, run_id))
    return ended
