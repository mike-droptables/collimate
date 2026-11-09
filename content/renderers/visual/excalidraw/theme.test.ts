// SPDX-License-Identifier: MIT
//
// theme.test.ts — Excalidraw's chrome variables and drawing ground, from the
// skin (row R05 of the 2026-10-06 workbench plan, follow-up W2b).
//
// The chrome is Excalidraw's own CSS variables, each set to a skin token for
// the light and the dark theme selector. The ground of a drawing that keeps
// Excalidraw's default follows the skin's --canvas; a colour the user chose
// is kept and saved. The style control's other look, Excalidraw's own
// (RS-04), drops the skin's sheet and grounds the drawing on the library's
// default. The harness bundles main.tsx with framework imports
// stubbed and passes EXCALIDRAW_RENDERER_BUNDLE; see
// tests/test_excalidraw_renderer_theme.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.EXCALIDRAW_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'EXCALIDRAW_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_excalidraw_renderer_theme.py does this).',
    );
}

let excalidrawVars: (scheme: 'light' | 'dark') => Record<string, string>;
let excalidrawCss: () => string;
let preInverted: (color: string) => string | null;
let darkSelectionCss: (theme: { vars?: Record<string, string> }) => string;
let followsSkin: (ground: unknown) => boolean;
let sceneGround: (ground: unknown, own?: boolean) => string;
let ownLook: (themeId: unknown) => boolean;
let EXCALIDRAW_THEMES: ReadonlyArray<{ id: string; label: string }>;
let savedGround: (state: unknown, file: unknown) => unknown;
let SKIN_GROUND: string;
let EXCALIDRAW_DEFAULT_GROUND: string;

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    ({ excalidrawVars, excalidrawCss, followsSkin, sceneGround, savedGround,
        SKIN_GROUND, EXCALIDRAW_DEFAULT_GROUND, preInverted, darkSelectionCss, ownLook, EXCALIDRAW_THEMES } = mod);
    assert.equal(typeof excalidrawVars, 'function', 'main.tsx must export excalidrawVars');
    assert.equal(typeof sceneGround, 'function', 'main.tsx must export sceneGround');
});

// The skin record's 25 names (the W2 contract's THEME_VAR_NAMES).
const SKIN_TOKENS = new Set([
    '--canvas', '--sheet', '--sheet-2', '--sheet-3', '--line', '--line-2', '--ink', '--ink-2', '--ink-3',
    '--brass', '--brass-soft', '--teal', '--teal-soft', '--teal-sheet', '--red', '--red-soft', '--green',
    '--code-bg', '--prose-ink', '--shadow', '--lift', '--font-ui', '--font-mono', '--font-prose', '--radius',
]);
const LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/;
const ONE_TOKEN = /^var\((--[a-z0-9-]+)\)$/;
const TINT = /^color-mix\(in srgb, var\((--[a-z0-9-]+)\) \d{1,2}%, transparent\)$/;

/** The skin token a mapped value reads, or null when it is anything else. */
function tokenOf(value: string): string | null {
    const m = ONE_TOKEN.exec(value) ?? TINT.exec(value);
    return m && SKIN_TOKENS.has(m[1]) ? m[1] : null;
}

test('every mapped Excalidraw variable resolves to a skin token, no literal (R05 W2b)', () => {
    for (const scheme of ['light', 'dark'] as const) {
        const vars = excalidrawVars(scheme);
        assert.ok(Object.keys(vars).length > 80, `${scheme}: the map covers the library's palette`);
        for (const [name, value] of Object.entries(vars)) {
            assert.match(name, /^--[a-zA-Z0-9-]+$/, name);
            assert.doesNotMatch(value, LITERAL, `${scheme} ${name}: ${value}`);
            assert.doesNotMatch(value, /\b(white|black)\b/, `${scheme} ${name}: ${value}`);
            assert.ok(tokenOf(value), `${scheme} ${name} = ${value} is not a skin token`);
        }
        assert.ok(!('--theme-filter' in vars), 'the dark filter stays the library\'s');
    }
});

test('the two theme selectors map the same variables (R05 W2b)', () => {
    const light = excalidrawVars('light');
    const dark = excalidrawVars('dark');
    // The dark selection colour is computed from the record (darkSelectionCss),
    // never a token: the dark canvas filter would turn var(--brass) brown.
    assert.equal(light['--color-selection'], 'var(--brass)');
    assert.ok(!('--color-selection' in dark), 'the dark map leaves the selection to darkSelectionCss');
    const { '--color-selection': _selection, ...lightRest } = light;
    assert.deepEqual(Object.keys(lightRest).sort(), Object.keys(dark).sort());
    const differ = Object.keys(lightRest).filter((k) => light[k] !== dark[k]).sort();
    // Only the library's light-to-dark ramp turns over with the scheme.
    for (const k of differ) assert.match(k, /^--color-(gray-\d+|surface-lowest)$/, k);
    assert.equal(light['--color-surface-lowest'], 'var(--sheet)');
    assert.equal(dark['--color-surface-lowest'], 'var(--canvas)');
    assert.equal(light['--color-gray-10'], 'var(--sheet-2)');   // lightest grey: a sheet in a light skin
    assert.equal(dark['--color-gray-10'], 'var(--ink)');        // and an ink in a dark one
    assert.equal(light['--color-gray-100'], 'var(--ink)');
    assert.equal(dark['--color-gray-100'], 'var(--canvas)');
});

test('islands, surfaces, borders, text, icons, accents and hover take their roles (R05 W2b)', () => {
    for (const scheme of ['light', 'dark'] as const) {
        const v = excalidrawVars(scheme);
        const expect: Record<string, string> = {
            '--island-bg-color': '--sheet-2',
            '--default-bg-color': '--sheet',
            '--default-border-color': '--line-2',
            '--text-primary-color': '--ink',
            '--color-on-surface': '--ink',
            '--icon-fill-color': '--ink-2',
            '--color-primary': '--brass',
            '--color-primary-light': '--brass-soft',
            '--color-primary-darker': '--brass',
            '--color-on-primary-container': '--brass',
            '--link-color': '--teal',
            '--focus-highlight-color': '--teal',
            '--button-hover-bg': '--sheet-3',
            '--color-surface-high': '--sheet-3',
            '--color-surface-low': '--sheet-2',
            '--color-surface-mid': '--sheet-2',
            '--shadow-island': '--shadow',
            '--ui-font': '--font-ui',
        };
        for (const [name, token] of Object.entries(expect)) {
            assert.equal(v[name], `var(${token})`, `${scheme} ${name}`);
        }
    }
});

test('the injected sheet sets both theme selectors from tokens only (R05 W2b)', () => {
    const css = excalidrawCss();
    assert.doesNotMatch(css, LITERAL);
    assert.match(css, /html \.excalidraw \{/);
    assert.match(css, /html \.excalidraw\.theme--dark \{/);
    assert.match(css, /html, body, #mount \{[^}]*background: var\(--canvas\);/);
    assert.doesNotMatch(css, /--theme-filter/);
    for (const [, name] of css.matchAll(/var\((--[a-z0-9-]+)\)/g)) {
        assert.ok(SKIN_TOKENS.has(name), `the sheet reads ${name}, not a skin token`);
    }
    // The dark block carries the dark ramp, the light block the light one.
    const dark = /html \.excalidraw\.theme--dark \{([^}]*)\}/.exec(css)![1];
    const light = /html \.excalidraw \{([^}]*)\}/.exec(css)![1];
    assert.match(dark, /--color-gray-100: var\(--canvas\);/);
    assert.match(light, /--color-gray-100: var\(--ink\);/);
    // The dark theme's literal hover fill sits at the library's doubled-class
    // specificity; the restatement must outrank it.
    assert.match(css, /html \.excalidraw\.theme--dark\.excalidraw \.App-menu__left[^{]*\{ --button-hover-bg: var\(--sheet-3\); \}/);
});

test('a default or missing ground follows the skin; a user colour is kept (R05 W2b)', () => {
    assert.equal(EXCALIDRAW_DEFAULT_GROUND, '#ffffff');
    for (const ground of [undefined, null, '', '#ffffff', '#FFFFFF', ' #ffffff ']) {
        assert.equal(followsSkin(ground), true, String(ground));
        assert.equal(sceneGround(ground), SKIN_GROUND, String(ground));
    }
    for (const ground of ['#a5d8ff', '#ffc9c9', '#1e1e1e', 'transparent']) {
        assert.equal(followsSkin(ground), false, ground);
        assert.equal(sceneGround(ground), ground, ground);
    }
});

test('the skin\'s ground is never written into the file (R05 W2b)', () => {
    // Still following the skin: the file keeps what it had, including nothing.
    assert.equal(savedGround(SKIN_GROUND, undefined), undefined);
    assert.equal(savedGround(SKIN_GROUND, '#ffffff'), '#ffffff');
    const saved = JSON.parse(JSON.stringify({ viewBackgroundColor: savedGround(SKIN_GROUND, undefined) }));
    assert.deepEqual(saved, {});
    // The user picked a colour: that colour is saved.
    assert.equal(savedGround('#ffc9c9', undefined), '#ffc9c9');
    assert.equal(savedGround('#ffc9c9', '#ffffff'), '#ffc9c9');
    // A user colour in the file stays as it is.
    assert.equal(savedGround('#a5d8ff', '#a5d8ff'), '#a5d8ff');
    assert.equal(savedGround('transparent', 'transparent'), 'transparent');
});

// Excalidraw's dark mode is `filter: invert(93%) hue-rotate(180deg)` on its
// canvas (sRGB, alpha untouched). What a viewer sees is the filtered canvas
// pixel over the mount's ground.
type RGBA = [number, number, number, number];
const hex = (h: string): RGBA => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).concat(1) as RGBA;
function darkFilter([r, g, b, a]: RGBA): RGBA {
    const inv = (c: number) => 0.93 - 0.86 * c;
    const [R, G, B] = [inv(r), inv(g), inv(b)];
    const clamp = (c: number) => Math.min(1, Math.max(0, c));
    return [
        clamp(-0.574 * R + 1.43 * G + 0.144 * B),
        clamp(0.426 * R + 0.43 * G + 0.144 * B),
        clamp(0.426 * R + 1.43 * G - 0.856 * B),
        a,
    ];
}
function seen(ground: string, scheme: 'light' | 'dark', mount: string): string {
    const px: RGBA = ground === 'transparent' ? [0, 0, 0, 0] : hex(ground);
    const shown = scheme === 'dark' ? darkFilter(px) : px;
    const under = hex(mount);
    const out = [0, 1, 2].map((i) => shown[i] * shown[3] + under[i] * (1 - shown[3]));
    return '#' + out.map((c) => Math.round(c * 255).toString(16).padStart(2, '0')).join('');
}

test('the skin\'s canvas shows, uninverted, in both schemes (R05 W2b)', () => {
    const skins: Array<[string, 'light' | 'dark', string]> = [
        ['night', 'dark', '#151311'],
        ['night-cool', 'dark', '#10161b'],
        ['paper', 'light', '#e6e4de'],
        ['a black user skin', 'dark', '#000000'],
    ];
    for (const [name, scheme, canvas] of skins) {
        assert.equal(seen(sceneGround(undefined), scheme, canvas), canvas, `${name}: the ground is the skin's canvas`);
        assert.equal(seen(sceneGround('#ffffff'), scheme, canvas), canvas, `${name}: the default ground too`);
    }
    // Why the ground is not the canvas colour itself: the dark filter would
    // invert it to a light grey.
    assert.notEqual(seen('#151311', 'dark', '#151311'), '#151311');
    // A user's colour is the library's to paint, inverted in the dark scheme
    // exactly as on excalidraw.com.
    assert.equal(seen(sceneGround('#a5d8ff'), 'light', '#e6e4de'), '#a5d8ff');
});

// ---------------------------------------------------------------------------
// The selection outline in the dark scheme
// ---------------------------------------------------------------------------

const SKINS = {
    night: { scheme: 'dark', vars: { '--brass': '#c69d64', '--canvas': '#151311' } },
    'night-cool': { scheme: 'dark', vars: { '--brass': '#b9966b', '--canvas': '#10161b' } },
} as const;

const toHex = (px: RGBA) => '#' + px.slice(0, 3).map((c) => Math.round(c * 255).toString(16).padStart(2, '0')).join('');

test('the dark selection outline comes through the canvas filter as the skin\'s brass (R05 W2b)', () => {
    const expected: Record<string, string> = { night: '#825210', 'night-cool': '#845b29' };
    for (const [name, skin] of Object.entries(SKINS)) {
        const css = darkSelectionCss(skin);
        const m = /^html \.excalidraw\.theme--dark \{ --color-selection: (#[0-9a-f]{6}); \}$/.exec(css);
        assert.ok(m, `${name}: ${css}`);
        assert.equal(m[1], expected[name], `${name}: the pre-inverted brass`);
        assert.equal(toHex(darkFilter(hex(m[1]))), skin.vars['--brass'], `${name}: the filter gives back the brass`);
    }
    // What the token itself would have shown: brown, not brass.
    assert.equal(toHex(darkFilter(hex('#c69d64'))), '#815e2d');
    // The library's own dark value is the same construction: #3530c4 is drawn
    // as its dark theme's lavender.
    assert.equal(toHex(darkFilter(hex('#3530c4'))), '#b4b0ff');
    // Translucency is kept; an unreadable or missing brass leaves the library's value.
    assert.match(preInverted('rgba(198, 157, 100, 0.5)')!, /^#825210(7f|80)$/);
    assert.equal(preInverted('oklch(0.7 0.1 70)'), null);
    assert.equal(darkSelectionCss({ vars: {} }), '');
    assert.equal(darkSelectionCss({}), '');
});

// ---------------------------------------------------------------------------
// Every literal colour the library paints is restated
// ---------------------------------------------------------------------------
// The library's stylesheet at the pinned version is vendored as a fixture.
// Each declaration whose value is a literal colour is either a root palette
// variable (covered by excalidrawVars), restated by excalidrawCss on the same
// selector with `html` in front, or on a component listed below with why.

const REPO = process.cwd();
const LIBRARY_CSS_FIXTURE = 'sdk/tests/fixtures/excalidraw-0.18.1-index.css';
const LIBRARY_CSS = readFileSync(path.join(REPO, LIBRARY_CSS_FIXTURE), 'utf8');
const MAIN = readFileSync(path.join(REPO, 'content/renderers/visual/excalidraw/main.tsx'), 'utf8');

const LIBRARY_LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\b(white|black)\b/;

/** Not restated, with the reason. A class in the selector matching one of these exempts the rule. */
const LEFT_TO_THE_LIBRARY: Array<[RegExp, string]> = [
    [/^(collab-button|CollabButton-collaborators|UserList__)/, 'collaboration, which this renderer never turns on'],
    [/^(publish-library|single-library-item)/, 'the library-publishing form for excalidraw.com'],
    [/^ErrorSplash/, 'the error splash, which 0.18.1 never renders'],
    // Classes 0.18.1's stylesheet still carries but its components no longer
    // render (absent from the package's index.js and chunks).
    [/^(color-picker|color-picker-swatch|color-picker-keybinding|color-picker-triangle-shadow|color-picker-transparent|color-picker-content--canvas-title|color-picker-type-\w+)$/, 'the retired colour picker'],
    [/^(picker-swatch|picker-type-\w+)$/, 'the retired picker swatches'],
    [/^(ExportDialog-imageExportButton|divider)$/, 'retired export button and divider'],
];

function splitTopLevel(list: string): string[] {
    const out: string[] = [];
    let depth = 0, start = 0;
    for (let i = 0; i < list.length; i++) {
        const ch = list[i];
        if (ch === '(' || ch === '[') depth++;
        else if (ch === ')' || ch === ']') depth--;
        else if (ch === ',' && depth === 0) { out.push(list.slice(start, i)); start = i + 1; }
    }
    out.push(list.slice(start));
    return out.map((x) => x.trim().replace(/\s+/g, ' ')).filter(Boolean);
}

type Decl = { selector: string; prop: string; value: string };

/** Every (selector, property, value) of a flat or @media-nested stylesheet. */
function declarationsOf(css: string): Decl[] {
    const out: Decl[] = [];
    const flat = css.replace(/\/\*[\s\S]*?\*\//g, '');
    for (const [, sel, body] of flat.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        for (const decl of body.split(';')) {
            const at = decl.indexOf(':');
            if (at < 0) continue;
            const prop = decl.slice(0, at).trim();
            const value = decl.slice(at + 1).trim();
            for (const selector of splitTopLevel(sel)) out.push({ selector, prop, value });
        }
    }
    return out;
}

/** Whether a restating declaration covers the library's property (a longhand
 *  colour counts). The menu and help buttons paint `background-color:
 *  var(--background)`, so painting the fill restates their `--background`. */
const covers = (ours: string, theirs: string) =>
    ours === theirs || ours === `${theirs}-color`
    || ((theirs === 'background' || theirs === '--background') && ours === 'background-color');

test('every literal colour the library paints on a rendered component is restated on a token (R05 W2b)', () => {
    // The fixture is the stylesheet main.tsx loads.
    assert.match(MAIN, /@excalidraw\/excalidraw@0\.18\.1\/dist\/prod\/index\.css/);
    const ours = new Map<string, Set<string>>();
    for (const d of declarationsOf(excalidrawCss())) {
        if (!ours.has(d.selector)) ours.set(d.selector, new Set());
        ours.get(d.selector)!.add(d.prop);
        assert.doesNotMatch(d.value, LITERAL, `${d.selector} ${d.prop}: ${d.value}`);
    }
    const literals = declarationsOf(LIBRARY_CSS)
        .filter((d) => LIBRARY_LITERAL.test(d.value.replace(/var\(--[\w-]+/g, 'var(')));
    assert.ok(literals.length > 150, `the fixture parsed (${literals.length} literal declarations)`);

    const unexplained: string[] = [];
    const usedExemptions = new Set<string>();
    let restated = 0;
    for (const { selector, prop, value } of literals) {
        if (selector === '.excalidraw' || selector === '.excalidraw.theme--dark') {
            const scheme = selector === '.excalidraw' ? 'light' : 'dark';
            if (scheme === 'dark' && prop === '--color-selection') continue;   // darkSelectionCss
            assert.ok(prop in excalidrawVars(scheme), `${selector} ${prop}: ${value} is not mapped`);
            continue;
        }
        const classes = [...selector.matchAll(/\.([A-Za-z_][\w-]*)/g)].map((m) => m[1]);
        const exempt = LEFT_TO_THE_LIBRARY.find(([re]) => classes.some((c) => re.test(c)));
        if (exempt) { usedExemptions.add(exempt[1]); continue; }
        const props = ours.get(`html ${selector}`);
        if (props && [...props].some((p) => covers(p, prop))) { restated++; continue; }
        unexplained.push(`${selector} { ${prop}: ${value} }`);
    }
    assert.deepEqual(unexplained, [], 'library literal colours with no restatement');
    assert.ok(restated >= 40, `restated ${restated}`);
    for (const [, why] of LEFT_TO_THE_LIBRARY) assert.ok(usedExemptions.has(why), `stale exemption: ${why}`);
});

test('the dialogs, cards, swatches and hints a drawing reaches are on the skin (R05 W2b)', () => {
    const css = excalidrawCss();
    for (const rule of [
        // Reset-canvas confirm, the library sidebar's browse button, the text-to-diagram button.
        /html \.excalidraw \.Dialog__action-button--primary, html \.excalidraw \.Dialog__action-button--danger, html \.excalidraw \.library-menu-browse-button, html \.excalidraw \.ttd-dialog-panel-button\.excalidraw-button \{ color: var\(--sheet\); \}/,
        // The export dialog's cards: the library sets lime and pink inline.
        /html \.excalidraw \.Card \{ --card-color: var\(--brass\) !important; --card-color-darker: var\(--brass\) !important; --card-color-darkest: var\(--brass\) !important; \}/,
        /html \.excalidraw \.Card \.Card-icon, html \.excalidraw \.Card \.Card-button\.ToolIcon_type_button \.ToolIcon__label \{ color: var\(--sheet\); \}/,
        /html \.excalidraw \.Card \.Card-button\.ToolIcon_type_button \.Spinner \{ --spinner-color: var\(--sheet\); \}/,
        // Text on the filled warning button (brass): light on paper, dark on night.
        /html \.excalidraw \.ExcButton--color-warning\.ExcButton--variant-filled \{ --text-color: var\(--sheet\); \}/,
        /html \.excalidraw \.ElementLinkDialog \.ElementLinkDialog__input \.ElementLinkDialog__remove[^{]*\{ color: var\(--red\); \}/,
        /html \.excalidraw \.PasteChartDialog \.ChartPreview \{ border-color: var\(--line-2\); \}/,
        /html \.excalidraw \.PasteChartDialog \.ChartPreview:hover \{ border-color: var\(--brass\); \}/,
        /html \.excalidraw__embeddable-container \.excalidraw__embeddable-hint \{ background: var\(--sheet-2\); color: var\(--ink\);/,
    ]) assert.match(css, rule);
    // The swatches the 0.18.1 picker renders are `.color-picker__button`, whose
    // edges read the library's variables; the retired `.color-picker-swatch`
    // rules are not restated.
    assert.doesNotMatch(css, /color-picker-swatch|\.divider\b/);
});

// ---------------------------------------------------------------------------
// Excalidraw's own look (RS-04)
// ---------------------------------------------------------------------------

test('the style control offers the skin or Excalidraw’s own look, and no font or size (RS-04)', () => {
    assert.deepEqual(EXCALIDRAW_THEMES.map((t) => [t.id, t.label]), [['skin', 'Collimate'], ['excalidraw', 'Excalidraw']]);
    assert.equal(ownLook('excalidraw'), true);
    for (const id of [null, undefined, 'skin', 'light', 'dark', '']) assert.equal(ownLook(id), false, String(id));
    // Placed in Excalidraw's own top-right row, shown only while the pointer
    // is near it (the SDK's tests/test_style_control_reach.py): its parent is
    // that small row, so it measures moves over the drawing's wrapper.
    assert.match(MAIN, /<StyleControl api=\{api\} themes=\{EXCALIDRAW_THEMES\} font=\{false\} size=\{false\}\s*watch=\{wrapRef\} style=\{STYLE_CONTROL_PLACE\} \/>\s*\), \[api\]\);/);
    assert.match(MAIN, /const wrapRef = useRef<HTMLDivElement \| null>\(null\);/);
    assert.doesNotMatch(MAIN, /visible=\{|setHovered|onMouseEnter=/);
});

test('a read-only drawing mounts the style control over the canvas, not in Excalidraw’s top-right row (RS-04)', () => {
    // Excalidraw's mobile layout drops the renderTopRightUI row in view mode,
    // so the row carries the control only while the drawing is editable.
    assert.match(MAIN, /viewModeEnabled=\{isReadOnly\}\s*renderTopRightUI=\{isReadOnly \? undefined : renderTopRightUI\}/);
    assert.match(MAIN, /\{isReadOnly && \(\s*<StyleControl api=\{api\} themes=\{EXCALIDRAW_THEMES\} font=\{false\} size=\{false\}\s*style=\{STYLE_CONTROL_OVERLAY\} \/>\s*\)\}/);
    assert.match(MAIN, /const STYLE_CONTROL_OVERLAY = Object\.freeze\(\{ position: 'absolute', top: 8, right: 8, zIndex: 10 \}\);/);
    // The overlay anchors on the wrapper, its parent, whose moves it watches.
    assert.match(MAIN, /<div ref=\{wrapRef\} style=\{\{ position: 'relative', width: '100%', height: '100%' \}\}>/);
});

test('under Excalidraw’s own look a drawing that follows the skin takes the library’s ground; a user colour is kept (RS-04)', () => {
    for (const ground of [undefined, null, '', '#ffffff', '#FFFFFF']) {
        assert.equal(sceneGround(ground, true), EXCALIDRAW_DEFAULT_GROUND, String(ground));
        assert.equal(sceneGround(ground, false), SKIN_GROUND, String(ground));
    }
    for (const ground of ['#a5d8ff', '#1e1e1e', 'transparent']) assert.equal(sceneGround(ground, true), ground, ground);
    // The library's ground is never written into a file that left it to the skin.
    assert.equal(savedGround(EXCALIDRAW_DEFAULT_GROUND, undefined), undefined);
    assert.equal(savedGround(EXCALIDRAW_DEFAULT_GROUND, '#ffffff'), '#ffffff');
    assert.equal(savedGround('#ffc9c9', undefined), '#ffc9c9');
    // A light canvas in the light scheme, and the library's own dark (its
    // filter over white), not the skin's canvas, in the dark one.
    assert.equal(seen(sceneGround(undefined, true), 'light', '#e6e4de'), '#ffffff');
    assert.notEqual(seen(sceneGround(undefined, true), 'dark', '#151311'), '#151311');
});

test('Excalidraw’s own look drops the skin’s sheet and selection colour and keeps the scheme (RS-04)', () => {
    // The skin's sheet is a React <style>, rendered only under the skin, so a
    // change of look removes it; nothing injects it for good.
    assert.doesNotMatch(MAIN, /injectCss\(excalidrawCss\(\)\)/);
    assert.match(MAIN, /const skinCss = useMemo\(\(\) => excalidrawCss\(\), \[\]\);/);
    assert.match(MAIN, /\{!own && <style>\{skinCss\}<\/style>\}/);
    assert.match(MAIN, /const selectionCss = own \? '' : darkSelectionCss\(theme\);/);
    // The library follows the skin's scheme under either look.
    assert.match(MAIN, /excalidrawAPI=\{keepExcalidraw\}\s*theme=\{scheme\}\s*initialData=/);
    // The ground follows the look at mount, on a host revision and on a change.
    assert.match(MAIN, /sceneGround\(appState\.viewBackgroundColor, ownRef\.current\)/);
    assert.match(MAIN, /sceneGround\(scene\.state\.viewBackgroundColor, ownRef\.current\)/);
    assert.match(MAIN, /const next = own \? EXCALIDRAW_DEFAULT_GROUND : SKIN_GROUND;/);
    assert.match(MAIN, /captureUpdate: CaptureUpdateAction\.NEVER \}\);\s*\} finally \{ applyingHostRef\.current = false; \}\s*\}, \[own, excalidraw\]\);/);
});
