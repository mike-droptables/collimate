"""O03: mark_run_ended, the one terminal write of a run row."""
import json

import pytest


@pytest.fixture
def channel(fresh_cwd, monkeypatch):
    from models import Channel
    from state import state
    channel = Channel(name="Run endings")
    monkeypatch.setattr(state, "channels", [channel])
    return str(channel.id)


@pytest.fixture
def conn(channel):
    import database
    c = database.get_db_connection(channel)
    yield c
    c.close()


def _run(conn, run_id="r1", status="running"):
    conn.execute("INSERT INTO runs (id, status, kind) VALUES (?, ?, 'generator')", (run_id, status))


def _row(conn, run_id="r1"):
    return dict(conn.execute(
        "SELECT status, failure_reason, ended_at, note, outcome FROM runs WHERE id=?", (run_id,)).fetchone())


def test_ending_a_running_row_stamps_ended_at_note_and_outcome_O03(conn):
    from run_ends import mark_run_ended
    _run(conn)
    assert mark_run_ended(conn, "r1", status="completed", note="  3 of 4\nchecks passed ", outcome="pass") is True
    row = _row(conn)
    assert row["status"] == "completed" and row["failure_reason"] is None
    assert row["note"] == "3 of 4 checks passed" and row["outcome"] == "pass"
    assert row["ended_at"].endswith("+00:00") and "T" in row["ended_at"]


def test_an_ended_row_keeps_its_status_and_end_O03(conn):
    from run_ends import mark_run_ended
    _run(conn)
    mark_run_ended(conn, "r1", status="stopped")
    first = _row(conn)
    assert mark_run_ended(conn, "r1", status="completed", outcome="fail") is False
    after = _row(conn)
    assert (after["status"], after["ended_at"], after["outcome"]) == ("stopped", first["ended_at"], None)


def test_note_falls_back_to_failure_reason_O03(conn):
    from run_ends import mark_run_ended
    _run(conn)
    mark_run_ended(conn, "r1", status="failed", failure_reason="Traceback: boom")
    assert _row(conn)["note"] == "Traceback: boom"


def test_note_falls_back_to_the_stop_type_written_after_the_status_O03(conn):
    from run_ends import mark_run_ended
    _run(conn)
    assert mark_run_ended(conn, "r1", status="stopped") is True
    assert _row(conn)["note"] is None
    conn.execute("INSERT INTO run_events (run_id, event_type, payload) VALUES ('r1', 'stop', ?)",
                 (json.dumps({"stop_type": "force_stopped"}),))
    mark_run_ended(conn, "r1", status="stopped")
    assert _row(conn)["note"] == "force stopped"


def test_note_falls_back_to_no_output_O03(conn):
    from run_ends import mark_run_ended
    _run(conn)
    conn.execute("INSERT INTO run_events (run_id, event_type, payload) VALUES ('r1', 'no_output', '{}')")
    mark_run_ended(conn, "r1", status="completed")
    assert _row(conn)["note"] == "no output"


def test_an_explicit_note_wins_and_is_capped_O03(conn):
    from collimate._host.ipc import FINISH_NOTE_MAX_CHARS
    from run_ends import mark_run_ended
    _run(conn)
    conn.execute("INSERT INTO run_events (run_id, event_type, payload) VALUES ('r1', 'no_output', '{}')")
    mark_run_ended(conn, "r1", status="failed", failure_reason="boom", note="x" * 500)
    note = _row(conn)["note"]
    assert len(note) == FINISH_NOTE_MAX_CHARS and note.endswith("…")
    # A later call never overwrites a note.
    mark_run_ended(conn, "r1", status="failed", note="other")
    assert _row(conn)["note"] == note


def test_outcome_outside_pass_fail_is_refused_O03(conn):
    from run_ends import mark_run_ended
    _run(conn)
    with pytest.raises(ValueError):
        mark_run_ended(conn, "r1", status="completed", outcome="maybe")
    with pytest.raises(ValueError):
        mark_run_ended(conn, "r1", status="completed", outcome=["pass"])  # type: ignore[arg-type]
    assert _row(conn)["status"] == "running"


def test_mark_run_ended_does_not_commit_O03(conn, channel):
    import database
    from run_ends import mark_run_ended
    _run(conn)
    conn.commit()
    mark_run_ended(conn, "r1", status="completed")
    other = database.get_db_connection(channel)
    try:
        assert other.execute("SELECT status FROM runs WHERE id='r1'").fetchone()[0] == "running"
    finally:
        other.close()
    conn.rollback()
