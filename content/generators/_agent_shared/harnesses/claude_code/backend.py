# SPDX-License-Identifier: MIT
"""Claude-owned preparation for the host ACP leg and the host terminal.

Both legs run on this machine. The ACP leg runs out of this cell's private
harness home; the terminal runs this machine's own ``claude`` out of a
per-account home (``cli_host.account_home``), where whatever ``claude
/login`` writes persists across runs and cells of the same Key manager
entry. API keys use the native apiKeyHelper setting, a private file in the
cell's harness home; a static setup token, when the selected account
carries one, rides its native environment route; otherwise the terminal's
user signs in inside the TTY. Both legs work in the cell's real workspace
path, and the terminal files its session record in the cell's harness home
(the per-account ``projects/<workspace>`` folder links there for the leg),
so the live tail, the export and the cell's session store read one record.
The terminal exports its own session when the leg ends. The run-end owner
closes the shared home after the ACP profile and selected-account leases.
"""
from __future__ import annotations
import re
from _agent_shared.logging import safe_log as _secret_log
from _agent_shared.secret_hygiene import write_log as _write_log


import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
import shlex
import sys
import time
import uuid

import gen
from collimate.transcript import serialize_transcript
from collimate.providers import STANDARD_MODEL_ENDPOINTS
from gen import Message

from ...agent_events import TRACE_FILE
from ...agent_config import full_claude_model_id, is_claude_model
from _agent_shared.transcript_merge import TERMINAL_TRANSCRIPT as _TRANSCRIPT_FILE

from .. import CommandsPolicy, PreparedHarness
from ...host_adapters import host_env, ensure_claude_adapter, resolve_claude_adapter_version
from ...harness_homes import harness_home
from ...acp_client import SpawnProfile, _log, spawn_host_adapter

# The claude project dir IS the cell workspace — the CLI edits the cell's
# files directly (live_files mirrors them to the Files tab).

# ──────────────────────────────────────────────────────────────────────
# The per-agent config dir, its env, and its auth. Shared by BOTH claude legs
# — the host ACP adapter's child process (prepare) and the host TTY — so the
# two never drift into different isolation stories for the same cell.
# ──────────────────────────────────────────────────────────────────────

# claude files a session's transcript under ``$CLAUDE_CONFIG_DIR/projects/
# <name>``, and by default derives <name> from the process cwd. Every
# Collimate run gets a FRESH workspace tmp path, so the default would scatter
# one cell's sessions across a new project dir per run — and both ``--resume``
# and the SDK's session load search the CURRENT project dir, so last run's
# transcript would be unreachable, which is the entire point of the per-cell
# harness home (§2.5). Pinning the name takes the cwd out of the equation; the
# home is already per-cell, so one constant name inside it is unambiguous.
# Documented (verified 2026-09-10 against code.claude.com/docs/en/env-vars):
# "Set together with CLAUDE_CONFIG_DIR to choose the projects/ directory name
# Claude Code stores that session's transcripts and auto memory under, in place
# of one derived from the working directory path" — and read ONLY from the
# environment claude is started from, never from a settings file's env block.
#
# NOT SET. When set, the host leg's CLI must honour it too, or the TTY and
# the engine land in DIFFERENT projects/ dirs and the live tail (it reads
# the current project dir) misses the TTY's transcript — and a
# `docker: none` run drives whatever `claude` the
# machine has, which may predate the variable. Both stay on the cwd slug; the
# readers (``claude_session_paths`` below) already try this name
# too, so setting it is a one-line change here once every leg's CLI knows
# it.
CLAUDE_PROJECT_DIR_NAME = "cell"

# Provider environment name used by the selected account's subscription token.
CLAUDE_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


def claude_agent_env(config_dir: str) -> dict:
    """The env every Collimate-spawned ``claude`` gets, whatever its auth —
    the host adapter's CLI child and the host TTY alike.

    ``config_dir`` is the CLI's home: this cell's own harness home for the
    ACP leg, the entry's per-account home for the TTY. The docs call
    ``CLAUDE_CONFIG_DIR`` the path "all settings, session history, and plugins
    are stored under", and with it set the CLI also writes ``.claude.json``
    and ``.credentials.json`` inside it. That single variable IS the isolation
    story: an agent run loads none of the user's real ``~/.claude`` — their
    settings and hooks, auto memory, skills, plugins, MCP server list, session
    store — and writes into none of it either.

    The three flags follow Anthropic's own multi-tenant guidance. Auto memory
    off so an agent neither loads the user's memory files nor leaves new ones
    in their account's name. Non-essential traffic off because an unattended
    agent has no one to benefit from analytics and feature-flag fetches, and
    they are per-run startup latency. The auto-updater off because a
    background self-update on the host would swap the binary out from under a
    live run; the user's own ``claude update`` decides its version.
    """
    return {
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


# The claude built-ins that run a shell command: Bash, Monitor (a watched
# background command), and PowerShell (the Windows twin).
CLAUDE_SHELL_TOOLS = ("Bash", "Monitor", "PowerShell")


def claude_command_settings(commands: str) -> dict:
    """The CLI's flag-settings tier (``--settings``; the adapter's ``settings``
    option reaches the CLI as the same flag) that sets ``claude`` to the
    grant's ``commands`` level under deny and ask. Empty under allow.

    The cell's project and local settings files load beside it and are
    trusted. This tier still decides the level, per the Claude Code docs:
    ``--settings`` "takes a key you set here over the same key in local,
    project, or user settings" (code.claude.com/docs/en/settings, Settings
    precedence), and permission rules from every tier combine, "evaluated in
    order: deny, then ask, then allow" — "deny rules from any scope are
    evaluated before allow rules" (code.claude.com/docs/en/permissions). A
    cell's allow rule therefore adds to this tier but cannot lift its ask or
    deny rule.

    An explicit ``ask`` rule on a tool makes EVERY call to it prompt in every
    permission mode: it outranks the built-in read-only allowlist (``ls``,
    ``cat``, ``git status``), acceptEdits' file-command auto-approval, any
    allow rule, and a skill's ``allowed-tools``. Only the sandbox's
    ``autoAllowBashIfSandboxed`` sits above it, so the sandbox is switched
    off; bypass and auto mode are taken off the table (the TUI's shift+tab
    cycles through them). Hooks and skills the cell ships run as Claude Code
    runs them."""
    if commands == "allow":
        return {}
    permissions: dict = {
        "deny" if commands == "deny" else "ask": list(CLAUDE_SHELL_TOOLS),
        "disableBypassPermissionsMode": "disable",
        "disableAutoMode": "disable",
    }
    settings: dict = {"permissions": permissions, "disableAutoMode": "disable"}
    if commands == "ask":
        settings["sandbox"] = {"enabled": False, "autoAllowBashIfSandboxed": False}
    return settings


def claude_cell_mcp_servers(workspace: str) -> dict:
    """The MCP servers the cell declares in its own ``.mcp.json`` (at the
    workspace root: ``{"mcpServers": {name: definition}}``), for the chat
    leg's ``mcpServers`` option; ``{}`` when the file is absent or invalid.

    The chat runs with ``--strict-mcp-config`` and these definitions ride
    ``--mcp-config`` verbatim (``${VAR}`` expansion and all, which the CLI
    applies there too) rather than through the CLI's project scope. That
    scope merges the ``.mcp.json`` of every folder from the workspace up to
    the root (the workspace's definition wins a shared name), and in a
    non-interactive session such as the adapter's it loads every server it
    finds without approval (code.claude.com/docs/en/mcp), so
    ``enabledMcpjsonServers`` narrows nothing there: on this host locus the
    folders above the workspace reach the user's home. An ``sdk`` entry is
    skipped: that type names a server living inside the SDK's own process,
    not one a file can describe."""
    try:
        with open(os.path.join(workspace, ".mcp.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {name: spec for name, spec in servers.items()
            if isinstance(spec, dict) and spec.get("type") != "sdk"}


def _claude_managed_settings_dir() -> str:
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    return "/etc/claude-code"


def claude_managed_rules_only(directory: str = "") -> bool:
    """Whether this machine's Claude Code managed settings set
    ``allowManagedPermissionRulesOnly`` — the CLI then drops every permission
    rule outside the managed tier, the ask rule of
    :func:`claude_command_settings` included. Reads ``managed-settings.json``
    and the ``managed-settings.d/`` drop-ins the CLI merges.

    File-based managed settings only: the flag delivered another way — MDM
    (the macOS ``com.anthropic.claudecode`` managed preferences, the Windows
    ``HKLM\\SOFTWARE\\Policies\\ClaudeCode`` key) or server-managed
    organization settings — is not seen here, so ``commands: ask`` is not
    lowered to deny on such a machine."""
    directory = directory or _claude_managed_settings_dir()
    paths = [os.path.join(directory, "managed-settings.json")]
    drop_ins = os.path.join(directory, "managed-settings.d")
    with contextlib.suppress(OSError):
        paths += [os.path.join(drop_ins, name) for name in sorted(os.listdir(drop_ins))
                  if name.endswith(".json")]
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("allowManagedPermissionRulesOnly") is True:
            return True
    return False


# The ACP legs bill the subscription through a static setup token; without
# one only the CLI surface runs, signed in inside its own TTY.
TOKEN_MISSING = (
    "Claude Code's Annotation and Chat surfaces bill the subscription through a setup token: run "
    "`claude setup-token` in a terminal on this machine and paste the token it prints into the "
    "selected Key manager entry. Without one only the CLI surface runs, signing in through claude "
    "on this machine.")


async def claude_subscription_token(*, required: bool = True) -> str:
    """The selected account's static ``claude setup-token`` token; ``""``
    when it carries none and ``required`` is false (the host TTY, whose user
    signs in through ``claude /login`` instead)."""
    account = await asyncio.to_thread(gen.account, "model")
    from ...secret_hygiene import remember_account
    remember_account(account)
    secrets = account.get("secrets") or {}
    token = secrets.get("oauth_token") or secrets.get("access_token") or secrets.get("token")
    if not isinstance(token, str) or not token:
        if required:
            raise RuntimeError(TOKEN_MISSING)
        return ""
    return token


_API_KEY_FILE = ".collimate-api-key"
_API_KEY_HELPER = ".collimate-api-key-helper.py"
_API_KEY_HELPER_SOURCE = '''import os, stat, sys
from pathlib import Path
try:
    path = Path(__file__).parent / ".collimate-api-key"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        state = os.fstat(stream.fileno())
        if not stat.S_ISREG(state.st_mode) or stat.S_IMODE(state.st_mode) != 0o600:
            raise ValueError("credential mode")
        value = stream.read(16385).strip()
    if not value or len(value) > 16384 or not all(32 <= byte <= 126 for byte in value):
        raise ValueError("credential shape")
    sys.stdout.buffer.write(value)
except Exception:
    sys.stderr.write("Selected credential unavailable\\n")
    sys.exit(1)
'''


def _write_api_key(home: str, key: str | None) -> None:
    from ...harness_files import write_native_file
    # Same anchored writer as other native secrets. These root-level files
    # never enter the retained projects/todos history whitelist.
    write_native_file(home, _API_KEY_FILE, None)
    write_native_file(home, _API_KEY_HELPER, None)
    if key is not None:
        write_native_file(home, _API_KEY_FILE, key.encode())
        try:
            write_native_file(home, _API_KEY_HELPER, _API_KEY_HELPER_SOURCE.encode())
        except BaseException:
            write_native_file(home, _API_KEY_FILE, None)
            raise


async def _native_auth(auth: str, home: str, *, key: str = "",
                       token_required: bool = True) -> tuple[dict, str]:
    """``(env, apiKeyHelper)`` for ``auth``: the API key as a private file in
    ``home`` and the helper command that prints it, or the subscription's
    setup token in the environment; under a subscription with no token and
    ``token_required`` false, nothing at all — the CLI's own sign-in in its
    home serves."""
    async def write(key):
        worker = asyncio.create_task(asyncio.to_thread(_write_api_key, home, key))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            worker.result()
            cleanup = asyncio.create_task(asyncio.to_thread(_write_api_key, home, None))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise

    # A failed or changed selection must not leave a previous API helper.
    await write(None)
    # Both auths bill the provider registry's Anthropic endpoint (the CLI
    # appends /v1/messages itself).
    base = {"ANTHROPIC_BASE_URL": STANDARD_MODEL_ENDPOINTS["anthropic"].removesuffix("/v1")}
    if auth == "claude_code":
        token = await claude_subscription_token(required=token_required)
        return ({**base, CLAUDE_TOKEN_ENV: token} if token else {}), ""
    if not key:
        raise RuntimeError("Claude Code: select an Anthropic API key in Key manager")
    from ...secret_hygiene import remember
    remember(key)
    await write(key)
    return base, shlex.join([sys.executable, os.path.join(home, _API_KEY_HELPER)])


@dataclass(frozen=True)
class _TerminalPrepared(PreparedHarness):
    # The CLI's per-account home (CLAUDE_CONFIG_DIR) and this cell's harness
    # home, where the leg's session record lands.
    home: str
    cell_home: str


async def prepare(ctx: dict, *, leg: str = "acp", request=None) -> PreparedHarness:
    """Own the host adapter's and the host TTY's distinct native preparation."""
    if leg == "acp":
        return await _prepare_host(ctx)
    if request is None:
        raise ValueError("Claude terminal preparation requires its native session request")
    model = (ctx.get("model") or "sonnet").strip()
    if not is_claude_model(model):
        raise RuntimeError(
            "claude-terminal mode runs the `claude` CLI and only accepts Claude "
            f"models (sonnet/opus/haiku/claude-*); got {model!r}"
        )
    from ...cli_host import account_home, cli_binary, leg_env
    binary = cli_binary("claude-code", ctx.get("host_harness"))
    auth = ctx["auth"]
    commands = ctx["commands"]
    if commands == "ask" and await asyncio.to_thread(claude_managed_rules_only):
        # The machine's managed tier drops the ask rule, so this machine's
        # claude would run read-only commands unasked; deny cannot be held on
        # a TTY here either.
        raise RuntimeError(
            "Claude Code's CLI surface cannot hold commands: ask on this machine: its Claude Code "
            "managed settings set allowManagedPermissionRulesOnly, which drops the ask rule that makes "
            "claude ask before each command; use commands: allow, or remove that setting")
    account = await asyncio.to_thread(gen.account, "model")
    config_home = await asyncio.to_thread(account_home, "claude-code", account)
    cell_home = await asyncio.to_thread(harness_home, "claude-code")
    auth_env, helper = await _native_auth(auth, cell_home, key=ctx["credentials"].key,
                                          token_required=False)
    signed_in = "API key helper" if helper else (
        "the entry's setup token" if auth_env else "its own sign-in in that home (`/login` in the TTY)")
    _secret_log(f"Claude terminal: this machine's {binary}, config dir {config_home} (one per Key "
                f"manager entry), {signed_in} — your ~/.claude is not read")
    env = leg_env(**claude_agent_env(config_home), **auth_env,
                  ANTHROPIC_MODEL=full_claude_model_id(model))
    policy = CommandsPolicy(commands, claude_command_settings(commands))
    argv = (binary, *claude_command_argv(commands, api_key_helper=helper))
    return _TerminalPrepared(policy, None, None, argv, env, model, config_home, cell_home)


async def _prepare_host(ctx: dict) -> PreparedHarness:
    """claude-code, HOST locus — the ``docker: none`` claude experience.

    The adapter is Collimate's own claude-agent-acp; the CLI it drives is the
    one bundled inside it, or — when the grant selects a usable
    ``claude`` on this machine — that machine copy (``CLAUDE_CODE_EXECUTABLE``).

    API authentication is a private-file helper in explicit native settings;
    the selected key is absent from the replacing child environment. Static
    setup tokens retain their separate native environment route.

    **Both** auths run out of this cell's own harness home
    (``CLAUDE_CONFIG_DIR`` — see ``claude_agent_env``), so an agent run reads
    and writes none of the user's real ``~/.claude``: not their settings and
    hooks, not their memory, skills, plugins or MCP server list, not their
    session store, and above all not their credential file. Subscription auth
    carries a static ``claude setup-token`` token rather than the user's OAuth
    chain (``claude_subscription_token`` has the full rationale — the
    credential file's refresh token rotates and is single-use, and the CLI
    rewrites it by rename, so concurrent sharers revoke each other's
    login)."""
    from ...agent_config import full_claude_model_id

    # Off the loop: a floated adapter resolves its version against the
    # registry, a network round trip.
    version = await asyncio.to_thread(resolve_claude_adapter_version)
    binary = await asyncio.to_thread(ensure_claude_adapter, version)
    # Our env REPLACES the child's, and every hop down there resolves node
    # through PATH: the adapter binary is a `#!/usr/bin/env node` script and
    # the Agent SDK inside it spawns node again for the CLI. So when
    # ensure_claude_adapter had to look past PATH for a node >= 22 (the
    # desktop-launcher case — the session PATH carries the distro's old node,
    # not the user's nvm one), the same dir must lead the child's PATH or the
    # grandchild silently falls back to the runtime we just rejected.
    # It is CLAUDE_CONFIG_DIR below — not a scrubbed HOME — that keeps the CLI
    # out of ~/.claude, the one part of that home this run must not touch.
    env = await asyncio.to_thread(host_env)
    home = await asyncio.to_thread(harness_home, "claude-code")
    env.update(claude_agent_env(home))
    # A usable `claude` selected on this machine: the adapter
    # drives THAT CLI (the adapter reads CLAUDE_CODE_EXECUTABLE in place of
    # the one bundled inside it), so the user's own `claude update` is what
    # decides the version the API sees. Otherwise the bundled CLI serves.
    found = ctx.get("host_harness") if ctx.get("runtime") == "standard" else None
    if found:
        env["CLAUDE_CODE_EXECUTABLE"] = found["path"]
        _log(f"standard: running claude {found['version']} at {found['path']} (this machine's own copy, "
             f"not managed by Collimate), driven by claude-agent-acp {version}")
    auth_env, helper = await _native_auth(ctx["auth"], home, key=ctx["credentials"].key)
    env.update(auth_env)
    route = "subscription token" if ctx["auth"] == "claude_code" else "API key helper"
    _log(f"Claude host: own config dir ({home}), {route} — your ~/.claude is not read")
    if ctx["model"]:
        env["ANTHROPIC_MODEL"] = full_claude_model_id(ctx["model"])
        # Give the native model catalog this exact ID. Otherwise the adapter
        # resolves a dated selection to a picker alias on session/load, losing
        # the selected pin before the host checks the resumed model.
        env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = env["ANTHROPIC_MODEL"]

    # The built-in tools are an ALLOWLIST (``tools``, which the adapter passes
    # in place of the whole ``claude_code`` preset): the CLI's built-in set
    # grows release to release (Monitor runs a shell, Workflow and the Cron /
    # worktree / messaging tools act beyond the turn), and a runner may float
    # the adapter to `latest`, so a list of what to block would let every
    # new tool through on a host locus with no container behind it. The CLI
    # ignores a name it does not know, so the list may name tools a given
    # release lacks. ``disallowedTools`` is a second layer the adapter honours
    # by name. Neither honors our ``permissionMode``
    # (verified against claude-agent-acp@0.70.0 and, from its source, 0.81.1:
    # the adapter sets its own resolved mode AFTER spreading our options — it
    # runs its own permission flow regardless), so ``request_permission`` in
    # the client is the third gate: it refuses execute/write/fetch kinds the
    # run didn't grant, any path outside the workspace, and any kind it does
    # not know. Native Bash follows the grant's ``commands`` axis — a shell
    # on THIS machine:
    #   * deny — Bash, Monitor and PowerShell are out of the allowlist,
    #     disallowed by name, and denied by rule;
    #   * ask — Bash is offered, and the flag-settings ask rule makes the CLI
    #     ask for every command (read-only ones too), in every mode, which
    #     the client gate turns into a card;
    #   * allow — Bash is offered and pre-approved (``allowedTools``).
    # Monitor stays out under every level: it runs its command in the
    # background, beyond the turn the grant was asked about.
    commands = ctx["commands"]
    notice = ""
    if commands == "ask" and await asyncio.to_thread(claude_managed_rules_only):
        commands = "deny"
        notice = ("commands: ask resolves to deny — this machine's Claude Code "
                  "managed settings set allowManagedPermissionRulesOnly, which "
                  "drops the ask rule that makes claude ask before each command")
    tools = ["Read", "Glob", "Grep", "Agent", "Skill",
             "EnterPlanMode", "ExitPlanMode"]
    disallowed: list = []
    if ctx["enable_file_tools"]:
        tools += ["Write", "Edit", "MultiEdit", "NotebookEdit"]
    else:
        disallowed += ["Write", "Edit", "MultiEdit", "NotebookEdit"]
    if commands == "deny":
        disallowed += list(CLAUDE_SHELL_TOOLS)
    else:
        tools += ["Bash"]
        disallowed += [t for t in CLAUDE_SHELL_TOOLS if t != "Bash"]
    if ctx["websearch"]:
        tools += ["WebFetch", "WebSearch"]
    else:
        disallowed += ["WebFetch", "WebSearch"]
    options: dict = {
        # The DEFAULT mode, and bypass ruled out for the whole session: the
        # client's request_permission is the gate (grant kinds, approvals
        # cards), and it only runs while the CLI keeps asking. The adapter
        # ignores this permissionMode today (it sets its own after spreading
        # ours), but the presets float `latest`, so we never ask for bypass;
        # 0.81.1 honours allowDangerouslySkipPermissions: false, which takes
        # bypass out of the mode list and the CLI's argv.
        "permissionMode": "default",
        "allowDangerouslySkipPermissions": False,
        # The cell's own ``.claude/`` settings, CLAUDE.md, rules, skills,
        # commands and agents load (``--setting-sources=project,local``); the
        # user tier is CLAUDE_CONFIG_DIR, the per-cell harness home, which is
        # Collimate's and stays out. The grant's commands level rides the
        # flag-settings tier (claude_command_settings), which outranks the
        # project files. MCP is strict (``--strict-mcp-config``; the adapter
        # spreads these options into the SDK query): the CLI starts the
        # servers of the cell's own ``.mcp.json``, passed below from that
        # file (claude_cell_mcp_servers says why not through the CLI's
        # project scope), and Collimate's bridge, which the adapter adds
        # from session/new and which wins a shared name. (User and local
        # scope live in the harness home's ``.claude.json``; the claude.ai
        # connectors need an OAuth scope the setup-token does not carry.)
        "settingSources": ["project", "local"],
        "strictMcpConfig": True,
        "tools": tools,
    }
    cwd = gen.workspace_path()
    cell_servers = await asyncio.to_thread(claude_cell_mcp_servers, cwd)
    if cell_servers:
        options["mcpServers"] = cell_servers
    options["disallowedTools"] = disallowed
    # The flag-settings tier also asks for the Reasoning block's text: since
    # claude 2.1.x the API OMITS thinking text unless the CLI requests
    # summaries (``showThinkingSummaries``; verified on 2.1.281 — without it
    # every thinking block arrives as a bare signature and the chat's
    # Reasoning block stays empty).
    options["settings"] = {"showThinkingSummaries": True,
                           **claude_command_settings(commands)}
    if helper:
        options["settings"]["apiKeyHelper"] = helper
    if commands == "allow":
        options["allowedTools"] = ["Bash"]
    if ctx["system_prompt"]:
        options["systemPrompt"] = {
            "type": "preset", "preset": "claude_code",
            "append": ctx["system_prompt"],
        }

    async def _spawn():
        return await spawn_host_adapter(
            "claude-code", [binary], env=env, cwd=cwd,
        )

    profile = SpawnProfile(
        locus="host", cwd=cwd, spawn=_spawn,
        # The level this harness really runs at, and why it differs from the
        # grant's.
        commands=commands, commands_notice=notice,
        session_meta={"claudeCode": {"options": options}},
        adapter_version=version,
        fold_system_prompt=False,        # the _meta seam carries it
        bind_host="127.0.0.1",           # host-reachable bridge, no docker need
        # ANTHROPIC_MODEL already carries the model; set_config_option would
        # inject a caveat-wrapped /model command (SpawnProfile.model_applied).
        model_applied=bool(ctx["model"]),
        model_value=full_claude_model_id(ctx["model"]) if ctx["model"] else "",
    )

    return PreparedHarness(CommandsPolicy(commands, options["settings"], notice),
        profile, None, (binary,), env, ctx["model"])


@asynccontextmanager
async def terminal_context(prepared, request, bridge):
    """Run this machine's ``claude`` in the cell's real workspace, its session
    record filed in the cell's harness home, and export that session when
    the leg ends."""
    from ...cli_host import link_cell_dir, sweep_dangling_links, unlink_cell_dir
    from ...terminal_runner import TerminalLaunch
    project = gen.workspace_path()
    config_home, cell_home = prepared.home, prepared.cell_home
    folder = os.path.join("projects", _claude_project_slug(project))
    target = os.path.join(cell_home, folder)
    harness_session_id = request.resume_harness_session_id or str(uuid.uuid4())
    link = ""

    def arm():
        nonlocal link
        # The CLI files this workspace's sessions under projects/<slug>; the
        # slug is this run's own workspace, so the link is this leg's alone.
        sweep_dangling_links(os.path.join(config_home, "projects"))
        link = link_cell_dir(config_home, folder, target)
        _seed_claude_config_host(config_home, project, prepared.commands.level)

    # Where the record lands: the cell's harness home through the link, or
    # the per-account home when a folder of the CLI's own holds the place.
    def store_dir() -> str:
        return cell_home if link else config_home

    try:
        # Same native arming boundary: a seed/register failure still exports
        # this exact session.
        await asyncio.to_thread(arm)
        command = list(prepared.argv)
        command += ["--session-id", harness_session_id]
        if request.extra_system:
            command += ["--append-system-prompt", request.extra_system]
        first_message = (request.first_message or "").strip()
        if first_message:
            command += ["--", first_message]

        async def capture():
            await _capture_chat(harness_session_id, store_dir=store_dir(), project=project)
        yield TerminalLaunch(
            container_id=None, argv=tuple(command), cwd=project,
            env=prepared.env, label=f"Claude · {prepared.model}",
            buttons=[{"id": "capture_chat", "label": "Capture Chat"}],
            ready_status="Terminal ready — open the cell to attach.",
            harness_session_id=harness_session_id,
            auxiliary_handlers={"capture_chat": capture},
        )
    finally:
        def teardown():
            try:
                messages, raw_jsonl = pull_claude_transcript(
                    harness_session_id, store_dir=store_dir(), project=project)
            except Exception as e:  # noqa: BLE001
                _secret_log(f"transcript read failed: {e}")
                messages, raw_jsonl = [], None
            try:
                _write_chatlog(messages)
            except Exception as e:  # noqa: BLE001
                _secret_log(f"chatlog export failed: {e}")
            try:
                _export_trace(raw_jsonl)
            except Exception as e:  # noqa: BLE001
                _secret_log(f"trace export failed: {e}")
            unlink_cell_dir(link, target)
        # Preserve the run end's existing bounded off-loop teardown await.
        await asyncio.to_thread(teardown)


def claude_command_argv(commands: str, *, api_key_helper: str = "") -> list:
    """The TUI flags for the grant's ``commands`` level.

    * allow — ``--dangerously-skip-permissions``: unattended, on this
      machine (what ``commands: allow`` grants a CLI on the host).
    * ask — claude's own prompt asks before every command (the human is at
      the TTY): the default mode (a subscription otherwise starts in auto)
      and the flag-settings tier of :func:`claude_command_settings`, whose ask
      rule reaches every command in every mode.
    * deny — the same, with the shell tools disallowed and denied instead.
      ``--restricted`` is not used: it loads no settings file but managed
      and ``--settings`` ones, so the cell's own settings would be dropped.

    Under deny and ask the settings files are the cell's project and local
    ones (``--setting-sources=project,local``); the user tier is the CLI's
    per-account home, which is Collimate's, not the cell's. The ``!``
    prefix stays: those are commands the human types."""

    settings = claude_command_settings(commands)
    if api_key_helper:
        settings["apiKeyHelper"] = api_key_helper
    if commands == "allow":
        # Native 2.1.281 migrates the old .claude.json acceptance into settings.
        # Carry it in the current flag-settings tier for this allowed TTY only.
        settings["skipDangerousModePermissionPrompt"] = True
        return ["--dangerously-skip-permissions", "--settings", json.dumps(settings)]
    argv = ["--permission-mode", "default", "--setting-sources=project,local",
            "--settings", json.dumps(settings)]
    if commands == "deny":
        argv += ["--disallowedTools", ",".join(CLAUDE_SHELL_TOOLS)]
    return argv


def _seed_claude_config_host(config_dir: str, project: str, commands: str) -> None:
    """Pre-answer claude's first-run flow so the TTY opens on a prompt.

    Without this the interactive TTY walks the user through the theme picker
    and the trust dialog on EVERY session. With ``CLAUDE_CONFIG_DIR`` set the
    flags claude consults live in ``$CLAUDE_CONFIG_DIR/.claude.json``, a file
    inside the TTY's per-account home, seeded here before the CLI starts.

    A merge, never a rewrite: the file holds what the CLI itself recorded —
    the account its ``/login`` signed in, earlier runs' state. Deliberately
    minimal — no account or identity fields: identity comes from the CLI's
    own sign-in, the selected token or the private helper, and a seeded
    account block would be a fabricated login record. Trusting ``project``
    is definitionally correct (it IS this cell's own workspace). The entries
    of earlier runs' workspaces that no longer exist are dropped, so the file
    does not grow by one per run. Bypass acceptance is carried by the allow
    TTY's native flag settings, not this state seed. Under deny and ask any
    tool the project's entry carries as allowed is withdrawn — a grant saved
    by a previous leg must not outlive it.

    The user's own ``claude`` never reads this file — without the env var it
    uses ``~/.claude.json`` — and this directory is not theirs in any case, so
    the write cannot perturb their setup.

    It is, however, shared with every other TTY on the same entry, whose live
    CLI writes this same file as it runs. So the seed treats a file it cannot
    parse as a possible half-written state rather than as junk (it is
    preserved, not clobbered), and writes through a temp file +
    ``os.replace`` so a concurrent reader sees either the old state or the
    new one and never a truncated one."""
    path = os.path.join(config_dir, ".claude.json")
    os.makedirs(config_dir, exist_ok=True)
    state: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            state = loaded
    except OSError:
        pass                          # no file yet — the run's first writer
    except ValueError:
        # Unparseable. An EMPTY file carries nothing, so seeding over it loses
        # nothing; anything else is the run's claude state (its trusted
        # projects, whatever the host leg's CLI recorded) caught mid-write or
        # genuinely corrupt — either way not ours to delete. Keep it under a
        # timestamped name and start fresh, so the leg proceeds and the copy
        # lasts for the rest of the run (the home is removed at process exit).
        try:
            if os.path.getsize(path) > 0:
                kept = f"{path}.corrupt-{int(time.time())}"
                os.replace(path, kept)
                _secret_log(f"{path} did not parse — kept it as {kept}, "
                        "seeding a fresh one")
        except OSError as exc:
            # A seed that cannot preserve still seeds: the leg opening on a
            # prompt matters more than the copy.
            _secret_log(f"could not set aside {path}: {exc}")
    state["hasCompletedOnboarding"] = True
    projects = state.get("projects")
    if not isinstance(projects, dict):
        projects = state["projects"] = {}
    for gone in [path for path in projects
                 if path != project and os.path.isabs(path) and not os.path.isdir(path)]:
        del projects[gone]
    entry = projects.setdefault(project, {})
    entry["hasTrustDialogAccepted"] = True
    entry["hasCompletedProjectOnboarding"] = True
    if commands != "allow":
        entry["allowedTools"] = []
    # Same directory as the target, so the replace is a rename within one
    # filesystem — atomic; pid-suffixed so two Collimate processes seeding the
    # same cell cannot share a temp file.
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _secret_log(f"Seeded {path} (onboarding done, {project} trusted)")


def _write_chatlog(messages: list) -> None:
    if not messages:
        return
    # Append-only merge: what the live tail already wrote stays; only the
    # turns it has not are added (transcript_merge).
    from _agent_shared.transcript_merge import write_merged_transcript
    added = write_merged_transcript(
        [Message(role=m["role"], content=m["content"]) for m in messages],
        path=_TRANSCRIPT_FILE)
    _secret_log(f"{_TRANSCRIPT_FILE}: {added} message(s) added from the claude session")


async def _capture_chat(harness_session_id: str, *, store_dir: str, project: str) -> None:
    import asyncio
    from datetime import datetime, timezone

    gen.phase("running", "Capturing chat…")
    snapshot = await asyncio.to_thread(
        _capture_snapshot, harness_session_id, store_dir=store_dir, project=project)
    folder = datetime.now(timezone.utc).strftime("captures/%Y%m%dT%H%M%S.%fZ")
    for name, content in snapshot.items():
        gen.update_file(f"{folder}/{name}", content)
    await gen.commit()
    _secret_log(f"Captured Claude session in {folder}")
    gen.phase("running", "Terminal ready — open the cell to attach.")


def _capture_snapshot(harness_session_id: str, *, store_dir: str, project: str) -> dict[str, str]:
    """Read the transcript and session export from this run's harness home."""
    messages, raw_jsonl = pull_claude_transcript(
        harness_session_id, store_dir=store_dir, project=project)
    snapshot: dict[str, str] = {}
    if messages:
        snapshot["transcript.json"] = serialize_transcript(
            [Message(role=m["role"], content=m["content"]) for m in messages]
        )
    if raw_jsonl:
        snapshot["claude_session.jsonl"] = raw_jsonl
        snapshot["trace.json"] = json.dumps(
            trace_from_claude_session(raw_jsonl), default=str)
    from ...secret_hygiene import refresh, scrub_persisted
    refresh()
    return {name: scrub_persisted(name, value) for name, value in snapshot.items()}


def _export_trace(raw_jsonl: str | None) -> None:
    """Persist the session's raw record as the terminal comes down: claude's own
    session jsonl (thinking + tool blocks intact — the fullest fidelity this
    mode has) plus its normalization into the shared trace vocabulary. Both are
    written on every run; any processing of them is post-hoc and external."""
    if not raw_jsonl:
        return
    _write_log("_agent/terminal/claude_session.jsonl", raw_jsonl, category="log")
    trace = trace_from_claude_session(raw_jsonl)
    _write_log(TRACE_FILE, json.dumps(trace, default=str), category="log")


def _claude_project_slug(project: str) -> str:
    """claude's project-dir name for a cwd: EVERY non-alphanumeric character
    becomes ``-`` (verified against the CLI itself — ``.`` and ``_`` are
    sanitized too, not just ``/``). Getting this wrong is silent: the export
    reader opens a path that never exists and returns empty."""
    import re
    return re.sub(r"[^a-zA-Z0-9]", "-", project)


def claude_session_paths(store_dir: str, project: str, harness_session_id: str) -> list:
    """Every place claude could have filed this session's jsonl inside its
    config dir, best candidate first.

    Today that is the cwd slug: neither leg sets ``CLAUDE_CODE_PROJECT_DIR_NAME``
    (see ``acp_client.CLAUDE_PROJECT_DIR_NAME`` for why — the host adapter's
    bundled CLI predates the variable, and the two legs must agree). The pinned
    name is tried second so the day it is switched on, or a CLI that honours it
    from a settings-file env block, cannot make the miss SILENT — the reader
    would otherwise open a path that never exists and export an empty
    transcript.
    """


    if not store_dir or not harness_session_id:
        return []
    names = (_claude_project_slug(project), CLAUDE_PROJECT_DIR_NAME)
    return [os.path.join(store_dir, "projects", name, f"{harness_session_id}.jsonl")
            for name in dict.fromkeys(names)]


def pull_claude_transcript(harness_session_id: str, *, store_dir: str, project: str):
    # Read EXACTLY this run's session jsonl, scoped to (this project dir, this
    # session id). The harness home persists across runs of the cell, so that
    # scoping is what keeps a stale prior run's conversation from being
    # exported in place of this one. ``store_dir`` is the config dir the
    # record lands in — a plain file read.
    raw = None
    for path in claude_session_paths(store_dir, project, harness_session_id):
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
            break
        except OSError:
            continue
    if raw is None:
        return [], None
    messages: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        msg = entry.get("message") or {}
        role = msg.get("role") or entry.get("type")
        text = _extract_text(msg.get("content"))
        if not text:
            continue
        messages.append({"role": role, "content": text})
    return messages, raw


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text") or "" for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def trace_from_claude_session(raw_jsonl: str) -> list:
    """Normalize a raw claude CLI session (``claude_session.jsonl``) — the
    fullest-fidelity record the claude-code terminal mode has, with thinking and
    tool_use/tool_result blocks intact — into the shared trace vocabulary. This
    is what the persisted ``_agent/trace.json`` carries."""
    trace: list = []
    for line in (raw_jsonl or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        msg = entry.get("message") or {}
        role = msg.get("role") or entry.get("type")
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                trace.append({"type": "user" if role == "user" else "text", "text": content})
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                trace.append({"type": "user" if role == "user" else "text",
                              "text": block["text"]})
            elif btype == "thinking" and block.get("thinking"):
                trace.append({"type": "thinking", "text": block["thinking"]})
            elif btype == "tool_use":
                trace.append({"type": "tool_use", "tool": block.get("name", ""),
                              "input": block.get("input")})
            elif btype == "tool_result":
                trace.append({"type": "tool_result",
                              "output": _flatten_blocks(block.get("content"))})
    return trace


def _flatten_blocks(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = [b.get("text", "") for b in content
               if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(t for t in out if t)
    return "" if content is None else str(content)


# The API's per-model Claude Code version floor, as the CLI reports it.
_VERSION_FLOOR = re.compile(
    r"Claude Code (\S+) does not support this model.*?version (\S+) or newer is required",
    re.IGNORECASE | re.DOTALL,
)


def explain_failure(text: str, *, runtime: str, cli_path: "str | None" = None) -> str:
    """The real fix for the CLI's version-floor refusal, whose own advice
    ("update") names the user's ``claude`` even when the turn ran the CLI
    bundled in Collimate's adapter, which ``claude update`` never touches;
    "" for any other failure."""
    m = _VERSION_FLOOR.search(text)
    if not m:
        return ""
    used, need = m.group(1).rstrip(".,;:"), m.group(2).rstrip(".,;:")
    if runtime == "standard":
        where = f" at {cli_path}" if cli_path else ""
        return (f"this run drives this machine's `claude`{where} ({used}): update that copy "
                f"(`claude update`) to reach {need}")
    return (f"the Claude Code {used} that failed is bundled in Collimate's claude-agent-acp "
            "adapter, which `claude update` does not touch — install a usable `claude` on PATH "
            "outside Snap to use this machine's CLI, or let the runner update packages so the run "
            "installs the latest adapter")
