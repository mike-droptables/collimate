# SPDX-License-Identifier: MIT
"""Cline terminal, configuration, and session-record helpers.

Authentication comes from the model account selected for the run. Key manager
owns login; subscription credentials are leased into temporary host storage
shared by the account’s active runs. Harness homes are temporary and only
session history is retained between runs. ACP and terminal surfaces use the
same credential bindings and workspace-staged session homes.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import asyncio
from contextlib import asynccontextmanager
import os


from .. import CommandsPolicy, PreparedHarness
from ...acp_client import SpawnProfile, spawn_container_adapter
from ...harness_files import write_native_file

_HOME = "/home/coder"

# The container path of the ONE ``.cline`` Collimate owns — the per-cell
# harness home, bind-mounted here under BOTH auths. It is never the user's
# real ~/.cline: that directory carries their own provider credentials, their
# whole session history, their hub/kanban state and their installation
# identity, and handing it to an agent would hand over all of it at once.
CLINE_HOME = f"{_HOME}/.cline"

# The one piece of the chatgpt leg that IS shared across cells: the login, as
# a DIRECTORY in the selected account's lease (login_store.login_path),
# bind-mounted rw into every cline container.
#
# A DIRECTORY, not codex's single shared inode, because cline's credential
# writer is temp-file + rename (``ProviderSettingsManager.write`` stages
# ``<path>.<pid>.tmp`` and ``renameSync``s it into place — three successive
# logins were observed landing on three different inodes). Renaming ONTO a
# bind mountpoint either fails or detaches the bind and strands every other
# container on a dead file, so the mountpoint has to be the DIRECTORY and the
# rename has to happen inside it. Per-cell COPIES are out for the same reason
# they are out for codex: OpenAI's refresh tokens rotate and are single-use, so
# N copies of one chain revoke each other within minutes.
#
# ONE DIRECTORY PER (harness, plan), never a store shared between plans: cline
# keeps every provider in ONE providers.json, and its ACP arm restores the
# FIRST of [cline, cline-pass, openai-codex] that has a stored key — then uses
# that key even when ``CLINE_PROVIDER`` names a different provider. A store
# holding both a Cline account and a ChatGPT login would therefore be able to
# send the Cline token to chatgpt.com. Keeping this store exclusive to the
# ChatGPT login makes that unreachable by construction.
#
# The host writes the lease file and collects it at release; signing out
# happens in Key manager.

# Where that directory lands inside the container: OUTSIDE the per-cell
# ``.cline`` mount, deliberately. The credential is the only thing cells share;
# nesting it under the home mount would put it one ``CLINE_DIR`` rethink away
# from dragging sessions and installation identity along with it.
CLINE_LOGIN_MOUNT = f"{_HOME}/.cline-login"

# The file inside it, named by ``CLINE_PROVIDER_SETTINGS_PATH`` (paths.ts
# ``resolveProviderSettingsPath``) — the undocumented-but-present lever that
# pins providers.json EXACTLY, and the reason this share is possible at all:
# it moves ONLY the credential out of the per-cell home and leaves the
# sessions, logs, hub address and installation id behind. Both legs export it,
# so whichever starts first, both read and write the same store.
CLINE_LOGIN_FILE = f"{CLINE_LOGIN_MOUNT}/providers.json"

# FIRST USE needs no bootstrapping dance here, unlike codex: cline reads a
# missing, empty or garbage providers.json as "logged out" (``read()`` falls
# back to empty settings on ENOENT and on a parse failure), so there is no
# placeholder trap to avoid and nothing to promote. A run under the plan
# refuses an empty store before the container starts (selected login validation).


# ── The terminal's API key ────────────────────────────────────────────
#
# cline 3.0.65's TUI reads NO CLINE_PROVIDER / CLINE_API_KEY / CLINE_MODEL
# (only its ACP arm does): with nothing stored it opens on its provider
# onboarding and asks the user to paste the key the account already holds.
# What it does read is providers.json, at CLINE_PROVIDER_SETTINGS_PATH when
# that is set. So an api_key terminal leg gets its own providers.json in the
# per-cell harness home, named by that env on the TTY's exec only — never on
# the container. The ACP arm takes its selected key through its own exec,
# as required by the pinned native ACP authentication path. The key rides
# in that file alone (0600, host-written temp-file + rename): not in any
# argv, not in the TTY's or the container's environment, not in the
# workspace. The directory is the file's own so that cline's temp-file +
# rename rewrites land beside it; credentials and stages are removed when
# the TTY leg ends, while an empty private directory may remain.
CLINE_KEY_SETTINGS_DIR = "collimate-key"
CLINE_KEY_SETTINGS_FILE = f"{CLINE_HOME}/{CLINE_KEY_SETTINGS_DIR}/providers.json"
# Where the TUI's providers.json id differs from the ACP env's
# CLINE_PROVIDER (the adapter's native id): cline reads a stored "openai" as
# its legacy alias for "openai-compatible" (no base URL, and the model dropped
# to gpt-4o); its OpenAI provider is "openai-native" (probed at 3.0.65 with a
# dummy key: every other id opens the TUI at its prompt on the given model).
_TUI_SETTINGS_IDS = {"openai": "openai-native"}


def key_provider_id(cline_provider: str) -> str:
    """The providers.json id for the ACP env's ``CLINE_PROVIDER`` value."""
    return _TUI_SETTINGS_IDS.get(cline_provider, cline_provider)


def key_provider_settings(provider_id: str, key: str, model: str = "",
                          *, now: "str | None" = None, base_url: str = "",
                          also: "tuple[str, ...]" = ()) -> dict:
    """The providers.json cline's TUI opens at its prompt on (probed at
    3.0.65, ``StoredProviderSettingsSchema``): ``lastUsedProvider`` is what
    selects the provider — without it the TUI falls back to its own Cline
    gateway and the onboarding — and ``updatedAt`` must be a ``Z``-suffixed
    ISO datetime (zod's ``datetime()`` rejects an offset, and a file that
    fails the schema reads as empty, which cline then overwrites). ``model``
    is left out when blank: cline's own default for the provider."""
    if now is None:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    settings = {"provider": provider_id}
    if key:
        settings["apiKey"] = key
    if model:
        settings["model"] = model
    # ``baseUrl`` is the provider settings field cline's session config reads
    # (``StoredProviderSettingsSchema``, probed at 3.0.73): without it cline
    # bills its built-in URL for the provider.
    if base_url:
        settings["baseUrl"] = base_url
    return {
        "version": 1,
        "lastUsedProvider": provider_id,
        "modes": {},
        "providers": {pid: {"settings": {**settings, "provider": pid}, "updatedAt": now,
                            "tokenSource": "manual"}
                      for pid in (provider_id, *also)},
    }


def registry_endpoint(provider: str) -> str:
    """The provider's registry endpoint, read at call time ('' when it has none)."""
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    return STANDARD_MODEL_ENDPOINTS.get(provider, "")


def key_settings_host_dir() -> str:
    """The host side of :data:`CLINE_KEY_SETTINGS_FILE`'s directory, inside
    this cell's disposable harness home."""
    from ...harness_homes import harness_home

    return os.path.join(harness_home("cline"), CLINE_KEY_SETTINGS_DIR)


def write_key_settings(provider_id: str, key: str, model: str = "",
                       base_url: str = "", also: "tuple[str, ...]" = ()) -> str:
    """Write the leg's providers.json (0600, atomic) and return its host path.
    ``key`` is blank on the ACP leg (its key rides the environment): the file
    then carries only the provider's registry ``base_url``."""
    import json

    from ...harness_homes import harness_home
    home = harness_home("cline")
    write_native_file(home, f"{CLINE_KEY_SETTINGS_DIR}/providers.json", json.dumps(
        key_provider_settings(provider_id, key, model, base_url=base_url, also=also)).encode())
    return os.path.join(home, CLINE_KEY_SETTINGS_DIR, "providers.json")


def remove_key_settings() -> None:
    """Remove native credential rewrite stages without following planted links."""
    import stat
    from collimate import fs
    from ...harness_homes import harness_home
    home = harness_home("cline")
    write_native_file(home, f"{CLINE_KEY_SETTINGS_DIR}/providers.json", None)
    try:
        directory = fs.open_parent(os.path.abspath(os.sep),
            fs.relative_parts(os.path.abspath(home).lstrip(os.sep))
            + [CLINE_KEY_SETTINGS_DIR, "providers.json"], create=False)
    except FileNotFoundError:
        return
    try:
        for name in os.listdir(directory):
            if name.startswith("providers.json.") and name.endswith(".tmp"):
                item = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(item.st_mode):
                    raise PermissionError("Cline credential stage is not a regular file")
                os.unlink(name, dir_fd=directory)
    finally:
        os.close(directory)


def shared_login_dir() -> str:
    """Directory for this selected account's temporary credential lease."""
    return os.path.dirname(shared_login_path())


def shared_login_path() -> str:
    """The host path of the shared ``providers.json`` — the host side of
    :data:`CLINE_LOGIN_FILE`, the lease file the host writes and collects."""
    from ...login_store import login_path

    return login_path("cline")




def cline_plan_env() -> dict:
    """The env that points a container at Collimate's shared credential store.

    Exported by BOTH legs: the TUI and ``cline --acp`` read and refresh the
    same leased store, which is the whole reason one login serves every cell.
    Set under the chatgpt auth only: pointing an api_key run at the shared
    file would let a pasted key land beside (or over) the subscription. (An
    api_key TTY points the same env at its own per-cell file instead:
    :data:`CLINE_KEY_SETTINGS_FILE`.)"""
    return {"CLINE_PROVIDER_SETTINGS_PATH": CLINE_LOGIN_FILE}


# ── The grant's commands axis ─────────────────────────────────────────
#
# deny  — ``disabledTools: ["run_commands"]`` in cline's GLOBAL settings (the
#         tool is not offered at all, subagents included), plus the client
#         gate's refusal of any command request.
# ask   — cline's own approval flow: the ACP arm asks for every tool through
#         session/request_permission (the client cards commands), and the TUI
#         runs with ``--auto-approve false``.
# allow — the ACP client answers allow_once; the TUI runs
#         ``--auto-approve true``.
#
# The settings that enforce deny/ask live in files outside the per-cell home,
# named by ``CLINE_GLOBAL_SETTINGS_PATH`` and ``CLINE_MCP_SETTINGS_PATH`` and
# written afresh as root every leg: that home is the agent's to write (its
# editor tool reaches it), the home's own global-settings.json could turn
# ``toolAutoApprove`` back on or drop the disabled tool, and a stdio server
# in the home's MCP settings file spawns its command when a session is
# built — no tool call, no ask. On the ACP leg they stay root-owned. The TUI
# writes its own settings back to the same files (Tab's Plan/Act mode,
# Shift+Tab's auto-approve, ``/mcp``) and exits on a failed write, so there
# they are handed to ``coder`` once written; the agent still cannot change
# them unseen, because ``--auto-approve false`` has cline ask the human
# before every edit — until the human turns auto-approve on themselves.
CLINE_POLICY_SETTINGS = "/etc/collimate/cline/global-settings.json"
CLINE_MCP_POLICY_SETTINGS = "/etc/collimate/cline/mcp-settings.json"

# The tools whose delegates run with no approval at all: the sub-agent
# ``spawn_agent`` starts and the teammates ``team_spawn_teammate`` creates
# and ``team_run_task`` runs are built without the lead's approval hook
# (source-verified at cline 3.0.61). Not loaded under ask; under deny their
# delegates have no ``run_commands`` either (``disabledTools`` filters
# every agent's tools).
_UNATTENDED_DELEGATE_TOOLS = ("spawn_agent", "team_spawn_teammate",
                              "team_run_task")


def cline_policy_settings(commands: str) -> "dict | None":
    """The global settings a leg at ``commands`` runs under, or ``None`` for
    allow (cline's defaults stand). Written whole, so nothing else in the
    file — a per-model ``tools`` table, ``toolAutoApprove`` — can re-arm
    what it takes away."""
    if commands == "allow":
        return None
    disabled = (["run_commands"] if commands == "deny"
                else list(_UNATTENDED_DELEGATE_TOOLS))
    return {"toolAutoApprove": False, "disabledTools": disabled}


def mcp_server_entry(url: str) -> dict:
    """Installed stdio client avoids native HTTP OAuth settings rewrites."""
    return {"transport": {"type": "stdio", "command": "mcp-remote",
            "args": [url, "--allow-http", "--transport", "http-only"]},
            "timeout": 1800}


def apply_commands_policy(env, commands: str, *, terminal: bool,
                          mcp_servers: "dict | None" = None) -> dict:
    """Put the leg's commands policy in place and return the env it needs:
    deny/ask write the complete global policy; an explicit server declaration
    also writes the owned MCP settings under allow, preserving native global
    defaults. ACP files stay root-owned. TTY hands only these exact files to
    coder so its own UI writes land. Workspace hooks retain native trust."""
    import json
    import shlex

    from ...cli_container import enforce_in_container, root_file_script

    settings = cline_policy_settings(commands)
    if settings is None and mcp_servers is None:
        return {}
    files = {CLINE_MCP_POLICY_SETTINGS: json.dumps({"mcpServers": dict(mcp_servers or {})})}
    if settings is not None:
        files = {CLINE_POLICY_SETTINGS: json.dumps(settings), **files}
    script = [root_file_script(path, body) for path, body in files.items()]
    if terminal:
        script.append("chown coder:coder "
                      + " ".join(shlex.quote(path) for path in files))
    enforce_in_container(env, " && ".join(script),
                         what="writing cline's commands policy")
    out = {"CLINE_MCP_SETTINGS_PATH": CLINE_MCP_POLICY_SETTINGS}
    if settings is not None:
        out["CLINE_GLOBAL_SETTINGS_PATH"] = CLINE_POLICY_SETTINGS
    return out


def cline_volumes(auth: str) -> dict:
    """Link the run's workspace-staged home and, for chatgpt, login directory.

    ACP and terminal legs use the same links into the single workspace mount.
    The selected account must already contain the requested login.
    """
    from ...harness_homes import harness_home
    from ...login_store import dir_bind

    volumes = {harness_home("cline"): {"bind": CLINE_HOME, "mode": "rw"}}
    if (auth or "").strip().lower() == "chatgpt":
        volumes.update(dir_bind(shared_login_dir(), CLINE_LOGIN_MOUNT))
    return volumes


async def prepare(ctx: dict, *, leg: str = "acp") -> PreparedHarness:
    """Prepare selected credentials, native command policy and owned MCP routing."""
    from ...cli_container import ensure_env
    auth = ctx["auth"]
    model = (ctx.get("model") or "").strip()
    plan = auth == "chatgpt"
    key = ctx["credentials"].key or None
    if not ctx.get("websearch"):
        _secret_log("cline has no websearch-off config seam — " +
            ("fetch-kind permission requests will be refused instead" if leg == "acp"
             else "its built-in web tools stay available in the terminal"))
    env_vars = {"CLINE_PROVIDER": ctx["native_provider"].id}
    if plan:
        env_vars.update(cline_plan_env())
    if model:
        env_vars["CLINE_MODEL"] = model
    if key and leg == "acp":
        env_vars["CLINE_API_KEY"] = key
    await asyncio.to_thread(remove_key_settings)
    volumes = await asyncio.to_thread(cline_volumes, auth)
    env_obj = await ensure_env("cline", build_dir="cline", volumes=volumes)
    policy = CommandsPolicy(ctx["commands"], cline_policy_settings(ctx["commands"]) or {})
    env_vars.update(await asyncio.to_thread(apply_commands_policy, env_obj,
        policy.level, terminal=leg == "terminal", mcp_servers={}))
    async def bind_bridge(bridge):
        servers = {"collimate": mcp_server_entry(bridge.url_container)} if bridge else {}
        await asyncio.to_thread(apply_commands_policy, env_obj, policy.level,
                                terminal=leg == "terminal", mcp_servers=servers)
    # Every key-billed provider is billed at the registry endpoint: cline's
    # provider settings carry it as ``baseUrl`` (the key stays in the env on
    # the ACP leg, in this file on the terminal leg).
    endpoint = "" if plan else registry_endpoint(ctx["provider"])
    if (key if leg != "acp" else endpoint) and not plan:
        native = env_vars["CLINE_PROVIDER"]
        try:
            await asyncio.to_thread(write_key_settings, key_provider_id(native),
                (key or "") if leg != "acp" else "", model if leg != "acp" else "",
                endpoint, (native,) if native != key_provider_id(native) else ())
        except BaseException:
            await asyncio.to_thread(remove_key_settings)
            raise
        env_vars["CLINE_PROVIDER_SETTINGS_PATH"] = CLINE_KEY_SETTINGS_FILE
    argv = ["cline", "--acp"] if leg == "acp" else ["cline", "-i", "--auto-approve", "true" if policy.level == "allow" else "false"]
    profile = None
    if leg == "acp":
        async def spawn():
            return await spawn_container_adapter("cline", env_obj, argv, env_vars=env_vars)
        profile = SpawnProfile(locus="container", cwd="/workspace", spawn=spawn,
            commands=policy.level, env_obj=env_obj, model_applied=True,
            bind_bridge=bind_bridge)
    return PreparedHarness(policy, profile, env_obj, tuple(argv), env_vars, model)


def _stop_owned_hub(env, exec_env: dict) -> None:
    """Retire this run-container's native hub without deleting conversations."""
    import json
    rc, text = env.exec(["cline", "hub", "status"], env=exec_env, timeout=10)
    if rc != 0:
        raise RuntimeError("Cline native hub status failed during teardown")
    state = json.loads(text)
    if state.get("running") is not True:
        return
    # Native status confirms a live daemon before stop's discovered-PID fallback;
    # an old discovery record alone must not signal a reused process ID.
    rc, text = env.exec(["cline", "hub", "stop"], env=exec_env, timeout=10)
    if rc != 0 or json.loads(text).get("stopped") is not True:
        raise RuntimeError("Cline native hub shutdown failed during teardown")


@asynccontextmanager
async def terminal_lifetime(prepared, request):
    """Own workspace context and native cleanup beyond the bridge lifetime."""
    from ...cli_container import inject_context_file, remove_context_file
    env = prepared.env_obj
    try:
        if request.resume_harness_session_id:
            _secret_log("cline is terminal session tier C — starting a fresh terminal")
        # Arm cleanup before entry: a failed context-manager entry is not
        # installed on the runner's exit stack. Preserve the user's own file.
        if request.extra_system and request.extra_system.strip():
            injection = asyncio.create_task(asyncio.to_thread(
                inject_context_file, env, "/workspace/AGENTS.md", request.extra_system))
            try:
                await asyncio.shield(injection)
            except asyncio.CancelledError:
                # A thread cannot be cancelled. Its final write must precede
                # teardown, even if another cancellation arrives meanwhile.
                while not injection.done():
                    try:
                        await asyncio.shield(injection)
                    except asyncio.CancelledError:
                        continue
                injection.result()
                raise
        yield
    finally:
        def _teardown() -> None:
            try:
                remove_context_file(env, "/workspace/AGENTS.md")
            except Exception:  # noqa: BLE001
                pass
            try:
                remove_key_settings()
            finally:
                try:
                    apply_commands_policy(env, prepared.commands.level, terminal=True, mcp_servers={})
                finally:
                    _stop_owned_hub(env, prepared.env)

        # Off the loop; keep the run end's existing bounded teardown wait.
        await asyncio.to_thread(_teardown)
    # Container teardown belongs to session.run's run-level close_all.


@asynccontextmanager
async def terminal_context(prepared, request, bridge):
    """Bind this fresh native TTY to its own bridge; lifetime owns teardown."""
    from ...terminal_runner import TerminalLaunch
    env = prepared.env_obj
    binding = asyncio.create_task(asyncio.to_thread(
        apply_commands_policy, env, prepared.commands.level, terminal=True,
        mcp_servers={"collimate": mcp_server_entry(bridge.url_container)} if bridge else {}))
    try:
        await asyncio.shield(binding)
    except asyncio.CancelledError:
        # Settle the native write before bridge shutdown and the final empty
        # MCP reset. A late binding must never undo that reset.
        while not binding.done():
            try:
                await asyncio.shield(binding)
            except asyncio.CancelledError:
                continue
        binding.result()
        raise
    yield TerminalLaunch(
        container_id=env.container_id, argv=prepared.argv, cwd="/workspace",
        env={"TERM": "xterm-256color", **prepared.env},
        label=f"Cline · {prepared.model or 'default'}",
        buttons=[],
        ready_status="Cline terminal ready — open the cell to attach.",
        harness_session_id="",
    )
