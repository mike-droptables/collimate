// The annotation slice's store subscriber: the file last viewed per cell
// follows the active file tab, a deferred annotation-pane or status-section
// open lands once its cell is reachable (or expires), and a channel switch
// abandons Add annotation requests, clears the notices and drops the status
// sections.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { PENDING_OPEN_MS } from './annotationLifecycle';
import { activateTab, allTabs, createDefaultLayout, openEmptyTab, openFileTab } from '@/lib/cellLayout';

const store = () => useCollimateStore.getState();
const CH = (id: string) => ({ id, name: id, main_stacks: [{ id: 's', name: '', cells: [{ id: 'c1', name: 'C', files: [] }] }], nav_stacks: [], main_selection_id: null, nav_selection_id: null });

beforeEach(() => {
    useCollimateStore.setState({
        activeChannel: CH('ch'), state: { channels: [CH('ch')] }, cellCache: {}, cellLayouts: {}, cellDisplaysReady: true,
        lastFileTab: {}, pendingAnnotationPaneOpen: null, annotationPaneView: {}, annotationPaneFocus: {}, annotationRequests: {}, annotationNotice: {},
        statusSection: {}, statusSectionFocus: {}, pendingStatusSectionOpen: null,
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

describe('a deferred annotation-pane open', () => {
    it('lands once the channel is active, the displays are loaded and the cell is there', () => {
        useCollimateStore.setState({ activeChannel: CH('other'), cellDisplaysReady: false, cellLayouts: { c1: createDefaultLayout({ filePaths: [], pinnedPath: null }) } });
        useCollimateStore.setState({ pendingAnnotationPaneOpen: { channel: 'ch', cellId: 'c1', view: { view: 'all', option: 'default' }, focus: 'composer', at: Date.now() } });
        expect(store().pendingAnnotationPaneOpen).not.toBeNull();
        useCollimateStore.setState({ activeChannel: CH('ch') });
        expect(store().pendingAnnotationPaneOpen).not.toBeNull();
        useCollimateStore.setState({ cellDisplaysReady: true });
        expect(store().pendingAnnotationPaneOpen).toBeNull();
        expect(store().annotationPaneView.c1).toEqual({ view: 'all', option: 'default' });
        expect(store().annotationPaneFocus.c1.target).toBe('composer');
        expect(store().cellLayouts.c1.runsPane.open).toBe(true);
    });

    it('expires after 15 s', () => {
        vi.useFakeTimers();
        useCollimateStore.setState({ activeChannel: CH('other') });
        useCollimateStore.setState({ pendingAnnotationPaneOpen: { channel: 'ch', cellId: 'c1', view: { view: 'all', option: 'default' }, focus: 'view', at: Date.now() } });
        vi.advanceTimersByTime(PENDING_OPEN_MS + 1);
        useCollimateStore.setState({ activeChannel: CH('ch') });
        expect(store().pendingAnnotationPaneOpen).toBeNull();
        expect(store().annotationPaneView.c1).toBeUndefined();
    });
});

describe('a deferred status-section open', () => {
    it('lands once the channel is active, the displays are loaded and the cell is there, focused', () => {
        useCollimateStore.setState({ activeChannel: CH('other'), cellDisplaysReady: false });
        useCollimateStore.setState({ pendingStatusSectionOpen: { channel: 'ch', cellId: 'c1', at: Date.now() } });
        useCollimateStore.setState({ activeChannel: CH('ch') });
        expect(store().pendingStatusSectionOpen).not.toBeNull();
        expect(store().statusSection.c1).toBeUndefined();
        useCollimateStore.setState({ cellDisplaysReady: true });
        expect(store().pendingStatusSectionOpen).toBeNull();
        expect(store().statusSection.c1).toEqual({ open: true, expandedRunId: null });
        expect(store().statusSectionFocus.c1.seq).toBe(1);
    });

    it('lands on the run\'s row it was asked for, without the keys when asked so (a run start in a cell not yet there)', () => {
        useCollimateStore.setState({ activeChannel: CH('ch'), cellDisplaysReady: false, runs: {
            'run-1': { runId: 'run-1', cellId: 'c1', channel: 'ch', runnerId: 'agent', runnerName: 'Agent', generatorName: 'agent', label: 'agent-run-1', folder: '_runs/agent-run-1' },
        } } as never);
        store().openStatusSection('c1', { channel: 'ch', expandRunId: 'run-1', focus: false });
        expect(store().statusSection.c1).toBeUndefined();
        useCollimateStore.setState({ cellDisplaysReady: true });
        expect(store().pendingStatusSectionOpen).toBeNull();
        expect(store().statusSection.c1).toEqual({ open: true, expandedRunId: 'run-1' });
        expect(store().statusSectionFocus.c1).toBeUndefined();
    });

    it('opened from another channel waits through the switch, which does not drop it', () => {
        store().openStatusSection('c1', { channel: 'other' });
        expect(store().pendingStatusSectionOpen).toMatchObject({ channel: 'other', cellId: 'c1' });
        useCollimateStore.setState({ activeChannel: CH('other'), state: { channels: [CH('other')] }, cellDisplaysReady: false } as never);
        expect(store().pendingStatusSectionOpen).not.toBeNull();
        useCollimateStore.setState({ cellDisplaysReady: true });
        expect(store().statusSection.c1.open).toBe(true);
    });

    it('expires after 15 s', () => {
        vi.useFakeTimers();
        useCollimateStore.setState({ activeChannel: CH('other') });
        useCollimateStore.setState({ pendingStatusSectionOpen: { channel: 'ch', cellId: 'c1', at: Date.now() } });
        vi.advanceTimersByTime(PENDING_OPEN_MS + 1);
        useCollimateStore.setState({ activeChannel: CH('ch') });
        expect(store().pendingStatusSectionOpen).toBeNull();
        expect(store().statusSection.c1).toBeUndefined();
    });
});

describe('a channel switch', () => {
    it('abandons every Add annotation in flight, clears the notices and drops the status sections', () => {
        const controller = new AbortController();
        useCollimateStore.setState({
            annotationRequests: { c1: { draftKey: 'k', path: 'p', controller } }, annotationNotice: { c1: 'x' },
            statusSection: { c1: { open: true, expandedRunId: null } },
        });
        useCollimateStore.setState({ activeChannel: CH('other') });
        expect(controller.signal.aborted).toBe(true);
        expect(store().annotationRequests).toEqual({});
        expect(store().annotationNotice).toEqual({});
        expect(store().statusSection).toEqual({});
        const quiet = store().annotationRequests;
        useCollimateStore.setState({ activeChannel: CH('third') });
        expect(store().annotationRequests).toBe(quiet);
    });
});
