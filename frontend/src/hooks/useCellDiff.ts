import { useMemo } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useFileBodies } from '@/hooks/useFileBodies';
import type { Cell, Channel, FileBody, VirtualFile } from '@/lib/types';

export interface CellDiffView {
    baseCellId: string;
    /** The base cell's name — the diff's "before" label and the banner's. */
    baseName: string;
    /** Base file name → content; null until the pinned bodies load. */
    baseFiles: Record<string, string> | null;
    /** The diffing cell's hydrated revision snapshots; null while they load. */
    currentFiles: FileBody[] | null;
}

function findLayoutCell(channel: Channel | null, cellId: string): Cell | undefined {
    if (!channel) return undefined;
    for (const stacks of [channel.main_stacks, channel.nav_stacks]) {
        for (const stack of stacks ?? []) {
            const hit = stack.cells.find(c => String(c.id) === cellId);
            if (hit) return hit;
        }
    }
    return undefined;
}

const NO_FILES: VirtualFile[] = [];

/** The diff state of `cell` (the store's cellDiffs entry, resolved), or
 *  null when the cell is not in a diff. */
export function useCellDiff(cell: Cell): CellDiffView | null {
    const cellId = String(cell.id);
    const baseCellId = useCollimateStore(s => s.cellDiffs[cellId]);
    // Layout cell (position-independent: found by id anywhere in the
    // channel) overlaid with the entity cache's fresher copy, the same
    // merge StackItem applies to its own cell. Both selectors return
    // existing references, so unrelated pushes don't re-render.
    const layoutCell = useCollimateStore(s => (baseCellId ? findLayoutCell(s.activeChannel, baseCellId) : undefined));
    const cachedCell = useCollimateStore(s => (baseCellId ? s.cellCache[baseCellId] : undefined));
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');

    const baseFilesList = cachedCell?.files ?? layoutCell?.files ?? NO_FILES;
    const baseName = cachedCell?.name ?? layoutCell?.name ?? '';
    const base = useFileBodies(channel, baseCellId ?? '', baseFilesList, undefined, !!baseCellId);
    const baseFiles = useMemo(() => base.files === null ? null : Object.fromEntries(
        base.files.map(file => [file.name, file.content])), [base.files]);
    const ownFiles = baseCellId ? cell.files : NO_FILES;
    const { files: currentFiles } = useFileBodies(channel, cellId, ownFiles, undefined, !!baseCellId);

    const found = !!baseCellId && !!(layoutCell || cachedCell);
    return useMemo(
        () => (found ? { baseCellId: baseCellId!, baseName, baseFiles, currentFiles } : null),
        [found, baseCellId, baseName, baseFiles, currentFiles],
    );
}
