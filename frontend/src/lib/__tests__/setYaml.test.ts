import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import {
    computeVisibility,
    configViolations,
    effectiveValues,
    enabledOptions,
    invalidOptionFields,
    isEmptyValue,
    matches,
    missingRequiredFields,
    normalizeOptions,
} from '../setYaml';
import type { ParsedSetYaml, SetYamlField, SetYamlFieldType } from '../setYaml';

/** A field as the runner catalog projects it: the raw descriptor, with
 *  `options` still in authoring shape and `type` unchecked. */
interface RawField {
    key: string;
    type: string;
    [k: string]: unknown;
}

/** Fields reach the app pre-parsed but un-normalized, so fixtures fold
 *  through the same normalizer production does (runnerApi.ts) rather
 *  than a YAML parser. */
function fixture(fields: RawField[], values: Record<string, unknown> = {}): ParsedSetYaml {
    return {
        fields: fields.map(f => ({
            ...f,
            type: f.type as SetYamlFieldType,
            options: normalizeOptions(f.options),
        })) as SetYamlField[],
        values,
    };
}

// Synthetic fixture — mirrors the .set_yaml shape (fields + values
// blocks) without referencing any shipped generator's file.
const DOC = fixture([
    { key: 'model', type: 'text', label: 'Model', required: true, default: 'tiny-1' },
    { key: 'max_tokens', type: 'number', label: 'Max tokens', required: true, default: 1024 },
    { key: 'mode', type: 'dropdown', default: 'fast' },
    { key: 'verbose', type: 'checkbox', default: false },
    { key: 'extras', type: 'list' },
    { key: 'blob', type: 'object' },
], { model: 'big-9' });

describe('normalizeOptions', () => {
    it('keeps object options and lifts bare scalars to {value,label}', () => {
        expect(normalizeOptions([
            'a',
            { value: 'b', label: 'Bee', description: 'the letter b', enable_if: { x: 1 } },
        ])).toEqual([
            { value: 'a', label: 'a' },
            { value: 'b', label: 'Bee', description: 'the letter b', enable_if: { x: 1 } },
        ]);
    });

    it('drops a non-array options value', () => {
        expect(normalizeOptions(undefined)).toBeUndefined();
        expect(normalizeOptions('a')).toBeUndefined();
    });

    it('maps a null scalar to the empty option', () => {
        expect(normalizeOptions([null])).toEqual([{ value: '', label: '' }]);
    });
});

describe('effectiveValues', () => {
    it('layers per-type empty base ⊕ defaults ⊕ values block', () => {
        const v = effectiveValues(DOC);
        expect(v).toEqual({
            model: 'big-9',      // values block beats default
            max_tokens: 1024,    // default
            mode: 'fast',
            verbose: false,
            extras: [],          // empty base (no default)
            blob: null,          // empty base (no default)
        });
    });

    it('values block keys win even when not declared as fields', () => {
        const v = effectiveValues(fixture([], { stray: 7 }));
        expect(v).toEqual({ stray: 7 });
    });
});

describe('isEmptyValue — the shared empty definition', () => {
    it('text/textarea: null and trimmed-blank are empty', () => {
        expect(isEmptyValue(null, 'text')).toBe(true);
        expect(isEmptyValue(undefined, 'textarea')).toBe(true);
        expect(isEmptyValue('', 'text')).toBe(true);
        expect(isEmptyValue('   \t', 'textarea')).toBe(true);
        expect(isEmptyValue('x', 'text')).toBe(false);
    });

    it('number: only null is empty — 0 is NOT', () => {
        expect(isEmptyValue(null, 'number')).toBe(true);
        expect(isEmptyValue(0, 'number')).toBe(false);
        expect(isEmptyValue(5, 'number')).toBe(false);
    });

    it('dropdown: null and "" are empty', () => {
        expect(isEmptyValue(null, 'dropdown')).toBe(true);
        expect(isEmptyValue('', 'dropdown')).toBe(true);
        expect(isEmptyValue('opt', 'dropdown')).toBe(false);
    });

    it('checkbox: never empty — false is NOT', () => {
        expect(isEmptyValue(false, 'checkbox')).toBe(false);
        expect(isEmptyValue(null, 'checkbox')).toBe(false);
    });

    it('list: null and [] are empty', () => {
        expect(isEmptyValue(null, 'list')).toBe(true);
        expect(isEmptyValue([], 'list')).toBe(true);
        expect(isEmptyValue(['a'], 'list')).toBe(false);
    });

    it('object: only null is empty', () => {
        expect(isEmptyValue(null, 'object')).toBe(true);
        expect(isEmptyValue({}, 'object')).toBe(false);
    });
});

// The SAME JSON table pins collimate.config_values.is_empty (SDK
// tests/test_config_values.py). It lives in the in-repo SDK at
// sdk/shared/. vitest runs with cwd=frontend, so resolve from this
// file's location rather than the cwd.
function sdkSharedPath(name: string): string {
    const here = path.dirname(fileURLToPath(import.meta.url));
    return path.resolve(here, '../../../../sdk/shared', name);
}

const contractPath = sdkSharedPath('empty-values-contract.json');
const hasContract = existsSync(contractPath);

describe('empty-values contract (shared fixture with the Python SDK)', () => {
    // Fail — never silently skip — when the shared fixture is missing (it is what
    // pins this mirror to the Python source of truth). It lives at
    // sdk/shared/empty-values-contract.json.
    it('the empty-values contract fixture is present in sdk/shared', () => {
        expect(hasContract).toBe(true);
    });
    const { cases } = (hasContract
        ? JSON.parse(readFileSync(contractPath, 'utf8'))
        : { cases: [] }) as {
        cases: { type: string; value: unknown; empty: boolean }[];
    };

    it('covers every field type', () => {
        const covered = new Set(cases.map(c => c.type));
        for (const t of [
            'text', 'textarea', 'number', 'checkbox', 'dropdown', 'list', 'object',
        ]) {
            expect(covered).toContain(t);
        }
    });

    // Unknown types are valid contract rows: isEmptyValue's default
    // branch applies text semantics to them.
    it.each(cases)('isEmptyValue($value, $type) === $empty', ({ type, value, empty }) => {
        expect(isEmptyValue(value, type as SetYamlFieldType)).toBe(empty);
    });
});

describe('missingRequiredFields', () => {
    it('returns [] when every required field resolves non-empty', () => {
        expect(missingRequiredFields(DOC)).toEqual([]);
    });

    it('flags required fields with empty effective values, using labels', () => {
        const parsed = fixture([
            { key: 'model', type: 'text', label: 'Model', required: true },
            { key: 'count', type: 'number', label: 'Count', required: true, default: 0 },
            { key: 'note', type: 'text', required: true },
        ], { note: '   ' });
        const missing = missingRequiredFields(parsed);
        // count: 0 is not empty; model has no default/value; note is blank.
        expect(missing).toEqual([
            { key: 'model', label: 'Model' },
            { key: 'note', label: 'note' },
        ]);
    });

    it('a values-block override clears the missing flag', () => {
        const parsed = fixture([
            { key: 'model', type: 'text', required: true },
        ], { model: 'filled' });
        expect(missingRequiredFields(parsed)).toEqual([]);
    });
});

// --- Conditional visibility / requirements (show_if / required_if / enable_if) ---

describe('configViolations (unit)', () => {
    it('reports missingRequired and invalidOptions with labels', () => {
        const parsed = fixture([
            { key: 'allow', type: 'dropdown', options: ['locked', 'opened'] },
            {
                key: 'pick',
                type: 'dropdown',
                label: 'Pick',
                options: ['readonly', { value: 'full', enable_if: { allow: 'opened' } }],
            },
        ], { allow: 'locked', pick: 'full' });
        const v = configViolations(parsed);
        expect(v.missingRequired).toEqual([]);
        expect(v.invalidOptions).toEqual([{ key: 'pick', label: 'Pick', value: 'full' }]);
    });
});

describe('missingRequiredFields — no-condition regression', () => {
    it('flags every visible required-and-empty field, ignoring filled ones', () => {
        const parsed = fixture([
            { key: 'a', type: 'text', label: 'A', required: true },
            { key: 'b', type: 'text', required: true, default: 'filled' },
            { key: 'c', type: 'number', required: true },
        ]);
        expect(missingRequiredFields(parsed)).toEqual([
            { key: 'a', label: 'A' },
            { key: 'c', label: 'c' },
        ]);
    });
});

// The SAME JSON table pins collimate.config_values.{canon,matches,
// compute_visibility,effective_required,enabled_options,
// invalid_option_violations} (SDK) and the schema_form renderer. Resolved
// like the empty-values fixture above, from sdk/shared/.
const conditionsContractPath = sdkSharedPath('set-yaml-conditions-contract.json');
const hasConditionsContract = existsSync(conditionsContractPath);

interface EvalCase {
    cond: Record<string, unknown>;
    values?: Record<string, unknown>;
    values_raw?: Record<string, unknown>;
    referent_hidden?: string[];
    match: boolean;
    note?: string;
}
interface GateCase {
    note: string;
    fields: RawField[];
    values: Record<string, unknown>;
    blocking_empties: string[];
}
interface OptionCase {
    note: string;
    fields: RawField[];
    values?: Record<string, unknown>;
    values_raw?: Record<string, unknown>;
    enabled: Record<string, string[]>;
    invalid_options: { key: string; value: string }[];
}

describe(
    'set-yaml conditions contract (shared fixture with the Python SDK)',
    () => {
        // Fail — never silently skip — when the shared fixture is missing: it is
        // the only thing pinning this TS mirror to the Python source of truth, so
        // a missing contract must break CI, not disappear. It lives at
        // sdk/shared/set-yaml-conditions-contract.json.
        it('the conditions contract fixture is present in sdk/shared', () => {
            expect(hasConditionsContract).toBe(true);
        });
        const fixtureRows = (hasConditionsContract
            ? JSON.parse(readFileSync(conditionsContractPath, 'utf8'))
            : { field_types: {}, eval_cases: [], gate_cases: [], option_cases: [] }) as {
            field_types: Record<string, string>;
            eval_cases: EvalCase[];
            gate_cases: GateCase[];
            option_cases: OptionCase[];
        };
        const { field_types, eval_cases, gate_cases, option_cases } = fixtureRows;

        // eval_cases: matches() ingests raw parsed values (row.values ??
        // row.values_raw); referent_hidden marks those referents not-visible.
        eval_cases.forEach((row, i) => {
            it(`eval_case[${i}] ${JSON.stringify(row.cond)} → ${row.match}`, () => {
                const resolved = (row.values ?? row.values_raw ?? {}) as Record<string, unknown>;
                const visible: Record<string, boolean> = {};
                for (const k of Object.keys(resolved)) visible[k] = true;
                for (const k of row.referent_hidden ?? []) visible[k] = false;
                expect(matches(row.cond, resolved, visible, field_types)).toBe(row.match);
            });
        });

        // gate_cases: normalize the row's fields exactly as the runner
        // catalog boundary does (values resolved by effectiveValues inside
        // missingRequiredFields) and assert the blocking keys.
        gate_cases.forEach((row, i) => {
            it(`gate_case[${i}] ${row.note}`, () => {
                const keys = missingRequiredFields(fixture(row.fields, row.values)).map(m => m.key);
                expect(keys).toEqual(row.blocking_empties);
            });
        });

        // option_cases: enabledOptions per asserted dropdown + invalidOptionFields.
        option_cases.forEach((row, i) => {
            it(`option_case[${i}] ${row.note}`, () => {
                const values = (row.values ?? row.values_raw ?? {}) as Record<string, unknown>;
                const parsed = fixture(row.fields, values);
                const resolved = effectiveValues(parsed);
                const visible = computeVisibility(parsed.fields, resolved);
                const fieldTypes: Record<string, string> = {};
                for (const f of parsed.fields) fieldTypes[f.key] = f.type;
                for (const [key, expected] of Object.entries(row.enabled)) {
                    const field = parsed.fields.find(f => f.key === key)!;
                    expect(enabledOptions(field, resolved, visible, fieldTypes)).toEqual(expected);
                }
                const violations = invalidOptionFields(parsed).map(v => ({
                    key: v.key,
                    value: v.value,
                }));
                expect(violations).toEqual(row.invalid_options);
            });
        });
    },
);
