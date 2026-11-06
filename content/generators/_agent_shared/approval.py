# SPDX-License-Identifier: MIT
"""The agent's approval cards — what it asks the user before acting, shown
through the SDK's one approval primitive (:func:`gen.ask`).

Two callers: the ACP client's permission gate (``approvals: ask``, and every
command request under the grant's ``commands: ask``) and the bridge's
``test_generator`` tool under ``commands: ask``.
This module decides what a card shows: a command card shows the whole
command or code, and one too long to show whole is refused unshown. The
card, the ``awaiting_approval`` phase, the bounded wait and the refusal when
nobody can answer are :func:`gen.ask`'s.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import asyncio
import contextlib
import sys
from typing import Callable, Optional

import gen

# How long a card waits for the human before it is refused. The wallclock
# watchdog is suspended while it waits (phase ``awaiting_approval``), so this
# is the ONLY bound on the wait.
ASK_TIMEOUT_S = 600.0

# How often a caller's own cancel (``is_cancelled``) is checked while a card
# waits.
_CANCEL_POLL_S = 0.15

# A command card shows the whole command or code. One longer than this is
# refused, never carded cut: an approval would cover a tail nobody saw.
COMMAND_CARD_CAP = 16_000


def command_card_text(raw_input) -> str:
    """The whole text a command card shows: a command line or code as given
    (trimmed), any other input as its JSON."""
    from .agent_events import tool_summary
    return tool_summary(raw_input, sys.maxsize)


def command_too_long(text: str) -> str:
    """Why a command card's ``text`` is refused unshown ("" when it fits)."""
    if len(text) <= COMMAND_CARD_CAP:
        return ""
    return (f"the command is {len(text):,} characters, longer than an approval "
            f"card shows ({COMMAND_CARD_CAP:,}), so it was refused rather than "
            "shown cut")


def _log(msg: str) -> None:
    try:
        _secret_log(msg)
    except Exception:  # noqa: BLE001 — bridge-free context
        pass


async def ask_human(
    *, title: str, kind: str, options: "Optional[list]" = None, raw_input=None,
    command: bool = False,
    is_cancelled: "Optional[Callable[[], bool]]" = None, log_prefix: str = "",
) -> "str | None":
    """Show one approval card and wait for the answer.

    ``options`` are ``{"option_id", "name", "kind"}`` dicts (``None``: the
    SDK's Allow / Reject pair, ``allow_once`` / ``reject_once``);
    ``raw_input`` (the tool call's input — a path, a query) is summarised
    onto the card at the tool-event cap. A ``command`` card shows its
    ``raw_input`` whole (:func:`command_card_text`) and is refused without a
    card when that is longer than :data:`COMMAND_CARD_CAP`. Returns the
    chosen ``option_id``, or ``None`` when the card was refused (see
    :func:`gen.ask`) or ``is_cancelled()`` turned true while it waited."""
    detail: "str | None" = None
    if command:
        detail = command_card_text(raw_input)
        too_long = command_too_long(detail)
        if too_long:
            _log(f"{log_prefix}approval request refused — {too_long}")
            return None
    elif raw_input is not None:
        from .agent_events import TOOL_INPUT_CAP, tool_summary
        detail = tool_summary(raw_input, TOOL_INPUT_CAP)
    ask = gen.ask(title or "Permission", kind=kind, detail=detail,
                  options=options, timeout=ASK_TIMEOUT_S)
    if is_cancelled is None:
        return await ask
    # The card's task is this call's own: however this call ends — the turn
    # stopped (``is_cancelled``), or this coroutine itself was cancelled (the
    # ACP connection closing cancels its request handlers) — the card closes
    # with it. gen.ask closes a cancelled card as refused, restoring the phase.
    task = asyncio.ensure_future(ask)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=_CANCEL_POLL_S)
            if not task.done() and is_cancelled():
                _log(f"{log_prefix}approval request withdrawn — the turn stopped")
                return None
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def approve_command(tool: str, text: str) -> "str | None":
    """The bridge's command tools' gate under ``commands: ask``: card the
    command (or code) ``text`` with the SDK's Allow / Reject pair. Returns
    ``None`` when the user allowed it, else the refusal text the tool returns
    to the model."""
    too_long = command_too_long(command_card_text(text))
    if too_long:
        _log(f"{tool}: approval request refused — {too_long}")
        return (f"REFUSED: {too_long}. Split it into shorter commands the "
                "user can read in full.")
    chosen = await ask_human(
        title=f"Run {tool}", kind="execute", raw_input=text, command=True,
        log_prefix=f"{tool}: ",
    )
    if chosen == "allow_once":
        return None
    return ("REFUSED: the user did not approve this command (or no one was "
            "there to approve it) — do not retry it unchanged; ask the user "
            "or take another approach.")


__all__ = [
    "ASK_TIMEOUT_S", "COMMAND_CARD_CAP", "command_card_text", "command_too_long",
    "ask_human", "approve_command",
]
