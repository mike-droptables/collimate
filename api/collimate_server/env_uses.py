"""Run environments: the stable generator trees, the uv environments their
runs build, when each was last used, their sizes, and their deletion.

A run of a generator version uses that version's read-only tree under
``.collimate/envs/`` (``run_staging.stage_stable_tree``). uv keys a script's
environment on the script's absolute path, so the stable tree makes one uv
environment per generator version, under uv's ``environments-v2``. This
module keeps ``app.db``'s ``env_uses`` rows (global migration 15): each
key's generator, tree, uv environment once known and first and last use;
and the in-memory count of live runs per key, which deletion never touches.

The uv environment is found after a native run ends with ``uv python find
--script <entry>``, which (uv 0.13.6, verified 2026-11-05) neither creates
nor syncs an environment: without one it answers some other interpreter,
which is not under ``environments-v2`` and is ignored.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import collimate_server.readonly_trees as readonly_trees
from collimate_server.database import get_global_db_connection
from collimate_server.run_staging import ENVS_DIR, StableTree, tree_lock

logger = logging.getLogger(__name__)

#: The name uv gives a script environment: ``<script stem>-<16 hex>``.
UV_ENV_NAME = re.compile(r"^[A-Za-z0-9._-]+-[0-9a-f]{16}$")
#: How long a deletion preview stays valid.
PREVIEW_SECONDS = 600
_UV_TIMEOUT_SECONDS = 30
PRUNE_TIMEOUT_SECONDS = 600

_live: Counter[str] = Counter()
_live_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Live keys and use records
# ---------------------------------------------------------------------------

def retain(key: str) -> None:
    """Count one more live run on ``key``: deletion skips it until
    :func:`release`."""
    with _live_lock:
        _live[key] += 1


def release(key: str) -> None:
    with _live_lock:
        if _live[key] <= 1:
            _live.pop(key, None)
        else:
            _live[key] -= 1


def live_keys() -> set[str]:
    with _live_lock:
        return set(_live)


def record_use(tree: StableTree, generator_id: str) -> None:
    """Upsert ``tree``'s row with ``last_used_at`` now (a run starting)."""
    stamp = now_iso()
    conn = get_global_db_connection()
    try:
        conn.execute(
            "INSERT INTO env_uses (key, generator_id, tree, uv_env, first_used_at, last_used_at) "
            "VALUES (?, ?, ?, NULL, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "generator_id=excluded.generator_id, tree=excluded.tree, last_used_at=excluded.last_used_at",
            (tree.key, generator_id, tree.root, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def rows() -> list[dict]:
    conn = get_global_db_connection()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT key, generator_id, tree, uv_env, first_used_at, last_used_at FROM env_uses "
            "ORDER BY last_used_at DESC, key")]
    finally:
        conn.close()


def _row(key: str) -> dict | None:
    conn = get_global_db_connection()
    try:
        row = conn.execute("SELECT * FROM env_uses WHERE key=?", (key,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The uv environment
# ---------------------------------------------------------------------------

def uv_query_env() -> dict[str, str]:
    """The server's variables a run inherits (the run environment's static
    allowlist): ``uv cache dir`` and ``uv cache prune`` run with these, so
    they see the cache the runs use."""
    from collimate_server.process_manager.manager import ProcessManager
    return {name: os.environ[name] for name in ProcessManager._ENV_ALLOWLIST if name in os.environ}


def uv_cache_dir(uv: str, env: dict[str, str]) -> str | None:
    """``uv cache dir`` under ``env``, resolved; None when uv does not say."""
    try:
        out = subprocess.run([uv, "cache", "dir"], env=env, capture_output=True, text=True,
                             timeout=_UV_TIMEOUT_SECONDS, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return os.path.realpath(out) if out else None


def environment_of(answer: str, cache: str) -> str | None:
    """The uv environment ``uv python find --script`` named, when its
    answer is ``<cache>/environments-v2/<stem>-<16hex>/bin/python…``;
    None for any other interpreter."""
    if not answer:
        return None
    path = Path(os.path.abspath(answer))
    if path.parent.name != "bin" or not path.name.startswith("python"):
        return None
    environment = path.parent.parent
    if not UV_ENV_NAME.match(environment.name):
        return None
    if os.path.realpath(environment.parent) != os.path.join(cache, "environments-v2"):
        return None
    return os.path.join(cache, "environments-v2", environment.name)


def resolve_uv_env(key: str, uv: str, entry: str, env: dict[str, str]) -> str | None:
    """Find and store the uv environment of ``key``'s tree after a native
    run of ``entry`` (the tree's entrypoint, absolute) ended, running
    ``uv`` with the run's ``env`` so its cache is the run's. A key whose
    environment is already known, or whose row is gone, is left alone."""
    row = _row(key)
    if row is None or row["uv_env"]:
        return row["uv_env"] if row else None
    cache = uv_cache_dir(uv, env)
    if cache is None:
        return None
    try:
        answer = subprocess.run(
            [uv, "python", "find", "--offline", "--no-python-downloads", "--script", entry],
            env=env, capture_output=True, text=True, timeout=_UV_TIMEOUT_SECONDS,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    environment = environment_of(answer, cache)
    if environment is None:
        return None
    conn = get_global_db_connection()
    try:
        conn.execute("UPDATE env_uses SET uv_env=? WHERE key=?", (environment, key))
        conn.commit()
    finally:
        conn.close()
    return environment


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------

@dataclass
class Size:
    #: Allocated bytes (``st_blocks * 512``), each inode once.
    bytes: int = 0
    #: What deleting it frees: directories, links and files with one link
    #: (uv hardlinks wheels' files from ``archive-v0``; those free nothing).
    reclaimable_bytes: int = 0


def measure(top: str | None, size: Size | None = None) -> Size:
    """Add the size of the tree at ``top`` (missing: nothing) to ``size``."""
    size = size or Size()
    if not top:
        return size
    seen: set[tuple[int, int]] = set()

    def count(found: os.stat_result) -> None:
        identity = (found.st_dev, found.st_ino)
        if identity in seen:
            return
        seen.add(identity)
        allocated = getattr(found, "st_blocks", 0) * 512
        size.bytes += allocated
        if stat.S_ISDIR(found.st_mode) or found.st_nlink == 1:
            size.reclaimable_bytes += allocated

    try:
        count(os.lstat(top))
    except OSError:
        return size
    for dirpath, dirnames, filenames, dir_fd in os.fwalk(top, follow_symlinks=False):
        for name in (*dirnames, *filenames):
            try:
                count(os.stat(name, dir_fd=dir_fd, follow_symlinks=False))
            except OSError:
                continue
    return size


def listing() -> list[dict]:
    """``GET /api/storage/envs``'s rows: every recorded key with its tree,
    uv environment, sizes, last use and whether a run uses it now."""
    live = live_keys()
    out = []
    for row in rows():
        size = measure(row["tree"])
        measure(row["uv_env"], size)
        out.append({
            "key": row["key"], "generator_id": row["generator_id"], "tree": row["tree"],
            **({"uv_env": row["uv_env"]} if row["uv_env"] else {}),
            "bytes": size.bytes, "reclaimable_bytes": size.reclaimable_bytes,
            "last_used_at": row["last_used_at"], "live": row["key"] in live,
        })
    return out


# ---------------------------------------------------------------------------
# Preview and delete
# ---------------------------------------------------------------------------

class Refused(ValueError):
    """A deletion request this module will not carry out."""


@dataclass
class Preview:
    mode: str
    days: int | None
    keys: list[str]
    expires: float


_previews: dict[str, Preview] = {}
_previews_lock = threading.Lock()


def _selected(entry: dict, mode: str, cutoff: datetime | None) -> bool:
    if entry["live"]:
        return False
    if mode == "all":
        return True
    assert cutoff is not None
    return datetime.fromisoformat(entry["last_used_at"]) < cutoff


def _cutoff(mode: str, days: int | None) -> datetime | None:
    if mode == "all":
        return None
    if mode != "unused" or days is None or days < 0:
        raise Refused("mode is 'all', or 'unused' with days >= 0")
    return datetime.now(timezone.utc) - timedelta(days=days)


def preview(mode: str, days: int | None = None) -> dict:
    """The keys a deletion of ``mode`` (``all``, or ``unused`` for
    ``days``) would remove now, live keys left out, under a
    ``preview_id`` valid for :data:`PREVIEW_SECONDS`."""
    cutoff = _cutoff(mode, days)
    chosen = [entry for entry in listing() if _selected(entry, mode, cutoff)]
    preview_id = secrets.token_hex(16)
    with _previews_lock:
        now = time.monotonic()
        for stale in [pid for pid, held in _previews.items() if held.expires <= now]:
            del _previews[stale]
        _previews[preview_id] = Preview(mode=mode, days=days, keys=[e["key"] for e in chosen],
                                        expires=now + PREVIEW_SECONDS)
    return {"preview_id": preview_id, "count": len(chosen),
            "bytes": sum(e["reclaimable_bytes"] for e in chosen), "exact": True, "envs": chosen}


def take_preview(preview_id: str) -> Preview:
    """The preview ``preview_id`` names, once (a delete consumes it)."""
    with _previews_lock:
        held = _previews.pop(preview_id, None)
    if held is None or held.expires <= time.monotonic():
        raise LookupError("The preview is unknown or has expired; preview again")
    return held


def checked_uv_env(uv_env: str, cache: str | None) -> str:
    """``uv_env`` when it is a direct child of ``<cache>/environments-v2``
    named like a uv script environment; raises :class:`Refused` otherwise
    (a cache uv did not name refuses every environment)."""
    if cache is None:
        raise Refused("uv did not name its cache directory")
    path = Path(uv_env)
    if not path.is_absolute() or not UV_ENV_NAME.match(path.name):
        raise Refused(f"{uv_env} is not named like a uv script environment")
    if os.path.realpath(path.parent) != os.path.join(cache, "environments-v2") or path.is_symlink():
        raise Refused(f"{uv_env} is not directly under {os.path.join(cache, 'environments-v2')}")
    return str(path)


def _remove_uv_env(uv_env: str) -> None:
    """Remove the uv environment ``uv_env`` holding its ``.lock``
    exclusively; raises ``BlockingIOError`` when uv holds it."""
    if not os.path.isdir(uv_env):
        return
    fd = os.open(os.path.join(uv_env, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shutil.rmtree(uv_env)
    finally:
        os.close(fd)


def _inside_envs(tree: str) -> bool:
    envs = os.path.realpath(ENVS_DIR)
    return (os.path.isabs(tree) and os.path.realpath(os.path.dirname(tree)) == envs
            and not os.path.basename(tree).startswith("."))


def delete_key(key: str, cache: str | None) -> int:
    """Remove ``key``'s uv environment, then its tree, then its row, under
    the key's lock; returns the bytes it freed (its reclaimable size).
    Raises :class:`Refused` for a live key or a path it will not touch,
    ``BlockingIOError`` when uv holds the environment."""
    with tree_lock(key):
        if key in live_keys():
            raise Refused("a run is using it")
        row = _row(key)
        if row is None:
            raise Refused("it is no longer recorded")
        uv_env = checked_uv_env(row["uv_env"], cache) if row["uv_env"] else None
        if not _inside_envs(row["tree"]):
            raise Refused(f"{row['tree']} is not a tree under {os.path.abspath(ENVS_DIR)}")
        size = measure(row["tree"])
        measure(uv_env, size)
        if uv_env is not None:
            _remove_uv_env(uv_env)
        readonly_trees.remove_tree(row["tree"], ignore_errors=True)
        if os.path.lexists(row["tree"]):
            raise OSError(f"{row['tree']} could not be removed")
        conn = get_global_db_connection()
        try:
            conn.execute("DELETE FROM env_uses WHERE key=?", (key,))
            conn.commit()
        finally:
            conn.close()
        return size.reclaimable_bytes


def delete(preview_id: str) -> dict:
    """Carry out the preview ``preview_id``: each of its keys still
    selected by its mode is deleted (:func:`delete_key`); the others, and
    any that fails, are listed with why."""
    from collimate_server.process_manager.native_tools import native_uv
    held = take_preview(preview_id)
    cutoff = _cutoff(held.mode, held.days)
    current = {entry["key"]: entry for entry in listing()}
    cache = uv_cache_dir(native_uv(), uv_query_env())
    removed: list[str] = []
    failed: list[dict] = []
    freed = 0
    for key in held.keys:
        entry = current.get(key)
        if entry is None:
            failed.append({"key": key, "reason": "it is no longer recorded"})
            continue
        if not _selected(entry, held.mode, cutoff):
            failed.append({"key": key, "reason": "a run used it since the preview"})
            continue
        try:
            freed += delete_key(key, cache)
        except Refused as refused:
            failed.append({"key": key, "reason": str(refused)})
        except BlockingIOError:
            failed.append({"key": key, "reason": "uv is using its environment"})
        except OSError as error:
            logger.warning("run environment %s: deletion failed: %s", key, error)
            failed.append({"key": key, "reason": str(error)})
        else:
            removed.append(key)
    return {"removed": removed, "failed": failed, "bytes_freed_estimate": freed}


def prune_uv_cache() -> dict:
    """``uv cache prune`` (uv's own, never ``--force``) on the cache the
    runs use: it removes dangling entries and every unused environment on
    this machine, which rebuild on demand."""
    from collimate_server.process_manager.native_tools import native_uv
    try:
        done = subprocess.run([native_uv(), "cache", "prune"], env=uv_query_env(), capture_output=True,
                              text=True, timeout=PRUNE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "output": str(error)}
    return {"ok": done.returncode == 0, "output": (done.stdout + done.stderr).strip()}


__all__ = ["PREVIEW_SECONDS", "Refused", "Size", "UV_ENV_NAME", "checked_uv_env", "delete", "delete_key",
           "environment_of", "listing", "live_keys", "measure", "preview", "prune_uv_cache", "record_use",
           "release", "resolve_uv_env", "retain", "rows", "take_preview", "uv_cache_dir", "uv_query_env"]
