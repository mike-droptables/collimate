// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/transfer', async original => ({
    ...await original<typeof import('@/lib/api/transfer')>(),
    fetchExportManifest: vi.fn(),
}));

import { fetchExportManifest, type ExportManifest } from '@/lib/api/transfer';
import { exportFileName, exportFolder, exportZip, folderExportAvailability, type ExportProgress } from './exportSinks';

/** A picked file: what its writable received, and whether it closed. */
class FakeFile {
    kind = 'file' as const;
    chunks: Uint8Array[] = [];
    closed = false;
    aborted = false;
    constructor(public name: string) {}
    async getFile(): Promise<File> { return new File(this.chunks as BlobPart[], this.name); }
    async createWritable() {
        return new WritableStream<Uint8Array>({
            write: chunk => { this.chunks.push(chunk); },
            close: () => { this.closed = true; },
            abort: () => { this.aborted = true; },
        });
    }
    text() { return new TextDecoder().decode(new Uint8Array(this.chunks.flatMap(c => [...c]))); }
}

/** A picked folder, made on demand. */
class FakeDir {
    kind = 'directory' as const;
    children = new Map<string, FakeDir | FakeFile>();
    constructor(public name: string) {}
    async getDirectoryHandle(name: string) {
        const found = this.children.get(name) ?? new FakeDir(name);
        this.children.set(name, found);
        return found as FakeDir;
    }
    async getFileHandle(name: string) {
        const found = this.children.get(name) ?? new FakeFile(name);
        this.children.set(name, found);
        return found as FakeFile;
    }
    async *values() { yield* this.children.values(); }
    /** The tree below, as path → text ('/' suffix for a folder). */
    tree(prefix = ''): Record<string, string> {
        return Object.fromEntries([...this.children].flatMap(([name, child]) => child instanceof FakeDir
            ? [[`${prefix}${name}/`, ''], ...Object.entries(child.tree(`${prefix}${name}/`))]
            : [[`${prefix}${name}`, child.text()]]));
    }
}

type PickerWindow = Record<'showDirectoryPicker' | 'showSaveFilePicker' | 'showOpenFilePicker', unknown>;
const win = window as unknown as PickerWindow;
const bodies: Record<string, string> = { h1: 'alpha', h2: 'bravo bravo', h3: '' };
const fetchMock = vi.fn(async (url: string) => {
    const hash = url.split('/blob/')[1];
    if (hash !== undefined) return hash in bodies ? new Response(bodies[hash]) : new Response('{"detail":"gone"}', { status: 404 });
    if (url.includes('/export.zip')) return new Response('PK-zip-bytes');
    return new Response('', { status: 500 });
});

beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockClear();
});
afterEach(() => {
    delete win.showDirectoryPicker;
    delete win.showSaveFilePicker;
    delete win.showOpenFilePicker;
    vi.unstubAllGlobals();
});

describe('export file names', () => {
    it('replaces what no OS allows, trims trailing dots and spaces, and never comes back empty', () => {
        expect(exportFileName('a/b:c*?"<>|\u0001 d')).toBe('a_b_c_______ d');
        expect(exportFileName('notes. . ')).toBe('notes');
        expect(exportFileName(' .. ')).toBe('export');
        expect(exportFileName(' .. ', 'Channel')).toBe('Channel');
    });
});

describe('export sinks: zip (TR-10, TR-11)', () => {
    it('streams the zip into the file the user picks', async () => {
        const target = new FakeFile('pick.zip');
        const picker = vi.fn(async () => target);
        win.showSaveFilePicker = picker;
        const seen: ExportProgress[] = [];
        expect(await exportZip('ch', { scope: 'cell', cellId: 'c1' }, 'My cell', { onProgress: p => seen.push(p) })).toBe('saved');
        expect(picker).toHaveBeenCalledWith(expect.objectContaining({ suggestedName: 'My cell.zip' }));
        expect(fetchMock.mock.calls[0][0]).toMatch(/\/channels\/ch\/export\.zip\?scope=cell&cell_id=c1$/);
        expect(target.text()).toBe('PK-zip-bytes');
        expect(target.closed).toBe(true);
        expect(seen.at(-1)).toEqual({ doneBytes: 12, totalBytes: null, doneFiles: 0, totalFiles: null });
    });

    it('a closed picker cancels quietly; nothing is fetched', async () => {
        win.showSaveFilePicker = vi.fn(async () => { throw new DOMException('closed', 'AbortError'); });
        expect(await exportZip('ch', { scope: 'all' }, 'chan')).toBe('cancelled');
        expect(fetchMock).not.toHaveBeenCalled();
    });

    it("a refused export aborts the picked file with the server's sentence", async () => {
        const target = new FakeFile('x.zip');
        win.showSaveFilePicker = vi.fn(async () => target);
        fetchMock.mockResolvedValueOnce(new Response('{"detail":"Unknown stack"}', { status: 404 }));
        await expect(exportZip('ch', { scope: 'stack', stackId: 's1' }, 'stack')).rejects.toThrow('Unknown stack');
        expect(target.aborted).toBe(true);
    });

    it('without a save picker the browser downloads the zip link', async () => {
        const clicks: HTMLAnchorElement[] = [];
        const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this); });
        expect(await exportZip('ch', { scope: 'stack', stackId: 's1' }, 'Stack')).toBe('downloaded');
        expect(clicks).toHaveLength(1);
        expect(clicks[0].download).toBe('Stack.zip');
        expect(clicks[0].href).toMatch(/\/channels\/ch\/export\.zip\?scope=stack&stack_id=s1$/);
        expect(fetchMock).not.toHaveBeenCalled();
        click.mockRestore();
    });
});

describe('export sinks: folder (TR-10, TR-11)', () => {
    const manifest: ExportManifest = {
        root: 'Cell',
        entries: [
            { path: 'Cell/a.txt', body_hash: 'h1', size: 5, encoding: 'utf-8' },
            { path: 'Cell/sub/deep/b.bin', body_hash: 'h2', size: 11, encoding: 'binary' },
            { path: 'Cell/empty.txt', body_hash: 'h3', size: 0, encoding: 'utf-8' },
        ],
        dirs: ['Cell/void'],
    };

    it('is unavailable, with a reason, without a directory picker', async () => {
        expect(folderExportAvailability()).toMatchObject({ available: false, reason: expect.stringContaining('zip') });
        await expect(exportFolder('ch', { scope: 'all' })).rejects.toThrow(/zip/);
        win.showDirectoryPicker = vi.fn();
        expect(folderExportAvailability()).toEqual({ available: true });
    });

    it('writes the manifest tree into the picked folder, empty folders included', async () => {
        const root = new FakeDir('picked');
        const picker = vi.fn(async () => root);
        win.showDirectoryPicker = picker;
        vi.mocked(fetchExportManifest).mockResolvedValue(manifest);
        const seen: ExportProgress[] = [];
        expect(await exportFolder('ch', { scope: 'cell', cellId: 'c1' }, { onProgress: p => seen.push(p), concurrency: 2 })).toBe('saved');
        expect(picker).toHaveBeenCalledWith({ mode: 'readwrite' });
        expect(fetchExportManifest).toHaveBeenCalledWith('ch', { scope: 'cell', cellId: 'c1' }, undefined);
        expect(root.tree()).toEqual({
            'Cell/': '', 'Cell/void/': '', 'Cell/a.txt': 'alpha', 'Cell/sub/': '', 'Cell/sub/deep/': '',
            'Cell/sub/deep/b.bin': 'bravo bravo', 'Cell/empty.txt': '',
        });
        expect(seen.at(-1)).toEqual({ doneBytes: 16, totalBytes: 16, doneFiles: 3, totalFiles: 3 });
    });

    it('a blob that cannot be fetched fails the export and aborts that file', async () => {
        const root = new FakeDir('picked');
        win.showDirectoryPicker = vi.fn(async () => root);
        vi.mocked(fetchExportManifest).mockResolvedValue({ root: 'C', entries: [{ path: 'C/x', body_hash: 'missing', size: 1, encoding: 'binary' }], dirs: [] });
        await expect(exportFolder('ch', { scope: 'all' })).rejects.toThrow('gone');
        const c = root.children.get('C') as FakeDir;
        expect((c.children.get('x') as FakeFile).aborted).toBe(true);
    });

    it('a closed directory picker cancels quietly before the manifest is asked for', async () => {
        win.showDirectoryPicker = vi.fn(async () => { throw new DOMException('closed', 'AbortError'); });
        vi.mocked(fetchExportManifest).mockClear();
        expect(await exportFolder('ch', { scope: 'all' })).toBe('cancelled');
        expect(fetchExportManifest).not.toHaveBeenCalled();
    });
});
