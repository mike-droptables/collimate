# SPDX-License-Identifier: MIT
"""OpenCode terminal, configuration, and session-record helpers.

Authentication comes from the model account selected for the run. Key manager
owns login; subscription credentials are leased into temporary host storage
shared by the account’s active runs. Harness homes are temporary and only
session history is retained between runs. ACP and terminal surfaces use the
same credential bindings and workspace-staged session homes.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log
from _agent_shared.secret_hygiene import write_log as _write_log


import asyncio
from contextlib import asynccontextmanager
import json
import re
import shlex
import os

import gen
from collimate.account_taxonomy import PLAN_LOGINS

from ... import new_tty_session
from ...agent_events import TRACE_FILE
from ...agent_config import resolve_model
from ...login_store import PLAN_LABELS
from ...transcript_merge import TERMINAL_TRANSCRIPT as _TRANSCRIPT_FILE
from .. import CommandsPolicy, PreparedHarness
from ...harness_homes import harness_home
from ...acp_client import SpawnProfile, spawn_container_adapter, spawn_host_adapter

_HOME = "/home/coder"

# opencode's own global config + the instruction file the session context
# rides (shared/docs/agent-runtime.md, instruction context). Both are CONTAINER-LOCAL: only
# ~/.local/share/opencode (the session store) is a mounted per-cell volume, so
# these are this run's alone and never reach the cell or the user's machine.
# ``instructions`` is opencode's documented config key ("Additional
# instruction files or patterns to include" — verified against the pinned
# opencode 1.17.14: absolute entries are globbed by ``Instruction.systemPaths``
# and read into the system prompt by ``Instruction.system``).
OPENCODE_CONFIG_DIR = f"{_HOME}/.config/opencode"
OPENCODE_CONFIG_PATH = f"{OPENCODE_CONFIG_DIR}/opencode.json"
CONTEXT_SEAM_PATH = f"{OPENCODE_CONFIG_DIR}/collimate-context.md"

# The grant's ``commands`` axis is opencode's documented ``permission.bash``
# in the GLOBAL config, on every leg (ACP container, ACP host, the TUI):
# ``deny`` removes the tool from the model, ``ask`` has opencode ask (a
# permission request in the chat, its own prompt in the TUI), ``allow`` runs
# unattended. Every built-in agent, subagents included, gets the global rules
# after its own (source-verified), so one key decides for opencode's agents.
#
# In the container Collimate's global config is out of the agent's reach: the
# config dir and ``~/.opencode`` are root-owned in a sticky, root-owned home
# (the image makes them so) and Collimate writes them as root. On the host
# the config dir is the per-cell home's ``XDG_CONFIG_HOME``.
#
# The cell's own ``opencode.json`` and ``.opencode/`` are trusted and merge
# AFTER the global config, so a cell that sets ``permission.bash`` raises (or
# lowers) the level the grant chose. The TUI's ``!`` lines and a command
# template's ``!`cmd``` run through the shell, not the bash tool: they are
# the human's own commands. Commands made only of ``cd`` / ``pushd`` /
# ``popd`` / bare variable assignments never ask (opencode's bash tool skips
# the permission step for them).


def git_env(commands: str) -> dict:
    """The env every opencode leg runs under at ``commands``: under deny and
    ask, the git opencode's git service runs on the workspace repository
    reads no global or system git config. That config sits in the harness
    home, where the agent's file tools can write it, and its filter,
    textconv and fsmonitor settings run commands with no shell tool and no
    ask. On the host leg this also sets aside the user's own ``~/.gitconfig``
    (identity, credential helpers) for commands a human approves under ask.

    It does not reach a repository's own config: the repository's
    ``.git/config`` is the cell's and still applies, and the snapshot
    git-dir's ``config`` (``~/.local/share/opencode/snapshot/<id>/<hash>/``,
    in the per-cell harness home) is closed by :func:`grant_config`
    turning snapshots off instead."""
    if commands == "allow":
        return {}
    return {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def grant_config(commands: str, *, websearch: bool, acp: bool) -> dict:
    """The opencode config keys that set a leg to the grant: ``bash`` at the
    ``commands`` level and the web tools off unless granted.

    Under deny and ask, snapshot tracking is off. opencode snapshots every
    turn with git on a git-dir of its own in the harness home, whose
    ``config`` the agent's file tools can reach, and whose filter and
    fsmonitor settings would run commands with no shell tool and no ask.

    ``acp`` legs also deny ``task`` under ask: a subagent's permission request
    names a child session the ACP adapter does not know, so it is dropped and
    the subagent's command would wait forever. The TUI shows those asks
    itself."""
    permission: dict = {"bash": commands}
    if commands == "ask" and acp:
        permission["task"] = "deny"
    if not websearch:
        permission.update(webfetch="deny", websearch="deny")
    config: dict = {"permission": permission}
    if commands != "allow":
        config["snapshot"] = False
    return config

# CLI credentials come from the selected account's temporary host lease.

# opencode's data root and its credential file inside the container. The data
# root IS the per-cell harness-home mount (see above); the auth.json path is
# the nested single-file bind's mountpoint. Only XDG_DATA_HOME/HOME can move
# either (OPENCODE_CONFIG_DIR moves the config dir alone), and Collimate sets
# neither.
OPENCODE_DATA_DIR = f"{_HOME}/.local/share/opencode"
OPENCODE_AUTH_PATH = f"{OPENCODE_DATA_DIR}/auth.json"

# Collimate auth value → the auth.json key its credential lives under, which
# is also opencode's provider id on the wire: the adapter's native id for the
# plan (test_backend_opencode pins the two equal).
OPENCODE_PLAN_KEYS = PLAN_LOGINS["opencode"].store_keys


def shared_login_path() -> str:
    """Selected account's host-managed temporary credential file."""
    from ...login_store import login_path
    return login_path("opencode")




def opencode_volumes(auth: str) -> dict:
    """Mount a temporary session home and the selected account credential lease.

    The ACP and terminal legs use this same arrangement. Sign-in is owned by
    Key manager; callers reject missing subscription credentials first.
    """
    from ...harness_homes import harness_home
    from ...login_store import single_file_bind

    volumes = {harness_home("opencode"): {"bind": OPENCODE_DATA_DIR, "mode": "rw"}}
    # The pipeline refused a plan leg whose lease holds no login.
    if (auth or "api_key").strip().lower() != "api_key":
        # Nested links are applied shallowest-first, so the credential path
        # is created inside the staged home.
        volumes.update(single_file_bind(shared_login_path(), OPENCODE_AUTH_PATH))
    return volumes


def native_id(provider: str, auth: str = "api_key") -> str:
    """opencode's (models.dev's) provider id for a selection — the adapter's:
    Gemini is ``google``, and the plan-only Copilot family is the built-in
    ``github-copilot`` plugin provider."""
    from .adapter import ADAPTER
    return ADAPTER.native_provider(provider, "" if auth == "api_key" else auth).id


def _opencode_model_id(native: str, model: str) -> str:
    """The ``<native>/<model>`` id opencode expects, the ``<native>/``
    prefix applied exactly once (idempotent): openrouter slugs are
    ``vendor/model`` and must still become ``openrouter/vendor/model`` — a
    bare ``"/" in model`` guard would wrongly leave the prefix off. Shared by
    the terminal surface and the ACP spawn profile."""
    return model if model.startswith(f"{native}/") else f"{native}/{model}"


def acp_config(*, auth: str, provider: str, model: str, commands: str,
               websearch: bool, instructions_path: str = "", policy: CommandsPolicy | None = None) -> dict:
    """The global ``opencode.json`` an ACP leg writes (the container profile
    and its host twin differ only in where): the model in opencode's own
    provider-prefixed vocabulary, a key-billed provider's standard endpoint,
    the grant (:func:`grant_config`), and the instruction file the session
    context rides when ``instructions_path`` is given."""
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    config: dict = {"$schema": "https://opencode.ai/config.json"}
    native = native_id(provider, auth)
    # A blank model under a plan (only reachable from a hand-built config —
    # resolve_model fills every plan's default) leaves the key out rather
    # than writing a dangling ``openai/``; a model the user already spelled
    # with its prefix (`opencode models github-copilot` prints them that way)
    # is not prefixed twice.
    if model or auth == "api_key":
        config["model"] = _opencode_model_id(native, model)
    if auth == "api_key":
        config["provider"] = {native: {"options": {"baseURL": STANDARD_MODEL_ENDPOINTS[provider]}}}
    config.update(policy.native if policy is not None else grant_config(commands, websearch=websearch, acp=True))
    if instructions_path:
        config["instructions"] = [instructions_path]
    return config


def write_api_credentials(path: str, provider: str, key: str) -> None:
    """Materialize only the selected key in the temporary harness auth file."""
    from ...login_store import write_private_atomic
    content = {native_id(provider): {"type": "api", "key": key}}
    write_private_atomic(path, json.dumps(content).encode("utf-8"))


async def prepare(ctx: dict, *, leg: str = "acp") -> PreparedHarness:
    """Prepare one native policy, credential arrangement and instruction seam.

    The terminal keeps its native subagent prompts; ACP denies task under ask.
    Host placement writes the same ACP policy into the run's private home.
    Every blocking setup step runs off the event loop.
    """
    from ...cli_container import ensure_env, write_root_file
    from ...host_adapters import host_env, write_host_seam, link_login

    auth, provider = ctx["auth"], ctx["provider"]
    plan = auth != "api_key"
    model = ctx["model"]
    if leg == "terminal":
        model = resolve_model("opencode", provider, model, auth=auth)
    if plan:
        from ...logging import safe_log
        safe_log(f"Using {PLAN_LABELS[auth]} · model {model}")
    env_vars = git_env(ctx["commands"])
    key = ctx["credentials"].key or None
    policy = CommandsPolicy(ctx["commands"], grant_config(
        ctx["commands"], websearch=ctx["websearch"], acp=leg != "terminal"))
    env_obj = None
    host = ctx.get("runtime") == "standard"
    if host:
        if leg == "terminal":
            raise ValueError("opencode terminal needs docker: full")
        found = ctx["host_harness"]
        home = await asyncio.to_thread(harness_home, "opencode")
        data, xdg_config = os.path.join(home, "data"), os.path.join(home, "config")
        config_dir = os.path.join(xdg_config, "opencode")
        env_vars = await asyncio.to_thread(host_env, XDG_DATA_HOME=data,
                                          XDG_CONFIG_HOME=xdg_config, **env_vars)
        if plan:
            await asyncio.to_thread(link_login, shared_login_path(),
                                    os.path.join(data, "opencode", "auth.json"))
        seam_path = os.path.join(config_dir, "collimate-context.md")
        seam = await asyncio.to_thread(write_host_seam, seam_path, ctx.get("system_prompt", ""))
        if key:
            await asyncio.to_thread(write_api_credentials, os.path.join(data, "opencode", "auth.json"), provider, key)
        config_path = os.path.join(config_dir, "opencode.json")
        argv, cwd = [found["path"], "acp"], gen.workspace_path()
    else:
        if key:
            home = await asyncio.to_thread(harness_home, "opencode")
            await asyncio.to_thread(write_api_credentials, os.path.join(home, "auth.json"), provider, key)
        volumes = await asyncio.to_thread(opencode_volumes, auth)
        env_obj = await ensure_env("opencode", build_dir="opencode", volumes=volumes)
        seam_path = CONTEXT_SEAM_PATH
        seam = await asyncio.to_thread(write_context_seam, env_obj, ctx.get("system_prompt", ""))
        config_path = OPENCODE_CONFIG_PATH
        argv, cwd = ["opencode"] if leg == "terminal" else ["opencode", "acp"], "/workspace"
    config = acp_config(auth=auth, provider=provider, model=model,
                        commands=policy.level, websearch=ctx["websearch"],
                        instructions_path=seam_path if seam else "", policy=policy)

    def write_host_config():
        os.makedirs(config_dir, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(config, indent=2))

    if host:
        await asyncio.to_thread(write_host_config)
    else:
        await asyncio.to_thread(write_root_file, env_obj, config_path,
                                json.dumps(config, indent=2), what="writing opencode's config")

    async def spawn():
        if host:
            return await spawn_host_adapter("opencode", argv, env=env_vars, cwd=cwd)
        return await spawn_container_adapter("opencode", env_obj, argv, env_vars=env_vars)

    profile = None if leg == "terminal" else SpawnProfile(
        locus="host" if host else "container", cwd=cwd, spawn=spawn,
        commands=policy.level, env_obj=env_obj,
        fold_system_prompt=not seam, bind_host="127.0.0.1" if host else None,
        model_applied=bool(model), model_value=config.get("model", ""))
    return PreparedHarness(policy, profile, env_obj, tuple(argv), env_vars,
                           config.get("model", ""))



def write_context_seam(env, text: str) -> bool:
    """opencode's own instruction seam: the session context as a
    container-local markdown file the global ``opencode.json`` names in its
    ``instructions`` array. The ONE writer for both the ACP spawn profile and
    the TTY runner — whoever opens the leg, opencode reads the same file, and
    neither plants anything in the workspace. Returns whether it carried
    text (a blank system prompt clears the file instead)."""
    from ...cli_container import write_context_seam as _write

    return _write(env, CONTEXT_SEAM_PATH, text, user="root")




# ──────────────────────────────────────────────────────────────────────
# Container + seeding shared by the surfaces — every opencode surface rides
# the ensure_env-cached container now (one image, one container, one session
# store per run), so the ACP leg and the TUI resolve to the same place.
# ──────────────────────────────────────────────────────────────────────


def _write_terminal_bridge(env, url: str | None) -> None:
    """Keep this leg's own remote server in the prepared native configuration."""
    from ...cli_container import write_root_file

    config = json.loads(env.read_file(OPENCODE_CONFIG_PATH, user='root'))
    if not isinstance(config, dict):
        raise ValueError('Prepared OpenCode config must be an object')
    servers = config.setdefault('mcp', {})
    if not isinstance(servers, dict):
        raise ValueError('Prepared OpenCode MCP config must be an object')
    if url:
        servers['collimate'] = {'type': 'remote', 'url': url, 'enabled': True}
    else:
        servers.pop('collimate', None)
    write_root_file(env, OPENCODE_CONFIG_PATH, json.dumps(config, indent=2),
                    what="writing OpenCode's terminal MCP config")


@asynccontextmanager
async def terminal_context(prepared: PreparedHarness, request, bridge):
    """Bind OpenCode's native carrier, then own its exact session/export lifetime."""
    from ...terminal_runner import TerminalLaunch

    env = prepared.env_obj
    await asyncio.to_thread(_write_terminal_bridge, env,
                            bridge.url_container if bridge else None)
    # Failed binding never arms export. Once bound, every acquired-leg exit
    # finalizes under the still-live bridge, including registration failure.
    before = None
    resume_harness_session_id = request.resume_harness_session_id
    try:
        if not resume_harness_session_id:
            before = await asyncio.to_thread(_listed_before, env)
        command = list(prepared.argv)
        if resume_harness_session_id:
            command += ["-s", resume_harness_session_id]
            _secret_log(f"the terminal runs on opencode session {resume_harness_session_id[:12]}…")
        yield TerminalLaunch(
            container_id=env.container_id, argv=tuple(command), cwd="/workspace",
            env={"TERM": "xterm-256color", **prepared.env},
            label=f"OpenCode · {prepared.model}",
            buttons=[],
            ready_status="OpenCode TUI ready — open the cell to attach.",
            harness_session_id=resume_harness_session_id, before=before,
        )
    finally:
        await asyncio.to_thread(_finalize, env,
                                harness_session_id=resume_harness_session_id, before=before)


def trace_from_opencode_export(data: dict) -> list:
    """Normalize an ``opencode export`` document — whose ``messages[].parts``
    carry text, reasoning, and tool parts — into the shared trace vocabulary.
    This is what the persisted ``_agent/trace.json`` carries."""
    trace: list = []
    for m in (data or {}).get("messages") or []:
        if not isinstance(m, dict):
            continue
        info = m.get("info") if isinstance(m.get("info"), dict) else m
        role = str(info.get("role") or m.get("role") or "").lower()
        parts = m.get("parts") if isinstance(m.get("parts"), list) else \
            info.get("parts") if isinstance(info.get("parts"), list) else []
        for p in parts:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype == "text" and p.get("text"):
                trace.append({"type": "user" if role == "user" else "text",
                              "text": p["text"]})
            elif ptype == "reasoning" and p.get("text"):
                trace.append({"type": "thinking", "text": p["text"]})
            elif ptype == "tool":
                state = p.get("state") if isinstance(p.get("state"), dict) else {}
                trace.append({"type": "tool_use", "tool": p.get("tool", ""),
                              "input": state.get("input")})
                output = state.get("output")
                if output:
                    trace.append({"type": "tool_result", "tool": p.get("tool", ""),
                                  "output": str(output)})
    return trace


def _finalize(env, *, harness_session_id: str = "",
              before: "frozenset | None" = None) -> None:
    """Teardown: export the transcript, then persist the run's raw record — the
    ``opencode export`` document (tool + reasoning parts included, which the
    open-format transcript strips) and its normalization into the shared trace
    vocabulary. Any processing of that record is post-hoc and external.

    The session exported is the leg's own: ``harness_session_id``, the one the TTY
    ran on, or — a fresh TTY — the ONE session that appeared in the store
    after ``before`` (its ids when the leg started). With no id, no snapshot,
    or several new sessions, nothing is exported: an earlier run's
    conversation is never written as this one's."""
    try:
        sid = harness_session_id or _tty_session(env, before)
        data = _export_chatlog(env, sid) if sid else None
    except Exception as e:  # noqa: BLE001
        _secret_log(f"chatlog export failed: {e}")
        data = None
    if not data:
        return
    try:
        _write_log("_agent/terminal/opencode_export.json",
                        json.dumps(data, indent=2, default=str),
                        category="log")
        trace = trace_from_opencode_export(data)
        if trace:
            _write_log(TRACE_FILE, json.dumps(trace, default=str), category="log")
    except Exception as e:  # noqa: BLE001
        _secret_log(f"trace export failed: {e}")


def cli_output(env, arguments: list[str]) -> str:
    """Read complete CLI output without the host exec tail cap.

    A private temporary file receives the export; the environment's chunked
    reader returns all bytes. No raw Docker operation or persistent copy.
    """
    code, path = env.exec(['mktemp', '/tmp/collimate-opencode-output.XXXXXX'], user='coder')
    if code != 0:
        raise RuntimeError('Cannot allocate private OpenCode output file')
    path = path.strip()
    command = shlex.join(['opencode', *arguments])
    try:
        code, _ = env.exec(['sh', '-c', command + ' > ' + shlex.quote(path) + ' 2>/dev/null'], user='coder')
        if code != 0:
            raise RuntimeError(f'`{command}` exited {code}')
        return env.read_file(path, user='coder').decode('utf-8', 'replace')
    finally:
        try:
            env.exec(['rm', '-f', '--', path], user='coder')
        except Exception:
            pass


def _export_chatlog(env, sid: str):
    """Write the open-format transcript from opencode's own export of session
    ``sid``; returns the parsed export document (``{"info", "messages"}``) or
    ``None``.

    opencode (>=1.x) keeps sessions in a SQLite DB, not a JSON storage tree, so
    the conversation comes from ``opencode export <id>`` — a clean
    ``{"info", "messages"}`` document — rather than reconstructed raw files.
    """
    raw = cli_output(env, ['export', sid])
    start = raw.find("{")
    if start < 0:
        _secret_log(f"opencode export {sid}: no JSON output")
        return None
    try:
        data = json.loads(raw[start:])
    except Exception as e:  # noqa: BLE001
        _secret_log(f"opencode export {sid}: could not parse export ({e})")
        return None
    from _agent_shared.terminal_tail import messages_from_opencode_export
    out = messages_from_opencode_export(data)
    if not out:
        _secret_log(f"opencode export {sid}: no chat text to write")
        return data
    from _agent_shared.transcript_merge import write_merged_transcript
    added = write_merged_transcript(out, path=_TRANSCRIPT_FILE)
    _secret_log(f"{_TRANSCRIPT_FILE}: {added} message(s) added via "
            f"`opencode export {sid}`")
    return data


def harness_session_ids(env) -> frozenset:
    """The ids (``ses_…``) of the sessions in this cell's opencode store, via
    ``opencode session list``. Raises when the store cannot be listed — an
    empty answer from a failed listing would make every session look new."""
    text = cli_output(env, ['session', 'list'])
    return frozenset(re.findall(r"ses_[A-Za-z0-9]+", text))


def _listed_before(env) -> "frozenset | None":
    """:func:`harness_session_ids` when a fresh TTY starts, or ``None`` (logged) when
    the store cannot be listed — the leg then exports nothing."""
    try:
        return harness_session_ids(env)
    except Exception as exc:  # noqa: BLE001 — the TTY still starts
        _secret_log(f"opencode: the session store could not be listed ({exc}) — "
                "this terminal's conversation will not be exported")
        return None


def _tty_session(env, before: "frozenset | None") -> str:
    """The session a fresh TTY wrote: the ONE id in the store now that
    ``before`` lacks (``new_tty_session``), ``""`` — logged — when there is
    no snapshot, none appeared, or several did."""
    if before is None:
        return ""
    now = harness_session_ids(env)
    sid = new_tty_session(before, now)
    if not sid:
        _secret_log(f"opencode: the terminal wrote {len(now - before)} new "
                "session(s) — none exported")
    return sid


def text_from_parts(parts) -> str:
    if not isinstance(parts, list):
        return ""
    chunks = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
            chunks.append(p["text"])
        elif isinstance(p, str):
            chunks.append(p)
    return "\n".join(chunks)
