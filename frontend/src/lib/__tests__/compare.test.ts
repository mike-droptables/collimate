import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect } from 'vitest';
import { compareCellLabel, isComparableTextFile, sameNameCandidates, otherComparableFiles } from '../compare';
import type { Cell, Channel, VirtualFile } from '@/lib/types';

const vf = (name: string, extra: Partial<VirtualFile> = {}): VirtualFile =>
    (fileFixture(name, `content of ${name}`, {language: '', timestamp: 't', ...extra}));

const cell = (id: string, name: string, files: VirtualFile[]): Cell =>
    ({ id, name, files });

const channel: Channel = {
    id: 'ch1',
    name: 'Main',
    nav_stacks: [
        { id: 'sN', name: 'Refs', cells: [cell('c4', 'Ref cell', [vf('notes.md')])] },
    ],
    main_stacks: [
        { id: 's1', name: 'Variant A', cells: [cell('c1', 'Draft', [vf('notes.md'), vf('extra.py')])] },
        {
            id: 's2', name: 'Variant B', cells: [
                cell('c2', 'Draft', [vf('notes.md')]),
                cell('c3', 'Old', [vf('other.md'), vf('img.png')]),
            ],
        },
    ],
    nav_selection_id: null,
    main_selection_id: null,
};

describe('isComparableTextFile', () => {
    it('accepts plain text files', () => {
        expect(isComparableTextFile(vf('a.md'))).toBe(true);
        expect(isComparableTextFile(vf('src/app.py'))).toBe(true);
    });
    it('rejects binary extensions and CAS-backed blobs', () => {
        expect(isComparableTextFile(vf('logo.png'))).toBe(false);
        expect(isComparableTextFile(vf('big.txt', { body_hash: 'abc', encoding: 'binary' }))).toBe(false);
    });
});

describe('sameNameCandidates', () => {
    it('finds same-named files across both stack columns, excluding the current cell', () => {
        const cands = sameNameCandidates(channel, 'c1', 'notes.md');
        expect(cands.map(c => c.label)).toEqual(['Variant B / Draft', 'Refs / Ref cell']);
        expect(cands.map(c => c.cellId)).toEqual(['c2', 'c4']);
    });
    it('returns nothing when no other cell has the file', () => {
        expect(sameNameCandidates(channel, 'c1', 'extra.py')).toEqual([]);
    });
    it('names a cell of an unnamed stack by the cell alone', () => {
        const unnamed: Channel = {
            ...channel,
            nav_stacks: [],
            main_stacks: [
                { id: 's1', name: '', cells: [cell('c1', 'CellOne', [vf('notes.md')])] },
                { id: 's2', name: '', cells: [cell('c2', 'CellTwo', [vf('notes.md')])] },
            ],
        };
        expect(sameNameCandidates(unnamed, 'c1', 'notes.md').map(c => c.label)).toEqual(['CellTwo']);
    });
});

describe('compareCellLabel', () => {
    it('reads "<stack> / <cell>", or the cell alone without a stack name', () => {
        expect(compareCellLabel('Variant B', 'Draft')).toBe('Variant B / Draft');
        expect(compareCellLabel('', 'Draft')).toBe('Draft');
        expect(compareCellLabel('   ', 'Draft')).toBe('Draft');
        expect(compareCellLabel(undefined, 'Draft')).toBe('Draft');
    });
});

describe('otherComparableFiles', () => {
    it('lists the cell\'s other text files, skipping binaries', () => {
        const files = otherComparableFiles(cell('c3', 'Old', [vf('other.md'), vf('img.png'), vf('a.txt')]), 'other.md');
        expect(files.map(f => f.name)).toEqual(['a.txt']);
    });
});
