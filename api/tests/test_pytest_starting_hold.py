# SPDX-License-Identifier: MIT
"""The starting hold (run_registry): every start holds its cell from the run
row's insert until it registers or unwinds, keyed by run, and Stop all
cancels every hold without freeing the cell before the start lets go. The
reaper of an ended run removes it, reaps its containers and reclaims its app
tree, and leaves no hold. Synthetic stubs throughout."""
from __future__ import annotations

import asyncio

from fake_manager import FakeManager


def test_a_starting_hold_counts_as_running(monkeypatch):
    """While the hold stands, the cell is running for every other gate too
    (idle checkpoint, stack removal, merge). Holds are keyed by run, so two
    starts on one cell keep both."""
    import run_registry as registry
    monkeypatch.setattr(registry, "_holds", {})
    monkeypatch.setattr(registry, "active_managers", {})
    registry.reserve("run-a", "cell-h")
    registry.reserve("run-b", "cell-h")
    assert registry.hold_is_live("run-a") and registry.hold_is_live("run-b")
    assert "cell-h" in registry.running_cell_ids()
    registry.release("run-b")
    assert registry.hold_is_live("run-a")
    assert "cell-h" in registry.running_cell_ids()
    registry.release("run-a")
    assert "cell-h" not in registry.running_cell_ids()


def test_a_cancelled_hold_still_holds_the_cell_until_released(monkeypatch):
    """Stop all cancels a hold without freeing the cell: the start holding
    it is unwinding, and only its release makes the cell free."""
    import run_registry as registry
    monkeypatch.setattr(registry, "_holds", {})
    monkeypatch.setattr(registry, "active_managers", {})
    registry.reserve("run-a", "cell-h")
    assert registry.hold_is_live("run-a")
    assert registry.cancel_holds() == 1
    assert not registry.hold_is_live("run-a")
    assert "cell-h" in registry.held_cell_ids()
    assert "cell-h" in registry.running_cell_ids()
    registry.release("run-a")
    assert "cell-h" not in registry.running_cell_ids()


def test_the_reaper_of_an_ended_run_cleans_up_and_leaves_no_hold(monkeypatch):
    """A run that ended is popped from the registry, its containers are
    reaped and its app tree reclaimed, in that order, and its cell is left
    with no hold and no running run."""
    import routes.runs.helpers as helpers
    import run_registry as registry
    monkeypatch.setattr(registry, "active_managers", {})
    monkeypatch.setattr(helpers, "active_managers", registry.active_managers)
    monkeypatch.setattr(registry, "_holds", {})
    calls: list = []

    def reap_containers(run_id):
        assert run_id not in registry.active_managers
        calls.append(("containers", run_id))

    monkeypatch.setattr(helpers, "_reap_run_containers", reap_containers)
    monkeypatch.setattr(helpers, "_reclaim_run_app_dir",
                        lambda manager: calls.append(("app_tree", manager.run_id)))
    mgr = FakeManager(run_id="run-old", cell_id="cell-r", runner_id="synth",
                      final_status="completed")
    registry.active_managers["run-old"] = mgr
    asyncio.run(helpers._reap_process("run-old", mgr))
    assert "run-old" not in registry.active_managers
    assert calls == [("containers", "run-old"), ("app_tree", "run-old")]
    assert registry._holds == {}
    assert "cell-r" not in registry.running_cell_ids()


def test_the_hold_stands_from_the_accept_until_the_spawn_registers_GN03(client, monkeypatch):
    """POST /runs answers once the run is accepted; its hold stands while the
    background spawn prepares (the cell counts as running) and is released
    once the spawn registered the run."""
    import threading
    import process_manager.manager as pm
    import routes.runs.create as create
    import run_registry as registry
    from conftest import install_runner
    from run_spawn import wait_spawned

    registered = {}

    class Manager(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            registered[self.run_id] = self

        async def start(self):
            pass

    async def no_reaper(*_args):
        pass

    gate = threading.Event()
    real_retain = pm.retain_account_leases

    async def held(context, owner):
        await asyncio.to_thread(gate.wait, 30)
        return await real_retain(context, owner)

    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(create, "_reap_process", no_reaper)
    monkeypatch.setattr(pm, "retain_account_leases", held)
    runner, _ = install_runner(generator="hold_probe", runner="hold-probe")
    cell = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    try:
        response = client.post(f"/api/channels/{client.channel_id}/runs",
                               json={"runner": runner, "cell_id": cell})
        assert response.status_code == 200 and response.json()["status"] == "queued", response.text
        run_id = response.json()["run_id"]
        assert registry.hold_is_live(run_id) and cell in registry.running_cell_ids()
        assert registered == {}
    finally:
        gate.set()
    wait_spawned(run_id)
    try:
        assert set(registered) == {run_id}
        assert not registry.hold_is_live(run_id) and run_id not in registry._holds
    finally:
        registry.active_managers.pop(run_id, None)


def test_a_stop_of_a_queued_run_unwinds_its_spawn_as_stopped_GN03(client, monkeypatch):
    """A Stop that reaches an accepted run before it registered ends its row
    stopped; the spawn sees that at its last gate and unwinds without
    registering or spawning, sweeping the run card."""
    import threading
    import process_manager.manager as pm
    import routes.runs.create as create
    import run_registry as registry
    from conftest import install_runner
    from run_spawn import run_row, wait_spawned

    registered = {}

    class Manager(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            registered[self.run_id] = self

    gate = threading.Event()
    real_retain = pm.retain_account_leases

    async def held(context, owner):
        await asyncio.to_thread(gate.wait, 30)
        return await real_retain(context, owner)

    monkeypatch.setattr(create, "ProcessManager", Manager)
    monkeypatch.setattr(pm, "retain_account_leases", held)
    runner, _ = install_runner(generator="queued_stop", runner="queued-stop")
    cell = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    try:
        response = client.post(f"/api/channels/{client.channel_id}/runs",
                               json={"runner": runner, "cell_id": cell})
        assert response.status_code == 200, response.text
        run_id, label = response.json()["run_id"], response.json()["label"]
        assert client.delete(f"/api/channels/{client.channel_id}/runs/{run_id}").status_code == 200
    finally:
        gate.set()
    wait_spawned(run_id)
    assert registered == {} and run_id not in registry.active_managers
    assert run_row(client.channel_id, run_id)["status"] == "stopped"
    assert run_id not in registry._holds
    from state import state as app_state
    files = {f.name for ch in app_state.channels for s in ch.main_stacks for c in s.cells
             if str(c.id) == cell for f in c.files}
    assert not any(name.startswith(f"_runs/{label}/") for name in files)
