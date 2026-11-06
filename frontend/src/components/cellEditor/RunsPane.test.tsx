// The cell's status pane: docked on the right (a column) or across the
// bottom (a band), with its resize handle on the content-facing edge; the
// router over its three views (status, a run's drilldown, All) with each
// view's rail and the Hide toggle; the keyboard's way in (on open, on a view
// change, on a focus request) and out (Esc steps back, then closes).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { forwardRef, useImperativeHandle, useState } from 'react';
import { RunsPane } from './RunsPane';
import { CellSelectionContext } from '@/contexts/CellSelectionContext';
import { createDefaultLayout, openRunsPane, setRunsPane } from '@/lib/cellLayout';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { CellLayout, RunsPaneState, RunsPaneView } from '@/stores/collimate/types';
import type { RunRecord } from '@/lib/types';

const pool = vi.hoisted(() => ({ invalidate: vi.fn() }));
vi.mock('@/contexts/GuestPoolContext', async (importOriginal) => {
    const actual = await importOriginal<typeof import('@/contexts/GuestPoolContext')>();
    return { ...actual, useGuestPool: () => ({ ...actual.useGuestPool(), invalidate: pool.invalidate, containerOf: () => null }) };
});
vi.mock('@/hooks/useRunInbox', () => ({ useRunInbox: () => ({ cards: [], status: 'off', answer: () => false }) }));
vi.mock('@/components/runsPane/DraftEditor', () => ({
    DraftEditor: forwardRef(function FakeEditor(_props: unknown, ref) {
        useImperativeHandle(ref, () => ({ focus: () => { (document.querySelector('[data-testid="fake-editor"]') as HTMLElement | null)?.focus(); return true; }, text: async () => '', reset: () => {} }));
        return <textarea data-testid="fake-editor" />;
    }),
}));

const RUN: RunRecord = { runId: 'run-1', cellId: 'cell-1', channel: 'ch', runnerId: 'agent', runnerName: 'Agent', generatorName: 'agent', label: 'agent-11111111', folder: '_runs/agent-11111111' };

const layoutWith = (patch: Partial<RunsPaneState>): CellLayout =>
    setRunsPane(createDefaultLayout({ filePaths: new Set<string>(), pinnedPath: null }), patch);

function Harness({ initial, allowRun, selected }: { initial: CellLayout; allowRun: boolean; selected: boolean }) {
    const [layout, setLayout] = useState(initial);
    return (
        <div data-cell-id="cell-1">
            <button type="button" data-testid="runs-pane-toggle">caret</button>
            <button type="button" onClick={() => setLayout(l => openRunsPane(l))}>open</button>
            <CellSelectionContext.Provider value={selected}>
                <div data-runs-dock-region style={{ display: 'flex' }}>
                    <div>content</div>
                    <RunsPane cellId="cell-1" layout={layout} onLayoutChange={fn => setLayout(fn)} allowRun={allowRun} allowAnnotate maximized={false} />
                </div>
            </CellSelectionContext.Provider>
            <output data-testid="pane-state">{JSON.stringify(layout.runsPane)}</output>
        </div>
    );
}

const mount = (patch: Partial<RunsPaneState> = { open: true }, { allowRun = true, selected = false } = {}) =>
    render(<Harness initial={layoutWith(patch)} allowRun={allowRun} selected={selected} />);

const aside = () => screen.queryByRole('complementary', { name: 'Status' });
const state = (): RunsPaneState => JSON.parse(screen.getByTestId('pane-state').textContent!);
const hide = () => screen.getByRole('button', { name: 'Hide status' });
const handle = () => screen.getByRole('separator', { name: 'Resize status' });
const caret = () => screen.getByTestId('runs-pane-toggle');
const view = (v: RunsPaneView) => act(() => { useCollimateStore.getState().setRunsPaneView('cell-1', v); });

/** The cell is 900 x 600; the pane is its own inline size. */
function mockBoxes() {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
        const isPane = this.tagName === 'ASIDE';
        const width = isPane && this.style.width ? parseFloat(this.style.width) : 900;
        const height = isPane && this.style.height ? parseFloat(this.style.height) : 600;
        return { x: 0, y: 0, top: 0, left: 0, right: width, bottom: height, width, height, toJSON: () => ({}) } as DOMRect;
    });
}

beforeEach(() => {
    pool.invalidate.mockClear();
    useCollimateStore.setState({
        runs: {}, stoppingRunIds: new Set(), runPhases: {}, lastFinishedByCell: {}, activeRunsEverywhere: [],
        cellLogRuns: {}, runLogs: {}, runLogArrivals: {}, pickSessions: {}, cellHistory: {}, dockDrag: null,
        activeChannel: { id: 'ch', name: 'ch', main_stacks: [], nav_stacks: [] },
        runsPaneView: {}, runsPaneFocus: {}, finishedRuns: {}, annotationContexts: {}, fileTouches: {}, lastFileTab: {},
        composerDrafts: {}, annotationRequests: {}, annotationNotice: {},
        loadCellAnnotations: vi.fn(async () => {}), refreshAnnotationContext: vi.fn(async () => {}), loadFileTouches: vi.fn(async () => {}),
        readSavedCellLogs: vi.fn(async () => {}),
    } as never);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('the status pane per dock side', () => {
    it('renders nothing while closed', () => {
        mount({ open: false });
        expect(aside()).toBeNull();
    });

    it.each([
        ['right', ['h-full', 'border-l'], { width: '280px', minWidth: 'min(200px, min(66.6667%, 100% - 160px))', maxWidth: 'min(66.6667%, 100% - 160px)', height: '' }],
        ['bottom', ['w-full', 'border-t'], { height: '200px', minHeight: 'min(120px, min(66.6667%, 100% - 120px))', maxHeight: 'min(66.6667%, 100% - 120px)', width: '' }],
    ] as const)('docked %s, it is the Status aside with its border and its stored size', (side, classes, style) => {
        mount({ open: true, side });
        const pane = aside()!;
        expect(pane).toHaveAttribute('data-runs-pane');
        expect(pane).toHaveAttribute('data-runs-side', side);
        expect(pane.className.split(/\s+/)).toEqual(expect.arrayContaining([...classes, 'bg-sheet', 'flex-none']));
        for (const [key, value] of Object.entries(style)) expect(pane.style[key as 'width']).toBe(value);
    });

    it('the stored width and height are kept apart', () => {
        mount({ open: true, side: 'right', width: 320, height: 150 });
        expect(aside()!.style.width).toBe('320px');
        cleanup();
        mount({ open: true, side: 'bottom', width: 320, height: 150 });
        expect(aside()!.style.height).toBe('150px');
    });
});

describe('the router', () => {
    it('shows the status view with no stored view', () => {
        mount();
        expect(aside()).toHaveAttribute('data-runs-view', 'status');
        expect(screen.getByTestId('status-view')).toBeInTheDocument();
    });

    it('shows a drilldown for a live run, a run only listed live, and a finished run (dimmed)', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN } });
        mount();
        view({ view: 'run', runId: 'run-1', option: 'default' });
        expect(screen.getByTestId('run-drilldown')).toHaveAttribute('data-run-id', 'run-1');
        expect(screen.getByTestId('run-drilldown')).not.toHaveAttribute('data-ended');
        act(() => useCollimateStore.setState({
            runs: {}, activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [{ run_id: 'run-1', cell_id: 'cell-1', cell_name: 'C', runner_id: 'agent', runner_name: 'Agent', label: 'agent-11111111', folder: '_runs/agent-11111111', generator_name: 'agent', started_at: null }] }],
        }));
        expect(screen.getByTestId('run-drilldown')).not.toHaveAttribute('data-ended');
        // Ended: the finished record wins over the row still listed (stopping).
        act(() => useCollimateStore.setState({ finishedRuns: { 'cell-1': [{ ...RUN, status: 'completed', endedAt: 1 }] } }));
        expect(screen.getByTestId('run-drilldown')).toHaveAttribute('data-ended', 'true');
        act(() => useCollimateStore.setState({ activeRunsEverywhere: [] }));
        expect(screen.getByTestId('run-drilldown')).toHaveAttribute('data-ended', 'true');
    });

    it('a drilldown of a run the pane no longer knows falls back to the status view', () => {
        mount();
        view({ view: 'run', runId: 'gone', option: 'default' });
        expect(screen.getByTestId('status-view')).toBeInTheDocument();
        expect(useCollimateStore.getState().runsPaneView['cell-1']).toEqual({ view: 'status' });
    });

    it('shows All, and wakes the guest pool on every view change', () => {
        mount();
        pool.invalidate.mockClear();
        view({ view: 'all', option: 'default' });
        expect(screen.getByTestId('run-all')).toBeInTheDocument();
        expect(pool.invalidate).toHaveBeenCalled();
        pool.invalidate.mockClear();
        view({ view: 'all', option: 'log' });
        expect(pool.invalidate).toHaveBeenCalled();
    });
});

describe('the rail', () => {
    it('sits on the tab strip\'s icon line: the view\'s title, then Hide status (pressed, an X) at the far end', () => {
        mount({ open: true, side: 'right' });
        const rail = hide().closest('[data-runs-dock-handle]')!.parentElement!;
        expect(rail.className.split(/\s+/)).toEqual(expect.arrayContaining(['h-[1.875rem]', 'items-end', 'pb-[calc(0.25rem-1px)]', 'border-b', 'border-line']));
        expect(hide()).toHaveAttribute('aria-pressed', 'true');
        expect(hide().querySelector('.lucide-x')).not.toBeNull();
        expect(hide().className.split(/\s+/)).toEqual(expect.arrayContaining(['h-[1.125rem]', 'aria-pressed:bg-sheet-2']));
        expect(rail.children[0].textContent).toBe('Status');
        expect(hide().closest('[data-runs-dock-handle]')!.classList.contains('ml-auto')).toBe(true);
    });

    it('Hide closes the pane and hands the keyboard to the header\'s caret', () => {
        mount({ open: true });
        fireEvent.click(hide());
        expect(aside()).toBeNull();
        expect(state().open).toBe(false);
        expect(document.activeElement).toBe(caret());
    });

    it('a right-click on Hide docks the pane on the other side, still open', () => {
        mount({ open: true, side: 'right' });
        const menu = fireEvent.contextMenu(hide());
        expect(menu).toBe(false);
        expect(state()).toMatchObject({ open: true, side: 'bottom' });
        expect(aside()).toHaveAttribute('data-runs-side', 'bottom');
        fireEvent.contextMenu(hide());
        expect(state().side).toBe('right');
    });

    it('its tooltip names the right-click and the drag', () => {
        mount({ open: true, side: 'right' });
        expect(hide().title).toBe('Hide status · right-click: dock on the bottom · drag to the right or bottom edge to dock');
    });
});

describe('the resize handle', () => {
    it.each([
        ['right', 'left', 'vertical', /-left-px\b/],
        ['bottom', 'top', 'horizontal', /-top-px\b/],
    ] as const)('docked %s, the handle sits on the %s edge, facing the content', (side, edge, orientation, place) => {
        mount({ open: true, side });
        expect(handle()).toHaveAttribute('data-resize-edge', edge);
        expect(handle()).toHaveAttribute('aria-orientation', orientation);
        expect(handle().className).toMatch(place);
    });

    it.each([
        ['right', 'clientX', 620, 560, 'width', 340],
        ['bottom', 'clientY', 400, 340, 'height', 260],
    ] as const)('docked %s, moving the edge toward the content grows the pane, committed on release', (side, axis, from, to, key, value) => {
        mockBoxes();
        mount({ open: true, side });
        fireEvent.pointerDown(handle(), { [axis]: from, button: 0, pointerId: 1 });
        fireEvent.pointerMove(handle(), { [axis]: to, pointerId: 1 });
        expect(state()[key]).toBeNull();
        fireEvent.pointerUp(handle(), { [axis]: to, pointerId: 1 });
        expect(state()[key]).toBe(value);
        expect(state()[key === 'width' ? 'height' : 'width']).toBeNull();
    });

    it('a quick stationary press hides the pane', () => {
        mount({ open: true, width: 300 });
        const now = vi.spyOn(Date, 'now');
        now.mockReturnValue(1000);
        fireEvent.pointerDown(handle(), { clientX: 300, clientY: 200, button: 0, pointerId: 1 });
        now.mockReturnValue(1100);
        fireEvent.pointerUp(handle(), { clientX: 300, clientY: 200, pointerId: 1 });
        expect(state()).toMatchObject({ open: false, width: 300 });
    });
});

describe('the keyboard', () => {
    it('a pane already open as it mounts takes nothing; opening it puts the keyboard in its view', () => {
        mount({ open: true });
        expect(document.activeElement).toBe(document.body);
        fireEvent.click(hide());
        fireEvent.click(screen.getByRole('button', { name: 'open' }));
        expect(document.activeElement).toBe(screen.getByTestId('status-view'));
    });

    it('a view change moves the keyboard into the new view; an option change does not', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN } });
        mount();
        view({ view: 'all', option: 'default' });
        expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
        act(() => (document.activeElement as HTMLElement).blur());
        view({ view: 'all', option: 'log' });
        expect(document.activeElement).toBe(document.body);
        view({ view: 'run', runId: 'run-1', option: 'default' });
        expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
    });

    it('Esc in a drilldown goes back to the status view on that run\'s row; Esc there closes the pane, going no further', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN } });
        mount();
        view({ view: 'run', runId: 'run-1', option: 'default' });
        fireEvent.keyDown(screen.getByTestId('annotation-timeline'), { key: 'Escape' });
        expect(screen.getByTestId('status-view')).toBeInTheDocument();
        const row = screen.getByTestId('status-run-row');
        expect(document.activeElement).toBe(row);
        const reachedWindow = vi.fn();
        window.addEventListener('keydown', reachedWindow);
        try {
            fireEvent.keyDown(row, { key: 'Escape' });
        } finally {
            window.removeEventListener('keydown', reachedWindow);
        }
        expect(aside()).toBeNull();
        expect(document.activeElement).toBe(caret());
        expect(reachedWindow).not.toHaveBeenCalled();
    });

    it('Esc on the rail\'s Hide in a drilldown goes back; on the status view it closes', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN } });
        mount();
        view({ view: 'all', option: 'default' });
        fireEvent.keyDown(hide(), { key: 'Escape' });
        expect(screen.getByTestId('status-view')).toBeInTheDocument();
        fireEvent.keyDown(hide(), { key: 'Escape' });
        expect(aside()).toBeNull();
    });

    it('in the full-window view, whose region covers the header, Esc on the status view hands the keyboard to the view', () => {
        useCollimateStore.setState({ maximizedOverlayCellId: 'cell-1' } as never);
        try {
            render(
                <div data-full-window="">
                    <Harness initial={layoutWith({ open: true })} allowRun selected={false} />
                </div>,
            );
            const status = screen.getByTestId('status-view');
            act(() => status.focus());
            fireEvent.keyDown(status, { key: 'Escape' });
            expect(aside()).toBeNull();
            expect(document.activeElement).toBe(document.querySelector('[data-full-window]'));
        } finally {
            useCollimateStore.setState({ maximizedOverlayCellId: null } as never);
        }
    });

    it('a focus request takes the view, or the composer; one older than the mount is not replayed', async () => {
        vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] });
        try {
            useCollimateStore.setState({ runsPaneFocus: { 'cell-1': { target: 'view', seq: 4, at: Date.now() - 60_000 } } });
            mount();
            act(() => { vi.runAllTimers(); });
            expect(document.activeElement).toBe(document.body);
            act(() => useCollimateStore.setState({ runsPaneView: { 'cell-1': { view: 'all', option: 'default' } } }));
            act(() => (document.activeElement as HTMLElement).blur());
            act(() => useCollimateStore.setState({ runsPaneFocus: { 'cell-1': { target: 'composer', seq: 5, at: Date.now() } } }));
            act(() => { vi.runAllTimers(); });
            expect(document.activeElement).toBe(screen.getByTestId('fake-editor'));
            act(() => useCollimateStore.setState({ runsPaneFocus: { 'cell-1': { target: 'view', seq: 6, at: Date.now() } } }));
            act(() => { vi.runAllTimers(); });
            expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
        } finally {
            vi.useRealTimers();
        }
    });

    it('a fresh focus request lands when the pane mounts with it (opened after a channel switch)', () => {
        vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] });
        try {
            useCollimateStore.setState({ runsPaneFocus: { 'cell-1': { target: 'view', seq: 1, at: Date.now() } } });
            mount();
            act(() => { vi.runAllTimers(); });
            expect(document.activeElement).toBe(screen.getByTestId('status-view'));
        } finally {
            vi.useRealTimers();
        }
    });
});
