// OS drag-and-drop → a flat list of files WITH their relative paths, so a
// dropped FOLDER lands as its whole recursive tree instead of silently
// doing nothing.
//
// Why this module exists at all: `dataTransfer.files` (the FileList every
// drop handler used to read) cannot describe a directory. A dropped folder
// still shows up there — as a size-0 `File` whose `arrayBuffer()` rejects or
// yields nothing — so reading the FileList produced junk entries named after
// the folder and dropped every file inside it. The only web API that can see
// into a dropped directory is the (webkit-prefixed, but universally
// implemented — Chrome, Firefox, Safari and Electron's Chromium) entries API:
//
//   dataTransfer.items[i].webkitGetAsEntry() -> FileSystemEntry
//   FileSystemDirectoryEntry.createReader().readEntries(cb) -> a BATCH
//   FileSystemFileEntry.file(cb, errCb) -> the File
//
// Two hard constraints shape the split into capture + collect:
//
//   1. `dataTransfer.items` is emptied the moment the drop handler returns,
//      so `webkitGetAsEntry()` MUST be called synchronously inside the
//      handler — before any `await`. `captureDrop()` is that synchronous
//      snapshot; every drop site calls it first and hands the capture to the
//      async work. The FileSystemEntry objects themselves stay valid after
//      the event, which is what makes the deferred walk legal.
//   2. `readEntries` returns at most ~100 entries per call (Chrome) and
//      signals "done" with an EMPTY batch — a single call silently truncates
//      big folders, so the reader must be pumped in a loop.
//
// Deliberately framework-free (no React, no store, no VirtualFile): it is
// pure DOM plumbing, unit-tested against fake entry objects, and every caller
// — the cell file tree, the folder view, the canvas —
// shares this one walk.

/** One dropped file plus where it sat inside the drop.
 *
 *  `path` is relative to the DROP (no leading slash, always '/' separators):
 *  a bare filename for a file dropped on its own, `photos/2024/a.jpg` for a
 *  file inside a dropped `photos` folder. Callers prefix their own target
 *  folder onto it. */
export interface DroppedFile {
    path: string;
    file: File;
}

/** What a walk found: every file, plus the folders that turned out to hold
 *  nothing at all. Empty folders can't survive as files, so callers mint the
 *  codebase's `<folder>/.collkeep` marker for them (see lib/fileTree). */
export interface DropCollection {
    files: DroppedFile[];
    emptyDirs: string[];
}

/** The synchronous snapshot of a drop, taken inside the drop handler.
 *
 *  `entries` are the top-level FileSystemEntry objects (files AND folders);
 *  `files` is the plain FileList snapshot kept as the fallback for any
 *  environment that hands us no entries (an old browser, a synthetic event in
 *  a test, a drag source that exposes only files). */
export interface DropCapture {
    entries: DropEntry[];
    files: File[];
}

/** Structural stand-in for FileSystemEntry / FileSystemFileEntry /
 *  FileSystemDirectoryEntry. Hand-rolled rather than using the DOM types so
 *  the walk can be driven by plain fake objects in tests, and so a browser
 *  that only half-implements the API (missing `isFile`, say) is still handled
 *  by sniffing for the methods that matter. */
export interface DropEntry {
    name: string;
    isFile?: boolean;
    isDirectory?: boolean;
    /** '/<top>/<sub>/<name>' in real implementations. Unused — the walk
     *  builds paths from the names it descends through, which is the same
     *  thing without trusting a field browsers disagree about. */
    fullPath?: string;
    file?(onDone: (file: File) => void, onError?: (err: unknown) => void): void;
    createReader?(): DropDirectoryReader;
}

export interface DropDirectoryReader {
    readEntries(onDone: (entries: DropEntry[]) => void, onError?: (err: unknown) => void): void;
}

/** Depth cap for the recursive walk. Directory entries can (via symlinks on
 *  some platforms) describe a cycle; a cap turns a hang into a warning. Deeper
 *  than any real dropped tree. */
const MAX_DEPTH = 32;

/** Batch cap for the readEntries pump: a reader that never returns its
 *  terminating empty batch would loop forever. 10k batches × ~100 entries is
 *  far beyond any plausible drop. */
const MAX_BATCHES = 10000;

/** Take the synchronous snapshot of a drop. MUST be called inside the drop
 *  handler, before any `await` — `dataTransfer.items` is cleared as soon as
 *  the event finishes, and with it the only way to see into a dropped folder.
 *  Never throws: a DataTransfer that refuses `items` or `files` (some drag
 *  sources, some test doubles) just yields an emptier capture. */
export function captureDrop(dt: DataTransfer | null | undefined): DropCapture {
    const files: File[] = [];
    try {
        if (dt?.files) files.push(...Array.from(dt.files as ArrayLike<File>));
    } catch {
        // A DataTransfer whose files accessor throws (protected drag source) —
        // the entries below may still work.
    }

    const entries: DropEntry[] = [];
    const items = dt?.items as ArrayLike<DataTransferItem> | undefined;
    if (items) {
        for (let i = 0; i < items.length; i++) {
            const item = items[i] as DataTransferItem & { webkitGetAsEntry?: () => unknown };
            if (typeof item?.webkitGetAsEntry !== 'function') continue;
            let entry: unknown;
            try {
                // Returns null for non-file items (a dragged string/URL) and in
                // browsers that expose the method but no filesystem for this
                // drag — both are simply "nothing to walk here".
                entry = item.webkitGetAsEntry();
            } catch {
                entry = null;
            }
            if (entry) entries.push(entry as DropEntry);
        }
    }
    return { entries, files };
}

/** True when the capture carries anything at all — the guard every drop site
 *  uses in place of the old `dataTransfer.files.length > 0` check (a drag with
 *  entries but no readable FileList must not be dismissed). */
export function hasDroppedItems(capture: DropCapture): boolean {
    return capture.entries.length > 0 || capture.files.length > 0;
}

/** Walk a capture into files-with-paths plus the empty folders found.
 *
 *  Recurses through every dropped directory; a folder that can't be read is
 *  reported and skipped rather than failing the whole drop (one unreadable
 *  subfolder must not cost the user the other 500 files). Falls back to the
 *  plain FileList — each file at its bare name — when the drop produced no
 *  entries at all, which keeps every pre-entries-API environment (and the
 *  synthetic drag events in tests) working exactly as before. */
export async function collectDroppedFiles(capture: DropCapture): Promise<DropCollection> {
    const files: DroppedFile[] = [];
    const emptyDirs: string[] = [];

    if (capture.entries.length === 0) {
        // No entries API for this drop. Folders are unreachable here (they'd
        // be size-0 Files), but plain files still land — the old behavior.
        return { files: capture.files.map(file => ({ path: file.name, file })), emptyDirs };
    }

    // Sequential on purpose: it keeps a deterministic order (the drop's own),
    // and a parallel walk of a deep tree would open thousands of file handles
    // at once for no wall-clock gain — the reads are IPC-bound, not CPU-bound.
    for (const entry of capture.entries) {
        await walkEntry(entry, entry?.name ?? '', files, emptyDirs, 0);
    }
    return { files, emptyDirs };
}

/** Depth-first walk of one entry. `path` is the entry's path relative to the
 *  drop (built from the names walked through, never from `fullPath`). */
async function walkEntry(
    entry: DropEntry | null | undefined,
    path: string,
    files: DroppedFile[],
    emptyDirs: string[],
    depth: number,
): Promise<void> {
    if (!entry || !path) return;

    if (isDirectoryEntry(entry)) {
        if (depth >= MAX_DEPTH) {
            console.warn(`[drop] folder "${path}" is nested deeper than ${MAX_DEPTH} levels — skipped`);
            return;
        }
        let children: DropEntry[];
        try {
            children = await readAllEntries(entry);
        } catch (err) {
            console.warn(`[drop] could not read folder "${path}" — skipped`, err);
            return;
        }
        if (children.length === 0) {
            // Genuinely empty: nothing below it can carry the folder, so the
            // caller mints a keep-marker for it.
            emptyDirs.push(path);
            return;
        }
        for (const child of children) {
            if (!child?.name) continue;
            await walkEntry(child, `${path}/${child.name}`, files, emptyDirs, depth + 1);
        }
        return;
    }

    if (typeof entry.file !== 'function') {
        // Neither a readable file nor a directory (a half-implemented entry) —
        // there is nothing sensible to import.
        console.warn(`[drop] "${path}" is neither a file nor a folder — skipped`);
        return;
    }
    try {
        files.push({ path, file: await fileOfEntry(entry) });
    } catch (err) {
        // A file the OS won't hand over (deleted mid-drag, permission denied).
        console.warn(`[drop] could not read file "${path}" — skipped`, err);
    }
}

/** A directory when it says so, or when it offers a reader and doesn't claim
 *  to be a file — implementations vary in which flags they set. */
function isDirectoryEntry(entry: DropEntry): boolean {
    if (entry.isDirectory === true) return true;
    return entry.isFile !== true && typeof entry.createReader === 'function';
}

/** Pump a directory reader until it returns an empty batch.
 *
 *  `readEntries` yields at most ~100 entries per call in Chrome and marks the
 *  end of the directory with an EMPTY batch — the classic bug here is calling
 *  it once and silently importing only the first 100 children. Calls are
 *  strictly sequential: the reader is stateful and overlapping reads skip or
 *  repeat entries. */
async function readAllEntries(dir: DropEntry): Promise<DropEntry[]> {
    const reader = dir.createReader?.();
    if (!reader || typeof reader.readEntries !== 'function') {
        throw new Error('directory reader unavailable');
    }
    const all: DropEntry[] = [];
    for (let i = 0; i < MAX_BATCHES; i++) {
        const batch = await new Promise<DropEntry[]>((resolve, reject) => {
            reader.readEntries(
                got => resolve(Array.isArray(got) ? got : []),
                err => reject(err ?? new Error('readEntries failed')),
            );
        });
        if (batch.length === 0) return all;
        all.push(...batch);
    }
    console.warn(`[drop] folder "${dir.name}" returned more than ${MAX_BATCHES} batches — truncated`);
    return all;
}

/** Promisified FileSystemFileEntry.file(). Rejects (rather than hanging) when
 *  the entry hands back nothing, so the caller can skip that one file. */
function fileOfEntry(entry: DropEntry): Promise<File> {
    return new Promise<File>((resolve, reject) => {
        if (typeof entry.file !== 'function') {
            reject(new Error('not a file entry'));
            return;
        }
        entry.file(
            file => (file ? resolve(file) : reject(new Error('empty file handle'))),
            err => reject(err ?? new Error('file() failed')),
        );
    });
}
