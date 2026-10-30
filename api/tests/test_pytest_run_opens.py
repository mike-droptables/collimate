"""Optimistic start (GN-03): a run's opened files exist and are claimed the
moment it is accepted.

The accept commits the run card and the starting placeholders of the
manifest's resolved ``opens`` in one version before the freeze, claims them
as starting surfaces and answers ``queued`` with ``opens``; the spawn runs
in the background. The run's own ``register`` upgrades its claim to live;
another run's start on a claimed cell-scoped path is a 409
``surface_taken``; the unwind and the run end release the claims.

A synthetic generator throughout; the managers are fakes that never spawn,
except where a real run's end is the point.
"""
from __future__ import annotations

import json
import shutil
import threading
import time

import pytest

from conftest import install_runner
from database import get_db_connection
from fake_manager import FakeManager
from run_spawn import run_row, wait_spawned

_SCHEMA = (
    "fields:\n"
    "  - key: surface\n"
    "    type: dropdown\n"
    "    options:\n"
    "      - value: doc\n"
    "        label: Document\n"
    "      - value: chat\n"
    "        label: Chat\n"
    "    default: chat\n"
    "  - key: against\n"
    "    type: text\n"
    "    default: review.panel\n"
)

_MANIFEST = (
    "ui_files: ['.chat', '.json']\n"
    "opens:\n"
    "  - path: '{run}/chat/conversation.chat'\n"
    "    if: {surface: chat}\n"
    "  - path: '{against}'\n"
    "    if: {surface: doc}\n"
)

_UI_FILES = (".chat", ".json")


def _install(name: str, surface: str, *, entrypoint_body: str | None = None) -> str:
    runner, _ = install_runner(
        generator=name, runner=name, draft_files={"settings.set_yaml": _SCHEMA},
        gen_manifest_extra=_MANIFEST,
        definition={"name": f"{name} runner",
                    "settings": {"settings.set_yaml": {"surface": surface}}},
        **({"entrypoint_body": entrypoint_body} if entrypoint_body else {}),
    )
    return runner


def _canvas_cell(client) -> str:
    return client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]


def _accepted(client, cell_id: str) -> dict[str, str]:
    from cell_commits import blob_bytes
    from persistence import snapshot_files
    conn = get_db_connection(client.channel_id)
    try:
        return {f["path"]: (blob_bytes(f["body_hash"]) or b"").decode("utf-8")
                for f in snapshot_files(conn, cell_id)}
    finally:
        conn.close()


def _surface(client, cell_id: str, path: str):
    return client.get(f"/api/channels/{client.channel_id}/cells/{cell_id}/surfaces/{path}")


def _active(client) -> dict:
    return {r["run_id"]: r for r in client.get(f"/api/channels/{client.channel_id}/runs/active").json()}


@pytest.fixture
def client(channel_client):
    """The live client on a channel holding the shipped renderers."""
    from conftest import create_suggested_channel
    client = channel_client[0]
    client.channel_id = create_suggested_channel(client, "Opens")
    return client


@pytest.fixture
def fake_runs(client, monkeypatch):
    """Managers that never spawn and stay registered (no reaper), and every
    broadcast recorded. Teardown drops what the test registered."""
    import events
    import routes.runs.create as create
    import run_registry
    import served_surfaces
    from run_folders import mint_run_label, run_folder

    managers: dict[str, FakeManager] = {}
    broadcasts: list[tuple[str, dict]] = []

    class Manager(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.label = mint_run_label(self.runner_id, self.run_id)
            self.folder = run_folder(self.label)
            self.ui_files = _UI_FILES
            self.sealed = False
            managers[self.run_id] = self

        async def start(self):
            pass

    async def no_reaper(*_args):
        pass

    real_broadcast = events.broadcast

    def record(channel, event):
        broadcasts.append((channel, event))
        return real_broadcast(channel, event)

    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", no_reaper)
    monkeypatch.setattr(events, "broadcast", record)
    yield managers, broadcasts
    for run_id in managers:
        run_registry.active_managers.pop(run_id, None)
        served_surfaces.release_run(run_id)


def _hold_spawns(monkeypatch) -> threading.Event:
    """Hold every spawn just before its last gate (in its lease retention)
    until the returned event is set."""
    import asyncio

    import process_manager.manager as pm
    gate = threading.Event()
    real_retain = pm.retain_account_leases

    async def held(context, owner):
        await asyncio.to_thread(gate.wait, 30)
        return await real_retain(context, owner)
    monkeypatch.setattr(pm, "retain_account_leases", held)
    return gate


def _post(client, runner: str, cell: str, **extra):
    return client.post(f"/api/channels/{client.channel_id}/runs",
                       json={"runner": runner, "cell_id": cell, **extra})


def test_the_card_and_placeholders_commit_in_one_version_before_the_freeze_GN03(client, fake_runs):
    """The accept's one "Run started" version holds the card and the chat
    pointer placeholder; it is the run's frozen input, so the hydrated
    workspace and the write baseline hold both. The answer and run_started
    carry ``opens``."""
    managers, broadcasts = fake_runs
    runner = _install("opens_chat", "chat")
    cell = _canvas_cell(client)
    response = _post(client, runner, cell)
    assert response.status_code == 200, response.text
    answer = response.json()
    run_id, label = answer["run_id"], answer["label"]
    pointer = f"_runs/{label}/chat/conversation.chat"
    card = f"_runs/{label}/{label}.run"
    assert answer["status"] == "queued"
    assert answer["opens"] == [pointer]
    started = [ev for _, ev in broadcasts if ev["type"] == "run_started"]
    assert [ev["opens"] for ev in started] == [[pointer]]
    wait_spawned(run_id)

    conn = get_db_connection(client.channel_id)
    try:
        from versions import find_tip, tip_file_map
        [version_id] = [v["id"] for v in conn.execute(
            "SELECT id FROM cells WHERE run_id=? AND version_name='Run started'", (run_id,))]
        files = tip_file_map(conn, version_id)
        tip = find_tip(conn, cell)
        blob = conn.execute(
            "SELECT blob_hash FROM run_artifacts WHERE run_id=? AND filepath='run.yaml'",
            (run_id,)).fetchone()[0]
    finally:
        conn.close()
    assert tip == version_id
    assert {card, pointer} <= set(files)
    import yaml
    from cell_commits import blob_bytes
    assert yaml.safe_load(blob_bytes(blob))["input_version_ids"] == [version_id]

    accepted = _accepted(client, cell)
    assert json.loads(accepted[pointer]) == {"starting": True, "label": "opens_chat runner"}
    from pathlib import Path
    workspace = Path(managers[run_id].workspace_dir)
    assert json.loads((workspace / pointer).read_text()) == {"starting": True, "label": "opens_chat runner"}
    assert (workspace / card).read_text() == accepted[card]
    baseline = json.loads((workspace / ".collrun/file_revisions.json").read_text())
    assert {card, pointer} <= set(baseline)


def test_a_starting_claim_is_listed_and_the_runs_register_upgrades_it_GN03(client, fake_runs, monkeypatch):
    """While the spawn is in flight the run is a queued /runs/active row
    whose surface is starting, and get_surface answers its starting
    coordinates; the run's register of the same path upgrades it to live,
    and surface_changed fires on the claim and on the upgrade."""
    import served_surfaces
    managers, broadcasts = fake_runs
    runner = _install("opens_claim", "chat")
    cell = _canvas_cell(client)
    gate = _hold_spawns(monkeypatch)
    try:
        response = _post(client, runner, cell)
        assert response.status_code == 200, response.text
        run_id, label = response.json()["run_id"], response.json()["label"]
        pointer = f"_runs/{label}/chat/conversation.chat"
        starting = {"state": "starting", "run_id": run_id, "label": "opens_claim runner",
                    "chat_id": "", "ws_path": "", "token": ""}
        assert _surface(client, cell, pointer).json() == starting
        row = _active(client)[run_id]
        assert row["phase"] == "queued" and row["stopping"] is False
        assert row["surfaces"] == [{"path": pointer, "label": "opens_claim runner", "state": "starting"}]
    finally:
        gate.set()
    wait_spawned(run_id)
    assert any(ev == {"type": "surface_changed", "cell_id": cell, "path": pointer}
               for _, ev in broadcasts)

    before = len(broadcasts)
    coordinates = served_surfaces.register(managers[run_id], pointer, label="Chat")
    assert coordinates["state"] == "live" and coordinates["chat_id"]
    live = _surface(client, cell, pointer).json()
    assert live["state"] == "live" and live["chat_id"] == coordinates["chat_id"]
    assert _active(client)[run_id]["surfaces"][0]["state"] == "live"
    assert any(ev["type"] == "surface_changed" for _, ev in broadcasts[before:])


def test_a_second_run_on_a_claimed_cell_path_is_409_surface_taken_GN03(client, fake_runs):
    """A cell-scoped path another run is starting (or serving) refuses the
    next start at POST, before any mutation: no row, no card."""
    runner = _install("opens_taken", "doc")
    cell = _canvas_cell(client)
    first = _post(client, runner, cell)
    assert first.status_code == 200, first.text
    wait_spawned(first.json()["run_id"])
    conn = get_db_connection(client.channel_id)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        conn.close()
    before = set(_accepted(client, cell))

    second = _post(client, runner, cell)
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["error"] == "surface_taken" and body["path"] == "review.panel"
    assert body["owner"] == first.json()["label"] and body["run_id"] == first.json()["run_id"]
    conn = get_db_connection(client.channel_id)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == rows
    finally:
        conn.close()
    assert set(_accepted(client, cell)) == before
    # Run in new cell is the way around it.
    assert _post(client, runner, cell, new_cell=True).status_code == 200


def test_a_spawn_failure_after_accept_fails_the_run_and_releases_its_claim_GN03(client, fake_runs, monkeypatch):
    """A failure after the accept is a failed run: run_status says so, the
    starting claim is released, the run folder swept."""
    import routes.runs.create as create
    managers, broadcasts = fake_runs

    class Failing(create.ProcessManager):
        async def start(self):
            raise RuntimeError("synthetic spawn failure")
    monkeypatch.setattr(create, "ProcessManager", Failing)
    runner = _install("opens_fail", "chat")
    cell = _canvas_cell(client)
    response = _post(client, runner, cell)
    assert response.status_code == 200, response.text
    run_id, label = response.json()["run_id"], response.json()["label"]
    wait_spawned(run_id)
    row = run_row(client.channel_id, run_id)
    assert row["status"] == "failed" and "synthetic spawn failure" in row["failure_reason"]
    assert any(ev["type"] == "run_status" and ev["run_id"] == run_id and ev["status"] == "failed"
               for _, ev in broadcasts)
    assert _surface(client, cell, f"_runs/{label}/chat/conversation.chat").status_code == 404
    assert not any(p.startswith(f"_runs/{label}/") for p in _accepted(client, cell))
    assert run_id not in _active(client)


def test_a_stop_that_reaches_a_queued_run_stops_it_at_its_last_gate_GN03(client, fake_runs, monkeypatch):
    """A per-run Stop of an accepted run whose spawn is in flight calls
    ``stop_queued`` before its first await: the run is listed stopping at
    once, and its spawn refuses at its last gate even though the row still
    reads 'running' there (the Stop's own row write may land later). It
    never registers and unwinds as stopped."""
    import routes.runs.create as create
    managers, broadcasts = fake_runs
    runner = _install("opens_stop", "chat")
    cell = _canvas_cell(client)
    gate = _hold_spawns(monkeypatch)
    try:
        response = _post(client, runner, cell)
        assert response.status_code == 200, response.text
        run_id, label = response.json()["run_id"], response.json()["label"]
        assert _active(client)[run_id]["stopping"] is False
        assert create.stop_queued("another-channel", run_id) is False
        assert create.stop_queued(client.channel_id, run_id) is True
        assert _active(client)[run_id]["stopping"] is True
        assert run_row(client.channel_id, run_id)["status"] == "running"
    finally:
        gate.set()
    wait_spawned(run_id)
    assert run_id not in managers
    assert run_row(client.channel_id, run_id)["status"] == "stopped"
    assert any(ev["type"] == "run_status" and ev["run_id"] == run_id and ev["status"] == "stopped"
               for _, ev in broadcasts)
    assert _surface(client, cell, f"_runs/{label}/chat/conversation.chat").status_code == 404
    assert not any(p.startswith(f"_runs/{label}/") for p in _accepted(client, cell))
    assert run_id not in _active(client)
    # A run that is no longer starting is not stopped this way.
    assert create.stop_queued(client.channel_id, run_id) is False
    assert run_id not in create._stops


def test_shutdown_waits_for_a_spawn_in_flight_and_it_unwinds_stopped_GN03(client, fake_runs, monkeypatch):
    """Shutdown cancels every starting hold, then waits for the spawns in
    flight (``wait_for_spawns``) before the persistence writer stops: a run
    accepted just before has unwound as stopped (row ended, claim released,
    folder swept) by the time the wait returns. A spawn still going at the
    bound is named."""
    import routes.runs.create as create
    import run_registry
    managers, _broadcasts = fake_runs
    runner = _install("opens_shutdown", "chat")
    cell = _canvas_cell(client)
    gate = _hold_spawns(monkeypatch)
    try:
        response = _post(client, runner, cell)
        assert response.status_code == 200, response.text
        run_id, label = response.json()["run_id"], response.json()["label"]
        run_registry.cancel_holds()
        assert _active(client)[run_id]["stopping"] is True
        assert client.portal.call(create.wait_for_spawns, 0.05) == [run_id]
        threading.Timer(0.1, gate.set).start()
        assert client.portal.call(create.wait_for_spawns, 30.0) == []
    finally:
        gate.set()
    assert run_id not in managers and run_id not in create._spawns
    assert run_row(client.channel_id, run_id)["status"] == "stopped"
    assert _surface(client, cell, f"_runs/{label}/chat/conversation.chat").status_code == 404
    assert not any(p.startswith(f"_runs/{label}/") for p in _accepted(client, cell))
    assert client.portal.call(create.wait_for_spawns, 0.05) == []


def test_the_stop_route_stops_a_queued_run_at_its_last_gate_GN03(client, fake_runs, monkeypatch):
    """DELETE .../runs/{id} on an accepted run whose spawn is in flight
    reaches it through ``stop_queued`` before the Stop's row write: the
    spawn refuses at its last gate and never registers."""
    import routes.runs.create as create
    managers, _broadcasts = fake_runs
    runner = _install("opens_stop_route", "chat")
    cell = _canvas_cell(client)
    gate = _hold_spawns(monkeypatch)
    try:
        response = _post(client, runner, cell)
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        stopped = client.delete(f"/api/channels/{client.channel_id}/runs/{run_id}")
        assert stopped.status_code == 200, stopped.text
        assert run_id in create._stops
    finally:
        gate.set()
    wait_spawned(run_id)
    assert run_id not in managers
    assert run_row(client.channel_id, run_id)["status"] == "stopped"


def test_the_shutdown_reap_wait_waits_for_spawns_in_flight_GN03(client, fake_runs, monkeypatch):
    """``wait_for_run_reaps`` (the lifespan's last run step) waits for the
    spawns in flight first, so a run accepted before the shutdown has
    unwound as stopped before the persistence writer stops."""
    import routes.runs.create as create
    import run_registry
    from routes.runs.teardown import wait_for_run_reaps
    managers, _broadcasts = fake_runs
    runner = _install("opens_reap_wait", "chat")
    cell = _canvas_cell(client)
    gate = _hold_spawns(monkeypatch)
    try:
        response = _post(client, runner, cell)
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        run_registry.cancel_holds()
        threading.Timer(0.1, gate.set).start()
        assert client.portal.call(wait_for_run_reaps, 30.0) == []
        assert run_id not in create._spawns
    finally:
        gate.set()
    assert run_id not in managers
    assert run_row(client.channel_id, run_id)["status"] == "stopped"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available on PATH; a real run needs it")
def test_the_run_end_releases_a_claim_the_run_never_registered_GN03(client):
    """A real run that opens a cell-scoped path and ends without serving
    it frees the starting claim (and the path's lock) at its end."""
    body = (
        "# /// script\n"
        "# requires-python = \">=3.11\"\n"
        "# dependencies = []\n"
        "# ///\n"
        "import gen\n"
        "async def run():\n"
        "    pass\n"
        "if __name__ == '__main__':\n"
        "    gen.main(run)\n"
    )
    runner = _install("opens_end", "doc", entrypoint_body=body)
    cell = _canvas_cell(client)
    response = _post(client, runner, cell)
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    assert _surface(client, cell, "review.panel").json()["state"] == "starting"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and run_row(client.channel_id, run_id)["status"] == "running":
        time.sleep(0.2)
    while time.monotonic() < deadline and _surface(client, cell, "review.panel").status_code != 404:
        time.sleep(0.2)
    assert run_row(client.channel_id, run_id)["status"] != "running"
    assert _surface(client, cell, "review.panel").status_code == 404
    assert "review.panel" not in _accepted(client, cell)


def test_the_runs_own_write_of_a_claimed_pointer_makes_it_live_GN03(client, fake_runs, monkeypatch):
    """A pointer type the generator fills by writing the file (a webview tab)
    registers no session: the run's own write of the path it holds a starting
    claim on makes the claim live, with artifact coordinates (no chat), and
    surface_changed fires. A later register of the same path still works, and
    the emit path itself is what calls it."""
    import served_surfaces
    from process_manager.own_cell_writes import OwnCellWritesMixin
    managers, broadcasts = fake_runs
    runner = _install("opens_written", "chat")
    cell = _canvas_cell(client)
    gate = _hold_spawns(monkeypatch)
    try:
        response = _post(client, runner, cell)
        assert response.status_code == 200, response.text
        run_id, label = response.json()["run_id"], response.json()["label"]
    finally:
        gate.set()
    wait_spawned(run_id)
    pointer = f"_runs/{label}/chat/conversation.chat"
    assert _surface(client, cell, pointer).json()["state"] == "starting"

    # Another run's write never touches the claim; a foreign path is ignored.
    assert served_surfaces.mark_written("someone-else", cell, [pointer]) == []
    assert served_surfaces.mark_written(run_id, cell, [f"_runs/{label}/other.chat"]) == []
    assert _surface(client, cell, pointer).json()["state"] == "starting"

    # The run's emit of the pointer's path upgrades the claim through the emit path.
    manager = managers[run_id]
    manager.created_cell = False
    manager._update_cell = lambda data: "v-written"
    manager.log_refusal = lambda *_args, **_kw: None
    manager._run_path_refusal = lambda path, **kw: OwnCellWritesMixin._run_path_refusal(manager, path, **kw)
    before = len(broadcasts)
    version_id, refused = OwnCellWritesMixin._emit_partial(
        manager, artifacts=[{"type": "workspace", "cell_path": pointer, "content": "{}", "encoding": "utf-8"}])
    assert version_id == "v-written" and refused == []
    live = _surface(client, cell, pointer).json()
    assert live == {"state": "live", "run_id": run_id, "label": "opens_written runner",
                    "chat_id": "", "ws_path": "", "token": ""}
    assert _active(client)[run_id]["surfaces"][0]["state"] == "live"
    assert any(ev == {"type": "surface_changed", "cell_id": cell, "path": pointer}
               for _, ev in broadcasts[before:])
    # Idempotent: a second write changes nothing and announces nothing.
    before = len(broadcasts)
    assert served_surfaces.mark_written(run_id, cell, [pointer]) == []
    assert len(broadcasts) == before

    # A register of the same path by the same run still replaces it with a session.
    coordinates = served_surfaces.register(manager, pointer, label="Chat")
    assert coordinates["state"] == "live" and coordinates["chat_id"]
    assert _surface(client, cell, pointer).json()["chat_id"] == coordinates["chat_id"]
