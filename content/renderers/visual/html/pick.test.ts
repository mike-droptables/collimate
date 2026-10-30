// SPDX-License-Identifier: MIT
//
// pick.test.ts — the element picker the html renderer answers annotation
// requests with: the document view's page bridge (PICK_BRIDGE_JS, in
// packages/renderer-hooks/src/document-view.tsx), run against a stand-in
// window and document, and the renderer's own context format (elementContext
// and its parts, in main.tsx).
//
// The harness bundles main.tsx and the document view with framework imports
// stubbed (scripts/bundle_renderer_test.mjs) and passes HTML_RENDERER_BUNDLE.
// It runs under node:test via the tsx loader; see
// tests/test_html_renderer_pick.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.HTML_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'HTML_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_html_renderer_pick.py does this).',
    );
}

type Pick = { tag: string; css: string; id: string; snippet: string; html: string };
let documentSrcDoc: (prepared: { html: string; nonce?: string }, dark: boolean, options: Record<string, string>, src: string, variables?: string | null, pointer?: boolean, pick?: boolean) => string;
let PICK_BRIDGE_JS: string;
let PICK_MESSAGE: Record<string, string>;
let elementPickOf: (raw: unknown) => Pick;
let pickColors: (theme: unknown) => Record<string, string>;
let elementContext: (fileName: string, pick: Pick) => { label: string; text: string; anchor?: Record<string, unknown> };
let pickLabel: (fileName: string, css: string) => string;
let pickText: (fileName: string, pick: Pick) => string;
let inlineCode: (value: string) => string;
let fenceFor: (value: string) => string;
let PICK_LABEL_MAX: number;

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    documentSrcDoc = mod.documentSrcDoc;
    PICK_BRIDGE_JS = mod.PICK_BRIDGE_JS;
    PICK_MESSAGE = mod.PICK_MESSAGE;
    elementPickOf = mod.elementPickOf;
    pickColors = mod.pickColors;
    elementContext = mod.elementContext;
    pickLabel = mod.pickLabel;
    pickText = mod.pickText;
    inlineCode = mod.inlineCode;
    fenceFor = mod.fenceFor;
    PICK_LABEL_MAX = mod.PICK_LABEL_MAX;
    assert.equal(typeof PICK_BRIDGE_JS, 'string', 'the document view must export PICK_BRIDGE_JS');
    assert.equal(typeof elementContext, 'function', 'the html renderer must export elementContext');
});

const PAGE = '<!doctype html>\n<html><head><meta charset="utf-8"><title>t</title></head><body><p>hi</p></body></html>';
const SRC = 'http://127.0.0.1:8000/darkreader/4.9.133/darkreader.js';
const NIGHT = { themeId: 'night', scheme: 'dark', known: true,
    vars: { '--sheet': '#1c1a17', '--ink': '#e2ddd4', '--brass': '#c69d64', '--brass-soft': 'rgba(198, 157, 100, 0.16)' } };
const COLORS = { line: '#c69d64ff', fill: '#c69d6429', ground: '#c69d64ff', ink: '#1c1a17ff' };

// ── a stand-in page ───────────────────────────────────────────────────────

type Rect = { top: number; left: number; width: number; height: number };

/** A window, a document and elements just big enough for the bridge. */
function fakePage() {
    const posts: any[] = [];
    const parent = { postMessage(message: unknown) { posts.push(message); } };
    const listeners: Record<string, { fn: (e: any) => void; opts: any }[]> = {};
    const timers: (() => void)[] = [];
    const shadows: { opts: unknown; kids: any[] }[] = [];
    let doc: any;
    class El {
        nodeType = 1;
        tagName: string;
        id = '';
        kids: El[] = [];
        parentElement: El | null = null;
        attrs: Record<string, string> = {};
        style: Record<string, string> = {};
        text = '';
        markup = '';
        rect: Rect = { top: 0, left: 0, width: 0, height: 0 };
        constructor(tag: string) { this.tagName = tag.toUpperCase(); }
        get isConnected(): boolean {
            for (let e: El | null = this; e; e = e.parentElement) if (e === doc.documentElement) return true;
            return false;
        }
        get previousElementSibling(): El | null {
            const p = this.parentElement;
            const i = p ? p.kids.indexOf(this) : -1;
            return p && i > 0 ? p.kids[i - 1] : null;
        }
        get textContent(): string { return this.text || this.kids.map((k) => k.textContent).join(''); }
        set textContent(value: string) { this.text = value; }
        get outerHTML(): string {
            if (this.markup) return this.markup;
            const tag = this.tagName.toLowerCase();
            return `<${tag}${this.id ? ` id="${this.id}"` : ''}>${this.text}${this.kids.map((k) => k.outerHTML).join('')}</${tag}>`;
        }
        setAttribute(name: string, value: string) { this.attrs[name] = String(value); }
        getAttribute(name: string) { return name in this.attrs ? this.attrs[name] : null; }
        appendChild(child: El) { child.parentElement = this; this.kids.push(child); return child; }
        remove() {
            const p = this.parentElement;
            if (p) p.kids.splice(p.kids.indexOf(this), 1);
            this.parentElement = null;
        }
        attachShadow(opts: unknown) {
            const root = { opts, kids: [] as El[], appendChild(child: El) { root.kids.push(child); return child; } };
            shadows.push(root);
            return root;
        }
        closest(selector: string): El | null {
            assert.equal(selector, '[id]');
            for (let e: El | null = this; e; e = e.parentElement) if (e.id) return e;
            return null;
        }
        getBoundingClientRect() { return this.rect; }
    }
    const el = (tag: string, kids: El[] = [], init: Partial<El> = {}) => {
        const node = Object.assign(new El(tag), init);
        for (const kid of kids) node.appendChild(kid);
        return node;
    };
    const head = el('head');
    const body = el('body');
    const html = el('html', [head, body]);
    doc = { documentElement: html, head, body, createElement: (tag: string) => new El(tag) };
    const win = {
        parent,
        CSS: { escape: (value: string) => value },
        setTimeout(fn: () => void) { timers.push(fn); return timers.length; },
        clearTimeout() {},
        addEventListener(type: string, fn: (e: any) => void, opts: unknown) { (listeners[type] ??= []).push({ fn, opts }); },
    };
    new Function('window', 'document', PICK_BRIDGE_JS)(win, doc);
    function fire(type: string, init: Record<string, unknown> = {}) {
        const event: any = {
            type, button: 0, target: body, defaultPrevented: false, stopped: false, ...init,
            preventDefault() { event.defaultPrevented = true; },
            stopImmediatePropagation() { event.stopped = true; },
        };
        for (const { fn } of listeners[type] ?? []) fn(event);
        return event;
    }
    const send = (data: unknown, source: unknown = parent) => fire('message', { source, data });
    const pickHost = () => html.kids.find((k) => k.tagName === 'COLLIMATE-PICK') ?? null;
    const cursorStyle = () => head.kids.find((k) => k.tagName === 'STYLE') ?? null;
    return { posts, parent, listeners, timers, shadows, el, doc, body, head, html, fire, send, pickHost, cursorStyle };
}

/** `section#sec > ul > li, li("Second item")`, in the page's body. */
function fixture(page: ReturnType<typeof fakePage>) {
    const first = page.el('li', [], { text: 'One' });
    const second = page.el('li', [], { text: 'Second item' });
    const list = page.el('ul', [first, second]);
    const section = page.el('section', [list], { id: 'sec' });
    page.body.appendChild(section);
    return { first, second, list, section };
}

const start = (page: ReturnType<typeof fakePage>, extra: Record<string, unknown> = {}) =>
    page.send({ type: 'collimate:pick-start', id: 'p1', hint: 'Pick one', colors: COLORS, ...extra });

// ── 1. the bridge rides only when asked ───────────────────────────────────

test('the picker rides in the one bootstrap only when asked, ahead of DarkReader (annotations)', () => {
    assert.equal(documentSrcDoc({ html: PAGE }, false, {}, SRC), PAGE);
    assert.equal(documentSrcDoc({ html: PAGE }, false, {}, SRC, null, false, false), PAGE);
    const out = documentSrcDoc({ html: PAGE }, false, {}, SRC, null, false, true);
    assert.equal(out.split(PICK_BRIDGE_JS).length - 1, 1);
    const scripts = out.match(/<script\b[^>]*>[\s\S]*?<\/script>/g) || [];
    assert.equal(scripts.length, 1);
    assert.ok(scripts[0].includes(PICK_BRIDGE_JS));
    // The rest of the page is untouched.
    assert.equal(out.replace(scripts[0], ''), PAGE);
    // Darkened: the picker precedes DarkReader's part, which returns early
    // when the library is missing.
    const dark = documentSrcDoc({ html: PAGE }, true, {}, SRC, null, false, true);
    assert.ok(dark.indexOf(PICK_BRIDGE_JS) > 0);
    assert.ok(dark.indexOf(PICK_BRIDGE_JS) < dark.indexOf('DarkReader.enable('));
    assert.ok(dark.indexOf(PICK_BRIDGE_JS) < dark.indexOf('if(!window.DarkReader)return;'));
    // A page's CSP nonce rides on the bootstrap.
    const nonced = documentSrcDoc({ html: PAGE, nonce: 'n0nce' }, false, {}, SRC, null, false, true);
    assert.ok(/<script nonce="n0nce">[\s\S]*collimate:pick-start/.test(nonced), nonced);
    // Nothing in the bridge can close its script, and it names only window,
    // document and built-ins.
    assert.equal(PICK_BRIDGE_JS.includes('<'), false);
    assert.equal(/(?<![\w.])(parent|Element|setTimeout|clearTimeout)\b/.test(PICK_BRIDGE_JS), false);
    assert.deepEqual({ ...PICK_MESSAGE }, {
        start: 'collimate:pick-start', cancel: 'collimate:pick-cancel', probe: 'collimate:pick-probe',
        ready: 'collimate:pick-ready', picked: 'collimate:picked', abandoned: 'collimate:pick-abandoned',
    });
});

// ── 2. idle ───────────────────────────────────────────────────────────────

test('an idle bridge announces itself, answers the parent’s probe only, and stops nothing (annotations)', () => {
    const page = fakePage();
    assert.deepEqual(page.posts, [{ type: 'collimate:pick-ready' }]);
    page.send({ type: 'collimate:pick-probe' });
    assert.deepEqual(page.posts, [{ type: 'collimate:pick-ready' }, { type: 'collimate:pick-ready' }]);
    page.send({ type: 'collimate:pick-probe' }, {});
    assert.equal(page.posts.length, 2, 'a probe from any other window gets nothing');
    const click = page.fire('click');
    assert.equal(click.defaultPrevented, false);
    assert.equal(click.stopped, false);
    assert.equal(page.fire('keydown', { key: 'Escape' }).defaultPrevented, false);
    assert.equal(page.pickHost(), null, 'no layer before the first pick');
    // Every press, click and key listener sits in the capture phase.
    for (const type of ['click', 'pointerdown', 'mousedown', 'keydown', 'keyup', 'keypress', 'contextmenu']) {
        assert.ok(page.listeners[type]?.length, type);
        assert.equal(page.listeners[type][0].opts.capture, true, type);
    }
});

// ── 3. armed ──────────────────────────────────────────────────────────────

test('an armed bridge outlines the element under the pointer in its own closed layer (annotations)', () => {
    const page = fakePage();
    const { second } = fixture(page);
    start(page);
    const host = page.pickHost();
    assert.ok(host, 'the layer is appended to the document element');
    assert.equal(host!.getAttribute('data-collimate-pick'), '');
    assert.deepEqual(page.shadows.map((s) => s.opts), [{ mode: 'closed' }]);
    const [box, tip] = page.shadows[0].kids;
    assert.equal(page.cursorStyle()!.textContent, '*{cursor:crosshair !important}');
    assert.equal(tip.textContent, 'Pick one');
    assert.equal(tip.style.display, 'block');
    assert.equal(tip.style.background, '#c69d64ff');
    assert.equal(tip.style.color, '#1c1a17ff');
    second.rect = { top: 10, left: 20, width: 30, height: 40 };
    page.fire('pointermove', { target: second });
    assert.deepEqual([box.style.top, box.style.left, box.style.width, box.style.height, box.style.display],
        ['10px', '20px', '30px', '40px', 'block']);
    assert.equal(box.style.border, '2px solid #c69d64ff');
    assert.equal(box.style.background, '#c69d6429');
    // Every press and key is the picker's while it is armed.
    for (const [type, init] of [['pointerdown', {}], ['mousedown', {}], ['keyup', { key: 'a' }], ['click', { button: 2 }]] as const) {
        const event = page.fire(type, { target: second, ...init });
        assert.equal(event.defaultPrevented, true, type);
        assert.equal(event.stopped, true, type);
    }
    assert.equal(page.posts.filter((p) => p.type === 'collimate:picked').length, 0, 'a right click picks nothing');
});

test('a colour that is not #rrggbbaa falls back to the system highlight, and an empty hint to the default (annotations)', () => {
    const page = fakePage();
    start(page, { hint: '', colors: { line: 'red', fill: '', ground: 'var(--brass)', ink: '#fff' } });
    const [box, tip] = page.shadows[0].kids;
    assert.equal(box.style.border, '2px solid Highlight');
    assert.equal(box.style.background, 'transparent');
    assert.equal(tip.style.background, 'Highlight');
    assert.equal(tip.style.color, 'HighlightText');
    assert.equal(tip.textContent, 'Click an element to attach it · Esc cancels');
});

// ── 4. a click picks ──────────────────────────────────────────────────────

test('a primary click posts the element and disarms (annotations)', () => {
    const page = fakePage();
    const { second } = fixture(page);
    start(page);
    page.posts.length = 0;
    const click = page.fire('click', { target: second });
    assert.equal(click.defaultPrevented, true);
    assert.equal(click.stopped, true, 'the page’s own handlers never see the pick');
    assert.deepEqual(page.posts, [{
        type: 'collimate:picked', id: 'p1', tag: 'li', css: '#sec > ul:nth-of-type(1) > li:nth-of-type(2)',
        elementId: 'sec', snippet: 'Second item', html: '<li>Second item</li>',
    }]);
    // Disarmed: the next click is the page's, and the cursor is restored.
    const later = page.fire('click', { target: second });
    assert.equal(later.defaultPrevented, false);
    assert.equal(page.cursorStyle(), null);
    assert.equal(page.posts.length, 1);
    // The flash holds the outline for a moment, then clears it.
    const [box] = page.shadows[0].kids;
    assert.equal(box.style.display, 'block');
    page.timers.at(-1)!();
    assert.equal(box.style.display, 'none');
});

test('text and markup are clipped, and a click on the bare page picks the body (annotations)', () => {
    const page = fakePage();
    const long = page.el('p', [], { text: 'word '.repeat(60), markup: '<p>' + 'x'.repeat(700) + '</p>' });
    page.body.appendChild(long);
    start(page);
    page.fire('click', { target: long });
    const picked = page.posts.at(-1);
    assert.equal(picked.css, 'p:nth-of-type(1)');
    assert.equal(picked.elementId, '');
    assert.equal(picked.snippet.length, 120);
    assert.ok(picked.snippet.endsWith('…'));
    assert.equal(picked.html.length, 600);
    assert.ok(picked.html.endsWith('…'));
    start(page, { id: 'p2' });
    page.fire('click', { target: page.html });
    assert.equal(page.posts.at(-1).id, 'p2');
    assert.equal(page.posts.at(-1).css, 'body');
    assert.equal(page.posts.at(-1).tag, 'body');
});

// ── 5. Esc and cancel ─────────────────────────────────────────────────────

test('Esc abandons the pick, and only the parent’s cancel of this pick disarms (annotations)', () => {
    const page = fakePage();
    const { second } = fixture(page);
    start(page);
    page.posts.length = 0;
    const esc = page.fire('keydown', { key: 'Escape' });
    assert.equal(esc.defaultPrevented, true);
    assert.deepEqual(page.posts, [{ type: 'collimate:pick-abandoned', id: 'p1' }]);
    assert.equal(page.fire('click', { target: second }).defaultPrevented, false);

    start(page);
    page.send({ type: 'collimate:pick-cancel', id: 'other' });
    assert.equal(page.fire('mousedown', { target: second }).defaultPrevented, true, 'another pick’s cancel changes nothing');
    page.send({ type: 'collimate:pick-cancel', id: 'p1' }, {});
    assert.equal(page.fire('mousedown', { target: second }).defaultPrevented, true, 'a cancel from another window changes nothing');
    page.send({ type: 'collimate:pick-cancel', id: 'p1' });
    assert.equal(page.fire('mousedown', { target: second }).defaultPrevented, false);
    assert.equal(page.cursorStyle(), null);

    // A start from any window but the parent is ignored, as is one without an id.
    page.send({ type: 'collimate:pick-start', id: 'p3', colors: COLORS }, {});
    page.send({ type: 'collimate:pick-start', id: '', colors: COLORS });
    assert.equal(page.fire('mousedown', { target: second }).defaultPrevented, false);
});

// ── 6. the payload and the renderer's context ─────────────────────────────

test('a picked payload is normalized and the skin gives the colours (annotations)', () => {
    assert.deepEqual(elementPickOf({ tag: 'LI', css: 'a > b', elementId: 'x', snippet: 's', html: '<b>' }),
        { tag: 'li', css: 'a > b', id: 'x', snippet: 's', html: '<b>' });
    assert.deepEqual(elementPickOf(null), { tag: 'element', css: '', id: '', snippet: '', html: '' });
    assert.equal(elementPickOf({ tag: 'not a tag' }).tag, 'element');
    assert.equal(elementPickOf({ snippet: 7 }).snippet, '');
    const long = elementPickOf({ tag: 'p', css: 'c'.repeat(1200), snippet: 's'.repeat(200), html: 'h'.repeat(900), elementId: 'i'.repeat(300) });
    assert.deepEqual([long.css.length, long.snippet.length, long.html.length, long.id.length], [1000, 120, 600, 200]);
    assert.ok(long.html.endsWith('…'));
    assert.deepEqual(pickColors(NIGHT), COLORS);
    assert.deepEqual(pickColors(null), { line: '', fill: '', ground: '', ink: '' });
});

const PICKED: Pick = { tag: 'li', css: '#sec > ul:nth-of-type(1) > li:nth-of-type(2)', id: 'sec', snippet: 'Second item', html: '<li>Second item</li>' };

test('the context names the file and the element; its text is the element as markdown (annotations)', () => {
    assert.equal(PICK_LABEL_MAX, 80);
    assert.deepEqual(elementContext('report.html', PICKED), {
        label: 'report.html › #sec > ul:nth-of-type(1) > li:nth-of-type(2)',
        text: [
            'Element `<li>` in `report.html` (css `#sec > ul:nth-of-type(1) > li:nth-of-type(2)`, id `sec`):',
            '',
            '> Second item',
            '',
            '```html',
            '<li>Second item</li>',
            '```',
        ].join('\n'),
        anchor: { css: PICKED.css, id: 'sec', snippet: 'Second item' },
    });
    // The id clause only with an id, the quote only with a snippet, the
    // fence only with markup; the body's path is its tag.
    assert.equal(pickText('docs/a.html', { tag: 'body', css: '', id: '', snippet: '', html: '' }),
        'Element `<body>` in `docs/a.html` (css `body`):');
    assert.equal(elementContext('docs/a.html', { tag: 'body', css: '', id: '', snippet: '', html: '' }).label, 'a.html › body');
});

test('a long label keeps the path’s tail and a short name (annotations)', () => {
    const css = 'html > body:nth-of-type(1) > ' + 'div:nth-of-type(3) > '.repeat(6) + 'span:nth-of-type(1)';
    const label = pickLabel('page.html', css);
    assert.equal(label.length, PICK_LABEL_MAX);
    assert.ok(label.startsWith('page.html › …'));
    assert.ok(label.endsWith('span:nth-of-type(1)'));
    const name = 'n'.repeat(60) + '.html';
    const clipped = pickLabel(`dir/${name}`, 'p');
    assert.equal(clipped, 'n'.repeat(39) + '… › p');
    assert.ok(pickLabel(name, css).length <= PICK_LABEL_MAX);
});

test('inline code and fences grow past the backticks inside (annotations)', () => {
    assert.equal(inlineCode('a'), '`a`');
    assert.equal(inlineCode('a`b'), '``a`b``');
    assert.equal(inlineCode('`x'), '`` `x ``');
    assert.equal(inlineCode('a``b'), '```a``b```');
    assert.equal(fenceFor('<p>plain</p>'), '```');
    assert.equal(fenceFor('<pre>``</pre>'), '```');
    assert.equal(fenceFor('<pre>```js</pre>'), '````');
    assert.equal(fenceFor('<pre>`````</pre>'), '``````');
    const text = pickText('a.html', { ...PICKED, html: '<pre>```</pre>' });
    assert.ok(text.endsWith('\n\n````html\n<pre>```</pre>\n````'), text);
});
