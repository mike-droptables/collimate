// Where an export's bytes go. A zip streams from the server straight into a
// file the user picked (or, without the File System Access API, into the
// browser's own download). A folder export writes the manifest's tree into
// a folder the user picked, fetching each cell file from the CAS by hash
// and each file the export staged (a repository's `.git`) through its
// token, released when the export ends: that needs showDirectoryPicker, so
// it is unavailable elsewhere.
//
// Each export opens its picker before its first await: browsers only open
// one inside the click or key press that asked for it.
import { blobUrl } from '@/lib/api/history';
import { exportFileUrl, exportZipUrl, fetchExportManifest, releaseExport, responseError, type ExportChoices, type ExportEntry, type ExportManifest, type ExportTarget } from '@/lib/api/transfer';
import { fsAccess, isPickerCancel, type FsDirectoryHandle, type FsWritable } from './fsAccess';

export type ExportFormat = 'folder' | 'zip';

export type ExportOutcome = 'saved' | 'downloaded' | 'cancelled';

export interface ExportProgress {
    doneBytes: number;
    /** Null while the size is unknown (a zip streams with no length). */
    totalBytes: number | null;
    doneFiles: number;
    totalFiles: number | null;
}

export interface ExportOptions extends ExportChoices {
    signal?: AbortSignal;
    onProgress?: (progress: ExportProgress) => void;
    /** What the export left behind, said once its manifest is read (a
     *  folder export; a zip carries its notes in its provenance file). */
    onNotes?: (notes: string[]) => void;
}

/** Files written at once by a folder export. */
export const FOLDER_WRITE_CONCURRENCY = 4;

export type Availability = { available: true } | { available: false; reason: string };

const FOLDER_UNAVAILABLE = 'Folder export needs a browser that can write to a folder (Chromium or the desktop app); this one saves a zip.';

/** Whether this browser can export into a folder, and why not. */
export function folderExportAvailability(): Availability {
    return typeof fsAccess().showDirectoryPicker === 'function' ? { available: true } : { available: false, reason: FOLDER_UNAVAILABLE };
}

/** A pass-through that counts the bytes going by. */
function counter(onBytes: (n: number) => void): TransformStream<Uint8Array, Uint8Array> {
    return new TransformStream({
        transform(chunk, controller) { onBytes(chunk.byteLength); controller.enqueue(chunk); },
    });
}

async function fetchOk(url: string, signal?: AbortSignal): Promise<Response> {
    const response = await fetch(url, { signal, cache: 'no-store' });
    if (!response.ok) throw await responseError(response);
    return response;
}

/** Pipe a response body into a writable, closing it (an empty body still
 *  makes an empty file). */
async function writeBody(response: Response, writable: FsWritable, onBytes: (n: number) => void, signal?: AbortSignal): Promise<void> {
    if (!response.body) { await writable.close(); return; }
    await response.body.pipeThrough(counter(onBytes)).pipeTo(writable, { signal });
}

/** Hand a download to the browser's download manager; an empty
 *  `filename` keeps the server's own name for it. */
export function downloadLink(href: string, filename: string): void {
    const a = document.createElement('a');
    a.href = href;
    a.download = filename;
    a.rel = 'noopener';
    a.hidden = true;
    document.body.appendChild(a);
    a.click();
    a.remove();
}

/** A name every OS accepts as a file name, for a saved export: the same
 *  cleaning the server gives a zip's root folder and download name, with
 *  `fallback` for a name that cleans away to nothing. */
export function exportFileName(name: string, fallback = 'export'): string {
    // eslint-disable-next-line no-control-regex -- control characters are what this strips
    const clean = name.replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').replace(/[. ]+$/, '').trim();
    return clean || fallback;
}

/** Save the export as `<name>.zip`: streamed into the file the user picks
 *  where the browser can pick one, else a download. */
export async function exportZip(channel: string, target: ExportTarget, name: string, options: ExportOptions = {}): Promise<ExportOutcome> {
    const { signal, onProgress } = options;
    const url = exportZipUrl(channel, target, options);
    const filename = `${name}.zip`;
    const fs = fsAccess();
    if (!fs.showSaveFilePicker) {
        downloadLink(url, filename);
        return 'downloaded';
    }
    let writable: FsWritable;
    try {
        const handle = await fs.showSaveFilePicker({ suggestedName: filename, types: [{ description: 'Zip archive', accept: { 'application/zip': ['.zip'] } }] });
        writable = await handle.createWritable();
    } catch (error) {
        if (isPickerCancel(error)) return 'cancelled';
        throw error;
    }
    let doneBytes = 0;
    try {
        const response = await fetchOk(url, signal);
        await writeBody(response, writable, n => {
            doneBytes += n;
            onProgress?.({ doneBytes, totalBytes: null, doneFiles: 0, totalFiles: null });
        }, signal);
    } catch (error) {
        // pipeTo aborts the file on a failed stream; a failure before it
        // began leaves it open.
        await writable.abort().catch(() => {});
        throw error;
    }
    return 'saved';
}

/** The refusal of a non-2xx response, carrying its status and body as
 *  `response` (as an axios error does), so `refusalText` gives the host's
 *  own sentence (`message`, else `detail`). */
async function refusal(response: Response): Promise<Error> {
    let data: unknown = undefined;
    try { data = await response.json(); } catch { /* a body that is not JSON says nothing more */ }
    const said = (data as { message?: unknown; detail?: unknown } | undefined);
    const message = typeof said?.message === 'string' ? said.message
        : typeof said?.detail === 'string' ? said.detail : `Request failed (${response.status})`;
    return Object.assign(new Error(message), { response: { status: response.status, data } });
}

/** Save the zip at `url` as `filename`, refusing with the host's words
 *  when it answers anything but 2xx: streamed into the file the user picks
 *  where the browser can pick one (the picker opens before any await),
 *  else fetched whole and handed to the download manager once the answer
 *  is ok, so a refusal never lands as a saved file. */
export async function saveCheckedZip(url: string, filename: string, signal?: AbortSignal): Promise<ExportOutcome> {
    const fs = fsAccess();
    if (!fs.showSaveFilePicker) {
        const response = await fetch(url, { signal, cache: 'no-store' });
        if (!response.ok) throw await refusal(response);
        const href = URL.createObjectURL(await response.blob());
        try {
            downloadLink(href, filename);
        } finally {
            setTimeout(() => URL.revokeObjectURL(href), 60_000);
        }
        return 'downloaded';
    }
    let writable: FsWritable;
    try {
        const handle = await fs.showSaveFilePicker({ suggestedName: filename, types: [{ description: 'Zip archive', accept: { 'application/zip': ['.zip'] } }] });
        writable = await handle.createWritable();
    } catch (error) {
        if (isPickerCancel(error)) return 'cancelled';
        throw error;
    }
    try {
        const response = await fetch(url, { signal, cache: 'no-store' });
        if (!response.ok) throw await refusal(response);
        await writeBody(response, writable, () => {}, signal);
    } catch (error) {
        await writable.abort().catch(() => {});
        throw error;
    }
    return 'saved';
}

/** Write the export's tree into a folder the user picks. */
export async function exportFolder(channel: string, target: ExportTarget, options: ExportOptions & { concurrency?: number } = {}): Promise<ExportOutcome> {
    const fs = fsAccess();
    if (!fs.showDirectoryPicker) throw new Error(FOLDER_UNAVAILABLE);
    let root: FsDirectoryHandle;
    try {
        root = await fs.showDirectoryPicker({ mode: 'readwrite' });
    } catch (error) {
        if (isPickerCancel(error)) return 'cancelled';
        throw error;
    }
    const manifest = await fetchExportManifest(channel, target, options.signal, { omitGitHistory: options.omitGitHistory });
    try {
        if (manifest.notes?.length) options.onNotes?.(manifest.notes);
        await writeManifest(channel, manifest, root, options);
    } finally {
        // The staged files are served until the export is released.
        if (manifest.token) await releaseExport(channel, manifest.token).catch(error => console.warn('[export] releasing the staged files failed', error));
    }
    return 'saved';
}

/** Where an entry's bytes come from: the CAS, or the export's staging. */
const entryUrl = (channel: string, token: string | undefined, entry: ExportEntry): string => {
    if ('body_hash' in entry) return blobUrl(channel, entry.body_hash);
    if (!token) throw new Error(`The export names a staged file without its token: ${entry.path}`);
    return exportFileUrl(channel, token, entry.export_file);
};

/** Write the manifest's folders and files under `root`. */
async function writeManifest(
    channel: string, manifest: ExportManifest, root: FsDirectoryHandle,
    options: ExportOptions & { concurrency?: number },
): Promise<void> {
    const { signal, onProgress } = options;
    const totalBytes = manifest.entries.reduce((sum, entry) => sum + entry.size, 0);
    let doneBytes = 0;
    let doneFiles = 0;
    const report = () => onProgress?.({ doneBytes, totalBytes, doneFiles, totalFiles: manifest.entries.length });

    // One handle per folder path, made once even when several writes reach
    // it together.
    const folders = new Map<string, Promise<FsDirectoryHandle>>([['', Promise.resolve(root)]]);
    const folder = (path: string): Promise<FsDirectoryHandle> => {
        let made = folders.get(path);
        if (!made) {
            const slash = path.lastIndexOf('/');
            const parent = slash < 0 ? '' : path.slice(0, slash);
            made = folder(parent).then(dir => dir.getDirectoryHandle(path.slice(slash + 1), { create: true }));
            folders.set(path, made);
        }
        return made;
    };

    report();
    for (const dir of manifest.dirs) await folder(dir);
    let next = 0;
    // The first failure stops the other writers taking more files.
    let failed = false;
    const writeEntries = async () => {
        while (next < manifest.entries.length && !failed) {
            if (signal?.aborted) throw new DOMException('The export was cancelled.', 'AbortError');
            const entry = manifest.entries[next++];
            const slash = entry.path.lastIndexOf('/');
            const dir = await folder(slash < 0 ? '' : entry.path.slice(0, slash));
            const file = await dir.getFileHandle(entry.path.slice(slash + 1), { create: true });
            const writable = await file.createWritable();
            try {
                const response = await fetchOk(entryUrl(channel, manifest.token, entry), signal);
                await writeBody(response, writable, n => { doneBytes += n; report(); }, signal);
            } catch (error) {
                await writable.abort().catch(() => {});
                throw error;
            }
            doneFiles += 1;
            report();
        }
    };
    const worker = () => writeEntries().catch(error => { failed = true; throw error; });
    const concurrency = Math.max(1, options.concurrency ?? FOLDER_WRITE_CONCURRENCY);
    await Promise.all(Array.from({ length: Math.min(concurrency, manifest.entries.length) }, worker));
}
