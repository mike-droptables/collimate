"""D06: grants determine placement and impossible runs stop before effects."""
import asyncio

import pytest

import _agent_shared as eng
from _agent_shared import agent_config as shared_config  # noqa: E402
from collimate import host_harness
from collimate.permissions import Grant

from tests._workspace import GENERATORS


def grant(docker="full", internet="full", commands="deny", allow=()):
    return Grant(docker=docker, internet=internet, commands=commands, internet_allow=allow)


def found(backend):
    from collimate.account_taxonomy import HARNESS_HOST_BINARY
    binary = HARNESS_HOST_BINARY[backend]
    return host_harness.HostHarness(backend, binary, f"/usr/local/bin/{binary}", "9.0.0", False, ("/usr/local/bin",))


@pytest.mark.parametrize("backend", list(shared_config._BACKENDS))
@pytest.mark.parametrize("docker", ["none", "limited", "full"])
def test_grant_selects_actual_backend_placement__D06(monkeypatch, backend, docker):
    monkeypatch.setattr(host_harness, "probe", lambda name: found(name))
    # limited places no harness (the host raises an Annotation run's grant
    # before this); none places the host-locus adapters and the host twins.
    refuses = docker == "limited" or (
        docker == "none" and backend not in {"pydantic-ai", "claude-code", "codex", "goose", "opencode"})
    if refuses:
        process = "docker: limited" if docker == "limited" and backend in {"pydantic-ai", "claude-code"} else "docker: full"
        with pytest.raises(ValueError, match=process):
            eng.parse_agent_config({"surface": "annotation", "backend": backend}, grant=grant(docker), can_ask=True)
    else:
        cfg = eng.parse_agent_config({"surface": "annotation", "backend": backend}, grant=grant(docker), can_ask=True)
        host = backend == "claude-code" or (docker == "none" and backend != "pydantic-ai")
        assert cfg["runtime"] == ("standard" if host else "docker")
        assert bool(cfg["host_harness"]) is host


@pytest.mark.parametrize("key", ["runtime", "websearch"])
def test_removed_settings_are_refused__D06(key):
    with pytest.raises(ValueError, match="unknown config key"):
        eng.parse_agent_config({"surface": "annotation", key: "docker"}, grant=grant(), can_ask=True)


@pytest.mark.parametrize("surface,config,permission,answer,reason", [
    ("terminal", {"backend": "codex"}, grant("none"), True, "terminal surface needs docker: full"),
    ("annotation", {}, grant("limited", commands="allow"), True, "docker: limited"),
    ("annotation", {"backend": "codex", "mcp": "http_stdio"}, grant("none"), True, "http_stdio"),
    ("chat", {"approvals": "ask"}, grant(), False, "approvals: ask"),
    ("chat", {}, grant(commands="ask"), False, "commands: ask"),
])
def test_impossible_grants_refuse_without_degrading__D06(monkeypatch, surface, config, permission, answer, reason):
    monkeypatch.setattr(host_harness, "probe", lambda name: found(name))
    with pytest.raises(ValueError, match=reason):
        eng.parse_agent_config({**config, "surface": surface}, grant=permission, can_ask=answer)


def account(endpoint):
    return {"service": "openai", "protocol": "openai", "model": "local-model", "endpoint": endpoint}


@pytest.mark.parametrize("endpoint,permission,allowed", [
    ("https://api.example.com/v1", grant(internet="none"), False),
    ("https://api.example.com/v1", grant(internet="proxy", allow=(" .EXAMPLE.com. ",)), True),
    ("https://notexample.com/v1", grant(internet="proxy", allow=("example.com",)), False),
    ("https://api.example.com/v1", grant(internet="proxy"), False),
    ("http://localhost:1234/v1", grant(internet="none"), True),
    ("http://127.0.0.2:1234/v1", grant(internet="none"), True),
    ("http://[::1]:1234/v1", grant(internet="none"), True),
    ("http://host.docker.internal:1234/v1", grant(internet="none"), True),
    ("http://localhost.evil/v1", grant(internet="none"), False),
    ("http://169.254.169.254/v1", grant(internet="none"), False),
    ("http://10.0.0.5/v1", grant(internet="none"), False),
    ("http://localhost:x@169.254.169.254/v1", grant(), False),
    ("http://localhost:bad/v1", grant(), False),
    ("file:///localhost/model", grant(), False),
])
def test_model_endpoint_is_checked_before_start__D06(endpoint, permission, allowed):
    config = {"provider": "openai", "_account": account(endpoint)}
    if not allowed:
        with pytest.raises(ValueError, match="endpoint|internet"):
            eng.parse_agent_config({**config, "surface": "annotation"}, grant=permission, can_ask=True)
    else:
        cfg = eng.parse_agent_config({**config, "surface": "annotation"}, grant=permission, can_ask=True)
        assert cfg["websearch"] is (permission.internet != "none")


@pytest.mark.parametrize("internet", ["none", "proxy", "full"])
def test_opaque_subscription_has_no_invented_endpoint__D06(internet):
    config = {"backend": "codex", "auth": "chatgpt"}
    permission = grant(internet=internet, allow=("openai.com",))
    if internet == "full":
        assert eng.parse_agent_config({**config, "surface": "annotation"}, grant=permission, can_ask=True)["auth"] == "chatgpt"
    else:
        with pytest.raises(ValueError, match="internet"):
            eng.parse_agent_config({**config, "surface": "annotation"}, grant=permission, can_ask=True)


@pytest.mark.parametrize("internet", ["none", "proxy", "full"])
def test_saved_subscription_catalog_endpoint_is_not_transport_evidence__D06(internet):
    from _agent_shared.accounts import model_config
    snapshot = {
        "kind": "model", "service": "openai", "protocol": "openai",
        "model": "gpt-5", "auth_type": "cli",
        "metadata": {"harness": "codex", "plan": "chatgpt"},
        "endpoint": "https://api.openai.com/v1",
    }
    config = model_config({}, snapshot)
    assert config["auth"] == "chatgpt"
    permission = grant(internet=internet, allow=("openai.com",))
    if internet == "full":
        assert eng.parse_agent_config({**config, "surface": "annotation"}, grant=permission, can_ask=True)["auth"] == "chatgpt"
    else:
        with pytest.raises(ValueError, match="host cannot be verified.*internet: full"):
            eng.parse_agent_config({**config, "surface": "annotation"}, grant=permission, can_ask=True)


_CLAUDE_KEY = {"service": "anthropic", "protocol": "anthropic", "model": "",
               "metadata": {"harness": "claude-code"}}
_CODEX_KEY = {"service": "openai", "protocol": "openai-responses", "model": "",
              "metadata": {"harness": "codex"}}


@pytest.mark.parametrize("surface,model,permission,reason", [
    ("annotation", account("https://api.example.com/v1"), grant(internet="none"), "internet"),
    ("chat", account("https://api.example.com/v1"), grant(internet="none"), "internet"),
    # A CLI in its container needs docker: full; one on this machine refuses deny.
    ("terminal", _CODEX_KEY, grant(docker="none"), "terminal surface needs docker: full"),
    ("terminal", _CLAUDE_KEY, grant(docker="none"), "cannot run its CLI with commands: deny"),
    ("terminal", _CLAUDE_KEY, grant(docker="limited", commands="ask"), "cannot run its CLI with docker: limited"),
])
def test_entrypoint_refuses_before_any_workspace_or_session_effect__D06(
        host_free_run, monkeypatch, surface, model, permission, reason):
    """The Agent generator parses its config — through its own entrypoint,
    the configured surface and its schema — before it stages a skill or
    opens a session."""
    import importlib.util
    from _agent_shared import accounts, session
    entry = GENERATORS / "agent/agent/agent.py"
    spec = importlib.util.spec_from_file_location(f"grant_{surface}_entry", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(accounts, "model_account", lambda: model)
    monkeypatch.setattr(session.gen, "config", lambda _: {"surface": surface})
    monkeypatch.setattr(session.gen, "grant", lambda: permission)
    monkeypatch.setattr(session, "stage_builtin_skills", lambda *a, **k: pytest.fail("staged before refusal"))
    monkeypatch.setattr(session.entry, "run_session", lambda *a, **k: pytest.fail("session before refusal"))
    with pytest.raises(ValueError, match=reason):
        asyncio.run(module.session.run(here=str(entry.parent)))


@pytest.mark.parametrize("backend,docker,placement,commands,mcp", [
    ("claude-code", "limited", "docker", "deny", "off"),
    ("codex", "full", "standard", "deny", "off"),
    ("pi", "none", "standard", "deny", "off"),
    ("codex", "none", "standard", "deny", "off"),
    ("pydantic-ai", "limited", "docker", "allow", "off"),
    ("claude-code", "full", "docker", "deny", "http_stdio"),
])
def test_direct_turn_cannot_bypass_placement_grant__D06(native_bridge, monkeypatch, backend, docker, placement, commands, mcp):
    from _agent_shared import acp_run
    native_bridge.permissions = {"docker": docker, "internet": "full", "commands": commands}
    monkeypatch.setattr(eng, "discover_cell_caps", lambda: pytest.fail("discovery before refusal"))
    monkeypatch.setattr(acp_run, "run_acp_turn", lambda **_: pytest.fail("model before refusal"))
    with pytest.raises(RuntimeError, match="docker|container|CLI"):
        asyncio.run(eng.run_agent(prompt="x", backend=backend, runtime=placement, commands=commands, cell_mcp=mcp))
