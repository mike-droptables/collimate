// SPDX-License-Identifier: MIT
//
// theme.test.ts — the Monaco renderer's one editor theme, 'collimate',
// built from the skin record (row R05 of the 2026-10-06 workbench plan), with
// its syntax colours from the SDK's syntaxPalette (R05 W2b).
//
// Monaco takes colours as hex only, so every colour the builder emits must
// be a concrete #rrggbbaa resolved from the record: rgba() tokens such as
// --brass-soft keep their alpha as the last two digits. The harness bundles
// main.tsx with framework imports stubbed (scripts/bundle_renderer_test.mjs)
// and passes MONACO_RENDERER_BUNDLE; see tests/test_monaco_renderer_theme.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

// The SDK's palette, from source: the bundle carries the same implementation.
import { syntaxPalette } from '../../../../sdk/packages/renderer-hooks/src/syntax.ts';

const BUNDLE = process.env.MONACO_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'MONACO_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_monaco_renderer_theme.py does this).',
    );
}

type Rule = { token: string; foreground?: string; fontStyle?: string };
let monacoThemeData: (theme: any) => { base: string; inherit: boolean; rules: Rule[]; colors: Record<string, string> };
let MONACO_THEME: string;
let MONACO_TOKEN_ROLES: Record<string, string>;
let monacoFontOptions: (theme: any) => { fontFamily?: string };

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    monacoThemeData = mod.monacoThemeData;
    MONACO_THEME = mod.MONACO_THEME;
    MONACO_TOKEN_ROLES = mod.MONACO_TOKEN_ROLES;
    monacoFontOptions = mod.monacoFontOptions;
});

// The built-in Night and Paper skins, as the host sends them.
const NIGHT = {
    themeId: 'night', scheme: 'dark', known: true,
    vars: {
        '--canvas': '#151311', '--sheet': '#1c1a17', '--sheet-2': '#23201c', '--sheet-3': '#2a2621',
        '--line': '#312d27', '--line-2': '#3f3931',
        '--ink': '#e2ddd4', '--ink-2': '#a39d92', '--ink-3': '#6f695f',
        '--brass': '#c69d64', '--brass-soft': 'rgba(198, 157, 100, 0.16)',
        '--teal': '#7fb0ab', '--teal-soft': 'rgba(127, 176, 171, 0.14)', '--teal-sheet': '#1a211f',
        '--red': '#cf8b7c', '--red-soft': 'rgba(207, 139, 124, 0.16)', '--green': '#90b893',
        '--code-bg': '#121008', '--prose-ink': '#e9e4da', '--lift': 'rgba(0, 0, 0, 0.8)',
    },
    fonts: { ui: 'Inter, sans-serif', mono: 'Geist Mono, monospace', prose: 'Alegreya, serif' },
};

const PAPER = {
    themeId: 'paper', scheme: 'light', known: true,
    vars: {
        '--canvas': '#e6e4de', '--sheet': '#f8f7f3', '--sheet-2': '#f1efea', '--sheet-3': '#e9e7e1',
        '--line': '#d4d0c7', '--line-2': '#c4bfb5',
        '--ink': '#1e2227', '--ink-2': '#5d646b', '--ink-3': '#9a9fa4',
        '--brass': '#8a6d3b', '--brass-soft': 'rgba(138, 109, 59, 0.12)',
        '--teal': '#2e7773', '--teal-soft': 'rgba(46, 119, 115, 0.10)', '--teal-sheet': '#f1f5f3',
        '--red': '#a3463a', '--red-soft': 'rgba(163, 70, 58, 0.10)', '--green': '#3d7750',
        '--code-bg': '#efede7', '--prose-ink': '#1a1e22', '--lift': 'rgba(40, 36, 28, 0.22)',
    },
    fonts: { ui: 'Inter', mono: 'Geist Mono', prose: 'Alegreya' },
};

const NIGHT_COOL = {
    themeId: 'night-cool', scheme: 'dark', known: true,
    vars: {
        '--canvas': '#10161b', '--sheet': '#171d23', '--sheet-2': '#1d252c', '--sheet-3': '#232c34',
        '--line': '#2a333b', '--line-2': '#344049',
        '--ink': '#dbdfe3', '--ink-2': '#99a3ab', '--ink-3': '#626e77',
        '--brass': '#b9966b', '--brass-soft': 'rgba(185, 150, 107, 0.14)',
        '--teal': '#76a9a6', '--teal-soft': 'rgba(118, 169, 166, 0.14)', '--teal-sheet': '#172226',
        '--red': '#c9857a', '--red-soft': 'rgba(201, 133, 122, 0.16)', '--green': '#88b592',
        '--code-bg': '#131920', '--lift': 'rgba(0, 0, 0, 0.8)',
    },
    fonts: { ui: 'Inter', mono: 'Geist Mono', prose: 'Alegreya' },
};

const SKINS = { night: NIGHT, 'night-cool': NIGHT_COOL, paper: PAPER } as const;

test('the editor theme is one named theme rebuilt from the record (R05)', () => {
    assert.equal(MONACO_THEME, 'collimate');
});

test('every Monaco colour is resolved from a sample record as #rrggbbaa (R05)', () => {
    const data = monacoThemeData(NIGHT);
    assert.equal(data.base, 'vs-dark');
    const c = data.colors;
    // The row's mapping, token by token.
    assert.equal(c['editor.background'], '#1c1a17ff');               // sheet
    assert.equal(c['editor.foreground'], '#e2ddd4ff');               // ink
    assert.equal(c['editor.lineHighlightBackground'], '#23201cff');  // sheet-2
    assert.equal(c['editor.selectionBackground'], '#c69d6429');      // brass-soft
    assert.equal(c['editorCursor.foreground'], '#7fb0abff');         // teal: the caret is live
    assert.equal(c['editorLineNumber.foreground'], '#6f695fff');     // ink-3
    assert.equal(c['editorGutter.foldingControlForeground'], '#6f695fff'); // ink-3
    assert.equal(c['editorGutter.background'], '#1c1a17ff');         // sheet
    assert.equal(c['editorWidget.background'], '#23201cff');         // sheet-2
    assert.equal(c['editorWidget.border'], '#3f3931ff');             // line-2
    assert.equal(c['editorSuggestWidget.background'], '#23201cff');  // sheet-2
    assert.equal(c['editorSuggestWidget.border'], '#3f3931ff');      // line-2
    assert.equal(c['editorHoverWidget.background'], '#23201cff');    // sheet-2
    assert.equal(c['editorError.foreground'], '#cf8b7cff');          // red
    assert.equal(c['focusBorder'], '#7fb0abff');                     // teal: the focus ring
    // Diff colours: the palette's removed (red) and added (green) at red-soft's alpha.
    assert.equal(c['diffEditor.removedTextBackground'], '#cf8b7c29');
    assert.equal(c['diffEditor.removedLineBackground'], '#cf8b7c29');
    assert.equal(c['diffEditor.insertedTextBackground'], '#90b89329');
    assert.equal(c['diffEditor.insertedLineBackground'], '#90b89329');
    // No colour leaves the builder in any other form.
    for (const [key, value] of Object.entries(c)) {
        assert.match(value, /^#[0-9a-f]{8}$/, `${key} = ${value}`);
    }
});

test('rgba tokens become 8-digit hex with their alpha, per skin (R05)', () => {
    const data = monacoThemeData(PAPER);
    assert.equal(data.base, 'vs');
    assert.equal(data.colors['editor.background'], '#f8f7f3ff');
    assert.equal(data.colors['editor.selectionBackground'], '#8a6d3b1f'); // 0.12
    assert.equal(data.colors['diffEditor.removedTextBackground'], '#a3463a1a'); // 0.10
    assert.equal(data.colors['diffEditor.insertedTextBackground'], '#3d77501a');
});

test('a skin change rebuilds different colours from the new record (R05)', () => {
    const night = monacoThemeData(NIGHT).colors;
    const paper = monacoThemeData(PAPER).colors;
    assert.notEqual(night['editor.background'], paper['editor.background']);
    assert.notEqual(night['editorCursor.foreground'], paper['editorCursor.foreground']);
});

test('the placeholder record inherits the base theme\'s colours and highlighting (R05 W2b)', () => {
    const data = monacoThemeData({ themeId: 'unknown', scheme: 'dark', known: false, vars: {}, fonts: { ui: '', mono: '', prose: '' } });
    assert.equal(data.base, 'vs-dark');
    // No palette: the base's token rules and its '' rule (the code foreground)
    // stand in, rather than Monaco's default black on the dark ground.
    assert.equal(data.inherit, true);
    assert.deepEqual(data.colors, {});
    assert.deepEqual(data.rules, []);
});

test('code is drawn in the record\'s mono font, per skin (R05)', () => {
    assert.deepEqual(monacoFontOptions(NIGHT), { fontFamily: 'Geist Mono, monospace' });
    assert.deepEqual(monacoFontOptions(PAPER), { fontFamily: 'Geist Mono' });
    // The placeholder names no font: Monaco keeps its own stack until one arrives.
    assert.deepEqual(monacoFontOptions({ scheme: 'dark', vars: {}, fonts: { ui: '', mono: '', prose: '' } }), {});
});

// --- R05 W2b: syntax colours from the skin --------------------------------

// The token classes Monaco's grammars emit that the theme must name.
const REQUIRED_CLASSES = ['comment', 'keyword', 'string', 'number', 'type', 'identifier',
    'delimiter', 'operator', 'tag', 'attribute.name', 'attribute.value', 'regexp', 'invalid'];
// And every variant Monaco's own vs, vs-dark and hc-black themes single out
// (monaco-editor 0.57.0, esm/vs/editor/standalone/common/themes.js).
const SUFFIXED_VARIANTS = ['annotation', 'attribute.value.hex.css', 'attribute.value.html',
    'attribute.value.number', 'attribute.value.number.css', 'attribute.value.unit',
    'attribute.value.unit.css', 'attribute.value.xml', 'constant', 'delimiter.html', 'delimiter.xml',
    'emphasis', 'key', 'keyword.flow', 'keyword.flow.scss', 'keyword.json', 'meta', 'meta.scss',
    'meta.tag', 'metatag', 'metatag.content.html', 'metatag.html', 'metatag.php', 'metatag.xml',
    'number.hex', 'operator.scss', 'operator.sql', 'operator.swift', 'predefined.sql', 'string.html',
    'string.key', 'string.key.json', 'string.sql', 'string.value', 'string.value.json', 'string.yaml',
    'strong', 'tag.class.pug', 'tag.id.pug', 'variable', 'variable.parameter', 'variable.predefined'];

test('a skin with a palette inherits no token rules from Monaco\'s base, on every skin (R05 W2b)', () => {
    for (const [id, skin] of Object.entries(SKINS)) {
        const data = monacoThemeData(skin);
        assert.equal(data.inherit, false, id);
        assert.equal(data.base, skin.scheme === 'light' ? 'vs' : 'vs-dark', id);
    }
});

test('every token class maps to a palette colour, on every skin (R05 W2b)', () => {
    for (const [id, skin] of Object.entries(SKINS)) {
        const palette = syntaxPalette(skin as any);
        assert.ok(palette, id);
        const rules = monacoThemeData(skin).rules;
        const byToken = new Map(rules.map((r) => [r.token, r]));
        assert.equal(byToken.size, rules.length, `${id}: one rule per class`);
        for (const token of [...REQUIRED_CLASSES, ...SUFFIXED_VARIANTS]) {
            assert.ok(byToken.has(token), `${id}: no rule for ${token}`);
        }
        const values = new Set(Object.values(palette!));
        for (const rule of rules) {
            const role = MONACO_TOKEN_ROLES[rule.token];
            assert.ok(role in palette!, `${id}: ${rule.token} -> ${role}`);
            assert.equal(rule.foreground, palette![role as keyof typeof palette], `${id}: ${rule.token}`);
            assert.ok(values.has(rule.foreground!), `${id}: ${rule.token} = ${rule.foreground}`);
        }
    }
});

test('the class-to-role map is the palette\'s vocabulary (R05 W2b)', () => {
    const p = syntaxPalette(NIGHT as any)!;
    const role = (token: string) => MONACO_TOKEN_ROLES[token];
    assert.equal(role('comment'), 'comment');
    assert.equal(role('keyword'), 'keyword');
    assert.equal(role('string'), 'string');
    assert.equal(role('number'), 'number');
    assert.equal(role('type'), 'type');
    assert.equal(role('identifier'), 'variable');
    assert.equal(role('delimiter'), 'punctuation');
    assert.equal(role('operator'), 'operator');
    assert.equal(role('tag'), 'tag');
    assert.equal(role('attribute.name'), 'attribute');
    assert.equal(role('attribute.value'), 'string');
    assert.equal(role('regexp'), 'regexp');
    assert.equal(role('invalid'), 'invalid');
    // On Night, the unstepped roles are the skin's own tokens.
    const rule = (token: string) => monacoThemeData(NIGHT).rules.find((r) => r.token === token)!;
    assert.equal(rule('keyword').foreground, '#7fb0ab');   // teal
    assert.equal(rule('string').foreground, '#90b893');    // green
    assert.equal(rule('number').foreground, '#c69d64');    // brass
    assert.equal(rule('comment').foreground, '#6f695f');   // ink-3
    assert.equal(rule('identifier').foreground, '#e2ddd4'); // ink
    assert.equal(rule('delimiter').foreground, '#a39d92');  // ink-2
    assert.equal(rule('invalid').foreground, '#cf8b7c');   // red
    assert.equal(rule('type').foreground, p.type);         // teal, one step lighter
    assert.equal(rule('emphasis').fontStyle, 'italic');
    assert.equal(rule('strong').fontStyle, 'bold');
});

test('bracket pairs cycle teal, brass and green; diff fills are added and removed (R05 W2b)', () => {
    for (const [id, skin] of Object.entries(SKINS)) {
        const p = syntaxPalette(skin as any)!;
        const c = monacoThemeData(skin).colors;
        const soft = (hex: string) => hex + toAlpha(skin.vars['--red-soft']);
        assert.equal(c['editorBracketHighlight.foreground1'], p.keyword + 'ff', id);
        assert.equal(c['editorBracketHighlight.foreground2'], p.number + 'ff', id);
        assert.equal(c['editorBracketHighlight.foreground3'], p.string + 'ff', id);
        assert.equal(c['editorBracketHighlight.unexpectedBracket.foreground'], p.invalid + 'ff', id);
        assert.equal(c['diffEditor.insertedTextBackground'], soft(p.added), id);
        assert.equal(c['diffEditor.insertedLineBackground'], soft(p.added), id);
        assert.equal(c['diffEditor.removedTextBackground'], soft(p.removed), id);
        assert.equal(c['diffEditor.removedLineBackground'], soft(p.removed), id);
        assert.equal(c['editorGutter.modifiedBackground'], p.modified + 'ff', id);
        for (const [key, value] of Object.entries(c)) {
            assert.match(value, /^#[0-9a-f]{8}$/, `${id}: ${key} = ${value}`);
        }
    }
});

test('a skin change rebuilds the token rules from the new palette (R05 W2b)', () => {
    const night = monacoThemeData(NIGHT).rules.find((r) => r.token === 'keyword')!.foreground;
    const paper = monacoThemeData(PAPER).rules.find((r) => r.token === 'keyword')!.foreground;
    assert.equal(night, '#7fb0ab');
    assert.equal(paper, '#2e7773');
});

test('a skin whose --ink cannot be read inherits the base\'s highlighting (R05 W2b)', () => {
    // A user skin may write any CSS colour; oklch() does not resolve to hex.
    const skin = { ...NIGHT, vars: { ...NIGHT.vars, '--ink': 'oklch(0.9 0.01 80)' } };
    assert.equal(syntaxPalette(skin as any), null);
    const data = monacoThemeData(skin);
    assert.equal(data.inherit, true);
    assert.deepEqual(data.rules, []);
    assert.equal(data.colors['editor.background'], '#1c1a17ff');
    assert.equal(data.colors['editor.foreground'], undefined);
});

test('a skin with one unreadable hue keeps the palette\'s highlighting (R05 W2b)', () => {
    const skin = { ...NIGHT, vars: { ...NIGHT.vars, '--green': 'hsl(130 30% 60%)' } };
    const p = syntaxPalette(skin as any);
    assert.ok(p);
    const data = monacoThemeData(skin);
    assert.equal(data.inherit, false);
    assert.ok(data.rules.length > 0);
    // The other hues are the skin's own; green's roles fall back to --ink.
    const rule = (token: string) => data.rules.find((r) => r.token === token)!;
    assert.equal(rule('keyword').foreground, '#7fb0ab');
    assert.equal(rule('string').foreground, p!.string);
    assert.equal(p!.string, '#e2ddd4');
    assert.equal(data.colors['editor.background'], '#1c1a17ff');
});

// Every key a visible editor surface paints. Monaco 0.57.0's colour registry
// supplies its own vs/vs-dark default for any key a theme leaves unnamed,
// inherit: false or not, so each of these must come from the skin.
const REQUIRED_COLOR_KEYS = [
    'editor.background', 'editor.foreground', 'editor.lineHighlightBackground', 'editor.lineHighlightBorder',
    'editor.selectionBackground', 'editor.inactiveSelectionBackground', 'editor.selectionHighlightBackground',
    'editor.wordHighlightBackground', 'editor.wordHighlightStrongBackground', 'editor.findMatchBackground',
    'editor.findMatchHighlightBackground', 'editor.findMatchBorder', 'editor.findRangeHighlightBackground',
    'editor.hoverHighlightBackground', 'editor.rangeHighlightBackground',
    'editorCursor.foreground', 'editorGutter.background', 'editorGutter.foldingControlForeground',
    'editorLineNumber.foreground', 'editorLineNumber.activeForeground', 'editorIndentGuide.background1',
    'editorIndentGuide.activeBackground1', 'editorWhitespace.foreground', 'editorRuler.foreground',
    'editorOverviewRuler.border', 'editorOverviewRuler.findMatchForeground',
    'editorOverviewRuler.selectionHighlightForeground', 'editorOverviewRuler.wordHighlightForeground',
    'editorOverviewRuler.errorForeground', 'editorOverviewRuler.warningForeground',
    'editorOverviewRuler.infoForeground', 'editorOverviewRuler.bracketMatchForeground',
    'editorOverviewRuler.rangeHighlightForeground', 'editorOverviewRuler.addedForeground',
    'editorOverviewRuler.deletedForeground', 'editorOverviewRuler.modifiedForeground',
    'editorError.foreground', 'editorWarning.foreground', 'editorInfo.foreground', 'editorLink.activeForeground',
    'editorLightBulb.foreground', 'editorLightBulbAutoFix.foreground', 'editorGhostText.foreground',
    'editorMarkerNavigation.background', 'editorMarkerNavigationError.background',
    'editorMarkerNavigationWarning.background', 'editorMarkerNavigationInfo.background',
    'editorBracketMatch.background', 'editorBracketMatch.border',
    'editorBracketHighlight.foreground1', 'editorBracketHighlight.foreground2', 'editorBracketHighlight.foreground3',
    'editorBracketHighlight.unexpectedBracket.foreground', 'editorStickyScroll.background',
    'editorStickyScrollHover.background', 'scrollbar.shadow', 'scrollbarSlider.background',
    'scrollbarSlider.hoverBackground', 'scrollbarSlider.activeBackground', 'widget.shadow', 'focusBorder',
    'editorWidget.background', 'editorWidget.foreground', 'editorWidget.border',
    'editorHoverWidget.background', 'editorHoverWidget.foreground', 'editorHoverWidget.border',
    'textLink.foreground', 'textLink.activeForeground', 'textPreformat.foreground', 'textCodeBlock.background',
    'keybindingLabel.foreground', 'keybindingLabel.background', 'keybindingLabel.border',
    'keybindingLabel.bottomBorder',
    'peekView.border', 'peekViewTitle.background', 'peekViewTitleLabel.foreground',
    'peekViewTitleDescription.foreground', 'peekViewEditor.background', 'peekViewEditorGutter.background',
    'peekViewEditor.matchHighlightBackground', 'peekViewResult.background', 'peekViewResult.fileForeground',
    'peekViewResult.lineForeground', 'peekViewResult.selectionBackground', 'peekViewResult.selectionForeground',
    'peekViewResult.matchHighlightBackground',
    'editorSuggestWidget.background', 'editorSuggestWidget.foreground', 'editorSuggestWidget.border',
    'editorSuggestWidget.selectedBackground', 'editorSuggestWidget.selectedForeground',
    'editorSuggestWidget.highlightForeground', 'list.hoverBackground', 'list.activeSelectionBackground',
    'list.activeSelectionForeground', 'list.focusBackground', 'list.highlightForeground',
    'list.inactiveSelectionBackground', 'list.focusOutline',
    'menu.background', 'menu.foreground', 'menu.border', 'menu.selectionBackground', 'menu.selectionForeground',
    'menu.separatorBackground', 'quickInput.background', 'quickInput.foreground', 'quickInputList.focusBackground',
    'quickInputList.focusForeground', 'pickerGroup.foreground', 'pickerGroup.border',
    'input.background', 'input.foreground', 'input.border', 'input.placeholderForeground',
    'inputOption.activeBorder', 'inputOption.activeBackground', 'inputOption.activeForeground',
    'diffEditor.insertedTextBackground', 'diffEditor.insertedLineBackground', 'diffEditor.removedTextBackground',
    'diffEditor.removedLineBackground', 'diffEditorGutter.insertedLineBackground',
    'diffEditorGutter.removedLineBackground', 'diffEditorOverview.insertedForeground',
    'diffEditorOverview.removedForeground', 'diffEditor.unchangedRegionBackground',
    'diffEditor.unchangedRegionForeground', 'diffEditor.border', 'diffEditor.diagonalFill',
    'editorGutter.addedBackground', 'editorGutter.deletedBackground', 'editorGutter.modifiedBackground',
];

test('every colour key a visible surface uses is named from the skin, on every skin (R05 W2b)', () => {
    for (const [id, skin] of Object.entries(SKINS)) {
        const c = monacoThemeData(skin).colors;
        for (const key of REQUIRED_COLOR_KEYS) {
            assert.ok(key in c, `${id}: ${key} is left to Monaco's colour registry`);
            assert.match(c[key], /^#[0-9a-f]{8}$/, `${id}: ${key} = ${c[key]}`);
        }
    }
});

test('the scrollbar thumb reads on the editor ground, on every skin (R05 W2b)', () => {
    for (const [id, skin] of Object.entries(SKINS)) {
        const c = monacoThemeData(skin).colors;
        const sheet = c['editor.background'];
        for (const key of ['scrollbarSlider.background', 'scrollbarSlider.hoverBackground',
            'scrollbarSlider.activeBackground']) {
            const r = contrast(composite(c[key], sheet), sheet);
            assert.ok(r >= 1.5, `${id}: ${key} ${c[key]} on ${sheet}: ${r.toFixed(2)}`);
        }
    }
});

/** `#rrggbbaa` painted over an opaque `#rrggbbaa` ground, as sRGB 0..1. */
function composite(hex8: string, ground: string): number[] {
    const ch = (h: string, i: number) => parseInt(h.slice(i, i + 2), 16) / 255;
    const a = ch(hex8, 7);
    return [1, 3, 5].map((i) => a * ch(hex8, i) + (1 - a) * ch(ground, i));
}

/** WCAG 2 contrast of an sRGB colour against an opaque `#rrggbbaa`. */
function contrast(rgb: number[], ground: string): number {
    const lin = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    const lum = (v: number[]) => v.map(lin).reduce((s, c, i) => s + c * [0.2126, 0.7152, 0.0722][i], 0);
    const g = [1, 3, 5].map((i) => parseInt(ground.slice(i, i + 2), 16) / 255);
    const [hi, lo] = [lum(rgb), lum(g)].sort((p, q) => q - p);
    return (hi + 0.05) / (lo + 0.05);
}

/** The two alpha digits of an rgba() token, as toHex8 writes them. */
function toAlpha(rgba: string): string {
    const a = Number(rgba.slice(rgba.lastIndexOf(',') + 1, -1));
    return Math.round(a * 255).toString(16).padStart(2, '0');
}

// ---------------------------------------------------------------------------
// The style: Monaco's own themes, a mono family, the text size (RS-04)
// ---------------------------------------------------------------------------

test('the style control offers the skin or Monaco’s own themes, a mono family and the text size (RS-04)', async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    assert.deepEqual(mod.MONACO_THEMES.map((t: { id: string }) => t.id), ['skin', 'vs', 'vs-dark', 'hc-black']);
    // A stored built-in shows it; anything else is the skin's one theme.
    for (const id of ['vs', 'vs-dark', 'hc-black']) assert.equal(mod.monacoThemeName(id), id);
    for (const id of [null, undefined, 'skin', '', 'collimate', 'hc-light', 7]) assert.equal(mod.monacoThemeName(id), MONACO_THEME, String(id));
    // The size scales Monaco's own, inside 70%–200%; null is Monaco's own.
    assert.deepEqual({ ...mod.MONACO_TEXT_SIZE }, { min: 0.7, max: 2, step: 0.1, default: 1 });
    assert.equal(mod.monacoFontSize(14, null), 14);
    assert.equal(mod.monacoFontSize(12, '1.5'), 12);
    assert.equal(mod.monacoFontSize(14, 1.2), 16.8);
    assert.equal(mod.monacoFontSize(14, 5), 28);
    assert.equal(mod.monacoFontSize(14, 0.1), 9.8);
    // The family's stack from the catalogue, else the record's mono font.
    const families = [{ family: 'Fira Code', label: 'Fira Code', role: 'mono', stack: '"Fira Code", mono-stack' }];
    assert.equal(mod.familyStack('Fira Code', families), '"Fira Code", mono-stack');
    assert.equal(mod.familyStack('Gone', families), null);
    assert.equal(mod.familyStack(null, families), null);
    assert.deepEqual(mod.monacoEditorFont(NIGHT, '"Fira Code", mono-stack'), { fontFamily: '"Fira Code", mono-stack' });
    assert.deepEqual(mod.monacoEditorFont(NIGHT, null), monacoFontOptions(NIGHT));
});

test('a press below the last line is Monaco’s own: the renderer adds no caret handling (RS-04)', async () => {
    const fs = await import('node:fs');
    const path = await import('node:path');
    const source = fs.readFileSync(path.join(process.cwd(), 'content', 'renderers', 'documents', 'monaco', 'main.tsx'), 'utf8');
    // Monaco 0.57.0's mouse target for the area after the lines is
    // CONTENT_EMPTY at the last line's last column, which its mouse handler
    // focuses and moves the cursor to (mouseTarget.js _hitTestViewLines,
    // mouseHandler.js _onMouseDown), so nothing intercepts the press.
    assert.match(source, /Monaco 0\.57\.0 does\n\/\/ this itself/);
    assert.doesNotMatch(source, /onMouseDown\(|MouseTargetType/);
    assert.match(source, /const MONACO_BASE = 'https:\/\/cdn\.jsdelivr\.net\/npm\/monaco-editor@0\.57\.0\/min\/vs';/);
    // The control, top right, shown only while the pointer is near its
    // corner: it owns that reach (the SDK's tests/test_style_control_reach.py),
    // watching its parent, the editor box, so the editor keeps no hover state.
    assert.match(source, /<div style=\{\{ \.\.\.EDITOR_BOX, position: 'relative' \}\}>\s*<div ref=\{containerRef\}/);
    assert.match(source, /<StyleControl api=\{api\} themes=\{MONACO_THEMES\} font="mono" size=\{MONACO_TEXT_SIZE\}\s*style=\{STYLE_CONTROL_PLACE\} \/>/);
    assert.doesNotMatch(source, /visible=\{|setHovered|onMouseEnter=/);
});
