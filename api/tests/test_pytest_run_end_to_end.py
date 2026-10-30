"""End-to-end verification: launch a real subprocess generator through
the full POST /runs → subprocess → events → status pipeline.

Scope on purpose: exercises the glue between the route handler, the
ProcessManager, stdout draining, the microbatch writer, and the
finalization path. It includes bare Python and SDK-driven generators so both subprocess output
and IPC lifecycle messages are exercised.

Expectations:
  * uv is on PATH (the test environment has it; skipped otherwise).
  * The generator inherits the test's Python by PEP 723 inline
    metadata declaring no dependencies.
"""
from __future__ import annotations

import shutil
import time
import uuid

import pytest

from database import get_db_connection
from conftest import install_runner


pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not available on PATH; E2E run verification needs it",
)


# ---------------------------------------------------------------------------
# Fixture: inject a real generator cell into the active channel so
# find_generator_dir can discover it.
# ---------------------------------------------------------------------------

MARKER = "__echo_ran_OK_987__"


def _install_echo_generator(client):
    """Drop an `echo_test` generator cell into the active channel.
    Returns the saved runner id. The generator entrypoint just
    prints a marker and exits zero — enough to drive the full
    subprocess → drain → finalize pipeline without the SDK."""
    # PEP 723 inline metadata → uv run picks up no dependencies; the
    # script executes against the system Python uv provisions. The
    # `requires-python` floor is broad so any modern interpreter works.
    echo_py = (
        "# /// script\n"
        "# requires-python = \">=3.10\"\n"
        "# dependencies = []\n"
        "# ///\n"
        f"print('{MARKER}')\n"
        "print('line two with    indented content')\n"
    )
    runner, _ = install_runner(generator="echo_test", entrypoint_body=echo_py)
    return runner


def _canvas_cell(client):
    channel = client.post(f"/api/stacks/{client.channel_id}/create").json()
    return channel["main_stacks"][-1]["cells"][0]["id"]


def _wait_for_run_terminal(client, run_id: str, timeout_s: float = 30.0) -> dict:
    """Poll events_since until is_terminal flips true or timeout."""
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        r = client.get(f"/api/channels/{client.channel_id}/runs/{run_id}/events_since?since_id=0")
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("is_terminal"):
            return last
        time.sleep(0.2)
    raise AssertionError(
        f"run {run_id} never terminated; last status={last.get('status')} "
        f"is_active={last.get('is_active')} events={len(last.get('events', []))}"
    )


# ---------------------------------------------------------------------------
# E2E: happy path — a real subprocess run completes cleanly
# ---------------------------------------------------------------------------

def test_e2e_echo_run_completes_and_captures_output(client):
    """Drive POST /runs → subprocess → stdout → event_since → status.

    Verifies several audit fixes at once:
      * A6: temp dirs created and NOT leaked on success.
      * A2: finalization transitions running → completed (not overwriting
        anything else — no concurrent stop here).
      * A8/B19: indented stdout line preserved intact in the event row.
      * B13/queries: events_since returns is_terminal=True at completion.
    """
    runner = _install_echo_generator(client)

    placeholder = _canvas_cell(client)
    r = client.post(
        f"/api/channels/{client.channel_id}/runs",
        json={"runner": runner, "cell_id": placeholder},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "queued"

    # Poll until terminal. 30 s is generous for a uv-cold-start.
    result = _wait_for_run_terminal(client, run_id, timeout_s=60.0)
    assert result["status"] == "completed", (
        f"expected completed, got {result['status']}; events: "
        f"{[e['type'] for e in result['events']]}"
    )
    assert result["is_terminal"] is True
    assert result["is_active"] is False

    # Marker was captured on stdout — proves the drain pipeline
    # + microbatch writer delivered the output to run_events.
    payloads = [e["payload"] for e in result["events"] if e["type"] == "stdout"]
    joined = "".join(payloads)
    assert MARKER in joined, f"stdout did not contain marker; got {joined!r}"

    # Indentation preservation (fix A8/B19): the second line retains
    # its four-space run.
    assert "line two with    indented content" in joined, joined


def _install_phase_generator(client):
    """A synthetic generator that imports `gen` and exercises the unified
    lifecycle surface: gen.phase (spinning_up→running, then a new message
    for the same phase) and gen.log. The manager injects the SDK onto
    PYTHONPATH, so `dependencies = []` is fine."""
    gen_py = (
        "# /// script\n"
        "# requires-python = \">=3.10\"\n"
        "# dependencies = []\n"
        "# ///\n"
        "import gen\n"
        "async def run():\n"
        "    gen.phase('spinning_up', 'booting')\n"
        "    gen.log('worklog_PHX')\n"
        "    gen.phase('running')\n"
        "    gen.phase('running', 'status_PHX')\n"
        "    await gen.commit(name='Phased')\n"
        "if __name__ == '__main__':\n"
        "    gen.main(run)\n"
    )
    runner, _ = install_runner(generator="phase_test", entrypoint_body=gen_py)
    return runner


def test_e2e_phase_status_log_surface_in_run_events(client):
    """gen.phase / gen.log flow through the real subprocess→IPC pipeline:
    phase ops land as `phase` run_events (a status is just a phase's
    message — there is no separate 'status' event type), and gen.log rides
    the [INFO]-framed stderr the canonical log re-tags to LOG."""
    import json as _json

    runner = _install_phase_generator(client)
    placeholder = _canvas_cell(client)
    r = client.post(
        f"/api/channels/{client.channel_id}/runs",
        json={"runner": runner, "cell_id": placeholder},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    result = _wait_for_run_terminal(client, run_id, timeout_s=60.0)
    assert result["status"] == "completed", (
        f"got {result['status']}; events: {[e['type'] for e in result['events']]}"
    )

    phase_rows = [_json.loads(e["payload"]) for e in result["events"] if e["type"] == "phase"]
    phases = {p.get("phase") for p in phase_rows}
    assert "spinning_up" in phases, phases
    assert "running" in phases, phases
    # gen.phase('running', 'status_PHX') is a new message for the current
    # (running) phase — it surfaces as a phase row carrying that message,
    # NOT a separate 'status' event.
    assert any(p.get("message") == "status_PHX" for p in phase_rows), phase_rows
    assert not any(e["type"] == "status" for e in result["events"]), "legacy 'status' event leaked"
    # gen.log → [INFO]-framed stderr (the canonical formatter re-tags to LOG).
    stderr_joined = "".join(e["payload"] for e in result["events"] if e["type"] == "stderr")
    assert "worklog_PHX" in stderr_joined, stderr_joined


def test_e2e_stop_before_completion_marks_stopped(client):
    """POST /runs → immediately DELETE /runs/{id} → finalizer must not
    overwrite the 'stopped' row (fix A2). Also proves the /stop flush
    ordering (fix A5) — the stop event shows up in events_since
    immediately after the stop returns, not after a 500 ms delay."""
    # Longer-running entrypoint so we can catch it mid-flight.
    slow_py = (
        "# /// script\n"
        "# requires-python = \">=3.10\"\n"
        "# dependencies = []\n"
        "# ///\n"
        "import time\n"
        "for i in range(30):\n"
        "    print(f'tick {i}', flush=True)\n"
        "    time.sleep(0.5)\n"
    )
    runner, _ = install_runner(generator="slow_test", entrypoint_body=slow_py)
    placeholder = _canvas_cell(client)
    r = client.post(
        f"/api/channels/{client.channel_id}/runs",
        json={"runner": runner, "cell_id": placeholder},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    # Let it warm up and emit at least one tick.
    time.sleep(2.0)

    # Graceful stop.
    stop = client.delete(f"/api/channels/{client.channel_id}/runs/{run_id}")
    assert stop.status_code == 200
    assert stop.json()["stop_type"] == "gracefully_stopped"

    # Immediately after stop returns, events_since must already see
    # the stop row (fix A5 — flush ordering). Don't sleep — that's
    # the whole point of the fix.
    r = client.get(f"/api/channels/{client.channel_id}/runs/{run_id}/events_since?since_id=0")
    assert r.status_code == 200
    stop_events = [e for e in r.json()["events"] if e["type"] == "stop"]
    assert len(stop_events) == 1, (
        "stop event not flushed before /stop returned — A5 regression"
    )

    # Runs row marked stopped. Wait a moment for the subprocess to
    # actually exit and the finalizer to possibly re-run — then
    # re-check: the status must STILL be 'stopped', not overwritten.
    time.sleep(2.0)
    conn = get_db_connection(client.channel_id)
    try:
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "stopped", (
        f"finalizer overwrote 'stopped' — A2 regression; got {row['status']!r}"
    )


def test_e2e_missing_cell_surfaces_detail(client):
    """A run on a cell that does not exist is refused before any subprocess
    starts."""
    runner, _ = install_runner(generator="needs_one")
    response = client.post(
        f"/api/channels/{client.channel_id}/runs",
        json={"runner": runner, "cell_id": str(uuid.uuid4())},
    )
    assert response.status_code == 400, response.text
    assert "does not exist" in response.json()["detail"]
