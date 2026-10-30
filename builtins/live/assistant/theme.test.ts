// SPDX-License-Identifier: MIT
//
// theme.test.ts — the chat renderer paints on the skin tokens and nothing
// else (plan 2026-10-06, row R03).
//
// Runs under node:test via the tsx loader (see
// tests/test_assistant_transcript.py). The harness bundles main.tsx with its
// CDN imports stubbed and passes the ESM path via ASSISTANT_MAIN_BUNDLE.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const BUNDLE = process.env.ASSISTANT_MAIN_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'ASSISTANT_MAIN_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_assistant_transcript.py does this).',
    );
}

const REPO = process.cwd();
const MAIN = readFileSync(path.join(REPO, 'builtins/live/assistant/main.tsx'), 'utf8');

// The SDK's token vocabulary, read from its source so the two cannot drift.
function vocabulary(): Set<string> {
    const src = readFileSync(path.join(REPO, 'sdk/packages/renderer-hooks/src/index.tsx'), 'utf8');
    const block = /THEME_VAR_NAMES[^=]*=\s*Object\.freeze\(\[([\s\S]*?)\]/.exec(src);
    assert.ok(block, 'THEME_VAR_NAMES is declared in the renderer runtime');
    return new Set([...block[1].matchAll(/'(--[a-z0-9-]+)'/g)].map((m) => m[1]));
}

const COLOUR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/;

function varNames(text: string): string[] {
    return [...text.matchAll(/var\((--[a-z0-9-]+)/g)].map((m) => m[1]);
}

let mod: any;
let NAMES: Set<string>;

before(async () => {
    mod = await import(pathToFileURL(path.resolve(BUNDLE)).href);
    NAMES = vocabulary();
});

test('every chat style token is a skin variable (R03)', () => {
    const values = [...Object.values(mod.T), mod.FONT_UI, mod.MONO] as string[];
    for (const value of values) {
        const m = /^var\((--[a-z0-9-]+)\)$/.exec(value);
        assert.ok(m, `${value} is a bare var() reference`);
        assert.ok(NAMES.has(m[1]), `${m[1]} is in the skin vocabulary`);
    }
    assert.equal(mod.T.teal, 'var(--teal)', 'the accent is teal, not a fixed blue');
    assert.equal(mod.T.sheet, 'var(--sheet)', 'the ground is the sheet');
    assert.equal(mod.FONT_UI, 'var(--font-ui)');
    assert.equal(mod.MONO, 'var(--font-mono)');
});

test('the injected shadow CSS and the invalid-session fallback use only tokens (R03)', () => {
    for (const [label, text] of [['SHADOW_CSS', mod.SHADOW_CSS], ['INVALID_SESSION_HTML', mod.INVALID_SESSION_HTML]]) {
        assert.equal(typeof text, 'string', `${label} is exported`);
        assert.doesNotMatch(text, COLOUR_LITERAL, `${label} has no colour literal`);
        const names = varNames(text);
        assert.ok(names.length > 0, `${label} paints from tokens`);
        for (const name of names) assert.ok(NAMES.has(name), `${label}: ${name} is a skin variable`);
    }
    assert.match(mod.INVALID_SESSION_HTML, /background:var\(--sheet\)/);
    assert.match(mod.INVALID_SESSION_HTML, /font-family:var\(--font-ui\)/);
});

test('the chat controls take the skin UI font, not the UA system font (R03)', () => {
    assert.match(mod.SHADOW_CSS, /button[^{]*\{[^}]*font:\s*inherit/);
    for (const el of ['button', 'select', 'input', 'textarea']) {
        assert.match(mod.SHADOW_CSS, new RegExp(`\\b${el}\\b[^{]*\\{[^}]*font:\\s*inherit`), `${el} inherits the root font`);
    }
});

test('the connection dot is green when open, red when closed or busy (R03)', () => {
    assert.equal(mod.statusDotColor('open'), 'var(--green)');
    assert.equal(mod.statusDotColor('connecting'), 'var(--ink-3)');
    assert.equal(mod.statusDotColor('closed'), 'var(--red)');
    assert.equal(mod.statusDotColor('busy'), 'var(--red)');
});

test('the renderer source carries no private palette or scheme fallback (R03)', () => {
    assert.doesNotMatch(MAIN, COLOUR_LITERAL, 'no hex, rgb() or hsl() literal in main.tsx');
    for (const gone of ['--aui-', 'useScheme', 'matchMedia', 'system-ui', 'deliberately not the host']) {
        assert.ok(!MAIN.includes(gone), `${gone} is gone`);
    }
    for (const name of varNames(MAIN)) assert.ok(NAMES.has(name), `${name} is a skin variable`);
    assert.match(MAIN, /api\.injectCss\(SHADOW_CSS\)/, 'the shadow CSS goes through api.injectCss');
});
