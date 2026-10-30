"""Run every "off-loop" helper inline: the pyodide runtime has one thread.

The app pushes sync sqlite/filesystem work through ``asyncio.to_thread``,
``loop.run_in_executor`` and (via Starlette) ``anyio.to_thread.run_sync``.
None of those can spawn a worker here, so all three become "call it now and
hand back a finished awaitable". Besides making the app runnable at all,
this makes the single event loop deterministic: a route's DB write finishes
before the next await, exactly in program order.

Installed once, before ``import main`` — the app binds ``asyncio.to_thread``
by attribute lookup at call time, so patching the module attribute is enough.
"""
from __future__ import annotations

import asyncio
import functools


async def _to_thread(func, /, *args, **kwargs):
    return func(*args, **kwargs)


def _run_in_executor(self, executor, func, *args):
    fut = self.create_future()
    try:
        fut.set_result(func(*args))
    except BaseException as exc:  # noqa: BLE001 — mirrors a worker's future
        fut.set_exception(exc)
    return fut


async def _anyio_run_sync(func, /, *args, abandon_on_cancel=False, cancellable=None, limiter=None):
    return func(*args)


def install() -> None:
    asyncio.to_thread = _to_thread
    # The loop pyodide runs (pyodide.webloop.WebLoop) inherits from
    # AbstractEventLoop; patch the base and the concrete class so whichever
    # the lookup resolves to is ours.
    asyncio.AbstractEventLoop.run_in_executor = _run_in_executor
    asyncio.BaseEventLoop.run_in_executor = _run_in_executor
    try:
        from pyodide.webloop import WebLoop
        WebLoop.run_in_executor = _run_in_executor
    except ImportError:
        pass
    # Starlette's run_in_threadpool / iterate_in_threadpool call
    # ``anyio.to_thread.run_sync`` through the module attribute.
    import anyio.to_thread
    anyio.to_thread.run_sync = functools.wraps(anyio.to_thread.run_sync)(_anyio_run_sync)


def install_persistence() -> None:
    """Use the app's synchronous persistence path in the one-thread runtime.

    Every mutation is durable before returning; the app's existing explicit
    flush barrier remains unchanged and also runs on lifespan shutdown.
    """
    import state

    def start(*, delay=0.25):
        if state._persistence_writer is not None:
            raise RuntimeError("Cannot replace an active persistence writer")

    def stop():
        state.flush_persist_state()

    state.start_persistence_writer = start
    state.stop_persistence_writer = stop


def install_validation(*, drain=None) -> None:
    """Schedule unchanged advisory validation work on the sole event loop."""
    from collections import OrderedDict
    import threading
    import time
    import user_validation

    if drain is None:
        def drain(pending):
            from pyodide.ffi import run_sync
            run_sync(pending)

    class InlineValidationService(user_validation.ValidationService):
        def __init__(self):
            self.condition = threading.Condition()
            self.stopping = False
            self.scan_due = None
            self.pending = OrderedDict()
            self.seen = {}
            self.results = OrderedDict()
            self.loop = asyncio.get_running_loop()
            self.handle = None
            self.busy = False
            self.idle = None

        def _arm(self):
            if self.handle is not None:
                self.handle.cancel()
                self.handle = None
            if self.stopping or self.busy:
                return
            dates = list(self.pending.values()) + ([self.scan_due] if self.scan_due is not None else [])
            if dates:
                self.handle = self.loop.call_later(max(0, min(dates) - time.monotonic()), self._step)

        def wake(self, channel=None, cell_id=None):
            super().wake(channel, cell_id)
            self._arm()

        def read(self, channel, cell_id):
            result = super().read(channel, cell_id)
            self._arm()
            return result

        def _step(self):
            self.handle = None
            if self.stopping or self.busy:
                return
            now = time.monotonic()
            scan = self.scan_due is not None and self.scan_due <= now
            key = next((k for k, due in self.pending.items() if due <= now), None)
            if scan:
                self.scan_due = None
            elif key is not None:
                self.pending.pop(key)
            else:
                self._arm()
                return
            self.busy = True
            self.idle = self.loop.create_future()
            try:
                if scan:
                    self._scan()
                else:
                    self._validate(key)
            except Exception:
                user_validation.logger.exception('Background user validation failed for %s', key)
            finally:
                self.busy = False
                self.idle.set_result(None)
                self._arm()

        def close(self):
            self.stopping = True
            if self.handle is not None:
                self.handle.cancel()
                self.handle = None
            self.pending.clear()
            # JSPI lets unrelated callbacks run while verification is suspended.
            # Drain the active worker before lifecycle cleanup releases its data.
            if self.busy:
                drain(self.idle)
            self.results.clear()
            self.seen.clear()

    user_validation.ValidationService = InlineValidationService
