# SPDX-License-Identifier: MIT
"""Unit tests for the codex backend's PURE helpers (``codex_backend``).

Config-toml assembly, the TUI session-rollout parser and the
credential/home arrangement (``codex_volumes`` + the shared login store and
the promote that fills it) are all bridge-free and deterministic, so they test
in isolation. The docker /
``gen`` / network paths (the selected terminal entry) are covered by
the end-to-end smoke — except for the container arrangement the TTY leg asks
for, which is asserted here by stubbing ``gen`` and ``ensure_env``: it has to
MATCH the ACP profile's, because one cached container serves both legs.
Nothing in this module touches docker, subprocess, or the network. Only
synthetic inputs; no on-disk generator names or shipped-asset filenames.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from pathlib import Path
import tomllib

import pytest


# The engine is generator-side shared code; import it the way the `agent`
# generator does (sibling on the generators path).
from _agent_shared import harness_homes, login_store as ls
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
import _agent_shared as engine  # noqa: E402
from _agent_shared.harnesses.codex import backend as cb  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from collimate.account_taxonomy import MODEL_HARNESS_RULES  # noqa: E402
from tests._commands_matrix import recording_runtime  # noqa: F401

# The run's terminal pointer a leg is handed (synthetic run label).
_POINTER = "_runs/tester-0badc0de/terminal/codex.terminal"


# ── codex_config_toml ─────────────────────────────────────────────────

def test_config_toml_openai_defaults_are_empty():
    assert cb.codex_config_toml(provider="openai") == ""


def test_config_toml_openrouter_table():
    toml = cb.codex_config_toml(provider="openrouter")
    assert 'model_provider = "collimate"' in toml
    assert "[model_providers.collimate]" in toml
    assert 'name = "openrouter"' in toml
    assert 'base_url = "https://openrouter.ai/api/v1"' in toml
    assert 'experimental_bearer_token = ""' in toml
    assert 'wire_api = "responses"' in toml
    assert "[mcp_servers" not in toml


def test_config_toml_groq_table():
    toml = cb.codex_config_toml(provider="groq")
    assert 'name = "groq"' in toml
    assert 'base_url = "https://api.groq.com/openai/v1"' in toml
    assert 'experimental_bearer_token = ""' in toml
    assert 'wire_api = "responses"' in toml


# ── trace_from_codex_session ──────────────────────────────────────────

def test_trace_from_session_maps_response_items():
    lines = [
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "hello"}]}},
        {"type": "response_item", "payload": {
            "type": "reasoning", "summary": [{"type": "summary_text", "text": "let me think"}]}},
        {"type": "response_item", "payload": {
            "type": "function_call", "name": "shell",
            "arguments": "{\"command\": [\"ls\"]}"}},
        {"type": "response_item", "payload": {
            "type": "function_call_output", "output": "result text"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}]}},
    ]
    jsonl = "\n".join(json.dumps(x) for x in lines) + "\nnot-json-line\n"
    trace = cb.trace_from_codex_session(jsonl)

    assert [e["type"] for e in trace] == \
        ["user", "thinking", "tool_use", "tool_result", "text"]
    assert trace[0]["text"] == "hello"
    assert trace[1]["text"] == "let me think"
    assert trace[2]["tool"] == "shell"
    assert trace[2]["input"] == '{"command": ["ls"]}'
    assert trace[3]["output"] == "result text"
    assert trace[4]["text"] == "done"


def test_trace_from_session_handles_unwrapped_and_string_content():
    # Some rollout lines carry the item at top level (no payload wrapper), and
    # content can be a bare string.
    lines = [
        {"type": "message", "role": "user", "content": "bare string prompt"},
        {"type": "message", "role": "assistant", "content": "bare reply"},
    ]
    jsonl = "\n".join(json.dumps(x) for x in lines)
    trace = cb.trace_from_codex_session(jsonl)
    assert trace == [
        {"type": "user", "text": "bare string prompt"},
        {"type": "text", "text": "bare reply"},
    ]


def test_trace_from_session_empty():
    assert cb.trace_from_codex_session("") == []


# ── the harness home + the shared ChatGPT login ───────────────────────
#
# The contract: CODEX_HOME is Collimate's per-cell harness home under BOTH
# auths (the user's ~/.codex is never read or written), and the chatgpt leg
# shares nothing but ONE auth.json inode — once a sign-in exists to share.


def _write_login(path: str, content) -> str:
    """An auth.json at ``path`` — a dict is dumped, a string written raw (the
    truncated-mid-write case)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content if isinstance(content, str) else json.dumps(content))
    return path


_SIGNED_IN = {"tokens": {"access_token": "a", "refresh_token": "r"},
              "last_refresh": "2026-09-11T00:00:00Z"}
# What the old flow pre-created so the bind would have a source file.
_PLACEHOLDER = {"OPENAI_API_KEY": None, "tokens": None, "last_refresh": None}


def test_a_logged_out_store_is_refused_before_any_bind(cache_root):
    """The blocker this flow was rebuilt around: codex reads an auth.json
    whose tokens are null as "Logged in using ChatGPT" and then aborts at
    bootstrap, so a placeholder would lock the TUI out. Nothing is created:
    the pipeline refuses a ChatGPT leg whose lease holds no login before a
    home is staged or a container placed."""
    assert ls.selected_login_ready("codex", "chatgpt") is False
    with pytest.raises(RuntimeError, match="codex auth=chatgpt"):
        asyncio.run(_pipeline.prepare(_HARNESS["codex"], {
            "auth": "chatgpt", "provider": "openai", "model": "", "commands": "deny"}))
    assert not os.path.exists(cb.shared_login_path())


@pytest.mark.parametrize("content, ready", [
    # The old placeholder is NOT a login (it is why there is no placeholder).
    (_PLACEHOLDER, False),
    # An API-key login is not a ChatGPT subscription login.
    ({"OPENAI_API_KEY": "sk-x", "tokens": None}, False),
    (_SIGNED_IN, True),
    # A Business/Enterprise Codex access-token login writes no tokens at all.
    ({"OPENAI_API_KEY": None, "personal_access_token": "at-x"}, True),
    ({"OPENAI_API_KEY": None, "agent_identity": "eyJ.x.y"}, True),
])
def test_selected_login_ready_reads_the_tokens(cache_root, content, ready):
    _write_login(cb.shared_login_path(), content)
    assert ls.selected_login_ready("codex", "chatgpt") is ready


def test_selected_login_ready_is_false_for_an_unreadable_store(cache_root):
    """Absent, or truncated by a concurrent in-place write: either way there is
    nothing to sign in with, and the caller's job is to say so — never to
    crash on the parse."""
    assert ls.selected_login_ready("codex", "chatgpt") is False
    _write_login(cb.shared_login_path(), '{"tokens": {"access_to')
    assert ls.selected_login_ready("codex", "chatgpt") is False


def test_codex_volumes_bind_the_login_on_the_plan_only(cache_root):
    """A ChatGPT leg binds the leased auth.json over CODEX_HOME's (the
    pipeline already refused a lease with no login); an API-key leg never
    does, and neither ever mounts the user's own codex home."""
    from _agent_shared.harness_homes import harness_home

    home = harness_home("codex")
    mount = {"bind": "/home/coder/.codex-collimate", "mode": "rw"}
    assert cb.codex_volumes("api_key") == {home: mount}
    assert cb.codex_volumes("chatgpt") == {
        home: mount,
        cb.shared_login_path(): {
            "bind": "/home/coder/.codex-collimate/auth.json", "mode": "rw"},
    }
    real = os.path.expanduser("~/.codex")
    assert not any(src == real or src.startswith(real + os.sep)
                   for src in cb.codex_volumes("chatgpt"))


def test_a_missing_login_names_key_manager(cache_root):
    from _agent_shared import login_store as ls

    with pytest.raises(RuntimeError) as refused:
        asyncio.run(_pipeline.prepare(_HARNESS["codex"], {
            "auth": "chatgpt", "provider": "openai", "model": "", "commands": "deny"}))
    # The shared template, plan label and all — never a hand-copied literal.
    assert str(refused.value) == "codex auth=chatgpt: " + ls.login_required(plan="chatgpt", harness="codex")
    assert "Key manager" in str(refused.value)


# ── promote_shared_login: the first sign-in becomes everyone's ────────


class _Stop(Exception):
    """Ends the TTY runner at the container boundary — everything after it is
    docker and the terminal surface."""


@pytest.fixture
def stub_terminal(monkeypatch, cache_root):
    """Drive the selected Codex terminal entry as far as its container request, with the
    container and the run surface stubbed out. Returns the recorded kwargs."""
    from _agent_shared import cli_container as cc

    rec: dict = {"logs": []}

    async def _ensure_env(name, **kw):
        rec.update(name=name, **kw)
        raise _Stop()

    async def _update(**kw):
        return None


    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(cb.gen, "commit", _update, raising=False)
    monkeypatch.setattr(cb.gen, "phase", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(cb.gen, "log", lambda m: rec["logs"].append(m),
                        raising=False)
    return rec


def _run_terminal():
    with pytest.raises(_Stop):
        asyncio.run(engine.surface_runner("codex", "terminal")(
            {"backend": "codex", "auth": "chatgpt", "provider": "openai", "commands": "ask"}, pointer=_POINTER))


def test_terminal_leg_with_a_signed_in_store_says_so_and_mounts_it(
        stub_terminal):
    _write_login(cb.shared_login_path(), _SIGNED_IN)
    _run_terminal()
    assert stub_terminal["volumes"] == cb.codex_volumes("chatgpt")
    assert cb.shared_login_path() in stub_terminal["volumes"]
    assert not any("Device Code" in m for m in stub_terminal["logs"])
    assert cb.shared_login_path() in stub_terminal["volumes"]


def test_the_api_key_never_rides_the_container_env(monkeypatch, stub_terminal):
    """The key reaches the TTY's exec env only: a container the TTY provisions
    (the mint failed, or the container was replaced) must not carry it in its
    own environment, where docker inspect and every later exec would see it."""
    async def _key(provider, **kw):
        return "k-secret"

    monkeypatch.setattr(_pipeline, "account_api_key", _key)
    with pytest.raises(_Stop):
        asyncio.run(engine.surface_runner("codex", "terminal")(
            {"backend": "codex", "auth": "api_key", "provider": "openrouter",
             "commands": "ask"}, pointer=_POINTER))
    assert not any(k.endswith("API_KEY") for k in stub_terminal.get("env_vars", {}))
    assert "k-secret" not in json.dumps(stub_terminal.get("env_vars", {}))


class _TermEnv:
    container_id = "c-1"

    def __init__(self):
        self.execs: list = []

    def exec(self, cmd, **kw):
        self.execs.append((cmd, kw))
        return 0, ""


@pytest.fixture
def stub_tty(monkeypatch, stub_terminal):
    """``stub_terminal`` carried on to the terminal registration: the
    container's files and the ``serve_terminal`` kwargs are recorded."""
    from _agent_shared import cli_container as cc

    rec = stub_terminal
    rec["files"] = {}

    async def _ensure_env(name, **kw):
        rec.update(name=name, **kw)
        rec["env"] = _TermEnv()
        return rec["env"]

    def _serve_terminal(container_id, command, **kw):
        rec["serve"] = {"command": list(command), **kw}
        raise _Stop()

    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(cc, "write_policy_file",
                        lambda env, path, content, **kw: rec["files"].update({path: content}))
    monkeypatch.setattr(cb, "write_codex_locks", lambda *a, **k: None)
    monkeypatch.setattr(cb.gen, "serve_terminal", _serve_terminal, raising=False)
    return rec


@pytest.mark.parametrize("websearch", [False, True])
def test_the_terminal_config_carries_the_web_grant(stub_tty, websearch):
    """The TTY rewrites the per-cell config.toml the ACP leg wrote, so it must
    carry the same web toggle, or a runner with web search off gets codex's
    own default in the TUI. A blank model labels the terminal "default"."""
    _write_login(cb.shared_login_path(), _SIGNED_IN)
    with pytest.raises(_Stop):
        asyncio.run(engine.surface_runner("codex", "terminal")(
            {"backend": "codex", "auth": "chatgpt", "provider": "openai",
             "commands": "ask", "model": "", "websearch": websearch},
            pointer=_POINTER))
    toml = tomllib.loads(Path(harness_homes.harness_home("codex"), "config.toml").read_text())
    assert toml["tools"]["web_search"] is websearch
    assert stub_tty["serve"]["label"] == "Codex · default"


# ── the key env: one spelling for every leg (F1547) ────────────────────


_CODEX_PROVIDERS = tuple(MODEL_HARNESS_RULES["codex"]["providers"])


@pytest.mark.parametrize('provider', _CODEX_PROVIDERS)
@pytest.mark.parametrize('leg', ['acp', 'terminal'])
def test_every_codex_leg_uses_only_selected_native_credentials_F1547(recording_runtime, provider, leg):  # noqa: F811
    from tests.test_codex_prepare import context
    prepared = asyncio.run(_pipeline.prepare(_HARNESS['codex'], context(provider=provider), leg=leg))
    assert 'matrix-fake-key' not in json.dumps(prepared.env)
    home = Path(harness_homes.harness_home('codex'))
    if provider == 'openai':
        assert json.loads((home/'auth.json').read_text()) == {'OPENAI_API_KEY':'matrix-fake-key'}
    else:
        config = tomllib.loads((home/'config.toml').read_text())
        assert config['model_providers']['collimate']['experimental_bearer_token'] == 'matrix-fake-key'
        assert 'env_key' not in config['model_providers']['collimate']

@pytest.fixture(autouse=True)
def selected_account_runtime(account_runtime):
    snapshot = {"service": "openai", "auth_type": "cli", "metadata": {"harness": "codex", "plan": "chatgpt"}, "secrets": {"oauth_token": "test-subscription"}}
    with account_runtime(snapshot) as (root, _select_plan):
        yield root


# ── the commands grant (deny < ask < allow) ───────────────────────────


@pytest.mark.parametrize("commands, features", [
    ("deny", {"shell_tool": False}),
    ("ask", {}),
    ("allow", {}),
])
def test_codex_features_per_level(commands, features):
    """Hooks are the cell's own config: no level turns them off. Code mode
    stays the model's: its script reaches only tools the level gates."""
    assert cb.codex_features(commands) == features


def test_acp_env_allow_is_full_access_with_the_cells_hooks_trusted():
    env = cb.codex_acp_env("allow")
    assert env["INITIAL_AGENT_MODE"] == "agent-full-access"
    assert json.loads(env["CODEX_CONFIG"])["bypass_hook_trust"] is True
    assert json.loads(env["CODEX_CONFIG"])["features"] == {"shell_snapshot": False}


@pytest.mark.parametrize("commands", ["deny", "ask"])
def test_acp_env_below_allow_runs_the_stock_read_only_preset(commands):
    """deny and ask start in codex-acp's own ``read-only`` preset (on-request
    approvals the user answers, workspace-write sandbox), with the level's
    features in the TOP config layer."""
    env = cb.codex_acp_env(commands)
    assert env["INITIAL_AGENT_MODE"] == "read-only"
    config = json.loads(env["CODEX_CONFIG"])
    assert config["bypass_hook_trust"] is True
    assert config["features"] == {**cb.codex_features(commands), "shell_snapshot": False}
    assert config["allow_login_shell"] is False
    assert config["shell_environment_policy"]["inherit"] == "none"


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_the_cells_hooks_run_without_codexs_trust_review_on_both_legs(commands):
    """The cell is trusted: the chat's thread config carries codex's
    ``bypass_hook_trust`` (what its own ``--dangerously-bypass-hook-trust``
    sends on thread/start) and the TUI takes the flag itself, at every
    level. Hooks stay on, and the requirements never lock them."""
    assert json.loads(cb.codex_acp_env(commands)["CODEX_CONFIG"])["bypass_hook_trust"] is True
    assert "--dangerously-bypass-hook-trust" in cb.codex_tui_args(commands)
    assert "hooks" not in cb.codex_features(commands)
    assert "hooks" not in tomllib.loads(cb.codex_requirements_toml(commands)).get("features", {})


@pytest.mark.parametrize("commands", ["deny", "ask"])
def test_requirements_lock_pins_policy_reviewer_and_features(commands):
    """One lock for both legs: the on-request policy the chat's read-only
    preset sends and the terminal's `-a on-request` asks for, answered by
    the user. Hooks are not pinned."""
    req = tomllib.loads(cb.codex_requirements_toml(commands))
    assert req["allowed_approval_policies"] == ["on-request"]
    assert req["allowed_approvals_reviewers"] == ["user"]
    assert req["features"]["unified_exec"] is False      # no write_stdin
    assert "code_mode" not in req["features"]
    assert "hooks" not in req["features"]
    assert req["features"].get("shell_tool", True) is (commands == "ask")


def test_requirements_under_allow_pin_nothing():
    assert tomllib.loads(cb.codex_requirements_toml("allow")) == {}


class _LockEnv:
    """A container that records what runs as root and the files written."""

    def __init__(self, exit_code=0):
        self.container = self
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
            output = b"denied" if self.exit_code else b""
        return _Res()

    def exec(self, argv, **kwargs):
        result = self.exec_run(argv, user=kwargs.get("user"))
        return result.exit_code, result.output.decode("utf-8")


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_every_leg_writes_the_lock_as_root_and_wipes_the_rules(commands):
    env = _LockEnv()
    cb.write_codex_locks(env, commands, "/home/coder/.codex-collimate")
    (script,) = env.root
    assert env.files[cb.REQUIREMENTS_PATH] == cb.codex_requirements_toml(commands)
    assert "chmod 644" in script
    assert script.endswith("rm -rf /home/coder/.codex-collimate/rules")


def test_a_lock_that_cannot_be_written_stops_the_leg():
    with pytest.raises(RuntimeError, match="codex's commands policy"):
        cb.write_codex_locks(_LockEnv(exit_code=1), "ask", "/h")


def test_host_rules_wipe(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "default.rules").write_text('prefix_rule(pattern=["echo"], decision="allow")')
    cb.clear_host_rules(str(tmp_path))
    assert not (tmp_path / "rules").exists()
    cb.clear_host_rules(str(tmp_path))      # nothing there: still fine


def test_tui_args_per_level():
    """The terminal runs the chat leg's model: the cell trusted at every
    level (the workspace a trusted project, so the cell's .codex/ loads, and
    its hooks run without the trust review), and under deny and ask the
    read-only preset's on-request approvals, answered by the user, in the
    workspace-write sandbox."""
    trust = ["-c", 'projects./workspace.trust_level="trusted"',
             "--dangerously-bypass-hook-trust"]
    assert cb.codex_tui_args("allow") == [*trust, "--dangerously-bypass-approvals-and-sandbox"]
    ask = cb.codex_tui_args("ask")
    assert ask == [*trust, "-a", "on-request", "-s", "workspace-write",
                   "-c", 'approvals_reviewer="user"']
    # deny: the same prompts, and no shell tool for the agent.
    deny = cb.codex_tui_args("deny")
    assert deny == [*ask, "--disable", "shell_tool"]
    assert "hooks" not in deny and "--dangerously-bypass-approvals-and-sandbox" not in deny


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_terminal_leg_opens_at_every_level(stub_terminal, commands):
    """The terminal is offered at every level: under deny the agent has no
    shell tool there (codex_tui_args), and a `!` line the user types is the
    user's own."""
    _write_login(cb.shared_login_path(), _SIGNED_IN)
    with pytest.raises(_Stop):
        asyncio.run(engine.surface_runner("codex", "terminal")(
            {"backend": "codex", "auth": "chatgpt", "provider": "openai",
             "commands": commands}, pointer=_POINTER))
    assert stub_terminal["name"] == "codex"


# ── mode lock ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("commands, value, ok", [
    ("allow", "agent-full-access", True), ("allow", "agent", True), ("allow", "read-only", True),
    ("ask", "read-only", True), ("ask", "agent-full-access", False), ("ask", "agent", False),
    ("deny", "read-only", True), ("deny", "agent-full-access", False), ("deny", "agent", False),
])
def test_codex_mode_rule(commands, value, ok):
    from _agent_shared import acp_run
    assert acp_run.mode_allowed("codex", commands, value) is ok


def test_the_host_adapter_default_is_the_image_adapter_owner_F1402_F1425(monkeypatch):
    from _agent_shared import cli_container, host_adapters
    from _agent_shared.adapter_artifacts import ADAPTERS
    from _env_shared import upstream
    from types import SimpleNamespace

    monkeypatch.setattr(host_adapters.gen, 'packages', lambda: {})
    monkeypatch.setattr(host_adapters.gen, 'grant', lambda: SimpleNamespace(update_packages=False))
    monkeypatch.setattr(upstream, 'resolve_requests', lambda *args, **kwargs: {})
    assert host_adapters.resolve_adapter_version('codex-acp') == ADAPTERS['codex-acp'].fallback_version
    assert cli_container._requested_versions('codex')['CODEX_ACP_VERSION'] == ADAPTERS['codex-acp'].fallback_version


def test_openai_key_run_writes_the_registry_endpoint(monkeypatch):
    from collimate.providers import STANDARD_MODEL_ENDPOINTS
    monkeypatch.setitem(STANDARD_MODEL_ENDPOINTS, "openai", "http://mock/v1")
    toml = cb.codex_config_toml(provider="openai", model="m", openai_url=True)
    assert toml.splitlines() == ['model = "m"', 'openai_base_url = "http://mock/v1"']
    assert "openai_base_url" not in cb.codex_config_toml(provider="openai", model="m")
