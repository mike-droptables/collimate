// SPDX-License-Identifier: MIT
//
// theme.test.ts — the PDF renderer's pure model: its styles on the skin
// tokens, the one EmbedPDF release across its esm.sh pins and the PDFium
// build the engine downloads, the view controls' labels, the pointer modes
// (Pan by default, Select on demand), the view controls living in the
// corner StyleControl (no bar), and the viewer's box. The harness bundles
// main.tsx with framework imports stubbed and passes PDF_RENDERER_BUNDLE and
// PDF_RENDERER_SOURCE; see tests/test_pdf_renderer_model.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.PDF_RENDERER_BUNDLE;
const SOURCE = process.env.PDF_RENDERER_SOURCE;
if (!BUNDLE || !SOURCE) {
    throw new Error(
        'PDF_RENDERER_BUNDLE / PDF_RENDERER_SOURCE are not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_pdf_renderer_model.py does this).',
    );
}

let mod: any;
let source: string;

before(async () => {
    mod = await import(pathToFileURL(BUNDLE).href);
    source = readFileSync(SOURCE, 'utf8');
});

const LITERAL = /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i;

test('every @embedpdf pin is the one EmbedPDF release, and the PDFium build follows it', () => {
    const pins = [...source.matchAll(/https:\/\/esm\.sh\/@embedpdf\/[\w-]+@([\d.]+)/g)].map(m => m[1]);
    assert.ok(pins.length >= 10, `expected the plugin imports, found ${pins.length}`);
    assert.equal(new Set(pins).size, 1, `more than one EmbedPDF release: ${[...new Set(pins)]}`);
    assert.equal(mod.PDFIUM_WASM_URL, `https://cdn.jsdelivr.net/npm/@embedpdf/pdfium@${pins[0]}/dist/pdfium.wasm`);
    assert.deepEqual({ ...mod.ENGINE_OPTIONS }, { wasmUrl: mod.PDFIUM_WASM_URL, worker: false });
});

test('every @embedpdf import carries the one deps list, so the plugins share one core instance', () => {
    const urls = [...source.matchAll(/https:\/\/esm\.sh\/@embedpdf\/[^'"\s]+/g)].map(m => m[0]);
    assert.ok(urls.length >= 11);
    for (const url of urls) assert.match(url, /\?deps=react@19\.3\.1,react-dom@19\.3\.1&target=es2022$/);
});

test('the chrome paints from the skin tokens; a page keeps the light Canvas of an authored document', () => {
    for (const name of ['PDF_ROOT', 'PDF_BODY', 'PDF_VIEWPORT', 'PDF_POINTER_LAYER', 'PDF_PAGE', 'PDF_BUTTON', 'PDF_BUTTON_OFF', 'PDF_BUTTON_ON', 'PDF_SEGMENT_RADIUS', 'PDF_READOUT', 'PDF_LINK']) {
        for (const value of Object.values(mod[name])) assert.doesNotMatch(String(value), LITERAL, name);
        assert.ok(Object.isFrozen(mod[name]), name);
    }
    assert.equal(mod.PDF_ROOT.background, 'var(--sheet)');
    assert.equal(mod.PDF_VIEWPORT.background, 'var(--sheet-2)');
    assert.equal(mod.PDF_BUTTON.background, 'var(--sheet-2)');
    assert.equal(mod.PDF_BUTTON_ON.background, 'var(--brass-soft)');
    assert.equal(mod.PDF_BUTTON_OFF.color, 'var(--ink-3)');
    assert.equal(mod.PDF_PAGE.background, 'Canvas');
    assert.equal(mod.PDF_PAGE.colorScheme, 'light');
    assert.equal(mod.PDF_PAGE.boxShadow, 'var(--shadow)');
    assert.equal(mod.SELECTION_FILL, 'color-mix(in srgb, var(--brass) 35%, transparent)');
});

// EmbedPDF's Viewport pads itself by the viewport gap and the fit zooms size
// the pages to its client box less that gap: the padding sits inside the
// body's width, so a fit-width page ends a gap short of the visible edge.
test('the viewport is border-box, so its gap padding never pushes a fitted page past the edge', () => {
    assert.equal(mod.PDF_VIEWPORT.boxSizing, 'border-box');
    assert.equal(mod.PDF_VIEWPORT.width, '100%');
    assert.equal(mod.PDF_VIEWPORT.height, '100%');
});

// The viewer fills its box, the pages' row taking all of it: there is no
// footer.
test('the viewer fills its box, with no bar of its own', () => {
    assert.equal(mod.PDF_ROOT.position, 'relative', 'the corner control anchors to the viewer');
    assert.equal(mod.PDF_BODY.flex, 1);
    assert.equal(mod.PDF_BODY.minHeight, 0);
    assert.equal(mod.PDF_FOOTER, undefined);
    assert.doesNotMatch(source, /footer/i);
});

// The owner: "no bottom bar for pdf"; page, zoom and fit sit in the corner
// menu, the StyleControl's `extra` rows, as the markdown editor's style sits
// in it. The PDF offers no style of its own, so the popover holds only them.
test('the view controls are rows of the corner StyleControl, which offers no style of its own', () => {
    assert.match(source, /<StyleControl api=\{api\} themes=\{\[\]\} font=\{false\} size=\{false\} extra=\{<ViewRows /);
    for (const label of ['Page', 'Zoom', 'Fit', 'Pointer']) {
        assert.match(source, new RegExp(`<StyleControlRow label="${label}">`), label);
    }
    for (const title of ['Previous page', 'Next page', 'Zoom out', 'Zoom in', 'Fit the page width', 'Fit a whole page']) {
        assert.ok(source.includes(`title="${title}"`), title);
    }
});

test('the zoom and page labels: zoom as a percentage, pages as current of total, dashes before either is known', () => {
    assert.equal(mod.zoomLabel(1), '100%');
    assert.equal(mod.zoomLabel(1.2345), '123%');
    assert.equal(mod.zoomLabel(0.5), '50%');
    assert.equal(mod.zoomLabel(undefined), '—');
    assert.equal(mod.zoomLabel(NaN), '—');
    assert.equal(mod.pageLabel(3, 10), '3 / 10');
    assert.equal(mod.pageLabel(0, 10), '1 / 10');
    assert.equal(mod.pageLabel(12, 10), '10 / 10');
    assert.equal(mod.pageLabel(1, 0), '— / —');
    assert.equal(mod.pageLabel(undefined, undefined), '— / —');
});

// The owner: "grab and drag (navigation) the mouse click action (not grab
// image / copy)". Pan is EmbedPDF's pan plugin made every document's default
// mode; Select is the interaction manager's own pointer mode, where the
// selection layer works.
test('a press pans by default; Select is the one other mode, and anything unknown reads as Pan', () => {
    assert.deepEqual({ ...mod.POINTER_MODES }, { pan: 'panMode', select: 'pointerMode' });
    assert.deepEqual(mod.POINTER_CHOICES.map((c: any) => c.id), ['pan', 'select']);
    assert.deepEqual(mod.POINTER_CHOICES.map((c: any) => c.label), ['Pan', 'Select']);
    assert.deepEqual({ ...mod.PAN_CONFIG }, { defaultMode: 'always' });
    assert.deepEqual(JSON.parse(JSON.stringify(mod.SELECTION_CONFIG)), { marquee: { enabled: false } }, 'Select selects text: no capture box');
    assert.equal(mod.pointerChoiceOf('panMode'), 'pan');
    assert.equal(mod.pointerChoiceOf('pointerMode'), 'select');
    assert.equal(mod.pointerChoiceOf(null), 'pan', 'before the manager reports a mode: the default');
    assert.equal(mod.pointerChoiceOf('marqueeZoom'), 'pan');
    assert.equal(mod.interactionModeFor('pan'), 'panMode');
    assert.equal(mod.interactionModeFor('select'), 'pointerMode');
    assert.equal(mod.interactionModeFor(undefined), 'panMode');
    for (const choice of ['pan', 'select']) assert.equal(mod.pointerChoiceOf(mod.interactionModeFor(choice)), choice);
});

test('the pan plugin is registered with pan as the default, and the pages are never dragged as an image', () => {
    assert.match(source, /createPluginRegistration\(PanPluginPackage, PAN_CONFIG\)/);
    assert.match(source, /createPluginRegistration\(SelectionPluginPackage, SELECTION_CONFIG\)/);
    // The pan mode is global: it listens on the pointer layer inside the
    // viewport (the scrollbars stay outside it), as large as the pages.
    assert.match(source, /<Viewport [^>]*>\s*<GlobalPointerProvider documentId=\{documentId\} style=\{PDF_POINTER_LAYER\}>/);
    assert.equal(mod.PDF_POINTER_LAYER.minWidth, '100%');
    assert.equal(mod.PDF_POINTER_LAYER.minHeight, '100%');
    assert.match(source, /<RenderLayer [^>]*draggable=\{false\}/);
    assert.match(source, /onDragStart=\{[^}]*preventDefault\(\)\}/);
    assert.equal(mod.PDF_PAGE.userSelect, 'none');
    assert.equal(mod.PDF_PAGE.WebkitUserSelect, 'none');
});

test('the failure messages name the file and the engine source', () => {
    assert.equal(mod.documentFailureMessage('a.pdf'), 'Could not open “a.pdf” — the file may be empty, corrupt, or not a PDF.');
    assert.match(mod.engineFailureMessage(new Error('fetch failed')), /could not start \(fetch failed\)\. It loads once through the app's module mirror/);
    assert.match(mod.engineFailureMessage(undefined), /could not start\. It loads once/);
});
