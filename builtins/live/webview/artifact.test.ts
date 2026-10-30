// SPDX-License-Identifier: MIT
//
// artifact.test.ts — the webview renderer's leg of the `tab-<n>.webview` v4
// contract: the artifact parser (the one flat shape + the `requires` gate), the
// tab's session key and hello frame, and the RPC answer mapping, which is the
// half of the wire protocol in _web_shared/protocol.py that this renderer owns.
//
// Runs under node:test via the tsx loader (see tests/test_webview_artifact.py).
// The pure functions live in main.tsx; this harness bundles them because it
// pulls React from a CDN and the SDK from /api/…. The harness bundles main.tsx
// with those imports stubbed (scripts/bundle_renderer_test.mjs) and passes the
// bundle path via WEBVIEW_BUNDLE; we import that bundle and assert its EXPORTED
// parseArtifact / unmetRequirements / guestSessionKeyOf / helloFrame / answerRpc /
// guestFromHandle / bytesToBase64 / displayUrl / isBlankPage / withScheme /
// frameableUrl / fallbackPane / startingLabelOf / emitDevice / letterboxStyle.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const BUNDLE = process.env.WEBVIEW_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'WEBVIEW_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_webview_artifact.py does this).',
    );
}

// Populated from the bundled renderer in a `before` hook — the tsx loader
// transpiles this file to CommonJS, where top-level await is unsupported.
let parseArtifact: (raw: string) => any;
let unmetRequirements: (requires: string[], caps: any) => string[];
let answerRpc: (msg: any, guest: any) => Promise<any>;
let guestFromHandle: (handle: any) => any;
let bytesToBase64: (buf: ArrayBuffer | Uint8Array) => string;
let displayUrl: (u: string) => string;
let isBlankPage: (u: string) => boolean;
let withScheme: (target: string) => string;
let frameableUrl: (src: string, appOrigins: string[]) => boolean;
let fallbackPane: (frameSrc: string, appOrigins: string[]) => 'enter' | 'frame' | 'notice';
let guestSessionKeyOf: (a: any) => string;
let helloFrame: (tab: string, client: string, caps: any) => any;
let HTML_EXPRESSION: string;
let BLANK_PAGE: string;
let startingLabelOf: (raw: string) => string | null;
let emitDevice: (api: any, device: string) => Promise<boolean>;
let letterboxStyle: (width: number | null) => any;
let pageBoxStyleFor: (width: number | null) => any;
let DEVICES: readonly string[];
let DEVICE_LABELS: Record<string, string>;

before(async () => {
    const mod: any = await import(pathToFileURL(path.resolve(BUNDLE)).href);
    ({ parseArtifact, unmetRequirements, answerRpc, guestFromHandle, bytesToBase64,
       displayUrl, isBlankPage, withScheme, frameableUrl, fallbackPane, HTML_EXPRESSION, BLANK_PAGE,
       guestSessionKeyOf, helloFrame, startingLabelOf, emitDevice, letterboxStyle,
       pageBoxStyleFor, DEVICES, DEVICE_LABELS } = mod);
});

// The one artifact _tab_artifact() builds, verbatim in shape.
const NATIVE_ARTIFACT = {
    v: 4,
    tab: 'tab-1',
    url: 'https://example.com/',
    device: 'desktop',
    viewport: null,
    control: { wsBase: '/proxy/ch/run/9100/' },
    requires: ['nativeGuests'],
    native: { partition: 'persist:collimate-default', device: 'desktop' },
    buttons: [{ id: 'capture', label: 'Capture' }, { id: 'new_tab', label: 'New tab' }],
};

// ---------------------------------------------------------------------------
// parseArtifact — the session artifact
// ---------------------------------------------------------------------------

test('the artifact: control base, requires, and the flattened native block', () => {
    const a = parseArtifact(JSON.stringify(NATIVE_ARTIFACT));
    assert.strictEqual(a.v, 4);
    assert.strictEqual(a.tab, 'tab-1');
    assert.strictEqual(a.url, 'https://example.com/');
    assert.strictEqual(a.wsBase, '/proxy/ch/run/9100/');
    assert.deepStrictEqual(a.requires, ['nativeGuests']);
    // The partition arrives already prefixed and is carried through verbatim —
    // the host's partitionFor() is idempotent, so re-prefixing is not our job.
    assert.strictEqual(a.partition, 'persist:collimate-default');
    assert.strictEqual(a.device, 'desktop');
    assert.strictEqual(a.viewportWidth, null);
    assert.deepStrictEqual(a.buttons, NATIVE_ARTIFACT.buttons);
});

test('a phone or tablet tab carries its device and its viewport width (GN-04)', () => {
    const phone = parseArtifact(JSON.stringify({
        ...NATIVE_ARTIFACT, device: 'phone', viewport: { width: 412 },
        native: { partition: 'persist:collimate-work', device: 'phone' },
    }));
    assert.strictEqual(phone.device, 'phone');
    assert.strictEqual(phone.viewportWidth, 412);
    assert.strictEqual(phone.partition, 'persist:collimate-work');
    const tablet = parseArtifact(JSON.stringify({
        ...NATIVE_ARTIFACT, device: 'tablet', viewport: { width: 800 },
    }));
    assert.strictEqual(tablet.device, 'tablet');
    assert.strictEqual(tablet.viewportWidth, 800);
    // The shell composes the user agent: a stray one on the wire is not read.
    const stray = parseArtifact(JSON.stringify({
        ...NATIVE_ARTIFACT,
        native: { partition: 'persist:collimate-default', userAgent: 'Mozilla/5.0 (Test)' },
    }));
    assert.ok(!('userAgent' in stray), 'userAgent is gone from the parsed artifact');
});

test('an unknown device is desktop; a bad viewport is the full width (GN-04)', () => {
    for (const doc of [
        { ...NATIVE_ARTIFACT, device: 'watch', viewport: { width: 200 } },
        { ...NATIVE_ARTIFACT, device: 7 },
    ]) {
        assert.strictEqual(parseArtifact(JSON.stringify(doc)).device, 'desktop');
    }
    for (const viewport of [null, {}, { width: '412' }, { width: 0 }, { width: -5 }, 412]) {
        const a = parseArtifact(JSON.stringify({ ...NATIVE_ARTIFACT, device: 'phone', viewport }));
        assert.strictEqual(a.viewportWidth, null, `viewport ${JSON.stringify(viewport)}`);
    }
});

test('the letterbox centres the guest at the viewport width on the canvas (GN-04)', () => {
    // Desktop: the host fills the page box, on the sheet.
    assert.deepStrictEqual(letterboxStyle(null), { position: 'absolute', inset: 0 });
    assert.strictEqual(pageBoxStyleFor(null).background, 'var(--sheet)');
    // A device: a column that wide, centred (auto margins between 0 insets),
    // never wider than the box, with the canvas around it.
    const phone = letterboxStyle(412);
    assert.strictEqual(phone.width, 412);
    assert.strictEqual(phone.maxWidth, '100%');
    assert.strictEqual(phone.margin, '0 auto');
    assert.strictEqual(phone.left, 0);
    assert.strictEqual(phone.right, 0);
    assert.strictEqual(phone.top, 0);
    assert.strictEqual(phone.bottom, 0);
    assert.strictEqual(phone.position, 'absolute');
    assert.strictEqual(letterboxStyle(800).width, 800);
    assert.strictEqual(pageBoxStyleFor(412).background, 'var(--canvas)');
});

test('the device toggle fires the device action with its payload (GN-04)', async () => {
    assert.deepStrictEqual([...DEVICES], ['desktop', 'phone', 'tablet']);
    assert.deepStrictEqual(DEVICE_LABELS, { desktop: 'Desktop', phone: 'Phone', tablet: 'Tablet' });
    const calls: any[] = [];
    const api = { emitAction: async (id: string, payload: any) => { calls.push([id, payload]); return true; } };
    assert.strictEqual(await emitDevice(api, 'phone'), true);
    assert.strictEqual(await emitDevice(api, 'tablet'), true);
    // An unknown device and an api with no action channel send nothing.
    assert.strictEqual(await emitDevice(api, 'watch'), false);
    assert.strictEqual(await emitDevice({}, 'phone'), false);
    assert.deepStrictEqual(calls, [['device', { device: 'phone' }], ['device', { device: 'tablet' }]]);
});

test('the starting placeholder is recognised, a real tab is not (GN-03)', () => {
    assert.strictEqual(startingLabelOf(JSON.stringify({ starting: true, label: 'Web' })), 'Web');
    assert.strictEqual(startingLabelOf(JSON.stringify({ starting: true })), '');
    for (const raw of [JSON.stringify(NATIVE_ARTIFACT), JSON.stringify({ starting: 'yes' }), '', '{', 'null', '[]']) {
        assert.strictEqual(startingLabelOf(raw), null, raw);
    }
    // The placeholder is no session: the parse is the empty artifact.
    assert.strictEqual(parseArtifact(JSON.stringify({ starting: true, label: 'Web' })).wsBase, '');
});

test("unknown keys don't leak into the parsed artifact", () => {
    // The parser builds its own object field by field rather than spreading
    // the document, so the retired mode machinery — and anything a newer
    // generator adds — can only reach this renderer by being read for.
    const a = parseArtifact(JSON.stringify({
        ...NATIVE_ARTIFACT,
        mode: 'image',
        image: { viewport: { width: 300, height: 200 } },
        live: { token: 'stale' },
        controls: { addressBar: false, window: true },
    }));
    for (const key of ['mode', 'image', 'live', 'controls']) {
        assert.ok(!(key in a), `${key} should not survive parsing`);
    }
    // …and the fields it does declare come through untouched by the noise.
    assert.strictEqual(a.wsBase, '/proxy/ch/run/9100/');
    assert.strictEqual(a.partition, 'persist:collimate-default');
    assert.deepStrictEqual(a.buttons, NATIVE_ARTIFACT.buttons);
});

// ---------------------------------------------------------------------------
// parseArtifact — malformed input
// ---------------------------------------------------------------------------

test('an artifact whose v is not 4 parses to the empty artifact', () => {
    // The generator and this renderer ship together, so a `v` that isn't 4
    // means the two halves are out of sync — the waiting panel is the honest
    // render, not a half-understood artifact.
    const noVersion: any = { ...NATIVE_ARTIFACT };
    delete noVersion.v;
    for (const doc of [
        { ...NATIVE_ARTIFACT, v: 1 },
        { ...NATIVE_ARTIFACT, v: 2 },
        { ...NATIVE_ARTIFACT, v: 3 },
        { ...NATIVE_ARTIFACT, v: 5 },
        { ...NATIVE_ARTIFACT, v: '4' },   // the check is strict, not loose
        noVersion,
    ]) {
        const label = JSON.stringify(doc.v ?? null);
        const a = parseArtifact(JSON.stringify(doc));
        assert.strictEqual(a.v, 0, `expected the empty artifact for v=${label}`);
        assert.strictEqual(a.wsBase, '', `expected no wsBase for v=${label}`);
        assert.strictEqual(a.tab, '');
        assert.strictEqual(a.url, '');
        assert.strictEqual(a.partition, '');
        assert.deepStrictEqual(a.requires, []);
        assert.deepStrictEqual(a.buttons, []);
    }
});

test('malformed artifacts parse to the empty artifact (no control base)', () => {
    for (const raw of ['', '   ', '{', 'not json', 'null', '[]', '"a string"', '42']) {
        const a = parseArtifact(raw);
        assert.strictEqual(a.wsBase, '', `expected no wsBase for ${JSON.stringify(raw)}`);
        assert.strictEqual(a.partition, '');
        assert.deepStrictEqual(a.requires, []);
        assert.deepStrictEqual(a.buttons, []);
    }
});

test('a tab file without its tab or its control base is no session', () => {
    // A tab is addressed by (wsBase, tab): the hello names the tab on the
    // run's one control socket, so a file missing either has nothing to
    // attach to — the waiting panel, not a socket saying hello for nobody.
    const noTab: any = { ...NATIVE_ARTIFACT };
    delete noTab.tab;
    for (const doc of [
        noTab,
        { ...NATIVE_ARTIFACT, tab: '   ' },
        { ...NATIVE_ARTIFACT, tab: 7 },
        { ...NATIVE_ARTIFACT, control: {} },
        { ...NATIVE_ARTIFACT, control: null },
    ]) {
        const a = parseArtifact(JSON.stringify(doc));
        assert.strictEqual(a.v, 0);
        assert.strictEqual(a.wsBase, '');
        assert.strictEqual(a.tab, '');
    }
});

test('absent optional blocks: no requires, no buttons, no partition', () => {
    const a = parseArtifact(JSON.stringify({
        v: 4, tab: 'tab-2', url: 'https://x/', control: { wsBase: '/p/' },
    }));
    assert.strictEqual(a.wsBase, '/p/');
    assert.deepStrictEqual(a.buttons, []);
    assert.deepStrictEqual(a.requires, []);
    assert.strictEqual(a.partition, '');
    assert.strictEqual(a.device, 'desktop');
    assert.strictEqual(a.viewportWidth, null);
    // Nothing required gates nothing, in any client.
    assert.deepStrictEqual(unmetRequirements(a.requires, undefined), []);
});

test('junk entries in buttons/requires are dropped, not rendered', () => {
    const a = parseArtifact(JSON.stringify({
        ...NATIVE_ARTIFACT,
        buttons: [{ id: 'capture', label: 'Capture' }, { id: 'x' }, null, 'nope', { label: 'no id' }],
        requires: ['nativeGuests', 7, null, ''],
    }));
    assert.deepStrictEqual(a.buttons, [{ id: 'capture', label: 'Capture' }]);
    assert.deepStrictEqual(a.requires, ['nativeGuests']);
});

// ---------------------------------------------------------------------------
// A session that named no site — the empty url, and the blank page it opens on
// ---------------------------------------------------------------------------

test('an empty url is a whole artifact, not a rejected one', () => {
    // The site is optional. Only `control.wsBase` and `tab` decide whether a
    // session exists, so an empty url has to come through AS an empty string with
    // every other field intact — anything else would put the "waiting for the
    // generator" panel over a session that is perfectly live.
    const a = parseArtifact(JSON.stringify({ ...NATIVE_ARTIFACT, url: '' }));
    assert.strictEqual(a.url, '');
    assert.strictEqual(a.v, 4);
    assert.strictEqual(a.wsBase, '/proxy/ch/run/9100/');
    assert.strictEqual(a.partition, 'persist:collimate-default');
    assert.deepStrictEqual(a.requires, ['nativeGuests']);
    assert.deepStrictEqual(a.buttons, NATIVE_ARTIFACT.buttons);
});

test('an absent, blank or non-string url reads as no site, not as a bad artifact', () => {
    const noUrl: any = { ...NATIVE_ARTIFACT };
    delete noUrl.url;
    for (const doc of [
        noUrl,
        { ...NATIVE_ARTIFACT, url: '   ' },
        { ...NATIVE_ARTIFACT, url: null },
        { ...NATIVE_ARTIFACT, url: 42 },
    ]) {
        const label = JSON.stringify((doc as any).url ?? null);
        const a = parseArtifact(JSON.stringify(doc));
        assert.strictEqual(a.url, '', `expected no site for url=${label}`);
        assert.strictEqual(a.wsBase, '/proxy/ch/run/9100/',
                           `the artifact still stands for url=${label}`);
    }
});

test('the blank page is spelled to the guest but never shown in the address bar', () => {
    // `about:blank` is how "started without a site" reaches the guest; the
    // address bar has to stay empty over it so its placeholder shows and the
    // user has nothing to clear before typing.
    assert.strictEqual(BLANK_PAGE, 'about:blank');
    assert.strictEqual(displayUrl(BLANK_PAGE), '');
    assert.strictEqual(displayUrl(''), '');
    // Every real address is shown verbatim — including one that merely looks
    // adjacent to the blank page.
    assert.strictEqual(displayUrl('https://example.com/'), 'https://example.com/');
    assert.strictEqual(displayUrl('about:blank#x'), 'about:blank#x');
    assert.strictEqual(displayUrl('about:config'), 'about:config');
    // The canvas holds the Enter page form exactly while the address bar
    // would be empty.
    assert.strictEqual(isBlankPage(BLANK_PAGE), true);
    assert.strictEqual(isBlankPage(''), true);
    assert.strictEqual(isBlankPage('https://example.com/'), false);
    assert.strictEqual(isBlankPage('about:blank#x'), false);
});

test('a hand-typed address gets https unless it already names a scheme://', () => {
    // A bare host:port is not a scheme — `localhost:` must not read as one.
    assert.strictEqual(withScheme('localhost:3000'), 'https://localhost:3000');
    assert.strictEqual(withScheme('example.com:8443/x'), 'https://example.com:8443/x');
    assert.strictEqual(withScheme('example.com'), 'https://example.com');
    assert.strictEqual(withScheme('https://a.b/'), 'https://a.b/');
    assert.strictEqual(withScheme('http://localhost:3000'), 'http://localhost:3000');
    assert.strictEqual(withScheme('about:blank'), 'about:blank');
});

// ---------------------------------------------------------------------------
// frameableUrl / fallbackPane — what URL the browser fallback's sandboxed
// frame may be given (only that URL: a framed site can still navigate its own
// frame elsewhere, which no renderer check sees)
// ---------------------------------------------------------------------------

test('the fallback frames another origin\'s http(s) site (F1738)', () => {
    const app = ['http://localhost:8000'];
    assert.strictEqual(frameableUrl('https://example.com/', app), true);
    assert.strictEqual(frameableUrl('http://example.com:8000/x', app), true);
    // A local dev server on another port is another origin.
    assert.strictEqual(frameableUrl('http://localhost:3000/', app), true);
    assert.strictEqual(frameableUrl('http://127.0.0.1:5173/', app), true);
});

test('the fallback never frames the app\'s own origin (F1738)', () => {
    // The frame keeps allow-same-origin, so an app-origin document could lift
    // its sandbox and script the app: the app server, the /proxy path and a
    // relative path all stay out, under every loopback name for its port.
    const app = ['http://localhost:8000'];
    assert.strictEqual(frameableUrl('http://localhost:8000/', app), false);
    assert.strictEqual(frameableUrl('http://localhost:8000/proxy/ch/run/9100/', app), false);
    assert.strictEqual(frameableUrl('http://127.0.0.1:8000/', app), false);
    assert.strictEqual(frameableUrl('http://[::1]:8000/', app), false);
    assert.strictEqual(frameableUrl('/proxy/ch/run/9100/', app), false);
    assert.strictEqual(frameableUrl('example.com', app), false);
    // A split-origin setup: the API origin is the app's too.
    const split = ['http://localhost:5173', 'http://localhost:8000'];
    assert.strictEqual(frameableUrl('http://127.0.0.1:8000/proxy/x/', split), false);
    assert.strictEqual(frameableUrl('http://localhost:5173/', split), false);
    // Default ports compare as ports.
    assert.strictEqual(frameableUrl('https://app.example/', ['https://app.example:443']), false);
});

test('the fallback frames no script-bearing or origin-inheriting scheme (F1738)', () => {
    const app = ['http://localhost:8000'];
    for (const src of ['javascript:alert(1)', 'about:blank', 'about:srcdoc', 'data:text/html,<b>x</b>',
                       'blob:http://localhost:8000/x', 'file:///etc/passwd', '']) {
        assert.strictEqual(frameableUrl(src, app), false, src);
    }
});

test('the fallback pane frames only a URL frameableUrl allows (F1738)', () => {
    // FallbackSessionView renders its <iframe> only for 'frame'
    // (tests/test_renderer_frame_sandbox.py pins that branch).
    const app = ['http://localhost:8000'];
    assert.strictEqual(fallbackPane('https://example.com/', app), 'frame');
    assert.strictEqual(fallbackPane('http://localhost:3000/', app), 'frame');
    // The app's own origin, the /proxy path, a relative path and a script URL
    // get the notice instead of a frame.
    for (const src of ['http://localhost:8000/', 'http://127.0.0.1:8000/proxy/ch/run/9100/',
                       '/proxy/ch/run/9100/', 'javascript:alert(1)', 'about:blank']) {
        assert.strictEqual(fallbackPane(src, app), 'notice', src);
    }
    // No site named yet: the Enter page form, never src="" (the app itself).
    assert.strictEqual(fallbackPane('', app), 'enter');
});

// ---------------------------------------------------------------------------
// The tab's identity — session key and hello
// ---------------------------------------------------------------------------

test('each tab of each run is its own guest session (F0287)', () => {
    // The run's control base (it carries the run id) plus the tab: a second
    // tab of the same run, or the same tab number of another run, never
    // reattaches someone else's parked guest.
    const one = parseArtifact(JSON.stringify(NATIVE_ARTIFACT));
    const two = parseArtifact(JSON.stringify({ ...NATIVE_ARTIFACT, tab: 'tab-2' }));
    const rerun = parseArtifact(JSON.stringify({
        ...NATIVE_ARTIFACT, control: { wsBase: '/proxy/ch/run2/9100/' },
    }));
    assert.strictEqual(guestSessionKeyOf(one), '/proxy/ch/run/9100/#tab-1');
    assert.strictEqual(guestSessionKeyOf(two), '/proxy/ch/run/9100/#tab-2');
    assert.strictEqual(guestSessionKeyOf(rerun), '/proxy/ch/run2/9100/#tab-1');
    // …and the same tab remounted is the same session.
    assert.strictEqual(guestSessionKeyOf(parseArtifact(JSON.stringify(NATIVE_ARTIFACT))), guestSessionKeyOf(one));
});

test('the hello names the tab the socket serves', () => {
    // The run has one control socket endpoint for all its tabs; the hello is
    // how the daemon learns which tab's guest answers on this socket. Caps
    // ride verbatim (camelCase) — the daemon snake_cases them.
    assert.deepStrictEqual(
        helloFrame('tab-2', 'electron', { nativeGuests: true, guestCapture: true }),
        { kind: 'hello', tab: 'tab-2', client: 'electron',
          caps: { nativeGuests: true, guestCapture: true } });
    // A browser tab has no capability map at all.
    assert.deepStrictEqual(helloFrame('tab-1', 'browser', undefined),
        { kind: 'hello', tab: 'tab-1', client: 'browser', caps: {} });
});

// ---------------------------------------------------------------------------
// The `requires` gate
// ---------------------------------------------------------------------------

test('requires gate: a native artifact in a browser reports the missing cap', () => {
    const a = parseArtifact(JSON.stringify(NATIVE_ARTIFACT));
    // A browser tab has no api.desktop at all.
    assert.deepStrictEqual(unmetRequirements(a.requires, undefined), ['nativeGuests']);
    assert.deepStrictEqual(unmetRequirements(a.requires, null), ['nativeGuests']);
    // A map that carries the key as false is just as much a no as an absent
    // one — which is why the gate tests truthiness, not presence.
    assert.deepStrictEqual(
        unmetRequirements(a.requires, { nativeGuests: false, externalOpen: true }),
        ['nativeGuests'],
    );
});

test('requires gate: satisfied in a shell that can host guests', () => {
    const a = parseArtifact(JSON.stringify(NATIVE_ARTIFACT));
    assert.deepStrictEqual(
        unmetRequirements(a.requires, { nativeGuests: true, guestCapture: true }),
        [],
    );
});

// ---------------------------------------------------------------------------
// The RPC answer mapping — a pure function over an injected guest
// ---------------------------------------------------------------------------

/** A guest that records what it was asked and answers canned values. */
function fakeGuest(overrides: Record<string, any> = {}) {
    const calls: Array<[string, any]> = [];
    const guest: any = {
        calls,
        evaluate: async (js: string) => { calls.push(['evaluate', js]); return 'evaluated'; },
        html: async () => { calls.push(['html', null]); return '<html></html>'; },
        screenshot: async () => { calls.push(['screenshot', null]); return 'UE5H'; },
        url: async () => { calls.push(['url', null]); return 'https://example.com/now'; },
        navigate: async (u: string) => { calls.push(['navigate', u]); },
    };
    return Object.assign(guest, overrides);
}

test('evaluate: params.js is handed to the guest verbatim', async () => {
    const guest = fakeGuest();
    const js = '(() => document.title)()';
    const reply = await answerRpc({ kind: 'rpc', id: 'rpc-1', method: 'evaluate', params: { js } }, guest);
    assert.deepStrictEqual(reply, { kind: 'rpc_result', id: 'rpc-1', value: 'evaluated' });
    assert.deepStrictEqual(guest.calls, [['evaluate', js]]);
});

test('html / screenshot / url map onto their guest verbs', async () => {
    const guest = fakeGuest();
    assert.deepStrictEqual(
        await answerRpc({ kind: 'rpc', id: 'a', method: 'html', params: {} }, guest),
        { kind: 'rpc_result', id: 'a', value: '<html></html>' });
    assert.deepStrictEqual(
        await answerRpc({ kind: 'rpc', id: 'b', method: 'screenshot' }, guest),
        { kind: 'rpc_result', id: 'b', value: 'UE5H' });
    assert.deepStrictEqual(
        await answerRpc({ kind: 'rpc', id: 'c', method: 'url' }, guest),
        { kind: 'rpc_result', id: 'c', value: 'https://example.com/now' });
    assert.deepStrictEqual(
        guest.calls.map((c: any[]) => c[0]), ['html', 'screenshot', 'url']);
});

test('navigate answers value null once the guest accepted it', async () => {
    const guest = fakeGuest();
    const reply = await answerRpc(
        { kind: 'rpc', id: 'n1', method: 'navigate', params: { url: 'https://elsewhere/' } }, guest);
    assert.deepStrictEqual(reply, { kind: 'rpc_result', id: 'n1', value: null });
    assert.deepStrictEqual(guest.calls, [['navigate', 'https://elsewhere/']]);
});

test('an unknown method is an rpc_error, not a hang', async () => {
    const reply = await answerRpc({ kind: 'rpc', id: 'z', method: 'sudo' }, fakeGuest());
    assert.strictEqual(reply.kind, 'rpc_error');
    assert.strictEqual(reply.id, 'z');
    assert.match(reply.message, /unknown rpc method 'sudo'/);
});

test('a guest that refuses becomes an rpc_error carrying its message', async () => {
    const guest = fakeGuest({
        screenshot: async () => { throw new Error('capture is off'); },
    });
    const reply = await answerRpc({ kind: 'rpc', id: 'q', method: 'screenshot' }, guest);
    assert.deepStrictEqual(reply, { kind: 'rpc_error', id: 'q', message: 'capture is off' });
});

test('non-rpc messages (url, hello, junk) get no reply at all', async () => {
    const guest = fakeGuest();
    for (const msg of [
        { kind: 'url', url: 'https://x/' },
        { kind: 'hello', tab: 'tab-1' },
        {}, null, undefined, 'a string', 42,
    ]) {
        assert.strictEqual(await answerRpc(msg, guest), null, `expected no reply for ${JSON.stringify(msg)}`);
    }
    assert.deepStrictEqual(guest.calls, []);
});

test('a missing id still answers (as the empty id) rather than dropping the call', async () => {
    const reply = await answerRpc({ kind: 'rpc', method: 'url' }, fakeGuest());
    assert.deepStrictEqual(reply, { kind: 'rpc_result', id: '', value: 'https://example.com/now' });
});

// ---------------------------------------------------------------------------
// guestFromHandle — the mapping onto a mounted guest view
// ---------------------------------------------------------------------------

/** The slice of GuestHandle the RPC uses, recording its calls. */
function fakeHandle(overrides: Record<string, any> = {}) {
    const calls: Array<[string, any]> = [];
    return Object.assign({
        calls,
        evaluate: async (js: string) => {
            calls.push(['evaluate', js]);
            return js === HTML_EXPRESSION ? '<html><body>hi</body></html>' : 7;
        },
        capturePng: async () => {
            calls.push(['capturePng', null]);
            return Uint8Array.from([104, 101, 108, 108, 111]).buffer;  // "hello"
        },
        currentUrl: async () => { calls.push(['currentUrl', null]); return 'https://here/'; },
        loadURL: async (u: string) => { calls.push(['loadURL', u]); },
    }, overrides);
}

test('handle: evaluate passes the expression through untouched', async () => {
    const handle = fakeHandle();
    const reply = await answerRpc(
        { kind: 'rpc', id: '1', method: 'evaluate', params: { js: '(() => 1 + 1)()' } },
        guestFromHandle(handle));
    assert.deepStrictEqual(reply, { kind: 'rpc_result', id: '1', value: 7 });
    assert.deepStrictEqual(handle.calls, [['evaluate', '(() => 1 + 1)()']]);
});

test('handle: html is one evaluate of the serialised document', async () => {
    const handle = fakeHandle();
    const reply = await answerRpc({ kind: 'rpc', id: '2', method: 'html' }, guestFromHandle(handle));
    assert.deepStrictEqual(reply,
        { kind: 'rpc_result', id: '2', value: '<html><body>hi</body></html>' });
    assert.deepStrictEqual(handle.calls, [['evaluate', HTML_EXPRESSION]]);
    // The daemon's `_evaluate_expression` shape: an invoked zero-arg function.
    assert.strictEqual(HTML_EXPRESSION, '(() => document.documentElement.outerHTML)()');
});

test('handle: screenshot answers base64 with no data: prefix', async () => {
    const handle = fakeHandle();
    const reply = await answerRpc({ kind: 'rpc', id: '3', method: 'screenshot' }, guestFromHandle(handle));
    assert.deepStrictEqual(reply, { kind: 'rpc_result', id: '3', value: 'aGVsbG8=' });
    assert.deepStrictEqual(handle.calls, [['capturePng', null]]);
});

test('handle: url and navigate', async () => {
    const handle = fakeHandle();
    assert.deepStrictEqual(
        await answerRpc({ kind: 'rpc', id: '4', method: 'url' }, guestFromHandle(handle)),
        { kind: 'rpc_result', id: '4', value: 'https://here/' });
    assert.deepStrictEqual(
        await answerRpc({ kind: 'rpc', id: '5', method: 'navigate', params: { url: 'https://go/' } },
                        guestFromHandle(handle)),
        { kind: 'rpc_result', id: '5', value: null });
    assert.deepStrictEqual(handle.calls, [['currentUrl', null], ['loadURL', 'https://go/']]);
});

test('no mounted guest refuses every method instead of hanging', async () => {
    for (const method of ['evaluate', 'html', 'screenshot', 'url', 'navigate']) {
        const reply = await answerRpc({ kind: 'rpc', id: method, method, params: {} },
                                      guestFromHandle(null));
        assert.strictEqual(reply.kind, 'rpc_error', `${method} should refuse`);
        assert.match(reply.message, /no guest is mounted/);
    }
});

test('a handle whose call rejects is reported as an rpc_error', async () => {
    const handle = fakeHandle({
        capturePng: async () => { throw new Error('collimate-guest/not-attached'); },
    });
    const reply = await answerRpc({ kind: 'rpc', id: 'e', method: 'screenshot' },
                                  guestFromHandle(handle));
    assert.deepStrictEqual(reply,
        { kind: 'rpc_error', id: 'e', message: 'collimate-guest/not-attached' });
});

// ---------------------------------------------------------------------------
// bytesToBase64 — the PNG's ride over a JSON wire
// ---------------------------------------------------------------------------

test('base64: known vectors, from both an ArrayBuffer and a Uint8Array', () => {
    const bytes = Uint8Array.from([104, 101, 108, 108, 111]);  // "hello"
    assert.strictEqual(bytesToBase64(bytes), 'aGVsbG8=');
    assert.strictEqual(bytesToBase64(bytes.buffer), 'aGVsbG8=');
    assert.strictEqual(bytesToBase64(new Uint8Array(0)), '');
    // The PNG magic number, which is what a real screenshot starts with.
    assert.strictEqual(
        bytesToBase64(Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])),
        'iVBORw0KGgo=');
});

test('base64: a payload past the chunk boundary encodes identically', () => {
    // 0x8000 is the chunk size — go well past it, with every byte value, so a
    // chunking bug shows up as a mismatch rather than as a rare truncation.
    const n = 0x8000 * 2 + 137;
    const bytes = new Uint8Array(n);
    for (let i = 0; i < n; i++) bytes[i] = i % 256;
    assert.strictEqual(bytesToBase64(bytes), Buffer.from(bytes).toString('base64'));
});

test('PNG encoding uses available native base64 on the exact byte view __F1777', () => {
    const bytes=Uint8Array.from([1,2,3,4]).subarray(1,3);
    let calls=0;
    Object.defineProperty(bytes,'toBase64',{value:function(){calls++;assert.equal(this,bytes);return Buffer.from(this).toString('base64');}});
    assert.equal(bytesToBase64(bytes),'AgM=');
    assert.equal(calls,1);
});
