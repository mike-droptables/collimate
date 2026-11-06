// SPDX-License-Identifier: MIT
//
// theme.test.ts — schema_form's colour map on the skin tokens (row R05 of
// the 2026-10-06 workbench plan). Red marks errors and the remove button;
// teal marks on-states and focus rings; a focused field steps up to line-2;
// brass (the selection accent) appears nowhere in the form. The
// harness bundles main.tsx with framework imports stubbed and passes
// SCHEMA_FORM_BUNDLE; see tests/test_media_utility_renderer_tokens.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.SCHEMA_FORM_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'SCHEMA_FORM_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_media_utility_renderer_tokens.py does this).',
    );
}

let FORM_COLORS: Record<string, string>;
let FORM_CSS: string;

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    FORM_COLORS = mod.FORM_COLORS;
    FORM_CSS = mod.FORM_CSS;
});

const LITERAL = /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i;

test('the schema_form colour map is built on the skin tokens (R05)', () => {
    assert.equal(FORM_COLORS.bg, 'var(--sheet)');
    assert.equal(FORM_COLORS.panel, 'var(--sheet-2)');
    assert.equal(FORM_COLORS.border, 'var(--line)');
    assert.equal(FORM_COLORS.borderStrong, 'var(--line-2)');
    assert.equal(FORM_COLORS.text, 'var(--ink)');
    assert.equal(FORM_COLORS.textDim, 'var(--ink-2)');
    assert.equal(FORM_COLORS.textMuted, 'var(--ink-3)');
    assert.equal(FORM_COLORS.danger, 'var(--red)');
    assert.equal(FORM_COLORS.dangerSoft, 'var(--red-soft)');
    assert.equal(FORM_COLORS.on, 'var(--teal)');
    for (const v of Object.values(FORM_COLORS)) {
        assert.match(v, /^var\(--[\w-]+\)$/);
    }
});

test('schema_form steps a focused field up to line-2 and never uses brass (R05)', () => {
    assert.doesNotMatch(FORM_CSS, /var\(--brass/);
    for (const v of Object.values(FORM_COLORS)) assert.doesNotMatch(v, /var\(--brass/);
    assert.match(FORM_CSS, /\[data-sf-control\]:focus \{ border-color: var\(--line-2\) !important; \}/);
    assert.match(FORM_CSS, /\[data-sf-button\]:focus-visible \{ outline: 2px solid var\(--teal\);/);
    assert.match(FORM_CSS, /\[data-sf-button="danger"\]:hover \{ background: var\(--red-soft\) !important; color: var\(--red\) !important; \}/);
    assert.doesNotMatch(FORM_CSS, LITERAL);
});
