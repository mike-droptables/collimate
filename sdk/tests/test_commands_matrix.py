# SPDX-License-Identifier: MIT
"""F1560: freeze effective commands policy before harness prepare migration.

Expected policy values are literal behavioral assertions, not generated snapshots.
The real profile/terminal builders run; only Docker, credentials, adapters and
terminal registration are recording boundaries. Live enforcement is a separate gate.
"""
from __future__ import annotations

import asyncio
import json
import tomllib

import pytest

import _agent_shared as engine  # noqa: E402
from _agent_shared import agent_config as shared_config  # noqa: E402
from _agent_shared import acp_run as ar  # noqa: E402
from tests._commands_matrix import (  # noqa: E402
    TerminalRecorded,
    recording_runtime,  # noqa: F401 — registers the recording fixture
)

# Kept independent of production dispatch tables: adding/removing a leg must
# change the explicit matrix, and cannot silently remove regression coverage.
HARNESSES = ("claude-code", "codex", "antigravity", "goose", "opencode",
             "qwen-code", "cline", "pi", "hermes", "vibe", "copilot")
HOST_TWINS = ("codex", "goose", "opencode")
# pydantic-ai is a host-locus ACP harness with no terminal.
LEGS = ([(name, "acp") for name in (*HARNESSES, "pydantic-ai")]
        + [(name, "terminal") for name in HARNESSES]
        + [(name, "host-acp") for name in HOST_TWINS])


def _assert_policy(harness, leg, level, env, profile, tmp_path):
    """Assert the policy the actual process receives, including leg overrides."""
    terminal = leg == "terminal"
    launch = env.terminal if terminal else env.spawn
    variables = launch.get("env", launch.get("env_vars", {}))
    argv = launch["argv"]
    files = dict(env.files)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            if path.suffix in (".json", ".yaml", ".toml"):
                files[str(path)] = path.read_text()
    if profile is not None and harness != "hermes":
        assert profile.commands == level

    if harness == "opencode":
        configs = [json.loads(body) for path, body in files.items()
                   if path.endswith("collimate.json") or path.endswith("opencode.json")]
        # ACP carries its top config in the spawn env; terminal writes a file.
        if "OPENCODE_CONFIG_CONTENT" in variables:
            configs.append(json.loads(variables["OPENCODE_CONFIG_CONTENT"]))
        assert configs
        config = configs[-1]
        permission = {"bash": level}
        if level == "ask" and not terminal:
            permission["task"] = "deny"
        assert config["permission"] == permission
        assert config.get("snapshot", True) is (level == "allow")
        if level != "allow":
            assert variables["GIT_CONFIG_GLOBAL"] == "/dev/null"
            assert variables["GIT_CONFIG_NOSYSTEM"] == "1"
    elif harness == "goose":
        assert variables["GOOSE_MODE"] == {"deny": "auto", "ask": "approve", "allow": "auto"}[level]
        extensions = json.loads(variables.get("EXTENSIONS", "null"))
        if level == "allow":
            assert extensions is None
        else:
            assert extensions is not None
            assert "developer" in extensions
            assert ("shell" in extensions["developer"].get("available_tools", ["shell"])) is (level == "ask")
            assert set(extensions) == {"developer", "analyze", "todo", "apps", "tom", "skills"}
        policies = [body for path, body in files.items() if path.endswith("permission.yaml")]
        assert policies
        assert policies[-1] == ("user:\n  always_allow: []\n  ask_before: "
                                + ('["shell"]' if level == "ask" else "[]")
                                + "\n  never_allow: []\n")
    elif harness == "codex":
        if terminal:
            assert ("--dangerously-bypass-approvals-and-sandbox" in argv) is (level == "allow")
            if level != "allow":
                assert argv[argv.index("-a") + 1] == "on-request"
        else:
            assert variables["INITIAL_AGENT_MODE"] == ("agent-full-access" if level == "allow" else "read-only")
            config = json.loads(variables["CODEX_CONFIG"])
            assert config["bypass_hook_trust"] is True
            assert config.get("features", {}).get("shell_tool", True) is (level != "deny")
        if leg != "host-acp":
            requirements = tomllib.loads(files["/etc/codex/requirements.toml"])
            if level == "allow":
                assert requirements == {}
            else:
                assert requirements["allowed_approval_policies"] == ["on-request"]
                assert requirements["allowed_approvals_reviewers"] == ["user"]
                assert requirements["features"]["unified_exec"] is False
                assert requirements["features"].get("shell_tool", True) is (level != "deny")
    elif harness == "qwen-code":
        # OW-P4-11/P4F23 explicitly amends only the original Qwen deny policy.
        settings = json.loads(files[variables["QWEN_CODE_SYSTEM_SETTINGS_PATH"]])
        expected = ({"deny": ["run_shell_command", "agent"]} if level == "deny" else
                    {"ask": ["run_shell_command"], "deny": ["agent"]} if level == "ask" else None)
        assert settings.get("permissions") == expected
        assert "--yolo" not in argv
        assert variables["QWEN_CODE_SAFE_MODE"] == "0"
        assert variables["QWEN_CODE_SIMPLE"] == "0"
        assert "--no-safe-mode" in argv
        if level == "deny":
            assert argv[argv.index("--exclude-tools") + 1] == "run_shell_command,agent"
    elif harness == "cline":
        if terminal:
            assert argv[argv.index("--auto-approve") + 1] == ("true" if level == "allow" else "false")
        if level == "allow":
            assert "CLINE_GLOBAL_SETTINGS_PATH" not in variables
        else:
            policy = json.loads(files[variables["CLINE_GLOBAL_SETTINGS_PATH"]])
            assert policy == {"toolAutoApprove": False, "disabledTools":
                              ["run_commands"] if level == "deny" else
                              ["spawn_agent", "team_spawn_teammate", "team_run_task"]}
            assert json.loads(files[variables["CLINE_MCP_SETTINGS_PATH"]]) == {"mcpServers": {}}
            roots = [command[-1] for command, kwargs in env.execs if kwargs.get("user") == "root"]
            policy_script = next(script for script in roots
                                 if variables["CLINE_GLOBAL_SETTINGS_PATH"] in script)
            expected_chown = ("chown coder:coder " + variables["CLINE_GLOBAL_SETTINGS_PATH"]
                              + " " + variables["CLINE_MCP_SETTINGS_PATH"])
            assert (expected_chown in policy_script) is terminal
    elif harness == "pydantic-ai":
        # Host locus: Coder's own shell is removed under deny, carded under
        # ask (the client gate), and runs on this machine under allow.
        assert launch["backend"] == "pydantic-ai"
        assert argv[:2] == ["/fake/pydantic-ai/python", "-I"]
        assert variables["COLLIMATE_PAI_COMMANDS"] == level
        assert profile.locus == "host"
        assert list(env.spawn["inherited"].values()) == ["matrix-fake-key"]
        assert "matrix-fake-key" not in "".join(variables.values())
    elif harness == "pi":
        assert variables["COLLIMATE_COMMANDS"] == level
        if terminal:
            assert argv[0] == "/opt/collimate/pi/collimate-pi"
        else:
            assert argv[0] == "pi-acp"
            assert variables["PI_ACP_PI_COMMAND"] == "/opt/collimate/pi/collimate-pi"
    elif harness == "antigravity":
        from _agent_shared.harnesses.antigravity import backend as agy
        if terminal:
            configs = [json.loads(body) for path, body in files.items()
                       if path.endswith("settings.json") and "permissions" in body]
            assert configs
            rules = configs[-1]["permissions"]
            assert rules == ({"deny": ["command(*)"]} if level == "deny" else
                             {"ask": ["command(*)"]} if level == "ask" else {})
            assert ("--dangerously-skip-permissions" in argv) is (level == "allow")
        else:
            # The server asks the client for every command (kind execute, on
            # the shared gate); it has no settings of its own for the level.
            assert argv == ["/opt/agy-acp/agy_acp_server.par", "--uid="]
            assert not [name for name in variables if name.startswith("COLLIMATE_AGY")]
        guards = env.ensures[-1][1]["readonly_files"]
        assert json.loads(guards[agy.HOOKS_PATH]) == agy.hooks_json(level)
    elif harness == "hermes":
        import yaml
        managed = yaml.safe_load(files["/etc/hermes/config.yaml"])
        disabled = managed["agent"]["disabled_toolsets"]
        # Hermes cannot ask before every command: ask holds as deny.
        held = "deny" if level == "ask" else level
        assert ("terminal" in disabled and "code_execution" in disabled) is (held == "deny")
        assert managed["approvals"]["mode"] == ("off" if held == "allow" else "manual")
        if profile is not None:
            assert profile.commands == held
    elif harness == "vibe":
        disabled = json.loads(variables["VIBE_DISABLED_TOOLS"])
        assert ("bash" in disabled) is (level == "deny")
        assert variables.get("VIBE_TOOLS__BASH__PERMISSION") == {"deny": None, "ask": "ask",
                                                                "allow": "always"}[level]
        assert (variables.get("VIBE_ENABLE_AUTO_APPROVE") == "false") is (level != "allow")
        assert (variables.get("VIBE_TOOLS__BASH__ALLOWLIST") == "[]") is (level == "ask")
    elif harness == "copilot":
        flags = list(argv)
        denied = [flags[i + 1] for i, f in enumerate(flags) if f == "--deny-tool"]
        allowed = [flags[i + 1] for i, f in enumerate(flags) if f == "--allow-tool"]
        excluded = [flags[i + 1] for i, f in enumerate(flags) if f == "--excluded-tools"]
        assert ("shell" in denied) is (level == "deny")
        assert ("bash" in ",".join(excluded).split(",")) is (level == "deny")
        assert allowed == (["shell"] if level == "allow" else [])
        assert "--allow-all-tools" not in flags and "--yolo" not in flags and "--allow-all" not in flags
    elif harness == "claude-code":
        if terminal:
            assert ("--dangerously-skip-permissions" in argv) is (level == "allow")
            if level != "allow":
                permissions = json.loads(argv[argv.index("--settings") + 1])["permissions"]
                assert permissions.get("ask", []) == (["Bash", "Monitor", "PowerShell"] if level == "ask" else [])
                assert ("--disallowedTools" in argv) is (level == "deny")
                if level == "deny":
                    assert argv[argv.index("--disallowedTools") + 1] == "Bash,Monitor,PowerShell"
        else:
            options = profile.session_meta["claudeCode"]["options"]
            assert ("Bash" in options["tools"]) is (level != "deny")
            assert options["allowDangerouslySkipPermissions"] is False
            assert options["permissionMode"] == "default"
            assert options["disallowedTools"] == (["Bash", "Monitor", "PowerShell"] if level == "deny"
                                                  else ["Monitor", "PowerShell"])


@pytest.mark.parametrize("harness,leg", LEGS, ids=[f"{h}-{l}" for h, l in LEGS])
@pytest.mark.parametrize("level", ("deny", "ask", "allow"))
def test_harness_leg_commands_policy_F1560(recording_runtime, tmp_path, monkeypatch, harness, leg, level):  # noqa: F811
    env = recording_runtime
    ctx = {"auth": "api_key", "provider": "openrouter", "model": "openai/gpt-4.1-mini",
           "system_prompt": "", "enable_file_tools": True, "websearch": True,
           "commands": level, "runtime": "docker", "host_harness": None}
    if harness == "claude-code":
        ctx.update(provider="anthropic", model="sonnet")
        if leg == "terminal":
            # Its CLI is this machine's own, run out of the entry's home.
            import gen
            ctx.update(runtime="standard", host_harness={"path": "/fake/claude", "version": "2.1.281",
                                                         "binary": "claude"})
            monkeypatch.setattr(gen, "account", lambda role: {"id": "matrix-entry", "secrets": {}})
    if harness == "antigravity":
        ctx.update(provider="gemini", model="default")
    if harness == "vibe":
        ctx.update(provider="mistral", model="")
    profile = None
    if leg == "terminal":
        runner = engine.surface_runner(harness, "terminal")
        with pytest.raises(TerminalRecorded):
            asyncio.run(runner({**ctx, "backend": harness}, pointer="_runs/matrix-12345678/terminal/session.terminal"))
    else:
        if leg == "host-acp":
            ctx.update(runtime="standard", host_harness={"path": f"/fake/{harness}", "version": "1.0"})
        profile = asyncio.run(ar._PROFILES[harness](ctx))
        asyncio.run(profile.spawn())
    _assert_policy(harness, leg, level, env, profile, tmp_path)


def test_commands_matrix_enumerates_external_legs_F1560():
    from _agent_shared.harnesses import HARNESS
    assert set(ar._PROFILES) == {*HARNESSES, "pydantic-ai"}
    assert {name for name, row in HARNESS.items() if row.host_twin} == set(HOST_TWINS)
    assert set(shared_config._BACKENDS) == {*HARNESSES, "pydantic-ai"}
    assert HARNESS["pydantic-ai"].locus == "host" and HARNESS["pydantic-ai"].terminal is None
