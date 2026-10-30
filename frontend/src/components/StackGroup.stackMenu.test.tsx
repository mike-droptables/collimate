import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act, render, screen, cleanup, fireEvent, within, waitFor } from '@testing-library/react';
import { StackGroup, type StackCallbacks } from './StackGroup';
import { FocusAreaContext, type FocusContextType } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell, Stack, VirtualFile } from '@/lib/types';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

// The stack head and its stack menu are what's under test; the cells below
// drag in renderers, iframes and the whole cell editor. Stub them out, and
// keep the last cell's and the manage card's props: their Hide tools are
// wired from here.
const { cellProps, manageProps } = vi.hoisted(() => ({
    cellProps: { current: null as null | { capabilitiesCell?: boolean; onHideStack?: () => void; onDuplicateToNewStack?: () => void } },
    manageProps: { current: null as null | { stackId: string; isFront: boolean; isSelected: boolean; onSelect: () => void; onShowCell: (id: string) => void; onHide: () => void } },
}));
vi.mock('./StackItem', () => ({
    StackItem: (p: typeof cellProps.current) => { cellProps.current = p; return <div data-testid="stack-item" />; },
}));
vi.mock('./capabilities/CapabilitiesManageCard', () => ({
    CapabilitiesManageCard: (p: NonNullable<typeof manageProps.current>) => { manageProps.current = p; return <div data-testid="manage-card" />; },
}));

const file = (name: string): VirtualFile => (fileFixture(name, '', {language: 'plaintext', timestamp: '2026-01-01T00:00:00.000Z'}));
const cell = (id: string, files: VirtualFile[] = [], over: Partial<Cell> = {}): Cell =>
    ({ id, name: id, files, ...over });
const stack = (id: string, cells: Cell[], over: Partial<Stack> = {}): Stack =>
    ({ id, name: '', cells, ...over });

const noopCallbacks = (over: Partial<StackCallbacks> = {}): StackCallbacks => ({
    onSelect: () => {},
    onClose: () => {},
    onRemoveCell: () => {},
    onUpdate: () => {},
    ...over,
});

// Every callback the menu can reach, so each item renders.
const menuCallbacks = (over: Partial<StackCallbacks> = {}): StackCallbacks => noopCallbacks({
    onRenameStack: vi.fn(),
    onFanOut: vi.fn(),
    onDuplicateStack: vi.fn(),
    onMergeStack: vi.fn(),
    ...over,
});

const focus = {
    focusArea: 'main',
    selectedWindowId: null,
    mainFocusMode: 'window',

    setFocusArea: () => {},
    setSelectedWindowId: () => {},
    setMainFocusMode: () => {},

} as unknown as FocusContextType;

interface RenderOpts {
    stackIndex?: number;
    stackCount?: number;
    visibleTopIndex?: number;
    viewMode?: 'stack' | 'overview';
}

const ui = (s: Stack, callbacks: StackCallbacks, o: RenderOpts = {}) => (
    <FocusAreaContext.Provider value={focus.focusArea}>
        <StackGroup
            stack={s}
            width={800}
            height={400}
            isSelected
            viewMode={o.viewMode ?? 'stack'}
            callbacks={callbacks}
            stackIndex={o.stackIndex ?? 0}
            stackCount={o.stackCount ?? 1}
            visibleTopIndex={o.visibleTopIndex ?? 0}
        />
    </FocusAreaContext.Provider>
);
const renderStrip = (s: Stack, callbacks: StackCallbacks = noopCallbacks(), o: RenderOpts = {}) =>
    render(ui(s, callbacks, o));

const btn = (label: string) => screen.getByRole('button', { name: label }) as HTMLButtonElement;
const MENU_BUTTON = 'Stack menu';
const queryMenuButton = () => screen.queryByRole('button', { name: MENU_BUTTON });

const openMenu = () => {
    fireEvent.click(btn(MENU_BUTTON));
    return screen.getByRole('menu');
};
const item = (label: string) =>
    within(screen.getByRole('menu')).getByRole('menuitem', { name: new RegExp(`^${label}`) });
const isInert = (el: HTMLElement) => el.getAttribute('aria-disabled') === 'true';

const RUNNING = 'A run is live in this stack — stop it first';
const USE_CELL = "Single cell — use the cell's own Duplicate / remove controls";

// A cell whose effective files include an options declaration.
const optionsCell = (id = 'c') => cell(id, [file('.options_stack'), file('cell0/main.py'), file('cell1/notes.md')]);
const plain = (id: string) => cell(id, [file('main.py')]);
const card = (id: string) => cell(id, [], { builtin: true });

const resetStore = () => useCollimateStore.setState({
    runs: {}, stoppingRunIds: new Set(), runPhases: {},
} as never);

// A synthetic live run on cell `cellId`.
const liveRunOn = (cellId: string, phase?: string) => useCollimateStore.setState({
    runs: { 'run-1': { runId: 'run-1', cellId, channel: 'ch', runnerId: 'r', runnerName: 'R', generatorName: 'g', label: 'r-11111111', folder: '_runs/r-11111111' } }, stoppingRunIds: new Set(),
    runPhases: phase ? { 'run-1': { phase } } : {},
} as never);

describe('StackGroup — stack menu button', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    it('every real stack carries it, single-cell ones included, right of the move-right arrow (C06)', () => {
        renderStrip(stack('s1', [plain('a')]), menuCallbacks());
        const b = btn(MENU_BUTTON);
        expect(b.getAttribute('aria-haspopup')).toBe('menu');
        expect(b.getAttribute('aria-expanded')).toBe('false');
        // DOM order = visual order along the head.
        const right = btn('Move stack right');
        expect(right.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        cleanup();

        renderStrip(stack('s2', [plain('a'), plain('b')]), menuCallbacks());
        expect(queryMenuButton()).not.toBeNull();
    });

    it('fan columns and zoom-out have no head and no menu (C06) (C08)', () => {
        renderStrip(stack('fan-a', [plain('a')], {
            synthetic: true, sourceCellId: 'a', sourceStackId: 'S', fanIndex: 0, fanCount: 2,
        }), menuCallbacks());
        expect(queryMenuButton()).toBeNull();
        expect(document.querySelector('[data-stack-head]')).toBeNull();
        cleanup();

        renderStrip(stack('s1', [plain('a'), plain('b')]), menuCallbacks(), { viewMode: 'overview' });
        expect(queryMenuButton()).toBeNull();
        expect(document.querySelector('[data-stack-head]')).toBeNull();
    });

    it('lists Rename, Fan out, Duplicate, Merge, Remove in that order; no Split even on an options cell (C07)', () => {
        renderStrip(stack('s1', [optionsCell('a'), plain('b')]), menuCallbacks());
        const menu = openMenu();
        expect(btn(MENU_BUTTON).getAttribute('aria-expanded')).toBe('true');
        expect(menu.style.minWidth).toBe('236px');
        // Each item's text is its label plus an optional shortcut hint.
        const texts = within(menu).getAllByRole('menuitem').map(el => el.textContent ?? '');
        const order = ['Rename', 'Fan out', 'Duplicate', 'Merge', 'Remove'];
        expect(texts).toHaveLength(order.length);
        order.forEach((label, i) => expect(texts[i].startsWith(label)).toBe(true));
        // Remove sits under a separator.
        expect(within(menu).getByRole('separator')).toBeTruthy();
    });

    it('Capabilities offers Manage, Add from catalog…, Create a capability…, then Hide under a separator (C07)', () => {
        renderStrip(stack('caps', [card('d'), plain('b')], { name: 'Capabilities', stack_type: 'capabilities' }), menuCallbacks());
        const menu = openMenu();
        const names = within(menu).getAllByRole('menuitem').map(el => el.textContent ?? '');
        expect(names).toHaveLength(4);
        expect(names[0]).toBe('Manage');
        expect(names[1]).toBe('Add from catalog…');
        expect(names[2]).toBe('Create a capability…');
        expect(names[3]).toMatch(/^Hide/);
        expect(within(menu).getByRole('separator')).toBeTruthy();
        expect(item('Hide').title).toBe('Hide Capabilities to the top bar');
        // Hide is not destructive: no red row.
        expect(item('Hide').className).not.toContain('hover:text-red');
    });

    it('Hide plays the stack burn and hands the stack to onClose (the hide); no run holds it back (C07)', async () => {
        liveRunOn('b');
        const onClose = vi.fn().mockResolvedValue(true);
        renderStrip(stack('caps', [card('d'), plain('b')], { stack_type: 'capabilities' }), menuCallbacks({ onClose }));
        openMenu();
        expect(isInert(item('Hide'))).toBe(false);
        fireEvent.click(item('Hide'));
        expect(screen.queryByRole('menu')).toBeNull();
        await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1), { timeout: 3000 });
    });

    it('Manage fronts the manage card; Add from catalog… and Create a capability… raise manage requests', () => {
        useCollimateStore.setState({ capabilitiesManageRequest: null } as never);
        const onShowManage = vi.fn();
        const onSelect = vi.fn();
        renderStrip(stack('caps', [card('d')], { stack_type: 'capabilities' }), menuCallbacks({ onShowManage, onSelect }));
        openMenu();
        fireEvent.click(item('Manage'));
        expect(onSelect).toHaveBeenCalledTimes(1);
        expect(onShowManage).toHaveBeenCalledTimes(1);
        openMenu();
        fireEvent.click(item('Add from catalog…'));
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({ addFromCatalog: true });
        openMenu();
        fireEvent.click(item('Create a capability…'));
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({ create: true });
        expect(useCollimateStore.getState().capabilitiesManageRequest?.addFromCatalog).toBeUndefined();
    });
});

describe('StackGroup — Hide in Capabilities\' cell heads and manage card', () => {
    beforeEach(() => { cleanup(); resetStore(); cellProps.current = null; manageProps.current = null; });

    it('a Capabilities stack hands its cells Hide and no Duplicate; the head keeps → and … at their ends', () => {
        renderStrip(stack('caps', [card('d'), plain('b')], { name: 'Capabilities', stack_type: 'capabilities' }),
            menuCallbacks({ onSetTopSlot: vi.fn(), onDuplicateToNewStack: vi.fn() }), { visibleTopIndex: 2 });
        expect(cellProps.current!.capabilitiesCell).toBe(true);
        expect(cellProps.current!.onHideStack).toBeTypeOf('function');
        expect(cellProps.current!.onDuplicateToNewStack).toBeUndefined();
        expect(btn(MENU_BUTTON).className).toContain('right-0.5');
        expect(btn('Move stack right').className).toContain('right-6');
    });

    it('a cell\'s Hide plays the stack burn and hands the stack to onClose, without opening the menu or recentring', async () => {
        const onClose = vi.fn().mockResolvedValue(true);
        const onCenterStack = vi.fn();
        renderStrip(stack('caps', [card('d'), plain('a')], { name: 'Capabilities', stack_type: 'capabilities' }),
            menuCallbacks({ onClose, onCenterStack }), { visibleTopIndex: 1 });
        act(() => { cellProps.current!.onHideStack!(); });
        expect(screen.queryByRole('menu')).toBeNull();
        expect((document.querySelector('[data-stack-head]') as HTMLElement).style.pointerEvents).toBe('none');
        await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1), { timeout: 3000 });
        expect(onCenterStack).not.toHaveBeenCalled();
    });

    it('a standard stack\'s cells carry no Hide', () => {
        renderStrip(stack('s1', [plain('a'), plain('b')], { name: 'Drafts' }), menuCallbacks({ onSetTopSlot: vi.fn() }));
        expect(cellProps.current!.capabilitiesCell).toBe(false);
        expect(cellProps.current!.onHideStack).toBeUndefined();
    });

    it('slot 0 is the manage card: front and selected, its Show cell names a REAL index, its Hide hides the stack', async () => {
        const onShowCell = vi.fn();
        const onSelect = vi.fn();
        const onClose = vi.fn().mockResolvedValue(true);
        const caps = stack('caps', [card('d'), plain('a')], { stack_type: 'capabilities' });
        // Show cell reads the stack from the store when it runs (a cell just
        // added is there before the render's cells catch up).
        useCollimateStore.setState({ activeChannel: { id: 'ch', name: 'ch', main_stacks: [caps], nav_stacks: [], nav_selection_id: null, main_selection_id: 'caps' } } as never);
        renderStrip(caps, menuCallbacks({ onShowCell, onSelect, onClose }), { visibleTopIndex: 0 });
        expect(screen.getByTestId('manage-card')).toBeTruthy();
        expect(screen.queryByTestId('stack-item')).toBeNull();
        expect(manageProps.current).toMatchObject({ stackId: 'caps', isFront: true, isSelected: true });
        act(() => manageProps.current!.onShowCell('a'));
        expect(onShowCell).toHaveBeenCalledExactlyOnceWith(1);
        act(() => manageProps.current!.onShowCell('missing'));
        expect(onShowCell).toHaveBeenCalledTimes(1);
        useCollimateStore.setState({ activeChannel: null } as never);
        act(() => manageProps.current!.onSelect());
        expect(onSelect).toHaveBeenCalledWith();
        act(() => manageProps.current!.onHide());
        await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1), { timeout: 3000 });
    });

    it('a cell at slot k+1 is the front face, with the manage card gone from the stack view', () => {
        renderStrip(stack('caps', [card('d'), plain('a')], { stack_type: 'capabilities' }), menuCallbacks(), { visibleTopIndex: 2 });
        expect(screen.queryByTestId('manage-card')).toBeNull();
        expect(screen.getAllByTestId('stack-item')).toHaveLength(1);
    });

    it('the overview never shows the manage card', () => {
        renderStrip(stack('caps', [card('d'), plain('a')], { name: 'Capabilities', stack_type: 'capabilities' }),
            menuCallbacks(), { viewMode: 'overview', visibleTopIndex: 0 });
        expect(screen.queryByTestId('manage-card')).toBeNull();
        expect(screen.getAllByTestId('stack-item')).toHaveLength(2);
    });

    it('the manage card is never a drag source: dragging is only ever wired to cells', () => {
        const onCellDragStart = vi.fn();
        renderStrip(stack('caps', [card('d'), plain('a')], { stack_type: 'capabilities' }),
            menuCallbacks({ onCellDragStart }), { visibleTopIndex: 0 });
        expect(Object.keys(manageProps.current!)).not.toContain('onCellDragStart');
        expect(manageProps.current).not.toHaveProperty('onDragStart');
    });
});

describe('StackGroup — stack menu items', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    it('F0589 keeps menu order and single-cell reasons ahead of live-run reasons (C07)', () => {
        liveRunOn('c', 'awaiting_input');
        renderStrip(stack('s1', [optionsCell()]), menuCallbacks());
        const menu = openMenu();
        expect(within(menu).getAllByRole('menuitem').map(el => el.textContent))
            .toEqual([expect.stringMatching(/^Rename/), expect.stringMatching(/^Fan out/),
                expect.stringMatching(/^Duplicate/), expect.stringMatching(/^Merge/),
                expect.stringMatching(/^Remove/)]);
        expect(item('Merge').title).toBe('Merge needs a stack with 2+ cells');
        expect(item('Remove').title).toBe(USE_CELL);
    });

    it('F0589 rechecks a newly live run before a previously enabled Remove can burn its stack (C06)', () => {
        renderStrip(stack('s1', [plain('a'), plain('b')]), menuCallbacks());
        openMenu();
        const remove = item('Remove');
        expect(isInert(remove)).toBe(false);
        act(() => {
            liveRunOn('a');
            fireEvent.click(remove);
        });
        expect(btn(MENU_BUTTON).parentElement?.style.pointerEvents).toBe('auto');
    });

    it('multi-cell stack: every item is live and fires its callback', () => {
        const cbs = menuCallbacks();
        renderStrip(stack('s1', [plain('a'), plain('b')]), cbs);

        openMenu();
        fireEvent.click(item('Fan out'));
        expect(cbs.onFanOut).toHaveBeenCalledTimes(1);
        // Activation closes the menu.
        expect(screen.queryByRole('menu')).toBeNull();

        openMenu();
        fireEvent.click(item('Duplicate'));
        expect(cbs.onDuplicateStack).toHaveBeenCalledTimes(1);

        openMenu();
        expect(item('Merge').title).toBe('Merge — combine this stack into a single cell');
        fireEvent.click(item('Merge'));
        expect(cbs.onMergeStack).toHaveBeenCalledTimes(1);

        openMenu();
        expect(isInert(item('Remove'))).toBe(false);
    });

    it('single-cell stack: Fan out, Duplicate, Merge and Remove are inert with their reasons', () => {
        const cbs = menuCallbacks();
        renderStrip(stack('s1', [plain('a')]), cbs);
        openMenu();
        const cases: Array<[string, string]> = [
            ['Fan out', 'Fan out needs at least 2 cells'],
            ['Duplicate', USE_CELL],
            ['Merge', 'Merge needs a stack with 2+ cells'],
            ['Remove', USE_CELL],
        ];
        for (const [label, reason] of cases) {
            const el = item(label);
            expect(isInert(el)).toBe(true);
            expect(el.title).toBe(reason);
            // Inert, not disabled: it stays focusable so the reason is reachable.
            expect(el).not.toBeDisabled();
            fireEvent.click(el);
        }
        expect(cbs.onFanOut).not.toHaveBeenCalled();
        expect(cbs.onDuplicateStack).not.toHaveBeenCalled();
        expect(cbs.onMergeStack).not.toHaveBeenCalled();
        // An inert click keeps the menu open.
        expect(screen.getByRole('menu')).toBeTruthy();
        expect(isInert(item('Rename'))).toBe(false);
    });

    it('a live run in the stack greys Merge and Remove — Duplicate stays live (C07)', () => {
        liveRunOn('b');
        renderStrip(stack('s1', [optionsCell(), plain('b')]), menuCallbacks());
        openMenu();
        for (const label of ['Merge', 'Remove']) {
            expect(isInert(item(label))).toBe(true);
            expect(item(label).title).toBe(RUNNING);
        }
        // The copy leaves the runs' files behind.
        expect(isInert(item('Duplicate'))).toBe(false);
        expect(isInert(item('Fan out'))).toBe(false);
        expect(isInert(item('Rename'))).toBe(false);
    });

    it('a run idling in awaiting_input greys them too — every live run holds its files', () => {
        liveRunOn('b', 'awaiting_input');
        renderStrip(stack('s1', [optionsCell(), plain('b')]), menuCallbacks());
        openMenu();
        for (const label of ['Merge', 'Remove']) {
            expect(isInert(item(label))).toBe(true);
        }
    });

    it('Remove plays the burn with NO confirmation dialog', () => {
        const openConfirm = vi.fn().mockResolvedValue(false);
        useCollimateStore.setState({ openConfirm } as never);
        const onClose = vi.fn();
        renderStrip(stack('s1', [plain('a'), plain('b')], { name: 'My stack' }), menuCallbacks({ onClose }));
        openMenu();
        fireEvent.click(item('Remove'));
        expect(openConfirm).not.toHaveBeenCalled();
        expect(screen.queryByRole('menu')).toBeNull();
    });

    it('a refused or failed Remove undoes the burn: the head is clickable again (C06)', async () => {
        const onClose = vi.fn().mockResolvedValue(false);
        renderStrip(stack('s1', [plain('a'), plain('b')]), menuCallbacks({ onClose }));
        const strip = () => btn(MENU_BUTTON).parentElement as HTMLElement;
        openMenu();
        fireEvent.click(item('Remove'));
        expect(strip().style.pointerEvents).toBe('none');
        await waitFor(() => expect(onClose).toHaveBeenCalled(), { timeout: 3000 });
        await waitFor(() => expect(strip().style.pointerEvents).toBe('auto'));
    });
});

describe('StackGroup — stack menu keyboard + dismissal', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    it('focuses the first item, arrows / Home / End move, Enter activates', () => {
        const cbs = menuCallbacks();
        renderStrip(stack('s1', [plain('a'), plain('b')]), cbs);
        const menu = openMenu();
        expect(document.activeElement).toBe(item('Rename'));
        fireEvent.keyDown(menu, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(item('Fan out'));
        fireEvent.keyDown(menu, { key: 'End' });
        expect(document.activeElement).toBe(item('Remove'));
        fireEvent.keyDown(menu, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(item('Rename'));
        fireEvent.keyDown(menu, { key: 'ArrowUp' });
        expect(document.activeElement).toBe(item('Remove'));
        fireEvent.keyDown(menu, { key: 'Home' });
        fireEvent.keyDown(menu, { key: 'ArrowDown' });
        fireEvent.keyDown(menu, { key: 'ArrowDown' });
        expect(document.activeElement).toBe(item('Duplicate'));
        fireEvent.keyDown(menu, { key: 'Enter' });
        expect(cbs.onDuplicateStack).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole('menu')).toBeNull();
        // Activation hands focus back to the button, not to <body>.
        expect(document.activeElement).toBe(btn(MENU_BUTTON));
    });

    it('Tab closes and puts focus on the button, so the Tab moves on from the head', () => {
        renderStrip(stack('s1', [plain('a'), plain('b')]), menuCallbacks());
        const menu = openMenu();
        fireEvent.keyDown(menu, { key: 'Tab' });
        expect(screen.queryByRole('menu')).toBeNull();
        expect(document.activeElement).toBe(btn(MENU_BUTTON));
    });

    it('Esc closes and hands focus back to the button, without reaching window listeners', () => {
        renderStrip(stack('s1', [plain('a'), plain('b')]), menuCallbacks());
        const menu = openMenu();
        // The scene's Esc cascade listens on window in the capture phase and
        // skips keys from inside the menu; bubble-phase listeners never see
        // it at all.
        const bubble = vi.fn();
        window.addEventListener('keydown', bubble);
        fireEvent.keyDown(menu, { key: 'Escape' });
        window.removeEventListener('keydown', bubble);
        expect(bubble).not.toHaveBeenCalled();
        expect(menu.hasAttribute('data-owns-keyboard')).toBe(true);
        expect(screen.queryByRole('menu')).toBeNull();
        expect(document.activeElement).toBe(btn(MENU_BUTTON));
    });

    it('closes on an outside pointerdown and on window blur; the button toggles it', () => {
        renderStrip(stack('s1', [plain('a'), plain('b')]), menuCallbacks());
        openMenu();
        fireEvent.pointerDown(document.body);
        expect(screen.queryByRole('menu')).toBeNull();

        openMenu();
        fireEvent.blur(window);
        expect(screen.queryByRole('menu')).toBeNull();

        openMenu();
        fireEvent.pointerDown(btn(MENU_BUTTON));
        fireEvent.click(btn(MENU_BUTTON));
        expect(screen.queryByRole('menu')).toBeNull();
    });

    it('zooming out drops an open menu, and it stays closed on the way back', () => {
        const cbs = menuCallbacks();
        const s = stack('s1', [plain('a'), plain('b')]);
        const { rerender } = renderStrip(s, cbs);
        openMenu();
        rerender(ui(s, cbs, { viewMode: 'overview' }));
        expect(screen.queryByRole('menu')).toBeNull();
        rerender(ui(s, cbs));
        expect(screen.queryByRole('menu')).toBeNull();
    });
});

describe('StackGroup — rename + the name on the head (C06)', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    const nameInput = () => screen.getByLabelText('Stack name') as HTMLInputElement;
    const startRename = () => { openMenu(); fireEvent.click(item('Rename')); };

    it('Rename puts an input in the name\'s place; Enter saves the trimmed name (C06)', () => {
        const onRenameStack = vi.fn();
        renderStrip(stack('s1', [plain('a')], { name: 'Old' }), menuCallbacks({ onRenameStack }));
        startRename();
        const input = nameInput();
        expect(input.value).toBe('Old');
        expect(document.activeElement).toBe(input);
        expect(input.hasAttribute('data-owns-keyboard')).toBe(true);
        fireEvent.change(input, { target: { value: '  New name ' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(onRenameStack).toHaveBeenCalledTimes(1);
        expect(onRenameStack).toHaveBeenCalledWith('New name');
        expect(screen.queryByLabelText('Stack name')).toBeNull();
    });

    it('Esc cancels, blur saves, empty clears the name', () => {
        const onRenameStack = vi.fn();
        renderStrip(stack('s1', [plain('a')], { name: 'Old' }), menuCallbacks({ onRenameStack }));
        startRename();
        fireEvent.change(nameInput(), { target: { value: 'Nope' } });
        fireEvent.keyDown(nameInput(), { key: 'Escape' });
        expect(onRenameStack).not.toHaveBeenCalled();
        expect(screen.queryByLabelText('Stack name')).toBeNull();

        startRename();
        fireEvent.change(nameInput(), { target: { value: 'Blurred' } });
        fireEvent.blur(nameInput());
        expect(onRenameStack).toHaveBeenLastCalledWith('Blurred');

        startRename();
        fireEvent.change(nameInput(), { target: { value: '   ' } });
        fireEvent.keyDown(nameInput(), { key: 'Enter' });
        expect(onRenameStack).toHaveBeenLastCalledWith('');
    });

    it('an unchanged name saves nothing', () => {
        const onRenameStack = vi.fn();
        renderStrip(stack('s1', [plain('a')], { name: 'Same' }), menuCallbacks({ onRenameStack }));
        startRename();
        fireEvent.keyDown(nameInput(), { key: 'Enter' });
        expect(onRenameStack).not.toHaveBeenCalled();
    });

    it('the name sits on the head\'s top line and gives way to the pips on hover (C06)', () => {
        const { container } = renderStrip(stack('s1', [plain('a'), plain('b')], { name: 'Research' }), menuCallbacks({ onSetTopSlot: vi.fn() }));
        const label = container.querySelector('[data-stack-name]') as HTMLElement;
        expect(label).not.toBeNull();
        // The name alone: the count sits after the last pip (polish).
        expect(label.textContent).toBe('Research');
        expect(label.querySelector('[data-stack-count]')).toBeNull();
        expect(label.className).toContain('pointer-events-none');
        const head = container.querySelector('[data-stack-head]') as HTMLElement;
        // A colour fade to transparent, never opacity.
        fireEvent.pointerEnter(head);
        expect(label.className.split(/\s+/)).toContain('text-transparent');
        fireEvent.pointerLeave(head);
        expect(label.className.split(/\s+/)).not.toContain('text-transparent');
        expect(label.style.opacity).toBe('');
    });

    it('the rename input hides the name and the pips while it is open, and its clicks stay its own (C06)', () => {
        const onCenterStack = vi.fn();
        const { container } = renderStrip(stack('s1', [plain('a'), plain('b')], { name: 'Old' }), menuCallbacks({ onSetTopSlot: vi.fn(), onCenterStack }));
        startRename();
        expect(container.querySelector('[data-stack-name]')).toBeNull();
        expect(container.querySelector('[data-stack-scrubber]')).toBeNull();
        fireEvent.pointerDown(nameInput());
        fireEvent.click(nameInput());
        expect(onCenterStack).not.toHaveBeenCalled();
        expect(nameInput().className).toContain('focus:border-teal');
    });

    it('no name, no label (C06)', () => {
        const { container } = renderStrip(stack('s1', [plain('a')]), menuCallbacks());
        expect(container.querySelector('[data-stack-name]')).toBeNull();
    });
});

describe('StackGroup — move arrows', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    it('the arrows move the stack and stop at the row\'s ends (C06)', () => {
        const onMoveStack = vi.fn();
        renderStrip(stack('s1', [cell('a')]), noopCallbacks({ onMoveStack }), { stackIndex: 0, stackCount: 3 });
        expect(btn('Move stack left').disabled).toBe(true);
        // Dimmed is ink at 25% alpha, a colour (never an opacity fade).
        expect(btn('Move stack left').className).toContain('text-ink-3/25');
        expect(btn('Move stack left').className).not.toMatch(/opacity/);
        expect(btn('Move stack right').disabled).toBe(false);
        fireEvent.click(btn('Move stack right'));
        expect(onMoveStack).toHaveBeenCalledExactlyOnceWith(1);
        cleanup();
        renderStrip(stack('s1', [cell('a')]), noopCallbacks({ onMoveStack }), { stackIndex: 2, stackCount: 3 });
        expect(btn('Move stack right').disabled).toBe(true);
    });
});


describe('StackGroup strip and menu — skin tokens (S04)', () => {
    beforeEach(() => { cleanup(); resetStore(); });
    const OLD = RETIRED_TOKENS;

    it('the menu is the shared surface: menu rows, a red Remove row, the hairline separator (S04)', () => {
        renderStrip(stack('s1', [plain('a'), plain('b')]), menuCallbacks());
        const menu = openMenu();
        expect(menu.className.split(/\s+/)).toEqual(expect.arrayContaining(['bg-sheet', 'border-line-2', 'shadow-(--shadow)']));
        expect(item('Duplicate').className).toContain('hover:bg-sheet-2');
        expect(item('Remove').className).toContain('hover:text-red');
        expect(within(menu).getByRole('separator').className).toContain('bg-line');
        expect(menu.outerHTML).not.toMatch(OLD);
    });

    it('an inert row keeps its pointer so its title can explain itself (S04)', () => {
        renderStrip(stack('s1', [plain('a')]), menuCallbacks());
        openMenu();
        const fan = item('Fan out');
        expect(isInert(fan)).toBe(true);
        expect(fan.className).toContain('aria-disabled:pointer-events-auto');
    });

    it('the selected stack names itself in brass, plain text on the head; the head draws no retired token (S04) (C06)', () => {
        const { container } = renderStrip(stack('s1', [plain('a'), plain('b')], { name: 'Research' }), menuCallbacks({ onSetTopSlot: vi.fn() }));
        const name = container.querySelector('[data-stack-head] [data-stack-name]') as HTMLElement;
        expect(name.className.split(/\s+/)).toEqual(expect.arrayContaining(['text-brass', 'font-medium']));
        expect(name.className).not.toContain('border');
        expect(container.innerHTML).not.toMatch(OLD);
    });
});

describe('StackGroup — the overview column head\'s name (polish)', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    const overview = (s: Stack, over: Partial<StackCallbacks> = {}) =>
        render(
            <FocusAreaContext.Provider value={focus.focusArea}>
                <StackGroup
                    stack={s}
                    width={400}
                    height={300}
                    isSelected={false}
                    viewMode="overview"
                    callbacks={menuCallbacks(over)}
                    stackIndex={0}
                    stackCount={1}
                    visibleTopIndex={0}
                />
            </FocusAreaContext.Provider>,
        );
    const headName = () => document.querySelector('[data-overview-head] [data-stack-name]') as HTMLElement;
    const nameInput = () => screen.getByLabelText('Stack name') as HTMLInputElement;

    it('an empty name keeps a 96px clickable area on the head\'s 18px line, with a quiet "Name…" placeholder, never bold (polish)', () => {
        overview(stack('s1', [plain('a')]));
        const name = headName();
        expect(name.tagName).toBe('BUTTON');
        const cls = name.className.split(/\s+/);
        expect(cls).toEqual(expect.arrayContaining(['min-w-[6rem]', 'h-[1.125rem]', 'leading-[1.125rem]', 'font-medium', 'cursor-text']));
        expect(name.className).not.toMatch(/font-(semibold|bold)/);
        const placeholder = name.querySelector('[data-name-placeholder]') as HTMLElement;
        expect(placeholder.textContent).toBe('Name…');
        expect(placeholder.className).toContain('text-ink-3');
        // A named column shows its name, no placeholder.
        cleanup();
        overview(stack('s1', [plain('a')], { name: 'Drafts' }));
        expect(headName().textContent).toBe('Drafts');
        expect(headName().querySelector('[data-name-placeholder]')).toBeNull();
    });

    it('a single click opens the rename input in the name\'s place, focused, its placeholder gone; Enter renames through the callback (polish)', () => {
        const onRenameStack = vi.fn();
        overview(stack('s1', [plain('a')]), { onRenameStack });
        fireEvent.click(headName());
        const input = nameInput();
        expect(document.activeElement).toBe(input);
        expect(input.closest('[data-overview-head]')).not.toBeNull();
        expect(input.className.split(/\s+/)).toEqual(expect.arrayContaining(['h-[1.125rem]', 'min-w-[6rem]', 'placeholder:text-transparent']));
        expect(document.querySelector('[data-name-placeholder]')).toBeNull();
        fireEvent.change(input, { target: { value: '  Results ' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(onRenameStack).toHaveBeenCalledWith('Results');
        expect(screen.queryByLabelText('Stack name')).toBeNull();
    });

    it('Esc leaves the name as it was; blur commits (polish)', () => {
        const onRenameStack = vi.fn();
        overview(stack('s1', [plain('a')], { name: 'Drafts' }), { onRenameStack });
        fireEvent.click(headName());
        fireEvent.change(nameInput(), { target: { value: 'Other' } });
        fireEvent.keyDown(nameInput(), { key: 'Escape' });
        expect(onRenameStack).not.toHaveBeenCalled();
        expect(headName().textContent).toBe('Drafts');

        fireEvent.click(headName());
        fireEvent.change(nameInput(), { target: { value: 'Blurred' } });
        fireEvent.blur(nameInput());
        expect(onRenameStack).toHaveBeenLastCalledWith('Blurred');
    });

    it('the name leaves the grip\'s press and the column\'s own presses alone (polish)', () => {
        const onStackDragStart = vi.fn();
        const onColumnDown = vi.fn();
        const { container } = overview(stack('s1', [plain('a')]), { onStackDragStart });
        container.addEventListener('pointerdown', onColumnDown);
        fireEvent.pointerDown(document.querySelector('[data-overview-grip]')!);
        expect(onStackDragStart).toHaveBeenCalledTimes(1);
        onColumnDown.mockClear();
        // A press on the name bubbles on (no stopPropagation, no drag of its own).
        fireEvent.pointerDown(headName());
        expect(onColumnDown).toHaveBeenCalledTimes(1);
        expect(onStackDragStart).toHaveBeenCalledTimes(1);
    });

    it('the Capabilities column\'s name is not a rename field: a click opens no input (polish)', () => {
        const onRenameStack = vi.fn();
        overview(stack('caps', [plain('a')], { name: 'Capabilities', stack_type: 'capabilities' }), { onRenameStack });
        const name = headName();
        expect(name.tagName).toBe('SPAN');
        expect(name.textContent).toBe('Capabilities');
        expect(name.className).toContain('text-teal');
        fireEvent.click(name);
        expect(screen.queryByLabelText('Stack name')).toBeNull();
        expect(onRenameStack).not.toHaveBeenCalled();
    });

    it('a rename open on the stack view\'s head does not carry into the overview head (polish)', () => {
        const cbs = menuCallbacks();
        const s = stack('s1', [plain('a'), plain('b')], { name: 'Drafts' });
        const { rerender } = renderStrip(s, cbs);
        openMenu();
        fireEvent.click(item('Rename'));
        expect(screen.queryByLabelText('Stack name')).not.toBeNull();
        rerender(ui(s, cbs, { viewMode: 'overview' }));
        expect(screen.queryByLabelText('Stack name')).toBeNull();
        expect(headName().textContent).toBe('Drafts');
    });
});
