import {readPinnedText,descriptorOf} from './fileBodies';
// File operations use pinned bodies and conditional durable receipts.

import { VirtualFile } from '@/lib/types';
import { FileTransferEntry } from '@/stores/collimate/types';
import { withBody, needsTextBody } from '@/lib/fileCommands';
import { isRunPath, RUNS_PREFIX } from '@/lib/runningCell';

/** A change to a cell's file list. The cell's writer (CellEditor's
 *  handleUpdateFiles) applies it to the cell's LATEST list at commit time,
 *  once and synchronously; returning `current` itself writes nothing. A
 *  file operation never hands the writer a list it built before an await (a
 *  folder walk, an upload, a body fetch, a confirm dialog): that list would
 *  revert whatever a run or another surface wrote to the cell meanwhile. */
export type FilesUpdater = (current: VirtualFile[]) => VirtualFile[];

/** A cell's file writer: takes a FilesUpdater. */
export interface FileRename {source:string;target:string}
export type UpdateFiles = (update: FilesUpdater, options?:{renames:FileRename[]}) => void | Promise<void>;

/** At most this many body fetches in flight per hydration. */
const HYDRATE_CONCURRENCY = 4;

/** File operations use pinned bodies and conditional durable receipts. */
export async function fetchTextBodies(
    channel: string, cellId: string, files: readonly VirtualFile[], init?: RequestInit,
): Promise<Map<string, string>> {
    const wanted=files.filter(needsTextBody);const names=wanted.map(file=>file.name);
    const bodies = new Map<string, string>();
    let next = 0;
    let failed = false;
    const worker = async () => {
        while (!failed && next < names.length) {
            const name = names[next++];
            try {
                bodies.set(name, await readPinnedText(channel,cellId,descriptorOf(wanted.find(file=>file.name===name)!),init));
            } catch (e) {
                failed = true;
                throw e;
            }
        }
    };
    await Promise.all(Array.from({ length: Math.min(HYDRATE_CONCURRENCY, names.length) }, worker));
    return bodies;
}

/** Associate bytes only with the exact accepted revision/hash that was read.
 * A descriptor changed during the read must not receive those older bytes. */
export type WithBody = (file: VirtualFile) => VirtualFile | null;

/** Bytes fetched for immutable descriptors, guarded by revision and hash. */
export function withFetchedBodies(fetched: readonly VirtualFile[], bodies: ReadonlyMap<string, string>): WithBody {
    const fetchedAt = new Map(fetched.filter(needsTextBody).map(f => [f.name, `${f.file_revision}:${f.body_hash}`] as const));
    return file => {
        if (!needsTextBody(file)) return file;
        const body = bodies.get(file.name);
        return body !== undefined && fetchedAt.get(file.name) === `${file.file_revision}:${file.body_hash}`
            ? withBody(file, body)
            : null;
    };
}

/** A short, comma-separated list of `names` for a dialog body. */
export function summarize(names: string[], max = 8): string {
    const head = names.slice(0, max).join(', ');
    return names.length > max ? `${head}, …(+${names.length - max})` : head;
}

/** Parent folder of a path ('' for a root-level path). */
export function parentOf(path: string): string {
    const i = path.lastIndexOf('/');
    return i >= 0 ? path.slice(0, i) : '';
}

/** Last path segment (basename). */
export function basename(path: string): string {
    const i = path.lastIndexOf('/');
    return i >= 0 ? path.slice(i + 1) : path;
}

/** Join a folder and a relative path; an empty folder yields `rel` as-is. */
export function joinPath(folder: string, rel: string): string {
    return folder ? `${folder}/${rel}` : rel;
}

/** Build transfer entries from a source node. `relPath` is relative to the
 *  PARENT of `nodePath`, so the node's own top segment is preserved at the
 *  destination (copying `src/components` into `lib/` ⇒ `lib/components/…`).
 *  A folder carries every descendant (incl. its `.collkeep` marker), so an
 *  empty folder survives the round-trip. */
export function collectTransferEntries(
    files: VirtualFile[],
    nodePath: string,
    isFolder: boolean,
): FileTransferEntry[] {
    const parent = parentOf(nodePath);
    const strip = (name: string) => (parent ? name.slice(parent.length + 1) : name);
    if (!isFolder) {
        const f = files.find(x => x.name === nodePath);
        return f ? [{ relPath: strip(f.name), file: f }] : [];
    }
    const prefix = nodePath + '/';
    return files
        .filter(f => f.name === nodePath || f.name.startsWith(prefix))
        .map(f => ({ relPath: strip(f.name), file: f }));
}

/** A payload leaving its cell (another cell, the canvas)
 *  never carries `_runs/` files: they belong to a run of the source cell
 *  (see lib/fileLocks). Filters on the SOURCE path. */
export function withoutRunPaths(entries: FileTransferEntry[]): FileTransferEntry[] {
    return entries.some(e => isRunPath(e.file.name))
        ? entries.filter(e => !isRunPath(e.file.name))
        : entries;
}

function splitExt(name: string): [string, string] {
    const dot = name.lastIndexOf('.');
    return dot > 0 ? [name.slice(0, dot), name.slice(dot)] : [name, ''];
}

/** Insert " (copy)" / " (copy N)" before the extension until unique. For a
 *  folder the whole segment is the base (no extension split). */
function uniquifySegment(segment: string, isFolder: boolean, taken: (s: string) => boolean): string {
    if (!taken(segment)) return segment;
    const [base, ext] = isFolder ? [segment, ''] : splitExt(segment);
    for (let n = 1; ; n++) {
        const suffix = n === 1 ? ' (copy)' : ` (copy ${n})`;
        const cand = `${base}${suffix}${ext}`;
        if (!taken(cand)) return cand;
    }
}

export interface RebaseResult {
    /** New VirtualFiles to merge into the target cell. */
    files: VirtualFile[];
    /** Absolute paths created (for selection / opening). */
    addedPaths: string[];
    /** True if a collision forced an auto-rename of the top segment. */
    renamed: boolean;
}

/** File operations use pinned bodies and conditional durable receipts. */
export function rebaseEntries(
    entries: FileTransferEntry[],
    targetFolder: string,
    existingNames: Set<string>,
): RebaseResult {
    if (entries.length === 0) return { files: [], addedPaths: [], renamed: false };
    // A folder transfer has at least one nested entry (even an empty folder
    // carries `<name>/.collkeep`); a single file's relPath has no slash.
    const isFolder = entries.some(e => e.relPath.includes('/'));
    const topSegment = entries[0].relPath.split('/')[0];

    const collides = (seg: string): boolean => {
        // The run folder at the cell root is never free: only a run writes
        // under `_runs/`, and a user folder of that name lands renamed.
        if (!targetFolder && seg === RUNS_PREFIX) return true;
        const dstRoot = joinPath(targetFolder, seg);
        // An existing file OR folder of that name collides, whether a file or
        // a folder is landing: the two cannot share a path.
        if (existingNames.has(dstRoot)) return true;
        const pfx = dstRoot + '/';
        for (const n of existingNames) if (n.startsWith(pfx)) return true;
        return false;
    };

    const finalTop = uniquifySegment(topSegment, isFolder, collides);
    const renamed = finalTop !== topSegment;

    const out: VirtualFile[] = [];
    const addedPaths: string[] = [];
    for (const { relPath, file } of entries) {
        const rest = relPath.slice(topSegment.length); // leading '/<...>' or ''
        const newName = joinPath(targetFolder, finalTop + rest);
        out.push({ ...file, name: newName });
        addedPaths.push(newName);
    }
    return { files: out, addedPaths, renamed };
}

/** Upsert `additions` into `base` by name: an addition replaces a same-named
 *  entry in place, the rest are appended. Additions rebased through
 *  rebaseEntries never collide (it conflict-renames); an OS drop replaces
 *  on purpose. */
export function mergeByName(base: VirtualFile[], additions: VirtualFile[]): VirtualFile[] {
    const out = [...base];
    // Name -> slot, built once: the first match in `base` owns the slot, a
    // later addition of the same name overwrites it in place.
    const at = new Map<string, number>();
    out.forEach((f, i) => { if (!at.has(f.name)) at.set(f.name, i); });
    for (const add of additions) {
        const idx = at.get(add.name);
        if (idx !== undefined) out[idx] = add;
        else { at.set(add.name, out.length); out.push(add); }
    }
    return out;
}

/** Block a same-cell move/paste that would corrupt the tree: into the node
 *  itself, into one of its own descendants, or a no-op into its current
 *  parent. (Cross-cell transfers are never self-referential, so callers only
 *  apply this when source and target are the SAME cell.) */
export function isInvalidSameCellTarget(nodePath: string, targetFolder: string): boolean {
    return (
        targetFolder === nodePath ||
        targetFolder.startsWith(nodePath + '/') ||
        targetFolder === parentOf(nodePath)
    );
}
