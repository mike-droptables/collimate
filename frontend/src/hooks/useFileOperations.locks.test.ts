import { fileFixture } from '@/test/fileFixtures';
// A live run's files (everything under its `_runs/<label>/` folder) are its
// lock: the user never deletes or renames them, nor a folder holding one,
// and never creates, drops or duplicates anything INTO `_runs/` — only a run
// writes there. An in-cell Duplicate of a run's file is allowed and lands
// outside `_runs/`.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

const { openAlert, openConfirm } = vi.hoisted(() => ({
    openAlert: vi.fn(async (_opts: { title: string; body: string }) => {}),
    openConfirm: vi.fn(async () => true),
}));
vi.mock('@/stores/useCollimateStore', () => ({
    useCollimateStore: { getState: () => ({ openAlert, openConfirm }) },
}));

import { useFileOperations } from './useFileOperations';
import { buildFileTree, type TreeNode } from '@/lib/fileTree';
import { captureDrop } from '@/lib/dropEntries';
import type { UpdateFiles } from '@/lib/fileTransfer';
import type { RunRecord, VirtualFile } from '@/lib/types';

const vf = (name: string): VirtualFile => (fileFixture(name, `c:${name}`, {language: 'plaintext', timestamp: 't'}));
const RUN: RunRecord = {
    runId: 'run-1', cellId: 'cell-1', channel: 'ch', runnerId: 'r', runnerName: 'R',
    generatorName: 'g', label: 'r-11111111', folder: '_runs/r-11111111',
};
const LOCKED = `${RUN.folder}/chat/conversation.chat`;
const lockOwnerOf = (path: string) => (path.startsWith(`${RUN.folder}/`) ? RUN : null);
const FILES = [vf('notes.md'), vf(LOCKED)];

const nodeAt = (path: string): TreeNode => {
    let level = buildFileTree(FILES);
    const parts = path.split('/');
    for (let i = 0; i < parts.length - 1; i++) level = level[parts[i]].children;
    return level[parts[parts.length - 1]];
};

/** The cell's writer as CellEditor runs it: each update is applied to the
 *  cell's list, and `commit` sees only a list the update changed. */
const mount = () => {
    const commit = vi.fn<(files: VirtualFile[]) => void>();
    const onUpdateFiles: UpdateFiles = update => { const next = update(FILES); if (next !== FILES) commit(next); };
    const { result } = renderHook(() => useFileOperations({ files: FILES, onUpdateFiles, readOnly: false, lockOwnerOf, channel: 'ch', cellId: 'cell-1' }));
    return { ops: result.current, onUpdateFiles: commit };
};

beforeEach(() => { vi.clearAllMocks(); });

describe('useFileOperations — run locks', () => {
    it('a locked file, or a folder holding one, is never deleted (no confirm, no write)', async () => {
        const { ops, onUpdateFiles } = mount();
        for (const path of [LOCKED, `${RUN.folder}/chat`, RUN.folder, '_runs']) {
            await act(async () => { await ops.handleDelete(nodeAt(path)); });
        }
        expect(openConfirm).not.toHaveBeenCalled();
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });

    it('an ordinary file still deletes', async () => {
        const { ops, onUpdateFiles } = mount();
        await act(async () => { await ops.handleDelete(nodeAt('notes.md')); });
        expect(onUpdateFiles).toHaveBeenCalledTimes(1);
    });

    it('a delete lands on the cell as it is after the confirm, keeping a file written meanwhile (F0823)', async () => {
        let cell = FILES;
        const commit = vi.fn<(files: VirtualFile[]) => void>();
        const onUpdateFiles: UpdateFiles = update => commit(update(cell));
        openConfirm.mockImplementationOnce(async () => { cell = [...FILES, vf('out/new.md')]; return true; });
        const { result } = renderHook(() => useFileOperations({ files: FILES, onUpdateFiles, readOnly: false, lockOwnerOf, channel: 'ch', cellId: 'cell-1' }));
        await act(async () => { await result.current.handleDelete(nodeAt('notes.md')); });
        expect(commit.mock.calls[0][0].map(f => f.name)).toEqual([LOCKED, 'out/new.md']);
    });

    it('a folder delete removes only the files its dialog counted; one written into the folder meanwhile stays (F0823)', async () => {
        const start = [vf('notes.md'), vf('docs/a.md'), vf('docs/.collkeep')];
        let cell = start;
        const commit = vi.fn<(files: VirtualFile[]) => void>();
        const onUpdateFiles: UpdateFiles = update => commit(update(cell));
        openConfirm.mockImplementationOnce(async () => { cell = [...start, vf('docs/run-out.md')]; return true; });
        const { result } = renderHook(() => useFileOperations({ files: start, onUpdateFiles, readOnly: false, lockOwnerOf }));
        const docs = buildFileTree(start).docs;
        await act(async () => { await result.current.handleDelete(docs); });
        expect(openConfirm).toHaveBeenCalledWith(expect.objectContaining({ body: 'Delete `docs`? 1 file will be removed.' }));
        expect(commit.mock.calls[0][0].map(f => f.name)).toEqual(['notes.md', 'docs/run-out.md']);
    });

    it('a locked file or folder is never renamed; nothing is renamed INTO `_runs`', async () => {
        const { ops, onUpdateFiles } = mount();
        await act(async () => { await ops.handleRename(nodeAt(LOCKED), 'other.chat'); });
        await act(async () => { await ops.handleRename(nodeAt('_runs'), 'runs'); });
        await act(async () => { await ops.handleRename(nodeAt('notes.md'), '_runs'); });
        expect(onUpdateFiles).not.toHaveBeenCalled();
        await act(async () => { await ops.handleRename(nodeAt('notes.md'), 'renamed.md'); });
        expect(onUpdateFiles).toHaveBeenCalledTimes(1);
    });

    it('no new file or folder under `_runs/`', async () => {
        const { ops, onUpdateFiles } = mount();
        const file = await ops.handleCreateFileAtPath('x.md', RUN.folder);
        expect(file.ok).toBe(false);
        const typed = await ops.handleCreateFileAtPath('_runs/r-11111111/y.md');
        expect(typed.ok).toBe(false);
        const folder = await ops.handleCreateFolder('', '_runs');
        expect(folder.ok).toBe(false);
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });

    it('Duplicate of a run\'s file lands outside `_runs/`, the original untouched', async () => {
        const { ops, onUpdateFiles } = mount();
        await act(async () => { await ops.handleDuplicate(nodeAt(LOCKED)); });
        const names = onUpdateFiles.mock.calls[0][0].map(f => f.name);
        expect(names).toContain(LOCKED);
        expect(names).toContain('chat/conversation.chat');
    });

    it('an OS drop into `_runs/` is refused with an alert', async () => {
        const { ops, onUpdateFiles } = mount();
        const capture = captureDrop({ files: [new File(['x'], 'x.txt')], items: [] } as unknown as DataTransfer);
        await act(async () => { await ops.handleDropFiles(capture, RUN.folder); });
        expect(openAlert).toHaveBeenCalledWith(expect.objectContaining({ title: 'Drop refused' }));
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });
});
