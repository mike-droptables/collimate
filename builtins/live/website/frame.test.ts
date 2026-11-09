// SPDX-License-Identifier: MIT
//
// frame.test.ts — which addresses the website renderer's frame shows with
// its own origin (`allow-same-origin`): another site, or a `/proxy/` address
// on the app server, the path a live run's served page is reached through
// (F1739). Every other address on the app server is framed without it.
//
// Runs under node:test via the tsx loader (see tests/test_website_frame.py).
// `frameKeepsOrigin` lives inline in main.tsx; the harness bundles main.tsx
// with its CDN/SDK imports stubbed and passes the bundle path in
// WEBSITE_BUNDLE.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const BUNDLE = process.env.WEBSITE_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'WEBSITE_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_website_frame.py does this).',
    );
}

let frameKeepsOrigin: (frameSrc: string, appOrigins: string[]) => boolean;

before(async () => {
    const mod = await import(pathToFileURL(path.resolve(BUNDLE)).href);
    frameKeepsOrigin = mod.frameKeepsOrigin;
});

const APP = ['http://127.0.0.1:5001'];

test('a live run\'s proxied page keeps its origin (F1739)', () => {
    assert.equal(frameKeepsOrigin('http://127.0.0.1:5001/proxy/ch/run-1/8080/', APP), true);
    assert.equal(frameKeepsOrigin('http://127.0.0.1:5001/proxy/ch/run-1/8080/static/app.js?x=1', APP), true);
});

test('any other address on the app server is framed without its origin (F1739)', () => {
    for (const src of [
        'http://127.0.0.1:5001/',
        'http://127.0.0.1:5001/sandbox.html',
        'http://127.0.0.1:5001/api/channels/ch/cells/c1/files/index.html',
        'http://127.0.0.1:5001/api/system/stop_all',
        'http://127.0.0.1:5001/proxy',
        'http://127.0.0.1:5001/proxyfoo/x',
        // A dot segment resolves before the check: this is /api/x.
        'http://127.0.0.1:5001/proxy/../api/x',
    ]) {
        assert.equal(frameKeepsOrigin(src, APP), false, src);
    }
});

test('the app server under another loopback name is still the app server (F1739)', () => {
    assert.equal(frameKeepsOrigin('http://localhost:5001/api/state', APP), false);
    assert.equal(frameKeepsOrigin('http://[::1]:5001/', APP), false);
    assert.equal(frameKeepsOrigin('http://localhost:5001/proxy/ch/run-1/8080/', APP), true);
    // Another port on loopback is another server.
    assert.equal(frameKeepsOrigin('http://localhost:8080/', APP), true);
});

test('a split-origin API counts as the app server too (F1739)', () => {
    const origins = ['https://ui.example.com', 'https://api.example.com'];
    assert.equal(frameKeepsOrigin('https://api.example.com/api/state', origins), false);
    assert.equal(frameKeepsOrigin('https://ui.example.com/index.html', origins), false);
    assert.equal(frameKeepsOrigin('https://api.example.com/proxy/ch/r/1/', origins), true);
});

test('another site keeps its own origin (F1739)', () => {
    assert.equal(frameKeepsOrigin('https://example.com/', APP), true);
    assert.equal(frameKeepsOrigin('http://192.168.1.20:3000/app', APP), true);
});

test('what is not an absolute http(s) address never keeps an origin (F1739)', () => {
    for (const src of ['', '/proxy/ch/r/1/', 'javascript:alert(1)', 'data:text/html,<p>x', 'about:blank', 'blob:http://127.0.0.1:5001/x']) {
        assert.equal(frameKeepsOrigin(src, APP), false, src);
    }
});
