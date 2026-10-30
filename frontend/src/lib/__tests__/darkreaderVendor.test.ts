import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/** The document view (the SDK's packages/renderer-hooks/src/document-view.tsx,
 *  used by the html renderer) loads DarkReader
 *  from the app's own origin at DARKREADER_PATH, so a page darkens offline.
 *  The app serves exactly that pinned build from public/. */
const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const REPO = path.resolve(FRONTEND, '..');
const VIEW = path.join(REPO, 'sdk', 'packages', 'renderer-hooks', 'src', 'document-view.tsx');

/** sha256 of the npm package's darkreader.js (darkreader@4.9.133, integrity
 *  sha512-d3dRGpVgoBnMXl9qfnTuRvrdtO/9e6puFbsDwxfycTwbRNFI+sNX+093h6qnNo4VosyHHUaIXyunDWvbEudTRQ==). */
const PINNED_SHA256 = '82619e7a0bcabbea15a91488bc74fd0f9ba8f8f2a1cc67527f78166037cce166';

function sdkPin(): { version: string; path: string } {
    const source = readFileSync(VIEW, 'utf8');
    const version = /export const DARKREADER_VERSION = '([^']+)';/.exec(source)?.[1] ?? '';
    const pathTemplate = /export const DARKREADER_PATH = `([^`]+)`;/.exec(source)?.[1] ?? '';
    return { version, path: pathTemplate.replace('${DARKREADER_VERSION}', version) };
}

describe('the vendored DarkReader build', () => {
    it('is served at the path the SDK document view loads, the pinned bytes unmodified (polish)', () => {
        const pin = sdkPin();
        expect(pin.version).toBe('4.9.133');
        expect(pin.path).toBe('/darkreader/4.9.133/darkreader.js');
        const file = path.join(FRONTEND, 'public', pin.path);
        expect(existsSync(file)).toBe(true);
        const bytes = readFileSync(file);
        expect(bytes.subarray(0, 40).toString('utf8')).toContain(`Dark Reader v${pin.version}`);
        expect(createHash('sha256').update(bytes).digest('hex')).toBe(PINNED_SHA256);
    });

    it('ships its licence beside it and in THIRD_PARTY_LICENSES (polish)', () => {
        const dir = path.join(FRONTEND, 'public', 'darkreader', sdkPin().version);
        const licence = readFileSync(path.join(dir, 'LICENSE'), 'utf8');
        expect(licence).toMatch(/^MIT License/);
        expect(licence).toContain('Dark Reader Ltd.');
        const thirdParty = readFileSync(path.join(REPO, 'THIRD_PARTY_LICENSES'), 'utf8');
        expect(thirdParty).toContain('Dark Reader 4.9.133\n  License: MIT');
        expect(thirdParty).toContain('Files: frontend/public/darkreader/4.9.133/darkreader.js');
    });
});
