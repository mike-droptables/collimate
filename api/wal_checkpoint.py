"""Periodic passive SQLite WAL checkpointing.

PASSIVE copies available frames without waiting for readers or writers. SQLite
may retain the WAL high-water allocation while a reader pins older frames; this
avoids a periodic TRUNCATE reserving the writer lock for the busy timeout.
Durability remains governed by SQLite synchronous policy and transaction commits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60.0


def _checkpoint_one(db_path: str) -> tuple[int, int, int] | None:
    """Run a non-waiting PASSIVE checkpoint against one DB.

    Returns the ``(busy, log, checkpointed)`` triple SQLite emits, or
    None if the DB couldn't be opened (the file is missing or being
    rebuilt from a backup). A busy=1 is normal — it means another
    writer beat us to it, which is fine; we'll catch up next pass."""
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        finally:
            conn.close()
        return tuple(row) if row else None
    except sqlite3.DatabaseError as exc:
        logger.warning("wal_checkpoint failed for %s: %s", db_path, exc)
        return None


def checkpoint_all() -> dict:
    """Checkpoint the global DB + every channel DB once. Returns a
    summary dict for logging / surfacing on /ready."""
    summary: dict = {"global": None, "channels": {}}

    summary["global"] = _checkpoint_one(os.path.join(".collimate", "app.db"))

    channels_dir = os.path.join(".collimate", "channels")
    if os.path.isdir(channels_dir):
        for fname in os.listdir(channels_dir):
            if not fname.endswith(".db"):
                continue
            slug = fname[:-3]
            summary["channels"][slug] = _checkpoint_one(os.path.join(channels_dir, fname))
    return summary


async def run_periodic(interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
    """Run forever, checkpointing every ``interval_seconds``.

    Designed to be scheduled as a background task from the FastAPI
    lifespan and cancelled on shutdown. asyncio.CancelledError exits
    cleanly without an extra log line — the lifespan already records
    "shutting down".
    """
    interval = max(5.0, float(interval_seconds))  # floor of 5s — sanity bound
    logger.info("wal_checkpoint: running every %.1fs", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(checkpoint_all)
            except Exception:
                logger.exception("wal_checkpoint pass failed; continuing")
    except asyncio.CancelledError:
        # Final passive checkpoint covers the last teardown commits.
        try:
            await asyncio.to_thread(checkpoint_all)
        except Exception:
            logger.exception("final wal_checkpoint failed")
        raise
