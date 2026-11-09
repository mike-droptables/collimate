// Saving a board cell's tags (TG-05): the one write path CellTagMenu uses on
// both board cards (a shelf card's tags go through overviewSlice.setRemovedTags).
// The StackItem.saveMeta pattern: the cache is patched first so the chips
// change at once, then the full list goes through the meta PATCH. patchCell
// leaves updated_at alone, so a rebroadcast of the unchanged server cell
// would keep a failed edit (applyServerState skips a cached entry whose
// stamp matches): a failed request puts the prior list back itself, unless
// a later edit has replaced this one in the cache meanwhile.

import { patchCellMeta } from '@/lib/api/cells';
import { normalizeTags } from '@/lib/tags';
import { useCollimateStore } from '@/stores/useCollimateStore';

/** The tags the client holds for a cell: its cache entry's, else the
 *  active channel's layout cell's, else none. */
export function currentCellTags(cellId: string): string[] {
    const s = useCollimateStore.getState();
    const cached = s.cellCache[cellId]?.tags;
    if (cached) return cached;
    for (const stack of s.activeChannel?.main_stacks ?? []) {
        const cell = stack.cells.find(c => c.id === cellId);
        if (cell) return cell.tags ?? [];
    }
    return [];
}

/** Make `next` the cell's whole tag list. Resolves true once the server
 *  took it (its normalized list replaces the optimistic one), false when
 *  it refused (`prior`, by default the list the client holds, is back
 *  unless a later edit replaced this one). A list the server would refuse
 *  (lib/tags normalizeTags) rejects before the cache is touched. */
export async function saveCellTags(
    channel: string, cellId: string, next: string[], prior: string[] = currentCellTags(cellId),
): Promise<boolean> {
    const { patchCell } = useCollimateStore.getState();
    const edit = normalizeTags(next);
    patchCell(cellId, { tags: edit });
    const stillThisEdit = () => useCollimateStore.getState().cellCache[cellId]?.tags === edit;
    try {
        const receipt = await patchCellMeta(channel, cellId, { tags: edit });
        if (stillThisEdit()) patchCell(cellId, { tags: receipt.tags });
        return true;
    } catch (e) {
        console.error('Saving the cell tags failed', e);
        if (stillThisEdit()) patchCell(cellId, { tags: prior });
        return false;
    }
}
