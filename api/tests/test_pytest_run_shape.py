"""Run-request validation on POST /runs (runner-shaped).

A run is launched through a saved runner on one cell (``{runner, cell_id,
runner_revision?, new_cell?, selected_file?}``); create_run validates the
cell after resolving the runner, and never starts a run on a Capabilities
cell. Each rejection fires before any mutation (see
test_pytest_run_rejection_cleanup for the no-trace guarantee).
"""
from __future__ import annotations

import uuid

from conftest import add_capabilities_cell, install_runner

def _post(client, runner: str, **extra):
    body = {"runner": runner, **extra}
    return client.post(f"/api/channels/{client.channel_id}/runs", json=body)


def _canvas_cell(client) -> str:
    """Create a real cell on the canvas and return its id."""
    ch = client.post(f"/api/stacks/{client.channel_id}/create").json()
    return ch["main_stacks"][-1]["cells"][0]["id"]


# ---- runner resolution -----------------------------------------------------

def test_rejects_unknown_runner(client):
    cid = _canvas_cell(client)
    r = _post(client, "no-such-runner", cell_id=cid)
    assert r.status_code == 404, r.text
    assert "no-such-runner" in r.text


def test_rejects_tag_override(client):
    runner, _gen = install_runner(generator="shape_tag", runner="shape_tag")
    cid = _canvas_cell(client)
    r = _post(client, runner, tag="ghost", cell_id=cid)
    assert r.status_code == 422, r.text
    assert "extra_forbidden" in r.text and "tag" in r.text


def test_runner_and_generator_share_a_name(client):
    """Runner ids and generator ids are separate namespaces — a runner named
    ``web`` targeting a generator also named ``web`` must resolve."""
    runner, gen = install_runner(generator="dupname", runner="dupname")
    assert runner == "dupname" and gen == "dupname"
    cid = _canvas_cell(client)
    r = _post(client, "dupname", cell_id=cid)
    assert r.status_code != 400 and r.status_code != 404, r.text


# ---- the cell ---------------------------------------------------------------

def test_rejects_a_missing_cell_id(client):
    runner, _gen = install_runner(generator="shape_none", runner="shape_none")
    r = _post(client, runner)
    assert r.status_code == 422, r.text
    assert "cell_id" in r.text


def test_rejects_the_retired_request_fields(client):
    """The run's cell is ``cell_id``; a cell list and a client-sent call depth
    are refused."""
    runner, _gen = install_runner(generator="shape_old", runner="shape_old")
    cid = _canvas_cell(client)
    for extra in ({"cell_ids": [cid]}, {"call_depth": 1}):
        r = _post(client, runner, cell_id=cid, **extra)
        assert r.status_code == 422, r.text
        assert "extra_forbidden" in r.text


def test_rejects_nonexistent_cell(client):
    runner, _gen = install_runner(generator="shape_ghost", runner="shape_ghost")
    r = _post(client, runner, cell_id=str(uuid.uuid4()))
    assert r.status_code == 400, r.text
    assert "does not exist" in r.text


def _capabilities_cell() -> str:
    """Add a cell to the active channel's Capabilities stack."""
    from models import Cell
    from state import find_active_channel, persist_state

    cell = Cell(name="Synth capability", files=[])
    add_capabilities_cell(find_active_channel(), cell)
    persist_state()
    return str(cell.id)


def _runs_on(client, cell_id: str) -> list:
    from database import get_db_connection
    conn = get_db_connection(client.channel_id)
    try:
        return conn.execute("SELECT id FROM runs WHERE target_cell_id=?", (cell_id,)).fetchall()
    finally:
        conn.close()


def test_rejects_a_cell_in_the_capabilities_stack(client):
    """Nothing runs on Capabilities, not even on a new-cell clone of one of
    its cells: the server half of the frontend rule that hides every run
    affordance there."""
    runner, _gen = install_runner(generator="synth-gen", runner="synth-run")
    cid = _capabilities_cell()
    for new_cell in (False, True):
        r = _post(client, runner, cell_id=cid, new_cell=new_cell)
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "Runs never start on a Capabilities cell"
    assert _runs_on(client, cid) == []


def test_start_rechecks_capabilities_under_the_cell_lock(client, monkeypatch):
    """A cell moved onto Capabilities after the early gate and before the
    freeze takes its commit lock is refused there (409), before the start
    holds the cell or writes a run row or run card."""
    import routes.runs.create as create
    from cell_placement import capabilities_stack, find_cell
    from run_registry import running_cell_ids
    from state import find_active_channel, persist_state

    runner, _gen = install_runner(generator="synth-gen", runner="synth-run")
    cid = _canvas_cell(client)
    placeholders = create._start_placeholders

    def move_onto_capabilities(opens, runner_name):
        ch = find_active_channel()
        stack, cell = find_cell(ch, cid)
        assert stack is not None and cell is not None
        stack.cells.remove(cell)
        if not stack.cells:
            ch.main_stacks.remove(stack)
        capabilities_stack(ch).cells.append(cell)
        persist_state()
        return placeholders(opens, runner_name)

    monkeypatch.setattr(create, "_start_placeholders", move_onto_capabilities)
    r = _post(client, runner, cell_id=cid)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "The run’s cell moved onto Capabilities before startup"
    assert cid not in running_cell_ids()
    assert _runs_on(client, cid) == []
    from database import get_db_connection
    from persistence import snapshot_files
    conn = get_db_connection(client.channel_id)
    try:
        assert not any(f["path"].startswith("_runs/") for f in snapshot_files(conn, cid))
    finally:
        conn.close()


def test_valid_request_passes_gate(client):
    """A well-formed run on a canvas cell passes validation — it then spawns;
    we only care that it is not a 4xx."""
    runner, _gen = install_runner(generator="shape_ok", runner="shape_ok")
    cid = _canvas_cell(client)
    r = _post(client, runner, cell_id=cid)
    assert r.status_code < 400, r.text


# ---- request shape --------------------------------------------------------

def test_run_request_fields():
    from routes.runs.models import RunRequest

    # RunRequest takes a runner, its revision, the cell, clone placement and
    # the selected file — nothing else.
    assert set(RunRequest.model_fields) == {
        "runner", "runner_revision", "cell_id", "new_cell", "selected_file"}
    assert RunRequest.model_fields["selected_file"].default is None
    assert RunRequest.model_config.get("extra") == "forbid"
    assert RunRequest.model_fields["new_cell"].default is False


def test_renderer_action_request_fields():
    from routes.runs.models import RendererActionRequest

    # A renderer action names the action, the file it was emitted from and an
    # optional payload; the run is resolved from the source, never named.
    assert set(RendererActionRequest.model_fields) == {"action_id", "source", "payload"}
    assert RendererActionRequest.model_config.get("extra") == "forbid"


def test_process_manager_has_no_invocation_type(tmp_path):
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED

    pm = ProcessManager(
        channel=str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:run-shape:manager")),
        run_id="r", app_dir=str(tmp_path),
        workspace_dir=str(tmp_path), entrypoint="x.py", generator_name="x",
        permissions=UNRESTRICTED,
    )
    assert not hasattr(pm, "invocation_type")
