import {isRunPath} from '@/lib/runningCell';
import {moveCellFiles,currentCellFiles} from '@/lib/fileCommands';
import {flushFileDocuments} from '@/lib/fileDocuments';
// Shared copy/cut/paste + drag-and-drop engine for the file browser surfaces
// (Tree view and Folder view). Both views are bound to ONE cell ("self");
// transfers always TARGET self (the focused browser), while the SOURCE may be
// self or another cell — including a cell in another channel, which is why the
// payload lives in the store, not the OS clipboard. A copy pastes the files as
// they were copied (a large file's body fetched at paste time); a cut pastes
// what its source holds at paste time. A copy or cut also writes the copied
// path to the OS clipboard as text and remembers it (FileClipboard.osText),
// so a later paste can tell this clipboard from one another app filled
// (lib/clipboardPaste).

import { COLLIMATE_DND_MIME } from '@/lib/fileDrag';
import { useCallback } from 'react';
import type { VirtualFile } from '@/lib/types';
import { TreeNode, remintEmptiedParent } from '@/lib/fileTree';
import {
    collectTransferEntries, fetchTextBodies, rebaseEntries, mergeByName,
    isInvalidSameCellTarget, basename, withoutRunPaths, withFetchedBodies, summarize,
    type UpdateFiles, type WithBody,
} from '@/lib/fileTransfer';
import { lockOwnerUnder, runPathRefusal, type LockOwnerOf } from '@/lib/fileLocks';
import { needsTextBody } from '@/lib/fileCommands';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { FileClipboard, FileTransferEntry, FileTransferSource } from '@/stores/collimate/types';

interface UseFileTransferParams {
    cellId: string;
    channel: string | undefined;
    files: VirtualFile[];
    /** Mounted same-cell writer from useCellFileWrites. */
    onUpdateFiles?: UpdateFiles;
    readOnly: boolean;
    /** The live run owning a path in THIS cell (lib/fileLocks). A locked
     *  node is copied, never cut or dragged as a move; nothing lands under
     *  `_runs/`. Absent = nothing is locked. */
    lockOwnerOf?: LockOwnerOf;
}

/** File operations use pinned bodies and conditional durable receipts. */
export async function fetchSourceBodies(
    channel: string | undefined, cellId: string, files: VirtualFile[],
): Promise<WithBody | null> {
    const descriptors = files.filter(needsTextBody);
    if (descriptors.length === 0) return withFetchedBodies([], new Map());
    if (!channel) return null;
    try {
        return withFetchedBodies(descriptors, await fetchTextBodies(channel, cellId, descriptors, { cache: 'no-store' }));
    } catch {
        return null;
    }
}

/** What a transfer lands, read when it commits, each file with its pinned
 *  body from `withBody`. A copy lands the entries it was made with
 *  (`sourceNow` null). A move lands each of its files as the source holds
 *  it now (`sourceNow`, the source cell's files after its editors flushed),
 *  so an edit or a run's write made since the drag or the cut moves with
 *  it; one gone from the source stays gone. A move whose source cell is
 *  gone (`sourceNow` null) lands what was taken. Null when the descriptor's
 *  revision changed after its body was fetched: the transfer must not go
 *  through. Shared with the canvas drop path (useCanvasFileDrop). */
export function landingEntries(
    entries: FileTransferEntry[], sourceNow: VirtualFile[] | null, withBody: WithBody,
): FileTransferEntry[] | null {
    const byName = sourceNow && new Map(sourceNow.map(f => [f.name, f] as const));
    const landing: FileTransferEntry[] = [];
    for (const e of entries) {
        const now = byName ? byName.get(e.file.name) : e.file;
        if (!now) continue;
        const file = withBody(now);
        if (!file) return null;
        landing.push({ relPath: e.relPath, file });
    }
    return landing;
}

/** Whether the source still has the exact revision/hash the destination took. */
function holdsLanded(current:VirtualFile,landed:VirtualFile):boolean {
  return current.body_hash===landed.body_hash && current.encoding===landed.encoding && current.file_revision===landed.file_revision;
}

/** File operations use pinned bodies and conditional durable receipts. */
export async function pruneMovedFromSource(
    source: FileTransferSource, moved: ReadonlyMap<string, VirtualFile>,
): Promise<boolean> {
    // The durability barrier can await network receipts. Recheck the exact
    // source descriptor afterward so concurrent edits remain in the source.
    await flushCellSaves(source.cellId);
    const store = useCollimateStore.getState();
    const loc = store.locateCell(source.cellId);
    if (!loc) return true; // source already gone; nothing to remove
    const files = loc.cell.files ?? [];
    const kept: string[] = [];
    const remaining = files.filter(f => {
        const landed = moved.get(f.name);
        if (!landed) return true;
        if (holdsLanded(f, landed)) return false;
        kept.push(f.name);
        return true;
    });
    if (kept.length > 0) {
        void store.openAlert({
            title: 'Kept in the source cell',
            body: kept.length === 1
                ? `\`${kept[0]}\` changed during the move, so the source cell keeps it as well. The moved copy has the content from before the change.`
                : `${kept.length} files changed during the move, so the source cell keeps them as well: ${summarize(kept)}. The moved copies have the content from before the change.`,
        });
    }
    if (remaining.length === files.length) return true;
    const removed = files.filter(f => !remaining.includes(f)).map(f => f.name);
    await flushFileDocuments(undefined,source.cellId);
    // An edit made while issued writes drain must stay in the source.
    const latest = store.locateCell(source.cellId)?.cell.files ?? [];
    const removal = new Set(removed);
    const currentRemaining = latest.filter(f => !removal.has(f.name)
        || !holdsLanded(f, moved.get(f.name)!));
    return store.applyCellFiles(source.cellId, remintEmptiedParent(currentRemaining, source.nodePath));
}

export function useFileTransfer({ cellId, channel, files, onUpdateFiles, readOnly, lockOwnerOf }: UseFileTransferParams) {
    /** The clipboard's label ("'components'") for the Paste menu item; null
     *  when nothing is copied or cut. */
    const clipboardLabel = useCollimateStore(s => s.fileClipboard?.label ?? null);
    const isLockedNode = useCallback((node: TreeNode) => isRunPath(node.path)||!!lockOwnerUnder(
        files.map(f => f.name), node.path, node.type === 'folder', lockOwnerOf,
    ), [files, lockOwnerOf]);

    const isInternalDrag = useCallback(
        (e: React.DragEvent) => e.dataTransfer.types.includes(COLLIMATE_DND_MIME),
        [],
    );

    // Default copy across cells; default move within a cell (reorganize). Any
    // modifier flips. A read-only source can only be copied from.
    const resolveOp = useCallback((e: { ctrlKey: boolean; metaKey: boolean; altKey: boolean }, sameCell: boolean, sourceReadOnly?: boolean): 'copy' | 'move' => {
        const base: 'copy' | 'move' = sameCell ? 'move' : 'copy';
        const mod = e.ctrlKey || e.metaKey || e.altKey;
        let op = mod ? (base === 'move' ? 'copy' : 'move') : base;
        if (sourceReadOnly && op === 'move') op = 'copy';
        return op;
    }, []);

    /** Apply a transfer INTO this (self) cell at `targetFolder`. */
    const applyToSelf = useCallback(async (
        entries: FileTransferEntry[], source: FileTransferSource, op: 'copy' | 'move', targetFolder: string,
    ): Promise<boolean> => {
        if (readOnly || !onUpdateFiles) return false;
        const refusal = runPathRefusal(targetFolder);
        if (refusal) {
            void useCollimateStore.getState().openAlert({ title: 'Paste refused', body: refusal });
            return false;
        }
        const sameCell = source.cellId === cellId;
        const sameCellMove = sameCell && op === 'move';
        if (sameCellMove) {
            if (isInvalidSameCellTarget(source.nodePath, targetFolder)) return false;
            // A locked node (or a folder holding one) is copied, never moved.
            if (lockOwnerOf?.(source.nodePath)
                || lockOwnerUnder(files.map(f => f.name), source.nodePath, true, lockOwnerOf)) return false;
        }
        // A run's files never leave their cell: a transfer from another
        // cell arrives without them.
        const outgoing = sameCell ? entries : withoutRunPaths(entries);
        if (outgoing.length === 0) return false;
        const refuse = () => {
            // A move would otherwise destroy the source and write an empty
            // file at the target.
            void useCollimateStore.getState().openAlert({
                title: 'Transfer failed',
                body: 'Could not load the file contents to transfer. Open the file(s) and try again.',
            });
        };
        const withBody = await fetchSourceBodies(source.channel, source.cellId, outgoing.map(e => e.file));
        if (!withBody) {
            refuse();
            return false;
        }
        if (op === 'move') {
// File operations use pinned bodies and conditional durable receipts.
            await flushCellSaves(source.cellId);
            // Drain source document receipts before conditional deletion.
            // Accepted revision checks retain any newer source edits.
            if (!sameCell) await flushFileDocuments(undefined,source.cellId);
        }
        // Written into the cell's list as it is at commit time, after the
        // body fetches and the flush (landingEntries): a copy lands the
        // entries it was made with, a move each of its files as the source
        // holds it now, and anything else under a moved node stays where it
        // is. The writer runs this once, synchronously, so `moved` holds
        // what moved when it returns.
        let moved: ReadonlyMap<string, VirtualFile> = new Map();
        const destinations: {source_path:string;target_path:string;source_revision:number;target_revision:number}[]=[];
        const buildLanding=(current:VirtualFile[]) => {
            const sourceNow = op === 'copy' ? null
                : sameCell ? current
                    // A source cell that is gone leaves nothing to prune.
                    : useCollimateStore.getState().locateCell(source.cellId)?.cell.files ?? null;
            const landing = landingEntries(outgoing, sourceNow, withBody);
            if (!landing) {
                refuse();
                return current;
            }
            if (landing.length === 0) return current;
            const names = new Set(landing.map(e => e.file.name));
            const working = sameCellMove ? current.filter(f => !names.has(f.name)) : current;
            const { files: additions } = rebaseEntries(landing, targetFolder, new Set(working.map(f => f.name)));
            destinations.push(...landing.map((entry,index)=>({source_path:entry.file.name,target_path:additions[index].name,source_revision:entry.file.file_revision,target_revision:current.find(file=>file.name===additions[index].name)?.file_revision??0})));
            moved = new Map(landing.map(e => [e.file.name, e.file] as const));
            const next = mergeByName(working, additions);
            return sameCellMove ? remintEmptiedParent(next, source.nodePath) : next;
        };
        if(op==='move'&&!sameCell&&source.channel===channel){
            buildLanding(currentCellFiles(cellId));
            if(!destinations.length)return false;
            try{await moveCellFiles(channel!,source.cellId,cellId,destinations);return true;}catch(error){await useCollimateStore.getState().openAlert({title:'Move failed',body:String((error as Error)?.message??error)});return false;}
        }
        await onUpdateFiles(buildLanding);
        if (moved.size === 0) return false;
        if (sameCell || op !== 'move') return true;

        // Cross-cell MOVE: added here, now remove exactly what moved from the
        // source (its run files never moved). The prune reads the source
        // right after one more flush of its editors, so after the commit
        // above only newer text still in those editors can reach it, and a
        // file that the flush advances stays in
        // the source. Propagate the removal result so a failed source-prune
        // keeps the clipboard.
        return await pruneMovedFromSource(source, moved);
    }, [cellId, channel, files, onUpdateFiles, readOnly, lockOwnerOf]);

    // ── Clipboard ──────────────────────────────────────────────────────────
    // A copy pastes the entries it was made with; a cut is re-read from its
    // source at paste time (cutEntriesNow). Pinned text bodies are fetched
    // only when the paste needs them.
    const setClip = useCallback((node: TreeNode, op: 'copy' | 'cut') => {
        // Never move OUT of a read-only cell, nor a run's locked file.
        if (op === 'cut' && (readOnly || isLockedNode(node))) return;
        const entries = collectTransferEntries(files, node.path, node.type === 'folder');
        if (!entries.length) return;
        const source: FileTransferSource = { channel: channel ?? '', cellId, nodePath: node.path };
        const label = `'${basename(node.path)}'`;
        const osText = node.path;
        useCollimateStore.getState().setFileClipboard({ op, source, entries, label, osText });
        // Best effort: without focus or a secure context the write fails and
        // the OS clipboard keeps what it held.
        try {
            void navigator.clipboard?.writeText(osText).catch(() => {});
        } catch { /* the Clipboard API is absent */ }
    }, [files, channel, cellId, readOnly, isLockedNode]);

    const copyNode = useCallback((node: TreeNode) => setClip(node, 'copy'), [setClip]);
    const cutNode = useCallback((node: TreeNode) => setClip(node, 'cut'), [setClip]);

    /** What a cut moves: its node as the source cell holds it NOW. The
     *  editor stays open after a cut, and moving the cut-time entries while
     *  pruning the source would drop every edit made in between. Null when
     *  the node is gone from its cell. A source cell that is gone leaves
     *  nothing to prune, so its cut lands what was cut. */
    const cutEntriesNow = useCallback((clip: FileClipboard): FileTransferEntry[] | null => {
        const sourceFiles = clip.source.cellId === cellId
            ? files
            : useCollimateStore.getState().locateCell(clip.source.cellId)?.cell.files ?? null;
        if (sourceFiles === null) return clip.entries;
        // A cut folder's entries sit under it; a cut file's one entry is the
        // file. A cut folder is what sits under it now, never a same-named
        // file that replaced it.
        const under = `${clip.source.nodePath}/`;
        const isFolder = clip.entries.some(e => e.file.name.startsWith(under));
        const now = collectTransferEntries(sourceFiles, clip.source.nodePath, isFolder)
            .filter(e => !isFolder || e.file.name.startsWith(under));
        return now.length > 0 ? now : null;
    }, [cellId, files]);

    const pasteInto = useCallback((targetFolder: string) => {
        if (readOnly) return;
        const store = useCollimateStore.getState();
        const clip = store.fileClipboard;
        if (!clip) return;
        if (clip.op === 'copy') {
            void applyToSelf(clip.entries, clip.source, 'copy', targetFolder);
            return;
        }
        const entries = cutEntriesNow(clip);
        if (entries === null) {
            void store.openAlert({ title: 'Paste failed', body: `\`${clip.source.nodePath}\` no longer exists.` });
            store.clearFileClipboard();
            return;
        }
        void applyToSelf(entries, clip.source, 'move', targetFolder).then(ok => {
            if (ok) useCollimateStore.getState().clearFileClipboard();
        });
    }, [readOnly, applyToSelf, cutEntriesNow]);

    // ── Drag and drop ────────────────────────────────────────────────────────
    const startDrag = useCallback((node: TreeNode, e: React.DragEvent) => {
        const entries = collectTransferEntries(files, node.path, node.type === 'folder');
        if (!entries.length) return;
        const source: FileTransferSource = { channel: channel ?? '', cellId, nodePath: node.path };
        // A locked node drags as a copy only (the drop forces copy).
        useCollimateStore.getState().setFileDrag({ source, entries, sourceReadOnly: readOnly || isLockedNode(node) });
        // The cell and its paths ride the dataTransfer too, readable at drop
        // time by a renderer in this cell (the markdown editor inserts them).
        e.dataTransfer.setData(COLLIMATE_DND_MIME, JSON.stringify({ cellId, paths: entries.map(en => en.file.name) }));
        e.dataTransfer.effectAllowed = 'copyMove';
    }, [files, channel, cellId, readOnly, isLockedNode]);

    const endDrag = useCallback(() => {
        if (useCollimateStore.getState().fileDrag) useCollimateStore.getState().setFileDrag(null);
    }, []);

    const dropEffectFor = useCallback((e: React.DragEvent): 'copy' | 'move' => {
        const drag = useCollimateStore.getState().fileDrag;
        if (!drag) return 'copy';
        return resolveOp(e, drag.source.cellId === cellId, drag.sourceReadOnly);
    }, [cellId, resolveOp]);

    /** Handle a drop of an internal payload into `targetFolder`. Returns true
     *  if this was an internal transfer (handled here) — false means the caller
     *  should fall through to its OS-file drop path. */
    const dropInto = useCallback((targetFolder: string, e: React.DragEvent): boolean => {
        if (!isInternalDrag(e)) return false;
        e.preventDefault();
        e.stopPropagation();
        const drag = useCollimateStore.getState().fileDrag;
        useCollimateStore.getState().setFileDrag(null);
        if (!drag || readOnly) return true; // consumed; nothing to apply
        const op = resolveOp(e, drag.source.cellId === cellId, drag.sourceReadOnly);
        void applyToSelf(drag.entries, drag.source, op, targetFolder);
        return true;
    }, [isInternalDrag, readOnly, resolveOp, cellId, applyToSelf]);

    return {
        clipboardLabel,
        copyNode, cutNode, pasteInto,
        startDrag, endDrag, dropEffectFor, dropInto, isInternalDrag,
    };
}
