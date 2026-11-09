#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Collimate's PreToolUse hook for Google's ``agy`` CLI and its ACP server —
the grant's ``commands`` level, enforced inside the harness.

Installed in the adapter image as ``/usr/local/bin/collimate-agy-hook``
(root-owned) and registered in the GLOBAL ``~/.gemini/config/hooks.json``,
which Collimate installs in a protected root-owned directory (``antigravity_backend``). One
matcher group per exec tool (:data:`EXEC_TOOLS`), so this script only ever
sees a tool that runs code. Both agy and the ACP server (``antigravity-acp``)
run it with the tool call as JSON on stdin (``{"toolCall": {"name", "args"},
"conversationId", "stepIdx", …}``) and read one JSON decision from stdout.

``collimate-agy-hook deny`` refuses every exec tool. ``collimate-agy-hook
ask`` answers ``force_ask``: agy's own prompt in the TUI, with any cached
"always allow" ignored. Both are the TERMINAL's gate. The ACP server runs the
same hooks.json (one container, one file). Its shell (``run_command`` /
``shell``) asks the client itself (``session/request_permission``, answered
by Collimate's commands gate, which refuses under deny and cards under ask,
and shows the attempt either way), so for those a hook started by the server
(its parent process is the server binary) answers ``allow``: "no objection",
not an approval — the server's own permission flow still runs. Every other
exec tool runs without asking the client (``run_workflow``'s script,
``manage_task`` input, ``schedule``), so this hook is its only gate below
allow: it denies them there, ``manage_task``'s read-only actions excepted.
(The backend also withholds them from the session below allow.)

``manage_task`` (agy's background-task tool, auto-allowed by agy and
matched by no ``command(*)`` rule) lists, inspects or stops background tasks,
which runs nothing new and is allowed at both levels. Its ``send_input``
action types into a running command's stdin, so it — and any action this
hook does not know — is gated like a command: refused under deny. Neither agy
nor the server prompts for this auto-allowed tool whatever the hook answers
short of ``deny`` (``force_ask`` and ``ask`` both run it), so under ask it is
refused too, and the human types into the task themselves.

Fails CLOSED. agy treats a hook that exits non-zero, prints something that
is not a decision, or overruns its timeout as a denial, but treats EMPTY
output as "no opinion" and falls through to the permission rules. So every
path here prints a decision, and anything unexpected prints ``deny``. STDLIB
ONLY.
"""
from __future__ import annotations

import json
import os
import sys

# The tools that run code: agy's shell (``run_command``) and the ACP server's
# (``shell``, whose permission requests also go by the exec tool names
# ``run_command`` / ``shell``), ``manage_task`` (its ``send_input`` action feeds
# a running command), notebooks, and the two browser/extension evaluators. The
# matcher groups in hooks.json list exactly these names.
EXEC_TOOLS = (
    "run_command",
    "shell",
    "manage_task",
    "notebook_execution",
    "execute_browser_javascript",
    "run_extension_code",
    "run_workflow",
    "schedule",
)

# The exec tools the ACP server asks the client about itself.
SERVER_ASKS = frozenset({"run_command", "shell"})

# The ACP server binary the image installs: the parent of a hook it runs.
SERVER_BIN = "/opt/agy-acp/agy_acp_server.par"

# ``manage_task`` actions that run nothing new: allowed without a card.
TASK_READ_ACTIONS = frozenset({"list", "status", "kill"})


def _decision(decision: str, reason: str) -> dict:
    return {"decision": decision, "reason": reason}


def _deny(reason: str) -> dict:
    return _decision("deny", reason)


def started_by_acp_server() -> bool:
    """Whether the process that ran this hook is the ACP server (its argv[0]
    is the installed binary, read from /proc; unreadable is "no")."""
    try:
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as fh:
            return fh.read().split(b"\0", 1)[0] == SERVER_BIN.encode()
    except OSError:
        return False


def decide(level: str, event: dict, *, acp_server: bool = False) -> dict:
    """The decision for one PreToolUse event at ``level``; ``acp_server``
    says the ACP server ran the hook (its client gate decides, see above)."""
    call = event.get("toolCall") if isinstance(event, dict) else None
    call = call if isinstance(call, dict) else {}
    name = str(call.get("name") or "")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    if level not in ("deny", "ask"):
        return _deny(f"unknown commands level {level!r}")
    if name == "manage_task" and args.get("Action") in TASK_READ_ACTIONS:
        return _decision("allow", "manages background tasks without running anything")
    if acp_server and name in SERVER_ASKS:
        return _decision("allow", "the ACP client's permission gate decides")
    if acp_server:
        return _deny(f"{name or 'this tool'} runs code without asking; it runs only under commands: allow")
    if level == "deny":
        return _deny("this runner's commands permission is deny")
    if name not in EXEC_TOOLS:
        return _deny(f"{name or 'this tool'} is not a command tool")
    if name == "manage_task":
        return _deny("agy cannot prompt for input typed into a task; in the "
                     "terminal, type into it yourself")
    return _decision("force_ask", "this runner asks before every command")


def main(argv: list) -> int:
    level = argv[0] if argv else ""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except Exception:  # noqa: BLE001 — an unreadable event is refused
        event = {}
    try:
        out = decide(level, event, acp_server=started_by_acp_server())
    except Exception as exc:  # noqa: BLE001
        out = _deny(f"hook error: {exc}")
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
