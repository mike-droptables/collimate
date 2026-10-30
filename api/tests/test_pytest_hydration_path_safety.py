"""Path-traversal hardening for workspace hydration.

``cell_files.filepath`` is treated as untrusted input on the read side: a
malicious or buggy producer could write a row whose filepath escapes the
workspace dir. Before the hardening, ``shutil.copy2`` would gladly land
``../../etc/shadow`` outside the temp workspace under the server's uid.
The ``safe_dest_under`` helper is the single chokepoint that rejects
those rows; these tests pin its behavior.
"""
from __future__ import annotations

import os


def test_safe_dest_under_accepts_normal_relative_path(tmp_path):
    from run_staging import safe_dest_under
    dest_dir = str(tmp_path)
    got = safe_dest_under(dest_dir, "subdir/file.txt")
    assert got == os.path.realpath(os.path.join(dest_dir, "subdir/file.txt"))


def test_safe_dest_under_accepts_root_level_file(tmp_path):
    from run_staging import safe_dest_under
    got = safe_dest_under(str(tmp_path), "file.txt")
    assert got == os.path.realpath(os.path.join(str(tmp_path), "file.txt"))


def test_safe_dest_under_rejects_parent_traversal(tmp_path):
    from run_staging import safe_dest_under
    assert safe_dest_under(str(tmp_path), "../escape.txt") is None
    assert safe_dest_under(str(tmp_path), "a/../../escape.txt") is None
    assert safe_dest_under(str(tmp_path), "../../etc/shadow") is None


def test_safe_dest_under_rejects_absolute_path(tmp_path):
    from run_staging import safe_dest_under
    assert safe_dest_under(str(tmp_path), "/etc/shadow") is None
    assert safe_dest_under(str(tmp_path), "/tmp/some_other") is None


def test_safe_dest_under_rejects_backslash_separators(tmp_path):
    """Backslashes are not used as path separators on POSIX, so a row
    using them is unusual and worth refusing as a precaution. (Production
    rows always use forward slashes.)"""
    from run_staging import safe_dest_under
    assert safe_dest_under(str(tmp_path), "..\\..\\etc\\shadow") is None
    assert safe_dest_under(str(tmp_path), "subdir\\file.txt") is None


def test_safe_dest_under_handles_dot_segments(tmp_path):
    """``./file.txt`` should normalize cleanly to dest_dir/file.txt and pass."""
    from run_staging import safe_dest_under
    got = safe_dest_under(str(tmp_path), "./file.txt")
    assert got == os.path.realpath(os.path.join(str(tmp_path), "file.txt"))


def test_safe_dest_under_resolves_dest_dir_via_realpath(tmp_path):
    """If dest_dir itself is a symlink (e.g. /tmp -> /private/tmp on macOS),
    the comparison must use the resolved form. Otherwise the prefix check
    fails on legitimate paths."""
    from run_staging import safe_dest_under
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    os.symlink(real_dir, link_dir)
    got = safe_dest_under(str(link_dir), "file.txt")
    # Must resolve to the underlying real path so the containment check
    # works regardless of how the caller addresses the workspace.
    assert got == os.path.realpath(os.path.join(str(real_dir), "file.txt"))
