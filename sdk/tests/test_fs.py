# SPDX-License-Identifier: MIT
"""``collimate.fs`` — the public, symlink-safe file access the SDK's own
workspace I/O and generator code share (a container is root in the
bind-mounted workspace and can plant a link or FIFO there)."""
from __future__ import annotations

import errno
import os

import pytest

from collimate import fs


pytestmark = pytest.mark.skipif(not fs.FD_SAFE, reason="needs dir_fd support")


def test_write_read_append_and_unlink(tmp_path):
    fs.write_file(str(tmp_path), "a/b/c.txt", "one\n")
    fs.append_bytes(str(tmp_path), "a/b/c.txt", b"two\n")
    with os.fdopen(fs.open_file(str(tmp_path), "a/b/c.txt", write=False)) as fh:
        assert fh.read() == "one\ntwo\n"
    fs.unlink(str(tmp_path), "a/b/c.txt")
    fs.unlink(str(tmp_path), "a/b/c.txt")               # missing: a no-op
    assert not (tmp_path / "a" / "b" / "c.txt").exists()


def test_a_planted_symlink_is_never_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("host file")
    ws = tmp_path / "ws"
    (ws / "d").mkdir(parents=True)
    (ws / "d" / "leaf.txt").symlink_to(outside)
    (ws / "linkdir").symlink_to(tmp_path)
    with pytest.raises(OSError):
        fs.write_file(str(ws), "d/leaf.txt", "PWNED")
    with pytest.raises(OSError):
        fs.write_file(str(ws), "linkdir/outside.txt", "PWNED")
    assert outside.read_text() == "host file"
    fs.unlink(str(ws), "d/leaf.txt")                     # the link, never its target
    assert outside.exists() and not os.path.lexists(ws / "d" / "leaf.txt")


def test_a_fifo_is_refused_not_blocked_on(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(OSError):
        fs.open_file(str(tmp_path), "pipe", write=False)


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../x", "", "."])
def test_paths_outside_the_root_are_refused(tmp_path, path):
    with pytest.raises(PermissionError):
        fs.relative_parts(path)


def test_the_helpers_live_only_in_the_public_module():
    import collimate._host.bridge as host
    for gone in ("_FD_SAFE", "_O_NOFOLLOW", "_O_NONBLOCK", "_O_DIRECTORY", "_ws_parts",
                 "_open_ws_dir", "_ws_open", "_ws_unlink", "ws_write_file", "ws_append_bytes"):
        assert not hasattr(host, gone), gone
    assert set(fs.__all__) == {
        "FD_SAFE", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "WORKSPACE_SKIP_DIRS", "append_bytes",
        "open_file", "open_parent", "relative_parts", "unlink", "walk_regular", "write_file"}


@pytest.mark.parametrize("parts", [
    ["..", "x"], ["a", "..", "x"], ["/etc", "passwd"], ["a/b", "c"], ["", "x"],
    [".", "x"], ["x", ""], [],
])
def test_open_parent_refuses_a_component_that_is_not_a_plain_name(tmp_path, parts):
    """A caller's own parts list gets the same containment relative_parts
    gives a path: no climbing out, no absolute component (which ignores the
    directory fd), no separators smuggled inside one name."""
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(PermissionError):
        fs.open_parent(str(ws), parts, create=False)


def test_the_fallback_unlink_refuses_a_symlinked_parent(tmp_path, monkeypatch):
    """Without dir_fd support the helpers fall back to a resolved-location
    check; unlink checks the directory holding the entry, so a symlinked
    parent cannot redirect the removal outside the root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("host file")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "linkdir").symlink_to(outside)
    (ws / "own.txt").write_text("x")
    monkeypatch.setattr(fs, "FD_SAFE", False)
    with pytest.raises(PermissionError):
        fs.unlink(str(ws), "linkdir/victim.txt")
    assert (outside / "victim.txt").read_text() == "host file"
    fs.unlink(str(ws), "own.txt")
    assert not (ws / "own.txt").exists()


def _walk(top, **kw):
    """Every (path, bytes) walk_regular yields, and the errors it reports."""
    errors = []
    found = {}
    for path, fd in fs.walk_regular(str(top), onerror=errors.append, **kw):
        found[path] = os.pread(fd, 1 << 16, 0)
    return found, errors


def test_walk_regular_yields_only_regular_files_and_follows_nothing(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host file")
    ws = tmp_path / "ws"
    (ws / "a" / "b").mkdir(parents=True)
    (ws / "top.txt").write_text("top")
    (ws / "a" / "b" / "deep.txt").write_text("deep")
    (ws / "a" / "link.txt").symlink_to(outside / "secret.txt")
    (ws / "a" / "linkdir").symlink_to(outside)
    os.mkfifo(ws / "a" / "pipe")
    found, errors = _walk(ws)
    assert found == {"top.txt": b"top", "a/b/deep.txt": b"deep"}
    assert errors == []
    assert list(fs.walk_regular(str(ws / "a" / "linkdir"))) == []   # a symlinked top is not entered


def test_walk_regular_closes_each_fd_when_it_resumes(tmp_path):
    (tmp_path / "f").write_text("x")
    for _, fd in fs.walk_regular(str(tmp_path)):
        held = fd
    with pytest.raises(OSError):
        os.fstat(held)


def test_walk_regular_prune_sees_every_entry_and_leaves_out_what_it_answers(tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "x").write_text("x")
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "y").write_text("y")
    (tmp_path / "unopened").write_text("z")
    (tmp_path / "ln").symlink_to(tmp_path / "keep")
    seen = []

    def prune(path, entry):
        seen.append((path, entry.is_symlink()))
        return path in ("skip", "unopened")

    found, _ = _walk(tmp_path, prune=prune)
    assert found == {"keep/x": b"x"}
    assert sorted(seen) == [("keep", False), ("keep/x", False), ("ln", True), ("skip", False), ("unopened", False)]


def test_walk_regular_reports_depth_and_entry_caps(tmp_path):
    deep = tmp_path / "d1" / "d2" / "d3"
    deep.mkdir(parents=True)
    (tmp_path / "d1" / "one").write_text("1")
    (deep / "three").write_text("3")
    found, errors = _walk(tmp_path, max_depth=2)
    assert found == {"d1/one": b"1"}
    assert [(e.filename, e.errno) for e in errors] == [("d1/d2/d3", errno.ELOOP)]
    for n in range(5):
        (tmp_path / f"f{n}").write_text("x")
    found, errors = _walk(tmp_path, max_entries=3)
    assert found == {}
    assert [(e.filename, e.errno) for e in errors] == [("d1/d2/d3/three", errno.E2BIG)]   # where the cap was hit


def test_walk_regular_without_dir_fd_support_refuses_a_dir_fd(tmp_path, monkeypatch):
    (tmp_path / "f").write_text("x")
    monkeypatch.setattr(fs, "FD_SAFE", False)
    fd = os.open(str(tmp_path), os.O_RDONLY)
    try:
        with pytest.raises(ValueError):
            list(fs.walk_regular(".", dir_fd=fd))
    finally:
        os.close(fd)
    assert [path for path, _ in fs.walk_regular(str(tmp_path))] == ["f"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_walk_regular_reports_what_it_cannot_open(tmp_path):
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "x").write_text("x")
    (tmp_path / "unreadable").write_text("x")
    (tmp_path / "ok").write_text("ok")
    os.chmod(tmp_path / "locked", 0)
    os.chmod(tmp_path / "unreadable", 0)
    try:
        found, errors = _walk(tmp_path)
    finally:
        os.chmod(tmp_path / "locked", 0o755)
    assert found == {"ok": b"ok"}
    assert sorted(e.filename for e in errors) == ["locked", "unreadable"]
    _, errors = _walk(tmp_path / "missing")
    assert [(e.filename, e.errno) for e in errors] == [("", errno.ENOENT)]


def test_walk_regular_opens_its_top_from_a_directory_fd(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f").write_text("f")
    dfd = os.open(tmp_path, os.O_RDONLY)
    try:
        assert [p for p, _ in fs.walk_regular("sub", dir_fd=dfd)] == ["f"]
        assert [p for p, _ in fs.walk_regular(".", dir_fd=dfd)] == ["sub/f"]
    finally:
        os.close(dfd)
