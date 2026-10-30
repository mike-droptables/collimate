# SPDX-License-Identifier: MIT
"""Goose native preparation, terminal runner and session export helpers.

ACP and terminal legs share native mode, extension allowlist, permission reset
and hints. Selected API credentials use goose's private secrets.yaml storage;
only session records persist. Standard ACP runs the machine's Goose with its
config and data redirected into the run's temporary home.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log
from _agent_shared.secret_hygiene import write_log as _write_log

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
import shlex

import gen

from ...transcript_merge import TERMINAL_TRANSCRIPT as _TRANSCRIPT_FILE
from .. import CommandsPolicy, PreparedHarness
from ...harness_files import write_native_file
from ...acp_client import AcpSession, SpawnProfile, spawn_container_adapter, spawn_host_adapter

# The harness-native raw record, kept verbatim beside the normalized trace.
_SESSION_FILE = "_agent/terminal/goose_session.json"

# goose's GLOBAL hints file (shared/docs/agent-runtime.md, instruction context) — the instruction seam the session
# context rides for both the ACP leg and the TTY leg. Container-local: goose's
# per-cell mount is ~/.local/share/goose (its session store), so ~/.config/goose
# is this run's own directory and never reaches the cell or the user's machine.
# Verified live against the pinned goose 1.48.0: a hints file here is read for
# any cwd (`goose::hints::load_hints`, rendered under "### Global Hints"), and
# both `.goosehints` and `AGENTS.md` are recognised context filenames.
GOOSE_HINTS_PATH = "/home/coder/.config/goose/.goosehints"


# goose's permission store (``Paths::config_dir()/permission.yaml``) — in the
# container, beside the hints file.
GOOSE_PERMISSION_PATH = "/home/coder/.config/goose/permission.yaml"


def goose_volumes() -> dict:
    """Link the workspace-staged home as goose's complete data directory.

    ACP and terminal legs share this arrangement so sibling state directories
    are writable by the same user through the one workspace mount.
    """
    from ...harness_homes import harness_home

    return {harness_home("goose"): {
        "bind": "/home/coder/.local/share/goose", "mode": "rw"}}


def write_permission_file(env, commands: str) -> None:
    """A fresh ``permission.yaml`` in the container for this leg's commands
    level (see :func:`permission_yaml`); raises when it does not land, since
    a stale one would carry an "Always Allow" answer into the leg."""
    from ...cli_container import write_policy_file

    write_policy_file(env, GOOSE_PERMISSION_PATH, permission_yaml(commands),
                      what="resetting goose's permission store",
                      mkdir=os.path.dirname(GOOSE_PERMISSION_PATH))


def write_context_seam(env, text: str) -> bool:
    """goose's own instruction seam: the session context as the container's
    GLOBAL hints file. The ONE writer for the ACP spawn profile and the TTY
    runner alike — `goose session` has no --system flag and `goose acp` carries
    no system prompt, and neither leg plants anything in the workspace any
    more. Returns whether it carried text (a blank system prompt clears it)."""
    from ...cli_container import write_context_seam as _write

    return _write(env, GOOSE_HINTS_PATH, text)

# ──────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested — no docker / network / subprocess)
# ──────────────────────────────────────────────────────────────────────


# The developer extension's tools (goose 1.50). ``shell`` is the one that runs
# commands; the rest read and edit files.
_DEVELOPER_TOOLS = ("write", "edit", "shell", "tree", "read", "read_image")

# The platform extensions a deny or ask run keeps. Left out: summon (its
# delegates run in auto mode, asking nobody), the extension manager (it can
# enable more extensions mid-session, computercontroller among them) and the
# scheduler (scheduled recipes run in auto mode).
_KEPT_EXTENSIONS = ("analyze", "todo", "apps", "tom", "skills")

# goose's mode per commands level: auto runs tools unattended; approve asks
# before every tool call (smart_approve's LLM classifier would let "read-only"
# commands through, so it is never used). deny runs auto with no shell tool.
_GOOSE_MODE = {"allow": "auto", "ask": "approve", "deny": "auto"}

# The oldest goose whose deny/ask mechanisms (the EXTENSIONS allowlist with
# available_tools, the approve mode's permission flow) were verified.
HOST_MIN_VERSION = (1, 50, 0)


def goose_extensions(commands: str) -> "dict | None":
    """The ``EXTENSIONS`` config (goose reads it from the env, in place of the
    config file's list) for a commands level, or ``None`` under allow (goose's
    own defaults). deny keeps the developer extension without ``shell`` —
    ``available_tools`` filters both the tool listing and dispatch."""
    if commands == "allow":
        return None
    tools = [t for t in _DEVELOPER_TOOLS if commands == "ask" or t != "shell"]
    extensions = {"developer": {
        "enabled": True, "type": "platform", "name": "developer",
        "available_tools": tools,
    }}
    for name in _KEPT_EXTENSIONS:
        extensions[name] = {"enabled": True, "type": "platform", "name": name}
    return extensions


def extension_violations(entries: list, commands: str, *, servers=(),
                         bridge_urls=()) -> list:
    """What a goose session's extensions (``session/extensions/list``
    entries) hold beyond ``commands``' allowlist: extensions
    :func:`goose_extensions` leaves out, MCP servers other than ``servers``
    (the ones this leg handed the session) or an http server at one of
    ``bridge_urls`` (this run's own Collimate bridges,
    ``cli_container.run_bridge_urls``), and developer tools beyond the
    level's ``available_tools``. Empty under allow.

    The bridge URLs cover the terminal leg: goose's CLI saves its
    ``--with-streamable-http-extension`` bridge in the session under a name
    derived from the URL (``host_docker_internal_<port>_mcp``), so the chat
    leg's next load lists it — the leg's own bridge, stopped when the TTY
    ended, so it serves nothing any more.

    A loaded session keeps the extensions it was saved with, so this is the
    check that it matches the level; a new session is built from the
    allowlist itself."""
    allowed = goose_extensions(commands)
    if allowed is None:
        return []
    out: list = []
    for entry in entries:
        ext = entry.get("extension") if isinstance(entry, dict) else None
        if not isinstance(ext, dict):
            out.append("an unreadable extension")
            continue
        server = ext.get("server") if isinstance(ext.get("server"), dict) else {}
        name = str(ext.get("name") or server.get("name") or "")
        if ext.get("type") == "mcp":
            bridged = server.get("type") == "http" and server.get("url") in bridge_urls
            if name not in servers and not bridged:
                out.append(name or "an unnamed MCP server")
            continue
        if name not in allowed:
            out.append(name or "an unnamed extension")
            continue
        keep = allowed[name].get("available_tools")
        if keep is None:
            continue
        have = ext.get("available_tools")
        if not isinstance(have, list):
            out.append(f"{name} (all tools)")
        elif set(have) - set(keep):
            out.append(f"{name} ({', '.join(sorted(set(have) - set(keep)))})")
    return out


def permission_yaml(commands: str) -> str:
    """goose's ``permission.yaml``, rewritten on every leg so no "Always
    Allow" answer (which goose saves there) outlives it. ask names ``shell``
    ask-before — an approve-mode default made explicit."""
    ask_before = '["shell"]' if commands == "ask" else "[]"
    return ("user:\n"
            "  always_allow: []\n"
            f"  ask_before: {ask_before}\n"
            "  never_allow: []\n")


# How each goose provider takes its base URL, as (env var, suffix goose appends
# itself). ``None`` = a bundled declarative provider with a hard-coded
# ``base_url`` and no host setting; those get a ``custom_providers/<id>.json``
# override instead (see :func:`custom_provider_json`).
_HOST_VAR = {
    "anthropic": ("ANTHROPIC_HOST", "/v1"),            # + v1/messages
    "google": ("GOOGLE_HOST", "/v1beta"),              # + v1beta/models/..
    "openai": ("OPENAI_HOST", "/v1"),                  # + OPENAI_BASE_PATH
    "openrouter": ("OPENROUTER_HOST", "/api/v1"),      # + api/v1/chat/completions
}
_DECLARATIVE = {"groq": "GROQ_API_KEY", "cerebras": "CEREBRAS_API_KEY"}


def _registry_endpoint(native: str) -> str:
    """The provider registry's endpoint for goose's provider id ``native``,
    read at call time (the conformance test replaces the entry)."""
    from collimate import providers
    return providers.STANDARD_MODEL_ENDPOINTS[{"google": "gemini"}.get(native, native)]


def _strip(endpoint: str, suffix: str) -> str:
    base = endpoint.rstrip("/")
    if not base.endswith(suffix):
        raise ValueError(f"endpoint {endpoint!r} does not end in {suffix!r}")
    return base[: -len(suffix)]


def provider_host_env(native: str) -> dict:
    """The env that points goose's ``native`` provider at the registry endpoint
    (equal to goose's own default for the real endpoint)."""
    if native not in _HOST_VAR:
        return {}
    var, suffix = _HOST_VAR[native]
    env = {var: _strip(_registry_endpoint(native), suffix)}
    if native == "openai":
        env["OPENAI_BASE_PATH"] = "v1/chat/completions"
    return env


def custom_provider_json(native: str, model: str) -> "str | None":
    """goose's bundled groq/cerebras definitions with the registry endpoint as
    ``base_url``, to be written as ``custom_providers/<id>.json`` (a custom
    file of the same name replaces the bundled one). None for other providers."""
    if native not in _DECLARATIVE:
        return None
    return json.dumps({
        "name": native, "engine": "openai", "display_name": native.capitalize(),
        "api_key_env": _DECLARATIVE[native],
        "base_url": _registry_endpoint(native).rstrip("/") + "/chat/completions",
        "models": [{"name": model or "default", "context_limit": 131072}],
        "supports_streaming": True,
    }, indent=2)


def goose_env(
    *,
    native: str,
    model: str,
    tools_enabled: bool,
    commands: str,
) -> dict:
    """The ``GOOSE_*`` env dict that configures one goose run (no config.yaml).

    ``GOOSE_MODE`` is the tool gate: ``chat`` disables ALL tool execution (the
    text-only path, ``tools_enabled=False``); otherwise the commands level
    picks it (``_GOOSE_MODE``), and ``EXTENSIONS`` carries the level's
    extension allowlist (:func:`goose_extensions`). The model's own sampling
    and output defaults apply. The selected secret is supplied separately through goose's native
    secrets.yaml. ``native`` is goose's own provider id (the adapter's)."""
    env: dict = {
        "GOOSE_PROVIDER": native,
        "GOOSE_MODEL": str(model or ""),
        # Containers have no OS keyring, and telemetry has nowhere to go.
        "GOOSE_DISABLE_KEYRING": "1",
        "GOOSE_TELEMETRY_ENABLED": "false",
        "GOOSE_MODE": _GOOSE_MODE[commands] if tools_enabled else "chat",
        **provider_host_env(native),
    }
    extensions = goose_extensions(commands)
    if extensions is not None:
        env["EXTENSIONS"] = json.dumps(extensions)
    return env


def key_name(provider: str) -> str:
    """The secret name goose reads ``provider``'s key from (the adapter's
    native key env, else the provider registry's)."""
    from collimate.providers import PROVIDER_KEY_ENV
    from ..goose.adapter import ADAPTER
    return ADAPTER.native_provider(provider).key_env or PROVIDER_KEY_ENV[provider]


def write_api_credentials(root: str, relative: str, provider: str, key: str) -> None:
    """Write only the selected provider secret in native, private file storage."""
    # JSON mappings are YAML mappings; escaping is unambiguous for any key bytes.
    write_native_file(root, relative, json.dumps({key_name(provider): key}).encode("utf-8"))


async def _write_custom_provider(home: str, config_rel: str, native: str, model: str) -> None:
    body = custom_provider_json(native, model)
    if body is not None:
        await asyncio.to_thread(write_native_file, home, f"{config_rel}/custom_providers/{native}.json",
                                body.encode("utf-8"))


async def prepare(ctx: dict, *, leg: str = "acp") -> PreparedHarness:
    """Prepare native permissions, selected secret and hints at the chosen locus."""
    from ...agent_config import resolve_model
    from ...cli_container import ensure_env
    from ...harness_homes import harness_home
    from ...host_adapters import host_env

    provider = ctx["provider"]
    model = resolve_model("goose", provider, ctx.get("model")) if leg == "terminal" else ctx["model"]
    env_vars = goose_env(native=ctx["native_provider"].id, model=model,
                         tools_enabled=True if leg == "terminal" else tools_enabled(ctx),
                         commands=ctx["commands"])
    policy = CommandsPolicy(ctx["commands"], {name: value for name, value in env_vars.items()
                            if name in ("GOOSE_MODE", "EXTENSIONS")})
    key = ctx["credentials"].key
    home = await asyncio.to_thread(harness_home, "goose")
    host = ctx.get("runtime") == "standard"
    env_obj = None
    if host:
        if leg == "terminal":
            raise ValueError("goose terminal needs docker: full")
        found = ctx["host_harness"]
        data, config = os.path.join(home, "share"), os.path.join(home, "config")
        env_vars = await asyncio.to_thread(host_env, XDG_DATA_HOME=data,
                                          XDG_CONFIG_HOME=config, **env_vars)
        await asyncio.to_thread(write_api_credentials, home, "config/goose/secrets.yaml", provider, key)
        await _write_custom_provider(home, "config/goose", ctx["native_provider"].id, model)
        await asyncio.to_thread(write_native_file, home, "config/goose/permission.yaml", permission_yaml(policy.level).encode())
        body = (ctx.get("system_prompt") or "").strip()
        await asyncio.to_thread(write_native_file, home, "config/goose/.goosehints", (body + "\n").encode() if body else None)
        seam = bool(body)
        argv, cwd = [found["path"], "acp"], gen.workspace_path()
        from ...logging import safe_log
        safe_log(f"standard: running goose {found['version']} at {found['path']} (this machine's own copy, not managed by Collimate)")
    else:
        config_dir = os.path.join(home, "native-config")
        await asyncio.to_thread(write_api_credentials, home, "native-config/secrets.yaml", provider, key)
        await _write_custom_provider(home, "native-config", ctx["native_provider"].id, model)
        volumes = await asyncio.to_thread(goose_volumes)
        volumes[config_dir] = {"bind": "/home/coder/.config/goose", "mode": "rw"}
        env_obj = await ensure_env("goose", build_dir="goose", volumes=volumes)
        await asyncio.to_thread(write_permission_file, env_obj, policy.level)
        seam = await asyncio.to_thread(write_context_seam, env_obj, ctx.get("system_prompt", ""))
        argv, cwd = ["goose", "session"] if leg == "terminal" else ["goose", "acp"], "/workspace"

    async def spawn():
        if host:
            return await spawn_host_adapter("goose", argv, env=env_vars, cwd=cwd)
        return await spawn_container_adapter("goose", env_obj, argv, env_vars=env_vars)

    mode = policy.native["GOOSE_MODE"]
    profile = None if leg == "terminal" else SpawnProfile(
        locus="host" if host else "container", cwd=cwd, spawn=spawn,
        commands=policy.level, env_obj=env_obj, fold_system_prompt=not seam,
        bind_host="127.0.0.1" if host else None, model_applied=bool(model),
        session_mode=mode, session_mode_required=mode == "approve")
    return PreparedHarness(policy, profile, env_obj, tuple(argv), env_vars, model)


def tools_enabled(ctx: dict) -> bool:
    """Whether goose runs with tools at all: ``chat`` mode disables every
    tool, which a pure text-only turn (a caller's
    enable_file_tools=False with no web and no commands) must
    stay in — otherwise goose runs tools the caller asked it not to."""
    return bool(
        ctx["enable_file_tools"] or ctx["websearch"] or ctx["commands"] != "deny"
    )


async def verify_loaded(session: AcpSession) -> str:
    """Why a loaded goose session does not match the leg's commands level,
    or "": its extensions (goose's ``session/extensions/list``) must hold
    nothing this level's ``EXTENSIONS`` allowlist leaves out, beyond the MCP
    servers this leg passed and this run's own bridges.

    Both goose profiles (the container leg and the host twin) carry it as
    ``verify_loaded``, beside ``session_mode`` = ``GOOSE_MODE`` (required
    under ask, where it is ``approve``): ``GOOSE_MODE`` and ``EXTENSIONS``
    apply only to a NEW session, while ``session/load`` restores the mode and
    the extensions the session was saved with. So the profile selects the
    mode after either — under ask a session that could not be switched does
    not start — and a loaded session carrying an extension or tool this
    leg's allowlist drops is replaced by a new one."""
    from ...cli_container import run_bridge_urls

    if session.commands == "allow":
        return ""
    reply = await asyncio.wait_for(session.conn.ext_method(
        "goose/unstable/session/extensions/list",
        {"sessionId": session.acp_session_id}), timeout=30)
    entries = reply.get("extensions") if isinstance(reply, dict) else None
    if not isinstance(entries, list):
        return "goose did not list its extensions"
    extra = extension_violations(entries, session.commands,
                                 servers=session.mcp_server_names,
                                 bridge_urls=run_bridge_urls())
    if extra:
        return (f"it carries {', '.join(extra)}, which commands: "
                f"{session.commands} does not load")
    return ""


def _version_tuple(text: str) -> tuple:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(p) for p in match.groups()) if match else ()


def host_commands_level(commands: str, version: str) -> tuple:
    """``(level, warning)`` a standard-runtime goose can enforce: deny and ask
    rest on mechanisms verified from goose 1.50 on, so an older (or
    unversioned) goose is refused under them — deny has no stricter level to
    fall back to, and ask's fallback would need the same mechanisms."""
    if commands == "allow":
        return commands, ""
    found = _version_tuple(version)
    if found and found >= HOST_MIN_VERSION:
        return commands, ""
    need = ".".join(str(p) for p in HOST_MIN_VERSION)
    raise ValueError(
        f"commands: {commands} needs goose {need} or newer on this machine to be "
        f"enforced, and this machine has goose {version or '(unknown version)'} — "
        f"update goose, or run it in Docker")


def _stringify(v) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v)
    except (TypeError, ValueError):
        return str(v)


def _goose_record_entries(text: str) -> list:
    """The message entries of goose's ``session export --format json``
    document — ``{"messages": [...]}``, possibly nested under ``session``, or
    a bare list. Anything else holds no entries."""
    try:
        doc = json.loads(text or "")
    except (ValueError, TypeError):
        return []
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for holder in (doc, doc.get("session")):
            if not isinstance(holder, dict):
                continue
            for key in ("messages", "conversation", "history"):
                if isinstance(holder.get(key), list):
                    return holder[key]
    return []


def trace_from_goose_session(record: str) -> list:
    """Normalize a goose session export (:func:`goose_session_export`) into
    the shared trace vocabulary (``user`` / ``text`` / ``tool_use`` /
    ``tool_result``).

    Goose's export schema isn't fully documented, so parse tolerantly: each
    message is a bare ``{"role", "content"}`` or wrapped in a
    ``{"message": {...}}``. ``content`` is a string OR a list of parts; a text
    part maps to ``user``/``text`` by role, a ``toolRequest`` part to
    ``tool_use``, a ``toolResponse`` part to ``tool_result`` (regardless of the
    carrying role — goose feeds tool results back as user turns). A record
    that does not parse and non-dict entries are skipped."""
    trace: list = []
    for entry in _goose_record_entries(record):
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        role = str(msg.get("role") or entry.get("role") or "").lower()
        content = msg.get("content")
        if content is None:
            content = entry.get("content")
        if isinstance(content, str):
            if content.strip():
                trace.append({"type": "user" if role == "user" else "text", "text": content})
            continue
        for part in content if isinstance(content, list) else []:
            if isinstance(part, str):
                if part.strip():
                    trace.append({"type": "user" if role == "user" else "text", "text": part})
                continue
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type") or "")
            if ptype == "text":
                txt = part.get("text")
                if isinstance(txt, str) and txt.strip():
                    trace.append({"type": "user" if role == "user" else "text", "text": txt})
            elif ptype in ("toolRequest", "tool_request", "toolCall", "tool_use"):
                tc = part.get("toolCall") or part.get("tool_call") or part
                val = tc.get("value") if isinstance(tc, dict) and isinstance(tc.get("value"), dict) else tc
                name = args = None
                if isinstance(val, dict):
                    name = val.get("name") or part.get("name")
                    args = val.get("arguments") if "arguments" in val else val.get("input")
                trace.append({"type": "tool_use", "tool": name or "", "input": args})
            elif ptype in ("toolResponse", "tool_response", "toolResult", "tool_result"):
                tr = part.get("toolResult") or part.get("tool_result") or part
                val = tr.get("value") if isinstance(tr, dict) and "value" in tr else tr
                name = part.get("name") or (tr.get("name") if isinstance(tr, dict) else "")
                trace.append({"type": "tool_result", "tool": name or "", "output": _stringify(val)})
    return trace


# ──────────────────────────────────────────────────────────────────────
# Terminal runner — a live `goose session` TTY
# ──────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def terminal_context(prepared: PreparedHarness, request, bridge):
    """Own Goose's native argv, pre-arm snapshot and best-effort session export."""
    from ...terminal_runner import TerminalLaunch

    env = prepared.env_obj
    resume_harness_session_id = request.resume_harness_session_id
    command = list(prepared.argv)
    if resume_harness_session_id:
        command += ["--resume", "--session-id", resume_harness_session_id]
        _secret_log(f"the terminal runs on goose session {resume_harness_session_id[:8]}…")
    if bridge:
        command += ["--with-streamable-http-extension", bridge.url_container]
    term_env = {"TERM": "xterm-256color", **prepared.env}
    # Goose snapshots before export is armed. An unexpected listing exception
    # or cancellation still retires the acquired bridge, without exporting.
    before = None if resume_harness_session_id else await asyncio.to_thread(
        _listed_before, env)
    try:
        yield TerminalLaunch(
            container_id=env.container_id, argv=tuple(command), cwd="/workspace",
            env=term_env, label=f"Goose · {prepared.model}",
            buttons=[],
            ready_status="Goose terminal ready — open the cell to attach.",
            harness_session_id=resume_harness_session_id, before=before,
        )
    finally:
        def _teardown() -> None:
            try:
                _finalize_goose_terminal(env, harness_session_id=resume_harness_session_id,
                                         before=before)
            except Exception as e:  # noqa: BLE001 — teardown is best-effort
                _secret_log(f"goose finalize failed: {e}")

        await asyncio.to_thread(_teardown)
        # Cached environment lifetime remains the outer agent's responsibility.


def harness_session_ids(env) -> frozenset:
    """The ids of the sessions in this cell's goose store, via ``goose
    session list --format json``. Raises when the store cannot be listed —
    an empty answer from a failed listing would make every session look
    new. A listing with no JSON in it is an empty store only when it is
    blank or goose's empty-store notice (``No sessions found``); any other
    output, or JSON that does not parse, raises. A banner before the JSON is
    skipped."""
    rc, listing = env.exec(
        ["sh", "-c", "goose session list --format json 2>/dev/null"])
    if rc != 0:
        raise RuntimeError(f"`goose session list` exited {rc}")
    text = listing or ""
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0),
                default=-1)
    if start < 0:
        if text.strip().rstrip(".").lower() not in ("", "no sessions found"):
            raise RuntimeError(
                f"`goose session list` printed no JSON: {text.strip()[:120]!r}")
        return frozenset()
    rows, _end = json.JSONDecoder().raw_decode(text[start:])
    rows = rows if isinstance(rows, list) else rows.get("sessions") if isinstance(rows, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("`goose session list` returned a non-list session store")
    return frozenset(
        str(row.get("id") or row.get("session_id") or "")
        for row in rows if isinstance(row, dict)) - {""}


def _listed_before(env) -> "frozenset | None":
    """:func:`harness_session_ids` when a fresh TTY starts, or ``None`` (logged) when
    the store cannot be listed — the leg then exports nothing."""
    try:
        return harness_session_ids(env)
    except Exception as exc:  # noqa: BLE001 — the TTY still starts
        _secret_log(f"goose: the session store could not be listed ({exc}) — "
                "this terminal's conversation will not be exported")
        return None


def _tty_session(env, before: "frozenset | None") -> str:
    """The session a fresh TTY wrote: the ONE id in the store now that
    ``before`` lacks (``new_tty_session``), ``""`` — logged — when there is
    no snapshot, none appeared, or several did."""
    from ... import new_tty_session

    if before is None:
        return ""
    now = harness_session_ids(env)
    sid = new_tty_session(before, now)
    if not sid:
        _secret_log(f"goose: the terminal wrote {len(now - before)} new "
                "session(s) — none exported")
    return sid


def goose_session_export(env, harness_session_id: str) -> str:
    """Session ``harness_session_id``'s record as goose's own ``session export
    --format json`` document (goose keeps sessions in a SQLite store, and the
    CLI is the one reader that knows the store's layout). ``""`` without an
    id, or when there is nothing to export."""
    sid = (harness_session_id or "").strip()
    if not sid:
        return ""
    # Through a file read whole: the host keeps only the tail of a long
    # exec output, and a day-long session's export outgrows it.
    path = f"/tmp/collimate-goose-export-{os.urandom(8).hex()}.json"
    env.exec([
        "sh", "-c",
        f"goose session export --session-id {shlex.quote(sid)} --format json "
        f"> {shlex.quote(path)} 2>/dev/null",
    ])
    try:
        text = env.read_file(path).decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return ""
    finally:
        try:
            env.exec(["rm", "-f", path])
        except Exception:  # noqa: BLE001 — a leftover in the container's /tmp
            pass
    start = text.find("{")
    return text[start:] if start >= 0 else ""


def _finalize_goose_terminal(env, *, harness_session_id: str = "",
                             before: "frozenset | None" = None) -> None:
    """Export the session transcript, then persist the run's raw record
    (goose's own session export + the normalized trace) — the shared
    terminal-teardown shape.

    The session exported is the leg's own: ``harness_session_id``, the one the TTY
    ran on, or — a fresh TTY — the ONE session that appeared in the store
    after ``before`` (its ids when the leg started). With no id, no snapshot,
    or several new sessions, nothing is exported: an earlier run's
    conversation is never written as this one's."""
    from ...agent_events import TRACE_FILE
    from ...terminal_tail import messages_from_trace

    record = ""
    try:
        sid = harness_session_id or _tty_session(env, before)
        record = goose_session_export(env, sid)
    except Exception as e:  # noqa: BLE001
        _secret_log(f"goose session read failed: {e}")
    if not record:
        _secret_log("goose: no session found to export")
    trace = trace_from_goose_session(record) if record else []

    # Open-format transcript: user + assistant TEXT only (tool/thinking parts
    # stay in the persisted trace, not the plain chat log).
    msgs = messages_from_trace(trace)
    if msgs:
        try:
            from ...transcript_merge import write_merged_transcript
            added = write_merged_transcript(msgs, path=_TRANSCRIPT_FILE)
            _secret_log(f"{_TRANSCRIPT_FILE}: {added} message(s) added from the "
                    "goose session")
        except Exception as e:  # noqa: BLE001
            _secret_log(f"goose transcript export failed: {e}")

    if not record:
        return
    # The durable raw record: goose's own session export plus its normalized
    # trace. Any mining over them is post-hoc, outside the run.
    try:
        _write_log(_SESSION_FILE, record, category="log")
        if trace:
            _write_log(TRACE_FILE, json.dumps(trace, default=str), category="log")
    except Exception as e:  # noqa: BLE001
        _secret_log(f"goose session record export failed: {e}")
