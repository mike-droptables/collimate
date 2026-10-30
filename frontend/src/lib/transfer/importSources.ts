// Where an import's files come from: the OS pickers. The File System Access
// pickers where the browser has them (Chromium, the desktop app), the
// classic hidden <input type=file> elsewhere. A zip goes whole to the
// server, which expands it (lib/api/transfer.expandZip); nothing here reads
// a file's bytes.
//
// Every pick function opens its picker before its first await: browsers
// only open one inside the click or key press that asked for it.
import type { DropCollection } from '@/lib/dropEntries';
import { fsAccess, isPickerCancel, type FsDirectoryHandle } from './fsAccess';

export interface PickedFile {
    /** Relative to the picked folder ('/' separators), or a bare name. */
    path: string;
    file: File;
}

/** A picked or dropped folder: its files and empty folders relative to it,
 *  and its name (what a one-cell import is called). */
export interface PickedTree {
    rootName: string;
    /** The root is a folder of the user's (picked, or the one folder
     *  dropped), which keeps its name when the tree lands inside a cell. */
    rootIsFolder: boolean;
    files: PickedFile[];
    dirs: string[];
    /** Files the OS would not hand over (removed meanwhile, no permission). */
    unreadable: string[];
}

/** Whether the browser can pick (and so export into) a folder it may
 *  write: the File System Access API. */
export const hasDirectoryPicker = (): boolean => typeof fsAccess().showDirectoryPicker === 'function';

const ZIP_TYPES = new Set(['application/zip', 'application/x-zip-compressed', 'application/x-zip']);

/** A zip by its extension or its MIME type. */
export const isZipFile = (file: { name: string; type?: string }): boolean =>
    /\.zip$/i.test(file.name) || ZIP_TYPES.has((file.type ?? '').toLowerCase());

/** Walk a picked folder lazily: each file as it is reached, with its path
 *  under the folder. Empty folders go into `found.dirs` and files that
 *  cannot be read into `found.unreadable`. */
export async function* walkDirectory(
    dir: FsDirectoryHandle, found: { dirs: string[]; unreadable: string[] }, prefix = '',
): AsyncGenerator<PickedFile> {
    let empty = true;
    for await (const handle of dir.values()) {
        empty = false;
        const path = prefix ? `${prefix}/${handle.name}` : handle.name;
        if (handle.kind === 'directory') {
            yield* walkDirectory(handle, found, path);
            continue;
        }
        let file: File;
        try {
            file = await handle.getFile();
        } catch (error) {
            console.warn(`[import] could not read "${path}" — skipped`, error);
            found.unreadable.push(path);
            continue;
        }
        yield { path, file };
    }
    if (empty && prefix) found.dirs.push(prefix);
}

interface InputOptions {
    multiple?: boolean;
    directory?: boolean;
    accept?: string;
}

/** The classic file chooser: a hidden input clicked now. Null when the user
 *  closes it without choosing. */
function chooseWithInput({ multiple, directory, accept }: InputOptions): Promise<File[] | null> {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = !!multiple;
    if (directory) input.setAttribute('webkitdirectory', '');
    if (accept) input.accept = accept;
    input.hidden = true;
    document.body.appendChild(input);
    return new Promise<File[] | null>(resolve => {
        const done = (files: File[] | null) => { input.remove(); resolve(files); };
        input.addEventListener('change', () => done(input.files ? Array.from(input.files) : null), { once: true });
        input.addEventListener('cancel', () => done(null), { once: true });
        input.click();
    });
}

/** Pick any number of files. Null when the picker was closed. */
export async function pickFiles(): Promise<File[] | null> {
    const fs = fsAccess();
    if (!fs.showOpenFilePicker) return chooseWithInput({ multiple: true });
    try {
        const handles = await fs.showOpenFilePicker({ multiple: true });
        return await Promise.all(handles.map(handle => handle.getFile()));
    } catch (error) {
        if (isPickerCancel(error)) return null;
        throw error;
    }
}

/** Pick one file of `type`. Null when the picker was closed. */
async function pickOne(type: { description: string; mime: string; extension: string }): Promise<File | null> {
    const fs = fsAccess();
    if (!fs.showOpenFilePicker) return (await chooseWithInput({ accept: `${type.extension},${type.mime}` }))?.[0] ?? null;
    try {
        const [handle] = await fs.showOpenFilePicker({ multiple: false, types: [{ description: type.description, accept: { [type.mime]: [type.extension] } }] });
        return handle ? await handle.getFile() : null;
    } catch (error) {
        if (isPickerCancel(error)) return null;
        throw error;
    }
}

/** Pick one zip. Null when the picker was closed. */
export const pickZip = (): Promise<File | null> => pickOne({ description: 'Zip archive', mime: 'application/zip', extension: '.zip' });

/** Pick one JSON file. Null when the picker was closed. */
export const pickJson = (): Promise<File | null> => pickOne({ description: 'JSON file', mime: 'application/json', extension: '.json' });

/** A JSON file by its extension or its MIME type. */
export const isJsonFile = (file: { name: string; type?: string }): boolean =>
    /\.json$/i.test(file.name) || (file.type ?? '').toLowerCase() === 'application/json';

/** A webkitdirectory pick: each file's webkitRelativePath starts with the
 *  picked folder's name. Empty folders are not reported by this input. */
function treeFromRelativePaths(files: File[]): PickedTree | null {
    if (files.length === 0) return null;
    const relative = (file: File) => (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
    const rootName = relative(files[0]).split('/')[0] || 'Imported files';
    return {
        rootName,
        rootIsFolder: true,
        files: files.map(file => {
            const parts = relative(file).split('/');
            return { path: parts.length > 1 ? parts.slice(1).join('/') : parts[0], file };
        }),
        dirs: [],
        unreadable: [],
    };
}

/** Pick a folder and walk it. Null when the picker was closed. */
export async function pickFolder(): Promise<PickedTree | null> {
    const fs = fsAccess();
    if (!fs.showDirectoryPicker) {
        const files = await chooseWithInput({ directory: true, multiple: true });
        return files ? treeFromRelativePaths(files) : null;
    }
    let dir: FsDirectoryHandle;
    try {
        dir = await fs.showDirectoryPicker({ mode: 'read' });
    } catch (error) {
        if (isPickerCancel(error)) return null;
        throw error;
    }
    const found = { dirs: [] as string[], unreadable: [] as string[] };
    const files: PickedFile[] = [];
    for await (const picked of walkDirectory(dir, found)) files.push(picked);
    return { rootName: dir.name, rootIsFolder: true, files, dirs: found.dirs, unreadable: found.unreadable };
}

/** A drop onto the panel as a tree: one dropped folder is the root (as if
 *  it had been picked); anything else sits under a root named for the drop. */
export function treeFromDrop(drop: DropCollection, fallbackName = 'Dropped files'): PickedTree {
    const tops = new Set([...drop.files.map(f => f.path), ...drop.emptyDirs].map(path => path.split('/')[0]));
    const loose = drop.files.some(f => !f.path.includes('/'));
    if (tops.size === 1 && !loose) {
        const [rootName] = tops;
        const strip = (path: string) => path.slice(rootName.length + 1);
        return {
            rootName,
            rootIsFolder: true,
            files: drop.files.map(f => ({ path: strip(f.path), file: f.file })),
            dirs: drop.emptyDirs.map(strip).filter(Boolean),
            unreadable: [],
        };
    }
    return { rootName: fallbackName, rootIsFolder: false, files: drop.files.map(f => ({ path: f.path, file: f.file })), dirs: drop.emptyDirs, unreadable: [] };
}
