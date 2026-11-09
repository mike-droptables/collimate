import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within, act } from '@testing-library/react';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchActiveRunsEverywhere: vi.fn(async () => []),
    stopAllRuns: vi.fn(async () => undefined),
    stopChannelRuns: vi.fn(async () => ({ requested: 0 })),
}));

import { RunStatusIndicator } from './RunStatusIndicator';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { fetchActiveRunsEverywhere, stopAllRuns, stopChannelRuns, type ActiveRunInfo, type ChannelActiveRuns } from '@/lib/api';
import type { Channel } from '@/lib/types';
import { createDefaultLayout } from '@/lib/cellLayout';

const MIN = 60_000;
const ago = (minutes: number) => new Date(Date.now() - minutes * MIN).toISOString();
const HERE = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const TEST = '33333333-3333-4333-8333-333333333333';

const row = (id: string, cellId: string, cellName: string, runner: string, minutes: number,
    over: Partial<ActiveRunInfo> = {}): ActiveRunInfo => ({
    run_id: id, cell_id: cellId, cell_name: cellName, runner_id: runner, runner_name: runner,
    label: `${runner}-${id.replace('run-', '')}1111111`, folder: `_runs/${id}-label`, generator_name: 'synthetic',
    started_at: ago(minutes + 0.2), ...over,
});

const ALPHA: Channel = {
    id: HERE, name: 'Alpha', nav_stacks: [], nav_selection_id: null, main_selection_id: null,
    main_stacks: [
        { id: 'stack-1', name: 'Options', cells: [{ id: 'cell-1', name: 'Op log + merge screen', files: [] }] },
        { id: 'stack-2', name: '', cells: [{ id: 'cell-2', name: 'Notes', files: [] }] },
    ],
};

// Two runs in one cell here, two in other channels, and one there whose
// end is under way.
const EVERYWHERE: ChannelActiveRuns[] = [
    { channel: OTHER, channel_name: 'Beta', runs: [row('run-c', 'cell-t', 'Tutorial', 'git', 41)] },
    { channel: HERE, channel_name: 'Alpha', runs: [
        row('run-a', 'cell-1', 'Op log + merge screen', 'web', 12, { listening: true }),
        row('run-b', 'cell-1', 'Op log + merge screen', 'term', 4, { phase: 'awaiting_input' }),
    ] },
    { channel: TEST, channel_name: 'test', runs: [
        row('run-d', 'cell-x', 'New cell (Copy)', 'seed', 3, { listening: false }),
        row('run-e', 'cell-x', 'New cell (Copy)', 'ending', 1, { stopping: true }),
    ] },
];

const seed = (everywhere: ChannelActiveRuns[] = EVERYWHERE) => useCollimateStore.setState({
    activeRunsEverywhere: everywhere,
    stoppingRunIds: new Set(),
    runPhases: {},
    annotationContexts: {},
    pendingSelectCellId: null,
    runsMenuOpen: false,
    annotationPaneView: {}, annotationPaneFocus: {}, pendingAnnotationPaneOpen: null,
    statusSection: {}, statusSectionFocus: {}, pendingStatusSectionOpen: null,
});

const store = () => useCollimateStore.getState();
const status = () => screen.getByRole('button', { name: /running/ });
const mount = (props: Partial<{ isZoomedOut: boolean; onExitZoom: () => void; onSelectChannel: (id: string) => void | Promise<void> }> = {}) =>
    render(<RunStatusIndicator activeChannel={ALPHA} isZoomedOut={false} onExitZoom={vi.fn()} onSelectChannel={vi.fn()} {...props} />);
const openMenu = () => { fireEvent.click(status()); return screen.getByRole('dialog', { name: 'Running' }); };
const cellRow = (menu: HTMLElement, id: string) => menu.querySelector<HTMLButtonElement>(`[data-testid="runs-menu-cell"][data-cell-id="${id}"]`);
const chip = (menu: HTMLElement, id: string) => cellRow(menu, id)!.querySelector<HTMLElement>('[data-run-count]')!;
const stopChannel = (menu: HTMLElement, channel: string) => menu.querySelector<HTMLButtonElement>(`[data-testid="runs-menu-stop-channel"][data-channel="${channel}"]`);
const channelIds = (menu: HTMLElement) => within(menu).queryAllByTestId('runs-menu-channel').map(c => c.getAttribute('data-channel'));

describe('RunStatusIndicator', () => {
    beforeEach(() => {
        seed();
        useCollimateStore.setState({ state: null, cellLayouts: {}, cellDisplaysReady: false, activeChannel: ALPHA } as never);
        vi.mocked(stopAllRuns).mockClear();
        vi.mocked(stopChannelRuns).mockClear();
    });
    afterEach(() => { cleanup(); vi.useRealTimers(); });

    it('always shows "N running (M)": this channel teal with a filled dot, the rest bracketed; a stopping run does not count (C04)', () => {
        mount();
        const button = status();
        expect(button.textContent).toBe('2 running(2)');
        expect(button.getAttribute('aria-haspopup')).toBe('dialog');
        expect(button.getAttribute('aria-expanded')).toBe('false');
        expect(button.title).toBe('2 running in this channel, 2 in other channels');
        expect(button.className).toContain('text-teal');
        expect(screen.getByTestId('run-status-dot').className).toContain('bg-teal');
    });

    it('with nothing running here the status is quiet ink with a ring dot, and still counts the other channels (C04)', () => {
        seed(EVERYWHERE.filter(e => e.channel !== HERE));
        mount();
        const button = status();
        expect(button.textContent).toBe('0 running(2)');
        expect(button.className).toContain('text-ink-2');
        expect(button.className).not.toContain('text-teal');
        // The other channels' count is text too: ink-2 meets the 4.5:1
        // contrast gate on the canvas where ink-3 does not.
        expect(screen.getByText('(2)').className.split(/\s+/)).toContain('text-ink-2');
        const dot = screen.getByTestId('run-status-dot').className;
        expect(dot).toContain('bg-transparent');
        expect(dot).toContain('shadow-[inset_0_0_0_1.5px_var(--ink-3)]');
    });

    it('the count follows the store as the channel stream refreshes it (C04)', () => {
        mount();
        act(() => seed([{ channel: HERE, channel_name: 'Alpha', runs: [row('run-a', 'cell-1', 'Op log', 'web', 1)] }]));
        expect(status().textContent).toBe('1 running(0)');
    });

    it('lists the running cells by channel, this one first, each with its run count; no run is named or stopped on its own', () => {
        mount();
        const menu = openMenu();
        expect(menu.closest('[data-owns-keyboard]')).not.toBeNull();
        expect(channelIds(menu)).toEqual([HERE, OTHER, TEST]);
        const channels = within(menu).getAllByTestId('runs-menu-channel');
        expect(channels[0].textContent).toContain('This channel');
        expect(within(channels[0]).getAllByTestId('runs-menu-cell').map(c => c.getAttribute('data-cell-id'))).toEqual(['cell-1']);
        const head = cellRow(menu, 'cell-1')!;
        expect(head.tagName).toBe('BUTTON');
        expect(head.textContent).toBe('Op log + merge screenOptions2');
        expect(head.title).toBe('Go to this cell');
        // The count chip: teal while any of its runs is not stopping.
        expect(chip(menu, 'cell-1')).toHaveAttribute('data-run-count', '2');
        expect(chip(menu, 'cell-1').title).toBe('2 running');
        expect(chip(menu, 'cell-1').className).toContain('text-teal');
        // Another channel names its cell as the list does; a run whose end is
        // under way there is not counted.
        expect(cellRow(menu, 'cell-t')!.textContent).toBe('Tutorial1');
        expect(chip(menu, 'cell-x')).toHaveAttribute('data-run-count', '1');
        // No run rows, no Stop per run.
        expect(menu.querySelector('[data-run], [data-testid="runs-menu-run"], [data-testid="run-stop"], [data-testid="runs-menu-stop"]')).toBeNull();
        // Each channel ends with its own Stop all; the footer stops the server.
        expect(stopChannel(menu, HERE)!.textContent).toBe('Stop all 2 in Alpha');
        expect(stopChannel(menu, OTHER)!.textContent).toBe('Stop all 1 in Beta');
        expect(stopChannel(menu, TEST)!.textContent).toBe('Stop all 1 in test');
        expect(channels[1].lastElementChild!.contains(stopChannel(menu, OTHER))).toBe(true);
        expect(within(menu).getByTestId('runs-menu-stop-all').textContent).toBe('Stop all 4 on this server');
        // The first cell row takes the keyboard on open.
        expect(document.activeElement).toBe(head);
    });

    it('a cell of this channel whose runs are all stopping stays listed with a quiet chip, and its channel has no Stop all', () => {
        useCollimateStore.setState({ stoppingRunIds: new Set(['run-a', 'run-b']) });
        mount();
        const menu = openMenu();
        expect(cellRow(menu, 'cell-1')).not.toBeNull();
        expect(chip(menu, 'cell-1')).toHaveAttribute('data-run-count', '2');
        expect(chip(menu, 'cell-1').title).toBe('0 running, 2 stopping');
        expect(chip(menu, 'cell-1').className).toContain('text-ink-3');
        expect(chip(menu, 'cell-1').className).not.toContain('text-teal');
        expect(stopChannel(menu, HERE)).toBeNull();
        expect(within(menu).getByTestId('runs-menu-stop-all').textContent).toBe('Stop all 2 on this server');
        expect(status().textContent).toBe('0 running(2)');
    });

    it('Stop all N in this channel asks the channel to stop: its runs turn stopping and its cells stay listed', () => {
        mount();
        const menu = openMenu();
        const stop = stopChannel(menu, HERE)!;
        stop.focus();
        fireEvent.click(stop);
        expect(stopChannelRuns).toHaveBeenCalledWith(HERE);
        expect(stopAllRuns).not.toHaveBeenCalled();
        expect([...store().stoppingRunIds].sort()).toEqual(['run-a', 'run-b']);
        expect(cellRow(menu, 'cell-1')).not.toBeNull();
        expect(chip(menu, 'cell-1').className).toContain('text-ink-3');
        expect(stopChannel(menu, HERE)).toBeNull();
        expect(status().textContent).toBe('0 running(2)');
        expect(within(menu).getByTestId('runs-menu-stop-all').textContent).toBe('Stop all 2 on this server');
        // The stop left with its group: the menu keeps the keyboard.
        expect(document.activeElement).toBe(menu.querySelector('[tabindex="-1"]'));
    });

    it('a refused stop of this channel clears its runs\' stopping', async () => {
        vi.mocked(stopChannelRuns).mockRejectedValueOnce(new Error('refused'));
        mount();
        const menu = openMenu();
        fireEvent.click(stopChannel(menu, HERE)!);
        await vi.waitFor(() => expect(store().stoppingRunIds.size).toBe(0));
        expect(stopChannel(menu, HERE)!.textContent).toBe('Stop all 2 in Alpha');
    });

    it('another channel\'s Stop all hides its group; a refused stop brings it back; the stop locks the menu\'s clicks a moment', async () => {
        vi.useFakeTimers();
        vi.mocked(stopChannelRuns).mockRejectedValueOnce(new Error('refused'));
        mount();
        const menu = openMenu();
        fireEvent.click(stopChannel(menu, OTHER)!);
        expect(stopChannelRuns).toHaveBeenCalledWith(OTHER);
        expect(channelIds(menu)).toEqual([HERE, TEST]);
        await act(async () => {});
        expect(channelIds(menu)).toEqual([HERE, OTHER, TEST]);
        // A click right after a stop never reaches the stop that slid under it.
        fireEvent.click(stopChannel(menu, OTHER)!);
        expect(stopChannelRuns).toHaveBeenCalledTimes(1);
        act(() => { vi.advanceTimersByTime(600); });
        fireEvent.click(stopChannel(menu, OTHER)!);
        expect(stopChannelRuns).toHaveBeenCalledTimes(2);
        expect(channelIds(menu)).toEqual([HERE, TEST]);
        expect(status().textContent).toBe('2 running(1)');
        expect(within(menu).getByTestId('runs-menu-stop-all').textContent).toBe('Stop all 3 on this server');
    });

    it('a cell row goes to the cell and opens its status section, which takes the keyboard', () => {
        const onExitZoom = vi.fn();
        const updateRunsPane = vi.fn();
        useCollimateStore.setState({ cellLayouts: { 'cell-1': createDefaultLayout({ filePaths: [], pinnedPath: null }) }, updateRunsPane } as never);
        mount({ isZoomedOut: true, onExitZoom });
        const menu = openMenu();
        fireEvent.click(cellRow(menu, 'cell-1')!);
        expect(screen.queryByRole('dialog', { name: 'Running' })).toBeNull();
        expect(onExitZoom).toHaveBeenCalled();
        return vi.waitFor(() => {
            expect(store().pendingSelectCellId).toBe('cell-1');
            expect(store().statusSection['cell-1']).toMatchObject({ open: true });
            expect(store().statusSectionFocus['cell-1'].seq).toBe(1);
            // The annotation pane is left as it was.
            expect(updateRunsPane).not.toHaveBeenCalled();
            expect(store().annotationPaneView['cell-1']).toBeUndefined();
        });
    });

    it('another channel\'s cell switches the channel first; the section opens once the cell is there', async () => {
        let switched!: () => void;
        const onSelectChannel = vi.fn(() => new Promise<void>(resolve => { switched = resolve; }));
        mount({ onSelectChannel });
        const menu = openMenu();
        fireEvent.click(cellRow(menu, 'cell-t')!);
        expect(onSelectChannel).toHaveBeenCalledWith(OTHER);
        expect(store().pendingSelectCellId).toBeNull();
        await act(async () => switched());
        expect(store().pendingSelectCellId).toBe('cell-t');
        expect(store().pendingStatusSectionOpen).toMatchObject({ channel: OTHER, cellId: 'cell-t' });
        expect(store().statusSection['cell-t']).toBeUndefined();
    });

    it('a cell of this channel that left the canvas keeps its row, inert', () => {
        seed([{ channel: HERE, channel_name: 'Alpha', runs: [row('run-z', 'cell-gone', 'Gone', 'web', 1)] }]);
        mount();
        const menu = openMenu();
        const head = cellRow(menu, 'cell-gone')!;
        expect(head).toBeDisabled();
        expect(head.title).toBe('This cell is not on the canvas');
        // The keyboard lands on the next control there is.
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(stopChannel(menu, HERE));
    });

    it('↑↓ move over the cell rows and the stops in document order, wrapping', () => {
        mount();
        const menu = openMenu();
        const footer = within(menu).getByTestId('runs-menu-stop-all');
        const order = [stopChannel(menu, HERE), cellRow(menu, 'cell-t'), stopChannel(menu, OTHER), cellRow(menu, 'cell-x'),
            stopChannel(menu, TEST), footer, cellRow(menu, 'cell-1')];
        for (const target of order) {
            fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
            expect(document.activeElement).toBe(target);
        }
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
        expect(document.activeElement).toBe(footer);
    });

    it('with nothing running anywhere the menu says so and has no footer (C04)', () => {
        seed([]);
        mount();
        const menu = openMenu();
        expect(menu.textContent).toContain('Nothing is running.');
        expect(within(menu).queryByTestId('runs-menu-stop-all')).toBeNull();
    });

    it('Stop all N on this server stops every run at once, with no confirm (C04)', () => {
        const openConfirm = vi.fn();
        useCollimateStore.setState({ openConfirm } as never);
        mount();
        const menu = openMenu();
        fireEvent.click(within(menu).getByTestId('runs-menu-stop-all'));
        expect(stopAllRuns).toHaveBeenCalledTimes(1);
        expect(openConfirm).not.toHaveBeenCalled();
        expect(stopChannelRuns).not.toHaveBeenCalled();
        // This channel's cells stay listed, their runs stopping; the others leave.
        expect(channelIds(menu)).toEqual([HERE]);
        expect(cellRow(menu, 'cell-1')).not.toBeNull();
        expect([...store().stoppingRunIds].sort()).toEqual(['run-a', 'run-b']);
        expect(status().textContent).toBe('0 running(0)');
        expect(within(menu).queryByTestId('runs-menu-stop-all')).toBeNull();
    });

    it('the status keeps one width as its counts change, so the menu stays open after Stop all (C04)', () => {
        mount();
        const menu = openMenu();
        const button = status();
        expect(button.className).toContain('inline-grid');
        expect(button.className).toContain("after:content-['00_running_(00)'_/_'']");
        expect(button.className).toContain('after:[grid-area:1/1]');
        expect(button.className).toContain('after:invisible');
        expect(screen.getByTestId('run-status-label').className).toContain('[grid-area:1/1]');
        fireEvent.click(within(menu).getByTestId('runs-menu-stop-all'));
        expect(screen.getByRole('dialog', { name: 'Running' })).toBe(menu);
        expect(button.textContent).toBe('0 running(0)');
    });

    it('the menu opens from the store, as the Commands pane\'s "Running…" opens it, and its close clears the store (K01)', () => {
        mount();
        expect(screen.queryByRole('dialog', { name: 'Running' })).toBeNull();
        act(() => useCollimateStore.getState().setRunsMenuOpen(true));
        screen.getByRole('dialog', { name: 'Running' });
        expect(status()).toHaveAttribute('aria-expanded', 'true');
        act(() => useCollimateStore.getState().setRunsMenuOpen(false));
        expect(screen.queryByRole('dialog', { name: 'Running' })).toBeNull();
    });

    it('Esc closes the menu, goes no further and hands focus back to the status (C04)', () => {
        mount();
        const menu = openMenu();
        expect(menu.closest('[data-owns-keyboard]')).not.toBeNull();
        const reachedWindow = vi.fn();
        window.addEventListener('keydown', reachedWindow);
        try {
            fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
        } finally {
            window.removeEventListener('keydown', reachedWindow);
        }
        expect(reachedWindow).not.toHaveBeenCalled();
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(document.activeElement).toBe(status());
    });
});

describe('the every-channel runs in the store', () => {
    it('refreshActiveRunsEverywhere keeps the latest answer, never an older one that lands late (C04)', async () => {
        let resolveFirst!: (v: ChannelActiveRuns[]) => void;
        vi.mocked(fetchActiveRunsEverywhere)
            .mockImplementationOnce(() => new Promise(r => { resolveFirst = r; }))
            .mockResolvedValueOnce(EVERYWHERE.slice(1));
        const first = useCollimateStore.getState().refreshActiveRunsEverywhere();
        await useCollimateStore.getState().refreshActiveRunsEverywhere();
        resolveFirst(EVERYWHERE);
        await first;
        expect(useCollimateStore.getState().activeRunsEverywhere).toEqual(EVERYWHERE.slice(1));
    });
});
