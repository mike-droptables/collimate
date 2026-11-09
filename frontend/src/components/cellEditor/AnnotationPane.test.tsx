// The cell's annotation pane: docked on the right (a column) or across the
// bottom (a band), with its resize handle on the content-facing edge; the
// router over its two views (All, the default, and a run's drilldown) with
// each view's rail (the run picker first) and the Hide toggle; the
// keyboard's way in (on open, on a view change, on a focus request) and out
// (Esc closes the pane once the composer has closed).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { forwardRef, useImperativeHandle, useState } from 'react';
import { AnnotationPane } from './AnnotationPane';
import { CellSelectionContext } from '@/contexts/CellSelectionContext';
import { createDefaultLayout, openRunsPane, setRunsPane } from '@/lib/cellLayout';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { CellLayout, RunsPaneState, AnnotationPaneView } from '@/stores/collimate/types';
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
const RUN_2: RunRecord = { ...RUN, runId: 'run-2', runnerId: 'chat', runnerName: 'Chat', label: 'chat-22222222', folder: '_runs/chat-22222222' };

const layoutWith = (patch: Partial<RunsPaneState>): CellLayout =>
    setRunsPane(createDefaultLayout({ filePaths: new Set<string>(), pinnedPath: null }), patch);

function Harness({ initial, selected }: { initial: CellLayout; selected: boolean }) {
    const [layout, setLayout] = useState(initial);
    return (
        <div data-cell-id="cell-1">
            <button type="button" onClick={() => setLayout(l => openRunsPane(l))}>open</button>
            <CellSelectionContext.Provider value={selected}>
                <div data-runs-dock-region style={{ display: 'flex' }}>
                    <div><button type="button" data-testid="annotation-pane-toggle">toggle</button>content</div>
                    <AnnotationPane cellId="cell-1" layout={layout} onLayoutChange={fn => setLayout(fn)} allowAnnotate maximized={false} />
                </div>
            </CellSelectionContext.Provider>
            <output data-testid="pane-state">{JSON.stringify(layout.runsPane)}</output>
        </div>
    );
}

const mount = (patch: Partial<RunsPaneState> = { open: true }, { selected = false } = {}) =>
    render(<Harness initial={layoutWith(patch)} selected={selected} />);

const aside = () => screen.queryByRole('complementary', { name: 'Annotation' });
const state = (): RunsPaneState => JSON.parse(screen.getByTestId('pane-state').textContent!);
const hide = () => screen.getByRole('button', { name: 'Hide annotation' });
const handle = () => screen.getByRole('separator', { name: 'Resize annotation' });
const toggle = () => screen.getByTestId('annotation-pane-toggle');
const picker = () => screen.getByTestId('annotation-run-picker');
const view = (v: AnnotationPaneView) => act(() => { useCollimateStore.getState().setAnnotationPaneView('cell-1', v); });
const stored = () => useCollimateStore.getState().annotationPaneView['cell-1'];

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
        annotationPaneView: {}, annotationPaneFocus: {}, finishedRuns: {}, annotationContexts: {}, fileTouches: {}, lastFileTab: {},
        composerDrafts: {}, annotationRequests: {}, annotationNotice: {},
        loadCellAnnotations: vi.fn(async () => {}), refreshAnnotationContext: vi.fn(async () => {}), loadFileTouches: vi.fn(async () => {}),
        readSavedCellLogs: vi.fn(async () => {}), markFailureSeen: vi.fn(),
    } as never);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('the annotation pane per dock side', () => {
    it('renders nothing while closed', () => {
        mount({ open: false });
        expect(aside()).toBeNull();
    });

    it.each([
        ['right', ['h-full', 'border-l'], { width: '280px', minWidth: 'min(200px, min(66.6667%, 100% - 160px))', maxWidth: 'min(66.6667%, 100% - 160px)', height: '' }],
        ['bottom', ['w-full', 'border-t'], { height: '200px', minHeight: 'min(120px, min(66.6667%, 100% - 120px))', maxHeight: 'min(66.6667%, 100% - 120px)', width: '' }],
    ] as const)('docked %s, it is the Annotation aside with its border and its stored size', (side, classes, style) => {
        mount({ open: true, side });
        const pane = aside()!;
        expect(pane).toHaveAttribute('data-annotation-pane');
        expect(pane).toHaveAttribute('data-annotation-side', side);
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
    it('shows All with no stored view', () => {
        mount();
        expect(aside()).toHaveAttribute('data-annotation-view', 'all');
        expect(screen.getByTestId('run-all')).toBeInTheDocument();
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

    it('a drilldown of a run the pane no longer knows falls back to All', () => {
        mount();
        view({ view: 'run', runId: 'gone', option: 'log' });
        expect(screen.getByTestId('run-all')).toBeInTheDocument();
        expect(stored()).toEqual({ view: 'all', option: 'default' });
    });

    it('wakes the guest pool on every view and option change', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN } });
        mount();
        pool.invalidate.mockClear();
        view({ view: 'run', runId: 'run-1', option: 'default' });
        expect(pool.invalidate).toHaveBeenCalled();
        pool.invalidate.mockClear();
        view({ view: 'run', runId: 'run-1', option: 'log' });
        expect(pool.invalidate).toHaveBeenCalled();
    });
});

describe('the run picker', () => {
    it('heads the rail: All, or the run on show with its dot', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN } });
        mount();
        const rail = hide().closest('[data-runs-dock-handle]')!.parentElement!;
        expect(rail.firstElementChild!.firstElementChild).toBe(picker());
        expect(picker().textContent).toBe('All');
        expect(picker().querySelector('i')).toBeNull();
        view({ view: 'run', runId: 'run-1', option: 'default' });
        expect(picker().textContent).toBe('Agent 11111111');
        expect(picker().querySelector('i')!.className).toContain('border-teal');
        // The run's Stop stands right after it.
        expect(picker().nextElementSibling).toBe(screen.getByTestId('run-stop'));
    });

    it('lists All then each live run; picking one shows it, keeping the option; All goes back', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN, 'run-2': RUN_2 }, runPhases: { 'run-2': { phase: 'awaiting_approval' } } });
        mount();
        view({ view: 'all', option: 'log' });
        fireEvent.click(picker());
        expect(picker()).toHaveAttribute('aria-expanded', 'true');
        const menu = screen.getByRole('dialog', { name: 'Show run' });
        const rows = within(menu).getAllByRole('button');
        expect(rows.map(r => r.getAttribute('data-testid'))).toEqual(['annotation-pick-all', 'annotation-pick-run', 'annotation-pick-run']);
        expect(rows[0]).toHaveAttribute('aria-current', 'true');
        expect(rows.slice(1).map(r => [r.dataset.runId, r.textContent])).toEqual([['run-1', 'Agent11111111'], ['run-2', 'Chat22222222']]);
        expect(rows[2].querySelector('i')!.className).toContain('border-brass');
        fireEvent.click(rows[2]);
        expect(stored()).toEqual({ view: 'run', runId: 'run-2', option: 'log' });
        expect(screen.queryByRole('dialog', { name: 'Show run' })).toBeNull();
        expect(screen.getByTestId('run-drilldown')).toHaveAttribute('data-run-id', 'run-2');
        fireEvent.click(picker());
        fireEvent.click(screen.getByTestId('annotation-pick-all'));
        expect(stored()).toEqual({ view: 'all', option: 'log' });
    });

    it('lists a finished run on show, dimmed, after the live ones', () => {
        useCollimateStore.setState({ runs: { 'run-2': RUN_2 }, finishedRuns: { 'cell-1': [{ ...RUN, status: 'failed', endedAt: 1 }] } });
        mount();
        view({ view: 'run', runId: 'run-1', option: 'default' });
        expect(picker().querySelector('i')!.className).toContain('border-red');
        fireEvent.click(picker());
        const rows = screen.getAllByTestId('annotation-pick-run');
        expect(rows.map(r => [r.dataset.runId, r.dataset.ended ?? null])).toEqual([['run-2', null], ['run-1', 'true']]);
        expect(rows[1].className).toContain('opacity-[0.55]');
        expect(rows[1]).toHaveAttribute('aria-current', 'true');
    });

    it('↑↓ move over the menu\'s rows; Esc closes it back to the picker, the pane open', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN } });
        mount();
        fireEvent.click(picker());
        const all = screen.getByTestId('annotation-pick-all');
        const run = screen.getByTestId('annotation-pick-run');
        act(() => all.focus());
        fireEvent.keyDown(all, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(run);
        fireEvent.keyDown(run, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(all);
        fireEvent.keyDown(all, { key: 'ArrowUp' });
        expect(document.activeElement).toBe(run);
        fireEvent.keyDown(run, { key: 'Escape' });
        expect(screen.queryByRole('dialog', { name: 'Show run' })).toBeNull();
        expect(document.activeElement).toBe(picker());
        expect(aside()).not.toBeNull();
    });
});

describe('the rail', () => {
    it('sits on the tab strip\'s icon line: the picker, then Hide annotation (pressed, an X) at the far end', () => {
        mount({ open: true, side: 'right' });
        const rail = hide().closest('[data-runs-dock-handle]')!.parentElement!;
        expect(rail.className.split(/\s+/)).toEqual(expect.arrayContaining(['h-[1.875rem]', 'items-end', 'pb-[calc(0.25rem-1px)]', 'border-b', 'border-line']));
        expect(hide()).toHaveAttribute('aria-pressed', 'true');
        expect(hide().querySelector('.lucide-x')).not.toBeNull();
        expect(hide().className.split(/\s+/)).toEqual(expect.arrayContaining(['h-[1.125rem]', 'aria-pressed:bg-sheet-2']));
        expect(rail.children[0].textContent).toBe('All');
        expect(hide().closest('[data-runs-dock-handle]')!.classList.contains('ml-auto')).toBe(true);
    });

    it('Hide closes the pane and hands the keyboard to the annotation toggle on the cell\'s tab strip', () => {
        mount({ open: true });
        fireEvent.click(hide());
        expect(aside()).toBeNull();
        expect(state().open).toBe(false);
        expect(document.activeElement).toBe(toggle());
    });

    it('a right-click on Hide docks the pane on the other side, still open', () => {
        mount({ open: true, side: 'right' });
        const menu = fireEvent.contextMenu(hide());
        expect(menu).toBe(false);
        expect(state()).toMatchObject({ open: true, side: 'bottom' });
        expect(aside()).toHaveAttribute('data-annotation-side', 'bottom');
        fireEvent.contextMenu(hide());
        expect(state().side).toBe('right');
    });

    it('its tooltip names the right-click and the drag', () => {
        mount({ open: true, side: 'right' });
        expect(hide().title).toBe('Hide annotation · right-click: dock on the bottom · drag to the right or bottom edge to dock');
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
        expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
    });

    it('a view change moves the keyboard into the new view; an option change does not', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN } });
        mount();
        view({ view: 'run', runId: 'run-1', option: 'default' });
        expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
        act(() => (document.activeElement as HTMLElement).blur());
        view({ view: 'run', runId: 'run-1', option: 'file' });
        expect(document.activeElement).toBe(document.body);
        view({ view: 'all', option: 'default' });
        expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
    });

    it('Esc in a view closes the pane, going no further, and hands the keyboard to the toggle', () => {
        useCollimateStore.setState({ runs: { 'run-1': RUN } });
        mount();
        view({ view: 'run', runId: 'run-1', option: 'default' });
        const reachedWindow = vi.fn();
        window.addEventListener('keydown', reachedWindow);
        try {
            fireEvent.keyDown(screen.getByTestId('annotation-timeline'), { key: 'Escape' });
        } finally {
            window.removeEventListener('keydown', reachedWindow);
        }
        expect(aside()).toBeNull();
        expect(document.activeElement).toBe(toggle());
        expect(reachedWindow).not.toHaveBeenCalled();
    });

    it('Esc on the rail\'s Hide closes the pane', () => {
        mount();
        fireEvent.keyDown(hide(), { key: 'Escape' });
        expect(aside()).toBeNull();
    });

    it('Esc layers: the expanded composer collapses, the inline one closes to its add row, then the pane closes', () => {
        mount();
        fireEvent.click(screen.getByTestId('composer-add'));
        const editor = screen.getByTestId('fake-editor');
        expect(document.activeElement).toBe(editor);
        fireEvent.click(screen.getByTestId('composer-expand'));
        expect(screen.getByTestId('annotation-composer')).toHaveAttribute('data-expanded', 'true');
        fireEvent.keyDown(editor, { key: 'Escape' });
        expect(screen.getByTestId('annotation-composer')).toHaveAttribute('data-expanded', 'false');
        fireEvent.keyDown(editor, { key: 'Escape' });
        expect(screen.queryByTestId('annotation-composer')).toBeNull();
        expect(document.activeElement).toBe(screen.getByTestId('composer-add'));
        expect(aside()).not.toBeNull();
        fireEvent.keyDown(screen.getByTestId('composer-add'), { key: 'Escape' });
        expect(aside()).toBeNull();
        expect(document.activeElement).toBe(toggle());
    });

    it('in the full-window view (portaled out of the card), Esc hands the keyboard to the view', () => {
        useCollimateStore.setState({ maximizedOverlayCellId: 'cell-1' } as never);
        try {
            render(
                <div data-full-window="">
                    <Harness initial={layoutWith({ open: true })} selected={false} />
                </div>,
            );
            const log = screen.getByTestId('annotation-timeline');
            act(() => log.focus());
            fireEvent.keyDown(log, { key: 'Escape' });
            expect(aside()).toBeNull();
            expect(document.activeElement).toBe(document.querySelector('[data-full-window]'));
        } finally {
            useCollimateStore.setState({ maximizedOverlayCellId: null } as never);
        }
    });

    it('a focus request takes the view, or the composer (opening it); one older than the mount is not replayed', async () => {
        vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] });
        try {
            useCollimateStore.setState({ annotationPaneFocus: { 'cell-1': { target: 'view', seq: 4, at: Date.now() - 60_000 } } });
            mount();
            act(() => { vi.runAllTimers(); });
            expect(document.activeElement).toBe(document.body);
            act(() => useCollimateStore.setState({ annotationPaneFocus: { 'cell-1': { target: 'composer', seq: 5, at: Date.now() } } }));
            act(() => { vi.runAllTimers(); });
            expect(useCollimateStore.getState().composerDrafts['cell-1:all'].open).toBe(true);
            expect(document.activeElement).toBe(screen.getByTestId('fake-editor'));
            act(() => useCollimateStore.setState({ annotationPaneFocus: { 'cell-1': { target: 'view', seq: 6, at: Date.now() } } }));
            act(() => { vi.runAllTimers(); });
            expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
        } finally {
            vi.useRealTimers();
        }
    });

    it('a fresh focus request lands when the pane mounts with it (opened after a channel switch)', () => {
        vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame'] });
        try {
            useCollimateStore.setState({ annotationPaneFocus: { 'cell-1': { target: 'view', seq: 1, at: Date.now() } } });
            mount();
            act(() => { vi.runAllTimers(); });
            expect(document.activeElement).toBe(screen.getByTestId('annotation-timeline'));
        } finally {
            vi.useRealTimers();
        }
    });
});
