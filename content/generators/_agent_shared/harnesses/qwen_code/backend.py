# SPDX-License-Identifier: MIT
"""Qwen Code terminal mode — the ``qwen`` CLI's own TUI in its container.

The ``terminal`` surface for backend=qwen-code, attached through the
``.terminal`` renderer over the bind-mounted ``/workspace``
(``gen.live_files`` keeps the Files tab in sync). Terminal session tier C:
like the rest of its Gemini-CLI lineage this is always a FRESH terminal —
``resume_harness_session_id`` and ``first_message`` are accepted for runner-signature uniformity and ignored,
and there is no session-store exporter to backfill from.

Container + auth arrangement mirrors the ACP profile
(:func:`prepare`) on purpose: the same ``ensure_env`` cache
key and harness-home volume, so whichever leg starts first, both resolve to
one container and native private authentication settings, so the TUI bills the same
provider the engine-driven turns do.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import asyncio
from contextlib import asynccontextmanager
import json
import re

from collimate.providers import STANDARD_MODEL_ENDPOINTS

from .. import CommandsPolicy, PreparedHarness
from ...harness_files import write_native_file
from ...harness_homes import harness_home
from ...acp_client import SpawnProfile, spawn_container_adapter

_HOME = "/home/coder"

# qwen-code's GLOBAL context file (shared/docs/agent-runtime.md, instruction context) — the instruction seam the
# session context rides. ``/home/coder/.qwen`` is ALWAYS the per-cell harness
# home for this backend (there is no subscription mount), so the file is
# Collimate's own. Verified live against the pinned qwen-code 0.22.2: the
# gemini-CLI lineage reads ``Storage.getGlobalQwenDir()/QWEN.md`` for any cwd,
# independent of folder trust.
QWEN_CONTEXT_PATH = f"{_HOME}/.qwen/QWEN.md"

# The SYSTEM settings file (``QWEN_CODE_SYSTEM_SETTINGS_PATH``) — the scope
# whose single values beat user and workspace settings, and whose
# permission rules the others can only add to. Written as root outside the
# agent's home, so nothing the agent runs or edits can loosen it.
SYSTEM_SETTINGS_PATH = "/etc/collimate/qwen-settings.json"

_WEB_TOOLS = ["web_search", "web_fetch", "google_web_search"]

def auth_settings(provider: str, protocol: str, key: str | None, model: str) -> dict:
    """Qwen's native private settings auth/model fields: ``protocol`` is the
    adapter's native id for ``provider`` (qwen selects a protocol, and an
    OpenAI-compatible provider by its base URL)."""
    auth = {'selectedType': protocol}
    if key:
        auth['apiKey'] = key
    if protocol == 'anthropic':
        auth['baseUrl'] = STANDARD_MODEL_ENDPOINTS[provider].removesuffix('/v1')
    elif protocol != 'gemini':
        auth['baseUrl'] = STANDARD_MODEL_ENDPOINTS[provider]
    return {'security': {'auth': auth}, 'model': {'name': model}}


def qwen_volumes() -> dict:
    """Mount the per-cell harness home at ``~/.qwen`` — the ACP profile and
    the TTY runner share it, so both legs see the same home."""
    from ...harness_homes import harness_home

    return {harness_home("qwen-code"): {"bind": f"{_HOME}/.qwen", "mode": "rw"}}


# qwen's safe mode (``QWEN_CODE_SAFE_MODE``) and bare mode
# (``QWEN_CODE_SIMPLE``) drop the permission rules of every settings scope,
# the system file's included, and qwen reads both from its environment —
# which a ``.env`` file (the workspace's, or ``~/.qwen/.env`` in the harness
# home) may fill: neither is on qwen's list of keys a ``.env`` cannot set.
# Every leg's process env names both off. A value already set and non-empty
# wins over a ``.env`` at startup (qwen treats an empty one as unset, hence
# "0"); the ACP server's ``reloadEnvironment`` as each session opens writes
# ``.env`` keys over it, so a session the same process opens later is built
# under the ``.env``'s values. ``--no-safe-mode`` pins safe mode off on the
# command line (qwen reads the flag before the env). Bare mode has no such
# flag: under deny ``--exclude-tools`` adds shell and Agent deny rules after
# qwen drops the settings' rules, and the ACP leg opens one session per
# process (``one_session_per_process``). The upstream issue asks for both
# keys on the exclusion list. Source-verified at qwen-code 0.23.3.
MODE_ENV = {"QWEN_CODE_SAFE_MODE": "0", "QWEN_CODE_SIMPLE": "0"}


def mode_flags(commands: str) -> list:
    """The command-line flags every qwen leg runs with at ``commands``:
    safe mode off, and under deny shell and Agent excluded by rules that
    bare mode does not drop."""
    flags = ["--no-safe-mode"]
    if commands == "deny":
        flags += ["--exclude-tools", "run_shell_command,agent"]
    return flags


def approval_mode(commands: str) -> str:
    """The approval mode for a ``commands`` level. ``yolo`` ignores ask rules,
    ``auto`` lets an LLM classifier approve and ``auto-edit`` approves
    ``sed -i`` edits unasked, so only allow gets yolo; deny and ask run in
    ``default``, where the rules below decide."""
    return "yolo" if commands == "allow" else "default"


# The tool whose delegates can run commands without asking: a subagent
# whose definition sets its own ``approvalMode`` (yolo ignores ask rules) —
# the cell's, or one written into the harness home's ``~/.qwen/agents``.
# Teammates are subagents too. Not loaded under ask or deny. Ordinary
# in-process delegates inherit shell deny, but external ACP definitions can
# launch their configured executable before its protocol negotiation.
# ``create_sub_session`` and ``workflow`` run in the parent's mode;
# cron and loop re-prompt the same session.
# Source-verified at qwen-code 0.23.3.
_UNATTENDED_DELEGATE_TOOLS = ("agent",)


def system_settings(commands: str, *, websearch: bool,
                    mcp_servers: "dict | None" = None) -> dict:
    """The system settings for a leg: the ``commands`` level as a
    ``permissions`` rule on ``run_shell_command`` (deny removes it and the
    Agent dispatch tool; shell deny also covers ``monitor``; ask requires
    confirmation whatever any allow rule or saved "always" says, and denies the subagent tool;
    allow adds no rule), and the web tools excluded unless the run grants
    web search.

    Rules from every scope are unioned and deny beats ask beats allow, so the
    cell's own ``.qwen/settings.json`` adds rules but cannot lift these. What
    qwen-code runs by its own design is its trust model: hooks the cell
    ships and the managed auto-memory and auto-dream agents (forked in yolo).

    ``mcp_servers`` rides here rather than in the workspace: system-scoped
    servers are never held for approval."""
    settings: dict = {"tools": {"exclude": [] if websearch else list(_WEB_TOOLS)}}
    if commands == "deny":
        settings["permissions"] = {"deny": ["run_shell_command", "agent"]}
    elif commands == "ask":
        settings["permissions"] = {"ask": ["run_shell_command"],
                                   "deny": list(_UNATTENDED_DELEGATE_TOOLS)}
    if mcp_servers:
        settings["mcpServers"] = mcp_servers
    return settings


def write_system_settings(env, settings: dict) -> str:
    """Write the system settings file as root; returns its path for
    ``QWEN_CODE_SYSTEM_SETTINGS_PATH``."""
    from ...cli_container import write_root_file

    write_root_file(env, SYSTEM_SETTINGS_PATH, json.dumps(settings),
                    what="writing qwen-code's system settings")
    return SYSTEM_SETTINGS_PATH


def model_matches(value: str, target: str) -> bool:
    """Match native auth suffixes and ACP runtime snapshot prefixes."""
    while value.startswith('$runtime|'):
        rest = '|'.join(value.split('|')[2:])
        if not rest:
            break
        value = rest
    matched = re.fullmatch(r'(.+)\(([A-Za-z0-9_-]+)\)', value)
    return (matched.group(1) if matched else value) == target


async def prepare(ctx: dict, *, leg: str = 'acp') -> PreparedHarness:
    """Qwen's renderer: its private auth/model settings, root-owned system
    settings for the commands level, and its process for each leg."""
    from collimate.providers import PROVIDER_DEFAULT_MODELS
    from ...cli_container import ensure_env

    provider = ctx['provider']
    model = (ctx.get('model') or '').strip() or PROVIDER_DEFAULT_MODELS.get(provider, '')
    home = await asyncio.to_thread(harness_home, 'qwen-code')
    await asyncio.to_thread(write_native_file, home, 'settings.json', json.dumps(auth_settings(
        provider, ctx['native_provider'].id, ctx['credentials'].key, model)).encode())
    volumes = await asyncio.to_thread(qwen_volumes)
    env_obj = await ensure_env('qwen-code', build_dir='qwen-code', volumes=volumes)
    policy = CommandsPolicy(ctx['commands'], system_settings(
        ctx['commands'], websearch=bool(ctx.get('websearch'))))
    path = await asyncio.to_thread(write_system_settings, env_obj, policy.native)
    env_vars = {'QWEN_CODE_SYSTEM_SETTINGS_PATH': path, **MODE_ENV}
    mode = approval_mode(policy.level)
    argv = ['qwen', *(['--acp', '--experimental-skills'] if leg == 'acp' else []),
            *mode_flags(policy.level), '--approval-mode', mode]
    profile = None
    if leg == 'acp':
        # Native 0.23.3 awaits MCP discovery and model tool registration
        # during initialization with this setting, before the first prompt.
        env_vars['QWEN_CODE_LEGACY_MCP_BLOCKING'] = '1'
        async def spawn():
            return await spawn_container_adapter('qwen-code', env_obj, argv, env_vars=env_vars)
        profile = SpawnProfile(locus='container', cwd='/workspace', spawn=spawn,
            commands=policy.level, env_obj=env_obj, session_mode=mode, model_applied=True)
    return PreparedHarness(policy, profile, env_obj, tuple(argv), env_vars, model)


@asynccontextmanager
async def terminal_context(prepared: PreparedHarness, request, bridge):
    """Bind Qwen's own policy/carrier and launch a fresh tier-C conversation."""
    from ...terminal_runner import TerminalLaunch

    if request.resume_harness_session_id:
        _secret_log('qwen-code is terminal session tier C — starting a fresh terminal')
    # Preserve native policy; httpUrl selects Qwen's Streamable HTTP transport.
    settings = {**prepared.commands.native}
    if bridge is not None:
        settings['mcpServers'] = {**settings.get('mcpServers', {}),
                                 'collimate': {'httpUrl': bridge.url_container}}
    await asyncio.to_thread(write_system_settings, prepared.env_obj, settings)
    yield TerminalLaunch(
        container_id=prepared.env_obj.container_id, argv=prepared.argv,
        cwd="/workspace", env={'TERM': 'xterm-256color', **prepared.env},
        label=f"Qwen Code · {prepared.model or 'default'}",
        buttons=[],
        ready_status="Qwen Code terminal ready — open the cell to attach.",
        harness_session_id="",
    )
    # Container teardown belongs to the run-level owner, as before.
