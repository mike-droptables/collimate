"""collimate.config_values against the cross-language contract fixture.

``shared/empty-values-contract.json`` is the single table pinning the
per-field-type "empty value" semantics across the Python SDK and its
TypeScript mirrors (the frontend's setYaml tests load the same file).
Synthetic cases only — no shipped generator names.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from collimate.config_values import (
    UNKNOWN,
    canon,
    compute_visibility,
    effective_required,
    enabled_options,
    field_value_errors,
    field_visible,
    is_empty,
    matches,
    resolve_fields,
    type_empty_default,
    validate_config,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "shared" / "empty-values-contract.json"
_CONDITIONS = Path(__file__).resolve().parents[1] / "shared" / "set-yaml-conditions-contract.json"

_FIELD_TYPES = ("text", "textarea", "number", "checkbox", "dropdown", "list", "object")


def _cases() -> list[dict]:
    return json.loads(_CONTRACT.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize(
    "case", _cases(), ids=lambda c: f"{c['type']}-{c['value']!r}"
)
def test_is_empty_matches_contract(case):
    assert is_empty(case["value"], case["type"]) is case["empty"]


def test_contract_covers_every_field_type():
    covered = {c["type"] for c in _cases()}
    assert set(_FIELD_TYPES) <= covered
    # Plus at least one row pinning the unknown-type text fallback.
    assert covered - set(_FIELD_TYPES)


def test_type_empty_default_reads_empty_except_checkbox():
    for ftype in _FIELD_TYPES:
        assert is_empty(type_empty_default(ftype), ftype) is (ftype != "checkbox")
    assert type_empty_default("checkbox") is False
    assert type_empty_default("text") is None
    assert type_empty_default(None) is None


# --------------------------------------------------------------------------- #
# show_if / required_if / enable_if — pinned by set-yaml-conditions-contract.json
# --------------------------------------------------------------------------- #


def _conditions() -> dict:
    return json.loads(_CONDITIONS.read_text(encoding="utf-8"))


def _eval_cases() -> list[dict]:
    return _conditions()["eval_cases"]


def _gate_cases() -> list[dict]:
    return _conditions()["gate_cases"]


def _option_cases() -> list[dict]:
    return _conditions()["option_cases"]


@pytest.mark.parametrize(
    "row", _eval_cases(), ids=lambda r: json.dumps(r["cond"], sort_keys=True)
)
def test_eval_cases(row):
    """matches() against every eval row. This is the SDK/server surface, so
    we feed the raw domain (``values`` or ``values_raw``); ``values_renderer``
    rows still run through their ``values_raw``."""
    field_types = _conditions()["field_types"]
    resolved = row.get("values", row.get("values_raw"))
    hidden = set(row.get("referent_hidden", []))
    # visible over ALL declared fields (not the cond's keys — a v2 combinator's
    # key is any/all/not, so its real referents live nested); hidden ones off.
    visible = {k: (k not in hidden) for k in field_types}
    assert matches(row["cond"], resolved, visible, field_types) is row["match"]


@pytest.mark.parametrize(
    "row", _gate_cases(), ids=lambda r: r["note"][:48]
)
def test_gate_cases(row):
    fields = row["fields"]
    resolved = resolve_fields(fields, row["values"])
    report = validate_config(fields, resolved)
    assert [m["key"] for m in report["missing_required"]] == row["blocking_empties"]


@pytest.mark.parametrize(
    "row", _option_cases(), ids=lambda r: r["note"][:48]
)
def test_option_cases(row):
    fields = row["fields"]
    overlay = row.get("values") or row.get("values_raw")
    resolved = resolve_fields(fields, overlay)
    field_types = {f["key"]: f.get("type") for f in fields}
    visible = compute_visibility(fields, resolved)
    for key, expected in row["enabled"].items():
        field = next(f for f in fields if f["key"] == key)
        assert enabled_options(field, resolved, visible, field_types) == expected
    report = validate_config(fields, resolved)
    got = [{"key": o["key"], "value": o["value"]} for o in report["invalid_options"]]
    assert got == row["invalid_options"]


def test_validate_config_report_shape():
    fields = [
        {"key": "name", "type": "text", "label": "Name", "required": True},
        {"key": "age", "type": "number", "min": 0, "max": 120},
    ]
    resolved = resolve_fields(fields, {"name": "", "age": 200})
    report = validate_config(fields, resolved)
    assert set(report) == {
        "ok",
        "visible",
        "effective_required",
        "missing_required",
        "invalid_options",
        "field_errors",
    }
    assert report["visible"] == {"name": True, "age": True}
    assert report["effective_required"] == ["name"]
    assert report["missing_required"] == [{"key": "name", "label": "Name"}]
    assert report["invalid_options"] == []
    assert [e["key"] for e in report["field_errors"]] == ["age"]  # 200 > max
    assert report["ok"] is False


def test_validate_config_ok_when_clean():
    fields = [
        {"key": "name", "type": "text", "required": True},
        {"key": "age", "type": "number", "min": 0, "max": 120},
    ]
    report = validate_config(fields, resolve_fields(fields, {"name": "Ada", "age": 30}))
    assert report["ok"] is True
    assert report["missing_required"] == []
    assert report["field_errors"] == []


def test_compute_visibility_cycle_safe():
    """A 2-cycle reaching the runtime unlinted resolves to all-hidden."""
    fields = [
        {"key": "a", "type": "dropdown", "show_if": {"b": "x"}},
        {"key": "b", "type": "dropdown", "show_if": {"a": "x"}},
    ]
    assert compute_visibility(fields, {"a": "x", "b": "x"}) == {"a": False, "b": False}


def test_compute_visibility_self_reference_hidden():
    fields = [{"key": "a", "type": "dropdown", "show_if": {"a": "x"}}]
    assert compute_visibility(fields, {"a": "x"}) == {"a": False}


def test_field_visible_convenience():
    fields = [
        {"key": "gate", "type": "checkbox"},
        {"key": "dep", "type": "text", "show_if": {"gate": True}},
    ]
    assert field_visible("dep", fields, {"gate": True, "dep": ""}) is True
    assert field_visible("dep", fields, {"gate": False, "dep": ""}) is False


def test_show_when_is_not_a_condition():
    """``show_when`` was removed outright — it is an unknown key like any
    other, so a field carrying only it is unconditionally visible."""
    fields = [
        {"key": "gate", "type": "dropdown", "options": ["on", "off"]},
        {"key": "dep", "type": "text", "show_when": {"gate": "on"}},
    ]
    assert compute_visibility(fields, {"gate": "off", "dep": ""})["dep"] is True


def test_effective_required_composes_or():
    fields = [
        {"key": "gate", "type": "dropdown", "options": ["a", "b"]},
        {"key": "x", "type": "text", "required": True},
        {"key": "y", "type": "text", "required_if": {"gate": "a"}},
    ]
    resolved = {"gate": "a", "x": "", "y": ""}
    visible = compute_visibility(fields, resolved)
    x = next(f for f in fields if f["key"] == "x")
    y = next(f for f in fields if f["key"] == "y")
    assert effective_required(x, fields, resolved, visible) is True  # static required
    assert effective_required(y, fields, resolved, visible) is True  # required_if fires
    resolved_b = {"gate": "b", "x": "", "y": ""}
    vis_b = compute_visibility(fields, resolved_b)
    assert effective_required(y, fields, resolved_b, vis_b) is False  # required_if off


def test_matches_safety_polarity():
    ft = {"a": "dropdown"}
    assert matches({}, {"a": "x"}, {"a": True}, ft) is False  # empty map
    assert matches(None, {}, {}, ft) is False  # non-dict
    assert matches({"a": "x"}, {}, {"a": True}, ft) is False  # absent referent -> UNKNOWN
    # v2 combinators: {any:[...]} is a real OR now, not a reserved no-op.
    assert matches({"any": [{"a": "x"}]}, {"a": "x"}, {"a": True}, ft) is True
    assert matches({"any": [{"a": "x"}], "a": "x"}, {"a": "x"}, {"a": True}, ft) is False  # mixed keys
    assert matches({"all": []}, {"a": "x"}, {"a": True}, ft) is False  # empty list


def test_field_value_errors_dropdown_membership():
    fields = [{"key": "m", "type": "dropdown", "options": ["a", "b"]}]
    resolved = {"m": "zzz"}
    visible = compute_visibility(fields, resolved)
    errs = field_value_errors(fields, resolved, visible)
    assert [e["key"] for e in errs] == ["m"]
    # An empty dropdown is not a membership error.
    assert field_value_errors(fields, {"m": ""}, {"m": True}) == []


def test_field_value_errors_number_bounds_tolerate_non_numeric_min_max():
    fields = [{"key": "n", "type": "number", "min": "1", "max": "10"}]
    assert field_value_errors(fields, {"n": 5}, {"n": True}) == []
    assert [e["error"] for e in field_value_errors(fields, {"n": 0}, {"n": True})] == ["must be >= 1"]
    for bad in ("abc", [1], True):
        odd = [{"key": "n", "type": "number", "min": bad, "max": bad}]
        assert field_value_errors(odd, {"n": 0}, {"n": True}) == []


def test_canon_spot_checks():
    assert canon(0, "number") == canon("0", "number") == canon(0.0, "number")
    assert canon("on", "checkbox") == "true"
    assert canon("off", "checkbox") == "false"
    assert canon(True, "checkbox") == "true"
    assert canon(1, "checkbox") == "true"
    assert canon(None, "dropdown") is UNKNOWN
    assert canon("", "dropdown") is UNKNOWN
    assert canon(True, "dropdown") == "true"  # YAML-1.1 bool-word fold
    assert canon("anything", "text") is UNKNOWN  # illegal referent type
    assert canon(UNKNOWN, "number") is UNKNOWN
    assert canon("", "number") is UNKNOWN
    assert canon("", "checkbox") is UNKNOWN
    assert canon("1.5", "number") == 1.5
