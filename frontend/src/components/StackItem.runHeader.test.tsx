import { fixtureBodyHash } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent, cleanup } from '@testing-library/react';
import type { RefObject } from 'react';
import type { FocusContextType } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { createDefaultLayout, setRunsPane } from '@/lib/cellLayout';
import type { RunsPaneState } from '@/stores/collimate/types';
import type { Cell, RunRecord, VirtualFile } from '@/lib/types';

// The header (light, Run, title, tools and the status caret) is what's
// under test; the editor below (its status pane included) drags in
// renderers, iframes and the whole cell editor. The stub counts its
// renders: a StackItem re-render re-renders it. The width tool is its own
// unit (C17): a marker here.
const { editorRenders, headerReports } = vi.hoisted(() => ({
    editorRenders: vi.fn(),
    headerReports: [] as Array<{ cellId: string; ref: RefObject<HTMLElement | null>; enabled: boolean }>,
}));
vi.mock('./CellEditor', () => ({ default: () => { editorRenders(); return <div data-testid="cell-editor" />; } }));
vi.mock('./MarkdownView', () => ({ MarkdownView: () => null }));
vi.mock('./StackWidthControl', () => ({ StackWidthControl: () => <button type="button" aria-label="Width" /> }));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: vi.fn() }));
vi.mock('@/hooks/useCellChromeReport', () => ({
    useReportHeaderChrome: (cellId: string, ref: RefObject<HTMLElement | null>, enabled: boolean) => {
        headerReports.push({ cellId, ref, enabled });
    },
}));
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    stopRun: vi.fn().mockResolvedValue(undefined),
}));

import { StackItem } from './StackItem';
import stackItemSource from './StackItem.tsx?raw';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

// Two synthetic runs on cell-1.
const runRecord = (key: 'a' | 'b'): RunRecord => ({
    runId: `run-${key}`,
    cellId: 'cell-1',
    channel: 'ch',
    runnerId: `runner-${key}`,
    runnerName: `Runner ${key.toUpperCase()}`,
    generatorName: 'synthetic',
    label: `runner-${key}-${key.repeat(8)}`,
    folder: `_runs/runner-${key}-${key.repeat(8)}`,
});
const RUN_A = runRecord('a');
const RUN_B = runRecord('b');
const vf = (name: string): VirtualFile => (fileFixture(name, '', {language: 'plaintext', timestamp: 't'}));
const A_CHAT = `${RUN_A.folder}/chat/conversation.chat`;
const B_TAB = `${RUN_B.folder}/tab-1.webview`;
const FILES = [vf('notes.md'), vf(A_CHAT), vf(B_TAB)];

const cell = (extra: Partial<Cell> = {}): Cell => ({
    id: 'cell-1', name: 'Test cell', files: FILES, channel: 'ch', ...extra,
});

const focus = { focusArea: 'main' } as unknown as FocusContextType;

type Props = React.ComponentProps<typeof StackItem>;
const mount = (over: Partial<Props> = {}) => {
    const onRemoveCell = vi.fn();
    const onDuplicateToNewStack = vi.fn();
    const onSelect = vi.fn();
    const props: Props = {
        stackId: 's1', cell: cell(), index: 0, isFront: true, isSelected: true, viewMode: 'stack',
        width: 600, height: 400,
        focusArea: focus.focusArea,
        onSelect, onUpdate: vi.fn(), onCellDragStart: vi.fn(),
        onRemoveCell, onDuplicateToNewStack,
        ...over,
    };
    const view = render(<StackItem {...props} />);
    return { onRemoveCell, onDuplicateToNewStack, onSelect, rerender: (p: Partial<Props>) => view.rerender(<StackItem {...props} {...p} />) };
};

const liveRuns = (...runs: RunRecord[]) => useCollimateStore.setState({
    runs: Object.fromEntries(runs.map(r => [r.runId, r])),
    stoppingRunIds: new Set(),
} as never);
const endAll = () => act(() => {
    useCollimateStore.setState({ runs: {}, stoppingRunIds: new Set() } as never);
});

const removeButton = () => screen.getByRole('button', { name: 'Remove cell' });
const queryRemoveButton = () => screen.queryByRole('button', { name: 'Remove cell' });
const duplicate = () => screen.getByRole('button', { name: 'Duplicate cell' });
const light = () => screen.getByTestId('run-light');
const caret = () => screen.getByTestId('runs-pane-toggle');
const runButton = () => screen.queryByTestId('cell-run');
const runSlot = () => document.querySelector('[data-run-slot]') as HTMLElement | null;
const runsPane = () => useCollimateStore.getState().cellLayouts['cell-1'].runsPane;
const card = () => document.querySelector('[data-stack-card]') as HTMLElement;
/** The header the scene measures: the element its ref reports. */
const header = () => headerReports[headerReports.length - 1].ref.current as HTMLElement;
const tools = () => removeButton().parentElement as HTMLElement;

/** The cell's layout as its editor seeds it, its status pane patched. */
const withRunsPane = (patch: Partial<RunsPaneState> = {}) => useCollimateStore.setState({
    cellLayouts: { 'cell-1': setRunsPane(createDefaultLayout({ filePaths: [], pinnedPath: null }), patch) },
});

const resetStore = () => {
    useCollimateStore.setState({
        cellCache: {}, runs: {}, stoppingRunIds: new Set(),
        runPhases: {}, runLogs: {}, lastFinishedByCell: {},
        cellLayouts: {}, state: null, pendingSelectCellId: null, activeRunsEverywhere: [],
        cellHistory: {}, cellDiffs: {}, pickSessions: {}, cellDisplaysReady: true,
    } as never);
    withRunsPane();
    headerReports.length = 0;
};

describe('StackItem cell header (C11)', () => {
    beforeEach(() => { cleanup(); localStorage.clear(); resetStore(); });

    it('is the mock\'s 36px head: 14px in on the left, 10px gaps, a hairline under it, the light first (C11)', async () => {
        mount();
        const h = header();
        expect(h.className.split(/\s+/)).toEqual(expect.arrayContaining(['h-9', 'pl-[0.875rem]', 'pr-[0.625rem]', 'gap-[0.625rem]', 'border-b', 'border-line', 'items-center']));
        expect(h.contains(light())).toBe(true);
        expect(light().getAttribute('data-light-state')).toBe('idle');
        expect(headerReports[headerReports.length - 1]).toMatchObject({ cellId: 'cell-1', enabled: true });
    });

    it('the zoomed-out tile reports no header (it has a different one) (C11)', async () => {
        mount({ viewMode: 'overview' });
        expect(headerReports[headerReports.length - 1].enabled).toBe(false);
    });

    it('the title is the editable name in the UI face at 14px/600, ellipsized (C11)', async () => {
        mount();
        const title = screen.getByRole('button', { name: 'Test cell' });
        const classes = title.className.split(/\s+/);
        expect(classes).toEqual(expect.arrayContaining(['text-[0.875rem]', 'font-semibold', 'truncate', 'text-ink']));
        expect(classes).not.toContain('font-mono');
        expect(header().contains(title)).toBe(true);
    });

    it('tools: Tags, Move, Width, Duplicate, the status caret, Remove, in that order, each a 24px hit area (C11) (TG-05)', async () => {
        mount();
        const labels = [...tools().querySelectorAll<HTMLElement>('[aria-label]')].map(el => el.getAttribute('aria-label'));
        expect(labels).toEqual(['Tags', 'Move cell', 'Width', 'Duplicate cell', 'Show the status pane', 'Remove cell']);
        for (const name of ['Move cell', 'Duplicate cell', 'Remove cell']) {
            expect(screen.getByRole('button', { name }).className.split(/\s+/)).toEqual(expect.arrayContaining(['h-6', 'w-6']));
        }
        // Tags and the caret grow leftwards with their counts from a 24px minimum.
        for (const el of [screen.getByRole('button', { name: 'Tags' }), caret()]) {
            expect(el.className.split(/\s+/)).toEqual(expect.arrayContaining(['h-6', 'min-w-6']));
        }
    });

    it('the tools sit at 0.55 until the cell is hovered; the selected cell shows them at full (C11)', async () => {
        // Quiet is each tool's ink at 55% alpha, lifted to full by the card's
        // hover, and the fade is a colour fade: never an opacity class or
        // transition (one runs on the compositor and turns the card's text
        // grayscale while it runs).
        const quiet = ['[--tools-alpha:55%]', 'group-hover/card:[--tools-alpha:100%]', '[&_:where(button,[role=button])]:text-ink-3/(--tools-alpha)'];
        mount({ isSelected: false });
        expect(tools().className.split(/\s+/)).toEqual(expect.arrayContaining([...quiet, '[&_:where(button,[role=button])]:duration-[120ms]']));
        expect(tools().className).not.toMatch(/opacity/);
        expect(card().className.split(/\s+/)).toContain('group/card');
        cleanup();
        mount();
        for (const c of quiet) expect(tools().className.split(/\s+/)).not.toContain(c);
        expect(tools().className).not.toMatch(/opacity/);
    });

    it('there is no Center button (C11)', async () => {
        // The props the old header drew it from, spread in: it still has none.
        mount({ onCenterStack: vi.fn(), onCenterPanStart: vi.fn(), onCenterPanMove: vi.fn(), onCenterPanEnd: () => false } as unknown as Partial<Props>);
        expect(screen.queryByRole('button', { name: /center/i })).toBeNull();
    });

    it('a Capabilities cell: no light, no Run slot, Tags, Move, Width and Hide; no Duplicate, caret or Remove (C11) (D20)', async () => {
        mount({ capabilitiesCell: true, onHideStack: vi.fn() });
        expect(screen.queryByTestId('run-light')).toBeNull();
        expect(runButton()).toBeNull();
        expect(runSlot()).toBeNull();
        expect(screen.queryByTestId('runs-pane-toggle')).toBeNull();
        expect(screen.getByRole('button', { name: 'Tags' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Move cell' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Width' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Hide Capabilities' })).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Duplicate cell' })).toBeNull();
        expect(queryRemoveButton()).toBeNull();
        expect(header().className).toContain('bg-teal-sheet');
        // Its title is the editable name, as on any user cell.
        expect(screen.getByRole('button', { name: 'Test cell' })).toBeTruthy();
    });

    it('a Capabilities cell never shows Run or the light, even with a live run in the store (I2)', async () => {
        liveRuns(RUN_A);
        mount({ capabilitiesCell: true, onHideStack: vi.fn() });
        expect(screen.queryByTestId('run-light')).toBeNull();
        expect(screen.queryByTestId('runs-pane-toggle')).toBeNull();
        expect(runButton()).toBeNull();
    });

    it.each([false, true])('an idle header has no cell icon (Capabilities: %s)', capabilitiesCell => {
        mount({ capabilitiesCell, cell: cell({ icon: 'box' }) });
        expect(screen.queryByRole('button', { name: /cell icon/i })).toBeNull();
        expect(header().querySelector('svg.lucide-monitor')).toBeNull();
        if (!capabilitiesCell) {
            expect(light().className).toContain('border-ink-3');
            expect(light().querySelector('svg')).toBeNull();
        }
    });

    it('a built-in card: a Built-in chip, a plain title, Width and Hide (no tags, drag, remove, duplicate or caret)', async () => {
        mount({ capabilitiesCell: true, onHideStack: vi.fn(), cell: cell({ name: 'Definitions', builtin: true, files: [vf('generator/generator.handler'), vf('generator/derived/verify.ts')] }) });
        const chip = document.querySelector('[data-builtin-chip]') as HTMLElement;
        expect(chip.textContent).toBe('Built-in');
        // The Definitions card holds handlers: its chip points at its card in Capabilities.
        expect(chip.title).toBe('Part of the app: it can’t be removed, moved or edited. Add examples and regenerate skills from its card in Capabilities.');
        // The title is text, not the name editor.
        expect(screen.getByText('Definitions').tagName).toBe('SPAN');
        fireEvent.doubleClick(screen.getByText('Definitions'));
        expect(screen.queryByRole('textbox')).toBeNull();
        expect(screen.queryByRole('button', { name: 'Tags' })).toBeNull();
        expect(screen.queryByRole('button', { name: 'Move cell' })).toBeNull();
        expect(screen.getByRole('button', { name: 'Width' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Hide Capabilities' })).toBeTruthy();
        expect(screen.queryByTestId('runs-pane-toggle')).toBeNull();
        expect(screen.queryByRole('button', { name: 'Duplicate cell' })).toBeNull();
        expect(queryRemoveButton()).toBeNull();
        expect(runSlot()).toBeNull();
    });

    it('the Text card\'s chip says only that it is part of the app (it holds no handlers)', async () => {
        mount({ capabilitiesCell: true, cell: cell({ name: 'Text', builtin: true, files: [vf('text/text.renderer'), vf('text/main.tsx')] }) });
        expect((document.querySelector('[data-builtin-chip]') as HTMLElement).title).toBe('Part of the app: it can’t be removed, moved or edited.');
    });

    it('a built-in card\'s overview card has the chip and does not drag', async () => {
        const onCellDragStart = vi.fn();
        mount({ capabilitiesCell: true, viewMode: 'overview', cell: cell({ name: 'Live', builtin: true, files: [vf('todo/todo.renderer'), vf('todo/main.tsx')] }), onCellDragStart });
        const card = document.querySelector('[data-ov-card]') as HTMLElement;
        // The Live card holds renderers only: no examples or skills to add.
        expect((card.querySelector('[data-builtin-chip]') as HTMLElement).title).toBe('Part of the app: it can’t be removed, moved or edited.');
        expect(card.hasAttribute('data-stack-card')).toBe(false);
        fireEvent.pointerDown(card, { button: 0 });
        expect(onCellDragStart).not.toHaveBeenCalled();
    });
});

describe('StackItem — Hide in a Capabilities cell\'s head, in Remove\'s slot (D20)', () => {
    beforeEach(() => { cleanup(); localStorage.clear(); resetStore(); });

    const hide = () => screen.queryByTestId('cell-hide-stack') as HTMLButtonElement | null;
    const toolLabels = () => [...(hide()!.parentElement as HTMLElement).querySelectorAll<HTMLElement>('[aria-label]')]
        .map(el => el.getAttribute('aria-label'));

    it('a user cell: Tags, Move, Width, then Hide last (the head\'s quiet 24px tool)', () => {
        mount({ capabilitiesCell: true, onHideStack: vi.fn() });
        const b = hide()!;
        expect(b).toHaveAccessibleName('Hide Capabilities');
        expect(b.title).toBe('Hide Capabilities to the top bar');
        expect(b.disabled).toBe(false);
        expect(b.querySelector('svg.lucide-arrow-up')).not.toBeNull();
        expect(toolLabels()).toEqual(['Tags', 'Move cell', 'Width', 'Hide Capabilities']);
        expect(b.className.split(/\s+/)).toEqual(expect.arrayContaining(['h-6', 'w-6', 'text-ink-3', 'hover:bg-sheet-2']));
        expect(b.className).not.toContain('hover:bg-red-soft');
    });

    it('a built-in card: Width, Hide', () => {
        mount({ capabilitiesCell: true, cell: cell({ name: 'Definitions', builtin: true }), onHideStack: vi.fn() });
        expect(toolLabels()).toEqual(['Width', 'Hide Capabilities']);
    });

    it('a live run on the cell never greys it: nothing runs on Capabilities', () => {
        liveRuns(RUN_A);
        mount({ capabilitiesCell: true, onHideStack: vi.fn() });
        expect(hide()!.disabled).toBe(false);
    });

    it('a standard cell has none, even handed the callback', () => {
        mount({ onHideStack: vi.fn() });
        expect(hide()).toBeNull();
        expect(removeButton()).toBeTruthy();
    });

    it('a click hides the stack and goes no further: no select, no menu', () => {
        const onHideStack = vi.fn();
        const { onSelect } = mount({ capabilitiesCell: true, onHideStack });
        fireEvent.click(hide()!);
        expect(onHideStack).toHaveBeenCalledTimes(1);
        expect(onSelect).not.toHaveBeenCalled();
        expect(screen.queryByRole('menu')).toBeNull();
    });

    it('the memo re-renders when the cell moves onto or off Capabilities', () => {
        const { rerender } = mount({ onHideStack: vi.fn() });
        expect(hide()).toBeNull();
        rerender({ capabilitiesCell: true });
        expect(hide()).not.toBeNull();
        expect(header().className).toContain('bg-teal-sheet');
        rerender({ capabilitiesCell: false });
        expect(hide()).toBeNull();
        expect(removeButton()).toBeTruthy();
    });
});

describe('StackItem remove and duplicate (C11)', () => {
    beforeEach(() => { cleanup(); localStorage.clear(); resetStore(); });

    it('idle: Remove shows and removes (C11)', async () => {
        const { onRemoveCell } = mount();
        fireEvent.click(removeButton());
        expect(onRemoveCell).toHaveBeenCalledTimes(1);
    });

    it('while runs are live Remove hides, Duplicate stays (C11)', async () => {
        liveRuns(RUN_A, RUN_B);
        const { onDuplicateToNewStack } = mount();
        expect(queryRemoveButton()).toBeNull();
        fireEvent.click(duplicate());
        expect(onDuplicateToNewStack).toHaveBeenCalledTimes(1);
    });

    it('Remove returns the moment the last run ends (C11)', async () => {
        liveRuns(RUN_A);
        mount();
        expect(queryRemoveButton()).toBeNull();
        endAll();
        expect(removeButton().getAttribute('aria-disabled')).toBeNull();
    });

    it('a stopping run still counts as live (C11)', async () => {
        useCollimateStore.setState({ runs: { 'run-a': RUN_A }, stoppingRunIds: new Set(['run-a']) } as never);
        mount();
        expect(queryRemoveButton()).toBeNull();
    });

    it('fan column: Duplicate copies in place, Remove takes the cell with no burn (C11)', async () => {
        const { onRemoveCell, onDuplicateToNewStack } = mount({ isFanColumn: true });
        expect(duplicate().getAttribute('title')).toMatch(/duplicate cell into the next column/i);
        fireEvent.click(duplicate());
        expect(onDuplicateToNewStack).toHaveBeenCalledTimes(1);
        fireEvent.click(removeButton());
        expect(onRemoveCell).toHaveBeenCalledTimes(1);
        expect(document.querySelector('div.absolute.inset-0.bg-sheet.pointer-events-none')).toBeNull();
    });

    it('a failed removal drops the burn and keeps the card (C11)', async () => {
        mount({ onRemoveCell: () => Promise.resolve(null) });
        await act(async () => { fireEvent.click(removeButton()); });
        expect(document.querySelector('div.absolute.inset-0.bg-sheet.pointer-events-none')).toBeNull();
        expect(screen.getByTestId('cell-editor')).toBeTruthy();
    });

    it('a removal that landed keeps burning until the push unmounts the card (C11)', async () => {
        mount({ onRemoveCell: () => Promise.resolve({ id: 'ch' }) });
        await act(async () => { fireEvent.click(removeButton()); });
        expect(document.querySelector('div.absolute.inset-0.bg-sheet.pointer-events-none')).not.toBeNull();
    });

    it('Remove is a head tool with the red hover (S04)', async () => {
        mount();
        expect(removeButton().className.split(/\s+/)).toEqual(expect.arrayContaining(['hover:bg-red-soft', 'hover:text-red']));
    });
});

describe('StackItem light and the status caret (C16)', () => {
    beforeEach(() => { cleanup(); localStorage.clear(); resetStore(); });

    it('the light is a button before the title: a click selects the cell and opens the status pane, a second closes it (C16)', async () => {
        const { onSelect } = mount({ isSelected: false });
        const title = screen.getByRole('button', { name: 'Test cell' });
        expect(light().compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        fireEvent.click(light());
        expect(onSelect).toHaveBeenCalledWith(0);
        expect(runsPane().open).toBe(true);
        expect(light().getAttribute('aria-expanded')).toBe('true');
        expect(caret().getAttribute('aria-expanded')).toBe('true');
        fireEvent.click(light());
        expect(runsPane().open).toBe(false);
    });

    it('a back card\'s light is a plain dot; the zoomed-out tile\'s too (C16)', async () => {
        liveRuns(RUN_A);
        mount({ isFront: false });
        expect(light().tagName).toBe('SPAN');
        cleanup();
        mount({ viewMode: 'overview' });
        expect(light().tagName).toBe('SPAN');
    });

    it('idle, the caret is a plain ink-3 List glyph turned for the pane\'s side and state (C16)', async () => {
        mount();
        const glyph = () => caret().querySelector('svg.lucide-list')!;
        // Closed on the right: as drawn.
        expect(glyph().classList.contains('-scale-x-100')).toBe(false);
        expect(caret().className).toContain('text-ink-3');
        expect(caret().querySelector('[data-run-count]')).toBeNull();
        expect(caret().getAttribute('aria-expanded')).toBe('false');
        cleanup();
        withRunsPane({ open: true });
        mount();
        // Open on the right: mirrored.
        expect(glyph().classList.contains('-scale-x-100')).toBe(true);
        expect(glyph().classList.contains('rotate-90')).toBe(false);
        expect(caret().getAttribute('aria-label')).toBe('Hide the status pane');
        cleanup();
        withRunsPane({ side: 'bottom' });
        mount();
        // Closed at the bottom: pointed down; open there: pointed up.
        expect(glyph().classList.contains('rotate-90')).toBe(true);
        cleanup();
        withRunsPane({ side: 'bottom', open: true });
        mount();
        expect(glyph().classList.contains('-rotate-90')).toBe(true);
    });

    it.each([
        ['running', undefined, 'text-teal'],
        ['awaiting your message', 'awaiting_input', 'text-brass'],
        ['awaiting approval', 'awaiting_approval', 'text-brass'],
    ])('while runs are live (%s) the caret and its count take the state\'s ink (C16)', async (_label, phase, ink) => {
        useCollimateStore.setState({
            runs: { 'run-a': RUN_A, 'run-b': RUN_B }, stoppingRunIds: new Set(),
            runPhases: phase ? { 'run-a': { phase }, 'run-b': { phase } } : {},
        } as never);
        mount({ isSelected: false });
        const count = caret().querySelector('[data-run-count]') as HTMLElement;
        expect(count.textContent).toBe('2');
        expect(count.className.split(/\s+/)).toEqual(expect.arrayContaining([ink, 'tabular-nums', 'text-[0.6875rem]']));
        expect(caret().querySelector('svg')!.getAttribute('class')).toContain(ink);
        // One button: the number is inside it.
        expect(count.closest('button')).toBe(caret());
        expect(caret().getAttribute('aria-label')).toBe('Show the status pane · 2 running in this cell');
    });

    it('a stopping run turns the caret red (C16)', async () => {
        useCollimateStore.setState({ runs: { 'run-a': RUN_A }, stoppingRunIds: new Set(['run-a']) } as never);
        mount();
        expect((caret().querySelector('[data-run-count]') as HTMLElement).className).toContain('text-red');
    });

    it('the caret is Remove\'s left neighbour, and the rightmost tool while a run is live (C16)', async () => {
        mount();
        expect(caret().closest('[data-runs-dock-handle]')!.nextElementSibling).toBe(removeButton());
        cleanup();
        liveRuns(RUN_A);
        mount();
        expect(caret().closest('[data-runs-dock-handle]')!.nextElementSibling).toBeNull();
    });

    it('a caret click selects the cell and toggles the pane; a right-click docks it on the other side, unopened (C16)', async () => {
        const { onSelect } = mount({ isSelected: false });
        fireEvent.click(caret());
        expect(onSelect).toHaveBeenCalledWith(0);
        expect(runsPane()).toMatchObject({ open: true, side: 'right' });
        fireEvent.click(caret());
        expect(runsPane().open).toBe(false);
        const menu = fireEvent.contextMenu(caret());
        expect(menu).toBe(false);
        expect(runsPane()).toMatchObject({ open: false, side: 'bottom' });
        expect(caret().querySelector('svg.lucide-list')!.classList.contains('rotate-90')).toBe(true);
        fireEvent.contextMenu(caret());
        expect(runsPane().side).toBe('right');
    });

    it('the caret\'s tooltip names the toggle, the right-click and the drag (C16)', async () => {
        mount();
        expect(caret().title).toBe('Show the status pane · right-click: dock on the bottom · drag to the right or bottom edge to dock');
        cleanup();
        withRunsPane({ open: true, side: 'bottom' });
        mount();
        expect(caret().title).toBe('Hide the status pane · right-click: dock on the right · drag to the right or bottom edge to dock');
    });
});

describe('StackItem Run (C14)', () => {
    beforeEach(() => { cleanup(); localStorage.clear(); resetStore(); });

    it('the selected standard cell has Run right after its light: a 24px brass play tool (C14)', async () => {
        mount();
        const run = runButton()!;
        expect(run.tagName).toBe('BUTTON');
        expect(run.getAttribute('aria-label')).toBe('Run');
        expect(run.title).toContain('Run a runner in this cell');
        expect(run.className.split(/\s+/)).toEqual(expect.arrayContaining(['h-6', 'w-6']));
        expect(run.querySelector('[data-play-glyph]')!.getAttribute('class')).toContain('text-brass');
        expect(light().nextElementSibling).toBe(run);
        expect(runSlot()).toBeNull();
    });

    it('a click opens the cell\'s runners panel (C14)', async () => {
        mount();
        fireEvent.click(runButton()!);
        expect(useCollimateStore.getState().pickSessions['cell-1']?.kind).toBe('runners');
    });

    it('an unselected cell keeps Run\'s 24px slot empty, so the title stays put (C14)', async () => {
        mount({ isSelected: false });
        expect(runButton()).toBeNull();
        const slot = runSlot()!;
        expect(slot.className.split(/\s+/)).toEqual(expect.arrayContaining(['w-6', 'h-6', 'flex-none']));
        expect(light().nextElementSibling).toBe(slot);
        // The slot and the button share their box.
        cleanup();
        mount();
        const run = runButton()!;
        for (const c of slot.className.split(/\s+/).filter(c => c !== 'flex-none')) expect(run.className.split(/\s+/)).toContain(c);
    });

    it('no Run in version history, nor on a cell in a diff: the slot stays (C14)', async () => {
        useCollimateStore.setState({ cellHistory: { 'cell-1': { nodeId: 'n', versionId: null } } } as never);
        mount();
        expect(runButton()).toBeNull();
        expect(runSlot()).not.toBeNull();
        cleanup();
        resetStore();
        useCollimateStore.setState({ cellDiffs: { 'cell-1': 'base' }, cellCache: { base: { id: 'base', name: 'Base', files: [] } } } as never);
        mount({ cell: cell({ files: [] }) });
        expect(runButton()).toBeNull();
        expect(runSlot()).not.toBeNull();
    });
});

describe('StackItem memo — focus changes', () => {
    beforeEach(() => { cleanup(); localStorage.clear(); resetStore(); });

    it('a selection change elsewhere does not re-render the card; a focus-area change does', async () => {
        const { rerender } = mount();
        const before = editorRenders.mock.calls.length;
        rerender({ focusArea: focus.focusArea });
        expect(editorRenders.mock.calls.length).toBe(before);
        rerender({ focusArea: 'nav' });
        expect(editorRenders.mock.calls.length).toBeGreaterThan(before);
    });

    it('an unselected card ignores focus-area changes too', async () => {
        const { rerender } = mount({ isSelected: false });
        const before = editorRenders.mock.calls.length;
        rerender({ isSelected: false, focusArea: 'nav' });
        expect(editorRenders.mock.calls.length).toBe(before);
    });

    it('a shifted real-stack index refreshes the index-bound callbacks', async () => {
        const fnOld = vi.fn();
        const fnNew = vi.fn();
        const { rerender } = mount({ isSelected: false, cellIndex: 1, onRemoveCell: fnOld });
        rerender({ isSelected: false, cellIndex: 2, onRemoveCell: fnNew });
        fireEvent.click(removeButton());
        expect(fnNew).toHaveBeenCalledTimes(1);
        expect(fnOld).not.toHaveBeenCalled();
    });
});

describe('StackItem draws no depth layers behind the front card (C18)', () => {
    beforeEach(() => { cleanup(); localStorage.clear(); resetStore(); });

    it('the card is the only sheet: the stack head\'s pips show its depth (C18)', async () => {
        // A three-cell stack: the old card drew two peek layers behind it.
        mount({ cellCount: 3 } as unknown as Partial<Props>);
        const root = card().parentElement as HTMLElement;
        expect(root.children).toHaveLength(1);
        expect(document.querySelector('[style*="top: -10px"]')).toBeNull();
    });

    it('the peek layers are gone from the tree (C18)', async () => {
        expect(Object.keys(import.meta.glob('./PeekLayers.tsx'))).toEqual([]);
        expect(stackItemSource).not.toMatch(/PeekLayers/);
    });
});

describe('StackItem — zoom-out renders the overview card', () => {
    beforeEach(() => { cleanup(); localStorage.clear(); resetStore(); });

    it('a CAS-backed pinned file gets no text preview; a text one shows its content (O01)', async () => {
        const blob: VirtualFile = { ...vf('clip.dat'), body_hash: 'abc', encoding: 'binary' };
        mount({ viewMode: 'overview', cell: cell({ files: [blob], pinned: 'clip.dat' }) });
        expect(document.querySelector('pre')).toBeNull();
        cleanup();
        const notes: VirtualFile = { ...vf('notes.txt'), body_hash: fixtureBodyHash('hello') };
        mount({ viewMode: 'overview', cell: cell({ files: [notes], pinned: 'notes.txt' }) });
        expect((await screen.findByText('hello')).tagName).toBe('PRE');
    });

    it('mounts no editor, and its card is the overview card in the column flow (O01)', async () => {
        editorRenders.mockClear();
        mount({ viewMode: 'overview' });
        expect(editorRenders).not.toHaveBeenCalled();
        const article = document.querySelector('[data-ov-card="cell-1"]') as HTMLElement;
        expect(article.tagName).toBe('ARTICLE');
        expect(article.style.height).toBe('273px');
        expect(article.closest('[style*="position: absolute"]')).toBeNull();
    });
});

describe('StackItem — the card on the skin tokens (S04)', () => {
    beforeEach(() => { cleanup(); localStorage.clear(); resetStore(); });

    it('the focused cell wears the brass edge on a sheet, with the shared shadow (S04)', async () => {
        mount();
        const el = card();
        expect(el.className.split(/\s+/)).toEqual(expect.arrayContaining(['border-brass', 'shadow-(--shadow)', 'rounded-lg']));
        expect(el.style.backgroundColor).toBe('var(--sheet)');
        expect(document.body.innerHTML).not.toMatch(RETIRED_TOKENS);
    });

    it('a Capabilities cell marks itself with the teal-sheet head, never an amber or teal edge (S04)', async () => {
        mount({ capabilitiesCell: true, isSelected: false });
        expect(document.querySelector('.bg-teal-sheet')).not.toBeNull();
        expect(card().className.split(/\s+/)).toContain('border-line');
        expect(document.body.innerHTML).not.toMatch(RETIRED_TOKENS);
    });
});
