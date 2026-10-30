"""Reclamation of a finished run's temp app tree (``.collimate/tmp/app_*``).

Every run mkdtemps a copy of the generator tree plus a separate workspace
beside it (routes/runs/create.py). The app tree must be reclaimed so
``.collimate/tmp`` does not accumulate one generator-tree copy per run — and since uv keys a
script's env on the entrypoint's ABSOLUTE path, every fresh tree also minted a
fresh env under ~/.cache/uv (that cache is uv's to prune, not ours; these tests
only pin the part we own).

What's pinned here:
  * _reap_process deletes the temporary app tree and workspace of a
    finished run, preserving the committed cell rows.
  * a force-stopped run — cleanup task cancelled, no finalization — is
    reclaimed too, since that's the path a SIGKILL takes.
  * the prefix + containment guards: never ``.collimate/tmp`` itself, never a
    tree that holds the workspace.
  * stop_all_runs reclaims as well, because on server shutdown the lifespan
    waits for each run's reaper only until it has dropped the run from the
    registry, and the event loop's close can cut the reaper off before its
    own reclaim (a run whose cleanup outlived the bounded wait included).
  * the same thing end-to-end through a real ``uv run`` generator, so the
    layout under test is create.py's own rather than a reconstruction of it.
"""
from __future__ import annotations


from fake_manager import FakeManager
import asyncio
import glob
import json
import shutil
import signal as _signal
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from conftest import install_runner
from database import get_db_connection


CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:run-tmp-reclaim:channel"))


def _make_run_tree(cwd: Path, run_id: str) -> tuple[Path, Path, Path]:
    """Lay out one run's temp dirs exactly as create.py does: an ``app_*``
    tree holding the generator's own subdir (next to an ``_*`` shared sibling
    package), and a sibling ``ws_*`` workspace with a hydrated input.

    Returns ``(tree, gen_app_dir, workspace)`` — note ``gen_app_dir`` is what
    a ProcessManager gets as ``app_dir``, one level below the tree.
    """
    tmp_root = cwd / ".collimate" / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tree = Path(tempfile.mkdtemp(prefix=f"app_{run_id[:8]}_", dir=tmp_root))
    gen = tree / "demo_gen"
    gen.mkdir()
    (gen / "choice.generator").write_text(
        "id: demo_gen\nentrypoint: demo_gen.py\n"
    )
    (gen / "demo_gen.py").write_text("print('demo_gen ran')\n")
    shared = tree / "_demo_shared"
    shared.mkdir()
    (shared / "__init__.py").write_text("")
    ws = Path(tempfile.mkdtemp(prefix=f"ws_{run_id[:8]}_", dir=tmp_root))
    (ws / "input.txt").write_text("alpha\n")
    return tree, gen, ws


def _seed_run_with_cell(run_id: str, cell_id: str) -> None:
    """A finished run plus the cell version it committed — the data that must
    outlive the app tree."""
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'completed')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
            "version_name, source_type) "
            "VALUES (?, ?, 'demo_gen', 1, 'Output', 'generator')",
            (cell_id, run_id),
        )
        conn.commit()
    finally:
        conn.close()


class _ExitedProcess:
    """A process _reap_process can await that has already exited."""
    pid = -1
    returncode = 0

    async def wait(self):
        return self.returncode


async def test_completed_run_app_tree_goes_workspace_and_cells_stay(
    fresh_cwd, monkeypatch,
):
    """The reaped run's ``app_*`` tree is deleted; its ``ws_*`` workspace, the
    tmp root, and its committed cell row all survive."""
    import routes.runs.helpers as helpers
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED
    from run_registry import active_managers

    monkeypatch.setattr(helpers, "_reap_run_containers", lambda rid: None)

    run_id = str(uuid.uuid4())
    cell_id = str(uuid.uuid4())
    tree, gen, ws = _make_run_tree(fresh_cwd, run_id)
    _seed_run_with_cell(run_id, cell_id)

    manager = ProcessManager(
        channel=CHANNEL,
        run_id=run_id,
        app_dir=str(gen),          # the generator's subdir, as create.py passes
        workspace_dir=str(ws),
        entrypoint="demo_gen.py",
        generator_name="demo_gen",
        cell_id=cell_id,
        permissions=UNRESTRICTED,
    )
    manager.process = _ExitedProcess()
    active_managers[run_id] = manager
    try:
        await helpers._reap_process(run_id, manager)
    finally:
        active_managers.clear()

    assert not tree.exists(), (
        f"app tree {tree} should be reclaimed on reap (leaks one generator-"
        f"tree copy per run otherwise)"
    )
    assert ws.is_dir(), f"workspace {ws} must never be touched by the reclaim"
    assert (ws / "input.txt").read_text() == "alpha\n"
    assert (fresh_cwd / ".collimate" / "tmp").is_dir(), (
        "the shared tmp root must survive — only the run's own tree goes"
    )

    conn = get_db_connection(CHANNEL)
    try:
        cell = conn.execute(
            "SELECT version_name FROM cells WHERE id=?", (cell_id,),
        ).fetchone()
        run = conn.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert cell and cell["version_name"] == "Output"
    assert run and run["status"] == "completed"


async def test_force_stopped_run_is_reclaimed_too(fresh_cwd, monkeypatch):
    """A SIGKILLed run's cleanup task never finishes normally — _reap_process
    swallows the CancelledError, and must still reclaim the tree."""
    import routes.runs.helpers as helpers
    from run_registry import active_managers

    monkeypatch.setattr(helpers, "_reap_run_containers", lambda rid: None)

    run_id = str(uuid.uuid4())
    tree, gen, ws = _make_run_tree(fresh_cwd, run_id)

    async def _never():
        await asyncio.sleep(3600)

    cleanup = asyncio.create_task(_never())
    cleanup.cancel()

    manager = FakeManager(
        run_id=run_id, channel=CHANNEL, app_dir=str(gen), workspace_dir=str(ws),
        process=None, _cleanup_task=cleanup, cell_id=None,
    )
    active_managers[run_id] = manager
    try:
        await helpers._reap_process(run_id, manager)
    finally:
        active_managers.clear()

    assert not tree.exists(), "a force-stopped run must reclaim its tree too"
    assert ws.is_dir()


def test_reclaim_refuses_the_tmp_root_and_a_tree_holding_the_workspace(fresh_cwd):
    """Both guards. Without the ``app_`` prefix check, a manager whose app_dir
    sits directly in the tmp root would take EVERY run's tree with it; without
    the containment check, a workspace inside the tree would be deleted."""
    import routes.runs.helpers as helpers

    tmp_root = fresh_cwd / ".collimate" / "tmp"
    tmp_root.mkdir(parents=True)
    other = tmp_root / "app_other_run_keepme"
    other.mkdir()

    helpers._reclaim_run_app_dir(FakeManager(
        run_id="r1", app_dir=str(tmp_root), workspace_dir="",
    ))
    assert tmp_root.is_dir()
    assert other.is_dir(), "another live run's tree must not be collateral"

    tree = tmp_root / "app_selfhosting"
    (tree / "demo_gen").mkdir(parents=True)
    ws = tree / "ws_inside"
    ws.mkdir()
    (ws / "keep.txt").write_text("k\n")

    helpers._reclaim_run_app_dir(FakeManager(
        run_id="r2", app_dir=str(tree / "demo_gen"), workspace_dir=str(ws),
    ))
    assert (ws / "keep.txt").read_text() == "k\n"
    assert tree.is_dir()


def test_reclaim_accepts_an_app_dir_that_is_the_tree_itself(fresh_cwd):
    """A manager pointed straight at the mkdtemp'd dir (no generator subdir)
    is still reclaimed — the tree is identified by prefix, not by depth."""
    import routes.runs.helpers as helpers

    tmp_root = fresh_cwd / ".collimate" / "tmp"
    tmp_root.mkdir(parents=True)
    tree = tmp_root / "app_flat_run"
    tree.mkdir()
    (tree / "demo_gen.py").write_text("x\n")

    helpers._reclaim_run_app_dir(FakeManager(
        run_id="r3", app_dir=str(tree), workspace_dir=str(tmp_root / "ws_flat"),
    ))
    assert not tree.exists()
    assert tmp_root.is_dir()


_RECLAIM_PROBE = (
    "# /// script\n"
    "# requires-python = \">=3.11\"\n"
    "# dependencies = []\n"
    "# ///\n"
    "import asyncio\n"
    "from pathlib import Path\n"
    "import gen\n"
    "async def run():\n"
    "    async with gen.live_files(interval=0.1) as mirror:\n"
    "        async def on_turn(turn):\n"
    "            turn.append('user', turn.user_msg)\n"
    "            turn.append('assistant', 'ready')\n"
    "            (Path(gen.workspace_path()) / 'out.txt').write_text('produced\\n')\n"
    "            await mirror.flush()\n"
    "            release = Path(gen.workspace_path()) / '.collrun' / 'reclaim-probe-release'\n"
    "            for _ in range(600):\n"
    "                if release.exists():\n"
    "                    break\n"
    "                await asyncio.sleep(0.1)\n"
    "            else:\n"
    "                raise TimeoutError('test did not release the chat run')\n"
    "            gen.session.request_finish()\n"
    "        await gen.serve_chat(on_turn, threads_dir=gen.run_path('chat'),\n"
    "                             pointer_file=gen.run_path('chat/conversation.chat'),\n"
    "                             initial_message='start')\n"
    "if __name__ == '__main__':\n"
    "    gen.main(run)\n"
)


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not available on PATH; this drives a real `uv run` generator",
)
def test_real_run_end_to_end_leaves_no_app_tree(client):
    """Live chat and mirrored output publish, private ownership does not,
    and finalization reclaims both temporary trees without losing output."""
    runner, _gen = install_runner(
        generator="reclaimprobe", runner="reclaimprobe",
        entrypoint_body=_RECLAIM_PROBE,
        gen_manifest_extra="ui_files: [.chat, .json]\n",
    )
    cell_id = client.post(f"/api/stacks/{client.channel_id}/create").json()[
        "main_stacks"][-1]["cells"][0]["id"]
    r = client.post(
        f"/api/channels/{client.channel_id}/runs",
        json={"runner": runner, "cell_id": cell_id},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    from run_spawn import run_row, wait_spawned
    from persistence import snapshot_files
    from run_workspace_cleanup import MARKER
    wait_spawned(run_id)

    label = f"{runner}-{run_id[:8]}"
    folder = f"_runs/{label}"
    expected_live = {
        f"{folder}/{label}.run",
        f"{folder}/chat/conversation.chat",
        f"{folder}/chat/main.json",
        "out.txt",
    }

    def accepted_paths():
        conn = get_db_connection(client.channel_id)
        try:
            return {row["path"] for row in snapshot_files(conn, cell_id)}
        finally:
            conn.close()

    def recent_events():
        conn = get_db_connection(client.channel_id)
        try:
            rows = conn.execute(
                "SELECT event_type, payload FROM run_events WHERE run_id=? ORDER BY id DESC LIMIT 5",
                (run_id,),
            ).fetchall()
            return [(row["event_type"], row["payload"]) for row in rows]
        finally:
            conn.close()

    workspace = None
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            workspaces = glob.glob(f".collimate/tmp/ws_{run_id[:8]}_*")
            paths = accepted_paths()
            if workspaces:
                workspace = Path(workspaces[0])
            if workspace is not None and expected_live.issubset(paths):
                break
            if run_row(client.channel_id, run_id)["status"] != "running":
                pytest.fail(f"chat run ended before publishing its files: {paths}; {recent_events()}")
            time.sleep(0.2)
        assert workspace is not None, f"run workspace missing: {recent_events()}"
        assert expected_live.issubset(accepted_paths()), (
            f"chat files did not publish: {accepted_paths()}; {recent_events()}"
        )
        assert accepted_paths() == expected_live
        receipt = json.loads((workspace / MARKER).read_text())
        assert receipt["run_id"] == run_id, "crash cleanup must retain its ownership receipt"
        assert MARKER not in accepted_paths()
    finally:
        if workspace is not None and workspace.exists():
            (workspace / ".collrun" / "reclaim-probe-release").write_text("release\n")

    # The run's status flips terminal during finalization, a beat BEFORE the
    # reaper task gets to the reclaim — so wait for the end, then poll for
    # the tree instead of racing it.
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline and run_row(client.channel_id, run_id)["status"] == "running":
        time.sleep(0.2)
    tree_glob = f".collimate/tmp/app_{run_id[:8]}_*"
    while time.monotonic() < deadline and glob.glob(tree_glob):
        time.sleep(0.2)
    assert not glob.glob(tree_glob), (
        f"app tree still present after the run ended: {glob.glob(tree_glob)}"
    )

    ws = glob.glob(f".collimate/tmp/ws_{run_id[:8]}_*")
    assert not ws, f"finished run workspace still present: {ws}"

    conn = get_db_connection(client.channel_id)
    try:
        row = conn.execute(
            "SELECT cf.filepath, cf.blob_hash FROM cell_files cf JOIN cells c ON c.id=cf.cell_id "
            "WHERE c.run_id=? AND cf.filepath='out.txt'",
            (run_id,),
        ).fetchone()
        status = conn.execute(
            "SELECT status FROM runs WHERE id=?", (run_id,),
        ).fetchone()
        logged = {entry["filepath"] for entry in conn.execute(
            "SELECT filepath FROM run_artifacts WHERE run_id=? AND category='log'",
            (run_id,),
        )}
    finally:
        conn.close()
    assert status and status["status"] == "completed", status and status["status"]
    assert row, "the generator's output must survive as committed cell data"
    from cas import read_blob
    assert read_blob(row["blob_hash"]) == b"produced\n"
    assert accepted_paths() == {"out.txt"}, "private metadata and closed run UI must leave the cell"
    assert expected_live - {"out.txt"} <= logged, "chat files must survive in run logs"


class _LiveProcess:
    """Looks alive to stop_all_runs until the faked SIGTERM sets returncode."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None


class _StubManager(FakeManager):
    """Walks like a ProcessManager for the fields stop_all_runs reaches, plus
    the two dirs the reclaim reads."""

    def __init__(self, channel, run_id, process, app_dir, workspace_dir):
        super().__init__()
        self.channel = channel
        self.run_id = run_id
        self.process = process
        self.app_dir = app_dir
        self.workspace_dir = workspace_dir
        self._ipc_server = None

    def _broadcast_run_status(self, status):
        pass


async def test_stop_all_runs_reclaims_app_trees(fresh_cwd, monkeypatch):
    """Server shutdown goes through stop_all_runs, waits for each run's
    reaper only until it has dropped the run from the registry, and lets the
    process exit, so a reaper's own reclaim can be cut off — the ladder has
    to reclaim the trees itself, workspaces untouched."""
    from sessions import terminal as terminal_sessions
    import routes.runs.helpers as helpers
    import routes.runs.teardown as teardown
    from routes.runs import stop_all_runs
    from run_registry import active_managers

    run_id = str(uuid.uuid4())
    tree, gen, ws = _make_run_tree(fresh_cwd, run_id)
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute(
            "INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    manager = _StubManager(
        CHANNEL, run_id, _LiveProcess(pid=4242), str(gen), str(ws),
    )

    def fake_signal_process_group(pid, sig):
        if sig == _signal.SIGTERM and pid == manager.process.pid:
            manager.process.returncode = -_signal.SIGTERM
    monkeypatch.setattr(teardown, "signal_process_group", fake_signal_process_group)
    monkeypatch.setattr(helpers, "_reap_run_containers", lambda rid: None)
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)


    active_managers.clear()
    active_managers[run_id] = manager
    try:
        await stop_all_runs(grace_seconds=0.3)
    finally:
        active_managers.clear()

    assert not tree.exists(), (
        "stop_all_runs must reclaim the app tree — on shutdown nothing else will"
    )
    assert (ws / "input.txt").read_text() == "alpha\n"


async def test_stop_all_runs_reclaims_the_tree_of_a_run_whose_cleanup_outlived_the_wait__F0323(
        fresh_cwd, monkeypatch):
    """A run whose run-end cleanup outlasts the bounded wait still has its
    app tree reclaimed: its process is gone and no cleanup step reads the
    tree, and at shutdown nothing else ever reclaims it. The cleanup itself
    is left running."""
    from sessions import terminal as terminal_sessions
    import routes.runs.helpers as helpers
    import routes.runs.teardown as teardown
    from routes.runs import stop_all_runs
    from run_registry import active_managers

    monkeypatch.setattr(teardown, "CLEANUP_WAIT_SECONDS", 0.2)
    run_id = str(uuid.uuid4())
    tree, gen, ws = _make_run_tree(fresh_cwd, run_id)
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')", (run_id,))
        conn.commit()
    finally:
        conn.close()
    manager = _StubManager(CHANNEL, run_id, _LiveProcess(pid=4343), str(gen), str(ws))
    forever = asyncio.Event()
    manager._cleanup_task = asyncio.ensure_future(forever.wait())

    def fake_signal_process_group(pid, sig):
        if sig == _signal.SIGTERM and pid == manager.process.pid:
            manager.process.returncode = -_signal.SIGTERM
    monkeypatch.setattr(teardown, "signal_process_group", fake_signal_process_group)
    monkeypatch.setattr(helpers, "_reap_run_containers", lambda rid: None)
    monkeypatch.setattr(terminal_sessions, "shutdown_all_sessions", lambda: 0)


    active_managers.clear()
    active_managers[run_id] = manager
    try:
        await stop_all_runs(grace_seconds=0.3)
        assert not manager._cleanup_task.done()
    finally:
        active_managers.clear()
        forever.set()
        await manager._cleanup_task

    assert not tree.exists()
    assert (ws / "input.txt").read_text() == "alpha\n"
