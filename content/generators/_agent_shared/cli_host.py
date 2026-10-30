# SPDX-License-Identifier: MIT
"""The host side of a CLI leg: the unmodified vendor CLI on this machine.

The sibling of :mod:`cli_container`. A harness whose CLI surface runs on this
machine (``Terminal.locus`` resolves to ``host``: Claude Code, Antigravity on
the Google plan) runs the machine's own copy of its CLI
(:mod:`collimate.host_harness`, found at config time and carried as
``ctx["host_harness"]``) in a PTY the host opens on this machine
(``gen.serve_terminal(None, …)``), with:

* **a per-account home** — ``<cache>/collimate/host-homes/<harness>/<account>/``,
  owner-only from the cache root down, outside every workspace and never
  mounted anywhere, keyed by the harness and the Key manager entry the run's
  model role selects (its credential account). The user signs in once,
  inside the CLI's own TTY and through the vendor's own flow; the CLI keeps
  that sign-in in this home and every later run of every cell on the same
  entry finds it there. Collimate never starts, reads, copies or writes the
  sign-in. The per-cell parts (session pointers, transcripts, the session
  store a cell carries between runs) stay per cell, in the cell's harness
  home (:mod:`harness_homes`).
* **the cell's real workspace path** as its working directory, as a host ACP
  leg uses (``spawn_host_adapter``): no mount.
* **its own environment**, replacing the child's (:func:`leg_env`): PATH led
  by the node the host adapters run on, HOME, TERM, the locale, and what the
  leg adds. A provider key for an API-key terminal rides the environment or
  a private file the CLI's own settings name, never the argv; a plan whose
  CLI signs in here gets no credential from Collimate at all.

The host labels the PTY child with the run (``COLLIMATE_HOST_TERMINAL``) and
kills its process group when the run ends; its startup sweep kills a
labelled child a killed server left behind.
"""
from __future__ import annotations

import hashlib
import os
import re

from .harnesses import HARNESS

#: The host-cache folder that holds the per-account homes.
HOMES_DIR = "host-homes"

# An account id used as a directory name as it is; anything else is hashed.
_SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# The locale variables a TUI needs to draw (UTF-8 box drawing, wide glyphs).
_LOCALE = ("LANG", "LC_ALL", "LC_CTYPE")


def account_key(account: dict) -> str:
    """The directory name of ``account``'s home: its credential account's id
    (one sign-in per Key manager entry, shared by the model entries under
    it), hashed when it is not a plain name."""
    identity = str(account.get("credential_account_id") or account.get("id") or "")
    if not identity:
        raise ValueError("the selected model account has no id to key its CLI home by")
    if _SAFE_KEY.fullmatch(identity) and identity not in (".", ".."):
        return identity
    return "sha256-" + hashlib.sha256(identity.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def homes_root() -> str:
    from .harness_homes import _cache_root
    return os.path.join(_cache_root(), HOMES_DIR)


def account_home(harness: str, account: dict | None = None) -> str:
    """The per-account home of ``harness``'s CLI on this machine for
    ``account`` (the run's selected model account by default), created
    owner-only (0700) from the host cache root down."""
    if harness not in HARNESS:
        raise ValueError(f"unknown harness {harness!r}")
    if account is None:
        import gen
        account = gen.account("model")
    from .harness_homes import _cache_root, _private_cache_dir
    home = os.path.join(homes_root(), harness, account_key(account))
    _private_cache_dir(_cache_root(), home)
    return home


def cli_binary(harness: str, found: dict | None) -> str:
    """The absolute path of the machine's own CLI the leg runs (the copy
    placement found, ``ctx["host_harness"]``); a refusal naming the binary
    when there is none — the CLI surface of this harness runs on this
    machine only."""
    row = HARNESS[harness]
    path = str((found or {}).get("path") or "")
    if not path or not os.path.isabs(path):
        raise RuntimeError(
            f"{row.label}'s CLI surface runs this machine's {row.host_binary}, which was not found "
            f"on it: install {row.host_binary} on PATH outside Snap, then start the run again")
    return path


def leg_env(*, home: str | None = None, **extra: str | None) -> dict:
    """The leg's whole environment: PATH led by the node the host adapters
    run on, HOME (``home``, or the user's), an xterm TERM, the locale, and
    ``extra`` (a ``None`` value is left out). Nothing else of this process's
    environment reaches the CLI: no other account's key, no server
    variable."""
    from .host_adapters import host_env
    env = host_env()
    env["TERM"] = "xterm-256color"
    if home is not None:
        env["HOME"] = home
    for name in _LOCALE:
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update({name: value for name, value in extra.items() if value is not None})
    return env


def link_cell_dir(home: str, relative: str, target: str) -> str:
    """Point ``relative`` (a folder inside the per-account ``home``) at the
    cell's own ``target`` folder for one leg, so what the CLI files there —
    a session's record — lands in the cell's harness home, whose session
    store the cell keeps between runs. A link left by an earlier leg is
    replaced; a real folder in its place (the CLI's, from a run outside
    Collimate) is kept and the link is not made. Returns the link's path,
    ``""`` when it was not made."""
    os.makedirs(target, mode=0o700, exist_ok=True)
    link = os.path.join(home, relative)
    os.makedirs(os.path.dirname(link), mode=0o700, exist_ok=True)
    if os.path.islink(link):
        os.unlink(link)
    elif os.path.exists(link):
        return ""
    os.symlink(target, link)
    return link


def unlink_cell_dir(link: str, target: str) -> None:
    """Remove the leg's link when it still points at its ``target``."""
    if link and os.path.islink(link) and os.readlink(link) == target:
        os.unlink(link)


def sweep_dangling_links(directory: str) -> None:
    """Remove the links in ``directory`` whose target is gone: the cell
    homes of runs that ended without their leg's own cleanup."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        if os.path.islink(path) and not os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


__all__ = ["HOMES_DIR", "account_home", "account_key", "cli_binary", "homes_root", "leg_env",
           "link_cell_dir", "sweep_dangling_links", "unlink_cell_dir"]
