"""Plan 1105 P6: the storage overview, the size cache, the manage listing
and the maintenance routes (S8).

The survey's machine-wide parts (uv's cache, the agent adapters, Docker)
are stubbed: a test never walks this machine's caches or reads its daemon.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.real_channel_homes


@pytest.fixture
def machine_parts(monkeypatch):
    """Stub the parts every server on the machine shares."""
    import collimate_server.storage_survey as storage_survey
    monkeypatch.setitem(storage_survey.PARTS, "uv_cache", lambda stop: {"path": "/uv", "bytes": 11})
    monkeypatch.setitem(storage_survey.PARTS, "adapters", lambda stop: {"path": "/adapters", "bytes": 22})
    monkeypatch.setitem(storage_survey.PARTS, "docker", lambda stop: {"available": False, "reason": "stubbed"})
    return storage_survey


def _create(client, name: str) -> str:
    response = client.post("/api/channels", params={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _surveyed(client, *, timeout: float = 30.0) -> dict:
    """Refresh and wait for the survey: the overview after it."""
    assert client.post("/api/storage/refresh").status_code == 202
    return _settled(client, timeout=timeout)


def _settled(client, *, timeout: float = 30.0) -> dict:
    """Wait for the running survey: the overview after it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = client.get("/api/storage").json()
        if answer["survey"]["state"] != "running":
            assert answer["survey"]["state"] == "idle", answer["survey"]
            return answer
        time.sleep(0.02)
    raise AssertionError("the survey did not finish")


def _du(path: Path) -> int:
    """GNU du's allocated bytes: every inode once, no symlink followed."""
    return int(subprocess.run(["du", "-s", "-B1", str(path)], capture_output=True, text=True,
                              check=True).stdout.split()[0])


def _allocated(path: Path) -> int:
    """Each inode under ``path`` once, by hand: ``st_blocks * 512``."""
    seen, total = set(), 0
    for found in [path, *path.rglob("*")]:
        info = os.lstat(found)
        if (info.st_dev, info.st_ino) not in seen:
            seen.add((info.st_dev, info.st_ino))
            total += info.st_blocks * 512
    return total


# -- measuring ---------------------------------------------------------------

def test_sizes_are_exact_on_a_hand_built_tree_with_a_hardlink_counted_once(fresh_cwd):
    from collimate_server.storage_survey import measure
    tree = fresh_cwd / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "one.bin").write_bytes(b"x")
    (tree / "a" / "five-k.bin").write_bytes(os.urandom(5000))
    (tree / "a" / "b" / "seventy-k.bin").write_bytes(os.urandom(70_000))
    os.link(tree / "a" / "b" / "seventy-k.bin", tree / "linked.bin")
    outside = fresh_cwd / "outside.bin"
    outside.write_bytes(os.urandom(200_000))
    os.symlink(outside, tree / "to-outside")
    (tree / "empty").mkdir()

    size = measure([tree])
    assert size.bytes == _allocated(tree) == _du(tree)
    assert size.files == 3
    seventy = os.lstat(tree / "linked.bin").st_blocks * 512
    assert seventy >= 70_000
    # Both links are inside: removing the tree frees the file.
    assert size.reclaimable_bytes == size.bytes
    # Only one link inside: removing ``a`` frees all but that file.
    part = measure([tree / "a"])
    assert part.bytes == _du(tree / "a")
    assert part.reclaimable_bytes == part.bytes - seventy
    assert measure([fresh_cwd / "missing"]).bytes == 0


def test_a_channel_folder_is_split_into_its_parts(fresh_cwd):
    from collimate_server.storage_survey import measure_channel_folder
    folder = fresh_cwd / "channel"
    for name, size in (("channel.db", 8192), ("channel.db-wal", 4096), ("channel.json", 100)):
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(b"\0" * size)
    for name in ("git/objects/aa", "git-rescue/x", "objects/ab", "objects.trash/sweep/ab", "cell-state/c1"):
        (folder / name).mkdir(parents=True)
        (folder / name / "body").write_bytes(os.urandom(3000))
    entry = measure_channel_folder(folder)
    assert entry["bytes"] == _du(folder)
    assert entry["files"] == 8
    parts = entry["parts"]
    assert set(parts) == {"channel_db", "git", "objects", "objects_trash", "cell_state", "other"}
    assert parts["git"] == _du(folder / "git") + _du(folder / "git-rescue")
    assert parts["objects_trash"] == _du(folder / "objects.trash")
    assert parts["channel_db"] == sum(os.lstat(folder / n).st_blocks * 512 for n in ("channel.db", "channel.db-wal"))
    assert parts["other"] == os.lstat(folder / "channel.json").st_blocks * 512
    assert entry["bytes"] == os.lstat(folder).st_blocks * 512 + sum(parts.values())


# -- the survey and its cache ------------------------------------------------

def test_the_survey_runs_off_the_event_loop_and_concurrent_requests_coalesce(api_app, machine_parts, monkeypatch):
    import asyncio
    storage_survey = machine_parts
    real = storage_survey.survey
    release, entered = threading.Event(), threading.Event()
    calls = []

    def slow(stop=None, publish=None):
        try:
            asyncio.get_running_loop()
            calls.append("on a loop")
        except RuntimeError:
            calls.append(threading.current_thread() is not threading.main_thread())
        entered.set()
        assert release.wait(10)
        return real(stop, publish)
    monkeypatch.setattr(storage_survey, "survey", slow)
    with TestClient(api_app) as client:
        assert client.post("/api/storage/refresh").json()["survey"]["state"] == "running"
        assert entered.wait(10)
        # The loop serves while the survey walks; every request joins it.
        assert client.get("/api/status").status_code == 200
        assert client.post("/api/storage/refresh").status_code == 202
        cold = client.get("/api/storage").json()
        assert cold["survey"]["state"] == "running" and "app_db" not in cold
        release.set()
        answer = _settled(client)
        assert calls == [True]
        assert answer["app_db"]["bytes"] > 0 and answer["survey"]["computed_at"]


def test_the_servers_own_sizes_are_published_before_the_machine_wide_caches(api_app, machine_parts, monkeypatch):
    storage_survey = machine_parts
    release, entered = threading.Event(), threading.Event()

    def slow_uv_cache(stop):
        entered.set()
        assert release.wait(10)
        return {"path": "/uv", "bytes": 33}
    with TestClient(api_app) as client:
        first = _surveyed(client)
        assert first["uv_cache"]["bytes"] == 11
        monkeypatch.setitem(storage_survey.PARTS, "uv_cache", slow_uv_cache)
        client.post("/api/storage/refresh")
        assert entered.wait(10)
        meanwhile = client.get("/api/storage").json()
        assert meanwhile["survey"]["state"] == "running"
        assert meanwhile["survey"]["pending"] == ["uv_cache", "adapters", "docker"]
        assert meanwhile["survey"]["computed_at"] > first["survey"]["computed_at"]
        assert meanwhile["app_db"]["computed_at"] > first["app_db"]["computed_at"]
        assert meanwhile["uv_cache"] == first["uv_cache"]  # the last measurement until the new one
        release.set()
        assert _settled(client)["uv_cache"]["bytes"] == 33


def test_the_cache_survives_a_restart(api_app, machine_parts, monkeypatch):
    with TestClient(api_app) as client:
        channel = _create(client, "Kept")
        first = _surveyed(client)
        sized = next(c for location in first["locations"] for c in location["channels"] if c["id"] == channel)
        assert sized["bytes"] > 0
    import collimate_server.storage_survey as storage_survey

    def never(stop=None, publish=None):
        raise AssertionError("the restarted server measured again")
    monkeypatch.setattr(storage_survey, "survey", never)
    with TestClient(api_app) as client:
        again = client.get("/api/storage").json()
        assert again["survey"] == {"state": "idle", "computed_at": first["survey"]["computed_at"]}
        assert again["app_db"] == first["app_db"] and again["totals"] == first["totals"]
        row = next(c for c in client.get("/api/channels/manage").json()["channels"] if c["id"] == channel)
        assert (row["bytes"], row["computed_at"]) == (sized["bytes"], sized["computed_at"])
    stored = sqlite3.connect(Path(".collimate") / "app.db").execute(
        "SELECT value FROM ui_state WHERE key='storage.sizes'").fetchone()[0]
    assert json.loads(stored)["channels"][channel]["bytes"] == sized["bytes"]


def test_the_overview_reports_locations_parts_and_totals(api_app, machine_parts, fresh_cwd):
    with TestClient(api_app) as client:
        channel = _create(client, "Sized")
        answer = _surveyed(client)
    from collimate_server.channel_paths import installation_id
    assert answer["server"]["data_dir"] == str(fresh_cwd)
    assert answer["server"]["installation_id"] == installation_id()
    [location] = answer["locations"]
    assert (location["path"], location["is_default"], location["state"]) == (str(fresh_cwd / "Channels"), True, "ok")
    assert location["free_bytes"] > 0 and location["total_bytes"] >= location["free_bytes"]
    [row] = location["channels"]
    assert row["id"] == channel and row["bytes"] == _du(fresh_cwd / "Channels" / channel)
    assert location["bytes"] == row["bytes"] + location["other_bytes"]
    for part in ("app_db", "app_store", "staged_store", "envs", "scratch", "other"):
        assert part in answer, part
    assert answer["scratch"]["live_runs"] == 0
    assert answer["uv_cache"] == {"path": "/uv", "bytes": 11, "computed_at": answer["uv_cache"]["computed_at"]}
    assert answer["docker"]["available"] is False
    counted = (location["bytes"] + answer["app_db"]["bytes"] + answer["app_store"]["bytes"]
               + answer["app_store"]["trash_bytes"] + answer["staged_store"]["bytes"]
               + answer["staged_store"]["trash_bytes"] + answer["envs"]["bytes"] + answer["scratch"]["tmp_bytes"]
               + answer["scratch"]["pycache_bytes"] + answer["other"]["bytes"])
    assert answer["totals"]["bytes"] == counted
    assert "previous_layout" not in answer


def test_the_previous_layout_is_reported(api_app, machine_parts, fresh_cwd):
    old = fresh_cwd / ".collimate"
    (old / "channels").mkdir(parents=True)
    (old / "channels" / "0123.db").write_bytes(os.urandom(9000))
    (old / "objects" / "ab").mkdir(parents=True)
    (old / "objects" / "ab" / ("c" * 62)).write_bytes(os.urandom(5000))
    with TestClient(api_app) as client:
        answer = _surveyed(client)
        previous = answer["previous_layout"]
        assert previous["paths"] == [str(old / "channels"), str(old / "objects")]
        assert previous["bytes"] == _du(old / "channels") + _du(old / "objects")
        assert "removed by hand" in previous["note"]
        shutil.rmtree(old / "channels")
        shutil.rmtree(old / "objects")
        gone = client.get("/api/storage").json()
        assert "previous_layout" not in gone
        assert gone["totals"]["bytes"] == answer["totals"]["bytes"] - previous["bytes"]


def test_a_cleanup_and_a_newer_measurement_outlast_an_older_survey(fresh_cwd):
    from collimate_server.storage_survey import _merge
    fresh = {"computed_at": "2026-11-05T10:00:05", "locations": {},
             "channels": {"a": {"bytes": 1, "computed_at": "2026-11-05T10:00:01"}},
             "parts": {"envs": {"bytes": 5, "computed_at": "2026-11-05T10:00:04"}}}
    old = {"channels": {"a": {"bytes": 2, "computed_at": "2026-11-05T10:00:02"},
                        "b": {"bytes": 3, "computed_at": "2026-11-05T10:00:03"},
                        "c": {"bytes": 4, "computed_at": "2026-11-05T09:00:00"}},
           "parts": {"envs": {"bytes": 6, "computed_at": "2026-11-05T09:59:00"}}, "locations": {}}
    merged = _merge(fresh, old, started_at="2026-11-05T10:00:00")
    assert merged["channels"] == {"a": {"bytes": 2, "computed_at": "2026-11-05T10:00:02"},
                                  "b": {"bytes": 3, "computed_at": "2026-11-05T10:00:03"}}
    assert merged["parts"]["envs"]["bytes"] == 5


# -- a channel's size and the manage listing ---------------------------------

def test_a_channels_size_is_measured_now_and_cached(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        channel = _create(client, "Measured")
        folder = fresh_cwd / "Channels" / channel
        (folder / "cell-state" / "c").mkdir(parents=True)
        (folder / "cell-state" / "c" / "session").write_bytes(os.urandom(40_000))
        size = client.get(f"/api/channels/{channel}/size").json()
        assert size["bytes"] == _du(folder) and size["parts"]["cell_state"] == _du(folder / "cell-state")
        row = next(c for c in client.get("/api/channels/manage").json()["channels"] if c["id"] == channel)
        assert (row["bytes"], row["files"], row["computed_at"]) == (size["bytes"], size["files"], size["computed_at"])
        assert client.get(f"/api/channels/{'0' * 8}-0000-4000-8000-{'0' * 12}/size").status_code == 404
        assert client.get("/api/channels/not-a-channel/size").status_code == 404
        assert client.delete(f"/api/channels/{channel}").status_code == 200
    from collimate_server.database import read_global_json
    assert channel not in read_global_json("storage.sizes")["channels"]


def test_a_created_channel_is_sized_at_once(api_app):
    with TestClient(api_app) as client:
        channel = _create(client, "New")
        row = next(c for c in client.get("/api/channels/manage").json()["channels"] if c["id"] == channel)
        assert row["bytes"] > 0 and row["computed_at"]


def test_the_manage_listing_shows_the_archived_moving_and_unavailable_states(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        ok, archived, moving, broken = (_create(client, name) for name in ("Plain", "Shelved", "Moving", "Broken"))
        shelved = client.post(f"/api/channels/{archived}/archive")
        assert shelved.status_code == 200, shelved.text
    channels = fresh_cwd / "Channels"
    conn = sqlite3.connect(channels / broken / "channel.db")
    conn.execute("UPDATE ui_state SET value=? WHERE key='layout'", (json.dumps({"format": "nonsense"}),))
    conn.commit()
    conn.close()
    with TestClient(api_app) as client:
        import collimate_server.channel_guard as channel_guard
        assert channel_guard.mark_moving(moving)
        try:
            rows = {row["id"]: row for row in client.get("/api/channels/manage").json()["channels"]}
        finally:
            channel_guard.clear_moving(moving)
        # The archive routes answer the listing's own row.
        unshelved = client.post(f"/api/channels/{archived}/unarchive").json()
        listed = next(row for row in client.get("/api/channels/manage").json()["channels"] if row["id"] == archived)
        assert unshelved == listed and listed["archived"] is False
    assert [rows[c]["name"] for c in (ok, archived, moving, broken)] == ["Plain", "Shelved", "Moving", "Broken"]
    assert (rows[ok]["state"], rows[ok]["archived"], rows[ok]["reason"]) == ("ok", False, None)
    assert (rows[archived]["state"], rows[archived]["archived"]) == ("ok", True)
    assert rows[archived]["archived_at"] == shelved.json()["archived_at"] is not None
    assert {key: value for key, value in shelved.json().items() if key != "active"} == \
        {key: value for key, value in rows[archived].items() if key != "active"}
    assert (rows[moving]["state"], rows[moving]["archived"]) == ("moving", False)
    assert rows[broken]["state"] == "unavailable" and rows[broken]["reason"]
    assert len({row["location_id"] for row in rows.values()}) == 1
    assert rows[ok]["path"] == str(channels / ok)
    assert rows[ok]["bytes"] > 0 and rows[ok]["files"] > 0 and rows[ok]["computed_at"]
    assert sum(row["active"] for row in rows.values()) == 1
    assert list(rows) == sorted(rows, key=lambda c: (rows[c]["name"].casefold(), c))


def test_the_light_catalog_has_no_unavailable_list(api_app):
    with TestClient(api_app) as client:
        _create(client, "Light")
        assert set(client.get("/api/channels").json()) == {"channels", "active_channel_id"}


# -- maintenance ---------------------------------------------------------------

def _stamp(path: Path, run_id: str, *, phase: str, pid: int) -> None:
    receipt = {"version": 1, "data_dir": str(Path.cwd().resolve()), "run_id": run_id, "server_pid": pid,
               "server_start": None, "phase": phase, "process_group": None}
    (path / ".collrun").mkdir(parents=True)
    (path / ".collrun" / "run-owner.json").write_text(json.dumps(receipt))


def _dead_pid() -> int:
    """A pid no process can have: above the kernel's pid_max (an exited
    child's pid can be reused under a parallel suite)."""
    return int(Path("/proc/sys/kernel/pid_max").read_text()) + 1


def test_clear_scratch_refuses_while_a_run_is_live_and_keeps_what_is_in_use(api_app, fresh_cwd, monkeypatch):
    import uuid
    import collimate_server.run_workspace_cleanup as run_workspace_cleanup
    monkeypatch.setattr(run_workspace_cleanup, "_container_mounts", lambda: [])
    # The lifespan's startup reclaim runs on a worker thread after readiness
    # (main.lifespan, reap_orphan_containers); the receipts below must be
    # stamped after it, or it reclaims the abandoned ones first.
    import threading
    reaped = threading.Event()
    original_reclaim = run_workspace_cleanup.reclaim_abandoned_directories

    def noted_reclaim(*args, **kwargs):
        try:
            return original_reclaim(*args, **kwargs)
        finally:
            reaped.set()

    monkeypatch.setattr(run_workspace_cleanup, "reclaim_abandoned_directories", noted_reclaim)
    with TestClient(api_app) as client:
        assert reaped.wait(10), "the startup reclaim did not run"
        tmp, pycache = fresh_cwd / ".collimate" / "tmp", fresh_cwd / ".collimate" / "pycache"
        aged = time.time() - 3600
        old_tree = tmp / "app_0123abcd_old"
        (old_tree / "lib").mkdir(parents=True)
        (old_tree / "lib" / "gen.py").write_bytes(os.urandom(4000))
        os.chmod(old_tree / "lib", 0o555)
        os.utime(old_tree, (aged, aged))
        in_flight = tmp / "git-read-abc123"
        in_flight.mkdir()
        (pycache / "x" / "__pycache__").mkdir(parents=True)
        (pycache / "x" / "__pycache__" / "m.pyc").write_bytes(os.urandom(2000))
        dead = _dead_pid()
        runs = {phase: str(uuid.uuid4()) for phase in ("staged", "running", "ours")}
        spaces = {phase: tmp / f"ws_{run_id[:8]}_x" for phase, run_id in runs.items()}
        _stamp(spaces["staged"], runs["staged"], phase="staged", pid=dead)
        _stamp(spaces["running"], runs["running"], phase="running", pid=dead)
        _stamp(spaces["ours"], runs["ours"], phase="running", pid=os.getpid())

        from collimate_server.run_registry import active_managers
        active_managers["live-run"] = SimpleNamespace(channel="c", workspace_dir=str(spaces["ours"]))
        try:
            refused = client.post("/api/storage/scratch/clear")
            assert refused.status_code == 409 and "Runs are still running" in refused.json()["detail"]
            assert old_tree.is_dir()
        finally:
            active_managers.pop("live-run")
        expected = _du(old_tree) + _du(spaces["staged"]) + _du(pycache / "x")
        cleared = client.post("/api/storage/scratch/clear")
        assert cleared.status_code == 200, cleared.text
        result = cleared.json()
        assert result["bytes_freed"] == expected and result["removed"] == 3, result
        assert not old_tree.exists() and not spaces["staged"].exists() and not list(pycache.iterdir())
        assert in_flight.is_dir() and spaces["running"].is_dir() and spaces["ours"].is_dir()
        assert {kept["name"] for kept in result["kept"]} == {in_flight.name, spaces["running"].name, spaces["ours"].name}


def test_clear_scratch_refuses_while_an_export_holds_pins(api_app):
    with TestClient(api_app) as client:
        channel = _create(client, "Exporting")
        from collimate_server.cas import channel_store
        store = channel_store(channel)
        digest = store.store_bytes(b"pinned")
        with store.pin_blobs([digest]):
            refused = client.post("/api/storage/scratch/clear")
        assert refused.status_code == 409 and "export" in refused.json()["detail"]
        assert client.post("/api/storage/scratch/clear").status_code == 200


def test_collect_now_frees_at_once_and_records_the_collection(api_app):
    with TestClient(api_app) as client:
        channel = _create(client, "Collected")
        from collimate_server.cas import channel_store
        from collimate_server.cas_gc import MIN_BLOB_AGE_SECONDS, last_collection_at
        store = channel_store(channel)
        digest = store.store_bytes(os.urandom(50_000))
        aged = time.time() - MIN_BLOB_AGE_SECONDS - 60
        os.utime(store.blob_path(digest), (aged, aged))
        from collimate_server.database import write_global_json
        before = "2026-01-01T00:00:00+00:00"
        write_global_json("maintenance.last_collection_at", before)
        response = client.post("/api/storage/collect")
        assert response.status_code == 200, response.text
        result = response.json()
        mine = next(outcome for outcome in result["channels"] if outcome["id"] == channel)
        assert mine["swept"] == 1 and mine["freed_bytes"] >= 50_000
        assert result["freed_bytes"] >= mine["freed_bytes"]
        assert not os.path.exists(store.blob_path(digest)) and not os.listdir(store.trash)
        assert result["last_collection_at"] == last_collection_at() != before
        assert isinstance(result["git"], list)


@pytest.mark.parametrize("last", ["overdue", "recent", "never"])
def test_an_overdue_collection_runs_at_startup_and_only_then(api_app, monkeypatch, last):
    import collimate_server.cas_gc as cas_gc
    import collimate_server.git_store as git_store
    from collimate_server.database import read_global_json, write_global_json
    now = datetime.now(timezone.utc)
    if last != "never":
        ended = now - (timedelta(hours=25) if last == "overdue" else timedelta(hours=1))
        write_global_json(cas_gc.LAST_COLLECTION_KEY, ended.isoformat(timespec="seconds"))
    collected = threading.Event()
    calls = []
    monkeypatch.setattr(cas_gc, "run_gc", lambda **kwargs: (calls.append(kwargs), collected.set(), {})[2])
    monkeypatch.setattr(git_store, "collect_all", lambda: [])
    with TestClient(api_app) as client:
        assert client.get("/api/status").status_code == 200
        ran = collected.wait(5 if last == "overdue" else 0.5)
        if last == "overdue":
            assert ran and calls == [{}]
            deadline = time.monotonic() + 5
            while datetime.fromisoformat(read_global_json(cas_gc.LAST_COLLECTION_KEY)) < now - timedelta(minutes=1):
                assert time.monotonic() < deadline
                time.sleep(0.02)
        else:
            assert not ran and calls == []
    recorded = read_global_json(cas_gc.LAST_COLLECTION_KEY)
    assert recorded is not None
    if last == "never":
        assert datetime.fromisoformat(recorded) >= now - timedelta(minutes=1)


def test_the_old_maintenance_routes_and_ready_fields_are_gone(api_app):
    with TestClient(api_app) as client:
        assert client.post("/api/maintenance/gc").status_code in (404, 405)
        assert client.get("/api/maintenance/disk_usage").status_code in (404, 405)
        checks = client.get("/ready").json()["checks"]
        assert not [key for key in checks if key.startswith("cas_")]
    import collimate_server.cas_gc as cas_gc
    assert not hasattr(cas_gc, "disk_usage")
