import { useCollimateStore } from '@/stores/useCollimateStore';
import { revealCellFile } from '@/lib/revealFile';
import type { SpawnResult } from '@/lib/api/capabilitiesStack';

/**
 * Open the work cell a Build or Create spawn just made, ready to run: apply
 * the answered layout, select the cell, open its brief as the active tab
 * (so it is the run's selected file) and open the runners panel over it.
 * The user picks any runner there; nothing here picks one.
 *
 * The layout carries no file lists, so the brief's tab opens once the
 * cell's files arrive with the next state push (at once when they already
 * have). A channel switch first drops that wait.
 */
export function openSpawnedCell(result: SpawnResult): void {
    const store = useCollimateStore.getState();
    const channel = store.activeChannel?.id;
    store.applyOptimisticLayout(result);
    store.requestSelectCell(result.cell_id);
    whenCellHolds(channel, result.cell_id, result.brief_path, () => revealCellFile(result.cell_id, result.brief_path));
    store.openPickSession(result.cell_id, 'runners');
}

/** Run `then` once the active channel's cell `cellId` lists `path`. */
function whenCellHolds(channel: string | undefined, cellId: string, path: string, then: () => void): void {
    const holds = (): boolean => !!useCollimateStore.getState().locateCell(cellId)?.cell.files.some(f => f.name === path);
    if (holds()) { then(); return; }
    const stop = useCollimateStore.subscribe(state => {
        if (state.activeChannel?.id !== channel) { stop(); return; }
        if (holds()) { stop(); then(); }
    });
}
