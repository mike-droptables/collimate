"""Start freeze keeps identity held while materialization leaves state_lock."""
from conftest import install_runner
from run_spawn import run_row, wait_spawned


def test_materialization_runs_without_global_lock_but_holds_cell__F0333_P0F17(client, monkeypatch):
    import run_staging
    import run_registry
    import state
    runner, _ = install_runner(generator="freeze_scale")
    cell = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    observations = []
    def writing(_dest, _file):
        observations.append((state.state_lock._is_owned(), cell in run_registry.running_cell_ids()))
        raise OSError("synthetic materialization stop")
    monkeypatch.setattr(run_staging, "_write_source_file", writing)
    result = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cell})
    assert result.status_code == 200, result.text
    wait_spawned(result.json()["run_id"])
    assert "synthetic materialization stop" in run_row(client.channel_id, result.json()["run_id"])["failure_reason"]
    assert observations == [(False, True)]
    assert cell not in run_registry.running_cell_ids()


def test_workspace_reclaim_only_removes_owned_run_directory__P0F19(fresh_cwd):
    from types import SimpleNamespace
    from process_manager.finalization import reclaim_run_workspace
    root = fresh_cwd / ".collimate/tmp"
    root.mkdir(parents=True)
    own = root / "ws_abcdefgh_owned"
    sibling = root / "ws_other123_keep"
    own.mkdir()
    sibling.mkdir()
    manager = SimpleNamespace(run_id="abcdefgh-more", workspace_dir=str(own))
    assert reclaim_run_workspace(manager)
    assert not own.exists() and sibling.exists()
    manager.workspace_dir = str(sibling)
    assert not reclaim_run_workspace(manager)
    own.symlink_to(sibling, target_is_directory=True)
    manager.workspace_dir = str(own)
    assert not reclaim_run_workspace(manager)
    assert sibling.exists()


async def test_process_exit_is_observed_before_inherited_pipe_eof__P0F18():
    import asyncio
    import os
    import signal
    import sys
    from process_manager.finalization import wait_process_exit
    # A synthetic child exits while its grandchild still owns the output pipe.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'])",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        await asyncio.wait_for(wait_process_exit(proc), 1.5)
        assert proc.returncode == 0
        assert not proc.stdout.at_eof()
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.communicate()
def test_start_holds_snapshot_and_cancel_are_thread_safe__P0F17_P0F18(monkeypatch):
    import sys
    import threading
    import run_registry

    monkeypatch.setattr(run_registry, "_holds", {})
    for i in range(1000):
        run_registry.reserve(f"fixed-{i}", f"cell-{i}")
    stop = threading.Event()
    old_interval = sys.getswitchinterval()
    def writes():
        while not stop.is_set():
            run_registry.reserve("changing", "dynamic")
            run_registry.release("changing")
    worker = threading.Thread(target=writes)
    sys.setswitchinterval(0.000001)
    worker.start()
    try:
        for _ in range(1000):
            assert "cell-0" in run_registry.held_cell_ids()
            run_registry.cancel_holds()
        assert not run_registry.hold_is_live("fixed-0")
        assert "cell-0" in run_registry.held_cell_ids()
    finally:
        stop.set()
        worker.join(5)
        sys.setswitchinterval(old_interval)
