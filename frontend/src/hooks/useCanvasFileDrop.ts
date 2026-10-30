import {moveCellFiles} from '@/lib/fileCommands';
import {createStack,moveStackTo,patchCellMeta} from '@/lib/api';
import type {Channel} from '@/lib/types';
import {flushFileDocuments} from '@/lib/fileDocuments';
import { COLLIMATE_DND_MIME, carriesOsFiles } from '@/lib/fileDrag';
// Drops onto the main canvas. Two payloads land here:
//
//   * OS files — every drop becomes one new cell: each file streams into
//     the CAS (lib/fileIngest) and the cell is created through the import
//     commit (POST /import, one stack of one cell). Dropped FOLDERS come in
//     whole: the entries-API walk (lib/dropEntries) reproduces the recursive
//     tree inside the new cell, empty directories included.
//   * an INTERNAL cell-file drag (COLLIMATE_DND_MIME, payload in the store's
//     `fileDrag`) that missed every file browser — the dragged file/folder
//     becomes a new cell of its own.
//
// A cell's own browser stopPropagations the internal drops it consumes
// (useFileTransfer.dropInto), so only drags that land on bare canvas reach
// this hook. Everything else (stack/tab drags) carries neither 'Files' nor
// the marker MIME and is ignored by every handler.
//
// The new-cell cue (`isDragging`: the NEW CELL slot ghost, NewCellSlotGhost)
// follows the same ownership: it is up only while the canvas's own dragover
// fires. A Files pane stops the dragover of every drag it takes (an OS file
// headed into the cell — the pane shows its own "Add to <cell>" frame — or
// a folder-to-folder move) and of every drag it refuses (a read-only tree, a
// run folder: they cancel it with dropEffect 'none', so the browser neither
// drops nor opens the file), so a drag over one never raises it. The slot
// the ghost shows and the slot the drop uses are one function,
// measureDropSlot.
//
// The hook also keeps the store's `osFileDragActive` (window listeners, so
// no handler's stopPropagation hides the drag): while an OS file drag is
// over the window, renderers take no pointer events and desktop guests are
// occluded, so no renderer document swallows the drag. A drag that enters
// the window over a renderer iframe never reaches these listeners; the
// frame reports it instead (OS_FILE_DRAG, lib/sandboxBridge).

import { useCallback, useEffect, useRef, useState } from 'react';
import { STACK_GAP } from '@/hooks/useStackLayout';
import { realInsertIndex, type FanRange } from '@/lib/fanOut';
import { commitImport, type ImportFile } from '@/lib/api/transfer';
import { uploadAll } from '@/lib/transfer/uploadQueue';
import { isReservedCellPath } from '@/lib/transfer/cellPaths';
import { ingestFilesToVirtualFiles } from '@/lib/fileIngest';
import { captureDrop, collectDroppedFiles, hasDroppedItems, type DroppedFile } from '@/lib/dropEntries';
import {
    fetchSourceBodies, landingEntries, pruneMovedFromSource,
} from '@/hooks/useFileTransfer';
import { rebaseEntries, withoutRunPaths } from '@/lib/fileTransfer';
import { isFolderKeep } from '@/lib/fileTree';
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import type { VirtualFile } from '@/lib/types';
import { apiErrorDetail } from '@/lib/utils';
import type { FileDragState } from '@/stores/collimate/types';
import { useCollimateStore } from '@/stores/useCollimateStore';

const hasFiles = (e: React.DragEvent) => carriesOsFiles(e.dataTransfer);
const isInternalDrag = (e: React.DragEvent) => e.dataTransfer.types.includes(COLLIMATE_DND_MIME);
/** Either payload the canvas accepts. Anything else passes straight through. */
const isAcceptedDrag = (e: React.DragEvent) => hasFiles(e) || isInternalDrag(e);

/** Copy vs move for an internal drag, mirroring useFileTransfer.resolveOp: a
 *  new cell is never the source cell, so the cross-cell default is COPY and a
 *  modifier flips it to move; a read-only source can only be copied from. */
const wantsMove = (
    e: { ctrlKey: boolean; metaKey: boolean; altKey: boolean }, sourceReadOnly?: boolean,
) => (e.ctrlKey || e.metaKey || e.altKey) && !sourceReadOnly;

/** The name a cell made from a drop takes. */
const DROPPED_CELL_NAME = 'Dropped files';

/** VirtualFiles → the import commit's files, every one a CAS reference. A
 *  file carrying its text (an internal drag hydrates its bodies) uploads
 *  that text first; one carrying only a hash is already stored. */
async function importFilesOf(files: readonly VirtualFile[]): Promise<ImportFile[]> {
    const inline = files.filter(f => f.encoding !== 'binary' && 'content' in f);
    const stored = await uploadAll(inline.map(f => ({
        path: f.name, body: new Blob(['content' in f ? String(f.content) : ''], { type: 'text/plain;charset=utf-8' }),
    })));
    return files.map(f => {
        const blobHash = stored.get(f.name)?.blob_hash ?? f.body_hash;
        if (!blobHash) throw new Error(`File ${f.name} has no stored body.`);
        return { path: f.name, blob_hash: blobHash };
    });
}

/** One new stack of one cell at `insertIndex` (undefined appends); the
 *  new stack's id. A `.collkeep` marker travels as the folder it keeps: the
 *  server mints the new cell's own. Paths in a reserved band (`_runs/`,
 *  `.collrun`) are left out: the commit refuses them. */
async function createDroppedCell(
    channel: string, files: readonly VirtualFile[], dirs: string[], insertIndex: number | undefined,
): Promise<string | undefined> {
    const landing = files.filter(f => !isReservedCellPath(f.name));
    const kept = landing.filter(f => isFolderKeep(f.name) && f.name.includes('/')).map(f => f.name.slice(0, f.name.lastIndexOf('/')));
    const res = await commitImport(channel, {
        insert_index: insertIndex ?? null,
        stacks: [{ name: '', cells: [{
            name: DROPPED_CELL_NAME,
            files: await importFilesOf(landing.filter(f => !isFolderKeep(f.name))),
            dirs: [...dirs.filter(d => !isReservedCellPath(d)), ...kept],
        }] }],
    });
    return res.stack_ids[0];
}

/** Where a canvas drop at `clientX` lands: the main_stacks index, and the
 *  slot's place in client coordinates. */
export interface DropSlot {
    /** The main_stacks index the new stack is created at. */
    insertIndex: number;
    /** The slot's centre: the middle of the gap between its two columns
     *  (half a gap outside the first or last one). */
    gapX: number;
    /** The neighbouring columns' vertical extent. */
    top: number;
    bottom: number;
}

/** The slot a canvas drop at `clientX` lands in, measured from the on-screen
 *  `[data-stack-id]` columns: the display slot is the number of columns
 *  whose centre sits left of the pointer. The columns are the scene's
 *  DISPLAY list, so a fanned stack shows one column per cell: a slot between
 *  two of them names no place for a new stack and snaps to the nearer of
 *  the fan's two outside gaps, and the API index is the slot in main_stacks
 *  space (lib/fanOut.realInsertIndex). Client rects hold in the main row and
 *  the overview alike. The canvas's drop and its NEW CELL ghost both call
 *  this, so the ghost shows exactly where the stack lands. null with no
 *  measurable column (an empty channel, jsdom): the create appends. */
export function measureDropSlot(clientX: number, fan: FanRange | null): DropSlot | null {
    const boxes = Array.from(document.querySelectorAll<HTMLElement>('[data-stack-id]'), node => {
        const r = node.getBoundingClientRect();
        return { left: r.left, right: r.left + r.width, top: r.top, bottom: r.top + r.height, empty: r.width === 0 && r.height === 0 };
    });
    if (boxes.length === 0 || boxes.some(b => b.empty)) return null;
    boxes.sort((a, b) => a.left - b.left);
    const n = boxes.length;
    let slot = boxes.filter(b => clientX > (b.left + b.right) / 2).length;
    if (fan && fan.end < n && slot > fan.start && slot <= fan.end) {
        slot = clientX - boxes[fan.start].left <= boxes[fan.end].right - clientX ? fan.start : fan.end + 1;
    }
    const gapAfter = (i: number) => Math.max(0, boxes[i + 1].left - boxes[i].right);
    const prev = boxes[slot - 1];
    const next = boxes[slot];
    let gapX: number;
    if (prev && next) gapX = (prev.right + next.left) / 2;
    else if (next) gapX = next.left - (n > 1 ? gapAfter(0) : STACK_GAP) / 2;
    else gapX = prev.right + (n > 1 ? gapAfter(n - 2) : STACK_GAP) / 2;
    const sides = [prev, next].filter((b): b is typeof boxes[number] => !!b);
    return {
        insertIndex: realInsertIndex(fan, slot),
        gapX,
        top: Math.min(...sides.map(b => b.top)),
        bottom: Math.max(...sides.map(b => b.bottom)),
    };
}

export interface CanvasFileDrop {
    /** A drag the canvas takes is over bare canvas: show the new-cell cue. */
    isDragging: boolean;
    /** The drag's latest clientX over bare canvas, null when none. A ref,
     *  never state: the pointer must not re-render the app root at pointer
     *  rate. NewCellSlotGhost reads it each frame. */
    pointerX: React.RefObject<number | null>;
    /** Spread on the scene layer. Enter and leave are CAPTURE handlers:
     *  they count every element the drag crosses, and a child that stops
     *  its own dragleave (a Files pane's folder row does) must not hide one
     *  from the count, or the count never unwinds and the cue outlives a
     *  drag that left the window or was cancelled. */
    dragProps: {
        onDragEnterCapture: (e: React.DragEvent) => void;
        onDragOver: (e: React.DragEvent) => void;
        onDragLeaveCapture: (e: React.DragEvent) => void;
        onDrop: (e: React.DragEvent) => void;
    };
}

/** Attach to the scene-layer div. `channel` is the active channel UUID;
 *  drops no-op without one. `onStackCreated` gets each new cell's stack id
 *  once the refreshed state holds it — the drop is a user add, so the
 *  caller selects it (which brings it fully on screen). `fan` is the
 *  scene's fanned stack, which maps the drop slot to a main_stacks index. */
export function useCanvasFileDrop(
    channel: string | undefined,
    onStackCreated?: (stackId: string) => void,
    fan: FanRange | null = null,
): CanvasFileDrop {
    const [isDragging, setIsDragging] = useState(false);
    // Whether the canvas itself is the drop target. Every dragenter (the
    // pointer crossed into any element under the canvas) clears it; the
    // canvas's own dragover, which the browser dispatches right after and
    // which a child taking the drop stops, sets it again. The enter/leave
    // counter alone would also count the time spent over a Files pane.
    const [ownsDrop, setOwnsDrop] = useState(false);
    const counter = useRef(0);
    const pointerX = useRef<number | null>(null);
    const onStackCreatedRef = useRef(onStackCreated);
    useEffect(() => { onStackCreatedRef.current = onStackCreated; }, [onStackCreated]);
    const fanRef = useRef(fan);
    useEffect(() => { fanRef.current = fan; }, [fan]);

    // Safety net for the stuck cue: the dragenter/dragleave counter only
    // returns to 0 through onDrop / a leave, but a child (a cell's file
    // browser) can stopPropagation on its own drop, and HTML5 fires NO dragleave
    // after a drop — so the canvas counter never unwinds and the cue sticks.
    // A window-level drop/dragend (capture phase, so it runs before any
    // handler's stopPropagation) always resets it. These reset local state
    // and the store's flag ONLY — they never preventDefault, so the canvas
    // onDrop still fires and creates its cell.
    //
    // The same window listeners keep `osFileDragActive`: up on any OS file
    // dragenter/dragover, down on the drop or dragend. A drag that ends
    // outside the window (dropped elsewhere, cancelled there) sends neither
    // to this document, but no pointer event fires while a drag is in
    // flight, so the first one after it ends the drag: the flag and any
    // cue a missed leave left up.
    useEffect(() => {
        const store = useCollimateStore.getState;
        const reset = () => {
            counter.current = 0;
            pointerX.current = null;
            setIsDragging(false);
            setOwnsDrop(false);
            store().setOsFileDragActive(false);
        };
        const osDrag = (e: DragEvent) => {
            if (carriesOsFiles(e.dataTransfer)) store().setOsFileDragActive(true);
        };
        const pointerAfterDrag = () => {
            if (counter.current > 0 || store().osFileDragActive) reset();
        };
        const listeners: Array<[string, EventListener]> = [
            ['dragenter', osDrag as EventListener],
            ['dragover', osDrag as EventListener],
            ['drop', reset],
            ['dragend', reset],
            ['pointermove', pointerAfterDrag],
            ['pointerdown', pointerAfterDrag],
        ];
        for (const [type, listener] of listeners) window.addEventListener(type, listener, true);
        return () => {
            for (const [type, listener] of listeners) window.removeEventListener(type, listener, true);
        };
    }, []);

    /** One new cell holding the dropped tree. `dropped` carries each file's
     *  path relative to the drop, so a dropped FOLDER reproduces its whole
     *  structure inside the cell; the server keeps each of `emptyDirs` with
     *  a `.collkeep` marker so empty folders survive the trip too. A dropped
     *  `_runs/` or `.collrun` tree is left out before anything uploads. */
    const newCellFromDrop = useCallback(async (
        allDropped: DroppedFile[], allEmptyDirs: string[], insertIndex?: number,
    ) => {
        if (!channel) return;
        const dropped = allDropped.filter(d => !isReservedCellPath(d.path));
        const emptyDirs = allEmptyDirs.filter(d => !isReservedCellPath(d));
        const store = useCollimateStore.getState();
        // Ingest sits INSIDE the try: it uploads hundreds of files for a dropped
        // tree, and while it skips the individual ones it can't take, anything
        // that does escape must reach the user as an alert — this promise is
        // floated by the drop handler, so an escape would otherwise be a silent
        // unhandled rejection with no cell and no explanation.
        try {
            const files = await ingestFilesToVirtualFiles(dropped);
            if (files.length === 0 && emptyDirs.length === 0) return;
            const stackId = await createDroppedCell(channel, files, emptyDirs, insertIndex);
            // The response carries no channel, so the refresh is what puts
            // the stack on the canvas; select it only then.
            await store.fetchState();
            if (stackId) onStackCreatedRef.current?.(stackId);
        } catch (e) {
            await store.openAlert({ title: 'Could not create cell', body: apiErrorDetail(e) || 'Dropping the files failed.' });
        }
    }, [channel]);

    /** Internal cell-file drag → a new cell holding the dragged entries at
     *  their relPath (a folder brings every descendant, `.collkeep` markers
     *  included), inserted at the drop position like an OS-file drop. A
     *  copy lands what was dragged; a move lands its files as the source
     *  holds them when the cell is created and then prunes them from the
     *  source, as a cross-cell paste does (useFileTransfer.applyToSelf). */
    const newCellFromDrag = useCallback(async (
        drag: FileDragState, moveWanted: boolean, insertIndex: number | undefined,
    ) => {
        if (!channel) return;
        const store = useCollimateStore.getState();
        const { source } = drag;
        // A run's files never leave their cell.
        const outgoing = withoutRunPaths(drag.entries);
        if (outgoing.length === 0) return;
        const refuse = () => store.openAlert({
            title: 'Transfer failed',
            body: 'Could not load the file contents to transfer. Open the file(s) and try again.',
        });
        // An accepted descriptor carries no content: read its pinned
        // bytes before anything is created, and refuse the transfer outright
        // if they can't be loaded rather than landing empty files.
        const withBody = await fetchSourceBodies(source.channel, source.cellId, outgoing.map(e => e.file));
        if (!withBody) {
            await refuse();
            return;
        }
// File operations use pinned bodies and conditional durable receipts.
        if (moveWanted) await flushCellSaves(source.cellId);
        const sourceNow = moveWanted
            ? useCollimateStore.getState().locateCell(source.cellId)?.cell.files ?? null
            : null;
        const landing = landingEntries(outgoing, sourceNow, withBody);
        if (!landing) {
            await refuse();
            return;
        }
        // Rebase at the new cell's root ('' target, nothing to collide with):
        // the grabbed node keeps its own top segment, exactly as a drop into a
        // browser's root folder would.
        const { files } = rebaseEntries(landing, '', new Set<string>());
        if (files.length === 0) return;
        let stackId: string | undefined;
        try {
            if(moveWanted&&source.channel===channel){
                await flushFileDocuments(source.channel,source.cellId);
                const current=store.locateCell(source.cellId)?.cell.files;
                if(!current)throw new Error('The source cell is unavailable.');
                const created=await createStack(channel) as Channel;
                const stack=created.main_stacks.at(-1),cell=stack?.cells[0];
                if(!stack||!cell)throw new Error('The empty destination was not created.');
                stackId=stack.id;store.applyOptimisticChannel(created);
                await moveCellFiles(channel,source.cellId,cell.id,landing.map((entry,index)=>{
                    const original=current.find(file=>file.name===entry.file.name);
                    if(!original||original.file_revision!==entry.file.file_revision)throw new Error('The source changed. Retry the move.');
                    return {source_path:original.name,target_path:files[index].name,source_revision:original.file_revision,target_revision:0};
                }));
                await patchCellMeta(channel,cell.id,{name:source.nodePath.split('/').at(-1)??'Files'});
                if(insertIndex!==undefined&&insertIndex<created.main_stacks.length-1)await moveStackTo(channel,stack.id,insertIndex,'insert');
                await store.fetchState();onStackCreatedRef.current?.(stack.id);return;
            }
            stackId = await createDroppedCell(channel, files, [], insertIndex);
        } catch (e) {
            await store.openAlert({ title: 'Could not create cell', body: apiErrorDetail(e) || 'Dropping the files failed.' });
            return;
        }
        if (moveWanted) {
            // The source could change while the cell was created. Its
            // editors hand over what was typed meanwhile, and every draft
            // armed so far is written before the prune tombstones the moved
            // paths: one landing after its tombstone would bring the file
            // back at the source's next checkpoint. The prune flushes the
            // editors once more, for what was typed while those drafts were
            // written, and leaves in the source any file that flush saves or
            // that no longer holds what the new cell got, so neither a run's
            // write nor text typed into the source's editors is lost.
            await flushCellSaves(source.cellId);
            await flushFileDocuments(undefined,source.cellId);
            await pruneMovedFromSource(source, new Map(landing.map(e => [e.file.name, e.file] as const)));
        }
        await store.fetchState();
        if (stackId) onStackCreatedRef.current?.(stackId);
    }, [channel]);

    const onDrop = useCallback(async (e: React.DragEvent) => {
        if (!isAcceptedDrag(e)) return;
        e.preventDefault();
        e.stopPropagation();
        counter.current = 0;
        pointerX.current = null;
        setIsDragging(false);
        setOwnsDrop(false);
        // Capture where the files landed (and, for an internal drag, the
        // copy-vs-move modifier state) BEFORE any await — the new stack
        // inserts at the slot the NEW CELL ghost showed, measured against
        // the stacks' layout at drop time (the ingest/upload below can take
        // long enough for the canvas to pan or re-lay out).
        const insertIndex = measureDropSlot(e.clientX, fanRef.current)?.insertIndex;

        if (isInternalDrag(e)) {
            const store = useCollimateStore.getState();
            const drag = store.fileDrag;
            store.setFileDrag(null);           // consumed either way
            if (!drag || drag.entries.length === 0 || !channel) return;
            await newCellFromDrag(drag, wantsMove(e, drag.sourceReadOnly), insertIndex);
            return;
        }

        // Same rule as insertIndex, harder constraint: dataTransfer.items — the ONLY
        // view into a dropped FOLDER — is cleared the instant this handler
        // yields, so snapshot the entries here and walk them afterwards.
        const capture = captureDrop(e.dataTransfer);
        if (!hasDroppedItems(capture) || !channel) return;
        const { files: dropped, emptyDirs } = await collectDroppedFiles(capture);
        if (dropped.length === 0 && emptyDirs.length === 0) return;
        await newCellFromDrop(dropped, emptyDirs, insertIndex);
    }, [channel, newCellFromDrop, newCellFromDrag]);

    const onDragEnterCapture = useCallback((e: React.DragEvent) => {
        if (!isAcceptedDrag(e)) return;
        e.preventDefault();
        counter.current += 1;
        setIsDragging(true);
        setOwnsDrop(false);
    }, []);

    const onDragOver = useCallback((e: React.DragEvent) => {
        if (!isAcceptedDrag(e)) return;
        e.preventDefault();
        setOwnsDrop(true);
        pointerX.current = e.clientX;
        e.dataTransfer.dropEffect = isInternalDrag(e)
            && wantsMove(e, useCollimateStore.getState().fileDrag?.sourceReadOnly)
            ? 'move' : 'copy';
    }, []);

    const onDragLeaveCapture = useCallback((e: React.DragEvent) => {
        if (!isAcceptedDrag(e)) return;
        e.preventDefault();
        counter.current = Math.max(0, counter.current - 1);
        if (counter.current === 0) {
            pointerX.current = null;
            setIsDragging(false);
        }
    }, []);

    return {
        isDragging: isDragging && ownsDrop,
        pointerX,
        dragProps: {
            onDragEnterCapture, onDragOver, onDragLeaveCapture,
            // The drop is async and React's handler can't await it, so the
            // promise is floated — with a terminal catch, or a failure anywhere
            // in the walk/import would surface only as an unhandled rejection.
            // Everything expected is already reported to the user upstream
            // (per-file skips, "Could not create cell"), so this is the
            // last-resort net.
            onDrop: (e) => {
                void onDrop(e).catch(err => console.error('[drop] canvas drop failed', err));
            },
        },
    };
}
