# SPDX-License-Identifier: MIT
"""Run git for one repository with no ambient config, credential helpers,
hooks or prompts, exactly one transport, and the credential only where the
account allows it: for HTTPS an in-memory Basic header scoped to the
account's base, for SSH a private key written to a 0600 file in a 0700
temporary directory for the duration of one git command and deleted after,
and swept by the next Git run if the process was killed first (the single
place in the engine a secret touches a file; D4. The Agent's session key and
Launch's container files are the other exceptions, which docs/ai_tools/notes/
2026-10-29/project_git_trust.md records). Never logs, returns or stores
git's stderr. This module does not import ``gen``. ``resolve_remote`` is
the one place a record becomes an address; ``_spawn`` and ``transport`` are
the test seams."""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Sequence
from urllib.parse import urlsplit

from collimate.git_hosts import host_key_name

if TYPE_CHECKING:
    from _env_shared.git_account import GitAccount

NETWORK_TIMEOUT = 900
LS_REMOTE_TIMEOUT = 60
LOCAL_TIMEOUT = 120
VERSION_TIMEOUT = 15
MIN_GIT = (2, 31)
_STDERR_CAP = 64 * 1024
_spawn = asyncio.create_subprocess_exec          # observation seam (tests wrap it)

FailureKind = Literal["missing_git", "broken_git", "old_git", "timeout", "network", "redirect", "auth",
                      "forbidden", "not_found", "host_key", "key", "missing_ssh", "other"]

# Ambient variables a git child must never see: every GIT_* variable (traces,
# config injection, askpass, ssh command) plus the tool tokens, askpass
# helpers and SSH agent of the run.
_DROPPED = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST",
                      "GITLAB_TOKEN", "GITLAB_HOST", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "SSH_AUTH_SOCK"})
_DEFAULT_IDENTITY = ("Collimate", "collimate@localhost.invalid")
_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")
# The protocols an HttpsAuth header may travel on; only the git http-backend
# test widens it to its local http server.
_HEADER_PROTOCOLS: tuple[str, ...] = ("https",)
_SESSION = re.compile(r"collimate-git-([1-9][0-9]{0,9})-[A-Za-z0-9_]+")
_SCP = re.compile(r"([^@/:]+)@([^@/:]+):(.*)", re.DOTALL)


@dataclass(frozen=True)
class Remote:
    """The address git is handed and the one transport it may use for it:
    ``https://host[:port]/path``, ``ssh://user@host[:port]/path`` or scp
    ``user@host:path`` (protocol ``ssh``); ``file://`` only through
    :func:`transport` in tests. ``known_host`` is the name ssh looks the
    host up by (``host`` or ``[host]:port``), ``""`` for https."""
    url: str
    protocols: tuple[str, ...]                   # exactly one: the ONLY transport git may use for it
    known_host: str = ""


@dataclass(frozen=True)
class HttpsAuth:
    scope: str                                   # the account's base, ending with "/"
    authorization: str = field(repr=False)       # base64(user:secret)


@dataclass(frozen=True)
class SshAuth:
    private_key: str = field(repr=False)         # normalised OpenSSH text, one trailing LF
    known_hosts: tuple[str, ...] = ()            # known_hosts lines


GitAuth = "HttpsAuth | SshAuth | None"


def _ssh_parts(remote: str) -> "tuple[str, int | None, str] | None":
    """``(host, port, path)`` of an ``ssh://`` or scp address; None for any other."""
    if remote.lower().startswith("ssh://"):
        parts = urlsplit(remote)
        return (parts.hostname or "").lower(), parts.port, parts.path
    if "://" not in remote and (match := _SCP.fullmatch(remote)):
        return match[2].lower(), None, match[3]
    return None


def resolve_remote(remote: str, account: "GitAccount | None") -> Remote:
    """The production address of a checkout record. A forge account with an
    SSH key reaches its https record over ``ssh://git@<web host>/<project>.git``
    (the web base's path is not part of an SSH path); an ``ssh://`` or scp
    record goes to git byte for byte; anything else is https."""
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


def transport(remote: Remote, record: str) -> Remote:
    """The address git really talks to: ``remote`` itself. The test seam:
    ``fake_remote`` returns a ``file://`` Remote for ``record`` instead."""
    return remote


def sweep_stale_sessions() -> None:
    """Remove the temporary session folders (``collimate-git-<pid>-*``) of
    Git runs and Agent sessions that were killed before their ``finally``
    ran, with any SSH key they held: only real folders of this user whose
    process is gone. Never raises, logs nothing."""
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


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


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


def basic_authorization(username: str, secret: str) -> str:
    """The HTTP Basic credential ``base64(username:secret)``."""
    return base64.b64encode(f"{username}:{secret}".encode("utf-8")).decode("ascii")


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


def git_environment(auth: "HttpsAuth | SshAuth | None", home: Path, identity: "Identity | None" = None,
                    *, ssh_command: "str | None" = None, no_lazy_fetch: bool = False) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key not in _DROPPED}
    # The ceiling keeps git's repository discovery from climbing above
    # ``home``: a repository another user planted there (``/tmp/.git``) never
    # supplies config (git before 2.35.2 would read it).
    env.update(PATH=search_path(), HOME=str(home), XDG_CONFIG_HOME=str(home),
               GIT_CEILING_DIRECTORIES=str(home.parent), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_ADVICE="0",
               LC_ALL="C", LANGUAGE="")
    if isinstance(auth, HttpsAuth):
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.{auth.scope}.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {auth.authorization}")
    elif isinstance(auth, SshAuth) and ssh_command:
        env.update(GIT_SSH_COMMAND=ssh_command, GIT_SSH_VARIANT="ssh")
    name, email = (identity.name, identity.email) if identity else _DEFAULT_IDENTITY
    env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    if no_lazy_fetch:
        env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def git_prefix(remote: Remote) -> list[str]:
    """The options before every subcommand (the program comes from :func:`_execute`)."""
    return [
        "-c", "credential.helper=", "-c", "core.askPass=", "-c", f"core.hooksPath={os.devnull}",
        "-c", "core.fsmonitor=false", "-c", "core.fileMode=false", "-c", "core.autocrlf=false",
        "-c", "core.quotePath=false", "-c", "http.followRedirects=false", "-c", "http.sslVerify=true",
        "-c", "protocol.allow=never",
        *[x for p in remote.protocols for x in ("-c", f"protocol.{p}.allow=always")],
        *(["-c", "ssh.variant=ssh"] if remote.protocols == ("ssh",) else []),
        "-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=30", "-c", "gc.auto=0",
        "-c", "maintenance.auto=false", "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false",
        "-c", "advice.detachedHead=false", "-c", "commit.gpgSign=false", "-c", "init.defaultBranch=main",
        "-c", "merge.renames=true",
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


async def _read_capped(stream) -> bytes:
    """Read ``stream`` to its end, keeping at most the first 64 KiB."""
    kept = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(kept)
        if len(kept) < _STDERR_CAP:
            kept += chunk[:_STDERR_CAP - len(kept)]


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
    """git commands against one remote, with one credential, HOME and identity."""

    def __init__(self, remote: Remote, auth: "HttpsAuth | SshAuth | None", home: Path,
                 identity: "Identity | None" = None) -> None:
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

    async def run(self, args: Sequence[str], cwd: Path, *, network: bool = False,
                  ok: tuple[int, ...] = (0,), timeout: "float | None" = None,
                  input: "bytes | None" = None, no_lazy_fetch: bool = False) -> Completed:
        args = list(args)
        if timeout is None:
            timeout = (LS_REMOTE_TIMEOUT if args[0] == "ls-remote"
                       else NETWORK_TIMEOUT if network else LOCAL_TIMEOUT)
        keys: "Path | None" = None
        command: "str | None" = None
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
            code, stdout, stderr = await _execute([*git_prefix(self.remote), *args], str(cwd), env,
                                                  timeout, input, args[0])
        finally:
            if keys is not None:
                shutil.rmtree(keys, ignore_errors=True)
        kind = None if code == 0 else classify(stderr)
        del stderr
        if code not in ok:
            raise GitFailure(kind or "other", args[0], code)
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
