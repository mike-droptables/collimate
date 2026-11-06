// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/transfer', async original => ({
    ...await original<typeof import('@/lib/api/transfer')>(),
    fetchExportManifest: vi.fn(),
    releaseExport: vi.fn(async () => {}),
}));

import { fetchExportManifest, releaseExport, type ExportManifest } from '@/lib/api/transfer';
import { exportFileName, exportFolder, exportZip, folderExportAvailability, saveCheckedZip, type ExportProgress } from './exportSinks';

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
    const staged = /\/export\/tok\/file\?path=(.*)$/.exec(url);
    if (staged) return new Response(`staged:${decodeURIComponent(staged[1])}`);
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

describe('export sinks: a checked zip (a rescued .git)', () => {
    const URL_ = '/api/channels/ch/cells/c1/repositories/rescue/export?path=';
    const gone = () => new Response('{"error":"rescue_not_found","message":"no rescued git history at ."}', { status: 404 });

    it('without a save picker downloads the bytes only once the answer is ok', async () => {
        const clicks: HTMLAnchorElement[] = [];
        const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this); });
        vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:zip'), revokeObjectURL: vi.fn() }));
        fetchMock.mockResolvedValueOnce(new Response('PK-rescue'));
        expect(await saveCheckedZip(URL_, 'cell git history.zip')).toBe('downloaded');
        expect(clicks.map(a => [a.getAttribute('href'), a.download])).toEqual([['blob:zip', 'cell git history.zip']]);
        fetchMock.mockResolvedValueOnce(gone());
        await expect(saveCheckedZip(URL_, 'cell git history.zip')).rejects.toMatchObject({
            message: 'no rescued git history at .', response: { status: 404 } });
        expect(clicks).toHaveLength(1);
        click.mockRestore();
    });

    it('with a save picker a refusal aborts the picked file', async () => {
        const target = new FakeFile('x.zip');
        win.showSaveFilePicker = vi.fn(async () => target);
        fetchMock.mockResolvedValueOnce(gone());
        await expect(saveCheckedZip(URL_, 'x.zip')).rejects.toThrow('no rescued git history at .');
        expect(target.aborted).toBe(true);
        fetchMock.mockResolvedValueOnce(new Response('PK-rescue'));
        const saved = new FakeFile('y.zip');
        win.showSaveFilePicker = vi.fn(async () => saved);
        expect(await saveCheckedZip(URL_, 'y.zip')).toBe('saved');
        expect(saved.text()).toBe('PK-rescue');
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
        expect(fetchExportManifest).toHaveBeenCalledWith('ch', { scope: 'cell', cellId: 'c1' }, undefined, { omitGitHistory: undefined });
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

// A repository's .git in a folder export is staged by the host and served
// through the export's token, released when the export ends (§7).
describe('export sinks: a repository’s staged files', () => {
    beforeEach(() => vi.mocked(releaseExport).mockClear());

    it('fetches each staged entry through the token route, says the notes and releases the staging', async () => {
        const root = new FakeDir('picked');
        win.showDirectoryPicker = vi.fn(async () => root);
        vi.mocked(fetchExportManifest).mockResolvedValue({
            root: 'Cell', token: 'tok', notes: ['app: the stash was left out'], dirs: ['Cell/.git/refs/heads'],
            entries: [
                { path: 'Cell/a.txt', body_hash: 'h1', size: 5, encoding: 'utf-8' },
                { path: 'Cell/.git/HEAD', export_file: '0/HEAD', size: 21 },
            ],
        });
        const notes: string[] = [];
        expect(await exportFolder('ch', { scope: 'cell', cellId: 'c1' }, { omitGitHistory: true, onNotes: said => notes.push(...said) })).toBe('saved');
        expect(fetchExportManifest).toHaveBeenLastCalledWith('ch', { scope: 'cell', cellId: 'c1' }, undefined, { omitGitHistory: true });
        expect(root.tree()).toMatchObject({ 'Cell/a.txt': 'alpha', 'Cell/.git/HEAD': 'staged:0/HEAD', 'Cell/.git/refs/heads/': '' });
        expect(fetchMock.mock.calls.map(call => String(call[0])).find(url => url.includes('/export/tok/file'))).toMatch(/\/channels\/ch\/export\/tok\/file\?path=0%2FHEAD$/);
        expect(notes).toEqual(['app: the stash was left out']);
        expect(releaseExport).toHaveBeenCalledWith('ch', 'tok');
    });

    it('a failed write still releases the staging', async () => {
        const root = new FakeDir('picked');
        win.showDirectoryPicker = vi.fn(async () => root);
        vi.mocked(fetchExportManifest).mockResolvedValue({
            root: 'C', token: 'tok', dirs: [], entries: [{ path: 'C/x', body_hash: 'missing', size: 1, encoding: 'binary' }],
        });
        await expect(exportFolder('ch', { scope: 'all' })).rejects.toThrow('gone');
        expect(releaseExport).toHaveBeenCalledWith('ch', 'tok');
    });

    it('a zip asks for the history to be omitted in its query', async () => {
        const clicks: HTMLAnchorElement[] = [];
        const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this); });
        await exportZip('ch', { scope: 'cell', cellId: 'c1' }, 'Cell', { omitGitHistory: true });
        expect(clicks[0].href).toMatch(/export\.zip\?scope=cell&cell_id=c1&omit_git_history=true$/);
        click.mockRestore();
    });
});
