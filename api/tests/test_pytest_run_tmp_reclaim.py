"""Run directories under ``.collimate/tmp`` are workspaces only.

Every run gets a fresh workspace (``ws_<run8>_*``: hydrated inputs and its
output) and runs its generator from that version's stable, read-only tree
under ``.collimate/envs/`` (``run_staging.stage_stable_tree``), shared by every
run of the version; no run makes a tree of its own. What's pinned here:

  * the reaper of a finished run, and of a force-stopped one, releases the
    run's stable tree (its live count in ``env_uses``), hands a native run's
    launch to the uv environment lookup, and removes neither the tree nor
    the workspace (finalization reclaims the workspace; nothing per run
    reclaims the tree);
  * stop_all_runs leaves both alone as well;
  * end to end through a real ``uv run`` generator: the workspace is
    reclaimed at run end without losing output, the stable tree stays whole
    and read-only, and nothing named ``app_*`` appears under tmp.
"""
from __future__ import annotations


from fake_manager import FakeManager
import asyncio
import glob
import json
import shutil
import signal as _signal
import stat
import time
import uuid
from pathlib import Path

import pytest

from conftest import install_runner
from collimate_server.database import get_db_connection


CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:run-tmp-reclaim:channel"))
KEY = "0123456789abcdef" * 4


def _make_run_dirs(cwd: Path, run_id: str) -> tuple[Path, Path, Path]:
    """One run's directories as create.py lays them out: a stable tree
    holding the generator's own folder (beside a ``_*`` library), and a
    ``ws_*`` workspace with a hydrated input. Returns ``(tree, gen_dir,
    workspace)``; ``gen_dir`` is what a ProcessManager gets as ``app_dir``."""
    tree = cwd / ".collimate" / "envs" / f"demo_gen-{KEY[:16]}"
    gen = tree / "demo_gen"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "choice.generator").write_text("id: demo_gen\nentrypoint: demo_gen.py\n")
    (gen / "demo_gen.py").write_text("print('demo_gen ran')\n")
    (tree / "_demo_shared").mkdir(exist_ok=True)
    (tree / "_demo_shared" / "__init__.py").write_text("")
    tmp_root = cwd / ".collimate" / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    ws = tmp_root / f"ws_{run_id[:8]}_probe"
    ws.mkdir()
    (ws / "input.txt").write_text("alpha\n")
    return tree, gen, ws


class _ExitedProcess:
    """A process _reap_process can await that has already exited."""
    pid = -1
    returncode = 0

    async def wait(self):
        return self.returncode


@pytest.fixture
def lookups(monkeypatch):
    """The uv environment lookups the reaper starts, recorded instead of run."""
    import collimate_server.env_uses as env_uses
    import collimate_server.routes.runs.helpers as helpers
    calls: list = []
    monkeypatch.setattr(env_uses, "resolve_uv_env", lambda *args: calls.append(args))
    monkeypatch.setattr(helpers, "_reap_run_containers", lambda rid: None)
    return calls


async def test_the_reaper_releases_the_stable_tree_and_removes_no_directory(fresh_cwd, lookups):
    """A finished native run: its key leaves the live set, its launch goes to
    the uv environment lookup, and the tree and the workspace are still
    there."""
    import collimate_server.env_uses as env_uses
    import collimate_server.routes.runs.helpers as helpers
    from collimate_server.process_manager import ProcessManager
    from collimate_server.runner_permissions import UNRESTRICTED
    from collimate_server.run_registry import active_managers

    run_id = str(uuid.uuid4())
    tree, gen, ws = _make_run_dirs(fresh_cwd, run_id)
    manager = ProcessManager(
        channel=CHANNEL, run_id=run_id, app_dir=str(gen), app_root_dir=str(tree),
        workspace_dir=str(ws), entrypoint="demo_gen.py", generator_name="demo_gen",
        permissions=UNRESTRICTED, env_key=KEY,
    )
    manager.native_launch = ("uv", str(gen / "demo_gen.py"), {"HOME": "/h"})
    manager.process = _ExitedProcess()
    env_uses.retain(KEY)
    active_managers[run_id] = manager
    try:
        await helpers._reap_process(run_id, manager)
        await asyncio.sleep(0.05)
    finally:
        active_managers.clear()

    assert KEY not in env_uses.live_keys()
    assert lookups == [(KEY, "uv", str(gen / "demo_gen.py"), {"HOME": "/h"})]
    assert (gen / "demo_gen.py").is_file() and (tree / "_demo_shared").is_dir()
    assert (ws / "input.txt").read_text() == "alpha\n"


async def test_a_force_stopped_run_releases_its_tree_too(fresh_cwd, lookups):
    """A SIGKILLed run's cleanup task never finishes normally (_reap_process
    swallows the CancelledError), and the reaper still releases the tree; a
    run that never spawned natively starts no lookup."""
    import collimate_server.env_uses as env_uses
    import collimate_server.routes.runs.helpers as helpers
    from collimate_server.run_registry import active_managers

    run_id = str(uuid.uuid4())
    tree, gen, ws = _make_run_dirs(fresh_cwd, run_id)

    async def _never():
        await asyncio.sleep(3600)

    cleanup = asyncio.create_task(_never())
    cleanup.cancel()
    manager = FakeManager(
        run_id=run_id, channel=CHANNEL, app_dir=str(gen), workspace_dir=str(ws),
        process=None, _cleanup_task=cleanup, cell_id=None, env_key=KEY, native_launch=None,
    )
    env_uses.retain(KEY)
    active_managers[run_id] = manager
    try:
        await helpers._reap_process(run_id, manager)
    finally:
        active_managers.clear()

    assert KEY not in env_uses.live_keys()
    assert lookups == []
    assert gen.is_dir() and ws.is_dir()


def test_two_runs_of_one_key_keep_it_live_until_both_release():
    import collimate_server.env_uses as env_uses
    key = uuid.uuid4().hex * 2
    env_uses.retain(key)
    env_uses.retain(key)
    env_uses.release(key)
    assert key in env_uses.live_keys()
    env_uses.release(key)
    assert key not in env_uses.live_keys()


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
def test_real_run_end_to_end_reclaims_its_workspace_and_keeps_its_stable_tree(client):
    """Live chat and mirrored output publish, private ownership does not;
    finalization reclaims the workspace without losing output, the run
    makes no tree of its own under tmp, and its generator version's stable
    tree stays, whole and read-only, released by the reaper."""
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
    from collimate_server.persistence import snapshot_files
    from collimate_server.run_workspace_cleanup import MARKER
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
    import collimate_server.env_uses as env_uses
    (row,) = env_uses.rows()
    while time.monotonic() < deadline and row["key"] in env_uses.live_keys():
        time.sleep(0.2)
    assert row["key"] not in env_uses.live_keys(), "the reaper releases the run's stable tree"
    assert row["generator_id"] == "reclaimprobe"
    tree = Path(row["tree"])
    assert tree.parent == (Path(".collimate") / "envs").resolve()
    assert (tree / ".complete").is_file() and (tree / "reclaimprobe" / "reclaimprobe.py").is_file()
    assert stat.S_IMODE(tree.stat().st_mode) == 0o555
    assert [path.name for path in Path(".collimate/tmp").iterdir() if path.name.startswith("app_")] == []

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
    from collimate_server.cas import channel_store
    assert channel_store(client.channel_id).read_blob(row["blob_hash"]) == b"produced\n"
    assert accepted_paths() == {"out.txt"}, "private metadata and closed run UI must leave the cell"
    assert expected_live - {"out.txt"} <= logged, "chat files must survive in run logs"


class _LiveProcess:
    """Looks alive to stop_all_runs until the faked SIGTERM sets returncode."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None


class _StubManager(FakeManager):
    """Walks like a ProcessManager for the fields stop_all_runs reaches."""

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


async def test_stop_all_runs_leaves_the_stable_tree_and_the_workspace(fresh_cwd, monkeypatch):
    """Server shutdown goes through stop_all_runs: it stops the run and
    removes no directory, the stable tree (shared with every run of its
    version) and the workspace (finalization's, or a later start's) alike."""
    from collimate_server.sessions import terminal as terminal_sessions
    import collimate_server.routes.runs.helpers as helpers
    import collimate_server.routes.runs.teardown as teardown
    from collimate_server.routes.runs import stop_all_runs
    from collimate_server.run_registry import active_managers

    run_id = str(uuid.uuid4())
    tree, gen, ws = _make_run_dirs(fresh_cwd, run_id)
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'x', 'running')", (run_id,))
        conn.commit()
    finally:
        conn.close()
    manager = _StubManager(CHANNEL, run_id, _LiveProcess(pid=4242), str(gen), str(ws))

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

    assert (gen / "demo_gen.py").is_file() and (tree / "_demo_shared").is_dir()
    assert (ws / "input.txt").read_text() == "alpha\n"
