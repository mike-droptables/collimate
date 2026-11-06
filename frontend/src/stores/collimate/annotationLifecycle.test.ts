// The annotation slice's store subscriber: the file last viewed per cell
// follows the active file tab, a deferred status-pane open lands once its
// cell is reachable (or expires), and a channel switch abandons Add
// annotation requests and clears the notices.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { PENDING_RUNS_PANE_OPEN_MS } from './annotationLifecycle';
import { activateTab, allTabs, createDefaultLayout, openEmptyTab, openFileTab } from '@/lib/cellLayout';

const store = () => useCollimateStore.getState();
const CH = (id: string) => ({ id, name: id, main_stacks: [{ id: 's', name: '', cells: [{ id: 'c1', name: 'C', files: [] }] }], nav_stacks: [], main_selection_id: null, nav_selection_id: null });

beforeEach(() => {
    useCollimateStore.setState({
        activeChannel: CH('ch'), state: { channels: [CH('ch')] }, cellCache: {}, cellLayouts: {}, cellDisplaysReady: true,
        lastFileTab: {}, pendingRunsPaneOpen: null, runsPaneView: {}, runsPaneFocus: {}, annotationRequests: {}, annotationNotice: {},
    } as never);
});
afterEach(() => { vi.useRealTimers(); });

describe('the file last viewed', () => {
    it('follows the active file tab: an open, another tab, a reveal; an empty tab keeps the file before it', () => {
        let layout = createDefaultLayout({ filePaths: ['a.md', 'b.md'], pinnedPath: null });
        // Hydration: the first layout notes its active file.
        useCollimateStore.setState({ cellLayouts: { c1: openFileTab(layout, 'a.md', null) } });
        expect(store().lastFileTab.c1).toBe('a.md');
        layout = openFileTab(store().cellLayouts.c1, 'b.md', null, undefined, { newTab: true });
        useCollimateStore.setState({ cellLayouts: { c1: layout } });
        expect(store().lastFileTab.c1).toBe('b.md');
        const tabA = allTabs(layout).find(t => t.kind === 'file' && t.path === 'a.md')!;
        useCollimateStore.setState({ cellLayouts: { c1: activateTab(layout, tabA.id) } });
        expect(store().lastFileTab.c1).toBe('a.md');
        const leaf = store().cellLayouts.c1.activeLeafId;
        useCollimateStore.setState({ cellLayouts: { c1: openEmptyTab(store().cellLayouts.c1, leaf) } });
        expect(store().lastFileTab.c1).toBe('a.md');
        // A change elsewhere leaves the cell's entry as it was.
        const before = store().lastFileTab;
        useCollimateStore.setState({ cellLayouts: { ...store().cellLayouts, c2: createDefaultLayout({ filePaths: [], pinnedPath: null }) } });
        expect(store().lastFileTab).toBe(before);
    });
});

describe('a deferred status-pane open', () => {
    it('lands once the channel is active, the displays are loaded and the cell is there', () => {
        useCollimateStore.setState({ activeChannel: CH('other'), cellDisplaysReady: false, cellLayouts: { c1: createDefaultLayout({ filePaths: [], pinnedPath: null }) } });
        useCollimateStore.setState({ pendingRunsPaneOpen: { channel: 'ch', cellId: 'c1', view: { view: 'all', option: 'default' }, focus: 'composer', at: Date.now() } });
        expect(store().pendingRunsPaneOpen).not.toBeNull();
        useCollimateStore.setState({ activeChannel: CH('ch') });
        expect(store().pendingRunsPaneOpen).not.toBeNull();
        useCollimateStore.setState({ cellDisplaysReady: true });
        expect(store().pendingRunsPaneOpen).toBeNull();
        expect(store().runsPaneView.c1).toEqual({ view: 'all', option: 'default' });
        expect(store().runsPaneFocus.c1.target).toBe('composer');
        expect(store().cellLayouts.c1.runsPane.open).toBe(true);
    });

    it('expires after 15 s', () => {
        vi.useFakeTimers();
        useCollimateStore.setState({ activeChannel: CH('other') });
        useCollimateStore.setState({ pendingRunsPaneOpen: { channel: 'ch', cellId: 'c1', view: { view: 'status' }, focus: 'view', at: Date.now() } });
        vi.advanceTimersByTime(PENDING_RUNS_PANE_OPEN_MS + 1);
        useCollimateStore.setState({ activeChannel: CH('ch') });
        expect(store().pendingRunsPaneOpen).toBeNull();
        expect(store().runsPaneView.c1).toBeUndefined();
    });
});

describe('a channel switch', () => {
    it('abandons every Add annotation in flight and clears the notices', () => {
        const controller = new AbortController();
        useCollimateStore.setState({ annotationRequests: { c1: { draftKey: 'k', path: 'p', controller } }, annotationNotice: { c1: 'x' } });
        useCollimateStore.setState({ activeChannel: CH('other') });
        expect(controller.signal.aborted).toBe(true);
        expect(store().annotationRequests).toEqual({});
        expect(store().annotationNotice).toEqual({});
        const quiet = store().annotationRequests;
        useCollimateStore.setState({ activeChannel: CH('third') });
        expect(store().annotationRequests).toBe(quiet);
    });
});
