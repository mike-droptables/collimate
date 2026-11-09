"""The per-cell repository lease: one live run is the sole writer of a cell's
git state (``docs/plans/2026-10-31-git-in-cells/design.md`` §4.1, R1).

An in-memory registry keyed by ``(channel, cell_id)``. A run that will
materialize the cell's repositories claims the lease at start; it is
``live`` while the run works and ``releasing`` from the moment the run is
stopped or exits, whichever comes first, until its final harvest commits or
rescues, when ``release_run`` drops it and sets its ``released`` Event.
Another run of the cell started while it is live gets a read-only ``.git``
(``git_harvest.materialize``), and a Git runner is refused with
:class:`RepositoryBusy`. Starts and copies wait on a releasing entry
(:func:`wait_released`) so they see its harvest: a run started while the
holder is being stopped takes the lease and a writable ``.git``. A restart
clears the registry: crash recovery claims afresh.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

_logger = logging.getLogger(__name__)

LIVE: Literal["live"] = "live"
RELEASING: Literal["releasing"] = "releasing"

# How long a copy or a run start waits on a releasing lease before it goes
# on with the rows it has.
RELEASE_WAIT_SECONDS = 60.0

# The label of the releasing lease crash recovery holds on a cell while it
# harvests an interrupted run there.
RECOVERY = "recovery"

_POLL_SECONDS = 0.05


@dataclass
class Lease:
    """One cell's lease. ``materialized`` maps each repository path the run
    holds a ``.git`` for to the state hash it was built from or last
    harvested to (the base of the "unchanged" check); ``seq`` numbers the
    run's harvest attempts; ``released`` is set once the entry is gone."""

    channel: str
    cell_id: str
    run_id: str
    label: str
    state: Literal["live", "releasing"] = LIVE
    materialized: dict[str, str] = field(default_factory=dict)
    seq: int = 0
    released: threading.Event = field(default_factory=threading.Event)

    def next_seq(self) -> int:
        """The number of a new harvest attempt, taken when it reads."""
        with _lock:
            self.seq += 1
            return self.seq


class RepositoryBusy(ValueError):
    """Another run holds the cell's lease: a Git runner's start is refused
    with 409 and this body, shaped like ``served_surfaces.SurfaceTaken``."""

    def __init__(self, owner: str, run_id: str):
        super().__init__("repository_busy")
        self.owner, self.run_id = owner, run_id

    @property
    def message(self) -> str:
        return (f"git is in use by run {self.run_id[:8]} on this cell; "
                "Duplicate the cell for a second working copy")

    @property
    def readonly_line(self) -> str:
        """Why a run that is not a Git runner gets a read-only ``.git``
        while this lease is held: its run log's line, its card's and its
        ``/runs/active`` row's ``git.reason``."""
        return (f"git is read-only in this run: run {self.run_id[:8]} ({self.owner}) holds this cell's git; "
                "Duplicate the cell to commit")

    def response(self):
        return {"error": "repository_busy", "owner": self.owner, "run_id": self.run_id, "message": self.message}


_lock = threading.Lock()
_leases: dict[tuple[str, str], Lease] = {}


def claim(channel: str, cell_id: str, run_id: str, label: str, *, state: Literal["live", "releasing"] = LIVE) -> Lease:
    """Claim the cell's lease for ``run_id`` (``label`` names it in a
    refusal) and return it. A claim by its own holder returns the held
    entry; one held by another run, live or releasing, raises
    :class:`RepositoryBusy`. Crash recovery claims straight into
    ``releasing``, so copies wait on its harvest."""
    key = (str(channel), str(cell_id))
    with _lock:
        held = _leases.get(key)
        if held is not None:
            if held.run_id != run_id:
                raise RepositoryBusy(held.label, held.run_id)
            return held
        lease = Lease(key[0], key[1], run_id, label, state)
        _leases[key] = lease
        return lease


def holder(channel: str, cell_id: str) -> Lease | None:
    """The cell's lease, or None when no run holds it."""
    with _lock:
        return _leases.get((str(channel), str(cell_id)))


def begin_release(channel: str, cell_id: str, run_id: str) -> Lease | None:
    """Mark ``run_id``'s lease on the cell ``releasing`` (its run is being
    stopped or has exited, and the final harvest follows); None when it
    holds none."""
    with _lock:
        lease = _leases.get((str(channel), str(cell_id)))
        if lease is None or lease.run_id != run_id:
            return None
        lease.state = RELEASING
        return lease


def release_run(run_id: str) -> list[Lease]:
    """Drop every lease ``run_id`` holds, setting each one's ``released``
    Event; the dropped entries are returned."""
    with _lock:
        gone = [lease for lease in _leases.values() if lease.run_id == run_id]
        for lease in gone:
            del _leases[(lease.channel, lease.cell_id)]
    for lease in gone:
        lease.released.set()
    return gone


def wait_released_sync(channel: str, cell_id: str, *, timeout: float | None = None) -> bool:
    """:func:`wait_released` for a worker thread: blocks on the releasing
    entry's Event. Call it outside any cell lock."""
    timeout = RELEASE_WAIT_SECONDS if timeout is None else timeout
    lease = holder(channel, cell_id)
    if lease is None or lease.state != RELEASING:
        return True
    if lease.released.wait(timeout):
        return True
    _logger.warning("git state of cell %s is still being saved by run %s after %.0f s; going on",
                    cell_id, lease.run_id[:8], timeout)
    return False


async def wait_released(channel: str, cell_id: str, *, timeout: float | None = None) -> bool:
    """Wait, up to ``timeout`` seconds (:data:`RELEASE_WAIT_SECONDS` when
    None), for a releasing lease on the cell to
    be released. True when none is releasing (or it was released in time);
    False on timeout, logged, and the caller goes on with the rows it has.
    A live lease is not waited on. Call it outside ``cell_write_scope``: the
    harvest it waits for commits under the cell lock."""
    timeout = RELEASE_WAIT_SECONDS if timeout is None else timeout
    lease = holder(channel, cell_id)
    if lease is None or lease.state != RELEASING:
        return True
    deadline = time.monotonic() + timeout
    while not lease.released.is_set():
        if time.monotonic() >= deadline:
            _logger.warning("git state of cell %s is still being saved by run %s after %.0f s; going on",
                            cell_id, lease.run_id[:8], timeout)
            return False
        await asyncio.sleep(_POLL_SECONDS)
    return True
