// @vitest-environment jsdom
// Only connected, Clear and the two-click rule, driven from the journal's
// names and the arrows' labels against the store (the board and the shelf
// read the same filter).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render } from '@testing-library/react';
import type { Channel, Stack } from '@/lib/types';
import type { JournalEvent, Lineage, RemovedCell } from '@/lib/api/cells';

const cells = vi.hoisted(() => ({ fetchJournal: vi.fn() }));
vi.mock('@/lib/api/cells', async (original) => ({ ...await original<typeof import('@/lib/api/cells')>(), ...cells }));

import { useCollimateStore } from '@/stores/useCollimateStore';
import {
    ALL_CELLS, boardFollowHint, connectedCells, defaultOverviewView, shelfEntries, shelfFollowHint, subjectCellId,
} from '@/stores/collimate/overviewSlice';
import { JournalPane } from './JournalPane';
import { OverviewArrows } from './OverviewArrows';

const stack = (id: string, ids: [string, string][]): Stack => ({
    id, name: id, cells: ids.map(([cid, name]) => ({ id: cid, name, files: [], created_at: '2026-10-01T09:00:00' })),
});
const channel: Channel = {
    id: 'ch', name: 'chan', nav_stacks: [], nav_selection_id: null, main_selection_id: 's1',
    main_stacks: [stack('s1', [['a', 'Frame']]), stack('s2', [['c', 'Op log']]), stack('s3', [['e', 'Unrelated']])],
};
const shelf = (cell_id: string, name: string) => ({ cell_id, name, stack_cell_count: 1 }) as RemovedCell;
const end = (cell_id: string) => ({ cell_id, version_id: null });
// a was duplicated as d (on the shelf) and split into c (on the board); e
// has no lines; g is on the shelf with no lines to a.
const lineage: Lineage = {
    cells: [],
    moves: [
        { id: 1, op: 'duplicate', at: '2026-10-04T10:00:00+00:00', op_id: 'o1', from: [end('a')], to: [end('d')] },
        { id: 2, op: 'split', at: '2026-10-04T11:00:00+00:00', op_id: 'o2', from: [end('a')], to: [end('c')] },
    ],
};
const moveEvent = (kind: 'duplicate' | 'split', from: [string, string], to: [string, string]): JournalEvent => ({
    t: '2026-10-04T10:00:00+00:00', kind, by: { kind: 'user', runner_id: null, runner_name: null },
    cells: [{ cell_id: from[0], name: from[1], status: 'open' }, { cell_id: to[0], name: to[1], status: 'open' }],
    run: null, files: [], files_total: 0, version_id: null,
});
const journal = (events: JournalEvent[]) => ({ events, loading: false, complete: true, error: null });

const s = () => useCollimateStore.getState();
const name = (id: string) => document.querySelector<HTMLElement>(`[data-journal-pane] [data-cell-name="${id}"]`)!;
const head = () => document.querySelector('[data-journal-pane] h3')!.textContent;

let unsubscribe = () => {};

beforeEach(() => {
    cells.fetchJournal.mockResolvedValue([]);
    useCollimateStore.setState({ activeChannel: channel, selectedCellId: null, runs: {}, pendingSelectCellId: null });
    s().dropOverviewData();
    useCollimateStore.setState(state => ({
        overview: defaultOverviewView(),
        overviewData: {
            ...state.overviewData, channel: 'chan', removed: [shelf('d', 'Clocks'), shelf('g', 'Gone')], lineage,
            journals: {
                a: journal([moveEvent('split', ['a', 'Frame'], ['c', 'Op log']), moveEvent('duplicate', ['a', 'Frame'], ['d', 'Clocks'])]),
                c: journal([moveEvent('split', ['a', 'Frame'], ['c', 'Op log'])]),
                d: journal([moveEvent('duplicate', ['a', 'Frame'], ['d', 'Clocks'])]),
                [ALL_CELLS]: journal([]),
            },
        },
    }));
    // The scene selects what is asked for (StackApp's part).
    unsubscribe = useCollimateStore.subscribe((state, prev) => {
        if (state.pendingSelectCellId && state.pendingSelectCellId !== prev.pendingSelectCellId) {
            useCollimateStore.setState({ selectedCellId: state.pendingSelectCellId, pendingSelectCellId: null });
        }
    });
    useCollimateStore.setState({ selectedCellId: 'a' });
    s().setJournalOpen(true);
    s().toggleConnected();
    render(<div data-overview-root><JournalPane /><OverviewArrows /></div>);
});
afterEach(() => {
    unsubscribe();
    vi.restoreAllMocks();
});

describe('Only connected', () => {
    it('keeps the subject and the cells with lines to it, on the board and on the shelf (O07)', () => {
        expect(connectedCells(s())).toEqual(new Set(['a', 'c', 'd']));
        expect(shelfEntries(s()).map(e => e.cell_id)).toEqual(['d']);
        // Off, everything shows again.
        act(() => s().toggleConnected());
        expect(connectedCells(s())).toBeNull();
        expect(shelfEntries(s()).map(e => e.cell_id)).toEqual(['d', 'g']);
    });

    it('a first click on a name selects; a second click on the selected cell follows its lines (O07)', () => {
        fireEvent.click(name('c'));
        expect(s().selectedCellId).toBe('c');
        expect(subjectCellId(s())).toBe('a');
        expect(boardFollowHint(s(), 'c')).toBe(true);
        expect(head()).toBe('Frame');
        fireEvent.click(name('c'));
        expect(subjectCellId(s())).toBe('c');
        expect(boardFollowHint(s(), 'c')).toBe(false);
        expect(head()).toBe('Op log');
        expect(connectedCells(s())).toEqual(new Set(['c', 'a']));
    });

    it('a shelf cell named in the journal is picked first, then followed (O07)', () => {
        fireEvent.click(name('d'));
        expect(s().overview).toMatchObject({ shelfOpen: true, picked: 'd', shelfSelection: 'd' });
        expect(shelfFollowHint(s(), 'd')).toBe(true);
        expect(subjectCellId(s())).toBe('a');
        fireEvent.click(name('d'));
        expect(subjectCellId(s())).toBe('d');
        expect(head()).toBe('Clocks');
        expect(document.querySelector('[data-removed-tag]')).not.toBeNull();
    });

    it('a double click on a name is the two clicks (O07)', () => {
        // The browser's click, click, dblclick.
        fireEvent.click(name('c'), { detail: 1 });
        fireEvent.click(name('c'), { detail: 2 });
        fireEvent.doubleClick(name('c'));
        expect(subjectCellId(s())).toBe('c');
    });

    it('Clear shows every cell and the All cells journal; a board cell then takes two clicks (O07)', () => {
        act(() => s().clearAnchor());
        expect(connectedCells(s())).toBeNull();
        expect(head()).toBe('All cells');
        // Nothing is drawn without a subject.
        expect(document.querySelectorAll('[data-ov-arrows] g')).toHaveLength(0);
        act(() => s().walkTo('a'));
        expect(s().overview.boardPick).toBe('a');
        expect(head()).toBe('All cells');
        act(() => s().walkTo('a'));
        expect(subjectCellId(s())).toBe('a');
        expect(head()).toBe('Frame');
    });

    it('with Only connected off a name is followed at once (O07)', () => {
        act(() => s().toggleConnected());
        fireEvent.click(name('c'));
        expect(subjectCellId(s())).toBe('c');
        expect(head()).toBe('Op log');
    });
});
