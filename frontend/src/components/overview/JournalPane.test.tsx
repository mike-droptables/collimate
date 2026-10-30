// @vitest-environment jsdom
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import type { Channel, Stack } from '@/lib/types';
import type { JournalEvent, JournalRun, Lineage, RemovedCell } from '@/lib/api/cells';

// The journal shows the server's UTC times in local time: run in a zone well
// off UTC (UTC-7 in October), so a UTC time shown raw, or a day taken from
// the UTC date, fails here on any machine.
const zone = vi.hoisted(() => {
    const was = process.env.TZ;
    process.env.TZ = 'America/Los_Angeles';
    return { was };
});

const cells = vi.hoisted(() => ({ fetchJournal: vi.fn(), fetchLineage: vi.fn(), fetchRemovedCells: vi.fn() }));
vi.mock('@/lib/api/cells', async (original) => ({ ...await original<typeof import('@/lib/api/cells')>(), ...cells }));

import { useCollimateStore } from '@/stores/useCollimateStore';
import { ALL_CELLS, defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { JournalPane } from './JournalPane';

// Sunday 4 October 2026, 15:00 local time (22:00 UTC).
const NOW = new Date('2026-10-04T22:00:00+00:00');
/** A local time in October 2026, written as the server sends it (UTC, with its offset). */
const at = (day: number, h: number, m: number) => new Date(2026, 9, day, h, m).toISOString().replace('Z', '+00:00');

const USER = { kind: 'user', runner_id: null, runner_name: null } as const;
const runnerBy = (name: string) => ({ kind: 'runner', runner_id: name, runner_name: name }) as const;
const ref = (cell_id: string, name: string, status: 'open' | 'removed' = 'open') => ({ cell_id, name, status });
const FRAME = ref('a', 'Frame');

const run = (over: Partial<JournalRun> = {}): JournalRun => ({
    run_id: 'r1', runner_id: 'seed-ideas', runner_name: 'seed-ideas', status: 'completed', outcome: null, note: null,
    started_at: at(4, 13, 30), ended_at: at(4, 13, 40), files_written: [], ...over,
});
const event = (t: string, over: Partial<JournalEvent> = {}): JournalEvent => ({
    t, kind: 'edit', by: USER, cells: [FRAME], run: null, files: [], files_total: 0, version_id: null, ...over,
});

const stack = (id: string, name: string, ids: [string, string][], type?: Stack['stack_type']): Stack => ({
    id, name, stack_type: type, cells: ids.map(([cid, cname]) => ({ id: cid, name: cname, files: [], created_at: '2026-10-01T09:00:00' })),
});
const channel: Channel = {
    id: 'ch', name: 'chan', nav_stacks: [], nav_selection_id: null, main_selection_id: 's1',
    main_stacks: [
        stack('s1', 'Options', [['a', 'Frame'], ['b', 'LWW']]),
        stack('s2', '', [['c', 'Op log']]),
        stack('cap', 'Capabilities', [['gen', 'seed-ideas']], 'capabilities'),
    ],
};
const removedD: RemovedCell = {
    cell_id: 'd', tip_version_id: 'dv', name: 'Clocks', icon: null, stack_name: 'Options', stack_cell_count: 2,
    reason: 'removed', created_at: at(3, 10, 0), edited_at: at(3, 11, 0), removed_at: at(4, 9, 0),
    made_by: USER, files: ['SeedIdea.md'], file_count: 1, preview: null, tags: [],
};
const lineage: Lineage = { cells: [], moves: [] };

const s = () => useCollimateStore.getState();

/** Open the journal on `a` with these journals loaded. */
function open(journals: Record<string, JournalEvent[]>, complete = true) {
    useCollimateStore.setState(state => ({
        overviewData: {
            ...state.overviewData, channel: 'chan', removed: [removedD], lineage,
            journals: Object.fromEntries(Object.entries(journals).map(([key, events]) => [key, { events, loading: false, complete, error: null }])),
        },
    }));
    useCollimateStore.setState({ selectedCellId: 'a' });
    s().setJournalOpen(true);
    return render(<div data-overview-root><JournalPane /></div>);
}

const sentences = () => [...document.querySelectorAll('[data-journal-sentence]')].map(el => el.textContent);
const head = () => document.querySelector('[data-journal-pane] h3')!.textContent;

beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    cells.fetchJournal.mockResolvedValue([]);
    useCollimateStore.setState({
        activeChannel: channel, selectedCellId: null, runs: {}, pendingSelectCellId: null,
    });
    s().dropOverviewData();
    useCollimateStore.setState({ overview: defaultOverviewView(), commandPane: defaultCommandPane() });
});
afterEach(() => vi.useRealTimers());
afterAll(() => {
    if (zone.was === undefined) delete process.env.TZ;
    else process.env.TZ = zone.was;
});

describe('the journal pane', () => {
    it('groups the events by local day under day heads, newest first, with local times (O06)', () => {
        // The server's times, UTC; the local day differs from the UTC day for
        // the first (UTC Monday) and for the two on Saturday evening (UTC Sunday).
        open({
            a: [
                event('2026-10-05T03:30:00+00:00'),
                event('2026-10-04T21:05:00+00:00'),
                event('2026-10-04T20:40:00+00:00'),
                event('2026-10-04T05:30:00+00:00'),
                event('2026-10-04T01:05:00+00:00'),
                event('2026-10-02T16:12:00+00:00', { kind: 'make' }),
            ],
        });
        const days = [...document.querySelectorAll('[data-journal-day]')];
        expect(days.map(d => d.firstElementChild!.textContent)).toEqual(['Sun 4 Oct · today', 'Sat 3 Oct', 'Fri 2 Oct']);
        expect(days.map(d => d.querySelectorAll('[data-journal-row]').length)).toEqual([3, 2, 1]);
        const times = [...document.querySelectorAll('[data-journal-row] > span:first-child')].map(el => el.textContent);
        expect(times).toEqual(['20:30', '14:05', '13:40', '22:30', '18:05', '09:12']);
        // The day heads are uppercase by style, sticky under the 36px head.
        expect(days[0].firstElementChild!.className).toMatch(/uppercase/);
        expect(days[0].firstElementChild!.className).toMatch(/sticky top-9/);
    });

    it('writes makes, edits and moves as sentences, every cell by name (O06)', () => {
        open({
            a: [
                event(at(4, 14, 0), { kind: 'make' }),
                event(at(4, 13, 0), { kind: 'make', by: runnerBy('seed-ideas') }),
                event(at(4, 12, 0)),
                event(at(4, 11, 0), { kind: 'duplicate', cells: [ref('b', 'LWW'), ref('d', 'Clocks', 'removed')] }),
                event(at(4, 10, 0), { kind: 'merge', cells: [ref('b', 'LWW'), ref('c', 'Op log'), ref('h', 'Hybrid')] }),
                event(at(4, 9, 0), { kind: 'split', cells: [ref('c', 'Op log'), ref('f', 'Format')] }),
                event(at(4, 8, 0), { kind: 'edit', by: runnerBy('seed-ideas'), cells: [ref('gen', 'seed-ideas')] }),
            ],
        });
        expect(sentences()).toEqual([
            'You made Frame',
            'seed-ideas made Frame',
            'You edited Frame',
            'You duplicated LWW as Clocks',
            'You merged LWW and Op log into Hybrid',
            'You split Format out of Op log',
            // A runner's edit reads the same on every stack, Capabilities included.
            'seed-ideas edited seed-ideas',
        ]);
        const glyphs = [...document.querySelectorAll('[data-journal-row] > span:nth-child(2) > *')]
            .map(g => g.getAttribute('data-move-glyph') ?? (g.hasAttribute('data-pip') ? 'pip' : '?'));
        expect(glyphs).toEqual(['pip', 'pip', 'pip', 'duplicate', 'merge', 'split', 'pip']);
    });

    it('writes runs with their note or state, the new version, the files written and the cells made (O06)', () => {
        open({
            a: [
                event(at(4, 14, 50), { kind: 'run', by: runnerBy('challenge'), run: run({ runner_name: 'challenge', status: 'running', ended_at: null, started_at: at(4, 14, 48) }) }),
                event(at(4, 14, 0), {
                    kind: 'run', run: run({ note: 'reproduced the LWW loss', files_written: ['challenge/lww-repro.log'] }), version_id: 'v9',
                    files_total: 1,
                }),
                event(at(4, 13, 0), { kind: 'run', run: run({ status: 'failed', outcome: 'fail' }) }),
                event(at(4, 12, 0), { kind: 'run', run: run({ outcome: 'pass', files_written: ['a.md', 'b.md', 'c.md', 'd.md'] }), files_total: 5 }),
                event(at(4, 11, 0), { kind: 'run', run: run({ status: 'stopped', note: 'stopped by you' }) }),
                event(at(4, 10, 20), {
                    kind: 'run_cell', run: run({ note: 'three options from the axes' }),
                    cells: [FRAME, ref('b', 'LWW'), ref('c', 'Op log'), ref('h', 'Hybrid', 'removed')],
                }),
                event(at(4, 9, 0), { kind: 'run', run: run({ status: 'timed_out' }) }),
            ],
        });
        expect(sentences()).toEqual([
            'challenge is running on Framerunning for 12m',
            'seed-ideas ran on Framereproduced the LWW loss · new version · wrote lww-repro.log',
            'seed-ideas ran on Framefailed',
            'seed-ideas ran on Framepassed · wrote a.md, b.md, c.md and 2 more',
            'seed-ideas ran on Framestopped by you',
            'seed-ideas ran on Framethree options from the axes · made LWW, Op log and Hybrid',
            'seed-ideas ran on Frametimed out',
        ]);
        const marks = [...document.querySelectorAll('[data-run-mark]')].map(m => m.getAttribute('data-run-mark'));
        expect(marks).toEqual(['running', 'done', 'fail', 'pass', 'stopped', 'done', 'fail']);
        // Runner names in the run style, file names in mono.
        expect(document.querySelector('[data-runner-name]')!.className).toMatch(/text-ink-2/);
        expect(document.querySelector('[data-file-name]')!.className).toMatch(/font-mono/);
        // The running ring is teal with a halo; a pass is green; a failure a red ring.
        const ring = (mark: string) => document.querySelector(`[data-run-mark="${mark}"]`)!.className;
        expect(ring('running')).toMatch(/bg-teal .*shadow-\[0_0_0_3px_var\(--teal-soft\)\]/);
        expect(ring('pass')).toMatch(/border-green bg-green/);
        expect(ring('fail')).toMatch(/border-red/);
        expect(ring('fail')).not.toMatch(/bg-red/);
    });

    it('names the files a run wrote by base name, with a folder suffix where two share one (O01)', () => {
        open({ a: [event(at(4, 12, 0), { kind: 'run', run: run({ files_written: ['Scratch/scratch.txt', 'Scratch (Copy)/scratch.txt', 'Scratch/plot.py'] }), files_total: 3 })] });
        expect(sentences()).toEqual(['seed-ideas ran on Framedone · wrote Scratch/scratch.txt, Scratch (Copy)/scratch.txt and plot.py']);
    });

    it('names a run by its runner\'s name, and by its id when it has none (O09)', () => {
        open({
            a: [
                event(at(4, 12, 0), { kind: 'run', run: run({ runner_id: 'challenge', runner_name: 'Challenge it' }) }),
                event(at(4, 11, 0), { kind: 'run', run: run({ runner_id: 'challenge', runner_name: null }) }),
                event(at(4, 10, 0), { kind: 'run', run: run({ runner_id: 'challenge', runner_name: '' }) }),
            ],
        });
        expect([...document.querySelectorAll('[data-runner-name]')].map(el => el.textContent)).toEqual(['Challenge it', 'challenge', 'challenge']);
    });

    it('walks from a name: a board cell is selected, a shelf cell is picked in the opened shelf (O06)', () => {
        const dup = event(at(4, 11, 0), { kind: 'duplicate', cells: [ref('c', 'Op log'), ref('d', 'Clocks', 'removed')] });
        open({ a: [dup], c: [dup] });
        const name = (id: string) => document.querySelector<HTMLElement>(`[data-cell-name="${id}"]`)!;
        expect(name('c').tagName).toBe('BUTTON');
        // A removed cell's name is underlined dashed.
        expect(name('d').className).toMatch(/decoration-dashed/);
        fireEvent.click(name('c'));
        expect(s().pendingSelectCellId).toBe('c');
        // With Only connected off the walk makes it the subject.
        expect(head()).toBe('Op log');
        fireEvent.click(name('d'));
        expect(s().overview).toMatchObject({ shelfOpen: true, shelfSelection: 'd', picked: 'd', scrollTo: 'd', anchor: 'd' });
    });

    it('a cell the overview does not show is named but does not walk (O06)', () => {
        open({ a: [event(at(4, 11, 0), { kind: 'split', cells: [FRAME, ref('zz', 'Elsewhere', 'removed')] })] });
        expect(document.querySelector('[data-cell-name="zz"]')!.tagName).toBe('SPAN');
    });

    it('heads the pane with the cell, its stack and the event count, and tags a shelf cell removed (O06)', () => {
        open({ a: [event(at(4, 14, 0)), event(at(4, 13, 0))], d: [event(at(3, 11, 0), { cells: [ref('d', 'Clocks', 'removed')] })] });
        expect(head()).toBe('Frame');
        const pane = document.querySelector('[data-journal-pane]')!;
        expect(pane).toHaveTextContent('Options');
        expect(pane).toHaveTextContent('2 events');
        expect(document.querySelector('[data-removed-tag]')).toBeNull();
        act(() => s().pickShelf('d'));
        expect(head()).toBe('Clocks');
        expect(document.querySelector('[data-removed-tag]')).toHaveTextContent('removed');
        expect(pane).toHaveTextContent('1 event');
    });

    it('a removed cell\'s head names its stack only when the shelf record says it came from a multi-cell stack (O03)', () => {
        const d = [event(at(3, 11, 0), { cells: [ref('d', 'Clocks', 'removed')] })];
        const view = open({ d });
        act(() => s().pickShelf('d'));
        const pane = () => document.querySelector('[data-journal-pane]')!;
        expect(pane()).toHaveTextContent('Options');
        view.unmount();
        // The same cell, removed from a stack it was alone in.
        open({ d });
        act(() => useCollimateStore.setState(state => ({
            overviewData: { ...state.overviewData, removed: [{ ...removedD, stack_cell_count: 1 }] },
        })));
        act(() => s().pickShelf('d'));
        expect(head()).toBe('Clocks');
        expect(pane()).not.toHaveTextContent('Options');
    });

    it('after Clear it is the All cells journal (O06)', () => {
        open({
            a: [event(at(4, 14, 0))],
            [ALL_CELLS]: [event(at(4, 14, 0)), event(at(4, 13, 0), { cells: [ref('c', 'Op log')] })],
        });
        act(() => { s().toggleConnected(); s().clearAnchor(); });
        expect(head()).toBe('All cells');
        expect(sentences()).toEqual(['You edited Frame', 'You edited Op log']);
        expect(cells.fetchJournal).not.toHaveBeenCalled();
    });

    it('loads its first page when it is not cached, and the next page at its foot (O06)', async () => {
        cells.fetchJournal.mockResolvedValueOnce([event(at(4, 14, 0))]);
        open({ a: [event(at(4, 14, 0)), event(at(4, 12, 0))] }, false);
        const pane = document.querySelector('[data-journal-pane]')!;
        expect(pane).toHaveTextContent('2+ events');
        await act(async () => { fireEvent.scroll(pane); });
        expect(cells.fetchJournal).toHaveBeenCalledWith('chan', 'a', { before: at(4, 12, 0), limit: 100 });
        await act(async () => { s().pickShelf('d'); });
        expect(cells.fetchJournal).toHaveBeenLastCalledWith('chan', 'd', { before: null, limit: 100 });
    });

    it('Hide shuts the journal; with no cell selected the pane says so (O06)', () => {
        open({});
        act(() => { useCollimateStore.setState({ selectedCellId: null }); });
        expect(head()).toBe('Journal');
        expect(screen.getByText('Select a cell to read its journal.')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Hide the journal' }));
        expect(s().overview.journalOpen).toBe(false);
        expect(document.querySelector('[data-journal-pane]')).toBeNull();
    });

    it('the head\'s Hide is the mock\'s cross, named Hide the journal (polish)', () => {
        open({ a: [event(at(4, 14, 0))] });
        const hide = screen.getByRole('button', { name: 'Hide the journal' });
        const glyph = hide.querySelector('svg[data-journal-hide-glyph]')!;
        expect(glyph.getAttribute('viewBox')).toBe('0 0 16 16');
        expect(glyph.querySelector('path')!.getAttribute('d')).toBe('M4.5 4.5l7 7M11.5 4.5l-7 7');
        // Nothing else draws in the button: the panel glyph is gone.
        expect(hide.querySelectorAll('svg')).toHaveLength(1);
        fireEvent.click(hide);
        expect(s().overview.journalOpen).toBe(false);
    });

    it('scrolls on its own and keeps presses off the board (O06)', () => {
        open({ a: [event(at(4, 14, 0))] });
        const pane = document.querySelector<HTMLElement>('[data-journal-pane]')!;
        expect(pane.className).toMatch(/overflow-y-auto/);
        expect(pane.style.width).toBe('374px');
        const root = document.querySelector('[data-overview-root]')!;
        const pan = vi.fn();
        root.addEventListener('pointerdown', pan);
        root.addEventListener('wheel', pan);
        fireEvent.pointerDown(pane);
        fireEvent.wheel(pane);
        expect(pan).not.toHaveBeenCalled();
    });

    it('docks against the Commands pane\'s inner edge while the pane is open on the right, sliding over 160ms (polish)', () => {
        open({ a: [event(at(4, 14, 0))] });
        const pane = document.querySelector<HTMLElement>('[data-journal-pane]')!;
        expect(pane.style.right).toBe('0px');
        expect(pane.className).not.toMatch(/\bright-0\b/);
        act(() => {
            s().setCommandPaneSide('right');
            s().openCommandPane();
        });
        expect(pane.style.right).toBe('352px');
        expect(pane.style.transition).toBe('right 160ms ease');
        // Docked on the left the pane is on the other side of the overview.
        act(() => s().setCommandPaneSide('left'));
        expect(pane.style.right).toBe('0px');
        act(() => {
            s().setCommandPaneSide('right');
            s().closeCommandPane();
        });
        expect(pane.style.right).toBe('0px');
    });
});
it('cell renames display historical literal names and a separate current-cell link with explicit actor',()=>{
 open({a:[event(at(4,14,0),{kind:'cell_renamed',old_name:'Old literal',new_name:'New literal',cells:[ref('a','Frame')],by:runnerBy('Captured runner')})]});
 expect(sentences()).toEqual(['Captured runner renamed “Old literal” to “New literal”Frame']);
 const row=document.querySelector('[data-journal-row]')!;
 expect(row.textContent).toContain('Old literal');expect(row.textContent).toContain('New literal');
 expect([...row.querySelectorAll('button')].some(button=>button.textContent==='Frame')).toBe(true);
});
