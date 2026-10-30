"""Refused starts must leave no trace behind.

Every 4xx gate in create.py completes before any state mutation: after a
rejection the channel has no new stack/cell (no ``new_cell`` clone) and the
runs table has no new row. Once the start is accepted, a failure in its
background spawn (the workspace, the last gate, the process) is a failed
run, not a 4xx: the unwind sweeps the run card, releases the starting
claims and removes the clone stack.

All generators/runners are synthetic (conftest install_runner).
"""
from __future__ import annotations

import uuid

import pytest

from conftest import install_runner
from database import get_db_connection
from fake_manager import FakeManager
from run_spawn import run_row, wait_spawned


def _channel_snapshot() -> tuple[int, int, set[str]]:
    """(#main_stacks, #nav_stacks, all cell ids) of the active channel."""
    from state import state as app_state

    ch = next(c for c in app_state.channels if c.id == app_state.active_channel_id)
    cell_ids = {
        str(c.id)
        for s in ch.main_stacks + ch.nav_stacks
        for c in s.cells
    }
    return len(ch.main_stacks), len(ch.nav_stacks), cell_ids


def _run_row_count(client) -> int:
    conn = get_db_connection(client.channel_id)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
    finally:
        conn.close()


def _canvas_cell(client) -> str:
    """Create a real cell on the canvas and return its id."""
    ch = client.post(f"/api/stacks/{client.channel_id}/create").json()
    return ch["main_stacks"][-1]["cells"][0]["id"]


def _assert_rejected_without_trace(
    client, runner: str, *, expect_status: int = 400, **req_extra
) -> "object":
    """POST a run that must reject with ``expect_status``, then assert channel
    state and the runs table are byte-identical to before — no clone stack,
    no run card, no run row. A fresh canvas cell is the run's cell unless the
    test names its own ``cell_id``."""
    req_extra.setdefault("cell_id", _canvas_cell(client))

    before = _channel_snapshot()
    runs_before = _run_row_count(client)

    r = client.post(
        f"/api/channels/{client.channel_id}/runs",
        json={"runner": runner, **req_extra},
    )
    assert r.status_code == expect_status, r.text

    after = _channel_snapshot()
    assert after == before, (
        f"rejected run mutated channel state: {before} -> {after}"
    )
    assert _run_row_count(client) == runs_before, "rejected run left a run row"
    return r


def test_nonexistent_cell_leaves_no_trace(client):
    runner, _gen = install_runner(generator="reject_ghost", runner="reject_ghost")
    r = _assert_rejected_without_trace(client, runner, cell_id=str(uuid.uuid4()))
    assert "does not exist" in r.text


def test_new_cell_on_a_refused_runner_leaves_no_clone(client):
    """``new_cell`` clones only after the gates: a refused start makes no
    stack."""
    r = _assert_rejected_without_trace(
        client, "__no_such_runner__", expect_status=404, new_cell=True)
    assert "__no_such_runner__" in r.text


def test_unknown_runner_leaves_no_trace(client):
    # No runner installed at all — a 404 must still leave no trace.
    r = _assert_rejected_without_trace(
        client, "__no_such_runner__", expect_status=404,
    )
    assert "__no_such_runner__" in r.text


def test_tag_override_leaves_no_trace(client):
    runner, _gen = install_runner(generator="reject_tag", runner="reject_tag")
    r = _assert_rejected_without_trace(client, runner, expect_status=422, tag="ghost")
    assert "extra_forbidden" in r.text and "tag" in r.text


def test_config_override_leaves_no_trace(client):
    """Execution accepts only the persisted runner configuration."""
    runner, _gen = install_runner(
        generator="reject_cfg", runner="reject_cfg",
        draft_files={
            "knobs.set_yaml": (
                "fields:\n"
                "  - key: model\n"
                "    type: text\n"
                "    label: Model\n"
            ),
        },
    )
    r = _assert_rejected_without_trace(
        client, runner, expect_status=422,
        config_values={"knobs.set_yaml": {"model": "sneaky"}},
    )
    assert "extra_forbidden" in r.text and "config_values" in r.text


def test_missing_required_leaves_no_trace(client):
    runner, _gen = install_runner(
        generator="reject_required", runner="reject_required",
        draft_files={
            "need.set_yaml": (
                "fields:\n"
                "  - key: model\n"
                "    type: text\n"
                "    label: Model\n"
                "    required: true\n"
            ),
        },
    )
    r = _assert_rejected_without_trace(client, runner)
    assert r.json()["detail"] == "Runner is incomplete: Model"


def test_prompt_override_leaves_no_trace(client):
    """The prompt must be saved on the runner before execution."""
    runner, _gen = install_runner(generator="reject_prompt", runner="reject_prompt")
    r = _assert_rejected_without_trace(client, runner, expect_status=422, prompt="hello there")
    assert "extra_forbidden" in r.text and "prompt" in r.text


# ---------------------------------------------------------------------------
# Unwinds after the mutations began: the card is swept, the clone removed
# ---------------------------------------------------------------------------

def _cell_files(cell_id: str) -> set[str]:
    from state import state as app_state
    for ch in app_state.channels:
        for s in ch.main_stacks + ch.nav_stacks:
            for c in s.cells:
                if str(c.id) == cell_id:
                    return {f.name for f in c.files}
    raise AssertionError(f"cell {cell_id} is gone")


def _tip_files(client, cell_id: str) -> set[str]:
    from versions import find_tip, tip_file_map
    conn = get_db_connection(client.channel_id)
    try:
        tip = find_tip(conn, cell_id)
        return set(tip_file_map(conn, tip))
    finally:
        conn.close()


@pytest.mark.parametrize("new_cell", [False, True])
def test_a_spawn_failure_sweeps_the_card_and_removes_the_clone(client, monkeypatch, new_cell):
    import routes.runs.create as create

    class _Failing(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.process = None

        async def start(self):
            raise RuntimeError("synthetic spawn failure")

    monkeypatch.setattr(create, "ProcessManager", _Failing)
    runner, _gen = install_runner(generator="unwind_spawn", runner="unwind_spawn")
    cid = _canvas_cell(client)
    stacks_before = _channel_snapshot()[0]
    r = client.post(f"/api/channels/{client.channel_id}/runs",
                    json={"runner": runner, "cell_id": cid, "new_cell": new_cell})
    assert r.status_code == 200 and r.json()["status"] == "queued", r.text
    wait_spawned(r.json()["run_id"])
    assert _channel_snapshot()[0] == stacks_before, "the clone stack stayed"
    assert not any(p.startswith("_runs/") for p in _cell_files(cid) | _tip_files(client, cid))
    row = run_row(client.channel_id, r.json()["run_id"])
    assert row["status"] == "failed" and "synthetic spawn failure" in row["failure_reason"]


def test_a_start_refused_at_the_last_gate_sweeps_its_card(client, monkeypatch):
    """Shutdown landing while the spawn prepared: the accepted run fails at
    the last gate, the run card the accept committed is swept again and the
    clone stack removed."""
    import routes.runs.create as create
    import run_registry as registry

    real = create.resolve_optional_requirements

    def resolve_then_shut_down(*args):
        registry.mark_shutting_down(True)
        return real(*args)
    monkeypatch.setattr(create, "resolve_optional_requirements", resolve_then_shut_down)
    runner, _gen = install_runner(generator="unwind_gate", runner="unwind_gate")
    cid = _canvas_cell(client)
    stacks_before = _channel_snapshot()[0]
    try:
        r = client.post(f"/api/channels/{client.channel_id}/runs",
                        json={"runner": runner, "cell_id": cid, "new_cell": True})
        assert r.status_code == 200, r.text
        wait_spawned(r.json()["run_id"])
    finally:
        registry.mark_shutting_down(False)
    row = run_row(client.channel_id, r.json()["run_id"])
    assert row["status"] == "failed" and "shutting down" in row["failure_reason"]
    assert _channel_snapshot()[0] == stacks_before
    assert not any(p.startswith("_runs/") for p in _cell_files(cid) | _tip_files(client, cid))


@pytest.mark.parametrize("new_cell", [False, True])
def test_name_ownership_comes_only_from_the_host_clone__F1645(client, monkeypatch, new_cell):
    from process_manager import ProcessManager
    from state import require_channel

    captured = {}

    async def start(self):
        captured["created_cell"] = getattr(self, "created_cell", None)
        captured["cell_id"] = self.cell_id
        self._emit_partial(cell_name="Chosen runner", operation_id=str(uuid.uuid4()),
                           writer_id=str(uuid.uuid4()), edit_seq=1, base_revisions={})

    monkeypatch.setattr(ProcessManager, "start", start)
    runner, _ = install_runner(generator="name_owner", runner="name_owner")
    cid = _canvas_cell(client)
    source = next(c for s in require_channel(client.channel_id).main_stacks for c in s.cells if str(c.id) == cid)
    source.name = "User title"
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={
        "runner": runner, "cell_id": cid, "new_cell": new_cell,
    })
    assert response.status_code == 200, response.text
    wait_spawned(response.json()["run_id"])
    assert captured["created_cell"] is new_cell
    assert (captured["cell_id"] != cid) is new_cell
    assert source.name == "User title"
    if new_cell:
        clone = next(c for s in require_channel(client.channel_id).main_stacks for c in s.cells
                     if str(c.id) == captured["cell_id"])
        assert clone.name == "Chosen runner"
