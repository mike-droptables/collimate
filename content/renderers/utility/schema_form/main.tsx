import React, { useState, useEffect, useRef, useCallback } from 'https://esm.sh/react@19.3.1?target=es2022';
import * as jsyaml from 'https://esm.sh/js-yaml@5.5.0?target=es2022';
import {
    defineRenderer,
    tokens,
    useCollimateFile,
} from '/api/sdk/react.js';
// ===========================================================================
// v1 `.set_yaml` conditional-field evaluator (show_if / required_if / option
// enable_if)
// ===========================================================================
//
// The evaluator is exported for the bundled condition-model harness.
// Renderer package imports are handled by the canonical SDK compiler.
//
// These functions are a dependency-free mirror of the Python source of truth in
// src/collimate/config_values.py, pinned cross-language by
// shared/set-yaml-conditions-contract.json; when the semantics change, update
// that fixture first, then this section and its Python twin together.

/** The minimal shape the evaluator reads off a field declaration. Kept
 *  permissive (all-optional + index signature) so the real FieldSchema below is
 *  structurally assignable to it. */
export interface FieldLike {
    key?: string;
    type?: string;
    label?: string;
    required?: boolean;
    show_if?: any;
    required_if?: any;
    options?: any[];
    [k: string]: any;
}

/** Sentinel for a referent value that is absent, empty, or hidden. Distinct
 *  from every legal canonical value; UNKNOWN never satisfies a match
 *  (three-valued logic), which is what makes hidden referents and reference
 *  cycles resolve to the safe polarity. Compare with ===. */
export const UNKNOWN: unique symbol = Symbol('UNKNOWN');
export type Canonical = string | number | typeof UNKNOWN;

// Truthy folds. checkbox and the dropdown YAML-1.1 bool-word fold are kept
// separate: a checkbox operand may be int 1/0, a dropdown one never is.
const CHECKBOX_TRUE = new Set(['true', 'on', 'yes', '1']);
const CHECKBOX_FALSE = new Set(['false', 'off', 'no', '0']);
const BOOL_WORD_TRUE = new Set(['on', 'yes', 'true']);
const BOOL_WORD_FALSE = new Set(['off', 'no', 'false']);

/** Python-style truthiness for the value shapes a condition carries
 *  (None/dict/list/str/bool/number), so `if show_if:`, `not enable_if`, and
 *  `if required_if:` fold exactly as config_values.py does — an empty map
 *  `{}` is FALSY (an empty condition is inert here; lint rejects it
 *  separately). */
function truthy(v: any): boolean {
    if (v == null || v === false || v === '' || v === 0) return false;
    if (Array.isArray(v)) return v.length > 0;
    if (typeof v === 'object') return Object.keys(v).length > 0;
    return true;
}

// --------------------------------------------------------------------------- //
// Type-aware canonicalization (mirrors config_values.canon and helpers).
// --------------------------------------------------------------------------- //

function checkboxCanon(value: any): Canonical {
    if (value == null) return UNKNOWN;
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (typeof value === 'string') {
        const s = value.trim().toLowerCase();
        if (s === '') return UNKNOWN;
        if (CHECKBOX_TRUE.has(s)) return 'true';
        if (CHECKBOX_FALSE.has(s)) return 'false';
        return UNKNOWN; // unrecognized token -> safe polarity
    }
    if (typeof value === 'number') {
        if (value === 1) return 'true';
        if (value === 0) return 'false';
        return UNKNOWN;
    }
    return UNKNOWN;
}

function numberCanon(value: any): Canonical {
    if (value == null || typeof value === 'boolean') return UNKNOWN;
    let n: number;
    if (typeof value === 'number') {
        if (!Number.isFinite(value)) return UNKNOWN;
        n = value;
    } else if (typeof value === 'string') {
        const s = value.trim();
        if (s === '') return UNKNOWN;
        n = Number(s);
        if (!Number.isFinite(n)) return UNKNOWN;
    } else {
        return UNKNOWN;
    }
    // JS unifies int/float, so 0 === "0" === 0.0 compare equal without a collapse.
    return n;
}

function enumCanon(value: any): Canonical {
    if (value == null) return UNKNOWN;
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    const s = String(value).trim();
    if (s === '') return UNKNOWN;
    const low = s.toLowerCase();
    if (BOOL_WORD_TRUE.has(low)) return 'true';
    if (BOOL_WORD_FALSE.has(low)) return 'false';
    return low;
}

/** Type-aware canonicalization of a condition operand or referent value.
 *  Coerces `value` into the one canonical domain of `fieldType` so the three
 *  ingestion surfaces (renderer String(v), frontend/server raw bool/number)
 *  compare equal. Returns UNKNOWN for anything that cannot be a meaningful
 *  referent value. text/textarea/list/object/unknown are illegal referents ->
 *  UNKNOWN (runtime stays on the safe polarity by never matching). */
export function canon(value: any, fieldType?: string): Canonical {
    if (value === UNKNOWN) return UNKNOWN;
    if (fieldType === 'checkbox') return checkboxCanon(value);
    if (fieldType === 'number') return numberCanon(value);
    if (fieldType === 'dropdown') return enumCanon(value);
    return UNKNOWN;
}

/** Mirror the renderer's String(v) normalization of an option value: bool ->
 *  lowercase word, null/undefined -> "", else String. */
function optionValueStr(value: any): string {
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (value == null) return '';
    return String(value);
}

/** Build the {key: declared_type} index conditions coerce against. */
function typeIndex(fields: FieldLike[]): Record<string, string | undefined> {
    const out: Record<string, string | undefined> = {};
    for (const f of fields) {
        if (f != null && typeof f === 'object' && 'key' in f) out[(f as any).key] = f.type;
    }
    return out;
}

// --------------------------------------------------------------------------- //
// Condition evaluation.
// --------------------------------------------------------------------------- //

/** Three-valued (Kleene) result: a definite boolean, or UNKNOWN. */
type Tri = boolean | typeof UNKNOWN;
const COMBINATORS = ['any', 'all', 'not'];

/** Evaluate a set_yaml condition against a resolved value map. A v1 FieldMap
 *  {field: scalar-or-list} (AND across keys, OR within a list) or a v2
 *  combinator {any:[...]} / {all:[...]} / {not: cond}. A hidden (per `visible`)
 *  or absent referent contributes UNKNOWN. Coerces the three-valued result to
 *  the safe polarity — only a definite TRUE yields true. Returns false (never
 *  raises) on malformed input: non-object / empty {} / a combinator mixed with
 *  field keys / any|all without a non-empty list. Generalizes v1 exactly. */
export function matches(
    cond: any,
    resolved: Record<string, any>,
    visible: Record<string, boolean> | null | undefined,
    fieldTypes: Record<string, string | undefined> | null | undefined,
): boolean {
    return eval3(cond, resolved, visible, fieldTypes || {}) === true;
}

function eval3(
    cond: any,
    resolved: Record<string, any>,
    visible: Record<string, boolean> | null | undefined,
    ft: Record<string, string | undefined>,
): Tri {
    if (cond == null || typeof cond !== 'object' || Array.isArray(cond)) return false;
    const keys = Object.keys(cond);
    if (keys.length === 0) return false;
    const combos = COMBINATORS.filter((k) => k in cond);
    if (combos.length > 0) {
        if (keys.length !== 1) return false; // a combinator must not mix with field keys
        const op = combos[0];
        const arg = (cond as any)[op];
        if (op === 'not') {
            const r = eval3(arg, resolved, visible, ft);
            return r === UNKNOWN ? UNKNOWN : !r;
        }
        if (!Array.isArray(arg) || arg.length === 0) return false;
        const subs: Tri[] = arg.map((c) => eval3(c, resolved, visible, ft));
        if (op === 'all') { // Kleene AND: FALSE dominates, else UNKNOWN, else TRUE
            if (subs.some((s) => s === false)) return false;
            return subs.some((s) => s === UNKNOWN) ? UNKNOWN : true;
        }
        // any — Kleene OR: TRUE dominates, else UNKNOWN, else FALSE
        if (subs.some((s) => s === true)) return true;
        return subs.some((s) => s === UNKNOWN) ? UNKNOWN : false;
    }
    // v1 flat FieldMap — Kleene AND across keys.
    let result: Tri = true;
    for (const ref of keys) {
        const r = evalTerm(ref, (cond as any)[ref], resolved, visible, ft);
        if (r === false) return false;
        if (r === UNKNOWN) result = UNKNOWN;
    }
    return result;
}

function evalTerm(
    ref: string,
    acceptable: any,
    resolved: Record<string, any>,
    visible: Record<string, boolean> | null | undefined,
    ft: Record<string, string | undefined>,
): Tri {
    const ftype = ft[ref];
    let rv: Canonical;
    if (visible != null && !visible[ref]) {
        rv = UNKNOWN; // not-yet-visible referent
    } else if (!Object.prototype.hasOwnProperty.call(resolved, ref)) {
        rv = UNKNOWN; // absent referent
    } else {
        rv = canon(resolved[ref], ftype);
    }
    if (rv === UNKNOWN) return UNKNOWN;
    const operands = Array.isArray(acceptable) ? acceptable : [acceptable];
    for (const a of operands) {
        if (rv === canon(a, ftype)) return true;
    }
    return false;
}

/** Monotone fixpoint returning {field_key: bool}. Starts all-hidden (safe
 *  polarity) and sweeps at most len(fields) + 1 times, flipping a field to
 *  visible only when it has no condition or its show_if matches against the
 *  currently-visible referents. A not-yet-visible referent contributes
 *  UNKNOWN, so cycles resolve deterministically to all-hidden and transitive
 *  hiding is automatic. */
export function computeVisibility(
    fields: FieldLike[],
    resolved: Record<string, any>,
): Record<string, boolean> {
    const fieldTypes = typeIndex(fields);
    const visible: Record<string, boolean> = {};
    const keyed: FieldLike[] = [];
    for (const f of fields) {
        if (f != null && typeof f === 'object' && 'key' in f) {
            visible[(f as any).key] = false;
            keyed.push(f);
        }
    }
    let changed = true;
    let passes = 0;
    const n = fields.length;
    while (changed && passes <= n) {
        changed = false;
        passes += 1;
        for (const f of keyed) {
            const key = (f as any).key;
            const cond = f.show_if;
            const next = truthy(cond) ? matches(cond, resolved, visible, fieldTypes) : true;
            if (next !== visible[key]) {
                visible[key] = next;
                changed = true;
            }
        }
    }
    return visible;
}

/** A field is effectively required iff it is visible AND (required: true OR its
 *  required_if matches). required and required_if compose with OR — required_if
 *  never relaxes a static required. A hidden field is never required. */
export function effectiveRequired(
    field: FieldLike,
    fields: FieldLike[],
    resolved: Record<string, any>,
    visible: Record<string, boolean>,
    fieldTypes?: Record<string, string | undefined> | null,
): boolean {
    const key = field.key as string;
    if (!visible[key]) return false;
    if (field.required === true) return true;
    const reqIf = field.required_if;
    if (truthy(reqIf)) {
        const ft = fieldTypes || typeIndex(fields);
        return matches(reqIf, resolved, visible, ft);
    }
    return false;
}

/** String-form values of a dropdown's currently-enabled options. Bare scalar
 *  options are always enabled. Object options {value, label?, description?,
 *  enable_if?} are enabled iff they carry no enable_if or its condition matches
 *  (an UNKNOWN referent leaves the option disabled — safe polarity).
 *  Non-dropdown / optionless fields -> []. */
export function enabledOptions(
    field: FieldLike,
    resolved: Record<string, any>,
    visible: Record<string, boolean> | null | undefined,
    fieldTypes: Record<string, string | undefined> | null | undefined,
): string[] {
    if (field == null || typeof field !== 'object' || field.type !== 'dropdown') return [];
    const out: string[] = [];
    for (const opt of field.options || []) {
        let ok: boolean;
        let val: any;
        if (opt != null && typeof opt === 'object' && !Array.isArray(opt)) {
            const enableIf = (opt as any).enable_if;
            ok = !truthy(enableIf) || matches(enableIf, resolved, visible, fieldTypes);
            val = (opt as any).value;
        } else {
            ok = true;
            val = opt;
        }
        if (ok) out.push(optionValueStr(val));
    }
    return out;
}

/** [{key, label, value}] for each VISIBLE dropdown whose resolved value
 *  canon-equals (dropdown domain) a DECLARED option that is currently disabled.
 *  Runs after visibility — options never feed back into it; hidden fields
 *  contribute nothing. A value matching NO declared option is not an
 *  invalid_option (that is the advisory membership error, handled in validate). */
export function invalidOptionViolations(
    fields: FieldLike[],
    resolved: Record<string, any>,
    visible: Record<string, boolean>,
    fieldTypes?: Record<string, string | undefined> | null,
): Array<{ key: string; label: string; value: string }> {
    const ft = fieldTypes || typeIndex(fields);
    const out: Array<{ key: string; label: string; value: string }> = [];
    for (const field of fields) {
        if (field == null || typeof field !== 'object' || field.type !== 'dropdown') continue;
        const key = field.key as string;
        if (!visible[key]) continue;
        const rvc = canon(resolved[key], 'dropdown');
        if (rvc === UNKNOWN) continue; // empty/absent value is not a disabled-option violation
        const enabled = new Set(enabledOptions(field, resolved, visible, ft));
        for (const opt of field.options || []) {
            const optval = (opt != null && typeof opt === 'object' && !Array.isArray(opt)) ? (opt as any).value : opt;
            if (canon(optval, 'dropdown') === rvc) { // matched a declared option
                const s = optionValueStr(optval);
                if (!enabled.has(s)) out.push({ key, label: (field.label as string) || key, value: s });
                break;
            }
        }
    }
    return out;
}

/** Coerce a raw YAML value into the renderer's per-type value domain (what the
 *  form holds in state). Mirrors the schema_form field types. Exported so the
 *  Node condition test harness can apply the renderer's value ingestion (over a
 *  contract's values_renderer block) without pulling in this CDN-ESM module. */
export function coerceValue(v: any, f: FieldLike): any {
    switch (f.type) {
        case 'checkbox':
            return checkboxCanon(v) === 'true';
        case 'number': {
            if (v === '' || v == null) return null;
            const n = typeof v === 'number' ? v : Number(v);
            return Number.isFinite(n) ? n : null;
        }
        case 'list':
            if (Array.isArray(v)) return v.map((x) => (x == null ? '' : String(x)));
            if (v == null || v === '') return [];
            return [String(v)];
        case 'object':
            return v == null ? null : v;
        case 'text':
        case 'textarea':
        case 'dropdown':
        default:
            if (v == null) return '';
            return String(v);
    }
}

/** Per-type "empty" test for a required gate. Mirrors the frontend's
 *  isEmptyValue (src/lib/setYaml.ts) and collimate.config_values.is_empty,
 *  pinned by shared/empty-values-contract.json: checkbox is never empty;
 *  number/object only when null; dropdown when null or ''; list when null or
 *  []; text/textarea/unknown when null or blank after trim. */
export function isEmptyValue(value: any, fieldType?: string): boolean {
    switch (fieldType) {
        case 'checkbox':
            return false;
        case 'number':
        case 'object':
            return value == null;
        case 'dropdown':
            return value == null || value === '';
        case 'list':
            return value == null || (Array.isArray(value) && value.length === 0);
        case 'text':
        case 'textarea':
        default:
            return value == null || (typeof value === 'string' && value.trim() === '');
    }
}

// ===========================================================================
// Schema definitions
// ===========================================================================

type FieldType = 'text' | 'textarea' | 'number' | 'checkbox' | 'dropdown' | 'list' | 'object';

interface FieldSchema {
    key: string;
    type: FieldType;
    label?: string;
    description?: string;
    required?: boolean;
    default?: any;
    options?: any[];
    secret?: boolean;
    min?: number;
    max?: number;
    // v1 conditional-field keys (see the condition evaluator section above
    // and shared/set-yaml-conditions-contract.json).
    show_if?: any;
    required_if?: any;
}

interface FormSchema {
    title: string;
    fields: FieldSchema[];
}

// ===========================================================================
// Mode detection
// ===========================================================================

type Mode = 'set';

export function modeFromFilename(filename: string): Mode | null {
    const lower = (filename || '').toLowerCase();
    if (lower.endsWith('.set_yaml')) return 'set';
    return null;
}

// ===========================================================================
// YAML helpers
// ===========================================================================

/** Whether a YAML stream holds a document: js-yaml throws on one that is
 *  empty, blank or only comments, which reads as no value here. */
function hasYamlDocument(text: string): boolean {
    return text.split(/\r\n|\r|\n/).some((line) => {
        const body = line.replace(/^\uFEFF/, '').trim();
        return body !== '' && !body.startsWith('#');
    });
}

function loadYaml(text: string): { doc: any; error: string | null } {
    if (!hasYamlDocument(text)) return { doc: {}, error: null };
    let parsed: any;
    try {
        parsed = jsyaml.load(text);
    } catch (e: any) {
        return { doc: {}, error: (e && e.message) || String(e) };
    }
    if (parsed == null) return { doc: {}, error: null };
    if (typeof parsed !== 'object' || Array.isArray(parsed)) {
        return { doc: {}, error: 'Top level must be a mapping' };
    }
    return { doc: parsed, error: null };
}

function dumpYaml(obj: any): string {
    return jsyaml.dump(obj, { lineWidth: 100, noRefs: true, sortKeys: false });
}

// ===========================================================================
// Build initial form state
// ===========================================================================

interface FormState {
    schema: FormSchema;
    // form value per schema-field key (raw, untyped — strings/bools/numbers/arrays)
    values: Record<string, any>;
    // for .set: the user-authored fields[] array, preserved verbatim on save
    rawFields: any[];
    // for .object fields: the raw YAML text the user is currently editing (per field key)
    objectText: Record<string, string>;
    // set when the file does not parse as a YAML mapping; the form then
    // shows a banner and never saves (a save would rebuild the doc from
    // defaults and drop every key)
    parseError: string | null;
}

function valueToObjectText(v: any): string {
    if (v == null) return '';
    return dumpYaml(v).trimEnd();
}

function buildInitialState(content: string): FormState {
    const { doc: parsed, error } = loadYaml(content);
    const rawFields: any[] = Array.isArray(parsed.fields) ? parsed.fields : [];
    const valuesBlock: Record<string, any> = (parsed.values && typeof parsed.values === 'object') ? parsed.values : {};
    const fields: FieldSchema[] = rawFields.map(normalizeFieldDecl);
    const values: Record<string, any> = {};
    const objectText: Record<string, string> = {};
    for (const f of fields) {
        const present = Object.prototype.hasOwnProperty.call(valuesBlock, f.key);
        const v = present ? valuesBlock[f.key] : f.default;
        values[f.key] = coerceValue(v, f);
        if (f.type === 'object') objectText[f.key] = valueToObjectText(v);
    }
    return {
        schema: { title: 'Settings', fields },
        values,
        rawFields,
        objectText,
        parseError: error,
    };
}


/** A dropdown option normalized from either authoring shape: a plain
 *  scalar (`options: [modify, create]`)
 *  or the `{value, label, description}` mapping documented for
 *  .set_yaml fields in how-to-write-a-generator.md. */
interface DropdownOption {
    value: string;
    label: string;
    description?: string;
    // v1 per-option gate: an object option is disabled unless enable_if matches
    // (see enabledOptions above). Bare scalar options carry none.
    enable_if?: any;
}

function normalizeOptions(opts: any[] | undefined): DropdownOption[] {
    return (opts || []).map((o) => {
        if (o != null && typeof o === 'object' && !Array.isArray(o)) {
            const value = o.value == null ? '' : String(o.value);
            return {
                value,
                label: o.label == null ? value : String(o.label),
                description: o.description == null ? undefined : String(o.description),
                enable_if: o.enable_if,
            };
        }
        const s = o == null ? '' : String(o);
        return { value: s, label: s };
    });
}

function normalizeFieldDecl(decl: any): FieldSchema {
    return {
        key: String(decl.key || ''),
        type: (decl.type || 'text') as FieldType,
        label: decl.label,
        description: decl.description,
        required: !!decl.required,
        default: decl.default,
        options: Array.isArray(decl.options) ? decl.options : undefined,
        secret: !!decl.secret,
        min: typeof decl.min === 'number' ? decl.min : undefined,
        max: typeof decl.max === 'number' ? decl.max : undefined,
        // Conditions are carried verbatim; both round-trip untouched via
        // state.rawFields.
        show_if: decl.show_if,
        required_if: decl.required_if,
    };
}

// coerceValue is defined and exported in the condition evaluator section above,
// so the Node condition test harness can apply the renderer's value ingestion
// without importing this CDN-ESM module.

// ===========================================================================
// Validation
// ===========================================================================

function validate(
    state: FormState,
    visible: Record<string, boolean>,
    fieldTypes: Record<string, string | undefined>,
): Record<string, string> {
    const errors: Record<string, string> = {};
    const fields = state.schema.fields;
    // Conditions evaluate against the live value map. Visibility is derived
    // (not stored) by the caller; a hidden field runs NO validation — it is
    // skipped as the first statement below, before every type-keyed branch.
    // Dropdowns whose current value canon-equals a declared-but-disabled option.
    const invalidKeys = new Set(
        invalidOptionViolations(fields, state.values, visible, fieldTypes).map((vi) => vi.key)
    );
    for (const f of fields) {
        if (!visible[f.key]) continue;
        const v = state.values[f.key];
        // Effective-required: static required OR required_if matches (OR-compose).
        const required = effectiveRequired(f, fields, state.values, visible, fieldTypes);
        if (required) {
            const fv = f.type === 'object'
                ? ((state.objectText[f.key] || '').trim() === '' ? null : state.objectText[f.key])
                : v;
            if (isEmptyValue(fv, f.type)) {
                errors[f.key] = 'Required';
                continue;
            }
        }
        if (f.type === 'dropdown' && invalidKeys.has(f.key)) {
            errors[f.key] = 'Not selectable with the current settings';
            continue;
        }
        if (f.type === 'dropdown' && f.options && v) {
            const opts = normalizeOptions(f.options);
            if (!opts.some((o) => o.value === String(v))) {
                errors[f.key] = `Must be one of: ${opts.map((o) => o.value).join(', ')}`;
            }
        }
        if (f.type === 'number' && v != null && v !== '') {
            if (typeof v !== 'number' || !Number.isFinite(v)) {
                errors[f.key] = 'Must be a number';
            } else {
                if (typeof f.min === 'number' && v < f.min) errors[f.key] = `Must be ≥ ${f.min}`;
                if (typeof f.max === 'number' && v > f.max) errors[f.key] = `Must be ≤ ${f.max}`;
            }
        }
        if (f.type === 'object' && hasYamlDocument(state.objectText[f.key] || '')) {
            try {
                jsyaml.load(state.objectText[f.key]);
            } catch (e: any) {
                errors[f.key] = `Invalid YAML: ${e.message || e}`;
            }
        }
    }
    return errors;
}

// ===========================================================================
// Serialize form state back to a YAML document
// ===========================================================================

function serialize(state: FormState): string {
    const valuesBlock: Record<string, any> = {};
    // Deliberately walks ALL fields, hidden included: a hidden field keeps
    // its resolved value in values: so a choice survives a content re-push
    // (no silent data loss). This is safe because every gate is
    // visibility-first — a hidden override is inert at each gate. Do NOT
    // filter by visibility here.
    for (const f of state.schema.fields) {
        let v = state.values[f.key];
        if (f.type === 'object') {
            const text = state.objectText[f.key] || '';
            try {
                v = hasYamlDocument(text) ? jsyaml.load(text) : null;
            } catch {
                v = null;
            }
        }
        valuesBlock[f.key] = stripEmpty(v, f);
    }
    const out: any = {
        values: valuesBlock,
        fields: state.rawFields,
    };
    return dumpYaml(out);
}


function stripEmpty(v: any, f: FieldSchema): any {
    if (f.type === 'number' && (v === null || v === '')) return null;
    if (f.type === 'list' && Array.isArray(v)) return v.filter((x) => x !== '');
    return v;
}

// ===========================================================================
// UI — colors and shared styles (inline only — Shadow DOM)
// ===========================================================================

// The form's colours, every one a skin token. Red marks errors and the
// destructive remove button; teal marks on-states (a ticked checkbox) and
// focus rings; a focused field's border steps up to line-2.
const C = Object.freeze({
    bg: tokens.color.sheet,
    panel: tokens.color.sheet2,
    input: tokens.color.sheet,
    border: tokens.color.line,
    borderStrong: tokens.color.line2,
    text: tokens.color.ink,
    textDim: tokens.color.ink2,
    textMuted: tokens.color.ink3,
    label: tokens.color.ink,
    on: tokens.color.teal,
    danger: tokens.color.red,
    dangerSoft: tokens.color.redSoft,
    raised: tokens.color.sheet2,
});

/** The colour map, exported for the model test. */
export const FORM_COLORS = C;

const F = tokens.font;
const R = {
    field: `calc(${tokens.radius} - 2px)`,
    button: `calc(${tokens.radius} - 3px)`,
    small: `calc(${tokens.radius} - 4px)`,
};

const control = {
    width: '100%', background: C.input, border: `1px solid ${C.border}`, borderRadius: R.field,
    color: C.text, fontFamily: F.mono, fontSize: 12, outline: 'none', boxSizing: 'border-box' as const,
};

const S = {
    page: { width: '100%', height: '100%', background: C.bg, color: C.text, fontFamily: F.ui, fontSize: 13, overflow: 'auto' as const, boxSizing: 'border-box' as const },
    header: { padding: '14px 20px 10px', borderBottom: `1px solid ${C.border}`, background: C.panel, position: 'sticky' as const, top: 0, zIndex: 2 },
    title: { fontSize: 14, fontWeight: 600, color: C.label },
    subtitle: { fontSize: 11.5, color: C.textMuted, marginTop: 2, fontFamily: F.mono },
    body: { padding: 16, maxWidth: 720, margin: '0 auto' },
    field: { marginBottom: 18 },
    labelRow: { display: 'flex', alignItems: 'baseline', gap: 6, marginBottom: 4 },
    label: { color: C.label, fontWeight: 500, fontSize: 12.5 },
    requiredMark: { color: C.danger, fontWeight: 700, fontSize: 12 },
    desc: { color: C.textDim, fontSize: 12, lineHeight: 1.5, marginBottom: 6 },
    input: { ...control, padding: '7px 10px' },
    textarea: { ...control, padding: '8px 10px', resize: 'vertical' as const, minHeight: 60 },
    select: { ...control, padding: '7px 10px' },
    // Merged over input/textarea/select when validate() flagged the field
    // (required-empty being the main case) so the offender is visible at
    // a glance, not just via the small message underneath.
    inputDanger: { border: `1px solid ${C.danger}`, background: C.dangerSoft },
    error: { color: C.danger, fontSize: 11.5, marginTop: 4, fontFamily: F.mono },
    listRow: { display: 'flex', gap: 6, marginBottom: 4 },
    button: { background: 'none', border: `1px solid ${C.borderStrong}`, borderRadius: R.button, height: 24, padding: '0 9px', color: C.textDim, fontFamily: F.ui, fontSize: 12, cursor: 'pointer' },
    buttonDanger: { background: 'none', border: `1px solid ${C.borderStrong}`, borderRadius: R.button, height: 30, padding: '0 10px', color: C.danger, fontFamily: F.ui, fontSize: 13, cursor: 'pointer' },
    checkboxRow: { display: 'flex', alignItems: 'center', gap: 8 },
    checkbox: { width: 14, height: 14, accentColor: C.on, cursor: 'pointer' },
    badge: { display: 'inline-block', padding: '2px 6px', borderRadius: R.small, background: C.raised, color: C.textDim, fontFamily: F.mono, fontSize: 10.5 },
    // A notice (no fields declared) sits on a raised row; a failure (the
    // file does not parse, the extension is not ours) is red.
    banner: { padding: '8px 12px', background: C.raised, border: `1px solid ${C.borderStrong}`, color: C.textDim, fontSize: 12, borderRadius: R.field, marginBottom: 12 },
    bannerError: { padding: '8px 12px', background: C.dangerSoft, border: `1px solid color-mix(in srgb, ${C.danger} 40%, ${C.border})`, color: C.danger, fontSize: 12, borderRadius: R.field, marginBottom: 12, fontFamily: F.mono },
};

// Pseudo-class rules inline styles cannot express, injected once into the
// shadow root. A control's inline border would win over a sheet rule, so the
// focus border is !important.
export const FORM_CSS = `
[data-sf-control]:focus { border-color: ${C.borderStrong} !important; }
[data-sf-button]:hover { background: ${C.raised} !important; color: ${C.text} !important; }
[data-sf-button="danger"]:hover { background: ${C.dangerSoft} !important; color: ${C.danger} !important; }
[data-sf-button]:focus-visible { outline: 2px solid ${C.on}; outline-offset: 2px; }
[data-sf-control]::placeholder { color: ${C.textMuted}; }
`;

const styledApis = new WeakSet<object>();
function injectFormCss(api: any) {
    if (!api || typeof api.injectCss !== 'function' || styledApis.has(api)) return;
    styledApis.add(api);
    api.injectCss(FORM_CSS);
}

// ===========================================================================
// Field renderers
// ===========================================================================

interface FieldProps {
    field: FieldSchema;
    value: any;
    objectText?: string;
    error?: string;
    readOnly: boolean;
    onChange: (v: any) => void;
    onObjectTextChange?: (text: string) => void;
    // For dropdowns: the string-form values of the currently-enabled options
    // (computed once in the render loop). undefined for non-dropdown fields.
    enabledOptionValues?: string[];
}

function FieldView({ field, value, objectText, error, readOnly, onChange, onObjectTextChange, enabledOptionValues }: FieldProps) {
    return (
        <div style={S.field}>
            <div style={S.labelRow}>
                <span style={S.label}>{field.label || field.key}</span>
                {field.required && <span style={S.requiredMark} title="Required">*</span>}
                <span style={{ marginLeft: 'auto', ...S.badge }}>{field.type}</span>
            </div>
            {field.description && <div style={S.desc}>{field.description}</div>}
            {renderInput(field, value, objectText, error, readOnly, onChange, onObjectTextChange, enabledOptionValues)}
            {error && <div style={S.error}>{error}</div>}
        </div>
    );
}

function renderInput(
    field: FieldSchema,
    value: any,
    objectText: string | undefined,
    error: string | undefined,
    readOnly: boolean,
    onChange: (v: any) => void,
    onObjectTextChange?: (text: string) => void,
    enabledOptionValues?: string[]
) {
    const danger = error ? S.inputDanger : undefined;
    switch (field.type) {
        case 'textarea':
            return (
                <textarea
                    style={{ ...S.textarea, ...danger }} data-sf-control=""
                    value={value ?? ''}
                    disabled={readOnly}
                    onChange={(e) => onChange(e.target.value)}
                    spellCheck={false}
                />
            );
        case 'number':
            return (
                <input
                    type="number"
                    style={{ ...S.input, ...danger }} data-sf-control=""
                    value={value == null ? '' : value}
                    disabled={readOnly}
                    min={field.min}
                    max={field.max}
                    onChange={(e) => {
                        const raw = e.target.value;
                        if (raw === '') onChange(null);
                        else {
                            const n = Number(raw);
                            onChange(Number.isFinite(n) ? n : null);
                        }
                    }}
                />
            );
        case 'checkbox':
            return (
                <div style={S.checkboxRow}>
                    <input
                        type="checkbox"
                        style={S.checkbox}
                        checked={!!value}
                        disabled={readOnly}
                        onChange={(e) => onChange(e.target.checked)}
                    />
                    <span style={{ color: C.textDim, fontFamily: F.mono, fontSize: 11.5 }}>
                        {value ? 'true' : 'false'}
                    </span>
                </div>
            );
        case 'dropdown': {
            // enable_if-disabled options grey out but keep rendering, so a
            // selected-but-now-disabled value stays visible (validate flags it).
            const enabledSet = enabledOptionValues ? new Set(enabledOptionValues) : null;
            return (
                <select
                    style={{ ...S.select, ...danger }} data-sf-control=""
                    value={value ?? ''}
                    disabled={readOnly}
                    onChange={(e) => onChange(e.target.value)}
                >
                    {!field.required && <option value="">— none —</option>}
                    {normalizeOptions(field.options).map((opt) => (
                        <option
                            key={opt.value}
                            value={opt.value}
                            title={opt.description}
                            disabled={enabledSet ? !enabledSet.has(opt.value) : false}
                        >
                            {opt.label}
                        </option>
                    ))}
                </select>
            );
        }
        case 'list':
            return <ListField value={value} readOnly={readOnly} onChange={onChange} />;
        case 'object':
            return (
                <textarea
                    style={{ ...S.textarea, minHeight: 100, ...danger }} data-sf-control=""
                    value={objectText ?? ''}
                    disabled={readOnly}
                    onChange={(e) => onObjectTextChange && onObjectTextChange(e.target.value)}
                    spellCheck={false}
                    placeholder="# raw YAML"
                />
            );
        case 'text':
        default:
            return (
                <input
                    type={field.secret ? 'password' : 'text'}
                    style={{ ...S.input, ...danger }} data-sf-control=""
                    value={value ?? ''}
                    disabled={readOnly}
                    onChange={(e) => onChange(e.target.value)}
                    spellCheck={false}
                    autoComplete={field.secret ? 'off' : undefined}
                />
            );
    }
}

function ListField({ value, readOnly, onChange }: { value: any; readOnly: boolean; onChange: (v: any) => void }) {
    const items: string[] = Array.isArray(value) ? value : [];
    const update = (idx: number, v: string) => {
        const next = items.slice();
        next[idx] = v;
        onChange(next);
    };
    const remove = (idx: number) => {
        const next = items.slice();
        next.splice(idx, 1);
        onChange(next);
    };
    const add = () => onChange([...items, '']);

    return (
        <div>
            {items.length === 0 && (
                <div style={{ color: C.textMuted, fontSize: 11.5, fontFamily: F.mono, marginBottom: 4 }}>
                    (empty)
                </div>
            )}
            {items.map((it, idx) => (
                <div key={idx} style={S.listRow}>
                    <input
                        type="text"
                        style={{ ...S.input, flex: 1 }} data-sf-control=""
                        value={it}
                        disabled={readOnly}
                        onChange={(e) => update(idx, e.target.value)}
                        spellCheck={false}
                    />
                    {!readOnly && (
                        <button type="button" style={S.buttonDanger} data-sf-button="danger" aria-label="Remove item" onClick={() => remove(idx)}>
                            ×
                        </button>
                    )}
                </div>
            ))}
            {!readOnly && (
                <button type="button" style={S.button} data-sf-button="" onClick={add}>
                    + add
                </button>
            )}
        </div>
    );
}

// ===========================================================================
// Top-level component
// ===========================================================================

function SchemaForm() {
    const { content, saveContent, isReadOnly, api } = useCollimateFile();
    const filename = api.getFilename();
    const mode = modeFromFilename(filename);

    // Initial form state; host content pushes rebuild it in the [content]
    // effect below, and the initialContent ref skips our own save echo.
    const initialContent = useRef(content);
    const [state, setState] = useState<FormState | null>(() => {
        if (!mode) return null;
        return buildInitialState(content || '');
    });

    // Host content push (external file replacement): rebuild the form from
    // the new YAML in place — no native remount. `content` updates via
    // api.onSetContent (wired in defineRenderer). The initialContent ref
    // guard skips our own saveContent echo so typing doesn't reset state.
    useEffect(() => {
        if (!mode) return;
        if (initialContent.current === content) return;
        initialContent.current = content;
        setState(buildInitialState(content || ''));
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [content]);

    // Every field edit publishes its serialized snapshot immediately.
    const saveRaw = saveContent;

    // Focus, hover and placeholder rules (pseudo-classes) for the shadow root.
    useEffect(() => { injectFormCss(api); }, [api]);

    const scheduleSave = useCallback(
        (next: FormState) => {
            if (!mode) return;
            if (next.parseError) return;
            const yamlText = serialize(next);
            initialContent.current = yamlText;
            saveRaw(yamlText);
        },
        [mode, saveRaw]
    );

    if (!mode) {
        return (
            <div style={S.page}>
                <div style={S.body}>
                    <div style={S.bannerError}>
                        Schema Form was opened on a file with an unrecognised extension: {filename}
                    </div>
                </div>
            </div>
        );
    }

    if (!state) return null; // unreachable: state is null only when mode is null

    if (state.parseError) {
        return (
            <div style={S.page}>
                <div style={S.header}>
                    <div style={S.title}>{state.schema.title}</div>
                    <div style={S.subtitle}>{filename}</div>
                </div>
                <div style={S.body}>
                    <div style={S.bannerError}>
                        {`Could not parse this file as YAML: ${state.parseError}. Fix it in the text editor; the form will not save until it parses.`}
                    </div>
                </div>
            </div>
        );
    }

    // Derived once per render (visibility is a pure function of the values).
    // Hidden fields are not rendered or validated; dropdown enable_if is
    // evaluated against the same visible map.
    const visible = computeVisibility(state.schema.fields, state.values);
    const fieldTypes = typeIndex(state.schema.fields);

    const errors = validate(state, visible, fieldTypes);
    const errorCount = Object.keys(errors).length;

    const updateValue = (key: string, v: any) => {
        const next: FormState = {
            ...state,
            values: { ...state.values, [key]: v },
        };
        setState(next);
        scheduleSave(next);
    };

    const updateObjectText = (key: string, text: string) => {
        const next: FormState = {
            ...state,
            objectText: { ...state.objectText, [key]: text },
        };
        setState(next);
        scheduleSave(next);
    };

    return (
        <div style={S.page}>
            <div style={S.header}>
                <div style={S.title}>{state.schema.title}</div>
                <div style={S.subtitle}>
                    {filename}
                    {errorCount > 0 && (
                        <span style={{ color: C.danger, marginLeft: 12 }}>
                            {errorCount} error{errorCount === 1 ? '' : 's'}
                        </span>
                    )}
                    {isReadOnly && <span style={{ color: C.textMuted, marginLeft: 12 }}>read-only</span>}
                </div>
            </div>
            <div style={S.body}>
                {state.schema.fields.length === 0 && (
                    <div style={S.banner}>
                        This settings file has no <code>fields:</code> block. The author should declare a list of
                        fields with <code>key</code>, <code>type</code>, and (optionally) <code>label</code>,{' '}
                        <code>default</code>, <code>options</code>, etc.
                    </div>
                )}
                {state.schema.fields.map((f) => {
                    if (!visible[f.key]) return null;
                    const enabledOptionValues = f.type === 'dropdown'
                        ? enabledOptions(f, state.values, visible, fieldTypes)
                        : undefined;
                    return (
                        <FieldView
                            key={f.key}
                            field={f}
                            value={state.values[f.key]}
                            objectText={state.objectText[f.key]}
                            error={errors[f.key]}
                            readOnly={isReadOnly}
                            onChange={(v) => updateValue(f.key, v)}
                            onObjectTextChange={(t) => updateObjectText(f.key, t)}
                            enabledOptionValues={enabledOptionValues}
                        />
                    );
                })}
            </div>
        </div>
    );
}

export default defineRenderer({ component: SchemaForm });
