const atomic = vi.hoisted(() => ({move:vi.fn(async()=>{}), target:[] as import('@/lib/types').VirtualFile[]}));
import { fileFetchCache } from '@/lib/fileFetchCache';
import { fixtureBodyHash, fixtureText } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { COLLIMATE_DND_MIME } from '@/lib/fileDrag';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { DragEvent as ReactDragEvent } from 'react';
import { renderHook, waitFor, act } from '@testing-library/react';
import type { VirtualFile } from '@/lib/types';
import type { TreeNode } from '@/lib/fileTree';
import type { UpdateFiles } from '@/lib/fileTransfer';
import type { FileClipboard, FileDragState } from '@/stores/collimate/types';

vi.mock('@/lib/fileCommands',async original=>({...await original<typeof import('@/lib/fileCommands')>(),moveCellFiles:atomic.move,currentCellFiles:()=>atomic.target}));

// The path-transform helpers (collectTransferEntries, rebaseEntries, …) are
// pure and already unit-tested — use them for real. Only the store and the
// hydration fetch are stubbed; the store is a real zustand store so the hook's
// subscription (clipboardLabel) reacts to writes like in the app. A
// Source identity is its accepted revision/hash, independent of metadata
// object replacement during the durability barrier.
vi.mock('@/lib/fileBodies', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/fileBodies')>()),
    readPinnedText: vi.fn(),
}));
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
}));
vi.mock('@/stores/useCollimateStore', async () => {
    const { create } = await import('zustand');
    return {
        useCollimateStore: create((set: (s: object) => void) => ({
            fileClipboard: null,
            fileDrag: null,
            setFileClipboard: (c: unknown) => set({ fileClipboard: c }),
            clearFileClipboard: () => set({ fileClipboard: null }),
            setFileDrag: (d: unknown) => set({ fileDrag: d }),
            locateCell: (_id: string) => null,
            applyCellFiles: vi.fn(async () => true),
            openAlert: vi.fn(async () => undefined),
        })),
    };
});

import { useFileTransfer, pruneMovedFromSource } from './useFileTransfer';
import { readPinnedText } from '@/lib/fileBodies';
import { registerCellSaveBarrier } from '@/lib/cellSaveBarrier';
import { useCollimateStore } from '@/stores/useCollimateStore';

const store = useCollimateStore as unknown as {
    setState: (s: object) => void;
    getState: () => {
        fileClipboard: FileClipboard | null;
        fileDrag: FileDragState | null;
        applyCellFiles: ReturnType<typeof vi.fn>;
        openAlert: ReturnType<typeof vi.fn>;
    };
};

const vf = (name: string, extra: Partial<VirtualFile> = {}): VirtualFile =>
    (fileFixture(name, `//${name}`, {language: 'typescript', timestamp: 't0', ...extra}));

const fileNode = (path: string): TreeNode =>
    ({ name: path.split('/').pop()!, path, type: 'file', children: {} });
const folderNode = (path: string): TreeNode =>
    ({ name: path.split('/').pop()!, path, type: 'folder', children: {} });

const dragEvent = (internal = true, mods: Partial<ReactDragEvent> = {}) => ({
    dataTransfer: { types: internal ? [COLLIMATE_DND_MIME] : ['Files'], setData: vi.fn(), effectAllowed: '' },
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
    ctrlKey: false, metaKey: false, altKey: false,
    ...mods,
}) as unknown as ReactDragEvent;

const FILES = () => [vf('a.ts'), vf('src/b.ts'), vf('src/c.ts')];

/** Mount the hook over `files`. The writer runs as CellEditor runs it: each
 *  update is applied to what the cell holds at commit time (`cellNow`,
 *  default: the mounted files), and the returned `onUpdateFiles` spy sees
 *  only a list the update changed. */
const mount = (over: Partial<Parameters<typeof useFileTransfer>[0]> = {}, cellNow?: () => VirtualFile[]) => {
    const commit = vi.fn<(files: VirtualFile[]) => void>();
    const files = over.files ?? FILES();
    atomic.target=files;
    const onUpdateFiles: UpdateFiles = update => {
        const current = cellNow ? cellNow() : files;
        const next = update(current);
        if (next !== current) commit(next);
    };
    const props = {
        cellId: 'self', channel: 'chan', files, onUpdateFiles, readOnly: false, ...over,
    };
    return { onUpdateFiles: commit, ...renderHook(() => useFileTransfer(props)) };
};

beforeEach(() => {
    atomic.move.mockReset().mockResolvedValue(undefined);
    store.setState({
        fileClipboard: null, fileDrag: null,
        locateCell: () => null,
        applyCellFiles: vi.fn(async () => true),
        openAlert: vi.fn(async () => undefined),
    });
    vi.mocked(readPinnedText).mockReset().mockImplementation(async (_channel, _cellId, descriptor) => fileFetchCache.get<string>(`body:${descriptor.body_hash}`) ?? '');
});

describe('clipboard', () => {
    it('copyNode fills the clipboard and clipboardLabel tracks it', async () => {
        const { result } = mount();
        expect(result.current.clipboardLabel).toBeNull();
        act(() => result.current.copyNode(fileNode('src/b.ts')));
        const clip = store.getState().fileClipboard!;
        expect(clip.op).toBe('copy');
        expect(clip.label).toBe("'b.ts'");
        expect(clip.source).toEqual({ channel: 'chan', cellId: 'self', nodePath: 'src/b.ts' });
        expect(clip.entries).toEqual([{ relPath: 'b.ts', file: vf('src/b.ts') }]);
        await waitFor(() => expect(result.current.clipboardLabel).toBe("'b.ts'"));
    });

    it('a copy or cut writes the copied path to the OS clipboard and remembers it as osText (FP-05)', () => {
        const writeText = vi.fn(async (_text: string) => {});
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
        try {
            const { result } = mount();
            act(() => result.current.copyNode(folderNode('src')));
            expect(writeText).toHaveBeenLastCalledWith('src');
            expect(store.getState().fileClipboard!.osText).toBe('src');
            act(() => result.current.cutNode(fileNode('a.ts')));
            expect(writeText).toHaveBeenLastCalledWith('a.ts');
            expect(store.getState().fileClipboard).toMatchObject({ op: 'cut', osText: 'a.ts' });
        } finally {
            Reflect.deleteProperty(navigator, 'clipboard');
        }
    });

    it('a refused OS clipboard write still fills the internal clipboard (FP-05)', async () => {
        const writeText = vi.fn(async () => { throw new DOMException('Document is not focused.', 'NotAllowedError'); });
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
        try {
            const { result } = mount();
            act(() => result.current.copyNode(fileNode('a.ts')));
            await Promise.resolve();
            expect(store.getState().fileClipboard).toMatchObject({ op: 'copy', osText: 'a.ts' });
        } finally {
            Reflect.deleteProperty(navigator, 'clipboard');
        }
    });

    it('cutNode is refused in a read-only cell; a missing node stores nothing', () => {
        const { result } = mount({ readOnly: true });
        act(() => result.current.cutNode(fileNode('src/b.ts')));
        expect(store.getState().fileClipboard).toBeNull();
        const { result: rw } = mount();
        act(() => rw.current.copyNode(fileNode('not-there.ts')));
        expect(store.getState().fileClipboard).toBeNull();
    });

    it('Copy fetches nothing; the paste fetches a stripped body, per file, from the drafts-first route (F0884, F0863)', async () => {
        const files = [vf('d/big.txt', {  body_hash: fixtureBodyHash('') }), vf('d/small.txt', { body_hash: fixtureBodyHash('S') })];
        vi.mocked(readPinnedText).mockImplementation(async (_ch,_cell,file)=>file.path==='d/big.txt'?'REAL BYTES':'S');
        const { result, onUpdateFiles } = mount({ files });
        act(() => result.current.copyNode(folderNode('d')));
        await act(async () => {});
        expect(readPinnedText).not.toHaveBeenCalled();
        expect(store.getState().fileClipboard?.entries.find(e => e.file.name === 'd/big.txt')?.file).not.toHaveProperty('content');
        act(() => result.current.pasteInto('dst'));
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        expect(readPinnedText).toHaveBeenCalledTimes(2);
        expect(readPinnedText).toHaveBeenCalledWith('chan', 'self', expect.objectContaining({path:'d/big.txt'}), { cache: 'no-store' });
        const out = onUpdateFiles.mock.calls[0][0];
        expect(out.find(f => f.name === 'dst/d/big.txt')).toMatchObject({ content: 'REAL BYTES', file_revision: 1 });
        expect(out.find(f => f.name === 'dst/d/small.txt')).toMatchObject({ content: 'S' });
    });

    it('paste into a folder adds the copied file under it', async () => {
        const { result, onUpdateFiles } = mount();
        act(() => result.current.copyNode(fileNode('src/b.ts')));
        act(() => result.current.pasteInto('dst'));
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        const names = onUpdateFiles.mock.calls[0][0].map((f: VirtualFile) => f.name);
        expect(names).toEqual(['a.ts', 'src/b.ts', 'src/c.ts', 'dst/b.ts']);
        // A copy keeps the clipboard for repeat pastes.
        expect(store.getState().fileClipboard).not.toBeNull();
    });

    it('paste after cut moves the file and consumes the clipboard', async () => {
        const { result, onUpdateFiles } = mount();
        act(() => result.current.cutNode(fileNode('src/b.ts')));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        const names = onUpdateFiles.mock.calls[0][0].map((f: VirtualFile) => f.name);
        expect(names).toEqual(['a.ts', 'src/c.ts', 'b.ts']);
        await waitFor(() => expect(store.getState().fileClipboard).toBeNull());
    });

    it('refuses a same-cell move into the node itself, a descendant, or its parent', async () => {
        const { result, onUpdateFiles } = mount({ files: [vf('src/b.ts'), vf('src/sub/d.ts')] });
        act(() => result.current.cutNode(folderNode('src')));
        act(() => result.current.pasteInto('src/sub'));
        await Promise.resolve();
        expect(onUpdateFiles).not.toHaveBeenCalled();
        expect(store.getState().fileClipboard).not.toBeNull();
    });

    it('paste no-ops without a clipboard or in a read-only cell', async () => {
        const { result, onUpdateFiles } = mount();
        act(() => result.current.pasteInto('dst'));
        const { result: ro, onUpdateFiles: roUpdate } = mount({ readOnly: true });
        act(() => store.setState({
            fileClipboard: {
                op: 'copy', label: "'a.ts'",
                source: { channel: 'chan', cellId: 'self', nodePath: 'a.ts' },
                entries: [{ relPath: 'a.ts', file: vf('a.ts') }],
            },
        }));
        act(() => ro.current.pasteInto('dst'));
        await Promise.resolve();
        expect(onUpdateFiles).not.toHaveBeenCalled();
        expect(roUpdate).not.toHaveBeenCalled();
    });

    it('refuses the transfer when stripped content cannot be fetched', async () => {
        vi.mocked(readPinnedText).mockRejectedValue(new Error('offline'));
        const { result, onUpdateFiles } = mount();
        act(() => store.setState({
            fileClipboard: {
                op: 'cut', label: "'big.txt'",
                source: { channel: 'other-chan', cellId: 'other', nodePath: 'big.txt' },
                entries: [{ relPath: 'big.txt', file: vf('big.txt', {  }) }],
            },
        }));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(store.getState().openAlert).toHaveBeenCalledWith(
            expect.objectContaining({ title: 'Transfer failed' })));
        expect(onUpdateFiles).not.toHaveBeenCalled();
        expect(store.getState().fileClipboard).not.toBeNull();
    });
});

// A cut moves what its source holds when it is pasted: the editor stays open
// after Ctrl+X, and edits made between the cut and the paste move with the
// file. A copy keeps what it was made with.
describe('cut then paste moves the current content (F0824)', () => {
    const edited = () => [vf('a.ts'), vf('src/b.ts', { body_hash: fixtureBodyHash('EDITED') }), vf('src/c.ts')];
    const mountLive = () => {
        const commit = vi.fn<(files: VirtualFile[]) => void>();
        let cell = FILES();
        const onUpdateFiles: UpdateFiles = update => {
            const next = update(cell);
            if (next !== cell) { cell = next; commit(next); }
        };
        const hook = renderHook((files: VirtualFile[]) => useFileTransfer({
            cellId: 'self', channel: 'chan', files, onUpdateFiles, readOnly: false,
        }), { initialProps: cell });
        const edit = (files: VirtualFile[]) => { cell = files; hook.rerender(files); };
        return { ...hook, commit, edit };
    };

    it('a same-cell cut pastes the file as it is at paste time', async () => {
        const { result, commit, edit } = mountLive();
        act(() => result.current.cutNode(fileNode('src/b.ts')));
        act(() => edit(edited()));
        act(() => result.current.pasteInto('dst'));
        await waitFor(() => expect(commit).toHaveBeenCalled());
        const out = commit.mock.calls[0][0];
        expect(fixtureText(out.find(f => f.name === 'dst/b.ts'))).toBe('EDITED');
        expect(out.some(f => f.name === 'src/b.ts')).toBe(false);
        await waitFor(() => expect(store.getState().fileClipboard).toBeNull());
    });

    it('a cut folder moves the files it holds at paste time', async () => {
        const { result, commit, edit } = mountLive();
        act(() => result.current.cutNode(folderNode('src')));
        act(() => edit([...edited(), vf('src/d.ts')]));
        act(() => result.current.pasteInto('dst'));
        await waitFor(() => expect(commit).toHaveBeenCalled());
        const names = commit.mock.calls[0][0].map(f => f.name);
        expect(names).toEqual(['a.ts', 'dst/src/b.ts', 'dst/src/c.ts', 'dst/src/d.ts']);
        expect(fixtureText(commit.mock.calls[0][0].find(f => f.name === 'dst/src/b.ts'))).toBe('EDITED');
    });

    it('a cross-cell cut pastes what the source cell holds now, then prunes it', async () => {
        const source = [vf('x.ts', { body_hash: fixtureBodyHash('NOW') }), vf('keep.ts')];
        const { result, onUpdateFiles } = mount();
        act(() => store.setState({
            fileClipboard: {
                op: 'cut', label: "'x.ts'",
                source: { channel: 'source-channel', cellId: 'other', nodePath: 'x.ts' },
                entries: [{ relPath: 'x.ts', file: vf('x.ts', { body_hash: fixtureBodyHash('AT CUT') }) }],
            },
            locateCell: () => ({ cell: { id: 'other', files: source } }),
        }));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(store.getState().applyCellFiles).toHaveBeenCalled());
        expect(fixtureText(onUpdateFiles.mock.calls[0][0].find(f => f.name === 'x.ts'))).toBe('NOW');
        expect(store.getState().applyCellFiles.mock.calls[0][1].map((f: VirtualFile) => f.name)).toEqual(['keep.ts']);
    });

    it('a cut whose node is gone refuses the paste and drops the clipboard', async () => {
        const { result, commit, edit } = mountLive();
        act(() => result.current.cutNode(fileNode('src/b.ts')));
        act(() => edit([vf('a.ts'), vf('src/c.ts')]));
        act(() => result.current.pasteInto('dst'));
        await act(async () => {});
        expect(commit).not.toHaveBeenCalled();
        expect(store.getState().openAlert).toHaveBeenCalledWith(expect.objectContaining({ title: 'Paste failed' }));
        expect(store.getState().fileClipboard).toBeNull();
    });

    it('a cut folder replaced by a same-named file refuses the paste: that file was never cut', async () => {
        const { result, commit, edit } = mountLive();
        act(() => result.current.cutNode(folderNode('src')));
        act(() => edit([vf('a.ts'), vf('src')]));
        act(() => result.current.pasteInto('dst'));
        await act(async () => {});
        expect(commit).not.toHaveBeenCalled();
        expect(store.getState().openAlert).toHaveBeenCalledWith(expect.objectContaining({ title: 'Paste failed' }));
        expect(store.getState().fileClipboard).toBeNull();
    });

    it('a copy still pastes what it was made with', async () => {
        const { result, commit, edit } = mountLive();
        act(() => result.current.copyNode(fileNode('src/b.ts')));
        act(() => edit(edited()));
        act(() => result.current.pasteInto('dst'));
        await waitFor(() => expect(commit).toHaveBeenCalled());
        const out = commit.mock.calls[0][0];
        expect(fixtureText(out.find(f => f.name === 'dst/b.ts'))).toBe('//src/b.ts');
        expect(fixtureText(out.find(f => f.name === 'src/b.ts'))).toBe('EDITED');
    });
});

// The body fetch before a paste awaits; the paste lands on what the cell holds
// when it commits, and a move takes away exactly what it moved.
describe('a paste that waited on a body lands on the latest files (F0823, F0891)', () => {
    const RUN_WRITE = vf('out/run.log', { body_hash: fixtureBodyHash('run') });

    it('a cross-cell copy keeps a file the run wrote during the fetch', async () => {
        let cell = FILES();
        vi.mocked(readPinnedText).mockImplementation(async () => { cell = [...FILES(), RUN_WRITE]; return 'BIG'; });
        const { result, onUpdateFiles } = mount({}, () => cell);
        act(() => store.setState({
            fileClipboard: {
                op: 'copy', label: "'big.txt'",
                source: { channel: 'source-channel', cellId: 'other', nodePath: 'big.txt' },
                entries: [{ relPath: 'big.txt', file: vf('big.txt', {  body_hash: fixtureBodyHash('') }) }],
            },
        }));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        const out = onUpdateFiles.mock.calls[0][0];
        expect(out.find(f => f.name === 'out/run.log')).toEqual(RUN_WRITE);
        expect(out.find(f => f.name === 'big.txt')).toMatchObject({ content: 'BIG', file_revision: 1 });
    });

    it('a same-cell move takes away only what moved; a file written under the node meanwhile stays', async () => {
        const start = [vf('src/big.txt', {  body_hash: fixtureBodyHash('') }), vf('src/b.ts')];
        let cell = start;
        vi.mocked(readPinnedText).mockImplementation(async () => { cell = [...start, vf('src/new.ts')]; return 'BIG'; });
        const { result, onUpdateFiles } = mount({ files: start }, () => cell);
        act(() => result.current.cutNode(folderNode('src')));
        act(() => result.current.pasteInto('dst'));
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        expect(onUpdateFiles.mock.calls[0][0].map(f => f.name)).toEqual(['src/new.ts', 'dst/src/big.txt', 'dst/src/b.ts']);
    });

    it('refuses a same-cell move when accepted metadata changes during pinned reads',async()=>{
 const start=[vf('src/a.ts'),vf('src/b.ts')];let cell=start;
 vi.mocked(readPinnedText).mockImplementation(async()=>{cell=[start[0],fileFixture('src/b.ts','new',{file_revision:2})];return 'old';});
 const {result,onUpdateFiles}=mount({files:start},()=>cell);
 act(()=>result.current.cutNode(folderNode('src')));act(()=>result.current.pasteInto('dst'));
 await waitFor(()=>expect(store.getState().openAlert).toHaveBeenCalledWith(expect.objectContaining({title:'Transfer failed'})));
 expect(onUpdateFiles).not.toHaveBeenCalled();expect(cell[1].file_revision).toBe(2);
});

    it('refuses a cross-channel move when accepted source metadata changes during pinned reads',async()=>{
 const before=[vf('d/a.ts'),vf('d/b.ts')];let source=before;
 vi.mocked(readPinnedText).mockImplementation(async()=>{source=[before[0],fileFixture('d/b.ts','new',{file_revision:2})];return 'old';});
 const {result,onUpdateFiles}=mount();
 act(()=>store.setState({fileClipboard:{op:'cut',label:'d',source:{channel:'source-channel',cellId:'other',nodePath:'d'},entries:before.map(file=>({relPath:file.name,file}))},locateCell:()=>({cell:{id:'other',files:source}})}));
 act(()=>result.current.pasteInto(''));
 await waitFor(()=>expect(store.getState().openAlert).toHaveBeenCalledWith(expect.objectContaining({title:'Transfer failed'})));
 expect(onUpdateFiles).not.toHaveBeenCalled();expect(store.getState().applyCellFiles).not.toHaveBeenCalled();
});

    it('a large file rewritten during the fetch refuses the move: nothing lands, nothing is pruned (F0823, F0824)', async () => {
        const start = [vf('src/big.txt', {  body_hash: fixtureBodyHash('') }), vf('src/b.ts')];
        let cell = start;
        vi.mocked(readPinnedText).mockImplementation(async () => {
            cell = [vf('src/big.txt', {  body_hash: fixtureBodyHash(''), file_revision: 2 }), vf('src/b.ts')];
            return 'OLD BIG';
        });
        const { result, onUpdateFiles } = mount({ files: start }, () => cell);
        act(() => result.current.cutNode(folderNode('src')));
        act(() => result.current.pasteInto('dst'));
        await waitFor(() => expect(store.getState().openAlert).toHaveBeenCalledWith(
            expect.objectContaining({ title: 'Transfer failed' })));
        expect(onUpdateFiles).not.toHaveBeenCalled();
        expect(store.getState().applyCellFiles).not.toHaveBeenCalled();
        expect(store.getState().fileClipboard).not.toBeNull();
    });

    it('a user folder named `_runs` moved to the root lands renamed, never dropped', async () => {
        const { result, onUpdateFiles } = mount({ files: [vf('foo/_runs/a.txt'), vf('foo/b.txt')] });
        act(() => result.current.cutNode(folderNode('foo/_runs')));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        expect(onUpdateFiles.mock.calls[0][0].map(f => f.name)).toEqual(['foo/b.txt', '_runs (copy)/a.txt']);
    });
});

// The editor keeps a typed change in a 1.5 s autosave debounce. A move
// flushes the source cell's editors just before it commits (after any body
// fetch), so those keystrokes move with the file rather than saving later to
// a path that is gone. A move out of another cell then writes the drafts that
// flush armed before it prunes the source, so none lands after the prune, and
// the prune flushes the editors once more: text typed while those drafts are
// written stays in the source.
describe('a move takes the source editors\' pending saves with it (F0824)', () => {
    it('a same-cell cut pastes the text still waiting in the editor', async () => {
        let cell = FILES();
        const off = registerCellSaveBarrier('self', () => {
            cell = cell.map(f => (f.name === 'src/b.ts' ? { ...f, content: 'TYPED' } : f));
        });
        try {
            const { result, onUpdateFiles } = mount({}, () => cell);
            act(() => result.current.cutNode(fileNode('src/b.ts')));
            act(() => result.current.pasteInto('dst'));
            await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
            const out = onUpdateFiles.mock.calls[0][0];
            expect(fixtureText(out.find(f => f.name === 'dst/b.ts'))).toBe('TYPED');
            expect(out.some(f => f.name === 'src/b.ts')).toBe(false);
        } finally {
            off();
        }
    });

    it('a cross-cell cut pastes the text still waiting in the source cell\'s editor', async () => {
        let source = [vf('x.ts', { body_hash: fixtureBodyHash('SAVED') }), vf('keep.ts')];
        // The editor's pending autosave: the first flush saves it.
        let typed: string | null = 'TYPED';
        const off = registerCellSaveBarrier('other', () => {
            if (typed === null) return;
            const content = typed;
            typed = null;
            source = source.map(f => (f.name === 'x.ts' ? { ...f, content } : f));
        });
        try {
            const { result, onUpdateFiles } = mount();
            act(() => store.setState({
                fileClipboard: {
                    op: 'cut', label: "'x.ts'",
                    source: { channel: 'source-channel', cellId: 'other', nodePath: 'x.ts' },
                    entries: [{ relPath: 'x.ts', file: vf('x.ts', { body_hash: fixtureBodyHash('SAVED') }) }],
                },
                locateCell: () => ({ cell: { id: 'other', files: source } }),
            }));
            act(() => result.current.pasteInto(''));
            await waitFor(() => expect(store.getState().applyCellFiles).toHaveBeenCalled());
            expect(fixtureText(onUpdateFiles.mock.calls[0][0].find(f => f.name === 'x.ts'))).toBe('TYPED');
            expect(store.getState().applyCellFiles.mock.calls[0][1].map((f: VirtualFile) => f.name)).toEqual(['keep.ts']);
        } finally {
            off();
        }
    });

    it('text typed while a large body is fetched moves with the file (F0824)', async () => {
        const start = [vf('src/big.txt', {  body_hash: fixtureBodyHash('') }), vf('src/b.ts', { body_hash: fixtureBodyHash('SAVED') })];
        let cell = start;
        // What the editor holds in its autosave debounce; flushing it saves
        // it into the cell.
        let typed: string | null = null;
        const off = registerCellSaveBarrier('self', () => {
            if (typed === null) return;
            const content = typed;
            typed = null;
            cell = cell.map(f => (f.name === 'src/b.ts' ? { ...f, content } : f));
        });
        vi.mocked(readPinnedText).mockImplementation(async () => { typed = 'TYPED DURING FETCH'; return 'BIG'; });
        try {
            const { result, onUpdateFiles } = mount({ files: start }, () => cell);
            act(() => result.current.cutNode(folderNode('src')));
            act(() => result.current.pasteInto('dst'));
            await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
            expect(onUpdateFiles.mock.calls[0][0].map(f => [f.name, fixtureText(f)])).toEqual([
                ['dst/src/big.txt', 'BIG'], ['dst/src/b.ts', 'TYPED DURING FETCH'],
            ]);
        } finally {
            off();
        }
    });
});

describe('cross-cell transfers', () => {
    const crossClip = (op: 'copy' | 'cut'): FileClipboard => ({
        op, label: "'x.ts'",
        source: { channel: 'source-channel', cellId: 'other', nodePath: 'x.ts' },
        entries: [{ relPath: 'x.ts', file: vf('x.ts') }], osText: 'x.ts',
    });

    it('a cross-cell copy only adds to self', async () => {
        const { result, onUpdateFiles } = mount();
        act(() => store.setState({ fileClipboard: crossClip('copy') }));
        act(() => result.current.pasteInto('dst'));
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        const names = onUpdateFiles.mock.calls[0][0].map((f: VirtualFile) => f.name);
        expect(names).toContain('dst/x.ts');
        expect(store.getState().applyCellFiles).not.toHaveBeenCalled();
    });

    it('a cross-cell move prunes the source cell after adding here', async () => {
        const source = [vf('x.ts'), vf('keep.ts')];
        const { result, onUpdateFiles } = mount();
        act(() => store.setState({
            fileClipboard: crossClip('cut'),
            locateCell: () => ({ cell: { id: 'other', files: source } }),
        }));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(store.getState().applyCellFiles).toHaveBeenCalled());
        expect(onUpdateFiles).toHaveBeenCalled();
        const [cellId, pruned] = store.getState().applyCellFiles.mock.calls[0];
        expect(cellId).toBe('other');
        expect(pruned.map((f: VirtualFile) => f.name)).toEqual(['keep.ts']);
        await waitFor(() => expect(store.getState().fileClipboard).toBeNull());
    });

    it('a cross-cell move of a binary prunes it from the source, which still holds the same blob (F0823, F0824)', async () => {
        const img = vf('img.png', { language: 'binary', body_hash: 'h1', encoding: 'binary' });
        const keep = vf('keep.ts');
        const { result, onUpdateFiles } = mount();
        act(() => store.setState({
            fileClipboard: {
                op: 'cut', label: "'img.png'",
                source: { channel: 'source-channel', cellId: 'other', nodePath: 'img.png' },
                entries: [{ relPath: 'img.png', file: img }],
            },
            locateCell: () => ({ cell: { id: 'other', files: [img, keep] } }),
        }));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(store.getState().applyCellFiles).toHaveBeenCalled());
        expect(onUpdateFiles.mock.calls[0][0].find(f => f.name === 'img.png')).toMatchObject({ body_hash: 'h1' });
        const [cellId, pruned] = store.getState().applyCellFiles.mock.calls[0];
        expect(cellId).toBe('other');
        expect(pruned.map((f: VirtualFile) => f.name)).toEqual(['keep.ts']);
        expect(store.getState().openAlert).not.toHaveBeenCalled();
        await waitFor(() => expect(store.getState().fileClipboard).toBeNull());
    });

    it('a run\'s files never cross into another cell; a move prunes only what moved', async () => {
        const runFile = vf('_runs/r-11111111/chat/conversation.chat');
        const source = [vf('x.ts'), runFile];
        const { result, onUpdateFiles } = mount();
        act(() => store.setState({
            fileClipboard: {
                op: 'cut', label: "'root'",
                source: { channel: 'source-channel', cellId: 'other', nodePath: 'x.ts' },
                entries: [{ relPath: 'x.ts', file: vf('x.ts') }, { relPath: runFile.name, file: runFile }],
            },
            locateCell: () => ({ cell: { id: 'other', files: source } }),
        }));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(store.getState().applyCellFiles).toHaveBeenCalled());
        const names = onUpdateFiles.mock.calls[0][0].map((f: VirtualFile) => f.name);
        expect(names).toContain('x.ts');
        expect(names.some((n: string) => n.includes('_runs'))).toBe(false);
        const [, pruned] = store.getState().applyCellFiles.mock.calls[0];
        expect(pruned.map((f: VirtualFile) => f.name)).toEqual([runFile.name]);
    });

    it('a payload of nothing but a run\'s files lands nothing in another cell', async () => {
        const runFile = vf('_runs/r-11111111/x.chat');
        const { result, onUpdateFiles } = mount();
        act(() => store.setState({
            fileClipboard: {
                op: 'copy', label: "'x.chat'",
                source: { channel: 'source-channel', cellId: 'other', nodePath: runFile.name },
                entries: [{ relPath: 'x.chat', file: runFile }],
            },
        }));
        act(() => result.current.pasteInto(''));
        await act(async () => {});
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });

    it('a cross-cell move hands the source its untouched large files back as husks, never fetched (F0822)', async () => {
        const husk = vf('big.md', {  body_hash: fixtureBodyHash('') });
        const source = [vf('x.ts'), husk];
        const { result, onUpdateFiles } = mount();
        act(() => store.setState({
            fileClipboard: crossClip('cut'),
            locateCell: () => ({ cell: { id: 'other', files: source } }),
        }));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(store.getState().applyCellFiles).toHaveBeenCalled());
        expect(onUpdateFiles).toHaveBeenCalled();
        expect(vi.mocked(readPinnedText).mock.calls.map(call=>call[2].path)).toEqual(['x.ts']);
        const [, pruned] = store.getState().applyCellFiles.mock.calls[0];
        expect(pruned).toEqual([husk]);
    });

    it('a move whose source cell is already gone still lands the files', async () => {
        const { result, onUpdateFiles } = mount();
        act(() => store.setState({ fileClipboard: crossClip('cut') }));
        act(() => result.current.pasteInto(''));
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        expect(store.getState().applyCellFiles).not.toHaveBeenCalled();
        await waitFor(() => expect(store.getState().fileClipboard).toBeNull());
    });
});

describe('drag and drop', () => {
    it('startDrag stores the payload and puts {cellId, paths} on the cell-file type (F0873, RS-03)', () => {
        const { result } = mount();
        const e = dragEvent();
        act(() => result.current.startDrag(fileNode('src/b.ts'), e));
        const drag = store.getState().fileDrag!;
        expect(drag.source.nodePath).toBe('src/b.ts');
        expect(drag.sourceReadOnly).toBe(false);
        expect(e.dataTransfer.setData).toHaveBeenCalledExactlyOnceWith(
            COLLIMATE_DND_MIME, JSON.stringify({ cellId: 'self', paths: ['src/b.ts'] }));
        expect(COLLIMATE_DND_MIME).toBe('application/x-collimate-files');
        expect(e.dataTransfer.effectAllowed).toBe('copyMove');
    });

    it('a folder drag carries every file under the folder as its paths (RS-03)', () => {
        const { result } = mount();
        const e = dragEvent();
        act(() => result.current.startDrag(folderNode('src'), e));
        const [type, value] = vi.mocked(e.dataTransfer.setData).mock.calls[0];
        expect(type).toBe(COLLIMATE_DND_MIME);
        const payload = JSON.parse(value as string) as { cellId: string; paths: string[] };
        expect(payload.cellId).toBe('self');
        expect(payload.paths).toEqual(['src/b.ts', 'src/c.ts']);
        expect(payload.paths).toEqual(store.getState().fileDrag!.entries.map(en => en.file.name));
    });

    it('startDrag with nothing to carry stores no payload; endDrag clears one', () => {
        const { result } = mount();
        act(() => result.current.startDrag(fileNode('missing.ts'), dragEvent()));
        expect(store.getState().fileDrag).toBeNull();
        act(() => result.current.startDrag(fileNode('a.ts'), dragEvent()));
        expect(store.getState().fileDrag).not.toBeNull();
        act(() => result.current.endDrag());
        expect(store.getState().fileDrag).toBeNull();
        act(() => result.current.endDrag()); // idempotent with no drag
        expect(store.getState().fileDrag).toBeNull();
    });

    it('dropEffectFor: move within a cell, copy across cells, modifiers flip, read-only pins copy', () => {
        const { result } = mount();
        expect(result.current.dropEffectFor(dragEvent())).toBe('copy'); // no drag
        const drag = (cellId: string, sourceReadOnly = false) => store.setState({
            fileDrag: {
                source: { channel: 'chan', cellId, nodePath: 'a.ts' },
                entries: [{ relPath: 'a.ts', file: vf('a.ts') }], sourceReadOnly,
            },
        });
        drag('self');
        expect(result.current.dropEffectFor(dragEvent())).toBe('move');
        expect(result.current.dropEffectFor(dragEvent(true, { ctrlKey: true }))).toBe('copy');
        drag('other');
        expect(result.current.dropEffectFor(dragEvent())).toBe('copy');
        expect(result.current.dropEffectFor(dragEvent(true, { altKey: true }))).toBe('move');
        drag('other', true);
        expect(result.current.dropEffectFor(dragEvent(true, { altKey: true }))).toBe('copy');
    });

    it('dropInto ignores OS-file drags and consumes internal ones', async () => {
        const { result, onUpdateFiles } = mount();
        expect(result.current.isInternalDrag(dragEvent(false))).toBe(false);
        expect(result.current.dropInto('dst', dragEvent(false))).toBe(false);
        act(() => store.setState({
            fileDrag: {
                source: { channel: 'source-channel', cellId: 'other', nodePath: 'x.ts' },
                entries: [{ relPath: 'x.ts', file: vf('x.ts') }], sourceReadOnly: false,
            },
        }));
        let handled = false;
        act(() => { handled = result.current.dropInto('dst', dragEvent()); });
        expect(handled).toBe(true);
        expect(store.getState().fileDrag).toBeNull();
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        expect(onUpdateFiles.mock.calls[0][0].map((f: VirtualFile) => f.name)).toContain('dst/x.ts');
    });

    it('dropInto on a read-only cell consumes the drag without applying it', async () => {
        const { result, onUpdateFiles } = mount({ readOnly: true });
        act(() => store.setState({
            fileDrag: {
                source: { channel: 'source-channel', cellId: 'other', nodePath: 'x.ts' },
                entries: [{ relPath: 'x.ts', file: vf('x.ts') }], sourceReadOnly: false,
            },
        }));
        expect(result.current.dropInto('dst', dragEvent())).toBe(true);
        expect(store.getState().fileDrag).toBeNull();
        await Promise.resolve();
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });
});

describe('run folders and locked files', () => {
    const LOCKED = '_runs/r-11111111/chat/conversation.chat';
    const run = {
        runId: 'run-1', cellId: 'self', channel: 'chan', runnerId: 'r', runnerName: 'R',
        generatorName: 'g', label: 'r-11111111', folder: '_runs/r-11111111',
    };
    const lockOwnerOf = (path: string) => (path.startsWith(`${run.folder}/`) ? run : null);
    const files = () => [vf('a.ts'), vf(LOCKED)];

    it('a locked file can be copied but never cut', () => {
        const { result } = mount({ files: files(), lockOwnerOf });
        act(() => result.current.cutNode(fileNode(LOCKED)));
        expect(store.getState().fileClipboard).toBeNull();
        act(() => result.current.cutNode(folderNode('_runs')));
        expect(store.getState().fileClipboard).toBeNull();
        act(() => result.current.copyNode(fileNode(LOCKED)));
        expect(store.getState().fileClipboard?.op).toBe('copy');
    });

    it('a locked node drags as a copy only', () => {
        const { result } = mount({ files: files(), lockOwnerOf });
        act(() => result.current.startDrag(fileNode(LOCKED), dragEvent()));
        expect(store.getState().fileDrag?.sourceReadOnly).toBe(true);
        expect(result.current.dropEffectFor(dragEvent())).toBe('copy');
    });

    it('an in-cell copy of a locked file lands outside `_runs/` and the original stays', async () => {
        const { result, onUpdateFiles } = mount({ files: files(), lockOwnerOf });
        act(() => result.current.copyNode(fileNode(LOCKED)));
        act(() => result.current.pasteInto('dst'));
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        const names = onUpdateFiles.mock.calls[0][0].map((f: VirtualFile) => f.name);
        expect(names).toContain('dst/conversation.chat');
        expect(names).toContain(LOCKED);
    });

    it('nothing is pasted or dropped under `_runs/`', async () => {
        const { result, onUpdateFiles } = mount({ files: files(), lockOwnerOf });
        act(() => result.current.copyNode(fileNode('a.ts')));
        act(() => result.current.pasteInto('_runs/r-11111111'));
        await waitFor(() => expect(store.getState().openAlert).toHaveBeenCalledWith(
            expect.objectContaining({ title: 'Paste refused' })));
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });

    it('a same-cell move of a locked node (or a folder holding one) is refused', async () => {
        const { result, onUpdateFiles } = mount({ files: files(), lockOwnerOf });
        act(() => store.setState({
            fileDrag: {
                source: { channel: 'chan', cellId: 'self', nodePath: '_runs' },
                entries: [{ relPath: LOCKED, file: vf(LOCKED) }],
            },
        }));
        act(() => { result.current.dropInto('dst', dragEvent(true, { altKey: false } as never)); });
        await act(async () => {});
        // The same-cell default would be a move; the locked source forces a
        // copy (sourceReadOnly is unset here, so the backstop refuses).
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });
});

it.each([false,true])('a same-channel cut uses an atomic revision-guarded move (refused=%s)',async refused=>{
 const source=[vf('notes.md')];const {result,onUpdateFiles}=mount({files:[]});
 if(refused)atomic.move.mockRejectedValueOnce(new Error('source revision conflict'));
 act(()=>store.setState({fileClipboard:{op:'cut',label:'notes',source:{channel:'chan',cellId:'other',nodePath:'notes.md'},entries:[{relPath:'notes.md',file:source[0]}]},locateCell:()=>({cell:{id:'other',files:source}})}));
 act(()=>result.current.pasteInto(''));
 await waitFor(()=>expect(atomic.move).toHaveBeenCalledWith('chan','other','self',[{source_path:'notes.md',target_path:'notes.md',source_revision:1,target_revision:0}]));
 await act(async()=>{});
 expect(onUpdateFiles).not.toHaveBeenCalled();expect(store.getState().applyCellFiles).not.toHaveBeenCalled();
 if(refused){expect(store.getState().fileClipboard).not.toBeNull();expect(store.getState().openAlert).toHaveBeenCalledWith(expect.objectContaining({title:'Move failed'}));}
 else expect(store.getState().fileClipboard).toBeNull();
});
it('source pruning accepts unchanged revision/hash when a store publication replaces metadata objects',async()=>{
 const moved=vf('clone.ts');let files=[moved,vf('keep.ts')];
 const off=registerCellSaveBarrier('clone-cell',()=>{files=files.map(file=>({...file}));});
 store.setState({locateCell:()=>({cell:{id:'clone-cell',files}})});
 try{expect(await pruneMovedFromSource({channel:'source-channel',cellId:'clone-cell',nodePath:'clone.ts'},new Map([['clone.ts',moved]]))).toBe(true);
  expect(store.getState().applyCellFiles).toHaveBeenCalledWith('clone-cell',[expect.objectContaining({name:'keep.ts'})]);
  expect(store.getState().openAlert).not.toHaveBeenCalled();
 }finally{off();}
});
