import type { FileDescriptor } from '@/lib/fileProtocol';
import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type { UpdateFiles } from '@/lib/fileTransfer';
import type { VirtualFile } from '@/lib/types';

const { openAlert, readPinnedText } = vi.hoisted(() => ({
    openAlert: vi.fn(async (_opts: { title: string; body: string }) => {}),
    readPinnedText: vi.fn(async (_ch: string, _cell: string, descriptor: FileDescriptor) => `full body of ${descriptor.path}`),
}));
vi.mock('@/lib/api', () => ({ cellFileUrl: vi.fn() }));
vi.mock('@/lib/fileBodies', async importActual => ({
    ...(await importActual<typeof import('@/lib/fileBodies')>()), readPinnedText,
}));
vi.mock('@/stores/useCollimateStore', () => ({
    useCollimateStore: { getState: () => ({ openAlert, openConfirm: vi.fn(async () => true) }) },
}));
import { useFileOperations } from './useFileOperations';
import { registerCellSaveBarrier } from '@/lib/cellSaveBarrier';
import { buildFileTree, type TreeNode } from '@/lib/fileTree';

const metadataFile = (name: string) => fileFixture(name, `full body of ${name}`, { language: 'markdown' });
const FILES = [metadataFile('big.md'), metadataFile('docs/big.md'),
    fileFixture('docs/img.png', '', { encoding: 'binary', language: 'binary', body_hash: 'a'.repeat(64) })];
const RUN_WRITE = fileFixture('out/run.log', 'run', { file_revision: 2 });
const nodeAt = (path: string): TreeNode => {
    let level = buildFileTree(FILES);
    const parts = path.split('/');
    for (const part of parts.slice(0, -1)) level = level[part].children;
    return level[parts.at(-1)!];
};
const mount = (cellNow: () => VirtualFile[] = () => FILES) => {
    const commit = vi.fn<(files: VirtualFile[], options?: Parameters<UpdateFiles>[1]) => void>();
    const onUpdateFiles: UpdateFiles = (update, options) => {
        const current = cellNow();
        const next = update(current);
        if (next !== current) commit(next, options);
    };
    const { result } = renderHook(() => useFileOperations({
        files: FILES, onUpdateFiles, readOnly: false, channel: 'ch', cellId: 'cell-1',
    }));
    return { ops: result.current, commit };
};
const byName = (files: VirtualFile[], name: string) => files.find(f => f.name === name);
beforeEach(() => {
    vi.clearAllMocks();
    readPinnedText.mockReset();
    readPinnedText.mockImplementation(async (_ch, _cell, descriptor) => `full body of ${descriptor.path}`);
});

describe('metadata rename forwards explicit intent to the file command owner', () => {
    it('keeps the accepted descriptor without fetching bytes', async () => {
        const { ops, commit } = mount();
        await act(async () => { await ops.handleRename(nodeAt('big.md'), 'final.md'); });
        const [files, options] = commit.mock.calls[0];
        expect(byName(files, 'final.md')).toEqual({ ...FILES[0], name: 'final.md' });
        expect(byName(files, 'big.md')).toBeUndefined();
        expect(options).toEqual({ renames: [{ source: 'big.md', target: 'final.md' }] });
        expect(readPinnedText).not.toHaveBeenCalled();
    });
    it('forwards folder members with unchanged text and binary hashes', async () => {
        const { ops, commit } = mount();
        await act(async () => { await ops.handleRename(nodeAt('docs'), 'papers'); });
        const [files, options] = commit.mock.calls[0];
        expect(byName(files, 'papers/big.md')).toEqual({ ...FILES[1], name: 'papers/big.md' });
        expect(byName(files, 'papers/img.png')).toEqual({ ...FILES[2], name: 'papers/img.png' });
        expect(options?.renames).toEqual([
            { source: 'docs/big.md', target: 'papers/big.md' },
            { source: 'docs/img.png', target: 'papers/img.png' },
        ]);
        expect(readPinnedText).not.toHaveBeenCalled();
    });
    it('uses the latest descriptor and preserves unrelated files', async () => {
        const latest = [fileFixture('big.md', 'new accepted bytes', { file_revision: 2, language: 'markdown' }), ...FILES.slice(1), RUN_WRITE];
        const { ops, commit } = mount(() => latest);
        await act(async () => { await ops.handleRename(nodeAt('big.md'), 'final.md'); });
        expect(byName(commit.mock.calls[0][0], 'final.md')).toEqual({ ...latest[0], name: 'final.md' });
        expect(byName(commit.mock.calls[0][0], RUN_WRITE.name)).toEqual(RUN_WRITE);
    });
    it('refuses a destination occupied before its updater runs', async () => {
        const { ops, commit } = mount(() => [...FILES, fileFixture('final.md', 'theirs')]);
        await expect(ops.handleRename(nodeAt('big.md'), 'final.md')).rejects.toThrow('already exists');
        expect(commit).not.toHaveBeenCalled();
    });
});

describe('duplicate reads its immutable source descriptor', () => {
    it('copies pinned bytes and leaves the source descriptor untouched', async () => {
        const { ops, commit } = mount();
        await act(async () => { await ops.handleDuplicate(nodeAt('big.md')); });
        expect(byName(commit.mock.calls[0][0], 'big (copy).md')).toMatchObject({ content: 'full body of big.md' });
        expect(byName(commit.mock.calls[0][0], 'big.md')).toEqual(FILES[0]);
        expect(readPinnedText).toHaveBeenCalledWith('ch', 'cell-1', expect.objectContaining({
            path: 'big.md', file_revision: FILES[0].file_revision, body_hash: FILES[0].body_hash,
        }), { cache: 'no-store' });
    });
    it('preserves an unrelated publication during the pinned read', async () => {
        let current = FILES;
        readPinnedText.mockImplementationOnce(async () => { current = [...FILES, RUN_WRITE]; return 'full body of big.md'; });
        const { ops, commit } = mount(() => current);
        await act(async () => { await ops.handleDuplicate(nodeAt('big.md')); });
        expect(byName(commit.mock.calls[0][0], RUN_WRITE.name)).toEqual(RUN_WRITE);
    });
    it('refuses stale bytes when an accepted source revision changes during fetch', async () => {
        let current = FILES;
        readPinnedText.mockImplementationOnce(async () => {
            current = [fileFixture('big.md', 'new accepted bytes', { file_revision: 2 }), ...FILES.slice(1)];
            return 'full body of big.md';
        });
        const { ops, commit } = mount(() => current);
        await act(async () => { await ops.handleDuplicate(nodeAt('big.md')); });
        expect(commit).not.toHaveBeenCalled();
        expect(openAlert).toHaveBeenCalledWith(expect.objectContaining({ title: 'Duplicate failed' }));
    });
    it('refuses stale bytes when an editor save advances the descriptor after fetch', async () => {
        let current = FILES;
        const unregister = registerCellSaveBarrier('cell-1', () => {
            current = [fileFixture('big.md', 'saved while copying', { file_revision: 2 }), ...FILES.slice(1)];
        });
        try {
            const { ops, commit } = mount(() => current);
            await act(async () => { await ops.handleDuplicate(nodeAt('big.md')); });
            expect(commit).not.toHaveBeenCalled();
            expect(openAlert).toHaveBeenCalledWith(expect.objectContaining({ title: 'Duplicate failed' }));
        } finally { unregister(); }
    });
    it('reports a pinned read failure without writing a file', async () => {
        readPinnedText.mockRejectedValueOnce(new Error('HTTP 500'));
        const { ops, commit } = mount();
        await act(async () => { await ops.handleDuplicate(nodeAt('big.md')); });
        expect(commit).not.toHaveBeenCalled();
        expect(openAlert).toHaveBeenCalledWith(expect.objectContaining({ title: 'Duplicate failed' }));
    });
});
