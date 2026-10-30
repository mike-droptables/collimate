// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { Channel, Stack } from '@/lib/types';
import type { Lineage, RemovedCell } from '@/lib/api/cells';

const cells = vi.hoisted(() => ({
    fetchRemovedCells: vi.fn(), fetchLineage: vi.fn(), fetchJournal: vi.fn(),
    restoreRemovedCell: vi.fn(), removeCellById: vi.fn(), putRemovedCellTags: vi.fn(), fetchChannelTags: vi.fn(),
}));
vi.mock('@/lib/api/cells', async (original) => ({ ...await original<typeof import('@/lib/api/cells')>(), ...cells }));
const stacksApi = vi.hoisted(() => ({ moveStackTo: vi.fn() }));
vi.mock('@/lib/api/stacks', async (original) => ({ ...await original<typeof import('@/lib/api/stacks')>(), ...stacksApi }));
const history = vi.hoisted(() => ({ fetchCellHistory: vi.fn() }));
vi.mock('@/lib/api/history', async (original) => ({ ...await original<typeof import('@/lib/api/history')>(), ...history }));

import { useCollimateStore } from '@/stores/useCollimateStore';
import { REMOVE_RUNNING, defaultOverviewView, type OverviewDrop } from '@/stores/collimate/overviewSlice';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { isCellOccluded, resetGuestOcclusion } from '@/lib/guestOcclusion';
import { OverviewCard } from './OverviewCard';
import { RemovedShelf } from './RemovedShelf';
import { shelfColumns } from './shelfColumns';

const removed = (id: string, over: Partial<RemovedCell> = {}): RemovedCell => ({
    cell_id: id, tip_version_id: `${id}-v`, name: id.toUpperCase(), icon: null, stack_name: null, stack_cell_count: 1,
    reason: 'removed', created_at: '2026-10-01T10:00:00+00:00', edited_at: '2026-10-02T10:00:00+00:00',
    removed_at: '2026-10-03T10:00:00+00:00', made_by: { kind: 'user', runner_id: null, runner_name: null },
    files: ['a.md'], file_count: 1, preview: null, tags: [], ...over,
});

// On the shelf: x (made first, edited last), y, z. On the board: a, b.
const SHELF = [
    removed('x', { created_at: '2026-10-01T08:00:00+00:00', edited_at: '2026-10-05T08:00:00+00:00' }),
    removed('y', { created_at: '2026-10-02T08:00:00+00:00', edited_at: '2026-10-04T08:00:00+00:00' }),
    removed('z', { created_at: '2026-10-03T08:00:00+00:00', edited_at: '2026-10-03T09:00:00+00:00' }),
];
const stack = (id: string, ids: string[]): Stack => ({
    id, name: id, cells: ids.map(cid => ({ id: cid, name: cid.toUpperCase(), files: [], created_at: '2026-10-01T00:00:00' })),
});
const channel: Channel = {
    id: 'ch', name: 'chan', nav_stacks: [], nav_selection_id: null, main_selection_id: 's1',
    main_stacks: [stack('s1', ['a']), stack('s2', ['b'])],
};
// a was duplicated as y; b has no lines.
const lineage: Lineage = {
    cells: [],
    moves: [{ id: 1, op: 'duplicate', at: '2026-10-02T10:00:00+00:00', op_id: 'o1', from: [{ cell_id: 'a', version_id: null }], to: [{ cell_id: 'y', version_id: null }] }],
};

const s = () => useCollimateStore.getState();
const order = () => [...document.querySelectorAll('[data-rm-card]')].map(el => el.getAttribute('data-rm-card'));
const cardOf = (id: string) => document.querySelector<HTMLElement>(`[data-rm-card="${id}"]`)!;
const count = () => document.querySelector('[data-rm-count]')!.textContent;
const requestSelectCell = vi.fn();

async function mount(over: Partial<ReturnType<typeof defaultOverviewView>> = {}, list = SHELF) {
    cells.fetchRemovedCells.mockImplementation(async (_ch: string, opts: { q?: string }) =>
        opts.q ? list.filter(entry => entry.cell_id === opts.q) : list);
    useCollimateStore.setState({ overview: { ...defaultOverviewView(), shelfOpen: true, ...over } });
    await act(async () => { await s().loadOverview('chan'); });
    return render(<RemovedShelf />);
}

beforeEach(() => {
    vi.clearAllMocks();
    resetGuestOcclusion();
    cells.fetchLineage.mockResolvedValue(lineage);
    cells.fetchJournal.mockResolvedValue([]);
    cells.fetchChannelTags.mockResolvedValue([]);
    cells.putRemovedCellTags.mockImplementation(async (_ch: string, id: string, tags: string[]) => ({ cell_id: id, tags }));
    history.fetchCellHistory.mockResolvedValue({ nodes: [], total_versions: 1, current: { node_id: null, version_cell_id: 'v' } });
    useCollimateStore.setState({
        activeChannel: channel, selectedCellId: null, runs: {}, pendingSelectCellId: null,
        applyOptimisticChannel: vi.fn(), applyOptimisticLayout: vi.fn(), requestSelectCell,
        commandPane: defaultCommandPane(),
    });
    s().dropOverviewData();
});

describe('RemovedShelf', () => {
    it('sorts by Last edited, newest first, and by Created or ascending on a click (O05)', async () => {
        await mount();
        expect(order()).toEqual(['x', 'y', 'z']);
        expect(screen.getByRole('button', { name: 'Last edited' })).toHaveAttribute('aria-pressed', 'true');
        fireEvent.click(screen.getByRole('button', { name: 'Created' }));
        expect(s().overview.sort).toBe('created');
        expect(order()).toEqual(['z', 'y', 'x']);
        expect(cardOf('z')).toHaveTextContent('Created');
        fireEvent.click(screen.getByRole('button', { name: 'Asc' }));
        expect(screen.getByRole('button', { name: 'Asc' })).toHaveAttribute('aria-pressed', 'true');
        expect(order()).toEqual(['x', 'y', 'z']);
        expect(cells.fetchRemovedCells).toHaveBeenCalledTimes(1);
    });

    it('counts the shelf, and "k of n" while a search narrows it on the server (O05)', async () => {
        await mount();
        expect(count()).toBe('3 removed');
        fireEvent.change(screen.getByRole('textbox', { name: 'Search removed cells' }), { target: { value: 'y' } });
        await waitFor(() => expect(order()).toEqual(['y']));
        expect(cells.fetchRemovedCells).toHaveBeenLastCalledWith('chan', { sort: 'edited', dir: 'desc', q: 'y' });
        expect(count()).toBe('1 of 3 removed');
    });

    it('Escape clears the search, then lets the field go (O05)', async () => {
        await mount({ query: 'zz' });
        const field = screen.getByRole('textbox', { name: 'Search removed cells' });
        expect(field).toHaveValue('zz');
        field.focus();
        fireEvent.keyDown(field, { key: 'Escape' });
        expect(s().overview.query).toBe('');
        expect(field).toHaveValue('');
        expect(order()).toEqual(['x', 'y', 'z']);
        fireEvent.keyDown(field, { key: 'Escape' });
        expect(document.activeElement).not.toBe(field);
    });

    it('the first card carries Restore until another is clicked (O05)', async () => {
        await mount();
        expect(within(cardOf('x')).getByRole('button', { name: /Restore/ })).toBeInTheDocument();
        fireEvent.click(cardOf('y'));
        expect(s().overview.shelfSelection).toBe('y');
        expect(within(cardOf('x')).queryByRole('button', { name: /Restore/ })).toBeNull();
        expect(within(cardOf('y')).getByRole('button', { name: /Restore/ })).toBeInTheDocument();
        // With Only connected off a click also makes it the anchor.
        expect(s().overview.anchor).toBe('y');
    });

    it('Restore restores the cell and the board selects it (O05)', async () => {
        const shelf = [...SHELF];
        cells.restoreRemovedCell.mockImplementation(async () => {
            shelf.splice(shelf.findIndex(entry => entry.cell_id === 'y'), 1);
            return { ok: true, cell_id: 'y', stack_id: 's3', channel };
        });
        await mount({ shelfSelection: 'y' }, shelf);
        fireEvent.click(within(cardOf('y')).getByRole('button', { name: /Restore/ }));
        await waitFor(() => expect(requestSelectCell).toHaveBeenCalledWith('y'));
        expect(cells.restoreRemovedCell).toHaveBeenCalledWith('chan', 'y');
        await waitFor(() => expect(order()).toEqual(['x', 'z']));
    });

    it('a failed Restore says why (O05)', async () => {
        cells.restoreRemovedCell.mockRejectedValue(new Error('the cell is on the canvas'));
        await mount();
        fireEvent.click(within(cardOf('x')).getByRole('button', { name: /Restore/ }));
        expect(await screen.findByRole('alert')).toHaveTextContent('the cell is on the canvas');
        expect(order()).toContain('x');
    });

    it('with Only connected on, a first click picks and a second click follows its lines (O05)', async () => {
        await mount({ journalOpen: true, onlyConnected: true, cleared: true });
        fireEvent.click(cardOf('y'), { detail: 1 });
        expect(s().overview).toMatchObject({ shelfSelection: 'y', picked: 'y', anchor: null });
        expect(cardOf('y').querySelector('[data-follow-hint]')).toHaveTextContent('Click again to follow its lines');
        fireEvent.click(cardOf('y'), { detail: 1, timeStamp: 5000 });
        expect(s().overview.anchor).toBe('y');
        expect(cardOf('y').querySelector('[data-follow-hint]')).toBeNull();
        // The anchor's card takes the journal style while the journal is open.
        expect(cardOf('y')).toHaveAttribute('data-journal-style', 'true');
    });

    it('a double click acts on the card its first click hit (O05)', async () => {
        await mount({ journalOpen: true, onlyConnected: true, cleared: true });
        // The second click of the double click lands on another card.
        fireEvent.click(cardOf('y'), { detail: 1 });
        fireEvent.click(cardOf('z'), { detail: 2 });
        expect(s().overview).toMatchObject({ anchor: 'y', picked: 'y', shelfSelection: 'y' });
        // y's lines lead to a on the board, not to z: Only connected lets z go.
        expect(order()).toEqual(['y']);
        await waitFor(() => expect(history.fetchCellHistory).toHaveBeenCalled());
    });

    it('a double click whose first click already followed does nothing more (O05)', async () => {
        const pickShelf = vi.spyOn(s(), 'pickShelf');
        const anchorGhost = vi.spyOn(s(), 'anchorGhost');
        useCollimateStore.setState({ pickShelf, anchorGhost });
        await mount({ journalOpen: true, onlyConnected: true, picked: 'z', shelfSelection: 'z' });
        fireEvent.click(cardOf('z'), { detail: 1 });
        expect(s().overview).toMatchObject({ anchor: 'z', picked: 'z' });
        fireEvent.click(cardOf('z'), { detail: 2 });
        expect(pickShelf).toHaveBeenCalledTimes(1);
        expect(anchorGhost).not.toHaveBeenCalled();
        expect(s().overview).toMatchObject({ anchor: 'z', picked: 'z', shelfSelection: 'z' });
    });

    it('says when nothing has been removed (O05)', async () => {
        await mount({}, []);
        expect(screen.getByText('Nothing has been removed.')).toBeInTheDocument();
        expect(count()).toBe('0 removed');
    });

    it('says when no removed cell matches the search (O05)', async () => {
        await mount();
        fireEvent.change(screen.getByRole('textbox', { name: 'Search removed cells' }), { target: { value: 'nothing' } });
        expect(await screen.findByText('No removed cell matches.')).toBeInTheDocument();
        expect(count()).toBe('0 of 3 removed');
    });

    it('keeps only the cells connected to the subject, and names it when none is (O05)', async () => {
        useCollimateStore.setState({ selectedCellId: 'a' });
        await mount({ journalOpen: true, onlyConnected: true });
        expect(order()).toEqual(['y']);
        expect(count()).toBe('1 of 3 removed');
        act(() => { useCollimateStore.setState({ selectedCellId: 'b', overview: { ...s().overview, anchor: 'b' } }); });
        expect(screen.getByText('No removed cell is connected to B.')).toBeInTheDocument();
    });

    it('freezes native guests while open and lets its cards go once shut (O05)', async () => {
        vi.useFakeTimers();
        try {
            useCollimateStore.setState({ overview: { ...defaultOverviewView(), shelfOpen: true } });
            useCollimateStore.setState(prev => ({ overviewData: { ...prev.overviewData, channel: 'chan', removed: SHELF } }));
            render(<RemovedShelf />);
            expect(isCellOccluded(null)).toBe(true);
            act(() => s().setShelfOpen(false));
            expect(isCellOccluded(null)).toBe(false);
            expect(order()).toHaveLength(3);
            act(() => { vi.advanceTimersByTime(300); });
            expect(order()).toHaveLength(0);
        } finally {
            vi.useRealTimers();
        }
    });

    it('its cards keep the wheel; over the control row the wheel reaches the overview and pans the board (O05)', async () => {
        const { container } = await mount();
        const reached = vi.fn();
        container.addEventListener('wheel', reached);
        fireEvent.wheel(document.querySelector('[data-rm-grid]')!);
        fireEvent.wheel(document.querySelector('[data-rm-card]')!);
        expect(reached).not.toHaveBeenCalled();
        fireEvent.wheel(screen.getByRole('textbox', { name: 'Search removed cells' }));
        expect(reached).toHaveBeenCalledTimes(1);
    });

    it('scrolls a card walked to into view, once (O05)', async () => {
        await mount();
        const grid = document.querySelector<HTMLElement>('[data-rm-grid]')!;
        Object.defineProperty(grid, 'clientHeight', { configurable: true, value: 200 });
        Object.defineProperty(grid, 'scrollTop', { configurable: true, writable: true, value: 0 });
        Object.defineProperty(cardOf('y'), 'offsetTop', { configurable: true, value: 100 });
        Object.defineProperty(cardOf('z'), 'offsetTop', { configurable: true, value: 600 });
        // A card already in view leaves the grid where it is.
        act(() => s().walkTo('y'));
        expect(s().overview.scrollTo).toBeNull();
        expect(grid.scrollTop).toBe(0);
        // A card below the fold comes up to the grid's top padding.
        act(() => s().walkTo('z'));
        expect(s().overview.scrollTo).toBeNull();
        expect(s().overview.shelfSelection).toBe('z');
        expect(grid.scrollTop).toBe(586);
    });
});

describe('the shelf\'s last-removed sort and tag filter chips', () => {
    // x: done, ui; y: done; z: wip.
    const TAGGED = [
        removed('x', { removed_at: '2026-10-04T08:00:00+00:00', tags: ['done', 'ui'] }),
        removed('y', { removed_at: '2026-10-09T08:00:00+00:00', tags: ['Done'] }),
        removed('z', { removed_at: '2026-10-06T08:00:00+00:00', tags: ['wip'] }),
    ];
    const field = () => screen.getByRole('textbox', { name: 'Search removed cells' });
    const chips = () => [...document.querySelectorAll('[data-rm-tag-filter]')].map(el => el.getAttribute('data-rm-tag-filter'));
    const suggestions = () => [...document.querySelectorAll('[data-rm-tag-suggestion]')].map(el => el.getAttribute('data-rm-tag-suggestion'));
    const type = (text: string) => {
        field().focus();
        fireEvent.change(field(), { target: { value: text } });
    };

    it('sorts by Last removed, the card saying when (TG-04)', async () => {
        await mount({}, TAGGED);
        fireEvent.click(screen.getByRole('button', { name: 'Last removed' }));
        expect(s().overview.sort).toBe('removed');
        expect(order()).toEqual(['y', 'z', 'x']);
        expect(cardOf('y')).toHaveTextContent('Removed');
        fireEvent.click(screen.getByRole('button', { name: 'Asc' }));
        expect(order()).toEqual(['x', 'z', 'y']);
        expect(cells.fetchRemovedCells).toHaveBeenCalledTimes(1);
    });

    it('typing lists the shelf\'s matching tags under the search; Enter makes the match a chip that filters, and clears the text (TG-04)', async () => {
        await mount({}, TAGGED);
        type('DO');
        expect(screen.getByRole('listbox', { name: 'Matching tags' })).toBeInTheDocument();
        // One per case-folded spelling: the first loaded spelling.
        expect(suggestions()).toEqual(['done']);
        fireEvent.keyDown(field(), { key: 'Enter' });
        expect(s().overview.tagFilters).toEqual(['done']);
        expect(field()).toHaveValue('');
        expect(chips()).toEqual(['done']);
        expect(order()).toEqual(['x', 'y']);
        expect(count()).toBe('2 of 3 removed');
        // The chips sit between the search and the two icon buttons.
        const row = document.querySelector('[data-rm-controls]')!;
        const kids = [...row.children];
        const chip = document.querySelector('[data-rm-tag-filter]')!;
        expect(kids.indexOf(chip)).toBeGreaterThan(kids.indexOf(field().closest('label')!));
        expect(kids.indexOf(chip)).toBeLessThan(kids.indexOf(screen.getByRole('button', { name: 'Show tags on cards' })));
        expect(kids.indexOf(chip)).toBeLessThan(kids.indexOf(screen.getByRole('button', { name: 'Remove zones' })));
        expect(kids.indexOf(document.querySelector('[data-rm-count]')!)).toBeGreaterThan(kids.indexOf(chip));
        // A chip already chosen is no suggestion; a click on one adds it, and chips AND together.
        type('d');
        expect(suggestions()).toEqual([]);
        type('u');
        expect(suggestions()).toEqual(['ui']);
        fireEvent.click(document.querySelector('[data-rm-tag-suggestion="ui"]')!);
        expect(chips()).toEqual(['done', 'ui']);
        expect(order()).toEqual(['x']);
        expect(field()).toHaveValue('');
    });

    it('a tag filter that is on is teal, the skin\'s on-state, never brass (TG-04, teal filters)', async () => {
        await mount({ tagFilters: ['done'] }, TAGGED);
        const chip = document.querySelector('[data-rm-tag-filter="done"]')!;
        const cls = chip.className.split(/\s+/);
        expect(cls).toEqual(expect.arrayContaining(['border-teal/50', 'bg-teal-soft', 'text-ink']));
        expect(chip.querySelector('svg.lucide-tag')!.getAttribute('class')).toContain('text-teal');
        expect(chip.outerHTML).not.toContain('brass');
    });

    it('a shelf card carries the tag menu: its tags change on the removed record, the chips and filters follow at once (TG-07)', async () => {
        await mount({ tagFilters: ['done'] }, TAGGED);
        expect(order()).toEqual(['x', 'y']);
        const trigger = within(cardOf('x')).getByRole('button', { name: 'Tags' });
        expect(trigger.textContent).toBe('2');
        // The head keeps the trigger before Restore, right-anchored.
        const restore = within(cardOf('x')).getByRole('button', { name: /Restore/ });
        expect(trigger.compareDocumentPosition(restore) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        fireEvent.click(trigger);
        const dialog = await screen.findByRole('dialog', { name: 'Tags' });
        // Opening it neither picked nor anchored another card.
        expect(s().overview.shelfSelection).toBeNull();
        await waitFor(() => expect(cells.fetchChannelTags).toHaveBeenCalledWith('chan'));
        // The vocabulary holds the shelf's tags; a toggle adds one.
        fireEvent.click(within(dialog).getByRole('button', { name: 'wip' }));
        expect(cells.putRemovedCellTags).toHaveBeenLastCalledWith('chan', 'x', ['done', 'ui', 'wip']);
        await waitFor(() => expect(trigger.textContent).toBe('3'));
        expect(cardOf('x').querySelector('[data-rm-tags]')).toHaveTextContent('wip');
        // Taking off the filtered tag takes the card off the filtered shelf.
        fireEvent.click(within(dialog).getByRole('button', { name: 'Remove done' }));
        expect(cells.putRemovedCellTags).toHaveBeenLastCalledWith('chan', 'x', ['ui', 'wip']);
        await waitFor(() => expect(order()).toEqual(['y']));
        expect(s().overviewData.removed!.find(e => e.cell_id === 'x')!.tags).toEqual(['ui', 'wip']);
    });

    it('a refused tag edit on a shelf card puts back the list it showed and says why (TG-07)', async () => {
        await mount({}, TAGGED);
        cells.putRemovedCellTags.mockRejectedValueOnce(new Error('Cell is on the canvas'));
        fireEvent.click(within(cardOf('z')).getByRole('button', { name: 'Tags' }));
        const dialog = await screen.findByRole('dialog', { name: 'Tags' });
        fireEvent.click(within(dialog).getByRole('button', { name: 'Remove wip' }));
        await waitFor(() => expect(s().overviewData.error).toBeTruthy());
        expect(s().overviewData.removed!.find(e => e.cell_id === 'z')!.tags).toEqual(['wip']);
    });

    it('the arrows pick among the matches; the list goes when the search loses focus (TG-04)', async () => {
        await mount({}, [...TAGGED, removed('w', { tags: ['wiki'] })]);
        type('wi');
        expect(suggestions()).toEqual(['wiki', 'wip']);
        fireEvent.keyDown(field(), { key: 'ArrowDown' });
        expect(screen.getByRole('option', { name: 'wip' })).toHaveAttribute('aria-selected', 'true');
        fireEvent.keyDown(field(), { key: 'Enter' });
        expect(chips()).toEqual(['wip']);
        type('wi');
        fireEvent.blur(field());
        expect(screen.queryByRole('listbox')).toBeNull();
    });

    it('Backspace on an empty search drops the last chip, and a chip\'s x drops it (TG-04)', async () => {
        await mount({}, TAGGED);
        act(() => { s().addTagFilter('done'); s().addTagFilter('ui'); });
        field().focus();
        fireEvent.keyDown(field(), { key: 'Backspace' });
        expect(chips()).toEqual(['done']);
        type('x');
        fireEvent.keyDown(field(), { key: 'Backspace' });
        expect(chips()).toEqual(['done']);
        fireEvent.click(screen.getByRole('button', { name: 'Stop filtering by done' }));
        expect(chips()).toEqual([]);
        expect(order()).toEqual(['x', 'y', 'z']);
    });

    it('the config button, right of the search and the tag toggle when no chip is chosen, opens the remove zones editor (TG-06)', async () => {
        await mount();
        const button = screen.getByRole('button', { name: 'Remove zones' });
        const row = [...document.querySelector('[data-rm-controls]')!.children];
        expect(row.indexOf(button)).toBe(row.indexOf(field().closest('label')!) + 2);
        expect(button).toHaveAttribute('aria-expanded', 'false');
        fireEvent.click(button);
        expect(screen.getByRole('dialog', { name: 'Remove zones' })).toBeInTheDocument();
        expect(button).toHaveAttribute('aria-expanded', 'true');
        fireEvent.click(button);
        expect(screen.queryByRole('dialog', { name: 'Remove zones' })).toBeNull();
    });

    it('the tag toggle, just left of the remove zones button, shows every board card\'s tags and says so (tags toggle)', async () => {
        await mount();
        const toggle = screen.getByRole('button', { name: 'Show tags on cards' });
        expect(toggle).toHaveAttribute('title', 'Show tags on cards');
        const row = [...document.querySelector('[data-rm-controls]')!.children];
        expect(row.indexOf(toggle)).toBe(row.indexOf(screen.getByRole('button', { name: 'Remove zones' })) - 1);
        expect(toggle.querySelector('svg.lucide-tag')).not.toBeNull();
        expect(toggle).toHaveAttribute('aria-pressed', 'false');
        expect(toggle.className.split(/\s+/)).toEqual(expect.arrayContaining(['h-6', 'w-6', 'text-ink-2']));
        fireEvent.click(toggle);
        expect(s().overview.showTags).toBe(true);
        expect(toggle).toHaveAttribute('aria-pressed', 'true');
        expect(toggle.className.split(/\s+/)).toEqual(expect.arrayContaining(['bg-sheet-2', 'text-ink']));
        fireEvent.click(toggle);
        expect(s().overview.showTags).toBe(false);
        expect(toggle).toHaveAttribute('aria-pressed', 'false');
    });

    it('the shelf\'s cards keep their one-line chips whatever the toggle (tags toggle)', async () => {
        await mount({ showTags: false }, [removed('x', { tags: ['wip'] })]);
        expect(cardOf('x').querySelector('[data-rm-tags]')).toHaveTextContent('wip');
        fireEvent.click(screen.getByRole('button', { name: 'Show tags on cards' }));
        expect(cardOf('x').querySelector('[data-rm-tags]')).toHaveTextContent('wip');
        expect(cardOf('x').querySelector('[data-ov-tags]')).toBeNull();
    });

    it('a board card held over the shelf splits it into the zones and lights the one under it (TG-06)', async () => {
        await mount();
        act(() => {
            s().adoptRemoveZones([
                { id: 'z1', width: 0.5, action: { kind: 'add_tag', tag: 'done' } },
                { id: 'z2', width: 0.5, action: { kind: 'remove' } },
            ]);
            s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'shelf', zone: 1, refusal: null });
        });
        const strip = document.querySelector<HTMLElement>('[data-rm-shelf] [data-rm-zone-strip]')!;
        expect([...strip.querySelectorAll('[data-rm-zone]')].map(el => el.textContent)).toEqual(['+ done', 'Standard']);
        expect(strip.querySelector('[data-lit]')).toHaveAttribute('data-rm-zone', '1');
        act(() => { s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'shelf', zone: 0, refusal: null }); });
        expect(strip.querySelector('[data-lit]')).toHaveAttribute('data-rm-zone', '0');
    });
});

describe('Remove on a board card, onto the shelf', () => {
    const board = stack('s1', ['a']).cells[0];
    const run = { runId: 'r1', cellId: 'a', channel: 'chan', runnerId: 'w', runnerName: 'writer', generatorName: 'g', label: 'w-r1', folder: '_runs/w-r1' };
    const removeButton = () => within(document.querySelector<HTMLElement>('[data-ov-card="a"]')!).getByRole('button', { name: /Remove/ });

    it('is disabled while the cell runs, and a click does not remove it (O05)', async () => {
        await mount();
        useCollimateStore.setState({ runs: { r1: run }, stoppingRunIds: new Set() });
        render(<OverviewCard cell={board} selected isOffscreen={false} />);
        expect(removeButton()).toBeDisabled();
        expect(removeButton()).toHaveAttribute('title', 'Stop its runs to remove it');
        fireEvent.click(removeButton());
        expect(cells.removeCellById).not.toHaveBeenCalled();
    });

    it('removes the idle cell; the shelf selects it, brings it into view and gives it Restore in the same head slot (O05)', async () => {
        const shelf = [...SHELF];
        cells.removeCellById.mockImplementation(async () => {
            shelf.unshift(removed('a', { edited_at: '2026-10-06T08:00:00+00:00' }));
            return { main_stacks: [stack('s2', ['b'])], main_selection_id: 's2' };
        });
        await mount({}, shelf);
        cleanup();
        render(<OverviewCard cell={board} selected isOffscreen={false} />);
        expect(removeButton()).toBeEnabled();
        // The board card's head: its title, then its controls, Remove last.
        const boardHead = removeButton().closest('[data-ov-card] > div')!;
        expect(boardHead.querySelector('h3')).toHaveTextContent('A');
        fireEvent.click(removeButton());
        await waitFor(() => expect(cells.removeCellById).toHaveBeenCalledWith('chan', 'a'));
        await waitFor(() => expect(s().overview).toMatchObject({ shelfSelection: 'a', scrollTo: 'a' }));
        await waitFor(() => expect(s().overviewData.removed?.map(entry => entry.cell_id)).toContain('a'));
        cleanup();
        render(<RemovedShelf />);
        const restore = within(cardOf('a')).getByRole('button', { name: /Restore/ });
        // Restore takes Remove's place: the last control of the head, after
        // the title and the Tags button, in the same right-anchored group.
        const head = cardOf('a').firstElementChild!;
        const controls = restore.parentElement!;
        expect(controls.parentElement).toBe(head);
        expect(head.lastElementChild).toBe(controls);
        expect(controls.lastElementChild).toBe(restore);
        expect(controls.className).toContain('ml-auto');
        expect(within(controls as HTMLElement).getByRole('button', { name: 'Tags' })).toBeInTheDocument();
        expect(head.querySelector('h3')).toHaveTextContent('A');
        expect([...boardHead.querySelectorAll('button')].at(-1)?.textContent).toBe('Remove');
        expect(s().overview.scrollTo).toBeNull();
    });
});

describe('dragging between the shelf and the board', () => {
    const rect = (left: number, top: number, right: number, bottom: number) => ({
        left, top, right, bottom, x: left, y: top, width: right - left, height: bottom - top, toJSON: () => ({}),
    }) as DOMRect;
    const grid = () => document.querySelector<HTMLElement>('[data-rm-grid]')!;
    /** The grid's cards (the ghost, outside it, is a card too). */
    const order = () => [...grid().querySelectorAll('[data-rm-card]')].map(el => el.getAttribute('data-rm-card'));
    const shelf = () => document.querySelector<HTMLElement>('[data-rm-shelf]')!;

    /** The shelf over a board (from y 346 down, 660 wide), as the overview lays them out. */
    async function mountOverOverview(over: Partial<ReturnType<typeof defaultOverviewView>> = {}) {
        cells.fetchRemovedCells.mockResolvedValue(SHELF);
        useCollimateStore.setState({ overview: { ...defaultOverviewView(), shelfOpen: true, ...over } });
        await act(async () => { await s().loadOverview('chan'); });
        const view = render(<div data-overview-root><RemovedShelf /><div data-overview-board /></div>);
        const board = document.querySelector<HTMLElement>('[data-overview-board]')!;
        board.getBoundingClientRect = () => rect(0, 346, 660, 800);
        for (const card of document.querySelectorAll<HTMLElement>('[data-rm-card]')) {
            card.getBoundingClientRect = () => rect(18, 50, 254, 298);
        }
        return view;
    }
    /** Press card `id` at (100, 100) and carry it to (x, y). */
    const lift = (id: string, x: number, y: number) => {
        fireEvent.pointerDown(cardOf(id), { button: 0, clientX: 100, clientY: 100, pointerId: 7 });
        fireEvent.pointerMove(grid(), { clientX: 104, clientY: 102, pointerId: 7 });   // under the 5px lift
        fireEvent.pointerMove(grid(), { clientX: x, clientY: y, pointerId: 7 });
    };

    it('lifts a card past 5px: the grid reflows without it and a dimmed ghost follows the pointer (polish)', async () => {
        await mountOverOverview();
        fireEvent.pointerDown(cardOf('y'), { button: 0, clientX: 100, clientY: 100, pointerId: 7 });
        fireEvent.pointerMove(grid(), { clientX: 103, clientY: 102, pointerId: 7 });
        expect(order()).toEqual(['x', 'y', 'z']);
        fireEvent.pointerMove(grid(), { clientX: 120, clientY: 160, pointerId: 7 });
        expect(order()).toEqual(['x', 'z']);
        const ghost = document.querySelector<HTMLElement>('[data-rm-ghost="y"]')!;
        expect(ghost.className).toContain('opacity-55');
        expect(ghost.className).toContain('pointer-events-none');
        expect(ghost.style.width).toBe('236px');
        expect(s().overview.drop).toEqual({ kind: 'restore', cellId: 'y', point: null, target: null });
    });

    it('a card carried out of the shelf selects no text on the way: the grid is select-none (polish)', async () => {
        await mountOverOverview();
        expect(grid().className.split(/\s+/)).toContain('select-none');
    });

    it('a card let go over the board is restored as a new column at the slot under the pointer (polish)', async () => {
        await mountOverOverview();
        const restored: Channel = { ...channel, main_stacks: [...channel.main_stacks, stack('s9', ['y'])] };
        cells.restoreRemovedCell.mockResolvedValue({ ok: true, cell_id: 'y', stack_id: 's9', channel: restored });
        stacksApi.moveStackTo.mockResolvedValue({ ...restored });
        lift('y', 300, 500);
        expect(s().overview.drop).toEqual({ kind: 'restore', cellId: 'y', point: { x: 300, y: 500 }, target: null });
        // The board names the slot under the pointer (useStackDrag) and opens a gap there.
        act(() => { s().setOverviewDrop({ ...(s().overview.drop as OverviewDrop & { kind: 'restore' }), target: { kind: 'column', slot: 1 } }); });
        await act(async () => { fireEvent.pointerUp(grid(), { clientX: 300, clientY: 500, pointerId: 7 }); });
        expect(cells.restoreRemovedCell).toHaveBeenCalledWith('chan', 'y');
        await waitFor(() => expect(stacksApi.moveStackTo).toHaveBeenCalledWith('chan', 's9', 1, 'insert'));
        await waitFor(() => expect(s().overview.drop).toBeNull());
        expect(requestSelectCell).toHaveBeenCalledWith('y');
        expect(document.querySelector('[data-rm-ghost]')).toBeNull();
    });

    it('a card let go over the shelf or the bar goes back to its place (polish)', async () => {
        await mountOverOverview();
        lift('y', 300, 500);
        fireEvent.pointerMove(grid(), { clientX: 300, clientY: 200, pointerId: 7 });
        expect(s().overview.drop).toEqual({ kind: 'restore', cellId: 'y', point: null, target: null });
        fireEvent.pointerUp(grid(), { clientX: 300, clientY: 200, pointerId: 7 });
        expect(cells.restoreRemovedCell).not.toHaveBeenCalled();
        expect(s().overview.drop).toBeNull();
        expect(order()).toEqual(['x', 'y', 'z']);
        // The click that ends the drag does not pick the card.
        fireEvent.click(grid());
        expect(s().overview.shelfSelection).toBeNull();
    });

    it('Escape puts a lifted card back and the release restores nothing (polish)', async () => {
        await mountOverOverview();
        lift('y', 300, 500);
        fireEvent.keyDown(window, { key: 'Escape' });
        expect(order()).toEqual(['x', 'y', 'z']);
        expect(s().overview.drop).toBeNull();
        expect(document.querySelector('[data-rm-ghost]')).toBeNull();
        fireEvent.pointerUp(grid(), { clientX: 300, clientY: 500, pointerId: 7 });
        expect(cells.restoreRemovedCell).not.toHaveBeenCalled();
    });

    it('its cards do not drag while Only connected keeps a set, nor from Restore (polish)', async () => {
        await mountOverOverview({ journalOpen: true, onlyConnected: true, anchor: 'a' });
        expect(order()).toEqual(['y']);
        lift('y', 300, 500);
        expect(s().overview.drop).toBeNull();
        expect(order()).toEqual(['y']);
        cleanup();
        await mountOverOverview();
        fireEvent.pointerDown(within(cardOf('x')).getByRole('button', { name: 'Restore' }), { button: 0, clientX: 100, clientY: 100, pointerId: 7 });
        fireEvent.pointerMove(grid(), { clientX: 300, clientY: 500, pointerId: 7 });
        expect(s().overview.drop).toBeNull();
    });

    it('lights as the drop target of a board card held over it, and shakes when it refuses a running one (polish)', async () => {
        await mountOverOverview();
        const animate = vi.fn();
        Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: animate });
        try {
            act(() => { s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'shelf', zone: 0, refusal: null }); });
            expect(shelf()).toHaveAttribute('data-drop-target', 'on');
            expect(shelf().querySelector('[data-rm-zone-strip] [data-lit]')).toHaveTextContent('Standard');
            act(() => { s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'shelf', zone: 0, refusal: REMOVE_RUNNING }); });
            expect(shelf()).toHaveAttribute('data-drop-target', 'refused');
            expect(shelf()).toHaveAttribute('title', REMOVE_RUNNING);
            expect(shelf().querySelector('[data-rm-zone-strip]')).toBeNull();
            act(() => { s().setOverviewDrop(null); s().refuseDrop(REMOVE_RUNNING, 'shelf'); });
            expect(shelf()).not.toHaveAttribute('data-drop-target');
            expect(shelf()).toHaveAttribute('title', REMOVE_RUNNING);
            expect(animate.mock.instances.at(-1)).toBe(shelf());
        } finally {
            delete (HTMLElement.prototype as { animate?: unknown }).animate;
        }
    });
});

describe('RemovedShelf under the Commands pane (polish)', () => {
    const dock = (side: 'left' | 'right') => act(() => {
        s().setCommandPaneSide(side);
        s().openCommandPane();
    });
    const shelf = () => document.querySelector<HTMLElement>('[data-rm-shelf]')!;
    const controls = () => document.querySelector<HTMLElement>('[data-rm-controls]')!;
    const grid = () => document.querySelector<HTMLElement>('[data-rm-grid]')!;
    const hint = () => shelf().querySelector<HTMLElement>('[data-rm-zone-strip]')!;

    it('keeps its ground and its control row\'s hairline under a pane docked left, its controls, grid and drop hint clear of it (polish)', async () => {
        await mount();
        act(() => { s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'shelf', zone: 0, refusal: null }); });
        expect(controls().style.paddingLeft).toBe('20px');
        expect(grid().style.marginLeft).toBe('0px');
        expect(hint().style.left).toBe('0px');
        dock('left');
        // The ground: the whole overview's width, as without the pane.
        expect(shelf().className).toContain('left-0');
        expect(shelf().style.right).toBe('0px');
        expect(shelf().style.marginRight).toBe('0px');
        // The hairline is the control row's own border: it spans the row.
        expect(controls().className).toContain('border-b');
        expect(controls().style.paddingLeft).toBe(`${20 + 352}px`);
        expect(controls().style.paddingRight).toBe('11px');
        expect(controls().style.transition).toContain('padding-left 160ms ease');
        expect(grid().style.marginLeft).toBe('352px');
        expect(grid().style.marginRight).toBe('0px');
        expect(hint().style.left).toBe('352px');
        expect(hint().style.right).toBe('0px');
    });

    it('keeps its controls, grid and drop hint clear of a pane docked right (polish)', async () => {
        await mount();
        act(() => { s().setOverviewDrop({ kind: 'remove', cellId: 'a', over: 'shelf', zone: 0, refusal: null }); });
        dock('right');
        expect(shelf().style.right).toBe('0px');
        expect(controls().style.paddingLeft).toBe('20px');
        expect(controls().style.paddingRight).toBe(`${11 + 352}px`);
        expect(grid().style.marginRight).toBe('352px');
        expect(hint().style.right).toBe('352px');
    });

    it('sizes its cards from the overview\'s whole width, so a pane pushes them aside, fewer to a row, and never resizes them (polish)', async () => {
        // The grid and the count use the same 20px padding and gap.
        expect(shelfColumns(1440)).toBe('repeat(auto-fill, 264px)');
        expect(shelfColumns(1100)).toBe('repeat(auto-fill, 340px)');
        expect(shelfColumns(200)).toBe('repeat(auto-fill, 260px)');
        const width = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1440);
        try {
            await mount();
            const before = grid().style.gridTemplateColumns;
            expect(before).toBe('repeat(auto-fill, 264px)');
            dock('right');
            expect(grid().style.gridTemplateColumns).toBe(before);
            dock('left');
            expect(grid().style.gridTemplateColumns).toBe(before);
            // An open journal takes its width from the shelf; the pane still takes none.
            act(() => { s().setJournalOpen(true); });
            expect(grid().style.gridTemplateColumns).toBe(shelfColumns(1440 - 374));
        } finally {
            width.mockRestore();
        }
    });

    it('ends at a journal docked against a pane on the right, its content in its usual place (polish)', async () => {
        await mount({ journalOpen: true });
        dock('right');
        expect(shelf().style.right).toBe('374px');
        expect(shelf().style.marginRight).toBe('352px');
        expect(controls().style.paddingRight).toBe('11px');
        expect(grid().style.marginRight).toBe('0px');
    });
});
