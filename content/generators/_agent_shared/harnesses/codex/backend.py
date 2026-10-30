# SPDX-License-Identifier: MIT
"""Codex terminal, configuration, and session-record helpers.

Authentication comes from the model account selected for the run. Key manager
owns login; subscription credentials are leased into temporary host storage
shared by the account’s active runs. Harness homes are temporary and only
session history is retained between runs. ACP and terminal surfaces use the
same credential bindings and workspace-staged session homes.
"""
from __future__ import annotations
import re
from _agent_shared.logging import safe_log as _secret_log
from _agent_shared.secret_hygiene import write_log as _write_log

import asyncio
from contextlib import asynccontextmanager
import json
import os
import shlex

import gen
from collimate.providers import STANDARD_MODEL_ENDPOINTS

from .. import CommandsPolicy, PreparedHarness
from ...harness_files import write_native_file
from ...acp_client import SpawnProfile, spawn_container_adapter, spawn_host_adapter


# The container path of the ONE CODEX_HOME Collimate owns — the per-cell
# harness home linked here through the workspace under BOTH auths. It is never the user's real
# ~/.codex: mounting that would hand the agent the user's settings, MCP config
# and whole session history, and — far worse — the live credential chain the
# account's rotating tokens depend on (which lives in the selected account's
# temporary lease instead).
CODEX_HOME = "/home/coder/.codex-collimate"

# The selected account's shared host lease holds auth.json. A file binding
# exposes it inside this run's CODEX_HOME; Codex's pinned file-store writer
# truncates and rewrites the existing inode. The vendor coordinates refreshes
# through its own file locks.
# The host collects refreshed bytes from the lease even after a killed run.
# All other vendor state stays in the private home, and only explicitly listed
# nonsecret session paths survive in the cache. The seeded config does not
# override cli_auth_credentials_store: Codex's default is its file store
# (verified at 0.154 and 0.156 on 2026-09-22).
# Key manager owns sign-in and persistent credential replacement.

def shared_login_path() -> str:
    """Selected account's host-managed temporary credential file."""
    from ...login_store import login_path
    return login_path("codex")






def codex_volumes(auth: str) -> dict:
    """Declare the staged home and the shared selected credential binding."""
    from ...harness_homes import harness_home

    volumes = {harness_home("codex"): {"bind": CODEX_HOME, "mode": "rw"}}
    # The pipeline refused a plan leg whose lease holds no login.
    if (auth or "").strip().lower() == "chatgpt":
        # cli_container maps this nested credential binding through the
        # workspace-backed home before placing the container.
        volumes[shared_login_path()] = {
            "bind": f"{CODEX_HOME}/auth.json", "mode": "rw"}
    return volumes

# Touched at terminal-session start; the teardown export only considers
# rollouts newer than it (see _newest_codex_session).
_START_MARKER = ".collimate-codex-start"

# The harness-native raw record, kept verbatim beside the normalized trace.
_SESSION_FILE = "_agent/terminal/codex_session.jsonl"


# ──────────────────────────────────────────────────────────────────────
# Pure helpers (unit-tested; no docker / gen / network).
# ──────────────────────────────────────────────────────────────────────

def _toml_str(v) -> str:
    """A single TOML value: bools/numbers bare, lists inline, everything else a
    basic (double-quoted) string with the escapes TOML requires."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_str(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


# ──────────────────────────────────────────────────────────────────────
# The commands grant (deny < ask < allow) in codex's own terms.
# ──────────────────────────────────────────────────────────────────────

# The requirements file codex reads as a HARD lock (it outranks every config
# layer, and a turn asking for anything outside it fails). Root-owned in the
# image; rewritten as root on every leg.
REQUIREMENTS_PATH = "/etc/codex/requirements.toml"

# codex-acp's stock session-mode presets the commands levels run in.
# read-only: on-request approvals answered by the user, workspace-write
# sandbox. agent-full-access: no approvals, no sandbox.
ASK_MODE = "read-only"
FULL_ACCESS_MODE = "agent-full-access"

# What commands: ask means on codex, under codex's own approval model — said
# in the run's warnings and log, and in the runner guide.
ASK_NOTE = (
    "commands: ask on codex follows codex's own approval model, in the chat "
    "and the terminal alike — a command that stays inside its workspace-write "
    "sandbox runs without asking; one that needs to leave the sandbox is shown "
    "to you for approval first (in the container that sandbox usually cannot "
    "start, so every command asks)")

# What ask adds on this machine (docker: none): no requirements file
# pins unified exec off there, and codex turns it back on unless one does,
# so ``write_stdin`` into a process an approval started is not carded.
HOST_ASK_NOTE = (
    "; with docker: none, input the agent types into a command you "
    "approved runs without a card, because nothing on this machine turns "
    "codex's unified exec off")


def codex_features(commands: str) -> dict:
    """The ``[features]`` codex runs with under a commands level: deny removes
    the shell tool (``exec_command`` and ``write_stdin``); ask and allow leave
    codex's defaults. Code mode — the model's scripted tool runner, a
    JavaScript cell whose only reach is the tools codex already offers — is
    on at every level, as the model's metadata decides: a shell command it
    scripts meets the same gate as one called directly. Hooks keep codex's
    default (on) at every level: they are the cell's own config, and run
    without codex's hook review (see :func:`codex_acp_env` and
    :func:`codex_tui_args`)."""
    return {"shell_tool": False} if commands == "deny" else {}



def codex_acp_env(commands: str) -> dict:
    """The codex-acp env for a commands level: the starting session-mode
    preset (``INITIAL_AGENT_MODE``) and the top config layer
    (``CODEX_CONFIG``, which no config.toml outranks; codex-acp sends it as
    the ``config`` of every ``thread/start`` and ``thread/resume``). deny and
    ask run the ``read-only`` preset with the level's features; deny has no
    shell tool there, and the client gate refuses any command request
    besides. At every level the layer carries ``bypass_hook_trust``, the
    thread-config twin of codex's ``--dangerously-bypass-hook-trust`` (the
    key codex's own TUI and ``codex exec`` send for that flag): the cell's
    hooks are trusted, so they run without codex's per-hook trust review,
    which the chat has no way to show."""
    config: dict = {"bypass_hook_trust": True, **shell_config()}
    features = {**codex_features(commands), "shell_snapshot": False}
    if features:
        config["features"] = features
    mode = FULL_ACCESS_MODE if commands == "allow" else ASK_MODE
    return {"INITIAL_AGENT_MODE": mode, "CODEX_CONFIG": json.dumps(config)}


def codex_requirements_toml(commands: str) -> str:
    """``/etc/codex/requirements.toml`` for a commands level, the same on the
    chat and the terminal leg. deny and ask pin the approval policy both legs
    run — ``on-request`` (the chat's ``read-only`` preset, the terminal's
    ``-a on-request``) — answered by the user (never codex's model reviewer),
    plus unified exec off (no ``write_stdin`` into a process an earlier
    approval started; only requirements can turn it off) and the level's
    features. allow pins nothing."""
    if commands == "allow":
        return "# commands: allow — no requirements\n"
    features = {**codex_features(commands), "unified_exec": False}
    lines = [
        f"# commands: {commands}",
        'allowed_approval_policies = ["on-request"]',
        'allowed_approvals_reviewers = ["user"]',
        "",
        "[features]",
    ]
    lines += [f"{key} = {_toml_str(value)}" for key, value in sorted(features.items())]
    return "\n".join(lines) + "\n"


def write_codex_locks(env, commands: str, codex_home: str) -> None:
    """Every container leg: the requirements lock, written as root, and no
    exec-policy rules carried in (an "always" answer writes one to
    ``$CODEX_HOME/rules``, and a rule runs its commands without asking).
    Raises when the lock can't be written — the run must not start without
    it."""
    from ...cli_container import enforce_in_container, root_file_script

    toml = codex_requirements_toml(commands)
    enforce_in_container(env, " && ".join([
        root_file_script(REQUIREMENTS_PATH, toml),
        f"rm -rf {shlex.quote(codex_home + '/rules')}",
    ]), what="writing codex's commands policy")


def clear_host_rules(codex_home: str) -> None:
    """Remove standing approvals, refusing unsafe retained-home paths."""
    import shutil
    import stat
    from collimate import fs
    if not fs.FD_SAFE:
        root = os.path.abspath(codex_home)
        if os.path.realpath(root) != root:
            raise PermissionError("Codex home contains a linked path")
        path = os.path.join(root, "rules")
        try:
            if os.path.islink(path):
                os.unlink(path)
            else:
                shutil.rmtree(path)
        except FileNotFoundError:
            pass
        return
    parts = fs.relative_parts(os.path.abspath(codex_home).lstrip(os.sep)) + ['rules']
    parent = fs.open_parent(os.path.abspath(os.sep), parts, create=False)
    try:
        try:
            node = os.stat('rules', dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(node.st_mode):
            shutil.rmtree('rules', dir_fd=parent)
        else:
            os.unlink('rules', dir_fd=parent)
    finally:
        os.close(parent)


# The cell trusted, as the chat leg trusts it: /workspace a trusted project
# (codex-acp opens every session root that way), so the cell's .codex/
# config, hooks and exec-policy rules load and codex shows no trust screen,
# and its hooks run without codex's per-hook trust review
# (--dangerously-bypass-hook-trust). The path segment is written bare:
# quoted (`projects."/workspace".…`), `-c` does not match the project
# (codex 0.154).
_TRUST_CELL = ["-c", 'projects./workspace.trust_level="trusted"',
               "--dangerously-bypass-hook-trust"]


def codex_tui_args(commands: str) -> list:
    """The TUI's flags for a commands level — the chat leg's model, in a TTY.
    The cell is trusted at every level (:data:`_TRUST_CELL`). deny and ask:
    the chat's ``read-only`` preset as flags — ``on-request`` approvals
    answered by the user in this TTY, in the ``workspace-write`` sandbox
    (the requirements pin the policy and the reviewer) and, under deny, no
    shell tool. allow: no prompts, no sandbox."""
    trust = list(_TRUST_CELL)
    if commands == "allow":
        return [*trust, "--dangerously-bypass-approvals-and-sandbox"]
    args = [*trust, "-a", "on-request", "-s", "workspace-write",
            "-c", 'approvals_reviewer="user"']
    for feature, enabled in sorted(codex_features(commands).items()):
        if not enabled:
            args += ["--disable", feature]
    return args


def codex_config_toml(
    *, provider: str,
    model: str = "", websearch: "bool | None" = None, key: str = "",
    openai_url: bool = False,
) -> str:
    """The ``$CODEX_HOME/config.toml`` text for a run, or ``""`` when nothing
    needs writing (openai provider, no model, no web setting).

    A non-openai provider (openrouter / groq) gets a ``[model_providers.collimate]``
    table on the Responses wire plus ``model_provider = "collimate"``; openai is
    native and takes none. Tables cannot be expressed as
    ``-c key=value`` overrides, which is exactly why they land here (in a
    fresh CODEX_HOME) instead."""
    prov = (provider or "openai").strip().lower()
    lines: list = []
    # Top-level keys FIRST — TOML assigns any key after a [table] header to
    # that table, so a trailing `model = …` would silently land inside the
    # provider table (and kill codex core at config parse).
    if model:
        lines.append(f"model = {_toml_str(model)}")
    if prov == "openai" and openai_url and STANDARD_MODEL_ENDPOINTS.get("openai"):
        # Native openai (API key) bills the registry endpoint through codex's
        # own ``openai_base_url`` key; ChatGPT sign-in keeps its backend.
        lines.append(f"openai_base_url = {_toml_str(STANDARD_MODEL_ENDPOINTS['openai'])}")
    if prov != "openai":
        # Every other provider the adapter bills is a Responses-wire table
        # at its standard endpoint.
        base_url = STANDARD_MODEL_ENDPOINTS[prov]
        lines += [
            'model_provider = "collimate"',
            "",
            "[model_providers.collimate]",
            f'name = "{prov}"',
            f'base_url = "{base_url}"',
            f'experimental_bearer_token = {_toml_str(key)}',
            'wire_api = "responses"',
        ]
    if websearch is not None:
        if lines:
            lines.append("")
        lines += ["[tools]", f"web_search = {_toml_str(bool(websearch))}"]
    return ("\n".join(lines) + "\n") if lines else ""


def _codex_content_text(content) -> str:
    """Flatten a rollout ``content`` / ``summary`` field (a string, or a list of
    ``{text}`` / ``{content}`` blocks, or plain strings) into one text string."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("text") or b.get("content") or ""
                if isinstance(t, str) and t.strip():
                    parts.append(t)
            elif isinstance(b, str) and b.strip():
                parts.append(b)
        return "\n".join(parts).strip()
    return ""


def trace_from_codex_session(jsonl: str) -> list:
    """Normalize a codex TUI session rollout (``$CODEX_HOME/sessions/**/*.jsonl``)
    into the shared trace vocabulary — the terminal mode's fullest-fidelity
    record. The rollout schema differs from ``codex exec --json``: lines carry
    response items, often under a ``{"payload": …}`` wrapper. Parse tolerantly:
    ``message`` (role user → ``user``, else ``text``), ``reasoning`` →
    ``thinking``, ``function_call`` / shell calls → ``tool_use``, their outputs
    → ``tool_result``. Best-effort — unrecognized / unparseable lines drop."""
    trace: list = []
    for line in (jsonl or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:  # noqa: BLE001 — tolerate garbage / partial lines
            continue
        if not isinstance(entry, dict):
            continue
        item = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
        itype = item.get("type") or ""
        if itype == "message":
            role = str(item.get("role") or "").lower()
            text = _codex_content_text(item.get("content"))
            if text:
                trace.append({"type": "user" if role == "user" else "text", "text": text})
        elif itype == "reasoning":
            text = _codex_content_text(item.get("summary")) or \
                _codex_content_text(item.get("content"))
            if text:
                trace.append({"type": "thinking", "text": text})
        elif itype in ("function_call", "local_shell_call", "custom_tool_call"):
            name = item.get("name") or item.get("tool") or "tool"
            args = item.get("arguments")
            if args is None:
                args = item.get("input") or item.get("action")
            trace.append({"type": "tool_use", "tool": str(name), "input": args})
        elif itype in (
            "function_call_output", "custom_tool_call_output", "local_shell_call_output",
        ):
            out = item.get("output")
            if isinstance(out, dict):
                out = out.get("content") or out.get("output") or json.dumps(out)
            trace.append({"type": "tool_result", "output": str(out or "")})
    return trace


# ──────────────────────────────────────────────────────────────────────
# Terminal mode — a live `codex` TUI in the container.
# ──────────────────────────────────────────────────────────────────────


def shell_config() -> dict:
    """Pinned native shell policy; snapshots would restore excluded parent vars."""
    return {"allow_login_shell": False, "shell_environment_policy": {
        "inherit": "none", "set": {name: value for name, value in {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", ""), "USER": os.environ.get("USER", ""),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp")}.items() if value}}}


async def prepare(ctx: dict, *, leg: str = "acp") -> PreparedHarness:
    """One native credential, config and commands preparation for every leg."""
    from ...cli_container import ensure_env
    from ...harness_homes import harness_home
    from ...host_adapters import (host_env, link_login, ensure_adapter,
                               resolve_adapter_version)
    provider, auth = ctx["provider"], ctx["auth"]
    key = ctx["credentials"].key
    home = await asyncio.to_thread(harness_home, "codex")
    host = ctx.get("runtime") == "standard"
    if host and leg == "terminal":
        raise ValueError("codex terminal needs docker: full")
    native = codex_acp_env(ctx["commands"])
    # The execution environment refers to the selected locus, never the SDK host.
    policy_config = json.loads(native["CODEX_CONFIG"])
    if not host:
        policy_config["shell_environment_policy"]["set"] = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/coder", "USER": "coder", "TMPDIR": "/tmp"}
    native["CODEX_CONFIG"] = json.dumps(policy_config)
    policy = CommandsPolicy(ctx["commands"], native)
    model = (ctx.get("model") or "").strip()
    toml = codex_config_toml(provider=provider, model=model,
                             websearch=ctx.get("websearch"), key=key,
                             openai_url=auth != "chatgpt")
    # The TUI consumes identical native shell policy from its private config.
    # Keep top-level overrides before provider and tools tables.
    shell = policy_config["shell_environment_policy"]
    preamble = '[features]\nshell_snapshot = false\n'
    preamble += '[shell_environment_policy]\ninherit = "none"\n[shell_environment_policy.set]\n'
    preamble += ''.join(f'{name} = {_toml_str(value)}\n' for name, value in shell['set'].items())
    # Config rendering places native tables after existing model/provider settings.
    toml = 'allow_login_shell = false\n' + toml + '\n' + preamble
    await asyncio.to_thread(write_native_file, home, "config.toml", toml.encode())
    if auth != "chatgpt":
        credentials = json.dumps({"OPENAI_API_KEY": key}) if provider == "openai" else '{}'
        await asyncio.to_thread(write_native_file, home, "auth.json", credentials.encode())
    env_obj = None
    env_vars = {"NO_BROWSER":"1", "CODEX_HOME":home if host else CODEX_HOME}
    version = ""
    if host:
        found = ctx['host_harness']
        version = await asyncio.to_thread(resolve_adapter_version, "codex-acp")
        adapter = await asyncio.to_thread(ensure_adapter, "codex-acp", version)
        await asyncio.to_thread(clear_host_rules, home)
        if auth == "chatgpt":
            await asyncio.to_thread(link_login, shared_login_path(), os.path.join(home, "auth.json"))
        env_vars = await asyncio.to_thread(host_env, **env_vars, CODEX_PATH=found['path'], **native)
        argv, cwd = [adapter], gen.workspace_path()
        _secret_log(f"standard: running codex {found['version']} at {found['path']} (this machine's own copy, not managed by Collimate)")
    else:
        volumes = await asyncio.to_thread(codex_volumes, auth)
        env_obj = await ensure_env('codex', build_dir='codex', volumes=volumes)
        await asyncio.to_thread(write_codex_locks, env_obj, policy.level, CODEX_HOME)
        env_vars.update(native if leg == 'acp' else {})
        argv, cwd = ['codex-acp'] if leg == 'acp' else ['codex', *codex_tui_args(policy.level)], '/workspace'
    async def spawn():
        if host:
            return await spawn_host_adapter('codex', argv, env=env_vars, cwd=cwd)
        return await spawn_container_adapter('codex', env_obj, argv, env_vars=env_vars)
    profile = None if leg == 'terminal' else SpawnProfile(
        locus='host' if host else 'container', cwd=cwd, spawn=spawn, commands=policy.level,
        env_obj=env_obj, bind_host='127.0.0.1' if host else None,
        adapter_version=version, model_applied=bool(model))
    prepared = PreparedHarness(policy, profile, env_obj, tuple(argv), env_vars, model)
    if leg == 'terminal':
        # The marker belongs to successful terminal preparation, before any
        # listening bridge. ACP/host preparation never advances this marker.
        from collimate import fs
        await asyncio.to_thread(fs.write_file, home, _START_MARKER, "")
    return prepared


def write_terminal_bridge(home: str, url: str | None) -> None:
    """Append only this leg's server to fresh, confined native config bytes."""
    from collimate import fs
    import tomllib

    fd = fs.open_file(home, 'config.toml', write=False)
    with os.fdopen(fd, 'rb') as handle:
        body = handle.read()
    config = tomllib.loads(body.decode('utf-8'))
    servers = config.get('mcp_servers', {})
    if not isinstance(servers, dict) or 'collimate' in servers:
        raise ValueError('Codex prepared config already contains an own MCP server')
    if url is not None:
        body += ('\n[mcp_servers.collimate]\nurl = ' + _toml_str(url) + '\n').encode()
        write_native_file(home, 'config.toml', body)


@asynccontextmanager
async def terminal_context(prepared: PreparedHarness, request, bridge):
    """Bind Codex's own carrier, launch its exact thread and own native cleanup."""
    from ...cli_container import inject_context_file, remove_context_file
    from ...harness_homes import harness_home
    from ...terminal_runner import TerminalLaunch

    env, model = prepared.env_obj, prepared.model
    home = await asyncio.to_thread(harness_home, 'codex')
    await asyncio.to_thread(write_terminal_bridge, home,
                            bridge.url_container if bridge else None)
    # Binding errors precede both export and workspace-context cleanup arming.
    wrote_seam = False
    try:
        extra_system = request.extra_system
        wrote_seam = bool(extra_system.strip())
        if extra_system and not wrote_seam:
            injection = asyncio.create_task(asyncio.to_thread(
                inject_context_file, env, "/workspace/AGENTS.md", extra_system))
            try:
                await asyncio.shield(injection)
            except asyncio.CancelledError:
                # A worker cannot be cancelled mid-write. Settle it before
                # native cleanup so it cannot recreate the removed context.
                while not injection.done():
                    try:
                        await asyncio.shield(injection)
                    except asyncio.CancelledError:
                        continue
                injection.result()
                raise
        tui_args = ["-c", "check_for_update_on_startup=false", *prepared.argv[1:]]
        resume_harness_session_id = request.resume_harness_session_id
        if resume_harness_session_id:
            # Resume keeps the native thread's own model; no -m override.
            command = ["codex", "resume", *tui_args, resume_harness_session_id]
            _secret_log(f"the terminal runs on codex thread {resume_harness_session_id[:8]}…")
        else:
            command = ["codex", *tui_args] + (["-m", model] if model else [])
        yield TerminalLaunch(
            container_id=env.container_id, argv=tuple(command), cwd="/workspace",
            env={"TERM": "xterm-256color", **prepared.env},
            label=f"Codex · {model or 'default'}",
            buttons=[],
            ready_status="Codex terminal ready — open the cell to attach.",
            harness_session_id=resume_harness_session_id,
        )
    finally:
        def _teardown() -> None:
            try:
                _finalize_codex_terminal(env, CODEX_HOME)
            except Exception as e:  # noqa: BLE001 — native teardown remains best-effort
                _secret_log(f"codex teardown failed: {e}")
            if not wrote_seam:
                try:
                    remove_context_file(env, "/workspace/AGENTS.md")
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.to_thread(_teardown)


def newest_codex_session_path(store_dir: str) -> str:
    """Newest regular rollout touched after this leg's host marker, or empty."""
    import stat

    try:
        marker = os.stat(os.path.join(store_dir, _START_MARKER), follow_symlinks=False)
    except OSError:
        return ""
    if not stat.S_ISREG(marker.st_mode):
        return ""
    newest = (marker.st_mtime_ns, "")
    for root, dirs, files in os.walk(os.path.join(store_dir, "sessions"), followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mtime_ns > newest[0]:
                newest = (info.st_mtime_ns, path)
    return newest[1]


def _newest_codex_session(store_dir: str) -> str:
    """Whole host-side export at teardown; the live tail uses incremental reads."""
    from collimate import fs
    path = newest_codex_session_path(store_dir)
    if not path:
        return ""
    try:
        fd = fs.open_file(store_dir, os.path.relpath(path, store_dir), write=False)
        with os.fdopen(fd, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def _finalize_codex_terminal(env, codex_home: str) -> None:
    """Teardown: export the terminal transcript (a log artifact) and persist the
    run's raw record (the rollout jsonl + its normalized trace) — all
    best-effort, each independently guarded so one failure doesn't sink the rest."""
    from ...agent_events import TRACE_FILE
    from ...terminal_tail import messages_from_trace

    from ...harness_homes import harness_home
    raw = _newest_codex_session(harness_home("codex"))
    if not raw:
        return
    trace = trace_from_codex_session(raw)

    try:
        messages = messages_from_trace(trace)
        if messages:
            from ...transcript_merge import write_merged_transcript
            added = write_merged_transcript(messages)
            _secret_log(f"terminal transcript: {added} message(s) added from "
                    "the codex session")
    except Exception as e:  # noqa: BLE001
        _secret_log(f"codex transcript export failed: {e}")

    # The durable raw record: codex's own rollout jsonl plus its normalized
    # trace. Any mining over them is post-hoc, outside the run.
    try:
        _write_log(_SESSION_FILE, raw, category="log")
        if trace:
            _write_log(TRACE_FILE, json.dumps(trace, default=str), category="log")
    except Exception as e:  # noqa: BLE001
        _secret_log(f"codex session record export failed: {e}")


def commands_note(commands: str, runtime: str) -> str:
    """The run warning for ``commands`` on codex ("" below ask): what ask
    means under codex's own approval model, and what it adds on this
    machine."""
    if commands != "ask":
        return ""
    return ASK_NOTE + (HOST_ASK_NOTE if runtime == "standard" else "")


# codex-acp forwards codex's own startup warning for a model outside codex's
# built-in catalog (every openrouter/groq id) as an agent_message_chunk. It is
# operator information, not the model's words: it goes to the run log, never
# into the reply or the transcript.
_METADATA_WARNING = re.compile(
    r"Warning: Model metadata for `[^`\n]*` not found\.[^\n]*(?:\n+|$)")


def drop_startup_warning(norm: dict) -> dict:
    """The ``normalize_update`` hook: a normalized update without codex's
    model-metadata startup warning."""
    events, trace = [], []
    dropped = False
    for bucket, out, key in ((norm["events"], events, "text_delta"),
                             (norm["trace"], trace, "text")):
        for ev in bucket:
            if ev.get("type") == key and isinstance(ev.get("text"), str):
                text = ev["text"]
                m = _METADATA_WARNING.match(text.lstrip())
                if m:
                    dropped = True
                    rest = text.lstrip()[m.end():]
                    if not rest:
                        continue
                    ev = {**ev, "text": rest}
            out.append(ev)
    if dropped:
        _secret_log("acp[codex]: codex has no metadata for this model and is using "
                    "its fallback metadata (warning kept out of the reply)")
    return {**norm, "events": events, "trace": trace}


def is_mcp_tool_approval(session, meta: dict) -> bool:
    """The ``tool_approval`` hook: codex asks before any MCP tool that is not
    annotated read-only (every Collimate bridge tool among them), and
    codex-acp sends that as kind ``execute`` — titled "MCP tool call
    approval", or untitled when it correlates with the tool call — with
    ``_meta.is_mcp_tool_approval``. It runs a tool, not a command, so the
    ``commands`` level does not decide it."""
    return meta.get("is_mcp_tool_approval") is True
