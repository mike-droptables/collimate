// Pure helpers for `.set_yaml` schema-form fields (the settings flavor
// of generator draft_files). Fields arrive pre-parsed from the runner
// catalog; value resolution mirrors the SDK host's `_parse_schema_values`
// (fields' defaults ⊕ `values:` block) and the schema_form renderer's
// field model; the "empty" definition below mirrors the Python source of
// truth (collimate.config_values, used by the SDK lint and
// api/set_yaml.py) and schema_form's validate — all pinned by the
// shared fixture at sdk/shared/empty-values-contract.json
// (asserted in __tests__/setYaml.test.ts).

export type SetYamlFieldType =
    | 'text'
    | 'textarea'
    | 'number'
    | 'checkbox'
    | 'dropdown'
    | 'list'
    | 'object';

/** A v1 `.set_yaml` condition: a plain FieldMap `{fieldKey: value | [values]}`.
 *  Multiple keys AND across fields; a list ORs within a field. The keys
 *  `any`/`all`/`not` are reserved for v2 combinators. Mirrors the Python
 *  source of truth (collimate.config_values). */
export type SetYamlCondition = Record<string, unknown | unknown[]>;

/** A dropdown option, normalized from either authoring shape (bare scalar
 *  or `{value,label,description}`) exactly like the schema_form renderer's
 *  normalizeOptions. `enable_if` gates the option's selectability. */
export interface SetYamlOption {
    value: string;
    label: string;
    description?: string;
    enable_if?: SetYamlCondition;
}

export interface SetYamlField {
    key: string;
    type: SetYamlFieldType;
    label?: string;
    required?: boolean;
    default?: unknown;
    /** Conditional visibility. Absent ⇒ always shown. */
    show_if?: SetYamlCondition;
    /** Conditional requiredness; composes with static `required` via OR. */
    required_if?: SetYamlCondition;
    /** Dropdown options in normalized form (see SetYamlOption). */
    options?: SetYamlOption[];
    /** Numeric bounds (number fields), carried through for validation. */
    min?: number;
    max?: number;
}

export interface ParsedSetYaml {
    fields: SetYamlField[];
    /** The raw `values:` block (user-edited overrides), NOT the
     *  resolved per-field values — see effectiveValues for those. */
    values: Record<string, unknown>;
}

/** A plain-object condition map, or undefined. show_if/required_if/
 *  enable_if are always authored as maps; a scalar/array/null is dropped
 *  (treated as absent), which lint catches as malformed. */
function asConditionMap(v: unknown): SetYamlCondition | undefined {
    return v != null && typeof v === 'object' && !Array.isArray(v)
        ? (v as SetYamlCondition)
        : undefined;
}

/** Normalize a raw `options:` array, mirroring the schema_form renderer's
 *  normalizeOptions: object options keep {value,label,description}; bare
 *  scalars become {value:String(v), label:String(v)}; null → "". `enable_if`
 *  is carried through (the renderer drops it today; the evaluator needs it).
 *  Exported because `.set_yaml` fields also arrive PRE-PARSED but UN-normalized
 *  from the runner catalog (`GET /api/runner_generators` projects the raw
 *  descriptors) — those boundaries must fold through this one normalizer, not
 *  a second copy. */
export function normalizeOptions(opts: unknown): SetYamlOption[] | undefined {
    if (!Array.isArray(opts)) return undefined;
    return opts.map((o): SetYamlOption => {
        if (o != null && typeof o === 'object' && !Array.isArray(o)) {
            const obj = o as Record<string, unknown>;
            const value = obj.value == null ? '' : String(obj.value);
            const out: SetYamlOption = {
                value,
                label: obj.label == null ? value : String(obj.label),
            };
            if (obj.description != null) out.description = String(obj.description);
            const enableIf = asConditionMap(obj.enable_if);
            if (enableIf) out.enable_if = enableIf;
            return out;
        }
        const s = o == null ? '' : String(o);
        return { value: s, label: s };
    });
}

/** The per-type "empty" base value — the bottom layer of the
 *  resolution formula. Chosen so every declared field key always has
 *  a value, and so the base itself reads as empty per isEmptyValue
 *  (checkbox is never empty; false is just its natural default). */
function emptyBaseFor(type: SetYamlFieldType): unknown {
    switch (type) {
        case 'number': return null;
        case 'checkbox': return false;
        case 'list': return [];
        case 'object': return null;
        default: return ''; // text / textarea / dropdown
    }
}

/** Resolve the effective {key: value} map for a parsed `.set_yaml`:
 *  per-type empty defaults ⊕ field `default:`s ⊕ `values:` block.
 *  Matches the host's `_parse_schema_values` layering (plus the empty
 *  base so unset fields still appear). */
export function effectiveValues(parsed: ParsedSetYaml): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const f of parsed.fields) {
        out[f.key] = Object.prototype.hasOwnProperty.call(f, 'default')
            ? f.default
            : emptyBaseFor(f.type);
    }
    for (const [k, v] of Object.entries(parsed.values)) {
        out[k] = v;
    }
    return out;
}

/** The shared empty-value definition (single source of truth on the
 *  frontend; must agree case-for-case with the contract table at
 *  sdk/shared/empty-values-contract.json). Per field type:
 *    text/textarea  null | "" (trimmed)
 *    number         null
 *    dropdown       null | ""
 *    checkbox       never empty
 *    list           null | []
 *    object         null
 *  0 and false are NOT empty. Prompt emptiness ( "" trimmed ) is
 *  evaluated by callers — the prompt isn't a .set_yaml field. */
export function isEmptyValue(value: unknown, fieldType: SetYamlFieldType): boolean {
    switch (fieldType) {
        case 'checkbox':
            return false;
        case 'number':
            return value === null || value === undefined;
        case 'dropdown':
            return value === null || value === undefined || value === '';
        case 'list':
            return value === null || value === undefined
                || (Array.isArray(value) && value.length === 0);
        case 'object':
            return value === null || value === undefined;
        case 'text':
        case 'textarea':
        default:
            return value === null || value === undefined
                || (typeof value === 'string' && value.trim() === '');
    }
}

// --------------------------------------------------------------------------- //
// Conditional field visibility & requirements (show_if / required_if /
// enable_if) — the TypeScript mirror of collimate.config_values. Pinned to
// the same behavior by sdk/shared/set-yaml-conditions-contract.json
// (asserted in __tests__/setYaml.test.ts). Semantics: collimate.config_values
// (the SDK source of truth).
// --------------------------------------------------------------------------- //

/** Sentinel for a referent value that is absent, empty, or hidden.
 *  UNKNOWN never satisfies a match (three-valued logic) — this is what makes
 *  hidden referents and reference cycles resolve to the safe polarity. */
const UNKNOWN: unique symbol = Symbol('UNKNOWN');

type Canonical = string | number | typeof UNKNOWN;

// Truthy folds. checkbox and the dropdown YAML-1.1 bool-word fold are kept
// separate: a checkbox operand may be an int 1/0, a dropdown one never is.
const CHECKBOX_TRUE = new Set(['true', 'on', 'yes', '1']);
const CHECKBOX_FALSE = new Set(['false', 'off', 'no', '0']);
const BOOL_WORD_TRUE = new Set(['on', 'yes', 'true']);
const BOOL_WORD_FALSE = new Set(['off', 'no', 'false']);

function checkboxCanon(value: unknown): Canonical {
    if (value === null || value === undefined) return UNKNOWN;
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (typeof value === 'string') {
        const s = value.trim().toLowerCase();
        if (s === '') return UNKNOWN;
        if (CHECKBOX_TRUE.has(s)) return 'true';
        if (CHECKBOX_FALSE.has(s)) return 'false';
        return UNKNOWN; // unrecognized token → safe polarity
    }
    if (typeof value === 'number') {
        if (value === 1) return 'true';
        if (value === 0) return 'false';
        return UNKNOWN;
    }
    return UNKNOWN;
}

function numberCanon(value: unknown): Canonical {
    // bool excluded (Python checks isinstance(value, bool) first).
    if (value === null || value === undefined || typeof value === 'boolean') return UNKNOWN;
    if (typeof value === 'number') return Number.isNaN(value) ? UNKNOWN : value;
    if (typeof value === 'string') {
        const s = value.trim();
        if (s === '') return UNKNOWN;
        const n = Number(s);
        return Number.isNaN(n) ? UNKNOWN : n;
    }
    return UNKNOWN;
}

function enumCanon(value: unknown): Canonical {
    if (value === null || value === undefined) return UNKNOWN;
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    const s = String(value).trim();
    if (s === '') return UNKNOWN;
    const low = s.toLowerCase();
    if (BOOL_WORD_TRUE.has(low)) return 'true';
    if (BOOL_WORD_FALSE.has(low)) return 'false';
    return low;
}

/** Type-aware canonicalization of a condition operand or referent value.
 *  Coerces `value` into the one canonical domain of `fieldType` so the
 *  ingestion surfaces (renderer String(v), frontend/server raw bool/number)
 *  compare equal. Returns UNKNOWN for anything that cannot be a meaningful
 *  referent value (empty/absent, or an illegal referent type). */
export function canon(value: unknown, fieldType: string | undefined): Canonical {
    if (value === UNKNOWN) return UNKNOWN;
    switch (fieldType) {
        case 'checkbox': return checkboxCanon(value);
        case 'number': return numberCanon(value);
        case 'dropdown': return enumCanon(value);
        // text / textarea / list / object / unknown: illegal referent type.
        default: return UNKNOWN;
    }
}

/** A non-empty plain-object condition — the only shape that carries a live
 *  condition. undefined / {} both read as "no condition" (mirrors Python's
 *  `not cond`). */
function isConditionMap(c: unknown): c is SetYamlCondition {
    return !!c && typeof c === 'object' && !Array.isArray(c)
        && Object.keys(c as object).length > 0;
}

function typeIndex(fields: SetYamlField[]): Record<string, string | undefined> {
    const out: Record<string, string | undefined> = {};
    for (const f of fields) out[f.key] = f.type;
    return out;
}

/** Three-valued (Kleene) result: a definite boolean, or UNKNOWN. */
type Tri = boolean | typeof UNKNOWN;
const COMBINATORS = ['any', 'all', 'not'];

/** Evaluate a set_yaml condition against a resolved value map. A v1 FieldMap
 *  {field: scalar-or-list} (AND across keys, OR within a list) or a v2
 *  combinator {any:[...]} / {all:[...]} / {not: cond}. A `visible` map (or null)
 *  makes a not-yet-visible referent contribute UNKNOWN; `fieldTypes` picks each
 *  referent's coercion domain. Coerces the three-valued result to the safe
 *  polarity — only a definite TRUE yields true. Returns false (never throws) on
 *  malformed input: non-object / empty {} / a combinator mixed with field keys /
 *  any|all without a non-empty list. Generalizes v1 exactly. */
export function matches(
    cond: unknown,
    resolved: Record<string, unknown>,
    visible: Record<string, boolean> | null,
    fieldTypes: Record<string, string | undefined>,
): boolean {
    return eval3(cond, resolved, visible, fieldTypes) === true;
}

function eval3(
    cond: unknown,
    resolved: Record<string, unknown>,
    visible: Record<string, boolean> | null,
    ft: Record<string, string | undefined>,
): Tri {
    if (!cond || typeof cond !== 'object' || Array.isArray(cond)) return false;
    const obj = cond as Record<string, unknown>;
    const keys = Object.keys(obj);
    if (keys.length === 0) return false;
    const combos = COMBINATORS.filter(k => k in obj);
    if (combos.length > 0) {
        if (keys.length !== 1) return false; // a combinator must not mix with field keys
        const op = combos[0];
        const arg = obj[op];
        if (op === 'not') {
            const r = eval3(arg, resolved, visible, ft);
            return r === UNKNOWN ? UNKNOWN : !r;
        }
        if (!Array.isArray(arg) || arg.length === 0) return false;
        const subs: Tri[] = arg.map(c => eval3(c, resolved, visible, ft));
        if (op === 'all') { // Kleene AND: FALSE dominates, else UNKNOWN, else TRUE
            if (subs.some(s => s === false)) return false;
            return subs.some(s => s === UNKNOWN) ? UNKNOWN : true;
        }
        // any — Kleene OR: TRUE dominates, else UNKNOWN, else FALSE
        if (subs.some(s => s === true)) return true;
        return subs.some(s => s === UNKNOWN) ? UNKNOWN : false;
    }
    // v1 flat FieldMap — Kleene AND across keys.
    let result: Tri = true;
    for (const ref of keys) {
        const r = evalTerm(ref, obj[ref], resolved, visible, ft);
        if (r === false) return false;
        if (r === UNKNOWN) result = UNKNOWN;
    }
    return result;
}

function evalTerm(
    ref: string,
    acceptable: unknown,
    resolved: Record<string, unknown>,
    visible: Record<string, boolean> | null,
    ft: Record<string, string | undefined>,
): Tri {
    const ftype = ft[ref];
    let rv: Canonical;
    if (visible !== null && !visible[ref]) {
        rv = UNKNOWN; // hidden (or not-in-map) referent
    } else if (!Object.prototype.hasOwnProperty.call(resolved, ref)) {
        rv = UNKNOWN; // absent referent
    } else {
        rv = canon(resolved[ref], ftype);
    }
    if (rv === UNKNOWN) return UNKNOWN;
    const operands = Array.isArray(acceptable) ? acceptable : [acceptable];
    return operands.some(a => rv === canon(a, ftype));
}

/** Monotone fixpoint (doc §3.6). Starts all-hidden (safe polarity) and sweeps
 *  at most fields.length + 1 times, flipping a field to visible only when it
 *  has no condition or its show_if matches against the currently-visible
 *  referents. Cycles resolve deterministically to all-hidden; transitive
 *  hiding is automatic. */
export function computeVisibility(
    fields: SetYamlField[],
    resolved: Record<string, unknown>,
): Record<string, boolean> {
    const fieldTypes = typeIndex(fields);
    const visible: Record<string, boolean> = {};
    for (const f of fields) visible[f.key] = false;
    let changed = true;
    let passes = 0;
    while (changed && passes <= fields.length) {
        changed = false;
        passes += 1;
        for (const f of fields) {
            const next = isConditionMap(f.show_if)
                ? matches(f.show_if, resolved, visible, fieldTypes)
                : true;
            if (next !== visible[f.key]) {
                visible[f.key] = next;
                changed = true;
            }
        }
    }
    return visible;
}

/** A field is effectively required iff it is visible AND (required: true OR
 *  its required_if matches). `required`/`required_if` compose with OR; a
 *  hidden field is never required (visibility-first). */
export function fieldIsRequired(
    field: SetYamlField,
    fields: SetYamlField[],
    resolved: Record<string, unknown>,
    visible: Record<string, boolean>,
): boolean {
    if (!visible[field.key]) return false;
    if (field.required === true) return true;
    if (isConditionMap(field.required_if)) {
        return matches(field.required_if, resolved, visible, typeIndex(fields));
    }
    return false;
}

/** String-form values of a dropdown's currently-enabled options. Bare-scalar
 *  and enable_if-less options are always enabled; an option with an enable_if
 *  is enabled iff its condition matches (UNKNOWN referent ⇒ disabled — safe
 *  polarity). Non-dropdown / optionless fields → []. */
export function enabledOptions(
    field: SetYamlField,
    resolved: Record<string, unknown>,
    visible: Record<string, boolean>,
    fieldTypes: Record<string, string | undefined>,
): string[] {
    if (!field || field.type !== 'dropdown') return [];
    const out: string[] = [];
    for (const opt of field.options ?? []) {
        const ok = !isConditionMap(opt.enable_if)
            || matches(opt.enable_if, resolved, visible, fieldTypes);
        if (ok) out.push(opt.value);
    }
    return out;
}

/** [{key,label,value}] for each VISIBLE dropdown whose resolved value
 *  canon-equals a DECLARED option that is currently disabled. Runs after the
 *  visibility fixpoint; hidden fields contribute nothing. A value matching NO
 *  declared option is NOT an invalid_option (that is an advisory membership
 *  error, not gated here). */
export function invalidOptionFields(
    parsed: ParsedSetYaml,
): { key: string; label: string; value: string }[] {
    const resolved = effectiveValues(parsed);
    const fieldTypes = typeIndex(parsed.fields);
    const visible = computeVisibility(parsed.fields, resolved);
    const out: { key: string; label: string; value: string }[] = [];
    for (const field of parsed.fields) {
        if (field.type !== 'dropdown') continue;
        if (!visible[field.key]) continue;
        const rvc = canon(resolved[field.key], 'dropdown');
        if (rvc === UNKNOWN) continue; // empty/absent value is not a disabled-option violation
        const enabled = new Set(enabledOptions(field, resolved, visible, fieldTypes));
        for (const opt of field.options ?? []) {
            if (canon(opt.value, 'dropdown') === rvc) { // matched a declared option
                if (!enabled.has(opt.value)) {
                    out.push({ key: field.key, label: field.label || field.key, value: opt.value });
                }
                break;
            }
        }
    }
    return out;
}

/** The full live run-gate report for one `.set_yaml`: the visible,
 *  effectively-required fields whose value is empty, plus each visible
 *  dropdown holding a disabled option. Both computed off effectiveValues +
 *  computeVisibility. */
export function configViolations(parsed: ParsedSetYaml): {
    missingRequired: { key: string; label: string }[];
    invalidOptions: { key: string; label: string; value: string }[];
} {
    return {
        missingRequired: missingRequiredFields(parsed),
        invalidOptions: invalidOptionFields(parsed),
    };
}

/** Required fields whose effective value is currently empty. Drives
 *  the live run gate in the big modal (the catalog's precomputed
 *  missing_required covers the unedited tag; this covers the user's
 *  in-flight edits). Visibility-first: a hidden field is skipped entirely,
 *  regardless of required / required_if. */
export function missingRequiredFields(
    parsed: ParsedSetYaml,
): { key: string; label: string }[] {
    const resolved = effectiveValues(parsed);
    const visible = computeVisibility(parsed.fields, resolved);
    const out: { key: string; label: string }[] = [];
    for (const f of parsed.fields) {
        if (!fieldIsRequired(f, parsed.fields, resolved, visible)) continue;
        if (isEmptyValue(resolved[f.key], f.type)) {
            out.push({ key: f.key, label: f.label || f.key });
        }
    }
    return out;
}

