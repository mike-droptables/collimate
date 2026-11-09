"""Fail-closed body-store mark-and-sweep, one store at a time, with a
recent-publication floor.

Each channel's store is collected on its own (:func:`collect_channel`): the
mark reads that channel's DB, its live layout and that store's pins, and the
sweep touches only that store. The app store (:func:`collect_app_store`)
marks with the three ``app.db`` tables that name its bodies and the server
settings row, whose user themes name their scene pictures. An unreadable
source prevents that store's sweep and trash purge, never another's. A
store or dedupe hit refreshes its mtime; objects younger than a day are
never swept, and the age-check/move shares the store's publication lock.
Unreferenced old objects move to the store's trash, then expire after the
separate trash grace period. Readers never depend on recovery from trash.

The lifespan's scheduler (:func:`run_periodic`) collects every store once a
day, counted from the last collection, which ``app.db`` records
(``maintenance.last_collection_at``): a server that starts with its
collection overdue collects right away, otherwise it waits for the due
time. ``POST /api/storage/collect`` collects now (:func:`collect_now`).
"""

from __future__ import annotations

import logging
import json
import os
import re
import shutil
import sqlite3
import time
import threading
import uuid
from typing import Iterable

from collimate_server.cas import BodyStore, app_store, channel_store, is_cas_hash
from collimate_server.database import get_db_connection, read_global_json, write_global_json

logger = logging.getLogger(__name__)


DEFAULT_GRACE_SECONDS = 24 * 3600
MIN_BLOB_AGE_SECONDS = 24 * 3600
#: How often every store is collected, counted from the last collection.
COLLECTION_INTERVAL_SECONDS = 24 * 3600
#: The ``app.db`` ``ui_state`` row holding when the last collection ended.
LAST_COLLECTION_KEY = "maintenance.last_collection_at"
# One collection per store at a time.
_collection_locks: dict[str, threading.Lock] = {}
_collection_locks_guard = threading.Lock()
_background_collection = None
_now_collection = None
_git_collection = None
# The scheduler's and collect-now's app.db reads and writes in flight.
_bookkeeping: set = set()


class IncompleteMark(RuntimeError):
    """No object can be reclaimed until every required source was read."""


def _collection_lock(store: BodyStore) -> threading.Lock:
    with _collection_locks_guard:
        lock = _collection_locks.get(store.root)
        if lock is None:
            lock = _collection_locks[store.root] = threading.Lock()
        return lock


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

    Any error (a lock held past the busy timeout) raises: an empty answer
    would read as "this DB references nothing".
    """
    from collimate_server.versions import iter_version_files
    live: set[str] = {row['blob_hash'] for row in iter_version_files(conn)}
    for sql in (
        "SELECT DISTINCT body_hash FROM file_revisions WHERE body_hash IS NOT NULL",
        "SELECT DISTINCT blob_hash FROM run_artifacts WHERE blob_hash IS NOT NULL",
    ):
        values = {r[0] for r in conn.execute(sql).fetchall()}
        if not all(is_cas_hash(value) for value in values):
            raise IncompleteMark("invalid stored content reference")
        live |= values
    if not all(is_cas_hash(value) for value in live):
        raise IncompleteMark("invalid stored version content reference")
    return live


def _layout_hashes(channel_id: str) -> set[str]:
    """The bodies the live layout cache of the channel names."""
    from collimate_server.state import state, state_lock
    found: set[str] = set()
    with state_lock:
        channel = next((ch for ch in state.channels if str(ch.id) == channel_id), None)
        if channel is None:
            return found
        for stack in (*channel.main_stacks, *channel.nav_stacks):
            for cell in stack.cells:
                for file in cell.files:
                    for value in (file.body_hash, file.blob_hash):
                        if value is not None:
                            if not is_cas_hash(value):
                                raise IncompleteMark("invalid live content reference")
                            found.add(value)
    return found


def channel_live_blobs(channel_id: str) -> set[str]:
    """Every body the channel's store must keep: its DB's references (after
    pruning retired file revisions), its live layout's and its pins."""
    from collimate_server.persistence import prune_file_revisions
    conn = get_db_connection(channel_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        prune_file_revisions(conn)
        live = _live_blobs_in_channel(conn)
        conn.commit()
    finally:
        conn.close()
    return live | _layout_hashes(channel_id)


def _iter_store_blobs(store: BodyStore) -> Iterable[tuple[str, str]]:
    """Yield (full_path, hash) for every object file of the store.

    Walks the two-character shard directories. Files whose names don't
    fit the expected sha256 hex shape are skipped — they can be partial
    .tmp uploads or files unrelated to the CAS layout.
    """
    if not os.path.isdir(store.root):
        return
    for shard in os.listdir(store.root):
        if shard.startswith(".") or len(shard) != 2:
            continue
        shard_dir = os.path.join(store.root, shard)
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


def _move_to_trash(store: BodyStore, src_path: str, sweep_id: str) -> str | None:
    """Move a blob to ``<store>.trash/<sweep_id>/`` preserving the
    two-char shard layout. Returns the destination path on success or None
    if the move failed (the source survives — fine, GC retries next pass)."""
    rel = os.path.relpath(src_path, store.root)
    dst = os.path.join(store.trash, sweep_id, rel)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src_path, dst)
        return dst
    except OSError as exc:
        logger.warning("GC: failed to move %s to trash: %s", src_path, exc)
        return None


def _purge_expired_trash(store: BodyStore, grace_seconds: float) -> tuple[int, int]:
    """Delete trash sweeps older than ``grace_seconds``. Returns how many
    sweep dirs went (a sweep dir holds many blobs) and the bytes they held."""
    from collimate_server.storage_survey import measure
    if not os.path.isdir(store.trash):
        return 0, 0
    cutoff = time.time() - grace_seconds
    removed = freed = 0
    for entry in os.listdir(store.trash):
        sweep_dir = os.path.join(store.trash, entry)
        if not os.path.isdir(sweep_dir):
            continue
        try:
            mtime = os.path.getmtime(sweep_dir)
        except OSError:
            continue
        if mtime <= cutoff:
            held = measure([sweep_dir]).bytes
            try:
                shutil.rmtree(sweep_dir)
                removed += 1
                freed += held
                logger.info("GC: purged expired trash sweep %s", sweep_dir)
            except OSError as exc:
                logger.warning("GC: failed to purge trash %s: %s", sweep_dir, exc)
    return removed, freed


def _reclaim_abandoned_writes(store: BodyStore) -> int:
    """Only aged unique publisher temporary files, under the store's publication lock."""
    cutoff = time.time() - MIN_BLOB_AGE_SECONDS
    removed = 0
    if not os.path.isdir(store.root) or os.path.islink(store.root):
        return 0
    with store.publication_lock:
        for shard in os.listdir(store.root):
            directory = os.path.join(store.root, shard)
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


def _sweep(store: BodyStore, live: set[str], grace_seconds: float) -> dict:
    sweep_id = uuid.uuid4().hex[:12]
    swept = 0
    cutoff = time.time() - MIN_BLOB_AGE_SECONDS
    for full_path, blob_hash in list(_iter_store_blobs(store)):
        if blob_hash in live:
            continue
        with store.publication_lock:
            if blob_hash in store.pinned_hashes():
                continue
            try:
                if os.stat(full_path).st_mtime >= cutoff:
                    continue
            except FileNotFoundError:
                continue
            if _move_to_trash(store, full_path, sweep_id):
                swept += 1
    reclaimed_tmp = _reclaim_abandoned_writes(store)
    purged, freed = _purge_expired_trash(store, grace_seconds)
    logger.info("GC pass %s of %s: live=%d swept=%d purged_sweeps=%d freed=%d",
                sweep_id, store.root, len(live), swept, purged, freed)
    return {"sweep_id": sweep_id, "live": len(live), "swept": swept,
            "purged_sweeps": purged, "freed_bytes": freed, "reclaimed_tmp": reclaimed_tmp}


def _aborted(reason: str) -> dict:
    return {"live": 0, "swept": 0, "purged_sweeps": 0, "freed_bytes": 0, "aborted": True, "reason": reason}


def collect_channel(channel_id: str, *, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> dict:
    """One mark-and-sweep of the channel's store. Skipped (``aborted``) while
    the channel is being created, deleted or moved, and when any of its
    sources cannot be read; never raises for one channel's trouble."""
    from collimate_server.channel_guard import is_moving
    from collimate_server.database import changing_channels, creating_channels
    import collimate_server.channel_paths as channel_paths
    if channel_id in creating_channels or channel_id in changing_channels or is_moving(channel_id):
        return {"id": channel_id, **_aborted("the channel is being created, deleted or moved")}
    try:
        store = channel_store(channel_id)
    except channel_paths.ChannelUnavailable as exc:
        return {"id": channel_id, **_aborted(exc.reason)}
    with _collection_lock(store):
        try:
            live = channel_live_blobs(channel_id) | store.pinned_hashes()
        except Exception as exc:
            logger.warning("GC of channel %s aborted before sweep: %s", channel_id, exc)
            return {"id": channel_id, **_aborted(str(exc))}
        return {"id": channel_id, **_sweep(store, live, grace_seconds)}


def app_store_live_blobs() -> set[str]:
    """The bodies ``app.db`` names: compiled renderer bundles, accepted
    handler builds, mirrored modules, and the scene pictures of the server
    settings' user themes (an upload only that row names;
    ``app_settings.scene_image_hashes``)."""
    from collimate_server.app_settings import APP_SETTINGS_KEY, scene_image_hashes
    from pathlib import Path
    live: set[str] = set()
    path = os.path.join(".collimate", "app.db")
    if not os.path.exists(path):
        return live
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        for (digest,) in conn.execute("SELECT blob_hash FROM renderer_bundles UNION SELECT blob_hash FROM handler_derived_files "
                                      "UNION SELECT blob_hash FROM esm_modules"):
            if not is_cas_hash(digest):
                raise IncompleteMark("invalid compiled, accepted-derived or mirrored reference")
            live.add(digest)
        for (value,) in conn.execute("SELECT value FROM ui_state WHERE key = ?", (APP_SETTINGS_KEY,)):
            try:
                settings = json.loads(value) if value else None
            except ValueError as exc:
                raise IncompleteMark("unreadable app settings") from exc
            pictures = set(scene_image_hashes(settings))
            if not all(is_cas_hash(digest) for digest in pictures):
                raise IncompleteMark("invalid app settings picture reference")
            live |= pictures
    finally:
        conn.close()
    return live


def collect_app_store(*, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> dict:
    """One mark-and-sweep of the app store."""
    store = app_store()
    with _collection_lock(store):
        try:
            live = app_store_live_blobs() | store.pinned_hashes()
        except Exception as exc:
            logger.warning("GC of the app store aborted before sweep: %s", exc)
            return _aborted(str(exc))
        return _sweep(store, live, grace_seconds)


def collect_staging_store(*, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> dict:
    """One sweep of the staging store: nothing names its bodies, so every
    one past the publication floor and not pinned goes (a new channel's
    creation copies what it takes within that day)."""
    from collimate_server.cas import staging_store
    store = staging_store()
    with _collection_lock(store):
        return _sweep(store, store.pinned_hashes(), grace_seconds)


def run_gc(*, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> dict:
    """Collect every channel's store (each registered channel and each
    indexed one, archived ones included), then the app store and the
    staging store; ``freed_bytes`` is what each store's trash purge gave
    back::

        {"channels": [{"id", "live", "swept", "purged_sweeps", "freed_bytes", ...}],
         "app_store": {...}, "staging": {...}}
    """
    import collimate_server.channel_paths as channel_paths
    from collimate_server.state import state, state_lock
    with state_lock:
        registered = {str(channel.id) for channel in state.channels}
    channels = sorted(registered | set(channel_paths.indexed_channels()))
    return {"channels": [collect_channel(channel_id, grace_seconds=grace_seconds) for channel_id in channels],
            "app_store": collect_app_store(grace_seconds=grace_seconds),
            "staging": collect_staging_store(grace_seconds=grace_seconds)}


def last_collection_at() -> str | None:
    """When the last collection of every store ended (ISO-8601 UTC), None
    before the first."""
    return read_global_json(LAST_COLLECTION_KEY)


def record_collection() -> str:
    """Record that a collection of every store ended now; returns the time."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_global_json(LAST_COLLECTION_KEY, now)
    return now


def seconds_until_due(interval: float = COLLECTION_INTERVAL_SECONDS) -> float:
    """How long until the next collection is due: 0 when it is overdue (the
    last one ended more than ``interval`` ago). A data dir that never
    collected starts its clock now: every body in it is younger than the
    publication floor, so there is nothing to collect yet."""
    from datetime import datetime, timezone
    last = last_collection_at()
    if last is None:
        record_collection()
        return interval
    try:
        ended = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return 0.0
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - ended).total_seconds()
    return min(interval, max(0.0, interval - elapsed))


async def run_periodic(interval: float = COLLECTION_INTERVAL_SECONDS) -> None:
    """The lifespan's collection scheduler, started after readiness: wait
    until the next collection is due (not at all when it is overdue), then
    collect every body store, then the channels' git stores, and record
    it; never blocks the loop."""
    import asyncio
    while True:
        await asyncio.sleep(await _bookkeep(seconds_until_due, interval))
        await collect_background()
        await asyncio.shield(request_git_collection())
        await _bookkeep(record_collection)


async def _bookkeep(work, *args):
    """``work(*args)`` on a tracked worker thread that shutdown waits for
    (:func:`wait_for_collection`): it reads or writes ``app.db``, which must
    not outlive the lifespan."""
    import asyncio
    from collimate_server.task_utils import track_task
    task = track_task(asyncio.to_thread(work, *args), name="cas-gc-schedule")
    _bookkeeping.add(task)
    task.add_done_callback(_bookkeeping.discard)
    return await asyncio.shield(task)


async def collect_now() -> dict:
    """``POST /api/storage/collect``: one collection of every store now,
    with no trash grace (what is swept is purged at once; the publication
    floor stays), then the git stores; recorded as the last collection.
    Answers each store's outcome, ``git``, ``freed_bytes`` (what the body
    stores gave back) and ``last_collection_at``."""
    import asyncio
    from collimate_server.task_utils import track_task
    global _now_collection
    _now_collection = track_task(asyncio.to_thread(run_gc, grace_seconds=0), name="cas-gc-now")
    result = await asyncio.shield(_now_collection)
    result["git"] = await asyncio.shield(request_git_collection())
    result["freed_bytes"] = (sum(outcome.get("freed_bytes", 0) for outcome in result["channels"])
                             + result["app_store"].get("freed_bytes", 0) + result["staging"].get("freed_bytes", 0))
    result["last_collection_at"] = await _bookkeep(record_collection)
    return result


async def collect_background() -> dict:
    """Coalesce daily requests into one tracked worker."""
    import asyncio
    return await asyncio.shield(request_collection())


def request_collection():
    """Register the worker immediately, so shutdown sees even a queued request."""
    import asyncio
    from collimate_server.task_utils import track_task
    global _background_collection
    if _background_collection is None or _background_collection.done():
        _background_collection = track_task(asyncio.to_thread(run_gc), name="cas-gc")
    return _background_collection


def _collect_git_stores() -> list[dict]:
    """Every registered channel's git store GC (``git_store.collect_all``),
    one outcome per channel; a failure is logged, never raised."""
    from dataclasses import asdict
    import collimate_server.git_store as git_store
    try:
        return [asdict(outcome) for outcome in git_store.collect_all()]
    except Exception:
        logger.exception("git store GC failed")
        return []


def request_git_collection():
    """The tracked worker collecting the git stores, one at a time, after
    the body stores (daily and on maintenance)."""
    import asyncio
    from collimate_server.task_utils import track_task
    global _git_collection
    if _git_collection is None or _git_collection.done():
        _git_collection = track_task(asyncio.to_thread(_collect_git_stores), name="git-store-gc")
    return _git_collection


async def wait_for_collection() -> None:
    """Do not release/change the data directory while a GC worker still uses it."""
    import asyncio
    for task in (_background_collection, _now_collection, _git_collection, *_bookkeeping):
        if task is not None and not task.done():
            await asyncio.shield(task)

