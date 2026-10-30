// SPDX-License-Identifier: MIT
//
// dark.test.ts — the page renderer's document view (the SDK's
// packages/renderer-hooks/src/document-view.tsx), tested off-DOM.
//
// `injectDarkReader` decides what actually reaches the user's browser inside
// the preview frame, so the two properties worth pinning are exactly the two
// that are invisible at a glance: that dark mode OFF ships the page untouched
// (no DarkReader, no beacon, byte-for-byte the file as authored, but for
// the renderer's variables block, its text-size zoom, with its listener), and that dark
// mode ON puts DarkReader at the FRONT of <head> — later and the page paints
// light first, then flips. DarkReader comes from the app's own origin.
//
// The harness bundles main.tsx and the document view with framework imports
// stubbed (scripts/bundle_renderer_test.mjs) and passes HTML_RENDERER_BUNDLE.
// It runs under node:test via the tsx loader; see
// tests/test_html_renderer_dark.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.HTML_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'HTML_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_html_renderer_dark.py does this).',
    );
}

// Populated in a `before` hook rather than by top-level await: the tsx loader
// transpiles this file to CommonJS, where top-level await is unsupported.
let injectDarkReader: (html: string, on: boolean, options: Record<string, string>, injection: { src: string; nonce?: string }) => string;
let documentSrcDoc: (prepared: { html: string; nonce?: string }, dark: boolean, options: Record<string, string>, src: string, variables?: string | null, pointer?: boolean, pick?: boolean) => string;
let darkReaderScriptUrl: () => string;
let DARKREADER_VERSION: string;
let DARKREADER_PATH: string;
let hostThemeKnown: (theme: any) => boolean;
let HOST_THEME_WAIT_MS: number;
let themeSettled: (theme: any, api: any) => boolean;
let frameBacking: (settled: boolean, dark: boolean, scheme: 'light' | 'dark') => Record<string, unknown>;
let darkReaderOptions: (theme: any) => Record<string, string>;
let darkReaderPoles: (scheme: 'light' | 'dark') => { ground: string; text: string };
let DARKREADER_MESSAGE: string;
let S: Record<string, Record<string, unknown>>;
type SkinPost = { type: string; options: Record<string, string>; variables: string | null };
let documentSkinKey: (dark: boolean, options: Record<string, string>, variables: string | null) => string;
let documentSkinPost: (listening: boolean, delivered: string, skinKey: string) => SkinPost | null;
let DOCUMENT_VARIABLES_ID: string;
let deliveredAfterLoad: (previewKey: string | null | undefined) => string;
let pageDark: (themeId: unknown) => boolean | null;
let pageZoomRule: (textSize: unknown) => string;
let PAGE_THEMES: ReadonlyArray<{ id: string; label: string }>;
let PAGE_TEXT_SIZE: Readonly<{ min: number; max: number; step: number; default: number }>;

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    injectDarkReader = mod.injectDarkReader;
    documentSrcDoc = mod.documentSrcDoc;
    darkReaderScriptUrl = mod.darkReaderScriptUrl;
    DARKREADER_VERSION = mod.DARKREADER_VERSION;
    DARKREADER_PATH = mod.DARKREADER_PATH;
    hostThemeKnown = mod.hostThemeKnown;
    HOST_THEME_WAIT_MS = mod.HOST_THEME_WAIT_MS;
    themeSettled = mod.themeSettled;
    frameBacking = mod.frameBacking;
    darkReaderOptions = mod.darkReaderOptions;
    darkReaderPoles = mod.darkReaderPoles;
    DARKREADER_MESSAGE = mod.DARKREADER_MESSAGE;
    S = mod.DOCUMENT_VIEW_STYLE;
    documentSkinKey = mod.documentSkinKey;
    documentSkinPost = mod.documentSkinPost;
    DOCUMENT_VARIABLES_ID = mod.DOCUMENT_VARIABLES_ID;
    deliveredAfterLoad = mod.deliveredAfterLoad;
    pageDark = mod.pageDark;
    pageZoomRule = mod.pageZoomRule;
    PAGE_THEMES = mod.PAGE_THEMES;
    PAGE_TEXT_SIZE = mod.PAGE_TEXT_SIZE;
    assert.equal(typeof injectDarkReader, 'function',
        'the document view must export injectDarkReader for this test to have anything to check');
    assert.equal(typeof hostThemeKnown, 'function',
        'the document view must export hostThemeKnown for this test to have anything to check');
});

const PAGE = '<!doctype html>\n<html><head><meta charset="utf-8"><title>t</title></head><body><p>hi</p></body></html>';

// The shipped skins' grounds and inks (frontend/src/lib/themes.ts).
const NIGHT_VARS = { '--sheet': '#1c1a17', '--sheet-2': '#23201c', '--line-2': '#3f3931', '--ink': '#e2ddd4' };
const PAPER_VARS = { '--sheet': '#f8f7f3', '--sheet-2': '#f1efea', '--line-2': '#c4bfb5', '--ink': '#1e2227' };
const FONTS = { ui: 'Inter, sans-serif', mono: 'Geist Mono, monospace', prose: 'Alegreya, serif' };
const NIGHT = { themeId: 'night', scheme: 'dark', known: true, vars: NIGHT_VARS, fonts: FONTS };
const PAPER = { themeId: 'paper', scheme: 'light', known: true, vars: PAPER_VARS, fonts: FONTS };

/** The library on the app's own origin, as `darkReaderScriptUrl` gives it. */
const SRC = 'http://127.0.0.1:8000/darkreader/4.9.133/darkreader.js';
const LIB = { src: SRC };

/** Dark mode on, darkened onto the night skin. */
const darken = (html: string) => injectDarkReader(html, true, darkReaderOptions(NIGHT), LIB);

/** Where the injected <script src="…darkreader…"> lands, or -1. */
function scriptAt(out: string): number {
    return out.indexOf(`<script src="${SRC}"`);
}

test('off leaves the document byte-for-byte alone (R04)', () => {
    assert.equal(injectDarkReader(PAGE, false, darkReaderOptions(NIGHT), LIB), PAGE);
    // Nothing at all, not even an inert marker — a light preview must not
    // load or run script the file did not ask for.
    assert.equal(injectDarkReader(PAGE, false, darkReaderOptions(NIGHT), LIB).includes('darkreader'), false);
    assert.equal(injectDarkReader(PAGE, false, darkReaderOptions(PAPER), LIB), PAGE);
    // No library URL (no http(s) location): the page as authored, never a
    // bootstrap with nothing to run.
    assert.equal(injectDarkReader(PAGE, true, darkReaderOptions(NIGHT), { src: '' }), PAGE);
});

test('on injects DarkReader immediately after <head> (R04)', () => {
    const out = darken(PAGE);
    const headEnd = out.indexOf('<head>') + '<head>'.length;
    assert.equal(scriptAt(out), headEnd,
        'DarkReader must be the FIRST thing in <head> — anything earlier in the ' +
        'head paints before it and the page flashes light');
    // …and ahead of the content that was already in the head.
    assert.ok(scriptAt(out) < out.indexOf('<meta charset'));
});

test('DarkReader is the pinned build the app serves from its own origin, never a CDN (polish)', () => {
    assert.equal(DARKREADER_VERSION, '4.9.133');
    assert.equal(DARKREADER_PATH, `/darkreader/${DARKREADER_VERSION}/darkreader.js`);
    // Node has no `location`: nothing to load from.
    assert.equal(darkReaderScriptUrl(), '');
    const g = globalThis as any;
    const prior = Object.getOwnPropertyDescriptor(g, 'location');
    try {
        Object.defineProperty(g, 'location', { value: { href: 'http://127.0.0.1:8000/sandbox.html?renderer=html' }, configurable: true });
        assert.equal(darkReaderScriptUrl(), SRC);
        Object.defineProperty(g, 'location', { value: { href: 'file:///tmp/x.html' }, configurable: true });
        assert.equal(darkReaderScriptUrl(), '');
    } finally {
        if (prior) Object.defineProperty(g, 'location', prior); else delete g.location;
    }
    assert.equal(darken(PAGE).includes('jsdelivr'), false);
});

test('with a nonce every injected tag carries it, behind a leading CSP meta (polish)', () => {
    const csp = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'nonce-abc123\'">';
    const doc = `<!doctype html>\n<html><head>${csp}<style id="v">:root{}</style></head><body></body></html>`;
    const out = injectDarkReader(doc, true, darkReaderOptions(NIGHT), { src: SRC, nonce: 'abc123' });
    // The policy stays first, so it governs the injected tags.
    assert.equal(scriptAt(out), out.indexOf(csp) + csp.length);
    const tags = out.match(/<script\b[^>]*>/g) || [];
    assert.equal(tags.length, 2);
    for (const tag of tags) assert.ok(tag.includes(' nonce="abc123"'), tag);
    // Without a nonce a leading CSP meta is not skipped: an authored page's
    // own policy never decides where the html renderer's tags go.
    const plain = injectDarkReader(doc, true, darkReaderOptions(NIGHT), LIB);
    assert.equal(scriptAt(plain), plain.indexOf('<head>') + '<head>'.length);
    assert.equal(/nonce=/.test(plain), false);
    // A nonce that could break out of the attribute is dropped, not written.
    const odd = injectDarkReader(doc, true, {}, { src: SRC, nonce: 'x" onload="y' });
    assert.equal(odd.includes('onload'), false);
});

test('documentSrcDoc is the prepared document, darkened by the same rule (polish)', () => {
    assert.equal(documentSrcDoc({ html: PAGE }, false, darkReaderOptions(NIGHT), SRC), PAGE);
    assert.equal(documentSrcDoc({ html: PAGE }, true, darkReaderOptions(NIGHT), SRC), darken(PAGE));
    const withNonce = documentSrcDoc({ html: PAGE, nonce: 'n1' }, true, darkReaderOptions(NIGHT), SRC);
    assert.equal(withNonce, injectDarkReader(PAGE, true, darkReaderOptions(NIGHT), { src: SRC, nonce: 'n1' }));
});

test('the page’s look is the style’s theme: Auto follows the skin, As authored and Darkened decide (RS-04)', () => {
    assert.deepEqual(PAGE_THEMES.map((t) => [t.id, t.label]),
        [['skin', 'Auto'], ['authored', 'As authored'], ['darkened', 'Darkened']]);
    assert.equal(pageDark('darkened'), true);
    assert.equal(pageDark('authored'), false);
    for (const auto of [null, undefined, 'skin', '', 'auto', 'dark', 1]) assert.equal(pageDark(auto), null, String(auto));
});

test('the text size zooms the page from the variables block, nothing at its own size (RS-04)', () => {
    assert.deepEqual({ ...PAGE_TEXT_SIZE }, { min: 0.5, max: 2, step: 0.1, default: 1 });
    for (const own of [null, undefined, 1, '1.2', Number.NaN]) assert.equal(pageZoomRule(own), '', String(own));
    assert.equal(pageZoomRule(1.2), ':where(:root) { zoom: 1.2; }');
    assert.equal(pageZoomRule(0.8), ':where(:root) { zoom: 0.8; }');
    assert.equal(pageZoomRule(5), ':where(:root) { zoom: 2; }');
    assert.equal(pageZoomRule(0.1), ':where(:root) { zoom: 0.5; }');
    // Nothing in it can close the block's style element.
    assert.doesNotMatch(pageZoomRule(1.5), /</);
});

test('the bootstrap enables DarkReader behind a guard (R04)', () => {
    const out = darken(PAGE);
    assert.ok(out.includes('DarkReader.enable('));
    // Bound fetch: DarkReader calls the stored method detached, and an unbound
    // window.fetch throws "Illegal invocation".
    assert.ok(out.includes('DarkReader.setFetchMethod(window.fetch.bind(window))'));
    // A blocked or offline CDN must degrade to the page as authored.
    assert.ok(out.includes('if(!window.DarkReader)return;'));
    assert.ok(out.includes('try{') && out.includes('catch(e){}'));
});

test('injects exactly once (R04)', () => {
    const out = darken(PAGE);
    assert.equal(out.split(`<script src="${SRC}"`).length - 1, 1);
});

test('a head with attributes still matches (R04)', () => {
    const out = darken('<html><head data-x="1"><title>t</title></head><body></body></html>');
    assert.equal(scriptAt(out), out.indexOf('<head data-x="1">') + '<head data-x="1">'.length);
});

test('<header> is not mistaken for <head> (R04)', () => {
    const out = darken('<div><header>nav</header><p>hi</p></div>');
    assert.equal(scriptAt(out), 0, 'a headless fragment gets the script first, not inside <header>');
    assert.ok(out.endsWith('<div><header>nav</header><p>hi</p></div>'));
});

test('falls back to just inside <html> when there is no head (R04)', () => {
    const out = darken('<html><body><p>hi</p></body></html>');
    assert.equal(scriptAt(out), '<html>'.length);
});

test('never jumps in front of a doctype (R04)', () => {
    // A doctype that is not the first thing in the file is ignored, and the
    // page silently drops into quirks mode.
    const out = darken('<!doctype html>\n<p>hi</p>');
    assert.ok(out.startsWith('<!doctype html>'));
    assert.equal(scriptAt(out), '<!doctype html>'.length);
});

test('empty and nullish input do not throw (R04)', () => {
    assert.equal(injectDarkReader('', false, {}, LIB), '');
    assert.ok(scriptAt(darken('')) === 0);
    assert.equal(injectDarkReader(undefined as unknown as string, false, {}, LIB), '');
});

// ── hostThemeKnown ────────────────────────────────────────────────────────
//
// The renderer must not build a srcDoc from the iframe host's PLACEHOLDER
// theme: it reads 'dark' before the host has sent anything, so a light-skin
// user would get a DarkReader-injected first paint and then a second load of
// the page when the real scheme lands. The snapshot's `known` flag is the only
// signal; these pin that nothing else (an id, a var map) stands in for it.

test('the placeholder snapshot is not a known theme (R04)', () => {
    assert.equal(hostThemeKnown({ themeId: 'unknown', scheme: 'dark', known: false, vars: {}, fonts: { ui: '', mono: '', prose: '' } }), false);
});

test('a theme the host sends is known, in either skin (R04)', () => {
    assert.equal(hostThemeKnown({ themeId: 'night', scheme: 'dark', known: true, vars: NIGHT_VARS, fonts: FONTS }), true);
    assert.equal(hostThemeKnown({ themeId: 'paper', scheme: 'light', known: true, vars: PAPER_VARS, fonts: FONTS }), true);
});

test('known alone decides: an id or a populated var map is not enough (R04)', () => {
    assert.equal(hostThemeKnown({ themeId: 'night', scheme: 'dark', known: false, vars: NIGHT_VARS, fonts: FONTS }), false);
    assert.equal(hostThemeKnown({ themeId: 'paper', scheme: 'light', vars: PAPER_VARS }), false);
    // An empty var map does not make a sent theme unknown.
    assert.equal(hostThemeKnown({ themeId: 'unknown', scheme: 'light', known: true, vars: {}, fonts: FONTS }), true);
    // Only the boolean true counts.
    assert.equal(hostThemeKnown({ themeId: 'paper', scheme: 'light', known: 'true', vars: PAPER_VARS }), false);
});

test('a missing or malformed snapshot is not a known theme (R04)', () => {
    for (const bad of [null, undefined, 'dark', 42, {}, { scheme: 'light' }]) {
        assert.equal(hostThemeKnown(bad), false, `${JSON.stringify(bad)} must not count as the host's answer`);
    }
});

test('a host without theme handles renders straight away, never a blank pane (R04)', () => {
    // No onThemeChange means no push will ever come: nothing to wait for.
    assert.equal(themeSettled(null, {}), true);
    assert.equal(themeSettled(null, null), true);
    assert.equal(themeSettled(null, undefined), true);
    assert.equal(themeSettled({ scheme: 'light', known: false }, { getTheme: () => ({}) }), true);
    // A host that can push is waited for until it does.
    const sub = { getTheme: () => null, onThemeChange: () => () => {} };
    assert.equal(themeSettled(null, sub), false);
    assert.equal(themeSettled({ themeId: 'unknown', scheme: 'dark', known: false, vars: {}, fonts: FONTS }, sub), false);
    assert.equal(themeSettled({ themeId: 'paper', scheme: 'light', known: true, vars: PAPER_VARS, fonts: FONTS }, sub), true);
});

test('the wait for the host is bounded (R04)', () => {
    // A host that never sends a theme must not leave the page blank forever.
    assert.equal(typeof HOST_THEME_WAIT_MS, 'number');
    assert.ok(HOST_THEME_WAIT_MS > 0 && HOST_THEME_WAIT_MS <= 5000, 'a blank pane is worse than a wrong guess after a few seconds');
});

// ── no frame ──────────────────────────────────────────────────────────────
//
// The page fills the renderer's box: no margin, no hairline, no radius. The
// page's own backing is DarkReader's ground only while DarkReader is
// darkening it, and nothing is painted on a guess.

test('the html renderer draws no frame: the page fills the cell (polish)', () => {
    assert.deepEqual(S.page, { position: 'relative', boxSizing: 'border-box', width: '100%', height: '100%' });
    assert.deepEqual(S.iframe, { display: 'block', width: '100%', height: '100%', border: 'none' });
    for (const style of Object.values(S)) {
        for (const prop of ['margin', 'padding', 'borderRadius', 'outline', 'boxShadow']) {
            assert.equal((style as Record<string, unknown>)[prop], undefined, prop);
        }
    }
    assert.equal('frame' in S, false);
    // The view draws no switch of its own: the renderer's style control is
    // the choice.
    assert.deepEqual(Object.keys(S).sort(), ['iframe', 'page']);
});

test('no chrome style carries a colour literal (R04)', () => {
    const literal = /#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i;
    for (const [name, style] of Object.entries(S)) {
        for (const [prop, value] of Object.entries(style)) {
            assert.ok(!(typeof value === 'string' && literal.test(value)),
                `S.${name}.${prop} = ${String(value)} is a colour literal; use a skin token`);
        }
    }
});

// ── DarkReader on the skin (W2b) ──────────────────────────────────────────
//
// A darkened page is darkened ONTO THE SKIN: DarkReader's dark ground and
// text are the skin's own colours, resolved from the record, and the frame
// behind the page paints the same token, so nothing in the renderer is a
// colour of DarkReader's or the browser's choosing outside the authored-page
// case (DarkReader off in a dark skin).

test("the DarkReader options builder takes the skin's sheet and ink (R04 W2b)", () => {
    assert.deepEqual(darkReaderOptions(NIGHT), {
        darkSchemeBackgroundColor: '#1c1a17ff',
        darkSchemeTextColor: '#e2ddd4ff',
    });
    // night-cool, with a token in rgb() syntax: resolved through toHex8.
    const cool = { ...NIGHT, themeId: 'night-cool', vars: { '--sheet': ' rgb(23, 29, 35) ', '--ink': '#dbdfe3' } };
    assert.deepEqual(darkReaderOptions(cool), {
        darkSchemeBackgroundColor: '#171d23ff',
        darkSchemeTextColor: '#dbdfe3ff',
    });
});

test('a light skin darkens onto its own ink, with its sheet as the text (R04 W2b)', () => {
    assert.deepEqual(darkReaderPoles('dark'), { ground: '--sheet', text: '--ink' });
    assert.deepEqual(darkReaderPoles('light'), { ground: '--ink', text: '--sheet' });
    assert.deepEqual(darkReaderOptions(PAPER), {
        darkSchemeBackgroundColor: '#1e2227ff',
        darkSchemeTextColor: '#f8f7f3ff',
    });
});

test('a snapshot without the tokens leaves DarkReader its own defaults (R04 W2b)', () => {
    // The iframe placeholder before the host's first theme, a host with no
    // theme wiring, and a value toHex8 cannot read: nothing is invented.
    assert.deepEqual(darkReaderOptions({ themeId: 'unknown', scheme: 'dark', known: false, vars: {}, fonts: { ui: '', mono: '', prose: '' } }), {});
    assert.deepEqual(darkReaderOptions(null), {});
    assert.deepEqual(darkReaderOptions({ scheme: 'dark', vars: { '--sheet': 'var(--x)', '--ink': '#e2ddd4' } }),
        { darkSchemeTextColor: '#e2ddd4ff' });
});

test("the bootstrap enables DarkReader with the skin's colours and DarkReader's own filters (R04 W2b)", () => {
    const out = darken(PAGE);
    assert.ok(out.includes('DarkReader.enable({"darkSchemeBackgroundColor":"#1c1a17ff","darkSchemeTextColor":"#e2ddd4ff"});'), out);
    // DarkReader's brightness/contrast/sepia defaults leave the computed
    // colours untouched; any filter setting would tint the page off the skin.
    for (const filter of ['brightness', 'contrast', 'sepia', 'grayscale']) {
        assert.equal(out.includes(filter), false, `${filter} must be DarkReader's default`);
    }
    // A different skin bakes different colours.
    const paper = injectDarkReader(PAGE, true, darkReaderOptions(PAPER), LIB);
    assert.ok(paper.includes('"darkSchemeBackgroundColor":"#1e2227ff"'));
});

test('the bootstrap re-enables DarkReader on a skin change from the parent only (R04 W2b)', () => {
    const out = darken(PAGE);
    assert.equal(DARKREADER_MESSAGE, 'collimate:darkreader');
    assert.ok(out.includes('window.addEventListener("message",'));
    assert.ok(out.includes(`e.source!==window.parent`), 'only the renderer may recolour the page');
    assert.ok(out.includes(`d.type!=="${DARKREADER_MESSAGE}"`));
    // Disable first: the pinned DarkReader keeps its first colours on a bare
    // re-enable of a page it has already darkened.
    assert.ok(out.includes('DarkReader.disable();DarkReader.enable(d.options||{})'));
    // The listener sits behind the same guard as the first enable.
    assert.ok(out.indexOf('if(!window.DarkReader)return;') < out.indexOf('addEventListener'));
});

test('nothing in the options can close the bootstrap script (R04 W2b)', () => {
    const out = injectDarkReader(PAGE, true, { darkSchemeBackgroundColor: '</script><script>alert(1)</script>' }, LIB);
    assert.equal(out.split('</script>').length - 1, 2, 'exactly the two script elements the block opens');
    assert.ok(out.includes('\\u003c/script>'));
});

test("the frame backing is the page's ground: the skin's token, or nothing yet (R04 W2b)", () => {
    // DarkReader on: the token DarkReader was given as the ground.
    // Its colour scheme is dark, matching the darkened document, or the
    // browser paints its own dark Canvas behind a page with no ground.
    assert.deepEqual(frameBacking(true, true, 'dark'), { background: 'var(--sheet)', colorScheme: 'dark' });
    assert.deepEqual(frameBacking(true, true, 'light'), { background: 'var(--ink)', colorScheme: 'dark' });
    for (const scheme of ['light', 'dark'] as const) {
        assert.equal(frameBacking(true, true, scheme).background, `var(${darkReaderPoles(scheme).ground})`);
    }
    // DarkReader off in a light skin: the mock's sheet, as before.
    assert.deepEqual(frameBacking(true, false, 'light'), { background: 'var(--sheet)' });
    // DarkReader off in a dark skin: the authored page's own default ground,
    // the browser's light Canvas (the documented authored-page case), never
    // the dark sheet, or every srcDoc swap flashes dark under a white page.
    assert.deepEqual(frameBacking(true, false, 'dark'), { background: 'Canvas', colorScheme: 'light' });
    // Not settled: nothing painted on a guess.
    for (const dark of [false, true]) {
        for (const scheme of ['light', 'dark'] as const) {
            assert.deepEqual(frameBacking(false, dark, scheme), { background: 'transparent' });
        }
    }
});

test('no frame backing carries a colour literal (R04 W2b)', () => {
    const literal = /#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i;
    for (const settled of [false, true]) {
        for (const dark of [false, true]) {
            for (const scheme of ['light', 'dark'] as const) {
                for (const value of Object.values(frameBacking(settled, dark, scheme))) {
                    assert.ok(!literal.test(String(value)), `${String(value)} is a colour literal`);
                }
            }
        }
    }
});

// ── when the preview is built, and the live repaint (W2b) ────────────────
//
// A darkened page bakes the skin's colours into its srcDoc, so it must not be
// built from the iframe host's placeholder: that bakes DarkReader.enable({}),
// the library's own greys, on every open. Only the page as authored (a stored
// override of '0') renders before the host has answered.

test('no page is built before the host theme, the page as authored included (polish)', () => {
    // The placeholder's colours are exactly what the wait keeps out of a
    // darkened page, and its scheme is a guess, so whether a page that
    // follows the skin is darkened at all is unknown until the host answers.
    // Every page waits on that one gate, the page as authored included.
    const placeholder = { themeId: 'unknown', scheme: 'dark', known: false, vars: {}, fonts: { ui: '', mono: '', prose: '' } };
    assert.equal(hostThemeKnown(placeholder), false);
    assert.deepEqual(darkReaderOptions(placeholder), {});
});

/** useDocumentView's delivery loop, off-DOM: the same two helpers in the order
 *  `deliver` and `onLoad` call them, recording what would be posted. */
function page(listening: boolean, bakedKey: string) {
    const posts: SkinPost[] = [];
    let delivered = deliveredAfterLoad(bakedKey);
    return {
        posts,
        deliver(skinKey: string) {
            const message = documentSkinPost(listening, delivered, skinKey);
            if (!message) return;
            posts.push(message);
            delivered = skinKey;
        },
        load(skinKey: string) {
            delivered = deliveredAfterLoad(bakedKey);
            this.deliver(skinKey);
        },
    };
}

const COOL = { ...NIGHT, themeId: 'night-cool', vars: { '--sheet': '#171d23', '--ink': '#dbdfe3' } };
/** The skin key `useDocumentView` derives for a darkened page without a
 *  variables block (read once the bundle is in). */
const key = (theme: unknown) => documentSkinKey(true, darkReaderOptions(theme), null);

test('a skin change repaints a darkened page once, with the new colours (R04 W2b)', () => {
    assert.notEqual(key(NIGHT), key(COOL));
    const p = page(true, key(NIGHT));
    // Built in night: the srcDoc already carries night's colours.
    p.deliver(key(NIGHT));
    assert.deepEqual(p.posts, []);
    // night -> night-cool: one post, the bootstrap's message with cool's options.
    p.deliver(key(COOL));
    assert.equal(p.posts.length, 1);
    assert.deepEqual(p.posts[0], { type: DARKREADER_MESSAGE, options: darkReaderOptions(COOL), variables: null });
    assert.equal(p.posts[0].type, 'collimate:darkreader');
    // The same options again (a re-render, an unrelated theme push): no repost.
    p.deliver(key(COOL));
    assert.equal(p.posts.length, 1);
});

test('a load after the skin moved on reposts the current colours (R04 W2b)', () => {
    // The srcDoc was baked in night; the post for night-cool went out while
    // the swapped document was still loading and may have reached the old one.
    const p = page(true, key(NIGHT));
    p.deliver(key(COOL));
    assert.equal(p.posts.length, 1);
    // The load resets to the baked colours, which differ, so it posts again.
    p.load(key(COOL));
    assert.equal(p.posts.length, 2);
    assert.deepEqual(p.posts[1].options, darkReaderOptions(COOL));
    // A load whose baked colours are current posts nothing.
    const fresh = page(true, key(COOL));
    fresh.load(key(COOL));
    assert.deepEqual(fresh.posts, []);
});

test('a page shown as authored, with no variables block, is never posted to (R04 W2b)', () => {
    const p = page(false, key(NIGHT));
    p.deliver(key(NIGHT));
    p.deliver(key(COOL));
    p.load(key(COOL));
    p.deliver(key(PAPER));
    assert.deepEqual(p.posts, []);
    assert.equal(documentSkinPost(false, '', key(COOL)), null);
    // Its key ignores DarkReader's colours: they are not in that page.
    assert.equal(documentSkinKey(false, darkReaderOptions(NIGHT), null), documentSkinKey(false, darkReaderOptions(COOL), null));
});

test('no preview means nothing is darkened yet (R04 W2b)', () => {
    assert.equal(deliveredAfterLoad(null), '');
    assert.equal(deliveredAfterLoad(undefined), '');
    assert.equal(deliveredAfterLoad(key(NIGHT)), key(NIGHT));
    // So the first darkened document to load is posted the current colours
    // unless its own srcDoc already carries them.
    assert.notEqual(documentSkinPost(true, deliveredAfterLoad(null), key(NIGHT)), null);
});

// ── the variables block, recoloured in place (R04 W2b) ───────────────────
//
// The variables block (the html renderer's text-size zoom) is baked into
// the first srcDoc (the first paint has it) and, on a change, written into
// the loaded page by the same message DarkReader's recolour rides on, so the
// srcDoc never depends on the skin and a skin change never reloads the page.

const RULE_NIGHT = ':where(:root) { --bg: #1c1a17; --fg: #e2ddd4; }';
const RULE_COOL = ':where(:root) { --bg: #171d23; --fg: #dbdfe3; }';

/** The bootstrap's inline script body (the one `<script>` without a src). */
function bootstrapOf(srcDoc: string): string {
    const hit = /<script(?: nonce="[^"]*")?>([\s\S]*?)<\/script>/.exec(srcDoc);
    assert.ok(hit, srcDoc);
    return hit![1];
}

/** Run a bootstrap in a stand-in page: a parent, the view's <style>, and a
 *  DarkReader that records each call with the block's text at that moment. */
function runBootstrap(srcDoc: string, variablesText: string) {
    const parent = {};
    const listeners: ((e: { source: unknown; data: unknown }) => void)[] = [];
    const style = { textContent: variablesText };
    const calls: [string, unknown?, string?][] = [];
    const DarkReader = {
        setFetchMethod() {},
        enable(options: unknown) { calls.push(['enable', options, style.textContent]); },
        disable() { calls.push(['disable']); },
    };
    const win = {
        parent, DarkReader, fetch() {},
        addEventListener(type: string, fn: (e: { source: unknown; data: unknown }) => void) {
            if (type === 'message') listeners.push(fn);
        },
    };
    const doc = { getElementById: (id: string) => (id === DOCUMENT_VARIABLES_ID ? style : null) };
    new Function('window', 'document', 'DarkReader', bootstrapOf(srcDoc))(win, doc, DarkReader);
    return {
        style, calls, listeners,
        post(data: unknown, source: unknown = parent) { for (const fn of listeners) fn({ source, data }); },
    };
}

test('the variables block leads the head with its listener, DarkReader or not (R04 W2b)', () => {
    assert.equal(DOCUMENT_VARIABLES_ID, 'collimate-document-variables');
    const light = documentSrcDoc({ html: PAGE }, false, darkReaderOptions(NIGHT), SRC, RULE_NIGHT);
    const at = light.indexOf('<head>') + '<head>'.length;
    assert.ok(light.startsWith(`<style id="${DOCUMENT_VARIABLES_ID}">${RULE_NIGHT}</style><script>`, at), light);
    // As authored: no library, and the page is otherwise untouched.
    assert.equal(light.includes(SRC), false);
    assert.equal(light.includes('DarkReader'), false);
    assert.equal(light.replace(/<style id="collimate-document-variables">[\s\S]*?<\/script>/, ''), PAGE);
    // Darkened: DarkReader still leads the head, the block right behind it.
    const dark = documentSrcDoc({ html: PAGE }, true, darkReaderOptions(NIGHT), SRC, RULE_NIGHT);
    assert.equal(scriptAt(dark), at);
    assert.ok(dark.indexOf(SRC) < dark.indexOf(`<style id="${DOCUMENT_VARIABLES_ID}">`));
    assert.ok(dark.indexOf(`<style id="${DOCUMENT_VARIABLES_ID}">`) < dark.indexOf('DarkReader.enable('));
    // One bootstrap: the library's tag and it, nothing more.
    assert.equal(dark.split('</script>').length - 1, 2);
    // An empty block (the page at its own text size) still has its
    // element, for the next block.
    const empty = documentSrcDoc({ html: PAGE }, false, darkReaderOptions(NIGHT), SRC, '');
    assert.ok(empty.includes(`<style id="${DOCUMENT_VARIABLES_ID}"></style>`), empty);
    // A view without a block gets none.
    assert.equal(documentSrcDoc({ html: PAGE }, false, darkReaderOptions(NIGHT), SRC), PAGE);
});

test('nothing in the variables block can close its style element (R04 W2b)', () => {
    const out = documentSrcDoc({ html: PAGE }, false, {}, SRC, ':root{--bg:x}</style><script>alert(1)</script>');
    assert.equal(out.split('</style>').length - 1, 1, out);
    assert.equal(out.split('</script>').length - 1, 1, out);
});

test('a skin change writes the variables into the loaded page, then recolours DarkReader (R04 W2b)', () => {
    const srcDoc = documentSrcDoc({ html: PAGE }, true, darkReaderOptions(NIGHT), SRC, RULE_NIGHT);
    const p = runBootstrap(srcDoc, RULE_NIGHT);
    assert.deepEqual(p.calls, [['enable', darkReaderOptions(NIGHT), RULE_NIGHT]]);
    // Only the parent, only the view's message.
    p.post({ type: DARKREADER_MESSAGE, options: darkReaderOptions(COOL), variables: RULE_COOL }, {});
    p.post({ type: 'other', options: darkReaderOptions(COOL), variables: RULE_COOL });
    assert.equal(p.style.textContent, RULE_NIGHT);
    assert.equal(p.calls.length, 1);
    // The view's post: the block first, so DarkReader re-enables over it.
    const post = documentSkinPost(true, documentSkinKey(true, darkReaderOptions(NIGHT), RULE_NIGHT),
        documentSkinKey(true, darkReaderOptions(COOL), RULE_COOL));
    assert.deepEqual(post, { type: DARKREADER_MESSAGE, options: darkReaderOptions(COOL), variables: RULE_COOL });
    p.post(post);
    assert.equal(p.style.textContent, RULE_COOL);
    assert.deepEqual(p.calls.slice(1), [['disable'], ['enable', darkReaderOptions(COOL), RULE_COOL]]);
});

test('a page shown as authored still takes the next skin\'s variables, by message (R04 W2b)', () => {
    const srcDoc = documentSrcDoc({ html: PAGE }, false, darkReaderOptions(PAPER), SRC, RULE_NIGHT);
    const p = runBootstrap(srcDoc, RULE_NIGHT);
    assert.deepEqual(p.calls, [], 'no DarkReader in a page shown as authored');
    // The block is the only thing in its key: DarkReader's colours alone post nothing.
    const baked = documentSkinKey(false, darkReaderOptions(PAPER), RULE_NIGHT);
    assert.equal(documentSkinPost(true, baked, documentSkinKey(false, darkReaderOptions(NIGHT), RULE_NIGHT)), null);
    const post = documentSkinPost(true, baked, documentSkinKey(false, darkReaderOptions(PAPER), RULE_COOL));
    assert.deepEqual(post, { type: DARKREADER_MESSAGE, options: {}, variables: RULE_COOL });
    p.post(post);
    assert.equal(p.style.textContent, RULE_COOL);
    assert.deepEqual(p.calls, []);
});

test('a page told to reports the pointer to its parent, the latest once an animation frame (style control reach)', () => {
    // Off by default: no report in either view without the flag.
    assert.equal(documentSrcDoc({ html: PAGE }, false, darkReaderOptions(NIGHT), SRC, RULE_NIGHT).includes('pointermove'), false);
    // With the flag and nothing else, the page carries the report alone.
    const alone = documentSrcDoc({ html: PAGE }, false, darkReaderOptions(NIGHT), SRC, null, true);
    assert.equal(alone.replace(/<script>[\s\S]*?<\/script>/, ''), PAGE);
    const srcDoc = documentSrcDoc({ html: PAGE }, false, darkReaderOptions(NIGHT), SRC, RULE_NIGHT, true);
    const posts: [unknown, unknown][] = [];
    const moves: [(e: { clientX: number; clientY: number }) => void, unknown][] = [];
    let frame: (() => void) | null = null;
    let frames = 0;
    const win = {
        parent: { postMessage(data: unknown, origin: unknown) { posts.push([data, origin]); } },
        innerWidth: 400, innerHeight: 300,
        addEventListener(type: string, fn: any, opts: unknown) { if (type === 'pointermove') moves.push([fn, opts]); },
        requestAnimationFrame(cb: () => void) { frame = cb; frames++; return frames; },
    };
    new Function('window', 'document', 'DarkReader', bootstrapOf(srcDoc))(win, { getElementById: () => null }, undefined);
    assert.equal(moves.length, 1);
    assert.deepEqual(moves[0][1], { capture: true, passive: true }, 'passive, in the capture phase: the page cannot stop it');
    const move = moves[0][0];
    move({ clientX: 10, clientY: 20 });
    move({ clientX: 380, clientY: 12 });
    assert.equal(frames, 1, 'one frame asked for, however many moves');
    assert.equal(posts.length, 0, 'nothing posted before the frame');
    frame!();
    assert.deepEqual(posts, [[{ type: 'collimate:pointer', x: 380, y: 12, w: 400, h: 300 }, '*']], 'the latest position');
    move({ clientX: 50, clientY: 60 });
    assert.equal(frames, 2);
    frame!();
    assert.deepEqual(posts[1][0], { type: 'collimate:pointer', x: 50, y: 60, w: 400, h: 300 });
    // A darkened page still runs DarkReader and its listener with the report.
    const dark = documentSrcDoc({ html: PAGE }, true, darkReaderOptions(NIGHT), SRC, RULE_NIGHT, true);
    const p = runBootstrap(dark, RULE_NIGHT);
    assert.deepEqual(p.calls, [['enable', darkReaderOptions(NIGHT), RULE_NIGHT]]);
    assert.equal(p.listeners.length, 2);
});
