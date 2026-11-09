"""Git in cells end to end, through real runs (``uv run``): a run that
clones a ``file://`` remote commits its cell's rows, the next run on the
cell finds the clone's ``.git`` with a clean status and its whole log, a
Duplicate gives both cells the branch and each keeps its own commits, and
while one run holds the cell's git a Git runner is refused and any other
run gets a read-only ``.git`` (design §4, §12)."""
from __future__ import annotations

import os
import shutil
import time
import uuid

import pytest

from conftest import install_runner
from collimate_server.database import get_db_connection
from git_repos import GIT_OK, commit, git, init
from run_spawn import wait_spawned

pytestmark = [GIT_OK, pytest.mark.skipif(shutil.which("uv") is None, reason="the runs need uv on PATH")]

# Runs the cell's step.sh in the workspace inside a live-files mirror; its
# output lands in out.txt, which the mirror carries to the cell. A cell file
# hold.txt keeps the run alive until the test acts on it.
_STEP = (
    "# /// script\n"
    "# requires-python = \">=3.11\"\n"
    "# dependencies = []\n"
    "# ///\n"
    "import os, subprocess\n"
    "import gen\n"
    "async def run():\n"
    "    ws = os.environ['COLLIMATE_WORKSPACE']\n"
    "    async with gen.live_files():\n"
    "        with open(os.path.join(ws, 'step.sh')) as fh:\n"
    "            script = fh.read()\n"
    "        subprocess.run(['sh', '-c', script], cwd=ws)\n"
    "        if os.path.exists(os.path.join(ws, 'hold.txt')):\n"
    "            gen.phase('awaiting_input', 'holding')\n"
    "            await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "if __name__ == '__main__':\n"
    "    gen.main(run)\n"
)

# The step's git: no config of this machine, a fixed author.
_PRELUDE = (
    "export HOME=\"$PWD/.collrun/home\" GIT_CONFIG_NOSYSTEM=1 GIT_AUTHOR_NAME=Ada "
    "GIT_AUTHOR_EMAIL=ada@example.com GIT_COMMITTER_NAME=Ada GIT_COMMITTER_EMAIL=ada@example.com\n"
    "mkdir -p \"$HOME\"\n"
)


def _poll(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def _db(client):
    return get_db_connection(client.channel_id)


def _rows(client, cid) -> dict:
    from collimate_server.repository_rows import rows_of
    from collimate_server.versions import find_tip
    conn = _db(client)
    try:
        return rows_of(conn, find_tip(conn, cid))
    finally:
        conn.close()


def _tip_text(client, cid, path) -> str | None:
    from collimate_server.cas import channel_store
    from collimate_server.versions import find_tip, tip_file_map
    conn = _db(client)
    try:
        digest = tip_file_map(conn, find_tip(conn, cid)).get(path)
    finally:
        conn.close()
    return None if digest is None else (channel_store(client.channel_id).read_blob(digest) or b"").decode()


def _cell(client, files: dict) -> str:
    created = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]
    _write(client, created["id"], files)
    return created["id"]


def _write(client, cid, files: dict) -> None:
    """Write (text) or delete (None) cell files, as a host edit."""
    from collimate_server.cell_commits import commit_cell_change
    run_id = str(uuid.uuid4())
    conn = _db(client)
    try:
        conn.execute("INSERT INTO runs(id,prompt,status) VALUES (?,'fixture','completed')", (run_id,))
        conn.commit()
    finally:
        conn.close()
    commit_cell_change(client.channel_id, cid, run_id, writes={p: v for p, v in files.items() if v is not None},
                       deletes=[p for p, v in files.items() if v is None])


def _run(client, runner, cid, *, done=True) -> str:
    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cid})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    wait_spawned(run_id)
    if done:
        _ended(client, run_id)
    return run_id


def _ended(client, run_id) -> None:
    def harvested():
        conn = _db(client)
        try:
            row = conn.execute("SELECT status, git_harvested_at FROM runs WHERE id=?", (run_id,)).fetchone()
        finally:
            conn.close()
        from collimate_server.run_registry import active_managers
        return row is not None and row[0] != "running" and row[1] is not None and run_id not in active_managers
    _poll(harvested, 120, f"run {run_id[:8]} to end and harvest")


def _card(client, run_id) -> dict:
    """The run's card as its "Run started" version holds it (the run-end
    sweep removes it from the tip)."""
    import json
    from collimate_server.cas import channel_store
    from collimate_server.run_folders import RUN_CARD_EXT, label_of
    from collimate_server.versions import tip_file_map
    conn = _db(client)
    try:
        started = conn.execute("SELECT id FROM cells WHERE run_id=? AND version_name='Run started'",
                               (run_id,)).fetchone()[0]
        files = tip_file_map(conn, started)
    finally:
        conn.close()
    (digest,) = [d for path, d in files.items()
                 if (label := label_of(path)) and label.endswith(run_id[:8]) and path.endswith(RUN_CARD_EXT)]
    return json.loads(channel_store(client.channel_id).read_blob(digest))


def _log(client, run_id) -> str:
    conn = _db(client)
    try:
        return "\n".join(str(r[0]) for r in conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? ORDER BY id", (run_id,)))
    finally:
        conn.close()


@pytest.fixture
def remote(tmp_path):
    source = init(tmp_path / "src")
    commit(source, {"README.md": "hello\n", "src/app.py": "print(1)\n"}, "first")
    commit(source, {"src/app.py": "print(2)\n"}, "second")
    bare = str(tmp_path / "remote.git")
    git(tmp_path, "clone", "-q", "--bare", source, bare)
    return f"file://{bare}"


def test_a_clone_lands_rows_the_next_run_sees_it_and_duplicates_keep_their_own_commits(client, remote):
    runner, _ = install_runner(generator="git_steps", runner="git-steps", entrypoint_body=_STEP)
    cid = _cell(client, {"step.sh": _PRELUDE + f"git clone -q {remote} proj > out.txt 2>&1\n"})
    _run(client, runner, cid)
    assert set(_rows(client, cid)) == {"proj"}
    assert _tip_text(client, cid, "proj/README.md") == "hello\n"

    _write(client, cid, {"step.sh": _PRELUDE + "cd proj && { git status --porcelain=v1; git log --format=%s; "
                                    "git remote get-url origin; } > ../out.txt 2>&1\n"})
    _run(client, runner, cid)
    assert _tip_text(client, cid, "out.txt") == f"second\nfirst\n{remote}\n"

    copy = client.post(f"/api/channels/{client.channel_id}/cells/{cid}/duplicate")
    assert copy.status_code == 200, copy.text
    other = copy.json()["new_cell_id"]
    assert _rows(client, other) == _rows(client, cid)
    for cell, word in ((cid, "mine"), (other, "theirs")):
        _write(client, cell, {"step.sh": _PRELUDE + f"cd proj && echo {word} > {word}.txt && git add {word}.txt "
                                                    f"&& git commit -q -m {word} && git log --format=%s > ../out.txt\n"})
        _run(client, runner, cell)
        assert _tip_text(client, cell, "out.txt") == f"{word}\nsecond\nfirst\n"
    assert _rows(client, cid)["proj"] != _rows(client, other)["proj"]
    _write(client, other, {"step.sh": _PRELUDE + "cd proj && git log --format=%s > ../out.txt\n"})
    _run(client, runner, other)
    assert _tip_text(client, other, "out.txt") == "theirs\nsecond\nfirst\n"


def test_while_a_run_holds_the_git_an_exclusive_generator_is_refused_and_others_get_a_read_only_git(client, remote):
    """A generator declaring `exclusive_repositories: true` (the Git generator
    does) is refused while another run holds the lease; any other run reads a
    read-only .git. The rule reads the manifest, never a generator id."""
    import collimate_server.git_store as git_store
    runner, _ = install_runner(generator="git_steps", runner="git-steps", entrypoint_body=_STEP)
    git_runner, _ = install_runner(generator="exclusive_steps", runner="exclusive-steps", entrypoint_body=_STEP,
                                   gen_manifest_extra="exclusive_repositories: true\n")
    cid = _cell(client, {"step.sh": _PRELUDE + f"git clone -q {remote} proj\n"})
    _run(client, runner, cid)
    _write(client, cid, {"hold.txt": "hold\n", "step.sh": _PRELUDE + "test -w proj/.git && echo held > out.txt\n"})
    holder = _run(client, runner, cid, done=False)
    _poll(lambda: _tip_text(client, cid, "out.txt") == "held\n", 90, "the holder's .git")
    assert _card(client, holder)["git"] == {"repositories": ["proj"], "access": "holder"}
    active = client.get(f"/api/channels/{client.channel_id}/runs/active").json()
    assert [r["git"] for r in active if r["run_id"] == holder] == [{"repositories": ["proj"], "access": "holder"}]

    refused = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": git_runner, "cell_id": cid})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["error"] == "repository_busy" and body["run_id"] == holder
    assert body["message"] == (f"git is in use by run {holder[:8]} on this cell; "
                               "Duplicate the cell for a second working copy")

    # The sibling reads its read-only .git; every write of git's fails.
    rows = _rows(client, cid)
    _write(client, cid, {"hold.txt": None, "step.sh": _PRELUDE + (
        "cd proj && { test -w .git || echo read-only; git status --porcelain=v1; git log --format=%s; "
        "git diff --stat HEAD~1 | tail -n 1; "
        "git commit -q --allow-empty -m mine 2>/dev/null && echo committed || echo commit refused; "
        "echo x > x.txt; git add x.txt 2>/dev/null && echo added || echo add refused; rm -f x.txt; "
        "git checkout -q -b mine 2>/dev/null && echo switched || echo checkout refused; } > ../side.txt 2>&1\n")})
    sibling = _run(client, runner, cid)
    assert _tip_text(client, cid, "side.txt") == (
        "read-only\nsecond\nfirst\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"
        "commit refused\nadd refused\ncheckout refused\n")
    assert _rows(client, cid) == rows and git_store.rescues_of(client.channel_id, cid) == {}
    readonly = (f"git is read-only in this run: run {holder[:8]} (git_steps) holds this cell's git; "
                "Duplicate the cell to commit")
    assert _log(client, sibling).count(readonly) == 1
    assert "git is in use" not in _log(client, sibling)
    assert _card(client, sibling)["git"] == {"repositories": ["proj"], "access": "readonly", "reason": readonly}

    stopped = client.delete(f"/api/channels/{client.channel_id}/runs/{holder}")
    assert stopped.status_code in (200, 202), stopped.text
    _ended(client, holder)
    assert set(_rows(client, cid)) == {"proj"}


def test_a_run_finds_the_band_ignored_the_formats_pinned_and_the_ceiling_above_its_workspace(client):
    runner, _ = install_runner(generator="git_steps", runner="git-steps", entrypoint_body=_STEP)
    cid = _cell(client, {"step.sh": "{ cat .collrun/.gitignore; echo \"$GIT_DEFAULT_REF_FORMAT $GIT_DEFAULT_HASH\"; "
                                    "echo \"$GIT_CEILING_DIRECTORIES\"; pwd -P; } > out.txt\n"})
    _run(client, runner, cid)
    band, formats, ceiling, workspace = _tip_text(client, cid, "out.txt").splitlines()
    assert band == "*" and formats == "files sha1"
    assert ceiling == os.path.dirname(workspace) and os.path.basename(ceiling) == "tmp"


def test_a_root_repository_comes_back_with_a_clean_status_and_its_log(client):
    runner, _ = install_runner(generator="git_steps", runner="git-steps", entrypoint_body=_STEP)
    cid = _cell(client, {"step.sh": _PRELUDE + "printf 'out.txt\\nstep.sh\\n' > .gitignore && echo a > a.txt "
                                    "&& git init -q . && git add .gitignore a.txt && git commit -q -m root "
                                    "&& git log --format=%s > out.txt\n"})
    first = _run(client, runner, cid)
    assert "git" not in _card(client, first)                # the cell had no repositories yet
    assert set(_rows(client, cid)) == {""}
    _write(client, cid, {"step.sh": _PRELUDE + "{ git status --porcelain=v1; git log --format=%s; } > out.txt 2>&1\n"})
    second = _run(client, runner, cid)
    assert _tip_text(client, cid, "out.txt") == "root\n"
    assert _card(client, second)["git"] == {"repositories": ["."], "access": "holder"}


def test_a_tracked_file_the_mirror_skips_stays_out_of_the_next_runs_deletions(client, tmp_path):
    source = init(tmp_path / "logged")
    commit(source, {"README.md": "hello\n", "build.log": "built\n"}, "first")
    bare = str(tmp_path / "logged.git")
    git(tmp_path, "clone", "-q", "--bare", source, bare)
    step = _STEP.replace("gen.live_files()", "gen.live_files(exclude=('.log',))")
    runner, _ = install_runner(generator="git_logs", runner="git-logs", entrypoint_body=step)
    cid = _cell(client, {"step.sh": _PRELUDE + f"git clone -q file://{bare} proj\n"})
    _run(client, runner, cid)
    assert _tip_text(client, cid, "proj/build.log") is None
    _write(client, cid, {"step.sh": _PRELUDE + "git -C proj status --porcelain=v1 > out.txt 2>&1\n"})
    _run(client, runner, cid)
    assert _tip_text(client, cid, "out.txt") == ""


def test_a_start_that_fails_after_its_claim_releases_the_lease(client, remote, monkeypatch):
    import collimate_server.git_harvest as git_harvest
    import collimate_server.repository_leases as repository_leases
    runner, _ = install_runner(generator="git_steps", runner="git-steps", entrypoint_body=_STEP)
    cid = _cell(client, {"step.sh": _PRELUDE + f"git clone -q {remote} proj\n"})
    _run(client, runner, cid)

    def broken(run, hydrated):
        assert repository_leases.holder(client.channel_id, cid) is run.lease is not None
        raise RuntimeError("materialize broke")

    monkeypatch.setattr(git_harvest, "materialize", broken)
    run_id = _run(client, runner, cid, done=False)

    def failed():
        conn = _db(client)
        try:
            row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        finally:
            conn.close()
        return row is not None and row[0] == "failed"
    _poll(failed, 60, "the start to fail")
    _poll(lambda: repository_leases.holder(client.channel_id, cid) is None, 30, "the lease to go")


def test_a_start_waits_for_a_releasing_lease_then_holds_the_git(client, remote):
    import threading
    import collimate_server.repository_leases as repository_leases
    runner, _ = install_runner(generator="git_steps", runner="git-steps", entrypoint_body=_STEP)
    cid = _cell(client, {"step.sh": _PRELUDE + f"git clone -q {remote} proj\n"})
    _run(client, runner, cid)
    _write(client, cid, {"step.sh": "if test -w proj/.git; then echo git; else echo none; fi > out.txt\n"})
    ending = "55555555-0000-4000-8000-000000000000"
    repository_leases.claim(client.channel_id, cid, ending, "ending runner", state=repository_leases.RELEASING)
    releaser = threading.Timer(2.0, repository_leases.release_run, args=(ending,))
    started = time.monotonic()
    releaser.start()
    try:
        run_id = _run(client, runner, cid)
    finally:
        releaser.cancel()
    assert time.monotonic() - started >= 2.0
    assert _tip_text(client, cid, "out.txt") == "git\n"
    assert "git is read-only" not in _log(client, run_id)
    assert _card(client, run_id)["git"]["access"] == "holder"


# _STEP whose held run outlives a graceful stop: on SIGTERM it lingers a few
# seconds before it ends, as a terminal or an agent winding down does.
_LINGERING_STEP = _STEP.replace("import os, subprocess\n", "import asyncio, os, signal, subprocess\n").replace(
    "            gen.phase('awaiting_input', 'holding')\n"
    "            await gen.wait_for_action_event(action_id='go', timeout=120)\n",
    "            loop, stopped = asyncio.get_running_loop(), asyncio.Event()\n"
    "            signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(stopped.set))\n"
    "            gen.phase('awaiting_input', 'holding')\n"
    "            await asyncio.wait_for(stopped.wait(), timeout=120)\n"
    "            await asyncio.sleep(5)\n")


def test_a_start_while_the_holder_is_being_stopped_waits_and_holds_the_git(client, remote):
    import collimate_server.repository_leases as repository_leases
    from collimate_server.run_registry import active_managers
    assert _LINGERING_STEP != _STEP
    runner, _ = install_runner(generator="git_lingers", runner="git-lingers", entrypoint_body=_LINGERING_STEP)
    cid = _cell(client, {"step.sh": _PRELUDE + f"git clone -q {remote} proj\n"})
    _run(client, runner, cid)
    _write(client, cid, {"hold.txt": "hold\n", "step.sh": _PRELUDE + "test -w proj/.git && echo held > out.txt\n"})
    holder = _run(client, runner, cid, done=False)
    _poll(lambda: _tip_text(client, cid, "out.txt") == "held\n", 90, "the holder's .git")
    _write(client, cid, {"hold.txt": None,
                         "step.sh": "if test -w proj/.git; then echo git; else echo none; fi > side.txt\n"})

    stopped = client.delete(f"/api/channels/{client.channel_id}/runs/{holder}")
    assert stopped.status_code in (200, 202), stopped.text
    lease = repository_leases.holder(client.channel_id, cid)
    assert lease is not None and lease.run_id == holder and lease.state == repository_leases.RELEASING
    assert active_managers[holder].process.returncode is None      # still winding down

    response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cid})
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    wait_spawned(run_id)
    assert lease.released.is_set()                                  # it started after the holder's harvest
    _ended(client, run_id)
    _ended(client, holder)
    assert _tip_text(client, cid, "side.txt") == "git\n"
    assert "git is read-only" not in _log(client, run_id)
    assert _card(client, run_id)["git"]["access"] == "holder"
    assert set(_rows(client, cid)) == {"proj"}
