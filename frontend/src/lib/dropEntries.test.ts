import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
    captureDrop, collectDroppedFiles, hasDroppedItems,
    type DropEntry, type DropDirectoryReader,
} from '@/lib/dropEntries';

// ── fake entries API ────────────────────────────────────────────────
// The real objects come from the browser's drag machinery, so the walk is
// driven here by hand-built stand-ins with the same shape: a file entry hands
// its File to a callback, a directory entry hands out BATCHES of children and
// terminates with an empty one (the readEntries contract this module exists to
// honor).

const asFile = (name: string) => new File([`content of ${name}`], name);

const fileEntry = (name: string, file: File = asFile(name)): DropEntry => ({
    name,
    isFile: true,
    isDirectory: false,
    file: (onDone) => onDone(file),
});

/** A file entry whose file() fails — the OS refusing the handle. */
const brokenFileEntry = (name: string): DropEntry => ({
    name,
    isFile: true,
    isDirectory: false,
    file: (_onDone, onError) => onError?.(new Error(`gone: ${name}`)),
});

/** A directory entry that serves `children` in slices of `batchSize`, then an
 *  empty batch — exactly how Chrome's reader behaves. `reads` counts the pump
 *  iterations so a test can prove the loop happened. */
const dirEntry = (
    name: string, children: DropEntry[], batchSize = 100,
): DropEntry & { reads: () => number } => {
    let reads = 0;
    return {
        name,
        isFile: false,
        isDirectory: true,
        createReader: (): DropDirectoryReader => {
            let cursor = 0;
            return {
                readEntries: (onDone) => {
                    reads += 1;
                    const batch = children.slice(cursor, cursor + batchSize);
                    cursor += batch.length;
                    onDone(batch);
                },
            };
        },
        reads: () => reads,
    };
};

/** A directory whose reader errors out (unreadable folder). */
const brokenDirEntry = (name: string): DropEntry => ({
    name,
    isFile: false,
    isDirectory: true,
    createReader: () => ({
        readEntries: (_onDone, onError) => onError?.(new Error(`unreadable: ${name}`)),
    }),
});

/** A DataTransfer stand-in. `entries` (when given) become the items'
 *  webkitGetAsEntry() results; `files` is the plain FileList fallback. Pass
 *  `entries: null` for an environment with no entries API at all. */
const transfer = (
    { entries, files = [] }: { entries?: (DropEntry | null)[] | null; files?: File[] },
): DataTransfer => ({
    files,
    items: entries === null || entries === undefined
        ? undefined
        : entries.map(entry => ({ kind: 'file', webkitGetAsEntry: () => entry })),
} as unknown as DataTransfer);

let warn: ReturnType<typeof vi.spyOn>;
beforeEach(() => { warn = vi.spyOn(console, 'warn').mockImplementation(() => {}); });
afterEach(() => { warn.mockRestore(); });

describe('captureDrop', () => {
    it('snapshots both the entries and the plain files', () => {
        const f = asFile('a.txt');
        const cap = captureDrop(transfer({ entries: [fileEntry('a.txt', f)], files: [f] }));
        expect(cap.entries.map(e => e.name)).toEqual(['a.txt']);
        expect(cap.files).toEqual([f]);
        expect(hasDroppedItems(cap)).toBe(true);
    });

    it('drops null entries (a dragged string item exposes none)', () => {
        const cap = captureDrop(transfer({ entries: [null, fileEntry('a.txt'), null] }));
        expect(cap.entries.map(e => e.name)).toEqual(['a.txt']);
    });

    it('survives a DataTransfer with no items, no files, or none at all', () => {
        expect(hasDroppedItems(captureDrop(transfer({ entries: null })))).toBe(false);
        expect(hasDroppedItems(captureDrop(null))).toBe(false);
        expect(hasDroppedItems(captureDrop(undefined))).toBe(false);
        // A files accessor that throws must not take the whole capture down.
        const hostile = { get files() { throw new Error('nope'); }, items: undefined } as unknown as DataTransfer;
        expect(() => captureDrop(hostile)).not.toThrow();
    });

    it('ignores items that expose no webkitGetAsEntry and entries that throw', () => {
        const dt = {
            files: [],
            items: [
                { kind: 'file' },                                     // pre-entries-API item
                { kind: 'file', webkitGetAsEntry: () => { throw new Error('boom'); } },
                { kind: 'file', webkitGetAsEntry: () => fileEntry('ok.txt') },
            ],
        } as unknown as DataTransfer;
        expect(captureDrop(dt).entries.map(e => e.name)).toEqual(['ok.txt']);
    });
});

describe('collectDroppedFiles', () => {
    it('walks a nested folder into relative paths', async () => {
        const tree = dirEntry('photos', [
            fileEntry('cover.jpg'),
            dirEntry('2024', [
                fileEntry('a.jpg'),
                dirEntry('raw', [fileEntry('b.dng')]),
            ]),
        ]);
        const { files, emptyDirs } = await collectDroppedFiles(captureDrop(transfer({ entries: [tree] })));
        expect(files.map(f => f.path)).toEqual([
            'photos/cover.jpg',
            'photos/2024/a.jpg',
            'photos/2024/raw/b.dng',
        ]);
        // The File itself rides along untouched — ingest reads its bytes later.
        expect(files[0].file.name).toBe('cover.jpg');
        expect(emptyDirs).toEqual([]);
    });

    it('keeps top-level files and a dropped folder in the same drop', async () => {
        const cap = captureDrop(transfer({
            entries: [fileEntry('notes.md'), dirEntry('src', [fileEntry('main.ts')])],
            files: [asFile('notes.md'), asFile('src')],
        }));
        const { files } = await collectDroppedFiles(cap);
        expect(files.map(f => f.path)).toEqual(['notes.md', 'src/main.ts']);
    });

    it('pumps readEntries until an empty batch — a >100-child folder is not truncated', async () => {
        const children = Array.from({ length: 250 }, (_, i) => fileEntry(`f${i}.txt`));
        const big = dirEntry('many', children);
        const { files } = await collectDroppedFiles(captureDrop(transfer({ entries: [big] })));
        expect(files).toHaveLength(250);
        expect(files[249].path).toBe('many/f249.txt');
        // 3 full batches (100/100/50) + the terminating empty one.
        expect(big.reads()).toBe(4);
    });

    it('reports empty folders (at every depth) instead of losing them', async () => {
        const tree = dirEntry('project', [
            dirEntry('empty', []),
            dirEntry('nested', [dirEntry('deep', [])]),
            fileEntry('README.md'),
        ]);
        const { files, emptyDirs } = await collectDroppedFiles(captureDrop(transfer({ entries: [tree] })));
        expect(files.map(f => f.path)).toEqual(['project/README.md']);
        expect(emptyDirs).toEqual(['project/empty', 'project/nested/deep']);
    });

    it('a folder dropped on its own with nothing in it is one empty dir', async () => {
        const { files, emptyDirs } = await collectDroppedFiles(
            captureDrop(transfer({ entries: [dirEntry('blank', [])] })));
        expect(files).toEqual([]);
        expect(emptyDirs).toEqual(['blank']);
    });

    it('skips an unreadable folder (warning) and keeps the rest of the drop', async () => {
        const cap = captureDrop(transfer({
            entries: [brokenDirEntry('locked'), dirEntry('ok', [fileEntry('a.txt')])],
        }));
        const { files, emptyDirs } = await collectDroppedFiles(cap);
        expect(files.map(f => f.path)).toEqual(['ok/a.txt']);
        // An unreadable folder is NOT an empty one — no marker should be minted.
        expect(emptyDirs).toEqual([]);
        expect(warn).toHaveBeenCalledWith(expect.stringContaining('locked'), expect.anything());
    });

    it('skips a file whose file() rejects and keeps its siblings', async () => {
        const cap = captureDrop(transfer({
            entries: [dirEntry('d', [brokenFileEntry('gone.txt'), fileEntry('here.txt')])],
        }));
        const { files } = await collectDroppedFiles(cap);
        expect(files.map(f => f.path)).toEqual(['d/here.txt']);
        expect(warn).toHaveBeenCalledWith(expect.stringContaining('d/gone.txt'), expect.anything());
    });

    it('skips an entry that is neither file nor directory', async () => {
        const cap = captureDrop(transfer({ entries: [{ name: 'weird', isFile: true } as DropEntry] }));
        const { files } = await collectDroppedFiles(cap);
        expect(files).toEqual([]);
        expect(warn).toHaveBeenCalledWith(expect.stringContaining('weird'));
    });

    it('treats an entry with a reader but no flags as a directory', async () => {
        // Some implementations set neither isFile nor isDirectory; the reader
        // is the real tell.
        const loose: DropEntry = {
            name: 'flagless',
            createReader: () => {
                let done = false;
                return {
                    readEntries: (onDone) => { onDone(done ? [] : [fileEntry('x.txt')]); done = true; },
                };
            },
        };
        const { files } = await collectDroppedFiles(captureDrop(transfer({ entries: [loose] })));
        expect(files.map(f => f.path)).toEqual(['flagless/x.txt']);
    });

    it('stops at the depth cap instead of chasing a cyclic tree', async () => {
        // A directory that is its own child — what a symlink loop looks like
        // through the entries API.
        const loop: DropEntry = {
            name: 'loop',
            isDirectory: true,
            createReader: () => {
                let done = false;
                return { readEntries: (onDone) => { onDone(done ? [] : [loop]); done = true; } };
            },
        };
        const { files } = await collectDroppedFiles(captureDrop(transfer({ entries: [loop] })));
        expect(files).toEqual([]);
        expect(warn).toHaveBeenCalledWith(expect.stringContaining('nested deeper than'));
    });

    it('falls back to the plain files when the entries API is missing', async () => {
        const a = asFile('a.txt');
        const b = asFile('b.txt');
        const { files, emptyDirs } = await collectDroppedFiles(
            captureDrop(transfer({ entries: null, files: [a, b] })));
        expect(files).toEqual([{ path: 'a.txt', file: a }, { path: 'b.txt', file: b }]);
        expect(emptyDirs).toEqual([]);
    });

    it('falls back when every item refuses an entry', async () => {
        const a = asFile('a.txt');
        const { files } = await collectDroppedFiles(
            captureDrop(transfer({ entries: [null, null], files: [a] })));
        expect(files).toEqual([{ path: 'a.txt', file: a }]);
    });

    it('does NOT fall back to the junk FileList when entries exist but yield nothing', async () => {
        // The whole bug: a dropped folder appears in dataTransfer.files as a
        // size-0 File named after the folder. With entries in hand we must
        // never import that.
        const cap = captureDrop(transfer({
            entries: [dirEntry('folder', [])], files: [new File([], 'folder')],
        }));
        const { files, emptyDirs } = await collectDroppedFiles(cap);
        expect(files).toEqual([]);
        expect(emptyDirs).toEqual(['folder']);
    });

    it('an empty capture collects nothing', async () => {
        expect(await collectDroppedFiles({ entries: [], files: [] }))
            .toEqual({ files: [], emptyDirs: [] });
    });
});
