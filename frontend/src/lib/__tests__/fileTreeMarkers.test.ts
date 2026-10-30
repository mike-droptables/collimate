import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect } from 'vitest';
import {
    hasOptionsStack, buildFileTree, augmentTreeWithDeleted, isFolderKeep,
    pruneRedundantMarkers, remintEmptiedParent, FOLDER_KEEP_FILE,
} from '@/lib/fileTree';
import type { VirtualFile } from '@/lib/types';

const file = (name: string, content = ''): VirtualFile => (fileFixture(name, content, {language: 'text', timestamp: ''}));

describe('folder-keep markers', () => {
    it('a marker-only folder renders as an empty folder, marker leaf hidden', () => {
        const tree = buildFileTree([file(`lib/${FOLDER_KEEP_FILE}`)]);
        expect(tree['lib']).toBeTruthy();
        expect(tree['lib'].type).toBe('folder');
        // The .collkeep leaf must NOT appear as a child.
        expect(Object.keys(tree['lib'].children)).toEqual([]);
    });

    it('keeps real files and hides only the marker leaf', () => {
        const tree = buildFileTree([file('lib/a.ts', 'x'), file(`lib/${FOLDER_KEEP_FILE}`)]);
        expect(Object.keys(tree['lib'].children)).toEqual(['a.ts']);
    });

    it('isFolderKeep matches bare and nested markers only', () => {
        expect(isFolderKeep(FOLDER_KEEP_FILE)).toBe(true);
        expect(isFolderKeep(`src/${FOLDER_KEEP_FILE}`)).toBe(true);
        expect(isFolderKeep('src/.collkeeper')).toBe(false);
        expect(isFolderKeep('src/keep.ts')).toBe(false);
    });

    it('augmentTreeWithDeleted shows a deleted empty folder, not a deleted marker file', () => {
        const displayed = [file('keep.ts', 'a')];
        const base = { 'keep.ts': 'a', [`gone/${FOLDER_KEEP_FILE}`]: '' };
        const tree = buildFileTree(displayed);
        augmentTreeWithDeleted(tree, base, new Set(displayed.map(f => f.name)));
        expect(tree['gone']).toBeTruthy();
        expect(tree['gone'].type).toBe('folder');
        expect(tree['gone'].isDeleted).toBe(true);
        expect(Object.keys(tree['gone'].children)).toEqual([]);
    });

    it('pruneRedundantMarkers drops a marker once the folder has a real file', () => {
        const pruned = pruneRedundantMarkers([file('lib/a.ts', 'x'), file(`lib/${FOLDER_KEEP_FILE}`)]);
        expect(pruned.map(f => f.name)).toEqual(['lib/a.ts']);
    });

    it('pruneRedundantMarkers keeps the marker of a genuinely empty folder', () => {
        const pruned = pruneRedundantMarkers([file(`lib/${FOLDER_KEEP_FILE}`), file('other/a.ts', 'x')]);
        expect(pruned.map(f => f.name).sort()).toEqual([`lib/${FOLDER_KEEP_FILE}`, 'other/a.ts']);
    });

    it('remintEmptiedParent re-adds a marker when the last real file leaves', () => {
        // After deleting lib/a.ts, lib is empty → re-mint lib/.collkeep.
        const after = remintEmptiedParent([], 'lib/a.ts');
        expect(after.map(f => f.name)).toEqual([`lib/${FOLDER_KEEP_FILE}`]);
    });

    it('remintEmptiedParent is a no-op when the folder still has files', () => {
        const remaining = [file('lib/b.ts', 'y')];
        expect(remintEmptiedParent(remaining, 'lib/a.ts')).toBe(remaining);
    });

    it('remintEmptiedParent does not add a redundant marker when an empty subfolder remains', () => {
        // a/sub is materialized by its own marker → a does not need its own.
        const remaining = [file(`a/sub/${FOLDER_KEEP_FILE}`)];
        expect(remintEmptiedParent(remaining, 'a/x.ts')).toBe(remaining);
    });

    it('remintEmptiedParent is a no-op for a root-level file', () => {
        const remaining: VirtualFile[] = [];
        expect(remintEmptiedParent(remaining, 'top.ts')).toBe(remaining);
    });
});


it('Split gate follows effective declaration files__D07', () => {
    expect(hasOptionsStack({files: [file('.options_stack', 'options: []')]})).toBe(true);
    expect(hasOptionsStack({files: [file('folder/choice.options_stack')]})).toBe(true);
    expect(hasOptionsStack({files: [file('folder/a.txt')]})).toBe(false);
    expect(hasOptionsStack({files: []})).toBe(false);
});
