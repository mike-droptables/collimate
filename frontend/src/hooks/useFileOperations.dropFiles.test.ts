import { fixtureBodyHash, fixtureText } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

// The drop path reaches the CAS (every file streams up) and the store
// (alerts) — stub both so the hook runs in jsdom with no backend. The fake
// upload files each body under its hash and answers the server's sniff
// (binary for an image or a NUL byte). Everything else (the entries walk,
// the ingest, the marker minting) is the real code.
vi.mock('@/lib/api/transfer', () => ({
    streamBlob: vi.fn(async (body: Blob) => {
        const text = await body.text();
        const binary = body.type.startsWith('image/') || text.includes('\0');
        return { blob_hash: binary ? `bin:${body.size}` : fixtureBodyHash(text), size: body.size, encoding: binary ? 'binary' : 'utf-8' };
    }),
}));
// Hoisted so the alert spy is the SAME function the module under test reaches
// (a vi.mock factory runs before the imports below).
const { openAlert } = vi.hoisted(() => ({
    openAlert: vi.fn(async (_opts: { title: string; body: string }) => {}),
}));
vi.mock('@/stores/useCollimateStore', () => ({
    useCollimateStore: { getState: () => ({ openAlert, openConfirm: vi.fn(async () => true) }) },
}));

import { useFileOperations } from './useFileOperations';
import { captureDrop, type DropEntry } from '@/lib/dropEntries';
import { streamBlob } from '@/lib/api/transfer';
import type { UpdateFiles } from '@/lib/fileTransfer';
import type { VirtualFile } from '@/lib/types';

const osFile = (name: string, body = `body of ${name}`, type = 'text/plain') => new File([body], name, { type });

const fileEntry = (name: string): DropEntry => ({
    name, isFile: true, isDirectory: false, file: (done) => done(osFile(name)),
});

/** A file the entries walk hands over fine but whose BYTES the OS refuses —
 *  deleted/renamed since the drag snapshot, permission denied, a cloud
 *  placeholder that won't hydrate. Chrome rejects a read of any slice with
 *  NotReadableError / NotFoundError exactly like this. */
const unreadableFileEntry = (name: string): DropEntry => ({
    name, isFile: true, isDirectory: false,
    file: (done) => {
        const f = new File([], name);
        Object.assign(f, { slice: () => ({ arrayBuffer: async () => { throw new Error('NotReadableError'); } }) });
        done(f);
    },
});

const dirEntry = (name: string, children: DropEntry[]): DropEntry => ({
    name, isFile: false, isDirectory: true,
    createReader: () => {
        let done = false;
        return { readEntries: (cb) => { cb(done ? [] : children); done = true; } };
    },
});

/** A DataTransfer stand-in exposing the dropped entries the way a browser does
 *  (plus the size-0 junk File it also puts in .files for a folder). */
const transfer = (entries: DropEntry[], files: File[] = []) => ({
    files,
    items: entries.map(entry => ({ kind: 'file', webkitGetAsEntry: () => entry })),
}) as unknown as DataTransfer;

const vfile = (name: string, content = 'old'): VirtualFile =>
    (fileFixture(name, content, {language: 'plaintext', timestamp: 't'}));

/** Drop `dt` into a cell holding `existing`. The writer runs as CellEditor
 *  runs it: each update applied to the cell's list; the returned spy sees
 *  only a list the update changed. */
const drop = async (existing: VirtualFile[], dt: DataTransfer, target: string) => {
    const commit = vi.fn<(files: VirtualFile[]) => void>();
    const onUpdateFiles: UpdateFiles = update => { const next = update(existing); if (next !== existing) commit(next); };
    const { result } = renderHook(() =>
        useFileOperations({ files: existing, onUpdateFiles, readOnly: false }));
    await act(async () => { await result.current.handleDropFiles(captureDrop(dt), target); });
    return commit;
};

beforeEach(() => vi.clearAllMocks());

describe('useFileOperations — dropping OS files and folders into a cell', () => {
    it('lands a dropped folder as its whole tree under the drop target', async () => {
        const tree = dirEntry('pkg', [
            fileEntry('index.ts'),
            dirEntry('lib', [fileEntry('util.ts')]),
        ]);
        const onUpdateFiles = await drop([vfile('keep.md')], transfer([tree], [new File([], 'pkg')]), 'src');
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        expect((onUpdateFiles.mock.calls[0][0] as VirtualFile[]).map(f => f.name)).toEqual([
            'keep.md', 'src/pkg/index.ts', 'src/pkg/lib/util.ts',
        ]);
    });

    it('keeps a bare file drop flat at the cell root', async () => {
        const onUpdateFiles = await drop([], transfer([fileEntry('a.txt')]), '');
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        const written = onUpdateFiles.mock.calls[0][0] as VirtualFile[];
        expect(written.map(f => f.name)).toEqual(['a.txt']);
        expect(fixtureText(written[0])).toBe('body of a.txt');
    });

    it('mints a .collkeep for an empty dropped folder, and none for one that got files', async () => {
        const tree = dirEntry('proj', [dirEntry('cache', []), fileEntry('README.md')]);
        const onUpdateFiles = await drop([], transfer([tree]), '');
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        expect((onUpdateFiles.mock.calls[0][0] as VirtualFile[]).map(f => f.name)).toEqual([
            'proj/README.md', 'proj/cache/.collkeep',
        ]);
    });

    it('does not re-mint a marker for a folder that already holds a real file', async () => {
        // The dropped `docs` is empty, but the cell already has content there —
        // a marker would be redundant clutter the pruner would strip anyway, so
        // the drop changes nothing and never touches the file set.
        const onUpdateFiles = await drop([vfile('docs/old.md')], transfer([dirEntry('docs', [])]), '');
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });

    it('a same-path file replaces the existing one in place', async () => {
        const onUpdateFiles = await drop([vfile('a.txt', 'old'), vfile('z.md')],
            transfer([fileEntry('a.txt')]), '');
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        const written = onUpdateFiles.mock.calls[0][0] as VirtualFile[];
        expect(written.map(f => f.name)).toEqual(['a.txt', 'z.md']);
        expect(fixtureText(written[0])).toBe('body of a.txt');
    });

    it('a drop that yields nothing usable leaves the cell untouched', async () => {
        // A folder drop where the folder no longer exists: entries present, no
        // files, no empty dirs (the read failed).
        const unreadable: DropEntry = {
            name: 'gone', isDirectory: true,
            createReader: () => ({ readEntries: (_ok, err) => err?.(new Error('gone')) }),
        };
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        const onUpdateFiles = await drop([vfile('keep.md')], transfer([unreadable]), '');
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });

    it('one unreadable file costs only itself — the rest of the tree still lands', async () => {
        // The whole point of importing folders: a 500-file drop must not be
        // lost because one member is a cloud placeholder or was deleted
        // mid-walk. The walk already skips what it can't open; the byte read
        // has to hold the same line.
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        const tree = dirEntry('pkg', [
            fileEntry('good1.ts'), unreadableFileEntry('bad.ts'), fileEntry('good2.ts'),
        ]);
        const onUpdateFiles = await drop([], transfer([tree]), '');
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        expect((onUpdateFiles.mock.calls[0][0] as VirtualFile[]).map(f => f.name))
            .toEqual(['pkg/good1.ts', 'pkg/good2.ts']);
        // ...and the user is told once, not silently short-changed.
        expect(openAlert).toHaveBeenCalledTimes(1);
        expect(openAlert).toHaveBeenCalledWith(
            expect.objectContaining({ title: 'One file was skipped' }));
    });

    it('never rejects, so a fire-and-forget drop can not raise an unhandled rejection', async () => {
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        const onUpdateFiles = vi.fn();
        const { result } = renderHook(() =>
            useFileOperations({ files: [], onUpdateFiles, readOnly: false }));
        let rejected = false;
        await act(async () => {
            await result.current
                .handleDropFiles(captureDrop(transfer([unreadableFileEntry('bad.ts')])), '')
                .catch(() => { rejected = true; });
        });
        expect(rejected).toBe(false);
        expect(onUpdateFiles).not.toHaveBeenCalled();   // nothing usable landed
    });

    it('a binary the CAS refuses is skipped, not fatal to its siblings', async () => {
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        vi.mocked(streamBlob).mockRejectedValueOnce(Object.assign(new Error('too big'), { response: { status: 413 } }));
        const png: DropEntry = {
            name: 'logo.png', isFile: true, isDirectory: false,
            file: (done) => done(new File([new Uint8Array([1, 2, 3])], 'logo.png', { type: 'image/png' })),
        };
        const onUpdateFiles = await drop([], transfer([dirEntry('art', [png, fileEntry('notes.md')])]), '');
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        expect((onUpdateFiles.mock.calls[0][0] as VirtualFile[]).map(f => f.name)).toEqual(['art/notes.md']);
    });

    it('every file streams up whole and lands by hash, text and binary alike, never read into memory (TR-07)', async () => {
        const readAll = vi.fn(async () => new ArrayBuffer(0));
        const big = new File([new Uint8Array([0, 1, 2])], 'big.mp4', { type: 'video/mp4' });
        Object.defineProperty(big, 'size', { value: 60 * 1024 * 1024 });
        Object.assign(big, { arrayBuffer: readAll, text: readAll });
        const bigEntry: DropEntry = { name: 'big.mp4', isFile: true, isDirectory: false, file: done => done(big) };
        vi.mocked(streamBlob).mockImplementationOnce(async body => ({ blob_hash: 'bin:big', size: body.size, encoding: 'binary' }));
        const onUpdateFiles = await drop([], transfer([bigEntry, fileEntry('small.txt')]), '');
        await waitFor(() => expect(onUpdateFiles).toHaveBeenCalled());
        const written = onUpdateFiles.mock.calls[0][0] as VirtualFile[];
        expect(written.map(f => [f.name, f.encoding, f.body_hash])).toEqual([
            ['big.mp4', 'binary', 'bin:big'], ['small.txt', 'utf-8', fixtureBodyHash('body of small.txt')],
        ]);
        expect('content' in written[1]).toBe(false);
        expect(vi.mocked(streamBlob).mock.calls[0][0]).toBe(big);
        expect(readAll).not.toHaveBeenCalled();
        expect(openAlert).not.toHaveBeenCalled();
    });

    it('merges into the cell as it is after the walk, keeping a file a run wrote meanwhile (F0823, F0891)', async () => {
        const RUN_WRITE = vfile('out/run.log', 'run');
        let cell = [vfile('keep.md')];
        const commit = vi.fn<(files: VirtualFile[]) => void>();
        const onUpdateFiles: UpdateFiles = update => commit(update(cell));
        // The walk hands the file over only after the run's write landed.
        const slow: DropEntry = {
            name: 'a.txt', isFile: true, isDirectory: false,
            file: (done) => { cell = [...cell, RUN_WRITE]; done(osFile('a.txt')); },
        };
        const { result } = renderHook(() =>
            useFileOperations({ files: [vfile('keep.md')], onUpdateFiles, readOnly: false }));
        await act(async () => { await result.current.handleDropFiles(captureDrop(transfer([slow])), ''); });
        expect(commit.mock.calls[0][0].map(f => f.name)).toEqual(['keep.md', 'out/run.log', 'a.txt']);
    });

    it('a read-only cell ignores the drop entirely', async () => {
        const onUpdateFiles = vi.fn();
        const { result } = renderHook(() =>
            useFileOperations({ files: [], onUpdateFiles, readOnly: true }));
        await act(async () => {
            await result.current.handleDropFiles(captureDrop(transfer([fileEntry('a.txt')])), '');
        });
        expect(onUpdateFiles).not.toHaveBeenCalled();
    });
});
