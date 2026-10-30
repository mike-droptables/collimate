# SPDX-License-Identifier: MIT
"""Mistral Vibe's renderers: its provider/model config, the commands policy
in its environment, and its two legs (``vibe-acp`` and the ``vibe`` TTY).

``VIBE_HOME`` is the per-cell harness home, bind-mounted at
``/home/coder/.vibe``. Vibe layers its configuration — defaults, the user
``config.toml`` there, a project ``.vibe/config.toml`` (trusted folders
only, and Collimate trusts none), then the ``VIBE_*`` environment, then an
agent profile — so the commands level rides the environment of the process
the generator starts, above every file the agent could rewrite. The one
layer above the environment, agent profiles under ``VIBE_HOME/agents``, is
cleared on every leg.

Every other file Vibe loads from ``VIBE_HOME`` that can name a command is
agent-writable too, and none of the layers above stops it: ``hooks.toml``
(``pre_tool`` hooks run any command), ``config.toml`` (``mcp_servers`` stdio
commands, ``tool_paths``), ``tools/`` (Python run on load), ``agents/``,
``plugins/`` (hooks and MCP servers), ``prompts/``, ``.env`` (injects
``VIBE_*`` variables the process did not set, e.g. ``VIBE_MCP_SERVERS``) and
``trusted_folders.toml`` (trusting ``/workspace`` switches on the workspace's
own ``.vibe/hooks.toml``, ``config.toml``, ``tools``, ``plugins``). Each is
cleared on the host before the spawn and then placed read-only in the
container (:func:`guard_files`), so a write by the agent fails; the
directories are placed as empty files, which Vibe reads as "no such
directory" and the agent cannot ``mkdir`` over.

Terminal session tier C: the TTY opens a fresh conversation, and its session
store (``logs/session``) is a format the transcript does not read.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import PurePosixPath
from contextlib import asynccontextmanager

from collimate.providers import STANDARD_MODEL_ENDPOINTS

from .. import CommandsPolicy, PreparedHarness
from ...acp_client import SpawnProfile, spawn_container_adapter
from ...harness_files import write_native_file
from ...harness_homes import harness_home
from ...logging import safe_log

_HOME = "/home/coder"
VIBE_HOME = f"{_HOME}/.vibe"

# The tools that run a command (bash and the managed-shell variants Vibe adds
# when its managed shell is on) and the web tools.
_SHELL_TOOLS = ("bash", "bash_output", "bash_stdin", "bash_kill")
_WEB_TOOLS = ("web_fetch", "web_search")

# Outbound traffic an unattended agent has no use for, and the background
# model calls that title a session.
_QUIET_ENV = {
    "VIBE_ENABLE_UPDATE_CHECKS": "false",
    "VIBE_ENABLE_AUTO_UPDATE": "false",
    "VIBE_ENABLE_TELEMETRY": "false",
    "VIBE_EXPERIMENTS__ENABLE": "false",
    "VIBE_SESSION_LOGGING__GENERATE_TITLES": "false",
}


def policy_env(commands: str, *, websearch: bool) -> dict:
    """The ``VIBE_*`` environment that holds ``commands``.

    * deny — the shell tools removed (removing ``bash`` also denies the
      harness's process builtins) and the auto-approve agent disabled, since
      its bypass would override a tool's permission;
    * ask — bash asks for every command: its default read-only allowlist
      (``ls``, ``cat``, ``git status``…) emptied, auto-approve disabled;
    * allow — bash runs unasked (its built-in denylist still applies).
    The web tools and Mistral's connectors are removed when the run has no
    internet."""
    disabled = list(_SHELL_TOOLS) if commands == "deny" else []
    if not websearch:
        disabled += list(_WEB_TOOLS)
    env = {**_QUIET_ENV, "VIBE_DISABLED_TOOLS": json.dumps(disabled)}
    if not websearch:
        env["VIBE_ENABLE_CONNECTORS"] = "false"
    if commands == "allow":
        env["VIBE_TOOLS__BASH__PERMISSION"] = "always"
    else:
        env["VIBE_ENABLE_AUTO_APPROVE"] = "false"
        # ``bypass_tool_permissions`` skips every tool approval; Vibe ignores
        # it only while auto-approve is disabled, so both are pinned.
        env["VIBE_BYPASS_TOOL_PERMISSIONS"] = "false"
    if commands == "ask":
        env["VIBE_TOOLS__BASH__PERMISSION"] = "ask"
        env["VIBE_TOOLS__BASH__ALLOWLIST"] = "[]"
    return env


def config_toml(*, provider: str, native: str, model: str) -> str:
    """``VIBE_HOME/config.toml``: the selected provider at its standard
    endpoint (Mistral's own backend for ``mistral``, OpenAI-style for the
    rest) and, when a model is set, a model entry aliased by its own id —
    the alias is what ``VIBE_ACTIVE_MODEL`` and the ACP ``model`` option
    name. A blank model on Mistral keeps Vibe's own default."""
    key_env = _key_env(provider)
    lines = ["[[providers]]", f"name = {json.dumps(native)}",
             f"api_base = {json.dumps(STANDARD_MODEL_ENDPOINTS[provider])}",
             f"api_key_env_var = {json.dumps(key_env)}"]
    lines.append('backend = "mistral"' if provider == "mistral" else 'api_style = "openai"')
    if model:
        lines += ["", "[[models]]", f"name = {json.dumps(model)}",
                  f"provider = {json.dumps(native)}", f"alias = {json.dumps(model)}"]
    return "\n".join(lines) + "\n"


def _key_env(provider: str) -> str:
    from collimate.providers import PROVIDER_KEY_ENV
    return PROVIDER_KEY_ENV[provider]


# Files under VIBE_HOME that must be empty, and the directories Vibe loads
# code or profiles from (placed as empty files).
_GUARDED_FILES = ("hooks.toml", ".env", "trusted_folders.toml")
_GUARDED_DIRS = ("tools", "agents", "plugins", "prompts")


def guard_files(config_body: str = "") -> dict:
    """Container path -> content the host places read-only (the sandbox's
    ``readonly_files``): everything Vibe loads from ``VIBE_HOME`` that can
    name a command, so a write by the agent fails. ``config_body`` is the
    exact ``config.toml``; empty leaves it writable (the terminal leg
    appends its bridge after the container is placed)."""
    guards = {f"{VIBE_HOME}/{name}": "" for name in (*_GUARDED_FILES, *_GUARDED_DIRS)}
    if config_body:
        guards[f"{VIBE_HOME}/config.toml"] = config_body
    return guards


def container_home(home: str, mount: str = "/workspace") -> str:
    """The harness home's path inside the container, under the workspace mount."""
    import gen
    root = os.path.realpath(gen.workspace_path())
    return f"{mount}/{os.path.relpath(os.path.realpath(home), root)}"


# Links the container's own ``VIBE_HOME`` (a real directory, below which the
# guards are mounted) to the per-cell home in the workspace for what the host
# shares with it: the session store and the files it writes (AGENTS.md, the
# terminal leg's config.toml). A mount cannot sit below a link, and the host
# refuses a read-only mount inside the workspace, so the home is not one.
_LINK_HOME = (
    'set -e; src="$1"; dst="$2"; shift 2; mkdir -p -- "$dst"; '
    'for name in logs AGENTS.md config.toml; do '
    'case " $* " in *" $name "*) continue;; esac; '
    'ln -sfn -- "$src/$name" "$dst/$name"; done'
)


def link_home(env, home_in_container: str, guards: dict) -> None:
    """Expose the shared parts of the per-cell home inside ``VIBE_HOME``."""
    held = [PurePosixPath(path).name for path in guards]
    code, output = env.exec(["sh", "-c", _LINK_HOME, "sh", home_in_container, VIBE_HOME, *held], user="root")
    if code != 0:
        raise RuntimeError(f"cannot link the vibe home: {output!r}")


async def prepare(ctx: dict, *, leg: str = "acp") -> PreparedHarness:
    """Vibe's renderer for one leg."""
    from ...cli_container import ensure_env

    provider, native = ctx["provider"], ctx["native_provider"].id
    model = (ctx.get("model") or "").strip()
    home = await asyncio.to_thread(harness_home, "vibe")
    body = config_toml(provider=provider, native=native, model=model)
    await asyncio.to_thread(write_native_file, home, "config.toml", body.encode())
    # Agent profiles sit above the environment, and hooks, tools, plugins, .env
    # and the trust store name commands: none survives into a leg.
    await asyncio.to_thread(_clear_profiles, home)
    await asyncio.to_thread(os.makedirs, os.path.join(home, "logs"), 0o700, True)
    guards = guard_files("" if leg == "terminal" else body)
    commands = ctx["commands"]
    env_vars = {**policy_env(commands, websearch=bool(ctx.get("websearch"))),
                _key_env(provider): ctx["credentials"].key, "VIBE_HOME": VIBE_HOME}
    if model:
        env_vars["VIBE_ACTIVE_MODEL"] = model
    policy = CommandsPolicy(commands, {**{k: v for k, v in env_vars.items()
                                          if k.startswith("VIBE_") and k != "VIBE_HOME"},
                                        "readonly_files": guards})
    # No volume: VIBE_HOME is the container's own directory, the guards
    # mounted below it and the shared parts of the per-cell home linked in.
    env_obj = await _place(ensure_env, {}, guards)
    await asyncio.to_thread(link_home, env_obj, await asyncio.to_thread(container_home, home), guards)
    if leg == "terminal":
        argv = ("vibe", "--workdir", "/workspace")
        return PreparedHarness(policy, None, env_obj, argv, env_vars, model)
    argv = ("vibe-acp",)

    async def spawn():
        return await spawn_container_adapter("vibe", env_obj, list(argv), env_vars=env_vars)

    profile = SpawnProfile(locus="container", cwd="/workspace", spawn=spawn,
                           commands=commands, env_obj=env_obj, model_applied=bool(model))
    return PreparedHarness(policy, profile, env_obj, argv, env_vars, model)


async def _place(ensure_env, volumes: dict, guards: dict):
    """The leg's container. A cached one placed with other guards (another
    model's ``config.toml``, or the terminal leg's writable one) is retired
    first: the read-only files are fixed at placement."""
    from ...cli_container import cached_env, close_env

    cached = cached_env("vibe")
    if cached is not None and getattr(cached, "_vibe_guards", guards) != guards:
        await asyncio.to_thread(close_env, "vibe", cached)
    env_obj = await ensure_env("vibe", build_dir="vibe", volumes=volumes, readonly_files=guards)
    try:
        env_obj._vibe_guards = guards
    except AttributeError:
        pass
    return env_obj


def _clear_profiles(home: str) -> None:
    """Remove what the agent left in the harness home that Vibe would load a
    command from; the files are emptied (and, later, placed read-only), the
    directories removed."""
    import shutil
    for name in (*_GUARDED_DIRS, *_GUARDED_FILES):
        path = os.path.join(home, name)
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    for name in _GUARDED_FILES:
        # Unlinked first: a leftover placement is root-owned, the directory is ours.
        with open(os.path.join(home, name), "wb"):
            pass


@asynccontextmanager
async def terminal_context(prepared: PreparedHarness, request, bridge):
    """Bind the run's tool bridge into Vibe's own MCP config and launch a
    fresh tier-C conversation."""
    from ...terminal_runner import TerminalLaunch

    if request.resume_harness_session_id:
        safe_log("vibe is terminal session tier C — starting a fresh terminal")
    home = await asyncio.to_thread(harness_home, "vibe")
    if bridge is not None:
        body = (await asyncio.to_thread(_read, home, "config.toml")) + (
            '\n[[mcp_servers]]\nname = "collimate"\ntransport = "streamable-http"\n'
            f"url = {json.dumps(bridge.url_container)}\n")
        await asyncio.to_thread(write_native_file, home, "config.toml", body.encode())
    yield TerminalLaunch(
        container_id=prepared.env_obj.container_id, argv=prepared.argv, cwd="/workspace",
        env={"TERM": "xterm-256color", **prepared.env},
        label=f"Mistral Vibe · {prepared.model or 'default'}", buttons=[],
        ready_status="Mistral Vibe terminal ready — open the cell to attach.",
        harness_session_id="",
    )


def _read(home: str, relative: str) -> str:
    from collimate import fs
    fd = fs.open_file(home, relative, write=False)
    with os.fdopen(fd, "rb") as handle:
        return handle.read().decode("utf-8")
