import { Channel } from '@/lib/types';
import { requestJson } from './client';


export const removeStack = async (channel: string, sid: string) => requestJson(`/stacks/${encodeURIComponent(channel)}/${encodeURIComponent(sid)}/remove`, 'POST');

export const minimizeStack = async (channel: string, sid: string) => requestJson(`/stacks/${encodeURIComponent(channel)}/${encodeURIComponent(sid)}/minimize`, 'POST');

export const restoreMinimizedStack = async (channel: string, sid: string, afterStackId?: string | null) => {
    const qs = afterStackId ? `?after=${encodeURIComponent(afterStackId)}` : '';
    return requestJson(`/stacks/${encodeURIComponent(channel)}/${encodeURIComponent(sid)}/restore_minimized${qs}`, 'POST');
};

// Duplicate the cell at `cellIdx` in place — the copy lands in the same stack,
// one slot after the original. Powers Duplicate on a fan column (the copy
// lands as the next column).
export const duplicateCellInStack = async (channel: string, sid: string, cellIdx: number): Promise<Channel | null> => {
    const result = await requestJson(`/stacks/${encodeURIComponent(channel)}/${encodeURIComponent(sid)}/duplicate_cell_in_stack/${cellIdx}`, 'POST') as Channel & { duplicated_cell_ids: string[] };
    return result.duplicated_cell_ids.length ? result : null;
};

export const duplicateStack = async (channel: string, sid: string) => requestJson(`/stacks/${encodeURIComponent(channel)}/${encodeURIComponent(sid)}/duplicate_stack`, 'POST');

/** Move the cell `cellId` out of stack `sid`: into a new stack at
 *  `targetIdx` ('insert') or into the stack at `targetIdx` at its REAL cell
 *  index `tgtCellIdx` ('stack'). The cell is named by id, so a stale view
 *  answers 409 ('The cell moved — try again') instead of moving another
 *  cell; moving a cell with a live run onto Capabilities answers 409 too. */
export const moveCellTo = async (channel: string, sid: string, targetIdx: number, mode: 'insert' | 'stack', cellId: string, tgtCellIdx: number = 0) => {
    const qs = new URLSearchParams({ cell_id: cellId, tgt_cell_index: String(tgtCellIdx) });
    return requestJson(`/stacks/${encodeURIComponent(channel)}/${encodeURIComponent(sid)}/move_cell_to/${targetIdx}/${mode}?${qs}`, 'POST');
};

export const moveStackTo = async (channel: string, sid: string, targetIdx: number, mode: 'insert' | 'stack') =>
    requestJson(`/stacks/${encodeURIComponent(channel)}/${encodeURIComponent(sid)}/move_stack_to/${targetIdx}/${mode}`, 'POST');


export const renameStack = async (channel: string, sid: string, name: string) => requestJson(`/stacks/${encodeURIComponent(channel)}/${encodeURIComponent(sid)}/rename?name=${encodeURIComponent(name)}`, 'PUT');


export const createStack = async (channel: string) => requestJson(`/stacks/${encodeURIComponent(channel)}/create`, 'POST');


/** Merge a ≥2-cell stack into one cell (one root options declaration), replacing the stack's cells in place. Returns the mutated channel
 *  for applyOptimisticChannel + the merged cell to select. */
export const mergeStack = async (
    channel: string, stackId: string,
): Promise<{ ok: boolean; new_cell_id: string; new_stack_id: string; channel: Channel }> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/merge`, 'POST', { stack_id: stackId });


/** Split one cell using its effective .options_stack declaration. Returns the mutated channel + the resulting
 *  cells (index 0 selected). */
export const splitCell = async (
    channel: string, cellId: string,
): Promise<{ ok: boolean; new_cell_ids: string[]; new_stack_id: string; channel: Channel }> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/split`, 'POST', { cell_id: cellId });


export const insertBlankCell = async (
    channel: string,
    stackId: string,
    opts: { id?: string; name?: string } = {},
): Promise<Channel> =>
    requestJson(`/stacks/${encodeURIComponent(channel)}/${encodeURIComponent(stackId)}/insert_blank_cell`, 'POST', {
        id: opts.id ?? null,
        name: opts.name ?? 'New cell',
    });
