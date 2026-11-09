"""Helpers for fire-and-forget asyncio tasks.

`asyncio.create_task` returns a Task whose exception is silently lost
unless someone awaits it or attaches a done-callback. ``track_task``
does both jobs: it keeps a strong reference (so the task isn't GC'd
mid-flight) and logs any unhandled exception that escapes the coroutine.
"""

import asyncio
import logging
from typing import Coroutine, Set

logger = logging.getLogger(__name__)

_BACKGROUND_TASKS: Set[asyncio.Task] = set()


def track_task(coro: Coroutine, *, name: str | None = None) -> asyncio.Task:
    """Create a task, retain a strong reference, log unhandled exceptions.

    Use instead of ``asyncio.create_task`` for any background coroutine
    whose result is never awaited. Cancellation is treated as expected
    and not logged.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception(
            "background task %r raised", task.get_name(), exc_info=exc
        )
