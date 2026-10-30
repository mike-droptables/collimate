import { fileFixture } from '@/test/fileFixtures';
// Create and Rename refuse a path that would clash with an existing file or
// folder (two entries at one path lose one of them) and names a folder can't
// take either ('.', '..', a slash, the `.collkeep` marker).
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

const { openAlert } = vi.hoisted(() => ({
    openAlert: vi.fn(async (_opts: { title: string; body: string }) => {}),
}));
vi.mock('@/lib/api', () => ({ cellFileUrl: vi.fn() }));
vi.mock('@/stores/useCollimateStore', () => ({
    useCollimateStore: { getState: () => ({ openAlert, openConfirm: vi.fn(async () => true) }) },
}));

import { useFileOperations } from './useFileOperations';
import { buildFileTree, FOLDER_KEEP_FILE, type TreeNode } from '@/lib/fileTree';
import type { UpdateFiles } from '@/lib/fileTransfer';
import type { VirtualFile } from '@/lib/types';

const vf = (name: string): VirtualFile => (fileFixture(name, `c:${name}`, {language: 'plaintext', timestamp: 't'}));
const FILES = [vf('a.md'), vf('b.md'), vf('src/x.md'), vf('lib/y.md')];

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
    const { result } = renderHook(() => useFileOperations({ files: FILES, onUpdateFiles, readOnly: false, channel: 'ch', cellId:'cell' }));
    return { ops: result.current, onUpdateFiles: commit };
};

beforeEach(() => { vi.clearAllMocks(); });

describe('useFileOperations — create validation', () => {
    it('refuses a file where a folder is, a file under a file, dot segments and the marker name', async () => {
        const { ops, onUpdateFiles } = mount();
        for (const input of ['src', 'a.md/b', 'x/../y', './z', `d/${FOLDER_KEEP_FILE}`]) {
            expect((await ops.handleCreateFileAtPath(input)).ok).toBe(false);
        }
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });

    it('still creates an ordinary nested file', async () => {
        const { ops, onUpdateFiles } = mount();
        expect(await ops.handleCreateFileAtPath('src/new.md')).toEqual({ ok: true, path: 'src/new.md' });
        expect(onUpdateFiles).toHaveBeenCalledTimes(1);
    });
});

describe('useFileOperations — rename validation', () => {
    it('refuses a clash or an invalid name with an alert and writes nothing', () => {
        const { ops, onUpdateFiles } = mount();
        act(() => { void ops.handleRename(nodeAt('a.md'), 'b.md'); });      // existing sibling file
        act(() => { void ops.handleRename(nodeAt('a.md'), 'src'); });       // existing folder
        act(() => { void ops.handleRename(nodeAt('src'), 'lib'); });        // folder onto folder
        act(() => { void ops.handleRename(nodeAt('a.md'), 'dir/a.md'); });  // a slash
        act(() => { void ops.handleRename(nodeAt('a.md'), '..'); });
        act(() => { void ops.handleRename(nodeAt('a.md'), FOLDER_KEEP_FILE); });
        expect(onUpdateFiles).not.toHaveBeenCalled();
        expect(openAlert).toHaveBeenCalledTimes(6);
    });

    it('an ordinary rename still goes through', async () => {
        const { ops, onUpdateFiles } = mount();
        await act(async () => { await ops.handleRename(nodeAt('src'), 'source'); });
        const names = onUpdateFiles.mock.calls[0][0].map(f => f.name);
        expect(names).toContain('source/x.md');
        expect(names).not.toContain('src/x.md');
    });
});
