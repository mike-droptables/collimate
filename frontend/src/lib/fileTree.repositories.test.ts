import { describe, expect, it } from 'vitest';
import { fileFixture } from '@/test/fileFixtures';
import { buildFileTree } from './fileTree';
import { folderEntries } from './folderBrowse';

// A repository's folder always has a row for its chip: one no cell file
// makes is synthesized, read-only (repositoryOnly).
describe('rows for repositories with no file', () => {
    const files = [fileFixture('app/a.txt', 'a'), fileFixture('readme.md', 'r')];

    it('buildFileTree adds a read-only folder (and its file-less parents) for each such repository', () => {
        const tree = buildFileTree(files, ['app', 'empty', 'deep/inside/repo']);
        expect(tree.app.repositoryOnly).toBeUndefined();
        expect(tree.empty).toMatchObject({ type: 'folder', path: 'empty', repositoryOnly: true, children: {} });
        expect(tree.deep).toMatchObject({ path: 'deep', repositoryOnly: true });
        expect(tree.deep.children.inside.children.repo).toMatchObject({ path: 'deep/inside/repo', repositoryOnly: true });
    });

    it('a repository folder under a real one is synthesized inside it', () => {
        const tree = buildFileTree(files, ['app/vendor']);
        expect(tree.app.repositoryOnly).toBeUndefined();
        expect(tree.app.children.vendor).toMatchObject({ path: 'app/vendor', repositoryOnly: true });
        expect(tree.app.children['a.txt'].type).toBe('file');
    });

    it('a file at the path leaves the repository without a row, and .git paths stay hidden', () => {
        const tree = buildFileTree([fileFixture('x', 'file'), fileFixture('.git/HEAD', 'h')], ['x/y', 'x']);
        expect(tree.x.type).toBe('file');
        expect(tree['.git']).toBeUndefined();
    });

    it('folderEntries lists a file-less repository folder among the subfolders', () => {
        expect(folderEntries(files, '', '', ['empty', 'app', 'deep/repo'])).toEqual([
            { path: 'app', name: 'app', kind: 'folder' },
            { path: 'deep', name: 'deep', kind: 'folder', repositoryOnly: true },
            { path: 'empty', name: 'empty', kind: 'folder', repositoryOnly: true },
            { path: 'readme.md', name: 'readme.md', kind: 'file', file: files[1] },
        ]);
        expect(folderEntries(files, 'deep', '', ['deep/repo'])).toEqual([{ path: 'deep/repo', name: 'repo', kind: 'folder', repositoryOnly: true }]);
        expect(folderEntries(files, 'app', '', ['app'])).toEqual([{ path: 'app/a.txt', name: 'a.txt', kind: 'file', file: files[0] }]);
    });
});
