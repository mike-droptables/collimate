// The cell paths an import never writes: a run's `_runs/` band, the
// workspace staging band `.collrun` and a repository's `.git` under every
// spelling git treats as one (api/collimate_server/reserved_paths.py). The import commit
// refuses any of them with a 400, so every client path leaves them out
// before anything is uploaded. sdk/shared/paths-contract.json pins the rule.
// A `.git` itself is split off first (repositoryDirOf): its tree uploads as
// the repository the new cell adopts, never as files.

import { isRunPath, normalizeRunPath } from '@/lib/runningCell';

/** The staging band's root. Mirrors api/collimate_server/reserved_paths.py RESERVED_PREFIX. */
const STAGING_PREFIX = '.collrun';

/** Code points git ignores when comparing a name to `.git`: the zero-width
 *  joiners and marks, the directional embeddings and overrides, the
 *  deprecated format controls and the byte-order mark. */
const IGNORABLE = /[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]/g;

/** The whitespace Python's `str.strip()` removes at either end
 *  (str.isspace): `String.trim()` misses U+001C-001F and U+0085. */
// eslint-disable-next-line no-control-regex -- \x1c-\x1f are whitespace to Python
const SURROUNDING_SPACE = /^[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g;

/** Whether one path segment names a repository's `.git` on some filesystem:
 *  casefolded, without ignorable code points, an NTFS `:` stream suffix,
 *  surrounding whitespace or trailing dots, it reads `.git` or the short
 *  name `git~1`. Mirrors collimate.pointers.is_dotgit_segment. */
export function isDotgitSegment(segment: string): boolean {
    const name = segment.toLowerCase().replace(IGNORABLE, '').split(':')[0].replace(SURROUNDING_SPACE, '').replace(/[. ]+$/, '');
    return name === '.git' || name === 'git~1';
}

/** Whether the host reserves `path`: a `.git`-equivalent segment anywhere,
 *  or the `.collrun` band. Mirrors collimate.pointers.is_reserved_path. */
export function isReservedPath(path: string): boolean {
    const normalized = normalizeRunPath(path);
    if (normalized === STAGING_PREFIX || normalized.startsWith(`${STAGING_PREFIX}/`)) return true;
    return normalized.split('/').some(isDotgitSegment);
}

/** Whether `path`, relative to a cell, lies in the `_runs/` or `.collrun`
 *  band (the band's own folder counts) or under a `.git`. */
export function isReservedCellPath(path: string): boolean {
    return isRunPath(path) || isReservedPath(path);
}

/** The segments no repository path holds, lower-cased: the two bands and
 *  the workspace skip dirs (collimate.fs.WORKSPACE_SKIP_DIRS). Mirrors
 *  api/collimate_server/reserved_paths.py _REPO_FORBIDDEN. */
const REPO_FORBIDDEN = new Set([STAGING_PREFIX, '_runs', '.git', 'node_modules', '.venv', '__pycache__']);

/** Whether `path` may name a repository of a cell: `''` (the cell root) or
 *  a relative folder with no empty, `.`, `..`, `.git`-equivalent, band or
 *  skip-dir segment. Mirrors api/collimate_server/reserved_paths.py is_repo_path. */
export function isRepoPath(path: string): boolean {
    if (path === '') return true;
    if (path.includes('\\')) return false;
    return path.split('/').every(segment => segment !== '' && segment !== '.' && segment !== '..'
        && !REPO_FORBIDDEN.has(segment.toLowerCase()) && !isDotgitSegment(segment));
}

/** The folder of the repository whose `.git` holds `path`: what comes
 *  before its first segment named exactly `.git` (what `git init` makes,
 *  and all the host's discovery adopts), or null outside any. */
export function repositoryDirOf(path: string): string | null {
    const parts = path.split('/');
    const at = parts.indexOf('.git');
    return at < 0 ? null : parts.slice(0, at).join('/');
}
