"""Host git: every git process the host itself runs, over a channel store, a
quarantine, a staging gitdir or a run's ``.git``
(``docs/plans/2026-10-31-git-in-cells/design.md`` §3.1).

Each child gets the hardened environment of :mod:`collimate.git_hosts` with
an empty temporary HOME, ``GIT_CEILING_DIRECTORIES`` at the realpath of
``.collimate/tmp`` and replace objects off; the ``-c`` hardening prefix and
the ``file``-only protocol pin; and an explicit ``--git-dir`` (with
``--work-tree`` for a work tree), never discovery. Calls are synchronous and
blocking, made for worker threads (async callers use ``asyncio.to_thread``).
:func:`capability` says once per process whether this machine's git is
usable; every module imports without git.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Literal

from collimate.git_hosts import HARDENING_FLAGS, MIN_GIT, hardened_env, protocol_flags

# The ``-c`` pairs of every store ``index-pack`` and repack. Neither is
# load-bearing at the 2.31 floor (``core.fsync`` is 2.36+, ignored before);
# the guarantee is the caller's fsync of ``objects/pack`` and ``objects``
# before the DB commit naming the new shas.
STORE_WRITE_FLAGS: tuple[str, ...] = ("-c", "transfer.unpackLimit=1", "-c", "core.fsync=committed")

# How much of a child's stderr a failure keeps (its tail, where git's
# ``fatal:`` line is).
STDERR_CAP = 64 * 1024

# The default limit of one git child, in seconds.
DEFAULT_TIMEOUT = 120.0

# How long the capability probe's ``git --version`` may take.
_PROBE_TIMEOUT = 10.0

_VERSION = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")


def tmp_root() -> str:
    """The realpath of the data dir's ``.collimate/tmp`` (made if missing):
    the parent of every temporary home and the ceiling of discovery."""
    root = os.path.realpath(os.path.join(".collimate", "tmp"))
    os.makedirs(root, exist_ok=True)
    return root


@dataclass(frozen=True)
class Capability:
    """Whether host git is usable: ``ok``; ``missing`` (no binary);
    ``broken`` (it does not run, or its version is unreadable); ``too_old``
    (below ``collimate.git_hosts.MIN_GIT``). Only ``ok`` runs git; the other
    three take the no-git branches alike."""

    state: Literal["ok", "missing", "broken", "too_old"]
    path: str | None = None
    version: tuple[int, ...] | None = None

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def describe(self) -> str:
        """What git on this machine is, for a refusal: "missing", "does not
        run", or "<version>, older than 2.31"."""
        if self.state == "ok":
            return ".".join(map(str, self.version or ()))
        if self.state == "too_old":
            floor = ".".join(map(str, MIN_GIT))
            return f"{'.'.join(map(str, self.version or ()))}, older than {floor}"
        return "missing" if self.state == "missing" else "does not run"


_capability: Capability | None = None
_capability_lock = threading.Lock()


def capability() -> Capability:
    """This machine's git, probed by the first caller (off the event loop:
    the probe spawns ``git --version``) and cached for the process. Never
    raises."""
    global _capability
    with _capability_lock:
        if _capability is None:
            _capability = _probe()
        return _capability


def reset_capability() -> None:
    """Forget the cached probe, so the next :func:`capability` probes again
    (tests change PATH)."""
    global _capability
    with _capability_lock:
        _capability = None


def _probe() -> Capability:
    path = shutil.which("git")
    if path is None:
        return Capability("missing")
    try:
        with tempfile.TemporaryDirectory(prefix="git-home-", dir=tmp_root()) as home:
            returncode, stdout = _spawn([path, "--version"], env=_environment(home), cwd=home, input=None,
                                        stderr=subprocess.DEVNULL, timeout=_PROBE_TIMEOUT)
    except FileNotFoundError:
        return Capability("missing")
    except (OSError, NotImplementedError, subprocess.SubprocessError):
        return Capability("broken", path)
    match = _VERSION.match(stdout.decode("utf-8", "replace").strip())
    if returncode != 0 or match is None:
        return Capability("broken", path)
    version = tuple(int(n) for n in match.groups() if n is not None)
    if version[:2] < MIN_GIT:
        return Capability("too_old", path, version)
    return Capability("ok", path, version)


def _spawn(argv: Sequence[str], *, env: Mapping[str, str], cwd: str, input: bytes | IO[bytes] | None, stderr,
           timeout: float, output: IO[bytes] | None = None) -> tuple[int, bytes]:
    """Run ``argv`` in a session of its own; its exit status and stdout
    (empty when ``output``, a file, takes it). ``input`` is bytes fed to
    stdin or a file stdin reads. Past ``timeout`` (or on any interruption)
    the whole process group is killed, git's own children with it (repack's
    ``pack-objects``, a helper), so nothing outlives the call to write after
    its caller's lock is gone; :class:`subprocess.TimeoutExpired` is then
    raised."""
    if input is None:
        stdin = subprocess.DEVNULL
    elif isinstance(input, bytes):
        stdin = subprocess.PIPE
    else:
        stdin = input
    with subprocess.Popen(argv, env=env, cwd=cwd, stdin=stdin, stdout=subprocess.PIPE if output is None else output,
                          stderr=stderr, start_new_session=True) as child:
        try:
            stdout, _ = child.communicate(input if isinstance(input, bytes) else None, timeout=timeout)
        except BaseException:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
    return child.returncode, stdout or b""


def _subcommand(command: Sequence[str]) -> str | None:
    """The git subcommand of ``command``: its first word that is neither an
    option nor the value of a ``-c``/``-C`` before it."""
    words = iter(command)
    for word in words:
        if word in ("-c", "-C"):
            next(words, None)
        elif not word.startswith("-"):
            return word
    return None


def _environment(home: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = hardened_env(os.environ, home=home, ceiling=tmp_root())
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    if extra:
        env.update(extra)
    return env


class GitFailed(Exception):
    """A host git child that did not succeed: ``returncode`` is its exit
    status, or None when it never ran to an exit (git unusable, spawn
    failure, timeout); ``stderr`` is the tail of what it printed, at most
    :data:`STDERR_CAP` bytes, decoded; ``subcommand`` is the git command
    it ran, as the message names it."""

    def __init__(self, command: Sequence[str], returncode: int | None, stderr: str, *, timed_out: bool = False):
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out
        self.subcommand = sub = _subcommand(self.command)
        name = f"git {sub}" if sub else "git"
        if timed_out:
            what = "timed out"
        elif returncode is None:
            what = "did not run"
        else:
            what = f"exited {returncode}"
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
        super().__init__(f"{name} {what}" + (f": {detail}" if detail else ""))


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: str


def run(
    args: Sequence[str],
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
    input: bytes | IO[bytes] | None = None,
    output: IO[bytes] | None = None,
    env: Mapping[str, str] | None = None,
    store_write: bool = False,
    ok_returncodes: Sequence[int] = (0,),
    timeout: float = DEFAULT_TIMEOUT,
) -> GitResult:
    """Run ``git <prefix> args`` and return its result, or raise
    :class:`GitFailed` when git is not usable, the child cannot start, it
    outlives ``timeout`` (it is killed, with every process it started) or
    exits outside ``ok_returncodes``.

    ``git_dir`` and ``work_tree`` become ``--git-dir``/``--work-tree``; with
    neither, git runs outside any repository. ``input`` is fed to stdin:
    bytes, or a file read from where it stands (otherwise stdin is empty).
    ``output``, a file, takes stdout instead of the result (a pack, a walk's
    listing). ``env`` adds variables after the hardening
    (``GIT_INDEX_FILE``, ``GIT_SHALLOW_FILE``). ``store_write`` adds
    :data:`STORE_WRITE_FLAGS`. The child runs in its temporary home, so a
    relative path in ``args`` resolves there: pass absolute paths."""
    command = list(args)
    cap = capability()
    if not cap.ok or cap.path is None:
        raise GitFailed(command, None, f"git on this machine is {cap.describe()}")
    argv = [cap.path, *HARDENING_FLAGS, *protocol_flags(["file"])]
    if git_dir is not None:
        argv.append(f"--git-dir={git_dir}")
    if work_tree is not None:
        argv.append(f"--work-tree={work_tree}")
    if store_write:
        argv += STORE_WRITE_FLAGS
    argv += command
    with tempfile.TemporaryDirectory(prefix="git-home-", dir=tmp_root()) as home, \
            tempfile.TemporaryFile(dir=home) as errors:
        try:
            returncode, stdout = _spawn(argv, env=_environment(home, env), cwd=home, input=input, stderr=errors,
                                        timeout=timeout, output=output)
        except subprocess.TimeoutExpired:
            raise GitFailed(command, None, _tail(errors), timed_out=True) from None
        except (OSError, NotImplementedError, subprocess.SubprocessError) as exc:
            raise GitFailed(command, None, str(exc)) from None
        stderr = _tail(errors)
    if returncode not in ok_returncodes:
        raise GitFailed(command, returncode, stderr)
    return GitResult(returncode, stdout, stderr)


def _tail(handle) -> str:
    """The last :data:`STDERR_CAP` bytes written to ``handle``, decoded."""
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(max(0, size - STDERR_CAP))
    return handle.read().decode("utf-8", "replace")
