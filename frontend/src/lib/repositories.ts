// A cell's repositories as the cell head and the Files pane show them (D5):
// the branch chip's words and hover text, and which folders hold one. The
// state snapshot's `repositories` (lib/types CellRepository) is the only
// source; nothing here asks git. No dirty count: the cell's files are the
// work tree, and a run's `git status` is where edits show.

import type { CellRepository } from '@/lib/types';

/** The repository kept at a cell path (`''` = the cell root), if any. */
export type RepositoryAt = (path: string) => CellRepository | undefined;

const NONE: RepositoryAt = () => undefined;

/** A lookup over a cell's snapshot repositories. */
export function repositoryLookup(repositories: readonly CellRepository[] | undefined): RepositoryAt {
    if (!repositories?.length) return NONE;
    const byPath = new Map(repositories.map(repo => [repo.path, repo] as const));
    return path => byPath.get(path);
}

/** The folders that hold a repository, the cell root left out: the paths
 *  a file tree shows a row for even when no file lies under them. */
export const repositoryFolders = (repositories: readonly CellRepository[] | undefined): string[] =>
    (repositories ?? []).map(repo => repo.path).filter(path => path !== '');

/** Whether `path` is `folder` or lies under it (`''` holds everything). */
export const within = (path: string, folder: string): boolean =>
    folder === '' || path === folder || path.startsWith(`${folder}/`);

/** The repositories a delete or rename of `folder` takes along: the one at
 *  it and every one nested under it. */
export const repositoriesUnder = (repositories: readonly CellRepository[] | undefined, folder: string): CellRepository[] =>
    (repositories ?? []).filter(repo => within(repo.path, folder));

/** Whether the tip holds a row at the entry's path. A row always has a
 *  HEAD, so it names a branch or is detached; a rescue the tip holds no
 *  repository for has neither. */
export const keptOnTip = (repo: CellRepository): boolean => repo.branch !== null || repo.detached;

/** The chip's branch word: the branch, `(detached)`, or `git` for a rescue
 *  with no repository on the tip. */
export function branchLabel(repo: CellRepository): string {
    if (repo.detached) return '(detached)';
    return repo.branch ?? 'git';
}

/** What a rescue says on its chip and in its menu. */
export const rescueText = (reason: string): string => `history not saved: ${reason}`;

/** Where a repository sits, in words. */
export const repositoryPlace = (path: string): string => (path ? `at ${path}` : 'at the cell root');

/** The chip as one line (the Files panel's row tag): `main ↑2 ↓1`,
 *  `(detached)`, or a rescue's text. */
export function repositoryTag(repo: CellRepository): string {
    if (repo.error?.rescued) return rescueText(repo.error.reason);
    const counts = [repo.ahead ? `↑${repo.ahead}` : '', repo.behind ? `↓${repo.behind}` : ''].filter(Boolean);
    return [branchLabel(repo), ...counts].join(' ');
}

/** The chip's hover text: where the repository is, what its branch tracks
 *  and how far apart they are, or why git refused it. */
export function repositoryTitle(repo: CellRepository): string {
    const lines = [`Git repository ${repositoryPlace(repo.path)}`];
    if (repo.detached) lines.push('HEAD is detached');
    else if (repo.branch) lines.push(repo.upstream ? `${repo.branch} tracks ${repo.upstream}` : `${repo.branch} has no upstream`);
    if (repo.upstream && repo.ahead !== null && repo.behind !== null) lines.push(`${repo.ahead} ahead, ${repo.behind} behind`);
    if (repo.error) lines.push(repo.error.rescued ? rescueText(repo.error.reason) : repo.error.reason);
    return lines.join('\n');
}
