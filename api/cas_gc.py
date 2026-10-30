"""Fail-closed CAS mark-and-sweep with a recent-publication floor.

Mark current and persisted layouts plus every normal and parked channel DB.
An unreadable source prevents both sweep and trash purge. A store or dedupe
hit refreshes its mtime; objects younger than a day are never swept, and the
age-check/move shares the CAS publication lock. Unreferenced old objects move
to trash, then expire after the separate trash grace period. A daily lifespan
task and channel deletion request the same collector; maintenance can run it
explicitly. Readers never depend on recovery from trash.
"""

from __future__ import annotations

import logging
import json
import os
import shutil
import sqlite3
import time
import threading
import uuid
from typing import Iterable, Iterator

from cas import OBJECTS_DIR as CAS_DIR
from database import get_db_connection

logger = logging.getLogger(__name__)


TRASH_DIR = os.path.join(".collimate", "objects.trash")
DEFAULT_GRACE_SECONDS = 24 * 3600
MIN_BLOB_AGE_SECONDS = 24 * 3600
_collection_lock = threading.Lock()
_background_collection = None


class IncompleteMark(RuntimeError):
    """No object can be reclaimed until every required source was read."""


def _channel_dbs() -> list[str]:
    """Return channel database basenames found by the physical maintenance scan.

    Iterates files instead of in-memory state so the GC works even if
    state isn't loaded (e.g. during a lifespan-init failure recovery)."""
    channels_dir = os.path.join(".collimate", "channels")
    if not os.path.isdir(channels_dir):
        return []
    out: list[str] = []
    for fname in os.listdir(channels_dir):
        if fname.endswith(".db"):
            out.append(fname[:-3])
    return sorted(out)


def _orphaned_channel_db_paths() -> list[str]:
    """Full paths of the orphan channel DBs parked under
    ``channels/orphaned/`` (database.set_aside_orphan_channel_db), which
    the user can still recover, so their blobs must stay live."""
    orphaned_dir = os.path.join(".collimate", "channels", "orphaned")
    if not os.path.isdir(orphaned_dir):
        return []
    return sorted(
        os.path.join(orphaned_dir, fname)
        for fname in os.listdir(orphaned_dir)
        if fname.endswith(".db")
    )


def _live_blobs_in_channel(conn: sqlite3.Connection) -> set[str]:
    """Live blob hashes referenced by the cells in this channel. Every
    version row is live: a removed cell keeps its history for the Removed
    shelf.

    Includes every retained ``file_revisions.body_hash``. Accepted current
    files and conflict candidates already reference immutable bodies before
    a history checkpoint is created, so version rows alone are insufficient.

    And any hash referenced by ``run_artifacts.blob_hash`` — per-run
    Config/Logs artifacts live only there (never in cell_files), so
    omitting them would trash every run's config and log on the first
    sweep.

    Each query runs on its own, so a parked orphan DB that predates one of
    these tables still contributes the others. Any other error (a lock
    held past the busy timeout) raises: an empty answer would read as "this
    DB references nothing".
    """
    from versions import iter_version_files
    try:
        live: set[str] = {row['blob_hash'] for row in iter_version_files(conn)}
    except sqlite3.OperationalError as exc:
        # An artifact-only source has no cells to reference. Missing snapshot
        # metadata in a source that DOES have cells is corruption, not empty.
        if not str(exc).startswith('no such table:') or conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cells'"
        ).fetchone():
            raise
        live = set()
    for sql in (
        "SELECT DISTINCT body_hash FROM file_revisions WHERE body_hash IS NOT NULL",
        "SELECT DISTINCT blob_hash FROM run_artifacts WHERE blob_hash IS NOT NULL",
    ):
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.OperationalError as exc:
            if str(exc).startswith(("no such table", "no such column")):
                continue
            raise
        from cas import is_cas_hash
        values = {r[0] for r in rows}
        if not all(is_cas_hash(value) for value in values):
            raise IncompleteMark("invalid stored content reference")
        live |= values
    from cas import is_cas_hash
    if not all(is_cas_hash(value) for value in live):
        raise IncompleteMark("invalid stored version content reference")
    return live


def iter_channel_blob_refs(*, skip_channel_id: str | None = None) -> Iterator[tuple[str, set[str] | None]]:
    """``(db, hashes)`` for every channel DB on disk, whoever owns it: each
    ``channels/<UUID>.db`` but ``skip_channel_id`` (a registered channel, a
    creation in flight, a channel of a saved app state kept aside
    unrestored — main.restore_app_state), then each parked
    ``channels/orphaned/*.db``. ``hashes`` is what
    :func:`_live_blobs_in_channel` finds there, or None when the DB cannot
    be read (logged): what that means for its blobs is the caller's call."""
    from database import channel_files_lock
    with channel_files_lock:
        channels = _channel_dbs()
        parked = _orphaned_channel_db_paths()
    for channel_id in channels:
        if channel_id == skip_channel_id:
            continue
        refs: set[str] | None
        try:
            conn = get_db_connection(channel_id, create=False)
            try:
                from persistence import prune_file_revisions
                conn.execute("BEGIN IMMEDIATE")
                prune_file_revisions(conn)
                refs = _live_blobs_in_channel(conn)
                conn.commit()
            finally:
                conn.close()
        except (sqlite3.DatabaseError, OSError, ValueError) as exc:
            logger.warning("blob references: cannot read channel DB %s: %s", channel_id, exc)
            refs = None
        yield channel_id, refs
    for path in parked:
        # A plain connection: get_db_connection requires UUIDs and runs
        # schema init/migrations, which must not touch a parked file.
        try:
            from pathlib import Path
            conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
            try:
                # Parked databases remain untouched. Apply numbered migrations
                # to a disposable copy before interpreting snapshot metadata.
                from migrations import upgrade_channel_db
                snapshot = sqlite3.connect(':memory:')
                try:
                    conn.backup(snapshot)
                    snapshot.row_factory = sqlite3.Row
                    upgrade_channel_db(snapshot)
                    refs = _live_blobs_in_channel(snapshot)
                finally:
                    snapshot.close()
            finally:
                conn.close()
        except (sqlite3.DatabaseError, OSError, ValueError) as exc:
            logger.warning("blob references: cannot read parked channel DB %s: %s", path, exc)
            refs = None
        yield path, refs


def collect_live_blobs() -> set[str]:
    """All layout and channel references, or an incomplete-mark refusal."""
    from database import creating_channels, changing_channels
    if creating_channels or changing_channels:
        raise IncompleteMark("channel creation or file move is in flight")
    from state import state, state_lock
    from genesis import snapshot_app_state
    with state_lock:
        live = _layout_hashes(snapshot_app_state(state))
    path = os.path.join(".collimate", "app.db")
    if os.path.exists(path):
        from pathlib import Path
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        try:
            for (digest,) in conn.execute("SELECT blob_hash FROM renderer_bundles UNION SELECT blob_hash FROM handler_derived_files "
                                        "UNION SELECT blob_hash FROM esm_modules"):
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)):
                    raise IncompleteMark("invalid compiled, accepted-derived or mirrored reference")
                live.add(digest)
            for (raw,) in conn.execute("SELECT value FROM ui_state WHERE key='app_state' OR key GLOB 'app_state.invalid-*'"):
                live |= _layout_hashes(json.loads(raw))
        finally:
            conn.close()
    for db, refs in iter_channel_blob_refs():
        if refs is None:
            raise IncompleteMark(f"unreadable channel database: {db}")
        live |= refs
    from cas import pinned_hashes
    live |= pinned_hashes()
    return live


def _layout_hashes(layout: dict) -> set[str]:
    if not isinstance(layout, dict) or not isinstance(layout.get("channels"), list):
        raise IncompleteMark("unreadable saved layout")
    found = set()
    for channel in layout["channels"]:
        for kind in ("main_stacks", "nav_stacks"):
            for stack in channel.get(kind, []):
                for cell in stack.get("cells", []):
                    for file in cell.get("files", []):
                        for key in ("content_hash", "blob_hash"):
                            value = file.get(key)
                            if value is not None:
                                if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                                    raise IncompleteMark("invalid saved content reference")
                                found.add(value)
    return found


def _iter_cas_blobs() -> Iterable[tuple[str, str]]:
    """Yield (full_path, hash) for every file under .collimate/objects/.

    Walks the two-character shard directories. Files whose names don't
    fit the expected sha256 hex shape are skipped — they can be partial
    .tmp uploads or files unrelated to the CAS layout.
    """
    if not os.path.isdir(CAS_DIR):
        return
    for shard in os.listdir(CAS_DIR):
        if shard.startswith(".") or len(shard) != 2:
            continue
        shard_dir = os.path.join(CAS_DIR, shard)
        if not os.path.isdir(shard_dir):
            continue
        for fname in os.listdir(shard_dir):
            full = os.path.join(shard_dir, fname)
            if not os.path.isfile(full):
                continue
            if fname.endswith(".tmp"):
                continue  # in-flight write; leave alone
            blob_hash = shard + fname
            if len(blob_hash) != 64:
                continue
            yield full, blob_hash


def _move_to_trash(src_path: str, sweep_id: str) -> str | None:
    """Move a blob to .collimate/objects.trash/<sweep_id>/ preserving
    the two-char shard layout. Returns the destination path on success
    or None if the move failed (the source survives — fine, GC retries
    next pass)."""
    rel = os.path.relpath(src_path, CAS_DIR)
    dst = os.path.join(TRASH_DIR, sweep_id, rel)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src_path, dst)
        return dst
    except OSError as exc:
        logger.warning("GC: failed to move %s to trash: %s", src_path, exc)
        return None


def _purge_expired_trash(grace_seconds: float) -> int:
    """Delete trash sweeps older than ``grace_seconds``. Returns count
    of removed sweep dirs (a sweep dir holds many blobs)."""
    if not os.path.isdir(TRASH_DIR):
        return 0
    cutoff = time.time() - grace_seconds
    removed = 0
    for entry in os.listdir(TRASH_DIR):
        sweep_dir = os.path.join(TRASH_DIR, entry)
        if not os.path.isdir(sweep_dir):
            continue
        try:
            mtime = os.path.getmtime(sweep_dir)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                shutil.rmtree(sweep_dir)
                removed += 1
                logger.info("GC: purged expired trash sweep %s", entry)
            except OSError as exc:
                logger.warning("GC: failed to purge trash %s: %s", sweep_dir, exc)
    return removed



def _reclaim_abandoned_cas_writes() -> int:
    """Only aged unique CAS publisher temporary files, under its publication lock."""
    import re
    from cas import publication_lock
    cutoff = time.time() - MIN_BLOB_AGE_SECONDS
    removed = 0
    if not os.path.isdir(CAS_DIR) or os.path.islink(CAS_DIR):
        return 0
    with publication_lock:
        for shard in os.listdir(CAS_DIR):
            directory = os.path.join(CAS_DIR, shard)
            if not re.fullmatch('[0-9a-f]{2}', shard) or os.path.islink(directory) or not os.path.isdir(directory):
                continue
            for filename in os.listdir(directory):
                if not re.fullmatch(r'[0-9a-f]{62}\.[0-9a-f]{32}\.tmp', filename):
                    continue
                path = os.path.join(directory, filename)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                if os.stat(path).st_mtime < cutoff:
                    os.unlink(path)
                    removed += 1
    return removed

def run_gc(*, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> dict:
    """Execute one mark-and-sweep cycle.

    Returns a status dict with counts so callers (the maintenance
    endpoint) can surface results::

        {
          "live": <int>,            # blobs the mark phase saw as referenced
          "swept": <int>,           # blobs moved to trash this pass
          "purged_sweeps": <int>,   # historical sweeps unlinked past grace
          "sweep_id": "<uuid>",
        }
    """
    with _collection_lock:
        return _run_gc(grace_seconds)


def _run_gc(grace_seconds: float) -> dict:
    sweep_id = uuid.uuid4().hex[:12]
    try:
        live = collect_live_blobs()
    except Exception as exc:
        logger.warning("GC aborted before sweep: %s", exc)
        return {"sweep_id": sweep_id, "live": 0, "swept": 0, "purged_sweeps": 0,
                "aborted": True, "reason": str(exc)}
    swept = 0
    cutoff = time.time() - MIN_BLOB_AGE_SECONDS
    from cas import publication_lock, pinned_hashes
    for full_path, blob_hash in list(_iter_cas_blobs()):
        if blob_hash in live:
            continue
        with publication_lock:
            if blob_hash in pinned_hashes():
                continue
            try:
                if os.stat(full_path).st_mtime >= cutoff:
                    continue
            except FileNotFoundError:
                continue
            if _move_to_trash(full_path, sweep_id):
                swept += 1
    reclaimed_tmp = _reclaim_abandoned_cas_writes()
    purged = _purge_expired_trash(grace_seconds)
    logger.info(
        "GC pass %s: live=%d swept=%d purged_sweeps=%d",
        sweep_id, len(live), swept, purged,
    )
    return {
        "sweep_id": sweep_id,
        "live": len(live),
        "swept": swept,
        "purged_sweeps": purged,
        "reclaimed_tmp": reclaimed_tmp,
    }


async def run_periodic(interval: float = 24 * 3600) -> None:
    """Daily collection, after protections are installed; never blocks the loop."""
    import asyncio
    while True:
        await asyncio.sleep(interval)
        await collect_background()


async def collect_background() -> dict:
    """Coalesce daily/deletion requests into one tracked worker."""
    import asyncio
    return await asyncio.shield(request_collection())


def request_collection():
    """Register the worker immediately, so shutdown sees even a queued request."""
    import asyncio
    from task_utils import track_task
    global _background_collection
    if _background_collection is None or _background_collection.done():
        _background_collection = track_task(asyncio.to_thread(run_gc), name="cas-gc")
    return _background_collection


async def wait_for_collection() -> None:
    """Do not release/change the data directory while a GC worker still uses it."""
    import asyncio
    if _background_collection is not None and not _background_collection.done():
        await asyncio.shield(_background_collection)


def disk_usage() -> dict:
    """Return current CAS disk usage for /ready surface.

    Best-effort: an OSError walking the tree returns whatever was
    counted up to that point with an ``error`` field flagged."""
    # Counters are ints; an ``error`` / ``trash_error`` string may be
    # added on OSError, hence the loose value type.
    out: dict = {"blob_count": 0, "bytes": 0, "trash_bytes": 0}
    try:
        for full_path, _ in _iter_cas_blobs():
            try:
                out["bytes"] += os.path.getsize(full_path)
                out["blob_count"] += 1
            except OSError:
                continue
    except OSError as exc:
        out["error"] = str(exc)
    if os.path.isdir(TRASH_DIR):
        try:
            for dirpath, _dirnames, filenames in os.walk(TRASH_DIR):
                for fn in filenames:
                    try:
                        out["trash_bytes"] += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        continue
        except OSError as exc:
            out["trash_error"] = str(exc)
    return out
