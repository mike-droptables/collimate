"""The settings YAML module exposes the shared overlay implementation."""

import importlib

import yaml


def test_set_yaml_module_executes_public_overlay__F0058():
    settings = importlib.import_module("set_yaml")
    original = """# settings preface
fields:
  - key: model
    type: text
    required: true
values:
  model: baseline
  untouched: 7
# preserved tail
label: Settings
"""
    merged = settings.overlay_set_yaml_values(original, {"model": "selected"})
    document = yaml.safe_load(merged)
    assert document["values"] == {"model": "selected", "untouched": 7}
    assert document["fields"] == yaml.safe_load(original)["fields"]
    assert merged.startswith(original.split("values:\n")[0])
    assert merged.endswith("# preserved tail\nlabel: Settings\n")
    assert settings.config_violations(
        "main.set_yaml", document["fields"], document["values"],
    ) == ([], [])
    assert settings.overlay_set_yaml_values(original, {}) == original
