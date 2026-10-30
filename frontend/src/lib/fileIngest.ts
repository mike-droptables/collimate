// Shared OS-file → VirtualFile ingest, for the file-tree drop (into a cell),
// the canvas drop (a new cell) and paste. Every file streams into the CAS
// through lib/transfer/uploadQueue (the browser sends the File body from
// disk; nothing is read into page memory), and the VirtualFile references
// it by hash: the server's sniff decides whether it is text or binary.

import { VirtualFile } from '@/lib/types';
import { FOLDER_KEEP_FILE, isFolderKeep } from '@/lib/fileTree';
import { binaryFile, textFile } from './fileCommands';
import { languageForPath } from '@/lib/fileLanguage';
import type { DroppedFile } from '@/lib/dropEntries';
import { uploadAll } from '@/lib/transfer/uploadQueue';
import { useCollimateStore } from '@/stores/useCollimateStore';

/** What ingest accepts. A plain FileList/File[] is the flat case (each file
 *  keeps its bare name); DroppedFile[] comes from the entries-API walk in
 *  lib/dropEntries and carries the file's path INSIDE a dropped folder, which
 *  becomes the VirtualFile name so the tree is reproduced verbatim. */
export type IngestInput = FileList | File[] | DroppedFile[];

/** Normalize either shape to path-carrying entries. Sniffs the DroppedFile
 *  shape rather than `instanceof File` — File identity differs across realms
 *  (iframes, jsdom) and a shape check can't be fooled by that. */
function asDroppedFiles(input: IngestInput): DroppedFile[] {
    return Array.from(input as ArrayLike<File | DroppedFile>).map(item => {
        const candidate = item as DroppedFile;
        return typeof candidate?.path === 'string' && candidate.file
            ? candidate
            : { path: (item as File).name, file: item as File };
    });
}

/** Whether the OS will hand over the file's bytes: one byte read. A file
 *  deleted or renamed since the drag snapshot, a permission error, a cloud
 *  placeholder that will not hydrate, or the size-0 pseudo-File a browser
 *  lists for a folder when it gives no entries API all reject here. */
async function readable(file: File): Promise<boolean> {
    try {
        await file.slice(0, 1).arrayBuffer();
        return true;
    } catch {
        return false;
    }
}

/** Ingest OS files into VirtualFiles, named by their path relative to the drop
 *  (a bare filename for a flat drop — callers add any folder prefix). Each
 *  one references its uploaded body by hash, text files too.
 *
 *  Per-file failures are ISOLATED: a file the OS won't hand over and a body
 *  the server won't store are each skipped, and the rest of the drop still
 *  lands; the user is told once, at the end. A single drop can carry a whole
 *  recursive tree, and one bad file must not cost the other 499. Callers
 *  fire this and forget (a DOM drop handler can't await), so throwing would
 *  also mean a silent unhandled rejection with no user feedback at all. */
export async function ingestFilesToVirtualFiles(input: IngestInput): Promise<VirtualFile[]> {
    const list = asDroppedFiles(input);
    const unreadable: string[] = [];
    const unstorable: string[] = [];
    const ready: DroppedFile[] = [];
    const probes = await Promise.all(list.map(dropped => readable(dropped.file)));
    list.forEach((dropped, i) => {
        if (probes[i]) { ready.push(dropped); return; }
        console.warn(`[drop] could not read "${dropped.path}" — skipped`);
        unreadable.push(dropped.path);
    });
    const stored = await uploadAll(ready.map(({ path, file }) => ({ path, body: file })), {
        onFileError: (path, error) => {
            console.warn(`[drop] could not store "${path}" — skipped`, error);
            unstorable.push(path);
        },
    });
    const out: VirtualFile[] = [];
    for (const { path } of ready) {
        const blob = stored.get(path);
        if (!blob) continue;
        out.push(blob.encoding === 'binary'
            ? binaryFile(path, blob.blob_hash)
            : { name: path, language: languageForPath(path), file_revision: 0, body_hash: blob.blob_hash, encoding: 'utf-8', writer_id: '', edit_seq: 0 });
    }
    if (unreadable.length > 0 || unstorable.length > 0) reportSkipped(unreadable, unstorable);
    return out;
}

/** Name up to five paths, then count the rest — a folder drop can skip
 *  hundreds and an alert listing them all is unreadable. */
function nameList(paths: string[]): string {
    const shown = paths.slice(0, 5).map(p => `\`${p}\``).join(', ');
    return paths.length > 5 ? `${shown} and ${paths.length - 5} more` : shown;
}

/** Tell the user about the files a drop couldn't take — ONCE, not once per
 *  file. Before folders were importable a failure meant one hand-picked file
 *  and a per-file dialog was fine; a recursive drop can skip many, and 300
 *  stacked alerts would be worse than the loss they report. */
function reportSkipped(unreadable: string[], unstorable: string[]): void {
    const lines: string[] = [];
    if (unreadable.length > 0) lines.push(`Could not be read: ${nameList(unreadable)}.`);
    if (unstorable.length > 0) lines.push(`Could not be stored: ${nameList(unstorable)}.`);
    const total = unreadable.length + unstorable.length;
    void useCollimateStore.getState().openAlert({
        title: total === 1 ? 'One file was skipped' : `${total} files were skipped`,
        body: lines.join('\n\n'),
    });
}

/** Keep-markers for the EMPTY folders a drop brought along.
 *
 *  An empty directory can't be represented by files, so this codebase keeps it
 *  alive with a hidden `<folder>/.collkeep` (lib/fileTree) that rides the whole
 *  files-only pipeline and materializes the directory on disk at run time.
 *  `dirPaths` are already full cell paths (the caller has prefixed its drop
 *  target), and `existing` is the file set AFTER the drop's own files were
 *  merged in — so the marker semantics hold exactly: a folder that holds a
 *  real file needs no marker, and an existing marker is never duplicated. */
export function folderMarkersFor(dirPaths: string[], existing: VirtualFile[]): VirtualFile[] {
    const out: VirtualFile[] = [];
    const known = new Set(existing.map(f => f.name));
    for (const dir of dirPaths) {
        if (!dir) continue;
        const prefix = `${dir}/`;
        const marker = `${prefix}${FOLDER_KEEP_FILE}`;
        if (known.has(marker)) continue;
        if (existing.some(f => f.name.startsWith(prefix) && !isFolderKeep(f.name))) continue;
        known.add(marker);
        out.push(textFile(marker,''));
    }
    return out;
}
