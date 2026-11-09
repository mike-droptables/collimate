// SPDX-License-Identifier: MIT
//
// text.test.ts — the text renderer's caret at the end and its style
// (RS-04), off-DOM. The harness bundles main.tsx with framework imports
// stubbed and passes TEXT_RENDERER_BUNDLE; see
// tests/test_text_renderer_model.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.TEXT_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'TEXT_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_text_renderer_model.py does this).',
    );
}

let m: any;
before(async () => {
    m = await import(pathToFileURL(BUNDLE).href);
});

const SOURCE = readFileSync(path.join(process.cwd(), 'builtins', 'text', 'text', 'main.tsx'), 'utf8');

/** A textarea as caretToEnd uses it. */
function area(value: string, disabled = false) {
    const log: string[] = [];
    return {
        log, value, disabled,
        focus() { log.push('focus'); },
        setSelectionRange(a: number, b: number) { log.push(`select ${a}-${b}`); },
    };
}

test('a press below the text puts the caret at its end; a press on the text is the browser’s (RS-04)', () => {
    const scroller = {};
    let prevented = 0;
    const press = (target: unknown, button = 0) => ({ target, currentTarget: scroller, button, preventDefault() { prevented += 1; } });
    const a = area('one\ntwo');
    assert.equal(m.caretToEnd(press(scroller), a), true);
    assert.deepEqual(a.log, ['focus', 'select 7-7']);
    assert.equal(prevented, 1);
    // On the area itself, with another button, read-only or with no area: untouched.
    const b = area('x');
    assert.equal(m.caretToEnd(press(b), b), false);
    assert.equal(m.caretToEnd(press(scroller, 2), b), false);
    assert.equal(m.caretToEnd(press(scroller), area('x', true)), false);
    assert.equal(m.caretToEnd(press(scroller), null), false);
    assert.deepEqual(b.log, []);
    assert.equal(prevented, 1);
    // The area is as tall as its text inside the scroller, which takes the press.
    assert.equal(m.TEXT_AREA.fieldSizing, 'content');
    assert.equal(m.TEXT_SCROLL.overflowY, 'auto');
    assert.match(SOURCE, /<div style=\{TEXT_SCROLL\} onMouseDown=\{\(e\) => caretToEnd\(e, areaRef\.current\)\}>/);
    assert.match(SOURCE, /@supports not \(field-sizing: content\) \{ textarea\[data-text-area\] \{ min-height: 100%; \} \}/);
});

test('the style control offers a mono family and the text size (RS-04)', () => {
    assert.deepEqual({ ...m.TEXT_SIZE }, { min: 0.7, max: 2, step: 0.1, default: 1 });
    assert.equal(m.textFontSize(null), '0.8125rem');
    assert.equal(m.textFontSize(1.5), '1.21875rem');
    assert.equal(m.textFontSize(9), '1.625rem');
    assert.equal(m.textFontSize('2'), '0.8125rem');
    const families = [{ family: 'Fira Code', label: 'Fira Code', role: 'mono', stack: '"Fira Code", mono-stack' }];
    assert.equal(m.familyStack('Fira Code', families), '"Fira Code", mono-stack');
    assert.equal(m.familyStack('Gone', families), null);
    assert.equal(m.familyStack(null, families), null);
    assert.equal(m.TEXT_ROOT.position, 'relative');
    // Shown only while the pointer is near its corner: the control owns
    // that reach (the SDK's tests/test_style_control_reach.py), watching its
    // parent, this root, so the editor keeps no hover state.
    assert.match(SOURCE, /<div style=\{TEXT_ROOT\}>/);
    assert.match(SOURCE, /<StyleControl api=\{api\} themes=\{TEXT_THEMES\} font="mono" size=\{TEXT_SIZE\}\s*style=\{\{ right: '1\.25rem' \}\} \/>/);
    assert.doesNotMatch(SOURCE, /visible=\{|setHovered|onMouseEnter=/);
    assert.match(SOURCE, /style=\{\{ \.\.\.TEXT_AREA, fontFamily, fontSize: textFontSize\(style\.textSize\) \}\}/);
});
