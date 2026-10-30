"""The .collrun staging band check agrees with where hydration writes.

Hydration skips reserved cell paths so a cell file can never land on the
host-staged runner config, and it resolves each path with normpath. The
reserved check must normalize the same way, or ``a/../.collrun/...`` would
pass as ordinary and then land inside the band.
"""
from __future__ import annotations

import os

import pytest

from reserved_paths import is_reserved_path
from run_staging import safe_dest_under


@pytest.mark.parametrize("path", [
    ".collrun", ".collrun/prompt.md", "/.collrun/config/files/x.set_yaml",
    "a/../.collrun/config/files/x.set_yaml", "./.collrun/prompt.md",
    "a/b/../../.collrun", ".collrun/./x", "a\\..\\.collrun\\x",
])
def test_paths_resolving_into_the_band_are_reserved(path, tmp_path):
    assert is_reserved_path(path)
    dest = safe_dest_under(str(tmp_path), path.replace("\\", "/"))
    if dest is not None:
        rel = os.path.relpath(dest, os.path.realpath(tmp_path))
        assert rel == ".collrun" or rel.startswith(".collrun" + os.sep)


@pytest.mark.parametrize("path", ["notes.txt", ".collrunner/x", "x/.collrun/y", "a/../b.txt", ""])
def test_ordinary_paths_are_not_reserved(path):
    assert not is_reserved_path(path)
