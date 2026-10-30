"""The top bar's running count (C04, D-C04).

``GET /api/runs/active`` lists every channel's live runs as
``[{channel, channel_name, runs}]``, each ``runs`` the channel's own
``/runs/active`` rows built from the same registry. Every client hears
``runs_active_changed {channel}`` on whichever channel stream it follows when
a run starts or reaches a terminal status, so the count is refetched on the
event and never polled. Manager stand-ins only: nothing spawns.
"""
from __future__ import annotations

import uuid

import pytest

from conftest import install_runner
from database import get_db_connection
from fake_manager import FakeManager


def _canvas_cell(client, channel: str, name: str):
    """A fresh stack's cell on ``channel``, renamed ``name``."""
    from state import get_channel_by_id
    stack_id = client.post(f"/api/stacks/{channel}/create").json()["main_stacks"][-1]["id"]
    owner = get_channel_by_id(channel)
    stack = next(s for s in owner.main_stacks if str(s.id) == stack_id)
    stack.cells[0].name = name
    return stack, stack.cells[0]


def _seed_run_row(channel: str) -> str:
    conn = get_db_connection(channel)
    try:
        rid = str(uuid.uuid4())
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'seed', 'running')", (rid,))
        conn.commit()
        return rid
    finally:
        conn.close()


@pytest.fixture
def two_channels(client):
    """Two UUID channels, each with one live run on a named cell."""
    from state import find_active_channel
    from run_registry import active_managers
    starting = str(find_active_channel().id)
    response = client.post("/api/channels", params={"name": "Second"})
    assert response.status_code == 200, response.text
    second = response.json()['id']
    runs = {}
    for channel, cell_name, runner in ((starting, "Op log", "Web"), (second, "Tutorial", "Terminal")):
        _stack, cell = _canvas_cell(client, channel, cell_name)
        rid = _seed_run_row(channel)
        active_managers[rid] = FakeManager(
            channel=channel, run_id=rid, cell_id=str(cell.id),
            runner_id=runner.lower(), runner_name=runner,
            label=f"{runner.lower()}-{rid[:8]}", folder=f"_runs/{runner.lower()}-{rid[:8]}")
        runs[channel] = (rid, str(cell.id), cell_name, runner)
    try:
        yield runs
    finally:
        for rid, *_rest in runs.values():
            active_managers.pop(rid, None)


def test_global_active_runs_lists_every_channel_in_the_per_channel_shape_C04(client, two_channels):
    r = client.get("/api/runs/active")
    assert r.status_code == 200, r.text
    body = {entry["channel"]: entry for entry in r.json()}
    assert set(body) == set(two_channels)
    assert {entry['channel_name'] for entry in body.values()} == {'Starting', 'Second'}
    for channel, (rid, cell_id, cell_name, runner) in two_channels.items():
        [row] = body[channel]["runs"]
        assert row["run_id"] == rid and row["cell_id"] == cell_id
        assert row["cell_name"] == cell_name and row["runner_name"] == runner
        assert row["stopping"] is False
        # The start comes from the runs row, as UTC ISO 8601.
        assert row["started_at"].endswith("Z") and "T" in row["started_at"]
        # The same rows the channel's own route answers.
        assert client.get(f"/api/channels/{channel}/runs/active").json() == body[channel]["runs"]


def test_global_active_runs_is_empty_when_nothing_runs_C04(client):
    r = client.get("/api/runs/active")
    assert r.status_code == 200 and r.json() == []


def test_global_active_runs_omits_unregistered_uuid_without_opening_its_database(client):
    from pathlib import Path
    from run_registry import active_managers
    identity, run_id = str(uuid.uuid4()), str(uuid.uuid4())
    active_managers[run_id] = FakeManager(channel=identity, run_id=run_id)
    try:
        response = client.get('/api/runs/active')
        assert response.status_code == 200 and response.json() == []
        assert not Path(f'.collimate/channels/{identity}.db').exists()
    finally:
        active_managers.pop(run_id, None)


@pytest.fixture
def heard(monkeypatch):
    """Every system-wide broadcast, and a manager stand-in for starts."""
    import events
    import routes.runs.create as create
    all_events: list = []

    class _Manager(FakeManager):
        async def start(self):
            pass

    async def _no_reap(*_a):
        pass

    monkeypatch.setattr(create, "ProcessManager", _Manager)
    monkeypatch.setattr(create, "_reap_process", _no_reap)
    monkeypatch.setattr(events, "broadcast", lambda _ch, _ev: None)
    monkeypatch.setattr(events, "broadcast_all", all_events.append)
    return all_events


def test_runs_active_changed_is_heard_everywhere_on_a_start_C04(client, heard):
    from state import find_active_channel
    from run_registry import active_managers
    channel = str(find_active_channel().id)
    runner, _gen = install_runner(generator="count_probe", runner="count-probe")
    _stack, cell = _canvas_cell(client, channel, "Probe")
    r = client.post(f"/api/channels/{channel}/runs", json={"runner": runner, "cell_id": str(cell.id)})
    assert r.status_code == 200, r.text
    active_managers.pop(r.json()["run_id"], None)
    assert heard == [{"type": "runs_active_changed", "channel": channel}]


@pytest.mark.parametrize("status", ["completed", "failed", "stopped"])
def test_runs_active_changed_is_heard_everywhere_on_a_terminal_status_C04(client, heard, status):
    from state import find_active_channel
    from process_manager import ProcessManager
    channel = str(find_active_channel().id)
    ProcessManager._broadcast_run_status(FakeManager(channel=channel, run_id="r1"), status)
    assert heard == [{"type": "runs_active_changed", "channel": channel}]
