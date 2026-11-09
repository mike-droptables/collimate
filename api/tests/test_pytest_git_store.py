"""The channel object store (``api/collimate_server/git_store.py``): creation, locks and
lifecycle, the harvest's store side (quarantine, band guard, walk, pack,
``index-pack --fsck-objects``, check), rescues, the export pack, the copy
between stores, ahead/behind, GC and startup hygiene, over real
repositories made with the host git under ``tmp_path``."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import threading
import time
import uuid
import zipfile

import pytest

import collimate_server.channel_paths as channel_paths
import collimate_server.git_host as git_host
import collimate_server.git_store as git_store
import collimate_server.repository_rows as repository_rows
import collimate_server.repository_state as rs
from collimate_server.database import changing_channels, creating_channels, get_db_connection
from conftest import channel_home
from git_repos import GIT_OK, commit, git, git_env, init

pytestmark = GIT_OK

RUN = "0123abcd-0000-4000-8000-000000000000"
DAY = 86400


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    """The data dir (the channels live in the test's channel location)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(data)
    git_host.reset_capability()
    git_store.forget_counts()
    yield
    changing_channels.clear()
    creating_channels.clear()


def new_channel() -> str:
    """A channel with its DB."""
    channel = str(uuid.uuid4())
    get_db_connection(channel).close()
    return channel


@pytest.fixture
def channel() -> str:
    return new_channel()


def store_git(channel, *args, input=None, env=None, check=True) -> str:
    """Plain git over the channel's store."""
    store = git_store.store_path(channel)
    return git(store, f"--git-dir={store}", *args, input=input, env=env, check=check)


def in_store(channel, sha) -> bool:
    store = git_store.store_path(channel)
    done = subprocess.run(["git", f"--git-dir={store}", "cat-file", "-e", sha], env=git_env(store),
                          capture_output=True)
    return done.returncode == 0


def read_state(ws, repo_path="", *, objects, base=None, **kw) -> rs.RepositoryState:
    got = rs.normalize(rs.read_repository(str(ws), repo_path), objects, base=base, **kw)
    assert got.refusal is None, got.refusal
    assert got.state is not None
    return got.state


def harvest(channel, ws, repo_path="", *, base=None, ledger=()):
    """One harvest's store side: quarantine, normalize, band guard,
    ingest; the guarded result, the ingest and the landed files."""
    with git_store.quarantine(channel, str(ws), repo_path, run_id=RUN, ledger=ledger) as q:
        state = read_state(ws, repo_path, objects=q.objects, base=base)
        guarded = git_store.band_guard(q, state, base)
        assert guarded.refusal is None, guarded.refusal
        assert guarded.state is not None
        with git_store.in_flight(channel):
            done = git_store.ingest(q, guarded.state, base)
        return guarded, done, q.landed()


def put_rows(channel, cell_id, states):
    conn = get_db_connection(channel)
    try:
        conn.execute("INSERT OR IGNORE INTO cells (id, source_type) VALUES (?, 'user')", (cell_id,))
        repository_rows.write_rows(conn, cell_id, states)
        conn.commit()
    finally:
        conn.close()


def pack_files(channel) -> list[str]:
    return sorted(os.listdir(os.path.join(git_store.store_path(channel), "objects", "pack")))


# --- creation and locks ---------------------------------------------------------------------------


def test_ensure_writes_the_exact_layout_and_is_idempotent(channel):
    old = os.umask(0o077)
    try:
        path = git_store.ensure(channel)
    finally:
        os.umask(old)
    assert path == os.path.realpath(channel_paths.git_dir(channel))
    folder = str(channel_paths.channel_dir(channel))
    found = {}
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            full = os.path.join(root, name)
            found[os.path.relpath(full, path)] = stat.S_IMODE(os.lstat(full).st_mode)
    assert found == {"HEAD": 0o644, "config": 0o644, "objects": 0o755, "objects/info": 0o755,
                     "objects/pack": 0o755, "refs": 0o755}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o755
    with open(os.path.join(path, "HEAD"), "rb") as fh:
        assert fh.read() == b"ref: refs/heads/none\n"
    assert store_git(channel, "rev-parse", "--is-bare-repository").strip() == "true"
    assert store_git(channel, "count-objects").startswith("0 objects")
    inode = os.stat(path).st_ino
    assert git_store.ensure(channel) == path
    assert os.stat(path).st_ino == inode
    assert not [n for n in os.listdir(folder) if n.startswith(".creating-")]


def test_ensure_refuses_a_changing_channel_an_absent_db_and_a_store_it_does_not_own(channel, monkeypatch):
    changing_channels.add(channel)
    with pytest.raises(git_store.StoreRefused, match="being deleted"):
        git_store.ensure(channel)
    changing_channels.discard(channel)
    bare = channel_home()
    os.unlink(channel_paths.db_path(bare))
    with pytest.raises(git_store.StoreRefused, match="no database"):
        git_store.ensure(bare)
    # A symlink at the store path is not testable here: store_path() now
    # realpaths the whole git_dir, so it resolves to the (owned) target.
    open(git_store.store_path(channel), "w").close()
    with pytest.raises(git_store.StoreRefused, match="not a directory the server user owns"):
        git_store.ensure(channel)
    os.unlink(git_store.store_path(channel))
    git_store.ensure(channel)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(".").st_uid + 1)
    with pytest.raises(git_store.StoreRefused):
        git_store.ensure(channel)
    with pytest.raises(ValueError):
        git_store.ensure("not-a-uuid")


def test_the_store_lock_is_a_cell_lock_in_its_own_namespace(channel):
    from collimate_server.cell_commits import cell_commit_lock
    assert git_store.store_lock(channel) is cell_commit_lock(f"channel-store:{channel}")
    assert git_store.store_lock(channel) is not cell_commit_lock(channel)


def test_in_flight_counts_nest_and_ingest_requires_one(channel, tmp_path):
    assert not git_store.ingest_in_flight(channel)
    with git_store.in_flight(channel):
        with git_store.in_flight(channel):
            assert git_store.ingest_in_flight(channel)
        assert git_store.ingest_in_flight(channel)
    assert not git_store.ingest_in_flight(channel)
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    with git_store.quarantine(channel, ws, "", run_id=RUN) as q:
        state = read_state(ws, objects=q.objects)
        with pytest.raises(RuntimeError, match="outside git_store.in_flight"):
            git_store.ingest(q, state, None)


def test_an_ingest_entering_flight_waits_out_a_collect_already_under_the_store_lock(channel, monkeypatch):
    collecting, finish, order = threading.Event(), threading.Event(), []

    def repacking(identity, store, cutoff):
        collecting.set()
        finish.wait(10)
        order.append("collected")
        return 0

    monkeypatch.setattr(git_store, "_collect_store", repacking)
    collect = threading.Thread(target=git_store.collect, args=(channel,))
    collect.start()
    assert collecting.wait(10)

    def ingest():
        with git_store.in_flight(channel):
            order.append("in flight")

    entering = threading.Thread(target=ingest)
    entering.start()
    time.sleep(0.2)
    assert order == []                       # the count waits for the collect's store lock
    finish.set()
    collect.join(10)
    entering.join(10)
    assert order == ["collected", "in flight"]


# --- lifecycle ------------------------------------------------------------------------------------


def make_rescue_area(channel):
    area = git_store.rescue_path(channel)
    os.makedirs(os.path.join(area, "cell", "slot", ".git", "objects"))
    return area


def test_purge_removes_store_and_rescues_even_when_a_directory_is_read_only(channel):
    store = git_store.ensure(channel)
    area = make_rescue_area(channel)
    locked = os.path.join(area, "cell", "slot", ".git", "objects")
    open(os.path.join(locked, "x"), "w").close()
    os.chmod(locked, 0o555)
    git_store.purge([store, area])
    assert not os.path.lexists(store) and not os.path.lexists(area)


def test_deleting_a_channel_takes_its_store_and_rescues(client):
    from collimate_server.state import channel_db_name, state
    channel = channel_db_name(state.channels[0])
    git_store.ensure(channel)
    make_rescue_area(channel)
    folder = channel_paths.channel_dir(channel)
    location = folder.parent
    response = client.delete(f"/api/channels/{channel}")
    assert response.status_code == 200, response.text
    assert not folder.exists()
    assert not [name for name in os.listdir(location) if channel in name]


def test_a_failed_create_removes_the_store_it_made(client, monkeypatch):
    import collimate_server.channel_sources as channel_sources

    def seed(ch, selection):
        get_db_connection(str(ch.id)).close()
        git_store.ensure(str(ch.id))
        raise RuntimeError("seed failed")

    monkeypatch.setattr(channel_sources, "seed_config", seed)
    location = channel_paths.default_location().path
    before = set(os.listdir(location))
    with pytest.raises(RuntimeError, match="seed failed"):
        client.post("/api/channels", params={"name": "Broken"})
    assert set(os.listdir(location)) <= before


# --- quarantine -----------------------------------------------------------------------------------


def test_the_quarantine_copies_only_loose_objects_and_packs(channel, tmp_path):
    ws = init(tmp_path / "ws")
    first = commit(ws, {"a": "1"})
    git(ws, "repack", "-a", "-d", "-q", "--write-bitmap-index")
    git(ws, "multi-pack-index", "write")
    git(ws, "commit-graph", "write", "--reachable")
    git(ws, "update-server-info")
    commit(ws, {"b": "2"})
    gitdir = os.path.join(ws, ".git")
    with open(os.path.join(gitdir, "shallow"), "w") as fh:
        fh.write(first + "\n")
    with open(os.path.join(gitdir, "objects", "info", "alternates"), "w") as fh:
        fh.write("/elsewhere/objects\n")
    loose = sorted(rel for rel in (os.path.relpath(os.path.join(r, f), os.path.join(gitdir, "objects"))
                                   for r, _, fs_ in os.walk(os.path.join(gitdir, "objects")) for f in fs_)
                   if len(rel) == 41)
    os.symlink(os.path.abspath("/etc/hostname"), os.path.join(gitdir, "objects", loose[0][:2], "f" * 38))
    with git_store.quarantine(channel, ws, "", run_id=RUN) as q:
        assert os.path.basename(q.path).startswith("git-harvest-0123abcd_")
        assert os.path.dirname(q.path) == git_host.tmp_root()
        packs = [rel for rel in q.copied if rel.startswith("pack/")]
        assert len(packs) == 1 and sorted(rel for rel in q.copied if not rel.startswith("pack/")) == loose
        listed = {os.path.relpath(os.path.join(r, f), q.path) for r, _, fs_ in os.walk(q.path) for f in fs_}
        stem = "objects/" + packs[0][:-len(".pack")]
        assert listed - {f"objects/{rel}" for rel in loose} <= {
            "HEAD", "config", "shallow", "objects/info/alternates", f"{stem}.pack", f"{stem}.idx", f"{stem}.rev"}
        with open(os.path.join(q.path, "objects", "info", "alternates")) as fh:
            assert fh.read() == os.path.join(git_store.store_path(channel), "objects") + "\n"
        with open(os.path.join(q.path, "shallow")) as fh:
            assert fh.read() == first + "\n"
        assert q.shallow_file == os.path.join(q.path, "shallow")
        path = q.path
    assert not os.path.exists(path)


def test_a_ledger_spares_the_files_an_earlier_quarantine_landed(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    guarded, _, landed = harvest(channel, ws)
    stray = git(ws, "hash-object", "-w", "--stdin", input=b"never committed").strip()
    assert landed and f"{stray[:2]}/{stray[2:]}" not in landed
    commit(ws, {"b": "2"})
    with git_store.quarantine(channel, ws, "", run_id=RUN, ledger=landed) as q:
        assert not q.copied & landed
        assert f"{stray[:2]}/{stray[2:]}" in q.copied
        state = read_state(ws, objects=q.objects, base=guarded.state)
        with git_store.in_flight(channel):
            git_store.ingest(q, state, guarded.state)


def test_a_file_whose_object_no_row_will_root_stays_out_of_the_ledger(channel, tmp_path):
    # The store holds the blob for another repository; this run's state
    # does not reach it, so a GC may sweep it before the run does.
    other = init(tmp_path / "other")
    commit(other, {"shared": "same bytes"})
    harvest(channel, other)
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    blob = git(ws, "hash-object", "-w", "--stdin", input=b"same bytes").strip()
    assert in_store(channel, blob)
    _, _, landed = harvest(channel, ws)
    assert landed and f"{blob[:2]}/{blob[2:]}" not in landed


def test_a_runs_repack_of_landed_loose_objects_lands_with_them(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    guarded, _, landed = harvest(channel, ws)
    assert len(landed) == 3 and not any(rel.startswith("pack/") for rel in landed)
    git(ws, "repack", "-d", "-q")
    with git_store.quarantine(channel, ws, "", run_id=RUN, ledger=landed) as q:
        packs = {rel for rel in q.copied if rel.startswith("pack/")}
        state = read_state(ws, objects=q.objects, base=guarded.state)
        assert q.landed() == frozenset()
        with git_store.in_flight(channel):
            assert git_store.ingest(q, state, guarded.state).objects == 0
        assert len(packs) == 1 and q.landed() == packs


def test_a_packs_objects_are_listed_inside_the_quarantine(channel, tmp_path, monkeypatch):
    """``show-index`` runs with the quarantine as its gitdir. Outside a
    repository git 2.47 has no hash algorithm: it crashes on an empty index
    and lists nothing for a real one (seen on 2.47.3; 2.31 and 2.53 do not
    care), and a pack whose objects go unlisted is copied again forever."""
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    git(ws, "repack", "-d", "-q")
    seen: list = []
    real = git_host.run

    def spy(args, **kw):
        if list(args[:1]) == ["show-index"]:
            seen.append(kw.get("git_dir"))
        return real(args, **kw)

    monkeypatch.setattr(git_host, "run", spy)
    _, _, landed = harvest(channel, ws)
    assert any(rel.startswith("pack/") for rel in landed)
    assert seen and all(gd and os.path.basename(gd).startswith("git-harvest-") for gd in seen)


@pytest.mark.parametrize("cap, value, words", [
    ("QUARANTINE_MAX_FILES", 1, "over 1 files"),
    ("QUARANTINE_MAX_BYTES", 10, "over 0 GiB"),
    ("QUARANTINE_SECONDS", -1, "not copied within"),
])
def test_the_quarantine_caps_refuse_and_leave_nothing(channel, tmp_path, monkeypatch, cap, value, words):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1", "b": "2"})
    monkeypatch.setattr(git_store, cap, value)
    with pytest.raises(git_store.StoreFailed, match=words):
        git_store.quarantine(channel, ws, "", run_id=RUN)
    assert not [n for n in os.listdir(git_host.tmp_root()) if n.startswith("git-harvest-")]


def test_a_symlinked_gitdir_is_not_quarantined(channel, tmp_path):
    real = init(tmp_path / "real")
    commit(real, {"a": "1"})
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(os.path.join(real, ".git"), ws / ".git")
    with pytest.raises(git_store.StoreFailed, match="gitdir unreadable"):
        git_store.quarantine(channel, str(ws), "", run_id=RUN)


# --- band guard -----------------------------------------------------------------------------------


def band_commit(ws, name="x"):
    os.makedirs(os.path.join(ws, ".collrun"), exist_ok=True)
    with open(os.path.join(ws, ".collrun", name), "w") as fh:
        fh.write("secret")
    git(ws, "add", "-f", f".collrun/{name}")


def guard(channel, ws, base):
    with git_store.quarantine(channel, ws, "", run_id=RUN) as q:
        state = read_state(ws, objects=q.objects, base=base)
        return state, git_store.band_guard(q, state, base)


def test_stash_all_holding_the_band_keeps_the_base_stash(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    base, _, _ = harvest(channel, ws)
    os.makedirs(os.path.join(ws, ".collrun"))
    with open(os.path.join(ws, ".collrun", "token"), "w") as fh:
        fh.write("t")
    with open(os.path.join(ws, "a"), "w") as fh:
        fh.write("2")
    git(ws, "stash", "--all", "-q")
    state, guarded = guard(channel, ws, base.state)
    assert "refs/stash" in rs.parse_refs(state.part("refs"))
    assert guarded.state is not None
    assert "refs/stash" not in rs.parse_refs(guarded.state.part("refs"))
    assert guarded.state.part("reflogs/refs/stash") is None
    assert "ref refs/stash: history holds .collrun/; not kept" in guarded.dropped
    assert rs.parse_refs(guarded.state.part("refs"))["refs/heads/main"] == rs.parse_refs(state.part("refs"))[
        "refs/heads/main"]


def test_a_band_path_staged_in_a_merge_drops_the_index_alone(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"f": "base\n"})
    git(ws, "checkout", "-q", "-b", "side")
    commit(ws, {"f": "side\n"})
    git(ws, "checkout", "-q", "main")
    commit(ws, {"f": "main\n"})
    base, _, _ = harvest(channel, ws)
    git(ws, "merge", "side", check=False)
    band_commit(ws)
    state, guarded = guard(channel, ws, base.state)
    assert state.part("index") is not None and state.part("progress/MERGE_HEAD") is not None
    assert guarded.state is not None
    assert guarded.state.part("index") is None
    assert guarded.state.part("progress/MERGE_HEAD") == state.part("progress/MERGE_HEAD")
    assert guarded.warnings == (f"index holds .collrun/; {rs.STAGED_RESOLUTIONS}",)


def test_an_auto_merge_tree_holding_the_band_drops_its_set(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    base, _, _ = harvest(channel, ws)
    band_commit(ws)
    tree = git(ws, "write-tree").strip()
    git(ws, "rm", "-q", "--cached", "-r", ".collrun")
    with open(os.path.join(ws, ".git", "AUTO_MERGE"), "w") as fh:
        fh.write(tree + "\n")
    state, guarded = guard(channel, ws, base.state)
    assert state.part("progress/AUTO_MERGE") is not None
    assert guarded.state is not None and guarded.state.part("progress/AUTO_MERGE") is None
    assert "progress AUTO_MERGE: history holds .collrun/; not kept" in guarded.dropped


def test_a_branch_a_tag_and_a_tree_ref_holding_the_band_keep_their_base(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    base, _, _ = harvest(channel, ws)
    base_main = rs.parse_refs(base.state.part("refs"))["refs/heads/main"]
    band_commit(ws)
    git(ws, "commit", "-q", "-m", "band")
    tree = git(ws, "rev-parse", "HEAD^{tree}").strip()
    git(ws, "tag", "-a", "-m", "t", "v1")
    git(ws, "update-ref", "refs/trees/t", tree)
    commit(ws, {"b": "2"})
    state, guarded = guard(channel, ws, base.state)
    assert guarded.state is not None
    refs = rs.parse_refs(guarded.state.part("refs"))
    assert refs["refs/heads/main"] == base_main
    assert "refs/tags/v1" not in refs and "refs/trees/t" not in refs
    assert guarded.state.part("reflogs/refs/heads/main") == base.state.part("reflogs/refs/heads/main")
    assert guarded.state.part("reflogs/HEAD") == base.state.part("reflogs/HEAD")
    assert "ref refs/heads/main: history holds .collrun/; base value kept" in guarded.dropped
    assert not (rs.roots(guarded.state) - rs.roots(base.state))


def test_a_clean_branch_whose_reflog_remembers_the_band_keeps_its_value(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    base, _, _ = harvest(channel, ws)
    band_commit(ws)
    git(ws, "commit", "-q", "-m", "band")
    band = git(ws, "rev-parse", "HEAD").strip()
    git(ws, "reset", "-q", "--hard", "HEAD~1")
    clean = commit(ws, {"b": "2"})
    state, guarded = guard(channel, ws, base.state)
    assert band in rs.roots(state)
    assert guarded.state is not None
    assert rs.parse_refs(guarded.state.part("refs"))["refs/heads/main"] == clean
    assert band not in rs.roots(guarded.state) and clean in rs.roots(guarded.state)
    assert set(guarded.dropped) == {"reflog refs/heads/main: history holds .collrun/; 2 entries dropped",
                                    "reflog HEAD: history holds .collrun/; 2 entries dropped",
                                    "progress ORIG_HEAD: history holds .collrun/; not kept"}
    entries, _, _ = rs.parse_reflog(guarded.state.part("reflogs/refs/heads/main"))
    assert entries[-1].new == clean


def test_band_history_below_the_haves_is_not_a_hit(channel, tmp_path):
    ws = init(tmp_path / "ws")
    band_commit(ws)
    git(ws, "commit", "-q", "-m", "band in the past")
    git(ws, "rm", "-q", "--cached", "-r", ".collrun")
    git(ws, "commit", "-q", "-m", "band gone")
    with git_store.quarantine(channel, ws, "", run_id=RUN) as q:
        first = read_state(ws, objects=q.objects)
    commit(ws, {"b": "2"})
    state, guarded = guard(channel, ws, first)
    assert guarded.state == state and not guarded.dropped


def test_a_repository_made_in_the_run_with_band_history_is_refused(channel, tmp_path):
    ws = init(tmp_path / "ws")
    band_commit(ws)
    git(ws, "commit", "-q", "-m", "band")
    _, guarded = guard(channel, ws, None)
    assert guarded.state is None and guarded.refusal and ".collrun/" in guarded.refusal


def test_haves_never_reach_argv(channel, tmp_path, monkeypatch):
    ws = init(tmp_path / "ws")
    for n in range(3):
        commit(ws, {"a": str(n)})
    base, _, _ = harvest(channel, ws)
    haves = rs.roots(base.state)
    commit(ws, {"b": "new"})
    seen = []
    spawn = git_host._spawn

    def record(argv, **kw):
        stdin = kw.get("input")
        seen.append((list(argv), stdin if isinstance(stdin, bytes) else b""))
        return spawn(argv, **kw)

    monkeypatch.setattr(git_host, "_spawn", record)
    harvest(channel, ws, base=base.state)
    walks = [(argv, stdin) for argv, stdin in seen if "rev-list" in argv or "pack-objects" in argv]
    assert {"rev-list", "pack-objects"} <= {word for argv, _ in walks for word in argv}
    for argv, stdin in walks:
        assert not any(have in word for have in haves for word in argv)
    assert any(f"^{have}".encode() in stdin for have in haves for _, stdin in walks)


# --- walk, pack, index-pack, check ----------------------------------------------------------------


def test_ingest_lands_every_object_and_writes_no_ref_or_shallow(channel, tmp_path):
    ws = init(tmp_path / "ws")
    tip = commit(ws, {"a": "1", "d/b": "2"})
    guarded, done, _ = harvest(channel, ws)
    assert done.objects > 0
    assert all(in_store(channel, sha) for sha in rs.roots(guarded.state))
    assert store_git(channel, "rev-list", "--objects", tip).count("\n") == 5
    store = git_store.store_path(channel)
    assert store_git(channel, "for-each-ref") == ""
    assert not os.path.exists(os.path.join(store, "shallow"))
    assert not [n for n in os.listdir(os.path.join(store, "objects")) if len(n) == 2]
    # A second harvest with nothing new indexes no pack.
    packs = pack_files(channel)
    _, again, _ = harvest(channel, ws, base=guarded.state)
    assert again.objects == 0 and pack_files(channel) == packs
    commit(ws, {"c": "3"})
    _, more, _ = harvest(channel, ws, base=guarded.state)
    assert more.objects == 3


def test_a_shallow_repository_keeps_its_boundary_and_the_store_is_never_shallow(channel, tmp_path):
    source = init(tmp_path / "source")
    for n in range(3):
        commit(source, {"a": str(n)})
    ws = str(tmp_path / "ws")
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{source}", ws)
    guarded, done, _ = harvest(channel, ws)
    shallow = guarded.state.part("shallow").decode().split()
    assert len(shallow) == 1 and in_store(channel, shallow[0])
    parent = git(source, "rev-parse", "HEAD~1").strip()
    assert not in_store(channel, parent)
    assert not os.path.exists(os.path.join(git_store.store_path(channel), "shallow"))
    grafts = tmp_path / "grafts"
    grafts.write_text(shallow[0] + "\n")
    assert store_git(channel, "rev-list", "--count", shallow[0], env={"GIT_SHALLOW_FILE": str(grafts)}).strip() == "1"


def tree_with(ws, raw: bytes) -> str:
    return git(ws, "hash-object", "-t", "tree", "-w", "--literally", "--stdin", input=raw).strip()


def commit_object(ws, tree: str, *, author_tz="+0000") -> str:
    body = (f"tree {tree}\nauthor A <a@b> 1700000000 {author_tz}\ncommitter A <a@b> 1700000000 +0000\n\nm\n")
    return git(ws, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", input=body.encode()).strip()


@pytest.mark.parametrize("kind, fsck_id", [
    ("hasDotgit", "hasDotgit"), ("+05", "badTimezone"), ("zeroPaddedFilemode", "zeroPaddedFilemode")])
def test_objects_fsck_rejects_fail_with_gits_words(channel, tmp_path, kind, fsck_id):
    ws = init(tmp_path / "ws")
    blob = git(ws, "hash-object", "-w", "--stdin", input=b"x").strip()
    if kind == "hasDotgit":
        tree = tree_with(ws, b"100644 .git\0" + bytes.fromhex(blob))
        tip = commit_object(ws, tree)
    elif kind == "+05":
        tree = tree_with(ws, b"100644 f\0" + bytes.fromhex(blob))
        tip = commit_object(ws, tree, author_tz="+05")
    else:
        tree = tree_with(ws, b"0100644 f\0" + bytes.fromhex(blob))
        tip = commit_object(ws, tree)
    git(ws, "update-ref", "refs/heads/main", tip)
    with git_store.quarantine(channel, ws, "", run_id=RUN) as q:
        state = read_state(ws, objects=q.objects)
        with git_store.in_flight(channel), pytest.raises(git_store.StoreFailed) as failed:
            git_store.ingest(q, state, None)
    assert failed.value.reason.startswith("objects refused: git index-pack exited 128: error: ")
    assert fsck_id in failed.value.reason and fsck_id in failed.value.git_stderr
    assert not in_store(channel, tip)


def test_a_100664_mode_is_kept(channel, tmp_path):
    # Early git's 100664 is badFilemode, which index-pack --fsck-objects
    # lets through on 2.31.1 (silently) and on 2.53 (with a warning): R6
    # adds no check of its own.
    ws = init(tmp_path / "ws")
    blob = git(ws, "hash-object", "-w", "--stdin", input=b"x").strip()
    tip = commit_object(ws, tree_with(ws, b"100664 f\0" + bytes.fromhex(blob)))
    git(ws, "update-ref", "refs/heads/main", tip)
    with git_store.quarantine(channel, ws, "", run_id=RUN) as q:
        state = read_state(ws, objects=q.objects)
        with git_store.in_flight(channel):
            git_store.ingest(q, state, None)
    assert in_store(channel, tip)


def test_a_missing_object_fails_the_walk(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    blob = git(ws, "rev-parse", "HEAD:a").strip()
    os.unlink(os.path.join(ws, ".git", "objects", blob[:2], blob[2:]))
    with git_store.quarantine(channel, ws, "", run_id=RUN) as q:
        state = read_state(ws, objects=q.objects)
        with git_store.in_flight(channel), pytest.raises(git_store.StoreFailed, match="history incomplete: git rev-list"):
            git_store.ingest(q, state, None)


def test_index_and_tree_roots_land_too(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"f": "base\n"})
    git(ws, "checkout", "-q", "-b", "side")
    commit(ws, {"f": "side\n"})
    git(ws, "checkout", "-q", "main")
    commit(ws, {"f": "main\n"})
    git(ws, "merge", "side", check=False)
    with open(os.path.join(ws, "f"), "w") as fh:
        fh.write("resolved\n")
    git(ws, "add", "f")
    guarded, _, _ = harvest(channel, ws)
    resolved = git(ws, "rev-parse", ":f").strip()
    assert guarded.state.part("index") is not None and resolved in rs.roots(guarded.state)
    assert in_store(channel, resolved)


# --- rescue ---------------------------------------------------------------------------------------


def run_gitdir(tmp_path, name="ws"):
    ws = init(tmp_path / name)
    commit(ws, {"a": "1"})
    return ws, os.path.join(ws, ".git")


def test_a_rescue_lands_beside_its_record_and_the_newest_wins(channel, tmp_path):
    ws, gitdir = run_gitdir(tmp_path)
    slot = git_store.rescue(channel, "cell-1", "sub/repo", gitdir, run_id=RUN, base_state_hash="h" * 64,
                            reason="objects refused: tok", git_stderr="fatal: https://u:pw@x/ tok " + "e" * 70000,
                            secrets=["tok"])
    digest = hashlib.sha256(b"sub/repo").hexdigest()[:16]
    assert slot == os.path.join(git_store.rescue_path(channel), "cell-1", digest)
    assert not os.path.exists(gitdir) and os.path.isdir(os.path.join(slot, ".git", "objects"))
    with open(os.path.join(slot, "rescue.json")) as fh:
        record = json.load(fh)
    assert record["run_id"] == RUN and record["repo_path"] == "sub/repo" and record["base_state_hash"] == "h" * 64
    assert record["reason"] == "objects refused: [redacted]"
    assert "pw" not in record["git_stderr"] and "tok" not in record["git_stderr"]
    assert len(record["git_stderr"].encode()) <= 64 * 1024
    assert git_store.rescues_of(channel, "cell-1") == {"sub/repo": record}
    _, second = run_gitdir(tmp_path, "ws2")
    git_store.rescue(channel, "cell-1", "sub/repo", second, run_id="feedbeef", base_state_hash=None,
                     reason="newer")
    assert git_store.rescues_of(channel, "cell-1")["sub/repo"]["reason"] == "newer"
    assert sorted(os.listdir(os.path.join(git_store.rescue_path(channel), "cell-1"))) == [digest]
    assert git_store.rescued_cells(channel) == {"cell-1"}


def test_a_failed_rescue_puts_the_gitdir_back(channel, tmp_path, monkeypatch):
    _, gitdir = run_gitdir(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(git_store.json, "dump", broken)
    with pytest.raises(OSError, match="disk full"):
        git_store.rescue(channel, "c", "", gitdir, run_id=RUN, base_state_hash=None, reason="r")
    assert os.path.isdir(os.path.join(gitdir, "objects"))
    assert os.listdir(os.path.join(git_store.rescue_path(channel), "c")) == []


def test_a_rescue_refuses_a_symlinked_gitdir_and_a_channel_being_deleted(channel, tmp_path):
    ws, gitdir = run_gitdir(tmp_path)
    link = os.path.join(tmp_path, "link")
    os.symlink(gitdir, link)
    with pytest.raises(git_store.StoreFailed, match="not a directory"):
        git_store.rescue(channel, "c", "", link, run_id=RUN, base_state_hash=None, reason="r")
    changing_channels.add(channel)
    with pytest.raises(git_store.StoreRefused):
        git_store.rescue(channel, "c", "", gitdir, run_id=RUN, base_state_hash=None, reason="r")
    assert os.path.isdir(gitdir)


def test_discard_removes_only_that_rescue(channel, tmp_path):
    _, one = run_gitdir(tmp_path, "one")
    _, two = run_gitdir(tmp_path, "two")
    git_store.rescue(channel, "c", "", one, run_id=RUN, base_state_hash=None, reason="r")
    git_store.rescue(channel, "c", "lib", two, run_id=RUN, base_state_hash=None, reason="r")
    assert git_store.discard(channel, "c", "") is True
    assert git_store.discard(channel, "c", "") is False
    assert set(git_store.rescues_of(channel, "c")) == {"lib"}
    assert git_store.discard(channel, "c", "lib") is True
    assert git_store.rescued_cells(channel) == set()


def test_an_exported_rescue_opens_as_a_repository_of_its_own(channel, tmp_path):
    # The rescued repository borrows its history from the store.
    ws = init(tmp_path / "ws")
    first = commit(ws, {"a": "1"})
    harvest(channel, ws)
    other = init(tmp_path / "other")
    git(other, "fetch", "-q", ws, "main")
    git(other, "update-ref", "refs/heads/main", first)
    shutil.rmtree(os.path.join(other, ".git", "objects"))
    os.makedirs(os.path.join(other, ".git", "objects", "info"))
    with open(os.path.join(other, ".git", "objects", "info", "alternates"), "w") as fh:
        fh.write(os.path.join(git_store.store_path(channel), "objects") + "\n")
    tip = commit(other, {"b": "2"})
    with open(os.path.join(other, ".git", "MERGE_HEAD"), "w") as fh:
        fh.write(first + "\n")
    git_store.rescue(channel, "c", "", os.path.join(other, ".git"), run_id=RUN, base_state_hash=None, reason="r")
    buffer = io.BytesIO()
    git_store.export_rescue(channel, "c", "", buffer)
    out = tmp_path / "out"
    with zipfile.ZipFile(buffer) as archive:
        names = archive.namelist()
        archive.extractall(out)
    assert ".git/objects/info/alternates" not in names
    assert any(name.endswith(".pack") for name in names) and any(name.endswith(".idx") for name in names)
    assert git(out, "--git-dir=.git", "rev-list", "--count", tip).strip() == "2"
    git(out, "--git-dir=.git", "fsck", "--full", "--no-dangling")
    assert not [n for n in os.listdir(git_host.tmp_root()) if n.startswith("export-")]
    with pytest.raises(LookupError):
        git_store.export_rescue(channel, "c", "missing", io.BytesIO())


def _exported(channel, cell, repo_path, out, **kw):
    buffer = io.BytesIO()
    git_store.export_rescue(channel, cell, repo_path, buffer, **kw)
    with zipfile.ZipFile(buffer) as archive:
        names = archive.namelist()
        archive.extractall(out)
    return names


def test_a_rescue_export_carries_no_secret_and_no_command_running_config(channel, tmp_path):
    token = "ghp_" + "s" * 36
    ws, gitdir = run_gitdir(tmp_path)
    git(ws, "config", "remote.origin.url", f"https://x-access-token:{token}@github.com/o/r")
    git(ws, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    git(ws, "config", "core.fsmonitor", "sh -c 'echo pwned'")
    git(ws, "config", "core.pager", "sh -c 'echo pwned'")
    git(ws, "config", "http.extraHeader", f"Authorization: Bearer {token}")
    git(ws, "config", "user.name", "Kept Name")
    git(ws, "update-ref", "-m", f"pushed to https://u:{token}@host/r", "refs/heads/pushed", "HEAD")
    with open(os.path.join(gitdir, "hooks", "post-checkout"), "w") as fh:
        fh.write("#!/bin/sh\necho pwned\n")
    with open(os.path.join(gitdir, "FETCH_HEAD"), "w") as fh:
        fh.write(f"0000 https://u:{token}@host/r\n")
    with open(os.path.join(gitdir, "MERGE_MSG"), "w") as fh:
        fh.write(f"merging with {token}\n")
    git_store.rescue(channel, "c", "", gitdir, run_id=RUN, base_state_hash=None, reason="r")
    out = tmp_path / "out"
    names = _exported(channel, "c", "", out, secrets=[token])
    assert not [name for name in names if name.startswith((".git/hooks/", ".git/FETCH_HEAD"))]
    for root, _dirs, files in os.walk(out):
        for name in files:
            with open(os.path.join(root, name), "rb") as fh:
                assert token.encode() not in fh.read(), name
    config = git(out, "--git-dir=.git", "config", "--list")
    assert "fsmonitor" not in config and "pager" not in config and "extraheader" not in config
    assert "remote.origin.url=https://github.com/o/r" in config and "user.name=Kept Name" in config
    assert "[redacted]" in (out / ".git" / "MERGE_MSG").read_text()
    assert git(out, "--git-dir=.git", "rev-parse", "pushed").strip() == git(out, "--git-dir=.git", "rev-parse",
                                                                             "HEAD").strip()


def test_a_rescue_export_never_follows_the_rescues_own_alternates(channel, tmp_path):
    other = init(tmp_path / "other")
    foreign = commit(other, {"secret.txt": "another store's\n"}, "foreign")
    ws, gitdir = run_gitdir(tmp_path)
    with open(os.path.join(gitdir, "objects", "info", "alternates"), "w") as fh:
        fh.write(os.path.join(other, ".git", "objects") + "\n")
    git(ws, "update-ref", "refs/heads/borrowed", foreign)
    own = git(ws, "rev-parse", "HEAD")
    git_store.rescue(channel, "c", "", gitdir, run_id=RUN, base_state_hash=None, reason="borrows")
    git_store.ensure(channel)

    def foreign_exported(out) -> bool:
        return subprocess.run(["git", "--git-dir", str(out / ".git"), "cat-file", "-e", foreign],
                              capture_output=True, env=git_env(str(out))).returncode == 0

    # The ref names an object only the other store holds: the export
    # never reads that store (current git fails on the ref, 2.31 leaves
    # the object out).
    try:
        _exported(channel, "c", "", tmp_path / "first")
    except git_store.StoreFailed as exc:
        assert "bad object refs/heads/borrowed" in exc.reason
    else:
        assert not foreign_exported(tmp_path / "first")
    # Without that ref, the export holds nothing of the other store.
    slot = os.path.dirname(git_store.rescue_gitdir(channel, "c", ""))
    os.unlink(os.path.join(slot, ".git", "refs", "heads", "borrowed"))
    with open(os.path.join(slot, ".git", "refs", "tags", "kept"), "w") as fh:
        fh.write(own)
    out = tmp_path / "out"
    names = _exported(channel, "c", "", out)
    assert ".git/objects/info/alternates" not in names
    assert not foreign_exported(out)


def test_a_rescue_export_with_packed_refs_alone_opens_as_a_repository(channel, tmp_path):
    ws, gitdir = run_gitdir(tmp_path)
    git(ws, "pack-refs", "--all")
    assert not os.listdir(os.path.join(gitdir, "refs", "heads"))
    git_store.rescue(channel, "c", "", gitdir, run_id=RUN, base_state_hash=None, reason="r")
    out = tmp_path / "out"
    _exported(channel, "c", "", out)
    assert git(out, "--git-dir=.git", "log", "--format=%H").strip()


def test_the_rescue_export_packs_under_the_store_lock(channel, tmp_path, monkeypatch):
    _, gitdir = run_gitdir(tmp_path)
    git_store.rescue(channel, "c", "", gitdir, run_id=RUN, base_state_hash=None, reason="r")
    held = []
    run = git_host.run

    def record(args, **kw):
        if "pack-objects" in args:
            held.append(git_store.store_lock(channel).held_by_me())
        return run(args, **kw)

    monkeypatch.setattr(git_host, "run", record)
    git_store.export_rescue(channel, "c", "", io.BytesIO())
    assert held == [True]


# --- export pack and copy -------------------------------------------------------------------------


def test_pack_for_export_packs_every_root(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    tip = commit(ws, {"b": "2"})
    guarded, _, _ = harvest(channel, ws)
    pack_dir = tmp_path / "export" / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    pack = git_store.pack_for_export(channel, guarded.state, str(pack_dir))
    assert pack is not None and os.path.isfile(pack) and os.path.isfile(pack[:-len(".pack")] + ".idx")
    export = tmp_path / "export"
    for name in ("refs", "objects/info"):
        (export / name).mkdir(parents=True, exist_ok=True)
    (export / "HEAD").write_text("ref: refs/heads/main\n")
    (export / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
    git(export, f"--git-dir={export}", "update-ref", "refs/heads/main", tip)
    git(export, f"--git-dir={export}", "fsck", "--full", "--no-dangling")
    unborn = rs.make_state("ref: refs/heads/main", {})
    assert git_store.pack_for_export(channel, unborn, str(pack_dir)) is None


def test_a_copy_keeps_full_history_shared_with_a_shallow_repository(channel, tmp_path):
    full = init(tmp_path / "full")
    for n in range(3):
        tip = commit(full, {"a": str(n)})
    shallow = str(tmp_path / "shallow")
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{full}", shallow)
    full_state, _, _ = harvest(channel, full)
    shallow_state, _, _ = harvest(channel, shallow)
    assert shallow_state.state.part("shallow").decode().split() == [tip]
    new = new_channel()
    git_store.copy_between_stores(channel, new, [full_state.state, shallow_state.state])
    assert store_git(new, "rev-list", "--count", tip).strip() == "3"
    assert store_git(new, "for-each-ref") == ""
    assert not os.path.exists(os.path.join(git_store.store_path(new), "shallow"))
    # A shallow repository alone copies under its grafts.
    only = new_channel()
    source = new_channel()
    harvest(source, shallow)
    git_store.copy_between_stores(source, only, [shallow_state.state])
    assert in_store(only, tip)
    assert not in_store(only, git(full, "rev-parse", "HEAD~1").strip())


def test_a_copy_of_a_shallow_clone_whose_cut_parent_only_an_uncopied_shallower_clone_holds(channel, tmp_path):
    upstream = init(tmp_path / "upstream")
    for n in range(2):
        commit(upstream, {"a": str(n)})
    older = str(tmp_path / "older")
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{upstream}", older)
    harvest(channel, older)                             # the store holds c1 alone, cut there
    tip = commit(upstream, {"a": "2"})
    newer = str(tmp_path / "newer")
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{upstream}", newer)
    newer_state, _, _ = harvest(channel, newer)         # c2, cut there; its parent c1 is in the store
    assert newer_state.state.part("shallow").decode().split() == [tip]
    copied = new_channel()
    git_store.copy_between_stores(channel, copied, [newer_state.state])
    assert in_store(copied, tip)


def test_a_failed_copy_raises(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    guarded, _, _ = harvest(channel, ws)
    with pytest.raises(git_store.StoreFailed, match="no store"):
        git_store.copy_between_stores(new_channel(), new_channel(), [guarded.state])
    bogus = rs.make_state("1" * 40, {})
    with pytest.raises(git_store.StoreFailed, match="repository history not copied"):
        git_store.copy_between_stores(channel, new_channel(), [bogus])


# --- ahead and behind -----------------------------------------------------------------------------


def tracked(tmp_path):
    """A clone one commit ahead of and two behind its upstream."""
    remote = init(tmp_path / "remote")
    commit(remote, {"a": "1"})
    ws = str(tmp_path / "ws")
    git(tmp_path, "clone", "-q", f"file://{remote}", ws)
    commit(remote, {"r": "theirs"})
    commit(remote, {"s": "theirs too"})
    git(ws, "fetch", "-q")
    commit(ws, {"l": "mine"})
    return ws


def test_ahead_behind_counts_against_the_upstream_and_is_cached(channel, tmp_path, monkeypatch):
    ws = tracked(tmp_path)
    guarded, _, _ = harvest(channel, ws)
    state = guarded.state
    loads = []

    def load():
        loads.append(1)
        return state

    assert git_store.ahead_behind(channel, "", state.manifest_hash, load) == {"ahead": 1, "behind": 2}
    assert git_store.cached_ahead_behind(channel, "", state.manifest_hash) == {"ahead": 1, "behind": 2}
    monkeypatch.setattr(git_host, "run", lambda *a, **k: pytest.fail("cached"))
    assert git_store.ahead_behind(channel, "", state.manifest_hash, load) == {"ahead": 1, "behind": 2}
    assert loads == [1]
    assert git_store.cached_ahead_behind(channel, "other", state.manifest_hash) == {"ahead": None, "behind": None}


def test_ahead_behind_without_an_upstream_is_null(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    guarded, _, _ = harvest(channel, ws)
    assert git_store.ahead_behind(channel, "", guarded.state.manifest_hash, lambda: guarded.state) == {
        "ahead": None, "behind": None}


def test_ahead_behind_never_raises(channel, tmp_path, monkeypatch):
    git_store.ensure(channel)
    config = rs.canonical_json([["branch.main.merge", "refs/heads/main"], ["branch.main.remote", "origin"],
                                ["remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"]])
    refs = rs.format_refs({"refs/heads/main": "1" * 40, "refs/remotes/origin/main": "2" * 40})
    state = rs.make_state("ref: refs/heads/main", {"refs": refs, "config": config})
    got = git_store.ahead_behind(channel, "", state.manifest_hash, lambda: state)
    assert got["ahead"] is None and got["behind"] is None
    assert got["error"]["rescued"] is False and "git rev-list exited 128" in got["error"]["reason"]
    assert git_store.cached_ahead_behind(channel, "", state.manifest_hash) == got

    def broken():
        raise LookupError("repository part x is not stored")

    assert git_store.ahead_behind(channel, "", "f" * 64, broken) == {"ahead": None, "behind": None}

    def slow(*args, **kwargs):
        raise git_host.GitFailed(["rev-list"], None, "", timed_out=True)

    monkeypatch.setattr(git_host, "run", slow)
    other = rs.make_state("ref: refs/heads/main", {"refs": refs + b"", "config": config, "exclude": b"x\n"})
    assert git_store.ahead_behind(channel, "", other.manifest_hash, lambda: other) == {"ahead": None, "behind": None}
    assert git_store.cached_ahead_behind(channel, "", other.manifest_hash) == {"ahead": None, "behind": None}
    assert (channel, other.manifest_hash, "") not in git_store._counts


def test_ahead_behind_is_null_for_a_db_error_or_a_malformed_config(channel):
    import sqlite3
    git_store.ensure(channel)

    def locked():
        raise sqlite3.OperationalError("database is locked")

    assert git_store.ahead_behind(channel, "", "e" * 64, locked) == {"ahead": None, "behind": None}
    bad = rs.make_state("ref: refs/heads/main", {"config": rs.canonical_json([[1, 2]])})
    assert git_store.ahead_behind(channel, "", bad.manifest_hash, lambda: bad) == {"ahead": None, "behind": None}


def test_the_upstream_is_the_first_refspec_mapping_the_first_merge():
    # As `git rev-parse @{u}` on 2.31.1 and 2.53: the first branch.<b>.merge,
    # the first fetch refspec mapping it, and a negative refspec vetoes nothing.
    config = rs.canonical_json([
        ["branch.main.merge", "refs/heads/a"], ["branch.main.merge", "refs/heads/b"], ["branch.main.remote", "o"],
        ["remote.o.fetch", "^refs/heads/a"], ["remote.o.fetch", "+refs/heads/*:refs/remotes/o/*"],
        ["remote.o.fetch", "refs/heads/a:refs/remotes/p/a"], ["remote.p.fetch", "refs/heads/a:refs/remotes/q/a"]])
    refs = rs.format_refs({"refs/heads/main": "1" * 40, "refs/remotes/o/a": "2" * 40, "refs/remotes/p/a": "3" * 40,
                           "refs/remotes/o/b": "4" * 40, "refs/remotes/q/a": "5" * 40})
    state = rs.make_state("ref: refs/heads/main", {"refs": refs, "config": config})
    assert git_store._upstream_shas(state) == ("1" * 40, "2" * 40)


def test_the_snapshot_carries_counts_and_rescues(channel, tmp_path):
    ws = tracked(tmp_path)
    guarded, _, _ = harvest(channel, ws)
    put_rows(channel, "cell-1", {"": guarded.state})
    _, gitdir = run_gitdir(tmp_path, "lost")
    git_store.rescue(channel, "cell-1", "lost", gitdir, run_id=RUN, base_state_hash=None, reason="objects refused")
    conn = get_db_connection(channel)
    try:
        entries, overflow = repository_rows.snapshot_entries(
            conn, "cell-1", counts=lambda *args: git_store.ahead_behind(channel, *args),
            rescued=git_store.rescues_of(channel, "cell-1"))
    finally:
        conn.close()
    assert overflow == 0
    assert entries == [
        {"path": "", "branch": "main", "upstream": "origin/main", "ahead": 1, "behind": 2, "detached": False},
        {"path": "lost", "branch": None, "upstream": None, "ahead": None, "behind": None, "detached": False,
         "error": {"reason": "objects refused", "rescued": True}},
    ]


# --- GC -------------------------------------------------------------------------------------------


def unreachable_in_store(channel, tmp_path):
    """Objects ingested for a state no row names: their files' shas."""
    ws = init(tmp_path / "gone")
    commit(ws, {"gone": "unreachable"})
    guarded, _, _ = harvest(channel, ws)
    return rs.roots(guarded.state)


def loose_files(channel):
    objects = os.path.join(git_store.store_path(channel), "objects")
    return sorted(os.path.join(objects, d, f) for d in os.listdir(objects) if len(d) == 2
                  for f in os.listdir(os.path.join(objects, d)))


def age(paths, seconds):
    then = time.time() - seconds
    for path in paths:
        os.utime(path, (then, then))


def test_collect_keeps_rows_loosens_the_rest_and_sweeps_it_after_a_day(channel, tmp_path):
    ws = init(tmp_path / "ws")
    tip = commit(ws, {"a": "1"})
    guarded, _, _ = harvest(channel, ws)
    put_rows(channel, "cell-1", {"": guarded.state})
    gone = unreachable_in_store(channel, tmp_path)
    done = git_store.collect(channel)
    assert done.status == "collected" and done.swept == 0
    assert all(in_store(channel, sha) for sha in gone), "loosened objects under 24 h stay"
    assert loose_files(channel)
    age(loose_files(channel), 3 * DAY)
    done = git_store.collect(channel)
    assert done.status == "collected" and done.swept == 3
    assert not any(in_store(channel, sha) for sha in gone)
    assert in_store(channel, tip) and store_git(channel, "rev-list", "--count", tip).strip() == "1"
    store = git_store.store_path(channel)
    assert store_git(channel, "for-each-ref") == "" and not os.path.exists(os.path.join(store, "refs", "gc-roots"))
    assert len([n for n in pack_files(channel) if n.endswith(".pack")]) == 1


def test_collect_keeps_shallow_history_under_its_grafts(channel, tmp_path):
    source = init(tmp_path / "source")
    for n in range(3):
        tip = commit(source, {"a": str(n)})
    ws = str(tmp_path / "ws")
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{source}", ws)
    guarded, _, _ = harvest(channel, ws)
    put_rows(channel, "cell-1", {"": guarded.state})
    assert git_store.collect(channel).status == "collected"
    assert in_store(channel, tip)


def test_collect_fails_closed_on_a_failed_repack(channel, tmp_path, monkeypatch):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    guarded, _, _ = harvest(channel, ws)
    put_rows(channel, "cell-1", {"": guarded.state})
    gone = unreachable_in_store(channel, tmp_path)
    git_store.collect(channel)
    age(loose_files(channel), 3 * DAY)
    run = git_host.run

    def failing(args, **kw):
        if "repack" in args:
            raise git_host.GitFailed(["repack"], 1, "fatal: out of luck")
        return run(args, **kw)

    monkeypatch.setattr(git_host, "run", failing)
    done = git_store.collect(channel)
    assert done.status == "failed" and "repack failed: git repack exited 1: fatal: out of luck" in done.reason
    assert all(in_store(channel, sha) for sha in gone), "no sweep after a failed repack"
    assert store_git(channel, "for-each-ref") == ""


def test_collect_prunes_nothing_on_a_corrupt_body(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    guarded, _, _ = harvest(channel, ws)
    put_rows(channel, "cell-1", {"": guarded.state})
    gone = unreachable_in_store(channel, tmp_path)
    git_store.collect(channel)
    age(loose_files(channel), 3 * DAY)
    body = b"not a manifest"
    conn = get_db_connection(channel)
    conn.execute("INSERT INTO cells (id, source_type) VALUES ('cell-2', 'user')")
    conn.execute("INSERT INTO repository_parts(hash, body) VALUES (?, ?)", (rs.part_hash(body), body))
    conn.execute("INSERT INTO cell_repositories(version_id, repo_path, state_hash) VALUES ('cell-2', '', ?)",
                 (rs.part_hash(body),))
    conn.commit()
    conn.close()
    done = git_store.collect(channel)
    assert done.status == "failed" and "invalid repository state" in done.reason
    assert all(in_store(channel, sha) for sha in gone)


def test_collect_prunes_nothing_when_a_part_no_longer_matches_its_hash(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    blob = git(ws, "hash-object", "-w", "--stdin", input=b"only a tag names me").strip()
    git(ws, "tag", "blobtag", blob)
    guarded, _, _ = harvest(channel, ws)
    put_rows(channel, "cell-1", {"": guarded.state})
    refs = guarded.state.part("refs")
    edited = b"".join(line + b"\n" for line in refs.split(b"\n") if line and b"blobtag" not in line)
    conn = get_db_connection(channel)
    conn.execute("UPDATE repository_parts SET body=? WHERE hash=?", (edited, rs.part_hash(refs)))
    conn.commit()
    conn.close()
    pack = os.path.join(git_store.store_path(channel), "objects", "pack")
    age([os.path.join(pack, name) for name in os.listdir(pack)], 3 * DAY)
    done = git_store.collect(channel)
    assert done.status == "failed" and "does not match its body" in done.reason
    assert in_store(channel, blob)


def test_collect_prunes_nothing_on_a_malformed_reflog_part(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    guarded, _, _ = harvest(channel, ws)
    gone = unreachable_in_store(channel, tmp_path)
    git_store.collect(channel)
    age(loose_files(channel), 3 * DAY)
    parts = guarded.state.named_parts()
    parts["reflogs/refs/heads/main"] += b"not a reflog line\n"
    put_rows(channel, "cell-1", {"": rs.make_state(guarded.state.manifest()["head"], parts)})
    done = git_store.collect(channel)
    assert done.status == "failed" and "reflogs/refs/heads/main has malformed lines" in done.reason
    assert all(in_store(channel, sha) for sha in gone)


def test_collect_sweeps_pack_temporaries_after_a_day(channel):
    # Swept before the repack: git reads a .tmp-* pair as a pack, and a torn
    # one would fail every repack, so nothing would ever be swept.
    pack = os.path.join(git_store.ensure(channel), "objects", "pack")
    old = ["tmp_pack_x", "tmp_idx_y", ".tmp-1-pack-z.pack", ".tmp-1-pack-z.idx"]
    for name in [*old, "tmp_pack_fresh", ".tmp-2-pack-fresh.pack"]:
        with open(os.path.join(pack, name), "wb") as fh:
            fh.write(b"partial")
    age([os.path.join(pack, name) for name in old], 3 * DAY)
    done = git_store.collect(channel)
    assert done.status == "collected" and done.swept == 4
    assert sorted(os.listdir(pack)) == [".tmp-2-pack-fresh.pack", "tmp_pack_fresh"]


def test_collect_without_usable_git_only_sweeps_rescues(channel, tmp_path, monkeypatch):
    git_store.ensure(channel)
    _, old = run_gitdir(tmp_path, "old")
    slot = git_store.rescue(channel, "c", "old", old, run_id=RUN, base_state_hash=None, reason="r")
    os.unlink(os.path.join(slot, "rescue.json"))
    age([slot], 31 * DAY)
    monkeypatch.setattr(git_host, "capability", lambda: git_host.Capability("too_old", "git", (2, 30, 0)))
    done = git_store.collect(channel)
    assert (done.status, done.reason, done.rescues) == ("skipped", "git on this machine is 2.30.0, older than 2.31", 1)


def test_collect_fails_closed_on_rows_without_a_store_or_a_missing_root(channel, tmp_path):
    ws = init(tmp_path / "ws")
    commit(ws, {"a": "1"})
    guarded, _, _ = harvest(channel, ws)
    put_rows(channel, "cell-1", {"": guarded.state})
    ghost = rs.make_state("2" * 40, {})
    put_rows(channel, "cell-2", {"": ghost})
    done = git_store.collect(channel)
    assert done.status == "failed" and "update-ref refused the roots" in done.reason
    assert store_git(channel, "for-each-ref") == ""
    other = new_channel()
    put_rows(other, "cell-1", {"": guarded.state})
    assert git_store.collect(other).reason == "rows without a store"


def test_collect_skips_a_channel_with_an_ingest_in_flight(channel, tmp_path):
    git_store.ensure(channel)
    with git_store.in_flight(channel):
        assert git_store.collect(channel).status == "skipped"
    assert git_store.collect(channel).status == "collected"


def test_collect_skips_an_ingest_that_began_while_it_waited_for_the_lock(channel, monkeypatch):
    # A harvest raises its count while a collect, past its first check,
    # waits on the store lock: collect checks again once it holds it.
    git_store.ensure(channel)
    lock = git_store.store_lock(channel)
    checked = threading.Event()
    done = []
    checks = git_store.ingest_in_flight

    def check(identity):
        busy = checks(identity)
        checked.set()
        return busy

    monkeypatch.setattr(git_store, "ingest_in_flight", check)
    lock.acquire()
    try:
        worker = threading.Thread(target=lambda: done.append(git_store.collect(channel)))
        worker.start()
        assert checked.wait(10)
        with git_store.in_flight(channel):
            lock.release()
            worker.join(10)
    finally:
        if lock.held_by_me():
            lock.release()
    assert done[0].status == "skipped" and done[0].reason == "an ingest is in flight"


def test_collect_sweeps_rescues_past_30_days(channel, tmp_path):
    git_store.ensure(channel)
    _, old = run_gitdir(tmp_path, "old")
    _, fresh = run_gitdir(tmp_path, "fresh")
    slot = git_store.rescue(channel, "c", "old", old, run_id=RUN, base_state_hash=None, reason="r")
    git_store.rescue(channel, "c", "fresh", fresh, run_id=RUN, base_state_hash=None, reason="r")
    record_path = os.path.join(slot, "rescue.json")
    with open(record_path) as fh:
        record = json.load(fh)
    record["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 31 * DAY))
    with open(record_path, "w") as fh:
        json.dump(record, fh)
    done = git_store.collect(channel)
    assert done.rescues == 1
    assert set(git_store.rescues_of(channel, "c")) == {"fresh"}


def test_collect_all_waits_for_channel_creation():
    creating_channels.add("x")
    assert git_store.collect_all() == []


# --- startup hygiene ------------------------------------------------------------------------------


def test_startup_hygiene_removes_quarantines_staging_store_refs_and_locks(channel, tmp_path):
    tmp = git_host.tmp_root()
    os.makedirs(os.path.join(tmp, "git-harvest-0123abcd_xyz", "objects"))
    os.makedirs(os.path.join(tmp, "export-abc"))
    for scratch in ("git-copy-abc", "git-home-abc", "git-read-abc", "git-index-abc"):
        os.makedirs(os.path.join(tmp, scratch))
    with open(os.path.join(tmp, "git-copy-abc", "copy.pack"), "wb") as fh:
        fh.write(b"PACK")
    open(os.path.join(tmp, "git-shallow-abc"), "w").close()
    os.makedirs(os.path.join(tmp, "git-harvest-nothex12_keep"))
    os.makedirs(os.path.join(tmp, "ws_0123abcd_keep"))
    target = tmp_path / "target"
    target.mkdir()
    os.symlink(target, os.path.join(tmp, "git-harvest-89abcdef_link"))
    ws = init(tmp_path / "ws")
    tip = commit(ws, {"a": "1"})
    harvest(channel, ws)
    store = git_store.store_path(channel)
    store_git(channel, "update-ref", "refs/gc-roots/x/0", tip)
    store_git(channel, "update-ref", "refs/heads/stray", tip)
    for lock in ("HEAD.lock", "packed-refs.lock", "config.lock", "refs/heads/main.lock"):
        open(os.path.join(store, lock), "w").close()
    outcomes = git_store.startup_hygiene([channel])
    assert sorted(os.listdir(tmp)) == ["git-harvest-nothex12_keep", "ws_0123abcd_keep"]
    assert target.is_dir()
    assert store_git(channel, "for-each-ref") == ""
    assert not [n for n in ("HEAD.lock", "packed-refs.lock", "config.lock") if os.path.exists(os.path.join(store, n))]
    assert not os.path.exists(os.path.join(store, "refs", "heads", "main.lock"))
    assert in_store(channel, tip)
    assert any("deleted 2 refs" in line for line in outcomes)


def test_startup_hygiene_without_git_keeps_store_refs(channel, tmp_path, monkeypatch):
    ws = init(tmp_path / "ws")
    tip = commit(ws, {"a": "1"})
    harvest(channel, ws)
    store_git(channel, "update-ref", "refs/heads/stray", tip)
    open(os.path.join(git_store.store_path(channel), "HEAD.lock"), "w").close()
    monkeypatch.setattr(git_host, "capability", lambda: git_host.Capability("missing"))
    git_store.startup_hygiene([channel])
    assert store_git(channel, "for-each-ref").strip().endswith("refs/heads/stray")
    assert not os.path.exists(os.path.join(git_store.store_path(channel), "HEAD.lock"))


# --- wiring ---------------------------------------------------------------------------------------


async def test_the_daily_gc_collects_the_git_stores_after_the_cas(monkeypatch):
    import asyncio
    import collimate_server.cas_gc as cas_gc
    import collimate_server.git_store as current
    calls = []
    monkeypatch.setattr(cas_gc, "run_gc", lambda: calls.append("cas") or {})
    monkeypatch.setattr(current, "collect_all", lambda: calls.append("git") or [])
    task = asyncio.create_task(cas_gc.run_periodic(interval=0.01))
    try:
        for _ in range(200):
            if "git" in calls:
                break
            await asyncio.sleep(0.005)
        assert calls[:2] == ["cas", "git"]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await cas_gc.wait_for_collection()


def test_collect_now_collects_each_git_store_after_the_body_stores(client):
    from collimate_server.state import channel_db_name, state
    channel = channel_db_name(state.channels[0])
    import collimate_server.git_store as current
    current.ensure(channel)
    response = client.post("/api/storage/collect")
    assert response.status_code == 200, response.text
    assert response.json()["git"] == [
        {"channel": channel, "status": "collected", "reason": "", "swept": 0, "rescues": 0}]


def test_startup_removes_stale_quarantines_and_store_refs(api_app, tmp_path):
    from fastapi.testclient import TestClient
    os.makedirs(os.path.join(".collimate", "tmp", "git-harvest-0123abcd_stale", "objects"))
    with TestClient(api_app) as c:
        assert c.get("/api/channels").status_code == 200
        assert not os.path.exists(os.path.join(".collimate", "tmp", "git-harvest-0123abcd_stale"))
