const atomicMove=vi.hoisted(()=>vi.fn(async()=>{}));
import { fileFetchCache } from '@/lib/fileFetchCache';
import { fixtureBodyHash, fixtureText } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { DragEvent as ReactDragEvent } from 'react';
import { renderHook, act, waitFor } from '@testing-library/react';

// The hook pulls the store and the import client at module load; stub them
// so the hook renders in jsdom without a backend. The store spies are mutable so each
// test can steer the environment.
const { storeSpies } = vi.hoisted(() => ({
    storeSpies: {
        fetchState: vi.fn(async () => {}),
        applyOptimisticChannel: vi.fn(),
        openAlert: vi.fn(async () => {}),
        // Internal (cell-to-cell) drag surface: the payload rides in the store,
        // and a MOVE prunes it out of the source cell.
        fileDrag: null as unknown,
        setFileDrag: vi.fn(),
        locateCell: vi.fn(() => null as unknown),
        applyCellFiles: vi.fn(async () => true),
        // An OS file drag over the window (renderers go passive).
        osFileDragActive: false,
        setOsFileDragActive: vi.fn(),
    },
}));
vi.mock('@/lib/api', () => ({
    createStack:vi.fn(),patchCellMeta:vi.fn(async()=>({})),moveStackTo:vi.fn(async()=>({})),
}));
// Every new cell is made by the import commit; a body the drop uploads first
// (an internal drag's text) comes back as `t:<its text>`.
vi.mock('@/lib/api/transfer', () => ({
    streamBlob: vi.fn(async (body: Blob) => ({ blob_hash: `t:${await body.text()}`, size: body.size, encoding: 'utf-8' })),
    commitImport: vi.fn(),
}));
vi.mock('@/lib/fileBodies', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/fileBodies')>()),
    readPinnedText: vi.fn(),
}));
// Only the ingest itself is stubbed (it uploads to the CAS).
vi.mock('@/lib/fileIngest', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/fileIngest')>()),
    ingestFilesToVirtualFiles: vi.fn(async () => []),
}));
vi.mock('@/stores/useCollimateStore', () => ({
    useCollimateStore: { getState: () => storeSpies },
}));

vi.mock('@/lib/fileCommands',async original=>({...await original<typeof import('@/lib/fileCommands')>(),moveCellFiles:atomicMove}));
import { useCanvasFileDrop, measureDropSlot } from './useCanvasFileDrop';
import type { FanRange } from '@/lib/fanOut';
import { createStack, patchCellMeta } from '@/lib/api';
import { commitImport, type ImportRequest } from '@/lib/api/transfer';
import { registerCellSaveBarrier } from '@/lib/cellSaveBarrier';
import { readPinnedText } from '@/lib/fileBodies';
import type { FileDragState } from '@/stores/collimate/types';
import type { VirtualFile } from '@/lib/types';
import { ingestFilesToVirtualFiles } from '@/lib/fileIngest';

// Minimal React.DragEvent carrying a Files-typed dataTransfer — enough for the
// `types.includes('Files')` guard + preventDefault the handlers call. No
// `items`, so captureDrop takes its plain-FileList fallback path: a flat drop,
// exactly like a browser without the entries API.
const fileDragEvent = (files: File[] = []) => ({
    preventDefault: () => {},
    stopPropagation: () => {},
    dataTransfer: { types: ['Files'], files, dropEffect: '' },
}) as unknown as ReactDragEvent;

/** A drop carrying real filesystem ENTRIES — what an OS folder drag looks
 *  like. `entries` are the dropEntries-shaped fakes built below. */
const entryDragEvent = (entries: unknown[], files: File[] = []) => ({
    preventDefault: () => {},
    stopPropagation: () => {},
    dataTransfer: {
        types: ['Files'],
        files,
        items: entries.map(entry => ({ kind: 'file', webkitGetAsEntry: () => entry })),
        dropEffect: '',
    },
}) as unknown as ReactDragEvent;

/** A dropped file entry. */
const entryFile = (name: string, file?: File) => ({
    name, isFile: true, isDirectory: false,
    file: (done: (f: File) => void) => done(file ?? plainFile(name)),
});

/** A dropped directory entry serving `children` then the terminating empty
 *  batch (the readEntries contract). */
const entryDir = (name: string, children: unknown[]) => ({
    name, isFile: false, isDirectory: true,
    createReader: () => {
        let done = false;
        return {
            readEntries: (cb: (e: unknown[]) => void) => { cb(done ? [] : children); done = true; },
        };
    },
});

const internalDragEvent = (mod = false) => ({
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
    clientX: 0,
    ctrlKey: mod, metaKey: false, altKey: false,
    dataTransfer: { types: ['application/x-collimate-files'], files: [], dropEffect: '' },
}) as unknown as ReactDragEvent;

// A drag that carries no payload the canvas understands (a stack/tab drag)
// — every handler must ignore it.
const foreignDragEvent = () => ({
    preventDefault: vi.fn(),
    dataTransfer: { types: ['application/x-collimate-tab'], files: [], dropEffect: '' },
}) as unknown as ReactDragEvent;

const vfile = (name: string, over: Partial<VirtualFile> = {}): VirtualFile =>
    (fileFixture(name, `c:${name}`, {language: 'text', timestamp: 't', ...over}));

/** The store payload a cell's file browser writes on dragstart. */
const fileDrag = (over: Partial<FileDragState> = {}): FileDragState => ({
    source: { channel: 'source-channel', cellId: 'src-cell', nodePath: 'src/lib' },
    entries: [
        { relPath: 'lib/a.ts', file: vfile('src/lib/a.ts') },
        { relPath: 'lib/nested/.collkeep', file: vfile('src/lib/nested/.collkeep', { body_hash: fixtureBodyHash('') }) },
    ],
    ...over,
});

const plainFile = (name: string) => new File([`content of ${name}`], name);

/** What ingest hands back for a dropped text file: its body already in the
 *  CAS, referenced by hash. */
const ingested = (name: string, text: string, over: Partial<VirtualFile> = {}) => fileFixture(name, text, { language: 'text', timestamp: 't', ...over });

/** The import commit a drop sends: one unnamed stack of one cell. */
const dropCommit = (files: [string, string][], dirs: string[] = [], insertIndex: number | null = null): ImportRequest => ({
    insert_index: insertIndex,
    stacks: [{ name: '', cells: [{ name: 'Dropped files', files: files.map(([path, blob_hash]) => ({ path, blob_hash })), dirs }] }],
});
const onCanvas = { stack_ids: ['st-new'], cell_ids: ['new-cell'] };

beforeEach(() => {
    atomicMove.mockReset().mockResolvedValue(undefined);
    vi.clearAllMocks();
    vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([]);
    // clearAllMocks keeps implementations — drop the per-test resolved/rejected
    // values too so a failure case can't leak into the next test.
    vi.mocked(commitImport).mockReset().mockResolvedValue(onCanvas);
    vi.mocked(readPinnedText).mockReset().mockImplementation(async (_channel, _cellId, descriptor) => fileFetchCache.get<string>(`body:${descriptor.body_hash}`) ?? '');
    storeSpies.fileDrag = null;
    storeSpies.locateCell.mockReturnValue(null);
    storeSpies.applyCellFiles.mockResolvedValue(true);
    storeSpies.osFileDragActive = false;
    storeSpies.setOsFileDragActive.mockImplementation((active: boolean) => { storeSpies.osFileDragActive = active; });
});

/** On-screen `[data-stack-id]` columns at these [left, width] spans, all
 *  100px tall from y=40. Returns their teardown. */
function mountColumns(spans: Array<[number, number]>): () => void {
    const els = spans.map(([left, width], i) => {
        const el = document.createElement('div');
        el.setAttribute('data-stack-id', `st-${i}`);
        el.getBoundingClientRect = () => ({
            left, width, right: left + width, x: left, top: 40, height: 100, bottom: 140, y: 40, toJSON: () => ({}),
        }) as DOMRect;
        document.body.appendChild(el);
        return el;
    });
    return () => els.forEach(el => el.remove());
}

/** A stack of three cells fanned out at main_stacks 0: display columns 0-2. */
const FAN: FanRange = { stackId: 's', name: 'S', realIdx: 0, start: 0, end: 2, count: 3 };

describe('useCanvasFileDrop new-cell cue', () => {
    it('raises isDragging once a file drag is over the bare canvas and clears it once the counter unwinds', () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        expect(result.current.isDragging).toBe(false);
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragOver(fileDragEvent()));
        expect(result.current.isDragging).toBe(true);
        act(() => result.current.dragProps.onDragLeaveCapture(fileDragEvent()));
        expect(result.current.isDragging).toBe(false);
    });

    it('raises the cue for an internal cell-file drag too', () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        const e = internalDragEvent();
        act(() => result.current.dragProps.onDragEnterCapture(e));
        act(() => result.current.dragProps.onDragOver(e));
        expect(result.current.isDragging).toBe(true);
        expect(e.dataTransfer.dropEffect).toBe('copy');
        act(() => result.current.dragProps.onDragLeaveCapture(e));
        expect(result.current.isDragging).toBe(false);
        expect(e.preventDefault).toHaveBeenCalled();
    });

    it("stays down while the drag is over a child that takes the drop itself, e.g. a Files pane (F0848)", () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        // Over a Files pane: its dragenter bubbles to the canvas, but the pane
        // stops the dragover that follows, so the canvas never sees one.
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        expect(result.current.isDragging).toBe(false);
        // Out onto bare canvas: enter, leave (the pane), then the canvas's own
        // dragover.
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragLeaveCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragOver(fileDragEvent()));
        expect(result.current.isDragging).toBe(true);
        // Back into the pane: down again, though the drag never left the canvas.
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragLeaveCapture(fileDragEvent()));
        expect(result.current.isDragging).toBe(false);
        // An internal folder-to-folder drag inside the pane never raises it.
        const internal = internalDragEvent();
        act(() => result.current.dragProps.onDragEnterCapture(internal));
        expect(result.current.isDragging).toBe(false);
    });

    it('an internal drag with a modifier advertises a move (copy from a read-only source)', () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        storeSpies.fileDrag = fileDrag();
        const e = internalDragEvent(true);
        act(() => result.current.dragProps.onDragOver(e));
        expect(e.dataTransfer.dropEffect).toBe('move');
        storeSpies.fileDrag = fileDrag({ sourceReadOnly: true });
        const ro = internalDragEvent(true);
        act(() => result.current.dragProps.onDragOver(ro));
        expect(ro.dataTransfer.dropEffect).toBe('copy');
    });

    it('ignores drags carrying neither OS files nor the cell-file marker', () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        const e = foreignDragEvent();
        act(() => result.current.dragProps.onDragEnterCapture(e));
        act(() => result.current.dragProps.onDragOver(e));
        act(() => result.current.dragProps.onDragLeaveCapture(e));
        act(() => result.current.dragProps.onDrop(e));
        expect(result.current.isDragging).toBe(false);
        expect(e.preventDefault).not.toHaveBeenCalled();
    });

    it('dragover advertises a copy drop', () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        const e = fileDragEvent();
        act(() => result.current.dragProps.onDragOver(e));
        expect(e.dataTransfer.dropEffect).toBe('copy');
    });

    it('resets isDragging when a child swallows the drop (no dragleave follows)', () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragOver(fileDragEvent()));
        expect(result.current.isDragging).toBe(true);
        // A cell file-browser drop stopPropagations, and HTML5 emits no dragleave
        // afterward — only the window-level (capture) 'drop' safety net resets us.
        act(() => { window.dispatchEvent(new Event('drop')); });
        expect(result.current.isDragging).toBe(false);
    });

    it('resets isDragging on a window dragend (drag abandoned)', () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragOver(fileDragEvent()));
        expect(result.current.isDragging).toBe(true);
        act(() => { window.dispatchEvent(new Event('dragend')); });
        expect(result.current.isDragging).toBe(false);
    });

    it('tears down the window listeners on unmount', () => {
        const { result, unmount } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragOver(fileDragEvent()));
        unmount();
        // A post-unmount window drop must not throw / touch state.
        expect(() => window.dispatchEvent(new Event('drop'))).not.toThrow();
    });
});

describe('measureDropSlot: the one slot the NEW CELL ghost shows and the drop uses', () => {
    it('is the gap nearest the pointer, half a gap outside the end columns', () => {
        const unmount = mountColumns([[0, 100], [120, 100], [240, 100]]);
        try {
            expect(measureDropSlot(130, null)).toEqual({ insertIndex: 1, gapX: 110, top: 40, bottom: 140 });
            expect(measureDropSlot(10, null)).toEqual({ insertIndex: 0, gapX: -10, top: 40, bottom: 140 });
            expect(measureDropSlot(400, null)).toEqual({ insertIndex: 3, gapX: 350, top: 40, bottom: 140 });
        } finally {
            unmount();
        }
    });

    it('a lone column is flanked by half a stack gap', () => {
        const unmount = mountColumns([[100, 200]]);
        try {
            expect(measureDropSlot(500, null)?.gapX).toBe(310);
            expect(measureDropSlot(0, null)?.gapX).toBe(90);
        } finally {
            unmount();
        }
    });

    it('the overview row (narrow columns, the zoomed gap) measures the same way', () => {
        const unmount = mountColumns([[200, 40], [246, 40], [292, 40], [338, 40]]);
        try {
            expect(measureDropSlot(300, null)).toEqual({ insertIndex: 2, gapX: 289, top: 40, bottom: 140 });
            expect(measureDropSlot(390, null)).toEqual({ insertIndex: 4, gapX: 381, top: 40, bottom: 140 });
        } finally {
            unmount();
        }
    });

    it('a fanned stack counts once: right of the fan the index is the display slot less the extra columns', () => {
        // Fan columns 0-2, then two real stacks (main_stacks 1 and 2).
        const unmount = mountColumns([[0, 100], [120, 100], [240, 100], [360, 100], [480, 100]]);
        try {
            expect(measureDropSlot(470, FAN)).toEqual({ insertIndex: 2, gapX: 470, top: 40, bottom: 140 });
            expect(measureDropSlot(300, FAN)?.insertIndex).toBe(1);
            expect(measureDropSlot(10, FAN)?.insertIndex).toBe(0);
        } finally {
            unmount();
        }
    });

    it("a slot between two of the fan's cells snaps to the nearer of its outside gaps", () => {
        const unmount = mountColumns([[0, 100], [120, 100], [240, 100], [360, 100], [480, 100]]);
        try {
            // Nearer the fan's left edge: before the fanned stack.
            expect(measureDropSlot(130, FAN)).toEqual({ insertIndex: 0, gapX: -10, top: 40, bottom: 140 });
            // Nearer its right edge: after it.
            expect(measureDropSlot(250, FAN)).toEqual({ insertIndex: 1, gapX: 350, top: 40, bottom: 140 });
        } finally {
            unmount();
        }
    });

    it('is null with no measurable column (the create appends)', () => {
        expect(measureDropSlot(100, null)).toBeNull();
    });

    it('the drop creates the stack at the fan-mapped index the ghost showed', async () => {
        vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([ingested('a.txt', 'A')]);
        const unmount = mountColumns([[0, 100], [120, 100], [240, 100], [360, 100], [480, 100]]);
        try {
            const { result } = renderHook(() => useCanvasFileDrop('chan', undefined, FAN));
            const e = fileDragEvent([plainFile('a.txt')]);
            Object.assign(e, { clientX: 470 });
            act(() => result.current.dragProps.onDrop(e));
            await waitFor(() => expect(commitImport).toHaveBeenCalled());
            expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['a.txt', fixtureBodyHash('A')]], [], 2));
        } finally {
            unmount();
        }
    });
});

describe('useCanvasFileDrop pointer and OS drag tracking', () => {
    it('the pointer over bare canvas rides a ref, cleared when the drag leaves or drops', () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        const over = Object.assign(fileDragEvent(), { clientX: 420 });
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragOver(over));
        expect(result.current.pointerX.current).toBe(420);
        act(() => result.current.dragProps.onDragLeaveCapture(fileDragEvent()));
        expect(result.current.pointerX.current).toBeNull();
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragOver(over));
        act(() => { window.dispatchEvent(new Event('drop')); });
        expect(result.current.pointerX.current).toBeNull();
    });

    const windowDrag = (type: string, types: string[]) => {
        const ev = new Event(type);
        Object.defineProperty(ev, 'dataTransfer', { value: { types } });
        return ev;
    };

    it('an OS file drag anywhere over the window raises osFileDragActive; the drop or dragend lowers it', () => {
        renderHook(() => useCanvasFileDrop('chan'));
        act(() => { window.dispatchEvent(windowDrag('dragenter', ['Files'])); });
        expect(storeSpies.osFileDragActive).toBe(true);
        act(() => { window.dispatchEvent(new Event('drop')); });
        expect(storeSpies.osFileDragActive).toBe(false);
        act(() => { window.dispatchEvent(windowDrag('dragover', ['Files'])); });
        expect(storeSpies.osFileDragActive).toBe(true);
        act(() => { window.dispatchEvent(new Event('dragend')); });
        expect(storeSpies.osFileDragActive).toBe(false);
    });

    it("a drag the app started itself never raises it", () => {
        renderHook(() => useCanvasFileDrop('chan'));
        act(() => { window.dispatchEvent(windowDrag('dragenter', ['application/x-collimate-files'])); });
        act(() => { window.dispatchEvent(windowDrag('dragover', ['application/x-collimate-tab'])); });
        expect(storeSpies.setOsFileDragActive).not.toHaveBeenCalledWith(true);
    });

    it('a drag that ended outside the window is over at the next pointer event', () => {
        renderHook(() => useCanvasFileDrop('chan'));
        act(() => { window.dispatchEvent(windowDrag('dragenter', ['Files'])); });
        expect(storeSpies.osFileDragActive).toBe(true);
        act(() => { window.dispatchEvent(new Event('pointermove')); });
        expect(storeSpies.osFileDragActive).toBe(false);
    });

    it('the next pointer event also takes down a NEW CELL cue a missed leave left up; one with no drag touches nothing', () => {
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => { window.dispatchEvent(new Event('pointermove')); });
        expect(storeSpies.setOsFileDragActive).not.toHaveBeenCalled();
        // Entered and over the canvas; the leave that would unwind the count
        // never came (the drag ended outside the window).
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragOver(Object.assign(fileDragEvent(), { clientX: 300 })));
        expect(result.current.isDragging).toBe(true);
        act(() => { window.dispatchEvent(new Event('pointerdown')); });
        expect(result.current.isDragging).toBe(false);
        expect(result.current.pointerX.current).toBeNull();
        // The count restarted: the next drag's single leave unwinds it.
        act(() => result.current.dragProps.onDragEnterCapture(fileDragEvent()));
        act(() => result.current.dragProps.onDragOver(fileDragEvent()));
        expect(result.current.isDragging).toBe(true);
        act(() => result.current.dragProps.onDragLeaveCapture(fileDragEvent()));
        expect(result.current.isDragging).toBe(false);
    });
});

describe('useCanvasFileDrop drops', () => {
    it('plain files land as one new cell and the canvas refreshes', async () => {
        vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([
            ingested('a.txt', 'A'),
            ingested('img.png', '', { language: 'binary', body_hash: 'h1', encoding: 'binary' }),
        ]);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(fileDragEvent([plainFile('a.txt'), plainFile('img.png')])));
        await waitFor(() => expect(storeSpies.fetchState).toHaveBeenCalled());
        // Every file rides as the hash ingest stored it under. jsdom has no
        // measurable stacks, so the drop-position index is null: the
        // commit appends.
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['a.txt', fixtureBodyHash('A')], ['img.png', 'h1']]));
        expect(result.current.isDragging).toBe(false);
    });

    it('the insert index is measured at drop time, not after the ingest', async () => {
        // Two on-screen stacks, both left of the drop point → index 2. The
        // ingest then "pans" the canvas so both sit right of it; the create
        // must still use the index the user saw at drop time.
        let left = 0;
        const stacks = [0, 1].map(i => {
            const el = document.createElement('div');
            el.setAttribute('data-stack-id', `st-${i}`);
            el.getBoundingClientRect = () => ({ left: left + i * 100, width: 50, height: 50 }) as DOMRect;
            document.body.appendChild(el);
            return el;
        });
        try {
            vi.mocked(ingestFilesToVirtualFiles).mockImplementation(async () => {
                left = 1000;
                return [ingested('a.txt', 'A')];
            });
            const { result } = renderHook(() => useCanvasFileDrop('chan'));
            const e = fileDragEvent([plainFile('a.txt')]);
            Object.assign(e, { clientX: 500 });
            act(() => result.current.dragProps.onDrop(e));
            await waitFor(() => expect(commitImport).toHaveBeenCalled());
            expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['a.txt', fixtureBodyHash('A')]], [], 2));
        } finally {
            stacks.forEach(el => el.remove());
        }
    });

    it('a dropped FOLDER lands as one cell with its tree, empty dirs sent for the server to keep', async () => {
        vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([
            ingested('proj/main.ts', 'x', { language: 'typescript' }),
        ]);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        const tree = entryDir('proj', [entryFile('main.ts'), entryDir('empty', [])]);
        // The browser ALSO puts a size-0 File named after the folder in
        // dataTransfer.files — the entries must win, or that junk becomes a file.
        act(() => result.current.dragProps.onDrop(entryDragEvent([tree], [plainFile('proj')])));
        await waitFor(() => expect(commitImport).toHaveBeenCalled());
        expect(ingestFilesToVirtualFiles).toHaveBeenCalledWith([
            expect.objectContaining({ path: 'proj/main.ts' }),
        ]);
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['proj/main.ts', fixtureBodyHash('x')]], ['proj/empty']));
    });

    it('a folder holding nothing but empty folders still lands as a cell of folders', async () => {
        vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([]);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(
            entryDragEvent([entryDir('shell', [entryDir('a', []), entryDir('b', [])])])));
        await waitFor(() => expect(commitImport).toHaveBeenCalled());
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([], ['shell/a', 'shell/b']));
    });

    it('the new stack is handed over for selection once the refresh holds it', async () => {
        vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([ingested('a.txt', 'A')]);
        const onStackCreated = vi.fn();
        const { result } = renderHook(() => useCanvasFileDrop('chan', onStackCreated));
        act(() => result.current.dragProps.onDrop(fileDragEvent([plainFile('a.txt')])));
        await waitFor(() => expect(onStackCreated).toHaveBeenCalledWith('st-new'));
        // After the refresh, so the selection never names a stack the
        // canvas doesn't hold yet.
        expect(storeSpies.fetchState.mock.invocationCallOrder[0])
            .toBeLessThan(onStackCreated.mock.invocationCallOrder[0]);
    });

    it('a failed cell creation surfaces the server detail in an alert', async () => {
        vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([ingested('a.txt', 'A')]);
        vi.mocked(commitImport).mockRejectedValue({ response: { data: { detail: 'quota full' } } });
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(fileDragEvent([plainFile('a.txt')])));
        await waitFor(() => expect(storeSpies.openAlert).toHaveBeenCalledWith(
            { title: 'Could not create cell', body: 'quota full' }));
        expect(storeSpies.fetchState).not.toHaveBeenCalled();
    });

    it('an ingest that blows up alerts instead of vanishing as an unhandled rejection', async () => {
        // Ingest skips the individual files it can't take, so this is the
        // shouldn't-happen case — but the drop promise is floated by the DOM
        // handler, so a throw has to reach the user, not just the console.
        vi.mocked(ingestFilesToVirtualFiles).mockRejectedValue(new Error('boom'));
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(fileDragEvent([plainFile('a.txt')])));
        await waitFor(() => expect(storeSpies.openAlert).toHaveBeenCalledWith(
            { title: 'Could not create cell', body: 'Dropping the files failed.' }));
        expect(commitImport).not.toHaveBeenCalled();
    });

    it('drops no-op without a channel, without files, or when nothing ingests', async () => {
        const { result: noChan } = renderHook(() => useCanvasFileDrop(undefined));
        act(() => noChan.current.dragProps.onDrop(fileDragEvent([plainFile('a.txt')])));
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(fileDragEvent([])));
        act(() => result.current.dragProps.onDrop(fileDragEvent([plainFile('empty.txt')])));
        await act(async () => {}); // flush the async handlers
        expect(commitImport).not.toHaveBeenCalled();
        expect(storeSpies.fetchState).not.toHaveBeenCalled();
    });

    it('every OS drop is one new cell; ingest sees each file with its path (TR-08)', async () => {
        vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([ingested('archive.zip', 'z'), ingested('notes.md', 'n')]);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        const files = [plainFile('archive.zip'), plainFile('notes.md')];
        act(() => result.current.dragProps.onDrop(fileDragEvent(files)));
        await waitFor(() => expect(commitImport).toHaveBeenCalledTimes(1));
        expect(ingestFilesToVirtualFiles).toHaveBeenCalledWith(files.map(f => ({ path: f.name, file: f })));
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['archive.zip', fixtureBodyHash('z')], ['notes.md', fixtureBodyHash('n')]]));
    });

    it('a dropped _runs/ or .collrun tree is left out before anything uploads, so the commit is not refused (TR-08)', async () => {
        vi.mocked(ingestFilesToVirtualFiles).mockResolvedValue([ingested('a.txt', 'A')]);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(entryDragEvent([
            entryDir('_runs', [entryFile('out.txt')]),
            entryFile('a.txt'),
            entryDir('.collrun', [entryDir('stage', [])]),
            entryDir('keep', []),
        ])));
        await waitFor(() => expect(commitImport).toHaveBeenCalled());
        expect(ingestFilesToVirtualFiles).toHaveBeenCalledWith([expect.objectContaining({ path: 'a.txt' })]);
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['a.txt', fixtureBodyHash('A')]], ['keep']));
    });
});

describe('useCanvasFileDrop — internal cell-file drag onto empty canvas', () => {
    it('lands the dragged entries as a new cell at their relPath and clears the drag', async () => {
        storeSpies.fileDrag = fileDrag();
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent()));
        await waitFor(() => expect(storeSpies.fetchState).toHaveBeenCalled());
        // The text the drag hydrated is uploaded; a marker travels as its folder.
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['lib/a.ts', 't:c:src/lib/a.ts']], ['lib/nested']));
        // Copy is the cross-cell default — the source keeps its files.
        expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
        expect(storeSpies.setFileDrag).toHaveBeenCalledWith(null);
    });

    it('a binary entry rides as a blob_hash reference, never inline content', async () => {
        storeSpies.fileDrag = fileDrag({
            source: { channel: 'source-channel', cellId: 'src-cell', nodePath: 'img.png' },
            entries: [{ relPath: 'img.png', file: vfile('img.png', { language: 'binary', body_hash: 'h1', encoding: 'binary' }) }],
        });
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent()));
        await waitFor(() => expect(commitImport).toHaveBeenCalled());
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['img.png', 'h1']]));
    });

    it('a stripped entry is hydrated before the cell is created', async () => {
        storeSpies.fileDrag = fileDrag({
            source: { channel: 'source-channel', cellId: 'src-cell', nodePath: 'big.txt' },
            entries: [{ relPath: 'big.txt', file: vfile('big.txt', { body_hash: fixtureBodyHash(''),  }) }],
        });
        vi.mocked(readPinnedText).mockResolvedValue('REAL');
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent()));
        await waitFor(() => expect(commitImport).toHaveBeenCalled());
        expect(readPinnedText).toHaveBeenCalledWith('source-channel', 'src-cell', expect.objectContaining({path:'big.txt'}), { cache: 'no-store' });
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['big.txt', 't:REAL']]));
    });

    it('refuses the transfer (no cell) when the content cannot be loaded', async () => {
        storeSpies.fileDrag = fileDrag({
            source: { channel: 'source-channel', cellId: 'src-cell', nodePath: 'big.txt' },
            entries: [{ relPath: 'big.txt', file: vfile('big.txt', { body_hash: fixtureBodyHash(''),  }) }],
        });
        vi.mocked(readPinnedText).mockRejectedValue(new Error('offline'));
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent()));
        await waitFor(() => expect(storeSpies.openAlert).toHaveBeenCalledWith(
            expect.objectContaining({ title: 'Transfer failed' })));
        expect(commitImport).not.toHaveBeenCalled();
    });

    it('a modifier makes it a MOVE — the source cell is pruned (and its emptied folder re-minted)', async () => {
        storeSpies.fileDrag = fileDrag();
        storeSpies.locateCell.mockReturnValue({
            channel: 'chan', stackId: 's1', cellIndex: 0,
            cell: {
                id: 'src-cell', name: 'src',
                files: [...(storeSpies.fileDrag as FileDragState).entries.map(e=>e.file), vfile('src/keep.ts')],
            },
        });
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent(true)));
        await waitFor(() => expect(storeSpies.applyCellFiles).toHaveBeenCalled());
        const [cellId, pruned] = storeSpies.applyCellFiles.mock.calls[0] as unknown as [string, VirtualFile[]];
        expect(cellId).toBe('src-cell');
        expect(pruned.map(f => f.name)).toEqual(['src/keep.ts']);
        expect(commitImport).toHaveBeenCalled();
    });

    it('a run\'s files never leave their cell: they drop out of the new cell, and a move leaves them', async () => {
        const runFile = vfile('_runs/runner-a-aaaaaaaa/chat/conversation.chat');
        storeSpies.fileDrag = fileDrag({
            source: { channel: 'source-channel', cellId: 'src-cell', nodePath: '' },
            entries: [
                { relPath: 'notes.md', file: vfile('notes.md') },
                { relPath: '_runs/runner-a-aaaaaaaa/chat/conversation.chat', file: runFile },
            ],
        });
        storeSpies.locateCell.mockReturnValue({
            channel: 'chan', stackId: 's1', cellIndex: 0,
            cell: { id: 'src-cell', name: 'src', files: [vfile('notes.md'), runFile] },
        });
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent(true)));
        await waitFor(() => expect(storeSpies.applyCellFiles).toHaveBeenCalled());
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['notes.md', 't:c:notes.md']]));
        const [, pruned] = storeSpies.applyCellFiles.mock.calls[0] as unknown as [string, VirtualFile[]];
        expect(pruned.map(f => f.name)).toEqual([runFile.name]);
    });

    it('a drag of nothing but a run\'s files creates no cell', async () => {
        storeSpies.fileDrag = fileDrag({
            source: { channel: 'source-channel', cellId: 'src-cell', nodePath: '_runs' },
            entries: [{ relPath: '_runs/r-11111111/x.chat', file: vfile('_runs/r-11111111/x.chat') }],
        });
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent()));
        await act(async () => {});
        expect(commitImport).not.toHaveBeenCalled();
    });

    it('an internal drop with no store payload (or no channel) only clears the drag', async () => {
        storeSpies.fileDrag = null;
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent()));
        storeSpies.fileDrag = fileDrag();
        const { result: noChan } = renderHook(() => useCanvasFileDrop(undefined));
        act(() => noChan.current.dragProps.onDrop(internalDragEvent()));
        await act(async () => {});
        expect(commitImport).not.toHaveBeenCalled();
        expect(storeSpies.setFileDrag).toHaveBeenCalledWith(null);
    });
});

// A move onto bare canvas lands each file as the source holds it when the new
// cell is created, as a cross-cell paste does: the source's editors hand over
// what is still in their 1.5 s autosave debounce first, and a run's write made
// since the drag moves too. The create is a server round trip, so a file the
// source changes during it no longer holds what the new cell got: the prune
// leaves it in the source and says so. The drafts armed before the prune are
// written before its tombstones, and the prune flushes the editors once more,
// so text typed while those drafts are written stays in the source too.
describe('useCanvasFileDrop — a move lands what the source holds when the cell is created (F0823, F0891, F0824)', () => {
    /** The source cell as the store holds it; `files` may be swapped by a test. */
    const sourceCell = (files: () => VirtualFile[]) => {
        storeSpies.locateCell.mockImplementation(() => ({
            channel: 'chan', stackId: 's1', cellIndex: 0,
            cell: { id: 'src-cell', name: 'src', files: files() },
        }));
    };
    const dragOf = (nodePath: string, files: VirtualFile[]): FileDragState => ({
        source: { channel: 'source-channel', cellId: 'src-cell', nodePath },
        entries: files.map(file => ({ relPath: file.name, file })),
    });
    /** The new cell's files as [path, the text it was uploaded with]. */
    const created = () => vi.mocked(commitImport).mock.calls[0][1].stacks[0].cells[0].files.map(f => [f.path, f.blob_hash.replace(/^t:/, '')]);
    const pruned = () => (storeSpies.applyCellFiles.mock.calls[0] as unknown as [string, VirtualFile[]])[1];

    it('a changed source revision after serialization refuses the stale transfer (F0824)', async () => {
        let source = [vfile('notes.md', { body_hash: fixtureBodyHash('SAVED') }), vfile('keep.md')];
        sourceCell(() => source);
        let typed: string | null = 'TYPED';
        const off = registerCellSaveBarrier('src-cell', () => {
            if (typed === null) return;
            const content = typed;
            typed = null;
            source = source.map(f => (f.name === 'notes.md' ? { ...f, body_hash: fixtureBodyHash(content), file_revision: 2 } : f));
        });
        vi.mocked(commitImport).mockResolvedValue(onCanvas);
        try {
            storeSpies.fileDrag = dragOf('notes.md', [vfile('notes.md', { body_hash: fixtureBodyHash('SAVED') })]);
            const { result } = renderHook(() => useCanvasFileDrop('chan'));
            act(() => result.current.dragProps.onDrop(internalDragEvent(true)));
            await waitFor(() => expect(storeSpies.openAlert).toHaveBeenCalledWith(expect.objectContaining({title:'Transfer failed'})));
            expect(commitImport).not.toHaveBeenCalled();
            expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
            expect(fixtureText(source[0])).toBe('TYPED');
        } finally {
            off();
        }
    });

    it('refuses a canvas transfer when a run changes the observed source revision during its pinned reads',async()=>{
 const before=[vfile('d/a.ts'),vfile('d/b.ts')];let source=before;sourceCell(()=>source);
 vi.mocked(readPinnedText).mockImplementation(async()=>{source=[before[0],fileFixture('d/b.ts','new',{file_revision:2})];return 'old';});
 storeSpies.fileDrag=dragOf('d',before);const {result}=renderHook(()=>useCanvasFileDrop('chan'));
 act(()=>result.current.dragProps.onDrop(internalDragEvent(true)));
 await waitFor(()=>expect(storeSpies.openAlert).toHaveBeenCalledWith(expect.objectContaining({title:'Transfer failed'})));
 expect(commitImport).not.toHaveBeenCalled();expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
});

    it('a large file rewritten during its body fetch refuses the move: no cell, nothing pruned (F0823, F0824)', async () => {
        const big = vfile('big.txt', { body_hash: fixtureBodyHash(''),  });
        let source = [big];
        sourceCell(() => source);
        vi.mocked(readPinnedText).mockImplementation(async () => {
            source = [{ ...big, file_revision: 2 }];
            return 'OLD BIG';
        });
        storeSpies.fileDrag = dragOf('big.txt', [big]);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent(true)));
        await waitFor(() => expect(storeSpies.openAlert).toHaveBeenCalledWith(
            expect.objectContaining({ title: 'Transfer failed' })));
        expect(commitImport).not.toHaveBeenCalled();
        expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
    });

    it('a file a run rewrites while the cell is created stays in the source, and the user is told (F0823, F0824)', async () => {
        const atDrag = [vfile('d/a.md', { body_hash: fixtureBodyHash('A') }), vfile('d/b.md', { body_hash: fixtureBodyHash('B') })];
        let source = [...atDrag, vfile('keep.md')];
        sourceCell(() => source);
        vi.mocked(commitImport).mockImplementation(async () => {
            source = source.map(f => (f.name === 'd/b.md' ? { ...f, body_hash: fixtureBodyHash('RUN WROTE DURING CREATE'), file_revision: 2 } : f));
            return onCanvas;
        });
        storeSpies.fileDrag = dragOf('d', atDrag);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent(true)));
        await waitFor(() => expect(storeSpies.fetchState).toHaveBeenCalled());
        expect(created()).toEqual([['d/a.md', 'A'], ['d/b.md', 'B']]);
        // Only the unchanged file leaves the source; the run's bytes stay.
        expect(pruned().map(f => [f.name, fixtureText(f)])).toEqual([
            ['d/b.md', 'RUN WROTE DURING CREATE'], ['keep.md', 'c:keep.md'],
        ]);
        expect(storeSpies.openAlert).toHaveBeenCalledWith(expect.objectContaining({
            title: 'Kept in the source cell', body: expect.stringContaining('`d/b.md`'),
        }));
    });

    it('a large file rewritten while the cell is created stays in the source (F0823, F0824)', async () => {
        const big = vfile('big.txt', { body_hash: fixtureBodyHash(''),  });
        let source = [big];
        sourceCell(() => source);
        vi.mocked(readPinnedText).mockResolvedValue('BIG');
        vi.mocked(commitImport).mockImplementation(async () => {
            source = [{ ...big, file_revision: 2 }];
            return onCanvas;
        });
        storeSpies.fileDrag = dragOf('big.txt', [big]);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent(true)));
        await waitFor(() => expect(storeSpies.fetchState).toHaveBeenCalled());
        expect(created()).toEqual([['big.txt', 'BIG']]);
        expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
        expect(storeSpies.openAlert).toHaveBeenCalledWith(expect.objectContaining({ title: 'Kept in the source cell' }));
    });

    it('text typed into the source editor while the cell is created stays in the source (F0824)', async () => {
        let source = [vfile('notes.md', { body_hash: fixtureBodyHash('SAVED') }), vfile('keep.md')];
        sourceCell(() => source);
        let typed: string | null = null;
        const off = registerCellSaveBarrier('src-cell', () => {
            if (typed === null) return;
            const content = typed;
            typed = null;
            source = source.map(f => (f.name === 'notes.md' ? { ...f, body_hash: fixtureBodyHash(content), file_revision: 2 } : f));
        });
        vi.mocked(commitImport).mockImplementation(async () => {
            typed = 'TYPED DURING CREATE';
            return onCanvas;
        });
        try {
            storeSpies.fileDrag = dragOf('notes.md', [vfile('notes.md', { body_hash: fixtureBodyHash('SAVED') })]);
            const { result } = renderHook(() => useCanvasFileDrop('chan'));
            act(() => result.current.dragProps.onDrop(internalDragEvent(true)));
            await waitFor(() => expect(storeSpies.fetchState).toHaveBeenCalled());
            expect(created()).toEqual([['notes.md', 'SAVED']]);
            expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
            expect(fixtureText(source.find(f => f.name === 'notes.md'))).toBe('TYPED DURING CREATE');
            expect(storeSpies.openAlert).toHaveBeenCalledWith(expect.objectContaining({
                title: 'Kept in the source cell', body: expect.stringContaining('`notes.md`'),
            }));
        } finally {
            off();
        }
    });

    it('a binary a run replaces while the cell is created stays in the source (F0823, F0824)', async () => {
        const img = vfile('img.png', { language: 'binary', body_hash: 'h1', encoding: 'binary' });
        let source = [img, vfile('keep.md')];
        sourceCell(() => source);
        vi.mocked(commitImport).mockImplementation(async () => {
            source = [{ ...img, body_hash: 'h2', encoding: 'binary', file_revision: 2 }, vfile('keep.md')];
            return onCanvas;
        });
        storeSpies.fileDrag = dragOf('img.png', [img]);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent(true)));
        await waitFor(() => expect(storeSpies.fetchState).toHaveBeenCalled());
        // The new cell holds the blob the drag took; the run's blob stays.
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['img.png', 'h1']]));
        expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
        expect(storeSpies.openAlert).toHaveBeenCalledWith(expect.objectContaining({
            title: 'Kept in the source cell', body: expect.stringContaining('`img.png`'),
        }));
    });

    it('a newer binary revision stays in the source even when its hash is unchanged (F0823, F0824)', async () => {
        const img = vfile('img.png', { language: 'binary', body_hash: 'h1', encoding: 'binary' });
        let source = [img, vfile('keep.md')];
        sourceCell(() => source);
        // A rewrite of the same bytes carries a newer stamp but the same
        // CAS blob, which the new cell holds.
        vi.mocked(commitImport).mockImplementation(async () => {
            source = [{ ...img, file_revision: 2 }, vfile('keep.md')];
            return onCanvas;
        });
        storeSpies.fileDrag = dragOf('img.png', [img]);
        const { result } = renderHook(() => useCanvasFileDrop('chan'));
        act(() => result.current.dragProps.onDrop(internalDragEvent(true)));
        await waitFor(() => expect(storeSpies.fetchState).toHaveBeenCalled());
        expect(commitImport).toHaveBeenCalledWith('chan', dropCommit([['img.png', 'h1']]));
        expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
        expect(storeSpies.openAlert).toHaveBeenCalledWith(expect.objectContaining({title:'Kept in the source cell'}));
    });

    it('a copy still lands what was dragged and leaves the source alone', async () => {
        let source = [vfile('notes.md', { body_hash: fixtureBodyHash('SAVED') })];
        sourceCell(() => source);
        const flushed = vi.fn(() => {
            source = [vfile('notes.md', { body_hash: fixtureBodyHash('TYPED') })];
        });
        const off = registerCellSaveBarrier('src-cell', flushed);
        vi.mocked(commitImport).mockResolvedValue(onCanvas);
        try {
            storeSpies.fileDrag = dragOf('notes.md', [vfile('notes.md', { body_hash: fixtureBodyHash('SAVED') })]);
            const { result } = renderHook(() => useCanvasFileDrop('chan'));
            act(() => result.current.dragProps.onDrop(internalDragEvent()));
            await waitFor(() => expect(storeSpies.fetchState).toHaveBeenCalled());
            expect(created()).toEqual([['notes.md', 'SAVED']]);
            expect(flushed).not.toHaveBeenCalled();
            expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
        } finally {
            off();
        }
    });
});

it('a same-channel canvas move creates an empty destination and atomically transfers observed revisions',async()=>{
 const source=vfile('notes.md');
 storeSpies.fileDrag={source:{channel:'chan',cellId:'src-cell',nodePath:'notes.md'},entries:[{relPath:'notes.md',file:source}]};
 storeSpies.locateCell.mockReturnValue({channel:'chan',stackId:'source',cellIndex:0,cell:{id:'src-cell',name:'Source',files:[source]}});
 vi.mocked(createStack).mockResolvedValue({id:'chan',name:'chan',main_stacks:[{id:'new-stack',name:'New',cells:[{id:'new-cell',name:'New',files:[]}]}],nav_stacks:[],main_selection_id:null,nav_selection_id:null});
 const {result}=renderHook(()=>useCanvasFileDrop('chan'));
 act(()=>result.current.dragProps.onDrop(internalDragEvent(true)));
 await waitFor(()=>expect(atomicMove).toHaveBeenCalledWith('chan','src-cell','new-cell',[{source_path:'notes.md',target_path:'notes.md',source_revision:1,target_revision:0}]));
 expect(commitImport).not.toHaveBeenCalled();expect(storeSpies.applyCellFiles).not.toHaveBeenCalled();
 await waitFor(()=>expect(patchCellMeta).toHaveBeenCalledWith('chan','new-cell',{name:'notes.md'}));
});
