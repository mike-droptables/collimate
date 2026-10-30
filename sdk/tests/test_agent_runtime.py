"""Grant-derived host runtime and actual ACP spawn/credential boundaries."""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from collimate.permissions import Grant

from _agent_shared import harness_homes
import _agent_shared as eng  # noqa: E402
from _agent_shared import host_adapters
from _agent_shared import acp_client as ac
from _agent_shared import acp_run
from _agent_shared.harnesses.goose import backend as gb  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from collimate import host_harness
from collimate.account_taxonomy import (
    HARNESS_HOST_BINARY,
    HARNESS_STANDARD_RUNTIME,
    MODEL_HARNESS_RULES,
)


def _found(harness="goose", path="/usr/local/bin/goose", version="1.51.0", snap=False):
    return host_harness.HostHarness(harness, HARNESS_HOST_BINARY[harness], path, version, snap, ("/usr/local/bin",))


def test_the_standard_set_is_exactly_the_engine_host_twins_plus_claude():
    from _agent_shared.harnesses import HARNESS
    twins = {name for name, row in HARNESS.items() if row.host_twin}
    assert set(HARNESS_STANDARD_RUNTIME) == twins | {"claude-code"}
    for name in twins:
        assert eng.backend_spec(name).containerized


@pytest.mark.parametrize("backend", ["goose", "opencode", "codex"])
def test_no_docker_selects_usable_host_twin__D06(monkeypatch, backend):
    monkeypatch.setattr(host_harness, "probe", lambda h, **kw: _found(h, f"/usr/local/bin/{HARNESS_HOST_BINARY[h]}"))
    cfg = eng.parse_agent_config({"surface": "annotation", "backend": backend}, grant=Grant(docker="none", internet="full"), can_ask=True)
    assert cfg["runtime"] == "standard" and cfg["host_harness"]["binary"] == HARNESS_HOST_BINARY[backend]
    assert not eng.run_needs_container(backend, "chat", "standard")
    assert eng.run_needs_container(backend, "terminal", "standard")


@pytest.mark.parametrize("found", [None, _found("codex", "/snap/bin/codex", "0.114.0", snap=True)])
def test_missing_or_confined_host_twin_refuses__D06(monkeypatch, found):
    monkeypatch.setattr(host_harness, "probe", lambda h, **kw: found)
    with pytest.raises(ValueError, match="outside Snap"):
        eng.parse_agent_config({"surface": "annotation", "backend": "codex"}, grant=Grant(docker="none", internet="full"), can_ask=True)


def test_nested_harness_does_not_probe_host__D06(monkeypatch):
    monkeypatch.setattr(host_harness, "probe", lambda *a: pytest.fail("unexpected host probe"))
    cfg = eng.parse_agent_config({"surface": "terminal", "backend": "goose"}, grant=Grant(docker="full", internet="full"), can_ask=True)
    assert cfg["runtime"] == "docker" and cfg["surface"] == "terminal"


@pytest.mark.parametrize("docker", ["none", "full"])
def test_claude_grant_selects_machine_cli__D06(monkeypatch, docker):
    monkeypatch.setattr(host_harness, "probe", lambda *a: _found("claude-code", "/local/claude", "2.1.281"))
    cfg = eng.parse_agent_config({"surface": "annotation", "backend": "claude-code"}, grant=Grant(docker=docker, internet="full"), can_ask=True)
    assert cfg["runtime"] == "standard" and cfg["host_harness"]["path"] == "/local/claude"
    assert any("/local/claude" in w for w in cfg["warnings"])


@pytest.mark.parametrize("found", [None, _found("claude-code", "/snap/bin/claude", "2.1.200", snap=True)])
def test_claude_without_usable_cli_uses_logged_bundled_choice__D06(monkeypatch, found):
    monkeypatch.setattr(host_harness, "probe", lambda *a: found)
    cfg = eng.parse_agent_config({"surface": "annotation", "backend": "claude-code"}, grant=Grant(docker="none", internet="full"), can_ask=True)
    assert cfg["runtime"] == "docker" and cfg["host_harness"] is None
    assert any("bundled" in w for w in cfg["warnings"])
    assert not eng.run_needs_container("claude-code", "chat", cfg["runtime"])





class _Reached(Exception):
    """The containerized turn got past run_agent's gate."""


@pytest.mark.parametrize("grant, runtime, refused", [
    ({"docker": "none"}, "docker", True),
    ({"docker": "limited"}, "docker", True),
    ({"docker": "full"}, "docker", False),
    # The machine's own CLI needs no container, whatever the grant.
    ({"docker": "none"}, "standard", False),
])
def test_run_agent_refuses_a_containerized_turn_the_grant_forbids(
        native_bridge, monkeypatch, grant, runtime, refused):
    """Behavioural, not a source check: a docker-placed turn under a grant
    below docker: full is refused with container_refusal's own message
    BEFORE the harness turn starts (no container is ever placed)."""
    native_bridge.permissions = {**grant, "internet": "full"}

    async def turn(**_):
        raise _Reached()
    monkeypatch.setattr(acp_run, "run_acp_turn", turn)
    run = eng.run_agent(prompt="x", backend="codex", runtime=runtime,
                        host_harness={"path": "/fixture/codex"} if runtime == "standard" else None)
    if refused:
        with pytest.raises(RuntimeError) as err:
            asyncio.run(run)
        assert str(err.value) == eng.container_refusal("codex")
    else:
        with pytest.raises(_Reached):
            asyncio.run(run)


def test_the_turn_gate_names_only_fixes_that_can_work():
    assert "outside Snap" in eng.container_refusal("codex")
    for backend in ("pi", "antigravity", "qwen-code", "cline"):
        msg = eng.container_refusal(backend)
        assert "docker: full" in msg and "docker: none" not in msg


@pytest.mark.parametrize("runtime,fix,not_fix", [
    ("docker", "install a usable `claude` on PATH", "choose runtime"),
    ("standard", "update that copy", "bundled"),
])
def test_a_claude_version_floor_failure_names_the_real_fix(runtime, fix, not_fix):
    raw = "API Error: 400 Claude Code 2.1.232 does not support this model; version 2.1.280 or newer is required."
    text = eng.explain_turn_failure(raw, backend="claude-code", runtime=runtime, cli_path="/local/claude")
    assert fix in text and not_fix not in text
    assert eng.explain_turn_failure("boom", backend="claude-code", runtime=runtime) == "boom"


@pytest.mark.parametrize("internet, fix", [
    ("none", "internet: none, so the agent cannot reach its model's endpoint"),
    ("proxy", "add the model endpoint's host to internet_allow"),
])
def test_a_model_the_grant_cannot_reach_names_the_internet_axis(internet, fix):
    # The egress proxy's refused CONNECT surfaces as the client's bare
    # "Connection error." — the user must read which grant axis caused it.
    text = eng.explain_turn_failure("Connection error.", backend="pydantic-ai",
                                    runtime="docker", internet=internet)
    assert text.startswith("Connection error.") and fix in text
    # Full internet (or no grant known) leaves a real outage untouched, and a
    # restricted grant never annotates an unrelated failure.
    assert eng.explain_turn_failure("Connection error.", backend="pydantic-ai",
                                    runtime="docker", internet="full") == "Connection error."
    assert eng.explain_turn_failure("Connection error.", backend="pydantic-ai",
                                    runtime="docker") == "Connection error."
    assert eng.explain_turn_failure("boom", backend="pydantic-ai", runtime="docker",
                                    internet=internet) == "boom"


def _twin_ctx(backend, **over):
    ctx = {"auth": "api_key", "provider": "openai", "model": "gpt-5", "system_prompt": "be brief",
           "enable_file_tools": True, "websearch": True,
           "runtime": "standard", "commands": "deny",
           "host_harness": {"path": f"/usr/local/bin/{HARNESS_HOST_BINARY[backend]}", "version": "1.2.3",
                            "binary": HARNESS_HOST_BINARY[backend]}}
    ctx.update(over)
    return ctx


@pytest.fixture
def host_seams(monkeypatch, tmp_path):
    """Everything a twin touches outside its own argv/env: the key, the
    per-cell home, the workspace, and the spawn itself (captured, not run)."""
    spawned = []

    async def _key(provider, **kw):
        return "sk-private-key"

    async def _spawn(backend, argv, *, env, cwd):
        spawned.append({"backend": backend, "argv": argv, "env": env, "cwd": cwd})
        return object()

    home = tmp_path / "home"
    from _agent_shared.harnesses.opencode import backend as oc
    monkeypatch.setattr(_pipeline, "account_api_key", _key)
    monkeypatch.setattr(oc, "harness_home", lambda backend: str(home / backend))
    monkeypatch.setattr(oc, "spawn_host_adapter", _spawn)
    from _agent_shared.harnesses.codex import backend as cb
    monkeypatch.setattr(cb, "spawn_host_adapter", _spawn)
    monkeypatch.setattr(gb, "spawn_host_adapter", _spawn)
    monkeypatch.setattr(harness_homes, "harness_home", lambda backend: str(home / backend))
    from _agent_shared.harnesses.claude_code import backend as ct
    monkeypatch.setattr(ct, "spawn_host_adapter", _spawn)
    monkeypatch.setattr(host_adapters, "host_node_bin_dir", lambda: "")
    monkeypatch.setattr(acp_run.gen, "workspace_path", lambda: str(tmp_path / "ws"), raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    # A key in the developer's own shell must not decide a "no key" assertion.
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return spawned, home


def test_goose_twin_spawns_the_machines_goose_with_relocated_xdg_dirs(host_seams):
    spawned, home = host_seams
    profile = asyncio.run(acp_run._PROFILES["goose"](_twin_ctx("goose")))
    assert profile.locus == "host" and profile.bind_host == "127.0.0.1"
    asyncio.run(profile.spawn())
    call = spawned[0]
    assert call["argv"] == ["/usr/local/bin/goose", "acp"]
    assert call["env"]["XDG_DATA_HOME"] == str(home / "goose" / "share")
    assert call["env"]["GOOSE_MODEL"] == "gpt-5" and call["env"]["PATH"] == "/usr/bin"
    assert (home / "goose" / "config" / "goose" / ".goosehints").read_text().strip() == "be brief"
    assert not any("install" in v or "update" in v for v in call["argv"])


def test_opencode_twin_writes_its_config_and_links_a_plan_login(host_seams, monkeypatch):
    import json
    spawned, home = host_seams
    lease = home / "lease" / "auth.json"
    lease.parent.mkdir(parents=True)
    lease.write_text('{"openai": {"type": "oauth"}}')
    monkeypatch.setattr(_pipeline, "selected_login_ready", lambda harness, plan: True)
    monkeypatch.setattr(_pipeline, "login_path", lambda harness: str(lease))
    monkeypatch.setattr("_agent_shared.harnesses.opencode.backend.shared_login_path", lambda: str(lease))
    profile = asyncio.run(acp_run._PROFILES["opencode"](_twin_ctx("opencode", auth="chatgpt")))
    asyncio.run(profile.spawn())
    call = spawned[0]
    assert call["argv"] == ["/usr/local/bin/opencode", "acp"]
    assert "OPENAI_API_KEY" not in call["env"], "a plan run carries no key beside the login"
    config = json.loads((home / "opencode" / "config" / "opencode" / "opencode.json").read_text())
    assert config["model"] == "openai/gpt-5" and profile.model_value == "openai/gpt-5"
    link = home / "opencode" / "data" / "opencode" / "auth.json"
    assert os.path.islink(link) and os.readlink(link) == str(lease)


@pytest.mark.parametrize("backend, store", [
    ("goose", ("goose", "sessions", "sessions.db")),
    ("opencode", ("opencode", "opencode.db")),
    ("opencode", ("opencode", "storage", "session", "ses_1.json")),
])
def test_a_twins_session_store_survives_the_run(host_seams, native_bridge, monkeypatch,
                                                tmp_path, backend, store):
    """F33: the twin relocates XDG_DATA_HOME inside the per-cell home, so its
    session store must be among what close_harness_homes persists — or the
    next run's session/load finds nothing and silently starts over."""
    from _agent_shared.harnesses.opencode import backend as oc
    monkeypatch.setattr(oc, "harness_home", harness_homes.harness_home)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    native_bridge.cell_id = "cell-f33"
    os.makedirs(ac.gen.workspace_path(), exist_ok=True)
    monkeypatch.setattr(harness_homes, "_HARNESS_HOMES", {})
    spawned, _ = host_seams
    profile = asyncio.run(acp_run._PROFILES[backend](_twin_ctx(backend)))
    asyncio.run(profile.spawn())
    data = spawned[0]["env"]["XDG_DATA_HOME"]
    path = os.path.join(data, *store)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("session")
    harness_homes.close_harness_homes()
    home = harness_homes.harness_home(backend)          # the next run's fresh temp home
    restored = os.path.join(home, os.path.relpath(path, os.path.dirname(data)))
    try:
        assert open(restored).read() == "session"
    finally:
        harness_homes.close_harness_homes()


def test_codex_twin_drives_the_machines_codex_through_collimates_own_adapter(host_seams, monkeypatch):
    spawned, home = host_seams
    monkeypatch.setattr(host_adapters, "resolve_adapter_version", lambda name: "1.11.0")
    monkeypatch.setattr(host_adapters, "ensure_adapter", lambda name, version: f"/cache/acp-adapters/{name}/{version}/node_modules/.bin/codex-acp")
    profile = asyncio.run(acp_run._PROFILES["codex"](_twin_ctx("codex")))
    asyncio.run(profile.spawn())
    call = spawned[0]
    assert call["argv"] == ["/cache/acp-adapters/codex-acp/1.11.0/node_modules/.bin/codex-acp"]
    assert call["env"]["CODEX_PATH"] == "/usr/local/bin/codex" and call["env"]["CODEX_HOME"] == str(home / "codex")
    assert not any(k.endswith("API_KEY") for k in call["env"])
    assert json.loads((home / "codex" / "auth.json").read_text())["OPENAI_API_KEY"] == "sk-private-key"
    assert (home / "codex" / "AGENTS.md").read_text().strip() == "be brief"
    assert "model = \"gpt-5\"" in (home / "codex" / "config.toml").read_text()
    assert profile.adapter_version == "1.11.0"


@pytest.mark.parametrize("provider", MODEL_HARNESS_RULES["codex"]["providers"])
def test_codex_twin_uses_only_selected_native_credentials__F1547(host_seams, monkeypatch, provider):
    spawned, home = host_seams
    monkeypatch.setattr(host_adapters, "resolve_adapter_version", lambda *args: "1.11.0")
    monkeypatch.setattr(host_adapters, "ensure_adapter", lambda *args: "/cache/codex-acp")
    profile = asyncio.run(acp_run._PROFILES["codex"](_twin_ctx("codex", provider=provider)))
    asyncio.run(profile.spawn())
    assert "sk-private-key" not in json.dumps(spawned[0]["env"])
    if provider == 'openai':
        assert json.loads((home/'codex'/'auth.json').read_text()) == {'OPENAI_API_KEY':'sk-private-key'}
    else:
        import tomllib
        config = tomllib.loads((home/'codex'/'config.toml').read_text())
        assert config['model_providers']['collimate']['experimental_bearer_token'] == 'sk-private-key'
        assert 'env_key' not in config['model_providers']['collimate']


def test_host_adapters_are_a_closed_list_installed_into_collimates_prefix(monkeypatch, tmp_path, host_free_run):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from _agent_shared.adapter_artifacts import ADAPTERS
    assert {name for name, artifact in ADAPTERS.items() if artifact.host_install} == {"claude-agent-acp", "codex-acp"}
    assert host_adapters.adapter_binary("codex-acp", "1.11.0") == str(tmp_path / "collimate" / "acp-adapters" / "codex-acp" / "1.11.0" / "node_modules" / ".bin" / "codex-acp")
    assert host_adapters.claude_adapter_binary("0.70.0").endswith("/acp-adapters/claude-agent-acp/0.70.0/node_modules/.bin/claude-agent-acp")
    with pytest.raises(KeyError):
        host_adapters.adapter_binary("codex", "0.156.0")   # the harness itself is never installed
    from _env_shared import upstream

    def unreachable(source, github_token=None):
        raise upstream.UpstreamError("registry unreachable")

    monkeypatch.setattr(upstream, "latest_version", unreachable)
    # A registry this machine never reached → the module default pin.
    assert host_adapters.resolve_adapter_version("codex-acp") == ADAPTERS["codex-acp"].fallback_version


def test_a_bound_account_never_yields_another_providers_key(monkeypatch):
    """The provider check is what keeps a key from reaching another vendor's
    endpoint (claude's profile asks for "anthropic" whatever is bound)."""
    from _agent_shared import accounts
    monkeypatch.setattr(accounts.gen, "account", lambda role: {
        "kind": "model", "service": "openai", "protocol": "openai-responses", "model": "gpt-5",
        "auth_type": "api_key", "secrets": {"api_key": "sk-bound"}}, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(PermissionError, match="does not grant"):
        asyncio.run(accounts.account_api_key("openrouter"))
    assert "OPENROUTER_API_KEY" not in os.environ
    assert asyncio.run(accounts.account_api_key("openai")) == "sk-bound"


def test_codex_twin_on_a_chatgpt_plan_carries_no_key_beside_the_login(host_seams, monkeypatch, tmp_path):
    spawned, home = host_seams
    lease = tmp_path / "lease" / "auth.json"
    lease.parent.mkdir(parents=True)
    lease.write_text("{}")
    monkeypatch.setattr(_pipeline, "selected_login_ready", lambda harness, plan: True)
    monkeypatch.setattr(_pipeline, "login_path", lambda harness: str(lease))
    monkeypatch.setattr("_agent_shared.harnesses.codex.backend.shared_login_path", lambda: str(lease))
    monkeypatch.setattr(host_adapters, "resolve_adapter_version", lambda name: "1.11.0")
    monkeypatch.setattr(host_adapters, "ensure_adapter", lambda name, version: "/cache/codex-acp")
    profile = asyncio.run(acp_run._PROFILES["codex"](_twin_ctx("codex", auth="chatgpt")))
    asyncio.run(profile.spawn())
    env = spawned[0]["env"]
    assert not [k for k in env if k.endswith("_API_KEY")], "a plan run carries no key beside the login"
    link = home / "codex" / "auth.json"
    assert os.path.islink(link) and os.readlink(link) == str(lease)


@pytest.mark.parametrize("backend", ["codex", "goose", "opencode"])
def test_a_host_twin_env_replaces_the_generators_own(host_seams, monkeypatch, backend):
    """The twin's env is built from scratch: the generator's IPC capability
    token, its grant and any ambient vendor key never reach the machine's
    harness."""
    spawned, _home = host_seams
    monkeypatch.setenv("COLLIMATE_IPC_TOKEN", "SENTINEL-IPC")
    monkeypatch.setenv("COLLIMATE_PERMISSIONS", "SENTINEL-GRANT")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SENTINEL-KEY")
    monkeypatch.setattr(host_adapters, "resolve_adapter_version", lambda name: "1.11.0")
    monkeypatch.setattr(host_adapters, "ensure_adapter", lambda name, version: "/cache/codex-acp")
    profile = asyncio.run(acp_run._PROFILES[backend](_twin_ctx(backend)))
    asyncio.run(profile.spawn())
    env = spawned[0]["env"]
    assert not any(v in ("SENTINEL-IPC", "SENTINEL-GRANT", "SENTINEL-KEY") for v in env.values())
    assert not [k for k in env if k.startswith("COLLIMATE_")]


# ── the commands grant: codex and goose ───────────────────────────────

_GRANT = {"docker": "full", "internet": "full"}


@pytest.mark.parametrize("runtime", ["standard", "docker"])
def test_codex_ask_stays_ask_and_says_what_it_means(monkeypatch, runtime):
    """codex's chat asks under its own approval model — a command inside its
    sandbox runs unasked — on either runtime, and the run says so. On this
    machine no requirements lock keeps unified exec off, so input typed into
    an approved command is not carded either, and the run says that too."""
    monkeypatch.setattr(host_harness, "probe", lambda h, **kw: _found("codex", "/usr/local/bin/codex", "0.156.1"))
    cfg = eng.parse_agent_config({"surface": "annotation", "backend": "codex"},
                                 grant=Grant.from_dict({**_GRANT, "docker": "none" if runtime == "standard" else "full", "commands": "ask"}), can_ask=True)
    assert cfg["runtime"] == runtime and cfg["commands"] == "ask"
    assert any("codex's own approval model" in w for w in cfg["warnings"])
    unified = [w for w in cfg["warnings"] if "unified exec" in w]
    assert bool(unified) is (runtime == "standard")
    for level in ("deny", "allow"):
        cfg = eng.parse_agent_config({"surface": "annotation", "backend": "codex"},
                                     grant=Grant.from_dict({**_GRANT, "docker": "none" if runtime == "standard" else "full", "commands": level}), can_ask=True)
        assert cfg["commands"] == level
        assert not any("approval model" in w for w in cfg["warnings"])


@pytest.mark.parametrize("commands", ["deny", "ask"])
def test_standard_goose_older_than_the_verified_floor_is_refused(monkeypatch, commands):
    monkeypatch.setattr(host_harness, "probe", lambda h, **kw: _found("goose", version="1.49.0"))
    with pytest.raises(ValueError, match="needs goose 1.50.0 or newer"):
        eng.parse_agent_config({"surface": "annotation", "backend": "goose"},
                               grant=Grant.from_dict({**_GRANT, "docker": "none", "commands": commands}), can_ask=True)
    cfg = eng.parse_agent_config({"surface": "annotation", "backend": "goose"},
                                 grant=Grant.from_dict({**_GRANT, "docker": "none", "commands": "allow"}), can_ask=True)
    assert cfg["commands"] == "allow"
    monkeypatch.setattr(host_harness, "probe", lambda h, **kw: _found("goose", version="1.50.0"))
    cfg = eng.parse_agent_config({"surface": "annotation", "backend": "goose"},
                                 grant=Grant.from_dict({**_GRANT, "docker": "none", "commands": commands}), can_ask=True)
    assert cfg["commands"] == commands and cfg["runtime"] == "standard"


@pytest.mark.parametrize("backend", ["codex", "goose"])
@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_the_terminal_surface_is_offered_at_every_level(backend, commands):
    cfg = eng.parse_agent_config({"surface": "terminal", "backend": backend},
                                 grant=Grant.from_dict({**_GRANT, "commands": commands}), can_ask=True)
    assert cfg["surface"] == "terminal"
    assert not any("surface: terminal" in w for w in cfg["warnings"])


@pytest.mark.parametrize("commands, mode, features", [
    ("allow", "agent-full-access", None),
    ("deny", "read-only", {"shell_tool": False}),
    ("ask", "read-only", None),
])
def test_codex_twin_env_per_level(host_seams, monkeypatch, commands, mode, features):
    import json
    spawned, home = host_seams
    monkeypatch.setattr(host_adapters, "resolve_adapter_version", lambda name: "1.11.0")
    monkeypatch.setattr(host_adapters, "ensure_adapter", lambda name, version: "/cache/codex-acp")
    rules = home / "codex" / "rules"
    rules.mkdir(parents=True)
    (rules / "default.rules").write_text("allow everything")
    profile = asyncio.run(acp_run._PROFILES["codex"](_twin_ctx("codex", commands=commands)))
    asyncio.run(profile.spawn())
    env = spawned[0]["env"]
    assert spawned[0]["argv"] == ["/cache/codex-acp"]
    assert env["INITIAL_AGENT_MODE"] == mode
    assert not rules.exists(), "an 'always' rule never outlives the leg"
    config = json.loads(env["CODEX_CONFIG"])
    assert config.pop("bypass_hook_trust") is True
    assert config["features"] == {**(features or {}), "shell_snapshot":False}
    assert config["allow_login_shell"] is False
    assert config["shell_environment_policy"]["inherit"] == "none"


@pytest.mark.parametrize("commands, mode, ask_before, shell", [
    ("allow", "auto", [], None),
    ("ask", "approve", ["shell"], True),
    ("deny", "auto", [], False),
])
def test_goose_twin_per_level(host_seams, commands, mode, ask_before, shell):
    import json
    import yaml
    spawned, home = host_seams
    perm = home / "goose" / "config" / "goose" / "permission.yaml"
    perm.parent.mkdir(parents=True)
    perm.write_text("user:\n  always_allow: [shell]\n  ask_before: []\n  never_allow: []\n")
    profile = asyncio.run(acp_run._PROFILES["goose"](_twin_ctx("goose", commands=commands)))
    asyncio.run(profile.spawn())
    env = spawned[0]["env"]
    assert env["GOOSE_MODE"] == mode
    assert profile.session_mode == mode
    assert profile.session_mode_required is (commands == "ask")
    assert profile.verify_loaded is gb.verify_loaded
    assert yaml.safe_load(perm.read_text())["user"] == {
        "always_allow": [], "ask_before": ask_before, "never_allow": []}
    if shell is None:
        assert "EXTENSIONS" not in env
    else:
        tools = json.loads(env["EXTENSIONS"])["developer"]["available_tools"]
        assert ("shell" in tools) is shell
