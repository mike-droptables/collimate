// SPDX-License-Identifier: MIT
//
// run.test.ts — the run card renderer's pure model: the `.run` card parse
// (the host's shape, and nothing else), the title, the start time, and the
// one line that says where the run's Stop and log are.
// Runs under node:test via the tsx loader against a stub-bundled build of
// main.tsx (a renderer must be a single self-contained module, so
// tests/test_run_renderer_model.py bundles it first); the bundle path arrives
// via RUN_RENDERER_BUNDLE, and TZ=UTC pins the local-time formatting. The
// (R03) cases pin the card's styles to the skin tokens.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const MAIN_TSX = path.resolve(path.dirname(fileURLToPath(import.meta.url)), 'main.tsx');

const BUNDLE = process.env.RUN_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'RUN_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_run_renderer_model.py does this).',
    );
}

let m: any;

before(async () => {
    m = await import(pathToFileURL(BUNDLE!).href);
});

// The card the host writes (api/cell_commits.run_card), in shape.
const CARD = {
    v: 1,
    run_id: '0123abcd-0000-4000-8000-000000000000',
    runner: { id: 'sample-runner', name: 'Sample Runner' },
    generator: 'sample',
    label: 'sample-runner-0123abcd',
    folder: '_runs/sample-runner-0123abcd',
    started_at: '2026-09-25T14:03:07.250000+00:00',
};

test('parseRunCard reads the host card', () => {
    assert.deepEqual(m.parseRunCard(JSON.stringify(CARD)), {
        runId: CARD.run_id,
        runnerId: 'sample-runner',
        runnerName: 'Sample Runner',
        generator: 'sample',
        label: 'sample-runner-0123abcd',
        folder: '_runs/sample-runner-0123abcd',
        startedAt: CARD.started_at,
    });
});

test('parseRunCard refuses what the host did not write', () => {
    const noLabel: any = { ...CARD };
    delete noLabel.label;
    for (const raw of [
        '', 'not json', '[]', 'null', '42',
        JSON.stringify({ ...CARD, v: 2 }),
        JSON.stringify({ ...CARD, v: '1' }),
        JSON.stringify({ ...CARD, run_id: '' }),
        JSON.stringify(noLabel),
    ]) {
        assert.equal(m.parseRunCard(raw), null, raw);
    }
});

test('a card missing its runner still parses and falls back to the generator', () => {
    const card = m.parseRunCard(JSON.stringify({
        ...CARD, runner: { id: null, name: null }, label: 'x-0123abcd',
    }));
    assert.equal(card.runnerId, '');
    assert.equal(card.runnerName, '');
    assert.equal(m.runTitle(card), 'sample');
    assert.equal(m.runTitle(m.parseRunCard(JSON.stringify(CARD))), 'Sample Runner');
    assert.equal(m.runTitle(m.parseRunCard(JSON.stringify({ ...CARD, runner: { id: 'rid' } }))), 'rid');
});

test('the start time reads in local time, and a bad one reads as nothing', () => {
    assert.equal(m.formatStarted(CARD.started_at), '2026-09-25 14:03:07');
    assert.equal(m.formatStarted(''), '');
    assert.equal(m.formatStarted('yesterday-ish'), '');
});

test('the card says where the run\'s Stop and log are', () => {
    assert.equal(m.RUN_NOTE,
        "This run's Stop is in its drilldown: the cell's status light opens the status pane, where the run's row opens it. The drilldown's Log option shows its lines.");
});

// ── the card on the skin tokens (R03) ─────────────────────────────────

const COLOUR_KEYS = ['color', 'background', 'border', 'borderColor', 'boxShadow', 'outline'];

/** Every `var(--name)` a style value references. */
const varsIn = (v: unknown): string[] =>
    [...String(v).matchAll(/var\((--[a-z0-9-]+)\)/g)].map((x) => x[1]);

test('every colour on the card is a skin token (R03)', () => {
    for (const [part, style] of Object.entries<any>(m.STYLES)) {
        for (const key of COLOUR_KEYS) {
            if (!(key in style)) continue;
            const value = String(style[key]);
            assert.ok(varsIn(value).length > 0, `${part}.${key} = ${value} names no token`);
            assert.doesNotMatch(value, /#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i, `${part}.${key}`);
        }
    }
});

test('the card sits on sheet, its panels on sheet-2 with line hairlines, text on the ink tiers (R03)', () => {
    const S = m.STYLES;
    assert.equal(S.root.background, 'var(--sheet)');
    assert.equal(S.root.color, 'var(--ink)');
    assert.equal(S.panel.background, 'var(--sheet-2)');
    assert.equal(S.panel.border, '1px solid var(--line)');
    assert.equal(S.title.color, 'var(--ink)');
    assert.equal(S.rowName.color, 'var(--ink-2)');
    assert.equal(S.rowValue.color, 'var(--ink)');
    for (const quiet of ['label', 'note', 'empty']) {
        assert.equal(S[quiet].color, 'var(--ink-3)', quiet);
    }
});

test('the run tag is neutral, not live and not selected (R03)', () => {
    const S = m.STYLES;
    assert.equal(S.tag.color, 'var(--ink-2)');
    assert.equal(S.tag.border, '1px solid var(--line-2)');
    assert.ok(!('background' in S.tag), 'the tag has no fill');
    const used = Object.values<any>(S.tag).flatMap(varsIn);
    assert.ok(!used.some((v) => v.startsWith('--teal') || v.startsWith('--brass')), used.join(' '));
    assert.equal(S.live, undefined);
    assert.equal(S.liveDot, undefined);
    const src = readFileSync(MAIN_TSX, 'utf8');
    assert.match(src, /data-testid="run-card-tag">run</);
    assert.doesNotMatch(src, />\s*running\b/);
});

test('the panel radius steps down from the skin radius (R03)', () => {
    assert.match(String(m.STYLES.panel.borderRadius), /var\(--radius\)/);
    assert.equal(m.STYLES.panel.borderRadius, 'calc(var(--radius) - 2px)');
});

test('the card marks no selection, so it carries no brass (R03)', () => {
    const used = Object.values<any>(m.STYLES).flatMap((s) => Object.values(s).flatMap(varsIn));
    assert.ok(!used.some((v) => v.startsWith('--brass')), used.join(' '));
});

test('the card speaks in the skin\'s ui face and its paths in the mono face (R03)', () => {
    const S = m.STYLES;
    assert.equal(S.root.fontFamily, 'var(--font-ui)');
    assert.equal(S.label.fontFamily, 'var(--font-mono)');
    assert.equal(S.rowMono.fontFamily, 'var(--font-mono)');
});

test('the renderer source holds no colour literal or fallback (R03)', () => {
    const src = readFileSync(MAIN_TSX, 'utf8');
    assert.doesNotMatch(src, /#[0-9a-fA-F]{3,8}\b/);
    assert.doesNotMatch(src, /\b(?:rgba?|hsla?)\(/);
    assert.doesNotMatch(src, /var\(--[a-z0-9-]+\s*,/);
    assert.match(src, /useTheme\(api\)/);
});
