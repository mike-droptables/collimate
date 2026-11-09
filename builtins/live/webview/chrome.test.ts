// SPDX-License-Identifier: MIT
//
// chrome.test.ts — the webview renderer's chrome is on the skin tokens (R05):
// the toolbar, its status dot, the history tools, the address field, the
// generator's buttons, the Enter page form, the fallback banner, the
// navigation error and the notice / crash panels read only `var(--token)`
// colours, fonts and radii. Teal is the live dot and the focus ring, red the
// closed dot and a refused navigation, and there is no brass (the renderer
// has no selection). The page in the guest or frame is never themed.
//
// Runs under node:test via the tsx loader (see tests/test_web_chrome.py),
// against main.tsx bundled with its CDN/SDK imports stubbed; the SDK's
// `tokens` in that bundle are the real ones.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const BUNDLE = process.env.WEBVIEW_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'WEBVIEW_BUNDLE is not set — bundle main.tsx first via ' +
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

const STATUSES = [
    { kind: 'idle' }, { kind: 'starting' }, { kind: 'connecting' }, { kind: 'live' },
    { kind: 'closed', reason: 'session ended' },
];

function everyStyle(): Record<string, unknown> {
    return {
        CHROME_CSS: m.CHROME_CSS,
        statusColorOf: STATUSES.map((s) => m.statusColorOf(s)),
        groundStyle: m.groundStyle,
        toolbarStyle: m.toolbarStyle,
        addressStyle: [m.addressStyle(false), m.addressStyle(true), m.addressStyle(false, 30), m.addressStyle(true, 30)],
        buttonStyle: ['tool', 'quiet', 'primary'].flatMap((kind) => [
            m.buttonStyle(kind), m.buttonStyle(kind, { hover: true }),
            m.buttonStyle(kind, { busy: true }), m.buttonStyle(kind, { height: 30 }),
        ]),
        fallbackBannerStyle: m.fallbackBannerStyle,
        navigationErrorStyle: m.navigationErrorStyle,
        pageBoxStyle: m.pageBoxStyle,
        pageBoxStyleFor: [m.pageBoxStyleFor(null), m.pageBoxStyleFor(412)],
        letterboxStyle: [m.letterboxStyle(null), m.letterboxStyle(412), m.letterboxStyle(800)],
        deviceGroupStyle: m.deviceGroupStyle,
        deviceSegmentStyle: [true, false].flatMap((active) => [
            m.deviceSegmentStyle(active), m.deviceSegmentStyle(active, { hover: true }),
            m.deviceSegmentStyle(active, { disabled: true }),
        ]),
        frameStyle: m.frameStyle,
        enterCardStyle: m.enterCardStyle,
        panelStyle: m.panelStyle,
    };
}

test('every chrome style reads skin tokens only, with no fallback or literal (R05)', () => {
    for (const [name, value] of Object.entries(everyStyle())) {
        assert.ok(value !== undefined, `${name} is exported`);
        assertOnTokens(name, value);
    }
});

test('the status dot is teal while live, red once closed, quiet while connecting (R05)', () => {
    assert.equal(m.statusColorOf({ kind: 'live' }), 'var(--teal)');
    assert.equal(m.statusColorOf({ kind: 'closed', reason: 'x' }), 'var(--red)');
    assert.equal(m.statusColorOf({ kind: 'connecting' }), 'var(--ink-3)');
    assert.equal(m.statusColorOf({ kind: 'idle' }), 'var(--ink-3)');
});

test('the toolbar is a 36px raised bar on the ui font, the address on the mono font (R05)', () => {
    assert.equal(m.toolbarStyle.height, 36);
    assert.equal(m.toolbarStyle.background, 'var(--sheet-2)');
    assert.equal(m.toolbarStyle.borderBottom, '1px solid var(--line)');
    assert.equal(m.toolbarStyle.fontFamily, 'var(--font-ui)');
    const bar = m.addressStyle(false);
    assert.equal(bar.height, 26);
    assert.equal(bar.fontFamily, 'var(--font-mono)');
    assert.equal(bar.background, 'var(--sheet)');
    assert.equal(bar.color, 'var(--ink)');
    assert.equal(bar.border, '1px solid var(--line)');
    assert.equal(m.addressStyle(true).border, '1px solid var(--line-2)');
    assert.equal(m.addressStyle(false, 30).height, 30);
    assert.equal(m.addressStyle(false, 30).fontFamily, 'var(--font-mono)');
});

test('history tools are borderless, actions quiet, Open and Reload primary (R05)', () => {
    const tool = m.buttonStyle('tool');
    assert.equal(tool.border, 'none');
    assert.equal(tool.background, 'transparent');
    assert.equal(tool.color, 'var(--ink-3)');
    assert.equal(tool.width, tool.height);
    assert.equal(tool.borderRadius, 'calc(var(--radius) - 4px)');
    assert.equal(m.buttonStyle('tool', { hover: true }).background, 'var(--sheet-3)');
    assert.equal(m.buttonStyle('tool', { hover: true }).color, 'var(--ink)');

    const quiet = m.buttonStyle('quiet');
    assert.equal(quiet.background, 'transparent');
    assert.equal(quiet.color, 'var(--ink-2)');
    assert.equal(quiet.border, '1px solid var(--line-2)');
    assert.equal(quiet.fontFamily, 'var(--font-ui)');
    assert.equal(m.buttonStyle('quiet', { busy: true }).cursor, 'wait');
    assert.equal(m.buttonStyle('quiet', { busy: true, hover: true }).background, 'transparent');

    const primary = m.buttonStyle('primary', { height: 30 });
    assert.equal(primary.height, 30);
    assert.equal(primary.background, 'var(--sheet)');
    assert.equal(primary.color, 'var(--ink)');
    assert.equal(primary.fontWeight, 600);
    assert.equal(primary.border, '1px solid var(--line-2)');
    assert.equal(m.buttonStyle('primary', { hover: true }).background, 'var(--sheet-3)');
});

test('the Enter page form is a sheet card; a refused navigation is red on red-soft (R05)', () => {
    assert.equal(m.enterCardStyle.background, 'var(--sheet)');
    assert.equal(m.enterCardStyle.border, '1px solid var(--line-2)');
    assert.equal(m.enterCardStyle.borderRadius, 'var(--radius)');
    assert.equal(m.enterCardStyle.boxShadow, 'var(--shadow)');
    assert.equal(m.enterCardStyle.fontFamily, 'var(--font-ui)');
    assert.equal(m.navigationErrorStyle.color, 'var(--red)');
    assert.equal(m.navigationErrorStyle.background, 'var(--red-soft)');
    assert.equal(m.fallbackBannerStyle.color, 'var(--ink-3)');
    assert.equal(m.pageBoxStyle.background, 'var(--sheet)');
    assert.equal(m.frameStyle.background, 'var(--sheet)');
    assert.equal(m.panelStyle.background, 'var(--sheet)');
    assert.equal(m.panelStyle.color, 'var(--ink-3)');
    assert.match(m.CHROME_CSS, /button:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--teal\)/);
    assert.match(m.CHROME_CSS, /input::placeholder\s*\{\s*color:\s*var\(--ink-3\)/);
});

test('the device toggle: the picked segment is ink on the sheet, the rest quiet; disabled while starting (GN-04)', () => {
    const on = m.deviceSegmentStyle(true);
    assert.equal(on.background, 'var(--sheet)');
    assert.equal(on.color, 'var(--ink)');
    assert.equal(on.fontWeight, 600);
    const off = m.deviceSegmentStyle(false);
    assert.equal(off.background, 'transparent');
    assert.equal(off.color, 'var(--ink-3)');
    assert.equal(m.deviceSegmentStyle(false, { hover: true }).background, 'var(--sheet-3)');
    const disabled = m.deviceSegmentStyle(false, { hover: true, disabled: true });
    assert.equal(disabled.background, 'transparent', 'a disabled segment does not light on hover');
    assert.equal(disabled.opacity, 0.45);
    assert.equal(disabled.cursor, 'default');
    assert.equal(m.deviceGroupStyle.border, '1px solid var(--line-2)');
    assert.equal(m.deviceGroupStyle.height, 24);
});

test('a letterboxed device sits on the canvas, the page column on the sheet (GN-04)', () => {
    assert.equal(m.pageBoxStyleFor(412).background, 'var(--canvas)');
    assert.equal(m.letterboxStyle(412).background, 'var(--sheet)');
    assert.equal(m.letterboxStyle(412).borderLeft, '1px solid var(--line)');
    assert.equal(m.statusColorOf({ kind: 'starting' }), 'var(--ink-3)');
});
