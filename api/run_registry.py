"""Async-safe registry of currently-running ProcessManagers, keyed by run_id.

Kept in its own module so every route + helper imports from a single
definition — there is exactly one global ``active_managers`` instance
process-wide.
"""

from contextlib import contextmanager
import threading
from typing import Any, Iterator

from database import canonical_channel_id


class ActiveManagersRegistry:
    """Async-safe registry of running ProcessManagers, keyed by run_id.

    Why this exists: individual dict ops (``get`` / ``pop`` /
    ``__setitem__``) are already atomic under CPython's GIL and
    asyncio's cooperative scheduler — those don't need guarding. The
    real hazard is **iterating while another coroutine mutates**: a
    ``for m in active_managers.values()`` that awaits inside the loop
    races with a concurrent ``pop`` (a run's reaper or a failed
    start's unwind) and either raises
    ``RuntimeError: dictionary changed size during iteration`` or
    silently skips entries.

    This wrapper preserves the dict-like surface that existing
    callers (subscript, ``get``, ``pop``, ``in``) expect, and ``values()``
    and ``items()`` always return a snapshot list, never a live view — so
    iteration is free of concurrent-modification hazards.

    Callers that don't await between a single get/pop/set don't need
    to do anything special — the methods pass straight through.
    """

    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def __contains__(self, run_id: str) -> bool:
        return run_id in self._d

    def __setitem__(self, run_id: str, manager: Any) -> None:
        self._d[run_id] = manager

    def __getitem__(self, run_id: str) -> Any:
        return self._d[run_id]

    def __len__(self) -> int:
        return len(self._d)

    def get(self, run_id: str, default: Any = None) -> Any:
        return self._d.get(run_id, default)

    def pop(self, run_id: str, default: Any = None) -> Any:
        return self._d.pop(run_id, default)

    def clear(self) -> None:
        """Drop all entries (test reset helper)."""
        self._d.clear()

    def values(self) -> list[Any]:
        # Returns a list snapshot — safe to iterate even if another
        # coroutine pops between awaits. The copy is O(n) in the
        # number of active runs (typically < 10), so cheap.
        return list(self._d.values())

    def items(self) -> list[tuple[str, Any]]:
        return list(self._d.items())


# The single process-wide registry — imported everywhere.
active_managers: ActiveManagersRegistry = ActiveManagersRegistry()

# Set once by the lifespan shutdown hook (main.py) just before it tears
# everything down. create_run checks it so a POST /runs racing shutdown
# can't spawn a generator after stop_all_runs has swept the registry —
# such a process (its own session, detached from the server's group)
# would survive server exit with nothing left to kill it. Deliberately
# NOT set by the in-app "Stop all" button: the server keeps serving and
# new runs stay legal there.
_shutting_down: bool = False


def mark_shutting_down(value: bool = True) -> None:
    """Flip the shutdown gate. The lifespan startup hook resets it to
    False so a process that runs several startup/shutdown cycles (tests,
    embedded servers) doesn't stay gated after the first shutdown."""
    global _shutting_down
    _shutting_down = value


def is_shutting_down() -> bool:
    return _shutting_down


# UUID channels held by starts/stops across awaited status and artifact writes.
# Deletion must wait for these operations as well as registered live runs.
_busy: dict[str, int] = {}


@contextmanager
def channel_busy(channel: str) -> Iterator[None]:
    """Mark ``channel``'s db as written for the ``with`` block: start_run
    holds it from before its first await until it returns (its run spawned
    and registered, or unwound); stop_run holds it from before its first
    await across its status and stop-event writes, and Stop all holds it
    for each channel's status write. A rename or delete on the loop
    therefore either ran first (the start or the stop then finds no
    channel) or sees the mark or the stop's registered runs."""
    channel = canonical_channel_id(channel)
    _busy[channel] = _busy.get(channel, 0) + 1
    try:
        yield
    finally:
        left = _busy[channel] - 1
        if left:
            _busy[channel] = left
        else:
            del _busy[channel]


def busy_channels() -> set[str]:
    """UUID channels with a start or stop writing their database."""
    return set(_busy)


def mark_stop_requested(manager) -> None:
    """Record that ``manager``'s run is being stopped — the user's Stop,
    Stop all, a snapshot's update-and-stop, the watchdog — BEFORE any
    signal. The mark is sticky: from then on the run registers no new
    surface and its annotation context refuses writes, /runs/active lists it as stopping,
    and its finish op writes no status, so a run still running ends with its
    stopper's status (``stopped``, or ``timed_out`` for the watchdog)."""
    if manager is None:
        return
    manager.stop_requested = True


class StartCancelled(Exception):
    """A start found its own hold cancelled by Stop all or a server shutdown
    and unwound before it went live."""


# The starting hold, {run_id: _Hold}. Every start reserves one for its cell
# when it inserts the run row (routes/runs/create.py) and releases it once
# it has registered its manager or unwound; in between, the cell counts as
# running for removal, merge, split, checkpoint, export and a move onto
# Capabilities. Stop all and a server shutdown cancel every hold
# (cancel_holds): a start whose hold was cancelled unwinds as stopped at its
# last gate, before it registers. Keyed by run, so several runs can start on
# one cell at once.
class _Hold:
    __slots__ = ("cell_id", "cancelled")

    def __init__(self, cell_id: str) -> None:
        self.cell_id = cell_id
        self.cancelled = False


_holds: dict[str, _Hold] = {}
_holds_lock = threading.Lock()


def reserve(run_id: str, cell_id: str) -> None:
    """Hold ``cell_id`` for the starting run ``run_id``."""
    with _holds_lock:
        _holds[str(run_id)] = _Hold(str(cell_id))


def release(run_id: str) -> None:
    with _holds_lock:
        _holds.pop(str(run_id), None)


def cancel_holds() -> int:
    """Cancel every starting hold (Stop all, server shutdown). The holds stay
    until their starts release them, so the cells are not free before those
    starts have unwound. Returns how many."""
    with _holds_lock:
        for hold in _holds.values():
            hold.cancelled = True
        return len(_holds)


def hold_is_live(run_id: str) -> bool:
    """Whether ``run_id``'s starting hold stands, uncancelled."""
    with _holds_lock:
        hold = _holds.get(str(run_id))
        return hold is not None and not hold.cancelled


def held_cell_ids() -> set[str]:
    """The cells a starting hold stands on, cancelled or not."""
    with _holds_lock:
        return {h.cell_id for h in _holds.values()}


def runs_on_cell(cell_id: str) -> list:
    """The live runs on a visible cell."""
    cid = str(cell_id)
    return [
        m for m in active_managers.values()
        if str(getattr(m, "cell_id", "") or "") == cid
    ]


def live_labels(cell_id: str) -> set[str]:
    """The labels of the live runs on ``cell_id`` — the run folders there
    that are locked."""
    return {m.label for m in runs_on_cell(cell_id) if getattr(m, "label", None)}


def lock_owner(cell_id: str, path: str):
    """The live run whose run folder holds ``path`` on ``cell_id``, or None."""
    from run_folders import label_of
    label = label_of(path)
    if not label:
        return None
    return next((m for m in runs_on_cell(cell_id) if getattr(m, "label", None) == label), None)


def running_cell_ids() -> set[str]:
    """Cells with a live run or a starting hold — the gate for stack/cell
    removal, merge, split, checkpoint, export and a move onto Capabilities."""
    live = {
        str(m.cell_id) for m in active_managers.values()
        if getattr(m, "cell_id", None)
    }
    return live | held_cell_ids()
