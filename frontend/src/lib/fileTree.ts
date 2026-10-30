import {textFile} from './fileCommands';
import { VirtualFile } from '@/lib/types';

/** Hidden per-folder marker file. Created folders (incl. empty ones) are
 *  kept alive by a `<folderpath>/.collkeep` file so they ride the entire
 *  files-only persistence pipeline (drafts, version chain, run hydration —
 *  an empty dir materializes on disk at run time). The marker is hidden
 *  ONLY at the frontend display layer (suppressed in buildFileTree); it must
 *  never be added to the backend `is_reserved_path`, or it couldn't carry
 *  the empty directory to disk. Namespaced (`.collkeep`, not `.keep`) so it
 *  can't collide with a user's real `.keep`/`.gitkeep`. */
export const FOLDER_KEEP_FILE = '.collkeep';

/** True if `name` is a folder-keep marker (bare or `<folder>/.collkeep`). */
export const isFolderKeep = (name: string): boolean =>
    name === FOLDER_KEEP_FILE || name.endsWith('/' + FOLDER_KEEP_FILE);

/** Drop a `.collkeep` marker once its folder holds a real (non-marker) file —
 *  the folder is then kept alive by real content and the marker is redundant
 *  clutter (it would otherwise leave a stray dotfile on disk after a run).
 *  Pure + idempotent; run on every file-set mutation. */
export function pruneRedundantMarkers(files: VirtualFile[]): VirtualFile[] {
    return files.filter((f) => {
        if (!isFolderKeep(f.name)) return true;
        const slash = f.name.lastIndexOf('/');
        if (slash < 0) return true; // a bare `.collkeep` at root — leave it
        const prefix = f.name.slice(0, slash + 1); // '<folder>/'
        const hasReal = files.some(
            (o) => o !== f && !isFolderKeep(o.name) && o.name.startsWith(prefix),
        );
        return !hasReal;
    });
}

/** After deleting/moving a file out, re-mint `<parent>/.collkeep` if the parent
 *  folder is now empty of real files — so a folder the user created doesn't
 *  silently vanish when its last file leaves. No-op at the root, when the
 *  folder still has real files, or when a marker already exists. */
export function remintEmptiedParent(files: VirtualFile[], removedPath: string): VirtualFile[] {
    const slash = removedPath.lastIndexOf('/');
    if (slash < 0) return files; // file was at root — no folder to keep
    const parent = removedPath.slice(0, slash);
    const prefix = parent + '/';
    // Any remaining descendant — real file OR a deeper subfolder's own marker —
    // already materializes this folder on disk, so a marker here would be a
    // redundant stray. Only re-mint when the folder is genuinely empty.
    const hasDescendant = files.some((f) => f.name.startsWith(prefix));
    if (hasDescendant) return files;
    const markerName = `${parent}/${FOLDER_KEEP_FILE}`;
    if (files.some((f) => f.name === markerName)) return files;
    return [
        ...files,
        textFile(markerName,''),
    ];
}

/** Effective cell files include optimistic draft writes and deletions.
 *  Backend selection and validation decide whether the declaration is usable. */
export function hasOptionsStack(cell: { files: VirtualFile[] }): boolean {
    return cell.files.some(file => file.name.endsWith('.options_stack'));
}

export interface TreeNode {
    name: string;
    path: string;
    type: 'file' | 'folder';
    children: Record<string, TreeNode>;
    fileIndex?: number;
    fileData?: VirtualFile;
    /** Set on virtual nodes synthesised for files / folders that exist
     *  in the diff target but not in the current cell. Deleted nodes
     *  render grayed-out with a "D" badge and are non-interactive
     *  (no rename / delete / open; no hover affordances). */
    isDeleted?: boolean;
}

export const buildFileTree = (files: VirtualFile[]) => {
    const root: Record<string, TreeNode> = {};

    files.forEach((file, index) => {
        const safeName = file.name.replace(/\\/g, '/');
        const parts = safeName.split('/');
        // A folder-keep marker exists only to materialize its (possibly empty)
        // folder: walk its ancestor segments so the folder node is created,
        // but never emit the marker's own leaf file row.
        const isKeep = parts[parts.length - 1] === FOLDER_KEEP_FILE;
        let currentLevel = root;

        parts.forEach((part, i) => {
            const isFile = i === parts.length - 1;
            const path = parts.slice(0, i + 1).join('/');

            if (isFile && isKeep) return;

            if (!currentLevel[part]) {
                currentLevel[part] = {
                    name: part,
                    path,
                    type: isFile ? 'file' : 'folder',
                    children: {},
                    ...(isFile ? { fileIndex: index, fileData: file } : {})
                };
            }

            if (!isFile) {
                currentLevel = currentLevel[part].children;
            }
        });
    });

    return root;
};

/** Inject virtual "deleted" nodes for every file that exists in
 *  `diffTargetFiles` but NOT in `currentFiles`. Walks the existing
 *  tree to place them in their correct folder; if the containing
 *  folder doesn't exist in the current tree either (folder-level
 *  delete), a virtual deleted folder is synthesised too.
 *
 *  Mutates the passed tree in place for simplicity — callers build
 *  the tree via useMemo so a fresh object is produced each render
 *  anyway. Returns the same root ref. */
export const augmentTreeWithDeleted = (
    tree: Record<string, TreeNode>,
    diffTargetFiles: Record<string, string>,
    currentFileNames: Set<string>,
): Record<string, TreeNode> => {
    for (const filename of Object.keys(diffTargetFiles)) {
        if (currentFileNames.has(filename)) continue;
        const safeName = filename.replace(/\\/g, '/');
        const parts = safeName.split('/');
        // Mirror buildFileTree's marker suppression: a deleted empty folder in
        // the diff target must show as a deleted folder, never a phantom
        // deleted `.collkeep` file row.
        const isKeep = parts[parts.length - 1] === FOLDER_KEEP_FILE;
        let currentLevel = tree;
        parts.forEach((part, i) => {
            const isFile = i === parts.length - 1;
            const path = parts.slice(0, i + 1).join('/');
            if (isFile && isKeep) return;
            if (!currentLevel[part]) {
                // Folder or file that's missing entirely from the current tree
                // — mark it as deleted so the UI greys it out.
                currentLevel[part] = {
                    name: part,
                    path,
                    type: isFile ? 'file' : 'folder',
                    children: {},
                    isDeleted: true,
                };
            }
            // An existing node with this name (even a same-named folder) is left alone.
            if (!isFile) currentLevel = currentLevel[part].children;
        });
    }
    return tree;
};

export type FileDiffStatus = 'added' | 'modified' | 'deleted';

/** A file node's status against the diff target: deleted (a virtual node for
 *  a file only in the target), added (only in the current cell) or modified
 *  (in both, with different content). Null for folders and unchanged files. */
export function fileDiffStatus(node: TreeNode, diffTargetFiles?: Record<string, string>): FileDiffStatus | null {
    if (node.type !== 'file') return null;
    if (node.isDeleted) return 'deleted';
    if (diffTargetFiles && node.fileData) {
        const t = diffTargetFiles[node.fileData.name];
        if (t === undefined) return 'added';
        if ('content' in node.fileData && t !== node.fileData.content) return 'modified';
    }
    return null;
}

/** Cheap line-diff stats for the file-tree badge. Uses multiset counts
 *  so duplicate lines are handled sensibly — a file going from
 *  `[a,a,b]` to `[a,b]` reports `-1/+0`, not `-2/+1`. Not a full LCS
 *  (tree badge doesn't need hunk-level precision), but the numbers
 *  always add to the same answer as the in-renderer diff does. */
export function computeLineStats(before: string, after: string): { added: number; removed: number } {
    const b = new Map<string, number>();
    const a = new Map<string, number>();
    for (const line of before.split('\n')) b.set(line, (b.get(line) ?? 0) + 1);
    for (const line of after.split('\n')) a.set(line, (a.get(line) ?? 0) + 1);
    let added = 0, removed = 0;
    for (const [line, count] of a) added += Math.max(0, count - (b.get(line) ?? 0));
    for (const [line, count] of b) removed += Math.max(0, count - (a.get(line) ?? 0));
    return { added, removed };
}
