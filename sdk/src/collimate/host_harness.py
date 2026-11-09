"""Is a harness CLI already on THIS machine? Public harness metadata, like
:mod:`collimate.account_taxonomy`: a read-only probe shared by the app's Key
manager (Verify says "available for both standard and in docker"
or "not found on machine, available in docker runs only") and the agent
runtime (a ``standard`` run spawns exactly the binary found here). Collimate
never installs or updates the machine's copy: the probe looks, runs
``--version`` once, and reports.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from collimate.account_taxonomy import HARNESS_HOST_BINARY

_VERSION = re.compile(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?)")
# Where version managers and package managers put a CLI that PATH may not
# lead to when the app was launched from a desktop icon rather than a shell.
_KNOWN_ROOTS = (
    "~/.local/bin", "~/.npm-global/bin", "~/.volta/bin", "~/.cargo/bin",
    "~/.nvm/versions/node/*/bin", "~/.local/share/fnm/node-versions/*/installation/bin",
    "/snap/bin", "/opt/homebrew/bin", "/usr/local/bin",
)
_cache: dict = {}
_probe_results: ContextVar[dict | None] = ContextVar("host_harness_probe_results", default=None)


@contextmanager
def probe_scope():
    """Cache all probe results within one startup, including blocked copies.

    Nested configuration reads share the current scope. A later startup probes
    afresh, so a newly installed CLI or a fixed copy becomes visible.
    """
    if _probe_results.get() is not None:
        yield
        return
    token = _probe_results.set({})
    try:
        yield
    finally:
        _probe_results.reset(token)


@dataclass(frozen=True)
class HostHarness:
    harness: str
    binary: str
    path: str
    version: str          # "" when the binary did not answer --version
    is_snap: bool
    searched: tuple

    @property
    def usable(self) -> bool:
        return bool(self.version) and not self.is_snap

    @property
    def blocker(self) -> str:
        """Why this copy cannot serve a standard run, or ``""``."""
        if not self.version:
            return f"{self.path} did not answer --version as {self.binary}"
        if self.is_snap:
            return (f"{self.path} is a strictly confined Snap, which cannot reach a run's workspace outside "
                    "your visible home folders")
        return ""

    def describe(self) -> str:
        return f"{self.binary} {self.version} at {self.path}" if self.version else f"{self.path} (did not answer --version)"


def _is_snap(path: str, real: str) -> bool:
    """A Snap command, however it is reached: every one is a symlink to the
    snap launcher (``/usr/bin/snap``), whether through ``/snap/bin``, the
    ``/var/lib/snapd/snap/bin`` some distros put on PATH, or a link of the
    user's own; a path inside a snap's mount (``/snap/<name>/…``) is one too."""
    return (path.startswith(("/snap/bin/", "/var/lib/snapd/snap/bin/"))
            or os.path.basename(real) == "snap" or "/snap/" in real)


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _natural_key(path: str) -> list:
    """Digit runs compare as numbers, so ``v20.11.0`` sorts after ``v20.9.0``."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", path)]


def _candidates(env: dict, home: str) -> tuple:
    seen, dirs = set(), []
    for entry in (env.get("PATH") or "").split(os.pathsep):
        if entry and entry not in seen:
            seen.add(entry)
            dirs.append(entry)
    for root in _KNOWN_ROOTS:
        pattern = root.replace("~", home, 1) if root.startswith("~") else root
        for entry in sorted(glob.glob(pattern), key=_natural_key, reverse=True):
            if entry not in seen:
                seen.add(entry)
                dirs.append(entry)
    return tuple(dirs)


def _version_of(path: str, env: dict) -> str:
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10,
                                env={**env, "NO_COLOR": "1"}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = _VERSION.search((result.stdout or "") + "\n" + (result.stderr or ""))
    return match.group(1) if match else ""


def probe(harness: str, *, env: dict | None = None, home: str | None = None) -> HostHarness | None:
    """The machine's own copy of ``harness``'s CLI, or ``None``. PATH is
    searched first, then the usual install roots. The first USABLE copy is the
    answer; a blocked one (a confined Snap, or one that does not answer
    ``--version``) is passed over for a usable copy further on, and is the
    answer only when nothing usable exists — so Verify and a standard run can
    name what is wrong with it. A usable hit for the live environment is
    cached per process; a miss or a blocked copy never is, so an install made
    after the app started (the fix Verify and the run point to) is found on
    the next look."""
    binary = HARNESS_HOST_BINARY.get(harness)
    if not binary:
        return None
    live = env is None and home is None
    scoped = _probe_results.get()
    scope_key = (harness, None if env is None else tuple(sorted(env.items())), home)
    if scoped is not None and scope_key in scoped:
        return scoped[scope_key]
    if scoped is None and live and harness in _cache:
        return _cache[harness]
    env = dict(os.environ) if env is None else dict(env)
    home = os.path.expanduser("~") if home is None else home
    searched = _candidates(env, home)
    first: HostHarness | None = None
    seen_real: set = set()
    for entry in searched:
        path = os.path.join(entry, binary)
        if not _executable(path):
            continue
        real = os.path.realpath(path)
        if real in seen_real:        # one binary reached through two dirs
            continue
        seen_real.add(real)
        found = HostHarness(harness, binary, path, _version_of(path, env), _is_snap(path, real), searched)
        if found.usable:
            first = found
            break
        first = first or found
    if first is not None and first.usable and live:
        _cache[harness] = first
    if scoped is not None:
        scoped[scope_key] = first
    return first


def forget() -> None:
    """Drop the per-process cache (tests, or after the user installs a CLI)."""
    _cache.clear()
