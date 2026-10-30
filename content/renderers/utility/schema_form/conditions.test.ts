// SPDX-License-Identifier: MIT
//
// conditions.test.ts — the renderer surface's leg of the cross-language
// show_if/required_if/enable_if contract. Runs under node:test via the tsx
// loader (see tests/test_renderer_conditions.py). The evaluator now lives
// INLINE in main.tsx. This test bundles the package with external React/YAML
// and SDK imports stubbed, then calls its exported evaluator through
// SCHEMA_FORM_BUNDLE (scripts/bundle_renderer_test.mjs). Assertions use
// shared/set-yaml-conditions-contract.json, the same fixture the
// Python (config_values.py) and frontend (setYaml.ts) surfaces run.
//
// Each surface must run its OWN ingestion before evaluating. For the renderer
// that is coerceValue over any values_renderer block (simulating the value
// domain the form actually holds after ingesting YAML); rows with only
// values/values_raw use those raw.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

// The evaluator's minimal field shape (main.tsx exports the real FieldLike, but
// the stubbed bundle carries no type declarations — a structural local mirror
// is all the assertions below need).
type FieldLike = { key?: string; type?: string; [k: string]: any };

const BUNDLE = process.env.SCHEMA_FORM_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'SCHEMA_FORM_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_renderer_conditions.py does this).',
    );
}

// The evaluator's exported functions, populated from the bundled renderer in a
// `before` hook. We do NOT top-level-await the import: the tsx loader transpiles
// this file to CommonJS, where top-level await is unsupported — the hook runs
// once before every suite instead.
let matches: any;
let computeVisibility: any;
let effectiveRequired: any;
let enabledOptions: any;
let invalidOptionViolations: any;
let coerceValue: any;
let isEmptyValue: any;
let modeFromFilename: any;

before(async () => {
    const mod: any = await import(pathToFileURL(path.resolve(BUNDLE)).href);
    ({
        matches,
        computeVisibility,
        effectiveRequired,
        enabledOptions,
        invalidOptionViolations,
        coerceValue,
        isEmptyValue,
        modeFromFilename,
    } = mod);
});

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONTRACT = path.resolve(__dirname, '../../../../sdk/shared/set-yaml-conditions-contract.json');
const contract = JSON.parse(readFileSync(CONTRACT, 'utf8'));
const EMPTY_CONTRACT = path.resolve(__dirname, '../../../../sdk/shared/empty-values-contract.json');
const emptyContract = JSON.parse(readFileSync(EMPTY_CONTRACT, 'utf8'));

type Dict = Record<string, any>;

/** Build a {key: type} index from a case's field declarations. */
function typeIndexOf(fields: FieldLike[]): Record<string, string | undefined> {
    const out: Record<string, string | undefined> = {};
    for (const f of fields) if (f && f.key != null) out[f.key] = f.type;
    return out;
}

// --------------------------------------------------------------------------- //
// eval_cases — operator + type-aware coercion + hidden/absent referent rules.
// --------------------------------------------------------------------------- //

test('eval_cases', () => {
    const fieldTypes: Record<string, string> = contract.field_types;
    for (const c of contract.eval_cases as any[]) {
        let resolved: Dict;
        if (c.values_renderer) {
            // Simulate renderer ingestion: coerceValue per referent field type.
            resolved = {};
            for (const [k, v] of Object.entries(c.values_renderer)) {
                resolved[k] = coerceValue(v, { type: fieldTypes[k] });
            }
        } else if (c.values_raw) {
            resolved = c.values_raw;
        } else {
            resolved = c.values || {};
        }

        // referent_hidden rows pin the hidden-referent rule: a matching value
        // that is hidden still contributes UNKNOWN. Build a visible map that is
        // all-true except the explicitly hidden referents; other rows pass null
        // (matches then falls back to resolved-presence only).
        let visible: Record<string, boolean> | null = null;
        if (Array.isArray(c.referent_hidden)) {
            visible = {};
            for (const k of Object.keys(fieldTypes)) visible[k] = true;
            for (const k of Object.keys(resolved)) visible[k] = true;
            for (const k of Object.keys(c.cond)) visible[k] = true;
            for (const k of c.referent_hidden) visible[k] = false;
        }

        const got = matches(c.cond, resolved, visible, fieldTypes);
        assert.strictEqual(
            got,
            c.match,
            `eval_case failed: ${JSON.stringify(c)} (resolved=${JSON.stringify(resolved)})`,
        );
    }
});

// --------------------------------------------------------------------------- //
// gate_cases — the composed visibility-first required gate (mirrors validate).
// --------------------------------------------------------------------------- //

test('gate_cases', () => {
    for (const g of contract.gate_cases as any[]) {
        const fields: FieldLike[] = g.fields;
        const values: Dict = g.values || {};
        const visible = computeVisibility(fields, values);
        const fieldTypes = typeIndexOf(fields);

        const blocking: string[] = [];
        for (const f of fields) {
            const key = f.key as string;
            if (!visible[key]) continue; // hidden => never gates
            if (!effectiveRequired(f, fields, values, visible, fieldTypes)) continue;
            if (isEmptyValue(values[key], fieldTypes[key])) blocking.push(key);
        }

        assert.deepStrictEqual(
            blocking.sort(),
            [...(g.blocking_empties as string[])].sort(),
            `gate_case failed: ${g.note}`,
        );
    }
});

// --------------------------------------------------------------------------- //
// option_cases — enabledOptions lists + invalidOptionViolations pairs.
// --------------------------------------------------------------------------- //

test('option_cases', () => {
    for (const o of contract.option_cases as any[]) {
        const fields: FieldLike[] = o.fields;
        const fieldTypes = typeIndexOf(fields);

        let values: Dict;
        if (o.values_renderer) {
            values = {};
            for (const [k, v] of Object.entries(o.values_renderer)) {
                values[k] = coerceValue(v, { type: fieldTypes[k] });
            }
        } else if (o.values_raw) {
            values = o.values_raw;
        } else {
            values = o.values || {};
        }

        const visible = computeVisibility(fields, values);
        const byKey: Record<string, FieldLike> = {};
        for (const f of fields) if (f.key != null) byKey[f.key] = f;

        // Per-field enabled-option lists (order-significant).
        for (const [key, expected] of Object.entries(o.enabled as Record<string, string[]>)) {
            const got = enabledOptions(byKey[key], values, visible, fieldTypes);
            assert.deepStrictEqual(got, expected, `option_case enabled[${key}] failed: ${o.note}`);
        }

        // Invalid-option violations, compared on {key, value} only.
        const gotViolations = invalidOptionViolations(fields, values, visible, fieldTypes)
            .map((v) => ({ key: v.key, value: v.value }));
        assert.deepStrictEqual(
            gotViolations,
            o.invalid_options as Array<{ key: string; value: string }>,
            `option_case invalid_options failed: ${o.note}`,
        );
    }
});

// --------------------------------------------------------------------------- //
// empty_values_contract — validate()'s per-type empty test.
// --------------------------------------------------------------------------- //

test('empty_values_contract', () => {
    for (const c of emptyContract.cases as any[]) {
        assert.strictEqual(
            isEmptyValue(c.value, c.type),
            c.empty,
            `empty case failed: ${JSON.stringify(c)}`,
        );
    }
});

// --------------------------------------------------------------------------- //
// Renderer ingestion and manifest save helpers.
// --------------------------------------------------------------------------- //

test('coerceValue checkbox folds like canon', () => {
    const cb = { type: 'checkbox' };
    assert.strictEqual(coerceValue('yes', cb), true);
    assert.strictEqual(coerceValue('On', cb), true);
    assert.strictEqual(coerceValue('1', cb), true);
    assert.strictEqual(coerceValue(true, cb), true);
    assert.strictEqual(coerceValue(1, cb), true);
    assert.strictEqual(coerceValue('no', cb), false);
    assert.strictEqual(coerceValue(0, cb), false);
    assert.strictEqual(coerceValue(undefined, cb), false);
});

test('settings files retain the SDK form mode__D13', () => {
    assert.equal(modeFromFilename('settings.set_yaml'), 'set');
    assert.equal(modeFromFilename('notes.txt'), null);
});
