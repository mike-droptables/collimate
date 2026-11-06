"""The reserved cell paths: the .collrun staging band and every .git.

Hydration skips reserved cell paths so a cell file can never land on the
host-staged runner config, and it resolves each path with normpath. The
reserved check must normalize the same way, or ``a/../.collrun/...`` would
pass as ordinary and then land inside the band. A ``.git``-equivalent
segment follows git's own ``verify_path`` (the shared contract vector), and
a repository path is narrower still.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from collimate_server.reserved_paths import is_dotgit_segment, is_repo_path, is_reserved_path
from collimate_server.run_staging import safe_dest_under


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


def test_reserved_paths_follow_the_cross_language_contract():
    import collimate
    fixture = json.loads((Path(collimate.__file__).resolve().parents[2] / "shared/paths-contract.json").read_text())
    for row in fixture["paths"]:
        assert is_reserved_path(row["path"]) is row["reserved"], row


@pytest.mark.parametrize("segment", [
    ".git", ".GIT", ".Git.", ".git ", " .git", ".git. . ", "git~1", "GIT~1", ".git::$INDEX_ALLOCATION",
    ".git:stream", "\u200c.git", ".g\u200dit", ".git\u200f", "\u202a.git\u202e", ".g\u206ait", "\ufeff.git",
])
def test_dotgit_equivalents(segment):
    assert is_dotgit_segment(segment)
    assert is_reserved_path(f"a/{segment}/b") and is_reserved_path(segment)


@pytest.mark.parametrize("segment", [".gitignore", ".github", ".gitmodules", "git~2", "git", "x.git", "g.it", ".git~1"])
def test_dotgit_lookalikes_are_ordinary(segment):
    assert not is_dotgit_segment(segment) and not is_reserved_path(f"a/{segment}")


@pytest.mark.parametrize("path", ["", "repo", "a/b", "My Repo/src", "a/.github", "x.git"])
def test_repository_paths(path):
    assert is_repo_path(path)


@pytest.mark.parametrize("path", [
    ".", "/a", "a/", "a//b", "a/../b", "../a", "a\\b", ".git", "a/.GIT", "GIT~1/x", ".collrun", "a/.collrun",
    "_runs", "_runs/x", "x/_Runs", "node_modules", "a/Node_Modules/b", ".venv", "__pycache__", "a/.git./b",
])
def test_paths_that_cannot_hold_a_repository(path):
    assert not is_repo_path(path)
