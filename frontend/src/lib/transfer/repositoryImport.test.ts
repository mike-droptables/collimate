import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/transfer', async original => ({
    ...await original<typeof import('@/lib/api/transfer')>(),
    uploadRepository: vi.fn(async () => ({ dir: 'proj', token: 'tok-1' })),
}));

import { uploadRepository } from '@/lib/api/transfer';
import { GITFILE_REASON, splitRepositories, uploadFoundRepository } from './repositoryImport';

const file = (path: string) => ({ path, file: new Blob([path]) });

beforeEach(() => vi.clearAllMocks());

// A picked or dropped `.git` never becomes cell files: it is split off by
// the folder it sits in, before the reserved-path rule could drop it.
describe('splitRepositories', () => {
    it('splits each exact .git, files and empty folders, by its folder', () => {
        const split = splitRepositories(
            [file('proj/a.txt'), file('proj/.git/HEAD'), file('proj/.git/objects/pack/p.pack'), file('.git/HEAD'), file('proj/sub/.git/config'), file('proj/.GIT/x')],
            ['proj/.git/refs/tags', 'proj/empty'],
        );
        expect(split.files.map(f => f.path)).toEqual(['proj/a.txt', 'proj/.GIT/x']);
        expect(split.dirs).toEqual(['proj/empty']);
        expect(split.repositories.map(r => ({ dir: r.dir, files: r.files.map(f => f.path), dirs: r.dirs }))).toEqual([
            { dir: 'proj', files: ['proj/.git/HEAD', 'proj/.git/objects/pack/p.pack'], dirs: ['proj/.git/refs/tags'] },
            { dir: '', files: ['.git/HEAD'], dirs: [] },
            { dir: 'proj/sub', files: ['proj/sub/.git/config'], dirs: [] },
        ]);
    });

    it('a .git file (a submodule checkout’s or linked worktree’s gitfile) is left out with its reason, its folder’s files kept', () => {
        const split = splitRepositories([file('p/.git/HEAD'), file('p/sub/.git'), file('p/sub/x.c'), file('.git')]);
        expect(split.repositories.map(r => ({ dir: r.dir, files: r.files.map(f => f.path) }))).toEqual([{ dir: 'p', files: ['p/.git/HEAD'] }]);
        expect(split.gitfiles).toEqual([{ path: 'p/sub/.git', reason: GITFILE_REASON }, { path: '.git', reason: GITFILE_REASON }]);
        expect(split.files.map(f => f.path)).toEqual(['p/sub/x.c']);
    });

    it('a submodule gitdir under modules/ stays its outer repository’s', () => {
        const split = splitRepositories([file('p/.git/modules/s/.git/HEAD'), file('p/.git/HEAD')]);
        expect(split.repositories.map(r => r.dir)).toEqual(['p']);
    });
});

describe('uploadFoundRepository', () => {
    it('zips the .git under its folder and returns the upload’s token', async () => {
        const token = await uploadFoundRepository('ch', { dir: 'proj', files: [file('proj/.git/HEAD')], dirs: ['proj/.git/refs/heads'] });
        expect(token).toBe('tok-1');
        const zip = vi.mocked(uploadRepository).mock.calls[0][1];
        const text = new TextDecoder().decode(new Uint8Array(await zip.arrayBuffer()));
        expect(text).toContain('proj/.git/HEAD');
        expect(text).toContain('proj/.git/refs/heads/');
    });

    it('always writes the folders git needs, which a folder picker drops when they are empty', async () => {
        await uploadFoundRepository('ch', { dir: '', files: [file('.git/HEAD'), file('.git/packed-refs')], dirs: ['.git/refs/tags'] });
        const zip = vi.mocked(uploadRepository).mock.calls[0][1];
        const names = new TextDecoder().decode(new Uint8Array(await zip.arrayBuffer())).match(/\.git\/[\w/-]*\//g) ?? [];
        for (const dir of ['.git/refs/heads/', '.git/refs/tags/', '.git/objects/info/', '.git/objects/pack/']) {
            expect(names.filter(name => name === dir)).toHaveLength(2);
        }
    });
});
