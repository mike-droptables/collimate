"""The selected file (GN-01): a run of a runner that takes one gets the
cell-relative path of the file the user selected.

The POST's ``selected_file`` passes the gates before any mutation (only for
a runner whose manifest takes it with its saved settings, a plain cell path outside ``_runs/`` and ``.collrun/``, present in
the run's cell), then travels as ``.collrun/selected_file`` and is recorded
in ``runner_snapshot`` and the run.yaml config artifact. The runner catalog
says per runner whether it takes one. Synthetic generators; the managers
are fakes that never spawn.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from conftest import install_runner
from database import get_db_connection
from fake_manager import FakeManager
from run_spawn import run_row, wait_spawned

_SCHEMA = (
    "fields:\n"
    "  - key: pick\n"
    "    type: checkbox\n"
    "    default: false\n"
)


def _install(name: str, *, pick: bool) -> str:
    """A runner of the one ``selected_probe`` generator (installed by the
    first call) with ``pick`` saved."""
    import runners
    from cell_placement import capabilities_stack_cells
    from state import state as app_state
    channel = next(c for c in app_state.channels if c.id == app_state.active_channel_id)
    index = runners.generator_index(capabilities_stack_cells(channel))
    if "selected_probe" not in index:
        runner, _ = install_runner(
            generator="selected_probe", runner=name, draft_files={"settings.set_yaml": _SCHEMA},
            gen_manifest_extra="takes_selected_file_if: {pick: true}\n",
            definition={"settings": {"settings.set_yaml": {"pick": pick}}},
        )
        return runner
    data = runners.config_template(index["selected_probe"], "default")["runner"]
    data.update(id=name, generator="selected_probe",
                settings={"settings.set_yaml": {"pick": pick}})
    runners.save_runner(channel, data, [], create=True)
    return name


@pytest.fixture
def fake_runs(client, monkeypatch):
    import routes.runs.create as create
    import run_registry

    managers: dict[str, FakeManager] = {}

    class Manager(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            managers[self.run_id] = self

        async def start(self):
            pass

    async def no_reaper(*_args):
        pass

    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", no_reaper)
    yield managers
    for run_id in managers:
        run_registry.active_managers.pop(run_id, None)


def _cell_with(client, files: dict[str, str]) -> str:
    """A canvas cell holding ``files`` (accepted)."""
    from cell_commits import commit_cell_change
    cell = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    conn = get_db_connection(client.channel_id)
    try:
        conn.execute("INSERT OR IGNORE INTO runs (id, prompt, status) VALUES ('seed', 'seed', 'completed')")
        conn.commit()
    finally:
        conn.close()
    commit_cell_change(client.channel_id, cell, "seed", writes=files)
    return cell


def _counts(client) -> tuple[int, int]:
    from state import state as app_state
    ch = next(c for c in app_state.channels if str(c.id) == client.channel_id)
    conn = get_db_connection(client.channel_id)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        conn.close()
    return rows, len(ch.main_stacks)


def _post(client, runner: str, cell: str, **extra):
    return client.post(f"/api/channels/{client.channel_id}/runs",
                       json={"runner": runner, "cell_id": cell, **extra})


def _run_yaml(client, run_id: str) -> dict:
    from cell_commits import blob_bytes
    conn = get_db_connection(client.channel_id)
    try:
        blob = conn.execute("SELECT blob_hash FROM run_artifacts WHERE run_id=? AND filepath='run.yaml'",
                            (run_id,)).fetchone()[0]
    finally:
        conn.close()
    return yaml.safe_load(blob_bytes(blob))


def test_the_selected_file_travels_to_the_run_and_is_recorded_GN01(client, fake_runs):
    runner = _install("takes-it", pick=True)
    cell = _cell_with(client, {"notes/draft.md": "# Draft\n"})
    response = _post(client, runner, cell, selected_file="notes/draft.md")
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    wait_spawned(run_id)
    workspace = Path(fake_runs[run_id].workspace_dir)
    assert (workspace / ".collrun/selected_file").read_bytes() == b"notes/draft.md"
    assert (workspace / "notes/draft.md").read_text() == "# Draft\n"
    assert json.loads(run_row(client.channel_id, run_id)["runner_snapshot"])["selected_file"] == "notes/draft.md"
    assert _run_yaml(client, run_id)["selected_file"] == "notes/draft.md"


def test_a_run_without_a_selected_file_has_none_GN01(client, fake_runs):
    runner = _install("takes-it", pick=True)
    cell = _cell_with(client, {"notes/draft.md": "# Draft\n"})
    response = _post(client, runner, cell)
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    wait_spawned(run_id)
    assert not (Path(fake_runs[run_id].workspace_dir) / ".collrun/selected_file").exists()
    assert "selected_file" not in json.loads(run_row(client.channel_id, run_id)["runner_snapshot"])
    assert "selected_file" not in _run_yaml(client, run_id)


def test_new_cell_keeps_the_selected_file_GN01(client, fake_runs):
    runner = _install("takes-it", pick=True)
    cell = _cell_with(client, {"a.txt": "alpha"})
    response = _post(client, runner, cell, selected_file="a.txt", new_cell=True)
    assert response.status_code == 200, response.text
    answer = response.json()
    wait_spawned(answer["run_id"])
    manager = fake_runs[answer["run_id"]]
    assert answer["cell_id"] != cell and manager.cell_id == answer["cell_id"]
    workspace = Path(manager.workspace_dir)
    assert (workspace / ".collrun/selected_file").read_text() == "a.txt"
    assert (workspace / "a.txt").read_text() == "alpha"


@pytest.mark.parametrize("case", ["not_taken", "runs", "dotdot", "absolute", "collrun",
                                  "inner_dotdot", "missing"])
def test_a_refused_selected_file_leaves_no_trace_GN01(client, fake_runs, case):
    takes = _install("takes-it", pick=True)
    declines = _install("declines-it", pick=False)
    cell = _cell_with(client, {"a.txt": "alpha"})
    before = _counts(client)
    body = {
        "not_taken": dict(runner=declines, selected_file="a.txt"),
        "runs": dict(runner=takes, selected_file="_runs/x-12345678/a.txt"),
        "dotdot": dict(runner=takes, selected_file="../a.txt"),
        "absolute": dict(runner=takes, selected_file="/a.txt"),
        "collrun": dict(runner=takes, selected_file=".collrun/prompt.md"),
        "inner_dotdot": dict(runner=takes, selected_file="x/../a.txt"),
        "missing": dict(runner=takes, selected_file="gone.txt", new_cell=True),
    }[case]
    runner = body.pop("runner")
    response = _post(client, runner, cell, **body)
    if case == "missing":
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "selected_file_missing"
    else:
        assert response.status_code == 400, response.text
    if case == "not_taken":
        assert response.json()["detail"] == "runner does not take a selected file"
    elif case != "missing":
        assert "selected_file must be a relative cell file path" in response.json()["detail"]
    assert _counts(client) == before
    assert fake_runs == {}


def test_a_selected_file_removed_before_the_freeze_is_refused_and_unwound_GN01(client, fake_runs, monkeypatch):
    """The file is checked again under the freeze's locks on the run's own
    cell: gone by then, the start is a 409 and the clone is removed."""
    import routes.runs.create as create
    runner = _install("takes-it", pick=True)
    cell = _cell_with(client, {"a.txt": "alpha"})
    before = _counts(client)
    calls = []
    real = create._cell_paths

    def vanishing(channel, cell_id):
        calls.append(cell_id)
        paths = real(channel, cell_id)
        return paths if len(calls) == 1 else paths - {"a.txt"}
    monkeypatch.setattr(create, "_cell_paths", vanishing)
    response = _post(client, runner, cell, selected_file="a.txt", new_cell=True)
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "selected_file_missing"
    assert len(calls) == 2 and calls[1] != cell
    assert _counts(client) == before
    assert fake_runs == {}


def test_the_catalogs_say_which_runners_take_the_selected_file_GN01(client):
    takes = _install("takes-it", pick=True)
    declines = _install("declines-it", pick=False)
    entries = {r["id"]: r for r in client.get("/api/runners").json()}
    assert entries[takes]["takes_selected_file"] is True
    assert entries[declines]["takes_selected_file"] is False
    [row] = [g for g in client.get("/api/generators").json() if g["name"] == "selected_probe"]
    assert row["takes_selected_file"] is False
    assert row["takes_selected_file_if"] == {"pick": True}
    assert row["requires_docker_if"] is None
    [payload] = [g for g in client.get("/api/runner_generators").json() if g["id"] == "selected_probe"]
    assert payload["takes_selected_file"] is False
    assert payload["takes_selected_file_if"] == {"pick": True}
