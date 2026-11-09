"""This server's record of the Collimate-built Docker images it uses.

Every ensure of a ``collimate-*`` image (the runner image through
``sandbox.ensure_image``, an environment image and its base through
``EnvironmentService._ensure_image``) upserts ``image_uses(ref, image_id,
first_used_at, last_used_at)`` in ``app.db``, on a cache hit and after a
build alike. The Docker cleanup (:mod:`collimate_server.docker_inventory`)
reads it to tell used images from unused ones; an image with no recorded use
counts as unused. The daemon is machine-wide and each server records only
its own uses, so another server's image looks unused here: deleting it only
makes that server build it again.

The module also keeps the in-process registry of refs being ensured right
now, which a deletion consults: a ref is never removed while an ensure of it
runs, and an ensure waits while a removal of its ref runs.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, Iterator

_logger = logging.getLogger("collimate.image_uses")

#: The repository prefix of every image Collimate builds.
COLLIMATE_PREFIX = "collimate-"

#: The label every Collimate build adds (images built before it have none,
#: so the inventory keys on the repository name).
BUILT_LABEL = "com.collimate.built"


def repository(ref: str) -> str:
    """``collimate-runner:py3.13-…`` → ``collimate-runner``. A registry port
    (``reg:5000/img``) is not a tag separator; a digest reference keeps what
    precedes its ``@``."""
    if "@" in ref:
        return ref.split("@", 1)[0]
    head, sep, tail = ref.rpartition(":")
    return head if sep and "/" not in tail else ref


def is_collimate_ref(ref: str) -> bool:
    """Whether ``ref`` names an image Collimate builds (repository ``collimate-*``)."""
    return repository(ref).startswith(COLLIMATE_PREFIX)


@dataclass(frozen=True)
class ImageUse:
    ref: str
    image_id: str | None
    first_used_at: str
    last_used_at: str


def record_use(ref: str, image_id: str | None) -> None:
    """Upsert this use of ``ref`` (now). A ref outside ``collimate-*`` is not
    recorded. Never raises for the database: a failed write is logged and
    the run that used the image goes on."""
    if not is_collimate_ref(ref):
        return
    from collimate_server.database import get_global_db_connection
    from collimate_server.time_wire import now_iso
    now = now_iso()
    try:
        conn = get_global_db_connection()
        try:
            conn.execute(
                "INSERT INTO image_uses (ref, image_id, first_used_at, last_used_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ref) DO UPDATE SET image_id=excluded.image_id, last_used_at=excluded.last_used_at",
                (ref, image_id, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        _logger.warning("could not record the use of image %s", ref, exc_info=True)


def last_uses() -> dict[str, ImageUse]:
    """Every recorded use, by ref."""
    from collimate_server.database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        rows = conn.execute("SELECT ref, image_id, first_used_at, last_used_at FROM image_uses").fetchall()
    finally:
        conn.close()
    return {row["ref"]: ImageUse(row["ref"], row["image_id"], row["first_used_at"], row["last_used_at"])
            for row in rows}


# -- refs being ensured, refs being removed ----------------------------------

_cond = threading.Condition()
_ensuring: Counter[str] = Counter()
_removing: set[str] = set()


def _try_begin(refs: tuple[str, ...]) -> bool:
    with _cond:
        if not _removing.isdisjoint(refs):
            return False
        _ensuring.update(refs)
        return True


def _end(refs: tuple[str, ...]) -> None:
    with _cond:
        _ensuring.subtract(refs)
        for ref in refs:
            if _ensuring[ref] <= 0:
                del _ensuring[ref]
        _cond.notify_all()


@contextmanager
def ensuring(refs: Iterable[str]) -> Iterator[None]:
    """Mark ``refs`` as being ensured (probe, build, record) for the block;
    blocks first while a removal of any of them runs."""
    names = tuple(refs)
    with _cond:
        _cond.wait_for(lambda: _removing.isdisjoint(names))
        _ensuring.update(names)
    try:
        yield
    finally:
        _end(names)


@asynccontextmanager
async def ensuring_async(refs: Iterable[str]) -> AsyncIterator[None]:
    """:func:`ensuring` for the event loop: it polls instead of blocking, so
    a cancelled wait leaves nothing marked."""
    names = tuple(refs)
    while not _try_begin(names):
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        _end(names)


@contextmanager
def removal_claim(refs: Iterable[str]) -> Iterator[bool]:
    """Claim ``refs`` for removal: yields False (nothing claimed) while an
    ensure of any of them runs, else True, and ensures of them wait until
    the block ends."""
    names = tuple(refs)
    with _cond:
        claimed = not any(_ensuring[ref] for ref in names) and _removing.isdisjoint(names)
        if claimed:
            _removing.update(names)
    try:
        yield claimed
    finally:
        if claimed:
            with _cond:
                _removing.difference_update(names)
                _cond.notify_all()
