"""Waiting for a run's background spawn in tests.

``POST /runs`` answers once a run is accepted; its spawn (workspace, config
capture, leases, the last gate, the process) continues as a task on the
server's loop. A test that reads what the spawn made waits for it here. The
TestClient must be entered (``with TestClient(app)``) so its loop outlives
the request that scheduled the spawn.
"""
from __future__ import annotations

import time


def wait_spawned(run_id: str, timeout: float = 30.0) -> None:
    """Block until ``run_id``'s spawn has registered the run or unwound."""
    from routes.runs import create
    deadline = time.monotonic() + timeout
    while run_id in create._spawns:
        if time.monotonic() > deadline:
            raise AssertionError(f"the spawn of run {run_id} did not settle in {timeout}s")
        time.sleep(0.01)


def post_run(client, body: dict, *, channel: str | None = None):
    """POST a run and, once accepted, wait for its spawn. Returns the
    response."""
    response = client.post(f"/api/channels/{channel or client.channel_id}/runs", json=body)
    if response.status_code == 200:
        wait_spawned(response.json()["run_id"])
    return response


def run_row(channel: str, run_id: str) -> dict:
    """The run's row (status, failure_reason, runner_snapshot, ...)."""
    from database import get_db_connection
    conn = get_db_connection(channel)
    try:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()
