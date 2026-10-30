# SPDX-License-Identifier: MIT
"""Unit tests for the pi backend (``generators/_agent_shared/harnesses/pi/backend.py``).

Two halves, both bridge-free and deterministic:

  * the credential arrangement — the shared plan login store, the readiness
    predicates the bind turns on, the promote that fills it, and
    ``pi_volumes``;
  * the terminal leg's container/TTY request, asserted by stubbing ``gen`` and
    ``ensure_env`` the way the codex and antigravity suites do. It has to MATCH
    the ACP profile's mounts, because one cached container serves both legs.

Nothing here touches docker, subprocess, or the network; the real TTY round
trip is the end-to-end smoke's job. Only synthetic inputs — no on-disk
generator names, no shipped-asset filenames, and never a real credential.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re

import pytest
import gen


# pi_backend is generator-side shared code; import it the way the `agent`
# generator does (sibling on the generators path).
from _agent_shared import login_store as ls
from _agent_shared.harnesses.pi import backend as pb  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
import _agent_shared as engine  # noqa: E402

from tests._workspace import GENERATORS


# The run's terminal pointer a leg is handed (synthetic run label).
_POINTER = "_runs/tester-0badc0de/terminal/pi.terminal"


@pytest.fixture
def home(cache_root):
    from _agent_shared.harness_homes import harness_home

    return harness_home("pi")


def _write_auth(path: str, content) -> str:
    """An auth.json at ``path`` — a dict is dumped, a string written raw (the
    truncated-mid-write and empty-mountpoint cases)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content if isinstance(content, str) else json.dumps(content))
    return path


def _oauth(**extra) -> dict:
    return {"type": "oauth", "access": "a", "refresh": "r",
            "expires": 9999999999999, **extra}


_CHATGPT = {"openai-codex": _oauth(accountId="acct-1")}
_XAI = {"xai": _oauth()}
# pi keeps EVERY credential in the one flat map, API keys included — and an
# API-key entry is not a subscription login.
_API_ENTRY = {"openai-codex": {"type": "api", "key": "sk-x"}}


# ── the store's location ──────────────────────────────────────────────


def test_the_container_paths_are_pis_own_defaults(cache_root):
    """Collimate sets neither PI_CODING_AGENT_DIR nor HOME away from the
    image's, so pi resolves its agent dir to ``$HOME/.pi/agent`` — which is
    what the mount and the nested bind are addressed against."""
    assert pb.PI_HOME == "/home/coder/.pi"
    assert pb.PI_AUTH_PATH == "/home/coder/.pi/agent/auth.json"
    assert pb.PI_SETTINGS_PATH == "/home/coder/.pi/agent/settings.json"
    assert pb.PI_AUTH_PATH.startswith(pb.PI_HOME + "/")


# ── readiness: what counts as a plan login ────────────────────────────

@pytest.mark.parametrize("content, chatgpt, xai", [
    ({}, False, False),                     # pi's own "{}" first-touch file
    (_CHATGPT, True, False),
    (_XAI, False, True),
    ({**_CHATGPT, **_XAI}, True, True),     # one file, both plans
    (_API_ENTRY, False, False),             # a key is not a subscription
    # A half-written entry is not a login either — pi's own reader demands
    # string access/refresh and a finite expiry.
    ({"openai-codex": {"type": "oauth"}}, True, False),
])
def test_selected_login_ready_reads_the_plan_key(cache_root, monkeypatch, content, chatgpt, xai):
    for plan, expected in [("chatgpt", chatgpt), ("xai", xai)]:
        _select_test_plan(monkeypatch, "pi", plan)
        _write_auth(pb.shared_login_path(), content)
        assert ls.selected_login_ready("pi", plan) is expected
    # api_key is never "ready": there is no plan key to look for.
    assert ls.selected_login_ready("pi", "api_key") is False


def test_selected_login_ready_is_false_for_an_unreadable_store(cache_root):
    """Absent, or truncated by a concurrent in-place write: either way there is
    nothing to sign in with, and the caller's job is to say so — never to
    crash on the parse."""
    assert ls.selected_login_ready("pi", "chatgpt") is False
    _write_auth(pb.shared_login_path(), '{"openai-codex": {"type": "oau')
    assert ls.selected_login_ready("pi", "chatgpt") is False
    _write_auth(pb.shared_login_path(), "")
    assert ls.selected_login_ready("pi", "chatgpt") is False


# ── pi_volumes: the bind exists only once there is a login ────────────

def test_volumes_bind_the_login_on_a_plan_only(cache_root, home):
    """A plan leg binds the leased store over pi's auth.json (the pipeline
    already refused a lease without that plan's login); an API-key leg never
    does."""
    mount = {home: {"bind": "/home/coder/.pi", "mode": "rw"}}
    assert pb.pi_volumes("api_key") == mount
    _write_auth(pb.shared_login_path(), _CHATGPT)
    assert pb.pi_volumes("chatgpt") == {
        **mount,
        pb.shared_login_path(): {"bind": pb.PI_AUTH_PATH, "mode": "rw"},
    }
    assert pb.pi_volumes("api_key") == mount
    # Never the user's own pi home, under any auth.
    real = os.path.expanduser("~/.pi")
    assert not any(src == real or src.startswith(real + os.sep)
                   for src in pb.pi_volumes("chatgpt"))


def test_volumes_create_the_agent_dir_before_declaring_the_nested_bind(
        cache_root, home, monkeypatch):
    """Docker creates a missing mountpoint's parent itself — as ROOT. That dir
    lands inside a bind of a host directory owned by the invoking user, so a
    root-owned ``agent/`` would leave pi (uid 1000) unable to write its
    settings, sessions and model cache beside the credential."""
    agent_dir = os.path.join(home, "agent")
    assert not os.path.exists(agent_dir)
    _select_test_plan(monkeypatch, "pi", "xai")
    _write_auth(pb.shared_login_path(), _XAI)
    assert pb.pi_volumes("xai")[pb.shared_login_path()]["bind"] == pb.PI_AUTH_PATH
    assert os.path.isdir(agent_dir)


# ── promote: the first sign-in becomes everyone's ─────────────────────


# ── settings.json (pure) ──────────────────────────────────────────────

def test_settings_quiet_on_both_legs_and_the_plan_pair_only_under_a_plan():
    """``quietStartup`` always (the banner pi prints would otherwise be the
    first thing in a transcript); the provider/model pair only under a plan,
    where it is what pi's startup resolution reads when the user resets the
    model from inside the TUI."""
    assert pb.settings_json(auth="api_key", provider="anthropic",
                            model="claude-sonnet-4-6") == {"quietStartup": True}
    assert pb.settings_json(auth="chatgpt", provider="openai-codex",
                            model="gpt-5.5") == {
        "quietStartup": True, "defaultProvider": "openai-codex",
        "defaultModel": "gpt-5.5"}
    # A blank model means "pi picks" — naming no model beats naming "".
    assert pb.settings_json(auth="xai", provider="xai", model="") == {
        "quietStartup": True, "defaultProvider": "xai"}


# ── the terminal leg's container + TTY request ────────────────────────


class _Stop(Exception):
    """Ends the TTY runner at the terminal-surface boundary — everything after
    it is the PTY and the renderer."""


class _FakeContainer:
    def __init__(self, rec):
        self.rec = rec

    def exec_run(self, argv, user=None):
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
        result = self.container.exec_run(argv, user=kwargs.get("user"))
        return result.exit_code, result.output.decode("utf-8")


@pytest.fixture
def stub_terminal(monkeypatch, cache_root, selected_account_runtime):
    """Drive the selected Pi terminal entry as far as its terminal registration, with the
    container and the run surface stubbed out. Returns the recorded calls."""
    target = selected_account_runtime / 'pi/auth.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    # A realistic opaque token avoids redacting individual letters in native status text.
    target.write_text(json.dumps({'openai-codex': {'type': 'oauth', 'refresh': 'synthetic-selected-refresh-opaque-value'}, 'github-copilot': {'type': 'oauth', 'refresh': 'synthetic-selected-refresh-opaque-value'}, 'xai': {'type': 'oauth', 'refresh': 'synthetic-selected-refresh-opaque-value'}}))
    from _agent_shared import cli_container as cc

    rec: dict = {"logs": [], "execs": [], "files": {}, "ensure": {}, "serve": {}}
    rec["env"] = _FakeEnv(rec)
    native_write = pb.write_native_file
    def recorded_native_write(root, relative, body):
        native_write(root, relative, body)
        path = pb.PI_HOME + '/' + relative
        if body is None:
            rec['files'].pop(path, None)
        else:
            rec['files'][path] = body.decode()
    monkeypatch.setattr(pb, 'write_native_file', recorded_native_write)

    async def _ensure_env(name, **kw):
        rec["ensure"] = {"name": name, **kw}
        return rec["env"]

    def _serve_terminal(container_id, command, **kw):
        rec["serve"] = {"container_id": container_id, "command": list(command), **kw}
        raise _Stop()

    async def _api_key(provider, **kw):
        rec["api_key_provider"] = provider
        return "key-1"

    async def _update(**kw):
        return None

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def no_bridge(**kwargs):
        yield None
    monkeypatch.setattr(cc, 'leg_bridge', no_bridge)
    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(gen, "commit", _update, raising=False)
    monkeypatch.setattr(_pipeline, "account_api_key", _api_key, raising=False)
    monkeypatch.setattr(gen, "serve_terminal", _serve_terminal, raising=False)
    monkeypatch.setattr(gen, "phase", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gen, "log", lambda m: rec["logs"].append(m),
                        raising=False)
    return rec


def _select_account_plan(plan):
    pytest.fail('selected host account fixture was not installed')


def _run_terminal(*, resume_harness_session_id: str = "", **cfg):
    base = {"backend": "pi", "auth": "chatgpt", "provider": "openai",
            "model": "gpt-5.5", "commands": "allow"}
    base.update(cfg)
    _select_account_plan(base["auth"])
    with pytest.raises(_Stop):
        asyncio.run(engine.surface_runner("pi", "terminal")(
            base, pointer=_POINTER, extra_system="be brief", resume_harness_session_id=resume_harness_session_id))


def test_terminal_leg_with_a_signed_in_store_says_so_and_mounts_it(
        stub_terminal, home):
    _write_auth(pb.shared_login_path(), _CHATGPT)
    _run_terminal()
    assert pb.shared_login_path() in stub_terminal["ensure"]["volumes"]
    assert not any("/login openai-codex" in m for m in stub_terminal["logs"])


def test_a_plan_leg_injects_no_api_key_at_all(stub_terminal):
    """A stored credential OWNS the provider in pi (ambient env is consulted
    only when nothing is stored), so a key handed to a plan container would be
    inert — but the run log would then name a key that billed nothing. The
    modal is never opened either: there is nothing to ask for."""
    _run_terminal(auth="xai", provider="xai", model="grok-4.6")
    assert "api_key_provider" not in stub_terminal
    assert "env_vars" not in stub_terminal["ensure"]
    assert stub_terminal["serve"]["env"] == {"TERM": "xterm-256color",
                                             "COLLIMATE_COMMANDS": "allow"}


def test_the_plan_pins_the_provider_over_a_residual_dropdown_value(stub_terminal):
    """The schema hides the provider field under a plan, but a cell can still
    carry a stale value. The plan pins it — a ChatGPT subscription can only
    ever bill openai-codex, whatever the config says."""
    _run_terminal(auth="chatgpt", provider="anthropic", model="gpt-5.5")
    assert stub_terminal["serve"]["command"] == [
        pb.PI_WRAPPER, "--model", "openai-codex/gpt-5.5",
        "--append-system-prompt", "be brief"]


def test_the_api_key_leg_uses_the_selected_native_credentials(stub_terminal):
    """The same native provider entry the ACP profile writes selects the key
    the terminal bills."""
    _run_terminal(auth="api_key", provider="anthropic", model="claude-sonnet-4-6")
    assert stub_terminal["api_key_provider"] == "anthropic"
    assert "ANTHROPIC_API_KEY" not in stub_terminal["serve"]["env"]
    assert json.loads(stub_terminal["files"][pb.PI_AUTH_PATH]) == {"anthropic": {"type": "api_key", "key": "key-1"}}
    assert stub_terminal["serve"]["env"]["TERM"] == "xterm-256color"
    assert stub_terminal["serve"]["command"] == [
        pb.PI_WRAPPER, "--model", "anthropic/claude-sonnet-4-6",
        "--append-system-prompt", "be brief"]


def test_the_api_key_stays_off_the_container_env_and_argv(stub_terminal):
    """The selected key stays in native private auth.json rather than the
    container environment, terminal exec environment or argv."""
    _run_terminal(auth="api_key", provider="openrouter",
                  model="google/gemini-flash-latest")
    assert "env_vars" not in stub_terminal["ensure"]
    assert "key-1" not in json.dumps(stub_terminal["ensure"], default=str)
    assert "key-1" not in json.dumps(stub_terminal["serve"]["command"])
    assert "OPENROUTER_API_KEY" not in stub_terminal["serve"]["env"]
    assert json.loads(stub_terminal["files"][pb.PI_AUTH_PATH]) == {"openrouter": {"type": "api_key", "key": "key-1"}}


def test_the_system_prompt_rides_pis_own_append_flag_not_the_workspace(
        stub_terminal):
    """§9.1: pi takes the session context on ``--append-system-prompt``, so
    nothing is planted in the cell's workspace (no AGENTS.md to strip at
    teardown) and nothing in the home either. ``--system-prompt`` would
    REPLACE pi's coding-assistant prompt, which is why it is not used."""
    _run_terminal()
    command = stub_terminal["serve"]["command"]
    assert "--append-system-prompt" in command
    assert "--system-prompt" not in command
    assert not any(p.startswith("/workspace") for p in stub_terminal["files"])


def test_the_plan_leg_seeds_pis_own_default_provider_and_model(stub_terminal):
    """settings.json is written on EVERY start: it lives in the per-cell home,
    which outlives the run, so skipping it would leave the previous leg's
    defaults in force for this TTY."""
    _run_terminal(auth="xai", provider="xai", model="grok-4.6")
    assert json.loads(stub_terminal["files"][pb.PI_SETTINGS_PATH]) == {
        "quietStartup": True, "defaultProvider": "xai",
        "defaultModel": "grok-4.6"}


def test_the_terminal_registers_pis_own_pointer_and_is_tier_c(stub_terminal):
    """Tier C: a resume id is accepted for signature uniformity and
    deliberately ignored — pi's TUI selects sessions interactively, and a
    wrong guess would strand the leg on someone else's conversation. The
    pointer declares no toolbar button."""
    _run_terminal(resume_harness_session_id="sess-abc")
    assert stub_terminal["serve"]["pointer"] == _POINTER
    assert stub_terminal["serve"]["cwd"] == "/workspace"
    assert stub_terminal["serve"]["keep_alive"] is True
    assert "sess-abc" not in " ".join(stub_terminal["serve"]["command"])
    assert any("terminal session tier C" in m for m in stub_terminal["logs"])
    assert stub_terminal["serve"]["buttons"] == []


def test_a_blank_model_falls_back_to_the_plans_own_default(stub_terminal):
    """A plan's catalogue is not the API's, so the blank-model default is the
    per-(backend, plan) one — pi's own auto-pick for the provider, not the
    provider's API default (which is an id the subscription cannot serve)."""
    _run_terminal(model="")
    assert stub_terminal["serve"]["command"][:3] == [
        pb.PI_WRAPPER, "--model", "openai-codex/gpt-5.5"]
    _run_terminal(auth="xai", provider="xai", model="")
    assert stub_terminal["serve"]["command"][:3] == [pb.PI_WRAPPER, "--model", "xai/grok-4.6"]


# ── the review's three store fixes (2026-09-11) ──────────────────────


# ── the grant's commands axis ─────────────────────────────────────────

_PI_DIR = os.path.join(GENERATORS, "_agent_shared", "harnesses", "pi", "image")


def _wrapper_argv(level, tmp_path) -> list:
    """What collimate-pi execs for ``level``: the wrapper run by sh with pi
    swapped for a recorder that prints its argv (and the level it saw)."""
    import subprocess

    script = open(os.path.join(_PI_DIR, "collimate-pi")).read()
    assert "PI=/usr/bin/pi\n" in script
    rec = tmp_path / "pi"
    rec.write_text('#!/bin/sh\nprintf "%s\\n" "level=$COLLIMATE_COMMANDS" "$@"\n')
    rec.chmod(0o755)
    wrapper = tmp_path / "collimate-pi"
    wrapper.write_text(script.replace("PI=/usr/bin/pi\n", f"PI={rec}\n"))
    env = {"PATH": os.environ.get("PATH", "")}
    if level is not None:
        env["COLLIMATE_COMMANDS"] = level
    out = subprocess.run(["sh", str(wrapper), "--mode", "rpc"], env=env,
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def test_the_wrapper_runs_pi_with_the_levels_flags(tmp_path):
    """Every level trusts the cell's project-local files for the run
    (``--approve``): the RPC chat loads them without a saved trust decision
    and the TUI does not ask."""
    gate = "/opt/collimate/pi/gate.ts"
    carrier = "/opt/collimate/pi/mcp.js"
    assert _wrapper_argv("allow", tmp_path) == ["level=allow", "--approve", "-e", carrier, "--mode", "rpc"]
    assert _wrapper_argv("ask", tmp_path) == ["level=ask", "--approve", "-e", carrier, "-e", gate,
                                              "--mode", "rpc"]
    denied = ["level=deny", "--approve", "--exclude-tools", "bash,powershell", "-e", carrier, "-e", gate,
              "--mode", "rpc"]
    assert _wrapper_argv("deny", tmp_path) == denied
    # Unset or unknown is deny.
    assert _wrapper_argv(None, tmp_path) == denied
    assert _wrapper_argv("yolo", tmp_path) == denied


def test_the_image_bakes_the_wrapper_and_gate_root_owned():
    dockerfile = open(os.path.join(_PI_DIR, "Dockerfile")).read()
    assert "COPY collimate-pi gate.ts mcp.js /opt/collimate/pi/" in dockerfile
    assert "chown -R root:root /opt/collimate" in dockerfile
    assert dockerfile.index("COPY collimate-pi") < dockerfile.index("USER coder")
    assert pb.PI_WRAPPER == "/opt/collimate/pi/collimate-pi"


def test_the_gate_confirms_with_the_title_the_client_recognises():
    """acp_client._pi_confirm keys on this exact title; the gate reads the
    level from the wrapper's env and fences write/edit to /workspace. A
    human's `!cmd` in the TUI is theirs: the gate has no user_bash handler."""
    gate = open(os.path.join(_PI_DIR, "gate.ts")).read()
    assert 'ctx.ui.confirm("collimate:command"' in gate
    assert 'process.env.COLLIMATE_COMMANDS === "ask"' in gate
    assert "user_bash" not in gate
    assert 'const WORKSPACE = "/workspace"' in gate
    assert 'new Set(["write", "edit"])' in gate
    assert 'new Set(["bash", "powershell"])' in gate


@pytest.mark.parametrize("level", ["deny", "ask", "allow"])
def test_the_tui_runs_the_wrapper_at_the_runs_level(stub_terminal, level):
    _run_terminal(commands=level)
    assert stub_terminal["serve"]["command"][0] == pb.PI_WRAPPER
    assert stub_terminal["serve"]["env"]["COLLIMATE_COMMANDS"] == level
    assert any(f"rm -f {pb.PI_MODELS_PATH}" in e for e in stub_terminal["execs"])


@pytest.mark.parametrize("auth, provider, cleared", [
    ("api_key", "openrouter", True), ("chatgpt", "openai", False)])
def test_the_tui_clears_an_api_key_legs_credential_store(stub_terminal, auth,
                                                          provider, cleared):
    """pi resolves a ``!`` credential by running it as shell on every turn,
    so an api_key leg removes the home's stale auth.json before staging its key;
    a plan leg's store is the bound lease and stays."""
    _run_terminal(auth=auth, provider=provider, model="m")
    (rm,) = [e for e in stub_terminal["execs"] if f"rm -f {pb.PI_MODELS_PATH}" in e]
    assert (pb.PI_AUTH_PATH in rm) is cleared


class _RootEnv:
    def __init__(self):
        self.container = self
        self.container_id = "cid"
        self.root: list = []
        self.files: dict = {}

    def exec_run(self, argv, user=None, workdir=None):
        if user == "root":
            self.root.append(argv[-1])

        class _Res:
            exit_code = 0
            output = b""
        return _Res()

    def exec(self, argv, **kwargs):
        result = self.exec_run(argv, user=kwargs.get("user"))
        return result.exit_code, result.output.decode("utf-8")


@pytest.mark.parametrize("level", ["deny", "ask", "allow"])
def test_the_acp_leg_spawns_pi_through_the_wrapper(monkeypatch, cache_root, level):
    from _agent_shared import acp_run as ar
    from _agent_shared import cli_container as cc

    env = _RootEnv()

    async def _ensure_env(name, **kw):
        return env

    async def _api_key(provider, **kw):
        return "k"

    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(cc, "write_container_file",
                        lambda target, path, content, **kw: env.files.__setitem__(path, content))
    monkeypatch.setattr(_pipeline, "account_api_key", _api_key)
    monkeypatch.setattr(ar.gen, "log", lambda *_a, **_k: None, raising=False)
    ctx = {"auth": "api_key", "provider": "openrouter", "model": "m",
           "system_prompt": "", "enable_file_tools": True,
           "websearch": True, "commands": level}
    profile = asyncio.run(ar._PROFILES["pi"](ctx))
    seen: dict = {}

    async def _spawn(backend, env_obj, argv, *, env_vars, cwd="/workspace"):
        seen.update(env_vars)
        return object()

    monkeypatch.setattr(pb, "spawn_container_adapter", _spawn)
    asyncio.run(profile.spawn())
    assert seen["PI_ACP_PI_COMMAND"] == pb.PI_WRAPPER
    assert seen["COLLIMATE_COMMANDS"] == level
    # models.json and, on this api_key leg, the credential store: both run
    # their `!` values as shell.
    assert env.root == [f"rm -f {pb.PI_MODELS_PATH} {pb.PI_AUTH_PATH}"]


@pytest.fixture(autouse=True)
def selected_account_runtime(account_runtime, monkeypatch):
    snapshot = {"service": "openai", "auth_type": "cli", "metadata": {"harness": "pi", "plan": "chatgpt"}, "secrets": {"oauth_token": "test-subscription"}}
    with account_runtime(snapshot, selectable=True) as (root, select_plan):
        monkeypatch.setitem(globals(), '_select_account_plan', select_plan)
        yield root


def _select_test_plan(monkeypatch, harness, plan):
    """Explicitly select a separate host account before acquiring its lease."""
    import gen
    from collimate.account_taxonomy import PLAN_PROVIDERS
    ls.close_account_credentials()
    monkeypatch.setattr(gen, "account", lambda role: {
        "service": PLAN_PROVIDERS[plan], "auth_type": "cli",
        "metadata": {"harness": harness, "plan": plan},
        "secrets": {"oauth_token": "test-subscription"}})
