// SPDX-License-Identifier: MIT
//
// links.test.ts — the page renderer's links (links.ts, re-exported by
// main.tsx), tested off-DOM: which file of the cell a reference the page
// loads names, the page's folder as its `<base>`, and the click bridge run
// against a stand-in window, so the message the page posts (and what it
// leaves to the browser) is pinned.
//
// The harness bundles main.tsx with framework imports stubbed
// (scripts/bundle_renderer_test.mjs) and passes HTML_RENDERER_BUNDLE; see
// tests/test_html_renderer_links.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.HTML_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'HTML_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_html_renderer_links.py does this).',
    );
}

let resolveCellPath: (docPath: string, href: string) => string | null;
let pageFolderUrl: (cellBase: string, docPath: string) => string;
let linkBridge: () => string;
let OPEN_LINK_MESSAGE: string;

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    resolveCellPath = mod.resolveCellPath;
    pageFolderUrl = mod.pageFolderUrl;
    linkBridge = mod.linkBridge;
    OPEN_LINK_MESSAGE = mod.OPEN_LINK_MESSAGE;
});

const BASE = 'http://app.test/api/cellcap/c4p/channels/ch/cells/k1/files/';

test('a reference names a file beside the page, `../` within the cell, `/` the cell root', () => {
    assert.equal(resolveCellPath('report.html', 'notes.md'), 'notes.md');
    assert.equal(resolveCellPath('report.html', './plot.png'), 'plot.png');
    assert.equal(resolveCellPath('site/index.html', 'notes.md'), 'site/notes.md');
    assert.equal(resolveCellPath('site/index.html', '../data/results.csv'), 'data/results.csv');
    assert.equal(resolveCellPath('site/index.html', '/notes.md'), 'notes.md');
    assert.equal(resolveCellPath('site/index.html', 'notes.md?x=1#part'), 'site/notes.md');
    assert.equal(resolveCellPath('report.html', 'my%20notes.md'), 'my notes.md');
    assert.equal(resolveCellPath('a/b/page.html', '%2e%2e/c.md'), 'a/c.md');
});

test('a scheme, `//host`, an anchor, an escape or nothing names no cell file', () => {
    for (const href of ['https://example.com', 'mailto:a@b.c', 'data:text/plain,x', 'javascript:void(0)',
        '//cdn.test/x.js', '#part', '', '../outside.md', 'site/../../outside.md', '%2e%2e/x',
        'a%2Fb.md', 'a%5Cb.md', '?only', '/', '.']) {
        assert.equal(resolveCellPath('report.html', href), null, href);
    }
    assert.equal(resolveCellPath('site/index.html', '../../x.md'), null);
});

test("the page's <base> is its folder under the cell's file base, encoded per segment", () => {
    assert.equal(pageFolderUrl(BASE, 'report.html'), BASE);
    assert.equal(pageFolderUrl(BASE, 'site/index.html'), BASE + 'site/');
    assert.equal(pageFolderUrl(BASE, 'my site/a b/x.html'), BASE + 'my%20site/a%20b/');
    assert.equal(pageFolderUrl(BASE, ''), BASE);
});

/** Run the bridge in a stand-in window and click one element: what the page
 *  posts, whether it cancelled the browser's navigation, and the hash it
 *  set. */
function click(link: { href: unknown; raw: string | null; tag?: string } | null,
               event: { button?: number; defaultPrevented?: boolean } = {}) {
    const listeners: Array<(e: any) => void> = [];
    const posts: Array<{ message: any; target: string }> = [];
    const location = { hash: '' };
    const window: any = {
        addEventListener: (type: string, fn: (e: any) => void) => { if (type === 'click') listeners.push(fn); },
        parent: { postMessage: (message: any, target: string) => posts.push({ message, target }) },
    };
    vm.runInNewContext(linkBridge(), { window, location });
    const anchor = link && {
        href: link.href,
        getAttribute: (name: string) => (name === 'href' ? link.raw : null),
    };
    let prevented = !!event.defaultPrevented;
    const e = {
        button: event.button ?? 0,
        get defaultPrevented() { return prevented; },
        preventDefault: () => { prevented = true; },
        target: { closest: (selector: string) => (selector === 'a[href],area[href]' ? anchor : null) },
    };
    assert.equal(listeners.length, 1);
    listeners[0](e);
    // The posts were built in the bridge's own realm: compare them as data.
    return { posts: JSON.parse(JSON.stringify(posts)), prevented: prevented && !event.defaultPrevented, hash: location.hash };
}

test('the bridge posts every link as its author wrote it and cancels the navigation', () => {
    assert.equal(OPEN_LINK_MESSAGE, 'collimate:open-link');
    for (const raw of ['notes.md', '../data/results.csv', '/notes.md', 'http://localhost:5173/', 'https://example.com',
        'mailto:a@b.c', 'file:///etc/passwd', '  notes.md  ']) {
        const r = click({ href: 'ignored', raw });
        assert.deepEqual(r.posts, [{ message: { type: OPEN_LINK_MESSAGE, href: raw.trim() }, target: '*' }], raw);
        assert.equal(r.prevented, true, raw);
    }
    // An SVG link's href is no string, but its attribute is what is posted.
    const svg = click({ href: { baseVal: 'notes.md' }, raw: 'notes.md' });
    assert.deepEqual(svg.posts, [{ message: { type: OPEN_LINK_MESSAGE, href: 'notes.md' }, target: '*' }]);
});

test('an anchor scrolls in place instead of resolving against the <base>', () => {
    const r = click({ href: BASE + '#results', raw: '#results' });
    assert.deepEqual(r.posts, []);
    assert.equal(r.prevented, true);
    assert.equal(r.hash, '#results');
});

test("a javascript: link, a handled click, another button and a non-link keep the page's and the browser's behaviour", () => {
    for (const [link, event] of [
        [{ href: 'javascript:void(0)', raw: 'javascript:void(0)' }, {}],
        [{ href: 'javascript:go()', raw: ' JavaScript:go()' }, {}],
        [{ href: BASE + 'notes.md', raw: 'notes.md' }, { defaultPrevented: true }],
        [{ href: BASE + 'notes.md', raw: 'notes.md' }, { button: 1 }],
        [null, {}],
    ] as const) {
        const r = click(link as any, event as any);
        assert.deepEqual(r.posts, [], JSON.stringify(link));
        assert.equal(r.prevented, false, JSON.stringify(link));
    }
});
