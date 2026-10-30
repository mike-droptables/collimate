# SPDX-License-Identifier: MIT
"""Harness command recognition, standing choices and session mode constraints."""
from __future__ import annotations
import shlex
import os
from typing import Any
from .harnesses import HARNESS, resolve

# claude-agent-acp's `exit-plan-*` permission options switch the session's
# permission MODE rather than answer one tool call, and their "clear context
# and …" variants also restart the CLI session under a new transcript id. Past
# such a switch the CLI stops calling canUseTool for what the new mode
# auto-approves, so the client's grant gate and approvals cards would go quiet
# — neither path offers them. `exit-plan-default` ("Yes, manually approve
# edits") keeps the mode.
def _switches_permission_mode(option_id: str) -> bool:
    oid = option_id or ""
    return oid.startswith("exit-plan-") and oid != "exit-plan-default"

_CLAUDE_UNASKED_MODES = frozenset({"bypassPermissions", "auto", "acceptEdits"})

_QWEN_MODES = {"deny": frozenset({"default", "plan", "auto-edit"}),
               "ask": frozenset({"default", "plan"})}

_PI_MODES = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})

def _claude_mode_allowed(commands: str, value) -> bool:
    if commands == "allow":
        return True
    unasked = (_CLAUDE_UNASKED_MODES if commands == "ask"
               else _CLAUDE_UNASKED_MODES - {"acceptEdits"})
    return value not in unasked

def _cline_mode_allowed(commands: str, value) -> bool:
    # cline's session options: the plan/act mode select (neither runs a
    # command unasked) and the ``auto_approve`` switch, whose ``true``
    # approves every tool call — commands included.
    return commands == "allow" or value not in (True, "true")

def _codex_mode_allowed(commands: str, value) -> bool:
    # Below allow only codex-acp's ``read-only`` preset: ``agent`` hands
    # escalations to codex's model reviewer and ``agent-full-access`` asks
    # nothing — and the container's requirements lock fails their turns.
    return commands == "allow" or value == "read-only"

def _goose_mode_allowed(commands: str, value) -> bool:
    # Under ask only ``approve`` (asks before every tool call) or ``chat``
    # (no tools): auto asks nothing, smart_approve lets its classifier
    # decide. deny has no shell tool in any mode.
    return commands != "ask" or value in ("approve", "chat")

def _opencode_mode_allowed(commands: str, value) -> bool:
    # opencode's modes are its primary agents, and every built-in agent
    # takes the global permission rules after its own, so the config's
    # ``bash`` rule holds in all of them. An agent the cell defines in its own
    # project config is the cell's (trusted) choice.
    return True

def _qwen_mode_allowed(commands: str, value) -> bool:
    allowed = _QWEN_MODES.get(commands)
    return allowed is None or str(value) in allowed

def _pi_mode_allowed(commands: str, value) -> bool:
    return str(value) in _PI_MODES

def _any_mode(commands: str, value) -> bool:
    # hermes's session modes are its edit-approval policies (default,
    # accept_edits, dont_ask); none runs a command — its commands level is
    # its managed toolsets and approvals mode.
    return True

def _vibe_mode_allowed(commands: str, value) -> bool:
    # Vibe's ``auto-approve`` mode bypasses every tool permission (its shell
    # included); the others (ask, accept-edits, plan) never run a command
    # unasked. Below allow the environment also disables auto-approve.
    return commands == "allow" or value != "auto-approve"

def _copilot_mode_allowed(commands: str, value) -> bool:
    # Copilot's ``allow_all`` option ("on") approves every tool call, commands
    # included, and so does its autopilot mode; agent and plan do not.
    return commands == "allow" or (value not in (True, "true", "on")
                                   and not str(value).endswith("#autopilot"))

def _no_modes(commands: str, value) -> bool:
    # A harness that offers no mode a run may switch to takes no mode change.
    return False

def _antigravity_mode_allowed(commands: str, value) -> bool:
    # antigravity-acp's modes: default (asks), auto_edit and yolo (approve
    # edits, or everything, without asking). Only default keeps every request
    # at the client's grant gate.
    return value == "default"

def _is_mcp_tool_call(tool_call) -> bool:
    """An MCP server's tool, by the adapter's ``_meta.claudeCode.mcpServer``
    tag (claude-agent-acp 0.81+, from the CLI's own tool routing). Never by
    the title: a built-in's title can come from model text, so an
    ``mcp__…`` title proves nothing."""
    meta = getattr(tool_call, "field_meta", None) or {}
    cc = meta.get("claudeCode") if isinstance(meta, dict) else None
    return isinstance(cc, dict) and bool(cc.get("mcpServer"))


def _tool_call_id(tool_call) -> str:
    return str(getattr(tool_call, "tool_call_id", "") or "")


def _goose_tool_name(meta) -> str:
    """goose's own tool name, from a tool call's ``_meta.goose.toolCall``."""
    goose = meta.get("goose") if isinstance(meta, dict) else None
    call = goose.get("toolCall") if isinstance(goose, dict) else None
    name = call.get("toolName") if isinstance(call, dict) else None
    return str(name or "")


def _goose_shell(session: Any, tool_call, title: str) -> bool:
    # goose 1.50 asks for its shell with kind "other", titled
    # "shell · <first line>" (rawInput {command, timeout_secs?}); its
    # tool_call update names the tool in _meta.goose.toolCall.toolName.
    if title == "shell" or title.startswith("shell ·"):
        return True
    name = (_goose_tool_name(getattr(tool_call, "field_meta", None))
            or session._tool_names.get(_tool_call_id(tool_call), ""))
    return name == "shell"


def _codex_sandbox_widening(session: Any, tool_call, title: str) -> bool:
    # codex's "Additional sandbox permissions" request widens what a command
    # may do — it is part of running that command.
    return title == "Additional sandbox permissions"


def _claude_monitor(session: Any, tool_call, title: str) -> bool:
    # claude's Monitor runs a shell command (Bash-equivalent), kind "other".
    return title == "Monitor"


def _opencode_shell_directory(session: Any, tool_call, title: str) -> bool:
    # opencode's shell asks ``external_directory`` (kind other) before a
    # command whose arguments or workdir reach outside the workspace, rawInput
    # {command, directories, patterns}; its file tools' ask of that name
    # carries {filepath, parentDir} and no command.
    cmd = _raw_input(tool_call).get("command")
    return isinstance(cmd, str) and bool(cmd.strip())


def _pi_confirm(session: Any, tool_call, title: str) -> bool:
    # Collimate's pi gate extension asks through ctx.ui.confirm, which pi-acp
    # forwards as request_permission: toolCallId "pi-ui-<uuid>", kind other,
    # our confirm title, rawInput {method: "confirm", title, message: <command>}.
    return (title == "collimate:command"
            and _tool_call_id(tool_call).startswith("pi-ui-"))


# Per-harness recognisers for a shell request that does NOT arrive as kind
# ``execute``: ``(session, tool_call, title) -> bool``.
_COMMAND_RECOGNISERS = {name: tuple(resolve(fn) for fn in row.commands.recognisers) for name, row in HARNESS.items()}

# Option ids a harness PERSISTS as a standing rule, whatever kind it labels
# them with. Never auto-picked, never offered on a commands: ask card —
# alongside every allow_always-kind option.
_PERSISTING_OPTION_IDS = {name: row.commands.persisting_options for name, row in HARNESS.items() if row.acp_argv}


# Option ids that switch a harness's permission MODE, by the mode they
# switch to: qwen's ``proceed_always`` on an edit ("Allow All Edits") or a
# plan exit ("auto-accept edits") moves the session to auto-edit, which runs
# ``sed -i`` unasked. Offered only where acp_run.mode_allowed keeps that mode
# within the grant.
_MODE_SWITCH_OPTIONS = {name: dict(row.commands.mode_switch_options) for name, row in HARNESS.items() if row.acp_argv}


def _persists(session: Any, option) -> bool:
    return str(getattr(option, "option_id", "") or "") in _PERSISTING_OPTION_IDS.get(
        session.backend, frozenset())


def _raw_input(tool_call) -> dict:
    raw = getattr(tool_call, "raw_input", None)
    return raw if isinstance(raw, dict) else {}


def _command_text(session: Any, tool_call) -> str:
    """The full command line(s) a command request would run, for its card
    and for matching the terminal/create it covers: pi's confirm carries it
    in ``message``, cline batches several in ``commands`` (one approval
    covers them all, so the card shows every one), antigravity's
    ``run_command`` in ``CommandLine``, the rest in ``command`` (a string,
    or an argv list). "" when the request carries none — its card then
    shows the whole raw input (:meth:`_CollimateClient._ask_human`)."""
    raw = _raw_input(tool_call)
    if _pi_confirm(session, tool_call, str(getattr(tool_call, "title", "") or "").strip()):
        return str(raw.get("message") or "")
    cmds = raw.get("commands")
    if isinstance(cmds, list) and cmds:
        return "\n".join(str(c) for c in cmds)
    cmd = raw.get("command")
    if isinstance(cmd, list):
        return shlex.join(str(c) for c in cmd)
    if isinstance(cmd, str):
        return cmd
    line = raw.get("CommandLine")
    return line if isinstance(line, str) else ""


_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})


def _same_command(approved: str, command: str, args) -> bool:
    """Whether a terminal/create runs the command a request approved —
    verbatim, or wrapped in a shell (a harness may send ``/usr/bin/bash -lc
    '<cmd>'`` with no args; others pass ``sh`` with ``["-c", line]``)."""
    want = approved.strip()
    argv = [str(command)] + [str(a) for a in args or []]
    candidates = {" ".join(argv).strip()} if args else {str(command).strip()}
    if not args:
        try:
            argv = shlex.split(str(command))
        except ValueError:
            argv = []
    if (len(argv) == 3 and os.path.basename(argv[0]) in _SHELLS
            and argv[1] in ("-c", "-lc")):
        candidates.add(argv[2].strip())
    return want in candidates


def _is_command_request(session: Any, tool_call) -> bool:
    """Whether a permission request asks to run a shell command — the thing
    the grant's ``commands`` axis gates. Kind ``execute`` always is; some
    harnesses ask for their shell under another kind, which
    :data:`_COMMAND_RECOGNISERS` names per backend."""
    if str(getattr(tool_call, "kind", "") or "") == "execute":
        return True
    title = str(getattr(tool_call, "title", "") or "").strip()
    return any(fn(session, tool_call, title)
               for fn in _COMMAND_RECOGNISERS.get(session.backend, ()))


def _is_mcp_tool_approval(session: Any, meta: dict) -> bool:
    """Whether a permission request approves a tool call rather than a
    command, by the harness's ``tool_approval`` hook — the ``commands`` level
    does not decide it. ``meta`` is the request's _meta (the router spreads
    it into the handler's kwargs). Resolved per call: the hook lives in the
    harness's backend, which imports the ACP client this module serves."""
    row = HARNESS.get(session.backend)
    hook = row.hooks.tool_approval if row else None
    return bool(hook and resolve(hook)(session, meta))


def mode_allowed(backend: str, commands: str, value) -> bool:
    row = HARNESS.get(backend)
    return bool(row and row.acp_argv and resolve(row.commands.mode_rule)(commands, value))
