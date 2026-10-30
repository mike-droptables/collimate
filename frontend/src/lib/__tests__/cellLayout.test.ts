import { describe, it, expect } from 'vitest';
import {
    CONTENT_MIN_HEIGHT,
    CONTENT_MIN_WIDTH,
    MIN_LEAF_SHARE,
    activateTab,
    activeLeaf,
    activeTab,
    allLeaves,
    allTabs,
    closeHistoryTabs,
    closeOtherTabs,
    closeTab,
    closeTabsToRight,
    cloneLayout,
    createDefaultLayout,
    closeRunsPane,
    cycleFilesPaneSide,
    cycleRunsPaneSide,
    defaultFilesPane,
    defaultRunsPane,
    dockFilesPane,
    dockRunsPane,
    filesPaneAxis,
    filesPaneCollapsed,
    filesPaneRowClass,
    filesPaneSize,
    nextFilesPaneSide,
    nextRunsPaneSide,
    findFileTab,
    findLeaf,
    findTabLeaf,
    isTabPinned,
    moveTab,
    normalize,
    openCompareTab,
    openEmptyTab,
    openFilePaths,
    openHistoryTab,
    openFileTab,
    openRunsPane,
    paneReserve,
    paneSizeStyle,
    reconcile,
    renameFileTab,
    resolvePinnedPath,
    revealFileTab,
    runsPaneAxis,
    runsPaneSize,
    setActiveLeaf,
    setTabRenderer,
    setFilesPane,
    setFilesPanelFilters,
    setRunsPane,
    setSplitSizes,
    splitWithTab,
    toggleRunsPane,
    type LayoutCellContext,
} from '@/lib/cellLayout';
import type {
    CellLayout,
    CellTab,
    ContentLeaf,
    ContentNode,
    ContentSplit,
    FilesPaneState,
} from '@/stores/collimate/types';

// ── Fixture builders ────────────────────────────────────────────────────
// Layouts are built by hand (rather than through the ops) so each test
// states the exact tree it exercises, including deliberately malformed
// ones that only normalize() is allowed to see.

const fileTab = (id: string, path: string): CellTab => ({ id, kind: 'file', path });
const emptyTab = (id: string): CellTab => ({ id, kind: 'empty' });
const historyTab = (id: string, path: string): CellTab => ({ id, kind: 'history', category: 'file', path });

/** `activeTabId` defaults to the first tab; pass it explicitly (incl. null)
 *  to build a leaf with a stale/absent active tab. */
const mkLeaf = (id: string, tabs: CellTab[], activeTabId?: string | null): ContentLeaf => ({
    type: 'leaf',
    id,
    tabs,
    activeTabId: activeTabId !== undefined ? activeTabId : (tabs[0]?.id ?? null),
});

const mkSplit = (
    id: string,
    dir: 'row' | 'column',
    children: ContentNode[],
    sizes?: number[],
): ContentSplit => ({
    type: 'split',
    id,
    dir,
    children,
    sizes: sizes ?? children.map(() => 1 / children.length),
});

const mkLayout = (
    root: ContentNode,
    activeLeafId?: string,
    filesPane?: Partial<FilesPaneState>,
): CellLayout => ({
    root,
    activeLeafId: activeLeafId ?? allLeaves(root)[0].id,
    filesPane: { ...defaultFilesPane(), ...filesPane },
    runsPane: defaultRunsPane(),
});

const ctx = (o: Partial<LayoutCellContext> = {}): LayoutCellContext => ({
    filePaths: [],
    pinnedPath: null,
    ...o,
});

const leafOf = (l: CellLayout, leafId: string): ContentLeaf => findLeaf(l.root, leafId)!;
const tabIdsOf = (l: CellLayout, leafId: string): string[] => leafOf(l, leafId).tabs.map(t => t.id);
const asSplit = (n: ContentNode): ContentSplit => {
    expect(n.type).toBe('split');
    return n as ContentSplit;
};
const asLeaf = (n: ContentNode): ContentLeaf => {
    expect(n.type).toBe('leaf');
    return n as ContentLeaf;
};

// ────────────────────────────────────────────────────────────────────────

describe('createDefaultLayout', () => {
    it('seeds a single leaf with the pinned file tab, active', () => {
        const l = createDefaultLayout(ctx({ filePaths: ['a.md'], pinnedPath: 'a.md' }));
        const leaf = asLeaf(l.root);
        expect(leaf.tabs).toHaveLength(1);
        expect(leaf.tabs[0]).toMatchObject({ kind: 'file', path: 'a.md' });
        expect(leaf.activeTabId).toBe(leaf.tabs[0].id);
        expect(l.activeLeafId).toBe(leaf.id);
    });

    it('falls back to a single empty tab when there is nothing to show', () => {
        const l = createDefaultLayout(ctx());
        const leaf = asLeaf(l.root);
        expect(leaf.tabs.map(t => t.kind)).toEqual(['empty']);
        expect(leaf.activeTabId).toBe(leaf.tabs[0].id);
    });

    it('mints fresh ids on every call', () => {
        const a = createDefaultLayout(ctx());
        const b = createDefaultLayout(ctx());
        expect(a.root.id).not.toBe(b.root.id);
        expect(asLeaf(a.root).tabs[0].id).not.toBe(asLeaf(b.root).tabs[0].id);
    });

    it('uses the default Files-pane descriptor: docked left, default sizes, hidden until shown (C12, FP-01)', () => {
        expect(createDefaultLayout(ctx()).filesPane).toEqual({
            side: 'left',
            width: null,
            height: null,
            collapsed: null,
            currentFolder: '',
            selection: null,
            panelFilters: { runsOnly: false, sortByRecency: false },
        });
    });
});

describe('filesPaneCollapsed', () => {
    it('starts collapsed while unset, including an empty new cell', () => {
        const layout = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        expect(filesPaneCollapsed(layout)).toBe(true);
        expect(filesPaneCollapsed(mkLayout(mkLeaf('g1', [])))).toBe(true);
        expect(layout.filesPane.collapsed).toBeNull();
    });

    it('derives collapsed once a real tab exists', () => {
        expect(filesPaneCollapsed(mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md')])))).toBe(true);
        expect(filesPaneCollapsed(mkLayout(mkLeaf('g1', [emptyTab('t1'), fileTab('t2', 'a.md')])))).toBe(true);
    });

    it('an explicit choice wins over the derivation, both ways', () => {
        expect(filesPaneCollapsed(mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md')]), 'g1', { collapsed: false }))).toBe(false);
        expect(filesPaneCollapsed(mkLayout(mkLeaf('g1', [emptyTab('t1')]), 'g1', { collapsed: true }))).toBe(true);
    });
});

describe('isTabPinned', () => {
    it('a file tab is pinned only as the pinned path', () => {
        expect(isTabPinned(fileTab('t', 'a.md'), 'a.md')).toBe(true);
        expect(isTabPinned(fileTab('t', 'a.md'), null)).toBe(false);
        expect(isTabPinned(emptyTab('t'), null)).toBe(false);
    });

    it('pins the file tab whose path is the cell preview', () => {
        expect(isTabPinned(fileTab('t', 'a.md'), 'a.md')).toBe(true);
        expect(isTabPinned(fileTab('t', 'b.md'), 'a.md')).toBe(false);
        expect(isTabPinned(fileTab('t', 'a.md'), null)).toBe(false);
    });

    it('never pins history/empty tabs', () => {
        expect(isTabPinned(emptyTab('t'), 'a.md')).toBe(false);
        expect(isTabPinned(historyTab('t', 'a.md'), 'a.md')).toBe(false);
    });
});

describe('resolvePinnedPath', () => {
    const files = [{ name: 'a.md' }, { name: 'b.md' }];
    it('is null without a pin', () => {
        expect(resolvePinnedPath(undefined, files)).toBeNull();
        expect(resolvePinnedPath(null, files)).toBeNull();
    });
    it('returns a pin that names an existing file', () => {
        expect(resolvePinnedPath('b.md', files)).toBe('b.md');
    });
    it('is null for a dangling pin', () => {
        expect(resolvePinnedPath('gone.md', files)).toBeNull();
    });
});

describe('queries', () => {
    const l = mkLayout(
        mkSplit('s1', 'row', [
            mkLeaf('g1', [fileTab('t1', 'a.md'), historyTab('t2', 'old.md')]),
            mkSplit('s2', 'column', [
                mkLeaf('g2', [historyTab('t3', 'older.md')]),
                mkLeaf('g3', [fileTab('t4', 'b.md'), emptyTab('t5')], 't5'),
            ]),
        ]),
        'g3',
    );

    it('walks every leaf and tab in tree order', () => {
        expect(allLeaves(l.root).map(g => g.id)).toEqual(['g1', 'g2', 'g3']);
        expect(allTabs(l).map(t => t.id)).toEqual(['t1', 't2', 't3', 't4', 't5']);
        expect(openFilePaths(l)).toEqual(['a.md', 'b.md']);
    });

    it('finds leaves, tab owners and file tabs', () => {
        expect(findLeaf(l.root, 'g2')!.id).toBe('g2');
        expect(findLeaf(l.root, 'nope')).toBeNull();
        expect(findTabLeaf(l, 't4')!.id).toBe('g3');
        expect(findTabLeaf(l, 'nope')).toBeNull();
        expect(findFileTab(l, 'b.md')).toMatchObject({ leaf: { id: 'g3' }, tab: { id: 't4' } });
        expect(findFileTab(l, 'missing.md')).toBeNull();
    });

    it('resolves the active leaf and tab, falling back when ids are stale', () => {
        expect(activeLeaf(l).id).toBe('g3');
        expect(activeTab(l)!.id).toBe('t5');
        const stale = { ...l, activeLeafId: 'gone' };
        expect(activeLeaf(stale).id).toBe('g1');
        const noActive = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md')], null));
        expect(activeTab(noActive)).toBeNull();
    });
});

describe('openFileTab', () => {
    it('R1: focuses the existing tab when the file is already open anywhere', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [emptyTab('t1')]),
                mkLeaf('g2', [fileTab('t2', 'a.md'), fileTab('t3', 'b.md')], 't3'),
            ]),
            'g1',
        );
        const next = openFileTab(l, 'a.md', null);
        expect(allTabs(next)).toHaveLength(3);
        expect(next.activeLeafId).toBe('g2');
        expect(leafOf(next, 'g2').activeTabId).toBe('t2');
        // The empty tab in the previously-active leaf is untouched.
        expect(tabIdsOf(next, 'g1')).toEqual(['t1']);
    });

    it('R2: takes over the active non-pinned tab in place', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')], 't2'));
        const next = openFileTab(l, 'c.md', 'a.md');
        const tabs = leafOf(next, 'g1').tabs;
        expect(tabs).toHaveLength(2);
        expect(tabs[0].id).toBe('t1');
        expect(tabs[1]).toMatchObject({ kind: 'file', path: 'c.md' });
        expect(leafOf(next, 'g1').activeTabId).toBe(tabs[1].id);
    });

    it('takes over an active empty tab', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        const tabs = leafOf(openFileTab(l, 'a.md', null), 'g1').tabs;
        expect(tabs).toHaveLength(1);
        expect(tabs[0]).toMatchObject({ kind: 'file', path: 'a.md' });
    });

    it('newTab: never takes the active tab over — inserts right after it', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'c.md')], 't1'));
        const next = openFileTab(l, 'b.md', null, undefined, { newTab: true });
        const leaf = leafOf(next, 'g1');
        expect(leaf.tabs.map(t => (t.kind === 'file' ? t.path : t.kind))).toEqual(['a.md', 'b.md', 'c.md']);
        expect(leaf.tabs.find(t => t.kind === 'file' && t.path === 'b.md')?.id).toBe(leaf.activeTabId);
        // Already open → just focused, never duplicated.
        const again = openFileTab(next, 'c.md', null, undefined, { newTab: true });
        expect(leafOf(again, 'g1').tabs).toHaveLength(3);
        expect(leafOf(again, 'g1').activeTabId).toBe('t2');
    });

    it('R2: inserts after a pinned file tab instead of replacing it', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), emptyTab('t2')], 't1'));
        const next = openFileTab(l, 'b.md', 'a.md');
        const tabs = leafOf(next, 'g1').tabs;
        expect(tabs.map(t => t.id)[0]).toBe('t1');
        expect(tabs[1]).toMatchObject({ kind: 'file', path: 'b.md' });
        expect(tabs[2].id).toBe('t2');
        expect(leafOf(next, 'g1').activeTabId).toBe(tabs[1].id);
    });

    it('inserts after the pinned tab', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'pin.md')]));
        const tabs = leafOf(openFileTab(l, 'a.md', 'pin.md'), 'g1').tabs;
        expect(tabs.map(t => t.kind === 'file' && t.path)).toEqual(['pin.md', 'a.md']);
    });

    it('appends when the target leaf has no active tab', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'pin.md')], null));
        const tabs = leafOf(openFileTab(l, 'a.md', 'pin.md'), 'g1').tabs;
        expect(tabs.map(t => t.kind === 'file' && t.path)).toEqual(['pin.md', 'a.md']);
    });

    it('opens into the ACTIVE leaf, not the first one', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [emptyTab('t1')]), mkLeaf('g2', [emptyTab('t2')])]),
            'g2',
        );
        const next = openFileTab(l, 'a.md', null);
        expect(leafOf(next, 'g1').tabs.map(t => t.kind)).toEqual(['empty']);
        expect(leafOf(next, 'g2').tabs.map(t => t.kind)).toEqual(['file']);
        expect(next.activeLeafId).toBe('g2');
    });

});

describe('openEmptyTab', () => {
    it('appends a fresh empty tab to the named leaf and activates it', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [fileTab('t1', 'a.md')]), mkLeaf('g2', [fileTab('t2', 'b.md')])]),
            'g1',
        );
        const next = openEmptyTab(l, 'g2');
        const tabs = leafOf(next, 'g2').tabs;
        expect(tabs.map(t => t.kind)).toEqual(['file', 'empty']);
        expect(leafOf(next, 'g2').activeTabId).toBe(tabs[1].id);
        expect(next.activeLeafId).toBe('g2');
    });

    it('leaves untouched branches of the tree structurally shared', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkSplit('s2', 'column', [mkLeaf('g1', [emptyTab('t1')]), mkLeaf('g2', [emptyTab('t2')])]),
                mkLeaf('g3', [emptyTab('t3')]),
            ]),
            'g3',
        );
        const next = openEmptyTab(l, 'g3');
        expect(asSplit(next.root).children[0]).toBe(asSplit(l.root).children[0]);
    });

    it('is a no-op for an unknown leaf', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        expect(openEmptyTab(l, 'nope')).toBe(l);
    });
});

describe('closeTab', () => {
    it('R4: refuses the pinned file tab', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')]));
        expect(closeTab(l, 't1', 'a.md')).toBe(l);
        // Unpinned, the same tab closes normally.
        expect(allTabs(closeTab(l, 't1', null)).map(t => t.id)).toEqual(['t2']);
    });

    it('is a no-op for an unknown tab', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        expect(closeTab(l, 'nope', null)).toBe(l);
    });

    it('activates the previous neighbour when the active tab closes', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md'), fileTab('t3', 'c.md')], 't2'));
        const next = closeTab(l, 't2', null);
        expect(tabIdsOf(next, 'g1')).toEqual(['t1', 't3']);
        expect(leafOf(next, 'g1').activeTabId).toBe('t1');
    });

    it('activates the next tab when the first one closes', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')], 't1'));
        expect(leafOf(closeTab(l, 't1', null), 'g1').activeTabId).toBe('t2');
    });

    it('leaves the active tab alone when a different tab closes', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')], 't2'));
        expect(leafOf(closeTab(l, 't1', null), 'g1').activeTabId).toBe('t2');
    });

    it('collapses the leaf (and its split) when its last tab closes', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [fileTab('t1', 'a.md')]), mkLeaf('g2', [fileTab('t2', 'b.md')])]),
            'g1',
        );
        const next = closeTab(l, 't1', null);
        expect(asLeaf(next.root).id).toBe('g2');
        expect(next.activeLeafId).toBe('g2');
    });

    it('reseeds an empty tab when the very last tab closes', () => {
        // The reseed must happen HERE, not be left to reconcile: reconcile
        // only re-runs when the CELL changes, and closing a tab changes only
        // the layout — a tab-less tree would stay blank forever.
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md')]));
        const next = closeTab(l, 't1', null);
        const leaf = asLeaf(next.root);
        expect(leaf.tabs.map(t => t.kind)).toEqual(['empty']);
        expect(leaf.activeTabId).toBe(leaf.tabs[0].id);
        expect(next.activeLeafId).toBe(leaf.id);
    });
});

describe('closeOtherTabs / closeTabsToRight', () => {
    it('closes every closable sibling but keeps pinned ones', () => {
        const l = mkLayout(mkLeaf('g1', [
            fileTab('t1', 'pin.md'), fileTab('t2', 'a.md'), fileTab('t4', 'b.md'),
        ], 't2'));
        const next = closeOtherTabs(l, 't4', 'pin.md');
        expect(tabIdsOf(next, 'g1')).toEqual(['t1', 't4']);
        expect(leafOf(next, 'g1').activeTabId).toBe('t4');
    });

    it('closeOtherTabs is a no-op for an unknown tab', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        expect(closeOtherTabs(l, 'nope', null)).toBe(l);
    });

    it('closes only the closable tabs to the right', () => {
        const l = mkLayout(mkLeaf('g1', [
            fileTab('t1', 'a.md'), fileTab('t2', 'b.md'), fileTab('t3', 'pin.md'), fileTab('t4', 'c.md'),
        ], 't1'));
        const next = closeTabsToRight(l, 't1', 'pin.md');
        expect(tabIdsOf(next, 'g1')).toEqual(['t1', 't3']);
    });

    it('closeTabsToRight is a no-op for the last tab and for an unknown tab', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')]));
        expect(closeTabsToRight(l, 't2', null)).toBe(l);
        expect(closeTabsToRight(l, 'nope', null)).toBe(l);
    });
});

describe('activateTab / setActiveLeaf', () => {
    const split = () => mkLayout(
        mkSplit('s1', 'row', [
            mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')], 't1'),
            mkLeaf('g2', [fileTab('t3', 'c.md')]),
        ]),
        'g1',
    );

    it('activates a tab and its leaf', () => {
        const next = activateTab(split(), 't3');
        expect(next.activeLeafId).toBe('g2');
        expect(leafOf(next, 'g2').activeTabId).toBe('t3');
    });

    it('is a no-op when the tab is unknown or already focused', () => {
        const l = split();
        expect(activateTab(l, 'nope')).toBe(l);
        expect(activateTab(l, 't1')).toBe(l);
    });

    it('moves leaf focus without touching the tree when the tab is already its leaf-active one', () => {
        const l = split();
        const next = activateTab(l, 't3');
        expect(next.root).toBe(l.root);
        expect(next.activeLeafId).toBe('g2');
    });

    it('setActiveLeaf validates the leaf id', () => {
        const l = split();
        expect(setActiveLeaf(l, 'g1')).toBe(l);
        expect(setActiveLeaf(l, 'nope')).toBe(l);
        expect(setActiveLeaf(l, 'g2').activeLeafId).toBe('g2');
    });
});

describe('moveTab', () => {
    const three = () => mkLayout(mkLeaf('g1', [fileTab('a', 'a.md'), fileTab('b', 'b.md'), fileTab('c', 'c.md')], 'b'));

    it('reorders within a leaf when moving right (insertion index accounts for the removal)', () => {
        expect(tabIdsOf(moveTab(three(), 'a', 'g1', 2), 'g1')).toEqual(['b', 'a', 'c']);
        expect(tabIdsOf(moveTab(three(), 'a', 'g1', 3), 'g1')).toEqual(['b', 'c', 'a']);
    });

    it('reorders within a leaf when moving left', () => {
        expect(tabIdsOf(moveTab(three(), 'c', 'g1', 0), 'g1')).toEqual(['c', 'a', 'b']);
        expect(tabIdsOf(moveTab(three(), 'c', 'g1', 1), 'g1')).toEqual(['a', 'c', 'b']);
    });

    it('clamps out-of-range indices', () => {
        expect(tabIdsOf(moveTab(three(), 'a', 'g1', 99), 'g1')).toEqual(['b', 'c', 'a']);
        expect(tabIdsOf(moveTab(three(), 'c', 'g1', -5), 'g1')).toEqual(['c', 'a', 'b']);
    });

    it('activates the moved tab', () => {
        const next = moveTab(three(), 'a', 'g1', 2);
        expect(leafOf(next, 'g1').activeTabId).toBe('a');
        expect(next.activeLeafId).toBe('g1');
    });

    it('a same-position move just activates the tab', () => {
        const l = three();
        const next = moveTab(l, 'a', 'g1', 0);
        expect(tabIdsOf(next, 'g1')).toEqual(['a', 'b', 'c']);
        expect(leafOf(next, 'g1').activeTabId).toBe('a');
    });

    it('moves a tab across leaves at the requested index and repairs the source', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [fileTab('a', 'a.md'), fileTab('b', 'b.md')], 'a'),
                mkLeaf('g2', [fileTab('c', 'c.md'), fileTab('d', 'd.md')], 'c'),
            ]),
            'g1',
        );
        const next = moveTab(l, 'a', 'g2', 1);
        expect(tabIdsOf(next, 'g1')).toEqual(['b']);
        expect(leafOf(next, 'g1').activeTabId).toBe('b');
        expect(tabIdsOf(next, 'g2')).toEqual(['c', 'a', 'd']);
        expect(leafOf(next, 'g2').activeTabId).toBe('a');
        expect(next.activeLeafId).toBe('g2');
    });

    it("leaves the source leaf's active tab alone when a background tab moves out", () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [fileTab('a', 'a.md'), fileTab('b', 'b.md')], 'b'),
                mkLeaf('g2', [fileTab('c', 'c.md')]),
            ]),
            'g1',
        );
        const next = moveTab(l, 'a', 'g2', 0);
        expect(leafOf(next, 'g1').activeTabId).toBe('b');
        expect(tabIdsOf(next, 'g2')).toEqual(['a', 'c']);
    });

    it('collapses the source leaf when it empties', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [fileTab('a', 'a.md')]), mkLeaf('g2', [fileTab('c', 'c.md')])]),
            'g1',
        );
        const next = moveTab(l, 'a', 'g2', 0);
        const leaf = asLeaf(next.root);
        expect(leaf.id).toBe('g2');
        expect(leaf.tabs.map(t => t.id)).toEqual(['a', 'c']);
        expect(next.activeLeafId).toBe('g2');
    });

    it('is a no-op for an unknown tab or target leaf', () => {
        const l = three();
        expect(moveTab(l, 'nope', 'g1', 0)).toBe(l);
        expect(moveTab(l, 'a', 'nope', 0)).toBe(l);
    });
});

describe('splitWithTab', () => {
    const two = () => mkLayout(mkLeaf('g1', [fileTab('a', 'a.md'), fileTab('b', 'b.md')], 'a'));

    it("splits 'right' into a row with the new leaf second", () => {
        const next = splitWithTab(two(), 'g1', 'b', 'right');
        const s = asSplit(next.root);
        expect(s.dir).toBe('row');
        expect(s.sizes).toEqual([0.5, 0.5]);
        expect(asLeaf(s.children[0]).id).toBe('g1');
        expect(asLeaf(s.children[0]).tabs.map(t => t.id)).toEqual(['a']);
        const created = asLeaf(s.children[1]);
        expect(created.tabs.map(t => t.id)).toEqual(['b']);
        expect(created.activeTabId).toBe('b');
        expect(next.activeLeafId).toBe(created.id);
    });

    it("splits 'left' into a row with the new leaf first", () => {
        const s = asSplit(splitWithTab(two(), 'g1', 'b', 'left').root);
        expect(s.dir).toBe('row');
        expect(asLeaf(s.children[0]).tabs.map(t => t.id)).toEqual(['b']);
        expect(asLeaf(s.children[1]).id).toBe('g1');
    });

    it("splits 'bottom' into a column with the new leaf second", () => {
        const s = asSplit(splitWithTab(two(), 'g1', 'b', 'bottom').root);
        expect(s.dir).toBe('column');
        expect(asLeaf(s.children[0]).id).toBe('g1');
        expect(asLeaf(s.children[1]).tabs.map(t => t.id)).toEqual(['b']);
    });

    it("splits 'top' into a column with the new leaf first", () => {
        const s = asSplit(splitWithTab(two(), 'g1', 'b', 'top').root);
        expect(s.dir).toBe('column');
        expect(asLeaf(s.children[0]).tabs.map(t => t.id)).toEqual(['b']);
        expect(asLeaf(s.children[1]).id).toBe('g1');
    });

    it('repairs the source leaf active tab when the split tab was active', () => {
        const next = splitWithTab(two(), 'g1', 'a', 'right');
        const s = asSplit(next.root);
        expect(asLeaf(s.children[0]).activeTabId).toBe('b');
    });

    it("is a no-op when the tab is its leaf's only tab", () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('a', 'a.md')]));
        expect(splitWithTab(l, 'g1', 'a', 'right')).toBe(l);
    });

    it('is a no-op for an unknown tab or target leaf', () => {
        const l = two();
        expect(splitWithTab(l, 'g1', 'nope', 'right')).toBe(l);
        expect(splitWithTab(l, 'nope', 'a', 'right')).toBe(l);
    });

    it('splits a leaf with a tab dragged from another leaf, collapsing the emptied source', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [fileTab('a', 'a.md')]), mkLeaf('g2', [fileTab('c', 'c.md')])]),
            'g1',
        );
        const next = splitWithTab(l, 'g2', 'a', 'bottom');
        const s = asSplit(next.root);
        expect(s.dir).toBe('column');
        expect(asLeaf(s.children[0]).id).toBe('g2');
        expect(asLeaf(s.children[1]).tabs.map(t => t.id)).toEqual(['a']);
    });

    it('merges a same-direction nested split, scaling the sizes', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [fileTab('a', 'a.md')]),
                mkLeaf('g2', [fileTab('b', 'b.md'), fileTab('c', 'c.md')], 'b'),
            ]),
            'g2',
        );
        const s = asSplit(splitWithTab(l, 'g2', 'c', 'right').root);
        expect(s.dir).toBe('row');
        expect(s.children.map(c => asLeaf(c).tabs.map(t => t.id))).toEqual([['a'], ['b'], ['c']]);
        expect(s.sizes).toEqual([0.5, 0.25, 0.25]);
    });

    it('keeps an opposite-direction split nested', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [fileTab('a', 'a.md')]),
                mkLeaf('g2', [fileTab('b', 'b.md'), fileTab('c', 'c.md')], 'b'),
            ]),
            'g2',
        );
        const s = asSplit(splitWithTab(l, 'g2', 'c', 'bottom').root);
        expect(s.dir).toBe('row');
        expect(asLeaf(s.children[0]).id).toBe('g1');
        const inner = asSplit(s.children[1]);
        expect(inner.dir).toBe('column');
        expect(s.sizes).toEqual([0.5, 0.5]);
    });
});

describe('setSplitSizes', () => {
    const l = mkLayout(
        mkSplit('s1', 'row', [
            mkLeaf('g1', [emptyTab('t1')]),
            mkSplit('s2', 'column', [mkLeaf('g2', [emptyTab('t2')]), mkLeaf('g3', [emptyTab('t3')])], [0.5, 0.5]),
        ], [0.5, 0.5]),
    );

    it('clamps to the minimum share and renormalizes', () => {
        // The floored entry must land ON the minimum, not be scaled back
        // below it by a post-clamp renormalize — and the other entry absorbs
        // the whole difference (untouched children must not reflow).
        const s = asSplit(setSplitSizes(l, 's1', [0.02, 0.98]).root);
        const total = s.sizes.reduce((a, b) => a + b, 0);
        expect(total).toBeCloseTo(1, 10);
        expect(s.sizes[0]).toBeCloseTo(MIN_LEAF_SHARE, 10);
        expect(s.sizes[1]).toBeCloseTo(1 - MIN_LEAF_SHARE, 10);
    });

    it('targets a nested split by id', () => {
        const next = setSplitSizes(l, 's2', [0.7, 0.3]);
        const inner = asSplit(asSplit(next.root).children[1]);
        expect(inner.sizes).toEqual([0.7, 0.3]);
        expect(asSplit(next.root).sizes).toEqual([0.5, 0.5]);
    });

    it('normalizes sizes that do not sum to 1', () => {
        const s = asSplit(setSplitSizes(l, 's1', [3, 1]).root);
        expect(s.sizes).toEqual([0.75, 0.25]);
    });

    it('is a no-op for an unknown split, a length mismatch, or unchanged sizes', () => {
        expect(setSplitSizes(l, 'nope', [0.5, 0.5])).toBe(l);
        expect(setSplitSizes(l, 's1', [0.3, 0.3, 0.4])).toBe(l);
        expect(setSplitSizes(l, 's1', [0.5, 0.5])).toBe(l);
    });
});

describe('setFilesPane', () => {
    it('merges a patch into the Files-pane descriptor', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        const next = setFilesPane(l, { collapsed: true, currentFolder: 'docs' });
        expect(next.filesPane).toMatchObject({ collapsed: true, currentFolder: 'docs' });
        expect(next.root).toBe(l.root);
    });

    it('is a no-op when every patched field already holds that value', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        expect(setFilesPane(l, { currentFolder: '', collapsed: null })).toBe(l);
        expect(setFilesPane(l, {})).toBe(l);
    });
});

describe('the Files pane dock (FP-01, FP-02, FP-03, FP-04)', () => {
    const l = () => mkLayout(mkLeaf('g1', [emptyTab('t1')]));

    it('switches between its two sides: left, top, left (FP-03)', () => {
        expect(nextFilesPaneSide('left')).toBe('top');
        expect(nextFilesPaneSide('top')).toBe('left');
    });

    it('cycleFilesPaneSide moves the side and leaves the shown state and sizes alone (FP-03)', () => {
        const hidden = setFilesPane(l(), { collapsed: true, width: 222, height: 133 });
        const next = cycleFilesPaneSide(hidden);
        expect(next.filesPane).toMatchObject({ side: 'top', collapsed: true, width: 222, height: 133 });
        expect(next.root).toBe(hidden.root);
        expect(cycleFilesPaneSide(next).filesPane.side).toBe('left');
    });

    it('dockFilesPane sets the side and shows the pane; the SAME layout when already there (FP-04)', () => {
        const next = dockFilesPane(l(), 'top');
        expect(next.filesPane).toMatchObject({ side: 'top', collapsed: false });
        expect(dockFilesPane(next, 'top')).toBe(next);
    });

    it('the column sizes by width, the band by height, each with its own default (FP-01)', () => {
        expect(filesPaneAxis('left')).toBe('x');
        expect(filesPaneAxis('top')).toBe('y');
        expect(filesPaneSize(l())).toBe(194);
        expect(filesPaneSize(setFilesPane(l(), { side: 'top' }))).toBe(198);
        const sized = setFilesPane(l(), { width: 250, height: 150 });
        expect(filesPaneSize(sized)).toBe(250);
        expect(filesPaneSize(setFilesPane(sized, { side: 'top' }))).toBe(150);
    });

    it('the pane row runs as a row or a column per side, so the DOM order never changes (FP-02)', () => {
        expect(filesPaneRowClass('left')).toBe('flex-row');
        expect(filesPaneRowClass('top')).toBe('flex-col');
    });
});

describe('the runs pane', () => {
    const l = () => mkLayout(mkLeaf('g1', [emptyTab('t1')]));

    it('starts closed, on the right, at the default sizes, with no view of its own; a new layout carries it', () => {
        expect(defaultRunsPane()).toEqual({ open: false, side: 'right', width: null, height: null });
        expect(Object.keys(defaultRunsPane()).sort()).toEqual(['height', 'open', 'side', 'width']);
        expect(createDefaultLayout(ctx()).runsPane).toEqual(defaultRunsPane());
    });

    it('setRunsPane merges a patch; the SAME layout when nothing changes', () => {
        const base = l();
        const next = setRunsPane(base, { side: 'bottom', width: 300 });
        expect(next.runsPane).toEqual({ ...defaultRunsPane(), side: 'bottom', width: 300 });
        expect(next.root).toBe(base.root);
        expect(next.filesPane).toBe(base.filesPane);
        expect(setRunsPane(base, { open: false, side: 'right' })).toBe(base);
        expect(setRunsPane(base, {})).toBe(base);
    });

    it('switches between its two sides: right, bottom, right', () => {
        expect(nextRunsPaneSide('right')).toBe('bottom');
        expect(nextRunsPaneSide('bottom')).toBe('right');
        const open = openRunsPane(setRunsPane(l(), { width: 260, height: 140 }));
        const turned = cycleRunsPaneSide(open);
        expect(turned.runsPane).toMatchObject({ side: 'bottom', open: true, width: 260, height: 140 });
        expect(cycleRunsPaneSide(turned).runsPane.side).toBe('right');
        expect(cycleRunsPaneSide(l()).runsPane).toMatchObject({ side: 'bottom', open: false });
    });

    it('dockRunsPane sets the side and opens the pane; the SAME layout when already there', () => {
        const next = dockRunsPane(l(), 'bottom');
        expect(next.runsPane).toMatchObject({ side: 'bottom', open: true });
        expect(dockRunsPane(next, 'bottom')).toBe(next);
    });

    it('opens (the SAME layout when already open), closes keeping its side and sizes, and toggles', () => {
        const opened = openRunsPane(setRunsPane(l(), { width: 260 }));
        expect(opened.runsPane).toEqual({ open: true, side: 'right', width: 260, height: null });
        expect(openRunsPane(opened)).toBe(opened);
        const closed = closeRunsPane(dockRunsPane(opened, 'bottom'));
        expect(closed.runsPane).toEqual({ open: false, side: 'bottom', width: 260, height: null });
        expect(closeRunsPane(closed)).toBe(closed);
        expect(toggleRunsPane(closed).runsPane.open).toBe(true);
        expect(toggleRunsPane(toggleRunsPane(closed)).runsPane.open).toBe(false);
    });

    it('the column sizes by width, the band by height, each with its own default', () => {
        expect(runsPaneAxis('right')).toBe('x');
        expect(runsPaneAxis('bottom')).toBe('y');
        expect(runsPaneSize(l())).toBe(280);
        expect(runsPaneSize(setRunsPane(l(), { side: 'bottom' }))).toBe(200);
        const sized = setRunsPane(l(), { width: 320, height: 160 });
        expect(runsPaneSize(sized)).toBe(320);
        expect(runsPaneSize(setRunsPane(sized, { side: 'bottom' }))).toBe(160);
    });

    it('a clone for another cell keeps the runs pane', () => {
        const source = dockRunsPane(openRunsPane(setRunsPane(l(), { height: 150 })), 'bottom');
        const clone = cloneLayout(source, () => false);
        expect(clone.runsPane).toEqual(source.runsPane);
        expect(clone.runsPane).not.toBe(source.runsPane);
    });
});

describe('setFilesPanelFilters', () => {
    it('patches the Files panel toggles through the pane, keeping the other one (P03)', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        const runs = setFilesPanelFilters(l, { runsOnly: true });
        expect(runs.filesPane.panelFilters).toEqual({ runsOnly: true, sortByRecency: false });
        expect(setFilesPanelFilters(runs, { sortByRecency: true }).filesPane.panelFilters).toEqual({ runsOnly: true, sortByRecency: true });
        expect(runs.root).toBe(l.root);
    });

    it('is a no-op when nothing changes (P03)', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        expect(setFilesPanelFilters(l, { runsOnly: false })).toBe(l);
        expect(setFilesPanelFilters(l, {})).toBe(l);
    });
});

describe('normalize', () => {
    it('returns the SAME object when nothing needs fixing', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [emptyTab('t1')]), mkLeaf('g2', [emptyTab('t2')])], [0.5, 0.5]),
        );
        expect(normalize(l)).toBe(l);
        const fresh = createDefaultLayout(ctx({ filePaths: ['a.md'], pinnedPath: 'a.md' }));
        expect(normalize(fresh)).toBe(fresh);
    });

    it('is idempotent', () => {
        const messy = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [emptyTab('t1')], 'gone'),
                mkSplit('s2', 'row', [mkLeaf('g2', []), mkLeaf('g3', [emptyTab('t3')])], [0.5, 0.5]),
            ], [0.5, 0.5]),
            'gone',
        );
        const once = normalize(messy);
        expect(normalize(once)).toBe(once);
    });

    it('collapses a single-child split', () => {
        const l = mkLayout(mkSplit('s1', 'row', [mkLeaf('g1', [emptyTab('t1')])], [1]), 'g1');
        expect(asLeaf(normalize(l).root).id).toBe('g1');
    });

    it('drops empty leaves and renormalizes the remaining sizes', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [emptyTab('t1')]),
                mkLeaf('g2', []),
                mkLeaf('g3', [emptyTab('t3')]),
            ], [0.25, 0.5, 0.25]),
            'g1',
        );
        const s = asSplit(normalize(l).root);
        expect(s.children.map(c => asLeaf(c).id)).toEqual(['g1', 'g3']);
        expect(s.sizes).toEqual([0.5, 0.5]);
    });

    it('merges a same-direction nested split, scaling by the parent share', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [emptyTab('t1')]),
                mkSplit('s2', 'row', [mkLeaf('g2', [emptyTab('t2')]), mkLeaf('g3', [emptyTab('t3')])], [0.25, 0.75]),
            ], [0.4, 0.6]),
            'g1',
        );
        const s = asSplit(normalize(l).root);
        expect(s.children.map(c => asLeaf(c).id)).toEqual(['g1', 'g2', 'g3']);
        expect(s.sizes[0]).toBeCloseTo(0.4, 10);
        expect(s.sizes[1]).toBeCloseTo(0.15, 10);
        expect(s.sizes[2]).toBeCloseTo(0.45, 10);
    });

    it('fills in missing size entries while merging a nested same-direction split', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [emptyTab('t1')]),
                mkSplit('s2', 'row', [mkLeaf('g2', [emptyTab('t2')]), mkLeaf('g3', [emptyTab('t3')])], []),
            ], []),
            'g1',
        );
        const s = asSplit(normalize(l).root);
        expect(s.children.map(c => asLeaf(c).id)).toEqual(['g1', 'g2', 'g3']);
        // Missing entries fall back to an equal share of their own split:
        // 0.5 for g1, 0.5 × 0.5 for each of g2/g3, then renormalized.
        expect(s.sizes[0]).toBeCloseTo(0.5, 10);
        expect(s.sizes[1]).toBeCloseTo(0.25, 10);
        expect(s.sizes[2]).toBeCloseTo(0.25, 10);
    });

    it('fills in missing size entries', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [emptyTab('t1')]), mkLeaf('g2', [emptyTab('t2')])], [0.6]),
            'g1',
        );
        const s = asSplit(normalize(l).root);
        expect(s.sizes[0]).toBeCloseTo(0.6 / 1.1, 10);
        expect(s.sizes[1]).toBeCloseTo(0.5 / 1.1, 10);
    });

    it('falls back to equal shares when the kept sizes sum to zero', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [emptyTab('t1')]),
                mkLeaf('g2', []),
                mkLeaf('g3', [emptyTab('t3')]),
            ], [0, 0, 0]),
            'g1',
        );
        expect(asSplit(normalize(l).root).sizes).toEqual([0.5, 0.5]);
    });

    it('keeps one tab-less leaf when every leaf empties', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', []), mkLeaf('g2', [])], [0.5, 0.5]),
            'g1',
        );
        const next = normalize(l);
        const leaf = asLeaf(next.root);
        expect(leaf.tabs).toEqual([]);
        expect(next.activeLeafId).toBe(leaf.id);
    });

    it('repairs a stale or missing activeTabId to the last tab', () => {
        const stale = mkLayout(mkLeaf('g1', [emptyTab('t1'), emptyTab('t2')], 'gone'));
        expect(asLeaf(normalize(stale).root).activeTabId).toBe('t2');
        const none = mkLayout(mkLeaf('g1', [emptyTab('t1'), emptyTab('t2')], null));
        expect(asLeaf(normalize(none).root).activeTabId).toBe('t2');
    });

    it('repairs a stale activeLeafId to the first surviving leaf', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [emptyTab('t1')]), mkLeaf('g2', [emptyTab('t2')])], [0.5, 0.5]),
            'gone',
        );
        expect(normalize(l).activeLeafId).toBe('g1');
    });

    it('preserves the Files-pane descriptor across repairs', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')], 'gone'), 'g1', { collapsed: true, currentFolder: 'docs' });
        expect(normalize(l).filesPane).toBe(l.filesPane);
    });
});

describe('reconcile', () => {
    it('drops tabs whose file vanished from the cell', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'gone.md')], 't2'));
        const next = reconcile(l, ctx({ filePaths: ['a.md'] }));
        expect(tabIdsOf(next, 'g1')).toEqual(['t1']);
        expect(leafOf(next, 'g1').activeTabId).toBe('t1');
    });

    it('accepts the file list as a Set', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'gone.md')]));
        const next = reconcile(l, ctx({ filePaths: new Set(['a.md']) }));
        expect(tabIdsOf(next, 'g1')).toEqual(['t1']);
    });

    it('collapses a leaf emptied by a vanished file', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [fileTab('t1', 'gone.md')]), mkLeaf('g2', [fileTab('t2', 'a.md')])], [0.5, 0.5]),
            'g1',
        );
        const next = reconcile(l, ctx({ filePaths: ['a.md'] }));
        expect(asLeaf(next.root).id).toBe('g2');
        expect(next.activeLeafId).toBe('g2');
    });

    it('focusPinned activates the newly pinned tab even over real content (the surface swap)', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'conversation.chat'), fileTab('t2', 'session.terminal')], 't1'));
        const next = reconcile(l, ctx({
            filePaths: ['conversation.chat', 'session.terminal'],
            pinnedPath: 'session.terminal',
            focusPinned: true,
        }));
        expect(leafOf(next, 'g1').activeTabId).toBe('t2');
    });

    it('an unchanged pin (no focusPinned) never steals focus from real content', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'conversation.chat'), fileTab('t2', 'session.terminal')], 't1'));
        const next = reconcile(l, ctx({
            filePaths: ['conversation.chat', 'session.terminal'],
            pinnedPath: 'session.terminal',
        }));
        expect(leafOf(next, 'g1').activeTabId).toBe('t1');
    });

    it('focusPinned creates AND activates a missing pinned tab', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md')], 't1'));
        const next = reconcile(l, ctx({
            filePaths: ['a.md', 'status.chat'],
            pinnedPath: 'status.chat',
            focusPinned: true,
        }));
        const tabs = leafOf(next, 'g1').tabs;
        expect(tabs[0]).toMatchObject({ kind: 'file', path: 'status.chat' });
        expect(leafOf(next, 'g1').activeTabId).toBe(tabs[0].id);
    });

    // ── unpinnedPath: the pin leaving a file takes its tab with it ──────
    // The pin moving in full: the user re-pins from one file to another, and
    // should end up looking at the newly pinned file with no parked tab of
    // the old one left behind.

    it('unpinnedPath closes the tab the pin moved off, and focuses the new one', () => {
        const l = mkLayout(mkLeaf('g1', [
            fileTab('t1', 'conversation.chat'),
            fileTab('t2', 'notes.md'),
        ], 't1'));
        const next = reconcile(l, ctx({
            filePaths: ['conversation.chat', 'notes.md', 'status.chat'],
            pinnedPath: 'status.chat',
            unpinnedPath: 'conversation.chat',
            focusPinned: true,
        }));
        const tabs = leafOf(next, 'g1').tabs;
        // The chat tab is gone; the status tab took the head and the focus.
        expect(tabs.map(t => (t.kind === 'file' ? t.path : t.kind)))
            .toEqual(['status.chat', 'notes.md']);
        expect(leafOf(next, 'g1').activeTabId).toBe(tabs[0].id);
    });

    it('swapping the cell\'s ONLY tab leaves just the new surface (no stray empty tab)', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'conversation.chat')], 't1'));
        const next = reconcile(l, ctx({
            filePaths: ['conversation.chat', 'status.chat'],
            pinnedPath: 'status.chat',
            unpinnedPath: 'conversation.chat',
            focusPinned: true,
        }));
        const leaf = asLeaf(next.root);
        expect(leaf.tabs).toHaveLength(1);
        expect(leaf.tabs[0]).toMatchObject({ kind: 'file', path: 'status.chat' });
        expect(leaf.activeTabId).toBe(leaf.tabs[0].id);
        expect(next.activeLeafId).toBe(leaf.id);
    });

    it('a manual unpin to nothing closes the tab (and reseeds when it was the last)', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'pin.md'), fileTab('t2', 'a.md')], 't1'));
        const next = reconcile(l, ctx({
            filePaths: ['pin.md', 'a.md'],
            pinnedPath: null,
            unpinnedPath: 'pin.md',
        }));
        expect(tabIdsOf(next, 'g1')).toEqual(['t2']);
        expect(leafOf(next, 'g1').activeTabId).toBe('t2');

        const only = mkLayout(mkLeaf('g1', [fileTab('t1', 'pin.md')], 't1'));
        const reseeded = asLeaf(reconcile(only, ctx({
            filePaths: ['pin.md'],
            pinnedPath: null,
            unpinnedPath: 'pin.md',
        })).root);
        expect(reseeded.tabs.map(t => t.kind)).toEqual(['empty']);
        expect(reseeded.activeTabId).toBe(reseeded.tabs[0].id);
    });

    it('an unpinnedPath with no open tab changes nothing (same object)', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md')], 't1'));
        expect(reconcile(l, ctx({
            filePaths: ['a.md', 'conversation.chat'],
            unpinnedPath: 'conversation.chat',
        }))).toBe(l);
    });

    it('closing the unpinned tab collapses the split leaf it was alone in', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [fileTab('t1', 'a.md')]),
                mkLeaf('g2', [fileTab('t2', 'conversation.chat')], 't2'),
            ], [0.5, 0.5]),
            'g2',
        );
        const next = reconcile(l, ctx({
            filePaths: ['a.md', 'conversation.chat'],
            pinnedPath: null,
            unpinnedPath: 'conversation.chat',
        }));
        const leaf = asLeaf(next.root);
        expect(leaf.id).toBe('g1');
        expect(leaf.tabs.map(t => t.id)).toEqual(['t1']);
        expect(next.activeLeafId).toBe('g1');
        expect(leaf.activeTabId).toBe('t1');
    });

    it('the unpinned tab being ACTIVE in a split leaf leaves that leaf on a neighbour', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [
                mkLeaf('g1', [fileTab('t1', 'a.md')]),
                mkLeaf('g2', [
                    fileTab('t2', 'b.md'),
                    fileTab('t3', 'conversation.chat'),
                    fileTab('t4', 'c.md'),
                ], 't3'),
            ], [0.5, 0.5]),
            'g2',
        );
        const next = reconcile(l, ctx({
            filePaths: ['a.md', 'b.md', 'c.md', 'conversation.chat', 'status.chat'],
            pinnedPath: 'status.chat',
            unpinnedPath: 'conversation.chat',
            focusPinned: true,
        }));
        // The split survives; g2 falls back to the previous-index neighbour…
        expect(tabIdsOf(next, 'g2')).toEqual(['t2', 't4']);
        expect(leafOf(next, 'g2').activeTabId).toBe('t2');
        // …while the newly pinned tab opens at the head of the FIRST leaf and
        // takes the cell's focus with it.
        const head = leafOf(next, 'g1').tabs[0];
        expect(head).toMatchObject({ kind: 'file', path: 'status.chat' });
        expect(next.activeLeafId).toBe('g1');
        expect(leafOf(next, 'g1').activeTabId).toBe(head.id);
    });

    it('an unpinned file deleted in the same update is handled once, not twice', () => {
        const l = mkLayout(mkLeaf('g1', [
            fileTab('t1', 'conversation.chat'),
            fileTab('t2', 'a.md'),
        ], 't1'));
        const next = reconcile(l, ctx({
            // conversation.chat is gone from the cell AND unpinned.
            filePaths: ['a.md', 'status.chat'],
            pinnedPath: 'status.chat',
            unpinnedPath: 'conversation.chat',
            focusPinned: true,
        }));
        const tabs = leafOf(next, 'g1').tabs;
        expect(tabs.map(t => (t.kind === 'file' ? t.path : t.kind)))
            .toEqual(['status.chat', 'a.md']);
        expect(leafOf(next, 'g1').activeTabId).toBe(tabs[0].id);
    });

    it('unpinnedPath equal to pinnedPath (a no-op pin change) keeps the tab', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'pin.md')], 't1'));
        expect(reconcile(l, ctx({
            filePaths: ['pin.md'],
            pinnedPath: 'pin.md',
            unpinnedPath: 'pin.md',
        }))).toBe(l);
    });

    it('without unpinnedPath a pin change still parks the old tab (unchanged behaviour)', () => {
        const l = mkLayout(mkLeaf('g1', [
            fileTab('t1', 'conversation.chat'),
            fileTab('t2', 'status.chat'),
        ], 't1'));
        const next = reconcile(l, ctx({
            filePaths: ['conversation.chat', 'status.chat'],
            pinnedPath: 'status.chat',
            focusPinned: true,
        }));
        expect(tabIdsOf(next, 'g1')).toEqual(['t1', 't2']);
        expect(leafOf(next, 'g1').activeTabId).toBe('t2');
    });

    it('R3: ensures the pinned file tab at the head of the first leaf', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [fileTab('t1', 'a.md')]), mkLeaf('g2', [fileTab('t2', 'b.md')])], [0.5, 0.5]),
            'g2',
        );
        const next = reconcile(l, ctx({ filePaths: ['a.md', 'b.md', 'pin.md'], pinnedPath: 'pin.md' }));
        const tabs = leafOf(next, 'g1').tabs;
        expect(tabs[0]).toMatchObject({ kind: 'file', path: 'pin.md' });
        expect(tabs.map(t => t.kind)).toEqual(['file', 'file']);
        // Focus stays where it was — the active tab was real content.
        expect(next.activeLeafId).toBe('g2');
        expect(leafOf(next, 'g1').activeTabId).toBe('t1');
    });

    it('does not duplicate a pinned tab that is already open in another leaf', () => {
        const l = mkLayout(
            mkSplit('s1', 'row', [mkLeaf('g1', [fileTab('t1', 'a.md')]), mkLeaf('g2', [fileTab('t2', 'pin.md')])], [0.5, 0.5]),
            'g1',
        );
        const next = reconcile(l, ctx({ filePaths: ['a.md', 'pin.md'], pinnedPath: 'pin.md' }));
        expect(next).toBe(l);
    });

    it('R3: steals focus from an empty tab when a pinned tab appears', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        const next = reconcile(l, ctx({ filePaths: ['pin.md'], pinnedPath: 'pin.md' }));
        const tabs = leafOf(next, 'g1').tabs;
        expect(tabs.map(t => t.kind)).toEqual(['file', 'empty']);
        expect(leafOf(next, 'g1').activeTabId).toBe(tabs[0].id);
    });

    it('R3: never steals focus from a real content tab', () => {
        const l = mkLayout(mkLeaf('g1', [historyTab('t1', 'old.md')]));
        const next = reconcile(l, ctx({ filePaths: ['pin.md'], pinnedPath: 'pin.md' }));
        expect(leafOf(next, 'g1').activeTabId).toBe('t1');
    });

    it('adopts the ensured tab when the tree has no focused tab at all', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'gone.md')]));
        const next = reconcile(l, ctx({ filePaths: ['pin.md'], pinnedPath: 'pin.md' }));
        const leaf = asLeaf(next.root);
        expect(leaf.tabs).toHaveLength(1);
        expect(leaf.tabs[0]).toMatchObject({ kind: 'file', path: 'pin.md' });
        expect(leaf.activeTabId).toBe(leaf.tabs[0].id);
    });

    it('R4: reseeds an empty tab into a tab-less tree', () => {
        const l = mkLayout(mkLeaf('g1', []));
        const next = reconcile(l, ctx());
        const leaf = asLeaf(next.root);
        expect(leaf.tabs.map(t => t.kind)).toEqual(['empty']);
        expect(leaf.activeTabId).toBe(leaf.tabs[0].id);
        expect(next.activeLeafId).toBe(leaf.id);
    });

    it('returns the SAME object when nothing needs reconciling', () => {
        const c = ctx({ filePaths: ['a.md'], pinnedPath: 'a.md' });
        const l = createDefaultLayout(c);
        expect(reconcile(l, c)).toBe(l);
    });

    it('is idempotent', () => {
        const c = ctx({ filePaths: ['pin.md', 'a.md'], pinnedPath: 'pin.md' });
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1'), fileTab('t2', 'gone.md')], 't1'));
        const once = reconcile(l, c);
        expect(reconcile(once, c)).toBe(once);
    });
});

describe('share floor across structural ops', () => {
    it('repeated same-direction splits never sink a child below MIN_LEAF_SHARE', () => {
        // Splitting an already-quartered leaf used to mint 0.125 shares whose
        // sash the resize handler refuses to move (pair < 2×MIN).
        let l = mkLayout(mkLeaf('g1', [
            fileTab('t1', 'a.md'), fileTab('t2', 'b.md'), fileTab('t3', 'c.md'), fileTab('t4', 'd.md'),
        ], 't1'));
        l = splitWithTab(l, 'g1', 't2', 'right');
        l = splitWithTab(l, 'g1', 't3', 'right');
        l = splitWithTab(l, 'g1', 't4', 'right');
        const root = l.root as ContentSplit;
        expect(root.type).toBe('split');
        expect(root.children).toHaveLength(4);
        for (const share of root.sizes) expect(share).toBeGreaterThanOrEqual(MIN_LEAF_SHARE - 1e-9);
        expect(root.sizes.reduce((a, b) => a + b, 0)).toBeCloseTo(1, 10);
    });

    it('keeps an entry floored in an earlier pass on the floor (floored set is sticky)', () => {
        // A share floored in one pass used to be scaled back under the floor
        // in the next, e.g. [0.05, 0.16, 0.79] → [0.15, 0.1498, 0.7002].
        for (const sizes of [[0.05, 0.16, 0.79], [0.01, 0.155, 0.155, 0.68]]) {
            const l = mkLayout(mkSplit('s1', 'row', sizes.map((_, i) => mkLeaf(`g${i}`, [emptyTab(`t${i}`)])), sizes.map(() => 1 / sizes.length)));
            const s = asSplit(setSplitSizes(l, 's1', sizes).root);
            for (const share of s.sizes) expect(share).toBeGreaterThanOrEqual(MIN_LEAF_SHARE - 1e-9);
            expect(s.sizes.reduce((a, b) => a + b, 0)).toBeCloseTo(1, 10);
        }
    });
});

describe('setTabRenderer / "Open with"', () => {
    it('sets, replaces and clears a file tab renderer override', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md')]));
        const set = setTabRenderer(l, 't1', 'code-editor');
        expect(leafOf(set, 'g1').tabs[0]).toMatchObject({ id: 't1', rendererId: 'code-editor' });
        const swapped = setTabRenderer(set, 't1', 'text');
        expect(leafOf(swapped, 'g1').tabs[0]).toMatchObject({ rendererId: 'text' });
        const cleared = setTabRenderer(swapped, 't1', undefined);
        expect('rendererId' in leafOf(cleared, 'g1').tabs[0]).toBe(false);
    });

    it('is reference-stable on no-op and refuses non-file tabs', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), emptyTab('t2')]));
        expect(setTabRenderer(l, 't1', undefined)).toBe(l);
        expect(setTabRenderer(l, 't2', 'code-editor')).toBe(l);
        expect(setTabRenderer(l, 'nope', 'code-editor')).toBe(l);
    });

    it('openFileTab carries an override into a new tab and retargets an open one', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        const opened = openFileTab(l, 'a.md', null, 'code-editor');
        const tab = leafOf(opened, 'g1').tabs[0];
        expect(tab).toMatchObject({ kind: 'file', path: 'a.md', rendererId: 'code-editor' });
        // Open-with on the already-open file switches the renderer in place
        // (one file = one tab), keeping the tab id.
        const retargeted = openFileTab(opened, 'a.md', null, 'text');
        expect(leafOf(retargeted, 'g1').tabs[0]).toMatchObject({ id: tab.id, rendererId: 'text' });
        // A plain re-open (no override argument) keeps the user's choice.
        expect(openFileTab(retargeted, 'a.md', null)).toBe(activateTab(retargeted, tab.id));
    });

    it('renameFileTab keeps the override through a rename', () => {
        const l = setTabRenderer(mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md')])), 't1', 'code-editor');
        const renamed = renameFileTab(l, 'a.md', 'b.md');
        expect(leafOf(renamed, 'g1').tabs[0]).toMatchObject({ path: 'b.md', rendererId: 'code-editor' });
    });
});

describe('renameFileTab', () => {
    it('retargets the tab in place, keeping its id and activation', () => {
        const l = mkLayout(mkSplit('s1', 'row', [
            mkLeaf('g1', [fileTab('t1', 'old.md')], 't1'),
            mkLeaf('g2', [fileTab('t2', 'other.md')]),
        ]));
        const next = renameFileTab(l, 'old.md', 'new.md');
        expect(leafOf(next, 'g1').tabs[0]).toMatchObject({ id: 't1', kind: 'file', path: 'new.md' });
        expect(leafOf(next, 'g1').activeTabId).toBe('t1');
        expect(leafOf(next, 'g2')).toBe(leafOf(l, 'g2'));
    });

    it('is reference-stable when no tab matches', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md')]));
        expect(renameFileTab(l, 'nope.md', 'x.md')).toBe(l);
    });
});

describe('history tabs (kind: history)', () => {
    const historyCtx = (paths: string[]): LayoutCellContext => ({
        filePaths: new Set(paths),
        pinnedPath: null,
    });

    it('openHistoryTab is a singleton per (category, path) and focuses on re-open', () => {
        let l = createDefaultLayout(historyCtx(['a.txt']));
        l = openHistoryTab(l, 'file', 'a.txt');
        l = openHistoryTab(l, 'config', 'run.yaml');
        l = openHistoryTab(l, 'file', 'a.txt'); // focuses, no duplicate
        const hist = allTabs(l).filter(t => t.kind === 'history');
        expect(hist).toHaveLength(2);
        // Same path in a DIFFERENT category is a distinct tab.
        l = openHistoryTab(l, 'log', 'a.txt');
        expect(allTabs(l).filter(t => t.kind === 'history')).toHaveLength(3);
    });

    it('survives reconcile — a history tab is never auto-closed by file changes', () => {
        let l = createDefaultLayout(historyCtx(['a.txt']));
        l = openHistoryTab(l, 'file', 'gone.txt');
        const next = reconcile(l, historyCtx(['a.txt'])); // gone.txt not in files
        expect(allTabs(next).some(t => t.kind === 'history' && t.path === 'gone.txt')).toBe(true);
    });

    it('is invisible to openFilePaths, never pinned, and not retargeted by rename', () => {
        let l = createDefaultLayout(historyCtx(['a.txt']));
        l = openHistoryTab(l, 'file', 'a.txt');
        expect(openFilePaths(l)).toEqual([]);
        const tab = allTabs(l).find(t => t.kind === 'history')!;
        expect(isTabPinned(tab, 'a.txt')).toBe(false);
        l = renameFileTab(l, 'a.txt', 'b.txt');
        expect(allTabs(l).some(t => t.kind === 'history' && t.path === 'a.txt')).toBe(true);
    });

    it('closeHistoryTabs sweeps every history tab and reseeds when the tree empties', () => {
        let l = createDefaultLayout(historyCtx([]));
        l = openHistoryTab(l, 'config', 'run.yaml');
        l = openHistoryTab(l, 'log', 'logs.txt');
        const swept = closeHistoryTabs(l);
        expect(allTabs(swept).some(t => t.kind === 'history')).toBe(false);
        expect(allTabs(swept).length).toBeGreaterThan(0); // never tab-less
        // No-op (reference-stable) when nothing to sweep.
        expect(closeHistoryTabs(swept)).toBe(swept);
    });

    it('setTabRenderer works on history tabs ("Open with"), and clearing keeps the kind', () => {
        let l = createDefaultLayout(historyCtx(['a.txt']));
        l = openHistoryTab(l, 'file', 'a.txt');
        const tab = allTabs(l).find(t => t.kind === 'history')!;
        l = setTabRenderer(l, tab.id, 'code-editor');
        let after = allTabs(l).find(t => t.id === tab.id)!;
        expect(after.kind === 'history' && after.rendererId === 'code-editor').toBe(true);
        l = setTabRenderer(l, tab.id, undefined);
        after = allTabs(l).find(t => t.id === tab.id)!;
        expect(after.kind === 'history' && after.rendererId === undefined
            && (after.kind === 'history' ? after.category : '') === 'file').toBe(true);
    });
});

describe('compare tabs (kind: compare)', () => {
    const side = (cellId: string, fileName: string, label = `Work / ${cellId}`) => ({ channel: 'ch', cellId, fileName, label });
    const req = (baseCell: string, baseFile = 'a.md') => ({ base: side(baseCell, baseFile), current: side('me', 'a.md') });
    const compareTabs = (l: CellLayout) => allTabs(l).filter(t => t.kind === 'compare');

    it('opens after the active tab and takes the focus', () => {
        const l = mkLayout(mkLeaf('g', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')], 't1'));
        const next = openCompareTab(l, req('other'));
        const tabs = leafOf(next, 'g').tabs;
        expect(tabs.map(t => t.kind)).toEqual(['file', 'compare', 'file']);
        expect(activeTab(next)).toMatchObject({ kind: 'compare', base: { cellId: 'other' }, current: { cellId: 'me', fileName: 'a.md' } });
    });

    it('takes over an active empty tab, and lands in the active leaf of a split', () => {
        const single = openCompareTab(mkLayout(mkLeaf('g', [emptyTab('e')])), req('other'));
        expect(leafOf(single, 'g').tabs.map(t => t.kind)).toEqual(['compare']);
        const split = mkLayout(mkSplit('s', 'row', [mkLeaf('g1', [fileTab('t1', 'a.md')]), mkLeaf('g2', [fileTab('t2', 'b.md')])]), 'g2');
        const next = openCompareTab(split, req('other'));
        expect(tabIdsOf(next, 'g1')).toEqual(['t1']);
        expect(leafOf(next, 'g2').tabs.map(t => t.kind)).toEqual(['file', 'compare']);
        expect(next.activeLeafId).toBe('g2');
    });

    it('a second compare replaces the open one in its slot, with a fresh id, and focuses it', () => {
        const split = mkLayout(mkSplit('s', 'row', [
            mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')], 't1'),
            mkLeaf('g2', [fileTab('t3', 'c.md')]),
        ]), 'g1');
        const first = openCompareTab(split, req('one'));
        const firstId = compareTabs(first)[0].id;
        // Focus moves elsewhere; the next compare still replaces the open one where it is.
        const moved = activateTab(first, 't3');
        const second = openCompareTab(moved, req('two'));
        expect(compareTabs(second)).toHaveLength(1);
        const tabs = leafOf(second, 'g1').tabs;
        expect(tabs.map(t => t.kind)).toEqual(['file', 'compare', 'file']);
        expect(tabs[1].id).not.toBe(firstId);
        expect(tabs[1]).toMatchObject({ base: { cellId: 'two' } });
        expect(second.activeLeafId).toBe('g1');
        expect(activeTab(second)?.id).toBe(tabs[1].id);
    });

    it('the same pair again only focuses the open tab (reference-stable when already focused)', () => {
        const open = openCompareTab(mkLayout(mkLeaf('g', [fileTab('t1', 'a.md')])), req('other'));
        expect(openCompareTab(open, req('other'))).toBe(open);
        const away = activateTab(open, 't1');
        const back = openCompareTab(away, req('other'));
        expect(compareTabs(back).map(t => t.id)).toEqual(compareTabs(open).map(t => t.id));
        expect(activeTab(back)?.kind).toBe('compare');
        // A renamed base cell reads as another side: the tab is replaced to carry the new label.
        const relabelled = openCompareTab(open, { ...req('other'), base: side('other', 'a.md', 'Work / renamed') });
        expect(compareTabs(relabelled)[0].id).not.toBe(compareTabs(open)[0].id);
    });

    it('reconcile keeps it while its file stands and drops it, focus falling back, once the file is gone', () => {
        const l = openCompareTab(mkLayout(mkLeaf('g', [fileTab('t1', 'b.md'), fileTab('t2', 'a.md')], 't1')), req('other'));
        expect(reconcile(l, ctx({ filePaths: ['a.md', 'b.md'] }))).toBe(l);
        const gone = reconcile(renameFileTab(l, 'a.md', 'z.md'), ctx({ filePaths: ['z.md', 'b.md'] }));
        expect(compareTabs(gone)).toEqual([]);
        expect(activeTab(gone)?.id).toBe('t1');
    });

    it('is no open file, never pinned and never retargeted by Open with', () => {
        const l = openCompareTab(mkLayout(mkLeaf('g', [fileTab('t1', 'b.md')])), req('other'));
        const tab = compareTabs(l)[0];
        expect(openFilePaths(l)).toEqual(['b.md']);
        expect(isTabPinned(tab, 'a.md')).toBe(false);
        expect(setTabRenderer(l, tab.id, 'code-editor')).toBe(l);
    });

    it('closes like any tab, reseeding an empty tab when it was the last; a clone for another cell leaves it out', () => {
        const only = openCompareTab(mkLayout(mkLeaf('g', [emptyTab('e')])), req('other'));
        const closed = closeTab(only, compareTabs(only)[0].id, null);
        expect(allTabs(closed).map(t => t.kind)).toEqual(['empty']);
        const l = openCompareTab(mkLayout(mkLeaf('g', [fileTab('t1', 'b.md')])), req('other'));
        expect(allTabs(cloneLayout(l, () => false)).map(t => t.kind)).toEqual(['file']);
    });
});

// ── Several runs per cell ───────────────────────────────────────────────
// Synthetic run folders: `_runs/<label>/…`, labels owned by two fake runs.
const A = '_runs/alpha-11111111';
const B = '_runs/beta-22222222';
const ownerOf = (live: string[]) => (p: string): string | null => {
    const m = p.match(/^_runs\/([^/]+)\//);
    return m && live.includes(m[1]) ? m[1] : null;
};

describe('openFileTab — background', () => {
    it('opens a new tab after the active one without activating it', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')], 't1'));
        const next = openFileTab(l, 'c.md', null, undefined, { background: true });
        const leaf = leafOf(next, 'g1');
        expect(leaf.tabs.map(t => t.kind === 'file' ? t.path : t.kind)).toEqual(['a.md', 'c.md', 'b.md']);
        expect(leaf.activeTabId).toBe('t1');
    });

    it('leaves an already-open tab unfocused', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', 'b.md')], 't1'));
        expect(openFileTab(l, 'b.md', null, undefined, { background: true })).toBe(l);
    });
});

describe('revealFileTab — a run shows its file without stealing focus', () => {
    const opts = (runLabel: string, activate?: boolean) => ({
        runLabel, pinnedPath: null, lockOwnerOf: ownerOf(['alpha-11111111', 'beta-22222222']), activate,
    });

    it('takes over an empty active tab', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        const leaf = leafOf(revealFileTab(l, `${A}/chat.chat`, opts('alpha-11111111')), 'g1');
        expect(leaf.tabs).toHaveLength(1);
        expect(leaf.tabs[0]).toMatchObject({ kind: 'file', path: `${A}/chat.chat` });
        expect(leaf.activeTabId).toBe(leaf.tabs[0].id);
    });

    it("a surface takes over its own run's card in place", () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t0', 'notes.md'), fileTab('t1', `${A}/alpha-11111111.run`)], 't1'));
        const leaf = leafOf(revealFileTab(l, `${A}/chat.chat`, opts('alpha-11111111')), 'g1');
        expect(leaf.tabs.map(t => t.kind === 'file' ? t.path : t.kind)).toEqual(['notes.md', `${A}/chat.chat`]);
        expect(leaf.activeTabId).toBe(leaf.tabs[1].id);
    });

    it('a second file of the same run opens beside it, focused', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', `${A}/tab-1.webview`)]));
        const leaf = leafOf(revealFileTab(l, `${A}/tab-2.webview`, opts('alpha-11111111')), 'g1');
        expect(leaf.tabs.map(t => t.kind === 'file' ? t.path : t.kind)).toEqual([`${A}/tab-1.webview`, `${A}/tab-2.webview`]);
        expect(leaf.activeTabId).toBe(leaf.tabs[1].id);
    });

    it("opens in the background beside another run's file or the user's file", () => {
        for (const active of [`${B}/term.terminal`, 'notes.md']) {
            const l = mkLayout(mkLeaf('g1', [fileTab('t1', active)]));
            const leaf = leafOf(revealFileTab(l, `${A}/chat.chat`, opts('alpha-11111111')), 'g1');
            expect(leaf.tabs.map(t => t.kind === 'file' ? t.path : t.kind)).toEqual([active, `${A}/chat.chat`]);
            expect(leaf.activeTabId).toBe('t1');
        }
    });

    it('a non-file active tab keeps its place; the file opens beside it, focused', () => {
        const l = mkLayout(mkLeaf('g1', [historyTab('t1', 'old.md')]));
        const leaf = leafOf(revealFileTab(l, `${A}/chat.chat`, opts('alpha-11111111')), 'g1');
        expect(leaf.tabs.map(t => t.kind)).toEqual(['history', 'file']);
        expect(leaf.activeTabId).toBe(leaf.tabs[1].id);
    });

    it('activate: true focuses a new tab beside real content, never replacing it', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', 'notes.md')]));
        const leaf = leafOf(revealFileTab(l, `${A}/alpha-11111111.run`, opts('alpha-11111111', true)), 'g1');
        expect(leaf.tabs.map(t => t.kind === 'file' ? t.path : t.kind)).toEqual(['notes.md', `${A}/alpha-11111111.run`]);
        expect(leaf.activeTabId).toBe(leaf.tabs[1].id);
    });

    it('activate: false opens in the background even over an empty tab', () => {
        const l = mkLayout(mkLeaf('g1', [emptyTab('t1')]));
        const leaf = leafOf(revealFileTab(l, `${A}/alpha-11111111.run`, opts('alpha-11111111', false)), 'g1');
        expect(leaf.tabs.map(t => t.kind)).toEqual(['empty', 'file']);
        expect(leaf.activeTabId).toBe('t1');
    });

    it('an already-open tab is focused by the rule, left alone otherwise', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', `${A}/chat.chat`), emptyTab('t2')], 't2'));
        expect(leafOf(revealFileTab(l, `${A}/chat.chat`, opts('alpha-11111111')), 'g1').activeTabId).toBe('t1');
        const busy = mkLayout(mkLeaf('g1', [fileTab('t1', `${A}/chat.chat`), fileTab('t2', 'notes.md')], 't2'));
        expect(revealFileTab(busy, `${A}/chat.chat`, opts('alpha-11111111'))).toBe(busy);
    });
});

describe('reconcile — a vanished active tab falls back as a close does', () => {
    it("lands on the previous survivor, whichever run owns it (C14)", () => {
        const l = mkLayout(mkLeaf('g1', [
            fileTab('t1', 'notes.md'), fileTab('t2', `${B}/term.terminal`), fileTab('t3', `${A}/chat.chat`),
        ], 't3'));
        const next = reconcile(l, ctx({ filePaths: ['notes.md', `${B}/term.terminal`] }));
        expect(tabIdsOf(next, 'g1')).toEqual(['t1', 't2']);
        expect(leafOf(next, 'g1').activeTabId).toBe('t2');
    });

    it('takes the next survivor when the first tab vanishes (C14)', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', `${A}/chat.chat`), fileTab('t2', `${B}/b.terminal`)], 't1'));
        const next = reconcile(l, ctx({ filePaths: [`${B}/b.terminal`] }));
        expect(tabIdsOf(next, 'g1')).toEqual(['t2']);
        expect(leafOf(next, 'g1').activeTabId).toBe('t2');
    });

    it('a non-active vanished tab leaves the focus alone (C14)', () => {
        const l = mkLayout(mkLeaf('g1', [fileTab('t1', `${B}/term.terminal`), fileTab('t2', `${A}/chat.chat`)], 't1'));
        const next = reconcile(l, ctx({ filePaths: [`${B}/term.terminal`] }));
        expect(tabIdsOf(next, 'g1')).toEqual(['t1']);
        expect(leafOf(next, 'g1').activeTabId).toBe('t1');
    });
});

describe('cloneLayout', () => {
    it("copies splits and tabs with fresh ids, minus dropped and history tabs", () => {
        const l = mkLayout(mkSplit('s1', 'row', [
            mkLeaf('g1', [fileTab('t1', 'a.md'), fileTab('t2', `${A}/chat.chat`)], 't2'),
            mkLeaf('g2', [fileTab('t3', 'b.md'), { id: 't4', kind: 'history', category: 'file', path: 'b.md' }], 't3'),
        ], [0.3, 0.7]), 'g2');
        const c = cloneLayout(l, t => t.kind === 'file' && t.path.startsWith('_runs/'));
        const split = asSplit(c.root);
        expect(split.id).not.toBe('s1');
        expect(split.sizes).toEqual([0.3, 0.7]);
        const [left, right] = split.children.map(asLeaf);
        expect(left.tabs.map(t => t.kind === 'file' ? t.path : t.kind)).toEqual(['a.md']);
        expect(right.tabs.map(t => t.kind === 'file' ? t.path : t.kind)).toEqual(['b.md']);
        expect(left.tabs[0].id).not.toBe('t1');
        // The dropped active tab falls back; the active leaf follows its copy.
        expect(left.activeTabId).toBe(left.tabs[0].id);
        expect(c.activeLeafId).toBe(right.id);
        expect(activeTab(c)).toMatchObject({ kind: 'file', path: 'b.md' });
    });

    it('a leaf left empty is pruned, and a layout left empty gets an empty tab', () => {
        const l = mkLayout(mkSplit('s1', 'row', [
            mkLeaf('g1', [fileTab('t1', `${A}/chat.chat`)]),
            mkLeaf('g2', [fileTab('t2', 'b.md')]),
        ]), 'g1');
        const c = cloneLayout(l, t => t.kind === 'file' && t.path.startsWith('_runs/'));
        expect(asLeaf(c.root).tabs.map(t => t.kind === 'file' ? t.path : t.kind)).toEqual(['b.md']);
        const all = cloneLayout(l, () => true);
        expect(asLeaf(all.root).tabs.map(t => t.kind)).toEqual(['empty']);
    });
});

describe('pane sizing beside the content', () => {
    const l = () => mkLayout(mkLeaf('g1', [emptyTab('t1')]));

    it('the Files pane leaves the content its minimum', () => {
        const r = paneReserve(setFilesPane(l(), { collapsed: false }), 'files', 'x');
        expect(r.css).toBe(`${CONTENT_MIN_WIDTH}px`);
        expect(r.px(500)).toBe(CONTENT_MIN_WIDTH);
        expect(paneReserve(l(), 'files', 'y').px(500)).toBe(CONTENT_MIN_HEIGHT);
    });

    it('the runs pane also leaves a shown Files pane on its axis its share', () => {
        const shown = setFilesPane(l(), { collapsed: false, width: 194 });
        const r = paneReserve(shown, 'runs', 'x');
        expect(r.css).toBe(`${CONTENT_MIN_WIDTH}px + min(194px, (100% - ${CONTENT_MIN_WIDTH}px) / 2)`);
        // Room to spare: the Files pane keeps its whole width.
        expect(r.px(800)).toBe(CONTENT_MIN_WIDTH + 194);
        // A narrow cell: the two panes split what the content leaves.
        expect(r.px(328)).toBe(CONTENT_MIN_WIDTH + (328 - CONTENT_MIN_WIDTH) / 2);
        // Hidden, or across the other axis, the Files pane takes nothing.
        expect(paneReserve(setFilesPane(shown, { collapsed: true }), 'runs', 'x').px(328)).toBe(CONTENT_MIN_WIDTH);
        expect(paneReserve(setFilesPane(shown, { side: 'top' }), 'runs', 'x').px(328)).toBe(CONTENT_MIN_WIDTH);
    });

    it('lets the pane minimum give way to the reserve', () => {
        expect(paneSizeStyle('x', 280, 200, '160px')).toEqual({
            width: 280,
            minWidth: 'min(200px, min(66.6667%, 100% - 160px))',
            maxWidth: 'min(66.6667%, 100% - 160px)',
        });
        expect(paneSizeStyle('y', 200, 120, '120px')).toEqual({
            height: 200,
            minHeight: 'min(120px, min(66.6667%, 100% - 120px))',
            maxHeight: 'min(66.6667%, 100% - 120px)',
        });
    });
});
