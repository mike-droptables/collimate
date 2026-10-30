import { fileFixture } from '@/test/fileFixtures';
// The Files panel's matching and ordering (lib/fileSearch): globs against
// the basename or, with a `/`, the whole path; substrings otherwise; Recent
// newest first, ties by path; the tree's folders-first row order.

import { describe, it, expect } from 'vitest';
import {
    compareRecency,
    compileQuery,
    globToRegExp,
    treeNodeOrder,
} from '@/lib/fileSearch';
import { buildFileTree } from '@/lib/fileTree';
import type { VirtualFile } from '@/lib/types';

const vf = (name: string, timestamp = '2026-01-01T00:00:00Z'): VirtualFile => (fileFixture(name, '', {language: 'plaintext', timestamp}));

const matchesQuery = (path: string, query: string) => compileQuery(query)(path);

describe('matchesQuery', () => {
    it('an empty or blank query matches everything', () => {
        expect(matchesQuery('a/b.ts', '')).toBe(true);
        expect(matchesQuery('a/b.ts', '   ')).toBe(true);
    });

    it('*.md is the markdown files anywhere (the basename)', () => {
        expect(matchesQuery('README.md', '*.md')).toBe(true);
        expect(matchesQuery('docs/guide/intro.md', '*.md')).toBe(true);
        expect(matchesQuery('docs/guide/intro.mdx', '*.md')).toBe(false);
        expect(matchesQuery('md/notes.txt', '*.md')).toBe(false);
        // The basename only: a folder name never matches a slash-free glob.
        expect(matchesQuery('src.d/a.ts', 'src*')).toBe(false);
        expect(matchesQuery('src.ts', 'src*')).toBe(true);
    });

    it('a glob with a / matches the whole path from the root, * within one segment', () => {
        expect(matchesQuery('src/a.ts', 'src/*.ts')).toBe(true);
        expect(matchesQuery('src/lib/a.ts', 'src/*.ts')).toBe(false);
        expect(matchesQuery('app/src/a.ts', 'src/*.ts')).toBe(false);
        expect(matchesQuery('src/a.ts', '/src/*.ts')).toBe(true);
    });

    it('** spans segments, and **/ also matches no folder', () => {
        expect(matchesQuery('src/lib/deep/a.ts', 'src/**/*.ts')).toBe(true);
        expect(matchesQuery('src/a.ts', 'src/**/*.ts')).toBe(true);
        expect(matchesQuery('lib/a.ts', 'src/**/*.ts')).toBe(false);
        expect(matchesQuery('src/x/y/z.md', 'src/**')).toBe(true);
        expect(matchesQuery('a/b/c.test.ts', '**/*.test.ts')).toBe(true);
        expect(matchesQuery('c.test.ts', '**/*.test.ts')).toBe(true);
    });

    it('? is one character of a segment', () => {
        expect(matchesQuery('a/v1.txt', 'v?.txt')).toBe(true);
        expect(matchesQuery('a/v10.txt', 'v?.txt')).toBe(false);
        expect(globToRegExp('a?b').test('a/b')).toBe(false);
    });

    it('globs are case-insensitive and everything else in them is literal', () => {
        expect(matchesQuery('docs/README.MD', '*.md')).toBe(true);
        expect(matchesQuery('SRC/A.TS', 'src/*.ts')).toBe(true);
        expect(matchesQuery('a+b(1).md', 'a+b(1).*')).toBe(true);
        expect(matchesQuery('aab1.md', 'a+b(1).*')).toBe(false);
    });

    it('a plain query is a case-insensitive substring of the full path', () => {
        expect(matchesQuery('src/components/Button.tsx', 'button')).toBe(true);
        expect(matchesQuery('src/components/Button.tsx', 'COMPONENTS/b')).toBe(true);
        expect(matchesQuery('src/components/Button.tsx', 'lib')).toBe(false);
        // A dot is literal, not a regex wildcard.
        expect(matchesQuery('src/axmd', '.md')).toBe(false);
    });
});

describe('treeNodeOrder', () => {
    const tree = buildFileTree([
        vf('b.md', '2026-01-01T00:00:00Z'),
        vf('a.md', '2026-01-01T00:00:00Z'),
        vf('z.md', '2026-03-01T00:00:00Z'),
        vf('m.md', '2026-02-01T00:00:00Z'),
        vf('old.md', 'not a date'),
        vf('dir/x.md'),
        vf('another/y.md'),
    ]);
    it('folders first by name, then files by name', () => {
        expect(Object.values(tree).sort(treeNodeOrder).map(n => n.name)).toEqual(['another', 'dir', 'a.md', 'b.md', 'm.md', 'old.md', 'z.md']);
    });

    it('Recent puts files newest first, ties by path, unknown times last', () => {
        const files = [vf('b.md'), vf('a.md'), vf('z.md', '2026-03-01T00:00:00Z'), vf('m.md', '2026-02-01T00:00:00Z'), vf('old.md', 'not a date')];
        expect(files.map(f => ({ path: f.name, timestamp: f.timestamp })).sort(compareRecency).map(f => f.path))
            .toEqual(['z.md', 'm.md', 'a.md', 'b.md', 'old.md']);
    });

    it('two unknown times fall back to the path', () => {
        expect(compareRecency({ path: 'x', timestamp: undefined }, { path: 'y', timestamp: undefined })).toBeLessThan(0);
    });
});
