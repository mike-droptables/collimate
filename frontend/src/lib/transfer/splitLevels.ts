// Where an imported tree splits into stacks and cells. Pure: a list of paths
// relative to the import's root (and its empty folders) in, the levels that
// make sense out, each with the stacks and cells it would create.
//
//   0  one cell holding the whole tree
//   1  each top folder a cell, in one new stack named after the root
//   2  each top folder a stack, its child folders that stack's cells
//   3  channel / stack / cell: the top folders are channels (an "all"
//      export's zip), their children stacks and the next folders cells
//
// A level needs at least one folder at its depth; one without is not
// offered. Files that sit above the split level (loose files) become one
// cell named after the folder that holds them, so nothing is dropped.
// Stacks and cells are told apart by their whole folder path, never by
// name alone: two channel folders holding a stack and cell of the same
// names make two cells. A file or folder whose path inside its cell is
// reserved (`_runs/`, `.collrun`) is left out of the level and listed.
//
// The tree's repositories (the folders whose `.git` was split off,
// lib/transfer/repositoryImport) go with the cell their folder lands in,
// keyed by their folder inside it (`repo_path`). A repository whose work
// tree the level spreads over several cells (its folder sits above the
// split level) is left out as "repository spans cells", its files still
// imported; so is one whose folder cannot hold a repository.

import { isRepoPath, isReservedCellPath } from './cellPaths';

export type SplitLevelId = 0 | 1 | 2 | 3;

export interface SplitFile {
    /** The file's path relative to the import root. */
    source: string;
    /** Its path inside the cell it lands in. */
    path: string;
}

/** A repository a cell adopts. */
export interface SplitRepository {
    /** Its folder relative to the import root. */
    source: string;
    /** Its folder inside the cell (`''` = the cell root). */
    repo_path: string;
}

export interface SplitCell {
    name: string;
    files: SplitFile[];
    /** Empty folders, relative to the cell. */
    dirs: string[];
    repositories: SplitRepository[];
}

/** What a level leaves out, by its path relative to the import root. */
export interface LeftOut {
    path: string;
    reason: string;
}

/** Why a reserved file or folder is left out. */
export const RESERVED_REASON = 'Reserved for runs';
/** Why a repository whose work tree several cells share is left out. */
export const SPANS_REASON = 'repository spans cells';

export interface SplitStack {
    name: string;
    cells: SplitCell[];
}

export interface SplitPreview {
    stacks: { name: string; cells: { name: string; fileCount: number }[] }[];
}

export interface SplitLevel {
    level: SplitLevelId;
    label: string;
    stacks: SplitStack[];
    preview: SplitPreview;
    /** What this level leaves out: files and empty folders whose path
     *  inside the cell is reserved, and repositories (`<dir>/.git`) it
     *  cannot give one cell. */
    leftOut: LeftOut[];
}

export const SPLIT_LEVEL_LABELS: Record<SplitLevelId, string> = {
    0: 'One cell',
    1: 'Each top folder a cell',
    2: 'Top folders are stacks',
    3: 'Channel / stack / cell',
};

const segments = (path: string) => path.split('/').filter(Boolean);

/** Whether some folder sits at `depth` (1 = a top folder): a file at least
 *  `depth` folders deep, or an empty folder at least that deep. */
function hasFolderAt(depth: number, files: readonly string[][], dirs: readonly string[][]): boolean {
    return files.some(parts => parts.length > depth) || dirs.some(parts => parts.length >= depth);
}

/** An ordered map from key to its group, so stacks and cells keep the order
 *  their first file had. */
function group<T>(map: Map<string, T>, key: string, make: () => T): T {
    let value = map.get(key);
    if (!value) { value = make(); map.set(key, value); }
    return value;
}

/** Split at `level`: the first `level` path segments name the stack and the
 *  cell; the rest is the path inside the cell. */
function split(level: SplitLevelId, rootName: string, files: readonly string[][], dirs: readonly string[][], repositories: readonly string[][]): { stacks: SplitStack[]; leftOut: LeftOut[] } {
    const stacks = new Map<string, { name: string; cells: Map<string, SplitCell> }>();
    const leftOut: LeftOut[] = [];
    /** The stack and cell names for a path's leading folders `head`
     *  (at most `level` of them: fewer means a loose file or folder). */
    const place = (head: string[]): [string, string] => {
        if (level === 0) return [rootName, rootName];
        if (level === 1) return [rootName, head[0] ?? rootName];
        if (level === 2) return [head[0] ?? rootName, head[1] ?? head[0] ?? rootName];
        // Level 3: the channel folder is no stack of its own.
        return [head[1] ?? head[0] ?? rootName, head[2] ?? head[1] ?? head[0] ?? rootName];
    };
    const cellOf = (head: string[]) => {
        const [stackName, cellName] = place(head);
        // Keyed by folder path: the stack by the folders above its cell
        // (level 3's channel folder included), the cell by its own. Loose
        // files are a cell of their own even where a folder of the same name
        // is one: key them apart.
        const loose = head.length < level;
        const stackKey = head.slice(0, Math.max(0, level - 1)).join('/');
        const stack = group(stacks, stackKey, () => ({ name: stackName, cells: new Map() }));
        return group(stack.cells, `${loose ? 'loose' : 'folder'}:${head.join('/')}`, () => ({ name: cellName, files: [], dirs: [], repositories: [] }));
    };
    for (const parts of files) {
        const head = parts.slice(0, Math.min(level, parts.length - 1));
        const path = parts.slice(head.length).join('/');
        if (isReservedCellPath(path)) { leftOut.push({ path: parts.join('/'), reason: RESERVED_REASON }); continue; }
        cellOf(head).files.push({ source: parts.join('/'), path });
    }
    for (const parts of dirs) {
        if (parts.length <= level) {
            // A folder exactly at the split depth is an empty cell; one above
            // it (an empty stack or channel folder) holds no cell.
            if (parts.length === level) cellOf(parts);
            continue;
        }
        const head = parts.slice(0, level);
        const path = parts.slice(level).join('/');
        if (isReservedCellPath(path)) { leftOut.push({ path: parts.join('/'), reason: RESERVED_REASON }); continue; }
        cellOf(head).dirs.push(path);
    }
    for (const parts of repositories) {
        const source = parts.join('/');
        const gitdir = source ? `${source}/.git` : '.git';
        // A folder at or below the split depth lands whole in its cell. One
        // above it does only while it holds no folder: its files are then
        // all its own loose cell's.
        const under = (other: readonly string[]) => other.length > parts.length && parts.every((part, i) => other[i] === part);
        const nested = files.some(other => other.length > parts.length + 1 && under(other)) || dirs.some(under);
        if (parts.length < level && nested) { leftOut.push({ path: gitdir, reason: SPANS_REASON }); continue; }
        const head = parts.slice(0, level);
        const repoPath = parts.slice(head.length).join('/');
        if (!isRepoPath(repoPath)) { leftOut.push({ path: gitdir, reason: `A repository cannot live at ${repoPath}` }); continue; }
        cellOf(head).repositories.push({ source, repo_path: repoPath });
    }
    return { stacks: [...stacks.values()].map(stack => ({ name: stack.name, cells: [...stack.cells.values()] })), leftOut };
}

export function previewOf(stacks: readonly SplitStack[]): SplitPreview {
    return { stacks: stacks.map(stack => ({ name: stack.name, cells: stack.cells.map(cell => ({ name: cell.name, fileCount: cell.files.length })) })) };
}

/** The levels a tree can split at, shallowest first. `paths` are its files
 *  relative to the root, `dirs` its empty folders and `repositories` the
 *  folders whose `.git` was split off (`''` the root); `rootName` names the
 *  stack and cell that the root itself (and loose files there) become. */
export function splitLevels(paths: readonly string[], dirs: readonly string[], rootName: string, repositories: readonly string[] = []): SplitLevel[] {
    const files = paths.map(segments).filter(parts => parts.length > 0);
    const folders = dirs.map(segments).filter(parts => parts.length > 0);
    const repos = repositories.map(segments);
    if (files.length === 0 && folders.length === 0 && repos.length === 0) return [];
    const levels: SplitLevel[] = [];
    for (const level of [0, 1, 2, 3] as const) {
        if (level > 0 && !hasFolderAt(level, files, folders)) continue;
        const { stacks, leftOut } = split(level, rootName, files, folders, repos);
        levels.push({ level, label: SPLIT_LEVEL_LABELS[level], stacks, preview: previewOf(stacks), leftOut });
    }
    return levels;
}
