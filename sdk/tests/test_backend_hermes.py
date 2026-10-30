# SPDX-License-Identifier: MIT
"""Hermes Agent's renderers and its session store reader."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


from _agent_shared.harnesses.hermes import backend as hermes  # noqa: E402


def test_a_builtin_provider_is_named_by_its_own_id():
    config = hermes.config_yaml(provider="openai", native="openai-api", model="gpt-6-astra")
    assert config["model"] == {"provider": "openai-api", "default": "gpt-6-astra"}
    assert "providers" not in config
    assert config["telemetry"]["shared_metrics"] == {"enabled": False, "send": False, "offer_version": 2}


def test_an_openai_compatible_provider_is_a_named_custom_provider_at_its_standard_endpoint():
    config = hermes.config_yaml(provider="groq", native="custom:groq", model="openai/gpt-oss-120b")
    assert config["model"] == {"provider": "custom:groq", "default": "openai/gpt-oss-120b"}
    assert config["providers"] == {"groq": {"api": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
                                            "transport": "chat_completions"}}


def test_the_key_and_the_endpoint_ride_the_process_environment():
    assert hermes.provider_env("anthropic", "anthropic", "sk") == {
        "ANTHROPIC_API_KEY": "sk", "ANTHROPIC_BASE_URL": "https://api.anthropic.com"}
    assert hermes.provider_env("mistral", "custom:mistral", "mk") == {"MISTRAL_API_KEY": "mk"}


def test_the_managed_scope_holds_the_commands_level_and_withholds_the_web():
    deny = hermes.managed_config("deny", websearch=False)
    assert set(deny["agent"]["disabled_toolsets"]) == {"browser", "terminal", "code_execution", "web", "cronjob", "delegation"}
    assert deny["approvals"]["mode"] == "manual"
    allow = hermes.managed_config("allow", websearch=True)
    assert allow["agent"]["disabled_toolsets"] == ["browser"] and allow["approvals"]["mode"] == "off"


def test_the_installs_link_points_at_the_image_and_is_idempotent(tmp_path):
    hermes._link_installs(str(tmp_path))
    hermes._link_installs(str(tmp_path))
    assert (tmp_path / "installs").readlink() == Path(hermes.INSTALLS)


def _store(home: Path) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
                "content TEXT, active INTEGER NOT NULL DEFAULT 1, timestamp REAL)")
    con.executemany("INSERT INTO sessions VALUES (?, 'local', 0)", [("local-old",), ("local-new",)])
    con.executemany("INSERT INTO messages (session_id, role, content, active, timestamp) VALUES (?, ?, ?, ?, 0)", [
        ("local-old", "user", "earlier conversation", 1),
        ("local-new", "user", "hello", 1), ("local-new", "tool", "tool output", 1),
        ("local-new", "assistant", "hi there", 1), ("local-new", "assistant", "superseded", 0)])
    con.commit()
    con.close()


def test_the_store_lists_sessions_and_reads_one_conversations_text(tmp_path):
    assert hermes.session_ids(str(tmp_path)) == frozenset()
    _store(tmp_path)
    assert hermes.session_ids(str(tmp_path)) == {"local-old", "local-new"}
    assert hermes.trace_from_state_db(str(tmp_path), "local-new") == [
        {"type": "user", "text": "hello"}, {"type": "text", "text": "hi there"}]
    messages = hermes.messages_from_state_db(str(tmp_path), "local-new")
    assert [(m.role, m.content) for m in messages] == [("user", "hello"), ("assistant", "hi there")]


def test_the_tail_follows_the_one_session_the_tty_opened(tmp_path):
    from _agent_shared.terminal_tail import _HermesTail
    _store(tmp_path)
    tail = _HermesTail(str(tmp_path), frozenset({"local-old"}))
    assert [(m.role, m.content) for m in tail.read()] == [("user", "hello"), ("assistant", "hi there")]
    assert _HermesTail(str(tmp_path), frozenset()).read() == []


# ── The agent-writable home: nothing in it may run a command below allow ──


def test_the_managed_scope_pins_every_feature_that_runs_a_file_the_agent_can_write():
    """The user config.yaml is the agent's to write: inline shell in skills,
    hook consent, plugins, a language server, a native SQLite extension,
    scheduled jobs and the command allowlist are pinned where it cannot reach."""
    for level in ("deny", "allow"):
        managed = hermes.managed_config(level, websearch=True)
        assert managed["skills"]["inline_shell"] is False
        assert managed["skills"]["external_dirs"] == [] and managed["skills"]["project_discovery"] is False
        assert managed["skills"]["trusted_project_dirs"] == []
        assert managed["hooks_auto_accept"] is False
        assert managed["plugins"] == {"enabled": [], "auto_apply": False, "allow_lazy_installs": False}
        assert managed["lsp"] == {"enabled": False}
        assert managed["sessions"] == {"cjk_fts": False}
        assert managed["cron"] == {"allow_agent_scheduling": False}
        assert managed["command_allowlist"] == []
        assert managed["mcp"] == {"auto_reload_on_config_change": False}


def test_below_allow_withholds_scheduled_jobs_and_subagents_with_the_command_toolsets():
    deny = hermes.managed_config("deny", websearch=True)["agent"]["disabled_toolsets"]
    assert {"terminal", "code_execution", "cronjob", "delegation"} <= set(deny)
    allow = hermes.managed_config("allow", websearch=True)["agent"]["disabled_toolsets"]
    assert not {"terminal", "code_execution", "cronjob", "delegation"} & set(allow)


def test_the_pins_are_not_shared_state_between_legs():
    first = hermes.managed_config("deny", websearch=False)
    first["skills"]["inline_shell"] = True
    first["plugins"]["enabled"].append("x")
    again = hermes.managed_config("deny", websearch=False)
    assert again["skills"]["inline_shell"] is False and again["plugins"]["enabled"] == []


def test_the_user_config_the_agent_reads_back_carries_none_of_the_pins_and_no_servers():
    config = hermes.config_yaml(provider="openai", native="openai-api", model="m")
    assert set(config) == {"telemetry", "updates", "security", "model"}


def _plant(home: Path) -> None:
    for relative in ("plugins/model-providers/x/__init__.py", "skills/probe/SKILL.md", "hooks/h/handler.py",
                     "cron/jobs.json", "scripts/s.sh", "lib/libfts5_cjk.so", "lsp/bin/server",
                     "skill-bundles/b/x", "plugin-data/p/x", "mcp-installs/m/x"):
        (home / relative).parent.mkdir(parents=True, exist_ok=True)
        (home / relative).write_text("x")
    for name in ("shell-hooks-allowlist.json", "relay-plugins.toml", "npmrc", ".env"):
        (home / name).write_text("x")


def test_each_leg_starts_from_a_home_with_no_plugins_skills_hooks_or_consent(tmp_path):
    _plant(tmp_path)
    (tmp_path / "state.db").write_text("session store")
    (tmp_path / "SOUL.md").write_text("persona")
    (tmp_path / "memories").mkdir()
    hermes.clear_home(str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["SOUL.md", "memories", "state.db"]
    hermes.clear_home(str(tmp_path))  # nothing to clear is not an error


def test_clearing_unlinks_a_planted_link_instead_of_following_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    home = tmp_path / "home"
    home.mkdir()
    (home / "plugins").symlink_to(outside)
    (home / "skills").symlink_to(outside / "keep")
    hermes.clear_home(str(home))
    assert not (home / "plugins").exists() and not os.path.lexists(home / "plugins")
    assert not os.path.lexists(home / "skills") and (outside / "keep").read_text() == "x"


def test_every_hermes_home_entry_that_is_loaded_as_code_is_cleared():
    assert {"plugins", "skills", "hooks", "cron", "scripts", "lib", "lsp", "shell-hooks-allowlist.json",
            ".env"} <= set(hermes.HOME_CLEARED)
    assert "state.db" not in hermes.HOME_CLEARED and "sessions" not in hermes.HOME_CLEARED


def test_prepare_clears_the_home_before_writing_the_config_and_roots_the_managed_scope(tmp_path, monkeypatch):
    """Per leg, in order: clear the home, write the clean user config, then the root-owned managed file."""
    import asyncio
    import types
    _plant(tmp_path)
    calls = []

    class Env:
        container_id = "c"

    async def ensure_env(name, **kw):
        calls.append(("ensure_env", sorted(p.name for p in tmp_path.iterdir())))
        return Env()

    import _agent_shared.cli_container as cc
    monkeypatch.setattr(cc, "ensure_env", ensure_env)
    monkeypatch.setattr(cc, "write_root_file",
                        lambda env, path, content, what: calls.append(("root", path, content)))
    monkeypatch.setattr(hermes, "harness_home", lambda backend: str(tmp_path))
    ctx = {"provider": "openai", "native_provider": types.SimpleNamespace(id="openai-api"), "model": "m",
           "commands": "deny", "websearch": False, "credentials": types.SimpleNamespace(key="k")}
    prepared = asyncio.run(hermes.prepare(ctx))
    assert calls[0] == ("ensure_env", ["config.yaml", "installs"])
    kind, path, content = calls[1]
    import yaml
    assert (kind, path) == ("root", hermes.MANAGED_CONFIG)
    assert yaml.safe_load(content) == prepared.commands.native
    assert prepared.commands.native["skills"]["inline_shell"] is False
    user = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "mcp_servers" not in user and "hooks" not in user and user["model"]["default"] == "m"


def test_the_terminal_leg_binds_the_bridge_in_the_managed_scope_not_the_user_config():
    managed = hermes.managed_config("deny", websearch=False)
    bound = hermes.terminal_managed_config(managed, "http://bridge/mcp")
    assert bound["mcp_servers"] == {"collimate": {"url": "http://bridge/mcp"}}
    assert bound["skills"] == managed["skills"] and "mcp_servers" not in managed
