import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// The app side's exit greps as tests: the retired names, the files each scan
// reads and the examples each one must hit and spare live in
// retired-names.json beside this file, which every scan skips by path and
// nothing else, so this file is scanned too and no name is assembled from
// parts to hide it.

interface FileSet { about: string; roots: string[] }
interface Scan {
    id: string; about: string; files: string; ignore_case?: boolean; word_start?: boolean;
    names?: string[]; patterns?: string[]; matches?: string[]; spares?: string[];
}
interface RetiredNames { file_sets: Record<string, FileSet>; skipped: string[]; text: string; scans: Scan[] }

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DATA = path.join(HERE, 'retired-names.json');
const REPO = path.resolve(HERE, '../../../..');
const DATA_FILE = path.relative(REPO, DATA).split(path.sep).join('/');
const retired: RetiredNames = JSON.parse(readFileSync(DATA, 'utf8'));
const SKIPPED = retired.skipped.map((source) => new RegExp(source));
const TEXT = new RegExp(retired.text);

const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');
function pattern(scan: Scan, flags = 'g'): RegExp {
    const parts = [...(scan.names ?? []).map((name) => (scan.word_start ? '\\b' : '') + escape(name)), ...(scan.patterns ?? [])];
    return new RegExp(parts.join('|'), flags + (scan.ignore_case ? 'i' : ''));
}

/** Tracked and untracked-but-not-ignored files of a file set, as they are on
 *  disk now (a file deleted from the tree is gone from the scan), never the
 *  data file. */
function scanned(setName: string): string[] {
    const listing = execFileSync('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', ...retired.file_sets[setName].roots], {
        cwd: REPO, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
    });
    return [...new Set(listing.split('\0').filter(Boolean))]
        .filter((name) => TEXT.test(name) || name === 'README.md')
        .filter((name) => name !== DATA_FILE && !SKIPPED.some((re) => re.test(name)))
        .filter((name) => existsSync(path.join(REPO, name)));
}

function offendersOf(expression: RegExp, files: readonly string[]): string[] {
    return files.flatMap((name) => {
        const lines = readFileSync(path.join(REPO, name), 'utf8').split('\n');
        return lines.flatMap((line, i) => {
            const hits = line.match(expression);
            return hits ? [`${name}:${i + 1}: ${[...new Set(hits)].join(', ')}`] : [];
        });
    });
}

// How many names and patterns each scan holds: a name leaves the data file
// only with this table.
const SIZES: Record<string, [number, number]> = {
    'TR-14': [19, 0], 'GN-02': [0, 6], 'capabilities-stack': [11, 0], 'AN-01': [27, 0], 'P5-renderer-hosts': [6, 0],
    'SDK-10': [7, 11],
};

describe('retired names (retired-names.json)', () => {
    it('holds the scans it held, each reading a declared file set', () => {
        expect(Object.fromEntries(retired.scans.map((scan) => [scan.id, [scan.names?.length ?? 0, scan.patterns?.length ?? 0]]))).toEqual(SIZES);
        for (const scan of retired.scans) expect(retired.file_sets).toHaveProperty(scan.files);
    });

    it('every file set reaches its roots and skips only the data file and the skipped paths', () => {
        for (const [setName, files] of Object.entries({
            app: ['frontend/src/lib/__tests__/retiredSurfaces.test.ts', 'api/main.py', 'README.md'],
            'app-and-frontend-tests': ['frontend/vitest.config.ts'],
            'app-and-sandbox': ['frontend/public/sandbox.html'],
            'app-code': ['frontend/public/sandbox.html', 'frontend/tests/renderers/runtime.spec.ts', 'api/process_manager/ipc.py'],
        })) {
            const set = scanned(setName);
            expect(set.length).toBeGreaterThan(500);
            for (const file of files) expect(set).toContain(file);
            expect(set).not.toContain(DATA_FILE);
        }
    });

    for (const scan of retired.scans) {
        describe(`${scan.id}: ${scan.about.split(':')[0]}`, () => {
            it('hits each of its names and examples and spares its live neighbours', () => {
                const once = pattern(scan, '');
                for (const name of scan.names ?? []) {
                    expect(`x ${name} y`).toMatch(once);
                    if (scan.ignore_case) expect(`x ${name.toUpperCase()} y`).toMatch(once);
                }
                for (const source of scan.patterns ?? []) {
                    const single = new RegExp(source, scan.ignore_case ? 'i' : '');
                    expect((scan.matches ?? []).some((example) => single.test(example))).toBe(true);
                }
                for (const example of scan.matches ?? []) expect(example).toMatch(once);
                for (const kept of scan.spares ?? []) expect(kept).not.toMatch(once);
            });

            it('no scanned file names one', () => {
                expect(offendersOf(pattern(scan), scanned(scan.files))).toEqual([]);
            });
        });
    }
});
