import { describe, expect, it } from 'vitest';
import type { CellRepository } from '@/lib/types';
import {
    branchLabel, keptOnTip, repositoriesUnder, repositoryFolders, repositoryLookup, repositoryTag, repositoryTitle, within,
} from './repositories';

const repo = (path: string, patch: Partial<CellRepository> = {}): CellRepository =>
    ({ path, branch: 'main', upstream: 'origin/main', ahead: 0, behind: 0, detached: false, ...patch });

// The chip's words come from the snapshot alone (D5): a branch, its counts
// against the upstream, a detached HEAD, git's refusal or a rescue.
describe('a repository chip in words', () => {
    it('names the branch and the counts above zero', () => {
        expect(repositoryTag(repo('', { ahead: 2, behind: 1 }))).toBe('main ↑2 ↓1');
        expect(repositoryTag(repo('', { ahead: 0, behind: 3 }))).toBe('main ↓3');
        expect(repositoryTag(repo('', { ahead: null, behind: null }))).toBe('main');
    });

    it('a detached HEAD reads (detached); a rescue with no row reads git', () => {
        expect(branchLabel(repo('', { branch: null, detached: true }))).toBe('(detached)');
        expect(branchLabel(repo('a', { branch: null, upstream: null }))).toBe('git');
    });

    it('a rescue says its history was not saved, with why', () => {
        expect(repositoryTag(repo('a', { error: { reason: 'cell was removed', rescued: true } }))).toBe('history not saved: cell was removed');
    });

    it('the hover names the place, the upstream, the counts and the refusal', () => {
        expect(repositoryTitle(repo('', { ahead: 2, behind: 1 }))).toBe('Git repository at the cell root\nmain tracks origin/main\n2 ahead, 1 behind');
        expect(repositoryTitle(repo('vendor/lib', { upstream: null, ahead: null, behind: null }))).toBe('Git repository at vendor/lib\nmain has no upstream');
        expect(repositoryTitle(repo('a', { branch: null, detached: true, upstream: null }))).toBe('Git repository at a\nHEAD is detached');
        expect(repositoryTitle(repo('a', { error: { reason: 'repository history incomplete in the channel store: a', rescued: false } })))
            .toContain('repository history incomplete in the channel store: a');
    });
});

describe('a cell’s repositories by path', () => {
    const repositories = [repo(''), repo('app'), repo('app/vendor/lib'), repo('docs')];

    it('looks a repository up by its folder', () => {
        const at = repositoryLookup(repositories);
        expect(at('app')?.path).toBe('app');
        expect(at('')?.path).toBe('');
        expect(at('nope')).toBeUndefined();
        expect(repositoryLookup(undefined)('')).toBeUndefined();
    });

    it('a folder takes the repositories at and under it; the root takes all', () => {
        expect(repositoriesUnder(repositories, 'app').map(r => r.path)).toEqual(['app', 'app/vendor/lib']);
        expect(repositoriesUnder(repositories, 'ap')).toEqual([]);
        expect(repositoriesUnder(repositories, '')).toHaveLength(4);
        expect(within('app/x', 'app')).toBe(true);
        expect(within('apple', 'app')).toBe(false);
    });

    it('the folders that get rows leave the root out', () => {
        expect(repositoryFolders(repositories)).toEqual(['app', 'app/vendor/lib', 'docs']);
    });

    it('a row on the tip has a branch or a detached HEAD', () => {
        expect(keptOnTip(repo(''))).toBe(true);
        expect(keptOnTip(repo('', { branch: null, detached: true }))).toBe(true);
        expect(keptOnTip(repo('', { branch: null, detached: false }))).toBe(false);
    });
});
