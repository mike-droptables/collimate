"""Shipped definition files preserve package-relative executable metadata."""

import yaml

from collimate.definitions import discover_definitions
from tests._workspace import BUILTINS, GENERATORS, RENDERERS


def test_shipped_packages_have_named_definition_files__D04():
    counts = {}
    for tree, suffix in [(GENERATORS, ".generator"), (RENDERERS, ".renderer"), (BUILTINS, ".renderer")]:
        definitions = sorted(tree.glob(f"*/*/*{suffix}"))
        counts[tree.name] = len(definitions)
        for definition in definitions:
            data = yaml.safe_load(definition.read_text())
            assert isinstance(data["id"], str) and data["id"]
            relative = definition.relative_to(tree).as_posix()
            discovery = discover_definitions([relative])
            assert discovery.problems == ()
            assert [(item.kind, item.definition_path) for item in discovery.packages] == [
                (suffix.removeprefix("."), relative)]
            assert (definition.parent / data["entrypoint"]).is_file()
    assert counts == {"generators": 4, "renderers": 12, "builtins": 7}
