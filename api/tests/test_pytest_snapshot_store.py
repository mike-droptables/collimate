"""build/snapshot_store.py: the module snapshot's bodies on the orphan branch.

Each test works in repositories of its own under tmp_path, with git's global
and system configuration out of the way."""
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("snapshot_store", ROOT / "build/snapshot_store.py")
store = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = store
_spec.loader.exec_module(store)

BODIES = [b"export const a = 1;\n", b"export const b = 2;\n", b"\x00\x01binary\r\n"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Snapshot Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "snapshot@example.invalid")


def write_snapshot(root: Path, bodies: list[bytes], *, named: list[bytes] | None = None) -> None:
    """index.json naming ``named`` (default: ``bodies``), and ``bodies`` under modules/."""
    snapshot = root / "module-snapshot"
    (snapshot / "modules").mkdir(parents=True, exist_ok=True)
    entries = {f"https://esm.sh/m{i}": {"hash": sha(body)} for i, body in enumerate(named or bodies)}
    (snapshot / "index.json").write_text(json.dumps({"entries": entries}))
    for body in bodies:
        (snapshot / "modules" / sha(body)).write_bytes(body)


@pytest.fixture
def repo(tmp_path):
    """A main checkout that ignores modules/ and commits the index, its bodies on disk."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("/module-snapshot/modules/\n")
    write_snapshot(root, BODIES)
    git(root, "add", ".gitignore", "module-snapshot/index.json")
    git(root, "commit", "-q", "-m", "main")
    return root


def branch_tree(root: Path) -> dict[str, bytes]:
    """The branch's tree as {path: bytes}."""
    paths = git(root, "ls-tree", "-r", "--name-only", store.BRANCH).splitlines()
    return {path: subprocess.run(["git", "-C", str(root), "cat-file", "blob", f"{store.BRANCH}:{path}"],
                                 check=True, capture_output=True).stdout for path in paths}


def test_publish_creates_the_orphan_branch_holding_only_the_bodies(repo, capsys):
    assert store.publish(repo) == 0
    commit = capsys.readouterr().out.splitlines()[-1]
    assert git(repo, "rev-parse", "refs/heads/module-snapshot") == commit
    assert git(repo, "rev-list", "--count", commit) == "1"  # a root commit: orphan
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # never checked out
    assert branch_tree(repo) == {f"modules/{sha(body)}": body for body in BODIES}
    # Nothing new: no commit.
    assert store.publish(repo) == 0
    assert capsys.readouterr().out.splitlines()[-1] == commit
    assert git(repo, "rev-parse", store.BRANCH) == commit


def test_a_later_publish_adds_one_commit_and_never_deletes(repo, capsys):
    store.publish(repo)
    first = capsys.readouterr().out.splitlines()[-1]
    # A refreeze drops one body and adds another.
    modules = repo / "module-snapshot/modules"
    (modules / sha(BODIES[0])).unlink()
    added = b"export const c = 3;\n"
    write_snapshot(repo, [*BODIES[1:], added])
    assert store.publish(repo) == 0
    second = capsys.readouterr().out.splitlines()[-1]
    assert git(repo, "rev-parse", f"{second}^") == first
    assert set(branch_tree(repo)) == {f"modules/{sha(body)}" for body in [*BODIES, added]}


def test_publish_refuses_a_body_not_named_by_its_sha256(repo):
    (repo / "module-snapshot/modules" / sha(b"other")).write_bytes(b"tampered")
    with pytest.raises(store.StoreError, match="not a body named by its sha256"):
        store.publish(repo)
    assert store.resolve(repo, store.BRANCH) is None


def test_fetch_fills_an_empty_folder_in_a_fresh_clone(repo, tmp_path, capsys):
    store.publish(repo)
    clone = tmp_path / "clone"
    # One branch, shallow: no module-snapshot ref of any kind, as in CI.
    git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "main", repo.as_uri(), str(clone))
    assert not (clone / "module-snapshot/modules").exists()
    assert store.resolve(clone, store.REMOTE) is None
    assert store.check(clone) == 1
    assert store.FETCH in capsys.readouterr().out
    assert store.fetch(clone) == 0  # fetches origin's branch, then writes from it
    assert store.resolve(clone, store.REMOTE) == git(repo, "rev-parse", store.BRANCH)
    for body in BODIES:
        assert (clone / "module-snapshot/modules" / sha(body)).read_bytes() == body
    assert sorted(p.name for p in (clone / "module-snapshot/modules").iterdir()) == sorted(map(sha, BODIES))
    assert store.check(clone) == 0
    assert git(clone, "status", "--porcelain") == ""  # the bodies are ignored on main


def test_fetch_is_idempotent_and_makes_the_folder_follow_the_index(repo, capsys):
    store.publish(repo)
    modules = repo / "module-snapshot/modules"
    assert store.fetch(repo) == 0
    assert "(0 written, 3 already there, 0 unnamed removed)" in capsys.readouterr().out
    before = {path.name: path.read_bytes() for path in modules.iterdir()}
    assert store.fetch(repo) == 0
    assert {path.name: path.read_bytes() for path in modules.iterdir()} == before
    # A body that differs from its name is rewritten from the branch. A
    # checkout whose index no longer names a body: the published body goes,
    # an unpublished file stays (it exists nowhere else).
    (modules / sha(BODIES[1])).write_bytes(b"changed")
    stray = b"never published"
    (modules / sha(stray)).write_bytes(stray)
    write_snapshot(repo, [], named=BODIES[1:])
    assert store.fetch(repo) == 0
    out = capsys.readouterr().out
    assert "(1 written, 1 already there, 1 unnamed removed)" in out
    assert f"kept module-snapshot/modules/{sha(stray)}" in out
    assert (modules / sha(BODIES[1])).read_bytes() == BODIES[1]
    assert sorted(path.name for path in modules.iterdir()) == sorted([sha(BODIES[1]), sha(BODIES[2]), sha(stray)])


def test_a_body_that_exists_nowhere_is_refused_by_name(repo, tmp_path):
    store.publish(repo)
    missing = b"frozen but never published"
    write_snapshot(repo, [], named=[*BODIES, missing])
    with pytest.raises(store.StoreError) as refused:
        store.fetch(repo)  # no origin: the fetch fails too, and says so
    assert f"modules/{sha(missing)}" in str(refused.value)
    assert "1 of the 4 bodies" in str(refused.value) and "git fetch" in str(refused.value)
    # Through the command line: exit 1, the name on stderr.
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "main", repo.as_uri(), str(clone))
    write_snapshot(clone, [], named=[*BODIES, missing])
    result = subprocess.run([sys.executable, "-c",
                             "import sys; sys.path.insert(0, sys.argv[1]); import snapshot_store as s; "
                             "from pathlib import Path; sys.exit(s.main(['fetch'], root=Path(sys.argv[2])))",
                             str(ROOT / "build"), str(clone)], capture_output=True, text=True)
    assert result.returncode == 1
    assert f"modules/{sha(missing)}" in result.stderr
    assert not (clone / "module-snapshot/modules" / sha(missing)).exists()


def test_main_ignores_the_bodies_and_commits_the_lockfile():
    snapshot = ROOT / "module-snapshot"
    ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", f"module-snapshot/modules/{'0' * 64}"])
    assert ignored.returncode == 0
    for name in ("index.json", "LICENSES.json", "builders.json"):
        assert git(ROOT, "ls-files", f"module-snapshot/{name}") == f"module-snapshot/{name}"
    assert git(ROOT, "ls-files", "module-snapshot/modules") == ""
    assert (snapshot / "index.json").is_file()
