# SPDX-License-Identifier: MIT
"""Unit tests for the cline backend's PURE helpers (``cline_backend``) — the
Cline CLI's terminal surface and the ChatGPT-subscription login share it mints.

The credential arrangement (the per-cell harness home, the shared login
DIRECTORY, the readiness predicate that decides whether a leg can run at all,
and the env that points both legs at the same store) is bridge-free and
deterministic, so it tests in isolation against synthetic inputs. The docker /
``gen`` / network paths are covered by the end-to-end smoke — except for the
container arrangement and the sign-in command the TTY leg asks for, which are
asserted here by stubbing ``gen`` and ``ensure_env``: the mounts have to MATCH
the ACP profile's (one cached container serves both legs), and the logged-out
command IS the one-time sign-in, so a silent change to either would only show
up as a cell that cannot authenticate.

Nothing here touches docker, subprocess, or the network, and no test ever
writes a real credential: the store is rooted at ``tmp_path`` and the
"tokens" are obvious fakes.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import re
import stat

import gen
import pytest


# The engine is generator-side shared code; import it the way the `agent`
# generator does (sibling on the generators path).
from _agent_shared import login_store as ls, surface_runner
from _agent_shared.harnesses.cline import backend as cb  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

# The run's terminal pointer a leg is handed (synthetic run label).
_POINTER = "_runs/tester-0badc0de/terminal/cline.terminal"


@pytest.fixture
def home(cache_root):
    from _agent_shared.harness_homes import harness_home

    return harness_home("cline")


def _write_store(data) -> str:
    """Put ``data`` (a dict, or raw text) where the shared store lives."""
    path = cb.shared_login_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))
    return path


def _signed_in(token: str = "sk-fake-access-token") -> dict:
    """A providers.json in cline's real shape, holding a ChatGPT login."""
    return {
        "version": 1,
        "lastUsedProvider": "openai-codex",
        "modes": {},
        "providers": {
            "openai-codex": {
                "settings": {
                    "provider": "openai-codex",
                    "auth": {"accessToken": token,
                             "refreshToken": "rt-fake",
                             "expiresAt": 1,
                             "accountId": "acct-fake"},
                },
                "updatedAt": "2026-09-11T00:00:00.000Z",
                "tokenSource": "oauth",
            },
        },
    }


# ── the store: where it is, and what it is ────────────────────────────
#
# The contract: /home/coder/.cline inside the container is Collimate's per-cell
# harness home under BOTH auths (the user's real ~/.cline is never read or
# written), and the chatgpt leg shares nothing but the LOGIN — as one shared
# DIRECTORY, because cline writes its credential temp-file + rename.


def test_no_path_this_module_touches_is_the_users_own_cline(home):
    """The whole isolation story in one assertion: every host path the backend
    reads or writes lives under Collimate's cache root, never under the user's
    ``~/.cline`` (which holds their own provider credentials, sessions, hub
    state and installation identity)."""
    real = os.path.expanduser("~/.cline")
    for path in (cb.shared_login_dir(), cb.shared_login_path(), home):
        assert path
        assert path != real and not path.startswith(real + os.sep), path


def test_the_store_is_the_one_spelled_out_shared_directory(selected_account_runtime):
    assert cb.shared_login_dir().startswith(str(selected_account_runtime))


def test_the_login_mount_sits_outside_the_per_cell_home():
    """The shared credential must not live inside the per-cell ``.cline``
    mount: that directory is this cell's sessions, logs and installation
    identity, and nesting the one shared thing inside it invites a later
    "just mount the home" simplification that would share all of it."""
    assert cb.CLINE_LOGIN_MOUNT != cb.CLINE_HOME
    assert not cb.CLINE_LOGIN_MOUNT.startswith(cb.CLINE_HOME + "/")
    assert cb.CLINE_LOGIN_FILE == cb.CLINE_LOGIN_MOUNT + "/providers.json"
    # And the env var that relocates the credential names exactly that file —
    # the only lever that moves the credential WITHOUT moving the home.
    assert cb.cline_plan_env() == {
        "CLINE_PROVIDER_SETTINGS_PATH": cb.CLINE_LOGIN_FILE}


# ── the readiness predicate ───────────────────────────────────────────


def test_no_store_at_all_is_not_a_login(cache_root):
    assert ls.selected_login_ready("cline", "chatgpt") is False


def test_an_empty_or_broken_store_is_not_a_login(cache_root):
    """What a logged-out cline leaves behind, and what a sibling container
    caught mid-rename looks like. Both read as "logged out" to cline itself,
    and must read that way here — a false "ready" turns the sign-in surface
    off and strands the cell."""
    _write_store({})
    assert ls.selected_login_ready("cline", "chatgpt") is False
    _write_store({"version": 1, "providers": {}})
    assert ls.selected_login_ready("cline", "chatgpt") is False
    _write_store("{not json at all")
    assert ls.selected_login_ready("cline", "chatgpt") is False
    _write_store("[]")
    assert ls.selected_login_ready("cline", "chatgpt") is False


def test_another_providers_credential_is_not_a_chatgpt_login(cache_root):
    """One providers.json holds EVERY provider, so "is there a file" is not the
    question — "is the openai-codex entry authenticated" is."""
    _write_store({"version": 1, "providers": {"anthropic": {
        "settings": {"provider": "anthropic", "apiKey": "sk-ant-fake"},
        "tokenSource": "manual"}}})
    assert ls.selected_login_ready("cline", "chatgpt") is False


def test_an_api_key_pasted_for_openai_codex_is_not_a_login(cache_root):
    """The plan login is an OAuth access token under ``settings.auth``; a
    pasted key lands at ``settings.apiKey`` and bills differently."""
    _write_store({"version": 1, "providers": {"openai-codex": {
        "settings": {"provider": "openai-codex", "apiKey": "sk-fake"},
        "tokenSource": "manual"}}})
    assert ls.selected_login_ready("cline", "chatgpt") is False


def test_an_empty_access_token_is_not_a_login(cache_root):
    store = _signed_in("")
    _write_store(store)
    assert ls.selected_login_ready("cline", "chatgpt") is False
    store["providers"]["openai-codex"]["settings"]["auth"].pop("accessToken")
    _write_store(store)
    assert ls.selected_login_ready("cline", "chatgpt") is False


def test_a_real_looking_oauth_entry_is_a_login(cache_root):
    """cline's own ``isConfigured`` asks exactly this: a non-empty
    ``providers["openai-codex"].settings.auth.accessToken``."""
    _write_store(_signed_in())
    assert ls.selected_login_ready("cline", "chatgpt") is True


# ── the volumes ───────────────────────────────────────────────────────


def test_api_key_runs_get_the_per_cell_home_and_nothing_else(cache_root, home):
    """The key auth has no business seeing the subscription store: a run that
    pasted a key must not be able to read (or overwrite) the plan credential."""
    assert cb.cline_volumes("api_key") == {
        home: {"bind": cb.CLINE_HOME, "mode": "rw"}}
    assert not os.path.exists(cb.shared_login_dir())


def test_the_plan_binds_the_store_directory_from_the_very_first_run(
        cache_root, home):
    """No promote step and no "only once it holds a login" gate, unlike codex:
    an empty store reads as logged out, so binding it up front is safe — and it
    is what lets the one-time sign-in land straight in the file every cell
    already mounts."""
    assert ls.selected_login_ready("cline", "chatgpt") is False
    assert cb.cline_volumes("chatgpt") == {
        home: {"bind": cb.CLINE_HOME, "mode": "rw"},
        cb.shared_login_dir(): {"bind": cb.CLINE_LOGIN_MOUNT, "mode": "rw"},
    }
    # Still the same arrangement once a login exists — nothing about the
    # mounts changes, only what the file inside them holds.
    _write_store(_signed_in())
    assert cb.cline_volumes("chatgpt") == {
        home: {"bind": cb.CLINE_HOME, "mode": "rw"},
        cb.shared_login_dir(): {"bind": cb.CLINE_LOGIN_MOUNT, "mode": "rw"},
    }


def test_the_store_directory_is_created_private_before_docker_sees_it(
        cache_root):
    """Docker creates a missing bind source as ROOT, which would leave the
    container's uid-1000 user unable to write its own credential — and a
    world-readable store would let any local account read the token."""
    cb.cline_volumes("chatgpt")
    store = cb.shared_login_dir()
    assert os.path.isdir(store)
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o700


# ── the terminal leg ──────────────────────────────────────────────────


class _Stop(Exception):
    """Ends the TTY runner at the terminal-surface boundary — everything after
    it is the PTY and the renderer."""


class _FakeContainer:
    def __init__(self, rec):
        self.rec = rec

    def exec_run(self, argv, user=None, workdir=None):
        cmd = " ".join(argv)
        self.rec["execs"].append(cmd)
        # Decode what write_container_file just wrote, so a test can assert on
        # the FILE rather than on the shell line that carried it.
        m = re.search(r"echo '?([A-Za-z0-9+/=]+)'? \| base64 -d > '?([^']+)'?$", cmd)
        if m:
            self.rec["files"][m.group(2)] = base64.b64decode(m.group(1)).decode()

        class _Res:
            exit_code = 0
            output = b""
        return _Res()


class _FakeEnv:
    container_id = "container-1"

    def __init__(self, rec):
        self.rec = rec
        self.container = _FakeContainer(rec)

    def exec(self, argv, **kwargs):
        if argv == ["cline", "hub", "status"]:
            return 0, '{"running": false}'
        result = self.container.exec_run(argv, user=kwargs.get("user"))
        return result.exit_code, result.output.decode("utf-8")

    def read_file(self, path, **kw):
        raise FileNotFoundError(path)       # the workspace has no such file yet


@pytest.fixture
def stub_terminal(monkeypatch, cache_root, selected_account_runtime):
    """Drive the selected Cline terminal runner as far as its terminal registration, with
    the container and the run surface stubbed out. Returns the
    recorded calls."""
    target = selected_account_runtime / 'cline/chatgpt/providers.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({'providers': {'openai-codex': {'settings': {'auth': {'accessToken': 'a'}}}}}))
    from _agent_shared import cli_container as cc

    rec: dict = {"logs": [], "execs": [], "files": {}, "ensure": {},
                 "serve": {}}
    rec["env"] = _FakeEnv(rec)

    async def _ensure_env(name, **kw):
        rec["ensure"] = {"name": name, **kw}
        return rec["env"]

    def _serve_terminal(container_id, command, **kw):
        rec["serve"] = {"container_id": container_id, "command": list(command), **kw}
        # What the TTY would find in its providers.json while it runs (the
        # leg's teardown deletes the file once the TTY is gone).
        path = os.path.join(cb.key_settings_host_dir(), "providers.json")
        if os.path.exists(path):
            with open(path) as fh:
                rec["key_file"] = {
                    "doc": json.load(fh),
                    "mode": stat.S_IMODE(os.stat(path).st_mode),
                    "dir_mode": stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode),
                }
        raise _Stop()

    async def _api_key(provider, **kw):
        rec["api_key_provider"] = provider
        return "cline-key-1"

    async def _update(**kw):
        return None


    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def no_bridge(**kwargs):
        yield None
    monkeypatch.setattr(cc, "leg_bridge", no_bridge)
    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(gen, "commit", _update, raising=False)
    monkeypatch.setattr(_pipeline, "account_api_key", _api_key, raising=False)
    monkeypatch.setattr(gen, "serve_terminal", _serve_terminal, raising=False)
    monkeypatch.setattr(gen, "phase", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gen, "log", lambda m: rec["logs"].append(m),
                        raising=False)
    return rec


def _run_terminal(*, resume_harness_session_id: str = "", **cfg):
    base = {"backend": "cline", "auth": "chatgpt", "provider": "openai",
            "model": "", "commands": "allow"}
    base.update(cfg)
    with pytest.raises(_Stop):
        asyncio.run(surface_runner("cline", "terminal")(
            base, pointer=_POINTER, extra_system="be brief", resume_harness_session_id=resume_harness_session_id))


def test_a_logged_out_plan_terminal_opens_on_the_sign_in_command(stub_terminal):
    os.unlink(cb.shared_login_path())
    with pytest.raises(RuntimeError, match="Key manager"):
        _run_terminal()


def test_a_logged_out_plan_terminal_says_what_the_wait_will_look_like(stub_terminal):
    os.unlink(cb.shared_login_path())
    with pytest.raises(RuntimeError, match="Key manager"):
        _run_terminal()


def test_a_signed_in_plan_terminal_is_just_the_tui(stub_terminal):
    _write_store(_signed_in())
    _run_terminal()
    assert stub_terminal["serve"]["command"] == ["cline", "-i", "--auto-approve", "true"]
    assert not any("300 s" in m for m in stub_terminal["logs"])


def test_the_plan_leg_never_asks_for_or_injects_a_key(stub_terminal):
    """Under the subscription a pasted key is the WRONG credential: cline would
    bill it while the store holds a plan the user already pays for."""
    _write_store(_signed_in())
    _run_terminal()
    assert "api_key_provider" not in stub_terminal
    env = stub_terminal["serve"]["env"]
    assert "CLINE_API_KEY" not in env
    assert env["CLINE_PROVIDER"] == "openai-codex"
    assert env["CLINE_PROVIDER_SETTINGS_PATH"] == cb.CLINE_LOGIN_FILE
    # Selected native configuration is scoped to this exec, not container defaults.
    assert "env_vars" not in stub_terminal["ensure"]


def test_the_key_leg_stays_off_the_shared_store(stub_terminal, home):
    """The api_key auth must never be pointed at the subscription store: its
    volumes are the per-cell home alone."""
    _run_terminal(auth="api_key", provider="anthropic")
    assert stub_terminal["api_key_provider"] == "anthropic"
    env = stub_terminal["serve"]["env"]
    assert env["CLINE_PROVIDER"] == "anthropic"
    assert env["CLINE_PROVIDER_SETTINGS_PATH"] != cb.CLINE_LOGIN_FILE
    assert stub_terminal["ensure"]["volumes"] == {
        home: {"bind": cb.CLINE_HOME, "mode": "rw"}}
    assert stub_terminal["serve"]["command"] == ["cline", "-i", "--auto-approve", "true"]


# ── the key leg's providers.json ──────────────────────────────────────
#
# cline 3.0.65's TUI ignores CLINE_PROVIDER / CLINE_API_KEY / CLINE_MODEL and
# opens on its provider onboarding; what it reads is providers.json, at
# CLINE_PROVIDER_SETTINGS_PATH. The shape below is the one probed to open the
# TUI at its prompt with the provider and model set.

_ZOD_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def test_the_key_file_has_the_shape_cline_s_tui_opens_on():
    doc = cb.key_provider_settings("openrouter", "sk-fake", "~google/gemini-flash-latest",
                                   now="2026-09-25T22:35:01.680Z")
    assert doc == {
        "version": 1,
        "lastUsedProvider": "openrouter",
        "modes": {},
        "providers": {"openrouter": {
            "settings": {"provider": "openrouter", "apiKey": "sk-fake",
                         "model": "~google/gemini-flash-latest"},
            "updatedAt": "2026-09-25T22:35:01.680Z",
            "tokenSource": "manual"}},
    }


def test_the_key_file_timestamp_is_one_cline_s_schema_accepts():
    """zod's ``datetime()`` rejects an offset (``+00:00``): a file that fails
    the schema reads as empty, and cline overwrites it with its own gateway."""
    stamp = cb.key_provider_settings("openrouter", "k")["providers"]["openrouter"]["updatedAt"]
    assert _ZOD_DATETIME.match(stamp), stamp


def test_a_blank_model_is_left_to_cline():
    doc = cb.key_provider_settings("openrouter", "k", "")
    assert "model" not in doc["providers"]["openrouter"]["settings"]


def test_the_key_file_sits_in_the_per_cell_home(cache_root, home):
    """The container path is inside the home mount, at the same relative path
    as the host file the runner writes."""
    rel = os.path.relpath(cb.CLINE_KEY_SETTINGS_FILE, cb.CLINE_HOME)
    assert not rel.startswith("..")
    assert os.path.join(cb.key_settings_host_dir(), "providers.json") == \
        os.path.join(home, rel)
    # Not cline's own default store, which the ACP leg reads.
    assert rel != "data/settings/providers.json"


def test_the_key_leg_writes_the_key_into_a_private_providers_file(stub_terminal):
    _run_terminal(auth="api_key", provider="openrouter",
                  model="~google/gemini-flash-latest")
    seen = stub_terminal["key_file"]
    assert seen["mode"] == 0o600 and seen["dir_mode"] == 0o700
    settings = seen["doc"]["providers"]["openrouter"]["settings"]
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    assert settings == {"provider": "openrouter", "apiKey": "cline-key-1",
                        "model": "~google/gemini-flash-latest",
                        "baseUrl": STANDARD_MODEL_ENDPOINTS["openrouter"]}
    assert seen["doc"]["lastUsedProvider"] == "openrouter"
    assert _ZOD_DATETIME.match(seen["doc"]["providers"]["openrouter"]["updatedAt"])
    assert stub_terminal["serve"]["env"]["CLINE_PROVIDER_SETTINGS_PATH"] == \
        cb.CLINE_KEY_SETTINGS_FILE


def test_an_openai_key_file_names_cline_s_native_openai_provider(stub_terminal):
    """cline reads a stored "openai" as its legacy alias for
    "openai-compatible" (no base URL, model dropped to gpt-4o): the TTY's file
    names "openai-native", while the ACP env keeps its own id."""
    _run_terminal(auth="api_key", provider="openai", model="gpt-5")
    doc = stub_terminal["key_file"]["doc"]
    assert doc["lastUsedProvider"] == "openai-native"
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    assert doc["providers"]["openai-native"]["settings"] == {
        "provider": "openai-native", "apiKey": "cline-key-1", "model": "gpt-5",
        "baseUrl": STANDARD_MODEL_ENDPOINTS["openai"]}
    assert stub_terminal["serve"]["env"]["CLINE_PROVIDER"] == "openai"


def test_every_other_provider_id_is_stored_as_is():
    from _agent_shared.harnesses.cline.adapter import ADAPTER

    for provider, native in ADAPTER.auth.providers.items():
        if provider != "openai":
            assert cb.key_provider_id(native.id) == native.id


def test_the_key_rides_only_in_that_file(stub_terminal):
    """Never an argv (``ps`` shows it to every user in the container), never
    the TTY's or the container's environment, never the workspace."""
    _run_terminal(auth="api_key", provider="openrouter", model="m")
    key = "cline-key-1"
    assert key not in " ".join(stub_terminal["serve"]["command"])
    assert not any(key in c for c in stub_terminal["execs"])
    assert key not in json.dumps(stub_terminal["serve"]["env"])
    assert "CLINE_API_KEY" not in stub_terminal["serve"]["env"]
    ensure_env = stub_terminal["ensure"].get("env_vars", {})
    assert key not in json.dumps(ensure_env)
    # The settings path is the TTY's alone: on the container it would move
    # the ACP arm's store too.
    assert "CLINE_PROVIDER_SETTINGS_PATH" not in ensure_env
    assert not any(key in body for body in stub_terminal["files"].values())


def test_the_key_file_leaves_with_the_tty(stub_terminal):
    _run_terminal(auth="api_key", provider="openrouter", model="m")
    assert "key_file" in stub_terminal
    assert not list(Path(cb.key_settings_host_dir()).glob("providers.json*"))


def test_the_plan_leg_writes_no_key_file(stub_terminal):
    _write_store(_signed_in())
    _run_terminal()
    assert "key_file" not in stub_terminal
    assert not os.path.exists(cb.key_settings_host_dir())
    assert stub_terminal["serve"]["env"]["CLINE_PROVIDER_SETTINGS_PATH"] == \
        cb.CLINE_LOGIN_FILE


def test_no_key_no_file(stub_terminal, monkeypatch):
    async def _no_key(provider, **kw):
        return None

    monkeypatch.setattr(_pipeline, "account_api_key", _no_key)
    _run_terminal(auth="api_key", provider="openrouter", model="m")
    assert "key_file" not in stub_terminal
    assert "CLINE_PROVIDER_SETTINGS_PATH" not in stub_terminal["serve"]["env"]


def test_the_model_rides_the_env_only_when_one_was_chosen(stub_terminal):
    """A blank model means "cline's own default" (gpt-5.4 under the plan) —
    an empty CLINE_MODEL would be a model id the catalogue cannot match."""
    _write_store(_signed_in())
    _run_terminal()
    assert "CLINE_MODEL" not in stub_terminal["serve"]["env"]
    _run_terminal(model="gpt-5.5")
    assert stub_terminal["serve"]["env"]["CLINE_MODEL"] == "gpt-5.5"
    assert stub_terminal["serve"]["label"] == "Cline · gpt-5.5"


def test_the_system_prompt_rides_the_workspace_fold_not_the_s_flag(
        stub_terminal):
    """``-s`` REPLACES cline's whole system prompt — its tool and safety
    instructions included — so the session context goes into the workspace
    context file instead (best-effort: whether cline reads AGENTS.md is
    unverified)."""
    _write_store(_signed_in())
    _run_terminal()
    assert "-s" not in stub_terminal["serve"]["command"]
    assert "--system" not in stub_terminal["serve"]["command"]
    assert "be brief" in stub_terminal["files"]["/workspace/AGENTS.md"]


def test_the_terminal_leg_registers_the_pointer_and_keeps_the_tty_alive(
        stub_terminal):
    _write_store(_signed_in())
    _run_terminal()
    assert stub_terminal["serve"]["pointer"] == _POINTER
    assert stub_terminal["serve"]["keep_alive"] is True
    assert stub_terminal["serve"]["cwd"] == "/workspace"
    assert stub_terminal["serve"]["env"]["TERM"] == "xterm-256color"


def test_a_resume_id_is_accepted_and_ignored_as_tier_c(stub_terminal):
    """Tier C: the ACP session id is not known to map onto a cline session id,
    so the terminal is always fresh — and the run log says so rather than
    silently dropping the id."""
    _write_store(_signed_in())
    _run_terminal(resume_harness_session_id="sess-123")
    assert stub_terminal["serve"]["command"] == ["cline", "-i", "--auto-approve", "true"]
    assert any("tier C" in m for m in stub_terminal["logs"])


# ── the grant's commands axis ─────────────────────────────────────────


class _PolicyEnv:
    """A container that records what runs as root and the files written."""

    def __init__(self, exit_code=0):
        self.container = self
        self.container_id = "cid"
        self.exit_code = exit_code
        self.root: list = []
        self.files: dict = {}

    def exec_run(self, argv, user=None, workdir=None):
        cmd = argv[-1]
        if user == "root":
            self.root.append(cmd)
        for m in re.finditer(r"echo '?([A-Za-z0-9+/=]+)'? \| base64 -d > '?([^' ]+)'?", cmd):
            self.files[m.group(2)] = base64.b64decode(m.group(1)).decode()

        class _Res:
            exit_code = self.exit_code
            output = b""
        return _Res()

    def exec(self, argv, **kwargs):
        if argv == ["cline", "hub", "status"]:
            return 0, '{"running": false}'
        result = self.exec_run(argv, user=kwargs.get("user"))
        return result.exit_code, result.output.decode("utf-8")


def test_policy_settings_per_level():
    """deny takes run_commands away from every agent; ask keeps it (cline
    asks) but does not load the tools whose delegates run with no approval
    hook: spawn_agent's sub-agent and the teammates team_spawn_teammate
    creates and team_run_task runs."""
    assert cb.cline_policy_settings("allow") is None
    assert cb.cline_policy_settings("ask") == {
        "toolAutoApprove": False,
        "disabledTools": ["spawn_agent", "team_spawn_teammate", "team_run_task"]}
    assert cb.cline_policy_settings("deny") == {
        "toolAutoApprove": False, "disabledTools": ["run_commands"]}


@pytest.mark.parametrize("level", ["deny", "ask"])
@pytest.mark.parametrize("terminal", [False, True])
def test_a_restricted_leg_writes_the_settings_as_root_and_names_them(level, terminal):
    """The global settings and cline's MCP settings are both written afresh
    as root, outside the agent-writable home: a stdio server planted in the
    home's MCP settings would spawn its command when the next session is
    built."""
    env = _PolicyEnv()
    out = cb.apply_commands_policy(env, level, terminal=terminal)
    assert out == {"CLINE_GLOBAL_SETTINGS_PATH": cb.CLINE_POLICY_SETTINGS,
                   "CLINE_MCP_SETTINGS_PATH": cb.CLINE_MCP_POLICY_SETTINGS}
    assert cb.CLINE_POLICY_SETTINGS.startswith("/etc/")
    assert cb.CLINE_MCP_POLICY_SETTINGS.startswith("/etc/")
    assert json.loads(env.files[cb.CLINE_POLICY_SETTINGS]) == cb.cline_policy_settings(level)
    assert json.loads(env.files[cb.CLINE_MCP_POLICY_SETTINGS]) == {"mcpServers": {}}
    # One root script; hooks and plugins in the workspace are left to
    # cline's own trust model.
    (script,) = env.root
    for path in (cb.CLINE_POLICY_SETTINGS, cb.CLINE_MCP_POLICY_SETTINGS):
        assert f"rm -f {path} && " in script
        assert f"chmod 644 {path}" in script


def test_the_root_write_replaces_the_file_rather_than_rewriting_it(tmp_path):
    """After a terminal leg the files are coder-owned; the next ACP leg's
    root write must not inherit that owner (a truncate-and-write keeps it)
    nor land in an inode something still holds open."""
    import subprocess

    from _agent_shared.cli_container import root_file_script

    path = tmp_path / "cline" / "global-settings.json"
    path.parent.mkdir()
    path.write_text("{}")
    before = os.stat(path).st_ino
    with open(path, "a"):
        subprocess.run(["sh", "-c", root_file_script(str(path), '{"a": 1}')],
                       check=True)
    assert json.loads(path.read_text()) == {"a": 1}
    assert os.stat(path).st_ino != before
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644


def test_the_acp_leg_keeps_the_settings_root_owned():
    env = _PolicyEnv()
    cb.apply_commands_policy(env, "deny", terminal=False)
    (script,) = env.root
    assert "chown" not in script


def test_the_tui_hands_the_settings_to_coder_after_writing_them():
    """cline's TUI writes its own settings back to these files — Tab's
    Plan/Act mode, Shift+Tab's auto-approve, /mcp — and exits on a failed
    write, so on the terminal leg both files end up coder-owned, after the
    root write that put the policy in them."""
    env = _PolicyEnv()
    cb.apply_commands_policy(env, "deny", terminal=True)
    (script,) = env.root
    assert script.endswith(f"chown coder:coder {cb.CLINE_POLICY_SETTINGS} "
                           f"{cb.CLINE_MCP_POLICY_SETTINGS}")
    assert script.index("chown") > script.rindex("base64 -d >")


def test_allow_leaves_cline_alone():
    env = _PolicyEnv()
    assert cb.apply_commands_policy(env, "allow", terminal=True) == {}
    assert env.root == []


def test_a_failed_policy_write_fails_the_leg():
    with pytest.raises(RuntimeError, match="cline"):
        cb.apply_commands_policy(_PolicyEnv(exit_code=1), "deny", terminal=False)


@pytest.mark.parametrize("level, flag", [("deny", "false"), ("ask", "false"),
                                         ("allow", "true")])
def test_the_tui_asks_under_deny_and_ask(stub_terminal, level, flag):
    _write_store(_signed_in())
    _run_terminal(commands=level)
    assert stub_terminal["serve"]["command"] == ["cline", "-i", "--auto-approve", flag]
    env = stub_terminal["serve"]["env"]
    if level == "allow":
        assert "CLINE_GLOBAL_SETTINGS_PATH" not in env
    else:
        assert env["CLINE_GLOBAL_SETTINGS_PATH"] == cb.CLINE_POLICY_SETTINGS
        written = [e for e in stub_terminal["execs"] if cb.CLINE_POLICY_SETTINGS in e]
        assert written


@pytest.fixture
def profile_env(monkeypatch, cache_root):
    """Run the cline ACP profile against a recording container."""
    from _agent_shared import acp_run as ar
    from _agent_shared import cli_container as cc

    env = _PolicyEnv()

    async def _ensure_env(name, **kw):
        return env

    async def _api_key(provider, **kw):
        return "test-key"

    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(_pipeline, "account_api_key", _api_key, raising=False)
    monkeypatch.setattr(ar.gen, "log", lambda *_a, **_k: None, raising=False)
    return env


def _profile_spawn_env(ctx_over) -> dict:
    from _agent_shared import acp_run as ar

    ctx = {"auth": "api_key", "provider": "openrouter", "model": "m",
           "system_prompt": "", "enable_file_tools": True, "websearch": True,
           "commands": "deny", **ctx_over}
    profile = asyncio.run(ar._PROFILES["cline"](ctx))
    seen: dict = {}

    async def _spawn(backend, env_obj, argv, *, env_vars, cwd="/workspace"):
        seen.update(env_vars, argv=argv)
        return object()

    old = cb.spawn_container_adapter
    cb.spawn_container_adapter = _spawn
    try:
        asyncio.run(profile.spawn())
    finally:
        cb.spawn_container_adapter = old
    return seen


@pytest.mark.parametrize("level", ["deny", "ask"])
def test_the_acp_leg_points_cline_at_the_policy(profile_env, level):
    env = _profile_spawn_env({"commands": level})
    assert env["CLINE_GLOBAL_SETTINGS_PATH"] == cb.CLINE_POLICY_SETTINGS
    assert env["CLINE_MCP_SETTINGS_PATH"] == cb.CLINE_MCP_POLICY_SETTINGS
    assert env["argv"] == ["cline", "--acp"]
    assert json.loads(profile_env.files[cb.CLINE_POLICY_SETTINGS]) == \
        cb.cline_policy_settings(level)
    assert json.loads(profile_env.files[cb.CLINE_MCP_POLICY_SETTINGS]) == {
        "mcpServers": {}}


def test_the_acp_leg_keeps_its_key_in_its_env_and_its_file_holds_only_the_endpoint(profile_env):
    """The ACP arm reads CLINE_PROVIDER / CLINE_API_KEY / CLINE_MODEL from its
    env; its providers.json (Collimate's per-cell file, never the plan's login
    store) carries only the provider and the registry endpoint as baseUrl."""
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    env = _profile_spawn_env({"commands": "allow"})
    assert env["CLINE_API_KEY"] == "test-key"
    assert env["CLINE_PROVIDER"] == "openrouter"
    assert env["CLINE_MODEL"] == "m"
    assert env["CLINE_PROVIDER_SETTINGS_PATH"] == cb.CLINE_KEY_SETTINGS_FILE
    assert env["CLINE_PROVIDER_SETTINGS_PATH"] != cb.CLINE_LOGIN_FILE
    files = list(Path(cb.key_settings_host_dir()).glob("providers.json"))
    assert len(files) == 1
    doc = json.loads(files[0].read_text())
    assert doc["providers"]["openrouter"]["settings"] == {
        "provider": "openrouter", "baseUrl": STANDARD_MODEL_ENDPOINTS["openrouter"]}
    assert "test-key" not in files[0].read_text()


def test_the_acp_leg_under_allow_is_cline_as_shipped(profile_env):
    env = _profile_spawn_env({"commands": "allow"})
    assert "CLINE_GLOBAL_SETTINGS_PATH" not in env
    assert cb.CLINE_POLICY_SETTINGS not in profile_env.files
    assert json.loads(profile_env.files[cb.CLINE_MCP_POLICY_SETTINGS]) == {"mcpServers": {}}


def test_the_mode_select_cannot_turn_auto_approve_on_under_deny_or_ask():
    from _agent_shared import acp_run as ar

    for level in ("deny", "ask"):
        assert not ar.mode_allowed("cline", level, True)
        assert not ar.mode_allowed("cline", level, "true")
        assert ar.mode_allowed("cline", level, "act")
        assert ar.mode_allowed("cline", level, "plan")
        assert ar.mode_allowed("cline", level, False)
    assert ar.mode_allowed("cline", "allow", True)


def _gate(commands, kind, raw_input, title="editor"):
    from acp.schema import AllowedOutcome
    from _agent_shared import acp_client as ac

    class _TC:
        pass

    tc = _TC()
    tc.kind, tc.title, tc.raw_input, tc.tool_call_id = kind, title, raw_input, "t1"
    s = ac.AcpSession(backend="cline",
                      profile=ac.SpawnProfile(locus="container", cwd="/workspace",
                                              spawn=None, commands=commands),
                      mcp_servers_for=lambda caps: ([], []), file_tools_enabled=True)

    class _Opt:
        def __init__(self, kind, oid):
            self.kind, self.option_id, self.name = kind, oid, oid

    resp = asyncio.run(ac._CollimateClient(s).request_permission(
        "sid", tc, [_Opt("allow_once", "allow_once"), _Opt("reject_once", "reject_once")]))
    out = resp.outcome
    return isinstance(out, AllowedOutcome) and out.option_id == "allow_once"


@pytest.mark.parametrize("level", ["deny", "ask", "allow"])
def test_an_edit_into_a_hook_dir_is_an_ordinary_edit(level):
    """cline loads workspace hooks and plugins under its own trust model: the
    gate treats an edit there like any other file edit."""
    assert _gate(level, "edit", {"path": "/workspace/.cline/hooks/PreToolUse.sh",
                                 "new_text": "echo hi"})


@pytest.fixture(autouse=True)
def selected_account_runtime(account_runtime):
    snapshot = {"service": "openai", "auth_type": "cli", "metadata": {"harness": "cline", "plan": "chatgpt"}, "secrets": {"oauth_token": "test-subscription"}}
    with account_runtime(snapshot) as (root, _select_plan):
        yield root


def test_a_failed_setup_step_still_strips_the_context_block(stub_terminal, monkeypatch):
    """The AGENTS.md block is planted inside the try whose teardown strips it,
    so a later setup step that raises (terminal registration here) cannot leave
    Collimate's scaffolding in the workspace."""
    from _agent_shared import cli_container as cc

    removed = []

    def fail_terminal(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(gen, "serve_terminal", fail_terminal)
    monkeypatch.setattr(cc, "remove_context_file",
                        lambda env, path: removed.append(path))
    base = {"backend": "cline", "auth": "chatgpt", "provider": "openai",
            "model": "", "commands": "allow"}
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(surface_runner("cline", "terminal")(
            base, pointer=_POINTER, extra_system="be brief"))
    assert "/workspace/AGENTS.md" in removed
