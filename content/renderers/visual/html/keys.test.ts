// SPDX-License-Identifier: MIT
//
// keys.test.ts — the page's key bridge (keys.ts, re-exported by main.tsx),
// run against a stand-in window: it asks its parent for the host's chords,
// consumes and reports a matching keydown, leaves every other key to the
// page, reports modifier releases and reports a double Esc as the release
// gesture.
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

let keyBridge: () => string;
let M: { hello: string; keys: string; keydown: string; keyup: string; release: string };

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    keyBridge = mod.keyBridge;
    M = mod.PAGE_KEY_MESSAGES;
});

/** A stand-in page: the bridge's listeners, what it posted to its parent,
 *  a way to press a key or answer as the parent, and its clock (ms). */
function page() {
    const clock = { now: 1_000_000 };
    const listeners: Record<string, Array<{ fn: (e: any) => void; capture: unknown }>> = {};
    const posts: any[] = [];
    const parent = { postMessage: (message: any, target: string) => { assert.equal(target, '*'); posts.push(message); } };
    const window: any = {
        parent,
        addEventListener: (type: string, fn: (e: any) => void, capture: unknown) => {
            (listeners[type] ??= []).push({ fn, capture });
        },
    };
    vm.runInNewContext(keyBridge(), { window, Date: { now: () => clock.now } });
    const fire = (type: string, e: any) => { for (const l of listeners[type] ?? []) l.fn(e); };
    const press = (type: 'keydown' | 'keyup', init: Record<string, unknown>) => {
        const e: any = { key: '', code: '', altKey: false, ctrlKey: false, shiftKey: false, metaKey: false, ...init };
        e.prevented = false; e.stopped = false;
        e.preventDefault = () => { e.prevented = true; };
        e.stopImmediatePropagation = () => { e.stopped = true; };
        fire(type, e);
        return e;
    };
    const answer = (combos: unknown[], source: unknown = parent) => fire('message', { source, data: { type: M.keys, combos } });
    return { listeners, posts: () => JSON.parse(JSON.stringify(posts)), press, answer, clock };
}

const MOVE = { key: 'ArrowRight', ctrlKey: true, shiftKey: true, altKey: false, metaKey: false };
const RUN = { key: 'r', code: 'KeyR', altKey: true, ctrlKey: false, shiftKey: false, metaKey: false };

test('the bridge asks its parent for the host chords and listens in the capture phase', () => {
    const p = page();
    assert.deepEqual(p.posts(), [{ type: 'collimate:page-keys-hello' }]);
    assert.equal(M.keys, 'collimate:page-keys');
    assert.equal(p.listeners.keydown[0].capture, true);
    assert.equal(p.listeners.keyup[0].capture, true);
});

test('a host chord is consumed in the page and reported; other keys stay the page’s', () => {
    const p = page();
    p.answer([MOVE, RUN]);
    const move = p.press('keydown', { key: 'ArrowRight', code: 'ArrowRight', ctrlKey: true, shiftKey: true });
    assert.equal(move.prevented, true);
    assert.equal(move.stopped, true);
    const run = p.press('keydown', { key: '®', code: 'KeyR', altKey: true });
    assert.equal(run.prevented, true);
    for (const init of [
        { key: 'ArrowRight', code: 'ArrowRight', ctrlKey: true },
        { key: 'r', code: 'KeyR' },
        { key: 'Escape', code: 'Escape' },
        { key: 'a', code: 'KeyA', ctrlKey: true },
    ]) {
        const e = p.press('keydown', init);
        assert.equal(e.prevented, false, JSON.stringify(init));
        assert.equal(e.stopped, false, JSON.stringify(init));
    }
    assert.deepEqual(p.posts().slice(1), [
        { type: 'collimate:page-keydown', key: 'ArrowRight', code: 'ArrowRight', altKey: false, ctrlKey: true, shiftKey: true, metaKey: false },
        { type: 'collimate:page-keydown', key: '®', code: 'KeyR', altKey: true, ctrlKey: false, shiftKey: false, metaKey: false },
    ]);
});

test('before the chords arrive, and from any other window, nothing is taken', () => {
    const p = page();
    p.answer([MOVE], {});
    const e = p.press('keydown', { key: 'ArrowRight', ctrlKey: true, shiftKey: true });
    assert.equal(e.prevented, false);
    assert.equal(p.posts().length, 1);
});

test('a remap replaces the chords', () => {
    const p = page();
    p.answer([MOVE]);
    p.answer([RUN]);
    assert.equal(p.press('keydown', { key: 'ArrowRight', ctrlKey: true, shiftKey: true }).prevented, false);
    assert.equal(p.press('keydown', { key: 'r', code: 'KeyR', altKey: true }).prevented, true);
});

test('every modifier release is reported, other releases are not', () => {
    const p = page();
    p.press('keyup', { key: 'Shift', code: 'ShiftLeft' });
    p.press('keyup', { key: 'Control', code: 'ControlLeft' });
    p.press('keyup', { key: 'ArrowRight', code: 'ArrowRight' });
    assert.deepEqual(p.posts().slice(1).map((m: any) => [m.type, m.key]), [
        ['collimate:page-keyup', 'Shift'], ['collimate:page-keyup', 'Control'],
    ]);
});

test('a second bare Esc within 600 ms is consumed and reported as the release; the first is the page’s', () => {
    const p = page();
    const ESC = { key: 'Escape', code: 'Escape' };
    const first = p.press('keydown', ESC);
    assert.equal(first.prevented, false);
    assert.equal(first.stopped, false);
    p.clock.now += 599;
    const second = p.press('keydown', ESC);
    assert.equal(second.prevented, true);
    assert.equal(second.stopped, true);
    assert.deepEqual(p.posts().slice(1), [{ type: 'collimate:page-release' }]);
    assert.equal(M.release, 'collimate:page-release');
    // The count starts over: a third press is a first again.
    p.clock.now += 10;
    assert.equal(p.press('keydown', ESC).prevented, false);
    // Too slow, or a modified Esc, is no release.
    p.clock.now += 600;
    assert.equal(p.press('keydown', ESC).prevented, false);
    p.clock.now += 10;
    assert.equal(p.press('keydown', { ...ESC, shiftKey: true }).prevented, false);
    p.clock.now += 700;
    p.press('keydown', ESC);
    p.clock.now += 10;
    p.press('keydown', { ...ESC, ctrlKey: true });
    assert.equal(p.posts().length, 2);
});

test('the bridge holds no `<`, so nothing can close its script element', () => {
    assert.ok(!keyBridge().includes('<'));
});
