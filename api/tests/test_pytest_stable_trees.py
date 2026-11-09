"""Stable generator trees and run environments (plan 1105, Phase 2).

A run of one generator version uses one read-only tree,
``.collimate/envs/<gen>-<key16>/``, keyed by the content of its source
snapshot and staged once; uv keys a script's environment on the script's
absolute path, so every run of the version shares one uv environment too.
``env_uses`` records each key's use and, once found, its uv environment;
``/api/storage/envs`` lists, previews and deletes them, and
``/api/storage/uv/prune`` runs uv's own prune while no run is live.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import install_runner
from collimate_server.database import get_db_connection, get_global_db_connection
from collimate_server.models import VirtualFile
from collimate_server.cas import app_store

ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0


def _snapshot(*, library="def helper():\n    return 1\n", entry="print('hi')\n"):
    return ("demo", [
        ("demo/demo.generator", VirtualFile(name="demo.generator", content="id: demo\nentrypoint: demo.py\n")),
        ("demo/demo.py", VirtualFile(name="demo.py", content=entry)),
        ("_demo_shared/__init__.py", VirtualFile(name="__init__.py", content=library)),
    ])


# ---------------------------------------------------------------------------
# The key and the staging
# ---------------------------------------------------------------------------

def test_the_key_follows_the_content_not_the_order(fresh_cwd):
    from collimate_server.run_staging import stable_tree, stable_tree_key
    gen_subdir, writes = _snapshot()
    key = stable_tree_key((gen_subdir, writes), app_store())
    assert stable_tree_key((gen_subdir, list(reversed(writes))), app_store()) == key
    assert stable_tree_key(_snapshot(), app_store()) == key
    tree = stable_tree(_snapshot(), app_store())
    assert tree.name == f"demo-{key[:16]}"
    assert tree.root == str(fresh_cwd / ".collimate" / "envs" / tree.name)
    assert tree.gen_dir == os.path.join(tree.root, "demo")


def test_an_edit_to_a_library_or_a_rename_makes_a_new_key():
    from collimate_server.run_staging import stable_tree_key
    key = stable_tree_key(_snapshot(), app_store())
    assert stable_tree_key(_snapshot(library="def helper():\n    return 2\n"), app_store()) != key
    assert stable_tree_key(_snapshot(entry="print('bye')\n"), app_store()) != key
    gen_subdir, writes = _snapshot()
    renamed = [("_other_shared/__init__.py" if rel.startswith("_demo_shared") else rel, f) for rel, f in writes]
    assert stable_tree_key((gen_subdir, renamed), app_store()) != key


def test_concurrent_first_runs_stage_once(fresh_cwd, monkeypatch):
    import collimate_server.run_staging as run_staging
    original = run_staging.materialize_generator_source
    calls = []

    def slow(snapshot, root, store):
        calls.append(root)
        time.sleep(0.2)
        return original(snapshot, root, store)

    monkeypatch.setattr(run_staging, "materialize_generator_source", slow)
    snapshot = _snapshot()
    tree = run_staging.stable_tree(snapshot, app_store())
    wrote: list[bool] = []
    threads = [threading.Thread(target=lambda: wrote.append(run_staging.stage_stable_tree(snapshot, tree, app_store())))
               for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1 and sorted(wrote) == [False] * 5 + [True]
    assert (Path(tree.root) / ".complete").read_text() == tree.key + "\n"
    assert sorted(os.listdir(fresh_cwd / ".collimate" / "envs")) == [tree.name]


@pytest.mark.skipif(ROOT_USER, reason="root ignores the write bits")
def test_the_tree_is_read_only_and_a_write_to_cwd_fails(fresh_cwd):
    import collimate_server.run_staging as run_staging
    snapshot = _snapshot()
    tree = run_staging.stable_tree(snapshot, app_store())
    run_staging.stage_stable_tree(snapshot, tree, app_store())
    for dirpath, dirnames, filenames in os.walk(tree.root):
        assert stat.S_IMODE(os.stat(dirpath).st_mode) == 0o555, dirpath
        for name in filenames:
            assert stat.S_IMODE(os.stat(os.path.join(dirpath, name)).st_mode) == 0o444, name
    with pytest.raises(PermissionError):
        open(os.path.join(tree.gen_dir, "demo.py"), "a").close()
    done = subprocess.run(
        ["python3", "-c", "open('written.txt', 'w')"], cwd=tree.gen_dir, capture_output=True, text=True)
    assert done.returncode != 0 and "PermissionError" in done.stderr
    assert not os.path.exists(os.path.join(tree.gen_dir, "written.txt"))


def test_the_staging_folder_is_renamed_while_still_writable_then_sealed(fresh_cwd, monkeypatch):
    """macOS refuses to rename a directory its owner cannot write, so the
    staging folder keeps its write bit through the rename and is sealed
    in place afterwards (the v0.1.1 mac release failed every run on it)."""
    import collimate_server.run_staging as run_staging
    seen: list[int] = []
    real_rename = os.rename

    def rename(src, dst):
        seen.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_rename(src, dst)

    monkeypatch.setattr(run_staging.os, "rename", rename)
    snapshot = _snapshot()
    tree = run_staging.stable_tree(snapshot, app_store())
    assert run_staging.stage_stable_tree(snapshot, tree, app_store()) is True
    assert seen and all(mode & stat.S_IWUSR for mode in seen)
    assert stat.S_IMODE(os.stat(tree.root).st_mode) == 0o555
    for dirpath, _dirnames, _filenames in os.walk(tree.root):
        assert stat.S_IMODE(os.stat(dirpath).st_mode) == 0o555, dirpath


def test_a_tree_left_without_complete_is_staged_again(fresh_cwd):
    import collimate_server.run_staging as run_staging
    snapshot = _snapshot()
    tree = run_staging.stable_tree(snapshot, app_store())
    os.makedirs(os.path.join(tree.root, "demo"))
    Path(tree.root, "demo", "demo.py").write_text("half")
    assert run_staging.stage_stable_tree(snapshot, tree, app_store()) is True
    assert Path(tree.gen_dir, "demo.py").read_text() == "print('hi')\n"


def test_startup_removes_staging_trees_and_keeps_the_others(fresh_cwd):
    import collimate_server.readonly_trees as readonly_trees
    import collimate_server.run_staging as run_staging
    envs = fresh_cwd / ".collimate" / "envs"
    staging = envs / ".staging-0123456789abcdef-cafe"
    (staging / "demo").mkdir(parents=True)
    (staging / "demo" / "demo.py").write_text("x")
    readonly_trees.make_readonly(str(staging))
    kept = envs / "demo-0123456789abcdef"
    kept.mkdir()
    assert run_staging.remove_staging_trees() == 1
    assert sorted(os.listdir(envs)) == [kept.name]


# ---------------------------------------------------------------------------
# Rows, sizes and deletion
# ---------------------------------------------------------------------------

@pytest.fixture
def envs(fresh_cwd, monkeypatch):
    """A staged tree recorded in ``env_uses``, a fake uv cache with its
    environment, and uv's ``cache dir`` answering that cache."""
    import collimate_server.env_uses as env_uses
    import collimate_server.run_staging as run_staging
    cache = fresh_cwd / "uv-cache"
    (cache / "archive-v0" / "wheel").mkdir(parents=True)
    shared = cache / "archive-v0" / "wheel" / "module.py"
    shared.write_bytes(b"s" * 40000)
    monkeypatch.setattr(env_uses, "uv_cache_dir", lambda uv, env: os.path.realpath(cache))

    def make(entry="print('hi')\n", *, generator="demo"):
        snapshot = _snapshot(entry=entry)
        tree = run_staging.stable_tree(snapshot, app_store())
        run_staging.stage_stable_tree(snapshot, tree, app_store())
        env_uses.record_use(tree, generator)
        uv_env = cache / "environments-v2" / f"demo-{tree.key[16:32]}"
        (uv_env / "bin").mkdir(parents=True)
        (uv_env / "bin" / "python").symlink_to("/usr/bin/python3")
        (uv_env / "own.bin").write_bytes(b"o" * 30000)
        os.link(shared, uv_env / "linked.py")
        (uv_env / ".lock").write_text("")
        conn = get_global_db_connection()
        try:
            conn.execute("UPDATE env_uses SET uv_env=? WHERE key=?", (str(uv_env), tree.key))
            conn.commit()
        finally:
            conn.close()
        return tree, uv_env

    return make, cache


def _set_last_used(key, when):
    conn = get_global_db_connection()
    try:
        conn.execute("UPDATE env_uses SET last_used_at=? WHERE key=?", (when, key))
        conn.commit()
    finally:
        conn.close()


def test_reclaimable_bytes_leave_out_hardlinked_files(envs):
    import collimate_server.env_uses as env_uses
    make, cache = envs
    tree, uv_env = make()
    (entry,) = env_uses.listing()
    assert entry["key"] == tree.key and entry["generator_id"] == "demo" and entry["live"] is False
    assert entry["tree"] == tree.root and entry["uv_env"] == str(uv_env)
    linked = os.stat(uv_env / "linked.py").st_blocks * 512
    assert linked > 0
    assert entry["bytes"] - entry["reclaimable_bytes"] == linked
    whole = env_uses.measure(tree.root)
    assert whole.bytes == whole.reclaimable_bytes > 0


def test_deletion_skips_live_keys_and_removes_the_others(envs):
    import collimate_server.env_uses as env_uses
    make, _cache = envs
    gone, gone_env = make("print('one')\n")
    live, live_env = make("print('two')\n")
    env_uses.retain(live.key)
    try:
        preview = env_uses.preview("all")
        assert [e["key"] for e in preview["envs"]] == [gone.key]
        assert preview["count"] == 1 and preview["exact"] is True
        assert preview["bytes"] == preview["envs"][0]["reclaimable_bytes"]
        result = env_uses.delete(preview["preview_id"])
    finally:
        env_uses.release(live.key)
    assert result["removed"] == [gone.key] and result["failed"] == []
    assert result["bytes_freed_estimate"] == preview["bytes"]
    assert not os.path.exists(gone.root) and not gone_env.exists()
    assert os.path.isdir(live.root) and live_env.is_dir()
    assert [row["key"] for row in env_uses.rows()] == [live.key]
    assert (_cache / "archive-v0" / "wheel" / "module.py").is_file()


def test_a_key_that_turned_live_after_the_preview_is_kept(envs):
    import collimate_server.env_uses as env_uses
    make, _cache = envs
    tree, uv_env = make()
    preview = env_uses.preview("all")
    env_uses.retain(tree.key)
    try:
        result = env_uses.delete(preview["preview_id"])
    finally:
        env_uses.release(tree.key)
    assert result["removed"] == [] and [f["key"] for f in result["failed"]] == [tree.key]
    assert os.path.isdir(tree.root) and uv_env.is_dir()


@pytest.mark.parametrize("where", ["outside", "nested", "misnamed"])
def test_deletion_refuses_a_uv_env_that_is_not_a_direct_child_of_environments_v2(envs, where):
    import collimate_server.env_uses as env_uses
    make, cache = envs
    tree, uv_env = make()
    target = {
        "outside": cache.parent / "elsewhere" / uv_env.name,
        "nested": uv_env / "inner-0123456789abcdef",
        "misnamed": cache / "environments-v2" / "0123456789abcdef",
    }[where]
    target.mkdir(parents=True)
    (target / "keep").write_text("k")
    conn = get_global_db_connection()
    try:
        conn.execute("UPDATE env_uses SET uv_env=? WHERE key=?", (str(target), tree.key))
        conn.commit()
    finally:
        conn.close()
    result = env_uses.delete(env_uses.preview("all")["preview_id"])
    assert result["removed"] == []
    assert "environments-v2" in result["failed"][0]["reason"] or "named" in result["failed"][0]["reason"]
    assert (target / "keep").is_file() and os.path.isdir(tree.root)


def test_deletion_skips_an_environment_uv_holds(envs):
    import collimate_server.env_uses as env_uses
    make, _cache = envs
    tree, uv_env = make()
    with open(uv_env / ".lock", "r+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        result = env_uses.delete(env_uses.preview("all")["preview_id"])
    assert result["removed"] == [] and result["failed"] == [{"key": tree.key, "reason": "uv is using its environment"}]
    assert uv_env.is_dir() and os.path.isdir(tree.root)


def test_unused_days_selects_by_last_use(envs):
    import collimate_server.env_uses as env_uses
    make, _cache = envs
    old, _ = make("print('old')\n")
    recent, _ = make("print('recent')\n")
    _set_last_used(old.key, "2026-01-01T00:00:00+00:00")
    preview = env_uses.preview("unused", 30)
    assert [e["key"] for e in preview["envs"]] == [old.key]
    assert env_uses.delete(preview["preview_id"])["removed"] == [old.key]
    assert os.path.isdir(recent.root)


def test_a_preview_is_used_once_and_expires(envs, monkeypatch):
    import collimate_server.env_uses as env_uses
    make, _cache = envs
    make()
    preview = env_uses.preview("all")
    later = time.monotonic() + env_uses.PREVIEW_SECONDS + 1
    monkeypatch.setattr(env_uses.time, "monotonic", lambda: later)
    with pytest.raises(LookupError):
        env_uses.delete(preview["preview_id"])
    with pytest.raises(LookupError):
        env_uses.delete(preview["preview_id"])


def test_environment_of_accepts_only_a_uv_script_environment(tmp_path):
    from collimate_server.env_uses import environment_of
    cache = str(tmp_path / "cache")
    os.makedirs(os.path.join(cache, "environments-v2"))
    good = os.path.join(cache, "environments-v2", "agent-0123456789abcdef", "bin", "python")
    assert environment_of(good, cache) == os.path.dirname(os.path.dirname(good))
    assert environment_of("/usr/bin/python3", cache) is None
    assert environment_of(os.path.join(cache, "environments-v2", "0123456789abcdef", "bin", "python"), cache) is None
    assert environment_of(os.path.join(cache, "builds-v0", "agent-0123456789abcdef", "bin", "python"), cache) is None
    assert environment_of("", cache) is None


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

def test_the_envs_routes_list_preview_and_delete(client, envs):
    make, _cache = envs
    tree, _uv_env = make()
    listed = client.get("/api/storage/envs").json()["envs"]
    assert [(e["key"], e["live"]) for e in listed] == [(tree.key, False)]
    assert client.post("/api/storage/envs/preview", json={"mode": "unused"}).status_code == 400
    assert client.post("/api/storage/envs/preview", json={"mode": "some"}).status_code == 422
    preview = client.post("/api/storage/envs/preview", json={"mode": "unused", "days": 0}).json()
    assert preview["count"] == 1
    deleted = client.post("/api/storage/envs/delete", json={"preview_id": preview["preview_id"]})
    assert deleted.status_code == 200 and deleted.json()["removed"] == [tree.key]
    again = client.post("/api/storage/envs/delete", json={"preview_id": preview["preview_id"]})
    assert again.status_code == 404
    assert client.get("/api/storage/envs").json() == {"envs": []}


def test_uv_prune_is_refused_while_a_run_is_live_and_runs_uvs_own_prune_otherwise(client, monkeypatch):
    import collimate_server.env_uses as env_uses
    from collimate_server.run_registry import active_managers
    from fake_manager import FakeManager
    ran: list = []

    def fake_run(argv, **kwargs):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="Removed 3 files\n", stderr="")

    monkeypatch.setattr(env_uses.subprocess, "run", fake_run)
    active_managers["live-run"] = FakeManager(run_id="live-run")
    try:
        refused = client.post("/api/storage/uv/prune")
    finally:
        active_managers.pop("live-run", None)
    assert refused.status_code == 409 and ran == []
    done = client.post("/api/storage/uv/prune")
    assert done.status_code == 200 and done.json() == {"ok": True, "output": "Removed 3 files"}
    assert [argv[1:] for argv in ran] == [["cache", "prune"]]


# ---------------------------------------------------------------------------
# Live: real runs through uv
# ---------------------------------------------------------------------------

_PROBE = (
    "# /// script\n"
    "# requires-python = \">=3.11\"\n"
    "# dependencies = []\n"
    "# ///\n"
    "import os, sys\n"
    "import gen\n"
    "async def run():\n"
    "    try:\n"
    "        open('written.txt', 'w').close()\n"
    "        outcome = 'wrote'\n"
    "    except PermissionError:\n"
    "        outcome = 'refused'\n"
    "    gen.update_file('cwd.txt', f'{outcome}\\n{os.getcwd()}\\n{sys.executable}\\n')\n"
    "if __name__ == '__main__':\n"
    "    gen.main(run)\n"
)


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH; this drives real `uv run` generators")
@pytest.mark.skipif(ROOT_USER, reason="root ignores the write bits")
def test_two_runs_of_one_version_share_one_tree_and_one_uv_environment(client, monkeypatch, tmp_path):
    """Both runs run in the same read-only folder (a write to cwd fails),
    on the same uv environment, which ``uv python find --script`` names
    after the runs, stably; ``env_uses`` holds one row naming it."""
    import collimate_server.env_uses as env_uses
    from collimate_server.process_manager.native_tools import native_uv
    from collimate_server.cas import channel_store
    from run_spawn import run_row, wait_spawned

    # Runs inherit XDG_CACHE_HOME: their uv cache is this test's, never the machine's.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    runner, _gen = install_runner(generator="treeprobe", runner="treeprobe", entrypoint_body=_PROBE)
    outputs = []
    for _ in range(2):
        cell_id = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]
        response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cell_id})
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        wait_spawned(run_id)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and run_row(client.channel_id, run_id)["status"] == "running":
            time.sleep(0.2)
        assert run_row(client.channel_id, run_id)["status"] == "completed", run_row(client.channel_id, run_id)
        conn = get_db_connection(client.channel_id)
        try:
            (blob,) = conn.execute(
                "SELECT cf.blob_hash FROM cell_files cf JOIN cells c ON c.id=cf.cell_id "
                "WHERE c.run_id=? AND cf.filepath='cwd.txt'", (run_id,)).fetchone()
        finally:
            conn.close()
        outputs.append(channel_store(client.channel_id).read_blob(blob).decode().splitlines())
        (row,) = env_uses.rows()
        while time.monotonic() < deadline and not env_uses.rows()[0]["uv_env"]:
            time.sleep(0.2)
    (row,) = env_uses.rows()
    assert [o[0] for o in outputs] == ["refused", "refused"]
    assert outputs[0][1] == outputs[1][1] == os.path.join(row["tree"], "treeprobe")
    assert outputs[0][2] == outputs[1][2]
    assert row["uv_env"] and outputs[0][2] == os.path.join(row["uv_env"], "bin", "python")
    assert Path(row["uv_env"]).parent == Path(tmp_path / "xdg-cache" / "uv" / "environments-v2").resolve()
    entry = os.path.join(row["tree"], "treeprobe", "treeprobe.py")
    env = env_uses.uv_query_env()
    found = [subprocess.run([native_uv(), "python", "find", "--script", entry], env=env,
                            capture_output=True, text=True, check=True).stdout.strip() for _ in range(2)]
    assert found == [outputs[0][2]] * 2
    assert sorted(os.listdir(Path(row["uv_env"]).parent)) == [Path(row["uv_env"]).name]
