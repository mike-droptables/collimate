// SPDX-License-Identifier: MIT
//
// theme.test.ts — the image renderer's transparency checkerboard on the
// skin tokens (row R05 of the 2026-10-06 workbench plan): sheet-3 squares
// on a sheet-2 ground, no literal. The harness bundles main.tsx with
// framework imports stubbed and passes IMAGE_RENDERER_BUNDLE; see
// tests/test_media_utility_renderer_tokens.py.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const BUNDLE = process.env.IMAGE_RENDERER_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'IMAGE_RENDERER_BUNDLE is not set — bundle main.tsx first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_media_utility_renderer_tokens.py does this).',
    );
}

let CHECKERBOARD: Record<string, string>;

before(async () => {
    const mod: any = await import(pathToFileURL(BUNDLE).href);
    CHECKERBOARD = mod.CHECKERBOARD;
});

test('the image checkerboard is sheet-3 on sheet-2 (R05)', () => {
    assert.equal(CHECKERBOARD.backgroundColor, 'var(--sheet-2)');
    const squares = CHECKERBOARD.backgroundImage.split('linear-gradient(').slice(1);
    assert.equal(squares.length, 4);
    for (const square of squares) assert.match(square, /var\(--sheet-3\)/);
    for (const v of Object.values(CHECKERBOARD)) {
        assert.doesNotMatch(v, /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i);
    }
});
