"""Coverage for the SSE backpressure path.

When a subscriber's queue fills up (slow client, suspended tab) the
broadcaster used to silently drop events — leaving the UI permanently
stale with no diagnostic. The new behaviour enqueues an ``OVERFLOW_SENTINEL``
that the SSE handler converts into a final ``{"type": "overflow"}``
event before closing the stream so the client can reconnect and
re-fetch the canonical state.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest


CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "sse-backpressure"))


@pytest.fixture(autouse=True)
def registered_channel(fresh_cwd, monkeypatch):
    import state
    from models import AppState, Channel
    channel = Channel(id=CHANNEL, name="SSE test")
    monkeypatch.setattr(state, "state", AppState(channels=[channel], active_channel_id=channel.id))
    # These tests exercise stream transport; scoped state construction is
    # covered by the multichannel coalescer tests.
    monkeypatch.setattr(state, "broadcast_state_update", lambda: None)


def _reset_events():
    """Each test wants a fresh subscriber map + loop snapshot."""
    import events
    events._main_loop = None
    events._subscribers.clear()


def test_overflow_sentinel_replaces_stale_backlog():
    """When the queue saturates, the broadcaster drops the now-stale
    backlog and posts a single overflow sentinel. The client will
    refetch /api/state on disconnect anyway, so the dropped events
    aren't useful — and keeping them around would prevent the sentinel
    from ever fitting into the queue."""
    _reset_events()
    import events

    async def _scenario():
        events.capture_loop()
        q = events.subscribe(CHANNEL)
        # Fill to capacity.
        for i in range(256):
            q.put_nowait({"i": i})

        events.broadcast(CHANNEL, {"trigger": True})

        # The queue should now contain ONLY the sentinel — the stale
        # backlog was discarded.
        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
        assert seen == [events.OVERFLOW_SENTINEL], (
            f"expected only the sentinel, got {len(seen)} items"
        )

    asyncio.run(_scenario())


def test_overflow_recovery_requires_reconnect_then_normal_broadcast_works__P4F03():
    """Overflow remains sticky; reconnecting creates a healthy subscriber."""
    _reset_events()
    import events

    async def _scenario():
        events.capture_loop()
        q = events.subscribe(CHANNEL)
        # Saturate, trigger overflow, drain sentinel.
        for i in range(256):
            q.put_nowait({"i": i})
        events.broadcast(CHANNEL, {"a": 1})
        assert q.get_nowait() is events.OVERFLOW_SENTINEL
        # Draining does not restore a stream whose snapshot became stale.
        events.broadcast(CHANNEL, {"b": 2})
        assert q.empty()
        assert q in events._overflowed
        events.unsubscribe(CHANNEL, q)
        assert q not in events._overflowed
        fresh = events.subscribe(CHANNEL)
        try:
            assert fresh is not q
            events.broadcast(CHANNEL, {"b": 3})
            assert fresh.get_nowait() == {"b": 3}
            assert fresh.empty()
            assert q.empty()
        finally:
            events.unsubscribe(CHANNEL, fresh)

    asyncio.run(_scenario())


def test_normal_broadcast_does_not_emit_sentinel():
    """Sanity guard: under normal load, no overflow sentinels appear."""
    _reset_events()
    import events

    async def _scenario():
        events.capture_loop()
        q = events.subscribe(CHANNEL)
        for i in range(10):
            events.broadcast(CHANNEL, {"i": i})

        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
        assert events.OVERFLOW_SENTINEL not in seen
        assert seen == [{"i": i} for i in range(10)]

    asyncio.run(_scenario())


def test_actual_sse_stream_emits_overflow_then_unsubscribes():
    """The real route delivers queued data, signals stale state, and retires
    the subscriber before a reconnect can acquire a fresh snapshot."""
    _reset_events()
    import events
    from routes.channels import stream_channel_state

    async def scenario():
        events.capture_loop()
        response = await stream_channel_state(CHANNEL)
        iterator = response.body_iterator
        connected = await anext(iterator)
        assert json.loads(connected.removeprefix("data: ")) == {"type": "connected"}
        queue = next(iter(events._subscribers[CHANNEL]))
        queue.put_nowait({"type": "ping"})
        queue.put_nowait(events.OVERFLOW_SENTINEL)
        chunks = [chunk async for chunk in iterator]
        assert json.loads(chunks[0].removeprefix("data: ")) == {"type": "ping"}
        assert json.loads(chunks[1].removeprefix("data: ")) == {"type": "overflow"}
        assert chunks[2] == "retry: 100\n\n"
        assert CHANNEL not in events._subscribers
        assert queue not in events._overflowed

    asyncio.run(scenario())


def test_second_lifespan_loop_replaces_closed_loop():
    """A later lifespan cycle in the same process runs on a new loop;
    capture_loop must adopt it instead of keeping the first, now closed,
    loop (which would silently drop every broadcast)."""
    _reset_events()
    import events

    async def _first():
        events.capture_loop()

    async def _second():
        events.capture_loop()
        q = events.subscribe(CHANNEL)
        events.broadcast(CHANNEL, {"x": 1})
        assert q.get_nowait() == {"x": 1}
        events.unsubscribe(CHANNEL, q)

    asyncio.run(_first())
    asyncio.run(_second())


def test_stream_never_iterated_leaves_no_subscriber():
    """A client gone before the response body is first pulled never starts
    the generator, so its finally never runs: the route must not have
    subscribed yet. Once iterated and closed, the subscriber is gone too."""
    _reset_events()
    import events
    from routes.channels import stream_channel_state

    async def _scenario():
        events.capture_loop()
        resp = await stream_channel_state(CHANNEL)
        assert CHANNEL not in events._subscribers
        del resp

        resp = await stream_channel_state(CHANNEL)
        first = await resp.body_iterator.__anext__()
        assert "connected" in first
        assert CHANNEL in events._subscribers
        await resp.body_iterator.aclose()
        assert CHANNEL not in events._subscribers

    asyncio.run(_scenario())
