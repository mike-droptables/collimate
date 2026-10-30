# SPDX-License-Identifier: MIT
"""The app's built-in cells: exactly the handlers and renderers the app's
own code names, and nothing seedable."""
from __future__ import annotations

from pathlib import Path

import yaml

from collimate.definitions import discover_definitions
from collimate.handlers import RESERVED_TYPES, validate_handler
from collimate._host.packaging import read_asset_cells

from tests._workspace import BUILTINS, RENDERERS


BUILTIN = BUILTINS


def _packages(root: Path, kind: str) -> dict[str, dict]:
    """``{id: manifest}`` of every ``kind`` package under an asset tree."""
    out = {}
    for cell in sorted(p for p in root.iterdir() if p.is_dir()):
        paths = [p.relative_to(cell).as_posix() for p in cell.rglob("*") if p.is_file()]
        found = discover_definitions(paths)
        assert not found.problems, found.problems
        for package in found.packages:
            if package.kind == kind:
                manifest = yaml.safe_load((cell / package.definition_path).read_text())
                assert manifest["id"] not in out, manifest["id"]
                out[manifest["id"]] = manifest
    return out


def test_builtin_holds_exactly_the_seven_renderer_ids():
    assert set(_packages(BUILTIN, "renderer")) == {
        "renderer_example", "text", "run", "assistant", "terminal", "website", "webview"}


def test_builtin_holds_the_six_handlers_and_renderers_holds_the_llm_summary_handler():
    """Built-in is what the app's own code names, plus git-checkout (the Git
    checkout record), built in by the owner's decision: without the
    abstraction git is a landmine for users, who use git often; the
    `.llm_summary` type the Agent writes on request ships in the seedable
    Documents cell."""
    assert set(_packages(BUILTIN, "handler")) == {
        "generator", "renderer", "runner", "handler", "options", "git-checkout"}
    assert set(_packages(RENDERERS, "handler")) == {"llm-summary"}


def test_reserved_types_are_the_union_of_the_builtin_handlers_types():
    declared = {kind["ext"] for manifest in _packages(BUILTIN, "handler").values()
                for kind in validate_handler(manifest, allow_reserved=True)["types"]}
    assert declared == RESERVED_TYPES


def test_builtin_and_seedable_renderer_ids_are_disjoint():
    assert not set(_packages(BUILTIN, "renderer")) & set(_packages(RENDERERS, "renderer"))


def test_builtin_reads_as_three_cells():
    cells = read_asset_cells(BUILTIN, "renderer")
    assert [(c.name, c.icon) for c in cells] == [
        ("Definitions", "braces"), ("Live", "radio"), ("Text", "file-text")]


def test_the_handler_handler_skills_are_its_machinery_and_its_authoring_skill():
    """The ``handler`` built-in carries its authoring skill and the three
    briefs the app hands a work cell: build, regenerate and create."""
    skills = BUILTIN / "definitions/handler/derived/skills"
    assert {p.name for p in skills.iterdir() if p.is_dir()} == {
        "build", "regenerate", "create", "handler-authoring"}
    for name in ("build", "regenerate", "create", "handler-authoring"):
        text = (skills / name / "SKILL.md").read_text()
        front = yaml.safe_load(text.split("---\n", 2)[1])
        assert front["name"] == name and front["description"], name
    assert not (skills / "create/references").exists()


def test_the_work_cell_briefs_name_paths_inside_the_package_folder():
    """The build and regenerate briefs write only under the package folder
    the YAML block at the top of brief.md names, and every brief ends by
    leaving its work for the user to add from the work cell's Files panel."""
    skills = BUILTIN / "definitions/handler/derived/skills"
    build = (skills / "build/SKILL.md").read_text()
    assert "`<package>/derived/verify.ts` and `<package>/derived/skills/**`" in build
    assert 'validate_file(path, verifier="<package>/derived/verify.ts")' in build
    assert "**Add to Capabilities** (Replace)" in build
    regenerate = (skills / "regenerate/SKILL.md").read_text()
    assert "Write only `<package>/derived/skills/**`" in regenerate
    assert "`build`, `regenerate` and `create` skills" in regenerate
    assert "**Use these skills**" in regenerate
    create = (skills / "create/SKILL.md").read_text()
    for key in ("`title`", "`make`", "`answers`", "`related`"):
        assert key in create, key
    assert "`guides/<kind>-authoring/SKILL.md`" in create
    assert "a generator\nbefore the runners that run it" in create
    for text in (build, regenerate, create):
        assert "Files\npanel" in text or "Files panel" in text
        flat = " ".join(text.split())
        assert "The YAML block at the top of brief.md" in flat
        assert "front matter" not in flat
