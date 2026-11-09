import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';
import { OUTPUT, buildScrollbars } from '../../scripts/build-scrollbars.mjs';

// The sandbox document loads the scrollbars as /scrollbars.js: one
// implementation, bundled by scripts/build-scrollbars.mjs and committed.
it('public/scrollbars.js is src/lib/scrollbars.ts bundled (run node scripts/build-scrollbars.mjs after an edit)', async () => {
    expect(readFileSync(OUTPUT, 'utf8')).toBe(await buildScrollbars());
});
