// SPDX-License-Identifier: MIT
//
// theme.test.ts — the slides renderer on the skin tokens (row R05 of the
// 2026-10-06 workbench plan). The slide markup is an HTML string, so its
// colours cannot be checked by eye in a style object: this pins that the
// builder writes token references only, prose on --prose-ink and code on
// --code-bg, and that the card is set in the prose face with the skin shadow.
// The harness bundles main.tsx with framework imports stubbed and passes
// SLIDES_RENDERER_BUNDLE; see tests/test_media_utility_renderer_tokens.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.SLIDES_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'SLIDES_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_media_utility_renderer_tokens.py does this).',
    );
}

let renderSlideMarkdown: (md: string) => string;
let splitSlides: (md: string) => string[];
let SLIDE_CARD: Record<string, unknown>;

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    renderSlideMarkdown = mod.renderSlideMarkdown;
    splitSlides = mod.splitSlides;
    SLIDE_CARD = mod.SLIDE_CARD;
});

const LITERAL = /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i;
const FALLBACK = /var\(--[\w-]+\s*,/;

// The SDK's token vocabulary, read from its source so the two cannot drift.
function vocabulary(): Set<string> {
    const src = readFileSync(path.join(process.cwd(), 'sdk/packages/renderer-hooks/src/index.tsx'), 'utf8');
    const block = /THEME_VAR_NAMES[^=]*=\s*Object\.freeze\(\[([\s\S]*?)\]/.exec(src);
    assert.ok(block, 'THEME_VAR_NAMES is declared in the renderer runtime');
    return new Set([...block[1].matchAll(/'(--[a-z0-9-]+)'/g)].map((m) => m[1]));
}

const DECK = [
    '# Title slide',
    '',
    '## Section',
    '',
    '### Detail',
    '',
    '- one with `code`',
    '- **two** and *three*',
    '',
    'A paragraph with `inline` code.',
].join('\n');

test('the slide HTML builder emits no colour literal, only skin tokens (R05)', () => {
    const html = renderSlideMarkdown(DECK);
    assert.doesNotMatch(html, LITERAL);
    assert.doesNotMatch(html, FALLBACK);
    const names = vocabulary();
    const used = [...html.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1]);
    assert.ok(used.length > 0, 'the builder writes token references');
    for (const name of used) assert.ok(names.has(name), `${name} is a skin variable`);
    assert.match(html, /<h1 style="[^"]*color:var\(--prose-ink\)/);
    assert.match(html, /<h2 style="[^"]*color:var\(--prose-ink\)/);
    assert.match(html, /<h3 style="[^"]*color:var\(--ink-2\)/);
    assert.match(html, /<ul style="[^"]*color:var\(--prose-ink\)/);
    assert.match(html, /<p style="[^"]*color:var\(--prose-ink\)/);
    assert.match(html, /<code style="background:var\(--code-bg\);[^"]*border:1px solid var\(--line\);[^"]*font-family:var\(--font-mono\)/);
});

test('the slide card is a sheet in the prose face with the skin shadow (R05)', () => {
    assert.equal(SLIDE_CARD.background, 'var(--sheet)');
    assert.equal(SLIDE_CARD.fontFamily, 'var(--font-prose)');
    assert.equal(SLIDE_CARD.color, 'var(--prose-ink)');
    assert.equal(SLIDE_CARD.boxShadow, 'var(--shadow)');
    assert.equal(SLIDE_CARD.borderRadius, 'var(--radius)');
    for (const v of Object.values(SLIDE_CARD)) {
        if (typeof v === 'string') assert.doesNotMatch(v, LITERAL);
    }
});

test('the builder still escapes markup and splits on rules (R05)', () => {
    assert.match(renderSlideMarkdown('<b>x</b>'), /&lt;b&gt;x&lt;\/b&gt;/);
    assert.match(renderSlideMarkdown('- a\n- b\n\nafter'), /<\/ul>\n*<p style="[^"]*">after<\/p>$/);
    assert.deepEqual(splitSlides('a\n---\nb\n\n---\n'), ['a', 'b']);
});

// ---------------------------------------------------------------------------
// Caret at the end of the source (RS-04)
// ---------------------------------------------------------------------------

test('a press below the source puts the caret at its end; a press on the text is the browser’s (RS-04)', async () => {
    const mod: any = await import(pathToFileURL(BUNDLE!).href);
    const log: string[] = [];
    const area = (value: string, disabled = false) => ({
        value, disabled,
        focus() { log.push('focus'); },
        setSelectionRange(a: number, b: number) { log.push(`select ${a}-${b}`); },
    });
    const scroller = {};
    let prevented = 0;
    const press = (target: unknown, button = 0) => ({ target, currentTarget: scroller, button, preventDefault() { prevented += 1; } });
    assert.equal(mod.sourceCaretToEnd(press(scroller), area('graph TD\nA-->B')), true);
    assert.deepEqual(log, ['focus', 'select 14-14']);
    const own = area('x');
    assert.equal(mod.sourceCaretToEnd(press(own), own), false);
    assert.equal(mod.sourceCaretToEnd(press(scroller, 1), own), false);
    assert.equal(mod.sourceCaretToEnd(press(scroller), area('x', true)), false);
    assert.equal(mod.sourceCaretToEnd(press(scroller), null), false);
    assert.equal(log.length, 2);
    assert.equal(prevented, 1);
    assert.equal(mod.SOURCE_SCROLL.overflowY, 'auto');
    const fs = await import('node:fs');
    const p = await import('node:path');
    const source = fs.readFileSync(p.join(process.cwd(), 'content', 'renderers', 'visual', 'slides', 'main.tsx'), 'utf8');
    assert.match(source, /<div style=\{SOURCE_SCROLL\} onMouseDown=\{\(e\) => sourceCaretToEnd\(e, areaRef\.current\)\}>/);
    assert.match(source, /fieldSizing: 'content'/);
    assert.match(source, /@supports not \(field-sizing: content\) \{ textarea\[data-source-area\] \{ min-height: 100%; \} \}/);
});
