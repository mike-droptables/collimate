"""Plain git for api tests that build real repositories under ``tmp_path``.

``git`` runs the host's git with no ambient configuration (no system or
global config, HOME at the directory it runs in, a fixed author), never through module
code, so a fixture means the same on every machine. ``GIT_OK`` skips a test
where this machine has no git at the floor.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from collimate.git_hosts import MIN_GIT


def _git_version_ok() -> bool:
    if not shutil.which("git"):
        return False
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.search(r"git version (\d+)\.(\d+)", out)
    return bool(match) and (int(match.group(1)), int(match.group(2))) >= MIN_GIT


GIT_OK = pytest.mark.skipif(not _git_version_ok(), reason=f"needs git {'.'.join(map(str, MIN_GIT))} or later")


def git_env(home: str, extra: dict | None = None) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(HOME=home, XDG_CONFIG_HOME=home, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_AUTHOR_NAME="Ada", GIT_AUTHOR_EMAIL="ada@example.com", GIT_COMMITTER_NAME="Ada",
               GIT_COMMITTER_EMAIL="ada@example.com", GIT_EDITOR="true", LC_ALL="C")
    env.update(extra or {})
    return env


def git(cwd, *args, input: bytes | None = None, env: dict | None = None, check: bool = True) -> str:
    """Run ``git args`` in ``cwd``; its stdout, decoded. A failure raises with git's words."""
    done = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True,
                          env=git_env(os.path.abspath(str(cwd)), env), timeout=60)
    if check and done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} exited {done.returncode}: {done.stderr.decode()}")
    return done.stdout.decode("utf-8", "replace")


def init(path, branch: str = "main") -> str:
    """A new repository at ``path`` on ``branch``; its path."""
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q")
    git(path, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    return str(path)


def commit(repo, files: dict, message: str = "change") -> str:
    """Write ``files`` (path → text, None deletes), commit them all; the new sha."""
    for name, text in files.items():
        full = os.path.join(str(repo), name)
        if text is None:
            git(repo, "rm", "-q", "--", name)
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)
        git(repo, "add", "--", name)
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()
