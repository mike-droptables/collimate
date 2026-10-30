# SPDX-License-Identifier: MIT
"""``collimate.fs`` — the public, symlink-safe file access the SDK's own
workspace I/O and generator code share (a container is root in the
bind-mounted workspace and can plant a link or FIFO there)."""
from __future__ import annotations

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
        "open_file", "open_parent", "relative_parts", "unlink", "write_file"}


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
