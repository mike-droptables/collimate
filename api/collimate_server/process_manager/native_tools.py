"""What a generator run on this machine starts with: which uv, and which Python.

A server started with ``--server-env ROOT`` (the desktop package's, naming the
self-contained environment it runs from) starts every native run with
``ROOT/bin/uv`` and, when the entrypoint's PEP 723 ``requires-python`` admits
it, passes ``--python ROOT/bin/python3``, so uv builds the run's environment
on that interpreter and downloads no Python. When ``requires-python``
excludes it (or says something :func:`admits` cannot read), the run gets no
``--python`` and uv's own discovery chooses, which may download one: a
generator that needs another Python still runs. Either way the run log says
which (:func:`run_python`).

Without ``--server-env`` (a checkout, ``uvx collimate``) a native run starts
with the ``uv`` its PATH finds and uv chooses its Python, as before. A
containerized run always uses its image's own uv and Python
(``optional_deps.CONTAINER_UV``).
"""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerEnv:
    """The environment a server runs from, as ``--server-env`` named it."""

    root: str
    uv: str
    python: str
    #: ``python``'s version, ``"3.13.18"``.
    version: str


class Refused(ValueError):
    """A ``--server-env`` that is not a usable environment."""


_server_env: ServerEnv | None = None


def configure_server_env(root: str | None) -> ServerEnv | None:
    """Start every native run with the tools of the environment at ``root``
    (absolute), or, with None, with the PATH's uv and uv's choice of Python.
    Refuses an environment whose ``bin/uv`` or ``bin/python3`` is not an
    executable file, or whose interpreter does not answer its version."""
    global _server_env
    if root is None:
        _server_env = None
        return None
    tools = {name: os.path.join(root, "bin", name) for name in ("uv", "python3")}
    for name, path in tools.items():
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise Refused(f"it has no executable bin/{name}")
    try:
        said = subprocess.run(
            [tools["python3"], "-I", "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise Refused(f"its bin/python3 does not run: {error}") from None
    if not re.fullmatch(r"\d+\.\d+\.\d+", said):
        raise Refused(f"its bin/python3 says its version is {said!r}")
    _server_env = ServerEnv(root=root, uv=tools["uv"], python=tools["python3"], version=said)
    return _server_env


def server_env() -> ServerEnv | None:
    """The environment ``--server-env`` named, or None."""
    return _server_env


def native_uv() -> str:
    """The uv a native run starts with: the server environment's, else
    ``uv`` (found on the run's PATH)."""
    return _server_env.uv if _server_env else "uv"


# PEP 723's reference expression for an inline metadata block.
_BLOCK = re.compile(r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")


def requires_python(entrypoint: str) -> str | None:
    """The ``requires-python`` of ``entrypoint``'s PEP 723 ``script`` block,
    or None when it has none (or no block, or the file cannot be read: the
    run itself then says why)."""
    try:
        with open(entrypoint, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    blocks = [m for m in _BLOCK.finditer(text) if m.group("type") == "script"]
    if len(blocks) != 1:
        return None
    content = "".join(line[2:] if line.startswith("# ") else line[1:]
                      for line in blocks[0].group("content").splitlines(keepends=True))
    try:
        value = tomllib.loads(content).get("requires-python")
    except tomllib.TOMLDecodeError:
        return None
    return value if isinstance(value, str) else None


_CLAUSE = re.compile(r"\s*(~=|===|==|!=|<=|>=|<|>)\s*(\S+)\s*")
_RELEASE = re.compile(r"\d+(?:\.\d+)*")


def _release(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _padded(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def admits(specifier: str, version: str) -> bool | None:
    """Whether the PEP 440 ``specifier`` admits the final release ``version``
    (``"3.13.18"``): True, False, or None when a clause uses what this does
    not read (a pre-, post- or dev-release, a local version, ``===`` against
    anything but the exact string), in which case uv decides."""
    have = _release(version)
    for clause in specifier.split(","):
        if not clause.strip():
            return None
        match = _CLAUSE.fullmatch(clause)
        if match is None:
            return None
        op, raw = match.groups()
        if op == "===":
            if raw != version:
                return False
            continue
        wildcard = op in ("==", "!=") and raw.endswith(".*")
        number = raw[:-2] if wildcard else raw
        if not _RELEASE.fullmatch(number):
            return None
        want = _release(number)
        mine, theirs = _padded(have, want)
        if wildcard:
            matched = have[:len(want)] == want
            ok = matched if op == "==" else not matched
        elif op == "~=":
            if len(want) < 2:
                return None
            ok = have[:len(want) - 1] == want[:-1] and mine >= theirs
        else:
            ok = {"==": mine == theirs, "!=": mine != theirs, "<=": mine <= theirs,
                  ">=": mine >= theirs, "<": mine < theirs, ">": mine > theirs}[op]
        if not ok:
            return False
    return True


def run_python(entrypoint: str) -> tuple[str | None, str | None]:
    """The interpreter to pass a native run of ``entrypoint`` as ``--python``
    (None: uv chooses), and the run log's line saying which (None without
    ``--server-env``, where nothing is chosen for the run)."""
    env = _server_env
    if env is None:
        return None, None
    wanted = requires_python(entrypoint)
    verdict = True if wanted is None else admits(wanted, env.version)
    if verdict:
        return env.python, f"Python {env.version}, the server environment's ({env.python})"
    why = (f"excludes the server environment's Python {env.version}" if verdict is False
           else f"is more than this server checks against its Python {env.version}")
    return None, (f"requires-python {wanted!r} {why}: uv chooses this run's Python "
                  "(and downloads one if none on this machine fits)")


__all__ = ["Refused", "ServerEnv", "admits", "configure_server_env", "native_uv", "requires_python",
           "run_python", "server_env"]
