# SPDX-License-Identifier: MIT
"""Collimate-owned host ACP adapter installation and Node discovery."""
from __future__ import annotations
import contextlib
import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from typing import Optional
import gen
from .harness_homes import _cache_root, _state_lock
from .logging import safe_log as _log

from .adapter_artifacts import PYTHON_ADAPTERS, host_adapter


def resolve_claude_adapter_version() -> str:
    return resolve_adapter_version("claude-agent-acp")


def adapter_binary(name: str, version: str) -> str:
    """The installed adapter binary path for ``version`` of the named host
    adapter in the Collimate-owned prefix (installed on first use, once per
    host+version)."""
    binary = host_adapter(name).executable
    prefix = os.path.join(_cache_root(), "acp-adapters", name, version)
    return os.path.join(prefix, "node_modules", ".bin", binary)


def claude_adapter_binary(version: str) -> str:
    return adapter_binary("claude-agent-acp", version)


def resolve_adapter_version(name: str) -> str:
    """The version of the named host adapter for this run, by the rule every
    image artifact follows: ``latest`` unless the runner's ``packages:`` pins
    it, resolved through the shared ``upstream`` machinery (gated on
    ``update_packages``, the last version this machine resolved standing in
    when it may not fetch), and the canonical fallback only for a source this
    machine never resolved."""
    from _env_shared.upstream import LATEST, resolve_requests
    artifact = host_adapter(name)
    artifacts = {name: {"source": f"npm:{artifact.package}", "arg": "ADAPTER_VERSION"}}
    requests = {name: gen.packages().get(name) or LATEST}
    resolved = resolve_requests(artifacts, requests,
                                allow_network=gen.grant().update_packages,
                                log=lambda m: _log(m))
    return resolved.get("ADAPTER_VERSION") or artifact.fallback_version


# ──────────────────────────────────────────────────────────────────────
# Host node discovery — PATH is not the whole story (the launcher trap).
# ──────────────────────────────────────────────────────────────────────

# Both halves of the host locus refuse to run on an older runtime (the adapter
# package itself and the claude CLI the bundled Agent SDK spawns under it), so
# this is the floor the whole backend is gated on.
_MIN_NODE_MAJOR = 22

# A version-looking path segment (``v22.22.1``, ``24.1.0``). Every version
# manager parks its builds under one, which is what lets a glob of installs be
# ordered newest-first WITHOUT running each candidate first.
_VERSION_SEG = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


@dataclass(frozen=True)
class HostNode:
    """A usable ``(node, npm)`` pair on this host.

    ``bin_dir`` is the directory a CHILD process's PATH must lead with for
    ``#!/usr/bin/env node`` to resolve to this node — deliberately ``""`` for
    the one PATH entry ``shutil.which`` already resolves to, because then there
    is nothing to prepend and an empty prefix keeps every child's PATH
    byte-identical to our own. A LATER PATH entry (a qualifying node shadowed
    by an older one) carries its real dir, exactly like a manager hit does."""

    node: str
    npm: str
    bin_dir: str
    version: str


# Resolved once per process: the ladder runs ``node --version`` on every
# candidate it reaches, and a chat run re-opens the adapter on each turn.
# A MISS is deliberately not cached: it is the one answer the user can change
# from outside a running process (`nvm install 24`, a package manager), and
# caching it would make the fix land only after a restart — the same
# "it works in my terminal, not in the app" shape this ladder exists to remove.
_HOST_NODE: Optional[HostNode] = None
# Host adapters whose ``--version`` preflight passed in this process, keyed
# ``(name, version, binary, node)``. Success-only, like ``_HOST_NODE``.
_PREFLIGHTED: set = set()


def _reset_host_node_cache() -> None:
    """Drop the per-process discovery cache. Tests need it; it is also the
    honest hook should a caller ever change PATH mid-process."""
    global _HOST_NODE
    _HOST_NODE = None
    _PREFLIGHTED.clear()


def _node_version(path: str) -> str:
    """``node --version`` for the binary at ``path`` (``""`` when it will not
    run). Module-level and deliberately the ONLY subprocess the ladder makes,
    so tests monkeypatch this one seam instead of planting real node builds."""
    try:
        res = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001 — an unrunnable candidate is simply a miss
        return ""
    return (res.stdout or "").strip()


def _node_exec_path(node: str) -> str:
    """``process.execPath`` for the node at ``node`` — the binary it IS, after
    a shim has picked one (``""`` when the probe will not run).

    Deliberately a second seam rather than a flag on :func:`_node_version`: it
    costs a whole node startup and is asked exactly once, of the winner only,
    because the answer matters only for the one dir we are about to prepend to
    a child's PATH. Tests monkeypatch this."""
    try:
        res = subprocess.run(
            [node, "-p", "process.execPath"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001 — a shim that will not answer is not fatal
        return ""
    return (res.stdout or "").strip()


def _node_major(version: str) -> int:
    """The major of a ``v24.1.0``-shaped string; ``0`` when it is not one — an
    unparseable version can never clear the floor, which is the safe answer for
    a candidate we know nothing about."""
    try:
        return int(version.lstrip("v").split(".")[0])
    except Exception:  # noqa: BLE001
        return 0


def _dir_version_key(path: str) -> tuple:
    """Sort key for a candidate bin dir: the DEEPEST version-looking segment of
    its path (``.../node-versions/v24.1.0/installation/bin`` → ``(24, 1, 0)``),
    so one manager's installs order newest-first. Dirs carrying no version
    segment all sort equal, leaving them in the order they were listed."""
    best = (0, 0, 0)
    for seg in path.split(os.sep):
        m = _VERSION_SEG.match(seg)
        if m:
            best = tuple(int(g or 0) for g in m.groups())
    return best


def _pattern(root: str, *tail: str) -> str:
    """A search pattern: a CALLER-derived ``root`` (a home dir, an ``$NVM_DIR``
    — text we do not control) glob-escaped, followed by the segments we
    deliberately star. Without the escape a home like ``/home/a[1]`` reads as a
    character class to :func:`glob.glob` and every family under it silently
    stops being found."""
    return os.path.join(glob.escape(root), *tail)


def _node_search_patterns(env, home: str) -> list:
    """Where a node >= 22 hides when PATH's own is the wrong one, in priority
    order.

    A desktop-launcher process inherits the SESSION's environment, not the
    login shell's, so the version manager the user actually lives in is
    invisible to :func:`shutil.which` — ``node`` is then whatever the distro
    shipped. These are the managers' install roots, read from each manager's
    own env var where it has one (so a relocated install is still found) and
    from ``$XDG_DATA_HOME`` where the manager honors it — a user who moves that
    root moves several managers at once, and the Linux XDG default alone leaves
    a mac (or a relocated root) with no ladder at all.

    Family order, and newest install first WITHIN a family: nvm, fnm, asdf,
    mise, volta, homebrew, ``/usr/local``, pnpm, then ``$N_PREFIX``. It is a
    preference between managers, NOT a ranking of versions — an earlier
    family's hit wins over a newer install in a later one, because the earlier
    families are the ones a user installs node into deliberately.

    Entries containing ``*`` are globs whose starred segment carries the
    version; every caller-derived part is escaped by :func:`_pattern`."""
    posix = os.name == "posix"
    xdg = env.get("XDG_DATA_HOME") or ""
    local_share = os.path.join(home, ".local", "share")
    # macOS has no XDG default: its per-user data lives here, and fnm's own
    # installer puts node-versions under it — without this root every fnm entry
    # below is a path that cannot exist on a mac.
    mac_share = os.path.join(home, "Library", "Application Support")

    nvm = env.get("NVM_DIR") or os.path.join(home, ".nvm")
    pats = [_pattern(nvm, "versions", "node", "*", "bin")]

    fnm_roots = [env.get("FNM_DIR") or ""]
    if xdg:
        fnm_roots.append(os.path.join(xdg, "fnm"))
    fnm_roots.append(os.path.join(local_share, "fnm"))
    if posix:
        fnm_roots.append(os.path.join(mac_share, "fnm"))
    pats += [_pattern(r, "node-versions", "*", "installation", "bin")
             for r in fnm_roots if r]

    if posix:
        # asdf ships no Windows build at all, so its root is POSIX-only.
        asdf = env.get("ASDF_DATA_DIR") or os.path.join(home, ".asdf")
        pats.append(_pattern(asdf, "installs", "nodejs", "*", "bin"))

    mise_roots = [env.get("MISE_DATA_DIR") or ""]
    if xdg:
        mise_roots.append(os.path.join(xdg, "mise"))
    mise_roots.append(os.path.join(local_share, "mise"))
    pats += [_pattern(r, "installs", "node", "*", "bin") for r in mise_roots if r]

    pats.append(_pattern(home, ".volta", "bin"))  # volta's shims, unversioned
    if posix:
        pats += [
            "/opt/homebrew/bin",                 # homebrew on apple silicon
            "/usr/local/bin",                    # intel homebrew + manual installs
        ]

    pnpm_roots = [os.path.join(xdg, "pnpm")] if xdg else []
    pnpm_roots.append(os.path.join(local_share, "pnpm"))
    pats += [_pattern(r, "nodejs", "*", "bin") for r in pnpm_roots]

    n_prefix = env.get("N_PREFIX")
    if n_prefix:
        # `n` installs into /usr/local by default — already covered above — so
        # it earns an entry only when the user has pointed it elsewhere.
        pats.append(_pattern(n_prefix, "bin"))
    # $XDG_DATA_HOME is usually ~/.local/share, so several roots above collide
    # by design; de-dupe so a family costs one stat, and so the "also searched"
    # line of the failure does not repeat itself.
    return list(dict.fromkeys(pats))


def _expand_pattern(pattern: str) -> list:
    """The existing dirs a search pattern names, newest-first for a glob.

    Everything goes through :func:`glob.glob`, even a pattern with no starred
    segment: the caller-derived part is glob-ESCAPED (see :func:`_pattern`), so
    an ``os.path.isdir`` on the raw text would be asking after
    ``/home/a[[]1]`` — a path that exists nowhere. glob is the only thing that
    reads that escape back out."""
    dirs = [d for d in glob.glob(pattern) if os.path.isdir(d)]
    dirs.sort(key=_dir_version_key, reverse=True)
    return dirs


def _executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _host_node_pair(node: str, npm: str, bin_dir: str) -> Optional[HostNode]:
    """Promote a candidate pair to a :class:`HostNode` only when BOTH halves
    exist and node clears the floor. npm matters as much as node: the install
    runs ``npm``, whose own shebang resolves node through PATH, so a dir with a
    shiny node and no npm beside it is not a usable install site."""
    if not (_executable(node) and _executable(npm)):
        return None
    version = _node_version(node)
    if _node_major(version) < _MIN_NODE_MAJOR:
        return None
    return HostNode(node=node, npm=npm, bin_dir=bin_dir, version=version)


def _resolve_shim(node: HostNode) -> HostNode:
    """Point ``bin_dir`` at the REAL install when the winner is a shim.

    volta's ``~/.volta/bin`` (and the asdf/mise shim dirs) hold launchers that
    choose a version from the CWD's project files, so leading a child's PATH
    with that dir pins nothing: the adapter runs in the agent's own workspace,
    where a stray ``package.json``/``.tool-versions`` — agent-writable — would
    get to pick the runtime for us, including one older than the floor we just
    checked. ``process.execPath`` is the shim's own answer to "which node did
    you exec", so its dirname is the build we preflighted. We move only when
    that dir also holds an executable npm, because ``bin_dir`` leads the PATH
    for the install too and an npm-less dir would strand it; any failure keeps
    the shim dir, which is still better than nothing."""
    real = _node_exec_path(node.node)
    if not real:
        return node
    real_dir = os.path.dirname(real)
    if not real_dir or real_dir == os.path.dirname(node.node):
        return node
    if not _executable(os.path.join(real_dir, "npm")):
        return node
    return replace(node, bin_dir=real_dir)


def find_host_node(env=None, home: Optional[str] = None) -> Optional[HostNode]:
    """The node >= 22 (plus npm) this host can run the claude adapter on, or
    ``None`` when there is none.

    PATH wins whenever it qualifies — that is the user's own declared runtime
    and the only one a terminal-started run ever needed — and EVERY entry on it
    is tried in order, not just the first ``node``: a machine that keeps the
    distro's v20 ahead of a v24 on the same PATH has a perfectly usable runtime
    that :func:`shutil.which` alone would never surface, and rejecting it sends
    the user hunting for a version manager they already installed past. The
    manager ladder runs only when no PATH entry qualifies, for the launcher
    case described in :func:`_node_search_patterns`: families are tried in the
    listed order, and inside a family the newest install wins.

    Cached per process on SUCCESS, and ONLY for the live environment: an
    explicit ``env``/``home`` is a probe (tests, diagnostics) that neither
    reads nor writes the cache, so a probe can never poison the real answer."""
    global _HOST_NODE
    live = env is None and home is None
    if live and _HOST_NODE is not None:
        return _HOST_NODE
    env = os.environ if env is None else env
    home = os.path.expanduser("~") if home is None else home

    found = None
    path_str = env.get("PATH") or ""
    # The entry a child reaches with no prefix at all — the ONE candidate whose
    # bin_dir stays "", since prepending a dir PATH already leads to is noise.
    which_node = shutil.which("node", path=path_str)
    for entry in path_str.split(os.pathsep):
        node = os.path.join(entry, "node") if entry else ""
        if not _executable(node):
            # Cheap first: most PATH entries hold no node at all, and the npm
            # fallback below re-scans the WHOLE of PATH — paying for that once
            # per empty dir turns a long PATH into a stat storm.
            continue
        # npm normally sits beside node, but a distro that splits them (or a
        # shim dir that only fronts node) still counts — fall back to PATH.
        npm = os.path.join(entry, "npm")
        if not _executable(npm):
            npm = shutil.which("npm", path=path_str) or ""
        found = _host_node_pair(node, npm, "" if node == which_node else entry)
        if found is not None:
            break
    if found is None:
        for pattern in _node_search_patterns(env, home):
            for d in _expand_pattern(pattern):
                found = _host_node_pair(
                    os.path.join(d, "node"), os.path.join(d, "npm"), d)
                if found is not None:
                    break
            if found is not None:
                break
    if found is not None:
        found = _resolve_shim(found)
        if live:
            _HOST_NODE = found
    return found


def host_node_bin_dir() -> str:
    """The dir a child's PATH must lead with to reach the node
    :func:`find_host_node` picked — ``""`` when PATH already leads there.

    Every hop in the host locus resolves the runtime through PATH: the adapter
    binary is a ``#!/usr/bin/env node`` script, and the Agent SDK inside it
    spawns node AGAIN for the claude CLI. Prepending this to a spawn env is
    what keeps that whole tree on the node we preflighted, instead of letting
    the grandchild fall back to the too-old one PATH still names."""
    node = find_host_node()
    return node.bin_dir if node is not None else ""


def _host_node_env(node: HostNode) -> dict:
    """Our own environment with PATH led by the chosen node, for the install
    and preflight subprocesses. Naming ``node.npm`` as the argv[0] is not
    enough on its own — npm and the adapter are both env-shebang scripts, so
    they would still be interpreted by whatever node PATH resolves first."""
    env = dict(os.environ)
    if node.bin_dir:
        # Join only the parts that exist: with an empty inherited PATH a plain
        # concatenation leaves a TRAILING EMPTY element, which POSIX resolves
        # as the current directory — here the agent-writable workspace, i.e. a
        # place a prompt-injected agent could drop its own `node`.
        env["PATH"] = os.pathsep.join(
            [p for p in (node.bin_dir, env.get("PATH") or "") if p])
    return env


def _no_host_node_error() -> RuntimeError:
    """The named failure for the enumerated fallback (§1), and the one place
    the launcher-PATH trap is explained — it is overwhelmingly the cause when
    the very same machine runs this adapter fine from a terminal."""
    env, home = os.environ, os.path.expanduser("~")
    path_node = shutil.which("node", path=env.get("PATH") or "")
    if path_node:
        version = _node_version(path_node) or "an unreadable version"
        on_path = f"{path_node} {version}"
    else:
        on_path = "none"
    # _node_search_patterns already drops the roots that cannot exist on this
    # platform, so the list names only places worth looking.
    searched = ", ".join(_node_search_patterns(env, home))
    msg = (
        f"backend 'claude-code' needs node + npm on the host for its ACP "
        f"adapter, with node >= {_MIN_NODE_MAJOR}. PATH's node: {on_path}. "
        f"Also searched: {searched}."
    )
    if os.name == "posix":
        # The desktop-session/login-shell split is a POSIX story (and the
        # launcher-env resolution that fixes it is POSIX-only too), so on
        # Windows this paragraph would send the reader after a shell profile
        # that has nothing to do with their PATH.
        msg += (
            f" If Collimate was started from a desktop launcher or a service, "
            f"its PATH is the desktop session's, not your terminal's — the "
            f"desktop app resolves your login shell's environment at startup, "
            f"so relaunching it picks up the node your shell can see; a "
            f"headless server needs node >= {_MIN_NODE_MAJOR} on PATH."
        )
    return RuntimeError(msg)


def _install_adapter(node: HostNode, package: str, version: str, prefix: str,
                     binary: str, env: dict) -> None:
    """``npm install`` ``package@version`` into ``prefix``; raises a named
    error when npm fails or leaves no ``binary`` behind."""
    _log(f"installing {package}@{version} (host adapter, node {node.version})…")
    res = subprocess.run(
        [node.npm, "install", "--prefix", prefix, "--no-fund", "--no-audit",
         f"{package}@{version}"],
        capture_output=True, text=True, timeout=300, env=env,
    )
    if res.returncode != 0 or not os.path.exists(binary):
        raise RuntimeError(
            f"installing {package}@{version} failed: "
            f"{(res.stderr or res.stdout or '').strip()[:500]}"
        )


def _preflight_adapter(binary: str, version: str, env: dict) -> str:
    """Why the adapter at ``binary`` does not answer ``--version`` with the
    pin, or ``""`` when it does."""
    try:
        got = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30,
            env=env,
        ).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"its --version preflight failed: {e}"
    if got != version and not got.endswith(version):
        return f"its --version preflight returned {got!r}, expected {version!r}"
    return ""


def _wipe_adapter_install(prefix: str) -> None:
    """Remove what an npm install left in ``prefix`` (its lock file stays)."""
    for entry in ("node_modules", "package.json", "package-lock.json"):
        path = os.path.join(prefix, entry)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)


def ensure_adapter(name: str, version: str) -> str:
    """Install (once) and preflight a host-side ACP adapter from
    the canonical host-eligible artifact rows into Collimate's own prefix; returns the binary
    path. Fails the run with a NAMED error when no host node >= 22 can be
    found, the install fails, or the ``--version`` preflight disagrees — the
    enumerated fallback (§1) is the runner offering a different backend, never
    a silent degrade. The harness CLI an adapter drives is never touched.

    Several runs can reach a new version's first use at once, and two npm
    installs into one prefix break each other, so the prefix's ``flock``
    orders them: the run-start preflight holds it shared (concurrent run
    starts preflight side by side, never while a run installs), and an
    install holds it exclusive. Every install is a fresh one, removing what
    an earlier install left first, so a killed or failed install heals on
    the next run: a missing binary, or one that fails its preflight again
    under the exclusive lock (a transient failure, such as a slow node start
    past the timeout, gets that second try), is installed once more before
    the run is failed. The cost of healing: the reinstall removes
    node_modules under any adapter another run started from this prefix,
    which then cannot start a new session until the install finishes."""
    artifact = host_adapter(name)
    node = find_host_node()
    if node is None:
        raise _no_host_node_error()
    binary = adapter_binary(name, version)
    prefix = os.path.dirname(os.path.dirname(os.path.dirname(binary)))
    # Both subprocesses below run under the chosen node, not PATH's own.
    env = _host_node_env(node)
    key = (name, version, binary, node.node)
    if key in _PREFLIGHTED and os.path.exists(binary):
        return binary   # already preflighted in this process
    # The lock makes the prefix owner-only from the cache root down, like the
    # session state beside it: the adapter runs as this user with the run's
    # credentials in its env.
    root = _cache_root()
    with _state_lock(root, prefix, shared=True):
        # The run-start preflight: the binary answers --version with the pin.
        if (os.path.exists(binary)
                and not _preflight_adapter(binary, version, env)):
            _PREFLIGHTED.add(key)
            return binary
    with _state_lock(root, prefix):
        # Checked again: another run may have installed or healed the prefix
        # while this one waited for the lock.
        if os.path.exists(binary):
            problem = _preflight_adapter(binary, version, env)
            if not problem:
                _PREFLIGHTED.add(key)
                return binary
            _log(f"the {name} adapter at {prefix} is stale or corrupt "
                 f"({problem}); reinstalling it")
        _wipe_adapter_install(prefix)
        _install_adapter(node, artifact.package, version, prefix, binary, env)
        problem = _preflight_adapter(binary, version, env)
        if problem:
            raise RuntimeError(
                f"the {name} adapter at {binary} failed after a fresh install "
                f"into {prefix}: {problem}")
        _PREFLIGHTED.add(key)
    return binary


def ensure_claude_adapter(version: str) -> str:
    return ensure_adapter("claude-agent-acp", version)


def python_adapter_interpreter(name: str) -> str:
    """The interpreter of the named Python host adapter's own environment."""
    artifact = PYTHON_ADAPTERS[name]
    return os.path.join(_cache_root(), "acp-adapters", name, artifact.version, "venv", "bin", "python")


def _preflight_python_adapter(python: str, artifact) -> str:
    """Why the environment at ``python`` does not hold the pinned adapter, or ``""``."""
    try:
        got = subprocess.run(
            [python, "-c", f"import importlib.metadata as m; print(m.version({artifact.module!r}))"],
            capture_output=True, text=True, timeout=60, env={"PATH": os.environ.get("PATH", "")},
        ).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"its version check failed: {e}"
    return "" if got == artifact.version else f"it holds {artifact.module} {got or 'nothing'}, expected {artifact.version}"


def _install_python_adapter(artifact, venv: str) -> None:
    """A fresh venv at ``venv`` with the adapter's pinned requirements: through
    uv where this machine has it (the generator itself runs on uv), else the
    standard library's venv and pip. The interpreter is the generator's own
    Python version."""
    import sys
    if os.path.isdir(venv) and not os.path.islink(venv):
        shutil.rmtree(venv)
    uv = shutil.which("uv")
    _log(f"installing {', '.join(artifact.requirements)} (host adapter environment)…")
    if uv:
        steps = [[uv, "venv", "--quiet", "--python", sys.executable, venv],
                 [uv, "pip", "install", "--quiet", "--python", os.path.join(venv, "bin", "python"),
                  *artifact.requirements]]
    else:
        steps = [[sys.executable, "-m", "venv", venv],
                 [os.path.join(venv, "bin", "python"), "-m", "pip", "install", "--quiet",
                  "--disable-pip-version-check", *artifact.requirements]]
    for step in steps:
        res = subprocess.run(step, capture_output=True, text=True, timeout=600)
        if res.returncode != 0:
            raise RuntimeError(f"installing the {artifact.module} adapter failed: "
                               f"{(res.stderr or res.stdout or '').strip()[:500]}")


def ensure_python_adapter(name: str) -> str:
    """Install (once) and preflight the named Python host adapter's own
    environment under Collimate's cache; returns its interpreter. The same
    shared-preflight / exclusive-install locking as :func:`ensure_adapter`,
    and the same healing: a missing or wrong environment is rebuilt once
    before the run fails with a named error."""
    artifact = PYTHON_ADAPTERS[name]
    python = python_adapter_interpreter(name)
    prefix = os.path.dirname(os.path.dirname(os.path.dirname(python)))
    key = (name, artifact.version, python)
    if key in _PREFLIGHTED and os.path.exists(python):
        return python
    root = _cache_root()
    with _state_lock(root, prefix, shared=True):
        if os.path.exists(python) and not _preflight_python_adapter(python, artifact):
            _PREFLIGHTED.add(key)
            return python
    with _state_lock(root, prefix):
        if os.path.exists(python) and not _preflight_python_adapter(python, artifact):
            _PREFLIGHTED.add(key)
            return python
        _install_python_adapter(artifact, os.path.join(prefix, "venv"))
        problem = _preflight_python_adapter(python, artifact)
        if problem:
            raise RuntimeError(f"the {name} adapter environment at {prefix} failed after a fresh "
                               f"install: {problem}")
        _PREFLIGHTED.add(key)
    return python


def host_env(**extra: str) -> dict:
    """The REPLACING child environment for a host twin: PATH (led by the
    node the host adapters run on), the user's HOME (module resolution and
    the harness's own runtime key off it), a dumb TERM, and nothing else the
    caller did not put there."""
    node_bin = host_node_bin_dir()
    env = {
        # Joined from the parts that exist: concatenating around a separator
        # would leave a trailing empty element when our own PATH is empty, and
        # POSIX reads an empty element as the CURRENT DIRECTORY — which for
        # this child is the agent's writable workspace.
        "PATH": os.pathsep.join([p for p in (node_bin, os.environ.get("PATH", "")) if p]),
        # HOME stays the user's: node, npm and the adapter's own module
        # resolution key off it.
        "HOME": os.environ.get("HOME", ""),
        "TERM": "dumb",
    }
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def write_host_seam(path: str, text: str) -> bool:
    """The on-disk twin of ``cli_container.write_context_seam``: the session
    context as a harness-owned instruction file, rewritten on every open and
    removed when the run carries no system prompt."""
    body = (text or "").strip()
    if not body:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    return True


def link_login(target: str, link: str) -> None:
    """Point the harness's store path at the account's lease file — one
    inode, so the in-place rewrites the harness makes as tokens rotate land
    in the lease the runtime commits back to the keyring."""
    os.makedirs(os.path.dirname(link), exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        os.remove(link)
    os.symlink(target, link)
