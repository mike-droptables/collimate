"""Materialize (``api/collimate_server/git_materialize.py``): a state read from a real
repository is written back as a ``.git`` over the cell's files, for a run and
for an export, and git then sees what the run left: a clean status, the
conflicts, the operation to finish, the paths the cell cannot hold kept at
HEAD (skip-worktree), the links it may create, HEAD's exec bits. Every
repository is made with plain git under ``tmp_path``; the channel store is a
bare copy of its objects."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess

import pytest

import collimate_server.cas as cas
import collimate_server.git_host as git_host
import collimate_server.git_materialize as gm
import collimate_server.repository_state as rs
from git_repos import GIT_OK, commit, git, git_env, init

pytestmark = GIT_OK

NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    """The data dir (``.collimate/tmp`` for host git's homes)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(data)
    git_host.reset_capability()


def lookup(gitdir) -> rs.GitdirObjects:
    """Objects over a bare gitdir borrowing ``gitdir``'s, as a harvest's
    quarantine borrows the run's and the store's."""
    repo = os.path.dirname(str(gitdir))
    target = os.path.join(os.path.dirname(repo), f"{os.path.basename(repo)}-lookup.git")
    if not os.path.isdir(target):
        os.makedirs(target)
        git(target, "init", "-q", "--bare")
        with open(os.path.join(target, "objects", "info", "alternates"), "w") as fh:
            fh.write(os.path.join(str(gitdir), "objects") + "\n")
    shallow = os.path.join(str(gitdir), "shallow")
    return rs.GitdirObjects(target, shallow_file=shallow if os.path.exists(shallow) else None)


def harvest(repo, *, repo_path: str = "", kept_out=(), alternates=None) -> rs.RepositoryState:
    """The state of ``<repo>/.git``, as a harvest stores it."""
    gitdir = os.path.join(str(repo), ".git")
    read = rs.read_gitdir(gitdir, repo_path=repo_path, alternates=alternates)
    done = rs.normalize(read, lookup(gitdir), kept_out=kept_out, now=NOW)
    assert done.refusal is None, done.refusal
    assert done.state is not None
    return done.state


def store_of(repo, tmp_path) -> str:
    """A channel store holding every object of ``repo``: its absolute ``objects``."""
    store = tmp_path / "store.git"
    if not store.exists():
        git(tmp_path, "init", "-q", "--bare", str(store))
        shutil.rmtree(store / "objects")
        shutil.copytree(os.path.join(str(repo), ".git", "objects"), store / "objects")
    return str(store / "objects")


def hydrate(repo, work_tree, *, skip=()) -> set[str]:
    """Copy ``repo``'s work tree files (no ``.git``) as a cell hydrates
    them: regular files, 0644, no links. The paths written."""
    written = set()
    for root, dirs, files in os.walk(str(repo)):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            source = os.path.join(root, name)
            rel = os.path.relpath(source, str(repo)).replace(os.sep, "/")
            if os.path.islink(source) or rel in skip:
                continue
            target = os.path.join(str(work_tree), rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, 0o644)
            written.add(rel)
    os.makedirs(str(work_tree), exist_ok=True)
    return written


def materialize(repo, tmp_path, *, name="ws", skip=(), state=None, **kw) -> tuple[str, gm.Materialized]:
    """Harvest ``repo``, hydrate a fresh work tree from its files and write
    the ``.git``; the work tree and the result."""
    state = state or harvest(repo)
    work_tree = str(tmp_path / name)
    hydrated = hydrate(repo, work_tree, skip=skip)
    done = gm.write_gitdir(work_tree, state, store_of(repo, tmp_path), hydrated=hydrated, **kw)
    return work_tree, done


def status(work_tree) -> str:
    return git(work_tree, "status", "--porcelain=v1")


def flags(work_tree) -> dict[str, str]:
    """``git ls-files -v``'s tag per path (``S``: skip-worktree)."""
    out = git(work_tree, "ls-files", "-v", "-z")
    return {record[2:]: record[0] for record in out.split("\0") if record}


def leftovers(work_tree) -> list[str]:
    return [name for name in os.listdir(work_tree) if name.startswith(".git.materialize-")]


def conflicted(tmp_path):
    """A repository stopped in a merge: ``f`` both modified (UU), ``added``
    added and ``gone`` deleted by the other side, ``clean`` modified by it."""
    repo = init(tmp_path / "src")
    commit(repo, {"f": "base\n", "gone": "g\n", "clean": "c\n"}, "base")
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, {"f": "side\n", "added": "a\n", "gone": None, "clean": "c2\n"}, "side")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"f": "main\n"}, "main")
    git(repo, "merge", "side", check=False)
    assert os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD"))
    return repo


# --- round trip ------------------------------------------------------------------------------------


def test_a_clean_repository_round_trips_to_a_clean_status_and_its_own_hash(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"a.txt": "a\n", "dir/b.txt": "b\n", "run.sh": "#!/bin/sh\n"}, "one")
    git(repo, "update-index", "--chmod=+x", "run.sh")
    commit(repo, {}, "exec")
    git(repo, "tag", "v1")
    git(repo, "branch", "topic")
    git(repo, "remote", "add", "origin", "https://example.com/r.git")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(repo, "config", "branch.main.remote", "origin")
    git(repo, "config", "branch.main.merge", "refs/heads/main")
    state = harvest(repo)
    work_tree, done = materialize(repo, tmp_path, state=state)

    assert done.state_hash == state.manifest_hash and done.warnings == () and done.links == ()
    assert status(work_tree) == ""
    assert not leftovers(work_tree)
    assert git(work_tree, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")
    assert git(work_tree, "rev-parse", "--abbrev-ref", "@{upstream}").strip() == "origin/main"
    assert git(work_tree, "log", "--format=%s").split() == ["exec", "one"]
    assert git(work_tree, "tag").split() == ["v1"]
    gitdir = os.path.join(work_tree, ".git")
    assert open(os.path.join(gitdir, "objects", "info", "alternates")).read() == store_of(repo, tmp_path) + "\n"
    packed = open(os.path.join(gitdir, "packed-refs")).read()
    assert not packed.startswith("#") and "^" not in packed
    names = [line.split(" ")[1] for line in packed.splitlines()]
    assert names == sorted(names, key=str.encode)
    assert open(os.path.join(gitdir, "refs", "remotes", "origin", "HEAD")).read() == "ref: refs/remotes/origin/main\n"
    assert os.listdir(os.path.join(gitdir, "hooks")) == []
    config = git(work_tree, "config", "--file", os.path.join(gitdir, "config"), "--list")
    for line in ("core.bare=false", "core.logallrefupdates=true", "gc.auto=0", "maintenance.auto=false",
                 "core.fsmonitor=false", "core.filemode=true", "remote.origin.url=https://example.com/r.git"):
        assert line in config.splitlines()
    exclude = open(os.path.join(gitdir, "info", "exclude")).read().splitlines()
    assert exclude[:5] == [".collkeep", "/.collrun/", "/_runs/", "/_agent/", "/.pydantic-ai-harness/"]

    back = rs.normalize(rs.read_gitdir(gitdir, alternates=store_of(repo, tmp_path)), lookup(gitdir), now=NOW)
    assert back.state is not None and back.state.manifest_hash == state.manifest_hash


def test_a_nested_repository_gets_only_its_own_host_lines(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"a": "a\n"})
    with open(os.path.join(repo, ".git", "info", "exclude"), "a") as fh:
        fh.write("mine")                                                    # no final LF
    state = harvest(repo, repo_path="sub")
    work_tree = str(tmp_path / "cell" / "sub")
    hydrated = {f"sub/{path}" for path in hydrate(repo, work_tree)}
    done = gm.write_gitdir(work_tree, state, store_of(repo, tmp_path), repo_path="sub", hydrated=hydrated)
    exclude = open(os.path.join(work_tree, ".git", "info", "exclude"), "rb").read()
    assert exclude.startswith(b".collkeep\n#") and exclude.endswith(b"\nmine") and b"/_runs/" not in exclude
    assert status(work_tree) == ""
    back = rs.normalize(rs.read_repository(str(tmp_path / "cell"), "sub", alternates=store_of(repo, tmp_path)),
                        lookup(os.path.join(work_tree, ".git")), now=NOW)
    assert back.state is not None and back.state.manifest_hash == done.state_hash


def test_an_unborn_repository_gets_an_empty_index(tmp_path):
    repo = init(tmp_path / "src")
    with open(os.path.join(repo, "new.txt"), "w") as fh:
        fh.write("n\n")
    work_tree, done = materialize(repo, tmp_path)
    assert status(work_tree) == "?? new.txt\n"
    assert git(work_tree, "symbolic-ref", "HEAD").strip() == "refs/heads/main"


# --- operations carry over (P6) ----------------------------------------------------------------------


def test_a_merge_conflict_carries_over_and_commits_with_two_parents_after_add(tmp_path):
    repo = conflicted(tmp_path)
    work_tree, done = materialize(repo, tmp_path)
    assert status(work_tree) == status(repo)
    assert {line[:2] for line in status(work_tree).splitlines()} >= {"UU", "A ", "D ", "M "}
    assert open(os.path.join(work_tree, "f")).read() == open(os.path.join(repo, "f")).read()
    refused = subprocess.run(["git", "commit", "--no-edit"], cwd=work_tree, capture_output=True,
                             env=git_env(work_tree))
    assert refused.returncode != 0
    with open(os.path.join(work_tree, "f"), "w") as fh:
        fh.write("resolved\n")
    git(work_tree, "add", "-A", "--", "f")
    git(work_tree, "commit", "--no-edit")
    parents = git(work_tree, "rev-list", "--parents", "-1", "HEAD").split()
    assert len(parents) == 3
    assert git(work_tree, "show", "HEAD:f") == "resolved\n"


def test_a_cherry_pick_conflict_carries_over_and_continues(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"f": "base\n"}, "base")
    git(repo, "checkout", "-q", "-b", "side")
    picked = commit(repo, {"f": "side\n"}, "side")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"f": "main\n"}, "main")
    git(repo, "cherry-pick", picked, check=False)
    assert os.path.exists(os.path.join(repo, ".git", "CHERRY_PICK_HEAD"))
    work_tree, _ = materialize(repo, tmp_path)
    assert status(work_tree) == status(repo) == "UU f\n"
    with open(os.path.join(work_tree, "f"), "w") as fh:
        fh.write("both\n")
    git(work_tree, "add", "-A", "--", "f")
    git(work_tree, "cherry-pick", "--continue")
    assert git(work_tree, "log", "-1", "--format=%s").strip() == "side"
    assert not os.path.exists(os.path.join(work_tree, ".git", "CHERRY_PICK_HEAD"))


def test_a_stopped_rebase_carries_over_and_continues(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"f": "base\n"}, "base")
    git(repo, "checkout", "-q", "-b", "topic")
    commit(repo, {"f": "topic\n"}, "topic one")
    commit(repo, {"g": "g\n"}, "topic two")
    git(repo, "checkout", "-q", "main")
    commit(repo, {"f": "main\n"}, "main")
    git(repo, "checkout", "-q", "topic")
    git(repo, "rebase", "main", check=False)
    assert os.path.isdir(os.path.join(repo, ".git", "rebase-merge"))
    work_tree, _ = materialize(repo, tmp_path)
    assert status(work_tree) == status(repo)
    assert "rebase in progress" in git(work_tree, "status")
    with open(os.path.join(work_tree, "f"), "w") as fh:
        fh.write("rebased\n")
    git(work_tree, "add", "-A", "--", "f")
    git(work_tree, "rebase", "--continue")
    assert git(work_tree, "log", "--format=%s", "-3").split("\n")[:3] == ["topic two", "topic one", "main"]
    assert git(work_tree, "symbolic-ref", "HEAD").strip() == "refs/heads/topic"


def test_a_stash_pop_conflict_carries_over_with_its_stash(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"f": "base\n"}, "base")
    with open(os.path.join(repo, "f"), "w") as fh:
        fh.write("stashed\n")
    git(repo, "stash")
    commit(repo, {"f": "moved on\n"}, "moved")
    git(repo, "stash", "pop", check=False)
    assert status(repo) == "UU f\n"
    work_tree, _ = materialize(repo, tmp_path)
    assert status(work_tree) == "UU f\n"
    assert git(work_tree, "stash", "list").count("stash@{") == 1
    git(work_tree, "add", "-A", "--", "f")
    assert status(work_tree) == "M  f\n"


def test_progress_files_are_the_last_write_with_no_git_after(tmp_path, monkeypatch):
    repo = conflicted(tmp_path)
    git(repo, "remote", "add", "origin", "https://example.com/r.git")
    state = harvest(repo)
    events = []
    real_run, real_write = git_host.run, gm.fs.write_file

    def run(args, **kw):
        events.append(("git", " ".join(args[:2])))
        return real_run(args, **kw)

    def write_file(root, path, content):
        events.append(("write", path))
        return real_write(root, path, content)

    monkeypatch.setattr(git_host, "run", run)
    monkeypatch.setattr(gm.fs, "write_file", write_file)
    work_tree, _ = materialize(repo, tmp_path, state=state)
    progress = [n for n, (kind, path) in enumerate(events) if kind == "write" and path.startswith(("MERGE_", "ORIG_"))]
    assert progress, events
    first = progress[0]
    assert all(kind == "write" for kind, _ in events[first:])
    assert [path for _, path in events[first:]] == sorted(path for _, path in events[first:])
    refresh = max(n for n, event in enumerate(events) if event == ("git", "update-index -q"))
    adds = [n for n, (kind, what) in enumerate(events) if kind == "git" and what == "config --file"]
    assert adds and refresh < min(adds) and max(adds) < first
    assert status(work_tree) == status(repo)


def test_a_progress_set_the_recheck_refuses_is_not_written(tmp_path):
    repo = conflicted(tmp_path)
    state = harvest(repo)
    parts = state.named_parts()
    parts["progress/rebase-merge/git-rebase-todo"] = b"pick 0123456 one\nexec rm -rf /\n"
    parts["progress/rebase-merge/head-name"] = b"refs/heads/main\n"
    tampered = rs.make_state(state.manifest()["head"], parts)
    work_tree, done = materialize(repo, tmp_path, state=tampered)
    gitdir = os.path.join(work_tree, ".git")
    assert not os.path.exists(os.path.join(gitdir, "rebase-merge"))
    assert not os.path.exists(os.path.join(gitdir, "MERGE_HEAD"))
    assert any("exec" in warning for warning in done.warnings)
    assert "UU" not in status(work_tree)                                    # the index was not replayed either


# --- config and filters ------------------------------------------------------------------------------


def test_no_filter_driver_runs_while_materializing(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f"[filter \"x\"]\n\tclean = touch {marker}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    repo = init(tmp_path / "src")
    commit(repo, {".gitattributes": "* filter=x\n", "a.txt": "a\n"})
    state = harvest(repo)
    parts = state.named_parts()
    config = json.loads(parts.get("config", b"[]"))
    parts["config"] = rs.canonical_json(sorted([*config, ["filter.x.clean", f"touch {marker}"],
                                                ["core.hooksPath", "/tmp/hooks"]]))
    tampered = rs.make_state(state.manifest()["head"], parts)
    work_tree, done = materialize(repo, tmp_path, state=tampered)
    assert not marker.exists()
    written = open(os.path.join(work_tree, ".git", "config")).read()
    assert "filter" not in written and "hooksPath" not in written.lower()
    assert any("core.hooksPath" in line and "filter.x.clean" in line for line in done.lines)
    assert not marker.exists()


def test_the_filter_guard_freezes_filtered_files_the_cell_does_not_hold_as_head(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {".gitattributes": "*.dat filter=lfs\n", "same.dat": "pointer\n", "differs.dat": "pointer\n",
                  "missing.dat": "pointer\n", "plain.txt": "p\n"})
    with open(os.path.join(repo, "differs.dat"), "w") as fh:
        fh.write("the real content\n")
    work_tree, done = materialize(repo, tmp_path, skip={"missing.dat"})
    tags = flags(work_tree)
    assert tags["differs.dat"] == "S" and tags["missing.dat"] == "S"
    assert tags["same.dat"] == "H" and tags["plain.txt"] == "H"
    assert status(work_tree) == ""
    assert sum("filtered files" in line for line in done.lines) == 1


def test_the_filter_guard_freezes_a_link_or_fifo_at_a_filtered_path_without_reading_it(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {".gitattributes": "*.dat filter=lfs\n", "fifo.dat": "pointer\n", "link.dat": "pointer\n",
                  "secret": "pointer\n"})
    work_tree = str(tmp_path / "ws")
    hydrated = hydrate(repo, work_tree)
    os.unlink(os.path.join(work_tree, "fifo.dat"))
    os.mkfifo(os.path.join(work_tree, "fifo.dat"))                         # an open() would block here
    os.unlink(os.path.join(work_tree, "link.dat"))
    os.symlink("secret", os.path.join(work_tree, "link.dat"))               # its target equals HEAD's blob
    done = gm.write_gitdir(work_tree, harvest(repo), store_of(repo, tmp_path), hydrated=hydrated)
    tags = flags(work_tree)
    assert tags["fifo.dat"] == "S" and tags["link.dat"] == "S" and tags["secret"] == "H"
    assert any("2 filtered files frozen" in line for line in done.lines)


def test_a_sparse_checkout_is_reapplied(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"keep/a": "a\n", "out/b": "b\n", "top": "t\n"})
    git(repo, "config", "core.sparseCheckout", "true")
    with open(os.path.join(repo, ".git", "info", "sparse-checkout"), "w") as fh:
        fh.write("/keep/\n/top\n")
    git(repo, "read-tree", "-mu", "HEAD")
    assert not os.path.exists(os.path.join(repo, "out", "b"))
    state = harvest(repo)
    work_tree, done = materialize(repo, tmp_path, state=state)
    assert flags(work_tree)["out/b"] == "S"
    assert status(work_tree) == ""
    config = open(os.path.join(work_tree, ".git", "config")).read().lower()
    assert config.count("sparsecheckout = true") == 1
    gitdir = os.path.join(work_tree, ".git")
    back = rs.normalize(rs.read_gitdir(gitdir, alternates=store_of(repo, tmp_path)), lookup(gitdir), now=NOW)
    assert back.state is not None and back.state.manifest_hash == state.manifest_hash


# --- failure is per repository -----------------------------------------------------------------------


def test_a_held_index_lock_fails_exactly_one_repository(tmp_path, monkeypatch):
    first = init(tmp_path / "one")
    commit(first, {"a": "a\n"})
    second = init(tmp_path / "two")
    commit(second, {"b": "b\n"})
    real_run = git_host.run

    def run(args, **kw):
        if list(args[:3]) == ["update-index", "-q", "--refresh"] and "/ws-one/" in (kw.get("git_dir") or ""):
            open(os.path.join(kw["git_dir"], "index.lock"), "w").close()
        return real_run(args, **kw)

    monkeypatch.setattr(git_host, "run", run)
    with pytest.raises(gm.MaterializeFailed) as failed:
        materialize(first, tmp_path / "a", name="ws-one")
    assert failed.value.step == "refresh"
    assert "exited 128" in failed.value.reason
    assert failed.value.lines[-1].startswith(".: materialize failed at refresh: git update-index exited 128")
    assert failed.value.entry() == {"reason": failed.value.reason}
    one = str(tmp_path / "a" / "ws-one")
    assert not os.path.exists(os.path.join(one, ".git")) and not leftovers(one)
    work_tree, _ = materialize(second, tmp_path / "b", name="ws-two")
    assert status(work_tree) == ""


@pytest.mark.parametrize("step,args", [("read-tree", ["read-tree"]),
                                       ("skip-worktree", ["update-index", "-z", "--skip-worktree"])])
def test_a_failure_at_any_step_removes_the_partial_directory(tmp_path, monkeypatch, step, args):
    repo = init(tmp_path / "src")
    commit(repo, {"a": "a\n", "big": "x" * 2000})
    monkeypatch.setattr(cas, "MAX_BLOB_BYTES", 1000)                        # so skip-worktree has a path
    real_run = git_host.run

    def run(argv, **kw):
        if list(argv[:len(args)]) == args:
            open(os.path.join(kw["git_dir"], "index.lock"), "w").close()
        return real_run(argv, **kw)

    monkeypatch.setattr(git_host, "run", run)
    with pytest.raises(gm.MaterializeFailed) as failed:
        materialize(repo, tmp_path)
    assert failed.value.step == step and "exited 128" in failed.value.reason
    work_tree = str(tmp_path / "ws")
    assert not os.path.exists(os.path.join(work_tree, ".git")) and not leftovers(work_tree)


def test_a_malformed_part_fails_its_repository_and_leaves_nothing(tmp_path):
    repo = conflicted(tmp_path)
    state = harvest(repo)
    parts = state.named_parts()
    parts["index"] = "100644 \u00e9{} 0\tf\0".format("a" * 39).encode("utf-8")
    with pytest.raises(gm.MaterializeFailed) as failed:
        materialize(repo, tmp_path, state=rs.make_state(state.manifest()["head"], parts))
    assert failed.value.step == "objects"
    assert failed.value.lines[-1].startswith(".: materialize failed at objects: ")
    work_tree = str(tmp_path / "ws")
    assert not os.path.exists(os.path.join(work_tree, ".git")) and not leftovers(work_tree)
    broken = rs.make_state(state.manifest()["head"], {**state.named_parts(), "config": b"not json"})
    with pytest.raises(gm.MaterializeFailed, match="JSONDecodeError") as failed:
        gm.write_gitdir(str(tmp_path / "ws2"), broken, store_of(repo, tmp_path))
    assert failed.value.step == "files"


def test_history_missing_from_the_store_leaves_the_repository_out(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"a": "a\n"})
    state = harvest(repo)
    git(tmp_path, "init", "-q", "--bare", str(tmp_path / "empty.git"))
    work_tree = str(tmp_path / "ws")
    hydrated = hydrate(repo, work_tree)
    with pytest.raises(gm.MaterializeFailed) as failed:
        gm.write_gitdir(work_tree, state, str(tmp_path / "empty.git" / "objects"), hydrated=hydrated)
    assert failed.value.step == "objects"
    assert failed.value.reason == "repository history incomplete in the channel store: ."
    assert not os.path.exists(os.path.join(work_tree, ".git")) and not leftovers(work_tree)


def test_an_existing_dotgit_is_never_replaced(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"a": "a\n"})
    work_tree = tmp_path / "ws"
    (work_tree / ".git").mkdir(parents=True)
    with pytest.raises(gm.MaterializeFailed, match="already exists"):
        gm.write_gitdir(str(work_tree), harvest(repo), store_of(repo, tmp_path))
    assert os.listdir(work_tree / ".git") == []


# --- skip-worktree (§4.3 step 8, §4.10) -----------------------------------------------------------------


def _upstream_with_big_file(tmp_path, monkeypatch):
    """An upstream holding ``big.bin`` over the (lowered) blob cap, and a
    clone of it; the clone's work tree, as a cell, cannot hold the file."""
    monkeypatch.setattr(cas, "MAX_BLOB_BYTES", 1000)
    upstream = init(tmp_path / "upstream")
    commit(upstream, {"big.bin": "x" * 2000, "small.txt": "s\n"}, "one")
    git(tmp_path, "clone", "-q", f"file://{upstream}", str(tmp_path / "src"))
    return upstream, str(tmp_path / "src")


def test_an_oversized_blob_stays_skip_worktree_after_an_ff_pull(tmp_path, monkeypatch):
    upstream, repo = _upstream_with_big_file(tmp_path, monkeypatch)
    work_tree, done = materialize(repo, tmp_path, skip={"big.bin"})
    assert flags(work_tree)["big.bin"] == "S"
    assert any("files over 1000 bytes: big.bin" in line for line in done.lines)
    assert status(work_tree) == ""
    commit(upstream, {"big.bin": "y" * 2000, "small.txt": "t\n"}, "two")
    git(work_tree, "pull", "-q", "--ff-only")
    assert git(work_tree, "log", "-1", "--format=%s").strip() == "two"
    assert flags(work_tree)["big.bin"] == "S"
    assert status(work_tree) == ""

    # Its edits hide until the bit is cleared.
    with open(os.path.join(work_tree, "big.bin"), "w") as fh:
        fh.write("edited")
    assert status(work_tree) == ""
    git(work_tree, "update-index", "--no-skip-worktree", "big.bin")
    assert status(work_tree) == " M big.bin\n"


def test_a_kept_out_path_is_marked_again(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"kept.txt": "k\n", "deleted.txt": "d\n"})
    state = harvest(repo, kept_out=["kept.txt"])
    work_tree, done = materialize(repo, tmp_path, state=state, skip={"kept.txt", "deleted.txt"})
    assert flags(work_tree)["kept.txt"] == "S"
    assert status(work_tree) == " D deleted.txt\n"                       # a deletion still shows
    assert any("kept out" in line for line in done.lines)


def test_a_conflicting_pull_clears_the_bit(tmp_path, monkeypatch):
    upstream, repo = _upstream_with_big_file(tmp_path, monkeypatch)
    commit(repo, {"big.bin": "local " * 400}, "local")
    commit(upstream, {"big.bin": "theirs " * 400}, "theirs")
    work_tree, _ = materialize(repo, tmp_path, skip={"big.bin"})
    assert flags(work_tree)["big.bin"] == "S"
    git(work_tree, "pull", "-q", "--no-rebase", check=False)
    assert status(work_tree) == "UU big.bin\n"
    assert "S" not in {tag for path, tag in flags(work_tree).items() if path == "big.bin"}


def test_paths_a_cell_cannot_hold_and_gitlinks_are_skip_worktree(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"ok.txt": "o\n", "node_modules/pkg/index.js": "x\n", "deep/__pycache__/m.pyc": "c\n",
                  "_runs/r/out.txt": "r\n", "back\\slash": "b\n"})
    sub = commit(init(tmp_path / "other"), {"x": "x\n"})
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sub},vendor/lib")
    commit(repo, {}, "gitlink")
    cell = {"ok.txt"}
    state = harvest(repo)
    work_tree = str(tmp_path / "ws")
    os.makedirs(work_tree)
    shutil.copyfile(os.path.join(repo, "ok.txt"), os.path.join(work_tree, "ok.txt"))
    done = gm.write_gitdir(work_tree, state, store_of(repo, tmp_path), hydrated=cell)
    tags = flags(work_tree)
    assert tags["ok.txt"] == "H"
    for path in ("node_modules/pkg/index.js", "deep/__pycache__/m.pyc", "_runs/r/out.txt", "back\\slash",
                 "vendor/lib"):
        assert tags[path] == "S", path
    assert status(work_tree) == ""
    assert any("gitlinks" in line for line in done.lines)


# --- modes and links ---------------------------------------------------------------------------------


def test_heads_exec_bits_are_applied_both_ways(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"run.sh": "#!/bin/sh\n", "plain.txt": "p\n"})
    git(repo, "update-index", "--chmod=+x", "run.sh")
    commit(repo, {}, "exec")
    work_tree = str(tmp_path / "ws")
    hydrated = hydrate(repo, work_tree)
    os.chmod(os.path.join(work_tree, "plain.txt"), 0o755)
    gm.write_gitdir(work_tree, harvest(repo), store_of(repo, tmp_path), hydrated=hydrated)
    assert stat.S_IMODE(os.stat(os.path.join(work_tree, "run.sh")).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(os.path.join(work_tree, "plain.txt")).st_mode) == 0o644
    assert status(work_tree) == ""


def test_links_are_created_only_where_every_condition_holds(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"dir/file": "f\n", "covered": "c\n"})
    links = {"ok": "dir/file", "dir/up": "../covered", "abs": "/etc/passwd", "out": "../outside",
             "dotgit": ".git/config", "ngit": "dir/.GIT/x", "nm": "node_modules/x", "band": "_runs/x",
             "dir/agent": "../_agent/x", "node_modules/l": "../dir/file"}
    for path, target in links.items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        os.symlink(target, full)
    git(repo, "add", "-A")
    os.unlink(os.path.join(repo, "covered"))
    os.makedirs(os.path.join(repo, "covered"))
    with open(os.path.join(repo, "covered", "x"), "w") as fh:     # a cell file under where a link would go
        fh.write("x\n")
    git(repo, "rm", "-q", "--cached", "covered")
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{git(repo, 'hash-object', '-w', '--stdin', input=b'dir/file').strip()},covered")
    git(repo, "commit", "-q", "-m", "links")
    state = harvest(repo)
    work_tree = str(tmp_path / "ws")
    hydrated = hydrate(repo, work_tree)
    assert "covered/x" in hydrated
    done = gm.write_gitdir(work_tree, state, store_of(repo, tmp_path), hydrated=hydrated)
    planned = {link.path: link.target for link in done.links}
    assert planned == {"ok": b"dir/file", "dir/up": b"../covered"}
    assert gm.create_links(work_tree, done.links) == []
    assert os.readlink(os.path.join(work_tree, "ok")) == "dir/file"
    assert os.readlink(os.path.join(work_tree, "dir", "up")) == "../covered"
    tags = flags(work_tree)
    for path in ("abs", "out", "dotgit", "ngit", "nm", "band", "dir/agent", "node_modules/l", "covered"):
        assert tags[path] == "S", path
        assert not os.path.islink(os.path.join(work_tree, path))
    assert os.path.isfile(os.path.join(work_tree, "covered", "x"))
    assert status(work_tree) == ""


def test_a_link_resolving_through_another_link_is_not_created(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"d/file": "f\n"})
    links = {"y": ".", "x": "y/..", "d/y": "..", "z": "d/y/..", "ok": "y", "fine": "d/file"}
    for path, target in links.items():
        os.symlink(target, os.path.join(repo, path))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "links")
    work_tree = str(tmp_path / "ws")
    hydrated = hydrate(repo, work_tree)
    done = gm.write_gitdir(work_tree, harvest(repo), store_of(repo, tmp_path), hydrated=hydrated)
    assert {link.path for link in done.links} == {"y", "d/y", "ok", "fine"}
    assert gm.create_links(work_tree, done.links) == []
    tags = flags(work_tree)
    assert tags["x"] == "S" and tags["z"] == "S"
    for link in done.links:
        resolved = os.path.realpath(os.path.join(work_tree, link.path))
        assert resolved == os.path.realpath(work_tree) or resolved.startswith(os.path.realpath(work_tree) + "/")
    assert status(work_tree) == ""


def test_a_link_under_a_linked_ancestor_or_over_a_file_is_not_created(tmp_path):
    work_tree = tmp_path / "ws"
    (work_tree / "real").mkdir(parents=True)
    os.symlink("real", work_tree / "a")
    (work_tree / "taken").write_text("mine\n")
    lines = gm.create_links(str(work_tree), [gm.Link("a/b", b"../x"), gm.Link("taken", b"x"),
                                            gm.Link("new/dir/c", b"../d")], repo_path="sub")
    assert not os.path.lexists(work_tree / "real" / "b")
    assert (work_tree / "taken").read_text() == "mine\n"
    assert os.readlink(work_tree / "new" / "dir" / "c") == "../d"
    assert len(lines) == 1 and lines[0].startswith("sub: links not created: a/b") and "taken" in lines[0]


@pytest.mark.parametrize("path,target,ok", [
    ("l", b"t", True),
    ("d/l", b"../t", True),
    ("l", b".", True),
    ("l", b"..", False),
    ("d/l", b"../../t", False),
    ("l", b"/abs", False),
    ("l", b"", False),
    ("l", b"\xff", False),
    ("l", b"a\\b", False),
    ("l", b".git", False),
    ("l", b"x/git~1/y", False),
    ("l", b".collrun/x", False),
    ("l", b"_agent", False),
    ("l", b".pydantic-ai-harness/x", False),
    ("l", b"a/.venv/b", False),
    ("__pycache__/l", b"../t", False),
    ("l", b"d/../.git/x", False),
    ("x", b"y/..", False),
    ("x", b"Y/a", False),
    ("x", b"y", True),
    ("x", b"d/y/..", False),
    ("x", b"d/y", True),
    ("d/x", b"../y/../t", False),
    ("d/x", b"../t", True),
])
def test_the_link_conditions(path, target, ok):
    assert gm._link_ok(path, target, "", set(), frozenset({"y", "d/y"})) is ok


def test_a_cell_file_at_or_under_a_link_refuses_it():
    assert gm._link_ok("l", b"t", "", gm._occupied({"l"}), frozenset()) is False
    assert gm._link_ok("l", b"t", "", gm._occupied({"l/x"}), frozenset()) is False
    assert gm._link_ok("l", b"t", "sub", gm._occupied({"sub/l/x/y"}), frozenset()) is False
    assert gm._link_ok("l", b"t", "sub", gm._occupied({"l/x", "sub/lx", "sub/m/l"}), frozenset()) is True
    assert gm._occupied({"a/b/c", "a/d"}) == {"a/b/c", "a/b", "a", "a/d"}


# --- export (§7, R13) ------------------------------------------------------------------------------------


def export(repo, tmp_path, *, name="export", state=None, skip=(), excluded=(), shallow=None) -> str:
    """Export ``repo``'s state as the exporter will: its files staged at
    their own names (but ``skip``), a pack of its roots from the store, the
    ``.git`` written in export mode. The export folder."""
    state = state or harvest(repo)
    objects = store_of(repo, tmp_path)
    folder = str(tmp_path / name)
    hydrated = hydrate(repo, folder, skip=skip)
    roots = sorted(rs.roots(state))
    pack = None
    if roots:
        env = {"GIT_SHALLOW_FILE": shallow} if shallow else {}
        done = subprocess.run(["git", f"--git-dir={os.path.dirname(objects)}", "pack-objects", "--revs", "--stdout",
                               "-q"], input="".join(f"{sha}\n" for sha in roots).encode(), capture_output=True,
                              env=git_env(str(tmp_path), env), check=True)
        pack = str(tmp_path / f"{name}.pack")
        with open(pack, "wb") as fh:
            fh.write(done.stdout)
    staging = str(tmp_path / "data" / ".collimate" / "tmp" / f"export-{name}" / "0")
    gm.write_gitdir(folder, state, objects, hydrated=hydrated, mode="export", pack=pack, staging=staging,
                    excluded=excluded)
    assert not os.path.exists(staging)
    return folder


def check_export(folder) -> dict[str, str]:
    gitdir = os.path.join(folder, ".git")
    assert not os.path.exists(os.path.join(gitdir, "objects", "info", "alternates"))
    git(folder, "fsck", "--full", "--no-progress")
    assert status(folder) == ""
    config = git(folder, "config", "--file", os.path.join(gitdir, "config"), "--list").splitlines()
    for line in ("core.repositoryformatversion=0", "core.bare=false", "core.filemode=false",
                 "core.logallrefupdates=true"):
        assert line in config
    assert not any(line.startswith(("gc.", "maintenance.", "core.fsmonitor")) for line in config)
    tags = flags(folder)
    modes = {record.split("\t", 1)[1]: record.split(" ")[0]
             for record in git(folder, "ls-files", "-s", "-z").split("\0") if record}
    for path, mode in modes.items():
        if mode in ("120000", "160000"):
            assert tags[path] == "S", path
    return tags


def test_an_exported_clean_repository_is_clean_and_whole(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"a.txt": "a\n", "run.sh": "#!/bin/sh\n"})
    git(repo, "update-index", "--chmod=+x", "run.sh")
    commit(repo, {}, "exec")
    git(repo, "remote", "add", "origin", "https://example.com/r.git")
    folder = export(repo, tmp_path)
    check_export(folder)
    assert git(folder, "remote", "get-url", "origin").strip() == "https://example.com/r.git"
    assert git(folder, "log", "--format=%s").split() == ["exec", "change"]
    exclude = open(os.path.join(folder, ".git", "info", "exclude")).read().splitlines()
    assert "/.collimate-provenance.json" in exclude
    packs = os.listdir(os.path.join(folder, ".git", "objects", "pack"))
    assert any(name.startswith("pack-") and name.endswith(".idx") for name in packs)
    assert not any(name.startswith("incoming") for name in packs)


def test_an_exported_repository_with_links_and_a_gitlink_marks_them(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"dir/file": "f\n"})
    os.symlink("dir/file", os.path.join(repo, "link"))
    sub = commit(init(tmp_path / "other"), {"x": "x\n"})
    git(repo, "add", "link")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sub},vendor")
    git(repo, "commit", "-q", "-m", "links")
    tags = check_export(export(repo, tmp_path))
    assert tags["link"] == "S" and tags["vendor"] == "S" and tags["dir/file"] == "H"


def test_an_exported_oversized_and_case_colliding_repository_is_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(cas, "MAX_BLOB_BYTES", 1000)
    repo = init(tmp_path / "src")
    commit(repo, {"big.bin": "x" * 2000, "README": "upper\n", "readme": "lower\n", "a": "a\n"})
    folder = export(repo, tmp_path, skip={"big.bin", "readme"}, excluded={"readme"})
    tags = check_export(folder)
    assert tags["big.bin"] == "S" and tags["readme"] == "S" and tags["README"] == "H"


def test_an_exported_shallow_repository_keeps_its_boundary(tmp_path):
    upstream = init(tmp_path / "upstream")
    for n in range(3):
        commit(upstream, {"f": f"{n}\n"}, f"c{n}")
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{upstream}", str(tmp_path / "src"))
    repo = str(tmp_path / "src")
    state = harvest(repo)
    assert state.part("shallow")
    folder = export(repo, tmp_path, state=state, shallow=os.path.join(repo, ".git", "shallow"))
    check_export(folder)
    assert git(folder, "rev-list", "--count", "HEAD").strip() == "1"
    assert open(os.path.join(folder, ".git", "shallow"), "rb").read() == state.part("shallow")


def test_an_exported_sparse_repository_is_clean_and_keeps_its_files(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"keep/a": "a\n", "out/b": "b\n", "out/c": "c\n", "top": "t\n"})
    git(repo, "config", "core.sparseCheckout", "true")
    with open(os.path.join(repo, ".git", "info", "sparse-checkout"), "w") as fh:
        fh.write("/keep/\n/top\n")
    git(repo, "read-tree", "-mu", "HEAD")
    os.makedirs(os.path.join(repo, "out"))
    with open(os.path.join(repo, "out", "c"), "w") as fh:                  # a cell file outside the patterns
        fh.write("c\n")
    folder = export(repo, tmp_path)
    tags = check_export(folder)
    assert tags["out/b"] == "S" and tags["keep/a"] == "H"
    assert open(os.path.join(folder, "out", "c")).read() == "c\n"


def test_an_exports_provenance_line_is_not_read_back(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"a": "a\n"})
    with open(os.path.join(repo, ".git", "info", "exclude"), "a") as fh:
        fh.write("mine\n")
    state = harvest(repo)
    folder = export(repo, tmp_path)
    exclude = open(os.path.join(folder, ".git", "info", "exclude")).read().splitlines()
    assert exclude.count("/.collimate-provenance.json") == 1
    read = rs.read_gitdir(os.path.join(folder, ".git"))
    assert read.info["exclude"] == state.part("exclude")


def test_an_export_is_sanitized_again_with_the_secrets_bound_now(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"a": "a\n"}, "uses tok-123456")
    git(repo, "remote", "add", "origin", "https://example.com/tok-123456/r.git")
    state = harvest(repo)
    assert b"tok-123456" in state.part("reflogs/HEAD") and b"tok-123456" in state.part("config")
    objects = store_of(repo, tmp_path)
    folder = str(tmp_path / "export")
    hydrated = hydrate(repo, folder)
    staging = str(tmp_path / "data" / ".collimate" / "tmp" / "export-s" / "0")
    done = subprocess.run(["git", f"--git-dir={os.path.dirname(objects)}", "pack-objects", "--revs", "--stdout",
                           "-q"], input="".join(f"{sha}\n" for sha in sorted(rs.roots(state))).encode(),
                          capture_output=True, env=git_env(str(tmp_path)), check=True)
    pack = tmp_path / "s.pack"
    pack.write_bytes(done.stdout)
    written = gm.write_gitdir(folder, state, objects, hydrated=hydrated, mode="export", pack=str(pack),
                              staging=staging, secrets=["tok-123456"])
    gitdir = os.path.join(folder, ".git")
    for root, _, files in os.walk(gitdir):
        for name in files:
            if "objects" not in root:
                assert b"tok-123456" not in open(os.path.join(root, name), "rb").read(), name
    assert "https://example.com/[redacted]/r.git" in git(folder, "remote", "get-url", "origin")
    assert any("config remote.origin.url: credentials removed" in line for line in written.lines)
    assert any("reflog HEAD: credentials removed" in line for line in written.lines)
    assert written.state_hash != state.manifest_hash
    check_export(folder)


def test_an_exported_merge_in_progress_keeps_its_conflicts(tmp_path):
    repo = conflicted(tmp_path)
    folder = export(repo, tmp_path)
    git(folder, "fsck", "--full", "--no-progress")
    assert status(folder) == status(repo)


def test_omitting_local_history_drops_reflogs_stash_and_orig_head(tmp_path):
    repo = init(tmp_path / "src")
    commit(repo, {"f": "one\n"})
    with open(os.path.join(repo, "f"), "w") as fh:
        fh.write("stashed\n")
    git(repo, "stash")
    commit(repo, {"g": "g\n"}, "two")
    git(repo, "update-ref", "ORIG_HEAD", "HEAD~1")
    state = harvest(repo)
    assert "refs/stash" in rs.parse_refs(state.part("refs") or b"")
    assert gm.export_state(state, omit_history=False) is state
    slim = gm.export_state(state, omit_history=True)
    names = slim.named_parts()
    assert not any(name.startswith("reflogs/") for name in names)
    assert "progress/ORIG_HEAD" not in names
    assert "refs/stash" not in rs.parse_refs(slim.part("refs") or b"")
    assert slim.manifest()["head"] == state.manifest()["head"]
    folder = export(repo, tmp_path, state=slim)
    check_export(folder)
    gitdir = os.path.join(folder, ".git")
    assert os.listdir(os.path.join(gitdir, "logs")) == []
    assert not os.path.exists(os.path.join(gitdir, "ORIG_HEAD"))
    assert git(folder, "stash", "list") == ""


# --- arguments and probes ----------------------------------------------------------------------------


def test_the_filemode_probe_is_git_inits(tmp_path):
    plain = tmp_path / "config"
    plain.write_text("")
    os.chmod(plain, 0o644)
    assert gm._filemode_trusted(str(plain)) is True
    assert stat.S_IMODE(os.stat(plain).st_mode) == 0o644
    os.chmod(plain, 0o755)
    assert gm._filemode_trusted(str(plain)) is False                     # git init trusts no exec start
    assert gm._filemode_trusted(str(tmp_path / "missing")) is False


def test_the_arguments_of_each_mode_are_checked(tmp_path):
    state = rs.make_state("ref: refs/heads/main", {})
    with pytest.raises(ValueError, match="absolute"):
        gm.write_gitdir(str(tmp_path / "ws"), state, "relative/objects")
    with pytest.raises(ValueError, match="staging"):
        gm.write_gitdir(str(tmp_path / "ws"), state, str(tmp_path), mode="export")
    with pytest.raises(ValueError, match="export"):
        gm.write_gitdir(str(tmp_path / "ws"), state, str(tmp_path), excluded={"a"})
    with pytest.raises(ValueError, match="export"):
        gm.write_gitdir(str(tmp_path / "ws"), state, str(tmp_path), secrets=["s"])
    assert not (tmp_path / "ws").exists()


def test_the_size_line_names_the_cap_as_it_is():
    assert gm._size(cas.MAX_BLOB_BYTES) == "50 MiB"
    assert gm._size(1000) == "1000 bytes"


def test_a_read_only_git_written_within_a_workspace_never_follows_a_link_out(tmp_path):
    """``within``: a work tree reached through a link from the workspace is
    refused at the finish, and nothing lands at the link's target."""
    repo = init(tmp_path / "src")
    commit(repo, {"a": "a\n"})
    ws = tmp_path / "ws"
    (ws / ".collrun").mkdir(parents=True)
    outside = tmp_path / "outside"
    hydrated = hydrate(repo, outside)
    (ws / "proj").symlink_to(outside, target_is_directory=True)
    with pytest.raises(gm.MaterializeFailed) as failed:
        gm.write_gitdir(str(ws / "proj"), harvest(repo), store_of(repo, tmp_path), hydrated=hydrated,
                        readonly=True, scratch=str(ws / ".collrun"), within=str(ws))
    assert failed.value.step == "finish"
    assert not os.path.lexists(outside / ".git")
    assert os.listdir(ws / ".collrun") == []
