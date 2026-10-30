"""
Event broadcast hub for SSE state streaming.

Provides a simple channel-based pub/sub system using asyncio.Queue.
Subscribers receive events pushed by the backend (DAG commits, status changes).
All operations must be called from the event loop thread.

Backpressure policy: when a subscriber's queue is full (slow client,
suspended browser tab) the broadcaster places a sentinel on the queue
instead of dropping the event silently. The SSE handler sees the
sentinel, emits overflow and closes the stream. The client reconnects
for a new revisioned snapshot before accepting further deltas.
"""

import asyncio
import logging
import json
import uuid
from collections import defaultdict

from database import canonical_channel_id

logger = logging.getLogger(__name__)


# Marker put on a subscriber queue when broadcast() encounters a full
# queue. The SSE handler treats receipt of this object as "your stream
# is stale, close the connection and let the client reconnect."
OVERFLOW_SENTINEL: dict = {"__overflow__": True}


_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
_main_loop = None
_state_heads: dict[str, tuple[str, dict]] = {}
_state_ready: set[asyncio.Queue] = set()
_overflowed: set[asyncio.Queue] = set()
_state_epoch = uuid.uuid4().hex
_state_sequence = 0


def capture_loop():
    """Capture the main event loop early so broadcast() works before any
    subscriber connects. Called from the FastAPI lifespan startup.

    Adopts the running loop whenever the stored one is missing, closed or
    a different loop: a later lifespan cycle in the same process (tests,
    an embedded server) runs on a new loop, and keeping the old, closed
    one would silently drop every broadcast."""
    global _main_loop, _state_epoch, _state_sequence
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _main_loop is None or _main_loop.is_closed() or _main_loop is not running:
        _main_loop = running
        # Pending coalesce timers belong to the old loop and never fire.
        _pending_coalesced.clear()
        _subscribers.clear()
        _state_heads.clear()
        _state_ready.clear()
        _overflowed.clear()
        _state_epoch = uuid.uuid4().hex
        _state_sequence = 0


def _dispatch_on_main_loop(fn) -> None:
    """Run ``fn`` on the main loop: inline when called from it, otherwise
    via ``call_soon_threadsafe``. Captures the loop on first use if startup
    didn't get it (or the stored one has closed)."""
    global _main_loop
    try:
        current_loop = asyncio.get_running_loop()
        if _main_loop is None or _main_loop.is_closed():
            capture_loop()
        if current_loop is _main_loop:
            fn()
            return
    except RuntimeError:
        pass

    if _main_loop is not None and not _main_loop.is_closed():
        _main_loop.call_soon_threadsafe(fn)


def subscribe(channel: str) -> asyncio.Queue:
    """Create a new subscriber queue for a channel's event stream."""
    capture_loop()
    channel = canonical_channel_id(channel)
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers[channel].append(q)
    return q


def unsubscribe(channel: str, q: asyncio.Queue):
    """Remove a subscriber queue."""
    channel = canonical_channel_id(channel)
    queues = _subscribers.get(channel, [])
    if q in queues:
        queues.remove(q)
    _state_ready.discard(q)
    _overflowed.discard(q)
    if not queues:
        _subscribers.pop(channel, None)
        _state_heads.pop(channel, None)


def broadcast(channel: str, event: dict):
    """Push an event to all subscribers of a channel. Thread-safe."""
    channel = canonical_channel_id(channel)

    def _put():
        for q in _subscribers.get(channel, []):
            _put_queue(channel, q, event)

    _dispatch_on_main_loop(_put)


class SerializedEvent(dict):
    """Wire dictionary with its once-built SSE body, shared by subscribers."""
    def __init__(self, event):
        super().__init__(event)
        self.sse_data = "data: " + json.dumps(event) + "\n\n"


# One timer for global state updates. Reset when a lifespan adopts a new loop.
_pending_coalesced: dict[str, object] = {}


def broadcast_coalesced(build_snapshots, delay: float = 0.25):
    """Diff off-loop; publish each baseline/revision and eligible queues atomically."""
    def schedule():
        if not any(_subscribers.values()):
            return
        if "state" in _pending_coalesced:
            if _pending_coalesced["state"] is True:
                _pending_coalesced["dirty"] = build_snapshots
            return

        epoch, owner_loop = _state_epoch, _main_loop
        def fire():
            global _state_sequence
            if epoch != _state_epoch or owner_loop is not _main_loop:
                return
            _pending_coalesced["state"] = True
            targets = {name: tuple(queues) for name, queues in _subscribers.items() if queues}
            baseline = {name: _state_heads.get(name) for name in targets}
            waiting = {name: any(q not in _state_ready for q in queues) for name, queues in targets.items()}
            candidates = {}
            for name in targets:
                _state_sequence += 1
                candidates[name] = f"{epoch}:{_state_sequence}"

            def complete(results):
                # A worker from a retired lifespan must not publish into the new loop.
                if epoch != _state_epoch:
                    return
                for channel, (snapshot, revision, full, delta, reset) in results.items():
                    queues = [q for q in targets[channel]
                              if q in _subscribers.get(channel, ()) and q not in _overflowed]
                    if not queues:
                        continue
                    # No await: the revision and snapshot become visible together.
                    _state_heads[channel] = (revision, snapshot)
                    for q in queues:
                        event = full if reset or q not in _state_ready else delta
                        if event is None:
                            continue
                        _put_queue(channel, q, event)
                        if q not in _overflowed:
                            _state_ready.add(q)
                _pending_coalesced.pop("state", None)
                again = _pending_coalesced.pop("dirty", None)
                if again is not None:
                    broadcast_coalesced(again, delay)

            def build():
                from state import state_patches
                from collimate._host.channel_events import StateSnapshot, StateDelta
                results = {}
                try:
                    for channel, snapshot in (build_snapshots(list(targets)) or {}).items():
                        prior = baseline[channel]
                        patches = state_patches(prior[1], snapshot) if prior is not None else []
                        reset = prior is None or prior[1].get("channel_scope") != snapshot.get("channel_scope")
                        revision = candidates[channel] if prior is None or reset or patches else prior[0]
                        full = (SerializedEvent(StateSnapshot(revision, snapshot).to_dict())
                                if reset or waiting[channel] else None)
                        delta = (SerializedEvent(StateDelta(prior[0], revision,
                                    snapshot.get("channel_scope"), patches).to_dict())
                                 if prior is not None and not reset and patches else None)
                        results[channel] = (snapshot, revision, full, delta, reset)
                except Exception:
                    logger.exception("coalesced state diff failed")
                _dispatch_on_main_loop(lambda: complete(results))

            asyncio.get_running_loop().run_in_executor(None, build)
        _pending_coalesced["state"] = asyncio.get_running_loop().call_later(delay, fire)
    _dispatch_on_main_loop(schedule)


def _put_queue(channel: str, q: asyncio.Queue, event: dict) -> None:
    """Loop-owned enqueue; overflow is sticky until the stream closes."""
    if q in _overflowed:
        return
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        _overflowed.add(q)
        logger.warning("SSE backpressure for channel %r; closing stream", channel)
        while not q.empty():
            q.get_nowait()
        q.put_nowait(OVERFLOW_SENTINEL)


def broadcast_all(event: dict):
    """Push an event to every subscriber across all channels. Used for
    system-wide notifications (e.g. server shutdown) where no single
    channel is meaningful. Same loop-dispatch + backpressure semantics
    as :func:`broadcast`."""

    def _put():
        for channel, queues in list(_subscribers.items()):
            for q in list(queues):
                _put_queue(channel, q, event)

    _dispatch_on_main_loop(_put)
