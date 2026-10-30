"""How a run ended: ``runs.ended_at``, ``note`` and ``outcome`` on every
terminal path (O03), and the generator's own verdict from its finish (O08).

Synthetic rows only: each test seeds a run row and drives the terminal
writer it names (the finish op, the process-exit finalize, the stop route's
helpers, Stop all's pass, crash recovery).
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

import pytest

from database import get_db_connection

WIRE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


def _seed_run(channel_id: str, status: str = "running", **cols) -> str:
    conn = get_db_connection(channel_id)
    try:
        rid = str(uuid.uuid4())
        keys = ["id", "prompt", "status", "kind"] + list(cols)
        vals = [rid, "seed", status, "generator"] + list(cols.values())
        conn.execute(
            f"INSERT INTO runs ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})", vals)
        conn.commit()
        return rid
    finally:
        conn.close()


def _seed_event(channel_id: str, rid: str, event_type: str, payload: str) -> None:
    conn = get_db_connection(channel_id)
    try:
        conn.execute("INSERT INTO run_events (run_id, event_type, payload) VALUES (?, ?, ?)",
                     (rid, event_type, payload))
        conn.commit()
    finally:
        conn.close()


def _row(channel_id: str, rid: str) -> dict:
    conn = get_db_connection(channel_id)
    try:
        return dict(conn.execute(
            "SELECT status, failure_reason, ended_at, note, outcome FROM runs WHERE id=?",
            (rid,)).fetchone())
    finally:
        conn.close()


def _finish(channel_id, rid, *, stop_requested=False, timeout_reason=None, **data):
    from process_manager.ipc import IPCMixin

    class _F(IPCMixin):
        pass

    f = _F()
    f.channel = channel_id
    f.run_id = rid
    f.log_buffer = []
    f.stop_requested = stop_requested
    f.timeout_reason = timeout_reason
    f.broadcasts = []
    f._broadcast_run_status = lambda st, reason=None: f.broadcasts.append((st, reason))
    asyncio.run(f._op_finish({"op": "finish", **data}))
    return f


class _Exited:
    """The process-exit finalize, composed onto the minimum a manager holds."""

    def __init__(self, channel_id, rid, returncode=0, timeout_reason=None):
        self.channel = channel_id
        self.run_id = rid
        self.process = type("P", (), {"returncode": returncode})()
        self.cell_id = None
        self.timeout_reason = timeout_reason
        self.log_buffer = []
        self.broadcasts = []

    def _broadcast_run_status(self, status, failure_reason=None):
        self.broadcasts.append((status, failure_reason))

    def _apply_phase(self, _p, _m=""):
        pass

    def _flush_log_buffer(self):
        pass


def _exit(channel_id, rid, **kw):
    from process_manager.finalization import FinalizationMixin

    class _D(_Exited, FinalizationMixin):
        pass

    _D(channel_id, rid, **kw)._finalize_run_status()


# ---------------------------------------------------------------------------
# O08: the generator's verdict
# ---------------------------------------------------------------------------

def test_a_generator_finish_lands_its_note_and_outcome_O08(client):
    rid = _seed_run(client.channel_id)
    f = _finish(client.channel_id, rid, success=True, error_message="",
                note="3 of 4 checks\npassed", outcome="fail")
    row = _row(client.channel_id, rid)
    assert (row["status"], row["failure_reason"]) == ("completed", None)
    assert (row["note"], row["outcome"]) == ("3 of 4 checks passed", "fail")
    assert WIRE_TIME.match(row["ended_at"])
    assert f.broadcasts == [("completed", None)]


def test_the_generators_end_survives_its_process_exit_O08(client):
    rid = _seed_run(client.channel_id)
    _finish(client.channel_id, rid, success=True, note="probe answered", outcome="pass")
    ended = _row(client.channel_id, rid)
    _exit(client.channel_id, rid, returncode=0)
    assert _row(client.channel_id, rid) == ended


def test_a_generator_note_is_one_line_and_capped_O08(client):
    rid = _seed_run(client.channel_id)
    _finish(client.channel_id, rid, success=False, error_message="boom", note="word " * 200, outcome="fail")
    row = _row(client.channel_id, rid)
    assert (row["status"], row["failure_reason"], row["outcome"]) == ("failed", "boom", "fail")
    assert len(row["note"]) == 300 and row["note"].endswith("…") and "\n" not in row["note"]


@pytest.mark.parametrize("outcome", ["maybe", ["pass"], {}])
def test_an_unknown_outcome_is_dropped_and_logged_O08(client, outcome):
    rid = _seed_run(client.channel_id)
    f = _finish(client.channel_id, rid, success=False, error_message="boom", note="done", outcome=outcome)
    row = _row(client.channel_id, rid)
    assert (row["status"], row["failure_reason"], row["note"], row["outcome"]) == (
        "failed", "boom", "done", None)
    assert any(e["type"] == "ipc_error" and repr(outcome) in e["payload"] for e in f.log_buffer)


def test_a_stopping_runs_finish_leaves_the_end_to_its_stopper_O08(client):
    from routes.runs.teardown import mark_run_stopped
    rid = _seed_run(client.channel_id)
    assert mark_run_stopped(client.channel_id, rid) == "stopped"
    _finish(client.channel_id, rid, stop_requested=True, success=False, error_message="Stopped by user",
            note="half done", outcome="pass")
    row = _row(client.channel_id, rid)
    assert (row["status"], row["note"], row["outcome"]) == ("stopped", None, None)


def test_get_run_returns_how_the_run_ended_O08(client):
    rid = _seed_run(client.channel_id)
    _finish(client.channel_id, rid, success=True, note="all green", outcome="pass")
    run = client.get(f"/api/channels/{client.channel_id}/runs/{rid}").json()["run"]
    assert (run["note"], run["outcome"]) == ("all green", "pass")
    assert WIRE_TIME.match(run["ended_at"])
    live = _seed_run(client.channel_id)
    assert client.get(f"/api/channels/{client.channel_id}/runs/{live}").json()["run"]["ended_at"] is None


# ---------------------------------------------------------------------------
# O03: ended_at on every terminal path
# ---------------------------------------------------------------------------

def test_a_failed_finish_without_a_note_takes_its_reason_O03(client):
    rid = _seed_run(client.channel_id)
    _finish(client.channel_id, rid, success=False, error_message="Traceback: boom")
    row = _row(client.channel_id, rid)
    assert (row["status"], row["note"], row["outcome"]) == ("failed", "Traceback: boom", None)
    assert WIRE_TIME.match(row["ended_at"])


@pytest.mark.parametrize("kw, status, note", [
    ({"returncode": 1}, "failed", "last words"),
    ({"returncode": 1, "timeout_reason": "Exceeded 5s timeout"}, "timed_out", "Exceeded 5s timeout"),
])
def test_process_exit_stamps_ended_at_O03(client, kw, status, note):
    rid = _seed_run(client.channel_id)
    _seed_event(client.channel_id, rid, "stderr", "noise\nlast words\n")
    _exit(client.channel_id, rid, **kw)
    row = _row(client.channel_id, rid)
    assert (row["status"], row["note"]) == (status, note)
    assert WIRE_TIME.match(row["ended_at"])


def test_a_completed_run_without_output_is_noted_O03(client):
    rid = _seed_run(client.channel_id)
    _exit(client.channel_id, rid, returncode=0)
    row = _row(client.channel_id, rid)
    assert (row["status"], row["note"]) == ("completed", "no output")
    assert WIRE_TIME.match(row["ended_at"])


def test_a_completed_run_with_output_has_no_note_O03(client):
    rid = _seed_run(client.channel_id)
    _seed_event(client.channel_id, rid, "commit", json.dumps({"cell_id": "v1", "name": "out"}))
    _exit(client.channel_id, rid, returncode=0)
    row = _row(client.channel_id, rid)
    assert (row["status"], row["note"]) == ("completed", None)
    assert WIRE_TIME.match(row["ended_at"])


def _stop_events(channel_id: str, rid: str) -> list[str]:
    conn = get_db_connection(channel_id)
    try:
        return [json.loads(r[0])["stop_type"] for r in conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? AND event_type='stop' ORDER BY id", (rid,))]
    finally:
        conn.close()


def test_a_stop_stamps_ended_at_and_notes_its_stop_type_before_the_signal_O03(client):
    """The Stop route's pre-signal write stamps ended_at and the note, so
    every run_status broadcast after the signal carries it; the stop event
    follows the log flush."""
    from process_manager.streams import run_status_event
    from routes.runs.teardown import mark_run_stopped, record_stop_event
    rid = _seed_run(client.channel_id)
    assert mark_run_stopped(client.channel_id, rid, "force_stopped", event=False) == "stopped"
    stopped = _row(client.channel_id, rid)
    assert WIRE_TIME.match(stopped["ended_at"]) and stopped["note"] == "force stopped"
    assert _stop_events(client.channel_id, rid) == []
    event = run_status_event(client.channel_id, rid, "stopped")
    assert (event["note"], event["ended_at"]) == ("force stopped", stopped["ended_at"])
    record_stop_event(client.channel_id, rid, "force_stopped")
    assert _row(client.channel_id, rid) == stopped and _stop_events(client.channel_id, rid) == ["force_stopped"]


@pytest.mark.parametrize("force", [False, True])
def test_the_stop_routes_broadcast_carries_the_stop_note_O03(client, monkeypatch, force):
    """A Stop of a live run notes the row before it signals, so the
    run_status the route (Force) or the run's finalize (graceful) sends
    carries the note, and the stop event lands after the log flush."""
    import routes.runs.control as control
    from fake_manager import FakeManager
    from process_manager.streams import run_status_event
    from run_registry import active_managers

    rid = _seed_run(client.channel_id)
    sent: list[dict] = []
    signals: list[int] = []

    class _Live:
        pid = 2**30
        returncode = None

        async def wait(self):
            return 0

    def signal_group(pid, sig, **_):
        signals.append(sig)
        # The run's finalize broadcasts once the signal lands.
        sent.append(run_status_event(client.channel_id, rid, "stopped"))
        return True

    monkeypatch.setattr(control, "signal_process_group", signal_group)
    manager = FakeManager(channel=client.channel_id, run_id=rid, process=_Live(),
                          _flush_log_buffer=lambda: None,
                          _broadcast_run_status=lambda status: sent.append(run_status_event(client.channel_id, rid, status)))
    active_managers[rid] = manager
    try:
        response = client.delete(f"/api/channels/{client.channel_id}/runs/{rid}" + ("?force=true" if force else ""))
    finally:
        active_managers.pop(rid, None)
    assert response.status_code == 200, response.text
    note = "force stopped" if force else "gracefully stopped"
    assert signals and sent and all(e["note"] == note and e["status"] == "stopped" for e in sent)
    assert all(WIRE_TIME.match(e["ended_at"]) for e in sent)
    assert _stop_events(client.channel_id, rid) == [note.replace(" ", "_")]


def test_a_force_stop_after_a_graceful_one_is_noted_forced_O03(client):
    from routes.runs.teardown import mark_run_stopped, record_stop_event
    rid = _seed_run(client.channel_id)
    assert mark_run_stopped(client.channel_id, rid, "gracefully_stopped", event=False) == "stopped"
    record_stop_event(client.channel_id, rid, "gracefully_stopped")
    graceful = _row(client.channel_id, rid)
    assert graceful["note"] == "gracefully stopped"
    assert mark_run_stopped(client.channel_id, rid, "force_stopped", event=False) == "stopped"
    record_stop_event(client.channel_id, rid, "force_stopped")
    row = _row(client.channel_id, rid)
    assert (row["status"], row["note"], row["ended_at"]) == (
        "stopped", "force stopped", graceful["ended_at"])
    assert _stop_events(client.channel_id, rid) == ["gracefully_stopped", "force_stopped"]


def test_a_stop_after_the_run_ended_keeps_its_end_O03(client):
    from routes.runs.teardown import mark_run_stopped
    rid = _seed_run(client.channel_id)
    _finish(client.channel_id, rid, success=True, note="done", outcome="pass")
    ended = _row(client.channel_id, rid)
    assert mark_run_stopped(client.channel_id, rid, "gracefully_stopped") == "completed"
    assert _row(client.channel_id, rid) == ended


def test_an_aborted_spawn_is_stopped_with_its_note_O03(client):
    from routes.runs.teardown import mark_run_stopped
    rid = _seed_run(client.channel_id)
    assert mark_run_stopped(client.channel_id, rid, "aborted_spawn") == "stopped"
    row = _row(client.channel_id, rid)
    assert row["note"] == "aborted spawn" and WIRE_TIME.match(row["ended_at"])


def test_stop_all_stamps_every_run_it_reaches_O03(client):
    from routes.runs.teardown import _mark_runs_stopped
    a, b = _seed_run(client.channel_id), _seed_run(client.channel_id)
    statuses = asyncio.run(_mark_runs_stopped({client.channel_id: [a, b]}, "stopped_all"))
    assert statuses == {a: "stopped", b: "stopped"}
    for rid in (a, b):
        row = _row(client.channel_id, rid)
        assert row["note"] == "stopped all" and WIRE_TIME.match(row["ended_at"])


@pytest.mark.parametrize("new_cell", [False, True])
@pytest.mark.parametrize("error, status", [
    (RuntimeError("synthetic start failure"), "failed"),
    ("start_cancelled", "stopped"),
])
def test_a_refused_start_stamps_ended_at_O03(client, monkeypatch, new_cell, error, status):
    """A start that fails after it was accepted (its spawn) ends the row
    through the unwind: the status, ended_at, the failure as its note, and
    no 'run_cell' move left behind."""
    import routes.runs.create as create
    from conftest import install_runner
    from run_registry import StartCancelled
    from run_spawn import wait_spawned
    from test_pytest_run_in_new_cell import _source_cell

    runner, _gen = install_runner(generator="refused_probe", runner="refused-probe")
    _ch, _stack, source, _png = _source_cell(client)
    exc = StartCancelled("the starting run was cancelled") if error == "start_cancelled" else error
    seen: dict = {}

    async def _refused(self):
        seen["run_id"] = self.run_id
        raise exc

    monkeypatch.setattr(create._RunStart, "_prepare_and_start", _refused)
    body = {"runner": runner, "cell_id": str(source.id), "new_cell": new_cell}
    response = client.post(f"/api/channels/{client.channel_id}/runs", json=body)
    assert response.status_code == 200, response.text
    wait_spawned(response.json()["run_id"])
    row = _row(client.channel_id, seen["run_id"])
    assert row["status"] == status and row["outcome"] is None
    assert WIRE_TIME.match(row["ended_at"])
    # A failed start keeps why as its note; a stopped one has no stop event here.
    assert row["note"] == ("synthetic start failure" if status == "failed" else None)
    conn = get_db_connection(client.channel_id)
    try:
        moves = conn.execute("SELECT 1 FROM cell_moves WHERE op='run_cell' AND op_id=?",
                             (seen["run_id"],)).fetchall()
    finally:
        conn.close()
    assert moves == []


def test_crash_recovery_stamps_ended_at_O03(api_app):
    from fastapi.testclient import TestClient

    with TestClient(api_app) as c:
        created = c.post("/api/channels", params={"name": "Starting"})
        assert created.status_code == 200
        channel_id = created.json()["id"]
        rid = _seed_run(channel_id, phase="running")
        done = _seed_run(channel_id, "completed")
    with TestClient(api_app):
        pass
    conn = get_db_connection(channel_id)
    try:
        phase = conn.execute("SELECT phase FROM runs WHERE id=?", (rid,)).fetchone()[0]
    finally:
        conn.close()
    row = _row(channel_id, rid)
    assert (row["status"], phase) == ("interrupted", "interrupted")
    assert row["note"] == "Server restarted while the run was active"
    assert WIRE_TIME.match(row["ended_at"])
    assert _row(channel_id, done)["ended_at"] is None
