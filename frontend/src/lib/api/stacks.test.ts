import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { moveCellTo } from './stacks';

describe('moveCellTo', () => {
    afterEach(() => vi.restoreAllMocks());

    it('names the moving cell by id and the target position by its real index', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { id: 'ch', main_stacks: [], nav_stacks: [] } });
        await moveCellTo('Q&A', 's/1', 2, 'stack', 'cell-7', 4);
        expect(request).toHaveBeenCalledWith(expect.objectContaining({
            method: 'POST', url: '/stacks/Q%26A/s%2F1/move_cell_to/2/stack?cell_id=cell-7&tgt_cell_index=4',
        }));
    });

    it('defaults the target cell index to 0 for a new column', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { id: 'ch', main_stacks: [], nav_stacks: [] } });
        await moveCellTo('ch', 's1', 3, 'insert', 'c1');
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: '/stacks/ch/s1/move_cell_to/3/insert?cell_id=c1&tgt_cell_index=0' }));
    });
});
