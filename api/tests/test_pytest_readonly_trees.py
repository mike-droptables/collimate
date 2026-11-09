"""Read-only trees (``api/collimate_server/readonly_trees.py``): a run's read-only ``.git``
is stripped of every write bit, and every place that removes a workspace
or a materialized ``.git`` removes one, where ``shutil.rmtree`` cannot; a
newer state replaces its entries in place, keeping the directory a
container binds."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import collimate_server.readonly_trees as readonly_trees

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root writes through any mode")


def _tree(top: Path) -> Path:
    (top / "refs" / "heads").mkdir(parents=True)
    (top / "HEAD").write_text("ref: refs/heads/main\n")
    (top / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    return top


def _modes(top: Path) -> dict[str, int]:
    return {str(p.relative_to(top)): stat.S_IMODE(p.lstat().st_mode) for p in [top, *top.rglob("*")]}


def test_make_readonly_strips_every_write_bit_and_leaves_links_alone(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("x")
    outside.chmod(0o644)
    top = _tree(tmp_path / ".git")
    (top / "link").symlink_to(outside)
    readonly_trees.make_readonly(str(top))
    modes = _modes(top)
    assert modes["."] == modes["refs"] == modes["refs/heads"] == 0o555
    assert modes["HEAD"] == modes["refs/heads/main"] == 0o444
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    with pytest.raises(PermissionError):
        (top / "index.lock").write_text("")


def test_a_read_only_tree_defeats_rmtree_and_remove_tree_removes_it(tmp_path):
    top = _tree(tmp_path / "ws" / "proj" / ".git")
    readonly_trees.make_readonly(str(top))
    with pytest.raises(PermissionError):
        shutil.rmtree(tmp_path / "ws")
    hidden = tmp_path / "ws" / "locked"
    hidden.mkdir()
    (hidden / "f").write_text("f")
    hidden.chmod(0o000)
    readonly_trees.remove_tree(str(tmp_path / "ws"))
    assert not (tmp_path / "ws").exists()
    readonly_trees.remove_tree(str(tmp_path / "ws"), ignore_errors=True)
    with pytest.raises(FileNotFoundError):
        readonly_trees.remove_tree(str(tmp_path / "ws"))


def test_remove_tree_unlinks_a_link_and_never_follows_one(tmp_path):
    kept = _tree(tmp_path / "kept")
    readonly_trees.make_readonly(str(kept))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "to-kept").symlink_to(kept)
    (tmp_path / "alias").symlink_to(kept)
    readonly_trees.remove_tree(str(ws))
    readonly_trees.remove_tree(str(tmp_path / "alias"))
    assert _modes(kept)["."] == 0o555 and (kept / "HEAD").exists()


def test_replace_entries_keeps_the_directory_and_ends_read_only(tmp_path):
    target = _tree(tmp_path / ".git")
    (target / "ORIG_HEAD").write_text("old\n")
    readonly_trees.make_readonly(str(target))
    inode = target.stat().st_ino
    fresh = _tree(tmp_path / "fresh")
    (fresh / "refs" / "heads" / "main").write_text("b" * 40 + "\n")
    (fresh / "index").write_bytes(b"DIRC")
    stale = tmp_path / "stale"
    stale.mkdir()
    readonly_trees.replace_entries(str(target), str(fresh), str(stale))
    assert target.stat().st_ino == inode
    assert sorted(os.listdir(target)) == ["HEAD", "index", "refs"]
    assert (target / "refs" / "heads" / "main").read_text() == "b" * 40 + "\n"
    assert set(_modes(target).values()) <= {0o555, 0o444}
    assert not stale.exists() and sorted(os.listdir(fresh)) == []


def test_replace_entries_and_remove_tree_never_follow_a_link_to_their_target(tmp_path):
    """A link named as the target (in ``dir_fd``) is refused or unlinked;
    the tree it points at keeps every entry."""
    outside = _tree(tmp_path / "outside")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").symlink_to(outside, target_is_directory=True)
    fresh = _tree(tmp_path / "fresh")
    stale = tmp_path / "stale"
    stale.mkdir()
    fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            readonly_trees.replace_entries(".git", str(fresh), str(stale), dir_fd=fd)
        readonly_trees.remove_tree(".git", dir_fd=fd)
    finally:
        os.close(fd)
    assert not os.path.lexists(work / ".git")
    assert sorted(os.listdir(outside)) == ["HEAD", "refs"] and os.access(outside, os.W_OK)


def test_a_finished_runs_workspace_with_a_read_only_git_is_reclaimed(tmp_path, monkeypatch):
    from collimate_server.process_manager.finalization import reclaim_run_workspace
    monkeypatch.chdir(tmp_path)
    run_id = "0123abcd-0000-4000-8000-000000000000"
    ws = Path(".collimate", "tmp", f"ws_{run_id[:8]}_x").absolute()
    readonly_trees.make_readonly(str(_tree(ws / "proj" / ".git")))
    assert reclaim_run_workspace(SimpleNamespace(workspace_dir=str(ws), run_id=run_id)) is True
    assert not ws.exists()
