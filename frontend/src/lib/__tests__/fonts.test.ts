import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { EBOOK_FONTS_REF, FONT_CATALOGUE, familyForStack, fontSlug, stackFor, type FontFamily, type FontSource } from '../fonts';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const FONTS_DIR = path.join(FRONTEND, 'public', 'fonts');
const FONTS_CSS = readFileSync(path.join(FONTS_DIR, 'fonts.css'), 'utf8');
const FONTS = Object.values(FONT_CATALOGUE).flat();
type GithubFamily = FontFamily & { source: Extract<FontSource, { provider: 'github' }> };
const GITHUB = FONTS.filter((f): f is GithubFamily => f.source.provider === 'github');

/** The licence text file a family ships beside its woff2 files. */
function licenseFile(f: FontFamily): string {
    return f.source.provider === 'google' ? 'OFL.txt' : 'LICENSE.txt';
}

interface Face { family: string; style: string; weight: string; url: string; unicodeRange: string }

function faces(css: string): Face[] {
    return [...css.matchAll(/@font-face\s*\{([^}]*)\}/g)].map((m) => {
        const prop = (name: string) => m[1].match(new RegExp(`${name}:\\s*([^;]+);`))?.[1].trim() ?? '';
        return {
            family: prop('font-family').replace(/^'|'$/g, ''),
            style: prop('font-style'),
            weight: prop('font-weight'),
            url: prop('src').match(/url\(([^)]+)\)/)?.[1] ?? '',
            unicodeRange: prop('unicode-range'),
        };
    });
}

const FACES = faces(FONTS_CSS);

/** Whether the face's weight (one value or a variable range) covers `w`. */
function covers(face: Face, w: number): boolean {
    const [lo, hi = lo] = face.weight.split(/\s+/).map(Number);
    return lo <= w && w <= hi;
}

describe('the bundled font catalogue', () => {
    it('offers seven ui, twelve prose and five mono families with their licences and distinct slugs (S03)', () => {
        expect([FONT_CATALOGUE.ui.length, FONT_CATALOGUE.prose.length, FONT_CATALOGUE.mono.length]).toEqual([7, 12, 5]);
        expect(new Set(FONTS.map((f) => f.slug)).size).toBe(FONTS.length);
        expect(new Set(FONTS.map((f) => f.license))).toEqual(new Set([
            'OFL-1.1',
            'Bitstream-Charter',
            'OFL-1.1 OR LPPL-1.3c OR AGPL-3.0-only WITH LicenseRef-URW-font-exception',
        ]));
        for (const f of FONTS) {
            if (f.source.provider === 'google') expect(f.license).toBe('OFL-1.1');
            expect(f.slug).toBe(fontSlug(f.family));
            expect(familyForStack(stackFor(f.family)!)).toBe(f);
        }
        expect(FONT_CATALOGUE.prose.find((f) => f.family === 'Cartisse')?.license).toBe('Bitstream-Charter');
    });

    it('pins the ebook-fonts core families to one commit, four static styles each (S03)', () => {
        expect(GITHUB.map((f) => f.label)).toEqual([
            'Jost', 'Legible Next', 'Libron', 'Sourcerer', 'Cartisse', 'Garamond', 'Bitter', 'Palatium',
        ]);
        expect(EBOOK_FONTS_REF).toMatch(/^[0-9a-f]{40}$/);
        for (const f of GITHUB) {
            expect(f.source.ref).toBe(EBOOK_FONTS_REF);
            expect(f.source.dir).toBe('fonts/core');
            expect(f.weights).toEqual([400, 700]);
            expect(f.italic).toBe(true);
            expect(f.source.origin.licenseText.length).toBeGreaterThan(0);
            for (const url of f.source.origin.licenseText) expect(url).toMatch(/^https:\/\/raw\.githubusercontent\.com\/[^/]+\/[^/]+\/[0-9a-f]{40}\//);
            expect(f.stack.endsWith(f.role === 'ui' ? 'sans-serif' : ', serif'), f.family).toBe(true);
        }
    });

    it.each(GITHUB.map((f) => [f.family, f] as const))('%s has its four woff2 files on disk and four @font-face rules (S03)', (_, f) => {
        const files = readdirSync(path.join(FONTS_DIR, f.slug));
        const woff2 = ['normal-400', 'italic-400', 'normal-700', 'italic-700'].map((style) => `${f.slug}-${style}.woff2`);
        expect(files.sort()).toEqual(['LICENSE.txt', ...woff2].sort());
        const own = FACES.filter((face) => face.family === f.family);
        expect(own.map((face) => face.url).sort()).toEqual(woff2.map((name) => `${f.slug}/${name}`).sort());
        for (const face of own) expect(face.url).toBe(`${f.slug}/${f.slug}-${face.style}-${face.weight}.woff2`);

        const license = readFileSync(path.join(FONTS_DIR, f.slug, 'LICENSE.txt'), 'utf8');
        expect(license).toContain(`License: ${f.license} (`);
        expect(license).toContain(`commit ${f.source.ref}, fonts/core/`);
        for (const url of f.source.origin.licenseText) expect(license).toContain(url);
        expect(license).toContain(f.license === 'Bitstream-Charter'
            ? 'BITSTREAM CHARTER is a registered trademark of Bitstream Inc.'
            : 'SIL OPEN FONT LICENSE Version 1.1');
    });

    it('bundles no modified face under a name its upstream licence reserves, so Charis is the Google original (S03)', () => {
        for (const f of GITHUB) {
            const license = readFileSync(path.join(FONTS_DIR, f.slug, 'LICENSE.txt'), 'utf8');
            const upstream = license.slice(license.indexOf('-'.repeat(79)));
            const reserved = [...upstream.matchAll(/Reserved Font Names? ((?:"[^"]+"(?:,? and |, )?)+)/g)]
                .flatMap((m) => [...m[1].matchAll(/"([^"]+)"/g)].map((n) => n[1]));
            for (const name of reserved) expect(f.family, `${f.family} reserves "${name}"`).not.toContain(name);
        }
        expect(GITHUB.some((f) => f.family.includes('Charis'))).toBe(false);
        expect(FONT_CATALOGUE.prose.find((f) => f.family === 'Charis SIL'))
            .toMatchObject({ source: { provider: 'google', family: 'Charis SIL' }, weights: [400, 700], italic: true });
    });

    it('writes an origin note after the font\'s own licence string and into THIRD_PARTY_LICENSES (S03)', () => {
        const legible = GITHUB.find((f) => f.family === 'NV Legible Next')!;
        expect(legible.source.origin.note).toContain('SIL Open Font License 1.1');
        const flat = (text: string) => text.replace(/\s+/g, ' ');
        const license = flat(readFileSync(path.join(FONTS_DIR, legible.slug, 'LICENSE.txt'), 'utf8'));
        const note = license.indexOf(legible.source.origin.note!);
        expect(note).toBeGreaterThan(license.indexOf('(name ID 13)'));
        expect(note).toBeLessThan(license.indexOf('(name ID 14)'));
        const licenses = flat(readFileSync(path.join(FRONTEND, '..', 'THIRD_PARTY_LICENSES'), 'utf8'));
        expect(licenses).toContain(`Note: ${legible.source.origin.note}`);
        for (const f of GITHUB.filter((g) => !g.source.origin.note)) {
            expect(readFileSync(path.join(FONTS_DIR, f.slug, 'LICENSE.txt'), 'utf8')).not.toContain('\n\nNote:');
        }
    });

    it.each(FONTS.map((f) => [f.family, f] as const))('%s has woff2 files and its licence text on disk (S03)', (_, f) => {
        const dir = path.join(FONTS_DIR, f.slug);
        const files = readdirSync(dir);
        expect(files).toContain(licenseFile(f));
        expect(files.filter((n) => n.endsWith('.woff2')).length).toBeGreaterThan(0);
    });

    it.each(FONTS.map((f) => [f.family, f] as const))('%s has an @font-face for every weight and style it offers (S03)', (_, f) => {
        const own = FACES.filter((face) => face.family === f.family);
        const styles = f.italic ? ['normal', 'italic'] : ['normal'];
        for (const style of styles) {
            for (const w of f.weights) {
                const matching = own.filter((face) => face.style === style && covers(face, w));
                // Google: latin and latin-ext, each its own file; github: one subset file per style
                expect(matching.length, `${f.family} ${style} ${w}`).toBe(f.source.provider === 'google' ? 2 : 1);
            }
        }
    });

    it('declares only files that exist, each swap-displayed and subset by unicode-range (S03)', () => {
        expect(FACES.length).toBeGreaterThan(0);
        const families = new Set(FONTS.map((f) => f.family));
        for (const face of FACES) {
            expect(families.has(face.family), face.family).toBe(true);
            expect(face.url).toMatch(/^[a-z0-9-]+\/[a-z0-9-]+\.woff2$/);
            expect(existsSync(path.join(FONTS_DIR, face.url)), face.url).toBe(true);
            expect(face.unicodeRange).toMatch(/^U\+/);
        }
        expect(FONTS_CSS.match(/font-display: swap;/g)).toHaveLength(FACES.length);
    });

    it('ships every woff2 it declares and nothing else, within the size budget (S03)', () => {
        const declared = new Set(FACES.map((face) => face.url));
        let bytes = 0;
        for (const f of FONTS) {
            for (const name of readdirSync(path.join(FONTS_DIR, f.slug))) {
                if (name.endsWith('.woff2')) expect(declared.has(`${f.slug}/${name}`), name).toBe(true);
                bytes += statSync(path.join(FONTS_DIR, f.slug, name)).size;
            }
        }
        const dirs = readdirSync(FONTS_DIR).filter((n) => statSync(path.join(FONTS_DIR, n)).isDirectory());
        expect(dirs.sort()).toEqual(FONTS.map((f) => f.slug).sort());
        expect(bytes).toBeLessThan(6 * 1024 * 1024);
    });

    it('names every family in THIRD_PARTY_LICENSES (S03)', () => {
        const licenses = readFileSync(path.join(FRONTEND, '..', 'THIRD_PARTY_LICENSES'), 'utf8');
        const section = licenses.slice(licenses.indexOf('BUNDLED FONTS'));
        for (const f of FONTS) {
            expect(section).toContain(`${f.family}\n  License: ${f.license} (`);
            expect(section).toContain(`frontend/public/fonts/${f.slug}/ (the full licence is ${licenseFile(f)} beside them)`);
            if (f.source.provider === 'github') expect(section).toContain(`commit ${f.source.ref})`);
        }
    });

    it('links fonts.css before any other stylesheet in index.html and sandbox.html (S03)', () => {
        for (const [file, href] of [['index.html', '/fonts/fonts.css'], ['public/sandbox.html', 'fonts/fonts.css']]) {
            const html = readFileSync(path.join(FRONTEND, file), 'utf8');
            const link = html.indexOf(`<link rel="stylesheet" href="${href}"`);
            expect(link, file).toBeGreaterThan(0);
            expect(html.search(/<style\b|rel="stylesheet"/), file).toBe(link + '<link '.length);
        }
    });
});
