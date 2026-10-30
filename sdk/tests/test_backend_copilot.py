# SPDX-License-Identifier: MIT
"""GitHub Copilot CLI's renderers: tool flags, BYOK and the token login."""
from __future__ import annotations

from pathlib import Path

import pytest


from _agent_shared.harnesses.copilot import backend as copilot  # noqa: E402


def _pairs(flags, name):
    return [flags[i + 1] for i, flag in enumerate(flags) if flag == name]


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_the_commands_level_is_the_server_wide_tool_flags(commands):
    flags = copilot.tool_flags(commands, websearch=True)
    assert "--disable-builtin-mcps" in flags and "--no-auto-update" in flags
    assert ("shell" in _pairs(flags, "--deny-tool")) is (commands == "deny")
    assert _pairs(flags, "--excluded-tools") == (["bash"] if commands == "deny" else [])
    assert _pairs(flags, "--allow-tool") == (["shell"] if commands == "allow" else [])
    assert not {"--allow-all", "--allow-all-tools", "--yolo"} & set(flags)


def test_no_internet_withholds_the_web_tools_and_denies_every_url():
    flags = copilot.tool_flags("deny", websearch=False)
    assert _pairs(flags, "--excluded-tools") == ["bash,web_fetch,web_search"]
    assert "url" in _pairs(flags, "--deny-tool")


def test_a_provider_key_runs_through_copilots_byok():
    # Copilot's Anthropic client appends ``/v1/messages`` itself, so the base
    # URL is the host: the registry's ``…/v1`` would reach ``/v1/v1/messages``.
    assert copilot.provider_env("anthropic", "sk", "claude-sonnet-5-5") == {
        "COPILOT_PROVIDER_BASE_URL": "https://api.anthropic.com", "COPILOT_PROVIDER_TYPE": "anthropic",
        "COPILOT_PROVIDER_API_KEY": "sk", "COPILOT_MODEL": "claude-sonnet-5-5"}
    env = copilot.provider_env("openai", "sk", "gpt-6-astra")
    assert env["COPILOT_PROVIDER_TYPE"] == "openai" and env["COPILOT_PROVIDER_WIRE_API"] == "responses"


def test_the_leased_token_is_one_stripped_line(tmp_path):
    lease = tmp_path / "token"
    lease.write_text("github_pat_11SYNTHETIC\n")
    assert copilot.read_token(str(lease)) == "github_pat_11SYNTHETIC"


@pytest.mark.parametrize("commands,value,allowed", [
    ("allow", "on", True), ("ask", "on", False), ("deny", "off", True),
    ("ask", "https://agentclientprotocol.com/protocol/session-modes#autopilot", False),
    ("ask", "https://agentclientprotocol.com/protocol/session-modes#plan", True)])
def test_allow_all_and_autopilot_are_offered_only_under_allow(commands, value, allowed):
    from _agent_shared.acp_policy import mode_allowed
    assert mode_allowed("copilot", commands, value) is allowed


@pytest.mark.parametrize("provider", ["openai", "anthropic", "groq", "cerebras", "openrouter", "xai", "mistral"])
def test_byok_base_url_plus_the_path_copilot_appends_is_the_registry_endpoint(provider):
    """Copilot appends ``/messages`` under an anthropic-type provider's own
    ``/v1`` (``<base>/v1/messages``) and ``/chat/completions`` (or
    ``/responses``) under an openai-type one's base as given, so the request
    path is the registry endpoint's path plus the call."""
    from collimate.providers import STANDARD_MODEL_ENDPOINTS

    env = copilot.provider_env(provider, "sk", "m")
    appended = "/v1/messages" if env["COPILOT_PROVIDER_TYPE"] == "anthropic" else (
        "/responses" if provider == "openai" else "/chat/completions")
    registry = STANDARD_MODEL_ENDPOINTS[provider].rstrip("/")
    reached = env["COPILOT_PROVIDER_BASE_URL"].rstrip("/") + appended
    assert "/v1/v1" not in reached
    assert reached.startswith(registry.removesuffix("/v1") if env["COPILOT_PROVIDER_TYPE"] == "anthropic" else registry)
    assert reached.endswith(("/messages", "/chat/completions", "/responses"))


def test_the_image_locks_the_customisation_surfaces_an_agent_can_write():
    """Under ``copilot --acp`` the workspace's hooks and MCP servers load
    whether or not its folder is trusted (``disableAllHooks`` is not honoured
    in 1.0.103), so the image carries the managed settings Copilot reads from
    /etc/github-copilot, root-owned: hooks, custom agents and skills may then
    come only from plugins or managed settings."""
    import json
    import re

    dockerfile = (Path(copilot.__file__).parent / "image" / "Dockerfile").read_text()
    managed = re.search(r"printf '%s\\n' '(\{.*?\})' > /etc/github-copilot/managed-settings\.json", dockerfile)
    assert managed, "the image must write /etc/github-copilot/managed-settings.json"
    # hooks, custom agents and skills; not mcp, which would drop the run's own bridge
    assert json.loads(managed.group(1)) == {"strictPluginOnlyCustomization": ["skills", "agents", "hooks"]}
    assert re.search(r"chmod 0644 /etc/github-copilot/managed-settings\.json", dockerfile)
    # written as root, before the image drops to the agent's user
    assert dockerfile.index("managed-settings.json") < dockerfile.index("USER coder")


def test_prepare_trusts_no_folder_and_writes_no_copilot_config(tmp_path, monkeypatch):
    """The old config.json trusted /workspace, which loads the workspace's MCP
    servers and settings hooks (a file the agent writes naming a command). No
    config.json is written, and what the agent left in the home (settings,
    MCP server file, saved approvals) is cleared, so nothing is trusted or read
    back."""
    import asyncio
    from types import SimpleNamespace

    written = []
    monkeypatch.setattr(copilot, "harness_home", lambda backend: str(tmp_path))
    monkeypatch.setattr(copilot, "write_native_file", lambda root, name, body: written.append((name, body)))

    async def ensure_env(*args, **kwargs):
        return SimpleNamespace(container_id="c")

    import _agent_shared.cli_container as cli_container
    monkeypatch.setattr(cli_container, "ensure_env", ensure_env)
    ctx = {"credentials": SimpleNamespace(plan=None, key="sk", login=""), "model": "m", "commands": "ask",
           "provider": "openai", "websearch": False}
    asyncio.run(copilot.prepare(ctx, leg="acp"))
    assert written == [(name, None) for name in
                       ("config.json", "settings.json", "mcp-config.json", "permissions-config.json")]
    assert not any(b and b"trustedFolders" in b for _name, b in written)


@pytest.mark.parametrize("service, auth, listed", [
    ("gitlab", "api_key", ["GIT_CONFIG_VALUE_0", "GITLAB_TOKEN"]),
    ("github", "api_key", ["GIT_CONFIG_VALUE_0"]),
    ("github", "ssh_key", []),
    (None, None, []),
])
def test_secret_env_vars_name_every_git_secret_copilot_receives(tmp_path, monkeypatch, service, auth, listed):
    """Copilot's own redaction list names every secret the spawn hands it:
    the git header and the GitLab token with a token account bound, never
    the gh names (Copilot never receives them), nothing for an SSH key or
    without the role."""
    import asyncio
    from types import SimpleNamespace

    from _agent_shared import accounts
    from _env_shared.git_account import git_account
    from tests._git_fixtures import bind_account, git_account_snapshot

    bind_account(monkeypatch, git_account_snapshot(service, auth) if service else None)
    monkeypatch.setattr(accounts, "_git", git_account())
    monkeypatch.setattr(copilot, "harness_home", lambda backend: str(tmp_path))
    monkeypatch.setattr(copilot, "write_native_file", lambda root, name, body: None)

    async def ensure_env(*args, **kwargs):
        return SimpleNamespace(container_id="c")

    import _agent_shared.cli_container as cli_container
    monkeypatch.setattr(cli_container, "ensure_env", ensure_env)
    ctx = {"credentials": SimpleNamespace(plan=None, key="sk", login=""), "model": "m", "commands": "ask",
           "provider": "openai", "websearch": False}
    prepared = asyncio.run(copilot.prepare(ctx, leg="acp"))
    [names] = _pairs(list(prepared.argv), "--secret-env-vars")
    assert names.split(",") == ["COPILOT_PROVIDER_API_KEY", *listed]
    assert not {"GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"} & set(names.split(","))


@pytest.mark.parametrize("kind,title,raw,command", [
    ("execute", "bash", {"command": "ls"}, True),
    ("other", "write_bash", {"shellId": "s1", "input": "touch x{enter}", "delay": 3}, True),
    ("other", "Write to shell", {"shellId": "s1", "input": "touch x{enter}"}, True),
    ("read", "view", {"path": "/workspace"}, False),
    ("other", "stop_bash", {"shellId": "s1"}, False)])
def test_a_write_into_a_running_shell_is_a_command_whatever_its_kind(kind, title, raw, command):
    """Input written into a running shell runs as a command, so under ask it
    is carded like one (and refused under deny)."""
    from types import SimpleNamespace

    from _agent_shared import acp_policy

    session = SimpleNamespace(backend="copilot", _tool_names={})
    call = SimpleNamespace(kind=kind, title=title, raw_input=raw, field_meta=None, tool_call_id="t1")
    assert acp_policy._is_command_request(session, call) is command
