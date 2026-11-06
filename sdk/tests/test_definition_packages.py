# SPDX-License-Identifier: MIT
"""Definition discovery shares one package boundary contract."""
import importlib
import json
from pathlib import Path


def test_definition_discovery_matches_shared_boundaries__D04():
    definitions = importlib.import_module("collimate.definitions")
    fixture = json.loads((Path(__file__).resolve().parents[1] / "shared/definition-packages-contract.json").read_text())
    for case in fixture:
        found = definitions.discover_definitions(case["paths"])
        assert [(p.kind, p.definition_path, p.prefix) for p in found.packages] == [tuple(p) for p in case["packages"]], case["name"]
        assert [(p.code, p.path) for p in found.problems] == [tuple(p) for p in case["problems"]], case["name"]


def test_lint_uses_package_boundaries_and_explicit_target__D04(tmp_path):
    from collimate.lint import lint_path, Level

    package = tmp_path / "ordinary" / "package"
    package.mkdir(parents=True)
    definition = package / "arbitrary.generator"
    definition.write_text("id: declared-id\nname: Demo\nsummary: Demo.\nentrypoint: main.py\nmain_config: cfg.set_yaml\n")
    (package / "main.py").write_text("")
    (package / "cfg.set_yaml").write_text("fields: []\n")
    hidden = package / "examples" / "broken.renderer"
    hidden.parent.mkdir()
    hidden.write_text("broken: yaml\n")
    problems = lint_path(tmp_path)
    assert not [p for p in problems if p.level == Level.ERROR]
    assert not [p for p in lint_path(package) if p.level == Level.ERROR]
    assert not [p for p in lint_path(definition) if p.level == Level.ERROR]
    (package / "other.handler").write_text("id: barrier\n")
    assert {p.code for p in lint_path(package) if p.level == Level.ERROR} == {"definition-folder-ambiguous"}


def test_explicit_definition_refuses_package_ambiguity__D04(tmp_path):
    from collimate.lint import Level, lint_path
    definition = tmp_path / 'named.generator'
    definition.write_text('id: demo\nname: Demo\nsummary: Demo.\nentrypoint: main.py\nmain_config: cfg.set_yaml\n')
    (tmp_path / 'main.py').write_text('')
    (tmp_path / 'cfg.set_yaml').write_text('fields: []\n')
    (tmp_path / 'another.handler').write_text('id: barrier\n')
    assert {p.code for p in lint_path(definition) if p.level == Level.ERROR} == {'definition-folder-ambiguous'}


def test_explicit_runner_target_preserves_partial_visibility__D04(tmp_path):
    from collimate.lint import Level, lint_path
    package = tmp_path / 'selected'
    package.mkdir()
    definition = package / 'named.runner'
    definition.write_text('id: demo\nname: Demo\ngenerator: external\nsettings: {}\n')
    assert not [p for p in lint_path(definition) if p.level == Level.ERROR]
    assert not [p for p in lint_path(package) if p.level == Level.ERROR]
    assert {p.code for p in lint_path(tmp_path) if p.level == Level.ERROR} == {'runner-generator'}
