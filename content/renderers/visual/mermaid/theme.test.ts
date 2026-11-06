// SPDX-License-Identifier: MIT
//
// theme.test.ts — Mermaid's `base` theme variables, built from the skin
// record (row R05 of the 2026-10-06 workbench plan).
//
// Mermaid cannot read CSS variables and derives its shades from concrete
// colours, so each variable is the token's resolved value. The harness
// bundles main.tsx with framework imports stubbed and passes
// MERMAID_RENDERER_BUNDLE; see tests/test_mermaid_renderer_theme.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.MERMAID_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'MERMAID_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_mermaid_renderer_theme.py does this).',
    );
}

let mermaidThemeVariables: (theme: any) => Record<string, string | boolean>;
let mermaidConfig: (theme: any) => Record<string, any>;

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    mermaidThemeVariables = mod.mermaidThemeVariables;
    mermaidConfig = mod.mermaidConfig;
});

const NIGHT = {
    themeId: 'night', scheme: 'dark', known: true,
    vars: {
        '--canvas': '#151311', '--sheet': '#1c1a17', '--sheet-2': '#23201c', '--sheet-3': '#2a2621',
        '--line': '#312d27', '--line-2': '#3f3931',
        '--ink': '#e2ddd4', '--ink-2': '#a39d92', '--ink-3': '#6f695f',
        '--brass': '#c69d64', '--brass-soft': 'rgba(198, 157, 100, 0.16)',
    },
    fonts: { ui: '"Inter Variable", Inter, system-ui, sans-serif', mono: 'Geist Mono', prose: 'Alegreya' },
};

test('the mermaid variables come from the record, one token each (R05)', () => {
    assert.deepEqual(mermaidThemeVariables(NIGHT), {
        darkMode: true,
        background: '#1c1a17',          // sheet
        primaryColor: '#23201c',        // sheet-2
        primaryTextColor: '#e2ddd4',    // ink
        primaryBorderColor: '#3f3931',  // line-2
        lineColor: '#6f695f',           // ink-3
        secondaryColor: '#2a2621',      // sheet-3
        tertiaryColor: '#1c1a17',       // sheet
        textColor: '#e2ddd4',           // ink
        noteBkgColor: '#2a2621',        // sheet-3, not the base theme's #fff5ad
        noteTextColor: '#e2ddd4',       // ink, not #333
        noteBorderColor: '#3f3931',     // line-2
        actorLineColor: '#3f3931',      // line-2, not grey
        edgeLabelBackground: '#1c1a17', // sheet, not darken(secondaryColor)
        sectionBkgColor: '#23201c',     // sheet-2
        altSectionBkgColor: '#1c1a17',  // sheet, not white
        fontFamily: '"Inter Variable", Inter, system-ui, sans-serif', // the record's ui font
    });
});

test('mermaid gets concrete colours from any CSS colour form, keeping alpha (R05)', () => {
    const vars = mermaidThemeVariables({
        scheme: 'light',
        vars: { '--sheet': 'rgb(248 247 243)', '--sheet-2': '#fff', '--ink': 'rgba(30, 34, 39, 0.5)' },
        fonts: { ui: '', mono: '', prose: '' },
    });
    assert.deepEqual(vars, {
        darkMode: false,
        background: '#f8f7f3',
        tertiaryColor: '#f8f7f3',
        primaryColor: '#ffffff',
        primaryTextColor: '#1e222780',
        textColor: '#1e222780',
        noteTextColor: '#1e222780',
        edgeLabelBackground: '#f8f7f3',
        sectionBkgColor: '#ffffff',
        altSectionBkgColor: '#f8f7f3',
    });
});

test('mermaid initialises the base theme with the variables and the ui font (R05)', () => {
    const config = mermaidConfig(NIGHT);
    assert.equal(config.theme, 'base');
    assert.equal(config.startOnLoad, false);
    assert.equal(config.securityLevel, 'strict');
    assert.equal(config.layout, 'dagre');
    assert.equal(config.look, 'classic');
    assert.equal(config.fontFamily, NIGHT.fonts.ui);
    assert.deepEqual(config.themeVariables, mermaidThemeVariables(NIGHT));
});

test('the placeholder record leaves mermaid on its base defaults (R05)', () => {
    const config = mermaidConfig({ themeId: 'unknown', scheme: 'dark', known: false, vars: {}, fonts: { ui: '', mono: '', prose: '' } });
    assert.deepEqual(config.themeVariables, { darkMode: true });
    assert.equal('fontFamily' in config, false);
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
    const source = fs.readFileSync(p.join(process.cwd(), 'content', 'renderers', 'visual', 'mermaid', 'main.tsx'), 'utf8');
    assert.match(source, /<div style=\{SOURCE_SCROLL\} onMouseDown=\{\(e\) => sourceCaretToEnd\(e, areaRef\.current\)\}>/);
    assert.match(source, /fieldSizing: 'content'/);
    assert.match(source, /@supports not \(field-sizing: content\) \{ textarea\[data-source-area\] \{ min-height: 100%; \} \}/);
});
