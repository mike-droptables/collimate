# SPDX-License-Identifier: MIT
"""Static conditional-field analysis using the shared runtime evaluator.

Manifest loading and Issue collection stay in lint.core; this module accepts
fields and a diagnostic sink and imports no manifest/transport owner.
"""
from __future__ import annotations

from typing import Any, Protocol
from collimate.config_values import (
    UNKNOWN, canon, compute_visibility, enabled_options, resolve_fields,
    is_empty, type_empty_default,
)


class ConditionReporter(Protocol):
    def error(self, code: str, message: str, field: str | None = None) -> None: ...
    def warn(self, code: str, message: str, field: str | None = None) -> None: ...


def condition_refs(cond: Any) -> list[str]:
    """Every main-config field key a prompt condition consults, recursing
    through the v2 ``any`` / ``all`` / ``not`` combinators. A pure ref
    collector (also used for set_yaml show_if referent sets and the cond-cycle
    graph): structural validation lives in :func:`walk_condition_terms`."""
    if not isinstance(cond, dict):
        return []
    out: list[str] = []
    for key, value in cond.items():
        if key == "not":
            out += condition_refs(value)
        elif key in ("any", "all"):
            for sub in value if isinstance(value, list) else []:
                out += condition_refs(sub)
        elif isinstance(key, str):
            out.append(key)
    return out


_RESERVED_COND_KEYS = ("any", "all", "not")
# Only these field types are legal condition referents in v1; a
# text/textarea/list/object referent (or a field with no/unknown type) has no
# defined comparison and is rejected.
_LEGAL_COND_REFERENT_TYPES = ("dropdown", "checkbox", "number")


def _is_combinator(cond: Any) -> bool:
    """Whether ``cond`` is a v2 combinator map (``any``/``all``/``not`` at top)."""
    return isinstance(cond, dict) and any(k in cond for k in _RESERVED_COND_KEYS)


def _has_show_if(field: Any) -> bool:
    """Whether ``field`` carries a non-null ``show_if``."""
    return isinstance(field, dict) and field.get("show_if") is not None


def _dropdown_option_canons(field: dict[str, Any]) -> set:
    """Canon'd (dropdown domain) values of a field's declared options, or an
    empty set when it declares no usable options."""
    out: set = set()
    for opt in field.get("options") or []:
        val = opt.get("value") if isinstance(opt, dict) else opt
        c = canon(val, "dropdown")
        if c is not UNKNOWN:
            out.add(c)
    return out


def _scc_cycles(order: list[str], graph: dict[str, set]) -> list[list[str]]:
    """Tarjan SCCs over ``graph``; return every component of size > 1 (a
    reference cycle). Self-edges are excluded by the caller, so a size-1
    component is never a cycle."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = [0]
    cycles: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                cycles.append(comp)

    for v in order:
        if v not in index:
            strongconnect(v)
    return cycles


def walk_condition_terms(rel, dep_key, kind, cond, ctx, *, allow_not,
                     subject=None, issue_field=None):
    """Validate v2 combinator structure and yield each leaf ``(ref, acceptable)``
    term, recursing through ``any``/``all``/``not``. Emits the structural errors
    ``cond-combinator-mixed`` (a combinator key sharing the map with others),
    ``cond-combinator-arg`` (``any``/``all`` without a non-empty list, or ``not``
    without a single map), and ``cond-not-in-show-if``. ``allow_not`` is ``False``
    for ``show_if`` — negation would break the monotone visibility fixpoint, so
    it is only legal in ``required_if`` / option ``enable_if`` (post-fixpoint).
    The caller applies the per-referent checks to the yielded leaves.
    ``subject`` / ``issue_field`` override the set_yaml-field phrasing of the
    structural messages and their ``field=`` (prompt conditions pass their
    manifest key)."""
    subj = subject if subject is not None else f"{rel}: field {dep_key!r} {kind!r}"
    fld = issue_field if issue_field is not None else f"{rel}:{dep_key}"
    if not isinstance(cond, dict) or not cond:
        return  # top-level emptiness handled by the caller
    combos = [k for k in _RESERVED_COND_KEYS if k in cond]
    if not combos:
        yield from cond.items()  # v1 flat FieldMap — leaf terms
        return
    if len(cond) != 1:
        ctx.error(
            "cond-combinator-mixed",
            f"{subj} combinator {combos[0]!r} must be "
            "the map's only key — a combinator can't mix with field terms",
            field=fld,
        )
        return
    op = combos[0]
    arg = cond[op]
    if op == "not":
        if not allow_not:
            ctx.error(
                "cond-not-in-show-if",
                f"{rel}: field {dep_key!r} 'show_if' uses 'not' — negation isn't "
                "allowed in show_if (it would break the monotone visibility "
                "fixpoint); use it only in required_if / option enable_if",
                field=f"{rel}:{dep_key}",
            )
            return
        if not isinstance(arg, dict) or not arg:
            ctx.error(
                "cond-combinator-arg",
                f"{subj} 'not' takes a single non-empty "
                "condition map",
                field=fld,
            )
            return
        yield from walk_condition_terms(rel, dep_key, kind, arg, ctx, allow_not=allow_not,
                                    subject=subject, issue_field=issue_field)
        return
    if not isinstance(arg, list) or not arg:
        ctx.error(
            "cond-combinator-arg",
            f"{subj} {op!r} takes a non-empty list of "
            "conditions",
            field=fld,
        )
        return
    for sub in arg:
        if not isinstance(sub, dict) or not sub:
            ctx.error(
                "cond-combinator-arg",
                f"{subj} {op!r} items must each be a non-empty condition map, "
                f"got {sub!r}",
                field=fld,
            )
            continue
        yield from walk_condition_terms(rel, dep_key, kind, sub, ctx, allow_not=allow_not,
                                    subject=subject, issue_field=issue_field)


def _analyze_condition(
    rel: str,
    dep_key: Any,
    dep_index: int,
    kind: str,
    cond: Any,
    declared: dict[str, dict[str, Any]],
    order: dict[str, int],
    field_types: dict[str, Any],
    ctx: ConditionReporter,
) -> None:
    """Per-referent checks shared by ``show_if`` and ``required_if``, recursing
    through v2 ``any``/``all``/``not`` combinators (see :func:`walk_condition_terms`).

    Emits (referent-level): ``cond-empty``, ``cond-unknown-field``,
    ``cond-self-reference``, ``cond-forward-reference``, ``cond-referent-type``,
    ``cond-value-not-an-option``, ``cond-value-type-mismatch``, plus the
    combinator-structure errors from the walker. ``cond`` is the already-folded
    condition; ``not`` is rejected in ``show_if``.
    """
    if cond is None:
        return
    if not isinstance(cond, dict) or not cond:
        ctx.error(
            "cond-empty",
            f"{rel}: field {dep_key!r} '{kind}' is empty or not a mapping — a "
            "condition must be a non-empty {field: value} map",
            field=f"{rel}:{dep_key}",
        )
        return
    for ref, acceptable in walk_condition_terms(
        rel, dep_key, kind, cond, ctx, allow_not=(kind != "show_if")
    ):
        if not isinstance(ref, str) or ref not in declared:
            ctx.error(
                "cond-unknown-field",
                f"{rel}: field {dep_key!r} '{kind}' references {ref!r}, which is "
                f"not a declared field in {rel}",
                field=f"{rel}:{dep_key}",
            )
            continue
        if ref == dep_key:
            ctx.error(
                "cond-self-reference",
                f"{rel}: field {dep_key!r} '{kind}' references itself",
                field=f"{rel}:{dep_key}",
            )
            continue
        if order.get(ref, -1) > dep_index:
            ctx.warn(
                "cond-forward-reference",
                f"{rel}: field {dep_key!r} '{kind}' references {ref!r}, declared "
                "later in the file — put referents before their dependents",
                field=f"{rel}:{dep_key}",
            )
        reftype = field_types.get(ref)
        operands = acceptable if isinstance(acceptable, list) else [acceptable]
        if reftype not in _LEGAL_COND_REFERENT_TYPES:
            ctx.error(
                "cond-referent-type",
                f"{rel}: field {dep_key!r} '{kind}' references {ref!r} (type "
                f"{reftype!r}) — only dropdown/checkbox/number fields are legal "
                "condition referents",
                field=f"{rel}:{dep_key}",
            )
            continue
        if reftype == "dropdown":
            opt_canons = _dropdown_option_canons(declared[ref])
            if opt_canons:  # skip when the referent declares no options
                for a in operands:
                    if canon(a, "dropdown") not in opt_canons:
                        ctx.error(
                            "cond-value-not-an-option",
                            f"{rel}: field {dep_key!r} '{kind}' tests "
                            f"{ref}={a!r}, but that is not one of {ref!r}'s options",
                            field=f"{rel}:{dep_key}",
                        )
        elif reftype == "checkbox":
            for a in operands:
                if canon(a, "checkbox") is UNKNOWN:
                    ctx.warn(
                        "cond-value-type-mismatch",
                        f"{rel}: field {dep_key!r} '{kind}' tests checkbox "
                        f"{ref}={a!r} — expected a boolean (true/false)",
                        field=f"{rel}:{dep_key}",
                    )
        elif reftype == "number":
            for a in operands:
                if canon(a, "number") is UNKNOWN:
                    ctx.warn(
                        "cond-value-type-mismatch",
                        f"{rel}: field {dep_key!r} '{kind}' tests number "
                        f"{ref}={a!r} — expected a numeric value",
                        field=f"{rel}:{dep_key}",
                    )


def _check_enable_if(
    rel: str,
    declared: dict[str, dict[str, Any]],
    field_types: dict[str, Any],
    fields: list[dict[str, Any]],
    ctx: ConditionReporter,
) -> None:
    """Option-level ``enable_if`` checks (a dropdown greys out an option when
    another field's value fails the option's condition).

    Emits ``enable-if-on-non-dropdown``, ``enable-if-empty``,
    ``enable-if-unknown-field``, ``enable-if-referent-type``,
    ``enable-if-value-not-an-option``, ``enable-if-reference-superset`` (warn),
    and ``enable-if-default-disabled``, plus the combinator-structure errors from
    :func:`walk_condition_terms` (``enable_if`` allows ``not`` — it is post-fixpoint).
    """
    resolved_default = None
    visible_default = None
    for key, f in declared.items():
        opts = f.get("options")
        if not isinstance(opts, list):
            continue
        ftype = field_types.get(key)
        show_keys = set(condition_refs(f.get("show_if")))

        for opt in opts:
            if not isinstance(opt, dict) or "enable_if" not in opt:
                continue
            enable_if = opt.get("enable_if")
            if ftype != "dropdown":
                ctx.error(
                    "enable-if-on-non-dropdown",
                    f"{rel}: field {key!r} (type {ftype!r}) has an option with "
                    "'enable_if' — enable_if is only valid on dropdown options",
                    field=f"{rel}:{key}",
                )
                continue
            if "value" not in opt:
                ctx.error(
                    "enable-if-on-non-dropdown",
                    f"{rel}: field {key!r} has an 'enable_if' option with no "
                    "'value' — enable_if is only valid on object options that "
                    "declare a value",
                    field=f"{rel}:{key}",
                )
                continue
            if not isinstance(enable_if, dict) or not enable_if:
                ctx.error(
                    "enable-if-empty",
                    f"{rel}: field {key!r} option {opt.get('value')!r} has an "
                    "empty or non-mapping 'enable_if'",
                    field=f"{rel}:{key}",
                )
                continue
            for ref, acceptable in walk_condition_terms(
                rel, key, "option enable_if", enable_if, ctx, allow_not=True
            ):
                if not isinstance(ref, str) or ref not in declared:
                    ctx.error(
                        "enable-if-unknown-field",
                        f"{rel}: field {key!r} option 'enable_if' references "
                        f"{ref!r}, not a declared field in {rel}",
                        field=f"{rel}:{key}",
                    )
                    continue
                reftype = field_types.get(ref)
                if reftype not in _LEGAL_COND_REFERENT_TYPES:
                    ctx.error(
                        "enable-if-referent-type",
                        f"{rel}: field {key!r} option 'enable_if' references "
                        f"{ref!r} (type {reftype!r}) — only dropdown/checkbox/"
                        "number are legal referents",
                        field=f"{rel}:{key}",
                    )
                    continue
                operands = acceptable if isinstance(acceptable, list) else [acceptable]
                if reftype == "dropdown":
                    opt_canons = _dropdown_option_canons(declared[ref])
                    if opt_canons:
                        for a in operands:
                            if canon(a, "dropdown") not in opt_canons:
                                ctx.error(
                                    "enable-if-value-not-an-option",
                                    f"{rel}: field {key!r} option 'enable_if' "
                                    f"tests {ref}={a!r}, not one of {ref!r}'s "
                                    "options",
                                    field=f"{rel}:{key}",
                                )
                # enable-if-reference-superset: an option may only gate on a
                # field this field's own show_if also references, or one that
                # is statically shown.
                # Exemption: a referent whose folded show_if EQUALS this
                # field's own — the two are shown/hidden together, so the
                # referent can never be hidden while the option is offered
                # (the exact hazard this rule guards against). Also skipped for a
                # v2 combinator enable_if: there each branch guards its own
                # referents (e.g. {any:[{mode:X},{mode:[Y],backend:Z}]} only
                # consults backend on the branch that first pins mode to Y, where
                # backend is visible), so the flat-map "may be hidden" heuristic
                # produces false positives.
                same_visibility = (
                    (declared.get(ref) or {}).get("show_if")
                    == f.get("show_if")
                )
                if (
                    ref not in show_keys
                    and _has_show_if(declared.get(ref))
                    and not same_visibility
                    and not _is_combinator(enable_if)
                ):
                    ctx.warn(
                        "enable-if-reference-superset",
                        f"{rel}: option 'enable_if' on field {key!r} references "
                        f"{ref!r}, which is conditionally shown but not in "
                        f"{key!r}'s show_if — it may be hidden when the option "
                        "is evaluated",
                        field=f"{rel}:{key}",
                    )

        # enable-if-default-disabled: the field's own default lands on an
        # option that is disabled with every field at its default.
        if ftype != "dropdown" or "default" not in f:
            continue
        if not any(isinstance(o, dict) and o.get("enable_if") for o in opts):
            continue
        if resolved_default is None:
            resolved_default = resolve_fields(fields)
            visible_default = compute_visibility(fields, resolved_default)
        if not visible_default.get(key):
            continue  # a hidden field's default never faces the user
        dc = canon(f["default"], "dropdown")
        if dc is UNKNOWN or dc not in _dropdown_option_canons(f):
            continue  # default is not among declared options — other rules apply
        enabled_canons = {
            canon(s, "dropdown")
            for s in enabled_options(f, resolved_default, visible_default, field_types)
        }
        if dc not in enabled_canons:
            ctx.error(
                "enable-if-default-disabled",
                f"{rel}: field {key!r} defaults to {f['default']!r}, but that "
                "option is disabled when every field sits at its default",
                field=f"{rel}:{key}",
            )


def check_set_yaml_conditions(
    rel: str, fields: list[dict[str, Any]], ctx: ConditionReporter
) -> None:
    """Validate ``show_if`` / ``required_if`` / ``enable_if`` in one ``.set_yaml``.

    ``fields`` is the file's parsed ``fields:`` list (dicts only, from
    :func:`_read_set_yaml_fields`). Every check skips gracefully on malformed
    or missing pieces — the runtime evaluator is defined for them, so lint only
    surfaces authoring mistakes, never blocks.
    """
    field_list = [f for f in fields if isinstance(f, dict)]

    declared: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    field_types: dict[str, Any] = {}
    for i, f in enumerate(field_list):
        k = f.get("key")
        if isinstance(k, str) and k not in declared:
            declared[k] = f
            order[k] = i
            field_types[k] = f.get("type")

    for i, f in enumerate(field_list):
        key = f.get("key")
        key_str = key if isinstance(key, str) else None

        # A field literally named any/all/not collides with the v2 combinators.
        if key_str in _RESERVED_COND_KEYS:
            ctx.error(
                "cond-reserved-key",
                f"{rel}: field is named {key_str!r} — any/all/not are reserved "
                "for v2 combinators; rename it",
                field=f"{rel}:{key_str}",
            )

        # required: true is meaningless on a field type whose empty-base value
        # is itself non-empty (only checkbox today — it always holds
        # true/false), so the field can never be "empty" and never gates a run.
        # Derived from the shared rule in collimate.config_values.
        ftype = field_types.get(key_str)
        if f.get("required") is True and not is_empty(
                type_empty_default(ftype), ftype):
            ctx.warn(
                "required-on-checkbox",
                f"{rel}: field {key_str!r} is a checkbox with 'required: "
                "true' — a checkbox is never empty, so the flag does nothing",
                field=f"{rel}:{key_str}",
            )

        eff_show = f.get("show_if")
        req_if = f.get("required_if")

        _analyze_condition(rel, key_str, i, "show_if", eff_show,
                           declared, order, field_types, ctx)
        _analyze_condition(rel, key_str, i, "required_if", req_if,
                           declared, order, field_types, ctx)

        show_is_map = isinstance(eff_show, dict) and bool(eff_show)
        req_is_map = isinstance(req_if, dict) and bool(req_if)

        # required_if on a checkbox is inert (a checkbox is never empty).
        if field_types.get(key_str) == "checkbox" and req_is_map:
            ctx.warn(
                "required-if-on-checkbox",
                f"{rel}: field {key_str!r} is a checkbox with 'required_if' — a "
                "checkbox is never empty, so it never gates a run",
                field=f"{rel}:{key_str}",
            )

        # Both required: true and show_if — almost always required_if was meant.
        if f.get("required") is True and show_is_map:
            ctx.warn(
                "required-with-show_if",
                f"{rel}: field {key_str!r} has both 'required: true' and "
                "'show_if' — this is always-required-when-shown; if you meant "
                "conditional gating, use 'required_if' and delete 'required: true'",
                field=f"{rel}:{key_str}",
            )

        # required_if provably unsatisfiable together with show_if: only flag
        # when a shared referent has disjoint canon'd operand sets (conservative).
        if show_is_map and req_is_map:
            for ref in set(eff_show) & set(req_if):
                if not isinstance(ref, str) or ref not in declared:
                    continue
                rt = field_types.get(ref)
                if rt not in _LEGAL_COND_REFERENT_TYPES:
                    continue
                s_ops = eff_show[ref] if isinstance(eff_show[ref], list) else [eff_show[ref]]
                r_ops = req_if[ref] if isinstance(req_if[ref], list) else [req_if[ref]]
                s_c = {canon(a, rt) for a in s_ops} - {UNKNOWN}
                r_c = {canon(a, rt) for a in r_ops} - {UNKNOWN}
                if s_c and r_c and s_c.isdisjoint(r_c):
                    ctx.warn(
                        "required-if-permanently-hidden",
                        f"{rel}: field {key_str!r} is required when "
                        f"{ref}={r_ops!r} but only shown when {ref}={s_ops!r} — "
                        "the required gate can never be visible",
                        field=f"{rel}:{key_str}",
                    )

        # required_if referents must be a subset of show_if referents, unless
        # the field has no show_if and every referent is itself statically shown.
        if req_is_map:
            req_refs = [
                r for r in req_if
                if isinstance(r, str) and r in declared and r not in _RESERVED_COND_KEYS
            ]
            if show_is_map:
                show_keys = set(condition_refs(eff_show))
                for r in req_refs:
                    if r not in show_keys:
                        ctx.error(
                            "required-if-reference-superset",
                            f"{rel}: field {key_str!r} 'required_if' references "
                            f"{r!r}, which its 'show_if' does not — required_if "
                            "may only depend on fields that also gate visibility",
                            field=f"{rel}:{key_str}",
                        )
            else:
                for r in req_refs:
                    if _has_show_if(declared.get(r)):
                        ctx.error(
                            "required-if-reference-superset",
                            f"{rel}: field {key_str!r} has no 'show_if' but "
                            f"'required_if' references {r!r}, which is itself "
                            "conditionally shown — required_if could fire while "
                            f"{r!r} is hidden",
                            field=f"{rel}:{key_str}",
                        )

    # Cycle detection over the show_if ∪ required_if reference graph (self-edges
    # excluded — cond-self-reference already covers those).
    graph: dict[str, set] = {k: set() for k in declared}
    for k, f in declared.items():
        for cond in (f.get("show_if"), f.get("required_if")):
            for ref in condition_refs(cond):
                if ref in declared and ref != k:
                    graph[k].add(ref)
    for comp in _scc_cycles(list(declared.keys()), graph):
        members = sorted(comp)
        ctx.warn(
            "cond-cycle",
            f"{rel}: fields {members} form a show_if/required_if reference "
            "cycle — the runtime resolves cycles to hidden, so they never show",
            field=f"{rel}:{members[0]}",
        )

    _check_enable_if(rel, declared, field_types, field_list, ctx)


