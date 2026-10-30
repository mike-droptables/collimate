// One folder's entries for the Files panel's Browse/Create mode
// (lib/folderBrowse): direct children only, folders first, by name;
// .collkeep markers keep a folder but are never entries; the query
// filters names.

import { describe, expect, it } from 'vitest';
import { fileFixture } from '@/test/fileFixtures';
import { folderEntries, parentFolder, ROOT_LABEL } from '@/lib/folderBrowse';

const FILES = [
    'readme.md',
    'b.txt',
    'docs/guide.md',
    'docs/api/index.md',
    'docs/zeta.txt',
    'assets/.collkeep',
    'Alpha/one.md',
    '.collkeep',
].map(name => fileFixture(name));

const listed = (folder: string, query = '') => folderEntries(FILES, folder, query).map(e => `${e.kind}:${e.path}`);

describe('folderEntries', () => {
    it('lists the root: its folders then its files, each by name; a marker is never an entry', () => {
        expect(listed('')).toEqual(['folder:Alpha', 'folder:assets', 'folder:docs', 'file:b.txt', 'file:readme.md']);
    });

    it('lists a subfolder\'s direct children with cell-relative paths, and the file each file entry names', () => {
        expect(listed('docs')).toEqual(['folder:docs/api', 'file:docs/guide.md', 'file:docs/zeta.txt']);
        const guide = folderEntries(FILES, 'docs', '').find(e => e.name === 'guide.md')!;
        expect(guide.file).toBe(FILES[2]);
        expect(folderEntries(FILES, 'docs', '').find(e => e.kind === 'folder')!.file).toBeUndefined();
    });

    it('a folder held only by its marker is listed and is empty inside', () => {
        expect(listed('')).toContain('folder:assets');
        expect(listed('assets')).toEqual([]);
    });

    it('does not take a sibling sharing the folder\'s name as a prefix', () => {
        const files = ['doc/a.md', 'docs/b.md'].map(name => fileFixture(name));
        expect(folderEntries(files, 'doc', '').map(e => e.path)).toEqual(['doc/a.md']);
    });

    it('filters by the entry name: a case-insensitive substring or a glob on the name, folders included', () => {
        expect(listed('', 'A')).toEqual(['folder:Alpha', 'folder:assets', 'file:readme.md']);
        expect(listed('', '*.md')).toEqual(['file:readme.md']);
        // The folder's path is not searched, only the names in it.
        expect(listed('docs', 'docs')).toEqual([]);
        expect(listed('docs', 'g*')).toEqual(['file:docs/guide.md']);
    });
});

describe('parentFolder', () => {
    it('is the folder above, and the root at and under the root', () => {
        expect(parentFolder('docs/api')).toBe('docs');
        expect(parentFolder('docs')).toBe('');
        expect(parentFolder('')).toBe('');
        expect(ROOT_LABEL).toBe('root');
    });
});
