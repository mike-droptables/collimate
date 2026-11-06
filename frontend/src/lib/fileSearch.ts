// Which files a query keeps and in what order. Pure — the Files panel
// (components/cellPanel/FilesPanel) lists what it keeps; the tree and the
// folder view order their rows with treeNodeOrder.
//
// Query rules:
//   - empty (or blank) → every file;
//   - with `*` or `?` → a case-insensitive glob. Without a `/` it matches
//     the BASENAME (`*.md` is the markdown files anywhere); with a `/` it
//     matches the whole cell-relative path from the root (`src/*.ts`).
//     `*` stays inside one segment, `**` spans segments (`**/` also
//     matches no folder at all), `?` is one character of a segment;
//   - anything else → a case-insensitive substring of the full path.
// "Runs" keeps only the files a live run locks (lib/fileLocks). "Recent"
// orders files newest first by their timestamp, ties by path.

import type { TreeNode } from '@/lib/fileTree';

const GLOB_CHARS = /[*?]/;
const REGEX_SPECIALS = /[.+^${}()|[\]\\]/g;

const normalize = (path: string): string => path.replace(/\\/g, '/');
const basename = (path: string): string => path.slice(path.lastIndexOf('/') + 1);

/** True when the query is a glob (holds `*` or `?`). */
export function isGlobQuery(query: string): boolean {
    return GLOB_CHARS.test(query);
}

/** A glob as an anchored, case-insensitive RegExp. `**` spans segments and
 *  `**` followed by `/` also matches no folder at all; `*` stays inside one
 *  segment; `?` is one character of a segment; everything else is literal. */
export function globToRegExp(glob: string): RegExp {
    let re = '';
    for (let i = 0; i < glob.length; i++) {
        const c = glob[i];
        if (c === '*') {
            if (glob[i + 1] === '*') {
                while (glob[i + 1] === '*') i++;
                if (glob[i + 1] === '/') {
                    i++;
                    re += '(?:.*/)?';
                } else {
                    re += '.*';
                }
            } else {
                re += '[^/]*';
            }
        } else if (c === '?') {
            re += '[^/]';
        } else {
            re += c.replace(REGEX_SPECIALS, '\\$&');
        }
    }
    return new RegExp(`^${re}$`, 'i');
}

/** A query compiled once into a path predicate (see the rules above). */
export function compileQuery(query: string): (path: string) => boolean {
    const q = normalize(query.trim());
    if (!q) return () => true;
    if (isGlobQuery(q)) {
        const pattern = q.replace(/^\/+/, '');
        const re = globToRegExp(pattern);
        return pattern.includes('/')
            ? (path) => re.test(normalize(path))
            : (path) => re.test(basename(normalize(path)));
    }
    const needle = q.toLowerCase();
    return (path) => normalize(path).toLowerCase().includes(needle);
}

/** A timestamp as ms; missing or unparseable sorts oldest. */
function recencyOf(timestamp: string | undefined): number {
    const t = timestamp ? Date.parse(timestamp) : NaN;
    return Number.isNaN(t) ? -Infinity : t;
}

const byPath = (a: string, b: string): number => a.localeCompare(b);

/** Newest first; equal (or both unknown) times fall back to the path. */
export function compareRecency(
    a: { path: string; timestamp?: string },
    b: { path: string; timestamp?: string },
): number {
    const ta = recencyOf(a.timestamp);
    const tb = recencyOf(b.timestamp);
    if (ta !== tb) return tb > ta ? 1 : -1;
    return byPath(a.path, b.path);
}

/** The order of one folder's rows: folders first, then files, each by
 *  name. */
export function treeNodeOrder(a: TreeNode, b: TreeNode): number {
    if (a.type !== b.type) return a.type === 'folder' ? -1 : 1;
    return a.name.localeCompare(b.name);
}
