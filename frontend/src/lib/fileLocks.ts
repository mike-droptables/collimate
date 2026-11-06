// Per-file locks in the Files pane. A live run's files — everything under
// its `_runs/<label>/` folder — are its lock: the user can read and edit
// them (their renderers are the run's interface) but never delete, rename,
// move or copy them out of the cell. Only a run writes under `_runs/`, so
// the user never creates, drops or pastes anything there either. The
// server enforces the same rules; these helpers keep the UI from offering
// what it would refuse.

import type { RunRecord } from '@/lib/types';
import type { TreeNode } from '@/lib/fileTree';
import { isRunPath, RUNS_PREFIX } from '@/lib/runningCell';
import { runDisplayName } from '@/lib/runLabels';

/** The live run whose folder holds `path`, else null — lib/runningCell
 *  lockOwner bound to one cell. */
export type LockOwnerOf = (path: string) => RunRecord | null;

/** The lock over a node: for a file its owner; for a folder the owner of
 *  the first locked file under it (a folder holding a locked file cannot
 *  be deleted, renamed or moved either). `paths` are the cell's file
 *  paths. */
export function lockOwnerUnder(
    paths: Iterable<string>,
    nodePath: string,
    isFolder: boolean,
    lockOwnerOf: LockOwnerOf | undefined,
): RunRecord | null {
    if (!lockOwnerOf || !isRunPath(nodePath)) return null;
    if (!isFolder) return lockOwnerOf(nodePath);
    const prefix = `${nodePath}/`;
    for (const p of paths) {
        if (!p.startsWith(prefix)) continue;
        const owner = lockOwnerOf(p);
        if (owner) return owner;
    }
    return null;
}

/** The run locking a tree node: a file's owner, or the owner of the first
 *  locked file anywhere under a folder. */
export function nodeLockOwner(node: TreeNode, lockOwnerOf: LockOwnerOf | undefined): RunRecord | null {
    if (!lockOwnerOf || node.isDeleted || !isRunPath(node.path)) return null;
    if (node.type === 'file') return lockOwnerOf(node.fileData?.name ?? node.path);
    for (const child of Object.values(node.children)) {
        const owner = nodeLockOwner(child, lockOwnerOf);
        if (owner) return owner;
    }
    return null;
}

/** The tooltip a locked file or folder carries. */
export function lockTitle(owner: RunRecord): string {
    return `Locked by ${runDisplayName(owner)} — it is read-only; duplicate an allowed snapshot outside its run folder`;
}

/** The user never writes a NEW path under `_runs/` — a folder target is
 *  moved out of it: `_runs/<label>/a/b` → `a/b`, `_runs/<label>` and
 *  `_runs` → the cell root. Anything else is returned unchanged. An
 *  in-cell Copy / Duplicate of a run's file lands at the rebased folder. */
export function folderOutsideRuns(folder: string): string {
    if (!isRunPath(folder)) return folder;
    const rest = folder.slice(RUNS_PREFIX.length + 1); // '<label>/a/b', '<label>' or ''
    const slash = rest.indexOf('/');
    return slash >= 0 ? rest.slice(slash + 1) : '';
}

/** Reason a user write at `path` (a new file, folder or drop target) is
 *  refused, else null. */
export function runPathRefusal(path: string): string | null {
    return isRunPath(path)
        ? `Files under ${RUNS_PREFIX}/ belong to runs; create it elsewhere.`
        : null;
}
