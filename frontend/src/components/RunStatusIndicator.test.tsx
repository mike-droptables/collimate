import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within, act } from '@testing-library/react';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchActiveRunsEverywhere: vi.fn(async () => []),
    stopRun: vi.fn(async () => undefined),
    stopAllRuns: vi.fn(async () => undefined),
}));

import { RunStatusIndicator } from './RunStatusIndicator';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { fetchActiveRunsEverywhere, stopAllRuns, stopRun, type ActiveRunInfo, type ChannelActiveRuns } from '@/lib/api';
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
    runsPaneView: {}, runsPaneFocus: {}, pendingRunsPaneOpen: null,
});

const status = () => screen.getByRole('button', { name: /running/ });
const mount = (props: Partial<{ isZoomedOut: boolean; onExitZoom: () => void; onSelectChannel: (id: string) => void | Promise<void> }> = {}) =>
    render(<RunStatusIndicator activeChannel={ALPHA} isZoomedOut={false} onExitZoom={vi.fn()} onSelectChannel={vi.fn()} {...props} />);
const openMenu = () => { fireEvent.click(status()); return screen.getByRole('dialog', { name: 'Running' }); };
const runRow = (menu: HTMLElement, id: string) => menu.querySelector<HTMLElement>(`[data-run="${id}"]`);
const runButton = (menu: HTMLElement, id: string) => runRow(menu, id)!.querySelector<HTMLButtonElement>('[data-testid="runs-menu-run"]')!;

describe('RunStatusIndicator', () => {
    beforeEach(() => {
        seed();
        useCollimateStore.setState({ state: null, cellLayouts: {}, cellDisplaysReady: false, activeChannel: ALPHA } as never);
        vi.mocked(stopRun).mockClear();
        vi.mocked(stopAllRuns).mockClear();
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

    it('groups channel → cell → run, this channel first, each run with its state, short id, listening and elapsed time', () => {
        useCollimateStore.setState({ runPhases: { 'run-b': { phase: 'awaiting_input' } },
            annotationContexts: { 'cell-1': { 'run-a': { listening: false } as never } } });
        mount();
        const menu = openMenu();
        expect(menu.closest('[data-owns-keyboard]')).not.toBeNull();
        const channels = within(menu).getAllByTestId('runs-menu-channel');
        expect(channels.map(c => c.getAttribute('data-channel'))).toEqual([HERE, OTHER, TEST]);
        expect(channels[0].textContent).toContain('This channel');
        const cells = within(channels[0]).getAllByTestId('runs-menu-cell');
        expect(cells.map(c => c.getAttribute('data-cell-id'))).toEqual(['cell-1']);
        const head = within(cells[0]).getAllByRole('button')[0];
        expect(head.textContent).toContain('Op log + merge screen');
        expect(head.textContent).toContain('Options');
        expect(head.title).toBe('Go to this cell');
        expect([...cells[0].querySelectorAll('[data-run]')].map(r => r.getAttribute('data-run'))).toEqual(['run-a', 'run-b']);
        expect(runButton(menu, 'run-a').textContent).toBe('weba111111112m');
        // The loaded context wins over the live row; the row speaks when there is none.
        expect(runRow(menu, 'run-a')!.querySelector('[data-testid="run-listening"]')).toHaveAttribute('data-listening', 'false');
        expect(runRow(menu, 'run-b')!.querySelector('[data-testid="run-listening"]')).toBeNull();
        expect(runRow(menu, 'run-d')!.querySelector('[data-testid="run-listening"]')).toHaveAttribute('data-listening', 'false');
        // State dots: running teal, awaiting brass.
        expect(runButton(menu, 'run-a').querySelector('i')!.className).toContain('bg-teal');
        expect(runButton(menu, 'run-b').querySelector('i')!.className).toContain('bg-brass');
        // This channel's rows carry the run's own Stop; another's the graceful stop.
        expect(within(runRow(menu, 'run-a')!).getByTestId('run-stop')).toBeTruthy();
        expect(within(runRow(menu, 'run-c')!).getByTestId('runs-menu-stop')).toBeTruthy();
        // The run whose end is under way elsewhere is not listed.
        expect(runRow(menu, 'run-e')).toBeNull();
        expect(within(menu).getByTestId('runs-menu-stop-all').textContent).toBe('Stop all 4');
        // The first run row takes the keyboard on open.
        expect(document.activeElement).toBe(runButton(menu, 'run-a'));
    });

    it('a stopping run of this channel stays listed (Force stop stays in reach), shown stopping', () => {
        vi.useFakeTimers();
        mount();
        const menu = openMenu();
        fireEvent.click(within(runRow(menu, 'run-a')!).getByTestId('run-stop'));
        expect(stopRun).toHaveBeenCalledWith(HERE, 'run-a');
        expect(runRow(menu, 'run-a')).not.toBeNull();
        expect(runButton(menu, 'run-a').textContent).toContain('stopping');
        expect(runButton(menu, 'run-a').querySelector('i')!.className).toContain('border-red');
        act(() => { vi.advanceTimersByTime(1100); });
        fireEvent.click(within(runRow(menu, 'run-a')!).getByTestId('run-stop'));
        expect(stopRun).toHaveBeenLastCalledWith(HERE, 'run-a', true);
        expect(status().textContent).toBe('1 running(2)');
    });

    it('another channel\'s Stop hides its row; a refused stop brings it back', async () => {
        vi.mocked(stopRun).mockRejectedValueOnce(new Error('refused'));
        mount();
        const menu = openMenu();
        fireEvent.click(within(runRow(menu, 'run-c')!).getByTestId('runs-menu-stop'));
        expect(stopRun).toHaveBeenCalledWith(OTHER, 'run-c');
        await vi.waitFor(() => expect(runRow(menu, 'run-c')).not.toBeNull());
        vi.mocked(stopRun).mockResolvedValueOnce(undefined);
        fireEvent.click(within(runRow(menu, 'run-c')!).getByTestId('runs-menu-stop'));
        expect(runRow(menu, 'run-c')).toBeNull();
    });

    it('a cell row goes to the cell and opens its status pane on the status view', () => {
        const onExitZoom = vi.fn();
        const updateRunsPane = vi.fn();
        useCollimateStore.setState({ cellLayouts: { 'cell-1': createDefaultLayout({ filePaths: [], pinnedPath: null }) }, updateRunsPane } as never);
        mount({ isZoomedOut: true, onExitZoom });
        const menu = openMenu();
        fireEvent.click(within(within(menu).getAllByTestId('runs-menu-cell')[0]).getAllByRole('button')[0]);
        expect(screen.queryByRole('dialog', { name: 'Running' })).toBeNull();
        expect(onExitZoom).toHaveBeenCalled();
        return vi.waitFor(() => {
            expect(useCollimateStore.getState().pendingSelectCellId).toBe('cell-1');
            expect(useCollimateStore.getState().runsPaneView['cell-1']).toEqual({ view: 'status' });
            expect(updateRunsPane).toHaveBeenCalledWith('cell-1', expect.any(Function));
        });
    });

    it('a run row opens that run\'s drilldown in its cell', async () => {
        useCollimateStore.setState({ cellLayouts: { 'cell-1': createDefaultLayout({ filePaths: [], pinnedPath: null }) }, updateRunsPane: vi.fn() } as never);
        mount();
        const menu = openMenu();
        fireEvent.click(runButton(menu, 'run-b'));
        await vi.waitFor(() => expect(useCollimateStore.getState().runsPaneView['cell-1']).toEqual({ view: 'run', runId: 'run-b', option: 'default' }));
        expect(useCollimateStore.getState().runsPaneFocus['cell-1'].target).toBe('view');
    });

    it('another channel\'s run switches the channel first; the pane opens once the cell is there', async () => {
        let switched!: () => void;
        const onSelectChannel = vi.fn(() => new Promise<void>(resolve => { switched = resolve; }));
        mount({ onSelectChannel });
        const menu = openMenu();
        fireEvent.click(runButton(menu, 'run-c'));
        expect(onSelectChannel).toHaveBeenCalledWith(OTHER);
        expect(useCollimateStore.getState().pendingSelectCellId).toBeNull();
        await act(async () => switched());
        expect(useCollimateStore.getState().pendingSelectCellId).toBe('cell-t');
        expect(useCollimateStore.getState().pendingRunsPaneOpen).toMatchObject({ channel: OTHER, cellId: 'cell-t', view: { view: 'run', runId: 'run-c' } });
    });

    it('a cell of this channel that left the canvas keeps its head, inert', () => {
        seed([{ channel: HERE, channel_name: 'Alpha', runs: [row('run-z', 'cell-gone', 'Gone', 'web', 1)] }]);
        mount();
        const menu = openMenu();
        const head = within(within(menu).getByTestId('runs-menu-cell')).getAllByRole('button')[0];
        expect(head).toBeDisabled();
        expect(head.title).toBe('This cell is not on the canvas');
    });

    it('↑↓ move over the cell heads and run rows, wrapping', () => {
        mount();
        const menu = openMenu();
        const order = ['run-b', 'cell-t', 'run-c', 'cell-x', 'run-d', 'cell-1', 'run-a'];
        const target = (key: string) => (key.startsWith('run') ? runButton(menu, key)
            : within(menu.querySelector<HTMLElement>(`[data-cell-id="${key}"]`)!).getAllByRole('button')[0]);
        for (const key of order) {
            fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
            expect(document.activeElement).toBe(target(key));
        }
        fireEvent.keyDown(document.activeElement!, { key: 'ArrowUp' });
        expect(document.activeElement).toBe(target('cell-1'));
    });

    it('with nothing running anywhere the menu says so and has no footer (C04)', () => {
        seed([]);
        mount();
        const menu = openMenu();
        expect(menu.textContent).toContain('Nothing is running.');
        expect(within(menu).queryByTestId('runs-menu-stop-all')).toBeNull();
    });

    it('Stop all N stops every run on the server at once, with no confirm (C04)', () => {
        const openConfirm = vi.fn();
        useCollimateStore.setState({ openConfirm } as never);
        mount();
        const menu = openMenu();
        fireEvent.click(within(menu).getByTestId('runs-menu-stop-all'));
        expect(stopAllRuns).toHaveBeenCalledTimes(1);
        expect(openConfirm).not.toHaveBeenCalled();
        // This channel's runs stay listed, stopping; the others leave.
        expect([...menu.querySelectorAll('[data-run]')].map(r => r.getAttribute('data-run'))).toEqual(['run-a', 'run-b']);
        expect(status().textContent).toBe('0 running(0)');
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
