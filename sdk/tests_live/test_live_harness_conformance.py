# SPDX-License-Identifier: MIT
"""LIVE harness conformance against a scripted provider: one contract, every
harness in the registry, no account billed.

Each harness runs its REAL adapter — image built from its folder, adapter
spawned, ``initialize`` and ``session/new`` over the real transport, the
normalizer, the bridge — against :mod:`provider_mock`, which the harness
reaches at its provider's registry endpoint (``collimate.providers``): the
test points that one endpoint at the mock, so a harness that builds its
provider URL any other way fails here instead of in a user's run. The mock
serves only under the registry endpoint's own path, so a wrong URL shape is
a miss too. Rows, per harness:

* **initialize** — the capabilities the client probes agree with the
  vendored ACP protocol matrix (``tests/data/acp_registry``) for the
  harness's registry id: protocol version 1, and ``loadSession`` as the
  matrix says (the session record's reopen relies on it);
* **a turn through the bridge** — the mock calls the collimate MCP server's
  ``list_files`` (however the harness namespaces it); its result reaches the
  model and the reply, the session id is recorded in
  ``_agent/session.json``, and a reopen loads it;
* **the commands grant** — the mock calls the harness's own shell tool on a
  probe only running it can answer, under deny, ask (rejected / allowed) and
  allow: deny never runs it and cards nothing, ask cards it and runs it only
  when allowed, allow runs it uncarded (``_assert_commands_outcome``);
* **a sandboxed shell** (pydantic-ai) — under ``docker: full`` the command
  runs in the sandbox container, under ``docker: none`` on this machine;
* **a real provider** — the one row that leaves the mock: for every
  harness × provider a key in the environment reaches (Cerebras, Gemini,
  OpenRouter), one short turn against the provider's real endpoint, in which
  the model lists the workspace and names the one file only that listing
  shows. It is billed, and skips without the key.

Gates: ``COLLIMATE_LIVE_DOCKER=1`` and a reachable daemon for the container
harnesses (the first run builds their images); host node >= 22 for
claude-code's adapter. Outside the real-provider row the provider key is a
placeholder: nothing is billed.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from _agent_shared.harnesses import HARNESS
from collimate.providers import PROVIDER_DEFAULT_MODELS, STANDARD_MODEL_ENDPOINTS

from .conftest import (
    KEY_ENV, bind_model_account, docker_available, eng, has_key, model_account, model_for, run_on_test_loop,
)
from .provider_mock import ProviderMock
from .test_live_acp_conformance import (
    _COMMANDS_LEVELS, _answer_cards, _assert_commands_outcome, _host_node_ok, _probe_command,
    _spy_terminals, _write_probe,
)

pytestmark = pytest.mark.usefixtures("one_loop_per_test")

_MATRIX = Path(__file__).resolve().parents[1] / "tests" / "data" / "acp_registry" / "protocol-matrix.json"

#: The mock's key: a placeholder no provider accepts.
_KEY = "sk-collimate-conformance-mock"


def _provider(harness: str) -> str:
    """The provider a harness bills the mock as: OpenRouter (the OpenAI wire,
    and the Responses wire for codex) wherever the harness bills it, else its
    one provider (claude-code: Anthropic; antigravity: Gemini)."""
    providers = HARNESS[harness].providers
    return next(p for p in ("openrouter", "anthropic", "gemini") if p in providers)


def _docker_bridge(harness: str) -> bool:
    """A container harness, or one whose shell runs in a sandbox the host places."""
    row = HARNESS[harness]
    return row.containerized or row.commands.sandboxed


def _gate(harness: str):
    row = HARNESS[harness]
    if _docker_bridge(harness):
        return pytest.mark.skipif(not docker_available(),
                                  reason="set COLLIMATE_LIVE_DOCKER=1 and have a reachable docker daemon")
    if row.locus == "host":
        return pytest.mark.skipif(not _host_node_ok(), reason="the claude ACP adapter needs host node >= 22")
    return pytest.mark.skipif(False, reason="")


def _harnesses(*, acp: bool = False) -> list:
    return [pytest.param(name, marks=_gate(name), id=name) for name, row in HARNESS.items()
            if not acp or row.acp_argv]


@pytest.fixture
def mock_provider(request, monkeypatch):
    """The scripted provider at ``harness``'s registry endpoint, the harness's
    run bridge (the docker env plane for a container harness) and the
    capabilities each ACP ``initialize`` probed."""
    harness = request.node.callspec.params["harness"]
    row = HARNESS[harness]
    bridge = request.getfixturevalue("docker_env_bridge" if _docker_bridge(harness) else "native_bridge")
    provider = request.node.callspec.params.get("provider") or _provider(harness)
    prefix = urlsplit(STANDARD_MODEL_ENDPOINTS[provider]).path.rstrip("/")
    mock = ProviderMock(prefix=prefix)
    probed: list = []
    from _agent_shared import acp_client
    real_probe = acp_client.probed_capabilities

    def probe(caps):
        found = real_probe(caps)
        probed.append((caps, found))
        return found

    monkeypatch.setattr(acp_client, "probed_capabilities", probe)
    with mock:
        monkeypatch.setitem(STANDARD_MODEL_ENDPOINTS, provider, mock.url_for(row.locus, prefix))
        monkeypatch.setenv(KEY_ENV[provider], _KEY)
        bind_model_account(model_account(provider, model=PROVIDER_DEFAULT_MODELS[provider], harness=harness),
                           bridge=bridge)
        yield mock, provider, probed


def _turn(harness: str, provider: str, prompt: str, **kw):
    """One engine turn. A host-locus harness (claude-code) runs the CLI
    bundled in its adapter: the default runtime selects no machine CLI."""
    return run_on_test_loop(asyncio.wait_for(eng.run_agent(
        prompt=prompt, backend=harness, auth="api_key", provider=provider,
        model=PROVIDER_DEFAULT_MODELS[provider], websearch=False, **kw,
    ), timeout=300))


def _matrix_row(harness: str) -> "dict | None":
    registry_id = HARNESS[harness].registry_id
    if not registry_id:
        return None
    rows = json.loads(_MATRIX.read_text())["agents"]
    return next((r for r in rows if r["id"] == registry_id), None)


@pytest.mark.parametrize("harness", _harnesses())
def test_a_turn_reaches_the_bridge_and_its_result_the_reply(mock_provider, harness):
    import gen
    from _agent_shared.acp_run import close_acp_sessions_sync, live_session

    mock, provider, probed = mock_provider
    gen.update_file("alpha.txt", "a")
    gen.update_file("beta.txt", "b")
    mock.action = ("tool", "list_files", {})
    events: list = []
    res = _turn(harness, provider, "List the files in this cell.", enable_file_tools=True,
                stream=True, on_event=events.append)
    detail = f"{harness}: reply {res.text!r}, calls {mock.calls}, misses {mock.misses}, trace {res.trace[-6:]}"
    assert mock.requests and not mock.misses, detail
    assert mock.reached("list_files"), detail
    assert "alpha.txt" in (res.text or "") and "beta.txt" in (res.text or ""), detail
    assert any(ev.get("type") == "text_delta" for ev in events), detail
    if not HARNESS[harness].acp_argv:
        return
    # initialize: the probed capabilities agree with the protocol matrix.
    assert probed, f"{harness}: no ACP initialize was probed"
    caps, found = probed[0]
    matrix = _matrix_row(harness)
    if matrix is not None:
        assert matrix["protocolVersion"] == 1
        assert found["load"] is bool(matrix["capabilities"]["loadSession"]), (harness, caps, matrix["capabilities"])
    # The session is recorded, and a reopen loads it.
    rec = json.loads(gen.read_file("_agent/session.json"))
    assert rec["backend"] == harness and rec["acp_session_id"], rec
    close_acp_sessions_sync()
    mock.action = ("text", "ACP-AGAIN")
    again = _turn(harness, provider, "Reply with exactly: ACP-AGAIN")
    assert "ACP-AGAIN" in (again.text or ""), f"{harness}: reopened session gave {again.text!r}"
    session = live_session(harness)
    if session is not None and found["load"]:
        assert session.loaded, f"{harness}: the reopen did not session/load"


@pytest.mark.parametrize("level", _COMMANDS_LEVELS)
@pytest.mark.parametrize("harness", _harnesses(acp=True))
def test_the_harness_follows_the_commands_grant(mock_provider, monkeypatch, harness, level):
    _commands_row(mock_provider, monkeypatch, harness, level)


def _commands_row(mock_provider, monkeypatch, harness: str, level: str) -> None:
    """One commands-grant row. A level the harness's mechanism cannot hold is
    held as the stricter level its notice names (hermes: ask as deny), and
    the row asserts that level's outcome — never skipped."""
    mock, provider, _probed = mock_provider
    commands, _, answer = level.partition("-")
    held, _notice = HARNESS[harness].resolve_commands(commands)
    mock.action = ("shell", _probe_command(held))
    cards = _answer_cards(monkeypatch, "reject_once" if answer == "reject" else "allow_once")
    terms = _spy_terminals(monkeypatch)
    digest = _write_probe()
    res = _turn(harness, provider, "Run the probe.", enable_file_tools=True, commands=commands)
    print(f"[mock] {harness}/{level}: calls {mock.calls} misses {mock.misses}")
    assert not mock.misses, mock.misses
    _assert_commands_outcome(harness, level if held == commands else held, res, cards, digest, terms,
                             host=HARNESS[harness].locus == "host")


@pytest.mark.parametrize("level", _COMMANDS_LEVELS)
@pytest.mark.parametrize("harness", [pytest.param(name, marks=_gate(name), id=name)
                                     for name, row in HARNESS.items() if row.commands.sandboxed])
def test_a_sandboxed_harness_follows_the_grant_on_this_machine_too(mock_provider, monkeypatch, harness, level):
    """Under docker: none a sandboxed harness's shell runs on this machine:
    the same grant holds there (ask cards every command, deny offers none)."""
    from collimate import native
    native.current_bridge().permissions = {"docker": "none", "internet": "full",
                                           "commands": level.partition("-")[0]}
    _commands_row(mock_provider, monkeypatch, harness, level)


def _marker_target(harness: str) -> str:
    """Where a vector's command writes its marker, as the harness sees it."""
    import gen
    if HARNESS[harness].containerized:
        return f"/workspace/{_BYPASS_MARKER}"
    return os.path.join(gen.workspace_path(), _BYPASS_MARKER)


_BYPASS_MARKER = "bypass.ran"


@pytest.mark.parametrize("level", ["deny", "ask-reject"])
@pytest.mark.parametrize("harness", _harnesses(acp=True))
def test_no_offered_shell_tool_runs_a_command_below_allow(mock_provider, monkeypatch, harness, level):
    """Every shell-like tool the harness offers is called, not just the
    first: under deny and a rejected ask none of them runs the command."""
    import gen
    mock, provider, _probed = mock_provider
    commands = level.partition("-")[0]
    _answer_cards(monkeypatch, "reject_once")
    mock.action = ("shell_all", f"touch {_marker_target(harness)}")
    res = _turn(harness, provider, "Run the probe.", enable_file_tools=True, commands=commands)
    marker = os.path.join(gen.workspace_path(), _BYPASS_MARKER)
    assert not os.path.exists(marker), f"{harness}/{level}: {mock.calls} ran: {res.text!r}"


def _bypass_params() -> list:
    from .bypass import VECTORS
    return [pytest.param(harness, vector, marks=_gate(harness), id=f"{harness}-{vector.name}")
            for harness, vectors in VECTORS.items() for vector in vectors]


@pytest.mark.parametrize("level", ["deny", "ask-reject"])
@pytest.mark.parametrize("harness,vector", _bypass_params())
def test_a_known_bypass_vector_runs_nothing_below_allow(mock_provider, monkeypatch, harness, vector, level):
    """A harness's known way around its own command gate (a workflow tool,
    an agent-writable config or hook file, a delegate) run by the scripted
    model under deny and a rejected ask: its marker command never runs."""
    import gen
    mock, provider, _probed = mock_provider
    commands = level.partition("-")[0]
    _answer_cards(monkeypatch, "reject_once")
    target = _marker_target(harness)
    if vector.setup is not None:
        vector.setup(gen, target)
    mock.action = vector.action(target)
    res = _turn(harness, provider, vector.prompt, enable_file_tools=True, commands=commands)
    print(f"[bypass] {harness}/{vector.name}/{level}: calls {mock.calls}")
    marker = os.path.join(gen.workspace_path(), _BYPASS_MARKER)
    assert not os.path.exists(marker), f"{harness}/{vector.name}/{level}: ran: {mock.calls} {res.text!r}"


# Every provider a harness bills through a URL Collimate writes for it, each
# reached at its registry endpoint (a wrong path shape is a miss).
_ENDPOINT_ROWS = {"copilot": ("openai", "anthropic", "groq", "cerebras", "openrouter", "xai", "mistral")}


@pytest.mark.parametrize("harness,provider", [
    pytest.param(harness, provider, marks=_gate(harness), id=f"{harness}-{provider}")
    for harness, providers in _ENDPOINT_ROWS.items() for provider in providers])
def test_every_billed_provider_is_reached_at_its_registry_endpoint(mock_provider, harness, provider):
    mock, provider, _probed = mock_provider
    mock.action = ("text", "ENDPOINT-OK")
    res = _turn(harness, provider, "Reply with exactly: ENDPOINT-OK")
    assert mock.requests and not mock.misses, (harness, provider, mock.misses)
    assert "ENDPOINT-OK" in (res.text or ""), (harness, provider, res.text)


@pytest.mark.parametrize("docker", ["full", "none"])
@pytest.mark.parametrize("harness", [pytest.param(name, marks=_gate(name), id=name)
                                     for name, row in HARNESS.items() if row.commands.sandboxed])
def test_a_sandboxed_harness_runs_commands_in_its_container_under_docker_full(
        mock_provider, monkeypatch, harness, docker):
    """Where a sandboxed harness's shell runs: under docker: full with
    commands: allow the probe runs inside the sandbox container
    (``/.dockerenv``, ``/workspace``) on the mounted workspace; under docker:
    none on this machine, in the run directory."""
    from collimate import native

    import gen

    mock, provider, _probed = mock_provider
    native.current_bridge().permissions = {"docker": docker, "internet": "full", "commands": "allow"}
    gen.update_file("sandbox-probe.txt", "mounted")
    mock.action = ("shell", "if test -f /.dockerenv; then where=IN-SANDBOX; else where=ON-HOST; fi; "
                            "echo \"$where-$(cat sandbox-probe.txt)\"; pwd")
    res = _turn(harness, provider, "Run the probe.", enable_file_tools=True, commands="allow")
    detail = f"{harness}/{docker}: reply {res.text!r}, calls {mock.calls}"
    assert mock.calls, detail
    if docker == "full":
        assert "IN-SANDBOX-mounted" in (res.text or "") and "/workspace" in (res.text or ""), detail
    else:
        assert "ON-HOST-mounted" in (res.text or "") and gen.workspace_path() in (res.text or ""), detail


# ── a real provider: one short billed turn per harness × reachable provider ──

#: The providers a real turn is billed to, where a key is in the environment.
_REAL_PROVIDERS = ("cerebras", "gemini", "openrouter")

#: Harness × provider rows the harness's current release cannot complete,
#: with the upstream cause. Cerebras rejects any request field it does not
#: implement, and these CLIs send one; the app wires both the way each
#: documents and does not rewrite requests (a stripping proxy is a bridge the
#: rules forbid), so the row is expected to fail until a release stops
#: sending the field. Strict: the day one passes, the row turns red and its
#: entry here is removed.
_UPSTREAM_BROKEN = {
    ("goose", "cerebras"): ("goose 1.56.0 replays reasoning_content on assistant turns (gpt-oss-120b) "
                            "and sends a role qwen-3.8-27b's chat template refuses"),
    ("copilot", "cerebras"): "copilot 1.0.104 sends assistant.refusal and a custom tool type",
}


def _real_rows() -> list:
    rows = []
    for name, row in HARNESS.items():
        for provider in _REAL_PROVIDERS:
            if provider in row.providers:
                marks = [_gate(name), pytest.mark.skipif(not has_key(provider), reason=f"needs {KEY_ENV[provider]}")]
                broken = _UPSTREAM_BROKEN.get((name, provider))
                if broken:
                    marks.append(pytest.mark.xfail(strict=True, reason=f"upstream: {broken}"))
                rows.append(pytest.param(name, provider, id=f"{name}-{provider}", marks=marks))
    return rows


@pytest.mark.parametrize("harness,provider", _real_rows())
def test_a_real_provider_answers_through_the_harness(request, harness, provider):
    """The real endpoint, the real key, the real model: the harness lists the
    workspace (through the collimate bridge's list_files, or a listing tool
    of its own) and the reply names the one file only that listing shows."""
    import secrets

    import gen

    bridge = request.getfixturevalue("docker_env_bridge" if _docker_bridge(harness) else "native_bridge")
    probe = f"probe-{secrets.token_hex(4)}.txt"
    gen.update_file(probe, "the file only a listing shows\n")
    gen.update_file("notes.md", "nothing here\n")
    model = model_for(provider)
    bind_model_account(model_account(provider, model=model, harness=harness), bridge=bridge)
    res = run_on_test_loop(asyncio.wait_for(eng.run_agent(
        prompt=("List the files in this workspace with your file-listing tool (list_files, or the one you "
                "have), then reply with only the name of the one file whose name starts with probe-."),
        backend=harness, auth="api_key", provider=provider, model=model, websearch=False,
        enable_file_tools=True,
    ), timeout=600))
    tools = [t.get("tool") for t in res.trace if t.get("type") == "tool_use"]
    print(f"[real] {harness}/{provider} {model}: tools={tools} reply={(res.text or '')[-200:]!r}")
    assert probe in (res.text or ""), f"{harness}/{provider}: reply {res.text!r}, trace {res.trace[-8:]}"
    assert tools, f"{harness}/{provider}: the reply named the file without a listing"
