import { describe, expect, it } from 'vitest';
import contract from '../../../../sdk/shared/paths-contract.json';
import { isDotgitSegment, isRepoPath, isReservedCellPath, isReservedPath } from './cellPaths';
import { buildFileTree } from '@/lib/fileTree';
import { textFile } from '@/lib/fileCommands';

// The host (api/collimate_server/reserved_paths.py), the SDK (collimate.pointers) and this
// client read every row of the shared vector alike.
describe('reserved cell paths contract', () => {
    it.each(contract.paths)('$path is reserved: $reserved', ({ path, reserved }) => {
        expect(isReservedPath(path)).toBe(reserved);
        expect(isReservedCellPath(path)).toBe(reserved);
    });
    it('reserves a run folder for imports but not as a host path', () => {
        expect(isReservedCellPath('_runs/run-1234abcd/x.chat')).toBe(true);
        expect(isReservedPath('_runs/run-1234abcd/x.chat')).toBe(false);
    });
    it('matches every spelling of .git and nothing that only resembles it', () => {
        for (const segment of ['.git', '.GIT', '.Git.', ' .git ', 'GIT~1', '.git::$DATA', '\u200c.git', '.gi\ufefft'])
            expect(isDotgitSegment(segment)).toBe(true);
        for (const segment of ['.gitignore', '.github', 'git~2', 'git', '.git~1'])
            expect(isDotgitSegment(segment)).toBe(false);
    });
    it.each(contract.paths)('the file tree shows $path only when it is no reserved path', ({ path, reserved }) => {
        const tree = buildFileTree([textFile(path, '')]);
        expect(Object.keys(tree).length === 0).toBe(reserved);
    });
    it.each(contract.paths.filter(row => row.reserved))('$path never names a repository', ({ path }) => {
        expect(isRepoPath(path)).toBe(false);
    });
});
