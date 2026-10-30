# SPDX-License-Identifier: MIT
"""Shared fixtures for the git engine (``_env_shared.git_run``,
``_env_shared.git_account`` and ``_env_shared.repositories``) and the Git
generator's tests.

``fake_remote`` stands bare repositories in for every remote
(:class:`FakeRemote`, keyed by the canonical record remote): it swaps only
``git_run.transport`` for a ``file://`` Remote, so ``resolve_remote`` and a
bound account's ``auth_for`` run on the production address exactly as in a
run. ``fake_ssh`` instead puts a logging ``ssh`` first on PATH that serves
the same repositories over git's ssh protocol (``data/fake_ssh.py``).
Seeding uses plain ``git`` with no ambient configuration and never calls
module code.

``git_spy`` wraps ``git_run._spawn`` (the observation seam): it records the
argv and environment of every git child, runs a hook just before a
subcommand, or fakes a subcommand's result instead of running it.

``git_account_snapshot`` builds the ``gen.account("git")`` snapshot of any
kind-git entry and ``bind_account`` binds it.

``ssh_keys`` is the session's throwaway SSH keys, made by ``ssh-keygen``
(``_ssh_keys.py``; no private key is committed); ``ssh_key`` reads one of the
same keys where a fixture cannot reach.

sdk/tests/conftest.py registers the four fixtures; a test module imports
only the helpers it names (``GIT_OK``, ``subcommand_of``, ``FakeRemote``,
``git_account_snapshot``, ``bind_account``, ``ssh_key``).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Callable, Union

import pytest

from _env_shared import git_run
from tests._ssh_keys import SshKeys, throwaway_keys

Content = Union[str, bytes, tuple]


def _git_version_ok() -> bool:
    if not shutil.which("git"):
        return False
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    match = re.search(r"git version (\d+)\.(\d+)", out)
    return bool(match) and (int(match[1]), int(match[2])) >= git_run.MIN_GIT


GIT_OK: bool = _git_version_ok()

_SEED_ENV_DROP = ("GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "SSH_ASKPASS", "SSH_AUTH_SOCK")


def _seed_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in _SEED_ENV_DROP}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", LC_ALL="C",
               GIT_AUTHOR_NAME="Upstream Dev", GIT_AUTHOR_EMAIL="dev@example.invalid",
               GIT_COMMITTER_NAME="Upstream Dev", GIT_COMMITTER_EMAIL="dev@example.invalid")
    return env


_FAKE_SSH = Path(__file__).parent / "data" / "fake_ssh.py"

GH = "https://github.com/octo/api.git"
GL = "https://example.com/gitlab/group/sub/app.git"
SSH = "ssh://git@box.example.org:2222/srv/app.git"


def repository_dir(root: Path, host: str, port: "int | None", path: str) -> Path:
    """Where a fake bare repository lives: ``root/<host>[_<port>]/<path>.git``,
    lowercased, with a leading ``/``, ``~/`` or ``~user/`` and one trailing
    ``.git`` removed from ``path`` (the fake ssh maps what git sends the same way)."""
    path = re.sub(r"^(/|~[A-Za-z0-9_.-]*/)", "", path).removesuffix(".git")
    where = host if port is None else f"{host}_{port}"
    return root / where.lower() / f"{path.lower()}.git"


def _record_dir(root: Path, remote: str) -> Path:
    if remote.startswith(("https://", "ssh://")):
        parts = urllib.parse.urlsplit(remote)
        return repository_dir(root, parts.hostname or "", parts.port, parts.path)
    _user, _, rest = remote.partition("@")
    host, _, path = rest.partition(":")
    return repository_dir(root, host, None, path)


class FakeRemote:
    """Bare repositories under ``root`` keyed by a canonical record remote
    (:func:`repository_dir`); every method takes that remote."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._home = root / ".home"
        self._home.mkdir(exist_ok=True)
        self._env = _seed_env(self._home)
        self._counter = 0

    # -- plumbing ---------------------------------------------------------

    def path(self, repository: str) -> Path:
        return _record_dir(self.root, repository)

    def url(self, repository: str) -> str:
        """The ``file://`` URL of the record ``repository``."""
        return f"file://{self.path(repository)}"

    def git(self, repository: str, *args: str, input: bytes | None = None,
            index: Path | None = None, check: bool = True) -> bytes:
        env = dict(self._env)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        result = subprocess.run(["git", *args], cwd=self.path(repository), env=env, input=input,
                                capture_output=True, timeout=60)
        if check and result.returncode:
            raise RuntimeError(f"seed git {args[0]} failed: {result.stderr.decode(errors='replace')}")
        return result.stdout

    def _blob(self, repository: str, content: bytes) -> str:
        return self.git(repository, "hash-object", "-w", "--stdin", input=content).decode().strip()

    def _write_commit(self, repository: str, parent: str | None, files: dict, branch: str,
                      message: str) -> str:
        self._counter += 1
        index = self.root / f".index-{self._counter}"
        try:
            if parent:
                self.git(repository, "read-tree", parent, index=index)
            else:
                self.git(repository, "read-tree", "--empty", index=index)
            for path, content in files.items():
                if content is None:
                    self.git(repository, "update-index", "--index-info", index=index,
                             input=f"0 {'0' * 40}\t{path}\n".encode())
                    continue
                mode, sha = self._entry(repository, content)
                self.git(repository, "update-index", "--add", "--replace", "--cacheinfo",
                         f"{mode},{sha},{path}", index=index)
            tree = self.git(repository, "write-tree", index=index).decode().strip()
        finally:
            index.unlink(missing_ok=True)
        args = ["commit-tree", tree, "-m", message]
        if parent:
            args += ["-p", parent]
        commit = self.git(repository, *args).decode().strip()
        self.git(repository, "update-ref", f"refs/heads/{branch}", commit)
        return commit

    def _entry(self, repository: str, content: Content) -> tuple[str, str]:
        if isinstance(content, str):
            return "100644", self._blob(repository, content.encode("utf-8"))
        if isinstance(content, (bytes, bytearray)):
            return "100644", self._blob(repository, bytes(content))
        kind, value = content
        if kind == "link":
            return "120000", self._blob(repository, str(value).encode("utf-8"))
        if kind == "exec":
            data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
            return "100755", self._blob(repository, data)
        if kind == "gitlink":
            return "160000", str(value)
        raise ValueError(f"unknown fake content kind {kind!r}")

    # -- the API tests use --------------------------------------------------

    def create(self, repository: str, files: dict, *, branch: str = "main",
               message: str = "initial") -> str:
        """A bare repository whose first commit on ``branch`` holds ``files``."""
        path = self.path(repository)
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", branch, str(path)],
                       env=self._env, check=True, capture_output=True, timeout=60)
        self.git(repository, "config", "uploadpack.allowFilter", "true")
        return self._write_commit(repository, None, files, branch, message)

    def commit(self, repository: str, files: dict, *, branch: str = "main",
               message: str = "upstream") -> str:
        """A new commit on ``branch`` (created from the default head when it
        does not exist yet); ``None`` deletes a path. Returns the new head."""
        parent = self.head(repository, branch)
        if parent is None:
            parent = self.git(repository, "rev-parse", "HEAD").decode().strip()
        return self._write_commit(repository, parent, files, branch, message)

    def head(self, repository: str, branch: str = "main") -> str | None:
        out = self.git(repository, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                       check=False).decode().strip()
        return out or None

    def branches(self, repository: str) -> dict[str, str]:
        out = self.git(repository, "for-each-ref", "--format=%(refname:short) %(objectname)",
                       "refs/heads").decode()
        return dict(line.split(" ", 1) for line in out.splitlines() if line)

    def rewrite(self, repository: str, branch: str = "main") -> str:
        """Drop the last commit of ``branch`` and put a different one in its place."""
        head = self.head(repository, branch)
        parent = self.git(repository, "rev-parse", f"{head}^").decode().strip()
        self._counter += 1
        return self._write_commit(repository, parent, {f"rewritten-{self._counter}.txt": "rewritten\n"},
                                  branch, "rewritten history")

    def delete_branch(self, repository: str, branch: str) -> None:
        self.git(repository, "update-ref", "-d", f"refs/heads/{branch}")

    def remove(self, repository: str) -> None:
        shutil.rmtree(self.path(repository))

    def tree(self, repository: str, ref: str) -> dict[str, bytes]:
        """The files at ``ref`` that a cell holds: regular blobs no larger
        than the engine's cap, outside the folders cells never keep."""
        from collimate.fs import WORKSPACE_SKIP_DIRS
        from _env_shared import repositories
        out = self.git(repository, "ls-tree", "-r", "-z", ref).split(b"\0")
        files: dict[str, bytes] = {}
        for record in filter(None, out):
            meta, path = record.decode().split("\t", 1)
            mode, _kind, sha = meta.split()
            if mode not in ("100644", "100755"):
                continue
            if set(path.split("/")) & WORKSPACE_SKIP_DIRS:
                continue
            data = self.git(repository, "cat-file", "blob", sha)
            if len(data) <= repositories.FILE_CAP:
                files[path] = data
        return files

    def commit_info(self, repository: str, ref: str) -> dict:
        out = self.git(repository, "log", "-1", "--format=%P%x00%an <%ae>%x00%B", ref).decode()
        parents, author, message = out.split("\0", 2)
        return {"parents": parents.split(), "author": author, "message": message.rstrip("\n")}


@pytest.fixture
def fake_remote(tmp_path, monkeypatch) -> FakeRemote:
    """Bare repositories stand in for every remote: only ``git_run.transport``
    is swapped for a ``file://`` Remote of the record, so ``resolve_remote``
    and the bound account's ``auth_for`` still see the production address."""
    hub = FakeRemote(tmp_path / "remote")
    monkeypatch.setattr(git_run, "transport",
                        lambda remote, record: git_run.Remote(hub.url(record), ("file",)))
    return hub


class FakeSsh:
    """The ``fake_ssh`` fixture: ``hub`` holds the repositories, ``calls()``
    the JSON line each ssh invocation logged."""

    def __init__(self, hub: FakeRemote, log: Path, bin_dir: Path) -> None:
        self.hub = hub
        self.log = log
        self.bin = bin_dir

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def command(self) -> str:
        """The fake ssh's absolute path (for a plain ``GIT_SSH_COMMAND``)."""
        return str(self.bin / "ssh")


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch) -> FakeSsh:
    """An executable ``ssh`` first on PATH that logs each call and serves
    ``git-upload-pack``/``git-receive-pack`` from a FakeRemote root, refusing a
    host its UserKnownHostsFile does not name ("Host key verification
    failed.", exit 255). Neither ``resolve_remote`` nor ``transport`` is patched."""
    hub = FakeRemote(tmp_path / "remote")
    bin_dir = tmp_path / "fake-ssh-bin"
    bin_dir.mkdir()
    script = bin_dir / "ssh"
    script.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(_FAKE_SSH))} \"$@\"\n")
    script.chmod(0o755)
    log = tmp_path / "fake-ssh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}")
    monkeypatch.setenv("FAKE_SSH_ROOT", str(hub.root))
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return FakeSsh(hub, log, bin_dir)


# ── accounts ───────────────────────────────────────────────────────────

_DEFAULT_ENDPOINT = {"github": "https://github.com", "gitlab": "https://gitlab.com"}
_DEFAULT_TOKEN = {"github": "ghp_fake", "gitlab": "glpat-fake"}


@pytest.fixture(scope="session")
def ssh_keys() -> SshKeys:
    """The throwaway SSH keys ``ssh-keygen`` makes once per session; skips
    without ``ssh-keygen``."""
    return throwaway_keys()


def ssh_key(name: str = "ed25519") -> str:
    """One of the session's throwaway SSH keys (``ssh_keys``): ``<name>`` is a
    private key, ``<name>.pub`` its public key line."""
    return throwaway_keys()[name]


def git_account_snapshot(service: str = "github", auth: str = "api_key", **over) -> dict:
    """A ``gen.account("git")`` private snapshot of a kind-git entry. ``over``
    replaces top-level fields; ``metadata`` merges into the default metadata
    (a ``None`` value removes that key)."""
    if auth == "basic":
        endpoint = "https://git.example.org" if service == "git" else _DEFAULT_ENDPOINT.get(service, "")
    else:
        endpoint = _DEFAULT_ENDPOINT.get(service, "")
    metadata = {"author_name": "Ada Lovelace", "author_email": "ada@example.org"}
    if auth == "basic":
        metadata["username"] = "ada"
    secrets = {"api_key": {"api_key": _DEFAULT_TOKEN.get(service, "ghp_fake")}, "basic": {"password": "pw-fake"},
               "ssh_key": {"private_key": ssh_key()}}.get(auth, {})
    snapshot = {"id": f"{service}-{auth}", "name": f"{service} {auth}", "kind": "git", "service": service,
                "protocol": service, "endpoint": endpoint, "model": "", "capabilities": ["git"],
                "auth_type": auth, "metadata": metadata, "credential_account_id": None, "secrets": secrets}
    for key, value in over.items():
        if key == "metadata":
            for name, item in value.items():
                if item is None:
                    metadata.pop(name, None)
                else:
                    metadata[name] = item
        else:
            snapshot[key] = value
    return snapshot


def bind_account(monkeypatch, snapshot: "dict | None", role: str = "git") -> None:
    """Make ``gen.account(role)`` answer ``snapshot`` (None: the role is unbound)."""
    import gen

    def lookup(name):
        if snapshot is None or name != role:
            raise LookupError(name)
        return snapshot

    monkeypatch.setattr(gen, "account", lookup)


# ── the spawn spy ──────────────────────────────────────────────────────


def subcommand_of(argv) -> str:
    """The git subcommand of an argv: the first word after ``git`` and its
    ``-c key=value`` options."""
    args = list(argv)[1:]
    i = 0
    while i < len(args):
        if args[i] == "-c":
            i += 2
            continue
        return args[i]
    return ""


class _FakeStdin:
    def write(self, data) -> None:
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _FakeProcess:
    """What ``asyncio.create_subprocess_exec`` returns, for a faked result."""

    def __init__(self, code: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = None
        self._code = code
        self.pid = -1
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = self._code
        return self._code

    async def communicate(self, input=None):
        await self.wait()
        return await self.stdout.read(), await self.stderr.read()

    def kill(self) -> None:
        self.returncode = -9


class GitSpy:
    def __init__(self, real: Callable) -> None:
        self._real = real
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self._before: list[tuple[str, Callable[[], None]]] = []
        self._fail: list[tuple[str, tuple[int, bytes, bytes]]] = []

    def before(self, subcommand: str, hook: Callable[[], None]) -> None:
        """Run ``hook`` just before the next spawn of ``subcommand``."""
        self._before.append((subcommand, hook))

    def fail(self, subcommand: str, *, code: int = 1, stdout: bytes = b"", stderr: bytes = b"") -> None:
        """Fake the next spawn of ``subcommand`` with this result instead of running it."""
        self._fail.append((subcommand, (code, stdout, stderr)))

    def subcommands(self) -> list[str]:
        return [subcommand_of(argv) for argv, _env in self.calls]

    async def spawn(self, *argv, **kwargs):
        sub = subcommand_of(argv)
        self.calls.append((tuple(str(a) for a in argv), dict(kwargs.get("env") or {})))
        for i, (name, hook) in enumerate(self._before):
            if name == sub:
                del self._before[i]
                hook()
                break
        for i, (name, result) in enumerate(self._fail):
            if name == sub:
                del self._fail[i]
                return _FakeProcess(*result)
        return await self._real(*argv, **kwargs)


@pytest.fixture
def git_spy(monkeypatch) -> GitSpy:
    spy = GitSpy(git_run._spawn)
    monkeypatch.setattr(git_run, "_spawn", spy.spawn)
    return spy
