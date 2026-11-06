# SPDX-License-Identifier: MIT
"""antigravity-acp's permission requests that run code without arriving as
kind ``execute``. Kept apart from ``backend`` (which imports the ACP client)
because the command gate resolves it when it is imported."""
from __future__ import annotations

# backend.SERVER_UNASKED_TOOLS (a test pins the two equal).
UNASKED = frozenset({"run_workflow", "manage_task", "schedule", "define_subagent",
                     "invoke_subagent", "manage_subagents"})


def server_exec_tool(session, tool_call, title: str) -> bool:
    """Whether a permission request names one of the server's tools that run
    code or start work without asking (``run_workflow``, ``manage_task``,
    ``schedule``, the subagent tools): the commands level's call, so it is
    refused under deny and carded under ask, whatever its kind."""
    raw = getattr(tool_call, "raw_input", None)
    raw = raw if isinstance(raw, dict) else {}
    names = {title.split(":", 1)[0].strip(), str(raw.get("toolName") or raw.get("tool") or "")}
    return bool(names & UNASKED)
