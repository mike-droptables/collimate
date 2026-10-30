# SPDX-License-Identifier: MIT
"""``gen.session`` — the live-session machinery the Agent generator drives.

Not the authoring contract: these six act on the run's inbox and its
:func:`gen.serve_chat` turn loop for the Agent generator
(``content/generators/_agent_shared/session``) and change with it, so they
are neither in ``gen.__all__`` nor in the API reference.

* :func:`submit_turn` queues a turn of the run's own (``source`` names who
  asked, ``thread_id`` its transcript); :func:`interrupt_turn` cuts the
  running turn short and returns ``False`` when none runs;
  :func:`chat_busy` says whether a turn runs or waits.
* :func:`emit_stream_event` pushes a typed event to the run's inbox.
* :func:`pop_config_request` takes the oldest config change a viewer sent.
* :func:`request_finish` ends the session gracefully after the current turn.

The implementation is :mod:`collimate.native`'s, beside ``serve_chat``,
whose state they share; this module re-exports exactly these names.
"""
from collimate.native import (  # noqa: F401
    chat_busy,
    emit_stream_event,
    interrupt_turn,
    pop_config_request,
    request_finish,
    submit_turn,
)

__all__ = [
    "chat_busy",
    "emit_stream_event",
    "interrupt_turn",
    "pop_config_request",
    "request_finish",
    "submit_turn",
]
