import { describe, expect, it } from 'vitest';
import { isRepoPath, repositoryDirOf } from './cellPaths';

// The repository-path rule mirrors api/collimate_server/reserved_paths.py is_repo_path, so an
// import never names a repository the commit refuses with a 400.
describe('repository paths', () => {
    it.each(['', 'app', 'a/b/c', 'Vendor.lib'])('%j may hold a repository', path => {
        expect(isRepoPath(path)).toBe(true);
    });

    it.each(['a//b', './a', 'a/..', 'a\\b', '_runs/x', 'x/_RUNS', '.collrun', 'a/node_modules/b', '.venv', '__pycache__/c', '.git', 'a/.GIT', 'git~1', 'a/.git.'])('%j may not', path => {
        expect(isRepoPath(path)).toBe(false);
    });

    it('a path inside an exact .git names the folder of its repository', () => {
        expect(repositoryDirOf('.git/HEAD')).toBe('');
        expect(repositoryDirOf('proj/.git/objects/pack/p.pack')).toBe('proj');
        expect(repositoryDirOf('proj/.git/modules/sub/.git/HEAD')).toBe('proj');
        expect(repositoryDirOf('proj/.GIT/HEAD')).toBeNull();
        expect(repositoryDirOf('proj/.gitignore')).toBeNull();
    });
});
