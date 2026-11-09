# SPDX-License-Identifier: MIT
"""Hermes Agent's renderers: its provider/model config, the commands policy
in its managed scope, and its two legs (``hermes acp`` and the ``hermes``
TTY), plus the session store readers the CLI surface's tail uses.

``HERMES_HOME`` is the per-cell harness home, bind-mounted at
``/home/coder/.hermes``: ``config.yaml`` (provider, model, quiet defaults),
``SOUL.md`` (the pipeline's instruction seam) and ``state.db``, Hermes's
SQLite session store, whose session ids are the ACP session ids. The
dependency environment Hermes was installed with lives in the image
(``/opt/hermes/installs``) and is linked into every home. The commands level
is Hermes's managed scope, ``/etc/hermes/config.yaml``, written as root on
every leg: it overrides the user config key by key and ``hermes config
set`` refuses to change what it pins.

The harness home is the agent's to write, so nothing below allow may rest on
a file in it: every Hermes feature that turns a file there into a command
(`!`cmd`` in a skill, shell hooks and their allowlist, plugins, a language
server, an SQLite extension, scheduled jobs, MCP servers) is pinned off in
the managed scope (:data:`_PINNED`), and each leg clears the entries those
features load from (:data:`HOME_CLEARED`) before it starts.

Commands: deny removes the terminal and code-execution toolsets; allow turns
approvals off. Hermes asks only before the commands its own pattern list
flags dangerous, so ask is not a level it holds (the adapter's ``levels``):
the pipeline resolves it to deny, with a notice.

Terminal session tier B: the TTY opens a conversation of its own, and the
tail follows the one session that appears in ``state.db`` after the ids
listed before it started.
"""
from __future__ import annotations

import asyncio
import copy
import os
import shutil
import sqlite3
from contextlib import asynccontextmanager

from collimate.providers import PROVIDER_KEY_ENV, STANDARD_MODEL_ENDPOINTS

from .. import CommandsPolicy, PreparedHarness
from ...acp_client import SpawnProfile, spawn_container_adapter
from ...harness_files import write_native_file
from ...harness_homes import harness_home
from ...logging import safe_log

HERMES_HOME = "/home/coder/.hermes"
MANAGED_CONFIG = "/etc/hermes/config.yaml"
# The installed dependency environment, linked into each per-cell home.
INSTALLS = "/opt/hermes/installs"

# Every native provider's base-URL variable: Hermes's own providers read
# their endpoint from these, set from the provider registry.
_BASE_URL_ENV = {"anthropic": "ANTHROPIC_BASE_URL", "gemini": "GEMINI_BASE_URL",
                 "openai-api": "OPENAI_BASE_URL", "openrouter": "OPENROUTER_BASE_URL",
                 "xai": "XAI_BASE_URL"}

# The toolsets that run a command, and the web and browser ones (the image
# carries no browser).
_COMMAND_TOOLSETS = ("terminal", "code_execution")
_WEB_TOOLSETS = ("web",)
# Scheduled jobs and subagents, withheld below allow like the command toolsets.
_BELOW_ALLOW_TOOLSETS = ("cronjob", "delegation")

_QUIET = {
    "telemetry": {"shared_metrics": {"enabled": False, "send": False, "offer_version": 2}},
    "updates": {"check": False},
    "security": {"allow_lazy_installs": False},
}

# Every Hermes feature that turns a file the agent can write into a command
# (or native code) below allow, pinned in the managed scope. The user config
# is the agent's to write (an edit is auto-approved; Hermes's own write_file /
# patch guard on config.yaml is one guard on some tools, not a boundary), so
# nothing here may rest on it. A list replaces the user's list; a scalar wins
# per leaf.
_PINNED = {
    # `!`cmd` in a skill body, run by skill_view and skill preloading.
    "skills": {"inline_shell": False, "external_dirs": [], "create_dir": "", "project_discovery": False,
               "trusted_project_dirs": [], "auto_load": []},
    # Shell-script hooks (`hooks:` in config.yaml) run once consented: never
    # auto-consent. The allowlist file sits in the agent-writable home, so it
    # is cleared every leg as well.
    "hooks_auto_accept": False,
    # Python plugins (the home's plugins/, entry points) and plugin installs.
    "plugins": {"enabled": [], "auto_apply": False, "allow_lazy_installs": False},
    # A language-server command from the config, started on a file edit.
    "lsp": {"enabled": False},
    # A loadable SQLite extension (lib/libfts5_cjk.so) opened with state.db.
    "sessions": {"cjk_fts": False},
    # Scheduled jobs run later, outside this leg's approvals.
    "cron": {"allow_agent_scheduling": False},
    # Dangerous-command patterns the approval gate lets through unasked.
    "command_allowlist": [],
    # A changed `mcp_servers` in the user config is not hot-reloaded into a
    # running session (each leg writes a config without any).
    "mcp": {"auto_reload_on_config_change": False},
}

# The harness-home entries Hermes loads code, hooks or consent from. The home
# is the agent's to write and is shared by every leg of a run, so each leg
# starts from none of them. Collimate stages nothing in any of them: its
# skills and tools arrive through the bridge and the workspace.
HOME_CLEARED = ("plugins", "skills", "hooks", "cron", "scripts", "lib", "lsp", "skill-bundles",
                "plugin-data", "mcp-installs", "shell-hooks-allowlist.json", "relay-plugins.toml",
                "npmrc", ".env")


def base_url(provider: str) -> str:
    """The provider's standard endpoint as Hermes takes it (the Anthropic
    SDK appends its own ``/v1``)."""
    endpoint = STANDARD_MODEL_ENDPOINTS[provider]
    return endpoint.removesuffix("/v1") if provider == "anthropic" else endpoint


def config_yaml(*, provider: str, native: str, model: str) -> dict:
    """``HERMES_HOME/config.yaml``: the selected provider and model — a
    built-in provider by its own id, an OpenAI-compatible one as a named
    custom provider at its standard endpoint — and the quiet defaults."""
    config: dict = {**_QUIET, "model": {"provider": native, "default": model}}
    if native.startswith("custom:"):
        name = native.split(":", 1)[1]
        config["providers"] = {name: {"api": STANDARD_MODEL_ENDPOINTS[provider],
                                      "key_env": PROVIDER_KEY_ENV[provider],
                                      "transport": "chat_completions"}}
    return config


def managed_config(commands: str, *, websearch: bool) -> dict:
    """The managed scope for a leg: the toolsets the level removes, the
    approvals mode it runs in and the pins of :data:`_PINNED`."""
    disabled = ["browser"]
    if commands != "allow":
        disabled += list(_COMMAND_TOOLSETS) + list(_BELOW_ALLOW_TOOLSETS)
    if not websearch:
        disabled += list(_WEB_TOOLSETS)
    return {**copy.deepcopy(_PINNED), "agent": {"disabled_toolsets": disabled},
            "approvals": {"mode": "off" if commands == "allow" else "manual"},
            **copy.deepcopy(_QUIET)}


def clear_home(home: str, names=HOME_CLEARED) -> None:
    """Remove ``names`` from the harness home without following a link the
    agent planted (a link is unlinked, a directory removed tree-wise)."""
    for name in names:
        path = os.path.join(home, name)
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            if os.path.lexists(path):
                raise PermissionError(f"hermes: cannot clear {name} from the harness home")


def provider_env(provider: str, native: str, key: str) -> dict:
    env = {PROVIDER_KEY_ENV[provider]: key}
    if native in _BASE_URL_ENV:
        env[_BASE_URL_ENV[native]] = base_url(provider)
    return env


def hermes_volumes() -> dict:
    return {harness_home("hermes"): {"bind": HERMES_HOME, "mode": "rw"}}


def _link_installs(home: str) -> None:
    """Point the home's dependency state at the image's install."""
    link = os.path.join(home, "installs")
    if os.path.islink(link) and os.readlink(link) == INSTALLS:
        return
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(INSTALLS, link)


async def prepare(ctx: dict, *, leg: str = "acp") -> PreparedHarness:
    """Hermes's renderer for one leg."""
    import yaml
    from ...cli_container import ensure_env, write_root_file

    provider, native = ctx["provider"], ctx["native_provider"].id
    model = (ctx.get("model") or "").strip()
    commands = ctx["commands"]
    home = await asyncio.to_thread(harness_home, "hermes")
    await asyncio.to_thread(_link_installs, home)
    # The home outlives a leg and the agent writes it: start from none of what
    # Hermes loads code from. Credentials ride the process environment, never
    # a file the agent reads back (.env is among the cleared entries).
    await asyncio.to_thread(clear_home, home)
    await asyncio.to_thread(write_native_file, home, "config.yaml",
                            yaml.safe_dump(config_yaml(provider=provider, native=native, model=model)).encode())
    managed = managed_config(commands, websearch=bool(ctx.get("websearch")))
    policy = CommandsPolicy(commands, managed)
    volumes = await asyncio.to_thread(hermes_volumes)
    env_obj = await ensure_env("hermes", build_dir="hermes", volumes=volumes)
    await asyncio.to_thread(write_root_file, env_obj, MANAGED_CONFIG, yaml.safe_dump(managed),
                            what="writing hermes's managed config")
    env_vars = {"HERMES_HOME": HERMES_HOME, "TERMINAL_HOME_MODE": "real",
                **provider_env(provider, native, ctx["credentials"].key)}
    if leg == "terminal":
        return PreparedHarness(policy, None, env_obj, ("hermes",), env_vars, model)
    argv = ("hermes", "acp")

    async def spawn():
        return await spawn_container_adapter("hermes", env_obj, list(argv), env_vars=env_vars)

    profile = SpawnProfile(locus="container", cwd="/workspace", spawn=spawn, commands=commands,
                           env_obj=env_obj, model_applied=bool(model),
                           model_value=f"{native}:{model}" if model else "")
    return PreparedHarness(policy, profile, env_obj, argv, env_vars, model)


# ── The session store: state.db, read on the host ─────────────────────


def state_db_path(home: str) -> str:
    return os.path.join(home, "state.db")


def _connect(path: str):
    """A read-only connection to a live WAL store (Hermes's gateway is its
    one writer), falling back to an immutable read when the shared-memory
    file cannot be made."""
    quoted = path.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    try:
        con = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=1.0)
        con.execute("SELECT 1 FROM sessions LIMIT 1")
        return con
    except sqlite3.Error:
        return sqlite3.connect(f"file:{quoted}?immutable=1", uri=True, timeout=1.0)


def session_ids(home: str) -> frozenset:
    """Every session id in ``home``'s store (none when there is no store)."""
    path = state_db_path(home)
    if not os.path.isfile(path):
        return frozenset()
    con = _connect(path)
    try:
        return frozenset(row[0] for row in con.execute("SELECT id FROM sessions"))
    except sqlite3.Error:
        return frozenset()
    finally:
        con.close()


def harness_session_ids() -> frozenset:
    """:func:`session_ids` of this cell's harness home — the terminal
    facet's listing, taken before the TTY starts."""
    return session_ids(harness_home("hermes"))


def trace_from_state_db(home: str, session_id: str) -> list:
    """The normalized trace of one session's user and assistant text, in
    order — the active, uncompacted messages."""
    from ...agent_events import trace_event
    path = state_db_path(home)
    if not session_id or not os.path.isfile(path):
        return []
    con = _connect(path)
    try:
        rows = con.execute(
            "SELECT role, content FROM messages WHERE session_id = ? AND active = 1 "
            "ORDER BY id", (session_id,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    trace = []
    for role, content in rows:
        text = content if isinstance(content, str) else ""
        if role == "user" and text.strip():
            trace.append(trace_event("user", text=text))
        elif role == "assistant" and text.strip():
            trace.append(trace_event("text", text=text))
    return trace


def messages_from_state_db(home: str, session_id: str) -> list:
    from ...terminal_tail import messages_from_trace
    return messages_from_trace(trace_from_state_db(home, session_id))


def terminal_managed_config(managed: dict, bridge_url: str) -> dict:
    """The managed scope with the run's tool bridge added as an MCP server:
    the one server the TTY gets lives where the agent cannot edit it, not in
    the user config."""
    return {**copy.deepcopy(managed), "mcp_servers": {"collimate": {"url": bridge_url}}}


@asynccontextmanager
async def terminal_context(prepared: PreparedHarness, request, bridge):
    """Bind the run's tool bridge into Hermes's managed MCP config, launch
    the TTY on a conversation of its own and export it when the leg ends."""
    import yaml
    from ... import new_tty_session
    from ...cli_container import write_root_file
    from ...terminal_runner import TerminalLaunch

    home = await asyncio.to_thread(harness_home, "hermes")
    before = await asyncio.to_thread(session_ids, home)
    if bridge is not None:
        await asyncio.to_thread(
            write_root_file, prepared.env_obj, MANAGED_CONFIG,
            yaml.safe_dump(terminal_managed_config(prepared.commands.native, bridge.url_container)),
            what="writing hermes's managed config")
    if request.resume_harness_session_id:
        safe_log("hermes names its own terminal conversation — starting a fresh one")
    try:
        yield TerminalLaunch(
            container_id=prepared.env_obj.container_id, argv=prepared.argv, cwd="/workspace",
            env={"TERM": "xterm-256color", **prepared.env},
            label=f"Hermes · {prepared.model or 'default'}", buttons=[],
            ready_status="Hermes terminal ready — open the cell to attach.",
            harness_session_id="", before=before,
        )
    finally:
        def finalize():
            sid = new_tty_session(before, session_ids(home))
            if not sid:
                safe_log("hermes: the terminal left no conversation of its own to export")
                return
            messages = messages_from_state_db(home, sid)
            if messages:
                from ...transcript_merge import TERMINAL_TRANSCRIPT, write_merged_transcript
                added = write_merged_transcript(messages, path=TERMINAL_TRANSCRIPT)
                safe_log(f"{TERMINAL_TRANSCRIPT}: {added} message(s) added from the Hermes session")
        try:
            await asyncio.to_thread(finalize)
        except Exception as exc:  # noqa: BLE001 — the export is best-effort
            safe_log(f"hermes terminal export failed: {exc}")
