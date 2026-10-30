# SPDX-License-Identifier: MIT
"""Drift guard: the Agent cell's generator's ``agent-config.set_yaml`` MUST
mirror the engine's ``_BACKENDS`` spec table (the single source of truth
every validator and dispatcher reads from) and the keys
``parse_agent_config`` reads on each surface. They are hand-maintained in
different files; this test computes every UI gate (``show_if`` / option
``enable_if``) straight from the spec table and asserts the shipped schema
agrees — so adding or changing a backend row forces the matching schema edit
rather than a silent mismatch where the runner editor offers a combination
the engine rejects (or hides one it would accept).

It also asserts the manifest declares config folders and the capability
knobs the permission-grant refactor deleted stay deleted — the exec/docker
sandbox is derived from the runner's permission grant now, never from
config.

Everything is derived from ``shared_config._BACKENDS`` / ``eng._REPORT_TYPES`` /
``shared_config._ALL_PROVIDERS`` — no hardcoded backend lists — so the expectations track
the table automatically.
"""
from __future__ import annotations

import os

import pytest

import yaml
from collimate.permissions import Grant

from tests._workspace import GENERATORS


pytestmark = pytest.mark.usefixtures("no_machine_harness_probe")

from _agent_shared import harness_homes  # noqa: E402
from _agent_shared import agent_config as shared_config  # noqa: E402
from _agent_shared.harnesses import HARNESS  # noqa: E402
import _agent_shared as eng  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PACKAGE = os.path.join(GENERATORS, "agent", "agent")


def _load_yaml(name: str) -> dict:
    with open(os.path.join(_PACKAGE, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_FIELD_LIST = _load_yaml("agent-config.set_yaml")["fields"]
_FIELDS = {f["key"]: f for f in _FIELD_LIST}
_MANIFEST = _load_yaml("agent.generator")
SURFACES = shared_config._SURFACES

BK = shared_config._BACKENDS
ALL_BACKENDS = set(BK)
TTY_BACKENDS = {b for b, s in BK.items() if "terminal" in s.uis}


# ── condition / option introspection ──────────────────────────────────


def _as_set(v) -> set:
    return set(v) if isinstance(v, list) else {v}


def _find_backend(cond):
    """The backend constraint inside a ``show_if`` / ``enable_if`` condition,
    as ``("in", {…})`` (an allow-list) or ``("not_in", {…})`` (a deny-list),
    or ``None`` when the condition carries no backend constraint. Walks the
    ``all`` / ``any`` / ``not`` combinators the schema actually uses."""
    if not isinstance(cond, dict):
        return None
    if "backend" in cond:
        return ("in", _as_set(cond["backend"]))
    if "not" in cond:
        inner = _find_backend(cond["not"])
        if inner is None:
            return None
        kind, s = inner
        return ("not_in", s) if kind == "in" else ("in", s)
    for combinator in ("all", "any"):
        if combinator in cond:
            for sub in cond[combinator] or []:
                got = _find_backend(sub)
                if got is not None:
                    return got
    return None


def _opt_value(opt):
    return opt["value"] if isinstance(opt, dict) else opt


def _opt_enable_if(opt):
    return opt.get("enable_if") if isinstance(opt, dict) else None


def _option_values(key: str) -> set:
    return {_opt_value(o) for o in _FIELDS[key]["options"]}


def _visible(values: dict) -> set:
    """The schema's visible field keys for these (partial) values, by the
    evaluator the runner editor and the engine share."""
    from collimate.config_values import compute_visibility, resolve_fields
    resolved = resolve_fields(_FIELD_LIST, values)
    return {k for k, shown in compute_visibility(_FIELD_LIST, resolved).items() if shown}


# ── surface: the run's one surface ─────────────────────────────────────


def test_surface_is_the_first_field_and_the_engines_axis_GN02():
    """`surface` leads the schema: Annotation (the default), Chat or CLI (the
    terminal surface) — exactly the engine's _SURFACES — always visible
    (every other gate keys off it) and ungated."""
    field = _FIELD_LIST[0]
    assert field["key"] == "surface" and field["type"] == "dropdown"
    assert _option_values("surface") == set(SURFACES) == {"annotation", "chat", "terminal"}
    assert {_opt_value(o): o["label"] for o in field["options"]} == {
        "annotation": "Annotation", "chat": "Chat", "terminal": "CLI"}
    assert field["default"] == "annotation"
    assert "show_if" not in field
    for opt in field["options"]:
        assert _opt_enable_if(opt) is None, _opt_value(opt)


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("backend", sorted(TTY_BACKENDS))
def test_each_surface_shows_only_keys_it_reads_GN02(surface, backend):
    """A field the runner editor shows on a surface is a key the engine reads
    there, and every key the surface reads that the schema carries is
    shown: the schema's show_if and the engine's _SURFACE_KEYS agree."""
    reads = shared_config._CONFIG_KEYS | shared_config._SURFACE_KEYS[surface]
    visible = _visible({"surface": surface, "backend": backend})
    assert visible <= reads, visible - reads
    hidden = (reads & set(_FIELDS)) - visible
    # The selected file goes first on a terminal only where the TTY takes a
    # first message (claude-code).
    allowed_hidden = ({"selected_file_first"}
                      if surface == "terminal" and backend != "claude-code" else set())
    assert hidden == allowed_hidden, hidden


# ── backend dropdown ───────────────────────────────────────────────────


def test_backend_options_are_exactly_the_spec_table():
    """The backend dropdown offers precisely the canonical spec-table names —
    no aliases, nothing missing — and is ALWAYS visible (a hidden referent
    would evaluate UNKNOWN and disable every option that keys off it). A
    backend with no TTY of its own (pydantic-ai) is disabled on the
    terminal surface; every TTY harness serves every surface. The
    manifest's model requirement lists the same harnesses."""
    from collimate.account_taxonomy import HARNESS_TERMINAL
    field = _FIELDS["backend"]
    assert _option_values("backend") == ALL_BACKENDS
    assert TTY_BACKENDS == set(HARNESS_TERMINAL)
    assert "show_if" not in field
    assert field["default"] == "pydantic-ai"
    for opt in field["options"]:
        gate = _opt_enable_if(opt)
        if _opt_value(opt) in TTY_BACKENDS:
            assert gate is None, _opt_value(opt)
        else:
            assert gate == {"not": {"surface": "terminal"}}, _opt_value(opt)
    harnesses = _MANIFEST["account_requirements"]["model"]["harnesses"]
    assert set(harnesses) == ALL_BACKENDS


# ── session: the annotation run's shape ───────────────────────────────


def test_session_options_are_the_axis():
    """`session` is exactly the engine's _SESSIONS (oneshot | interactive),
    defaults to interactive (a run that listens for messages until it is
    stopped) and shows on the annotation surface alone: chat and terminal
    runs are interactive. No option carries a gate: every backend serves
    both shapes. The v1 mode/ui pair and the `start` axis that replaced it
    are both gone, and so are the retired handoff and target settings."""
    assert _option_values("session") == set(shared_config._SESSIONS)
    assert _FIELDS["session"]["default"] == "interactive"
    assert _FIELDS["session"]["show_if"] == {"surface": "annotation"}
    for opt in _FIELDS["session"]["options"]:
        assert _opt_enable_if(opt) is None, _opt_value(opt)
    assert not {"mode", "ui", "start", "against", "read_" + "handoff"} & set(_FIELDS)


def test_system_prompt_is_advanced():
    """The system prompt is an advanced knob: the everyday config is the run
    shape, the harness and the prompt."""
    assert _FIELDS["system_prompt"]["advanced"] is True


def test_selected_file_first_is_a_checkbox_off_by_default_GN02():
    """"Start from the selected file" is off unless a runner turns it on, and
    shows wherever the first turn can carry it: an annotation run, the
    chat, and claude-code's TTY. With it on the runner takes the selected
    file."""
    field = _FIELDS["selected_file_first"]
    assert field["type"] == "checkbox" and field["default"] is False
    assert field["label"] == "Start from the selected file"
    assert field["show_if"] == {"any": [{"surface": ["annotation", "chat"]},
                                        {"backend": "claude-code"}]}
    assert _MANIFEST["takes_selected_file_if"] == {"selected_file_first": True}
    assert "takes_selected_file" not in _MANIFEST


def test_manifest_prompt_conditions():
    """The runner editor's prompt box: on the annotation surface it is a
    one-shot's task, its first message — shown and required exactly when
    `session: oneshot` (an interactive annotation run takes none). A chat
    always shows it, optional: the opening message. The terminal shows it
    for claude-code alone, whose TTY takes a first message, never
    required."""
    assert _MANIFEST["prompt_show_if"] == {"any": [
        {"surface": "chat"},
        {"all": [{"surface": "annotation"}, {"session": "oneshot"}]},
        {"all": [{"surface": "terminal"}, {"backend": "claude-code"}]}]}
    assert _MANIFEST["prompt_required_if"] == {"all": [{"surface": "annotation"}, {"session": "oneshot"}]}
    assert _MANIFEST["prompt_intent"]
    assert "prompt_required" not in _MANIFEST


# ── auth: each non-api_key option keys off the backends that honor it ──








def test_every_plan_pins_a_provider_the_defaults_table_knows():
    """parse_agent_config hard-errors on a provider outside
    PROVIDER_DEFAULT_MODELS, so a plan pinning one would break every run of
    that combination — and every per-(backend, plan) default names a real
    pair from the spec table."""
    for auth, provider in eng.PLAN_PROVIDERS.items():
        assert auth in shared_config._AUTHS, auth
        assert provider in eng.PROVIDER_DEFAULT_MODELS, (auth, provider)
    assert set(eng.PLAN_PROVIDERS) == set(shared_config._AUTHS) - {"api_key"}
    for backend, auth in eng.PLAN_DEFAULT_MODELS:
        assert backend in BK, backend
        assert auth in BK[backend].auths, (backend, auth)


def test_duplicate_permission_settings_are_absent__D06():
    assert "websearch" not in _FIELDS and "runtime" not in _FIELDS


# ── the config keys the engine reads ──────────────────────────────────


def test_every_schema_field_is_a_key_the_engine_reads():
    """parse_agent_config refuses a key no surface reads, so a schema field
    added without teaching the engine to read it fails every run — this pins
    the two together."""
    fields = set(_FIELDS)
    assert fields <= shared_config._KNOWN_KEYS, fields - shared_config._KNOWN_KEYS


def test_mcp_consent_field_matches_the_spec():
    """`mcp` is the cell-MCP consent tri-state of the annotation and chat
    surfaces, and BOTH non-off options carry exactly ONE exclusion and
    nothing else — the spec-derived no-MCP-client set (spec.cell_mcp=False:
    cline and pi, whose pinned adapters accept but never wire session MCP
    servers; parse_agent_config hard-rejects these too). http_stdio's
    container precondition depends on the run's permission grant, which a
    set_yaml condition cannot see, so the engine enforces it at run time
    instead. The terminal hides it: the harness's own TTY never attaches the
    cell's servers."""
    field = _FIELDS["mcp"]
    assert field["show_if"] == {"surface": ["annotation", "chat"]}
    no_mcp = sorted(b for b, s in BK.items() if not s.cell_mcp)
    expected = {"not": {"backend": no_mcp}} if no_mcp else None
    opts = {_opt_value(o): o for o in field["options"]}
    assert set(opts) == {"off", "http", "http_stdio"}
    assert _opt_enable_if(opts["off"]) is None
    assert _opt_enable_if(opts["http"]) == expected
    assert _opt_enable_if(opts["http_stdio"]) == expected
    # The engine side of the same fact: a cell_mcp=False backend hard-errors
    # on any non-off mcp value, naming the missing MCP client.
    for surface in ("annotation", "chat"):
        for b in no_mcp:
            with pytest.raises(ValueError, match="no.*MCP client"):
                eng.parse_agent_config(
                    {"surface": surface, "backend": b, "mcp": "http"}, grant=Grant.from_dict({}), can_ask=True)
    # Skills are no longer a knob at all — always delivered where supported.
    assert all(s.cell_skills for s in BK.values())
    assert "cell_skills" not in _FIELDS


@pytest.mark.parametrize("surface", ["annotation", "chat"])
def test_approvals_is_advanced_and_ask_is_live(host_free_run, surface):
    """`approvals` ships auto|ask, advanced, defaulting to auto. Phase 2
    lifted the ask gate: the option carries NO enable_if and the engine
    parses it through — the ACP permission handler surfaces each
    grant-permitted request as a card (an unknown value still hard-errors).
    The terminal hides it: the harness's own TTY answers its permission
    requests."""
    field = _FIELDS["approvals"]
    assert field.get("advanced") is True
    assert field["default"] == "auto"
    assert field["show_if"] == {"surface": ["annotation", "chat"]}
    assert _option_values("approvals") == {"auto", "ask"}
    for opt in field["options"]:
        assert _opt_enable_if(opt) is None, opt
    assert eng.parse_agent_config(
        {"surface": surface, "approvals": "ask"}, grant=Grant.from_dict({}), can_ask=True)["approvals"] == "ask"
    assert eng.parse_agent_config({"surface": surface}, grant=Grant.from_dict({}), can_ask=True)["approvals"] == "auto"
    with pytest.raises(ValueError, match="approvals"):
        eng.parse_agent_config({"surface": surface, "approvals": "sometimes"})


@pytest.mark.parametrize("surface", SURFACES)
def test_history_char_budget_is_gone(surface):
    """history_char_budget died with _compose_prompt (plan §2.2): the engine
    holds conversation natively now (the persistent ACP session; the
    in-process engine's per-thread message_history), so there is no prompt
    folding to budget. The schema no longer exposes it and the parser no
    longer emits it."""
    assert "history_char_budget" not in _FIELDS
    assert "history_char_budget" not in eng.parse_agent_config(
        {"surface": surface, "backend": "codex"}, grant=Grant.from_dict({}), can_ask=True)


# ── requires_docker: the Docker-policy axis ────────────────────────────


def test_only_a_container_cli_requires_docker_GN02():
    """A terminal whose CLI runs in the harness's own container needs docker:
    full (the runner lint and the app's saved-runner check read the
    manifest's condition); Claude Code's CLI, and Antigravity's on the Google
    plan, run on this machine, so their terminal does not; an annotation run
    and the chat place containers as their grant allows."""
    from collimate.lint import GeneratorRef
    assert _MANIFEST["requires_docker_if"]["surface"] == "terminal"
    assert "requires_docker" not in _MANIFEST
    ref = GeneratorRef.from_manifest(
        _MANIFEST, read_file=lambda rel: open(os.path.join(_PACKAGE, rel), encoding="utf-8").read())
    assert ref.docker_required({"surface": "terminal", "backend": "codex"})
    assert not ref.docker_required({"surface": "terminal", "backend": "claude-code"})
    assert not ref.docker_required({"surface": "terminal", "backend": "antigravity"})
    assert not ref.docker_required({"surface": "annotation"})
    assert not ref.docker_required({"surface": "chat"})
    assert not ref.docker_required({})


def test_run_needs_container_is_surface_aware_for_claude_code():
    """The per-run routing decision (selected for the current surface): every
    containerized backend (the CLI family and the antigravity CLI)
    containerizes on its ACP surfaces, and on the terminal where its CLI's
    locus is its container; claude-code never does (its annotation and chat
    turns run the ACP adapter as a host subprocess, its CLI runs on this
    machine); the in-process backend never does."""
    for b, s in BK.items():
        if s.containerized:
            for sf in shared_config._SURFACES:
                assert eng.run_needs_container(b, sf), (b, sf)
    assert eng.run_needs_container("antigravity", "annotation")
    assert not eng.run_needs_container("claude-code", "chat")
    assert not eng.run_needs_container("claude-code", "annotation")
    # The CLI surface runs where its terminal locus puts it for the auth:
    # claude-code's on this machine, antigravity's on the Google plan too.
    assert not eng.run_needs_container("claude-code", "terminal")
    assert not eng.run_needs_container("claude-code", "terminal", auth="claude_code")
    assert eng.run_needs_container("antigravity", "terminal")
    assert not eng.run_needs_container("antigravity", "terminal", auth="google")
    assert not eng.run_needs_container("pydantic-ai", "chat")


def test_the_agents_containers_need_docker_full(host_free_run):
    """The agent's containers — the CLI harness container and the exec
    AgentSandbox — are env-plane SIBLINGS placed from the native generator
    process, and the host gates every env-plane op at full: so the run
    places one exactly when its grant is docker: full (the unrestricted dev
    grant carries full), and `limited` (a containerized main loop with no
    docker reach) is refused here rather than failing at placement."""
    import asyncio

    import _agent_shared.acp_run as acp_run

    class _Reached(Exception):
        pass

    async def reached(**_):
        raise _Reached()

    for level, refused in (("none", True), ("limited", True), ("full", False)):
        host_free_run.permissions = {"docker": level, "internet": "full"}
        orig = acp_run.run_acp_turn
        acp_run.run_acp_turn = reached
        try:
            with pytest.raises(RuntimeError if refused else _Reached) as err:
                asyncio.run(eng.run_agent(prompt="x", backend="codex", runtime="docker"))
        finally:
            acp_run.run_acp_turn = orig
        if refused:
            assert "docker: full" in str(err.value), level
    host_free_run.permissions = {}
    cfg = eng.parse_agent_config({"surface": "terminal", "backend": "codex"})
    assert cfg["surface"] == "terminal" and cfg["runtime"] == "docker"


# ── prose defaults: the description string tracks the engine table ─────




# ── the vendored ACP-registry leg (Phase 4 — agent-v2 §6.3) ────────────
# The schema↔spec guard above keeps the UI honest; this leg keeps the SPEC
# honest against the world: a vendored snapshot of the ACP registry (the
# protocol matrix, the quarantine list, and each roster agent's invocation
# record — tests/data/acp_registry/, re-vendored deliberately per its
# README) pins the roster's registry-driven assumptions.


_REG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "acp_registry")


def _registry(name: str):
    import json
    with open(os.path.join(_REG_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


_ACP_BACKENDS = dict(BK)

# Native ACP harnesses the registry does not list yet: each declares why
# (``HarnessAdapter.unregistered``), so a blank registry id is never silent.
_UNREGISTERED = {b: s for b, s in _ACP_BACKENDS.items() if HARNESS[b].unregistered}
_REGISTRY_BACKENDS = {b: s for b, s in _ACP_BACKENDS.items() if b not in _UNREGISTERED}


def test_every_acp_backend_names_a_registry_id_or_says_why_not():
    """A blank ``acp_registry_id`` is legal for EXACTLY one reason: the
    registry does not list the harness yet, and its adapter says so
    (``unregistered``). Every ACP backend either names a registry id or
    declares why not — no third case, so a typo'd id cannot hide as
    "unlisted"."""
    for b, s in _ACP_BACKENDS.items():
        assert bool(s.acp_registry_id) != (b in _UNREGISTERED), (
            f"{b}: an ACP backend must name a registry id or declare why the "
            f"registry does not list it — exactly one")


def test_every_acp_backend_has_a_live_registry_matrix_row():
    """Every REGISTRY-sourced ACP backend names its registry id, and the
    vendored protocol matrix carries a row for it that initialized
    successfully on protocol v1 and advertises loadSession — the capability
    the in-run reopen (an idle park, a respawn) is gated on. An
    unregistered harness has no row to read."""
    rows = {a["id"]: a for a in _registry("protocol-matrix.json")["agents"]}
    for b, s in BK.items():
        if b in _UNREGISTERED:
            continue
        assert s.acp_registry_id, f"{b}: ACP backend with no registry id"
        row = rows.get(s.acp_registry_id)
        assert row is not None, (
            f"{b}: registry id {s.acp_registry_id!r} has no protocol-matrix "
            f"row — re-vendor tests/data/acp_registry and re-evaluate the "
            f"roster entry")
        assert row.get("protocolVersion") == 1, (b, row.get("protocolVersion"))
        assert row.get("initialize", {}).get("status") == "success", (
            f"{b}: the registry probe could not initialize this agent"
        )
        assert row.get("capabilities", {}).get("loadSession") is True, (
            f"{b}: the registry matrix reports no loadSession — the in-run "
            f"reopen assumes it"
        )


def test_roster_honors_the_registry_quarantine():
    """No roster backend maps to a quarantined registry agent — the §1 rule
    ("any launcher reading registry.json must honor quarantine.json"),
    enforced against the vendored snapshot."""
    quarantined = set(_registry("quarantine.json"))
    roster_ids = {s.acp_registry_id for s in _REGISTRY_BACKENDS.values()}
    assert not (roster_ids & quarantined), sorted(roster_ids & quarantined)


def test_an_unregistered_harness_is_still_absent_from_the_registry():
    """A harness declared unregistered is re-evaluated when a re-vendor shows
    the registry lists it: then it must name its registry id instead."""
    listed = {a["id"] for a in _registry("protocol-matrix.json")["agents"]}
    for b in _UNREGISTERED:
        assert b not in listed and f"{b}-agent" not in listed, (
            f"{b} is in the ACP registry now — name its registry id")


def test_acp_argv_matches_the_registry_invocation():
    """Each roster agent's ``acp_argv`` tail must equal the args of its
    registry invocation record (the npx/binary distribution in agents.json)
    — the drift that would otherwise rot silently: an upstream flag rename
    (the Gemini CLI's --experimental-acp → --acp) reaches the registry
    first. argv[0] is ours (the image-installed binary); the args are
    theirs."""
    agents = _registry("agents.json")
    assert set(agents) == {s.acp_registry_id
                           for s in _REGISTRY_BACKENDS.values()}, (
        "agents.json must vendor exactly the roster's registry ids — "
        "re-assemble it per tests/data/acp_registry/README.md")
    for b, s in _REGISTRY_BACKENDS.items():
        dist = agents[s.acp_registry_id].get("distribution") or {}
        if "npx" in dist:
            want = list(dist["npx"].get("args") or [])
        else:
            # binary distributions carry per-platform records; the roster
            # runs in Linux containers, so read the Linux ones (antigravity's
            # alone carry an argument, ``--uid=``).
            records = dist.get("binary") or {}
            plats = [v for k, v in records.items() if k.startswith("linux")] or list(records.values())
            assert plats, f"{b}: no distribution record in agents.json"
            want = list(plats[0].get("args") or [])
        assert list(s.acp_argv[1:]) == want, (
            f"{b}: acp_argv tail {list(s.acp_argv[1:])!r} drifted from the "
            f"registry invocation args {want!r}")


# ── manifest: config folders declared ─────────────────────────────────


def test_manifest_declares_config_folders():
    """The generator declares the _skills and _mcp folders with their
    samples (the terminal surface reads _skills alone: its TTY never takes
    the cell's MCP servers)."""
    folders = _MANIFEST.get("config_folders") or []
    assert {f["name"] for f in folders} == {"_skills", "_mcp"}
    for f in folders:
        assert f["description"].strip()
        assert f["sample"] == f"samples/{f['name']}"


def test_every_backend_runner_accepts_what_run_agent_dispatches():
    """The two-family dispatch in ``run_agent`` and each arm's signature must
    agree — the drift that let ``mcp_decls`` reach a runner that rejects it
    (an instant TypeError before the model was ever contacted, invisible to
    every unit test because they call the arms directly).

    Binds the exact kwargs each dispatch branch passes against the arm's
    signature, plus every backend's terminal surface, so a new kwarg (or a
    new backend) can't be added on one side only."""
    import inspect

    import _agent_shared.acp_run as acp_run

    # The kwargs each acp_locus family's branch passes, mirrored from
    # run_agent's _call_backend. Values are irrelevant — only name acceptance.
    _COMMON = {
        "provider", "model", "system_prompt", "prompt",
        "stream", "emit", "should_stop", "enable_file_tools", "attachments",
        "annotation_tools",
    }
    acp_kwargs = _COMMON | {
        "backend", "auth", "mcp_decls", "websearch",
        "turn_skills", "session_key", "commands",
    }

    def _accepts(fn, names: set) -> list:
        sig = inspect.signature(fn)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in sig.parameters.values()):
            return []
        return sorted(names - set(sig.parameters))

    # Every backend belongs to exactly one dispatch family.
    assert {s.acp_locus for s in shared_config._BACKENDS.values()} <= {"host", "container"}

    missing = _accepts(acp_run.run_acp_turn, acp_kwargs)
    assert not missing, f"ACP arm rejects run_agent kwargs: {missing}"

    # Every ACP backend has a spawn profile, a mode rule and session-store
    # paths; every terminal surface resolves.
    checked = 0
    for backend, spec in shared_config._BACKENDS.items():
        assert backend in acp_run._PROFILES, f"{backend} has no ACP spawn profile"
        assert backend in acp_run._MODE_RULES, f"{backend} has no mode rule"
        assert backend in harness_homes._SESSION_PATHS, f"{backend} has no session paths"
        assert spec.acp_argv, f"{backend} declares no adapter argv"
        if HARNESS[backend].terminal:
            fn = eng.surface_runner(backend, "terminal")
            assert callable(fn)
            # Bind the kwargs session.terminal.terminal_leg actually passes —
            # a kwarg added on one side only dies at run time with a
            # TypeError (the mcp_decls drift, again).
            import inspect
            params = inspect.signature(fn).parameters
            for kw in ("extra_system", "resume_harness_session_id", "first_message",
                       "on_ready"):
                assert kw in params, (
                    f"{backend} terminal runner is missing the dispatched "
                    f"kwarg {kw!r}")
            assert "end_event" not in params, backend
        checked += 1
    assert checked == len(shared_config._BACKENDS), checked
