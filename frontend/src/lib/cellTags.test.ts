import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AppState, Cell } from '@/lib/types';
import { useCollimateStore } from '@/stores/useCollimateStore';

vi.mock('@/lib/api/cells', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api/cells')>()),
    patchCellMeta: vi.fn(),
}));

import * as cells from '@/lib/api/cells';
import { currentCellTags, saveCellTags } from './cellTags';

// saveCellTags: the cache first, then the full list through the meta
// PATCH; a refusal puts the prior list back unless a later edit replaced it.

const initial = useCollimateStore.getState();
const cell = (over: Partial<Cell> = {}): Cell => ({
    id: 'cell-1', name: 'Notes', files: [], channel: 'ch', updated_at: '2026-10-11T10:00:00.000000', ...over,
});
const serverState = (tags: string[]): AppState => ({
    active_channel_id: 'ch',
    channels: [{
        id: 'ch', name: 'ch', nav_stacks: [],
        main_stacks: [{ id: 's1', name: '', cells: [cell({ tags })] }],
    }],
} as unknown as AppState);
const cachedTags = () => useCollimateStore.getState().cellCache['cell-1']?.tags;

describe('saveCellTags (TG-05)', () => {
    beforeEach(() => {
        useCollimateStore.setState(initial, true);
        vi.mocked(cells.patchCellMeta).mockReset();
    });
    afterEach(() => vi.restoreAllMocks());

    it('patches the cache at once and sends the full normalized list (TG-05)', async () => {
        useCollimateStore.getState().applyServerState(serverState(['draft']));
        let settle: (v: Partial<Cell> & { tags: string[] }) => void = () => {};
        vi.mocked(cells.patchCellMeta).mockImplementation(() => new Promise(r => { settle = r; }));
        const saved = saveCellTags('ch', 'cell-1', ['draft', '  needs   review ', 'DRAFT']);
        expect(cachedTags()).toEqual(['draft', 'needs review']);
        expect(cells.patchCellMeta).toHaveBeenCalledWith('ch', 'cell-1', { tags: ['draft', 'needs review'] });
        settle({ id: 'cell-1', tags: ['draft', 'needs review'] });
        await expect(saved).resolves.toBe(true);
        expect(cachedTags()).toEqual(['draft', 'needs review']);
    });

    it('a refusal puts the prior list back, and a rebroadcast keeps it there (TG-05)', async () => {
        const server = serverState(['draft']);
        useCollimateStore.getState().applyServerState(server);
        vi.mocked(cells.patchCellMeta).mockRejectedValue(new Error('refused'));
        const error = vi.spyOn(console, 'error').mockImplementation(() => {});
        const saved = saveCellTags('ch', 'cell-1', ['draft', 'later']);
        expect(cachedTags()).toEqual(['draft', 'later']);
        await expect(saved).resolves.toBe(false);
        expect(cachedTags()).toEqual(['draft']);
        useCollimateStore.getState().applyServerState(server);
        expect(cachedTags()).toEqual(['draft']);
        expect(error).toHaveBeenCalled();
    });

    it('a refusal leaves a later edit alone (TG-05)', async () => {
        useCollimateStore.getState().applyServerState(serverState([]));
        let fail: (e: Error) => void = () => {};
        vi.mocked(cells.patchCellMeta)
            .mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; }))
            .mockResolvedValueOnce({ id: 'cell-1', tags: ['b'] });
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const first = saveCellTags('ch', 'cell-1', ['a']);
        await saveCellTags('ch', 'cell-1', ['b']);
        fail(new Error('boom'));
        await expect(first).resolves.toBe(false);
        expect(cachedTags()).toEqual(['b']);
    });

    it('the prior list comes from the layout when the cache has none (TG-05)', () => {
        useCollimateStore.setState({
            cellCache: {},
            activeChannel: serverState(['kept']).channels[0],
        } as never);
        expect(currentCellTags('cell-1')).toEqual(['kept']);
        expect(currentCellTags('elsewhere')).toEqual([]);
    });
});
