import { useEffect, useRef } from 'react';
import type { Cell } from '@/lib/types';
import type { CellLayout } from '@/stores/collimate/types';
import type { LockOwnerOf } from '@/lib/fileLocks';
import type { CellSaveState } from './useCellSaveState';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { pruneRedundantMarkers } from '@/lib/fileTree';
import { replaceCellFiles } from '@/lib/fileCommands';
import { currentChannelId } from '@/lib/currentChannel';
import type { UpdateFiles } from '@/lib/fileTransfer';
import { isRunPath } from '@/lib/runningCell';
import { renameFileTab } from '@/lib/cellLayout';

export interface CellFileImportContext {
    cellId: string;
    channelId: string | undefined;
    canWrite: boolean;
    updateFiles: UpdateFiles;
}

interface CellFileWritesOptions {
    cell: Cell;
    channelId: string | undefined;
    readOnly: boolean;
    save: Pick<CellSaveState, 'latestCell' | 'commitCell' | 'notifyEdit'>;
    lockOwnerOf: LockOwnerOf;
    onLayoutChange: (update: (layout: CellLayout) => CellLayout) => void;
}

/** Structural edits always apply to the current cell and retain locked work. */
export function useCellFileWrites({
    cell, channelId, readOnly, save, lockOwnerOf, onLayoutChange,
}: CellFileWritesOptions) {
    const { latestCell, commitCell, notifyEdit } = save;
    // The cell's file writer (UpdateFiles, lib/fileTransfer). `update` runs
    // here, once, on the cell's LATEST files (latestCell): every structural
    // path (create, drop, paste, move, rename, delete, imports) may reach
    // this long after the render that handed it the writer, and the diff
    // below runs against what the cell holds now, so nothing written in
    // between is reverted or re-tombstoned.
    const persistFiles: UpdateFiles = async (update,options) => {
        if (readOnly) return;
        const latest = latestCell();
        const priorFiles = latest.files;
        const newFiles = update(priorFiles);
        if (newFiles === priorFiles) return;
        // Backstop for every structural path (create, drop, paste, move,
        // rename, delete): a live run's locked file is never removed or
        // replaced by a structural write (the stored copy stands — content
        // edits go through their FileDocument owner), and no NEW path lands
        // under `_runs/` (only a run writes its folder). The Files pane
        // never offers either; this guards the conditional mutation input.
        const oldFilesByName = new Map(priorFiles.map(f => [f.name, f] as const));
        const lockedNow = new Map(priorFiles
            .filter(f => isRunPath(f.name) || lockOwnerOf(f.name) !== null)
            .map(f => [f.name, f] as const));
        const guarded = newFiles.flatMap(f => {
            const locked = lockedNow.get(f.name);
            if (locked) return [locked];
            return oldFilesByName.has(f.name) || !isRunPath(f.name) ? [f] : [];
        });
        const kept = new Set(guarded.map(f => f.name));
        for (const [name, f] of lockedNow) if (!kept.has(name)) guarded.push(f);
        const mappedFiles=pruneRedundantMarkers(guarded);
        // Auto-repair the pinned pointer: if the pinned file was renamed or
        // deleted (its path is gone from the new set), clear it so nothing
        // dangles at a missing file. The user re-pins if needed. (The tab
        // itself is dropped by reconcile on the next pass.)
        const nextPinned = latest.pinned && mappedFiles.some(f => f.name === latest.pinned)
            ? latest.pinned
            : undefined;

        await replaceCellFiles(channelId || currentChannelId(),cell.id,mappedFiles,priorFiles,options?.renames);
        for(const rename of options?.renames??[])onLayoutChange(layout=>renameFileTab(layout,rename.source,rename.target));
        if(nextPinned!==latest.pinned)await commitCell({pinned:nextPinned});
        // Structural changes (create / rename / delete) are real edits
        // too — arm the idle checkpoint so the next file-switch /
        // 60s-idle / editor-close promotes them into a version. Without
        // this, creating a new file then switching tabs produces no
        // version because no document body edit notified the checkpoint.
        notifyEdit();
    };

    const handleUpdateFiles:UpdateFiles=(update,options)=>{
        const task=Promise.resolve(persistFiles(update,options));
        if(!options?.renames)void task.catch(error=>useCollimateStore.getState().openAlert({title:'Could not save file changes',body:String(error?.message??error)}));
        return task;
    };

    // Pin / unpin a file as the cell's preview (the user's pin; runs never
    // pin). Single-pin invariant: pinning a different file replaces the
    // previous one; clicking the already-pinned file clears it. Persists
    // through the channel-scoped metadata receipt. Pinned-ness of a TAB is derived from this pointer —
    // pinning opens the file's tab (or focuses it), gives it the pin icon
    // and takes away its ✕; unpinning CLOSES that tab. Both halves happen in
    // CellEditor's reconcile effect. A run's locked file is never pinned: it
    // vanishes with the run, so it cannot be the cell's preview.
    const handleTogglePin = (fileName: string) => {
        if (readOnly) return;
        if (cell.pinned !== fileName && lockOwnerOf(fileName)) return;
        const next = cell.pinned === fileName ? undefined : fileName;
        void commitCell({ pinned: next }).catch(error=>useCollimateStore.getState().openAlert({title:'Could not pin file',body:String(error?.message??error)}));
    };

    const fileImportContext = useRef<CellFileImportContext | null>(null);
    // A delayed copy into the cell (a history artifact's fetch) can outlive
    // this editor. It writes through the current writer when it finishes,
    // and is discarded if the target cell/channel has changed.
    useEffect(() => {
        fileImportContext.current = { cellId: cell.id, channelId, canWrite: !readOnly, updateFiles: handleUpdateFiles };
        return () => { fileImportContext.current = null; };
    });
    return { handleUpdateFiles, handleTogglePin, fileImportContext };
}
