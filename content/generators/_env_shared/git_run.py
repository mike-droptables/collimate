# SPDX-License-Identifier: MIT
"""Run git for one repository with no ambient config, credential helpers,
hooks or prompts, exactly one transport, and the credential only where the
account allows it: for HTTPS the Basic header the host put in the run's
environment (``secret_environment`` kind ``git``), scoped to the account's
base, for SSH a private key written to a 0600 file in a 0700 temporary
directory for the duration of one git command and deleted after, and swept
by the next Git run if the process was killed first (the single place in the
engine a secret touches a file; D4. The Agent's session key, Launch's
session key on this machine and Launch's container files are the other
exceptions, which docs/ai_tools/notes/2026-10-29/project_git_trust.md
records). git's own words reach the run's log only scrubbed of the
account's secret, after a line saying what failed; they are never returned
or stored. :func:`forward_git_environment` gives a
child that does not inherit this process's environment the same account
environment. This module does not import ``gen``. ``resolve_remote`` is the
one place a repository's address meets its transport; ``_spawn`` and
``transport`` are the test seams."""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from collimate.fs import WORKSPACE_SKIP_DIRS, walk_regular
from collimate.git_hosts import (
    AUTHOR_ENV, FORGE_ENV, HARDENING_FLAGS, MIN_GIT, GitAccount, HttpsAuth, Identity, Remote, SshAuth,
    basic_authorization, git_config_entries, git_config_env, hardened_env, host_key_name, protocol_flags,
)

__all__ = ["Completed", "Git", "GitFailure", "HttpsAuth", "Identity", "Remote", "SshAuth", "basic_authorization",
           "classify", "failure_line", "forward_git_environment", "git_environment", "git_prefix", "git_version",
           "repositories", "resolve_remote", "scrub", "search_path", "ssh_command", "sweep_stale_sessions",
           "transport", "write_private"]

NETWORK_TIMEOUT = 900
LS_REMOTE_TIMEOUT = 60
LOCAL_TIMEOUT = 120
VERSION_TIMEOUT = 15
_STDERR_CAP = 64 * 1024
_spawn = asyncio.create_subprocess_exec          # observation seam (tests wrap it)

FailureKind = Literal["missing_git", "broken_git", "old_git", "timeout", "network", "redirect", "auth",
                      "forbidden", "not_found", "host_key", "key", "missing_ssh", "other"]

_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")
# The protocols an account's header (an HttpsAuth, or the run's block) may
# travel on; only the git http-backend tests widen it to their loopback server.
_HEADER_PROTOCOLS: tuple[str, ...] = ("https",)
# The run's repository format pins (ref storage, object hash), which a git
# whose own defaults moved on would otherwise not keep.
_FORMAT_ENV = ("GIT_DEFAULT_REF_FORMAT", "GIT_DEFAULT_HASH")
_SESSION = re.compile(r"collimate-git-([1-9][0-9]{0,9})-[A-Za-z0-9_]+")
_SCP = re.compile(r"([^@/:]+)@([^@/:]+):(.*)", re.DOTALL)


def _ssh_parts(remote: str) -> "tuple[str, int | None, str] | None":
    """``(host, port, path)`` of an ``ssh://`` or scp address; None for any other."""
    if remote.lower().startswith("ssh://"):
        parts = urlsplit(remote)
        return (parts.hostname or "").lower(), parts.port, parts.path
    if "://" not in remote and (match := _SCP.fullmatch(remote)):
        return match[2].lower(), None, match[3]
    return None


def resolve_remote(remote: str, account: "GitAccount | None") -> Remote:
    """The production address of a repository's address, pinned to its one
    transport. A forge account with an SSH key reaches its https address
    over ``ssh://git@<web host>/<project>.git`` (the web base's path is not
    part of an SSH path); an ``ssh://`` or scp address goes to git byte for
    byte; anything else is https."""
    if account is not None and account.forge and account.transport == "ssh":
        web = urlsplit(account.web)
        prefix = web.path.rstrip("/") + "/"
        parts = urlsplit(remote)
        if (remote.startswith("https://") and (parts.hostname or "") == web.hostname
                and parts.port == web.port and parts.path.startswith(prefix)):
            project = parts.path[len(prefix):].removesuffix(".git")
            host = web.hostname or ""
            return Remote(f"ssh://git@{host}/{project}.git", ("ssh",), host_key_name(host, None))
    ssh = _ssh_parts(remote)
    if ssh is not None:
        host, port, _path = ssh
        return Remote(remote, ("ssh",), host_key_name(host, port))
    return Remote(remote, ("https",))


def transport(remote: Remote, url: str) -> Remote:
    """The address git really talks to for the repository at ``url``:
    ``remote`` itself. The test seam: ``fake_remote`` returns a ``file://``
    Remote for ``url`` instead."""
    return remote


# Where a cell keeps no repository: the host's bands and the workspace skip
# dirs, walked as the host's discovery walks them (``.git`` is never entered).
_NOT_SEARCHED = frozenset({*WORKSPACE_SKIP_DIRS, ".collrun", "_runs"} - {".git"})
_MAX_DEPTH = 64


def repositories(root: Path) -> list[str]:
    """The folders under ``root`` holding a repository, a directory named
    exactly ``.git`` (``"."`` for ``root`` itself), in path order, found as
    the host finds them (:func:`collimate.fs.walk_regular`): never inside a
    ``.git``, the host's bands or a skip dir, never through a link, at most
    64 levels down."""
    found: list[str] = []

    def prune(path: str, entry: os.DirEntry) -> bool:
        if entry.name == ".git":
            if entry.is_dir(follow_symlinks=False):
                found.append(path.rpartition("/")[0] or ".")
            return True
        return not entry.is_dir(follow_symlinks=False) or entry.name in _NOT_SEARCHED

    for _ in walk_regular(str(root), prune=prune, max_depth=_MAX_DEPTH):
        pass
    return sorted(found)


def sweep_stale_sessions() -> None:
    """Remove the temporary session folders (``collimate-git-<pid>-*``) of
    Git runs, Agent sessions and Launch runs on this machine that were
    killed before their ``finally`` ran, with any SSH key they held: only
    real folders of this user whose process is gone. Never raises, logs nothing."""
    try:
        root = Path(tempfile.gettempdir())
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        match = _SESSION.fullmatch(name)
        if match is None:
            continue
        pid = int(match[1])
        path = root / name
        try:
            info = os.lstat(path)
            if not os.path.isdir(path) or os.path.islink(path) or info.st_uid != os.getuid():
                continue
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                shutil.rmtree(path, ignore_errors=True)
            except (PermissionError, OverflowError, OSError):
                continue
        except OSError:
            continue


class GitFailure(RuntimeError):
    """A git command that did not succeed. Carries only the classification,
    the subcommand and the exit code (and git's version for ``old_git``);
    git's stderr is never stored."""

    def __init__(self, kind: FailureKind, command: str, code: int, *, version: str = "") -> None:
        super().__init__(f"git {command} failed ({kind})")
        self.kind = kind
        self.command = command
        self.code = code
        self.version = version


@dataclass(frozen=True)
class Completed:
    code: int
    stdout: bytes
    kind: "FailureKind | None"                   # classify(stderr) when code != 0, else None


def search_path() -> str:
    """The host PATH with only its absolute entries. git runs inside a
    checkout of the repository, where an empty or relative entry (``:/usr/bin``,
    ``node_modules/.bin``) would find a ``git`` or helper the repository ships."""
    return os.pathsep.join(entry for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
                           if os.path.isabs(entry))


_SSH_OPTIONS = (
    "IdentitiesOnly=yes", "IdentityAgent=none", "BatchMode=yes", "StrictHostKeyChecking=yes",
    None,  # UserKnownHostsFile
    "GlobalKnownHostsFile=/dev/null", "HashKnownHosts=no", "UpdateHostKeys=no", "CheckHostIP=no",
    "VerifyHostKeyDNS=no", "CanonicalizeHostname=no", "PasswordAuthentication=no",
    "KbdInteractiveAuthentication=no", "PreferredAuthentications=publickey", "ForwardAgent=no",
    "ForwardX11=no", "ClearAllForwardings=yes", "PermitLocalCommand=no", "ProxyCommand=none",
    "ProxyJump=none", "ControlMaster=no", "ControlPath=none", "ConnectTimeout=30",
    "ServerAliveInterval=15", "ServerAliveCountMax=4", "LogLevel=ERROR",
)


def ssh_command(ssh: str, key: Path, known_hosts: Path) -> str:
    """The ``GIT_SSH_COMMAND`` of one git command: no ssh config, only this
    key, no agent, no prompt, and only the hosts in ``known_hosts``."""
    words = [shlex.quote(ssh), "-F", "/dev/null", "-i", shlex.quote(str(key))]
    for option in _SSH_OPTIONS:
        words += ["-o", option if option is not None else f"UserKnownHostsFile={shlex.quote(str(known_hosts))}"]
    return " ".join(words)


def _carried(key: str) -> bool:
    """Whether a ``GIT_CONFIG_KEY_<n>`` of the run's environment may reach git:
    the account's ``http.<https base>/.extraHeader``, or ``safe.directory``."""
    if key.lower() == "safe.directory":
        return True
    section, _, rest = key.partition(".")
    url, _, variable = rest.rpartition(".")
    if section.lower() != "http" or variable.lower() != "extraheader" or not url.endswith("/"):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme in _HEADER_PROTOCOLS and bool(parts.hostname) and "@" not in parts.netloc
            and not parts.query and not parts.fragment)


def _account_block(source: Mapping[str, str]) -> list[tuple[str, str]]:
    """The run's ``GIT_CONFIG_COUNT`` block, kept whole only when git would
    read it and every key is :func:`_carried`; otherwise nothing of it."""
    try:
        entries = git_config_entries(source)
    except ValueError:
        return []
    return entries if all(_carried(key) for key, _value in entries) else []


def git_environment(auth: "HttpsAuth | SshAuth | None", home: Path, identity: "Identity | None" = None,
                    *, ssh_command: "str | None" = None, no_lazy_fetch: bool = False) -> dict[str, str]:
    """git's environment for one command: this process's through
    :func:`hardened_env` (no ``GIT_*``, tool token, askpass or SSH agent;
    HOME and XDG_CONFIG_HOME at ``home``; no system or global config; C
    locale) with only absolute PATH entries, then what the run's Git account
    put there: its ``GIT_CONFIG_COUNT`` block when every key is an
    extraHeader under an https base or ``safe.directory``, the author and
    committer, the forge names, and the run's ref format and hash pins
    (``GIT_DEFAULT_REF_FORMAT``, ``GIT_DEFAULT_HASH``). The ceiling is the run's
    ``GIT_CEILING_DIRECTORIES`` beside ``home``'s parent. ``auth``'s header
    joins the block once, ``identity`` replaces the author, and an SSH
    ``auth`` gets ``ssh_command``. Without an account and ``identity`` git
    asks who you are (D8)."""
    source = os.environ
    ceiling = os.pathsep.join(c for c in (source.get("GIT_CEILING_DIRECTORIES", ""), str(home.parent)) if c)
    env = hardened_env(source, home=str(home), ceiling=ceiling)
    env["PATH"] = search_path()
    env.update({name: source[name] for name in (*AUTHOR_ENV, *sorted(FORGE_ENV), *_FORMAT_ENV) if name in source})
    entries = _account_block(source)
    if isinstance(auth, HttpsAuth):
        entries.append((f"http.{auth.scope}.extraHeader", f"Authorization: Basic {auth.authorization}"))
    elif isinstance(auth, SshAuth) and ssh_command:
        env.update(GIT_SSH_COMMAND=ssh_command, GIT_SSH_VARIANT="ssh")
    env = git_config_env(entries, env)
    if identity is not None:
        env.update(GIT_AUTHOR_NAME=identity.name, GIT_AUTHOR_EMAIL=identity.email,
                   GIT_COMMITTER_NAME=identity.name, GIT_COMMITTER_EMAIL=identity.email)
    if no_lazy_fetch:
        env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def forward_git_environment(env: Mapping[str, str], *, container: bool = False) -> dict[str, str]:
    """``env``, the whole environment of a child that does not inherit this
    process's (a host adapter, a docker exec, a container), with the run's
    Git account environment added back from this process's: the host's
    ``GIT_CONFIG_COUNT`` block (the scoped Basic header) appended to
    ``env``'s own, and the author, committer and forge names, which ``env``
    cannot override. A container leg also appends ``safe.directory=*``: the
    workspace's owner need not be the container's user (Launch's root,
    Docker Desktop's mapping), and git 2.38+ honours it at command scope
    (P12). Nothing here sets ``GIT_CONFIG_GLOBAL`` or ``GIT_CONFIG_NOSYSTEM``,
    so an image's own /etc/gitconfig still applies. Forwarding twice is
    forwarding once."""
    source = os.environ
    entries = git_config_entries(source)
    if container:
        entries.append(("safe.directory", "*"))
    out = git_config_env(entries, env)
    out.update({name: source[name] for name in (*AUTHOR_ENV, *sorted(FORGE_ENV)) if name in source})
    return out


def git_prefix(remote: Remote) -> list[str]:
    """The options before every subcommand (the program comes from
    :func:`_execute`): the hardening, no redirects, TLS verified, exactly
    ``remote``'s transport, and the low-speed limits. Settings a repository
    keeps (line endings, path quoting, the default branch, rename
    detection) are its own."""
    return [
        *HARDENING_FLAGS,
        "-c", "http.followRedirects=false", "-c", "http.sslVerify=true",
        *protocol_flags(remote.protocols),
        *(["-c", "ssh.variant=ssh"] if remote.protocols == ("ssh",) else []),
        "-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=30",
    ]


# Checked first, in this order: the SSH and GitLab phrasings.
_FIRST: tuple[tuple[tuple[str, ...], FailureKind], ...] = (
    (("host key verification failed", "remote host identification has changed", "no matching host key type",
      "host key for"), "host_key"),
    (("load key", "unprotected private key file", "error in libcrypto"), "key"),
    (("permission denied (publickey", "http basic: access denied"), "auth"),
    (("connection refused", "no route to host", "connection closed by", "kex_exchange_identification",
      "connection timed out during banner exchange"), "network"),
    (("could not be found",), "not_found"),
    (("not allowed to push", "marked as read only"), "forbidden"),
)
_NETWORK = ("could not resolve host", "failed to connect", "connection timed out", "operation timed out",
            "operation too slow", "network is unreachable", "connection reset", "ssl", "tls", "certificate")
_AUTH = ("authentication failed", "invalid username or token", "invalid username or password",
         "password authentication is not supported", "returned error: 401", "could not read username",
         "terminal prompts disabled")
_NOT_FOUND = ("repository not found", "returned error: 404", "does not appear to be a git repository",
              "not found")


# git quotes the remote URL and names repositories and paths: their words
# (openssl, rustls, redirect-service) never say what failed.
_QUOTED = re.compile(r"'[^'\n]*'")
_NAMED = re.compile(r"\S*/\S*")


def classify(stderr: bytes) -> FailureKind:
    text = _NAMED.sub(" ", _QUOTED.sub(" ", stderr.decode("utf-8", "replace").lower()))
    for phrases, kind in _FIRST:
        if any(s in text for s in phrases):
            return kind
    if any(s in text for s in _NETWORK):
        return "network"
    if "redirect" in text:
        return "redirect"
    if ("error: 403" in text or "returned error: 403" in text or "denied to" in text
            or ("permission to" in text and "denied" in text)):
        return "forbidden"
    if any(s in text for s in _AUTH):
        return "auth"
    if any(s in text for s in _NOT_FOUND):
        return "not_found"
    return "other"


# What each failure kind says before git's own words (failure_line).
_FAILURE_LINES: dict[str, str] = {
    "missing_git": "git is not installed on this machine",
    "broken_git": "git on this machine does not run",
    "old_git": "git {version} on this machine is older than 2.31",
    "timeout": "git {command} took too long and was stopped",
    "network": "git {command} could not reach the remote",
    "redirect": "the remote answered git {command} with a redirect, which is not followed",
    "auth": "the remote did not accept the credential for git {command}, or asked for one",
    "forbidden": "the remote refused git {command}: the credential may not do this",
    "not_found": "the remote has no such repository, or the credential cannot see it",
    "host_key": "the SSH host key is not one the Git account's Known hosts trust",
    "key": "ssh could not use the Git account's SSH key",
    "missing_ssh": "this machine has no ssh for git {command}",
    "other": "git {command} failed (exit {code})",
}


def failure_line(failure: GitFailure) -> str:
    """The one line that says what a failed git command ran into, from its
    classification (:func:`classify`), never from git's text."""
    return _FAILURE_LINES[failure.kind].format(command=failure.command, code=failure.code,
                                               version=failure.version)


def scrub(text: str, forms: Sequence[str]) -> str:
    """``text`` with every non-empty form of a secret, longest first, replaced by ``[redacted]``."""
    for form in sorted((f for f in forms if f), key=len, reverse=True):
        text = text.replace(form, "[redacted]")
    return text


async def _read_capped(stream) -> bytes:
    """Read ``stream`` to its end, keeping at most its last 64 KiB, from the
    start of a line: git says what failed last, and a cut line could hold
    half a secret that no scrub would match."""
    kept = bytearray()
    cut = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        kept += chunk
        if len(kept) > _STDERR_CAP:
            del kept[:len(kept) - _STDERR_CAP]
            cut = True
    if cut:
        start = kept.find(b"\n")
        del kept[:start + 1 if start >= 0 else len(kept)]
    return bytes(kept)


async def _execute(args: Sequence[str], cwd: str, env: dict[str, str], timeout: float,
                   input: "bytes | None", command: str) -> tuple[int, bytes, bytes]:
    """Spawn git with ``args`` and return ``(code, stdout, stderr)``; stderr
    stays in memory for the caller to classify and drop. git is the absolute
    path found on ``env``'s PATH (absolute entries only), never a bare name
    the child would look up from ``cwd``."""
    program = shutil.which("git", path=env["PATH"])
    if program is None:
        raise GitFailure("missing_git", command, -1)
    try:
        proc = await _spawn(program, *args, cwd=cwd, env=env,
                            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise GitFailure("missing_git", command, -1) from None
    except OSError:
        raise GitFailure("broken_git", command, -1) from None

    async def feed() -> None:
        if input is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(input)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    async def communicate() -> tuple[int, bytes, bytes]:
        _fed, out, err = await asyncio.gather(feed(), proc.stdout.read(), _read_capped(proc.stderr))
        return await proc.wait(), out, err

    async def reap() -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()

    try:
        return await asyncio.wait_for(communicate(), timeout)
    except TimeoutError:
        await asyncio.shield(reap())
        raise GitFailure("timeout", command, -1) from None
    except asyncio.CancelledError:
        await asyncio.shield(reap())
        raise


async def git_version() -> str:
    """The version of the git on PATH (``"2.43.0"``). Raises
    :class:`GitFailure` ``missing_git``, ``broken_git`` (it does not run or
    answers no version) or ``old_git`` (older than :data:`MIN_GIT`)."""
    home = Path(tempfile.gettempdir())
    try:
        code, out, _err = await _execute(["--version"], str(home), git_environment(None, home),
                                         VERSION_TIMEOUT, None, "--version")
    except GitFailure as exc:
        if exc.kind == "missing_git":
            raise
        raise GitFailure("broken_git", "--version", exc.code) from None
    match = _VERSION.search(out.decode("utf-8", "replace"))
    if code != 0 or not match:
        raise GitFailure("broken_git", "--version", code)
    parts = (int(match[1]), int(match[2]), int(match[3] or 0))
    version = ".".join(str(p) for p in parts)
    if parts[:2] < MIN_GIT:
        raise GitFailure("old_git", "--version", 0, version=version)
    return version


class Git:
    """git commands against one remote, with one credential, HOME and
    identity. ``config`` adds ``key=value`` pairs after :func:`git_prefix`.
    With ``log``, git's words reach it scrubbed of ``secrets`` (and of the
    credential): a failure's :func:`failure_line` first, then git's stderr,
    and with ``echo`` its stdout. A command given a ``repository`` (its work
    tree) names that repository's ``.git`` and work tree, so git never
    discovers one."""

    def __init__(self, remote: Remote, auth: "HttpsAuth | SshAuth | None", home: Path,
                 identity: "Identity | None" = None, *, config: Sequence[str] = (),
                 log: "Callable[[str], None] | None" = None, secrets: Sequence[str] = ()) -> None:
        # Defence in depth behind GitAccount.auth_for: a credential is never
        # handed to an address its account does not cover.
        if remote.protocols != ("file",):
            if isinstance(auth, HttpsAuth) and not (
                    remote.protocols[0] in _HEADER_PROTOCOLS and remote.url.startswith(auth.scope)):
                raise ValueError("an HTTPS credential only serves addresses under its account's base")
            if isinstance(auth, SshAuth) and not (
                    remote.protocols == ("ssh",)
                    and any(remote.known_host in line.split(" ", 1)[0].split(",") for line in auth.known_hosts)):
                raise ValueError("an SSH key only serves ssh addresses whose host key is known")
        self.remote = remote
        self.auth = auth
        self.home = home
        self.identity = identity
        self.config = tuple(config)
        self.log = log
        self.secrets = (*secrets, *((auth.authorization,) if isinstance(auth, HttpsAuth) else ()))

    def _say(self, text: bytes) -> None:
        if self.log is None:
            return
        for line in scrub(text.decode("utf-8", "replace"), self.secrets).splitlines():
            self.log(line)

    async def run(self, args: Sequence[str], cwd: Path, *, network: bool = False,
                  ok: tuple[int, ...] = (0,), timeout: "float | None" = None,
                  input: "bytes | None" = None, no_lazy_fetch: bool = False, echo: bool = False,
                  repository: "Path | None" = None) -> Completed:
        args = list(args)
        if timeout is None:
            timeout = (LS_REMOTE_TIMEOUT if args[0] == "ls-remote"
                       else NETWORK_TIMEOUT if network else LOCAL_TIMEOUT)
        keys: "Path | None" = None
        command: "str | None" = None
        try:
            if isinstance(self.auth, SshAuth) and not no_lazy_fetch and self.remote.protocols == ("ssh",):
                ssh = shutil.which("ssh", path=search_path())
                if ssh is None:
                    raise GitFailure("missing_ssh", args[0], -1)
                keys = Path(tempfile.mkdtemp(prefix="ssh-", dir=self.home))
            try:
                if keys is not None:
                    write_private(keys / "key", self.auth.private_key)
                    write_private(keys / "known_hosts", "".join(f"{line}\n" for line in self.auth.known_hosts))
                    command = ssh_command(ssh, keys / "key", keys / "known_hosts")
                env = git_environment(self.auth, self.home, self.identity, ssh_command=command,
                                      no_lazy_fetch=no_lazy_fetch)
                prefix = [*git_prefix(self.remote), *(w for pair in self.config for w in ("-c", pair))]
                if repository is not None:
                    prefix += [f"--git-dir={repository / '.git'}", f"--work-tree={repository}"]
                code, stdout, stderr = await _execute([*prefix, *args], str(cwd), env, timeout, input, args[0])
            finally:
                if keys is not None:
                    shutil.rmtree(keys, ignore_errors=True)
        except GitFailure as exc:
            if self.log is not None:
                self.log(failure_line(exc))
            raise
        kind = None if code == 0 else classify(stderr)
        failure = GitFailure(kind or "other", args[0], code) if code not in ok else None
        if failure is not None and self.log is not None:
            self.log(failure_line(failure))
        self._say(stderr)
        if echo:
            self._say(stdout)
        if failure is not None:
            raise failure
        return Completed(code, stdout, kind)


def write_private(path: Path, text: str) -> None:
    """Create ``path`` (0600, never through a link) holding ``text``."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        data = text.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)
