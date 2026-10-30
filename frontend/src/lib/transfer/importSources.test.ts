// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { isJsonFile, isZipFile, pickFiles, pickFolder, pickJson, pickZip, treeFromDrop, walkDirectory } from './importSources';
import type { FsDirectoryHandle, FsFileHandle } from './fsAccess';

const fileHandle = (name: string, broken = false): FsFileHandle => ({
    kind: 'file', name,
    getFile: async () => { if (broken) throw new DOMException('gone', 'NotFoundError'); return new File([name], name); },
    createWritable: async () => { throw new Error('read only'); },
});
const dirHandle = (name: string, children: (FsFileHandle | FsDirectoryHandle)[]): FsDirectoryHandle => ({
    kind: 'directory', name,
    async *values() { yield* children; },
    getDirectoryHandle: async () => { throw new Error('read only'); },
    getFileHandle: async () => { throw new Error('read only'); },
});

type PickerWindow = Record<'showDirectoryPicker' | 'showOpenFilePicker', unknown>;
const win = window as unknown as PickerWindow;
afterEach(() => { delete win.showDirectoryPicker; delete win.showOpenFilePicker; vi.restoreAllMocks(); });

describe('import sources (TR-11)', () => {
    it('walks a picked folder lazily: paths under it, empty folders and unreadable files recorded', async () => {
        vi.spyOn(console, 'warn').mockImplementation(() => {});
        const root = dirHandle('proj', [
            fileHandle('a.txt'),
            dirHandle('src', [fileHandle('main.ts'), dirHandle('empty', []), fileHandle('lost.bin', true)]),
        ]);
        const found = { dirs: [] as string[], unreadable: [] as string[] };
        const walk = walkDirectory(root, found);
        expect((await walk.next()).value?.path).toBe('a.txt');
        const rest: string[] = [];
        for await (const picked of walk) rest.push(picked.path);
        expect(rest).toEqual(['src/main.ts']);
        expect(found).toEqual({ dirs: ['src/empty'], unreadable: ['src/lost.bin'] });
    });

    it('pickFolder uses the directory picker and names the tree after the folder', async () => {
        win.showDirectoryPicker = vi.fn(async () => dirHandle('photos', [fileHandle('x.jpg')]));
        const tree = await pickFolder();
        expect(tree).toMatchObject({ rootName: 'photos', rootIsFolder: true, dirs: [], unreadable: [] });
        expect(tree?.files.map(f => f.path)).toEqual(['x.jpg']);
    });

    it('a closed picker is no pick', async () => {
        win.showOpenFilePicker = vi.fn(async () => { throw new DOMException('closed', 'AbortError'); });
        expect(await pickFiles()).toBeNull();
    });

    it('without the pickers a hidden webkitdirectory input is clicked, and its paths lose the folder name', async () => {
        const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function (this: HTMLInputElement) {
            expect(this.hasAttribute('webkitdirectory')).toBe(true);
            const files = [new File(['1'], 'a.txt'), new File(['2'], 'b.txt')];
            Object.defineProperty(files[0], 'webkitRelativePath', { value: 'proj/a.txt' });
            Object.defineProperty(files[1], 'webkitRelativePath', { value: 'proj/sub/b.txt' });
            Object.defineProperty(this, 'files', { value: files });
            this.dispatchEvent(new Event('change'));
        });
        const tree = await pickFolder();
        expect(click).toHaveBeenCalledTimes(1);
        expect(tree?.rootName).toBe('proj');
        expect(tree?.files.map(f => f.path)).toEqual(['a.txt', 'sub/b.txt']);
        expect(document.querySelector('input[type=file]')).toBeNull();
    });

    it('pickZip and pickJson ask the file picker for their one type, and a closed picker is no pick', async () => {
        const picker = vi.fn(async () => [fileHandle('export.zip')]);
        win.showOpenFilePicker = picker;
        expect((await pickZip())?.name).toBe('export.zip');
        expect(picker).toHaveBeenLastCalledWith({ multiple: false, types: [{ description: 'Zip archive', accept: { 'application/zip': ['.zip'] } }] });
        picker.mockResolvedValueOnce([fileHandle('settings.json')]);
        expect((await pickJson())?.name).toBe('settings.json');
        expect(picker).toHaveBeenLastCalledWith({ multiple: false, types: [{ description: 'JSON file', accept: { 'application/json': ['.json'] } }] });
        win.showOpenFilePicker = vi.fn(async () => { throw new DOMException('closed', 'AbortError'); });
        expect(await pickJson()).toBeNull();
        win.showOpenFilePicker = vi.fn(async () => { throw new DOMException('denied', 'SecurityError'); });
        await expect(pickZip()).rejects.toThrow('denied');
    });

    it('without the file picker a hidden input of that type is clicked', async () => {
        const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function (this: HTMLInputElement) {
            Object.defineProperty(this, 'files', { value: [new File(['{}'], 'a.json')] });
            this.dispatchEvent(new Event('change'));
        });
        expect((await pickJson())?.name).toBe('a.json');
        expect((click.mock.contexts[0] as HTMLInputElement).accept).toBe('.json,application/json');
    });

    it('a JSON file is known by its extension or MIME type', () => {
        expect(isJsonFile({ name: 'A.JSON' })).toBe(true);
        expect(isJsonFile({ name: 'settings', type: 'application/json' })).toBe(true);
        expect(isJsonFile({ name: 'settings.txt', type: 'text/plain' })).toBe(false);
    });

    it('a zip is known by its extension or MIME type', () => {
        expect(isZipFile({ name: 'Export.ZIP' })).toBe(true);
        expect(isZipFile({ name: 'blob', type: 'application/x-zip-compressed' })).toBe(true);
        expect(isZipFile({ name: 'notes.md', type: 'text/markdown' })).toBe(false);
    });

    it('a drop of one folder is that folder; anything else sits under the drop', () => {
        const f = new File([''], 'x');
        expect(treeFromDrop({ files: [{ path: 'proj/a.txt', file: f }], emptyDirs: ['proj/void'] }))
            .toEqual({ rootName: 'proj', rootIsFolder: true, files: [{ path: 'a.txt', file: f }], dirs: ['void'], unreadable: [] });
        expect(treeFromDrop({ files: [{ path: 'a.txt', file: f }, { path: 'proj/b.txt', file: f }], emptyDirs: [] }))
            .toMatchObject({ rootName: 'Dropped files', rootIsFolder: false });
    });
});
