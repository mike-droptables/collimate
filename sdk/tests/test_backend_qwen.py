# SPDX-License-Identifier: MIT
"""Unit tests for the qwen-code backend's hold on the grant's ``commands``
axis (deny < ask < allow): the root-owned system settings file, the approval
mode on the command line and the session, the mode rules, the client's
mode-switch filtering and drift guard, and the TUI (offered at every level;
under deny the agent has no shell tool there).

Container-free: ``ensure_env`` and the container writer are stubbed, and the
spawn is captured, never run. The live round trips are in
``tests_live/test_live_acp_conformance.py``."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re

import pytest
import gen
from collimate.permissions import Grant

import _agent_shared as eng  # noqa: E402
import _agent_shared.acp_client as ac  # noqa: E402
import _agent_shared.acp_run as ar  # noqa: E402
from _agent_shared.harnesses.qwen_code import backend as qb  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from tests._agent_run import in_cell  # noqa: E402
from tests._workspace import GENERATORS

# The run's terminal pointer a leg is handed (synthetic run label).
_POINTER = "_runs/tester-0badc0de/terminal/qwen-code.terminal"


_WEB = ["web_search", "web_fetch", "google_web_search"]


# ── system settings + approval mode ───────────────────────────────────

@pytest.mark.parametrize("commands, permissions", [
    ("deny", {"deny": ["run_shell_command", "agent"]}),
    ("ask", {"ask": ["run_shell_command"], "deny": ["agent"]}),
    ("allow", None),
])
def test_system_settings_per_level(commands, permissions):
    """The shell rule and the web exclusion; under ask also the subagent
    tool, whose delegates (a definition with its own yolo approvalMode) run
    commands without asking — under deny the shell rule binds them. Hooks,
    the memory agents and the cell's own program-running settings stay as
    qwen-code and the cell set them."""
    s = qb.system_settings(commands, websearch=False)
    expected = {"tools": {"exclude": _WEB}}
    if permissions:
        expected["permissions"] = permissions
    assert s == expected


def test_system_settings_carry_the_web_tools_and_mcp_servers_when_asked():
    s = qb.system_settings("allow", websearch=True,
                           mcp_servers={"collimate": {"httpUrl": "http://x/mcp"}})
    assert s["tools"]["exclude"] == []
    assert s["mcpServers"] == {"collimate": {"httpUrl": "http://x/mcp"}}


@pytest.mark.parametrize("commands, mode", [
    ("deny", "default"), ("ask", "default"), ("allow", "yolo")])
def test_approval_mode_per_level(commands, mode):
    assert qb.approval_mode(commands) == mode


@pytest.mark.parametrize("commands, mode, allowed", [
    ("deny", "default", True), ("deny", "plan", True), ("deny", "auto-edit", True),
    ("deny", "auto", False), ("deny", "yolo", False),
    ("ask", "default", True), ("ask", "plan", True), ("ask", "auto-edit", False),
    ("ask", "auto", False), ("ask", "yolo", False),
    ("allow", "yolo", True), ("allow", "auto", True),
])
def test_qwen_mode_rules(commands, mode, allowed):
    assert ar.mode_allowed("qwen-code", commands, mode) is allowed


def test_a_mode_select_past_the_grant_is_refused():
    ar._LAST_UI_OPTIONS[("qwen-code", "k")] = [{"id": "mode", "currentValue": "default"}]
    try:
        with pytest.raises(PermissionError):
            asyncio.run(ar.apply_config_change("qwen-code", "k", "mode", "yolo",
                                               commands="ask"))
        with pytest.raises(PermissionError):
            asyncio.run(ar.apply_config_change("qwen-code", "k", "mode", "auto-edit",
                                               commands="ask"))
    finally:
        ar._LAST_UI_OPTIONS.clear()
        ar._STICKY_CONFIG.clear()


# ── the ACP profile ───────────────────────────────────────────────────

class _Env:
    def __init__(self):
        self.files: dict = {}
        self.container = self
        self.container_id = "cid"

    def exec(self, argv, **kw):
        result = self.exec_run(argv, **kw)
        return result.exit_code, result.output.decode()

    def exec_run(self, argv, user=None, **kw):
        # cli_container.write_root_file: base64 through sh, as ``user``.
        for m in re.finditer(r"echo '?([A-Za-z0-9+/=]+)'? \| base64 -d > '?([^' ]+)'?",
                             argv[-1]):
            self.files[m.group(2)] = (base64.b64decode(m.group(1)).decode(), user)

        class _R:
            exit_code = 0
            output = b""
        return _R()


@pytest.fixture
def qwen_env(request, monkeypatch, tmp_path):
    from _agent_shared import cli_container as cc

    env = _Env()

    async def _ensure_env(name, **kw):
        return env

    def _write(target, path, content, *, mkdir=None, user="coder"):
        target.files[path] = (content, user)

    async def _key(provider, **kw):
        return "test-key"

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    in_cell(request, monkeypatch, tmp_path / "run-ws", "cell-q")
    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(cc, "write_container_file", _write)
    monkeypatch.setattr(_pipeline, "account_api_key", _key)
    monkeypatch.setattr(ar.gen, "log", lambda *_a, **_k: None, raising=False)
    return env


def _ctx(**over) -> dict:
    ctx = {"auth": "api_key", "provider": "cerebras", "model": "qwen-3.8-27b",
           "system_prompt": "be brief", "enable_file_tools": True,
           "websearch": True, "commands": "deny"}
    ctx.update(over)
    return ctx


def _spawn(profile) -> dict:
    seen: dict = {}

    async def _fake(backend, env_obj, argv, *, env_vars, cwd="/workspace"):
        seen.update(argv=argv, env_vars=env_vars)
        return object()
    old = qb.spawn_container_adapter
    qb.spawn_container_adapter = _fake
    try:
        asyncio.run(profile.spawn())
    finally:
        qb.spawn_container_adapter = old
    return seen


def _assert_modes_off(env: dict) -> None:
    """qwen's safe and bare modes drop every scope's permission rules and are
    read from the env a workspace ``.env`` may fill. The leg names both off,
    non-empty (qwen's .env loader fills a key that is unset OR empty), and
    ``--no-safe-mode`` rides the argv (qwen reads it before the env)."""
    assert env["QWEN_CODE_SAFE_MODE"] == "0"
    assert env["QWEN_CODE_SIMPLE"] == "0"


def _mode_flags(commands: str) -> list:
    """Safe mode off on the command line, and under deny the shell excluded
    by ``--exclude-tools``: qwen adds that rule after bare mode drops the
    settings' rules, so a ``.env`` that turns bare mode on for a later
    session still finds no shell."""
    return ["--no-safe-mode"] + (
        ["--exclude-tools", "run_shell_command,agent"] if commands == "deny" else [])


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_mode_flags_exclude_the_shell_only_under_deny(commands):
    assert qb.mode_flags(commands) == _mode_flags(commands)


@pytest.mark.parametrize("commands, mode", [
    ("deny", "default"), ("ask", "default"), ("allow", "yolo")])
def test_acp_profile_holds_the_grant(qwen_env, commands, mode):
    profile = asyncio.run(_pipeline.prepare(_HARNESS['qwen-code'], _ctx(commands=commands, websearch=False))).profile
    content, user = qwen_env.files[qb.SYSTEM_SETTINGS_PATH]
    assert user == "root"
    assert not qb.SYSTEM_SETTINGS_PATH.startswith("/home/")
    assert json.loads(content) == qb.system_settings(commands, websearch=False)
    assert profile.session_mode == mode
    # Each qwen process builds one session: the ACP server writes the
    # workspace .env over its env as each session opens.
    assert profile.one_session_per_process is True
    spawned = _spawn(profile)
    assert spawned["argv"] == ["qwen", "--acp", "--experimental-skills",
                               *_mode_flags(commands), "--approval-mode", mode]
    assert spawned["env_vars"]["QWEN_CODE_SYSTEM_SETTINGS_PATH"] == qb.SYSTEM_SETTINGS_PATH
    _assert_modes_off(spawned["env_vars"])
    assert not any(p.startswith("/workspace") for p in qwen_env.files)


# ── the client gate ───────────────────────────────────────────────────

class _Opt:
    def __init__(self, kind, option_id):
        self.kind, self.option_id, self.name = kind, option_id, option_id


class _Call:
    def __init__(self, kind, title="", raw_input=None):
        self.kind, self.title, self.raw_input = kind, title, raw_input
        self.tool_call_id, self.field_meta = "t1", None


def _session(*, commands, session_mode="", **kw):
    profile = ac.SpawnProfile(locus="container", cwd="/workspace", spawn=None,
                              commands=commands, session_mode=session_mode)
    return ac.AcpSession(backend="qwen-code", profile=profile,
                         mcp_servers_for=lambda caps: ([], []), **kw)


def _cards(bridge) -> list:
    return [r["event"] for r in bridge.stream_events
            if r["event"].get("type") == "permission_request"]


# qwen's plan-exit options, verbatim ids and kinds (permissionUtils.ts, 0.23.3).
_PLAN_EXIT = [_Opt("allow_once", "restore_previous"), _Opt("allow_always", "proceed_always"),
              _Opt("allow_once", "proceed_once"), _Opt("reject_once", "cancel")]


def test_the_auto_edit_switch_is_never_offered_under_ask(approval_run):
    """"Yes, and auto-accept edits" (and an edit's "Allow All Edits") moves
    the session to auto-edit, which runs `sed -i` unasked: never on an ask
    card. Under allow it is harmless and stays."""
    bridge = approval_run(lambda card: "proceed_once")
    client = ac._CollimateClient(_session(commands="ask", approvals="ask"))
    asyncio.run(client.request_permission("s", _Call("switch_mode", "Exit plan"), _PLAN_EXIT))
    offered = [o["option_id"] for o in _cards(bridge)[0]["options"]]
    assert offered == ["restore_previous", "proceed_once", "cancel"]
    bridge.stream_events.clear()
    client = ac._CollimateClient(_session(commands="allow", approvals="ask"))
    asyncio.run(client.request_permission("s", _Call("switch_mode", "Exit plan"), _PLAN_EXIT))
    assert "proceed_always" in [o["option_id"] for o in _cards(bridge)[0]["options"]]


@pytest.mark.parametrize("commands, answer, picked", [
    ("ask", "cancel", "cancel"),
    ("ask", "proceed_once", "proceed_once"),
])
def test_a_shell_request_under_ask_is_the_humans_call(approval_run, commands, answer, picked):
    """qwen's shell ask (kind execute; with an ask rule only proceed_once and
    cancel are offered, but a stray always-option is stripped too)."""
    from acp.schema import AllowedOutcome
    bridge = approval_run(lambda card: answer)
    opts = [_Opt("allow_always", "proceed_always_project"), _Opt("allow_once", "proceed_once"),
            _Opt("reject_once", "cancel")]
    call = _Call("execute", "echo hi", raw_input={"command": "echo hi"})
    out = asyncio.run(ac._CollimateClient(_session(commands=commands)).request_permission(
        "s", call, opts)).outcome
    assert isinstance(out, AllowedOutcome) and out.option_id == picked
    assert [o["option_id"] for o in _cards(bridge)[0]["options"]] == ["proceed_once", "cancel"]


def test_a_shell_request_under_deny_is_refused():
    from acp.schema import AllowedOutcome
    opts = [_Opt("allow_once", "proceed_once"), _Opt("reject_once", "cancel")]
    call = _Call("execute", "echo hi", raw_input={"command": "echo hi"})
    out = asyncio.run(ac._CollimateClient(_session(commands="deny")).request_permission(
        "s", call, opts)).outcome
    assert isinstance(out, AllowedOutcome) and out.option_id == "cancel"


def test_a_drift_past_the_grant_is_switched_back(monkeypatch):
    """current_mode_update into a mode the level rules out (auto-edit under
    ask) sets the session straight back to the profile's mode; an allowed
    one is left alone."""
    s = _session(commands="ask", session_mode="default")
    s.config_options = [{"id": "mode", "category": "mode", "currentValue": "default",
                         "type": "select",
                         "options": [{"value": v} for v in
                                     ("plan", "default", "auto-edit", "auto", "yolo")]}]
    applied = []

    async def _apply(cid, value):
        applied.append((cid, value))
        return []
    monkeypatch.setattr(s, "apply_config_change", _apply)

    async def _run():
        s._on_update({"sessionUpdate": "current_mode_update", "currentModeId": "plan"})
        await asyncio.sleep(0)
        s._on_update({"sessionUpdate": "current_mode_update", "currentModeId": "auto-edit"})
        await asyncio.gather(*list(s._mode_tasks))
    asyncio.run(_run())
    assert applied == [("mode", "default")]


def test_no_drift_guard_under_allow(monkeypatch):
    s = _session(commands="allow", session_mode="yolo")
    called = []
    monkeypatch.setattr(s, "_apply_session_mode_request", lambda: called.append(1))
    s._on_update({"sessionUpdate": "current_mode_update", "currentModeId": "auto-edit"})
    assert called == [] and not s._mode_tasks


# ── the TUI ───────────────────────────────────────────────────────────

class _Stop(Exception):
    pass


@pytest.fixture
def qwen_tui(monkeypatch, qwen_env):

    rec: dict = {}


    def _serve_terminal(container_id, command, **kw):
        rec.update(command=list(command), env=kw["env"])
        raise _Stop()

    async def _update(**kw):
        return None

    async def _key(provider, **kw):
        return "test-key"
    monkeypatch.setattr(_pipeline, "account_api_key", _key, raising=False)
    for name, fn in (("commit", _update), ("serve_terminal", _serve_terminal),
                     ("phase", lambda *a, **k: None), ("log", lambda *a, **k: None)):
        monkeypatch.setattr(gen, name, fn, raising=False)
    return rec


def _tui(**over):
    cfg = {"backend": "qwen-code", "provider": "cerebras", "model": "qwen-3.8-27b",
           "websearch": True, "commands": "ask"}
    cfg.update(over)
    asyncio.run(eng.surface_runner("qwen-code", "terminal")(cfg, pointer=_POINTER, extra_system="be brief"))


@pytest.mark.parametrize("commands, mode", [
    ("deny", "default"), ("ask", "default"), ("allow", "yolo")])
def test_tui_holds_the_grant_in_system_scope(
        qwen_tui, qwen_env, commands, mode):
    with pytest.raises(_Stop):
        _tui(commands=commands)
    assert qwen_tui["command"] == ["qwen", *_mode_flags(commands), "--approval-mode", mode]
    assert qwen_tui["env"]["QWEN_CODE_SYSTEM_SETTINGS_PATH"] == qb.SYSTEM_SETTINGS_PATH
    _assert_modes_off(qwen_tui["env"])
    content, user = qwen_env.files[qb.SYSTEM_SETTINGS_PATH]
    settings = json.loads(content)
    assert user == "root"
    assert settings.get("permissions") == {
        "deny": {"deny": ["run_shell_command", "agent"]},
        "ask": {"ask": ["run_shell_command"], "deny": ["agent"]},
        "allow": None}[commands]
    assert not any(p.startswith("/workspace/.qwen") for p in qwen_env.files)


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_a_terminal_request_stays_a_terminal_at_every_level(commands):
    """Under deny the agent has no shell tool in the TUI; the `!` lines the
    user types are the user's own, so the surface is offered."""
    cfg = eng.parse_agent_config(
        {"surface": "terminal", "backend": "qwen-code", "provider": "cerebras"},
        grant=Grant.from_dict({"docker": "full", "internet": "full", "commands": commands}), can_ask=True)
    assert cfg["surface"] == "terminal"
    assert not any("degrades" in w for w in cfg["warnings"])


def test_the_image_falls_back_to_a_plain_qwen_code_release():
    """The Dockerfile default is the offline fallback (runs resolve the latest
    release): a plain version, never a range or a tag that moves."""
    import re
    with open(os.path.join(GENERATORS, "_agent_shared", "harnesses", "qwen_code", "image", "Dockerfile")) as fh:
        assert re.search(r"(?m)^ARG QWEN_CODE_VERSION=\d+\.\d+\.\d+$", fh.read())


@pytest.mark.parametrize('provider,protocol,endpoint',[
    ('anthropic','anthropic','https://api.anthropic.com'),
    ('gemini','gemini',None),('groq','openai','https://api.groq.com/openai/v1'),
    ('openrouter','openai','https://openrouter.ai/api/v1')])
def test_auth_settings_select_native_provider_and_optional_private_key(provider,protocol,endpoint):
    settings=qb.auth_settings(provider,protocol,'selected-native-test-key','chosen-model')
    assert settings['model']=={'name':'chosen-model'}
    auth=settings['security']['auth']
    assert auth['selectedType']==protocol
    assert auth['apiKey']=='selected-native-test-key'
    assert auth.get('baseUrl')==endpoint
    assert 'apiKey' not in qb.auth_settings(provider,protocol,None,'chosen-model')['security']['auth']
