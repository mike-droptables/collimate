"""The run lifecycle of a cell's git state (``api/collimate_server/git_harvest.py``, design
§4): the lease's claim and refusal, materialize at prepare, the final,
periodic and ``now`` harvests and their commit, ``kept_out``, rescue and
its routes, and recovery at startup, over real repositories and
``file://`` remotes made with the host git under ``tmp_path``.

A "run" here is what ``routes/runs/create.py`` builds for one (its runs
row, the frozen tip, the lease, a hydrated workspace with its ``.git``
materialized); the end-to-end cases at the bottom start real runs through
the route."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from collimate_server.database import get_db_connection
from git_repos import GIT_OK, commit, git, init
from test_pytest_options_identity import CHANNEL, client as client, make_stack, seed_version

pytestmark = GIT_OK


@pytest.fixture(autouse=True)
def _clean():
    import collimate_server.git_host as git_host
    git_host.reset_capability()
    yield
    import collimate_server.repository_leases as repository_leases
    repository_leases._leases.clear()


# --- the harness ------------------------------------------------------------------------------------


def new_cell(files=None) -> str:
    _ch, _stack, (cid,) = make_stack([("Repo", dict(files or {}))])
    seed_version(CHANNEL, cid)
    return cid


def tip_of(cid) -> str:
    from collimate_server.versions import find_tip
    conn = get_db_connection(CHANNEL)
    try:
        return find_tip(conn, cid)
    finally:
        conn.close()


def rows(cid) -> dict:
    from collimate_server.repository_rows import rows_of
    conn = get_db_connection(CHANNEL)
    try:
        return rows_of(conn, tip_of(cid))
    finally:
        conn.close()


def state_at(cid, repo_path=""):
    from collimate_server.repository_rows import load_state
    conn = get_db_connection(CHANNEL)
    try:
        return load_state(conn, rows(cid)[repo_path])
    finally:
        conn.close()


def versions_of(cid) -> int:
    from collimate_server.cell_files import walk_versions
    conn = get_db_connection(CHANNEL)
    try:
        return len(walk_versions(conn, cid))
    finally:
        conn.close()


def runs_row(run_id, cid, *, status="running"):
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status, generator_name, kind, target_cell_id) "
                     "VALUES (?, '', ?, 'tester', 'generator', ?)", (run_id, status, cid))
        conn.commit()
    finally:
        conn.close()


def run_column(run_id, column):
    conn = get_db_connection(CHANNEL)
    try:
        return conn.execute(f"SELECT {column} FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    finally:
        conn.close()


class Run:
    """One run on a cell, as the starting route builds it: its runs row, the
    frozen tip and rows, the lease when the tip has rows (``claim``), a
    hydrated workspace and the holder's ``.git`` per row."""

    def __init__(self, cid, workspace, *, claim=True, auto=False, run_id=None):
        import collimate_server.git_harvest as git_harvest
        import collimate_server.repository_leases as repository_leases
        import collimate_server.run_staging as run_staging
        self.cid = cid
        self.id = run_id or str(uuid.uuid4())
        self.ws = str(workspace)
        os.makedirs(self.ws, exist_ok=True)
        runs_row(self.id, cid)
        tip = seed_version(CHANNEL, cid)
        from collimate_server.repository_rows import rows_of
        conn = get_db_connection(CHANNEL)
        try:
            frozen = rows_of(conn, tip)
        finally:
            conn.close()
        lease = None
        if frozen and claim:
            try:
                lease = repository_leases.claim(CHANNEL, cid, self.id, "tester")
            except repository_leases.RepositoryBusy:
                lease = None
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self.git = git_harvest.RunGit(
            channel=CHANNEL, cell_id=cid, run_id=self.id, label="tester", workspace=self.ws,
            generator_name="tester", frozen_tip=tip, frozen_rows=frozen, lease=lease, holder=lease is not None,
            auto=auto, log=self.lines.append, warn=self.warnings.append)
        hydrated = run_staging.hydrate_workspace(CHANNEL, cid, self.ws, tip_id=tip)
        links = git_harvest.materialize(self.git, hydrated)
        git_harvest.create_links(self.git, links)
        conn = get_db_connection(CHANNEL)
        try:
            conn.execute("UPDATE runs SET git_input_version_id=?, git_holder=? WHERE id=?",
                         (tip, int(lease is not None), self.id))
            conn.commit()
        finally:
            conn.close()

    def path(self, *parts) -> str:
        return os.path.join(self.ws, *parts)

    def mirror(self, *, skip=()):
        """What the run's live-files mirror does: every regular file of the
        workspace (no ``.git``, ``.collrun``) into the cell, deletions too."""
        from collimate_server.cell_commits import commit_cell_change
        from collimate_server.persistence import snapshot_files
        writes, present = {}, set()
        for root, dirs, files in os.walk(self.ws):
            dirs[:] = [d for d in dirs if d not in (".git", ".collrun")]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, self.ws).replace(os.sep, "/")
                if os.path.islink(full) or rel in skip:
                    continue
                present.add(rel)
                with open(full, "rb") as fh:
                    writes[rel] = fh.read()
        conn = get_db_connection(CHANNEL)
        try:
            cell = {f["path"] for f in snapshot_files(conn, self.cid)}
        finally:
            conn.close()
        commit_cell_change(CHANNEL, self.cid, self.id, writes=writes, deletes=sorted(cell - present),
                           label="files", source_type="generator", generator_name="tester")

    def harvest(self, kind="final"):
        import collimate_server.git_harvest as git_harvest
        return git_harvest.harvest(self.git, kind)

    def finish(self, **mirror):
        """The run's end: the mirror's last flush, the final harvest, the
        release."""
        import collimate_server.repository_leases as repository_leases
        self.mirror(**mirror)
        try:
            return self.harvest("final")
        finally:
            repository_leases.release_run(self.id)


def remote(tmp_path, files=None, name="remote") -> tuple[str, str]:
    """A bare ``file://`` remote with one commit on main; its URL and the
    working clone it was pushed from."""
    source = init(tmp_path / f"{name}-src")
    commit(source, files or {"README.md": "hello\n", "src/app.py": "print(1)\n"}, "first")
    bare = str(tmp_path / f"{name}.git")
    git(tmp_path, "clone", "-q", "--bare", source, bare)
    return f"file://{bare}", source


def status(work_tree) -> str:
    return git(work_tree, "status", "--porcelain=v1")


def clone_into(run: Run, url: str, folder: str = "proj") -> str:
    git(run.ws, "clone", "-q", url, folder)
    return run.path(folder)


def cloned_cell(tmp_path, folder="proj"):
    """A cell whose first run cloned the remote into ``folder``; the cell
    and the remote's URL and working source."""
    url, source = remote(tmp_path)
    cid = new_cell()
    first = Run(cid, tmp_path / "ws-clone")
    clone_into(first, url, folder)
    assert first.finish().version_id
    return cid, url, source


# --- the clone, the next run, the commit --------------------------------------------------------------


def test_a_clone_lands_in_rows_and_the_next_run_sees_a_clean_status_and_full_log(client, tmp_path):
    url, _source = remote(tmp_path)
    cid = new_cell()
    first = Run(cid, tmp_path / "ws1")
    assert first.git.lease is None                      # no rows: no lease at start
    clone_into(first, url)
    done = first.finish()
    assert done.version_id and done.rescued == ()
    assert set(rows(cid)) == {"proj"}
    assert run_column(first.id, "git_harvested_at") is not None
    conn = get_db_connection(CHANNEL)
    try:
        label = conn.execute("SELECT version_name, source_type, run_id FROM cells WHERE id=?",
                             (done.version_id,)).fetchone()
    finally:
        conn.close()
    assert tuple(label) == ("Git state", "git_harvest", first.id)

    second = Run(cid, tmp_path / "ws2")
    assert second.git.lease is not None and set(second.git.materialized) == {"proj"}
    work_tree = second.path("proj")
    assert status(work_tree) == ""
    assert git(work_tree, "log", "--format=%s").split() == ["first"]
    assert git(work_tree, "remote", "get-url", "origin").strip() == url
    assert git(work_tree, "rev-parse", "--abbrev-ref", "main@{u}").strip() == "origin/main"


@pytest.mark.parametrize("fetch", [("--unshallow",), ("--deepen=1",)])
def test_a_harvest_after_a_fetch_past_the_shallow_cut_lands_the_history_it_fetched(client, tmp_path, fetch):
    import collimate_server.git_store as git_store
    source = init(tmp_path / "deep-src")
    for n in range(4):
        commit(source, {f"f{n}.txt": f"{n}\n"}, f"c{n}")
    bare = str(tmp_path / "deep.git")
    git(tmp_path, "clone", "-q", "--bare", source, bare)
    cid = new_cell()
    first = Run(cid, tmp_path / "ws-clone")
    git(first.ws, "clone", "-q", "--depth", "1", f"file://{bare}", "proj")
    assert first.finish().version_id
    assert state_at(cid, "proj").part("shallow")
    deeper = Run(cid, tmp_path / "ws-deeper")
    git(deeper.path("proj"), "fetch", "-q", *fetch, "origin")
    expected = git(deeper.path("proj"), "log", "--format=%s").split()
    assert len(expected) > 1
    assert deeper.finish().version_id
    store = git_store.store_path(CHANNEL)
    assert git(store, "--git-dir", store, "cat-file", "-t", git(deeper.path("proj"), "rev-parse", "HEAD~1").strip()
               ).strip() == "commit"
    pack_dir = tmp_path / "packs"
    pack_dir.mkdir()
    assert git_store.pack_for_export(CHANNEL, state_at(cid, "proj"), str(pack_dir))
    after = Run(cid, tmp_path / "ws-after")
    assert git(after.path("proj"), "log", "--format=%s").split() == expected


def test_an_unchanged_run_writes_nothing(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    before = versions_of(cid)
    run = Run(cid, tmp_path / "ws")
    assert run.harvest("periodic").version_id is None
    assert run.finish().version_id is None
    assert versions_of(cid) == before


def test_a_commit_in_the_run_replaces_the_row_and_a_removed_git_drops_it(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    old = rows(cid)["proj"]
    run = Run(cid, tmp_path / "ws")
    commit(run.path("proj"), {"new.txt": "n\n"}, "second")
    assert run.finish().version_id
    assert rows(cid)["proj"] != old
    head = state_at(cid, "proj")
    assert head.manifest()["head"] == "ref: refs/heads/main"

    gone = Run(cid, tmp_path / "ws-gone")
    shutil.rmtree(gone.path("proj", ".git"))
    assert gone.finish().version_id
    assert "proj" not in rows(cid)


def test_a_git_deleted_then_cloned_again_in_the_run_replaces_the_row_it_removed(client, tmp_path):
    import collimate_server.git_store as git_store
    cid, url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws")
    shutil.rmtree(run.path("proj", ".git"))
    run.mirror()
    assert run.harvest("periodic").version_id            # the removal lands mid-run
    assert "proj" not in rows(cid)
    shutil.rmtree(run.path("proj"))
    clone_into(run, url)
    commit(run.path("proj"), {"again.txt": "a\n"}, "cloned again")
    done = run.finish()
    assert done.version_id and done.rescued == ()
    assert git_store.rescues_of(CHANNEL, cid) == {}
    assert "proj" in rows(cid)


def test_the_periodic_harvest_lands_a_commit_while_the_run_lives_and_skips_an_unchanged_git(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws")
    assert run.harvest("periodic").version_id is None
    commit(run.path("proj"), {"x": "x\n"}, "live")
    run.mirror()
    assert run.harvest("periodic").version_id
    landed = rows(cid)["proj"]
    assert run.git.materialized["proj"] == landed
    assert run.harvest("periodic").version_id is None       # fingerprint unchanged
    assert run.finish().version_id is None


def test_a_held_lock_makes_a_periodic_tick_wait(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws")
    commit(run.path("proj"), {"x": "x\n"}, "live")
    lock = run.path("proj", ".git", "refs", "heads", "main.lock")
    Path(lock).write_text("")
    assert run.harvest("periodic").version_id is None
    os.unlink(lock)
    assert run.harvest("periodic").version_id


def test_an_attempt_that_gets_the_lane_after_the_final_harvest_writes_nothing(client, tmp_path):
    import collimate_server.git_harvest as git_harvest
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws")
    commit(run.path("proj"), {"x": "x\n"}, "kept")
    assert run.finish().version_id
    landed = rows(cid)
    # The workspace half gone, as its reclaim leaves it: a late attempt
    # would take the missing .git for a removed one.
    shutil.rmtree(run.path("proj"))
    for kind in ("now", "periodic"):
        assert run.harvest(kind) == git_harvest.Harvested()
    assert rows(cid) == landed


def test_an_attempt_of_a_run_whose_lease_another_run_now_holds_writes_nothing(client, tmp_path):
    import collimate_server.repository_leases as repository_leases
    cid, _url, _source = cloned_cell(tmp_path)
    old = Run(cid, tmp_path / "old")
    commit(old.path("proj"), {"x": "x\n"}, "stale")
    repository_leases.release_run(old.id)
    new = Run(cid, tmp_path / "new")
    assert new.git.lease is not None and new.git.lease is not old.git.lease
    before = rows(cid)
    assert old.harvest("now").version_id is None
    assert old.harvest("periodic").version_id is None
    assert rows(cid) == before


def test_a_cancelled_cleanup_waits_for_its_final_harvest_before_it_goes_on(monkeypatch):
    import asyncio
    import threading
    import collimate_server.git_harvest as git_harvest
    from collimate_server.process_manager.finalization import FinalizationMixin
    started, finish, done = threading.Event(), threading.Event(), []

    def slow(run, kind):
        started.set()
        finish.wait(10)
        done.append(kind)
        return git_harvest.Harvested()

    monkeypatch.setattr(git_harvest, "harvest", slow)
    manager = SimpleNamespace(git=SimpleNamespace(logged=set()))

    async def scenario():
        task = asyncio.create_task(FinalizationMixin._final_harvest(manager))
        await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0.2)
        waited = not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return waited

    assert asyncio.run(scenario()) is True
    assert done == ["final"]


def test_duplicate_gives_both_cells_the_branch_and_each_keeps_its_own_commits(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    response = client.post(f"/api/channels/{CHANNEL}/cells/{cid}/duplicate")
    assert response.status_code == 200, response.text
    copy = response.json()["new_cell_id"]
    assert rows(copy) == rows(cid)
    a, b = Run(cid, tmp_path / "wa"), Run(copy, tmp_path / "wb")
    assert git(b.path("proj"), "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    commit(a.path("proj"), {"a.txt": "a\n"}, "in a")
    commit(b.path("proj"), {"b.txt": "b\n"}, "in b")
    a.finish()
    b.finish()
    assert rows(cid)["proj"] != rows(copy)["proj"]
    again_a, again_b = Run(cid, tmp_path / "wa2"), Run(copy, tmp_path / "wb2")
    assert git(again_a.path("proj"), "log", "--format=%s").split()[0:2] == ["in", "a"]
    assert git(again_b.path("proj"), "log", "--format=%s").split()[0:2] == ["in", "b"]


def test_a_copy_waits_for_a_releasing_lease_then_harvests_a_live_holder(client, tmp_path, monkeypatch):
    import collimate_server.git_harvest as git_harvest
    import collimate_server.repository_leases as repository_leases
    from collimate_server.run_registry import active_managers
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws")
    commit(run.path("proj"), {"live.txt": "l\n"}, "unharvested")
    run.mirror()
    active_managers[run.id] = SimpleNamespace(git=run.git)
    try:
        git_harvest.settle(CHANNEL, [cid])
    finally:
        active_managers.pop(run.id, None)
    assert run.git.materialized["proj"] == rows(cid)["proj"]
    assert "unharvested" in git(run.path("proj"), "log", "--format=%s")
    # A releasing lease is waited on (up to its bound), never harvested now.
    import threading
    lease = repository_leases.begin_release(CHANNEL, cid, run.id)
    calls = []
    monkeypatch.setattr(git_harvest, "harvest", lambda *a: calls.append(a))
    monkeypatch.setattr(repository_leases, "RELEASE_WAIT_SECONDS", 0.2)
    started = time.monotonic()
    git_harvest.settle(CHANNEL, [cid])                  # nothing releases it: the bound
    assert 0.2 <= time.monotonic() - started < 5
    assert calls == []
    monkeypatch.setattr(repository_leases, "RELEASE_WAIT_SECONDS", 30)
    order = []

    def release():
        order.append("released")
        repository_leases.release_run(run.id)

    threading.Timer(0.3, release).start()
    started = time.monotonic()
    git_harvest.settle(CHANNEL, [cid])
    order.append("settled")
    assert order == ["released", "settled"] and time.monotonic() - started < 5
    assert lease.released.is_set() and calls == []


def test_a_first_clone_holds_a_releasing_lease_from_its_exit_so_a_copy_waits_for_its_rows(client, tmp_path):
    import threading
    import collimate_server.git_harvest as git_harvest
    import collimate_server.repository_leases as repository_leases
    url, _source = remote(tmp_path)
    cid = new_cell()
    run = Run(cid, tmp_path / "ws")
    assert run.git.lease is None                          # no rows: no lease at start
    clone_into(run, url)
    run.mirror()
    git_harvest.begin_release(run.git)                    # the run exits
    held = repository_leases.holder(CHANNEL, cid)
    assert held is run.git.lease and held.state == repository_leases.RELEASING
    copied = {}

    def duplicate():
        copied["response"] = client.post(f"/api/channels/{CHANNEL}/cells/{cid}/duplicate")

    copy = threading.Thread(target=duplicate)
    copy.start()
    time.sleep(0.3)
    assert copy.is_alive()                                # Duplicate waits on the final harvest
    assert run.harvest("final").version_id
    repository_leases.release_run(run.id)
    copy.join(30)
    response = copied["response"]
    assert response.status_code == 200, response.text
    assert set(rows(response.json()["new_cell_id"])) == {"proj"}


def test_a_final_harvest_that_claims_mid_run_takes_the_lease_releasing(client, tmp_path, monkeypatch):
    import collimate_server.git_store as git_store
    import collimate_server.repository_leases as repository_leases
    url, _source = remote(tmp_path)
    cid = new_cell()
    run = Run(cid, tmp_path / "ws")
    clone_into(run, url)
    seen = []
    ingest = git_store.ingest

    def watched(*args, **kwargs):
        seen.append(repository_leases.holder(CHANNEL, cid).state)
        return ingest(*args, **kwargs)

    monkeypatch.setattr(git_store, "ingest", watched)
    assert run.finish().version_id
    assert seen == [repository_leases.RELEASING]


def _canvas_cells(client) -> set[str]:
    snapshot = client.get("/api/state").json()
    return {cell["id"] for c in snapshot["channels"] for s in c["main_stacks"] for cell in s["cells"]}


def _stack_of(client, cid) -> str:
    snapshot = client.get("/api/state").json()
    return next(s["id"] for c in snapshot["channels"] for s in c["main_stacks"]
                if any(cell["id"] == cid for cell in s["cells"]))


@pytest.mark.parametrize("copy", ["duplicate", "fork", "duplicate_in_stack", "duplicate_stack", "export",
                                  "run_in_new_cell"])
def test_a_copy_route_harvests_the_live_holder_first(client, tmp_path, monkeypatch, copy):
    import zipfile
    import io
    from collimate_server.run_registry import active_managers
    started: list = []
    if copy == "run_in_new_cell":
        import collimate_server.routes.runs.create as create
        from conftest import install_runner

        class _Manager:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
                self.process = None
                started.append(self.run_id)

            async def start(self):
                pass

        async def _no_reap(*_a):
            pass

        monkeypatch.setattr(create, "ProcessManager", _Manager)
        monkeypatch.setattr(create, "_reap_process", _no_reap)
        runner, _generator = install_runner(generator="settle_probe", runner="settle-probe")
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws")
    commit(run.path("proj"), {"live.txt": "l\n"}, "unharvested")
    run.mirror()
    before, unharvested = _canvas_cells(client), rows(cid)["proj"]
    active_managers[run.id] = SimpleNamespace(git=run.git)
    try:
        if copy == "duplicate":
            response = client.post(f"/api/channels/{CHANNEL}/cells/{cid}/duplicate")
        elif copy == "fork":
            response = client.post(f"/api/channels/{CHANNEL}/cells/{cid}/fork", json={"target_cell_id": tip_of(cid)})
        elif copy == "duplicate_in_stack":
            response = client.post(f"/api/stacks/{CHANNEL}/{_stack_of(client, cid)}/duplicate_cell_in_stack/0")
        elif copy == "duplicate_stack":
            response = client.post(f"/api/stacks/{CHANNEL}/{_stack_of(client, cid)}/duplicate_stack")
        elif copy == "export":
            response = client.get(f"/api/channels/{CHANNEL}/export.zip", params={"scope": "cell", "cell_id": cid})
        else:
            from run_spawn import wait_spawned
            response = client.post(f"/api/channels/{CHANNEL}/runs",
                                   json={"runner": runner, "cell_id": cid, "new_cell": True})
            assert response.status_code == 200, response.text
            wait_spawned(response.json()["run_id"])
    finally:
        for run_id in (run.id, *started):
            active_managers.pop(run_id, None)
    assert response.status_code == 200, response.text
    landed = rows(cid)["proj"]
    assert landed != unharvested and run.git.materialized["proj"] == landed   # harvested before the copy
    if copy == "export":
        out = tmp_path / "exported"
        zipfile.ZipFile(io.BytesIO(response.content)).extractall(out)
        (work_tree,) = [root for root, dirs, _files in os.walk(out) if ".git" in dirs]
        assert "unharvested" in git(work_tree, "log", "--format=%s")
    else:
        (made,) = _canvas_cells(client) - before
        assert rows(made)["proj"] == landed


def test_an_auto_run_gets_no_now(client, tmp_path, monkeypatch):
    import collimate_server.git_harvest as git_harvest
    from collimate_server.run_registry import active_managers
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws", auto=True)
    calls = []
    monkeypatch.setattr(git_harvest, "harvest", lambda *a: calls.append(a))
    active_managers[run.id] = SimpleNamespace(git=run.git)
    try:
        assert git_harvest.now(CHANNEL, cid) is True
    finally:
        active_managers.pop(run.id, None)
    assert calls == []


# --- the lease ----------------------------------------------------------------------------------------


def _start(cid, run_id, generator="tester", docker="none", exclusive=False):
    """What the starting route's claim sees of a run; ``exclusive`` is its
    generator's ``exclusive_repositories`` flag (the Git generator's)."""
    from collimate_server.routes.runs import create
    start = SimpleNamespace(
        channel=CHANNEL, run_id=run_id, git_lines=[], git_access=None, made={"lease": False},
        resolved=SimpleNamespace(generator_id=generator, permissions=SimpleNamespace(docker=docker),
                                 generator_manifest={"exclusive_repositories": True} if exclusive else {}),
        _lease_label=lambda: "tester")
    create._RunStart._claim_repositories(start, cid)
    return start


def test_siblings_get_a_read_only_git_and_a_git_runner_is_refused_while_a_run_holds_the_lease(client, tmp_path):
    from collimate_server.routes.runs.refusals import RunRefused
    cid, _url, _source = cloned_cell(tmp_path)
    holder = _start(cid, "11111111-0000-4000-8000-000000000000")
    assert holder.made["lease"] is True
    assert holder.git_access == {"repositories": ["proj"], "access": "holder"} and holder.git_lines == []
    sibling = _start(cid, "22222222-0000-4000-8000-000000000000")
    assert sibling.made["lease"] is False
    readonly = ("git is read-only in this run: run 11111111 (tester) holds this cell's git; "
                "Duplicate the cell to commit")
    assert sibling.git_lines == [readonly]
    assert sibling.git_access == {"repositories": ["proj"], "access": "readonly", "reason": readonly}
    with pytest.raises(RunRefused) as refused:
        _start(cid, "33333333-0000-4000-8000-000000000000", generator="git", exclusive=True)
    assert refused.value.status_code == 409
    assert refused.value.body == {
        "error": "repository_busy", "owner": "tester", "run_id": "11111111-0000-4000-8000-000000000000",
        "message": "git is in use by run 11111111 on this cell; Duplicate the cell for a second working copy"}


def test_no_lease_without_rows_usable_git_or_for_docker_limited(client, tmp_path, monkeypatch):
    """A cell without rows has nothing to say; a ``docker: limited`` run and
    a run on a machine without usable git on a cell with rows get no
    ``.git``, and say why in their log and their ``git_access``."""
    import collimate_server.git_host as git_host
    import collimate_server.repository_leases as repository_leases
    bare = _start(new_cell(), str(uuid.uuid4()))
    assert bare.made["lease"] is False and bare.git_access is None and bare.git_lines == []
    cid, _url, _source = cloned_cell(tmp_path)
    limited = _start(cid, str(uuid.uuid4()), docker="limited")
    assert limited.made["lease"] is False
    assert limited.git_lines == ["repositories left out: a docker: limited run gets no .git"]
    assert limited.git_access == {"repositories": ["proj"], "access": "none", "reason": limited.git_lines[0]}
    monkeypatch.setattr(git_host, "_capability", git_host.Capability("missing"))
    no_git = _start(cid, str(uuid.uuid4()))
    assert no_git.made["lease"] is False
    assert no_git.git_lines == ["repositories left out: git on this machine is missing; install git 2.31 or later"]
    assert no_git.git_access == {"repositories": ["proj"], "access": "none", "reason": no_git.git_lines[0]}
    assert repository_leases.holder(CHANNEL, cid) is None


def git_exit(cwd, *args) -> int:
    """The exit code of ``git args`` in ``cwd`` (no ambient configuration)."""
    from git_repos import git_env
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=git_env(str(cwd)), timeout=60).returncode


def head(work_tree) -> str:
    return git(work_tree, "rev-parse", "HEAD").strip()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through any mode")
def test_a_sibling_reads_its_read_only_git_writes_nothing_and_rescues_only_what_it_made(client, tmp_path):
    import collimate_server.git_store as git_store
    import collimate_server.repository_state as rs
    cid, _url, _source = cloned_cell(tmp_path)
    holder = Run(cid, tmp_path / "holder")
    sibling = Run(cid, tmp_path / "sibling")
    work = sibling.path("proj")
    assert sibling.git.lease is None and sibling.git.readonly == {"proj": rows(cid)["proj"]}
    assert not os.access(os.path.join(work, ".git"), os.W_OK)
    for root, dirs, files in os.walk(os.path.join(work, ".git")):
        assert all(not os.stat(os.path.join(root, name)).st_mode & 0o222 for name in [*dirs, *files])
    # Reads work: status, log, diff, show, blame, grep, the branches.
    assert status(work) == "" and git(work, "log", "--format=%s") == "first\n"
    with open(os.path.join(work, "README.md"), "a") as fh:
        fh.write("more\n")
    assert git(work, "diff", "--stat").strip().startswith("README.md | 1 +")
    assert git(work, "show", "--format=%s", "-s", "HEAD") == "first\n"
    assert git(work, "blame", "--porcelain", "README.md").count("\nauthor Ada\n") >= 1
    assert git(work, "grep", "-c", "hello").strip() == "README.md:1"
    assert git(work, "branch", "--list").strip() == "* main"
    # Writes fail: the index, a commit, a branch, a stash, a tag, a reset.
    for args in (("add", "README.md"), ("commit", "-q", "-am", "mine"), ("checkout", "-q", "-b", "mine"),
                 ("stash", "-q"), ("tag", "mine"), ("reset", "-q", "--hard")):
        assert git_exit(work, *args) != 0, args
    assert head(work) == holder_head(holder)
    other = init(sibling.path("mine"))
    commit(other, {"m": "m\n"})
    before = rows(cid)
    done = sibling.finish()
    assert rows(cid) == before
    assert done.rescued == ("mine",)
    reasons = {path: record["reason"] for path, record in git_store.rescues_of(CHANNEL, cid).items()}
    assert reasons == {"mine": f"repository lease held by run {holder.id[:8]}"}
    assert "proj" in rs.discover(sibling.ws).repositories and sibling.git.readonly   # still there, never kept


def holder_head(run: Run) -> str:
    return head(run.path("proj"))


def test_a_read_only_git_follows_the_cell_and_goes_with_its_row(client, tmp_path):
    """The holder commits and its periodic harvest lands: the sibling's
    read-only ``.git`` then names the new commit; a removed row removes it."""
    from collimate_server.run_registry import active_managers
    cid, _url, _source = cloned_cell(tmp_path)
    holder = Run(cid, tmp_path / "holder")
    sibling = Run(cid, tmp_path / "sibling")
    active_managers[sibling.id] = SimpleNamespace(git=sibling.git)
    try:
        sha = commit(holder.path("proj"), {"later.txt": "later\n"}, "later")
        holder.mirror()
        assert holder.harvest("periodic").version_id
        landed = rows(cid)["proj"]

        def followed():
            return sibling.git.readonly.get("proj") == landed and head(sibling.path("proj")) == sha
        deadline = time.monotonic() + 30
        while not followed() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert followed()
        work = sibling.path("proj")
        assert git(work, "log", "--format=%s") == "later\nfirst\n"
        assert not os.access(os.path.join(work, ".git"), os.W_OK)
        assert not [name for name in os.listdir(work) if name.startswith(".git.")]
        assert not [name for name in os.listdir(sibling.path(".collrun")) if name.startswith(".git.")]

        shutil.rmtree(holder.path("proj", ".git"))
        assert holder.harvest("periodic").version_id
        assert "proj" not in rows(cid)
        removed = "git proj: read-only .git removed: the cell has no repository there now"
        deadline = time.monotonic() + 30
        while ("proj" in sibling.git.readonly or removed not in sibling.lines) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert sibling.git.readonly == {} and not os.path.lexists(sibling.path("proj", ".git"))
        assert removed in sibling.lines
    finally:
        active_managers.pop(sibling.id, None)


def test_a_read_only_git_never_follows_a_link_out_of_the_workspace(client, tmp_path):
    """The sibling puts a link to a folder outside its workspace in place of
    its repository's folder: the follow neither rewrites nor removes the
    ``.git`` there, and says so once."""
    import collimate_server.readonly_trees as readonly_trees
    from collimate_server.run_registry import active_managers
    cid, _url, _source = cloned_cell(tmp_path)
    holder = Run(cid, tmp_path / "holder")
    sibling = Run(cid, tmp_path / "sibling")
    outside = tmp_path / "outside"
    shutil.copytree(sibling.path("proj"), outside, symlinks=True)
    readonly_trees.make_writable(str(outside / ".git"))
    outside_head = head(str(outside))
    readonly_trees.remove_tree(sibling.path("proj"))
    os.symlink(outside, sibling.path("proj"))
    active_managers[sibling.id] = SimpleNamespace(git=sibling.git)
    try:
        commit(holder.path("proj"), {"later.txt": "later\n"}, "later")
        holder.mirror()
        assert holder.harvest("periodic").version_id
        not_updated = "git proj: read-only .git not updated: its folder is a link or not a folder"
        deadline = time.monotonic() + 30
        while not_updated not in sibling.lines and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not_updated in sibling.lines
        assert head(str(outside)) == outside_head and os.access(outside / ".git", os.W_OK)
        assert not [name for name in os.listdir(outside) if name.startswith(".git.")]

        shutil.rmtree(holder.path("proj", ".git"))
        assert holder.harvest("periodic").version_id
        not_removed = "git proj: read-only .git not removed: its folder is a link or not a folder"
        deadline = time.monotonic() + 30
        while not_removed not in sibling.lines and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not_removed in sibling.lines
        assert head(str(outside)) == outside_head and "proj" in sibling.git.readonly
    finally:
        active_managers.pop(sibling.id, None)


def test_a_run_without_the_lease_claims_it_at_its_first_harvest_of_a_new_git(client, tmp_path):
    import collimate_server.repository_leases as repository_leases
    cid = new_cell({"notes.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    assert run.git.lease is None
    repo = init(run.path("fresh"))
    commit(repo, {"f": "f\n"})
    run.mirror()
    assert run.harvest("periodic").version_id
    assert repository_leases.holder(CHANNEL, cid).run_id == run.id
    assert set(rows(cid)) == {"fresh"}


def test_a_new_git_after_another_run_changed_the_rows_is_rescued(client, tmp_path):
    import collimate_server.git_store as git_store
    cid = new_cell({"notes.md": "n\n"})
    late = Run(cid, tmp_path / "late")
    early = Run(cid, tmp_path / "early")
    commit(init(early.path("a")), {"x": "x\n"})
    early.finish()
    commit(init(late.path("b")), {"y": "y\n"})
    done = late.finish()
    assert done.rescued == ("b",)
    assert git_store.rescues_of(CHANNEL, cid)["b"]["reason"] == f"cell changed since run {late.id[:8]}"
    assert set(rows(cid)) == {"a"}


# --- materialize ------------------------------------------------------------------------------------


def test_a_held_index_lock_fails_one_repository_and_the_run_still_starts(client, tmp_path, monkeypatch):
    import collimate_server.git_harvest as git_harvest
    import collimate_server.git_host as git_host
    url, _ = remote(tmp_path)
    url2, _ = remote(tmp_path, {"b": "b\n"}, name="other")
    cid = new_cell()
    first = Run(cid, tmp_path / "ws1")
    clone_into(first, url, "one")
    clone_into(first, url2, "two")
    first.finish()
    real_run = git_host.run

    def run(args, **kw):
        if list(args[:3]) == ["update-index", "-q", "--refresh"] and "/one/.git.materialize" in (kw.get("git_dir") or ""):
            open(os.path.join(kw["git_dir"], "index.lock"), "w").close()
        return real_run(args, **kw)

    monkeypatch.setattr(git_host, "run", run)
    second = Run(cid, tmp_path / "ws2")
    assert set(second.git.materialized) == {"two"}
    assert "one" in second.git.failed and "exited 128" in second.git.failed["one"]
    assert not os.path.exists(second.path("one", ".git"))
    assert status(second.path("two")) == ""
    assert any(line.startswith("git one: materialize failed at refresh") for line in second.lines)
    errors = git_harvest.materialize_errors(CHANNEL, cid)
    assert errors == {"one": {"reason": second.git.failed["one"], "rescued": False}}
    snapshot = client.get("/api/state").json()
    entries = {e["path"]: e for c in snapshot["channels"] for s in c["main_stacks"] for cell in s["cells"]
               if cell["id"] == cid for e in cell["repositories"]}
    assert entries["one"]["error"] == {"reason": second.git.failed["one"], "rescued": False}
    assert "error" not in entries["two"]
    monkeypatch.setattr(git_host, "run", real_run)
    # The holder may re-clone the failed path; the harvest replaces its row
    # (a re-clone equal to the row writes nothing, so it commits as well).
    before = rows(cid)["one"]
    shutil.rmtree(second.path("one"))
    clone_into(second, url, "one")
    head = commit(second.path("one"), {"again.txt": "again\n"}, "again")
    assert second.finish().version_id
    assert rows(cid)["one"] != before and f"{head} refs/heads/main" in state_at(cid, "one").part("refs").decode()
    assert git_harvest.materialize_errors(CHANNEL, cid) == {}


def test_without_usable_git_repositories_are_left_out_and_the_final_harvest_rescues(client, tmp_path, monkeypatch):
    import collimate_server.git_host as git_host
    import collimate_server.git_store as git_store
    cid, _url, _source = cloned_cell(tmp_path)
    for capability, words in ((git_host.Capability("missing"), "missing"),
                              (git_host.Capability("broken", "/usr/bin/git"), "does not run"),
                              (git_host.Capability("too_old", "/usr/bin/git", (2, 30, 1)), "2.30.1, older than 2.31")):
        monkeypatch.setattr(git_host, "_capability", capability)
        run = Run(cid, tmp_path / f"ws-{capability.state}", claim=False)
        assert run.git.materialized == {} and not os.path.exists(run.path("proj", ".git"))
        assert run.lines == [f"repositories left out: git on this machine is {words}; install git 2.31 or later"]
        os.makedirs(run.path("made"))
        subprocess.run(["git", "init", "-q", run.path("made")], check=True, env={**os.environ, "HOME": run.ws})
        assert run.harvest("periodic").version_id is None
        done = run.finish()
        assert done.rescued == ("made",)
        assert git_store.rescues_of(CHANNEL, cid)["made"]["reason"] == (
            f"git history from this run was not kept: git on this machine is {words}; install git 2.31 or later")


def test_a_link_chain_across_two_repositories_of_the_cell_is_not_created(tmp_path):
    import collimate_server.git_harvest as git_harvest
    from collimate_server.git_materialize import Link
    ws = tmp_path / "ws"
    (ws / "sub" / "d").mkdir(parents=True)
    lines = []
    run = git_harvest.RunGit(channel=CHANNEL, cell_id="c", run_id="r", label="t", workspace=str(ws),
                             generator_name="t", frozen_tip="", frozen_rows={}, log=lines.append,
                             warn=lines.append)
    # Each passes its own repository's check; together the root's link
    # climbs out of the workspace through the nested repository's.
    git_harvest.create_links(run, {"": (Link("a", b"sub/d/l/../.."), Link("ok", b"sub/d")),
                                   "sub": (Link("d/l", b".."),)})
    assert not os.path.lexists(ws / "a")
    assert os.readlink(ws / "ok") == "sub/d" and os.readlink(ws / "sub" / "d" / "l") == ".."
    assert any("links not created (through another repository's link): a" in line for line in lines)


def test_links_land_after_the_baseline_and_the_band_ignores_itself(client, tmp_path):
    url, _ = remote(tmp_path)
    cid = new_cell()
    first = Run(cid, tmp_path / "ws1")
    work_tree = clone_into(first, url)
    os.symlink("README.md", os.path.join(work_tree, "link"))
    git(work_tree, "add", "link")
    git(work_tree, "commit", "-q", "-m", "link")
    first.finish(skip={"proj/link"})
    second = Run(cid, tmp_path / "ws2")
    assert os.readlink(second.path("proj", "link")) == "README.md"
    assert status(second.path("proj")) == ""


# --- kept_out and the band -----------------------------------------------------------------------------


def test_an_ff_pull_over_a_marked_oversized_blob_keeps_it_out_and_marks_it_again(client, tmp_path, monkeypatch):
    import collimate_server.cas as cas
    monkeypatch.setattr(cas, "MAX_BLOB_BYTES", 1000)
    url, source = remote(tmp_path, {"a.txt": "a\n", "big.bin": "x" * 2000})
    cid = new_cell()
    first = Run(cid, tmp_path / "ws1")
    clone_into(first, url)
    first.finish(skip={"proj/big.bin"})
    assert state_at(cid, "proj").part("kept_out") == b"big.bin\n"
    commit(source, {"big.bin": "y" * 3000, "a.txt": "b\n"}, "bigger")
    git(source, "push", "-q", url, "main")
    second = Run(cid, tmp_path / "ws2")
    work_tree = second.path("proj")
    assert "S big.bin" in git(work_tree, "ls-files", "-v")
    git(work_tree, "pull", "-q", "--ff-only")
    assert "S big.bin" in git(work_tree, "ls-files", "-v")
    assert os.path.getsize(os.path.join(work_tree, "big.bin")) == 3000
    second.finish(skip={"proj/big.bin"})
    assert state_at(cid, "proj").part("kept_out") == b"big.bin\n"
    third = Run(cid, tmp_path / "ws3")
    assert "S big.bin" in git(third.path("proj"), "ls-files", "-v")
    assert status(third.path("proj")) == ""


def test_kept_out_carries_forward_and_an_unchanged_one_writes_nothing(client, tmp_path):
    url, _ = remote(tmp_path, {"a.txt": "a\n", "node_modules/pkg/index.js": "x\n"})
    cid = new_cell()
    first = Run(cid, tmp_path / "ws1")
    clone_into(first, url)
    first.finish(skip={"proj/node_modules/pkg/index.js"})
    assert state_at(cid, "proj").part("kept_out") == b"node_modules/pkg/index.js\n"
    before = versions_of(cid)
    second = Run(cid, tmp_path / "ws2")
    assert status(second.path("proj")) == ""
    assert second.finish(skip={"proj/node_modules/pkg/index.js"}).version_id is None
    assert versions_of(cid) == before


def test_a_tracked_file_the_mirror_skipped_lands_in_kept_out_and_the_next_run_sees_a_clean_status(client, tmp_path):
    url, _ = remote(tmp_path, {"a.txt": "a\n", "build.log": "log\n"})
    cid = new_cell()
    run = Run(cid, tmp_path / "ws1")
    clone_into(run, url)
    # What the run's live-files mirror (exclude=(".log",)) reported at exit.
    run.git.skipped.add("proj/build.log")
    run.finish(skip={"proj/build.log"})
    assert state_at(cid, "proj").part("kept_out") == b"build.log\n"
    second = Run(cid, tmp_path / "ws2")
    assert status(second.path("proj")) == ""


def test_the_mirror_skipped_op_keeps_its_paths_for_the_runs_final_harvest():
    import asyncio
    from collimate_server.process_manager.ipc import IPCMixin
    acks, naks, refusals = [], [], []

    async def send_ack(body, request):
        acks.append(body)

    async def send_nak(op, reason, *, req_id):
        naks.append((op, reason))

    manager = SimpleNamespace(git=SimpleNamespace(skipped=set()), _send_ack=send_ack, _send_nak=send_nak,
                              log_refusal=lambda op, reason: refusals.append(reason))
    asyncio.run(IPCMixin._op_mirror_skipped(manager, {"req_id": "1", "paths": ["proj/build.log", "_agent/n.md"]}))
    asyncio.run(IPCMixin._op_mirror_skipped(manager, {"req_id": "2", "paths": "proj/build.log"}))
    assert manager.git.skipped == {"proj/build.log", "_agent/n.md"}
    assert acks == [{"op": "ack_mirror_skipped", "ok": True}]
    assert naks == [("mirror_skipped", "invalid_paths")] and refusals == ["invalid_paths"]


def test_a_refused_write_of_a_tracked_file_lands_in_kept_out(client, tmp_path):
    url, _ = remote(tmp_path, {"a.txt": "a\n", "bad.generator": "not: [valid\n"})
    cid = new_cell()
    run = Run(cid, tmp_path / "ws1")
    clone_into(run, url)
    run.git.refused.add("proj/bad.generator")
    run.finish(skip={"proj/bad.generator"})
    assert state_at(cid, "proj").part("kept_out") == b"bad.generator\n"


def test_a_failed_flush_only_carries_kept_out_forward(client, tmp_path):
    url, _ = remote(tmp_path, {"a.txt": "a\n", "b.txt": "b\n"})
    cid = new_cell()
    run = Run(cid, tmp_path / "ws1")
    clone_into(run, url)
    run.git.flush_failed = True
    run.git.refused.add("proj/b.txt")
    run.finish(skip={"proj/b.txt"})
    assert state_at(cid, "proj").part("kept_out") is None
    assert "git proj: cell lacks 1 tracked files" in run.lines


@pytest.mark.parametrize("make", ["stash_all", "add_f_in_merge", "auto_merge", "ref", "tag"])
def test_the_band_is_kept_out_of_the_state_end_to_end(client, tmp_path, make):
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws")
    work_tree = run.path("proj")
    os.makedirs(os.path.join(work_tree, ".collrun"), exist_ok=True)
    Path(work_tree, ".collrun", "x").write_text("secret")
    if make == "stash_all":
        Path(work_tree, "README.md").write_text("changed\n")
        git(work_tree, "stash", "--all", "-q")
    elif make in ("ref", "tag"):
        git(work_tree, "add", "-f", ".collrun/x")
        git(work_tree, "commit", "-q", "-m", "band")
        if make == "tag":
            git(work_tree, "tag", "leak")
        else:
            git(work_tree, "branch", "leak")
        git(work_tree, "reset", "-q", "--hard", "HEAD~1")
    else:
        git(work_tree, "checkout", "-q", "-b", "side")
        commit(work_tree, {"README.md": "side\n"}, "side")
        git(work_tree, "checkout", "-q", "main")
        commit(work_tree, {"README.md": "main\n"}, "main")
        git(work_tree, "merge", "-q", "side", check=False)
        if make == "add_f_in_merge":
            git(work_tree, "add", "-f", ".collrun/x")
        else:
            blob = git(work_tree, "hash-object", "-w", ".collrun/x").strip()
            band = git(work_tree, "mktree", input=f"100644 blob {blob}\tx\n".encode()).strip()
            leaked = git(work_tree, "mktree", input=f"040000 tree {band}\t.collrun\n".encode()).strip()
            Path(work_tree, ".git", "AUTO_MERGE").write_text(leaked + "\n")
    run.mirror()
    run.harvest("final")
    state = state_at(cid, "proj")
    named = state.named_parts()
    refs = named["refs"].decode()
    assert "refs/tags/leak" not in refs and "refs/heads/leak" not in refs and "refs/stash" not in refs
    if make == "add_f_in_merge":
        assert "progress/MERGE_HEAD" in named and "index" not in named
    if make == "auto_merge":
        assert "progress/AUTO_MERGE" not in named
    import collimate_server.git_store as git_store
    import collimate_server.repository_state as rs
    reached = git(tmp_path, f"--git-dir={git_store.store_path(CHANNEL)}", "rev-list", "--objects", "--stdin",
                  input="".join(f"{sha}\n" for sha in sorted(rs.roots(state))).encode())
    assert not any(line.split(" ", 1)[-1].split("/")[0] == ".collrun" for line in reached.splitlines() if " " in line)
    for warning in run.warnings + run.lines:
        assert "secret" not in warning


def test_haves_never_reach_argv_through_a_harvest(client, tmp_path, monkeypatch):
    import collimate_server.git_host as git_host
    import collimate_server.repository_state as rs
    cid, _url, _source = cloned_cell(tmp_path)
    haves = rs.roots(state_at(cid, "proj"))
    run = Run(cid, tmp_path / "ws")
    commit(run.path("proj"), {"n": "n\n"}, "new")
    run.mirror()
    seen = []
    spawn = git_host._spawn

    def record(argv, **kw):
        seen.append(list(argv))
        return spawn(argv, **kw)

    monkeypatch.setattr(git_host, "_spawn", record)
    assert run.harvest("final").version_id
    assert seen and not any(have in word for argv in seen for word in argv for have in haves)


def tree_with(ws, raw: bytes) -> str:
    return git(ws, "hash-object", "-t", "tree", "-w", "--literally", "--stdin", input=raw).strip()


def commit_object(ws, tree: str, *, author_tz="+0000") -> str:
    body = (f"tree {tree}\nauthor A <a@b> 1700000000 {author_tz}\ncommitter A <a@b> 1700000000 +0000\n\nm\n")
    return git(ws, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", input=body.encode()).strip()


@pytest.mark.parametrize("kind, fsck_id", [
    ("hasDotgit", "hasDotgit"), ("+05", "badTimezone"), ("zeroPaddedFilemode", "zeroPaddedFilemode")])
def test_objects_fsck_rejects_are_rescued_with_gits_words(client, tmp_path, kind, fsck_id):
    import collimate_server.git_store as git_store
    cid = new_cell({"n.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    ws = init(run.path("bad"))
    blob = git(ws, "hash-object", "-w", "--stdin", input=b"x").strip()
    if kind == "hasDotgit":
        tip = commit_object(ws, tree_with(ws, b"100644 .git\0" + bytes.fromhex(blob)))
    elif kind == "+05":
        tip = commit_object(ws, tree_with(ws, b"100644 f\0" + bytes.fromhex(blob)), author_tz="+05")
    else:
        tip = commit_object(ws, tree_with(ws, b"0100644 f\0" + bytes.fromhex(blob)))
    git(ws, "update-ref", "refs/heads/main", tip)
    done = run.finish()
    assert done.rescued == ("bad",) and rows(cid) == {}
    record = git_store.rescues_of(CHANNEL, cid)["bad"]
    assert fsck_id in record["reason"] and fsck_id in record["git_stderr"]
    assert os.path.isdir(git_store.rescue_gitdir(CHANNEL, cid, "bad"))


def test_an_early_gits_100664_mode_lands_through_a_harvest(client, tmp_path):
    # badFilemode, which design §4.8 lists among index-pack's fatal ids on
    # 2.31-2.37: index-pack --fsck-objects lets it through on 2.31.1 as on
    # current git (git_store's test_a_100664_mode_is_kept), so it lands.
    import collimate_server.git_store as git_store
    cid = new_cell({"n.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    ws = init(run.path("old"))
    blob = git(ws, "hash-object", "-w", "--stdin", input=b"x").strip()
    tip = commit_object(ws, tree_with(ws, b"100664 f\0" + bytes.fromhex(blob)))
    git(ws, "update-ref", "refs/heads/main", tip)
    done = run.finish()
    assert done.rescued == () and set(rows(cid)) == {"old"}
    assert git_store.rescues_of(CHANNEL, cid) == {}


def test_a_submodule_pointer_conflict_keeps_the_index_and_merge_head(client, tmp_path):
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws")
    work_tree = run.path("proj")
    one = commit(init(tmp_path / "sub-a"), {"s": "1\n"})
    two = commit(init(tmp_path / "sub-b"), {"s": "2\n"})
    git(work_tree, "checkout", "-q", "-b", "side")
    git(work_tree, "update-index", "--add", "--cacheinfo", f"160000,{one},sub")
    git(work_tree, "commit", "-q", "-m", "sub one")
    git(work_tree, "checkout", "-q", "main")
    git(work_tree, "update-index", "--add", "--cacheinfo", f"160000,{two},sub")
    git(work_tree, "commit", "-q", "-m", "sub two")
    git(work_tree, "merge", "-q", "side", check=False)
    assert "sub" in git(work_tree, "ls-files", "-u")
    run.mirror()
    run.finish()
    named = state_at(cid, "proj").named_parts()
    assert "progress/MERGE_HEAD" in named and "index" in named
    later = Run(cid, tmp_path / "later")
    assert status(later.path("proj")) == git(work_tree, "status", "--porcelain=v1") == "AA sub\n"
    assert {line.split()[2] for line in git(later.path("proj"), "ls-files", "-u").splitlines()} == {"2", "3"}
    assert os.path.isfile(later.path("proj", ".git", "MERGE_HEAD"))


# --- rescue routes and Discard ------------------------------------------------------------------------


def rescue_of(cid, run: Run, repo_path):
    """A rescue at ``repo_path`` of a repository the run made, as a final
    harvest keeps one it cannot."""
    import collimate_server.git_harvest as git_harvest
    import collimate_server.git_host as git_host
    import collimate_server.git_store as git_store
    commit(init(run.path(repo_path) if repo_path else run.ws), {"r": "r\n"})
    git_harvest._Harvest(run.git, "final", git_host.capability()).rescue(repo_path, "kept for a test")
    assert repo_path in git_store.rescues_of(CHANNEL, cid)


def test_discard_writes_a_none_version_only_with_a_row(client, tmp_path):
    import collimate_server.git_store as git_store
    import collimate_server.readonly_trees as readonly_trees
    cid, _url, _source = cloned_cell(tmp_path)
    run = Run(cid, tmp_path / "ws", claim=False)
    readonly_trees.remove_tree(run.path("proj"))           # its read-only .git with it
    rescue_of(cid, run, "proj")
    rescue_of(cid, run, "elsewhere")
    before = versions_of(cid)
    base = f"/api/channels/{CHANNEL}/cells/{cid}/repositories/rescue"
    assert client.delete(base, params={"path": "elsewhere"}).status_code == 204
    assert versions_of(cid) == before and "proj" in rows(cid)
    assert client.delete(base, params={"path": "proj"}).status_code == 204
    assert versions_of(cid) == before + 1 and "proj" not in rows(cid)
    conn = get_db_connection(CHANNEL)
    try:
        assert conn.execute("SELECT version_name, source_type FROM cells WHERE id=?",
                            (tip_of(cid),)).fetchone()[:] == ("Discard git history", "git_discard")
    finally:
        conn.close()
    assert git_store.rescues_of(CHANNEL, cid) == {}
    gone = client.delete(base, params={"path": "proj"})
    assert gone.status_code == 404 and gone.json()["error"] == "rescue_not_found"


def test_the_rescue_routes_answer_404_409_and_503(client, tmp_path, monkeypatch):
    import collimate_server.git_host as git_host
    import collimate_server.repository_leases as repository_leases
    cid = new_cell({"n.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    rescue_of(cid, run, "kept")
    base = f"/api/channels/{CHANNEL}/cells/{cid}/repositories/rescue"
    for method, suffix in (("post", "/retry"), ("get", "/export"), ("delete", "")):
        missing = getattr(client, method)(base + suffix, params={"path": "nothing"})
        assert missing.status_code == 404 and missing.json()["error"] == "rescue_not_found"
        bad = getattr(client, method)(base + suffix, params={"path": "a/.git"})
        assert bad.status_code == 404
    repository_leases.claim(CHANNEL, cid, "44444444-0000-4000-8000-000000000000", "busy runner")
    for method, suffix in (("post", "/retry"), ("delete", "")):
        busy = getattr(client, method)(base + suffix, params={"path": "kept"})
        assert busy.status_code == 409 and busy.json()["error"] == "repository_busy"
        assert busy.json()["owner"] == "busy runner"
    repository_leases.release_run("44444444-0000-4000-8000-000000000000")
    monkeypatch.setattr(git_host, "_capability", git_host.Capability("missing"))
    unavailable = client.post(base + "/retry", params={"path": "kept"})
    assert unavailable.status_code == 503 and unavailable.json()["error"] == "git_unavailable"


def test_retry_answers_404_before_503_and_refuses_a_removed_cell(client, tmp_path, monkeypatch):
    import collimate_server.git_host as git_host
    import collimate_server.repository_leases as repository_leases
    cid = new_cell({"n.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    rescue_of(cid, run, "kept")
    repository_leases.release_run(run.id)
    base = f"/api/channels/{CHANNEL}/cells/{cid}/repositories/rescue"
    assert client.post(f"/api/channels/{CHANNEL}/cells/{cid}/remove").status_code == 200
    removed = client.post(base + "/retry", params={"path": "kept"})
    assert removed.status_code == 409 and removed.json()["error"] == "cell_removed"
    assert client.post(f"/api/channels/{CHANNEL}/cells/{cid}/restore").status_code == 200
    monkeypatch.setattr(git_host, "_capability", git_host.Capability("missing"))
    missing = client.post(base + "/retry", params={"path": "nothing"})
    assert missing.status_code == 404 and missing.json()["error"] == "rescue_not_found"
    assert client.post(base + "/retry", params={"path": "kept"}).status_code == 503


def test_retry_carries_kept_out_forward_and_redacts_every_git_accounts_secret(client, tmp_path, monkeypatch):
    import collimate_server.accounts as accounts
    import collimate_server.git_harvest as git_harvest
    import collimate_server.git_host as git_host
    import collimate_server.repository_leases as repository_leases
    token = "ghp_" + "Z" * 36
    record = {"id": "gh", "name": "gh", "kind": "git", "service": "github", "protocol": "github", "endpoint": "",
              "model": "", "capabilities": ["git"], "auth_type": "api_key", "credential_account_id": None,
              "metadata": {"author_name": "Ada", "author_email": "ada@example.org"},
              "secrets": {"api_key": token}}
    monkeypatch.setattr(accounts, "list_accounts", lambda: {"accounts": [{"id": "gh", "kind": "git"},
                                                                         {"id": "model", "kind": "model"}]})
    monkeypatch.setattr(accounts, "private_account", lambda identity: record)
    url, _ = remote(tmp_path, {"a.txt": "a\n", "node_modules/pkg/index.js": "x\n"})
    cid = new_cell()
    first = Run(cid, tmp_path / "ws1")
    clone_into(first, url)
    first.finish(skip={"proj/node_modules/pkg/index.js"})
    run = Run(cid, tmp_path / "ws2")
    sha = commit(run.path("proj"), {"b.txt": "b\n"}, "more")
    git(run.path("proj"), "update-ref", "-m", f"pushed with {token}", "refs/heads/pushed", sha)
    run.mirror(skip={"proj/node_modules/pkg/index.js"})
    git_harvest._Harvest(run.git, "final", git_host.capability()).rescue("proj", "kept for a test")
    repository_leases.release_run(run.id)
    retried = client.post(f"/api/channels/{CHANNEL}/cells/{cid}/repositories/rescue/retry", params={"path": "proj"})
    assert retried.status_code == 200, retried.text
    state = state_at(cid, "proj")
    assert state.part("kept_out") == b"node_modules/pkg/index.js\n"
    assert all(token.encode() not in body for body in state.parts.values())
    assert b"pushed with" in state.part("reflogs/refs/heads/pushed")


def test_retry_lands_a_rescue_and_export_streams_a_repository(client, tmp_path):
    import zipfile
    import io
    cid = new_cell({"n.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    rescue_of(cid, run, "kept")
    base = f"/api/channels/{CHANNEL}/cells/{cid}/repositories/rescue"
    exported = client.get(base + "/export", params={"path": "kept"})
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    assert "attachment" in exported.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(exported.content)).namelist()
    assert ".git/HEAD" in names and any(name.startswith(".git/objects/pack/") for name in names)
    import collimate_server.repository_leases as repository_leases
    repository_leases.release_run(run.id)
    retried = client.post(base + "/retry", params={"path": "kept"})
    assert retried.status_code == 200, retried.text
    assert retried.json()["version_id"] == tip_of(cid)
    assert set(rows(cid)) == {"kept"}
    assert client.get(base + "/export", params={"path": "kept"}).status_code == 404


def test_a_rescue_export_that_fails_midway_fails_the_download(client, tmp_path, monkeypatch):
    import collimate_server.git_store as git_store
    cid = new_cell({"n.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    rescue_of(cid, run, "kept")

    def broken(channel, cell_id, repo_path, out, **_kw):
        out.write(b"PK\x03\x04 half a zip")
        raise OSError("disk went away")

    monkeypatch.setattr(git_store, "export_rescue", broken)
    import asyncio
    from collimate_server.routes.cells import export_repository_rescue

    async def download():
        response = await export_repository_rescue(CHANNEL, cid, "kept")
        return [chunk async for chunk in response.body_iterator]

    with pytest.raises(OSError, match="disk went away"):
        asyncio.run(download())


def test_a_harvest_rescue_waits_for_a_retry_or_discard_holding_the_cell(client, tmp_path):
    import threading
    import collimate_server.git_harvest as git_harvest
    import collimate_server.git_host as git_host
    import collimate_server.git_store as git_store
    from collimate_server.cell_commits import cell_commit_lock
    cid = new_cell({"n.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    commit(init(run.path("kept")), {"k": "k\n"})
    held, release = threading.Event(), threading.Event()

    def retry_like():
        with cell_commit_lock(cid):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=retry_like)
    holder.start()
    assert held.wait(10)
    rescuer = threading.Thread(
        target=lambda: git_harvest._Harvest(run.git, "final", git_host.capability()).rescue("kept", "late"))
    rescuer.start()
    time.sleep(0.3)
    assert git_store.rescues_of(CHANNEL, cid) == {}           # waits for the cell
    release.set()
    holder.join(10)
    rescuer.join(10)
    assert set(git_store.rescues_of(CHANNEL, cid)) == {"kept"}


def test_a_rescue_download_cancelled_while_it_waits_leaves_no_worker_blocked(client, tmp_path, monkeypatch):
    import asyncio
    import collimate_server.git_store as git_store
    cid = new_cell({"n.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    rescue_of(cid, run, "kept")

    def slow(channel, cell_id, repo_path, out, **_kw):
        time.sleep(1.0)
        out.write(b"PK late")

    monkeypatch.setattr(git_store, "export_rescue", slow)
    from collimate_server.routes.cells import export_repository_rescue

    async def download():
        response = await export_repository_rescue(CHANNEL, cid, "kept")
        chunks = response.body_iterator

        async def first():
            return await chunks.__anext__()

        reader = asyncio.ensure_future(first())
        await asyncio.sleep(0.3)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        await chunks.aclose()
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(loop.shutdown_default_executor(), timeout=5)

    asyncio.run(download())


def test_a_rescue_export_without_usable_git_is_503_before_any_byte(client, tmp_path, monkeypatch):
    import collimate_server.git_host as git_host
    cid = new_cell({"n.md": "n\n"})
    run = Run(cid, tmp_path / "ws")
    rescue_of(cid, run, "kept")
    monkeypatch.setattr(git_host, "_capability", git_host.Capability("missing"))
    exported = client.get(f"/api/channels/{CHANNEL}/cells/{cid}/repositories/rescue/export", params={"path": "kept"})
    assert exported.status_code == 503 and exported.json()["error"] == "git_unavailable"


# --- the container bind ---------------------------------------------------------------------------------


class _Containers:
    def __init__(self):
        self.ran = []

    def run(self, image, **kwargs):
        self.ran.append(kwargs)
        return SimpleNamespace(id="cid", attrs={"NetworkSettings": {"Ports": {}}}, reload=lambda: None)


def _service(run: Run, *, docker="full"):
    from collimate_server.process_manager.environments import EnvironmentService
    client_ = SimpleNamespace(containers=_Containers(), images=SimpleNamespace(get=lambda tag: object()))
    manager = SimpleNamespace(run_id=run.id, channel=CHANNEL, workspace_dir=run.ws, app_root_dir=run.ws,
                              permissions=SimpleNamespace(update_packages=False, docker=docker),
                              git=run.git, log_buffer=[], host_bindings={}, env_passthrough_sockets=frozenset())
    return EnvironmentService(manager, client_factory=lambda: client_), client_, manager


def _binds(docker_client) -> list[tuple]:
    return [(m["Target"], m["Source"], m["Type"], m["ReadOnly"])
            for m in docker_client.containers.ran[0].get("mounts", [])]


def test_the_store_is_bound_read_only_for_a_run_with_a_git_and_a_read_only_git_over_its_place(client, tmp_path):
    import collimate_server.git_store as git_store
    cid, _url, _source = cloned_cell(tmp_path)
    holder = Run(cid, tmp_path / "holder")
    service, docker_client, _ = _service(holder)
    service.place({"image": "img", "mount": "/workspace", "workspace": holder.ws})
    objects = os.path.join(git_store.store_path(CHANNEL), "objects")
    assert _binds(docker_client) == [(objects, objects, "bind", True)]
    assert objects not in docker_client.containers.ran[0]["volumes"]

    # A run whose only repositories are read-only: the store, and its .git
    # bound read-only over the workspace mount (root in the container).
    sibling = Run(cid, tmp_path / "sibling")
    service, docker_client, _ = _service(sibling)
    service.place({"image": "img", "mount": "/workspace", "workspace": sibling.ws})
    gitdir = os.path.realpath(sibling.path("proj", ".git"))
    assert _binds(docker_client) == [(objects, objects, "bind", True),
                                     ("/workspace/proj/.git", gitdir, "bind", True)]
    # A placement mounting only the repository's folder binds it there.
    service, docker_client, _ = _service(sibling)
    service.place({"image": "img", "mount": "/src", "workspace": sibling.path("proj")})
    assert _binds(docker_client)[1:] == [("/src/.git", gitdir, "bind", True)]
    plain = Run(new_cell({"a": "a"}), tmp_path / "plain")
    service, docker_client, _ = _service(plain)
    service.place({"image": "img", "mount": "/workspace", "workspace": plain.ws})
    assert "mounts" not in docker_client.containers.ran[0]
    import collimate_server.repository_leases as repository_leases
    service, docker_client, _ = _service(sibling, docker="limited")
    service.place({"image": "img", "mount": "/workspace", "workspace": sibling.ws})
    assert "mounts" not in docker_client.containers.ran[0]
    repository_leases.release_run(sibling.id)
    repository_leases.release_run(holder.id)
    limited = Run(cid, tmp_path / "limited")
    assert limited.git.materialized
    service, docker_client, _ = _service(limited, docker="limited")
    service.place({"image": "img", "mount": "/workspace", "workspace": limited.ws})
    assert "mounts" not in docker_client.containers.ran[0]


def test_an_overlapping_mount_leaves_the_repositories_out_of_the_container(client, tmp_path):
    import collimate_server.git_store as git_store
    cid, _url, _source = cloned_cell(tmp_path)
    holder = Run(cid, tmp_path / "holder")
    service, docker_client, manager = _service(holder)
    data = os.path.dirname(git_store.store_path(CHANNEL))
    service.place({"image": "img", "mount": data, "workspace": holder.ws})
    assert "mounts" not in docker_client.containers.ran[0]
    assert any("proj left out of this container" in entry["payload"] for entry in manager.log_buffer)


# --- recovery ---------------------------------------------------------------------------------------------


def crashed(cid, monkeypatch, *, claim=True) -> Run:
    """A run whose server died: its workspace under ``.collimate/tmp``
    with a receipt whose owner is gone, its row still running."""
    import collimate_server.run_workspace_cleanup as cleanup
    run_id = str(uuid.uuid4())
    ws = Path(".collimate", "tmp", f"ws_{run_id[:8]}_probe").absolute()
    ws.mkdir(parents=True)
    cleanup.stamp_directory(ws, run_id)
    monkeypatch.setattr(cleanup, "_alive", lambda pid: False)
    return Run(cid, ws, claim=claim, run_id=run_id)


def restart():
    """A new server: the lease registry empty, interrupted runs marked and
    claimed for recovery, then the reclaim harvesting them."""
    import collimate_server.repository_leases as repository_leases
    import collimate_server.run_workspace_cleanup as cleanup
    from collimate_server.process_manager.finalization import recover_interrupted_runs
    repository_leases._leases.clear()
    recovered = recover_interrupted_runs()
    leased = {cell: repository_leases.holder(CHANNEL, cell[1]) for cell in recovered.values()}
    result = cleanup.reclaim_abandoned_directories(container_mounts=[], recovered=recovered)
    return recovered, leased, result


def test_sigkill_then_restart_lands_the_ref(client, tmp_path, monkeypatch):
    import collimate_server.repository_leases as repository_leases
    cid, _url, _source = cloned_cell(tmp_path)
    run = crashed(cid, monkeypatch)
    sha = commit(run.path("proj"), {"after.txt": "a\n"}, "before the crash")
    run.mirror()
    recovered, leased, result = restart()
    assert recovered == {run.id: (CHANNEL, cid)}
    lease = leased[(CHANNEL, cid)]
    assert lease.label == "recovery" and lease.state == "releasing"
    assert result == {"reclaimed": 1, "retained": 0}
    assert not os.path.exists(run.ws)
    import collimate_server.repository_state as rs
    assert rs.parse_refs(state_at(cid, "proj").part("refs"))["refs/heads/main"] == sha
    assert run_column(run.id, "git_harvested_at") is not None
    assert repository_leases.holder(CHANNEL, cid) is None
    assert restart()[0] == {}                      # once


def test_recovery_lands_after_the_crashed_runs_own_periodic_harvest(client, tmp_path, monkeypatch):
    import collimate_server.git_store as git_store
    import collimate_server.repository_state as rs
    cid, _url, _source = cloned_cell(tmp_path)
    run = crashed(cid, monkeypatch)
    commit(run.path("proj"), {"one": "1\n"}, "one")
    run.mirror()
    assert run.harvest("periodic").version_id
    sha = commit(run.path("proj"), {"two": "2\n"}, "two")
    run.mirror()
    restart()
    assert git_store.rescues_of(CHANNEL, cid) == {}
    assert rs.parse_refs(state_at(cid, "proj").part("refs"))["refs/heads/main"] == sha


def test_a_run_whose_server_died_after_its_status_was_written_is_recovered(client, tmp_path, monkeypatch):
    import collimate_server.repository_state as rs
    cid, _url, _source = cloned_cell(tmp_path)
    run = crashed(cid, monkeypatch)
    sha = commit(run.path("proj"), {"late": "l\n"}, "before the final harvest")
    run.mirror()
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("UPDATE runs SET status='completed' WHERE id=?", (run.id,))
        conn.commit()
    finally:
        conn.close()
    recovered, _leased, result = restart()
    assert recovered == {run.id: (CHANNEL, cid)} and result == {"reclaimed": 1, "retained": 0}
    assert rs.parse_refs(state_at(cid, "proj").part("refs"))["refs/heads/main"] == sha
    assert run_column(run.id, "git_harvested_at") is not None


def test_a_run_whose_cell_was_merged_away_is_rescued_on_the_cell_that_took_it(client, tmp_path, monkeypatch):
    import collimate_server.git_store as git_store
    import collimate_server.repository_leases as repository_leases
    from test_pytest_options_identity import merge
    url, _ = remote(tmp_path)
    _ch, stack, (cid, other) = make_stack([("Repo", {}), ("Other", {"o.md": "o\n"})])
    seed_version(CHANNEL, cid)
    seed_version(CHANNEL, other)
    first = Run(cid, tmp_path / "ws-clone")
    clone_into(first, url)
    assert first.finish().version_id
    run = crashed(cid, monkeypatch)
    commit(run.path("proj"), {"x": "x\n"}, "lost?")
    repository_leases.release_run(run.id)
    merged = merge(client, stack)
    restart()
    assert git_store.rescues_of(CHANNEL, cid) == {}
    assert git_store.rescues_of(CHANNEL, merged)["proj"]["reason"] == f"cell changed since run {run.id[:8]}"
    assert run_column(run.id, "git_harvested_at") is not None


def test_a_git_neither_saved_nor_rescued_keeps_the_run_unharvested_and_its_workspace_for_recovery(
        client, tmp_path, monkeypatch):
    import errno
    import asyncio
    import collimate_server.git_harvest as git_harvest
    import collimate_server.git_host as git_host
    import collimate_server.git_store as git_store
    import collimate_server.repository_state as rs
    from collimate_server.process_manager.finalization import FinalizationMixin
    cid, _url, _source = cloned_cell(tmp_path)
    real_rescue, real_harvest = git_store.rescue, git_harvest.harvest

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    # The final harvest of a run on a machine whose git went away, on a
    # full disk: nothing lands, nothing is rescued, the run stays
    # unharvested with its .git where it was.
    final = Run(cid, tmp_path / "final")
    commit(final.path("proj"), {"f": "f\n"}, "unsaved")
    monkeypatch.setattr(git_host, "_capability", git_host.Capability("missing"))
    monkeypatch.setattr(git_store, "rescue", full)
    done = final.finish()
    assert done.unkept == ("proj",) and done.rescued == ()
    assert run_column(final.id, "git_harvested_at") is None
    assert os.path.isdir(final.path("proj", ".git"))
    manager = SimpleNamespace(git=final.git)
    monkeypatch.setattr(git_harvest, "harvest", lambda run, kind: done)
    asyncio.run(FinalizationMixin._final_harvest(manager))
    assert manager._git_unkept == ("proj",)                 # the cleanup keeps the workspace
    monkeypatch.setattr(git_harvest, "harvest", real_harvest)
    monkeypatch.setattr(git_store, "rescue", real_rescue)
    monkeypatch.setattr(git_host, "_capability", None)

    # A recovery that can neither land nor rescue keeps the workspace for
    # a later start, which lands it.
    run = crashed(cid, monkeypatch)
    sha = commit(run.path("proj"), {"after.txt": "a\n"}, "before the crash")
    run.mirror()
    monkeypatch.setattr(git_host, "_capability", git_host.Capability("missing"))
    monkeypatch.setattr(git_store, "rescue", full)
    _recovered, _leased, result = restart()
    assert result == {"reclaimed": 0, "retained": 1}
    assert os.path.isdir(run.path("proj", ".git"))
    assert run_column(run.id, "git_harvested_at") is None
    monkeypatch.setattr(git_host, "_capability", None)
    monkeypatch.setattr(git_store, "rescue", real_rescue)
    _recovered, _leased, result = restart()
    assert result["reclaimed"] == 1
    assert rs.parse_refs(state_at(cid, "proj").part("refs"))["refs/heads/main"] == sha
    assert run_column(run.id, "git_harvested_at") is not None


def test_channel_busy_counts_the_recovery_threads_marks_with_the_loops():
    import threading
    from collimate_server.run_registry import busy_channels, channel_busy
    channel = str(uuid.uuid4())

    def mark():
        for _ in range(2000):
            with channel_busy(channel):
                pass

    threads = [threading.Thread(target=mark) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert channel not in busy_channels()


def test_a_cell_removed_before_the_next_start_is_rescued_and_shown_on_restore(client, tmp_path, monkeypatch):
    import collimate_server.git_store as git_store
    cid, _url, _source = cloned_cell(tmp_path)
    run = crashed(cid, monkeypatch)
    commit(run.path("proj"), {"x": "x\n"}, "lost?")
    removed = client.post(f"/api/channels/{CHANNEL}/cells/{cid}/remove")
    assert removed.status_code == 200, removed.text
    restart()
    assert git_store.rescues_of(CHANNEL, cid)["proj"]["reason"] == "cell was removed"
    assert client.post(f"/api/channels/{CHANNEL}/cells/{cid}/restore").status_code == 200
    snapshot = client.get("/api/state").json()
    (entry,) = [e for c in snapshot["channels"] for s in c["main_stacks"] for cell in s["cells"]
                if cell["id"] == cid for e in cell["repositories"]]
    assert entry["error"] == {"reason": "cell was removed", "rescued": True}


def test_a_garbage_head_is_rescued(client, tmp_path, monkeypatch):
    import collimate_server.git_store as git_store
    cid, _url, _source = cloned_cell(tmp_path)
    run = crashed(cid, monkeypatch)
    Path(run.path("proj", ".git", "HEAD")).write_text("garbage\n")
    before = rows(cid)
    restart()
    record = git_store.rescues_of(CHANNEL, cid)["proj"]
    assert record["reason"] == "HEAD unreadable" and record["git_stderr"]
    assert rows(cid) == before


def test_a_recovery_after_a_newer_harvest_rescues(client, tmp_path, monkeypatch):
    import collimate_server.git_store as git_store
    import collimate_server.repository_leases as repository_leases
    cid, _url, _source = cloned_cell(tmp_path)
    run = crashed(cid, monkeypatch)
    commit(run.path("proj"), {"x": "x\n"}, "in the crashed run")
    repository_leases.release_run(run.id)
    newer = Run(cid, tmp_path / "newer")
    commit(newer.path("proj"), {"y": "y\n"}, "newer")
    newer.finish()
    landed = rows(cid)
    restart()
    assert rows(cid) == landed
    assert git_store.rescues_of(CHANNEL, cid)["proj"]["reason"] == f"cell changed since run {run.id[:8]}"


def test_a_run_during_recovery_gets_a_read_only_git(client, tmp_path, monkeypatch):
    import collimate_server.repository_leases as repository_leases
    from collimate_server.process_manager.finalization import recover_interrupted_runs
    cid, _url, _source = cloned_cell(tmp_path)
    crashed(cid, monkeypatch)
    repository_leases._leases.clear()
    recovered = recover_interrupted_runs()
    assert list(recovered.values()) == [(CHANNEL, cid)]
    start = _start(cid, str(uuid.uuid4()))
    assert start.made["lease"] is False
    assert start.git_lines[0].startswith("git is read-only in this run: run ")
    assert "(recovery) holds this cell's git" in start.git_lines[0]


def test_a_crashed_runs_read_only_git_is_never_kept_and_its_workspace_is_reclaimed(client, tmp_path, monkeypatch):
    import collimate_server.git_store as git_store
    import collimate_server.repository_leases as repository_leases
    cid, _url, _source = cloned_cell(tmp_path)
    holder = Run(cid, tmp_path / "holder")
    run = crashed(cid, monkeypatch, claim=False)
    assert set(run.git.readonly) == {"proj"} and not os.access(run.path("proj", ".git"), os.W_OK)
    repository_leases.release_run(holder.id)
    before = rows(cid)
    recovered, _leased, result = restart()
    assert recovered == {run.id: (CHANNEL, cid)} and result == {"reclaimed": 1, "retained": 0}
    assert not os.path.exists(run.ws)
    assert rows(cid) == before and git_store.rescues_of(CHANNEL, cid) == {}
    assert run_column(run.id, "git_harvested_at") is not None


def test_harvested_receipts_are_reclaimed_with_a_dead_owner_and_kept_with_a_living_group(fresh_cwd, monkeypatch):
    import collimate_server.run_workspace_cleanup as cleanup
    run_id = str(uuid.uuid4())
    ws = Path(".collimate", "tmp", f"ws_{run_id[:8]}_probe").absolute()
    ws.mkdir(parents=True)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        cleanup.stamp_directory(ws, run_id, phase="harvested", process_group=process.pid)
        monkeypatch.setattr(cleanup, "_alive", lambda pid: False)
        assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {"reclaimed": 0, "retained": 1}
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {"reclaimed": 1, "retained": 0}
    other = Path(".collimate", "tmp", f"ws_{run_id[:8]}_again").absolute()
    other.mkdir()
    cleanup.stamp_directory(other, run_id, phase="harvested")
    # A harvested receipt is only reclaimed: no harvest, whatever the map says.
    assert cleanup.reclaim_abandoned_directories(
        container_mounts=[], recovered={run_id: ("nowhere", "cell")}) == {"reclaimed": 1, "retained": 0}


# --- the fence ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("docker,expected", [("none", []), ("limited", ["ended"]), ("full", ["ended"]), (None, ["ended"])])
def test_the_fence_reaps_labelled_containers_except_for_docker_none__F0393(monkeypatch, docker, expected):
    import asyncio
    from collimate_server.process_manager.finalization import FinalizationMixin
    from collimate_server.routes.runs import helpers
    calls = []
    monkeypatch.setattr(helpers, "_reap_run_containers", calls.append)
    manager = SimpleNamespace(run_id="ended", process=None, permissions=SimpleNamespace(docker=docker),
                              _host_terminal_sessions={}, push_log=lambda line: None)
    asyncio.run(FinalizationMixin._fence_run_processes(manager))
    assert calls == expected


def test_the_fence_ends_a_host_terminals_whole_session_before_the_harvest(monkeypatch):
    import asyncio
    from collimate_server.process_manager.finalization import FinalizationMixin
    leader = subprocess.Popen([sys.executable, "-c",
                               "import os, subprocess, sys, time; "
                               "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                               "start_new_session=False, preexec_fn=os.setpgrp); "
                               "print(child.pid, flush=True); time.sleep(60)"],
                              stdout=subprocess.PIPE, start_new_session=True, text=True)
    stray = int(leader.stdout.readline())
    from collimate_server.process_signals import start_ticks
    manager = SimpleNamespace(run_id="t", process=None, permissions=SimpleNamespace(docker="none"),
                              _host_terminal_sessions={leader.pid: start_ticks(leader.pid)},
                              push_log=lambda line: None)
    started = time.monotonic()
    asyncio.run(FinalizationMixin._fence_run_processes(manager))
    assert time.monotonic() - started < 10
    leader.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(stray, 0)
        except ProcessLookupError:
            break
        try:
            os.waitpid(stray, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(stray, 0)


def test_the_fence_signals_nothing_for_a_mocked_process_or_a_session_that_is_not_a_pid_above_1(monkeypatch):
    import asyncio
    from unittest.mock import MagicMock
    from collimate_server.process_manager import finalization
    from collimate_server.process_manager.finalization import FinalizationMixin
    sent = []
    monkeypatch.setattr(finalization, "signal_process_group", lambda *args, **kw: sent.append(args))
    manager = SimpleNamespace(run_id="mocked", process=MagicMock(), permissions=SimpleNamespace(docker="none"),
                              _host_terminal_sessions={0: None, 1: None, -5: None}, push_log=lambda line: None)
    asyncio.run(FinalizationMixin._fence_run_processes(manager))
    assert sent == []


def test_the_fence_signals_a_session_or_group_only_while_its_pid_names_the_process_the_run_started(monkeypatch):
    import asyncio
    from collimate_server.process_manager import finalization
    from collimate_server.process_manager.finalization import FinalizationMixin
    from collimate_server.process_signals import start_ticks
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        started = start_ticks(child.pid)
        assert started
        sent = []
        monkeypatch.setattr(finalization, "signal_process_group", lambda group, sig, **kw: sent.append(group))
        monkeypatch.setattr(finalization, "FENCE_SECONDS", 0.1)

        def fence(ticks):
            sent.clear()
            manager = SimpleNamespace(run_id="t", process=SimpleNamespace(pid=child.pid), _process_started=ticks,
                                      permissions=SimpleNamespace(docker="none"),
                                      _host_terminal_sessions={child.pid: ticks}, push_log=lambda line: None)
            asyncio.run(FinalizationMixin._fence_run_processes(manager))
            return set(sent)

        assert fence(str(int(started) + 1)) == set()      # the pid now names another process
        assert fence(started) == {child.pid}
        assert fence(None) == set()                        # no start known: nothing proves the pid
    finally:
        child.kill()
        child.wait(timeout=10)


def test_the_fence_signals_an_unknown_start_only_for_the_generator_the_run_has_not_reaped(monkeypatch):
    import asyncio
    from collimate_server.process_manager import finalization
    from collimate_server.process_manager.finalization import FinalizationMixin
    sent = []
    monkeypatch.setattr(finalization, "signal_process_group", lambda group, sig, **kw: sent.append(group))
    monkeypatch.setattr(finalization, "FENCE_SECONDS", 0.1)
    monkeypatch.setattr(finalization, "_groups_alive", lambda groups: set())
    for returncode, expected in ((None, [4242]), (0, [])):
        sent.clear()
        manager = SimpleNamespace(run_id="t", process=SimpleNamespace(pid=4242, returncode=returncode),
                                  _process_started=None, permissions=SimpleNamespace(docker="none"),
                                  _host_terminal_sessions={4343: None}, push_log=lambda line: None)
        asyncio.run(FinalizationMixin._fence_run_processes(manager))
        assert sent == expected


def test_a_start_identity_without_proc_comes_from_ps(monkeypatch):
    import collimate_server.process_signals as process_signals
    monkeypatch.setattr(process_signals, "_PROC", "/nonexistent-proc")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        started = process_signals.start_ticks(child.pid)
        assert started and started.startswith("ps:")
        assert process_signals.start_ticks(child.pid) == started
    finally:
        child.kill()
        child.wait(timeout=10)
    assert process_signals.start_ticks(child.pid) is None


def test_a_new_host_terminal_forgets_the_run_sessions_that_are_over():
    from collimate_server.process_signals import start_ticks
    from collimate_server.routes.terminal import _forget_ended_sessions
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    gone = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    gone_started = start_ticks(gone.pid)
    gone.wait(timeout=10)
    try:
        sessions = {live.pid: start_ticks(live.pid), gone.pid: gone_started, 4545: None}
        _forget_ended_sessions(sessions)
        assert set(sessions) == {live.pid}
    finally:
        live.kill()
        live.wait(timeout=10)


@pytest.mark.parametrize("fenced, expected", [(False, ["cut"]), (True, [])])
def test_the_reaper_reaps_the_containers_of_a_cleanup_cut_short_before_its_fence(monkeypatch, fenced, expected):
    import asyncio
    from collimate_server.routes.runs import helpers
    calls = []
    monkeypatch.setattr(helpers, "_reap_run_containers", calls.append)

    async def scenario():
        async def cut():
            raise asyncio.CancelledError

        manager = SimpleNamespace(_cleanup_task=asyncio.ensure_future(cut()), _fenced=fenced, _writer_task=None,
                                  permissions=SimpleNamespace(docker="full"))
        await helpers._reap_process("cut", manager)

    asyncio.run(scenario())
    assert calls == expected


def test_a_cancelled_periodic_harvest_does_not_cancel_the_cleanup_that_stops_it():
    import asyncio
    from collimate_server.process_manager.finalization import FinalizationMixin

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(60))
        await asyncio.sleep(0)
        task.cancel()
        manager = SimpleNamespace(_git_stop=asyncio.Event(), _git_periodic=task)
        await FinalizationMixin._stop_periodic_harvest(manager)
        return manager._git_stop.is_set()

    assert asyncio.run(scenario())


# --- the run's environment ------------------------------------------------------------------------------


@pytest.mark.parametrize("docker", ["none", "full", "limited"])
def test_a_runs_git_env_pins_the_formats_and_natively_the_ceiling(tmp_path, docker):
    from collimate_server.process_manager.manager import run_git_env
    ws = tmp_path / "ws"
    ws.mkdir()
    env = run_git_env(str(ws), docker)
    assert env["GIT_DEFAULT_REF_FORMAT"] == "files" and env["GIT_DEFAULT_HASH"] == "sha1"
    if docker == "limited":
        assert "GIT_CEILING_DIRECTORIES" not in env
    else:
        assert env["GIT_CEILING_DIRECTORIES"] == os.path.dirname(os.path.realpath(ws))


def test_a_host_terminal_of_a_run_gets_the_ceiling_above_its_workspace(tmp_path):
    from collimate_server.routes.terminal import _host_argv_and_env
    from collimate_server.run_registry import active_managers
    run_id = str(uuid.uuid4())
    active_managers[run_id] = SimpleNamespace(workspace_dir=str(tmp_path / "ws"))
    try:
        session = SimpleNamespace(env={"PATH": "/bin"}, run_id=run_id, command=["sh"])
        argv, env = _host_argv_and_env(session)
    finally:
        active_managers.pop(run_id, None)
    assert argv == ["sh"] and env["PATH"] == "/bin"
    assert env["GIT_CEILING_DIRECTORIES"] == os.path.dirname(os.path.realpath(tmp_path / "ws"))
    orphan = SimpleNamespace(env={}, run_id=str(uuid.uuid4()), command=["sh"])
    assert "GIT_CEILING_DIRECTORIES" not in _host_argv_and_env(orphan)[1]


def test_a_host_twin_keeps_the_runs_git_pins(monkeypatch):
    generators = str(Path(__file__).resolve().parents[2] / "content" / "generators")
    monkeypatch.syspath_prepend(generators)
    from _agent_shared.host_adapters import host_env
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/data/.collimate/tmp")
    monkeypatch.setenv("GIT_DEFAULT_REF_FORMAT", "files")
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha1")
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    env = host_env()
    assert env["GIT_CEILING_DIRECTORIES"] == "/data/.collimate/tmp"
    assert env["GIT_DEFAULT_REF_FORMAT"] == "files" and env["GIT_DEFAULT_HASH"] == "sha1"
    assert "GIT_DIR" not in env
