import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect } from 'vitest';
import { buildFileTree, augmentTreeWithDeleted, computeLineStats } from '@/lib/fileTree';
import type { VirtualFile } from '@/lib/types';

const file = (name: string, content: string): VirtualFile => (fileFixture(name, content, {language: 'text', timestamp: ''}));

// These pure helpers are the shared diff-render orientation that BOTH the
// cell-history slider and the cell-pair diff depend on: `files` is the displayed /
// viewed side, `diffTargetFiles` (baseFiles) is the base to compare against.
describe('diff file-tree orientation', () => {
    it('injects a virtual deleted node for a base-only file (in base, not displayed)', () => {
        const displayed = [file('keep.ts', 'a')];
        const base = { 'keep.ts': 'a', 'gone.ts': 'old' };
        const tree = buildFileTree(displayed);
        augmentTreeWithDeleted(tree, base, new Set(displayed.map(f => f.name)));
        expect(tree['gone.ts']).toBeTruthy();
        expect(tree['gone.ts'].isDeleted).toBe(true);
        // A file present in the displayed set is never marked deleted.
        expect(tree['keep.ts'].isDeleted).toBeUndefined();
    });

    it('does not inject a deleted node for an added file (in displayed, not base)', () => {
        const displayed = [file('new.ts', 'fresh')];
        const base = {}; // empty base → the file is "added"
        const tree = buildFileTree(displayed);
        augmentTreeWithDeleted(tree, base, new Set(displayed.map(f => f.name)));
        // Only the real (added) node exists; no virtual deleted row.
        expect(Object.keys(tree)).toEqual(['new.ts']);
        expect(tree['new.ts'].isDeleted).toBeUndefined();
        // An added file resolves to no base content (→ FullFileEditor renders
        // it with `diffBaseContent === undefined`, i.e. no diff).
        expect(base['new.ts' as keyof typeof base]).toBeUndefined();
    });

    it('computes line stats in (base, displayed) order', () => {
        // base "a\nb" → displayed "a\nc\nd": -1 (b removed), +2 (c, d added)
        const { added, removed } = computeLineStats('a\nb', 'a\nc\nd');
        expect(added).toBe(2);
        expect(removed).toBe(1);
    });
});
