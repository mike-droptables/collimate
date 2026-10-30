# SPDX-License-Identifier: MIT
"""Antigravity: the ACP server's native configuration, the terminal leg, and
the CLI's session-record decoder.

The chat and annotation legs run Google's official ACP server
(``antigravity-acp``, registry id of the same name) in the harness container.
Authentication comes from the model account selected for the run: a Gemini
key rides ``GEMINI_API_KEY`` with ``auth.type`` ``gemini-api-key`` in the
server's ``settings.json``; the ``google`` plan leases the server's own token
document (``antigravity-acp/acp_token.json``) into temporary host storage
shared by the account's active runs. Harness homes are temporary and only
session history is retained between runs.

The terminal leg runs Google's ``agy`` CLI. On a Gemini key it runs in the
harness container like the server. On the ``google`` plan it is the
unmodified ``agy`` on this machine (``cli_host``), with HOME the entry's
per-account home, signed in through Google's own loopback browser flow,
which can complete only where the browser is; Collimate hands it no
credential and its sign-in persists in that home across runs.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log
from _agent_shared.secret_hygiene import write_log as _write_log


import asyncio
from contextlib import asynccontextmanager
import json
import os
import sqlite3


from ...acp_client import AdapterProcess, SpawnProfile, spawn_container_adapter
from .. import CommandsPolicy, PreparedHarness
from ...login_store import write_private_atomic
from ...transcript_merge import TERMINAL_TRANSCRIPT as _TRANSCRIPT_FILE

_HOME = "/home/coder"

# The protected config root in the disposable container. Only its vendor state
# subdirectory links into this run's workspace; hooks and rules stay root-owned.
ANTIGRAVITY_HOME = f"{_HOME}/.gemini"

# agy's own state directory inside that home (the terminal leg's): settings.json,
# the installation id, the logs and the conversation store.
STATE_DIR = "antigravity-cli"

# The ACP server's private directory (``$GEMINI_HOME/antigravity-acp``; the
# image sets GEMINI_HOME to ANTIGRAVITY_HOME): its settings.json, its token
# document, its trusted workspaces, and its conversation store
# (``conversations/``, separate from the CLI's). It links into the run's
# private workspace like the CLI's.
ACP_STATE_DIR = "antigravity-acp"
ACP_SETTINGS_PATH = f"{ANTIGRAVITY_HOME}/{ACP_STATE_DIR}/settings.json"

# The server's own token document: a Google ``authorized_user`` JSON it
# refreshes in place (Linux always stores it as this file). The google plan's
# login IS this document (``login_store.login_path("antigravity")``). Each run
# writes a 0600 COPY of it into its temporary harness home and
# ``register_materialized_copy`` flushes a refreshed copy back at release.
# Copies, not one shared inode (the opposite of codex), are safe because
# Google's refresh tokens do not rotate: a refresh returns a new access token
# and leaves the refresh token standing.
ACP_TOKEN_FILE = "acp_token.json"

# The ACP server, as installed by the image (root-owned, pinned): the argv the
# adapter declares, with ``--uid=`` as the registry manifest's Linux command.
SERVER_BIN = "/opt/agy-acp/agy_acp_server.par"
ACP_ARGV = (SERVER_BIN, "--uid=")

# ``settings.json`` ``auth.type`` per Collimate auth. ``agent_platform``
# (Gemini Enterprise Agent Platform, formerly Vertex AI) is written when asked
# for but no Key manager entry selects it: the roster has no Vertex provider
# row, so no run reaches it today.
ACP_AUTH_TYPES = {"api_key": "gemini-api-key", "google": "oauth-personal",
                  "agent_platform": "agent-platform"}

# The terminal leg's seams: the per-cell settings file, agy's global MCP
# config, and the global rules file. They sit at fixed paths because
# ``ensure_env`` caches one container per backend and whichever leg starts
# second inherits the first's arrangement (the ACP server ignores all three:
# it takes its prompt and MCP servers over ACP).
SETTINGS_PATH = f"{ANTIGRAVITY_HOME}/{STATE_DIR}/settings.json"
MCP_CONFIG_PATH = f"{ANTIGRAVITY_HOME}/config/mcp_config.json"
# agy reads GEMINI.md / AGENTS.md in the workspace, ``.agents/rules/*.md``
# beside it, and GLOBAL rules under the customization root
# (``~/.gemini/config/``). The global one is the seam Collimate owns: it lands
# in the per-cell home instead of littering the user's cell with a file the
# agent might also edit.
CONTEXT_SEAM_PATH = f"{ANTIGRAVITY_HOME}/config/rules/collimate-context.md"

# agy's GLOBAL hooks file (the customization root inside the per-cell home)
# and the per-cell project overrides beside it. Project settings take
# precedence over the global ones, so each leg clears them.
HOOKS_PATH = f"{ANTIGRAVITY_HOME}/config/hooks.json"

# Collimate's PreToolUse hook (``antigravity/image/agy_hook.py``, root-owned in
# the image) and the tools it gates — the same tuple as the hook's own
# EXEC_TOOLS, one matcher group each. ``shell`` is the ACP server's own name
# for its shell (agy calls it ``run_command``; the server knows both).
HOOK_BIN = "/usr/local/bin/collimate-agy-hook"
EXEC_TOOLS = (
    "run_command",
    "shell",
    "manage_task",
    "notebook_execution",
    "execute_browser_javascript",
    "run_extension_code",
    "run_workflow",
    "schedule",
)

# The ACP server's tools that run code (or start work that does) WITHOUT a
# ``session/request_permission`` of their own: ``run_workflow`` runs a Python
# script, ``manage_task`` types into a running command, ``schedule`` queues
# work, and the subagent tools start agents outside the session's approvals.
# Below allow they are withheld (``disabledTools``), the hook denies them, and a
# request that does name one is a command (``recognise.py``).
SERVER_UNASKED_TOOLS = ("run_workflow", "manage_task", "schedule", "define_subagent",
                        "invoke_subagent", "manage_subagents")

# The setting the terminal leg's Gemini key needs. Verified live:
# ``GEMINI_API_KEY`` alone is ignored by the CLI ("authentication failed or
# timed out"); with this key its log says "ChainedAuth: authenticated via
# gemini_api_key".
_MODEL_PROVIDER_KEY = "modelProvider"
_API_KEY_PROVIDER = "gemini"

# The quiet flags the CLI always gets: no tips banner, no feedback survey, no
# desktop notifications. Merged into whatever else the file holds, never
# written over it. agy rewrites settings.json at startup and drops a known key
# that holds its default (``notifications: false`` goes; ``showTips: false``
# stays, its default being on). There is no telemetry key: agy 1.2.11 deletes
# ``enableTelemetry`` whatever its value, and a ``telemetryEnabled`` or
# ``telemetry`` key changes nothing it sends. The data-use question is its
# first-run screen's, skipped below (:data:`_ONBOARDING_DONE`), which records
# no opt-out: there is none to record under an API key.
_QUIET_SETTINGS = {
    "showTips": False,
    "showFeedbackSurvey": False,
    "notifications": False,
}

# agy's first-run screens, answered at every start because the harness home is
# new each run and agy would otherwise ask all three every time. Probed against
# agy 1.2.11 (API-key auth) by walking the screens in a fresh home and diffing:
#
#   * colour scheme: recorded as ``colorScheme`` in settings.json, and only
#     when it is not the default (``terminal``, the first option, is stored as
#     no key at all). Collimate writes no key, so agy's default applies.
#   * "Terms of Service & Data Use": the terms, a security-risk notice, and a
#     box ("allow Google to collect and use my Interactions data") that
#     arrives ticked. Completing the screens writes
#     ``antigravity-cli/cache/onboarding.json`` and nothing else, whichever way
#     the box is answered: a ticked and an unticked run leave the same files
#     (installation ids aside) and send to the same hosts, in the same
#     volumes. The binary carries the answer as a setting of the signed-in
#     Google account (``UserSettings.telemetry_enabled``), so under an API key
#     nothing records it; the google leg is unverified.
#     Writing the completed record SKIPS this screen: Collimate accepts the
#     terms on the user's behalf, and records NO data-use opt-out, because
#     there is no local answer to record. Skipping it only means Collimate
#     never presses Enter on the pre-ticked box. It is not an opt-out:
#     either way agy's telemetry client posts to
#     ``https://play.googleapis.com/log`` on every turn (about 100 KB per turn
#     in the probe), and for a Google-signed-in account the account's own
#     setting, whatever Google's default is, applies.
#     Collimate's policy (decided 2026-09-25): no telemetry block in the
#     container. The decline is the GOOGLE ACCOUNT's setting, made once per
#     account: a user signing in with Google unticks the box on agy's own
#     screen at sign-in (on the host, where agy's home persists), and every
#     run under that login inherits it. An API-key run has no opt-out to make.
#   * folder trust: ``trustedWorkspaces`` in settings.json. The workspace is
#     trusted, as every file in the cell is: that folder only, never a wider
#     path.
_ONBOARDING_FILE = "cache/onboarding.json"
_ONBOARDING_DONE = {
    "consumerOnboardingComplete": True,
    "enterpriseOnboardingComplete": False,
    "onboardingComplete": True,
}
_TRUST_KEY = "trustedWorkspaces"
WORKSPACE_DIR = "/workspace"

# The model value that means "let agy choose" — the profile then passes no
# ``--model`` at all (a blank slug would be an invalid selection).
DEFAULT_MODEL_SENTINEL = "default"

# The server prints this on stderr when it has no usable token and starts a
# browser sign-in no headless run can finish; it then waits for the callback.
BROWSER_SIGN_IN_MARKER = "Open the following link to authenticate the ACP server"
BROWSER_SIGN_IN_REFUSED = (
    "The Google login for Antigravity is missing or expired: the ACP server "
    "asked for a browser sign-in, which a run cannot complete. Sign in once "
    "through the official antigravity-acp server (Zed or JetBrains; on macOS "
    "set AGY_ACP_FORCE_FILE_STORAGE=1 first) and paste the new acp_token.json "
    "into Key manager.")

# The raw harness-native record, kept verbatim beside the normalized trace.
_SESSION_FILE = "_agent/terminal/antigravity_session.json"

# A tool call's raw arguments are unbounded (a whole file's contents can ride
# a write_to_file call); the trace keeps a readable head.
_TOOL_INPUT_CAP = 2000


# ──────────────────────────────────────────────────────────────────────
# The Google login: the leased token and this run's copy.
# ──────────────────────────────────────────────────────────────────────


def shared_login_path() -> str:
    from ...login_store import login_path
    return login_path("antigravity")


def _login_bytes(path: str) -> bytes:
    """The leased token document at ``path``, or ``b""`` when there is nothing
    there (absent, or docker's mountpoint artifact / a truncated write). Its
    shape was checked when it was pasted (``google_authorized_user``)."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:     # absent / unreadable is "no login"
        return b""


def antigravity_volumes(auth: str) -> dict:
    """Stage the writable vendor state of both surfaces (the container is
    shared, so both legs bind the same two directories); policy stays outside
    the workspace. On the google plan the run's copy of the server's token
    document lands in the ACP directory first."""
    from ...harness_homes import harness_home
    from ...login_store import register_materialized_copy
    home = harness_home("antigravity")
    if (auth or "").strip().lower() == "google":
        target = os.path.join(home, ACP_STATE_DIR, ACP_TOKEN_FILE)
        if not os.path.isfile(target):
            write_private_atomic(target, _login_bytes(shared_login_path()))
        register_materialized_copy("antigravity", target, root=home)
    volumes = {}
    for directory in (STATE_DIR, ACP_STATE_DIR):
        state = os.path.join(home, directory)
        os.makedirs(state, mode=0o700, exist_ok=True)
        volumes[state] = {"bind": f"{ANTIGRAVITY_HOME}/{directory}", "mode": "rw"}
    return volumes


# ──────────────────────────────────────────────────────────────────────
# The commands grant: settings permissions, the hook, the protected hooks file.
# ──────────────────────────────────────────────────────────────────────
#
# Verified against agy 1.2.2 (a scripted Gemini endpoint behind
# GOOGLE_GEMINI_BASE_URL): permission rules resolve deny > ask > allow; a
# deny rule holds under --dangerously-skip-permissions and is checked before
# any hook; a PreToolUse hook's deny holds over an allow rule and over the
# skip flag; a hook that exits non-zero, prints a non-decision or times out
# denies; a hook's allow runs a tool in headless mode only where an allow rule
# covers it; force_ask (and an ask rule) prompts even past a persisted or
# per-conversation "always allow" — but not for a tool agy auto-allows
# (``manage_task``), which runs on force_ask or ask with no prompt, so only a
# hook deny holds there (verified in a pty against agy 1.2.1). The TUI's `!`
# bash mode runs the line the human types past both deny rules and hooks:
# that is the human's own command, at every level; the rules and the hook
# bind the agent.


# agy's timeout for Collimate's hook, which answers at once (it never waits
# for the human: the TUI prompts after it, the ACP server's client gate beside).
HOOK_TIMEOUT_S = 30


def hooks_json(commands: str, hook: str = HOOK_BIN) -> dict:
    """The global ``hooks.json`` agy and the ACP server both read, for a
    commands level: under deny / ask one PreToolUse group per exec tool
    running ``<hook> <level>`` (the image's root-owned
    ``collimate-agy-hook``, or :func:`host_hook` on this machine); under allow
    no hook at all."""
    if commands not in ("deny", "ask"):
        return {}
    handler = {"type": "command", "command": f"{hook} {commands}",
               "timeout": HOOK_TIMEOUT_S}
    return {"collimate-commands": {"PreToolUse": [
        {"matcher": f"^{name}$", "hooks": [dict(handler)]}
        for name in EXEC_TOOLS]}}


def permission_settings(commands: str) -> dict:
    """The terminal leg's settings.json keys the commands level owns. The
    human answers agy's own prompts; the only rules are the level's. (The ACP
    server has no such file: its permission requests reach the client.)"""
    rules: dict = {"deny": {"deny": ["command(*)"]},
                   "ask": {"ask": ["command(*)"]}}.get(commands, {})
    out: dict = {"permissions": rules}
    if commands in ("deny", "ask"):
        out["toolPermission"] = "request-review"
        out["enableTerminalSandbox"] = False
    return out


def host_hook() -> str:
    """The hook command on this machine: the same script the image installs
    (stdlib only), run by this interpreter."""
    import shlex
    import sys
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "image", "agy_hook.py")
    return shlex.join([sys.executable, script])


def command_guard_files(commands: str) -> dict[str, str]:
    """Declare immutable policy content; the host owns its read-only source."""
    return {HOOKS_PATH: json.dumps(hooks_json(commands)) + "\n"}


# ──────────────────────────────────────────────────────────────────────
# The config seams: settings.json, mcp_config.json, the global rules file.
# ──────────────────────────────────────────────────────────────────────


_CUSTOM_MODEL_PREFIX = "collimate:model:"


def _owned_model_keys(settings) -> set[str]:
    """Recognize only Collimate's keyed, unmodified native declarations."""
    if not isinstance(settings, dict):
        return set()
    custom = settings.get("customModelsConfig")
    if not isinstance(custom, dict):
        return set()
    entries = custom.get("customModels")
    if not isinstance(entries, dict):
        return set()
    return {key for key, declaration in entries.items()
            if isinstance(key, str) and key.startswith(_CUSTOM_MODEL_PREFIX)
            and key[len(_CUSTOM_MODEL_PREFIX):]
            and declaration == {"modelName": key[len(_CUSTOM_MODEL_PREFIX):],
                                "apiProvider": "API_PROVIDER_GOOGLE_GEMINI"}}


def settings_json(existing: "dict | None", *, commands: str, model: str = "",
                  auth: str = "api_key", workspace: str = WORKSPACE_DIR) -> dict:
    """The terminal leg's ``settings.json`` content — pure, so the merge rule
    is unit-testable without a container.

    MERGED, never clobbered: agy writes its own keys into that file while it
    runs (a colour scheme picked in the TUI, say), and a later leg keeps
    them. We own the quiet flags, ``modelProvider``, the trusted folder, and
    the commands level's keys (:func:`permission_settings`). ``permissions``
    is REPLACED, never merged, so no rule an earlier leg or agy's own
    "always allow" persisted survives into this one; so is
    ``trustedWorkspaces``, which names the leg's ``workspace`` and nothing
    else.

    Under a Gemini key (the container leg) ``modelProvider: "gemini"`` is
    what makes ``GEMINI_API_KEY`` count at all (the key alone is ignored),
    and a chosen model is declared as a Gemini API model. On the ``google``
    plan (the leg on this machine) agy's own sign-in bills: no provider
    override, no key, and no model declaration — a chosen model is one of
    agy's own, passed by name."""
    data = dict(existing) if isinstance(existing, dict) else {}
    data.update(_QUIET_SETTINGS)
    for key in ("toolPermission", "enableTerminalSandbox"):
        data.pop(key, None)
    data.update(permission_settings(commands))
    data[_TRUST_KEY] = [workspace]
    if auth == "api_key":
        data[_MODEL_PROVIDER_KEY] = _API_KEY_PROVIDER
    elif data.get(_MODEL_PROVIDER_KEY) == _API_KEY_PROVIDER:
        data.pop(_MODEL_PROVIDER_KEY)
    # Ownership is the namespaced native key plus our exact declaration.
    # Plain keys and user-modified declarations are never ours to remove.
    custom = data.get("customModelsConfig")
    if custom is not None and not isinstance(custom, dict):
        return data  # Preserve user bytes; native validates an invalid schema.
    custom = dict(custom or {})
    entries = custom.get("customModels")
    if entries is not None and not isinstance(entries, dict):
        return data
    entries = dict(entries or {})
    selected = (model or "").strip()
    selected_key = (_CUSTOM_MODEL_PREFIX + selected
                    if auth == "api_key" and selected and selected != DEFAULT_MODEL_SENTINEL else None)
    for key in _owned_model_keys(data):
        if key != selected_key:
            entries.pop(key)
    if selected_key and selected_key in entries and selected_key not in _owned_model_keys(data):
        raise ValueError("Antigravity's selected Collimate model entry was modified; cannot replace it")
    if selected_key and selected_key not in entries:
        entries[selected_key] = {"modelName": selected,
                                 "apiProvider": "API_PROVIDER_GOOGLE_GEMINI"}
    if entries:
        custom["customModels"] = entries
    else:
        custom.pop("customModels", None)
    if custom:
        data["customModelsConfig"] = custom
    else:
        data.pop("customModelsConfig", None)
    return data


def onboarding_json() -> dict:
    """agy's onboarding record with the first-run screens completed, exactly
    as agy writes it when a user finishes them (``cache/onboarding.json``).
    With it in place agy opens at its prompt: no colour-scheme screen, and no
    "Terms of Service & Data Use" screen with its pre-ticked data-use box.
    That accepts the terms for the user and records no data-use opt-out (see
    the note above :data:`_ONBOARDING_DONE`). ``enterpriseOnboardingComplete``
    stays false as agy wrote it under an API key; whether a Workspace
    account signed in on the google leg then gets a screen of its own is
    unverified."""
    return dict(_ONBOARDING_DONE)


def _read_json(path: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — absent, unreadable, or garbage: start clean
        return None


def _write_json_private(path: str, existing, data: dict) -> None:
    if existing == data:
        return          # already right — don't churn the file on every start
    write_private_atomic(path, (json.dumps(data, indent=2) + "\n").encode())


def write_settings(home: str, *, commands: str, model: str = "", auth: str = "api_key",
                   workspace: str = WORKSPACE_DIR) -> str:
    """Write the CLI's startup state before agy starts, under ``home`` (its
    ``~/.gemini``: the cell's harness home for the container leg, the
    per-account home's for the leg on this machine): ``settings.json``
    (:func:`settings_json`), and, under a Gemini key, the completed
    onboarding record (:func:`onboarding_json`), which together answer the
    three first-run screens. On the ``google`` plan the first-run screens
    are the user's own, answered once in agy's TTY at sign-in — the
    data-use choice there is the Google account's setting — and agy records
    them in the per-account home, so a later run opens at the prompt.

    Written before the CLI starts because agy reads both at startup and the
    settings decide how the very first turn authenticates. 0600 through
    ``mkstemp`` + ``os.replace`` like the credential beside them — not because
    settings are secret, but because a half-written settings.json is a CLI that
    refuses to start, and these files are rewritten on every leg of every run.
    The container's global hooks and instruction rules keep protected
    root-owned parents; native project state has its own writable child
    under that config parent."""
    path = os.path.join(home, STATE_DIR, "settings.json")
    existing = _read_json(path)
    merged = settings_json(existing if isinstance(existing, dict) else None,
                           commands=commands, model=model, auth=auth, workspace=workspace)
    _write_json_private(path, existing, merged)
    for key in sorted(_owned_model_keys(existing) - _owned_model_keys(merged)):
        _secret_log(f"antigravity: removed Collimate model entry {key!r} from settings.json")
    if auth == "api_key":
        path = os.path.join(home, STATE_DIR, *_ONBOARDING_FILE.split("/"))
        _write_json_private(path, _read_json(path), onboarding_json())
    # agy resolves custom models by their map key. The provider receives the
    # declaration's unchanged modelName, while native selection uses our key.
    selected = (model or "").strip()
    owned_key = _CUSTOM_MODEL_PREFIX + selected
    return owned_key if owned_key in _owned_model_keys(merged) else selected


def mcp_config_json(url: str | None) -> str:
    """The terminal leg's single Collimate HTTP bridge, or an empty config.
    (The ACP server takes its servers in ``session/new``.)"""
    from ...mcp_bridge import SERVER_NAME
    if url is not None and not isinstance(url, str):
        raise TypeError("the terminal MCP bridge URL must be text or None")
    servers = {SERVER_NAME: {"disabled": False, "serverUrl": url}} if url else {}
    return json.dumps({"mcpServers": servers}, indent=2) + "\n"


def write_mcp_config(env, url: str | None) -> None:
    """Replace the terminal leg's own global MCP config, including clearing it."""
    from ...cli_container import write_container_file
    write_container_file(env, MCP_CONFIG_PATH, mcp_config_json(url),
                         mkdir=os.path.dirname(MCP_CONFIG_PATH))


def write_context_seam(env, text: str) -> bool:
    """agy's own instruction seam: the session context as a GLOBAL rules file
    (``~/.gemini/config/rules/collimate-context.md``) inside the per-cell
    harness home. The terminal leg's seam (shared/docs/agent-runtime.md,
    instruction context; the ACP server takes the prompt in its ``_meta``);
    returns whether the seam carried the text, so the caller knows whether it
    still needs the workspace ``GEMINI.md`` fallback.

    That home is Collimate's, so this overwrites nothing of the user's."""
    from ...cli_container import write_root_file

    write_root_file(env, CONTEXT_SEAM_PATH, text or "",
                    what="antigravity context seam")
    return bool(text and text.strip())


# ──────────────────────────────────────────────────────────────────────
# The conversation store (SQLite + protobuf) — the harness-native record.
# ──────────────────────────────────────────────────────────────────────
#
# agy keeps one SQLite database per conversation, named by the conversation
# uuid (the one its headless ``init`` event announces, which our ACP session
# id IS). The headless process writes it at ``init``, before any input; the
# TTY names a conversation of its own, and a terminal leg's is the one record
# that appeared in the store while its TTY ran (:func:`conversation_ids`,
# ``new_tty_session``). The TUI writes that record when its first message is
# sent — none through its first-run screens or at its prompt before one, so a
# TTY never sent a message leaves nothing to carry — and SIGTERM ends it with
# the record whole (agy 1.2.11, API-key auth). The record is readable
# while agy writes it — which is what lets the terminal tail mirror a TTY
# conversation into the terminal transcript while the leg runs.
#
# Rows live in ``steps(idx, step_type, status, …, step_payload BLOB,
# step_format)`` and the payload is a protobuf with no ``.proto`` shipped
# anywhere. Everything below therefore decodes it by WIRE FORMAT only, from the
# field numbers observed in real databases, and never raises: a record we
# cannot read is a leg that reconciles less, never a run that fails.

# The two directories agy has used for the store. The current build writes the
# first; older logs still name the second, and a per-cell home persists across
# CLI upgrades, so both are searched.
_CONVERSATION_DIRS = (f"{STATE_DIR}/conversations", "antigravity/conversations")

# step_type values seen in the store. The payload layout differs per type, so
# these are how a row is read at all.
_STEP_USER_PROMPT = 14
_STEP_AGENT_TEXT = 15
_STEP_ERROR = 17
_STEP_TITLE = 23


def _conversation_dirs(home: str) -> list:
    """Every candidate store directory under ``home``, in preference order."""
    return [os.path.join(home, *rel.split("/")) for rel in _CONVERSATION_DIRS]


def conversation_db_path(home: str, conversation_id: str) -> str:
    """The database file of one conversation, looked up across both candidate
    store directories (a home written by an older CLI keeps its own). Returns
    the path that EXISTS; with none on disk, the path the current CLI would
    create — so callers can use ``os.path.exists`` on the result as "does this
    conversation have a record yet"."""
    cid = (conversation_id or "").strip()
    if not cid:
        return ""
    name = f"{cid}.db"
    for d in _conversation_dirs(home):
        path = os.path.join(d, name)
        if os.path.isfile(path):
            return path
    return os.path.join(_conversation_dirs(home)[0], name)


def conversation_ids(home: str) -> frozenset:
    """The id of every conversation record in ``home``'s store — a
    ``<id>.db`` in either candidate directory (the ``-wal`` / ``-shm``
    siblings are not records). A terminal leg lists them before its TTY
    starts: the per-cell home keeps this cell's earlier conversations, and
    only a record that was not there then can be the TTY's."""
    ids: set = set()
    for d in _conversation_dirs(home):
        try:
            names = os.listdir(d)
        except OSError:     # a store dir that does not exist
            continue
        ids.update(name[:-3] for name in names
                   if name.endswith(".db") and len(name) > 3)
    return frozenset(ids)


# agy's home inside the per-account home of a leg on this machine (HOME).
HOST_GEMINI_DIR = ".gemini"


def _selected_auth() -> str:
    import gen
    from collimate.account_taxonomy import model_account_auth
    return model_account_auth(gen.account("model"))


def terminal_on_host(auth: str) -> bool:
    from .adapter import terminal_locus
    return terminal_locus(auth) == "host"


def terminal_home(auth: "str | None" = None) -> str:
    """The ``~/.gemini`` a terminal leg's agy keeps its conversations in, for
    the selected account's ``auth``: the per-account home's on this machine
    (the ``google`` plan), this cell's harness home in the container."""
    auth = _selected_auth() if auth is None else auth
    if terminal_on_host(auth):
        from ...cli_host import account_home
        return os.path.join(account_home("antigravity"), HOST_GEMINI_DIR)
    from ...harness_homes import harness_home
    return harness_home("antigravity")


def harness_session_ids() -> frozenset:
    """:func:`conversation_ids` of the terminal leg's agy home
    (:func:`terminal_home`) — the ``harness_session_ids`` lookup of this
    backend's spec. A cell's harness home is seeded from the cell's
    persisted store on its first use, and a per-account home keeps the
    entry's earlier conversations, so a listing taken here before the TTY
    starts already holds every earlier conversation."""
    return conversation_ids(terminal_home())


def _varint(blob: bytes, i: int) -> tuple:
    """One protobuf varint at ``blob[i:]`` → ``(value, next_index)``."""
    value = shift = 0
    while True:
        if i >= len(blob):
            raise ValueError("truncated varint")
        byte = blob[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _pb_fields(blob: bytes) -> dict:
    """One protobuf message as ``{field_number: [value, …]}`` — length-delimited
    fields as raw ``bytes`` (a nested message, a string or a byte string are
    indistinguishable on the wire), varints as ``int``, fixed-width fields as
    raw bytes.

    A generic walker rather than a generated parser because Google ships no
    ``.proto`` for the conversation store: the shapes below are the field
    numbers observed in real databases, and anything unrecognised simply stays
    in the dict unread. Raises on a malformed message — every caller catches."""
    out: dict = {}
    i, n = 0, len(blob or b"")
    while i < n:
        key, i = _varint(blob, i)
        field, wire = key >> 3, key & 0x07
        if field == 0:
            raise ValueError("field number 0")
        if wire == 0:
            value, i = _varint(blob, i)
        elif wire == 1:
            value, i = blob[i:i + 8], i + 8
        elif wire == 2:
            length, i = _varint(blob, i)
            value, i = blob[i:i + length], i + length
        elif wire == 5:
            value, i = blob[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        if i > n:
            raise ValueError("truncated field")
        out.setdefault(field, []).append(value)
    return out


def _pb_text(blob: bytes, *path: int) -> str:
    """The UTF-8 string at a nested field path (``19, 3, 1`` = field 1 of the
    message at field 3 of the message at field 19), or ``""`` when any hop is
    missing or the bytes do not decode as that shape."""
    current = blob
    try:
        for num in path[:-1]:
            values = [v for v in _pb_fields(current).get(num, [])
                      if isinstance(v, bytes)]
            if not values:
                return ""
            current = values[0]
        values = [v for v in _pb_fields(current).get(path[-1], [])
                  if isinstance(v, bytes)]
    except Exception:  # noqa: BLE001 — a shape we cannot read is "no text"
        return ""
    return values[0].decode("utf-8", "replace") if values else ""


def _step_type(row_value, payload: bytes) -> int:
    """A step's type: the table column when it holds one (the CLI writes it
    there for its own queries), else field 1 of the payload."""
    if isinstance(row_value, int):
        return row_value
    try:
        values = [v for v in _pb_fields(payload).get(1, []) if isinstance(v, int)]
    except Exception:  # noqa: BLE001
        return -1
    return values[0] if values else -1


def _read_steps(path: str) -> list:
    """``[(idx, step_type, step_payload), …]`` in idx order from a conversation
    database, read-only.

    ``mode=ro`` over a URI so a live conversation is never written by the
    export (agy is the owner; the store is WAL and the CLI may be mid-turn),
    and the column set is read from the cursor rather than assumed, because
    this schema is undocumented and has already gained columns between builds.

    ``immutable=1`` is the fallback, not the default: a WAL database can only
    be read through its ``-shm`` file, which SQLite may need to CREATE, and a
    reader that cannot do that gets nothing at all. ``immutable`` skips the
    shared-memory machinery entirely — at the price of ignoring whatever sits
    in the ``-wal``, i.e. the newest turns — so it is worth a partial record
    but never worth preferring."""
    quoted = path.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    try:
        con = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        con = sqlite3.connect(f"file:{quoted}?immutable=1", uri=True, timeout=1.0)
    try:
        try:
            cur = con.execute("SELECT * FROM steps")
        except sqlite3.Error:
            con.close()
            con = sqlite3.connect(f"file:{quoted}?immutable=1", uri=True, timeout=1.0)
            cur = con.execute("SELECT * FROM steps")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        con.close()
    try:
        i_idx = cols.index("idx")
    except ValueError:
        i_idx = -1
    i_type = cols.index("step_type") if "step_type" in cols else -1
    i_payload = cols.index("step_payload") if "step_payload" in cols else -1
    if i_payload < 0:
        return []
    steps = []
    for n, row in enumerate(rows):
        payload = row[i_payload]
        if not isinstance(payload, (bytes, bytearray)):
            payload = b""
        idx = row[i_idx] if i_idx >= 0 and isinstance(row[i_idx], int) else n
        steps.append((idx, row[i_type] if i_type >= 0 else None, bytes(payload)))
    steps.sort(key=lambda s: s[0])
    return steps


def trace_from_conversation_db(path: str) -> list:
    """Normalize one agy conversation database into the shared trace
    vocabulary — the terminal mode's fullest-fidelity record, and the same
    normalization the live tail reads through.

    Best-effort by contract: it NEVER raises. A database that is locked,
    half-written, or laid out by a build whose field numbers moved yields
    ``[]`` or a partial trace, and the leg simply reconciles less.

    Layout (wire-format only — no ``.proto`` is published): step_type 14 is a
    user prompt (text at ``19.2``, or ``19.3.1``), 15 is agent text (``20.1``),
    17 an error and 23 a title update (both skipped — neither is conversation).
    Consecutive 15-rows are stream segments of ONE assistant message (each row
    grows in place as its segment streams), so they are concatenated rather
    than emitted as a message each. A row of any other type that carries a tool
    call at ``5.4`` (``.2`` = name, ``.3`` = raw JSON arguments) becomes a
    ``tool_use`` — that shape comes from community decoders and is unverified,
    which is exactly why a miss here costs nothing."""
    trace: list = []
    try:
        steps = _read_steps(path)
    except Exception:  # noqa: BLE001 — absent / locked / not a store
        return trace
    last_was_text = False
    for _idx, type_col, payload in steps:
        try:
            step_type = _step_type(type_col, payload)
            if step_type == _STEP_USER_PROMPT:
                text = _pb_text(payload, 19, 2) or _pb_text(payload, 19, 3, 1)
                if text:
                    trace.append({"type": "user", "text": text})
                last_was_text = False
            elif step_type == _STEP_AGENT_TEXT:
                text = _pb_text(payload, 20, 1)
                if not text:
                    continue
                if last_was_text and trace:
                    trace[-1]["text"] += text
                else:
                    trace.append({"type": "text", "text": text})
                last_was_text = True
            elif step_type in (_STEP_ERROR, _STEP_TITLE):
                last_was_text = False       # neither is part of the conversation
            else:
                name = _pb_text(payload, 5, 4, 2)
                if name:
                    raw = _pb_text(payload, 5, 4, 3)[:_TOOL_INPUT_CAP]
                    try:
                        args = json.loads(raw) if raw else None
                    except Exception:  # noqa: BLE001 — keep the raw string
                        args = raw
                    trace.append({"type": "tool_use", "tool": name, "input": args})
                last_was_text = False       # any other row breaks a text run
        except Exception:  # noqa: BLE001 — one unreadable row is not a failure
            last_was_text = False
    return trace


def messages_from_conversation_db(path: str) -> list:
    """The open-format transcript messages of one conversation — user +
    assistant TEXT only.

    Built through ``terminal_tail.messages_from_trace`` so the live tail and
    this leg's teardown export produce BYTE-IDENTICAL messages: two spellings
    of one turn would defeat the append-only merge's de-duplication and the
    terminal conversation would land in the transcript twice."""
    from ...terminal_tail import messages_from_trace

    return messages_from_trace(trace_from_conversation_db(path))


# ──────────────────────────────────────────────────────────────────────
# Terminal mode — a live `agy` TUI in the container.
# ──────────────────────────────────────────────────────────────────────


# The server's per-session ``_meta.antigravity`` tool filter
# (``disabledTools``): the built-in names to withhold. ``view_file`` always: its
# replacement reads anywhere under GEMINI_HOME, which holds the token document
# (the client's own ``fs/read_text_file`` callback stays, workspace-scoped).
# The web pair goes when the grant has no internet.
_ALWAYS_DISABLED_TOOLS = ("view_file",)
_WEB_TOOLS = ("search_web", "read_url_content")


def session_meta(system_prompt: str, *, websearch: bool, commands: str = "deny") -> dict:
    """The ``_meta`` the server's ``session/new`` and ``session/load`` take: the
    system prompt it appends to its own, the tool filter, and the bridge as an
    eager MCP server (its tools load as native tools; a lazy server's schemas
    are read back with ``view_file``, which is withheld). Below ``allow`` the
    tools that run code without asking (:data:`SERVER_UNASKED_TOOLS`) are
    withheld, and under ``deny`` the shell too."""
    from ...mcp_bridge import SERVER_NAME
    disabled = [*_ALWAYS_DISABLED_TOOLS, *([] if websearch else _WEB_TOOLS)]
    if commands != "allow":
        disabled += SERVER_UNASKED_TOOLS
    if commands == "deny":
        disabled += ("run_command",)
    meta: dict = {"disabledTools": disabled, "eagerMcpServers": [SERVER_NAME]}
    if (system_prompt or "").strip():
        meta["systemPrompt"] = {"append": system_prompt}
    return {"antigravity": meta}


def acp_settings_json(auth: str, *, project: str = "", location: str = "") -> dict:
    """The server's ``settings.json``: the auth method it infers when the client
    never calls ``authenticate`` (and, for Agent Platform, the GCP project and
    location)."""
    data: dict = {"auth": {"type": ACP_AUTH_TYPES[auth]}}
    if project or location:
        data["gcp"] = {key: value for key, value in (("project", project), ("location", location)) if value}
    return data


def write_acp_settings(home: str, auth: str, **gcp) -> None:
    """Write the server's settings on the HOST side of the mount, before the
    server starts (the server reads it at its first request)."""
    path = os.path.join(home, ACP_STATE_DIR, "settings.json")
    _write_json_private(path, _read_json(path), acp_settings_json(auth, **gcp))


class SignInGuard:
    """The ACP transport with the browser sign-in failing fast.

    A google-plan token the server cannot refresh (revoked, or Google's
    refresh token expired) starts its interactive login: it prints "Open the
    following link to authenticate the ACP server" on stderr and waits for a
    browser callback no headless run can complete, so the request that needed
    the login would hang until its own timeout. This reads the adapter log
    (the run's stderr sink, ``_agent/acp-antigravity.log``) for that line and,
    when it appears, answers every outstanding request with an actionable
    error and ends the server. Everything else passes through untouched."""

    def __init__(self, process, log_path: str, message: str = BROWSER_SIGN_IN_REFUSED,
                 poll_s: float = 0.25) -> None:
        self._process = process
        self._log_path = log_path
        self._message = message
        self._poll_s = poll_s
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pending: set = set()
        self._tripped = False
        self._offset = 0
        self._carry = b""
        self._tasks = [asyncio.create_task(self._pump()), asyncio.create_task(self._watch())]

    async def send(self, message: dict) -> None:
        if "method" in message and "id" in message:
            self._pending.add(message["id"])
        await self._process.transport.send(message)

    async def receive(self) -> "dict | None":
        return await self._queue.get()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self._process.transport.close()

    async def _pump(self) -> None:
        while True:
            message = await self._process.transport.receive()
            if message is not None and "id" in message and "method" not in message:
                self._pending.discard(message["id"])
            if self._tripped:
                return
            self._queue.put_nowait(message)
            if message is None:
                return

    def _log_tripped(self) -> bool:
        try:
            with open(self._log_path, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return False
        self._offset += len(chunk)
        window = self._carry + chunk
        self._carry = window[-len(BROWSER_SIGN_IN_MARKER):]
        return BROWSER_SIGN_IN_MARKER.encode() in window

    async def _watch(self) -> None:
        while not await asyncio.to_thread(self._log_tripped):
            await asyncio.sleep(self._poll_s)
        self._tripped = True
        _secret_log("antigravity: the ACP server asked for a browser sign-in; ending the run")
        for ident in sorted(self._pending, key=str):
            self._queue.put_nowait({"jsonrpc": "2.0", "id": ident,
                                    "error": {"code": -32000, "message": self._message}})
        await asyncio.to_thread(self._process.kill)
        self._queue.put_nowait(None)


async def prepare(ctx: dict, *, leg: str = 'acp') -> PreparedHarness:
    """Own both legs' selected auth, native policy, model and container setup."""
    from ...harness_homes import harness_home
    from ...cli_container import ensure_env

    terminal = leg == 'terminal'
    auth = ctx['auth']
    commands = ctx['commands']
    model = str(ctx.get('model') or '').strip()
    if model == DEFAULT_MODEL_SENTINEL:
        model = ''
    if terminal and terminal_on_host(auth):
        return await _prepare_host_terminal(ctx, model)

    home = await asyncio.to_thread(harness_home, 'antigravity')
    volumes = await asyncio.to_thread(antigravity_volumes, auth)
    guards = await asyncio.to_thread(command_guard_files, commands)
    env_vars = {}
    if ctx['credentials'].key:
        env_vars['GEMINI_API_KEY'] = ctx['credentials'].key

    if terminal:
        native_model = await asyncio.to_thread(write_settings, home, commands=commands, model=model)
        policy = CommandsPolicy(commands, {
            'settings': permission_settings(commands),
            'readonly_files': guards,
        })
        env_obj = await ensure_env('antigravity', build_dir='antigravity',
            volumes=volumes, readonly_files=guards)
        argv = ['agy', *(['--model', native_model] if native_model else []),
                *(['--dangerously-skip-permissions'] if commands == 'allow' else [])]
        return PreparedHarness(policy, None, env_obj, tuple(argv), env_vars, model)

    await asyncio.to_thread(write_acp_settings, home, auth)
    if auth == 'api_key':
        # The Go harness's own Gemini API base URL: the registry endpoint minus
        # the API version it appends.
        from collimate.providers import STANDARD_MODEL_ENDPOINTS
        env_vars['GOOGLE_GEMINI_BASE_URL'] = STANDARD_MODEL_ENDPOINTS['gemini'].removesuffix('/v1beta')
    policy = CommandsPolicy(commands, {'readonly_files': guards})
    env_obj = await ensure_env('antigravity', build_dir='antigravity',
        volumes=volumes, readonly_files=guards)
    argv = ACP_ARGV
    log_path = acp_log_path()

    async def spawn():
        process = await spawn_container_adapter('antigravity', env_obj, list(argv), env_vars=env_vars)
        if auth != 'google':
            return process
        guard = SignInGuard(process, log_path)
        return AdapterProcess(transport=guard, kill=process.kill, proc=process.proc, dispose=process.dispose)
    profile = SpawnProfile(locus='container', cwd='/workspace', spawn=spawn,
        commands=commands, env_obj=env_obj, fold_system_prompt=False,
        session_meta=session_meta(ctx['system_prompt'], websearch=bool(ctx['websearch']),
                                  commands=ctx['commands']))
    return PreparedHarness(policy, profile, env_obj, argv, env_vars, model)


async def _prepare_host_terminal(ctx: dict, model: str) -> PreparedHarness:
    """The ``google`` plan's terminal: this machine's own agy, HOME the
    entry's per-account home, no credential from Collimate — agy signs in
    through Google's own browser flow the first time and keeps the sign-in
    there. Its settings carry the commands level as the container leg's do,
    and its global hooks file runs the same hook script."""
    import gen
    from ...cli_host import account_home, cli_binary, leg_env
    binary = cli_binary('antigravity', ctx.get('host_harness'))
    commands = ctx['commands']
    account = await asyncio.to_thread(gen.account, 'model')
    home = await asyncio.to_thread(account_home, 'antigravity', account)
    gemini = os.path.join(home, HOST_GEMINI_DIR)
    workspace = gen.workspace_path()
    await asyncio.to_thread(write_settings, gemini, commands=commands, model=model,
                            auth=ctx['auth'], workspace=workspace)
    hooks = hooks_json(commands, host_hook())
    await asyncio.to_thread(write_private_atomic, os.path.join(gemini, 'config', 'hooks.json'),
                            (json.dumps(hooks) + "\n").encode())
    _secret_log(f"antigravity terminal: this machine's {binary}, home {home} (one per Key manager "
                "entry); agy's own Google sign-in — Collimate passes it no credential")
    policy = CommandsPolicy(commands, {'settings': permission_settings(commands), 'hooks': hooks})
    argv = [binary, *(['--model', model] if model else []),
            *(['--dangerously-skip-permissions'] if commands == 'allow' else [])]
    return PreparedHarness(policy, None, None, tuple(argv), leg_env(home=home), model)


def acp_log_path() -> str:
    """Where the run's adapter stderr is logged (``_agent/acp-antigravity.log``)."""
    import gen
    from ...acp_client import ACP_LOG_FILE
    return os.path.join(gen.workspace_path(), ACP_LOG_FILE.format(backend='antigravity'))


def model_matches(value: str, target: str) -> bool:
    """The default sentinel keeps the model the server starts a session on, with
    no held target. Any other id is compared literally: the server's model
    option lists the Gemini API ids (``gemini-3.8-flash``) and keeps the
    thinking level in an option of its own."""
    return target == DEFAULT_MODEL_SENTINEL


async def _terminal_native_work(function, *args):
    """Keep the native bridge/context alive until its uncancellable writer settles."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        worker.result()  # Preserve a native write failure before owner cleanup.
        raise


@asynccontextmanager
async def terminal_context(prepared: PreparedHarness, request, bridge):
    """Own native snapshot, measured MCP config, context and export."""
    if prepared.env_obj is None:
        async with _host_terminal_context(prepared, request, bridge) as launch:
            yield launch
        return
    from ...harness_homes import harness_home
    from ...cli_container import inject_context_file, remove_context_file
    from ...terminal_runner import TerminalLaunch

    home = await asyncio.to_thread(harness_home, 'antigravity')
    # Snapshot failure must not arm either export or fallback-context cleanup.
    before = await asyncio.to_thread(conversation_ids, home)
    env = prepared.env_obj
    assert env is not None
    wrote_seam = False

    def bind_context():
        nonlocal wrote_seam
        # Record the actual completed result even when the awaiting owner was
        # cancelled; it decides whether fallback removal belongs to this leg.
        wrote_seam = write_context_seam(env, request.extra_system)

    try:
        # The actual native 1.2.11 intake consumes this canonical HTTP config.
        # The common runner owns its bridge through native export/cleanup.
        await _terminal_native_work(write_mcp_config, env, bridge.url_container if bridge else None)
        await _terminal_native_work(bind_context)
        if request.extra_system and request.extra_system.strip() and not wrote_seam:
            await _terminal_native_work(inject_context_file, env, '/workspace/GEMINI.md', request.extra_system)
        yield TerminalLaunch(container_id=env.container_id, argv=prepared.argv,
            cwd='/workspace', env={'TERM': 'xterm-256color', **prepared.env},
            label=f"Antigravity · {prepared.model or 'default'}",
            buttons=[],
            ready_status='Antigravity terminal ready — open the cell to attach.',
            harness_session_id='')
    finally:
        def teardown():
            try:
                _finalize_antigravity_terminal(home, before=before)
            except Exception as e:  # noqa: BLE001 — native export is best-effort
                _secret_log(f'antigravity teardown failed: {e}')
            if not wrote_seam:
                try:
                    remove_context_file(env, '/workspace/GEMINI.md')
                except Exception:  # noqa: BLE001
                    pass
        await _terminal_native_work(teardown)
    # Container lifetime remains the run-level owner's.


@asynccontextmanager
async def _host_terminal_context(prepared: PreparedHarness, request, bridge):
    """The leg on this machine: its agy home's MCP config (the leg's bridge
    on loopback) and global rules file written before agy starts, the
    conversation store listed before it and exported after."""
    import gen
    from ...host_adapters import write_host_seam
    from ...terminal_runner import TerminalLaunch

    gemini = os.path.join(prepared.env['HOME'], HOST_GEMINI_DIR)
    before = await asyncio.to_thread(conversation_ids, gemini)

    def bind():
        write_private_atomic(os.path.join(gemini, 'config', 'mcp_config.json'),
                             mcp_config_json(bridge.url_host if bridge else None).encode())
        write_host_seam(os.path.join(gemini, 'config', 'rules', 'collimate-context.md'),
                        request.extra_system)

    try:
        await _terminal_native_work(bind)
        yield TerminalLaunch(container_id=None, argv=prepared.argv, cwd=gen.workspace_path(),
            env=prepared.env, label=f"Antigravity · {prepared.model or 'default'}", buttons=[],
            ready_status='Antigravity terminal ready — open the cell to attach.',
            harness_session_id='')
    finally:
        def teardown():
            try:
                _finalize_antigravity_terminal(gemini, before=before)
            except Exception as e:  # noqa: BLE001 — native export is best-effort
                _secret_log(f'antigravity teardown failed: {e}')
        await _terminal_native_work(teardown)


def _finalize_antigravity_terminal(home: str, *, before: frozenset) -> None:
    """Teardown: export the terminal transcript (a log artifact) and persist
    the run's raw record (the normalized trace of agy's conversation database)
    — each step independently guarded so one failure doesn't sink the rest.

    Which conversation: the one record that appeared in the store after
    ``before`` (its ids when the TTY started) — the rule the live tail
    follows (``new_tty_session``), so the transcript and the raw record are
    one conversation. None appeared, or
    several (which the TTY ended on is unknowable): nothing is exported — an
    earlier conversation the per-cell home keeps never is."""
    from ...agent_events import TRACE_FILE
    from ... import new_tty_session

    conversation_id = new_tty_session(before, conversation_ids(home))
    if not conversation_id:
        _secret_log("antigravity: the terminal left no conversation of its own "
                "to export")
        return
    path = conversation_db_path(home, conversation_id)
    trace = trace_from_conversation_db(path)
    if not trace:
        return

    try:
        from ...terminal_tail import messages_from_trace

        messages = messages_from_trace(trace)
        if messages:
            from ...transcript_merge import write_merged_transcript
            added = write_merged_transcript(messages, path=_TRANSCRIPT_FILE)
            _secret_log(f"{_TRANSCRIPT_FILE}: {added} message(s) added from the "
                    "Antigravity session")
    except Exception as e:  # noqa: BLE001
        _secret_log(f"antigravity transcript export failed: {e}")

    # The raw record: the conversation id (the one this terminal ran on)
    # plus the normalized trace. The database itself stays in the per-cell
    # home — it is a live SQLite store, not something to copy into a cell.
    try:
        _write_log(
            _SESSION_FILE,
            json.dumps({"conversation_id": conversation_id, "trace": trace},
                       indent=2, default=str),
            category="log")
        _write_log(TRACE_FILE, json.dumps(trace, default=str),
                        category="log")
    except Exception as e:  # noqa: BLE001
        _secret_log(f"antigravity session record export failed: {e}")
