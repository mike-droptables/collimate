import { describe, it, expect } from 'vitest';
import { encodeCellDisplay, decodeCellDisplay, reconcileCellDisplay } from './cellDisplay';
import { createDefaultLayout, splitWithTab, openFileTab, allLeaves } from './cellLayout';
import { defaultCellHistory } from '@/stores/collimate/types';
describe('durable cell display descriptor', () => {
    it('restores tab identity, renderer, topology, history and the dock side and sizes, without split sizes or runtime state (FP-01)', () => {
        let layout = createDefaultLayout({ filePaths: ['a.md', 'b.py'], pinnedPath: 'a.md' });
        layout = openFileTab(layout, 'b.py', null, 'code-editor', { newTab: true });
        const tab = allLeaves(layout.root)[0].tabs.find(t => t.kind === 'file' && t.path === 'b.py')!;
        layout = splitWithTab(layout, layout.activeLeafId, tab.id, 'right');
        layout = { ...layout, filesPane: { ...layout.filesPane, width: 311, height: 222, side: 'top', collapsed: false, currentFolder: 'private', selection: { nodePath: 'a.md', isFolder: false } }, pinnedTabCompact: true };
        const history = { ...defaultCellHistory(), nodeId: 'files:version-a', versionId: 'version-a' };
        const saved = encodeCellDisplay(layout, history);
        expect(JSON.stringify(saved)).not.toMatch(/sizes|currentFolder|selection/);
        expect(saved.filesPane).toMatchObject({ side: 'top', width: 311, height: 222 });
        const restored = decodeCellDisplay(saved);
        expect(restored.history).toEqual(history);
        expect(restored.layout.activeLeafId).toBe(layout.activeLeafId);
        expect(allLeaves(restored.layout.root).flatMap(l => l.tabs)).toEqual(allLeaves(layout.root).flatMap(l => l.tabs));
        expect(restored.layout.root.type).toBe('split');
        expect(restored.layout.filesPane).toMatchObject({ side: 'top', width: 311, height: 222, currentFolder: '', selection: null });
    });

    it.each(['left', 'top'] as const)('the %s side and default sizes round-trip as null sizes (FP-01)', (side) => {
        const layout = createDefaultLayout({ filePaths: [], pinnedPath: null });
        const saved = encodeCellDisplay({ ...layout, filesPane: { ...layout.filesPane, side } }, null);
        expect(saved.filesPane).toMatchObject({ side, width: null, height: null });
        expect(decodeCellDisplay(saved).layout.filesPane).toMatchObject({ side, width: null, height: null });
    });

    it('a new layout saves the default runs pane: closed, on the right, at the default sizes', () => {
        const saved = encodeCellDisplay(createDefaultLayout({ filePaths: [], pinnedPath: null }), null);
        expect(saved.runsPane).toEqual({ open: false, side: 'right', width: null, height: null });
    });

    it.each([
        { open: true, side: 'right', width: 320, height: null },
        { open: false, side: 'bottom', width: 250, height: 180 },
    ] as const)('the runs pane round-trips its four fields: %o', (runsPane) => {
        const layout = createDefaultLayout({ filePaths: [], pinnedPath: null });
        const saved = encodeCellDisplay({ ...layout, runsPane: { ...runsPane, extra: 'never persisted' } as unknown as typeof layout.runsPane }, null);
        expect(saved.runsPane).toEqual(runsPane);
        expect(decodeCellDisplay(saved).layout.runsPane).toEqual(runsPane);
    });

    it('reconciling a stored display carries its runs pane through', () => {
        const layout = createDefaultLayout({ filePaths: ['a.md'], pinnedPath: null });
        const runsPane = { open: true, side: 'bottom', width: null, height: 240 } as const;
        const saved = encodeCellDisplay({ ...layout, runsPane }, null);
        const { layout: restored } = reconcileCellDisplay(saved, { filePaths: ['a.md'], pinnedPath: null }, () => true);
        expect(restored.runsPane).toEqual(runsPane);
    });

    it('a stored display that still carries a stray tab decodes to exactly the four fields', () => {
        const layout = createDefaultLayout({ filePaths: [], pinnedPath: null });
        const saved = encodeCellDisplay(layout, null);
        const stray = { ...saved, runsPane: { ...saved.runsPane, tab: 'log' } } as typeof saved;
        expect(decodeCellDisplay(stray).layout.runsPane).toEqual({ open: false, side: 'right', width: null, height: null });
    });
});
it('projects every restorable tab kind explicitly, without copying arbitrary fields', () => {
    const side = { channel: '11111111-1111-4111-8111-111111111111', cellId: '22222222-2222-4222-8222-222222222222', fileName: 'a.md', label: 'Current label' };
    const base = createDefaultLayout({ filePaths: [], pinnedPath: null });
    const tabs = [{ id: 'file', kind: 'file', path: 'a.md', rendererId: 'text' }, { id: 'history', kind: 'history', category: 'log', path: 'trace.log', rendererId: 'text' }, { id: 'empty', kind: 'empty' }, { id: 'compare', kind: 'compare', base: side, current: side }] as import('@/stores/collimate/types').CellTab[];
    const layout = { ...base, root: { type: 'leaf' as const, id: 'leaf', tabs: tabs.map(t => ({ ...t, content: 'must never be persisted' })), activeTabId: 'compare' }, activeLeafId: 'leaf' };
    const encoded = encodeCellDisplay(layout, defaultCellHistory());
    expect(JSON.stringify(encoded)).not.toContain('content');
    expect(allLeaves(decodeCellDisplay(encoded).layout.root)[0].tabs).toEqual(tabs);
});
