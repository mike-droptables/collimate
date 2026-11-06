# SPDX-License-Identifier: MIT
"""The harness adapters are complete, one per folder, and drive the runtime views."""
from __future__ import annotations

import dataclasses
import os
import pytest

from tests._workspace import GENERATORS


_ROOT = GENERATORS / "_agent_shared" / "harnesses"


def test_every_harness_is_one_declared_folder():
    from _agent_shared.harnesses import HARNESS, ROSTER, HarnessAdapter, validate_registry
    assert tuple(HARNESS) == ROSTER == (
        "pydantic-ai", "claude-code", "antigravity", "codex", "goose", "opencode",
        "qwen-code", "cline", "pi", "hermes", "vibe", "copilot")
    validate_registry(HARNESS)
    folders = {p.name for p in _ROOT.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert folders == {name.replace("-", "_") for name in HARNESS}
    for name, row in HARNESS.items():
        assert isinstance(row, HarnessAdapter) and row.id == name
        package = _ROOT / name.replace("-", "_")
        assert (package / "adapter.py").is_file() and (package / "backend.py").is_file()
        # A container harness carries its image context; nothing else does
        # but the host-locus pydantic-ai, whose image is its command sandbox
        # (claude-code's CLI terminal runs on this machine, with no image).
        assert (package / "image" / "Dockerfile").is_file() is bool(row.image), name
        assert bool(row.image) == (row.containerized or row.commands.sandboxed), name


def test_a_missing_facet_is_an_error():
    from _agent_shared.harnesses import HARNESS, HarnessAdapter
    required = [f.name for f in dataclasses.fields(HarnessAdapter)
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING]
    assert set(required) >= {"id", "label", "locus", "acp_argv", "auth", "model", "instructions",
                             "tools", "commands", "session", "terminal", "hooks", "prepare"}
    for facet in required:
        supplied = {f.name: getattr(HARNESS["opencode"], f.name)
                    for f in dataclasses.fields(HarnessAdapter) if f.name != facet}
        with pytest.raises(TypeError, match=facet):
            HarnessAdapter(**supplied)


@pytest.mark.parametrize("change,message", [
    ({"locus": "elsewhere"}, "unknown locus"),
    ({"acp_argv": ()}, "adapter argv"),
    ({"image": ""}, "needs its image"),
    ({"host_twin": True, "host_binary": ""}, "host twin"),
])
def test_the_registry_refuses_an_incoherent_declaration(change, message):
    from _agent_shared.harnesses import HARNESS, validate_registry
    row = dataclasses.replace(HARNESS["qwen-code"], **change)
    with pytest.raises(ValueError, match=message):
        validate_registry({"qwen-code": row})


@pytest.mark.parametrize("harness,locus,message", [
    ("qwen-code", "elsewhere", "terminal locus must be"),
    ("qwen-code", "host", "needs the harness's CLI"),
])
def test_the_registry_refuses_an_incoherent_terminal_locus(harness, locus, message):
    from _agent_shared.harnesses import HARNESS, validate_registry
    row = HARNESS[harness]
    row = dataclasses.replace(row, host_binary="", terminal=dataclasses.replace(row.terminal, locus=locus))
    with pytest.raises(ValueError, match=message):
        validate_registry({harness: row})


def test_the_terminal_locus_is_declared_per_auth():
    """Claude Code's CLI runs on this machine under every auth;
    Antigravity's on the Google plan and in its container on a Gemini key;
    every other harness's in its container. An auth the harness does not
    offer is not a locus."""
    from _agent_shared.harnesses import HARNESS, terminal_locus
    assert {auth: terminal_locus(HARNESS["claude-code"], auth)
            for auth in HARNESS["claude-code"].auths} == {"api_key": "host", "claude_code": "host"}
    assert {auth: terminal_locus(HARNESS["antigravity"], auth)
            for auth in HARNESS["antigravity"].auths} == {"api_key": "container", "google": "host"}
    for name, row in HARNESS.items():
        if row.terminal and name not in ("claude-code", "antigravity"):
            assert {terminal_locus(row, auth) for auth in row.auths} == {"container"}, name
    with pytest.raises(ValueError, match="no CLI surface"):
        terminal_locus(HARNESS["pydantic-ai"], "api_key")


def test_every_declared_callable_resolves():
    from _agent_shared.harnesses import HARNESS, resolve
    for row in HARNESS.values():
        targets = [row.prepare, row.commands.mode_rule, *row.commands.recognisers]
        hooks = row.hooks
        targets += [t for t in (hooks.normalize_update, hooks.tool_approval, hooks.explain_failure,
                                hooks.host_commands_level, hooks.commands_note, row.model.matches,
                                row.session.verify_loaded) if t]
        if row.terminal:
            targets += [t for t in (row.terminal.native_context, row.terminal.tail,
                                    row.terminal.harness_session_ids or None,
                                    row.terminal.lifetime_context) if t]
            if row.terminal.locus not in ("host", "container"):
                targets.append(row.terminal.locus)
        for target in targets:
            assert callable(resolve(target)), (row.id, target)


def test_the_registry_drives_runtime_views():
    from _agent_shared import acp_run, cli_container, harness_homes, terminal_tail
    from _agent_shared.harnesses import BACKENDS, HARNESS
    assert set(acp_run._PROFILES) == {name for name, row in HARNESS.items() if row.acp_argv}
    assert cli_container._IMAGE_ARTIFACTS == {name: dict(row.artifacts) for name, row in HARNESS.items()
                                              if row.image}
    assert harness_homes._SESSION_PATHS == {name: tuple(row.session.paths)
                                            for name, row in HARNESS.items() if row.session.paths}
    for name, row in HARNESS.items():
        assert BACKENDS[name] is row.spec
        assert terminal_tail.has_tail_reader(name) is bool(row.terminal and row.terminal.tail)


def test_opencode_terminal_facet_drives_actual_dispatch():
    from _agent_shared import surface_runner
    from _agent_shared.harnesses import HARNESS, Terminal
    from _agent_shared.terminal_runner import run_terminal
    assert HARNESS['opencode'].terminal == Terminal(
        'B', 'harnesses.opencode.backend:terminal_context', True, 'Agent · OpenCode',
        tail='terminal_tail:opencode_tail')
    assert surface_runner('opencode', 'terminal') is run_terminal


def test_a_native_provider_map_is_the_one_place_a_harness_renames_a_provider():
    from _agent_shared.harnesses import HARNESS
    from collimate.providers import PROVIDERS
    for row in HARNESS.values():
        for provider, native in row.auth.providers.items():
            assert provider in PROVIDERS and PROVIDERS[provider].api_key, (row.id, provider)
            assert native.id, (row.id, provider)
        for plan in row.auth.plans:
            assert row.native_provider("", plan).id
    with pytest.raises(ValueError, match="cannot bill provider 'xai'"):
        HARNESS["goose"].native_provider("xai")
    assert HARNESS["goose"].native_provider("gemini").key_env == "GOOGLE_API_KEY"
    assert HARNESS["opencode"].native_provider("copilot", "github_copilot").id == "github-copilot"


@pytest.mark.parametrize("harness,asked,held", [
    ("hermes", "ask", "deny"), ("hermes", "allow", "allow"), ("codex", "ask", "ask"),
])
def test_a_level_the_native_mechanism_cannot_hold_resolves_down(harness, asked, held):
    from _agent_shared.harnesses import HARNESS
    level, notice = HARNESS[harness].resolve_commands(asked)
    assert level == held
    assert bool(notice) is (asked != held)


def test_every_image_context_is_its_harness_folder():
    from _agent_shared.harnesses import HARNESS, image
    for name, row in HARNESS.items():
        found = image(row)
        if found is None:
            continue
        assert os.path.isfile(os.path.join(_ROOT.parent, found.folder, "Dockerfile")), name
        assert found.name_prefix == f"collimate-{name}"
