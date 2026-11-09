// SPDX-License-Identifier: MIT
//
// theme.test.ts — the table renderer on the skin tokens (row R05 of the
// 2026-10-06 workbench plan): a sheet ground, sheet-2 head and bars, line
// hairlines, ink tiers, and zebra rows that mix 4% of the ink into the
// ground so they read on a dark or a light skin. The harness bundles
// main.tsx with framework imports stubbed and passes TABLE_RENDERER_BUNDLE;
// see tests/test_media_utility_renderer_tokens.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.TABLE_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'TABLE_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_media_utility_renderer_tokens.py does this).',
    );
}

let S: Record<string, any>;

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    S = mod.TABLE_STYLES;
});

const LITERAL = /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i;

function strings(value: unknown): string[] {
    if (typeof value === 'string') return [value];
    if (value && typeof value === 'object') return Object.values(value).flatMap(strings);
    return [];
}

test('table zebra mixes 4% ink into the ground (R05)', () => {
    assert.equal(S.zebraBg, 'color-mix(in srgb, var(--ink) 4%, transparent)');
    assert.deepEqual(S.rowStyles, [{ background: 'transparent' }, { background: S.zebraBg }]);
});

test('table surfaces sit on sheet, sheet-2 and the line tokens, with no literal (R05)', () => {
    assert.equal(S.wrap.background, 'var(--sheet)');
    assert.equal(S.wrap.color, 'var(--ink)');
    assert.equal(S.th.background, 'var(--sheet-2)');
    assert.equal(S.th.color, 'var(--ink-2)');
    assert.equal(S.th.borderBottom, '1px solid var(--line-2)');
    assert.equal(S.td.borderBottom, '1px solid var(--line)');
    assert.equal(S.bar.background, 'var(--sheet-2)');
    for (const v of strings(S)) {
        assert.doesNotMatch(v, LITERAL);
        assert.doesNotMatch(v, /var\(--[\w-]+\s*,/);
    }
});
