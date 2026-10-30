# SPDX-License-Identifier: MIT
"""GitHub Copilot CLI's renderers: its credential (a GitHub token, or a
provider key through Copilot's own BYOK), the commands policy as the
server-wide tool flags, and its two legs (``copilot --acp`` and the
``copilot`` TTY).

``COPILOT_HOME`` is the per-cell harness home, bind-mounted at
``/home/coder/.copilot``; it holds Copilot's session state and the global
custom instructions (the pipeline's seam). Copilot's tool flags are server-wide: a client cannot
change them per session, and deny rules outrank every allow (``copilot help
permissions``). The approvals a session saves (``permissions-config.json``)
are cleared on every leg.

Files the agent can write that Copilot would load and run a command from. The
agent writes the workspace (and, through an approved path, its harness home),
and ``copilot --acp`` loads repo hooks (``.github/hooks/*.json``) and the
home's hooks, settings and ``mcp-config.json`` WHETHER OR NOT THE FOLDER IS
TRUSTED: trust gates only the workspace's MCP servers (``.mcp.json``,
``.github/mcp.json``), repo settings hooks and Claude-style hooks, and only
in prompt mode (``-p``) the repo hooks too. Copilot 1.0.103 has no working
off-switch for hooks in ``config.json``/``settings.json`` (``disableAllHooks``
is documented but not honoured). The switch that holds is the device-level
managed-settings file, which the agent cannot write: the image carries
``/etc/github-copilot/managed-settings.json`` with
``strictPluginOnlyCustomization: [skills, agents, hooks]``, so hooks (repo,
home, settings), custom agents (which declare MCP servers) and skills (whose
``allowed-tools`` pre-approve tools) may come only from plugins or managed
settings, at every commands level. The ``mcp`` surface is NOT locked: that
would also drop the run's own bridge, which arrives as an ACP ``mcpServers``
entry. No folder is trusted (so no workspace MCP file loads), and the home
files that could name an MCP server are cleared on every leg.

Terminal session tier C: the TTY opens a fresh conversation, and its session
state is a format the transcript does not read.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from collimate.providers import PROVIDERS, STANDARD_MODEL_ENDPOINTS

from .. import CommandsPolicy, PreparedHarness
from ...acp_client import SpawnProfile, spawn_container_adapter
from ...harness_files import write_native_file
from ...harness_homes import harness_home
from ...logging import safe_log

COPILOT_HOME = "/home/coder/.copilot"

# The model's shell tool, as --excluded-tools names it, and its permission
# kind; the web tools.
_SHELL_TOOL = "bash"
_WEB_TOOLS = ("web_fetch", "web_search")


# Files in the harness home that Copilot reads and acts on, and that the
# agent could have written: cleared on every leg.
_HOME_FILES_CLEARED = ("config.json", "settings.json", "mcp-config.json", "permissions-config.json")


def tool_flags(commands: str, *, websearch: bool) -> list:
    """The server-wide tool flags for ``commands``.

    * deny — the shell tool is not offered at all and every shell permission
      is denied;
    * ask — no shell rule: Copilot asks before every command, which the
      client's gate answers (a card under ask);
    * allow — shell commands are pre-approved.
    The built-in GitHub MCP servers stay off (the run's own tools arrive over
    the bridge), and without internet the web tools are withheld and every
    URL denied."""
    excluded = [_SHELL_TOOL] if commands == "deny" else []
    flags = ["--disable-builtin-mcps", "--no-auto-update", "--no-ask-user"]
    if commands == "deny":
        flags += ["--deny-tool", "shell"]
    elif commands == "allow":
        flags += ["--allow-tool", "shell"]
    if not websearch:
        excluded += list(_WEB_TOOLS)
        flags += ["--deny-tool", "url"]
    if excluded:
        flags += ["--excluded-tools", ",".join(excluded)]
    return flags


def provider_env(provider: str, key: str, model: str) -> dict:
    """Copilot's BYOK environment for an API key on ``provider``: its
    standard endpoint, the wire Copilot speaks to it, and the model (BYOK
    needs one)."""
    wire = PROVIDERS[provider].wire
    base = STANDARD_MODEL_ENDPOINTS[provider].rstrip("/")
    if wire == "anthropic" and base.endswith("/v1"):
        # Copilot's Anthropic client appends ``/v1/messages`` itself.
        base = base[:-3]
    env = {"COPILOT_PROVIDER_BASE_URL": base,
           "COPILOT_PROVIDER_TYPE": "anthropic" if wire == "anthropic" else "openai",
           "COPILOT_PROVIDER_API_KEY": key, "COPILOT_MODEL": model}
    if provider == "openai":
        env["COPILOT_PROVIDER_WIRE_API"] = "responses"
    return env


def read_token(path: str) -> str:
    """The leased GitHub token (the plan's login), one stripped line."""
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def copilot_volumes() -> dict:
    return {harness_home("copilot"): {"bind": COPILOT_HOME, "mode": "rw"}}


async def prepare(ctx: dict, *, leg: str = "acp") -> PreparedHarness:
    """Copilot's renderer for one leg."""
    from ...accounts import git_harness_environment
    from ...cli_container import ensure_env
    from ...secret_hygiene import remember

    creds = ctx["credentials"]
    model = (ctx.get("model") or "").strip()
    commands = ctx["commands"]
    home = await asyncio.to_thread(harness_home, "copilot")
    # No folder is trusted (config.json stays absent) and nothing a previous
    # leg or the agent left in the home is read back: its saved approvals, its
    # user settings (hooks, a status line command…) and its MCP server file.
    # Folder trust does not gate hooks under ``--acp``; the image's managed
    # settings do (see the module docstring).
    for name in _HOME_FILES_CLEARED:
        await asyncio.to_thread(write_native_file, home, name, None)
    env_vars = {"COPILOT_HOME": COPILOT_HOME, "COPILOT_AUTO_UPDATE": "false"}
    if creds.plan:
        token = await asyncio.to_thread(read_token, creds.login)
        remember(token)
        env_vars["COPILOT_GITHUB_TOKEN"] = token
        if model:
            env_vars["COPILOT_MODEL"] = model
    else:
        env_vars.update(provider_env(ctx["provider"], creds.key, model))
    flags = tool_flags(commands, websearch=bool(ctx.get("websearch")))
    # Copilot redacts every secret it receives: its own login or provider key,
    # and the Git account's header and GitLab token (git_harness_environment,
    # merged at the spawn; it never gets the gh names). An SSH account adds no
    # name: no variable holds the key (GIT_SSH_COMMAND names its file, and
    # only on the host; Copilot runs in a container).
    git_env = git_harness_environment("copilot", "container")
    flags += ["--secret-env-vars", ",".join(n for n in ("COPILOT_GITHUB_TOKEN", "COPILOT_PROVIDER_API_KEY",
                                                        "GIT_CONFIG_VALUE_0", "GITLAB_TOKEN")
                                            if n in env_vars or n in git_env)]
    policy = CommandsPolicy(commands, {"flags": flags})
    volumes = await asyncio.to_thread(copilot_volumes)
    env_obj = await ensure_env("copilot", build_dir="copilot", volumes=volumes)
    if leg == "terminal":
        return PreparedHarness(policy, None, env_obj, ("copilot", *flags), env_vars, model)
    argv = ("copilot", "--acp", *flags)

    async def spawn():
        return await spawn_container_adapter("copilot", env_obj, list(argv), env_vars=env_vars)

    profile = SpawnProfile(locus="container", cwd="/workspace", spawn=spawn, commands=commands,
                           env_obj=env_obj, model_applied=bool(model))
    return PreparedHarness(policy, profile, env_obj, argv, env_vars, model)


@asynccontextmanager
async def terminal_context(prepared: PreparedHarness, request, bridge):
    """Bind the run's tool bridge through --additional-mcp-config and launch
    a fresh tier-C conversation."""
    from ...terminal_runner import TerminalLaunch

    if request.resume_harness_session_id:
        safe_log("copilot is terminal session tier C — starting a fresh terminal")
    argv = list(prepared.argv)
    if bridge is not None:
        servers = {"mcpServers": {"collimate": {"type": "http", "url": bridge.url_container,
                                                "tools": ["*"]}}}
        argv += ["--additional-mcp-config", json.dumps(servers)]
    yield TerminalLaunch(
        container_id=prepared.env_obj.container_id, argv=tuple(argv), cwd="/workspace",
        env={"TERM": "xterm-256color", **prepared.env},
        label=f"Copilot · {prepared.model or 'default'}", buttons=[],
        ready_status="GitHub Copilot terminal ready — open the cell to attach.",
        harness_session_id="",
    )
