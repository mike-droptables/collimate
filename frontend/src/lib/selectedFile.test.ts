// The cell's selected file (GN-01): the active cell-file tab, else the
// Files pane's file selection, else none; run output never counts.
import { describe, expect, it } from 'vitest';
import { createDefaultLayout, openFileTab, openHistoryTab } from '@/lib/cellLayout';
import type { CellLayout } from '@/stores/collimate/types';
import { selectedFileOf } from './selectedFile';

const empty = () => createDefaultLayout({ filePaths: [], pinnedPath: null });
const withSelection = (layout: CellLayout, nodePath: string, isFolder = false): CellLayout =>
    ({ ...layout, filesPane: { ...layout.filesPane, selection: { nodePath, isFolder } } });

describe('selectedFileOf (GN-01)', () => {
    it('the active file tab wins over the pane selection', () => {
        const layout = withSelection(openFileTab(empty(), 'docs/brief.md', null), 'notes.md');
        expect(selectedFileOf(layout)).toBe('docs/brief.md');
    });

    it('a run-file tab falls through to the pane selection', () => {
        const layout = withSelection(openFileTab(empty(), '_runs/agent-aaaaaaaa/chat/conversation.chat', null), 'notes.md');
        expect(selectedFileOf(layout)).toBe('notes.md');
    });

    it('a folder selection or a run-file selection is ignored', () => {
        expect(selectedFileOf(withSelection(empty(), 'docs', true))).toBeNull();
        expect(selectedFileOf(withSelection(empty(), '_runs/agent-aaaaaaaa/x.md'))).toBeNull();
    });

    it('a history or empty tab with no selection gives none', () => {
        expect(selectedFileOf(empty())).toBeNull();
        expect(selectedFileOf(openHistoryTab(empty(), 'file', 'notes.md'))).toBeNull();
        expect(selectedFileOf(withSelection(openHistoryTab(empty(), 'file', 'notes.md'), 'b.md'))).toBe('b.md');
        expect(selectedFileOf(undefined)).toBeNull();
    });
});
