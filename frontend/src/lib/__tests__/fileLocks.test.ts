import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect } from 'vitest';
import { folderOutsideRuns, lockOwnerUnder, lockTitle, runPathRefusal } from '@/lib/fileLocks';
import { withoutRunPaths } from '@/lib/fileTransfer';
import { runDisplayName, runShortId } from '@/lib/runLabels';
import type { RunRecord, VirtualFile } from '@/lib/types';

const RUN: RunRecord = {
    runId: 'run-1', cellId: 'cell-1', channel: 'ch', runnerId: 'r', runnerName: 'Runner R',
    generatorName: 'g', label: 'r-11111111', folder: '_runs/r-11111111',
};
const lockOwnerOf = (path: string) => (path.startsWith(`${RUN.folder}/`) ? RUN : null);
const vf = (name: string): VirtualFile => (fileFixture(name, '', {language: 'plaintext', timestamp: 't'}));

describe('fileLocks', () => {
    it('lockOwnerUnder: a file\'s owner, or the first owner under a folder', () => {
        const paths = ['notes.md', `${RUN.folder}/chat/c.chat`];
        expect(lockOwnerUnder(paths, `${RUN.folder}/chat/c.chat`, false, lockOwnerOf)).toBe(RUN);
        expect(lockOwnerUnder(paths, 'notes.md', false, lockOwnerOf)).toBeNull();
        expect(lockOwnerUnder(paths, '_runs', true, lockOwnerOf)).toBe(RUN);
        expect(lockOwnerUnder(paths, `${RUN.folder}/chat`, true, lockOwnerOf)).toBe(RUN);
        expect(lockOwnerUnder(paths, 'docs', true, lockOwnerOf)).toBeNull();
        expect(lockOwnerUnder(paths, '_runs', true, undefined)).toBeNull();
    });

    it('folderOutsideRuns moves a folder target out of `_runs/`', () => {
        expect(folderOutsideRuns('_runs/r-11111111/chat/threads')).toBe('chat/threads');
        expect(folderOutsideRuns('_runs/r-11111111')).toBe('');
        expect(folderOutsideRuns('_runs')).toBe('');
        expect(folderOutsideRuns('docs/_runs')).toBe('docs/_runs');
        expect(folderOutsideRuns('src')).toBe('src');
    });

    it('runPathRefusal names `_runs/` paths only', () => {
        expect(runPathRefusal('_runs/r-1/x.md')).toMatch(/_runs\//);
        expect(runPathRefusal('_runs')).not.toBeNull();
        expect(runPathRefusal('notes/_runs.md')).toBeNull();
    });

    it('lockTitle names the owning run', () => {
        expect(lockTitle(RUN)).toContain('Runner R 11111111');
    });

    it('withoutRunPaths drops `_runs/` entries by their SOURCE path', () => {
        const entries = [
            { relPath: 'notes.md', file: vf('notes.md') },
            { relPath: 'r-11111111/c.chat', file: vf(`${RUN.folder}/c.chat`) },
        ];
        expect(withoutRunPaths(entries).map(e => e.relPath)).toEqual(['notes.md']);
        const clean = [entries[0]];
        expect(withoutRunPaths(clean)).toBe(clean);
    });
});

describe('runLabels', () => {
    it('a run reads "<runner name> <short id>", a runner-less run its label', () => {
        expect(runShortId(RUN)).toBe('11111111');
        expect(runDisplayName(RUN)).toBe('Runner R 11111111');
        expect(runDisplayName({ label: 'sub-22222222', runnerName: null })).toBe('sub-22222222');
    });
});
