# SPDX-License-Identifier: MIT
"""The Agent cell's shipped generator: one ``agent`` over the shared runtime
(``_agent_shared/session``), whose ``surface`` setting picks Annotation (the
default), Chat or CLI (the terminal).

These pin the layout, the entrypoint, the union of the surfaces' UI files,
the presets (exactly nineteen, uniquely named, each listing every field),
the runner drafts, the terminal's conditional container contract, the
annotation default, the surface files the manifest opens, the package's
acceptance by the generator verifier, and the Guide preset's generated
mirror.
"""
from __future__ import annotations

import runpy
from pathlib import Path

import pytest
import yaml

from tests._workspace import GENERATORS


ROOT = Path(__file__).resolve().parents[1]
CELL = GENERATORS / "agent"
PACKAGE = CELL / "agent"

MAIN_CONFIG = "agent-config.set_yaml"
HARNESS_PRESETS = {"default": ("Agent", "pydantic-ai"), "claude-code": ("Claude Code", "claude-code"),
                   "antigravity": ("Antigravity", "antigravity"), "codex": ("Codex", "codex"),
                   "goose": ("goose", "goose"), "opencode": ("opencode", "opencode"),
                   "qwen-code": ("Qwen Code", "qwen-code"), "cline": ("Cline", "cline"), "pi": ("pi", "pi"),
                   "hermes": ("Hermes", "hermes"), "vibe": ("Mistral Vibe", "vibe"),
                   "copilot": ("GitHub Copilot CLI", "copilot")}
ONESHOT_PRESETS = ("axes-first", "challenge", "evaluate-best", "extract", "modify-files",
                   "seed-ideas", "summarize", "translate")
CHAT_PRESETS = ("guide", "superpowers")


def _manifest() -> dict:
    return yaml.safe_load((PACKAGE / "agent.generator").read_text())


def _fields() -> list:
    return yaml.safe_load((PACKAGE / MAIN_CONFIG).read_text())["fields"]


def _preset(preset: str) -> dict:
    return yaml.safe_load((PACKAGE / "configs" / preset / "manifest.config_yaml").read_text())


# ── layout ─────────────────────────────────────────────────────────────


def test_the_agent_cell_holds_the_one_generator_GN02():
    """The one generator package beside the two runner packages a
    suggestion offers as drafts (one per surface it starts on)."""
    assert sorted(p.name for p in CELL.iterdir()) == ["agent", "agent-annotation", "agent-chat",
                                                      "cell.yaml"]
    assert [p.name for p in CELL.rglob("*.generator")] == ["agent.generator"]
    cell = yaml.safe_load((CELL / "cell.yaml").read_text())
    assert cell["name"] == "Agent" and cell["shared"] == ["_env_shared", "_agent_shared"]


def test_the_package_is_its_generator_GN02():
    """The package folder is the generator id, its entrypoint the id's
    ``.py`` and its main config the shared name; it ships the config-folder
    samples."""
    manifest = _manifest()
    assert (manifest["id"], manifest["name"]) == ("agent", "Agent")
    assert manifest["entrypoint"] == "agent.py"
    assert manifest["main_config"] == MAIN_CONFIG
    assert manifest["draft_files"] == [MAIN_CONFIG]
    assert manifest["chat"] is True and manifest["enforces"] == ["commands"]
    assert manifest["live_files_mode"] == "manual"
    assert "parameters" not in manifest
    assert {p.name for p in PACKAGE.iterdir() if p.name != "__pycache__"} == {
        "agent.generator", "agent.py", MAIN_CONFIG, "configs", "samples"}
    for entry in manifest["configs"]:
        assert (PACKAGE / entry["path"]).is_file(), entry
    assert {e["id"] for e in manifest["configs"]} == {p.name for p in (PACKAGE / "configs").iterdir()}
    assert {f["name"]: f["sample"] for f in manifest["config_folders"]} == {
        "_skills": "samples/_skills", "_mcp": "samples/_mcp"}


def test_the_ui_files_are_the_union_of_the_surfaces_GN02():
    """The chat's pointer and transcripts, and the terminal's pointer; an
    annotation run has none."""
    assert _manifest()["ui_files"] == [".chat", ".json", ".terminal"]


def test_the_entrypoint_runs_the_session_with_its_package_folder_GN02(monkeypatch):
    """Run as a script, the entrypoint hands ``gen.main`` its top-level
    ``run`` (the generator contract, which ``collimate.testing.run_generator``
    reads too), and ``run`` runs the shared ``session.run`` with its package
    folder alone: the surface is the run's setting."""
    import asyncio

    import gen
    from _agent_shared import session

    started: list = []
    monkeypatch.setattr(gen, "main", started.append)
    entry = PACKAGE / "agent.py"
    namespace = runpy.run_path(str(entry), run_name="__main__")
    (run,) = started
    assert run is namespace["run"]
    calls: list = []

    async def recorded(*args, **kwargs):
        calls.append((args, kwargs))
    monkeypatch.setattr(session, "run", recorded)
    asyncio.run(run())
    assert calls == [((), {"here": str(entry.parent)})]


# ── presets ────────────────────────────────────────────────────────────


def test_the_twenty_two_presets_GN02():
    """One per harness on Annotation (generated from the harness adapters),
    the eight one-shots as one-shot annotation runs, and Guide and
    Superpowers on the chat; no two share a name."""
    configs = _manifest()["configs"]
    names = [entry["name"] for entry in configs]
    assert len(names) == len(set(names)) == 22, names
    assert [entry["id"] for entry in configs] == [*HARNESS_PRESETS, *ONESHOT_PRESETS, *CHAT_PRESETS]
    for entry in configs:
        assert _preset(entry["id"])["name"] == entry["name"], entry


@pytest.mark.parametrize("preset", [*HARNESS_PRESETS, *ONESHOT_PRESETS, *CHAT_PRESETS])
def test_each_preset_lists_every_field_in_schema_order_GN02(preset):
    settings = _preset(preset)["settings"]
    assert list(settings) == [MAIN_CONFIG]
    assert list(settings[MAIN_CONFIG]) == [f["key"] for f in _fields()]
    assert settings[MAIN_CONFIG]["selected_file_first"] is False


@pytest.mark.parametrize("preset", HARNESS_PRESETS)
def test_a_harness_preset_is_an_interactive_annotation_run_GN02(preset):
    name, backend = HARNESS_PRESETS[preset]
    data = _preset(preset)
    values = data["settings"][MAIN_CONFIG]
    assert data["name"] == name
    assert (values["surface"], values["session"], values["backend"]) == (
        "annotation", "interactive", backend)


def test_the_harness_presets_keep_their_permissions_GN02():
    """The in-process engine and Claude Code's adapter run on this machine
    (docker: none, commands deny); the container harnesses run with docker: full and
    may run commands."""
    for preset, (_name, backend) in HARNESS_PRESETS.items():
        data = _preset(preset)
        if backend in ("pydantic-ai", "claude-code"):
            assert data["permissions"] == {"docker": "none", "internet": "full", "commands": "deny"}
        else:
            assert data["permissions"] == {"docker": "full", "internet": "full", "commands": "allow"}
    # Every artifact floats to its latest release unless a runner pins it,
    # so no preset names one.
    assert all(_preset(preset)["packages"] == {} for preset in HARNESS_PRESETS)


@pytest.mark.parametrize("preset", ONESHOT_PRESETS)
def test_a_oneshot_preset_is_a_oneshot_annotation_run_GN02(preset):
    values = _preset(preset)["settings"][MAIN_CONFIG]
    assert (values["surface"], values["session"]) == ("annotation", "oneshot")
    assert (PACKAGE / "configs" / preset / "files" / "_skills").is_dir()


@pytest.mark.parametrize("preset", CHAT_PRESETS)
def test_guide_and_superpowers_are_chats_GN02(preset):
    values = _preset(preset)["settings"][MAIN_CONFIG]
    assert (values["surface"], values["session"]) == ("chat", "interactive")
    assert (PACKAGE / "configs" / preset / "files" / "_skills").is_dir()


@pytest.mark.parametrize("draft, surface", [("agent-annotation", "annotation"),
                                            ("agent-chat", "chat")])
def test_the_runner_drafts_are_annotation_and_chat_and_list_every_field_GN02(draft, surface):
    """The two runner drafts a suggestion offers: an interactive Annotation
    run and a Chat, each listing every field in schema order, both on the
    in-process engine with nothing selected."""
    data = yaml.safe_load((CELL / draft / f"{draft}.runner").read_text())
    assert (data["id"], data["generator"]) == (draft, "agent")
    values = data["settings"][MAIN_CONFIG]
    assert list(values) == [f["key"] for f in _fields()]
    assert (values["surface"], values["session"], values["backend"]) == (
        surface, "interactive", "pydantic-ai")
    assert values["selected_file_first"] is False and data["prompt"] == ""


# ── the terminal's container and the annotation default ───────────────


def test_only_a_container_cli_requires_docker_GN02():
    """The CLI surface of a harness whose CLI runs in its container on every
    account needs docker: full (the runner lint's ``runner-docker-required``);
    Claude Code's and Antigravity's CLI can run on this machine, so a runner
    switched to theirs does not. No preset is on the terminal."""
    from collimate.harness_catalog import HARNESSES
    from collimate.lint import GeneratorRef, lint_runner_manifest_data
    manifest = _manifest()
    containers = [name for name, row in HARNESSES.items()
                  if row["terminal_locus"] and set(row["terminal_locus"].values()) == {"container"}]
    assert manifest["requires_docker_if"] == {"surface": "terminal", "backend": containers}
    assert "claude-code" not in containers and "antigravity" not in containers and "codex" in containers
    assert "requires_docker" not in manifest
    ref = GeneratorRef.from_manifest(manifest, read_file=lambda rel: (PACKAGE / rel).read_text())

    def codes(preset_id, **over):
        preset = _preset(preset_id)
        runner = {"id": "r", "name": "R", "description": "", "generator": "agent", "tags": [],
                  "prompt": "", "settings": {MAIN_CONFIG: {**preset["settings"][MAIN_CONFIG], **over}},
                  "folders": [], "accounts": {}, "permissions": preset["permissions"],
                  "packages": preset["packages"]}
        return {i.code for i in lint_runner_manifest_data(runner, resolve=lambda _id: ref)}

    assert "runner-docker-required" not in codes("claude-code")
    assert "runner-docker-required" not in codes("claude-code", surface="terminal")
    assert "runner-docker-required" not in codes("codex")
    preset = _preset("codex")
    runner = {"id": "r", "name": "R", "description": "", "generator": "agent", "tags": [],
              "prompt": "", "settings": {MAIN_CONFIG: {**preset["settings"][MAIN_CONFIG], "surface": "terminal"}},
              "folders": [], "accounts": {}, "permissions": {**preset["permissions"], "docker": "none"},
              "packages": preset["packages"]}
    assert "runner-docker-required" in {i.code for i in lint_runner_manifest_data(runner, resolve=lambda _id: ref)}


def test_annotation_is_the_default_surface_GN02():
    """Annotation is the default surface (labelled Annotation / Chat / CLI);
    its session setting shows there alone; the retired target and handoff
    settings are gone."""
    fields = {f["key"]: f for f in _fields()}
    assert fields["surface"]["default"] == "annotation"
    assert [o["label"] for o in fields["surface"]["options"]] == ["Annotation", "Chat", "CLI"]
    assert fields["session"]["show_if"] == {"surface": "annotation"}
    assert list(fields) == ["surface", "session", "backend", "system_prompt", "mcp", "approvals",
                            "selected_file_first"]


def test_agent_declares_optional_git_role():
    """The optional ``git`` role carries the Git account into the harness; no role is of a repository kind."""
    roles = _manifest()["account_requirements"]
    assert roles["git"] == {'label': 'Git account', 'kind': 'git', 'capabilities': ['git'], 'required': False}
    assert all(role.get("kind") != "repository" for role in roles.values())
    assert "repository" not in roles
    fields = {f["key"]: f for f in _fields()}
    assert "repositories" not in fields


@pytest.mark.parametrize("values, expected", [
    ({}, []),
    ({"session": "oneshot"}, []),
    ({"surface": "chat"}, ["_runs/agent-1a2b3c4d/chat/conversation.chat"]),
    ({"surface": "terminal", "backend": "claude-code"}, ["_runs/agent-1a2b3c4d/terminal/claude-code.terminal"]),
])
def test_the_manifest_opens_the_surface_file_GN03(values, expected):
    from collimate.config_values import resolve_fields
    from collimate.surfaces import resolve_opens
    fields = _fields()
    assert resolve_opens(_manifest(), fields, resolve_fields(fields, values),
                         folder="_runs/agent-1a2b3c4d") == expected


def test_the_selected_file_is_taken_only_when_the_runner_asks_GN01():
    from collimate.config_values import compute_visibility, matches, resolve_fields, type_index
    fields = _fields()
    condition = _manifest()["takes_selected_file_if"]

    def takes(values):
        resolved = resolve_fields(fields, values)
        return matches(condition, resolved, compute_visibility(fields, resolved), type_index(fields))

    assert not takes({})
    assert takes({"selected_file_first": True})
    assert takes({"surface": "chat", "selected_file_first": True})
    assert takes({"surface": "terminal", "backend": "claude-code", "selected_file_first": True})
    assert not takes({"surface": "terminal", "backend": "codex", "selected_file_first": True})


# ── acceptance and the Guide's mirror ──────────────────────────────────


def test_shipped_agent_passes_accepted_generator_verifier_P4F17():
    from tests_live import conftest as live
    package = {path.relative_to(PACKAGE).as_posix(): path.read_text()
               for path in PACKAGE.rglob('*') if path.is_file()
               and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    _manifest_, verifier, _skills = next(record for record in live._accepted_handler_records()
                                         if record[0]['id'] == 'generator')
    code = live._compiled_accepted_verifier(verifier, (ROOT / 'shared/verify/prelude.js').read_bytes())
    path = 'agent.generator'
    verdict = live._watched_verdict(code, {'path': path, 'text': package[path],
                                           'package': package, 'cell_paths': list(package)})
    assert verdict['valid'], verdict


def test_the_guide_preset_mirrors_its_two_references():
    """The Guide is a chat preset; its references are the generated mirror
    of the canonical overview and philosophy guides."""
    references = PACKAGE / "configs/guide/files/_skills/collimate-guide/references"
    assert sorted(p.name for p in references.iterdir()) == ["collimate-overview.md", "why-collimate.md"]
    for path in references.iterdir():
        assert path.read_bytes() == (ROOT / "shared/docs" / path.name).read_bytes(), path.name
