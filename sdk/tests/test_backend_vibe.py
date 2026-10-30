# SPDX-License-Identifier: MIT
"""Mistral Vibe's renderers: provider config and the environment policy."""
from __future__ import annotations

import json
import tomllib

import pytest


from _agent_shared.harnesses.vibe import backend as vibe  # noqa: E402


def test_mistral_rides_vibes_own_backend_and_a_blank_model_keeps_vibes_default():
    config = tomllib.loads(vibe.config_toml(provider="mistral", native="mistral", model=""))
    assert config["providers"] == [{"name": "mistral", "api_base": "https://api.mistral.ai/v1",
                                    "api_key_env_var": "MISTRAL_API_KEY", "backend": "mistral"}]
    assert "models" not in config


def test_an_openai_compatible_provider_gets_a_model_aliased_by_its_own_id():
    config = tomllib.loads(vibe.config_toml(provider="openrouter", native="openrouter",
                                            model="anthropic/claude-sonnet-5.5"))
    assert config["providers"][0]["api_style"] == "openai"
    assert config["models"] == [{"name": "anthropic/claude-sonnet-5.5", "provider": "openrouter",
                                 "alias": "anthropic/claude-sonnet-5.5"}]


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_the_commands_level_rides_the_environment(commands):
    env = vibe.policy_env(commands, websearch=True)
    disabled = json.loads(env["VIBE_DISABLED_TOOLS"])
    assert ("bash" in disabled) is (commands == "deny")
    assert (env.get("VIBE_ENABLE_AUTO_APPROVE") == "false") is (commands != "allow")
    assert env.get("VIBE_TOOLS__BASH__PERMISSION") == {"deny": None, "ask": "ask", "allow": "always"}[commands]
    assert (env.get("VIBE_TOOLS__BASH__ALLOWLIST") == "[]") is (commands == "ask")
    assert env["VIBE_ENABLE_TELEMETRY"] == "false" and env["VIBE_EXPERIMENTS__ENABLE"] == "false"


def test_no_internet_withholds_the_web_tools_and_connectors():
    env = vibe.policy_env("allow", websearch=False)
    assert set(json.loads(env["VIBE_DISABLED_TOOLS"])) == {"web_fetch", "web_search"}
    assert env["VIBE_ENABLE_CONNECTORS"] == "false"


def test_agent_profiles_never_outlive_a_leg(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "loose.toml").write_text('safety = "yolo"')
    (tmp_path / "prompts").write_text("not a directory")
    vibe._clear_profiles(str(tmp_path))
    assert not (tmp_path / "agents").exists() and not (tmp_path / "prompts").exists()


@pytest.mark.parametrize("commands", ["deny", "ask"])
def test_bypass_tool_permissions_is_off_below_allow(commands):
    env = vibe.policy_env(commands, websearch=True)
    assert env["VIBE_BYPASS_TOOL_PERMISSIONS"] == "false"
    assert env["VIBE_ENABLE_AUTO_APPROVE"] == "false"


def test_allow_does_not_pin_bypass_tool_permissions():
    assert "VIBE_BYPASS_TOOL_PERMISSIONS" not in vibe.policy_env("allow", websearch=True)


_NAMING_A_COMMAND = ("hooks.toml", ".env", "trusted_folders.toml", "tools", "agents", "plugins", "prompts")


def test_every_file_vibe_loads_a_command_from_is_placed_read_only_and_empty():
    guards = vibe.guard_files("CONFIG")
    for name in _NAMING_A_COMMAND:
        assert guards[f"/home/coder/.vibe/{name}"] == "", name
    assert guards["/home/coder/.vibe/config.toml"] == "CONFIG"
    # The terminal leg appends its bridge after placement: config stays writable.
    assert "/home/coder/.vibe/config.toml" not in vibe.guard_files()


def test_the_home_is_linked_in_beside_the_guards_never_over_them():
    from types import SimpleNamespace
    ran = []
    env = SimpleNamespace(exec=lambda argv, user=None: (ran.append((argv, user)) or (0, "")))
    vibe.link_home(env, "/workspace/.collrun/homes/h", vibe.guard_files("c"))
    (argv, user), = ran
    assert user == "root" and argv[4:6] == ["/workspace/.collrun/homes/h", "/home/coder/.vibe"]
    # config.toml is a guard on the acp leg: the link loop must skip it.
    assert "config.toml" in argv[6:] and "hooks.toml" in argv[6:]
    ran.clear()
    vibe.link_home(env, "/workspace/.collrun/homes/h", vibe.guard_files())
    assert "config.toml" not in ran[0][0][6:]


def test_the_home_link_script_skips_guards_and_links_the_rest(tmp_path):
    import subprocess
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    for name in ("logs", "AGENTS.md", "config.toml"):
        (src / name).mkdir() if name == "logs" else (src / name).write_text(name)
    dst.mkdir()
    (dst / "config.toml").write_text("guard")
    subprocess.run(["sh", "-c", vibe._LINK_HOME, "sh", str(src), str(dst), "config.toml", "hooks.toml"], check=True)
    assert (dst / "logs").is_symlink() and (dst / "AGENTS.md").is_symlink()
    assert not (dst / "config.toml").is_symlink() and (dst / "config.toml").read_text() == "guard"


def test_the_guards_never_sit_inside_the_workspace_mount(tmp_path, monkeypatch):
    # The host refuses a read-only mount that overlaps the workspace mount, and
    # a mount below a link blocks the link: they stand in the container's own
    # VIBE_HOME instead.
    assert not any(path.startswith("/workspace") for path in vibe.guard_files("c"))
    import gen
    monkeypatch.setattr(gen, "workspace_path", lambda: str(tmp_path))
    home = tmp_path / ".collrun" / "homes" / "h"
    home.mkdir(parents=True)
    assert vibe.container_home(str(home)) == "/workspace/.collrun/homes/h"


def test_what_the_agent_left_in_the_home_never_outlives_a_leg(tmp_path):
    (tmp_path / "hooks.toml").write_text('[[hooks]]\nname="x"\ntype="pre_tool"\ncommand="touch /x"\n')
    (tmp_path / ".env").write_text("VIBE_MCP_SERVERS=[]")
    (tmp_path / "trusted_folders.toml").write_text('trusted = ["/workspace"]')
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "evil.py").write_text("import os")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "p").mkdir()
    (tmp_path / "agents").symlink_to("/tmp")
    vibe._clear_profiles(str(tmp_path))
    for name in ("hooks.toml", ".env", "trusted_folders.toml"):
        assert (tmp_path / name).is_file() and (tmp_path / name).read_text() == "", name
    for name in ("tools", "plugins", "agents", "prompts"):
        assert not (tmp_path / name).exists() and not (tmp_path / name).is_symlink(), name


def test_a_hooks_toml_directory_in_the_home_is_replaced_by_an_empty_file(tmp_path):
    (tmp_path / "hooks.toml").mkdir()
    (tmp_path / "hooks.toml" / "x").write_text("y")
    vibe._clear_profiles(str(tmp_path))
    assert (tmp_path / "hooks.toml").is_file()


def _prepare(tmp_path, monkeypatch, leg, commands="deny", model="m", placed=None):
    import asyncio
    from types import SimpleNamespace
    from _agent_shared import cli_container

    placed = placed if placed is not None else []

    async def ensure_env(name, **kwargs):
        env = SimpleNamespace(container_id="c", exec=lambda argv, user=None: (0, ""), **kwargs)
        placed.append(env)
        return env

    monkeypatch.setattr(cli_container, "ensure_env", ensure_env)
    monkeypatch.setattr(cli_container, "_ENVS", {})
    import gen
    monkeypatch.setattr(vibe, "harness_home", lambda backend: str(tmp_path))
    monkeypatch.setattr(gen, "workspace_path", lambda: str(tmp_path.parent))
    ctx = {"provider": "openrouter", "native_provider": SimpleNamespace(id="openrouter"), "model": model,
           "commands": commands, "credentials": SimpleNamespace(key="k"), "websearch": False}
    return asyncio.run(vibe.prepare(ctx, leg=leg))


@pytest.mark.parametrize("leg", ["acp", "terminal"])
def test_prepare_places_the_guards_and_clears_the_home(tmp_path, monkeypatch, leg):
    (tmp_path / "hooks.toml").write_text("agent wrote this")
    prepared = _prepare(tmp_path, monkeypatch, leg)
    guards = prepared.env_obj.readonly_files
    root = "/home/coder/.vibe"
    assert guards[f"{root}/hooks.toml"] == "" and prepared.env_obj.volumes == {}
    assert prepared.env["VIBE_BYPASS_TOOL_PERMISSIONS"] == "false"
    assert (tmp_path / "hooks.toml").read_text() == ""
    assert prepared.commands.native["readonly_files"] == guards
    config = guards.get(f"{root}/config.toml")
    assert (config == (tmp_path / "config.toml").read_text()) is (leg == "acp")
    assert (config is None) is (leg == "terminal")


def test_a_container_placed_with_other_guards_is_retired(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from _agent_shared import cli_container

    closed = []
    stale = SimpleNamespace(_vibe_guards={"/x": "stale"}, close=lambda: closed.append(1))
    monkeypatch.setattr(cli_container, "cached_env", lambda name: stale)
    monkeypatch.setattr(cli_container, "close_env", lambda name, env: closed.append(env))
    _prepare(tmp_path, monkeypatch, "acp")
    assert closed == [stale]


@pytest.mark.parametrize("commands,value,allowed", [
    ("allow", "auto-approve", True), ("ask", "auto-approve", False), ("deny", "accept-edits", True)])
def test_the_auto_approve_mode_is_offered_only_under_allow(commands, value, allowed):
    from _agent_shared.acp_policy import mode_allowed
    assert mode_allowed("vibe", commands, value) is allowed
