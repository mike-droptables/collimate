import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

// The W1 exit grep as a test: the skin tokens are the only colour names in the
// frontend, and colour values live only where the skins are defined.

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const SKIP_DIRS = new Set(['node_modules', 'dist', '__screenshots__', 'fonts', 'coverage', '.vite']);
const TEXT = /\.(?:ts|tsx|js|mjs|cjs|css|html|json|md)$/;

function walk(dir: string): string[] {
    return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) return SKIP_DIRS.has(entry.name) ? [] : walk(full);
        return TEXT.test(entry.name) ? [full] : [];
    });
}

const rel = (file: string) => path.relative(FRONTEND, file).split(path.sep).join('/');

const ALL = [
    ...walk(path.join(FRONTEND, 'src')),
    ...walk(path.join(FRONTEND, 'scripts')),
    ...walk(path.join(FRONTEND, 'tests')),
    ...['index.html', 'public/sandbox.html', 'vite.config.ts', 'vitest.config.ts', 'eslint.config.mjs']
        .map((name) => path.join(FRONTEND, name)),
].filter((file) => { try { readFileSync(file); return true; } catch { return false; } });

/** Source that paints the app: not tests, not test helpers. */
const PAINTING = ALL.filter((file) => {
    const name = rel(file);
    return /^(?:src\/.*\.(?:ts|tsx|css)|index\.html|public\/sandbox\.html)$/.test(name)
        && !/\.(?:test|spec)\.tsx?$|\/__tests__\/|^src\/test\/|\/__browser__\//.test(name);
});

const LITERAL = /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\(|color-mix\(/i;
const PALETTE = /\b(?:bg|text|border|ring|fill|stroke|outline|from|via|to|divide|placeholder|accent|caret|decoration|shadow)-(?:slate|zinc|gray|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b|\b(?:bg|text|border|ring|fill|stroke)-(?:white|black)\b/;

/** The two places a colour value may be written: the skins and the pre-theme paint. */
function withoutTokenDefinitions(name: string, source: string): string {
    if (name === 'src/lib/themes.ts') return '';
    if (name === 'src/app/globals.css') return source.replace(/^:root \{[^}]*\}/m, '');
    // A skin token with animated opacity adds no colour of its own. Keep
    // rejecting every other color-mix, including mixes with literal colours.
    return source.replace(/color-mix\(in srgb,\s*var\(--[\w-]+\)\s+var\(--[\w-]+\),\s*transparent\)/g, '');
}

describe('the W1 exit grep', () => {
    it('no file in frontend/ names a retired palette token outside the one list of them (S04)', () => {
        const offenders = ALL
            .filter((file) => rel(file) !== 'src/test/retiredTokens.ts')
            .filter((file) => RETIRED_TOKENS.test(readFileSync(file, 'utf8')))
            .map(rel);
        expect(offenders).toEqual([]);
    });

    it('no painting source holds a colour literal or a palette utility outside the skin definitions (S04)', () => {
        expect(PAINTING.length).toBeGreaterThan(200);
        const offenders = PAINTING.flatMap((file) => {
            const name = rel(file);
            const source = withoutTokenDefinitions(name, readFileSync(file, 'utf8'));
            return source.split('\n').flatMap((line, i) =>
                LITERAL.test(line) || PALETTE.test(line) ? [`${name}:${i + 1}: ${line.trim()}`] : []);
        });
        expect(offenders).toEqual([]);
    });
});
