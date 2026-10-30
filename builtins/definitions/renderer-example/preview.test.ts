// SPDX-License-Identifier: MIT
//
// preview.test.ts — the sealed preview's theme bridge, tested off-DOM.
//
// The sealed frame is opaque, so the host's :root tokens never reach it. The
// serialized bootstrap mirrors the theme record onto the frame's :root (by CSS
// name, on init and on every {op:'theme'} relay), and the frame's style
// imports the host's font stylesheet, which the CSP must admit. The bootstrap
// is run here exactly as the frame runs it: the script text frameSource emits,
// evaluated in a bare context with fake DOM objects, so a free variable in a
// serialized function fails the test.
//
// The harness bundles main.tsx with framework imports stubbed
// (scripts/bundle_renderer_test.mjs) and passes RENDERER_EXAMPLE_BUNDLE; see
// tests/test_renderer_example_theme.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.RENDERER_EXAMPLE_BUNDLE;
if (!BUNDLE) throw new Error('RENDERER_EXAMPLE_BUNDLE is not set (tests/test_renderer_example_theme.py bundles main.tsx)');
const HERE = path.resolve(process.cwd(), 'builtins/definitions/renderer-example');

let frameSource: (config: any, policy: string) => string;
let mirrorTheme: (root: any, theme: any, previous?: any) => void;
let previewPolicy: (apiOrigin: string, fontsUrl: string) => string;
before(async () => {
    ({ frameSource } = await import(pathToFileURL(BUNDLE).href));
    ({ mirrorTheme } = await import(pathToFileURL(path.join(HERE, 'bootstrap.js')).href));
    ({ previewPolicy } = await import(pathToFileURL(path.join(HERE, 'policy.js')).href));
});

const NIGHT = {
    themeId: 'night', scheme: 'dark', known: true,
    vars: { '--sheet': '#1c1a17', '--sheet-2': '#23201c', '--ink': '#e2ddd4', '--font-ui': '"Hanken Grotesk", sans-serif' },
    fonts: { ui: '"Hanken Grotesk", sans-serif', mono: '"IBM Plex Mono", monospace', prose: 'Alegreya, serif' },
};
const PAPER = {
    themeId: 'paper', scheme: 'light', known: true,
    vars: { '--sheet': '#f8f7f3', '--sheet-2': '#f1efea' },
    fonts: { ui: 'Inter, sans-serif', mono: 'monospace', prose: 'serif' },
};

/** A :root with just the CSSOM members mirrorTheme touches. */
function fakeRoot() {
    const props = new Map<string, string>();
    const attrs = new Map<string, string>();
    const target: any = {
        setProperty: (name: string, value: string) => { props.set(name, value); },
        removeProperty: (name: string) => { props.delete(name); },
    };
    const style = new Proxy(target, {
        get(t, key) {
            if (key === 'length') return props.size;
            if (typeof key === 'string' && /^\d+$/.test(key)) return [...props.keys()][Number(key)];
            return t[key];
        },
    });
    return { style, setAttribute: (name: string, value: string) => { attrs.set(name, value); }, props, attrs };
}

function directive(policy: string, name: string): string[] {
    const found = policy.split(';').map((part) => part.trim().split(/\s+/)).find(([head]) => head === name);
    return found ? found.slice(1) : [];
}

const CONFIG = {
    token: 'tok', cellId: 'cell', sample: 'a.txt', code: '', runtime: { react: '', session: '' }, imports: {},
    embed: 'iframe', hardReadOnly: false, theme: NIGHT, editing: { spellcheck: false },
    style: { theme: 'classic', font: null, textSize: 1.3 },
    fonts: [{ family: 'Lora', label: 'Lora', role: 'prose', stack: '"Lora", serif' }],
    parentOrigin: 'https://app.test',
    fontsUrl: 'https://app.test/fonts/fonts.css', urls: {},
};

/** Runs the frame's module script in a bare context; returns the root and a theme relay. */
function runFrame(source: string) {
    const script = /<script type="module">([\s\S]*)<\/script>/.exec(source)![1];
    const root = fakeRoot();
    const listeners: Record<string, Function[]> = {};
    const element = () => ({ style: {}, append() {}, attachShadow() { return element(); }, replaceChildren() {} });
    const parent = { postMessage() {} };
    const posted: any[] = [];
    const port: any = { postMessage(message: unknown) { posted.push(message); }, start() {}, onmessage: null };
    const context: any = {
        document: { documentElement: root, head: element(), body: element(), createElement: element },
        window: {
            addEventListener: (type: string, fn: Function) => { (listeners[type] ||= []).push(fn); },
            removeEventListener() {},
        },
        parent, setTimeout: () => 0, clearTimeout() {}, TextDecoder, TextEncoder, Uint8Array, atob, btoa,
        JSON, Map, Set, Promise, Object, Error, URL, encodeURIComponent, unescape, String, Number, Array,
    };
    context.globalThis = context;
    vm.runInNewContext(script, context);
    const connect = async () => {
        for (const fn of listeners.message || []) {
            fn({ source: parent, origin: CONFIG.parentOrigin, data: { tag: 'collimate-preview', op: 'connect', token: CONFIG.token }, ports: [port] });
        }
        await new Promise((resolve) => setImmediate(resolve));
    };
    const relay = async (theme: unknown) => {
        await port.onmessage({ data: { tag: 'collimate-preview', op: 'theme', theme } });
    };
    const relayEditing = async (editing: unknown) => {
        await port.onmessage({ data: { tag: 'collimate-preview', op: 'editing', editing } });
    };
    const relayStyle = async (style: unknown, fonts?: unknown) => {
        await port.onmessage({ data: { tag: 'collimate-preview', op: 'style', style, fonts } });
    };
    /** The api the bootstrap hands a mounted renderer. */
    const mountedApi = async () => {
        let api: any = null;
        await context.__collimatePreviewMount({ mount: (_el: unknown, given: unknown) => { api = given; } });
        return api;
    };
    return { root, connect, relay, relayEditing, relayStyle, mountedApi, posted };
}

test('mirrorTheme writes every token by CSS name, color-scheme and the skin id (R05)', () => {
    const root = fakeRoot();
    mirrorTheme(root, NIGHT);
    assert.equal(root.props.get('--sheet-2'), '#23201c');
    assert.equal(root.props.get('--font-ui'), '"Hanken Grotesk", sans-serif');
    assert.equal(root.props.get('color-scheme'), 'dark');
    assert.equal(root.attrs.get('data-theme'), 'night');
    assert.equal(root.attrs.get('data-scheme'), 'dark');
});

test('mirrorTheme replaces the skin and drops tokens the new record lacks (R05)', () => {
    const root = fakeRoot();
    mirrorTheme(root, NIGHT);
    mirrorTheme(root, PAPER, NIGHT);
    assert.equal(root.props.get('--sheet'), '#f8f7f3');
    assert.equal(root.props.has('--ink'), false);
    assert.equal(root.props.get('color-scheme'), 'light');
    assert.equal(root.attrs.get('data-theme'), 'paper');
});

test('a skin change keeps the custom properties the previewed renderer set on :root (R05)', () => {
    const root = fakeRoot();
    mirrorTheme(root, NIGHT);
    root.style.setProperty('--colmd-zoom', '1.25');
    mirrorTheme(root, PAPER, NIGHT);
    assert.equal(root.props.get('--colmd-zoom'), '1.25');
    assert.equal(root.props.has('--ink'), false);
    assert.equal(root.props.get('--sheet'), '#f8f7f3');
});

test('mirrorTheme ignores names that are not custom properties and non-string values (R05)', () => {
    const root = fakeRoot();
    mirrorTheme(root, { themeId: 'x', scheme: 'dark', vars: { color: 'red', '--a;b': '1', '--ok': 3 } });
    assert.deepEqual([...root.props.keys()], ['color-scheme']);
});

test('the placeholder record paints nothing but the dark scheme (R05)', () => {
    const root = fakeRoot();
    mirrorTheme(root, { themeId: 'unknown', scheme: 'dark', known: false, vars: {}, fonts: { ui: '', mono: '', prose: '' } });
    assert.deepEqual([...root.props.entries()], [['color-scheme', 'dark']]);
    assert.equal(root.attrs.get('data-theme'), 'unknown');
});

test('the serialized bootstrap mirrors the theme on init and on every relay (R05)', async () => {
    const frame = runFrame(frameSource(CONFIG, previewPolicy('https://app.test/api', CONFIG.fontsUrl)));
    assert.equal(frame.root.props.get('--sheet-2'), '#23201c');
    assert.equal(frame.root.attrs.get('data-theme'), 'night');
    frame.root.style.setProperty('--colmd-zoom', '1.25');
    await frame.connect();
    await frame.relay(PAPER);
    assert.equal(frame.root.props.get('--sheet-2'), '#f1efea');
    assert.equal(frame.root.props.has('--ink'), false);
    assert.equal(frame.root.props.get('--colmd-zoom'), '1.25');
    assert.equal(frame.root.props.get('color-scheme'), 'light');
});

test('the serialized bootstrap sets the spellcheck preference on its <html> on init and on every relay', async () => {
    const frame = runFrame(frameSource(CONFIG, previewPolicy('https://app.test/api', CONFIG.fontsUrl)));
    assert.equal(frame.root.attrs.get('spellcheck'), 'false');
    await frame.connect();
    await frame.relayEditing({ spellcheck: true });
    assert.equal(frame.root.attrs.get('spellcheck'), 'true');
    await frame.relayEditing({ spellcheck: false });
    assert.equal(frame.root.attrs.get('spellcheck'), 'false');
});

test('the frame document imports the fonts first and grounds on the tokens (R05)', () => {
    const source = frameSource(CONFIG, previewPolicy('https://app.test/api', CONFIG.fontsUrl));
    const style = /<style>([\s\S]*?)<\/style>/.exec(source)![1];
    assert.ok(style.startsWith('@import url("https://app.test/fonts/fonts.css");'), style);
    assert.match(style, /background:var\(--sheet\)/);
    assert.match(style, /color:var\(--ink\)/);
    assert.match(style, /font-family:var\(--font-ui\)/);
    assert.doesNotMatch(source.replace(/<script[\s\S]*<\/script>/, ''), /#[0-9a-f]{3,8}\b|rgba?\(|\bwhite\b/i);
});

test('the CSP admits the font stylesheet and its faces from the app origin only (R05)', () => {
    const same = previewPolicy('https://app.test/api', 'https://app.test/fonts/fonts.css');
    assert.deepEqual(directive(same, 'style-src'), ["'unsafe-inline'", 'https://app.test']);
    assert.deepEqual(directive(same, 'font-src'), ['https://app.test', 'data:']);
    assert.deepEqual(directive(same, 'connect-src'), ["'none'"]);
    const split = previewPolicy('http://127.0.0.1:5001/api', 'http://localhost:5173/fonts/fonts.css');
    assert.deepEqual(directive(split, 'font-src'), ['http://127.0.0.1:5001', 'http://localhost:5173', 'data:']);
    assert.deepEqual(directive(split, 'style-src'), ["'unsafe-inline'", 'http://localhost:5173']);
    assert.deepEqual(directive(split, 'img-src'), ['http://127.0.0.1:5001', 'data:']);
    assert.throws(() => previewPolicy('https://app.test', 'data:text/css,x'));
});

test('the sealed preview relays the host’s style and font catalogue; the previewed renderer’s setStyle stays in the frame (RS-02)', async () => {
    const frame = runFrame(frameSource(CONFIG, previewPolicy('https://app.test/api', CONFIG.fontsUrl)));
    await frame.connect();
    const api = await frame.mountedApi();
    // The frame's objects are another realm's: compare their JSON.
    const plain = (value: unknown) => JSON.parse(JSON.stringify(value));
    assert.ok(api, 'the renderer was mounted with the preview api');
    // The config's record and catalogue, as the host had them.
    assert.deepEqual(plain(api.getStyle()), { theme: 'classic', font: null, textSize: 1.3 });
    assert.deepEqual(plain(api.getFontFamilies().map((f: any) => f.family)), ['Lora']);
    const seen: any[] = [];
    const off = api.onStyleChange((style: unknown) => seen.push(style));
    assert.deepEqual(plain(seen), [{ theme: 'classic', font: null, textSize: 1.3 }], 'fires on subscribe');
    // A relay replaces the record (a malformed field reads as the default)
    // and the catalogue.
    await frame.relayStyle({ theme: null, font: 'Inter', textSize: 'big' },
        [{ family: 'Inter', label: 'Inter', role: 'ui', stack: '"Inter", sans-serif' }]);
    assert.deepEqual(plain(api.getStyle()), { theme: null, font: 'Inter', textSize: null });
    assert.deepEqual(plain(api.getFontFamilies().map((f: any) => f.family)), ['Inter']);
    // The renderer's own setStyle applies here and is never posted to the host.
    const before = frame.posted.length;
    api.setStyle({ textSize: 1.5 });
    assert.deepEqual(plain(api.getStyle()), { theme: null, font: 'Inter', textSize: 1.5 });
    assert.deepEqual(plain(seen.at(-1)), { theme: null, font: 'Inter', textSize: 1.5 });
    assert.deepEqual(plain(frame.posted.slice(before)), []);
    off();
    api.setStyle({ theme: 'nord' });
    assert.equal(seen.length, 3);
});

test('the preview forwards the host’s style with its catalogue and never a write of its own (RS-02)', async () => {
    const fs = await import('node:fs');
    const main = fs.readFileSync(path.join(HERE, 'main.tsx'), 'utf8');
    assert.match(main, /const offStyle=api\.onStyleChange\(style=>post\(\{op:'style',style,fonts:api\.getFontFamilies\(\)\}\)\);/);
    assert.match(main, /style:api\.getStyle\(\),fonts:api\.getFontFamilies\(\)/);
    assert.match(main, /offStyle\(\);/);
    assert.doesNotMatch(main, /setStyle/);
    const bootstrap = fs.readFileSync(path.join(HERE, 'bootstrap.js'), 'utf8');
    const setStyle = /setStyle:patch=>\{[^\n]*\},/.exec(bootstrap);
    assert.ok(setStyle, 'the frame api has setStyle');
    assert.doesNotMatch(setStyle![0], /post\(|request\(|write\(/);
});
