# SPDX-License-Identifier: MIT
"""Per-field-type "empty value" semantics for ``.set_yaml`` schema forms.

Public and stable — the single Python source of truth for what counts
as an *empty* field value. The SDK lint derives its
required-on-checkbox check from it and the app's ``api/set_yaml.py``
builds the required-field run gate on it. The TypeScript mirrors (the
schema_form renderer's ``validate`` and the frontend's ``setYaml.ts``)
are pinned to the same behavior by the cross-language fixture at
``shared/empty-values-contract.json`` — when the semantics change,
update that table first, then every implementation against it.
"""

from __future__ import annotations

__all__ = [
    "is_empty",
    "type_empty_default",
    "UNKNOWN",
    "canon",
    "matches",
    "type_index",
    "compute_visibility",
    "field_visible",
    "effective_required",
    "enabled_options",
    "invalid_option_violations",
    "field_value_errors",
    "validate_config",
    "resolve_fields",
    "parse_set_yaml",
    "resolve_generator_values",
    "resolve_form_values",
]

# Base value per field type before the defaults overlay. checkbox is
# False — never empty — everything else starts at None.
_EMPTY_BASE: dict = {"checkbox": False}


def type_empty_default(field_type):
    """The base value a field of ``field_type`` holds before any
    ``default:`` or ``values:`` overlay — the bottom layer of the value
    resolution formula. ``False`` for checkbox (a checkbox always holds
    a real value), ``None`` for everything else, so the base reads as
    empty per :func:`is_empty` for every type but checkbox.
    """
    return _EMPTY_BASE.get(field_type)


def is_empty(value, field_type) -> bool:
    """Single per-type "empty value" rule. ``0``/``False`` are NOT empty.

    text/textarea: ``None`` | ``""`` (trimmed) — number: ``None`` —
    dropdown: ``None`` | ``""`` — checkbox: never — list: ``None`` |
    ``[]`` — object: ``None``. Unknown/missing types get text
    semantics (matches schema_form's text fallback).
    """
    if field_type == "checkbox":
        return False
    if value is None:
        return True
    if field_type == "number":
        return False
    if field_type == "dropdown":
        return value == ""
    if field_type == "list":
        return isinstance(value, list) and len(value) == 0
    if field_type == "object":
        return False
    # text / textarea / anything else string-shaped
    return isinstance(value, str) and value.strip() == ""


# --------------------------------------------------------------------------- #
# Conditional field visibility & requirements (show_if / required_if / enable_if)
#
# The canonical Python evaluator for ``.set_yaml`` conditions — a plain FieldMap
# ``{field_key: scalar-or-list}`` (multiple keys AND, a list ORs within a field),
# or a v2 combinator ``{any:[...]}`` / ``{all:[...]}`` / ``{not: cond}`` evaluated
# with three-valued Kleene logic (see :func:`matches`). This module is the SDK
# source of truth; the TypeScript mirrors (frontend ``setYaml.ts``
# and the schema_form renderer) are pinned to the same behavior by
# ``shared/set-yaml-conditions-contract.json``. Stdlib-only on purpose — the
# app imports it. This module and its docstrings are the semantic reference.
# --------------------------------------------------------------------------- #


class _Unknown:
    """Sentinel for a referent value that is absent, empty, or hidden.

    Distinct from every legal canonical value. UNKNOWN never satisfies a
    match (three-valued logic); this is what makes hidden referents and
    reference cycles resolve to the safe polarity (hidden / not-required).
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNKNOWN"


#: Module-level singleton. Compare with ``is`` (``value is UNKNOWN``).
UNKNOWN = _Unknown()

# Truthy folds. checkbox and the dropdown YAML-1.1 bool-word fold are kept
# separate: a checkbox operand may be an int 1/0, a dropdown one never is.
_CHECKBOX_TRUE = frozenset({"true", "on", "yes", "1"})
_CHECKBOX_FALSE = frozenset({"false", "off", "no", "0"})
_BOOL_WORD_TRUE = frozenset({"on", "yes", "true"})
_BOOL_WORD_FALSE = frozenset({"off", "no", "false"})

_LEGAL_REFERENT_TYPES = frozenset({"checkbox", "number", "dropdown"})


def canon(value, field_type):
    """Type-aware canonicalization of a condition operand or referent value.

    Coerces ``value`` into the one canonical domain of ``field_type`` so the
    three ingestion surfaces (renderer ``String(v)``, frontend/server raw
    bool/number) compare equal. Returns :data:`UNKNOWN` for anything that
    cannot be a meaningful referent value.

    - ``UNKNOWN`` in → ``UNKNOWN``.
    - ``checkbox`` → ``"true"``/``"false"`` via truthy fold (bool; the
      strings true/false/on/off/yes/no/1/0 any case; ints 1/0);
      ``None``/``""``/unrecognized → ``UNKNOWN``.
    - ``number`` → canonical number (``0 == "0" == 0.0``, integral floats
      collapse to ``int``); ``""``/``None``/unparseable/bool → ``UNKNOWN``.
    - ``dropdown`` → YAML-1.1 bool-word fold then ``str().strip().lower()``;
      ``None``/``""`` → ``UNKNOWN`` (the dropdown type's empty value).
    - ``text``/``textarea``/``list``/``object``/unknown → ``UNKNOWN``: an
      illegal condition referent (lint catches authoring; runtime stays on
      the safe polarity by never matching).
    """
    if value is UNKNOWN:
        return UNKNOWN
    if field_type == "checkbox":
        return _checkbox_canon(value)
    if field_type == "number":
        return _number_canon(value)
    if field_type == "dropdown":
        return _enum_canon(value)
    # text / textarea / list / object / unknown: illegal referent type.
    return UNKNOWN


def _checkbox_canon(value):
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "":
            return UNKNOWN
        if s in _CHECKBOX_TRUE:
            return "true"
        if s in _CHECKBOX_FALSE:
            return "false"
        return UNKNOWN  # unrecognized token → safe polarity
    if isinstance(value, int):
        if value == 1:
            return "true"
        if value == 0:
            return "false"
        return UNKNOWN
    return UNKNOWN


def _number_canon(value):
    if value is None or isinstance(value, bool):
        return UNKNOWN
    if isinstance(value, (int, float)):
        n = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if s == "":
            return UNKNOWN
        try:
            n = float(s)
        except ValueError:
            return UNKNOWN
    else:
        return UNKNOWN
    # Collapse integral floats so 0 == "0" == 0.0 compare equal.
    return int(n) if n.is_integer() else n


def _enum_canon(value):
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value).strip()
    if s == "":
        return UNKNOWN
    low = s.lower()
    if low in _BOOL_WORD_TRUE:
        return "true"
    if low in _BOOL_WORD_FALSE:
        return "false"
    return low


def type_index(fields) -> dict:
    """Build the ``{field_key: declared_type}`` index conditions coerce
    against. Constructed once per visibility pass."""
    return {
        f["key"]: f.get("type")
        for f in fields
        if isinstance(f, dict) and "key" in f
    }


def _option_value_str(value) -> str:
    """Mirror the renderer's ``String(v)`` normalization of an option value:
    bool → lowercase word, ``None`` → ``""``, else ``str``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


_COMBINATORS = ("any", "all", "not")


def matches(cond, resolved, visible, field_types) -> bool:
    """Evaluate a set_yaml condition against a resolved value map.

    ``cond`` is a v1 FieldMap ``{field_key: scalar-or-list}`` (AND across keys,
    OR within a list) or a v2 combinator ``{any:[...]}`` / ``{all:[...]}`` /
    ``{not: cond}``. ``visible`` (a ``{key: bool}`` map, or ``None``) makes a
    not-yet-visible referent contribute :data:`UNKNOWN`; ``field_types`` picks
    each referent's coercion domain.

    Evaluation is three-valued Kleene (see :func:`_eval3`); this coerces the
    result to the **safe polarity** — only a definite ``TRUE`` yields ``True``,
    ``FALSE`` and ``UNKNOWN`` both yield ``False``. Never raises: malformed
    input (non-dict, empty ``{}``, a combinator mixed with field keys, ``any``/
    ``all`` without a non-empty list) evaluates ``FALSE``. This generalizes v1
    exactly — a flat FieldMap's top-level result is unchanged.
    """
    return _eval3(cond, resolved, visible, field_types or {}) is True


def _eval3(cond, resolved, visible, field_types):
    """Three-valued (Kleene) evaluation → ``True`` / ``False`` / :data:`UNKNOWN`.

    A combinator object carries exactly one of ``any``/``all``/``not`` and no
    field keys; ``any``/``all`` take a non-empty list of sub-conditions, ``not``
    a single one. Anything malformed collapses to ``False`` (safe polarity)."""
    if not isinstance(cond, dict) or not cond:
        return False
    combos = [k for k in _COMBINATORS if k in cond]
    if combos:
        if len(cond) != 1:  # a combinator must not mix with field keys / others
            return False
        op = combos[0]
        arg = cond[op]
        if op == "not":
            r = _eval3(arg, resolved, visible, field_types)
            return r if r is UNKNOWN else (not r)
        if not isinstance(arg, list) or not arg:
            return False
        subs = [_eval3(c, resolved, visible, field_types) for c in arg]
        if op == "all":  # Kleene AND: FALSE dominates, else UNKNOWN, else TRUE
            if any(s is False for s in subs):
                return False
            return UNKNOWN if any(s is UNKNOWN for s in subs) else True
        # any — Kleene OR: TRUE dominates, else UNKNOWN, else FALSE
        if any(s is True for s in subs):
            return True
        return UNKNOWN if any(s is UNKNOWN for s in subs) else False
    # v1 flat FieldMap — Kleene AND across keys.
    result = True
    for ref, acceptable in cond.items():
        r = _eval_term(ref, acceptable, resolved, visible, field_types)
        if r is False:
            return False
        if r is UNKNOWN:
            result = UNKNOWN
    return result


def _eval_term(ref, acceptable, resolved, visible, field_types):
    """One ``{field: scalar-or-list}`` term → ``True`` / ``False`` /
    :data:`UNKNOWN`. A hidden (per ``visible``) or absent referent is
    ``UNKNOWN``; otherwise the referent's canon value must equal a canon'd
    operand (list ORs), both in the referent's own field-type domain."""
    ftype = field_types.get(ref)
    if visible is not None and not visible.get(ref):
        rv = UNKNOWN
    elif ref not in resolved:
        rv = UNKNOWN
    else:
        rv = canon(resolved[ref], ftype)
    if rv is UNKNOWN:
        return UNKNOWN
    operands = acceptable if isinstance(acceptable, list) else [acceptable]
    return any(rv == canon(a, ftype) for a in operands)


def compute_visibility(fields, resolved) -> dict:
    """Monotone fixpoint — returns ``{field_key: bool}``.

    Starts all-hidden (safe polarity) and sweeps at most ``len(fields) + 1``
    times, flipping a field to visible only when it has no condition or its
    ``show_if`` matches against the *currently-visible* referents. A
    not-yet-visible referent contributes ``UNKNOWN``, so cycles resolve
    deterministically to all-hidden and transitive hiding is automatic —
    correctness does not depend on lint having ordered or de-cycled the file.
    """
    field_types = type_index(fields)
    visible = {f["key"]: False for f in fields if isinstance(f, dict) and "key" in f}
    changed = True
    passes = 0
    while changed and passes <= len(fields):
        changed = False
        passes += 1
        for f in fields:
            if not isinstance(f, dict) or "key" not in f:
                continue
            key = f["key"]
            cond = f.get("show_if")
            new = True if not cond else matches(cond, resolved, visible, field_types)
            if new != visible[key]:
                visible[key] = new
                changed = True
    return visible


def field_visible(key, fields, resolved) -> bool:
    """Whether ``key`` is visible given ``resolved`` — convenience over
    :func:`compute_visibility`."""
    return compute_visibility(fields, resolved).get(key, False)


def effective_required(field, fields, resolved, visible, field_types=None) -> bool:
    """A field is effectively required iff it is visible AND
    (``required: true`` OR its ``required_if`` matches). ``required`` and
    ``required_if`` compose with OR — ``required_if`` never relaxes a static
    ``required``. A hidden field is never required (visibility-first)."""
    key = field.get("key")
    if not visible.get(key):
        return False
    if field.get("required") is True:
        return True
    req_if = field.get("required_if")
    if req_if:
        if field_types is None:
            field_types = type_index(fields)
        return matches(req_if, resolved, visible, field_types)
    return False


def enabled_options(field, resolved, visible, field_types) -> list:
    """String-form values of a dropdown's currently-enabled options.

    Bare scalar options are always enabled. Object options
    ``{value, label?, description?, enable_if?}`` are enabled iff they carry
    no ``enable_if`` or its condition matches (an ``UNKNOWN`` referent leaves
    the option disabled — safe polarity). Values are stringified to mirror
    the renderer's ``String(v)``. Non-dropdown / optionless fields → ``[]``.
    """
    if not isinstance(field, dict) or field.get("type") != "dropdown":
        return []
    out = []
    for opt in field.get("options") or []:
        if isinstance(opt, dict):
            enable_if = opt.get("enable_if")
            ok = (not enable_if) or matches(enable_if, resolved, visible, field_types)
            val = opt.get("value")
        else:
            ok = True
            val = opt
        if ok:
            out.append(_option_value_str(val))
    return out


def invalid_option_violations(fields, resolved, visible, field_types=None) -> list:
    """``[{key, label, value}]`` for each VISIBLE dropdown whose resolved value
    canon-equals (dropdown domain) a DECLARED option that is currently
    disabled. Runs after visibility — options never feed back into it; hidden
    fields contribute nothing. A value matching NO declared option is *not*
    an invalid_option (that is the advisory membership error in
    :func:`field_value_errors`)."""
    if field_types is None:
        field_types = type_index(fields)
    out = []
    for field in fields:
        if not isinstance(field, dict) or field.get("type") != "dropdown":
            continue
        key = field.get("key")
        if not visible.get(key):
            continue
        rvc = canon(resolved.get(key), "dropdown")
        if rvc is UNKNOWN:
            continue  # empty/absent value is not a disabled-option violation
        enabled = set(enabled_options(field, resolved, visible, field_types))
        for opt in field.get("options") or []:
            optval = opt.get("value") if isinstance(opt, dict) else opt
            if canon(optval, "dropdown") == rvc:  # matched a declared option
                s = _option_value_str(optval)
                if s not in enabled:
                    out.append({"key": key, "label": field.get("label") or key, "value": s})
                break
    return out


def field_value_errors(fields, resolved, visible) -> list:
    """Advisory ``[{key, label, error}]`` for VISIBLE fields: a dropdown whose
    non-empty value matches no declared option (against ALL options, enabled
    or not), or a number outside ``min``/``max``. Hidden fields are skipped.
    Object-YAML validation stays renderer-only (keeps this module
    stdlib-only)."""
    out = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        key = field.get("key")
        if not visible.get(key):
            continue
        ftype = field.get("type")
        label = field.get("label") or key
        value = resolved.get(key)
        if ftype == "dropdown":
            if is_empty(value, "dropdown"):
                continue
            rvc = canon(value, "dropdown")
            if rvc is UNKNOWN:
                continue
            declared = {
                canon(o.get("value") if isinstance(o, dict) else o, "dropdown")
                for o in field.get("options") or []
            }
            if rvc not in declared:
                out.append({"key": key, "label": label, "error": f"{value!r} is not one of the declared options"})
        elif ftype == "number":
            n = canon(value, "number")
            if n is UNKNOWN:
                continue
            mn = _number_canon(field.get("min"))
            mx = _number_canon(field.get("max"))
            if mn is not UNKNOWN and n < mn:
                out.append({"key": key, "label": label, "error": f"must be >= {mn}"})
            elif mx is not UNKNOWN and n > mx:
                out.append({"key": key, "label": label, "error": f"must be <= {mx}"})
    return out


def validate_config(fields, values) -> dict:
    """Full gate report over an already-layered resolved value map.

    Returns ``{ok, visible, effective_required, missing_required,
    invalid_options, field_errors}``. ``missing_required`` holds the visible,
    effectively-required fields whose value :func:`is_empty`; ``ok`` is
    ``False`` iff there is a missing-required or invalid-option (advisory
    ``field_errors`` do not affect ``ok``). Labels fall back to the key.
    """
    field_types = type_index(fields)
    visible = compute_visibility(fields, values)
    eff_required = []
    missing = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        key = field.get("key")
        if effective_required(field, fields, values, visible, field_types):
            eff_required.append(key)
            if is_empty(values.get(key), field.get("type")):
                missing.append({"key": key, "label": field.get("label") or key})
    invalid = invalid_option_violations(fields, values, visible, field_types)
    errors = field_value_errors(fields, values, visible)
    return {
        "ok": not (missing or invalid),
        "visible": visible,
        "effective_required": eff_required,
        "missing_required": missing,
        "invalid_options": invalid,
        "field_errors": errors,
    }


def resolve_fields(fields, overlay=None) -> dict:
    """Layer a resolved value map: per-type empty base ⊕ field ``default`` ⊕
    ``overlay[key]`` (only for keys the overlay carries). Mirrors the value
    resolution formula every enforcement point already computes."""
    resolved = {}
    for field in fields:
        if not isinstance(field, dict) or "key" not in field:
            continue
        key = field["key"]
        value = type_empty_default(field.get("type"))
        if "default" in field:
            value = field["default"]
        if overlay is not None and key in overlay:
            value = overlay[key]
        resolved[key] = value
    return resolved


def parse_set_yaml(text: str) -> dict:
    """Parse a settings document; malformed or non-mapping input is empty.

    Resolution is explicit: :func:`resolve_generator_values` preserves the
    sparse values generators already read, while :func:`resolve_form_values`
    includes each declared field's empty base for form validation.
    """
    import yaml
    try:
        parsed = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resolve_generator_values(parsed: dict) -> dict:
    """Generator config values: defaults then values, with no empty bases.

    A plain YAML mapping passes through. A fields list or an object/null
    values block marks a schema form, whose other top-level keys are omitted.
    """
    fields = parsed.get("fields")
    values = parsed.get("values")
    schema = isinstance(fields, list) or (
        "values" in parsed and (values is None or isinstance(values, dict)))
    if not schema:
        return dict(parsed)
    resolved = {
        field["key"]: field["default"]
        for field in (fields if isinstance(fields, list) else [])
        if isinstance(field, dict) and isinstance(field.get("key"), str)
        and "default" in field
    }
    if isinstance(values, dict):
        resolved.update(values)
    return resolved


def resolve_form_values(parsed: dict) -> tuple[list[dict], dict]:
    """Form/gate fields and values: empty bases, defaults, then values.

    Explicit values for undeclared keys survive, matching editable settings.
    Malformed field entries are excluded from the returned schema.
    """
    raw_fields = parsed.get("fields")
    fields = [field for field in (raw_fields if isinstance(raw_fields, list) else [])
              if isinstance(field, dict) and isinstance(field.get("key"), str)
              and field["key"]]
    resolved = resolve_fields(fields)
    if isinstance(parsed.get("values"), dict):
        resolved.update(parsed["values"])
    return fields, resolved
