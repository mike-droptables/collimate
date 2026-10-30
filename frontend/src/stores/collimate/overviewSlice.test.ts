// @vitest-environment jsdom
import {waitFor} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Channel, Stack } from '@/lib/types';
import type { JournalEvent, Lineage, RemovedCell } from '@/lib/api/cells';

const cells = vi.hoisted(() => ({
    fetchRemovedCells: vi.fn(), fetchLineage: vi.fn(), fetchJournal: vi.fn(),
    restoreRemovedCell: vi.fn(), removeCellById: vi.fn(),
}));
vi.mock('@/lib/api/cells', async (original) => ({ ...await original<typeof import('@/lib/api/cells')>(), ...cells }));
const stacksApi = vi.hoisted(() => ({ moveStackTo: vi.fn(), moveCellTo: vi.fn() }));
vi.mock('@/lib/api/stacks', async (original) => ({ ...await original<typeof import('@/lib/api/stacks')>(), ...stacksApi }));

import { useCollimateStore } from '../useCollimateStore';
import {
    ALL_CELLS, SHELF_SEARCH_MS, boardFollowHint, boardRegion, boardOrder, cellPlace, connectedCells, defaultOverviewView,
    journalKey, provisionalRemoved, relationsOf, removableCard, shelfEntries, shelfFollowHint, sortRemoved, subjectCellId,
} from './overviewSlice';

const removed = (id: string, over: Partial<RemovedCell> = {}): RemovedCell => ({
    cell_id: id, tip_version_id: `${id}-v`, name: id.toUpperCase(), icon: null, stack_name: null, stack_cell_count: 1,
    reason: 'removed', created_at: '2026-10-01T10:00:00+00:00', edited_at: '2026-10-02T10:00:00+00:00',
    removed_at: '2026-10-03T10:00:00+00:00', made_by: { kind: 'user', runner_id: null, runner_name: null },
    files: ['a.md'], file_count: 1, preview: { path: 'a.md', text: '# A' }, tags: [], ...over,
});

const event = (t: string, over: Partial<JournalEvent> = {}): JournalEvent => ({
    t, kind: 'edit', by: { kind: 'user', runner_id: null, runner_name: null },
    cells: [{ cell_id: 'a', name: 'A', status: 'open' }], run: null, files: [], files_total: 0, version_id: null, ...over,
});

// The board: s1 holds a (newer) and b (older); s2 holds c; g is the Capabilities stack.
const stack = (id: string, ids: [string, string][], type?: Stack['stack_type']): Stack => ({
    id, name: id, stack_type: type, cells: ids.map(([cid, at]) => ({ id: cid, name: cid, files: [], created_at: at })),
});
const channel: Channel = {
    id: 'ch', name: 'chan', nav_stacks: [], nav_selection_id: null, main_selection_id: 's1',
    main_stacks: [
        stack('s1', [['a', '2026-10-02T09:00:00'], ['b', '2026-10-01T09:00:00']]),
        stack('s2', [['c', '2026-10-01T08:00:00']]),
        stack('g', [['gen', '2026-10-01T07:00:00']], 'capabilities'),
    ],
};

// a was duplicated as d (on the shelf); a and c merged into m; e ran into a.
const lineage: Lineage = {
    cells: [],
    moves: [
        { id: 1, op: 'duplicate', at: '2026-10-02T10:00:00+00:00', op_id: 'r1', from: [{ cell_id: 'a', version_id: 'v1' }], to: [{ cell_id: 'd', version_id: null }] },
        { id: 2, op: 'merge', at: '2026-10-02T11:00:00+00:00', op_id: 'r2', from: [{ cell_id: 'a', version_id: 'v2' }, { cell_id: 'c', version_id: 'v3' }], to: [{ cell_id: 'm', version_id: 'v4' }] },
        { id: 3, op: 'run_cell', at: '2026-10-02T12:00:00+00:00', op_id: 'r3', from: [{ cell_id: 'e', version_id: null }], to: [{ cell_id: 'a', version_id: null }], runner: { runner_id: 'x', runner_name: 'X' } },
    ],
};

const s = () => useCollimateStore.getState();
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

beforeEach(() => {
    vi.clearAllMocks();
    cells.fetchRemovedCells.mockImplementation(async (_ch: string, opts: { q?: string }) =>
        opts.q ? [removed('d')] : [removed('d', { edited_at: '2026-10-05T10:00:00+00:00' }), removed('m')]);
    cells.fetchLineage.mockResolvedValue(lineage);
    cells.fetchJournal.mockResolvedValue([]);
    useCollimateStore.setState({
        activeChannel: channel, selectedCellId: null, runs: {}, pendingSelectCellId: null,
        applyOptimisticChannel: vi.fn(), applyOptimisticLayout: vi.fn(),
    });
    s().dropOverviewData();
    useCollimateStore.setState({ overview: defaultOverviewView() });
});
afterEach(() => vi.useRealTimers());

describe('the board and the shelf', () => {
    it('loads the shelf and the lineage for the channel, and sorts on the client without refetching (O05)', async () => {
        await s().loadOverview('chan');
        expect(cells.fetchRemovedCells).toHaveBeenCalledWith('chan', { sort: 'edited', dir: 'desc' });
        expect(cells.fetchLineage).toHaveBeenCalledWith('chan');
        expect(s().overviewData.removed!.map(e => e.cell_id)).toEqual(['d', 'm']);
        s().setRemovedDir('asc');
        expect(s().overviewData.removed!.map(e => e.cell_id)).toEqual(['m', 'd']);
        expect(cells.fetchRemovedCells).toHaveBeenCalledTimes(1);
    });

    it('sorts like the server: missing times first ascending, ties by cell id either way (O05)', () => {
        const list = [removed('b', { created_at: 't2' }), removed('a', { created_at: 't2' }), removed('c', { created_at: null })];
        expect(sortRemoved(list, 'created', 'asc').map(e => e.cell_id)).toEqual(['c', 'a', 'b']);
        expect(sortRemoved(list, 'created', 'desc').map(e => e.cell_id)).toEqual(['a', 'b', 'c']);
    });

    it('searches on the server after the keys pause and narrows the shelf to the matches (O05)', async () => {
        await s().loadOverview('chan');
        vi.useFakeTimers();
        s().setRemovedQuery('d');
        s().setRemovedQuery('dd');
        await vi.advanceTimersByTimeAsync(SHELF_SEARCH_MS);
        expect(cells.fetchRemovedCells).toHaveBeenLastCalledWith('chan', { sort: 'edited', dir: 'desc', q: 'dd' });
        expect(cells.fetchRemovedCells).toHaveBeenCalledTimes(2);
        expect(shelfEntries(s()).map(e => e.cell_id)).toEqual(['d']);
        expect(s().overviewData.removed).toHaveLength(2);   // "1 of 2 removed"
        s().setRemovedQuery('');
        expect(shelfEntries(s())).toHaveLength(2);
    });

    it('sorts by last removed, either way, from removed_at (TG-04)', () => {
        const list = [
            removed('a', { removed_at: '2026-10-05T10:00:00+00:00', edited_at: '2026-10-01T10:00:00+00:00' }),
            removed('b', { removed_at: '2026-10-03T10:00:00+00:00', edited_at: '2026-10-09T10:00:00+00:00' }),
            removed('c', { removed_at: '2026-10-07T10:00:00+00:00' }),
        ];
        expect(sortRemoved(list, 'removed', 'desc').map(e => e.cell_id)).toEqual(['c', 'a', 'b']);
        expect(sortRemoved(list, 'removed', 'asc').map(e => e.cell_id)).toEqual(['b', 'a', 'c']);
    });

    it('the tag filter chips narrow the shelf on the client: a card carries every chip, case-folded (TG-04)', async () => {
        cells.fetchRemovedCells.mockResolvedValue([
            removed('d', { tags: ['Done', 'ui'] }), removed('m', { tags: ['done'] }), removed('n', { tags: [] }),
        ]);
        await s().loadOverview('chan');
        s().addTagFilter('  DONE ');
        expect(s().overview.tagFilters).toEqual(['DONE']);
        expect(shelfEntries(s()).map(e => e.cell_id).sort()).toEqual(['d', 'm']);
        // One chip per case-folded spelling; an empty one is no chip.
        s().addTagFilter('done');
        s().addTagFilter('   ');
        expect(s().overview.tagFilters).toEqual(['DONE']);
        s().addTagFilter('UI');
        expect(shelfEntries(s()).map(e => e.cell_id)).toEqual(['d']);
        expect(s().overviewData.removed).toHaveLength(3);
        expect(cells.fetchRemovedCells).toHaveBeenCalledTimes(1);
        s().removeTagFilter('done');
        expect(s().overview.tagFilters).toEqual(['UI']);
        s().clearTagFilters();
        expect(shelfEntries(s())).toHaveLength(3);
    });

    it('adopts and sets the remove zones (TG-06)', () => {
        const zones = [{ id: 'z1', width: 1, action: { kind: 'add_tag' as const, tag: 'done' } }];
        expect(s().overview.removeZones).toEqual([]);
        s().adoptRemoveZones(zones);
        expect(s().overview.removeZones).toBe(zones);
        s().setRemoveZones([]);
        expect(s().overview.removeZones).toEqual([]);
    });

    it('the tag toggle: off by default, set and adopted (tags toggle)', () => {
        expect(s().overview.showTags).toBe(false);
        s().setShowTags(true);
        expect(s().overview.showTags).toBe(true);
        s().adoptShowTags(false);
        expect(s().overview.showTags).toBe(false);
        s().adoptShowTags(true);
        expect(s().overview.showTags).toBe(true);
    });

    it('a resize below 110px shuts the shelf and keeps its height (O05)', () => {
        s().setShelfHeight(240);
        expect(s().overview).toMatchObject({ shelfOpen: true, shelfHeight: 240 });
        s().setShelfHeight(90);
        expect(s().overview).toMatchObject({ shelfOpen: false, shelfHeight: 240 });
    });

    it('restore applies the channel, takes the card off the shelf and selects the cell (O05)', async () => {
        await s().loadOverview('chan');
        s().pickShelf('d');
        const restored = { ...channel };
        cells.restoreRemovedCell.mockResolvedValue({ ok: true, cell_id: 'd', stack_id: 's9', channel: restored });
        expect(await s().restoreRemoved('d')).toBe(true);
        expect(cells.restoreRemovedCell).toHaveBeenCalledWith('chan', 'd');
        expect(s().applyOptimisticChannel).toHaveBeenCalledWith(restored);
        expect(s().pendingSelectCellId).toBe('d');
        expect(s().overview).toMatchObject({ shelfSelection: null, picked: null });
    });

    it('remove takes a board cell by id onto the shelf and selects the next board cell (O05)', async () => {
        await s().loadOverview('chan');
        const layout = { ...channel };
        cells.removeCellById.mockResolvedValue(layout);
        expect(await s().removeFromBoard('a')).toBe(true);
        expect(cells.removeCellById).toHaveBeenCalledWith('chan', 'a');
        expect(s().applyOptimisticLayout).toHaveBeenCalledWith(layout);
        expect(s().overview).toMatchObject({ shelfSelection: 'a', scrollTo: 'a' });
        expect(s().pendingSelectCellId).toBe('b');
        expect(s().takeShelfScroll()).toBe('a');
        expect(s().overview.scrollTo).toBeNull();
    });

    it('remove is refused for a running cell and for a Capabilities cell, without a request (O05)', async () => {
        await s().loadOverview('chan');
        useCollimateStore.setState({ runs: { r: { runId: 'r', cellId: 'a', channel: 'chan', runnerId: null, runnerName: null,
            generatorName: 'g', label: 'g-r', folder: '_runs/g-r' } } });
        expect(await s().removeFromBoard('a')).toBe(false);
        expect(await s().removeFromBoard('gen')).toBe(false);
        expect(cells.removeCellById).not.toHaveBeenCalled();
    });

    it('a refused remove (409) is reported, not thrown (O05)', async () => {
        await s().loadOverview('chan');
        cells.removeCellById.mockRejectedValue(Object.assign(new Error('x'), { response: { data: { detail: 'Cell is running — stop the run first' } } }));
        expect(await s().removeFromBoard('c')).toBe(false);
        expect(s().overviewData.error).toBe('Cell is running — stop the run first');
    });

    it('the board lists each column in its stack\'s own order, not by age: the order Remove steps through (polish)', () => {
        // s1 holds a (newer) before b (older).
        expect(boardOrder(channel.main_stacks)).toEqual(['a', 'b', 'c', 'gen']);
    });

    it('the board region sits below the shelf and its bar, left of an open journal (O01)', () => {
        expect(boardRegion({ width: 1200, height: 800 }, 316, true)).toEqual({ left: 0, top: 349, right: 826, bottom: 800 });
        expect(boardRegion({ width: 1200, height: 800 }, 0, false)).toEqual({ left: 0, top: 33, right: 1200, bottom: 800 });
    });
});

describe('the anchor and Only connected', () => {
    beforeEach(async () => { await s().loadOverview('chan'); });

    it('with Only connected off the anchor follows the selection, wherever it comes from (O06)', () => {
        useCollimateStore.setState({ selectedCellId: 'a' });
        expect(s().overview.anchor).toBe('a');
        s().pickShelf('d');
        expect(subjectCellId(s())).toBe('d');
        expect(cellPlace(s(), 'd')).toBe('shelf');
        useCollimateStore.setState({ selectedCellId: 'c' });
        expect(subjectCellId(s())).toBe('c');
    });

    it('with Only connected on a first click selects and a second click on the selected cell follows its lines (O07)', () => {
        useCollimateStore.setState({ selectedCellId: 'a' });
        s().setJournalOpen(true);
        s().toggleConnected();
        s().selectCard('c');
        expect(s().overview.anchor).toBe('a');
        expect(s().pendingSelectCellId).toBe('c');
        useCollimateStore.setState({ selectedCellId: 'c' });
        expect(subjectCellId(s())).toBe('a');
        expect(boardFollowHint(s(), 'c')).toBe(true);
        s().selectCard('c');
        expect(subjectCellId(s())).toBe('c');
        expect(boardFollowHint(s(), 'c')).toBe(false);
    });

    it('on the shelf the second click on the picked card anchors it (O07)', () => {
        useCollimateStore.setState({ selectedCellId: 'a' });
        s().setJournalOpen(true);
        s().toggleConnected();
        s().pickShelf('d');
        expect(s().overview).toMatchObject({ shelfSelection: 'd', picked: 'd', anchor: 'a' });
        expect(shelfFollowHint(s(), 'd')).toBe(true);
        s().pickShelf('d');
        expect(subjectCellId(s())).toBe('d');
        expect(shelfFollowHint(s(), 'd')).toBe(false);
    });

    it('Clear shows every cell and the All cells journal; after it a board card needs two clicks (O07)', () => {
        useCollimateStore.setState({ selectedCellId: 'a' });
        s().setJournalOpen(true);
        s().toggleConnected();
        expect(connectedCells(s())).toEqual(new Set(['a', 'd', 'm']));   // c merged beside a, not with it
        s().clearAnchor();
        expect(subjectCellId(s())).toBeNull();
        expect(journalKey(s())).toBe(ALL_CELLS);
        expect(connectedCells(s())).toBeNull();
        // The selected card, clicked once after Clear, is only picked.
        s().selectCard('a');
        expect(s().overview).toMatchObject({ boardPick: 'a', cleared: true });
        expect(boardFollowHint(s(), 'a')).toBe(true);
        s().selectCard('a');
        expect(subjectCellId(s())).toBe('a');
        expect(s().overview.cleared).toBe(false);
    });

    it('the lines are the subject\'s duplicates, merges and splits both ways, never a run in a new cell (O07)', () => {
        expect(relationsOf(lineage, 'a').map(r => [r.move.op, r.cellId, r.out])).toEqual([
            ['duplicate', 'd', true], ['merge', 'm', true],
        ]);
        expect(relationsOf(lineage, 'm').map(r => [r.move.op, r.cellId, r.out])).toEqual([
            ['merge', 'a', false], ['merge', 'c', false],
        ]);
        expect(relationsOf(lineage, 'e')).toEqual([]);
    });

    it('walking to a shelf cell opens the shelf, picks it, scrolls to it and clears a search that hides it (O06)', async () => {
        vi.useFakeTimers();
        s().setRemovedQuery('zzz');
        cells.fetchRemovedCells.mockResolvedValueOnce([]);
        await vi.advanceTimersByTimeAsync(SHELF_SEARCH_MS);
        expect(s().overviewData.removedMatches).toEqual([]);
        s().walkTo('m');
        expect(s().overview).toMatchObject({ shelfOpen: true, shelfSelection: 'm', picked: 'm', scrollTo: 'm', anchor: 'm', query: '' });
        expect(s().overviewData.removedMatches).toBeNull();
    });

    it('walking to a board cell clicks it; walking to an unknown cell does nothing (O06)', () => {
        s().walkTo('c');
        expect(s().pendingSelectCellId).toBe('c');
        expect(s().overview.anchor).toBe('c');
        s().walkTo('nowhere');
        expect(s().overview.anchor).toBe('c');
    });
});

describe('the journal and its invalidation', () => {
    beforeEach(async () => { await s().loadOverview('chan'); });

    it('shows a key from its first page and loads older pages by the last event\'s time (O06)', async () => {
        const first = Array.from({ length: 100 }, (_, i) => event(`2026-10-02T10:${String(59 - (i % 60)).padStart(2, '0')}:00+00:00`));
        cells.fetchJournal.mockResolvedValueOnce(first).mockResolvedValueOnce([event('2026-10-01T09:00:00+00:00')]);
        await s().showJournal('a');
        expect(cells.fetchJournal).toHaveBeenCalledWith('chan', 'a', { before: null, limit: 100 });
        expect(s().overviewData.journals.a.complete).toBe(false);
        await s().loadMoreJournal('a');
        expect(cells.fetchJournal).toHaveBeenLastCalledWith('chan', 'a', { before: first[99].t, limit: 100 });
        expect(s().overviewData.journals.a.events).toHaveLength(101);
        expect(s().overviewData.journals.a.complete).toBe(true);
        await s().loadMoreJournal('a');
        expect(cells.fetchJournal).toHaveBeenCalledTimes(2);
        await s().showJournal(ALL_CELLS);
        expect(cells.fetchJournal).toHaveBeenLastCalledWith('chan', null, { before: null, limit: 100 });
    });

    it('lineage_changed drops the named cells\' journals, refreshes the shown one and reloads the shelf and lineage (O04)', async () => {
        cells.fetchJournal.mockResolvedValue([event('2026-10-02T10:00:00+00:00')]);
        await s().showJournal('b');
        await s().showJournal(ALL_CELLS);
        await s().showJournal('a');
        vi.clearAllMocks();
        s().overviewLineageChanged(['a', 'd']);
        await flush();
        expect(Object.keys(s().overviewData.journals).sort()).toEqual(['a', 'b']);
        expect(cells.fetchJournal).toHaveBeenCalledWith('chan', 'a', { before: null, limit: 100 });
        expect(cells.fetchRemovedCells).toHaveBeenCalledTimes(1);
        expect(cells.fetchLineage).toHaveBeenCalledTimes(1);
    });

    it('a run\'s start or end drops the journals that hold it or belong to its cell (O04)', async () => {
        cells.fetchJournal.mockImplementation(async (_ch: string, cellId: string | null) => cellId === 'b'
            ? [event('2026-10-02T10:00:00+00:00', { kind: 'run', run: { run_id: 'r7', runner_id: null, runner_name: null,
                status: 'running', outcome: null, note: null, started_at: null, ended_at: null, files_written: [] } })]
            : []);
        await s().showJournal('a');
        await s().showJournal('b');
        await s().showJournal('c');
        await s().showJournal(null);
        s().overviewRunChanged('r7', 'c');
        expect(Object.keys(s().overviewData.journals)).toEqual(['a']);
    });

    it('the open journal stays current mid-run: a run starting, writing or ending on the anchor, or on a cell it names, refreshes it (O06)', async () => {
        // a's journal names c (a and c merged); b's names neither the run nor c.
        const merged = event('2026-10-02T11:00:00+00:00', {
            kind: 'merge', cells: [{ cell_id: 'a', name: 'A', status: 'open' }, { cell_id: 'c', name: 'C', status: 'open' }],
        });
        const onB = event('2026-10-02T10:00:00+00:00', { cells: [{ cell_id: 'b', name: 'B', status: 'open' }] });
        cells.fetchJournal.mockImplementation(async (_ch: string, cellId: string | null) => (cellId === 'a' ? [merged] : [onB]));
        await s().showJournal('b');
        await s().showJournal('a');
        const refreshed = async (run: () => void) => {
            vi.clearAllMocks();
            run();
            await flush();
            return cells.fetchJournal.mock.calls.map(call => call[1]);
        };
        // A run started on the anchor (run_started), not yet in its page.
        expect(await refreshed(() => s().overviewRunChanged('r9', 'a'))).toEqual(['a']);
        // A version that run writes mid-run (dag_commit carries the cell).
        expect(await refreshed(() => s().overviewRunChanged(null, 'a'))).toEqual(['a']);
        // A run on a cell the open journal names ends (run_status).
        expect(await refreshed(() => s().overviewRunChanged('r8', 'c'))).toEqual(['a']);
        expect(s().overviewData.journals.a.events).toEqual([merged]);
        // A run elsewhere leaves the open journal be; b's was never touched.
        expect(await refreshed(() => s().overviewRunChanged('r7', 'gen'))).toEqual([]);
        expect(Object.keys(s().overviewData.journals).sort()).toEqual(['a', 'b']);
    });

    it('a response for a dropped journal is not applied (O04)', async () => {
        let resolve!: (events: JournalEvent[]) => void;
        cells.fetchJournal.mockReturnValueOnce(new Promise(r => { resolve = r; }));
        const pending = s().showJournal('c');
        s().showJournal(null);
        s().overviewLineageChanged(['c']);
        resolve([event('2026-10-02T10:00:00+00:00')]);
        await pending;
        expect(s().overviewData.journals.c).toBeUndefined();
    });

    it('a stream (re)connect rereads what is loaded: the shelf, the lineage and the shown journal (O04)', async () => {
        cells.fetchJournal.mockResolvedValue([event('2026-10-02T10:00:00+00:00')]);
        await s().showJournal('b');
        await s().showJournal('a');
        vi.clearAllMocks();
        s().overviewResync();
        await flush();
        expect(Object.keys(s().overviewData.journals)).toEqual(['a']);
        expect(cells.fetchJournal).toHaveBeenCalledWith('chan', 'a', { before: null, limit: 100 });
        expect(cells.fetchRemovedCells).toHaveBeenCalledTimes(1);
        expect(cells.fetchLineage).toHaveBeenCalledTimes(1);
        // Outside the overview nothing is loaded and nothing is read.
        s().dropOverviewData();
        vi.clearAllMocks();
        s().overviewResync();
        await flush();
        expect(cells.fetchRemovedCells).not.toHaveBeenCalled();
        expect(cells.fetchLineage).not.toHaveBeenCalled();
    });

    it('a channel switch drops the data and the anchor (O04)', () => {
        s().pickShelf('d');
        useCollimateStore.setState({ activeChannel: { ...channel, id: 'other', name: 'other' } });
        expect(s().overviewData).toMatchObject({ channel: null, removed: null, lineage: null, journals: {} });
        expect(s().overview).toMatchObject({ anchor: null, picked: null, shelfSelection: null });
    });

    it('a channel switch leaves the tag toggle to the persistence hook, which adopts the next channel\'s (tags toggle)', async () => {
        await s().loadOverview('chan');
        s().setShowTags(true);
        useCollimateStore.setState({ activeChannel: { ...channel, id: 'other', name: 'other' } });
        expect(s().overview.showTags).toBe(true);
    });

    it('a channel switch clears the shelf\'s tag filter chips (TG-04)', async () => {
        await s().loadOverview('chan');
        s().addTagFilter('done');
        expect(s().overview.tagFilters).toEqual(['done']);
        useCollimateStore.setState({ activeChannel: { ...channel, id: 'other', name: 'other' } });
        expect(s().overview.tagFilters).toEqual([]);
    });
});

describe('dragging between the board and the shelf', () => {
    const ids = () => s().overviewData.removed!.map(e => e.cell_id);
    const deferred = <T,>() => {
        let resolve!: (value: T) => void;
        let reject!: (err: unknown) => void;
        const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
        return { promise, resolve, reject };
    };

    it('a removed board cell is on the shelf at once, from what the board knows of it (polish)', async () => {
        await s().loadOverview('chan');
        const call = deferred<Channel>();
        cells.removeCellById.mockReturnValue(call.promise);
        const pending = s().removeFromBoard('a');
        // Edited latest first: d (5 Oct), then m (2 Oct); a has no edit time, so it sorts last.
        await waitFor(()=>expect(ids()).toEqual(['d', 'm', 'a']));
        expect(s().overviewData.removed!.find(e => e.cell_id === 'a')).toMatchObject({
            name: 'a', stack_name: 's1', stack_cell_count: 2, reason: 'removed', tip_version_id: null, preview: null,
        });
        call.resolve({ ...channel });
        expect(await pending).toBe(true);
        expect(s().overview.shelfSelection).toBe('a');
    });

    it('a remove zone\'s action goes in the remove body, and the card on the shelf at once carries the tags it leaves (TG-06)', async () => {
        await s().loadOverview('chan');
        const tagged: Channel = {
            ...channel,
            main_stacks: [{ ...channel.main_stacks[0], cells: channel.main_stacks[0].cells.map(c => ({ ...c, tags: ['wip', 'ui'] })) }, ...channel.main_stacks.slice(1)],
        };
        useCollimateStore.setState({ activeChannel: tagged });
        const call = deferred<Channel>();
        cells.removeCellById.mockReturnValue(call.promise);
        const pending = s().removeFromBoard('a', { kind: 'add_tag', tag: ' done ' });
        await waitFor(() => expect(cells.removeCellById).toHaveBeenCalledWith('chan', 'a', { add_tags: ['done'] }));
        expect(s().overviewData.removed!.find(e => e.cell_id === 'a')?.tags).toEqual(['wip', 'ui', 'done']);
        call.resolve({ ...tagged });
        expect(await pending).toBe(true);

        cells.removeCellById.mockResolvedValue({ ...tagged });
        s().dropOverviewData();
        await s().loadOverview('chan');
        await s().removeFromBoard('b', { kind: 'remove_tag', tag: 'WIP' });
        expect(cells.removeCellById).toHaveBeenLastCalledWith('chan', 'b', { remove_tags: ['WIP'] });
        // A plain remove sends no body.
        await s().removeFromBoard('c', { kind: 'remove' });
        expect(cells.removeCellById).toHaveBeenLastCalledWith('chan', 'c');
        expect(provisionalRemoved(tagged.main_stacks[0].cells[0], tagged.main_stacks[0], 'now', { kind: 'remove_tag', tag: 'ui' }).tags).toEqual(['wip']);
    });

    it('a removal that fails takes its card back off the shelf (polish)', async () => {
        await s().loadOverview('chan');
        cells.removeCellById.mockRejectedValue(new Error('gone'));
        expect(await s().removeFromBoard('a')).toBe(false);
        expect(ids()).toEqual(['d', 'm']);
        expect(s().overviewData.error).toBeTruthy();
    });

    it('a shelf card leaves the shelf at once on restore, and comes back if it fails (polish)', async () => {
        await s().loadOverview('chan');
        const call = deferred<{ ok: boolean; cell_id: string; stack_id: string; channel: Channel }>();
        cells.restoreRemovedCell.mockReturnValue(call.promise);
        const pending = s().restoreRemoved('d');
        expect(ids()).toEqual(['m']);
        call.reject(new Error('conflict'));
        expect(await pending).toBe(false);
        expect(ids()).toEqual(['d', 'm']);
    });

    it('a restore at a board slot moves the new column there before the board shows it (polish)', async () => {
        await s().loadOverview('chan');
        const restored: Channel = { ...channel, main_stacks: [...channel.main_stacks, stack('s9', [['d', '2026-10-01T06:00:00']])] };
        cells.restoreRemovedCell.mockResolvedValue({ ok: true, cell_id: 'd', stack_id: 's9', channel: restored });
        const moved = { ...restored, main_stacks: [restored.main_stacks[0], restored.main_stacks[3], restored.main_stacks[1], restored.main_stacks[2]] };
        stacksApi.moveStackTo.mockResolvedValue(moved);
        expect(await s().restoreRemoved('d', { kind: 'column', slot: 1 })).toBe(true);
        expect(stacksApi.moveStackTo).toHaveBeenCalledWith('chan', 's9', 1, 'insert');
        expect(s().applyOptimisticChannel).toHaveBeenCalledWith(restored);
        expect(s().applyOptimisticLayout).toHaveBeenCalledWith(moved);
        expect(s().pendingSelectCellId).toBe('d');
        // At the right end already: no move.
        stacksApi.moveStackTo.mockClear();
        cells.restoreRemovedCell.mockResolvedValue({ ok: true, cell_id: 'm', stack_id: 's9', channel: restored });
        expect(await s().restoreRemoved('m', { kind: 'column', slot: 3 })).toBe(true);
        expect(stacksApi.moveStackTo).not.toHaveBeenCalled();
    });

    it('a restore into a stack moves the cell to the pointed position there before the board shows it (polish)', async () => {
        await s().loadOverview('chan');
        const restored: Channel = { ...channel, main_stacks: [...channel.main_stacks, stack('s9', [['d', '2026-10-01T06:00:00']])] };
        cells.restoreRemovedCell.mockResolvedValue({ ok: true, cell_id: 'd', stack_id: 's9', channel: restored });
        const joined = { ...channel, main_stacks: [stack('s1', [['a', ''], ['d', ''], ['b', '']]), ...channel.main_stacks.slice(1)] };
        stacksApi.moveCellTo.mockResolvedValue(joined);
        expect(await s().restoreRemoved('d', { kind: 'stack', stackId: 's1', index: 1 })).toBe(true);
        expect(stacksApi.moveCellTo).toHaveBeenCalledExactlyOnceWith('chan', 's9', 0, 'stack', 'd', 1);
        expect(stacksApi.moveStackTo).not.toHaveBeenCalled();
        expect(s().applyOptimisticChannel).toHaveBeenCalledWith(restored);
        expect(s().applyOptimisticLayout).toHaveBeenCalledWith(joined);
        expect(s().pendingSelectCellId).toBe('d');
    });

    it('a restore whose move fails keeps the column at the right end (polish)', async () => {
        await s().loadOverview('chan');
        const restored: Channel = { ...channel, main_stacks: [...channel.main_stacks, stack('s9', [['d', '2026-10-01T06:00:00']])] };
        cells.restoreRemovedCell.mockResolvedValue({ ok: true, cell_id: 'd', stack_id: 's9', channel: restored });
        stacksApi.moveStackTo.mockRejectedValue(new Error('nope'));
        expect(await s().restoreRemoved('d', { kind: 'column', slot: 0 })).toBe(true);
        expect(s().applyOptimisticChannel).toHaveBeenCalledWith(restored);
        expect(s().applyOptimisticLayout).not.toHaveBeenCalled();
    });

    it('holds the lifted card and counts each refusal (polish)', () => {
        s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'bar', zone: 0, refusal: null });
        expect(s().overview.drop).toEqual({ kind: 'remove', cellId: 'a', over: 'bar', zone: 0, refusal: null });
        s().refuseDrop('Stop its runs to remove it', 'bar');
        s().refuseDrop('Stop its runs to remove it', 'shelf');
        expect(s().overview.refusal).toEqual({ reason: 'Stop its runs to remove it', at: 'shelf', nonce: 2 });
        s().setOverviewDrop(null);
        expect(s().overview.drop).toBeNull();
    });

    it('only a standard stack\'s board cell can go onto the shelf, never a Capabilities cell; its card leaves run files out (polish)', () => {
        expect(removableCard(s(), 'a')).toBe(true);
        expect(removableCard(s(), 'gen')).toBe(false);
        expect(removableCard(s(), 'nowhere')).toBe(false);
        const cell = { id: 'x', name: 'X', files: [{ name: 'b.md' }, { name: '_runs/g/out.txt' }, { name: 'a.md' }] } as unknown as Stack['cells'][number];
        expect(provisionalRemoved(cell, stack('s', [['x', 't']]), 'now')).toMatchObject({ files: ['a.md', 'b.md'], file_count: 2, removed_at: 'now' });
    });
});
