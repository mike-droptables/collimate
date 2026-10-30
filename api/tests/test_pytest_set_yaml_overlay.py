"""Unit coverage for api/set_yaml.py — the shared empty-value rule,
.set_yaml resolution, the condition-aware run gate, and the
comment-preserving YAML splice.

This is the file-level .set_yaml machinery shared by the runner catalog, the
run path, and runner editing. These pin the contract mirrored across the SDK
lint, schema_form's validate, and frontend setYaml.ts: the per-type "empty"
definition, the resolution layering (per-type empties ⊕ field defaults ⊕ values
block ⊕ overlay), and the splice safety reparse. Synthetic fixtures only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from collimate.config_values import is_empty, parse_set_yaml, resolve_form_values
from set_yaml import (
    config_violations,
    overlay_set_yaml_values,
    splice_top_level_yaml_block,
)


# ---------------------------------------------------------------------------
# is_empty — the single empty-value definition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,ftype,expected", [
    # text/textarea: null | "" (trimmed)
    (None, "text", True),
    ("", "text", True),
    ("   ", "text", True),
    ("x", "text", False),
    ("0", "text", False),
    (None, "textarea", True),
    ("  \n ", "textarea", True),
    # number: null only — 0 is a value
    (None, "number", True),
    (0, "number", False),
    (0.0, "number", False),
    (42, "number", False),
    # dropdown: null | "" (NOT trimmed)
    (None, "dropdown", True),
    ("", "dropdown", True),
    (" ", "dropdown", False),
    ("live", "dropdown", False),
    # checkbox: never empty — False is a value
    (None, "checkbox", False),
    (False, "checkbox", False),
    (True, "checkbox", False),
    # list: null | []
    (None, "list", True),
    ([], "list", True),
    ([0], "list", False),
    # object: null only
    (None, "object", True),
    ({}, "object", False),
    # unknown/missing type falls back to text semantics
    (None, None, True),
    ("  ", None, True),
    ("x", None, False),
    (0, None, False),
])
def test_is_empty_matrix(value, ftype, expected):
    assert is_empty(value, ftype) is expected


# ---------------------------------------------------------------------------
# parse_set_yaml — resolution layering
# ---------------------------------------------------------------------------

SET_YAML = """\
# a column-zero comment
fields:
  - key: model
    type: text
    label: Model
    default: base-model
    required: true
  - key: max_tokens
    type: number
    default: 4096
  - key: note
    type: text
  - key: verbose
    type: checkbox
values:
  max_tokens: 1024
"""


def test_parse_set_yaml_layers_empties_defaults_and_values():
    fields, resolved = resolve_form_values(parse_set_yaml(SET_YAML))
    assert [f["key"] for f in fields] == ["model", "max_tokens", "note", "verbose"]
    assert resolved == {
        "model": "base-model",   # field default
        "max_tokens": 1024,      # values block beats field default
        "note": None,            # declared, no default → per-type empty
        "verbose": False,        # checkbox base is False, not None
    }


def test_parse_set_yaml_includes_undeclared_values_keys():
    # Like resolve_generator_values, explicit values keep undeclared keys.
    _, resolved = resolve_form_values(parse_set_yaml("fields: []\nvalues:\n  loose: 1\n"))
    assert resolved == {"loose": 1}


@pytest.mark.parametrize("text", ["", "- just\n- a list\n", "::: not yaml :::"])
def test_parse_set_yaml_tolerates_malformed(text):
    assert resolve_form_values(parse_set_yaml(text)) == ([], {})


def test_config_violations_reports_label_and_skips_checkbox():
    text = """\
fields:
  - key: model
    type: text
    label: Model
    required: true
  - key: confirm
    type: checkbox
    required: true
"""
    fields, resolved = resolve_form_values(parse_set_yaml(text))
    missing, invalid = config_violations("knobs.set_yaml", fields, resolved)
    # checkbox is never empty, so only `model` blocks
    assert missing == [{"file": "knobs.set_yaml", "key": "model", "label": "Model"}]
    assert invalid == []


# ---------------------------------------------------------------------------
# config_violations — the condition-aware gate, pinned to the same
# cross-language fixture the SDK evaluator tests use. Reached through the
# installed collimate package (the sdk/ workspace member, editable in dev
# and CI); skipped (not failed) when it does not have the fixture.
# ---------------------------------------------------------------------------

_CONDITIONS_NAME = "set-yaml-conditions-contract.json"


def _find_conditions() -> Path | None:
    try:
        from collimate.shared import shared_path

        path = shared_path(_CONDITIONS_NAME)
        if path.is_file():
            return path
    except (ImportError, FileNotFoundError):
        pass
    return None


_CONDITIONS_PATH = _find_conditions()
_HAS_CONDITIONS = _CONDITIONS_PATH is not None


def _conditions() -> dict:
    return json.loads(_CONDITIONS_PATH.read_text(encoding="utf-8"))


def _gate_cases() -> list[dict]:
    return _conditions()["gate_cases"] if _HAS_CONDITIONS else []


def _option_cases() -> list[dict]:
    return _conditions()["option_cases"] if _HAS_CONDITIONS else []


def _resolve_fields(fields: list[dict], overlay: dict) -> dict:
    """Layer per-type empty base ⊕ field default ⊕ overlay — the same map
    the SDK's ``resolve_fields`` builds, so a JSON fixture row (bare
    ``fields`` + ``values``) reaches ``config_violations`` in the resolved
    shape ``parse_set_yaml`` would produce for the equivalent ``.set_yaml``."""
    from collimate.config_values import resolve_fields

    return resolve_fields(fields, overlay)


@pytest.mark.skipif(not _HAS_CONDITIONS, reason="conditions contract fixture absent")
@pytest.mark.parametrize("row", _gate_cases(), ids=lambda r: r["note"][:48])
def test_config_violations_gate_cases(row):
    """blocking_empties from the fixture must equal config_violations'
    missing_required (visibility-first: hidden/transitively-hidden/cyclic
    required fields never block), each entry carrying the file + key + label."""
    fields = row["fields"]
    resolved = _resolve_fields(fields, row["values"])
    missing, _invalid = config_violations("gate.set_yaml", fields, resolved)
    assert [m["key"] for m in missing] == row["blocking_empties"]
    for m in missing:
        assert m["file"] == "gate.set_yaml"
        assert set(m) == {"file", "key", "label"}


@pytest.mark.skipif(not _HAS_CONDITIONS, reason="conditions contract fixture absent")
@pytest.mark.parametrize("row", _option_cases(), ids=lambda r: r["note"][:48])
def test_config_violations_option_cases(row):
    """invalid_options from the fixture must equal config_violations'
    invalid_options (a VISIBLE dropdown whose resolved value selects a
    currently-disabled option), each entry carrying the file + {key, value}."""
    fields = row["fields"]
    overlay = row.get("values") or row.get("values_raw")
    resolved = _resolve_fields(fields, overlay)
    _missing, invalid = config_violations("opt.set_yaml", fields, resolved)
    got = [{"key": o["key"], "value": o["value"]} for o in invalid]
    assert got == row["invalid_options"]
    for o in invalid:
        assert o["file"] == "opt.set_yaml"
        assert set(o) == {"file", "key", "label", "value"}


# ---------------------------------------------------------------------------
# overlay_set_yaml_values — how a relay tag's settings overlay lands in a
# .set_yaml file's values: block (the editor's settings_files projection).
# ---------------------------------------------------------------------------

def test_overlay_merges_values_and_preserves_comments():
    out = overlay_set_yaml_values(SET_YAML, {"model": "tag-model"})
    assert out.startswith("# a column-zero comment\n")
    parsed = yaml.safe_load(out)
    # overlay merged ON TOP of the existing values block
    assert parsed["values"] == {"max_tokens": 1024, "model": "tag-model"}
    # fields untouched
    assert parsed["fields"] == yaml.safe_load(SET_YAML)["fields"]


def test_overlay_appends_values_block_when_absent():
    text = "# header\nfields:\n  - key: a\n    type: text\n"
    out = overlay_set_yaml_values(text, {"a": "x"})
    assert out.startswith("# header\n")
    assert yaml.safe_load(out)["values"] == {"a": "x"}


def test_overlay_noop_when_overlay_empty():
    assert overlay_set_yaml_values(SET_YAML, {}) == SET_YAML


def test_overlay_raises_on_unparseable_text():
    with pytest.raises(ValueError):
        overlay_set_yaml_values("::: not yaml :::", {"a": 1})


# ---------------------------------------------------------------------------
# splice_top_level_yaml_block — comment-preserving replace of a top-level
# block (the relay CRUD splices the ``tags:`` block this way).
# ---------------------------------------------------------------------------

MANIFEST = """\
id: web-capture
# keep-me: column-zero comment between keys
tags:
  default: {}
  old:
    label: "x"

description: a relay
"""


def test_splice_replaces_block_and_preserves_other_lines():
    out = splice_top_level_yaml_block(
        MANIFEST, "tags", {"default": {}, "new": {"label": "y"}},
    )
    assert "# keep-me: column-zero comment between keys" in out
    assert "id: web-capture" in out
    parsed = yaml.safe_load(out)
    assert parsed["tags"] == {"default": {}, "new": {"label": "y"}}
    assert parsed["description"] == "a relay"
    # the column-0 comment ends the block scan — it must still precede
    # the spliced block, and the blank separator line must survive
    assert out.index("# keep-me") < out.index("tags:")
    assert "\n\ndescription:" in out


def test_splice_appends_when_key_absent():
    text = "id: g\n# trailing comment\n"
    out = splice_top_level_yaml_block(text, "tags", {"default": {}})
    assert out.startswith("id: g\n# trailing comment\n")
    assert yaml.safe_load(out)["tags"] == {"default": {}}


def test_splice_rejects_non_mapping_document():
    with pytest.raises(ValueError):
        splice_top_level_yaml_block("- a\n- b\n", "tags", {})
    with pytest.raises(ValueError):
        splice_top_level_yaml_block("{ broken: [", "tags", {})


def test_app_form_values_follow_shared_sdk_resolution_fixture__F1225():
    import collimate
    cases = json.loads((Path(collimate.__file__).resolve().parents[2] /
                        'shared/set-yaml-resolution-contract.json').read_text())
    for case in cases:
        assert resolve_form_values(parse_set_yaml(case['text']))[1] == case['form'], case['name']
