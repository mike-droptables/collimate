# SPDX-License-Identifier: MIT
"""Pi terminal, configuration, and session-record helpers.

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
import json
import os

from collimate.account_taxonomy import PLAN_LOGINS

from .. import CommandsPolicy, PreparedHarness
from ...harness_files import write_native_file
from ...harness_homes import harness_home
from ...acp_client import SpawnProfile, spawn_container_adapter

_HOME = "/home/coder"

# pi's config dir is ``$PI_CODING_AGENT_DIR`` or, unset, ``$HOME/.pi/agent``
# (the ``.pi`` name comes from the package's own ``piConfig.configDir``).
# Collimate sets neither variable, so these are the paths in every pi
# container — and the whole ``~/.pi`` tree is the per-cell harness home mount,
# which is what keeps the user's real ``~/.pi`` out of reach.
PI_HOME = f"{_HOME}/.pi"
PI_AGENT_DIR = f"{PI_HOME}/agent"
PI_AUTH_PATH = f"{PI_AGENT_DIR}/auth.json"
# pi's global settings file: ``quietStartup`` (the adapter otherwise prepends
# a "pi vX — …" banner to the first reply) plus, under a plan, the
# defaultProvider/defaultModel pair pi's startup model resolution honours.
PI_SETTINGS_PATH = f"{PI_AGENT_DIR}/settings.json"
PI_MCP_CONFIG_PATH = f"{PI_AGENT_DIR}/collimate-mcp.json"

# CLI credentials come from the selected account's temporary host lease.

# pi's custom-provider file. Its config values may start with ``!``, which
# pi RUNS as a shell command to get the value (an apiKey, a header) — a file
# in the agent-writable home that turns into a shell. Collimate configures
# native credentials itself, so the file is removed at the start of every leg.
# The credential store (:data:`PI_AUTH_PATH`) resolves ``!`` keys the same
# way on every turn; an api_key leg replaces it with the selected literal
# key (a plan leg's is the bound lease).
PI_MODELS_PATH = f"{PI_AGENT_DIR}/models.json"
# The ACP leg's instruction seam, relative to the harness home.
CONTEXT_FILE = "collimate-context.md"


# Providers whose pi 1.4.0 client appends ``/v1`` itself (its built-in
# ``baseUrl`` is the bare host): Anthropic and Mistral.
_BARE_HOST_PROVIDERS = ("anthropic", "mistral")


def _anthropic_messages_on_openrouter(model: str) -> bool:
    """pi 1.4.0 serves OpenRouter's Claude models (``anthropic/claude-*``
    without a variant suffix, its catalog's ``anthropic-messages`` group,
    whose base is the bare ``.../api``) over Anthropic Messages, whose client
    appends ``/v1/messages``; every other OpenRouter model goes over chat
    completions at ``.../api/v1``."""
    return model.startswith("anthropic/claude-") and ":" not in model


def models_json(pi_provider: str, collimate_provider: str, model: str = "") -> "dict | None":
    """The ONLY thing Collimate writes into models.json: the registry endpoint
    as a built-in provider's ``baseUrl`` override (pi 1.4.0: ``providers.<id>.
    baseUrl`` keeps the provider's models and authentication). No ``apiKey``,
    header or other value that could be a ``!command``. ``None`` when the
    provider has no registry endpoint. Read at call time. pi's Anthropic and Mistral clients
    append ``/v1`` themselves, so their default is the bare host: the registry's
    ``.../v1`` loses that suffix for them, keeping the real endpoint identical to
    pi's default. The override is provider-wide, so on OpenRouter the selected
    ``model``'s client decides: a Claude model's Anthropic Messages client gets
    the bare base too (:func:`_anthropic_messages_on_openrouter`)."""
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    url = STANDARD_MODEL_ENDPOINTS.get(collimate_provider, "")
    if not url:
        return None
    if collimate_provider in _BARE_HOST_PROVIDERS or (
            collimate_provider == "openrouter" and _anthropic_messages_on_openrouter(model)):
        url = url.rstrip("/").removesuffix("/v1")
    return {"providers": {pi_provider: {"baseUrl": url}}}

# The grant's commands axis. pi has no approval concept of its own, so the
# level rides a root-owned wrapper (pi/collimate-pi, baked into the image)
# that both legs run in place of ``pi``: pi-acp spawns it through
# ``PI_ACP_PI_COMMAND``, the terminal runs it directly, and
# ``COLLIMATE_COMMANDS`` picks the flags — deny drops the shell tools, ask
# loads the gate extension (pi/gate.ts) that confirms each shell call. The
# cell's project-local files are trusted: the wrapper passes pi's
# ``--approve`` on both legs, so the RPC chat loads them (it would skip them
# without a saved trust decision) and the TUI does not ask.
PI_WRAPPER = "/opt/collimate/pi/collimate-pi"


def pi_commands_env(commands: str) -> dict:
    """The env the wrapper reads the leg's commands level from."""
    return {"COLLIMATE_COMMANDS": commands}


def clear_command_values(env, auth: str) -> None:
    """Remove the home files whose ``!`` values pi runs as shell before the
    leg's pi starts (as root: the agent may have made them its own):
    :data:`PI_MODELS_PATH` always, :data:`PI_AUTH_PATH` on an api_key leg."""
    import shlex

    from ...cli_container import enforce_in_container

    paths = [PI_MODELS_PATH]
    if (auth or "api_key").strip().lower() not in PI_PLAN_KEYS:
        paths.append(PI_AUTH_PATH)
    enforce_in_container(env, "rm -f " + " ".join(shlex.quote(p) for p in paths),
                         what="clearing pi's models.json and credential store")

# Collimate auth value → the auth.json key its credential lives under, which
# is also pi's provider id on the wire. Store keys are owned by the taxonomy;
# the separate wire table selects the prefix used in a model id,
# even where the answers coincide. Note chatgpt is ``openai-codex``, NOT
# ``openai``: pi models the subscription as its own provider, with no API-key
# auth at all, so there is no key/plan ambiguity on that id.
PI_PLAN_KEYS = PLAN_LOGINS["pi"].store_keys


def shared_login_path() -> str:
    """Selected account's host-managed temporary credential file."""
    from ...login_store import login_path
    return login_path("pi")




def pi_volumes(auth: str) -> dict:
    """Mount a temporary session home and the selected account credential lease.

    The ACP and terminal legs use this same arrangement. Sign-in is owned by
    Key manager; callers reject missing subscription credentials first.
    """
    from ...harness_homes import harness_home
    from ...login_store import single_file_bind

    home = harness_home("pi")
    volumes = {home: {"bind": PI_HOME, "mode": "rw"}}
    # The pipeline refused a plan leg whose lease holds no login.
    if (auth or "").strip().lower() in PI_PLAN_KEYS:
        os.makedirs(os.path.join(home, "agent"), exist_ok=True)
        # The CLI helper remaps this host account binding under the workspace
        # home before linking that home into its container location.
        volumes.update(single_file_bind(shared_login_path(), PI_AUTH_PATH))
    return volumes


def settings_json(*, auth: str, provider: str, model: str) -> dict:
    """pi's global ``settings.json`` for a leg.

    ``quietStartup`` on both legs: the banner pi otherwise prints would be the
    first thing in a transcript (and, on the ACP leg, a chunk of the first
    reply). Under a plan the file ALSO carries the provider/model pair pi's
    startup resolution honours (``defaultProvider`` then ``defaultModel``),
    which is belt-and-braces beside the ``--model`` flag this runner passes:
    the flag decides this TTY, the settings decide anything the user starts
    from inside it (a ``/model`` reset, a second pi in the same container).
    ``defaultModel`` is the BARE model id — pi pairs it with the provider key,
    not with a ``provider/model`` slug."""
    settings: dict = {"quietStartup": True}
    if (auth or "").strip().lower() in PI_PLAN_KEYS:
        settings["defaultProvider"] = provider
        if model:
            settings["defaultModel"] = model
    return settings


def pi_default_model(provider: str, *, auth: str = "api_key") -> str:
    """The same blank-model default for direct ACP and terminal calls (F1555)."""
    from ... import PROVIDER_DEFAULT_MODELS
    from ...agent_config import resolve_model

    return (resolve_model("pi", provider, "", auth=auth) if auth in PI_PLAN_KEYS
            else PROVIDER_DEFAULT_MODELS.get(provider, ""))


async def bind_native_bridge(bridge):
    """Bind only this run's common tool bridge, independently of ACP intake."""
    content = json.dumps({"url": bridge.url_container}).encode() if bridge is not None else None

    def bind():
        home = harness_home("pi")
        write_native_file(home, "agent/collimate-mcp.json", content)

    worker = asyncio.create_task(asyncio.to_thread(bind))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        # The thread cannot be cancelled. Keep its bridge/home alive until it
        # settles, even if the owning task receives another cancellation.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        worker.result()  # Preserve native write failures before owner cleanup.
        raise


async def prepare(ctx: dict, *, leg: str = "acp") -> PreparedHarness:
    """Prepare Pi's native credentials, model and command wrapper for each leg."""
    from ...cli_container import ensure_env

    auth, provider = ctx["auth"], ctx["provider"]
    plan = auth in PI_PLAN_KEYS
    model = (ctx.get("model") or "").strip()
    if not model:
        model = pi_default_model(provider, auth=auth)
    pi_provider = ctx["native_provider"].id
    key = ctx["credentials"].key or None
    home = await asyncio.to_thread(harness_home, "pi")
    await bind_native_bridge(None)
    # Reject linked retained paths and stale executable custom-provider values.
    await asyncio.to_thread(write_native_file, home, "agent/models.json", None)
    override = None if plan else models_json(pi_provider, provider, model)
    if not plan:
        await asyncio.to_thread(write_native_file, home, "agent/auth.json", None)
    volumes = await asyncio.to_thread(pi_volumes, auth)
    env_obj = await ensure_env("pi", build_dir="pi", volumes=volumes)
    await asyncio.to_thread(clear_command_values, env_obj, auth)
    if override:
        await asyncio.to_thread(write_native_file, home, "agent/models.json",
                                json.dumps(override).encode())
    if not plan:
        credentials = {pi_provider: {"type": "api_key", "key": key}} if key else {}
        await asyncio.to_thread(write_native_file, home, "agent/auth.json", json.dumps(credentials).encode())
    await asyncio.to_thread(write_native_file, home, "agent/settings.json",
        json.dumps(settings_json(auth=auth, provider=pi_provider, model=model)).encode())
    policy = CommandsPolicy(ctx["commands"], pi_commands_env(ctx["commands"]))
    env_vars = dict(policy.native)
    argv = ["pi-acp"] if leg == "acp" else [PI_WRAPPER]
    profile = None
    if leg == "acp":
        env_vars["PI_ACP_PI_COMMAND"] = PI_WRAPPER
        # pi's own diagnostics reach the adapter's stderr (the run's adapter
        # log) through the wrapper.
        env_vars["COLLIMATE_PI_STDERR"] = "1"
        # The session context the pipeline wrote into the harness home
        # (``Instructions("home_file", CONTEXT_FILE)``), appended by the wrapper.
        env_vars["COLLIMATE_PI_CONTEXT"] = f"{PI_AGENT_DIR}/{CONTEXT_FILE}"
        async def spawn():
            return await spawn_container_adapter("pi", env_obj, argv, env_vars=env_vars)
        profile = SpawnProfile(locus="container", cwd="/workspace", spawn=spawn,
            commands=policy.level, env_obj=env_obj, model_applied=False, bind_bridge=bind_native_bridge,
            model_value=f"{pi_provider}/{model}" if model else "")
    else:
        if model:
            argv += ["--model", f"{pi_provider}/{model}"]
        prompt = (ctx.get("system_prompt") or "").strip()
        if prompt:
            argv += ["--append-system-prompt", prompt]
    return PreparedHarness(policy, profile, env_obj, tuple(argv), env_vars, model)


@asynccontextmanager
async def terminal_context(prepared: PreparedHarness, request, bridge):
    """Bind Pi's own carrier and launch a fresh native tier-C conversation."""
    from ...terminal_runner import TerminalLaunch

    if request.resume_harness_session_id:
        _secret_log("pi is terminal session tier C — starting a fresh terminal")
    await bind_native_bridge(bridge)
    yield TerminalLaunch(
        container_id=prepared.env_obj.container_id, argv=prepared.argv,
        cwd="/workspace", env={"TERM": "xterm-256color", **prepared.env},
        label=f"pi · {prepared.model or 'default'}",
        buttons=[],
        ready_status="pi terminal ready — open the cell to attach.",
        harness_session_id="",
    )
    # Container teardown belongs to session.run's run-level close_all.
