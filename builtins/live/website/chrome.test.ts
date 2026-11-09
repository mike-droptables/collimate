// SPDX-License-Identifier: MIT
//
// chrome.test.ts — the website renderer's chrome is on the skin tokens (R05):
// the toolbar, the address field, the buttons, the loading cover and the
// empty pane read only `var(--token)` colours, fonts and radii, with teal for
// the focus ring, red for a failed action and no brass (the renderer has no
// selection). The site in the frame is never themed.
//
// Runs under node:test via the tsx loader (see tests/test_web_chrome.py),
// against main.tsx bundled with its CDN/SDK imports stubbed; the SDK's
// `tokens` in that bundle are the real ones.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const BUNDLE = process.env.WEBSITE_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'WEBSITE_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_web_chrome.py does this).',
    );
}

let m: any;

before(async () => {
    m = await import(pathToFileURL(path.resolve(BUNDLE)).href);
});

const SKIN_VARS = new Set([
    '--canvas', '--sheet', '--sheet-2', '--sheet-3', '--line', '--line-2',
    '--ink', '--ink-2', '--ink-3', '--brass', '--brass-soft',
    '--teal', '--teal-soft', '--teal-sheet', '--red', '--red-soft', '--green',
    '--code-bg', '--prose-ink', '--shadow', '--lift',
    '--font-ui', '--font-mono', '--font-prose', '--radius',
]);

/** Every string value of a style (or a CSS text), checked for what R05 bans:
 *  a colour literal, a var() fallback, a name outside the skin vocabulary. */
function assertOnTokens(where: string, value: unknown) {
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    assert.doesNotMatch(text, /#[0-9a-f]{3,8}\b/i, `${where}: colour literal`);
    assert.doesNotMatch(text, /\b(rgba?|hsla?)\(/i, `${where}: colour literal`);
    assert.doesNotMatch(text, /var\(--[\w-]+\s*,/, `${where}: var() fallback`);
    assert.doesNotMatch(text, /\b(white|black)\b/i, `${where}: named colour`);
    for (const [, name] of text.matchAll(/var\((--[\w-]+)\)/g)) {
        assert.ok(SKIN_VARS.has(name), `${where}: ${name} is not a skin token`);
    }
    assert.doesNotMatch(text, /--brass/, `${where}: brass is the selection accent only`);
}

function everyStyle(): Record<string, unknown> {
    return {
        CHROME_CSS: m.CHROME_CSS,
        groundStyle: m.groundStyle,
        toolbarStyle: m.toolbarStyle,
        addressStyle: [m.addressStyle(false), m.addressStyle(true)],
        buttonStyle: ['quiet', 'action'].flatMap((kind) => [
            m.buttonStyle(kind), m.buttonStyle(kind, { hover: true }),
            m.buttonStyle(kind, { busy: true }), m.buttonStyle(kind, { dimmed: true }),
        ]),
        actionStatusStyle: [m.actionStatusStyle(true), m.actionStatusStyle(false)],
        frameStyle: m.frameStyle,
        coverStyle: [m.coverStyle(true), m.coverStyle(false)],
        emptyStyle: m.emptyStyle,
        startingStyle: m.startingStyle,
    };
}

test('every chrome style reads skin tokens only, with no fallback or literal (R05)', () => {
    for (const [name, value] of Object.entries(everyStyle())) {
        assert.ok(value !== undefined, `${name} is exported`);
        assertOnTokens(name, value);
    }
});

test('the toolbar is a 36px raised bar on the ui font, the address on the mono font (R05)', () => {
    assert.equal(m.toolbarStyle.height, 36);
    assert.equal(m.toolbarStyle.background, 'var(--sheet-2)');
    assert.equal(m.toolbarStyle.borderBottom, '1px solid var(--line)');
    assert.equal(m.toolbarStyle.fontFamily, 'var(--font-ui)');
    const idle = m.addressStyle(false);
    assert.equal(idle.fontFamily, 'var(--font-mono)');
    assert.equal(idle.background, 'var(--sheet)');
    assert.equal(idle.color, 'var(--ink)');
    assert.equal(idle.border, '1px solid var(--line)');
    assert.equal(m.addressStyle(true).border, '1px solid var(--line-2)');
    assert.equal(idle.borderRadius, 'calc(var(--radius) - 2px)');
});

test('quiet and action buttons follow the mock: bordered, sheet-3 on hover, dimmed while another runs (R05)', () => {
    const quiet = m.buttonStyle('quiet');
    assert.equal(quiet.background, 'transparent');
    assert.equal(quiet.color, 'var(--ink-2)');
    assert.equal(quiet.border, '1px solid var(--line-2)');
    assert.equal(quiet.fontFamily, 'var(--font-ui)');
    assert.equal(quiet.height, 24);
    assert.equal(quiet.borderRadius, 'calc(var(--radius) - 3px)');
    assert.equal(m.buttonStyle('quiet', { hover: true }).background, 'var(--sheet-3)');
    assert.equal(m.buttonStyle('quiet', { hover: true }).color, 'var(--ink)');
    const action = m.buttonStyle('action');
    assert.equal(action.background, 'var(--sheet)');
    assert.equal(action.color, 'var(--ink)');
    assert.equal(action.fontWeight, 600);
    assert.equal(m.buttonStyle('action', { hover: true }).background, 'var(--sheet-3)');
    assert.equal(m.buttonStyle('action', { busy: true, hover: true }).background, 'var(--sheet)');
    assert.equal(m.buttonStyle('action', { busy: true }).cursor, 'wait');
    assert.equal(m.buttonStyle('action', { dimmed: true }).opacity, 0.45);
});

test('a failed action is red, a sent one quiet; focus is teal (R05)', () => {
    assert.equal(m.actionStatusStyle(false).color, 'var(--red)');
    assert.equal(m.actionStatusStyle(true).color, 'var(--ink-2)');
    assert.match(m.CHROME_CSS, /button:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--teal\)/);
    assert.match(m.CHROME_CSS, /input::placeholder\s*\{\s*color:\s*var\(--ink-3\)/);
});

test('the loading cover and the frame ground sit on the sheet; the cover fades out (R05)', () => {
    assert.equal(m.frameStyle.background, 'var(--sheet)');
    const covered = m.coverStyle(true);
    assert.equal(covered.background, 'var(--sheet)');
    assert.equal(covered.color, 'var(--ink-3)');
    assert.equal(covered.fontFamily, 'var(--font-ui)');
    assert.equal(covered.opacity, 1);
    assert.equal(covered.pointerEvents, 'auto');
    const gone = m.coverStyle(false);
    assert.equal(gone.opacity, 0);
    assert.equal(gone.pointerEvents, 'none');
    assert.equal(m.emptyStyle.color, 'var(--ink-3)');
    assert.equal(m.groundStyle.background, 'var(--sheet)');
});

test('the host starting placeholder reads as starting, with the runner name (GN-03)', () => {
    assert.equal(m.startingLabelOf(JSON.stringify({ starting: true, label: 'Notebook server' })), 'Notebook server');
    assert.equal(m.startingLabelOf(JSON.stringify({ starting: true })), '');
    // A real pointer, a non-true flag and junk are not the placeholder.
    assert.equal(m.startingLabelOf(JSON.stringify({ url: '/proxy/1/', directUrl: 'http://127.0.0.1:9/' })), null);
    assert.equal(m.startingLabelOf(JSON.stringify({ starting: 'yes', label: 'x' })), null);
    assert.equal(m.startingLabelOf('[]'), null);
    assert.equal(m.startingLabelOf('not json'), null);
    assert.equal(m.startingLabelOf(''), null);
    // Its pane is quiet, centred, on the ui font.
    assert.equal(m.startingStyle.color, 'var(--ink-3)');
    assert.equal(m.startingStyle.fontFamily, 'var(--font-ui)');
    assert.equal(m.startingStyle.justifyContent, 'center');
});
