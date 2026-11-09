# SPDX-License-Identifier: MIT
"""Copilot's permission requests that run a command without arriving as kind
``execute``. Kept apart from ``backend`` (which imports the ACP client) because
the command gate resolves it when it is imported."""
from __future__ import annotations


def write_bash_input(session, tool_call, title: str) -> bool:
    """Whether a permission request writes input into a running shell (the
    ``write_bash`` tool): what it types runs as a command, so it is the
    commands level's call like ``bash`` itself. Recognised by the tool's
    name and by its shape — a ``shellId`` and an ``input`` with no
    ``command`` — whatever kind Copilot labels it with. (Copilot 1.0.103's
    ACP session offers ``read_bash``/``stop_bash``/``list_bash`` but not
    ``write_bash``; this holds where a version offers it.)"""
    raw = getattr(tool_call, "raw_input", None)
    raw = raw if isinstance(raw, dict) else {}
    if "command" in raw:
        return False
    names = {title, str(getattr(tool_call, "tool_name", "") or ""), str(raw.get("tool") or "")}
    return "write_bash" in names or ("shellId" in raw and isinstance(raw.get("input"), str))
