// The repositories an import brings. A `.git` in a picked or dropped tree
// is never a cell file: it is split off before the reserved-path rule
// (cellPaths isReservedCellPath) would leave it out, zipped as it is and
// uploaded (POST /api/channels/{channel}/import/repository), and the import commit names it
// by its token in the cell that adopts it. A zip the server expands sets
// its `.git`s aside the same way (ZipExpansion.repositories).

import { uploadRepository, type UploadOptions } from '@/lib/api/transfer';
import { repositoryDirOf } from './cellPaths';
import type { LeftOut } from './splitLevels';
import { zipMembers } from './zipWriter';

/** Why a `.git` FILE is left out: a submodule checkout's or a linked
 *  worktree's pointer to a gitdir elsewhere (the words a zip's gets). */
export const GITFILE_REASON = 'submodule/worktree gitfile (not supported)';

/** The folders git needs in a gitdir (it is no repository without `refs/`
 *  and `objects/`) and those an export writes, which a picker that reports
 *  no empty folder (`<input webkitdirectory>`) drops when the refs are
 *  packed and the objects too. */
const GITDIR_FOLDERS = ['refs/heads', 'refs/tags', 'objects/info', 'objects/pack'];

/** A repository found in an import's tree: the folder it sits in (relative
 *  to the tree's root, `''` for the root) and its `.git`'s files and empty
 *  folders at their paths in the tree. */
export interface FoundRepository<F> {
    dir: string;
    files: F[];
    dirs: string[];
}

/** An import tree without its repositories, the repositories, and the
 *  gitfiles left out. */
export interface RepositorySplit<F> {
    files: F[];
    dirs: string[];
    repositories: FoundRepository<F>[];
    gitfiles: LeftOut[];
}

/** Where the `.git` of a repository at `dir` sits in the tree. */
const gitdirOf = (dir: string) => (dir ? `${dir}/.git` : '.git');

/** Split each exact `.git` (and everything under it) out of a tree's
 *  files and empty folders, by the folder it sits in. A `.git` inside
 *  another's (a submodule's gitdir under `modules/`) stays part of the
 *  outer one; another spelling of `.git` is no repository and stays for the
 *  reserved-path rule to leave out. A `.git` that is a file (a gitfile) is
 *  no repository either: it is left out with its reason, the files beside
 *  it still import. */
export function splitRepositories<F extends { path: string }>(files: readonly F[], dirs: readonly string[] = []): RepositorySplit<F> {
    const found = new Map<string, FoundRepository<F>>();
    const repository = (dir: string) => {
        let repo = found.get(dir);
        if (!repo) { repo = { dir, files: [], dirs: [] }; found.set(dir, repo); }
        return repo;
    };
    const kept: F[] = [];
    const gitfiles: LeftOut[] = [];
    for (const file of files) {
        const dir = repositoryDirOf(file.path);
        if (dir === null) kept.push(file);
        else if (file.path === gitdirOf(dir)) gitfiles.push({ path: file.path, reason: GITFILE_REASON });
        else repository(dir).files.push(file);
    }
    const keptDirs: string[] = [];
    for (const path of dirs) {
        const dir = repositoryDirOf(path);
        if (dir === null) keptDirs.push(path); else repository(dir).dirs.push(path);
    }
    return { files: kept, dirs: keptDirs, repositories: [...found.values()], gitfiles };
}

/** Zip one repository's `.git` (members under `<dir>/.git/`, its empty
 *  folders kept and git's own always there) and upload it; the token the
 *  import commit adopts it by. */
export async function uploadFoundRepository(channel: string, repo: FoundRepository<{ path: string; file: Blob }>, options: UploadOptions = {}): Promise<string> {
    const gitdir = gitdirOf(repo.dir);
    const dirs = [...new Set([...repo.dirs, ...GITDIR_FOLDERS.map(folder => `${gitdir}/${folder}`)])];
    const zip = await zipMembers(repo.files.map(f => ({ path: f.path, body: f.file })), dirs, options.signal);
    return (await uploadRepository(channel, zip, options)).token;
}
