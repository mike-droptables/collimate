import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { executeSceneSizing, executeSceneActions, executeSceneBack, executeSceneModes, executeSceneMovement, RELEASE_DOUBLE_TAP_MS } from './sceneKeyboardEffects';
import { SHORTCUT_IDS as IDs } from '@/lib/keybindings/ids';
import { useSpatialStore, DEFAULT_MIN_CELL_SIZE } from '@/stores/useSpatialStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useKeybindingsStore } from '@/lib/keybindings/store';
import type { Stack } from '@/lib/types';
import { MIN_STACK_WIDTH, maxStackWidthFor } from './useStackLayout';
import { resolveCellSource } from '@/lib/fanOut';
import { createDefaultLayout } from '@/lib/cellLayout';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import * as stackWidth from '@/lib/stackWidth';
import { putChannelSettings, type ChannelSettings } from '@/lib/api';
import { noteGuestFocus } from '@/lib/selectionFocus';

// No network: the settings load never settles (the tests seed the doc),
// and saves resolve at once.
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchChannelSettings: vi.fn(() => new Promise(() => {})),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
}));

const initialStore = useCollimateStore.getState();
const fanRange = { stackId: 's1', name: '', realIdx: 0, start: 0, end: 1, count: 2 };
type Context = Parameters<typeof executeSceneSizing>[2];
const stacks: Stack[] = [
    { id: 's1', name: '', cells: [{ id: 'a', name: 'A', files: [] }, { id: 'b', name: 'B', files: [] }] },
    { id: 's2', name: '', cells: [{ id: 'c', name: 'C', files: [] }] },
];
const event = (repeat = false) => new KeyboardEvent('keydown', { cancelable: true, repeat });
const makeContext = (changes: Partial<Context> = {}) => ({
    ...useSpatialStore.getState(),
    mainStacks: stacks, displayStacks: stacks, displaySelectedId: 's1', selectedWindowId: 's1',
    activeChannel: { id: '11111111-1111-4111-8111-111111111111' }, isZoomedOut: false, sceneFocusArea: 'main', focusArea: 'main', containerOf: () => null,
    visibleTopIndicesRef: { current: { s1: 0 } }, cellSizeKeyStartRef: { current: null }, moveReleaseKeys: { current: new Set<string>() },
    actions: { newEmptyStackRight: vi.fn(async () => ({ cellId: 'new' })), winDuplicateToNewStack: vi.fn(async () => ({ cellId: 'copy' })),
        duplicateCellInStack: vi.fn(async () => ({ cellId: 'fan-copy' })), duplicateStack: vi.fn(), winRemoveCell: vi.fn(), winRotate: vi.fn() },
    getSelectedFrontCell: () => stacks[0].cells[0], selectedHistoryCellId: () => null, selectedPickSessionCellId: () => null,
    pendingMove: null, cellMoveLeftMatched: false, advancePendingCellMove: vi.fn(),
    pendingMoveSource: (id: string) => resolveCellSource(stacks, null, id, 0, () => false),
    handleRemoveStack: vi.fn(), selectDisplayId: vi.fn(), setFocusArea: vi.fn(),
    setFannedOutStackId: vi.fn(), fannedOutStackId: null, fan: null, toggleZoom: vi.fn(), setIsZoomedOut: vi.fn(),
    ...changes,
}) as unknown as Context;

beforeEach(() => {
    useSpatialStore.setState({ minCellSize: DEFAULT_MIN_CELL_SIZE, stackWidths: {}, maxColumns: {}, allStacksByChannel: {}, isMaxWidth: false, centerRequested: 0 });
    useChannelSettingsStore.setState({ doc: null } as never);
    useCollimateStore.setState({ ...initialStore, runs: {}, stoppingRunIds: new Set(), cellDiffs: {}, cellHistory: {}, maximizedOverlayCellId: null });
    useKeybindingsStore.setState({ overrides: {} });
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

// The active channel with its settings loaded: the max toggle stores the
// width before as its default.
const loadChannel = (defaultStackWidth: number | null = null) => {
    useCollimateStore.setState({ activeChannel: { id: '11111111-1111-4111-8111-111111111111', name: 'chan' } } as never);
    useChannelSettingsStore.setState({
        doc: { version: 1, kind: 'channel-settings', layout: { defaultStackWidth } } as unknown as ChannelSettings,
        loadedFor: '11111111-1111-4111-8111-111111111111',
    });
};
const channelDefault = () => useChannelSettingsStore.getState().doc?.layout?.defaultStackWidth;

it('F0936 width effects accelerate held keys, clamp/reset shared widths, and preserve max toggle restore', () => {
    vi.useFakeTimers(); vi.setSystemTime(1000);
    const ctx = makeContext();
    const first = event(); executeSceneSizing(first, IDs.SCENE_WIDTH_INC, ctx);
    expect(first.defaultPrevented).toBe(true);
    expect(useSpatialStore.getState().minCellSize).toBe(DEFAULT_MIN_CELL_SIZE + 50);
    vi.advanceTimersByTime(600); executeSceneSizing(event(true), IDs.SCENE_WIDTH_INC, ctx);
    expect(useSpatialStore.getState().minCellSize).toBe(DEFAULT_MIN_CELL_SIZE + 150);
    executeSceneSizing(event(true), IDs.SCENE_WIDTH_DEC, ctx);
    expect(useSpatialStore.getState().minCellSize).toBe(DEFAULT_MIN_CELL_SIZE + 50);
    executeSceneSizing(event(), IDs.SCENE_WIDTH_RESET, ctx);
    expect(ctx.cellSizeKeyStartRef.current).toBeNull();
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, ctx);
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: maxStackWidthFor(window.innerWidth), isMaxWidth: true });
    executeSceneSizing(event(), IDs.SCENE_WIDTH_DEC, ctx);
    expect(useSpatialStore.getState()).toMatchObject({ isMaxWidth: false });
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, ctx); executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, ctx);
    expect(useSpatialStore.getState().isMaxWidth).toBe(false);
    useSpatialStore.setState({ minCellSize: MIN_STACK_WIDTH, isMaxWidth: true });
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, ctx);
    expect(useSpatialStore.getState().minCellSize).toBe(Math.min(DEFAULT_MIN_CELL_SIZE, maxStackWidthFor(window.innerWidth)));
    executeSceneSizing(event(), IDs.SCENE_WIDTH_DEC, ctx);
    executeSceneSizing(event(), IDs.SCENE_WIDTH_INC, makeContext({ activeChannel: null }));
});

it('F0936 column sizing targets the display identity without changing shared width or inventing absent selection', () => {
    loadChannel();
    useSpatialStore.setState({ allStacksByChannel: { ['11111111-1111-4111-8111-111111111111']: false }, stackWidths: { 'fan-a': 500 } });
    const ctx = makeContext({ displaySelectedId: 'fan-a' });
    executeSceneSizing(event(), IDs.SCENE_WIDTH_INC, ctx);
    expect(useSpatialStore.getState().stackWidths['fan-a']).toBe(550);
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, ctx);
    expect(useSpatialStore.getState().stackWidths['fan-a']).toBe(maxStackWidthFor(window.innerWidth));
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, ctx);
    // Back to the column's width before maximising, now the channel default (polish).
    expect(useSpatialStore.getState().stackWidths['fan-a']).toBe(550);
    executeSceneSizing(event(), IDs.SCENE_WIDTH_RESET, ctx);
    executeSceneSizing(event(), IDs.SCENE_WIDTH_DEC, makeContext({ displaySelectedId: 'missing' }));
    const before = useSpatialStore.getState().stackWidths;
    executeSceneSizing(event(), IDs.SCENE_WIDTH_INC, makeContext({ displaySelectedId: null }));
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, makeContext({ displaySelectedId: null }));
    expect(useSpatialStore.getState().stackWidths).toBe(before);
    const zoomEvent = event(); executeSceneSizing(zoomEvent, IDs.SCENE_FULLSCREEN, makeContext({ isZoomedOut: true }));
    expect(zoomEvent.defaultPrevented).toBe(false);
});

it('Alt+F is the shared max toggle: maximising stores the width before as the channel default and un-maximising returns to it, shared and per stack (polish)', () => {
    loadChannel(640);
    const max = maxStackWidthFor(window.innerWidth);
    const toggle = vi.spyOn(stackWidth, 'toggleMaxWidth');
    useSpatialStore.setState({ minCellSize: 620 });
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, makeContext());
    expect(toggle).toHaveBeenLastCalledWith('s1', '11111111-1111-4111-8111-111111111111');
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: max, isMaxWidth: true });
    expect(channelDefault()).toBe(620);
    expect(putChannelSettings).toHaveBeenLastCalledWith('11111111-1111-4111-8111-111111111111', { layout: { defaultStackWidth: 620 } });
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, makeContext());
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: 620, isMaxWidth: false });
    useSpatialStore.setState({ allStacksByChannel: { ['11111111-1111-4111-8111-111111111111']: false }, stackWidths: { s1: 710 } });
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, makeContext());
    expect(useSpatialStore.getState().stackWidths.s1).toBe(max);
    expect(useSpatialStore.getState().maxColumns).toEqual({ s1: true });
    expect(channelDefault()).toBe(710);
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, makeContext());
    expect(useSpatialStore.getState().stackWidths.s1).toBe(710);
    expect(useSpatialStore.getState().minCellSize).toBe(620);
});

it('Alt+F on an already-full column stores nothing and goes to the channel default (polish)', () => {
    loadChannel(640);
    vi.mocked(putChannelSettings).mockClear();
    const max = maxStackWidthFor(window.innerWidth);
    useSpatialStore.setState({ minCellSize: max });
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, makeContext());
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: 640, isMaxWidth: false });
    expect(putChannelSettings).not.toHaveBeenCalled();
});

it('Alt+0 (Reset) returns to the channel default, as the Width tool\'s Reset: after a maximise from 420, to 420 (polish)', () => {
    loadChannel(640);
    useSpatialStore.setState({ minCellSize: 800 });
    executeSceneSizing(event(), IDs.SCENE_WIDTH_RESET, makeContext());
    expect(useSpatialStore.getState().minCellSize).toBe(640);
    useSpatialStore.setState({ minCellSize: 420 });
    executeSceneSizing(event(), IDs.SCENE_FULLSCREEN, makeContext());
    executeSceneSizing(event(), IDs.SCENE_WIDTH_RESET, makeContext());
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: 420, isMaxWidth: false });
});

it('F0936 actions preserve real selection, asynchronous runners-panel identity and Capabilities guards (P02)', async () => {
    const open = vi.spyOn(useCollimateStore.getState(), 'openPickSession').mockImplementation(() => {});
    const ctx = makeContext();
    for (const command of [IDs.STACK_NEW_CELL, IDs.STACK_NEW_CELL_RUN, IDs.STACK_DUPLICATE, IDs.STACK_DUPLICATE_RUN, IDs.STACK_DUPLICATE_STACK, IDs.STACK_CENTER, IDs.STACK_ALIGN_LEFT, IDs.STACK_ALIGN_RIGHT, IDs.STACK_REMOVE, IDs.CELL_REMOVE, IDs.STACK_ROTATE_PREV, IDs.STACK_ROTATE_NEXT, IDs.STACK_RUN]) {
        const e = event(); executeSceneActions(e, command, ctx); expect(e.defaultPrevented).toBe(true);
    }
    await Promise.resolve();
    expect(open.mock.calls).toEqual([['new', 'runners'], ['copy', 'runners']]);
    expect(ctx.actions.winRemoveCell).toHaveBeenCalledWith('s1', 0);
    expect(ctx.actions.winRotate).toHaveBeenCalledWith('s1', -1);
    expect(ctx.actions.winRotate).toHaveBeenCalledWith('s1', 1);
    expect(useCollimateStore.getState().pendingGeneratorRunnerStackId).toBe('s1');
    useCollimateStore.getState().clearGeneratorRunnerRequest();
    const capabilities = makeContext({ mainStacks: [{ ...stacks[0], stack_type: 'capabilities' }] });
    executeSceneActions(event(), IDs.STACK_RUN, capabilities);
    expect(useCollimateStore.getState().pendingGeneratorRunnerStackId).toBeNull();
    const fan = makeContext({ fan: fanRange, visibleTopIndicesRef: { current: { s1: -1 } } });
    executeSceneActions(event(), IDs.STACK_DUPLICATE, fan);
    executeSceneActions(event(), IDs.STACK_DUPLICATE_RUN, fan);
    await Promise.resolve();
    expect(fan.actions.duplicateCellInStack).toHaveBeenCalledWith('s1', 1);
    expect(open).toHaveBeenLastCalledWith('fan-copy', 'runners');
    executeSceneActions(event(), IDs.STACK_DUPLICATE_RUN, makeContext({ ...capabilities, fan: fanRange }));
    expect(fan.actions.duplicateCellInStack).toHaveBeenCalledTimes(2);
    executeSceneActions(event(), IDs.STACK_NEW_CELL, makeContext({ selectedWindowId: null }));
    // Alt+N opens the new cell's name field; Alt+Shift+N opens its runners
    // panel instead, which takes the keyboard.
    expect(vi.mocked(ctx.actions.newEmptyStackRight).mock.calls.slice(0, 2)).toEqual([['s1', { editTitle: true }], ['s1']]);
});

it('F0936 failed/no-result creation never opens a picker and absent focus/zoom does not mutate stacks', async () => {
    const open = vi.spyOn(useCollimateStore.getState(), 'openPickSession').mockImplementation(() => {});
    const ctx = makeContext();
    vi.mocked(ctx.actions.newEmptyStackRight).mockResolvedValue(null);
    vi.mocked(ctx.actions.winDuplicateToNewStack).mockResolvedValue(null);
    vi.mocked(ctx.actions.duplicateCellInStack).mockResolvedValue(null);
    executeSceneActions(event(), IDs.STACK_NEW_CELL_RUN, ctx);
    executeSceneActions(event(), IDs.STACK_DUPLICATE_RUN, ctx);
    executeSceneActions(event(), IDs.STACK_DUPLICATE_RUN, { ...ctx, fan: fanRange });
    await Promise.resolve(); expect(open).not.toHaveBeenCalled();
    for (const changes of [{ isZoomedOut: true }, { sceneFocusArea: 'nav' as const }, { selectedWindowId: null }]) {
        const e = event(); executeSceneActions(e, IDs.CELL_REMOVE, makeContext(changes)); expect(e.defaultPrevented).toBe(false);
    }
    // No front cell (the selected stack is gone): nothing to remove.
    const empty = makeContext({ mainStacks: [], getSelectedFrontCell: () => null });
    executeSceneActions(event(), IDs.CELL_REMOVE, empty);
    expect(empty.actions.winRemoveCell).not.toHaveBeenCalled();
});

it('F0936 Escape follows pending move, diff, full window, nested history, Commands pane, picker, fan, zoom, focus order', () => {
    const ctx = makeContext();
    executeSceneBack(event(), IDs.GLOBAL_BACK, { ...ctx, pendingMove: { type: 'cell', sourceId: 's1', originalIndex: 0, targetIndex: 1, mode: 'insert' } });
    expect(useSpatialStore.getState().pendingMove).toBeNull();
    const exit = vi.spyOn(useCollimateStore.getState(), 'exitCellDiff').mockImplementation(() => {});
    useCollimateStore.setState({ cellDiffs: { a: {} } } as never);
    executeSceneBack(event(), IDs.GLOBAL_BACK, ctx); expect(exit).toHaveBeenCalledWith('a');
    useCollimateStore.setState({ cellDiffs: {} });
    const history = vi.spyOn(useCollimateStore.getState(), 'setCellHistory');
    useCollimateStore.getState().setCellHistory('a', { versionId: 'older' });
    // The full-window view collapses before history: no rung is skipped
    // while it is up.
    useCollimateStore.setState({ maximizedOverlayCellId: 'a' });
    executeSceneBack(event(), IDs.GLOBAL_BACK, { ...ctx, selectedHistoryCellId: () => 'a' });
    expect(useCollimateStore.getState().maximizedOverlayCellId).toBeNull();
    expect(history).toHaveBeenCalledTimes(1);
    executeSceneBack(event(), IDs.GLOBAL_BACK, { ...ctx, selectedHistoryCellId: () => 'a' });
    expect(history).toHaveBeenLastCalledWith('a', { versionId: null });
    const layout = vi.spyOn(useCollimateStore.getState(), 'updateCellLayout').mockImplementation((_id, update) => { update(createDefaultLayout({ pinnedPath: null } as never)); });
    executeSceneBack(event(), IDs.GLOBAL_BACK, { ...ctx, selectedHistoryCellId: () => 'a' });
    expect(useCollimateStore.getState().cellHistory.a).toBeUndefined(); expect(layout).toHaveBeenCalled();
    useCollimateStore.getState().openCommandPane();
    const closeBefore = vi.spyOn(useCollimateStore.getState(), 'closePickSession').mockImplementation(() => {});
    executeSceneBack(event(), IDs.GLOBAL_BACK, { ...ctx, selectedPickSessionCellId: () => 'a' });
    expect(useCollimateStore.getState().commandPane.open).toBe(false); expect(closeBefore).not.toHaveBeenCalled();
    closeBefore.mockRestore();
    useCollimateStore.setState({ pickSessions: { a: { kind: 'files', query: '', at: 1 } } });
    const close = vi.spyOn(useCollimateStore.getState(), 'closePickSession').mockImplementation(() => {});
    executeSceneBack(event(), IDs.GLOBAL_BACK, { ...ctx, selectedPickSessionCellId: () => 'a' }); expect(close).toHaveBeenCalledWith('a', 'files');
    executeSceneBack(event(), IDs.GLOBAL_BACK, { ...ctx, fannedOutStackId: 's1' }); expect(ctx.setFannedOutStackId).toHaveBeenCalledWith(null);
    executeSceneBack(event(), IDs.GLOBAL_BACK, { ...ctx, isZoomedOut: true }); expect(ctx.setIsZoomedOut).toHaveBeenCalledWith(false);
    expect(ctx.setFocusArea).not.toHaveBeenCalled();
    executeSceneBack(event(), IDs.GLOBAL_BACK, ctx); expect(ctx.setFocusArea).toHaveBeenCalledWith('nav');
    executeSceneBack(event(), 'unmatched', { ...ctx, getSelectedFrontCell: () => null });
});

describe('Escape releases the renderer of the selected cell\'s focused tab', () => {
    /** Cell `a`'s card, its focused tab's slot and, in it, a native
     *  renderer (a field in a shadow root) and a host field. */
    const cellWithTab = () => {
        const cell = document.createElement('div'); cell.setAttribute('data-cell-id', 'a');
        const card = document.createElement('div'); card.setAttribute('data-stack-card', '');
        const slot = document.createElement('div'); slot.setAttribute('data-active-tab-body', '');
        const native = document.createElement('div'); native.setAttribute('data-native-renderer', '');
        const host = document.createElement('div'); native.append(host);
        const field = host.attachShadow({ mode: 'open' }).appendChild(document.createElement('textarea'));
        const form = document.createElement('input');
        slot.append(native, form); card.append(slot); cell.append(card); document.body.append(cell);
        return { cell, card, slot, field, form };
    };
    afterEach(() => { document.body.replaceChildren(); });

    it('a native renderer keeps a single Esc; a second within the double-tap moves the keyboard to the card', () => {
        vi.useFakeTimers(); vi.setSystemTime(10_000);
        const { card, field } = cellWithTab();
        field.focus();
        const ctx = makeContext({ fannedOutStackId: 's1' });
        const first = event();
        executeSceneBack(first, IDs.GLOBAL_BACK, ctx);
        expect(first.defaultPrevented).toBe(false);
        expect(ctx.setFannedOutStackId).not.toHaveBeenCalled();
        expect(document.activeElement).not.toBe(card);
        vi.setSystemTime(10_000 + RELEASE_DOUBLE_TAP_MS - 1);
        const second = event();
        executeSceneBack(second, IDs.GLOBAL_BACK, ctx);
        expect(second.defaultPrevented).toBe(true);
        expect(document.activeElement).toBe(card);
        expect(ctx.setFannedOutStackId).not.toHaveBeenCalled();
        // From the card the cascade carries on.
        executeSceneBack(event(), IDs.GLOBAL_BACK, ctx);
        expect(ctx.setFannedOutStackId).toHaveBeenCalledWith(null);
    });

    it('two Esc further apart than the double-tap stay the renderer\'s', () => {
        vi.useFakeTimers(); vi.setSystemTime(50_000);
        const { field } = cellWithTab();
        field.focus();
        const ctx = makeContext();
        executeSceneBack(event(), IDs.GLOBAL_BACK, ctx);
        vi.setSystemTime(50_000 + RELEASE_DOUBLE_TAP_MS + 1);
        const late = event();
        executeSceneBack(late, IDs.GLOBAL_BACK, ctx);
        expect(late.defaultPrevented).toBe(false);
        expect(ctx.setFocusArea).not.toHaveBeenCalled();
    });

    it('a field in the tab\'s slot, and a renderer frame over it (its Esc is already the second), release at once', () => {
        const { card, slot, form } = cellWithTab();
        form.focus();
        const ctx = makeContext();
        const press = event();
        executeSceneBack(press, IDs.GLOBAL_BACK, ctx);
        expect(press.defaultPrevented).toBe(true);
        expect(document.activeElement).toBe(card);
        const overlay = document.createElement('div'); overlay.id = 'collimate-surface-overlay';
        const frame = document.createElement('iframe'); overlay.append(frame); document.body.append(overlay);
        frame.focus();
        executeSceneBack(event(), IDs.GLOBAL_BACK, makeContext({ containerOf: (el: Element) => (el === frame ? slot : null) }));
        expect(document.activeElement).toBe(card);
        expect(ctx.setFocusArea).not.toHaveBeenCalled();
    });

    it('a desktop guest in the tab\'s slot holding the OS focus releases at once (the shell counted the double tap)', () => {
        const { card, field } = cellWithTab();
        const mount = (field.getRootNode() as ShadowRoot).appendChild(document.createElement('div'));
        noteGuestFocus(mount, true);
        try {
            const ctx = makeContext({ fannedOutStackId: 's1' });
            const press = event();
            executeSceneBack(press, IDs.GLOBAL_BACK, ctx);
            expect(press.defaultPrevented).toBe(true);
            expect(document.activeElement).toBe(card);
            expect(ctx.setFannedOutStackId).not.toHaveBeenCalled();
        } finally { noteGuestFocus(mount, false); }
    });

    it('keys on the card, or in another cell, are not released: Esc steps the app back', () => {
        const { card } = cellWithTab();
        card.tabIndex = -1; card.focus();
        const ctx = makeContext();
        executeSceneBack(event(), IDs.GLOBAL_BACK, ctx);
        expect(ctx.setFocusArea).toHaveBeenCalledWith('nav');
    });

    it('a pending move and the cell diff come first; in full window the view root takes the keys, and the next Esc collapses it', () => {
        vi.useFakeTimers(); vi.setSystemTime(90_000);
        const { cell, slot, form } = cellWithTab();
        const view = document.createElement('div'); view.setAttribute('data-full-window', '');
        view.append(slot); document.body.append(view);
        cell.remove();
        form.focus();
        const ctx = makeContext();
        executeSceneBack(event(), IDs.GLOBAL_BACK, { ...ctx, pendingMove: { type: 'cell', sourceId: 's1', originalIndex: 0, targetIndex: 1, mode: 'insert' } });
        expect(document.activeElement).toBe(form);
        useCollimateStore.setState({ maximizedOverlayCellId: 'a' });
        executeSceneBack(event(), IDs.GLOBAL_BACK, ctx);
        expect(document.activeElement).toBe(view);
        expect(useCollimateStore.getState().maximizedOverlayCellId).toBe('a');
        executeSceneBack(event(), IDs.GLOBAL_BACK, ctx);
        expect(useCollimateStore.getState().maximizedOverlayCellId).toBeNull();
    });
});

it('F0936 fan and zoom mode effects keep main-view selection and minimum-cell gates', () => {
    const ctx = makeContext(); executeSceneModes(event(), IDs.GLOBAL_TOGGLE_ZOOM, ctx); expect(ctx.toggleZoom).toHaveBeenCalledOnce();
    executeSceneModes(event(), IDs.SCENE_FAN_OUT, ctx); expect(ctx.setFannedOutStackId).toHaveBeenCalledWith('s1');
    executeSceneModes(event(), IDs.SCENE_FAN_OUT, { ...ctx, fannedOutStackId: 's1' }); expect(ctx.setFannedOutStackId).toHaveBeenCalledWith(null);
    for (const changes of [{ isZoomedOut: true }, { sceneFocusArea: 'nav' as const }, { selectedWindowId: null }, { selectedWindowId: 'missing' }, { mainStacks: [stacks[1]], selectedWindowId: 's2' }]) {
        const guarded = makeContext(changes); executeSceneModes(event(), IDs.SCENE_FAN_OUT, guarded); expect(guarded.setFannedOutStackId).not.toHaveBeenCalled();
    }
    executeSceneModes(event(), 'unmatched', ctx);
});

it('F0936 movement uses display columns, captures release modifiers and refuses missing source/edges', () => {
    const ctx = makeContext(); executeSceneMovement(event(), IDs.NAV_STACK_RIGHT, ctx); expect(ctx.selectDisplayId).toHaveBeenCalledWith('s2');
    executeSceneMovement(event(), IDs.NAV_STACK_LEFT, ctx); expect(ctx.selectDisplayId).toHaveBeenCalledOnce();
    executeSceneMovement(event(), IDs.NAV_STACK_LEFT, { ...ctx, displaySelectedId: 's2' }); expect(ctx.selectDisplayId).toHaveBeenLastCalledWith('s1');
    const pending = { type: 'cell' as const, sourceId: 's1', originalIndex: 0, targetIndex: 1, mode: 'insert' as const };
    executeSceneMovement(event(), IDs.NAV_STACK_RIGHT, { ...ctx, pendingMove: pending }); expect(ctx.advancePendingCellMove).toHaveBeenCalledWith(1);
    executeSceneMovement(event(), IDs.NAV_STACK_RIGHT, { ...ctx, pendingMove: pending, isZoomedOut: true });
    executeSceneMovement(event(), IDs.NAV_CELL_MOVE_RIGHT, ctx);
    expect(ctx.moveReleaseKeys.current).toEqual(new Set(['Control', 'Shift']));
    expect(useSpatialStore.getState().pendingMove).toMatchObject({ sourceId: 's1', targetIndex: 3 });
    executeSceneMovement(event(), IDs.NAV_CELL_MOVE_RIGHT, { ...ctx, pendingMove: pending });
    for (const changes of [{ isZoomedOut: true }, { sceneFocusArea: 'nav' as const }, { displaySelectedId: null }, { displaySelectedId: 'missing' }, { pendingMoveSource: () => null }]) {
        const guarded = makeContext(changes); const set = vi.fn(); guarded.setPendingMove = set;
        executeSceneMovement(event(), IDs.NAV_CELL_MOVE_RIGHT, guarded); expect(set).not.toHaveBeenCalled();
    }
    const left = makeContext({ cellMoveLeftMatched: true }); executeSceneMovement(event(), IDs.NAV_CELL_MOVE_LEFT, left);
    expect(left.moveReleaseKeys.current.has('Shift')).toBe(true);
    const unknown = event(); executeSceneMovement(unknown, 'unmatched', ctx); expect(unknown.defaultPrevented).toBe(false);
});

it('Escape closes the Commands pane after history and before the cell\'s panel, of whichever kind, and hands the keyboard to the cell (C19) (P01)', () => {
    const card = document.createElement('div'); card.setAttribute('data-cell-id', 'a');
    const inner = document.createElement('div'); inner.setAttribute('data-stack-card', ''); card.append(inner); document.body.append(card);
    useCollimateStore.setState({ pickSessions: { a: { kind: 'transfer', query: '', at: 1 } } });
    const close = vi.spyOn(useCollimateStore.getState(), 'closePickSession').mockImplementation(() => {});
    useCollimateStore.getState().openCommandPane();
    const ctx = makeContext({ selectedPickSessionCellId: () => 'a' });
    executeSceneBack(event(), IDs.GLOBAL_BACK, ctx);
    expect(useCollimateStore.getState().commandPane.open).toBe(false);
    expect(close).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(inner);
    executeSceneBack(event(), IDs.GLOBAL_BACK, ctx);
    expect(close).toHaveBeenCalledWith('a', 'transfer');
    card.remove();
});

it('Escape in the full-window view collapses it before it closes the Commands pane; then the pane hands the keyboard to the card (polish)', () => {
    const card = document.createElement('div'); card.setAttribute('data-cell-id', 'a');
    const inner = document.createElement('div'); inner.setAttribute('data-stack-card', ''); card.append(inner);
    const view = document.createElement('div'); view.setAttribute('data-full-window', '');
    const editor = document.createElement('textarea'); view.append(editor);
    document.body.append(card, view);
    useCollimateStore.setState({ maximizedOverlayCellId: 'a' });
    useCollimateStore.getState().openCommandPane();
    editor.focus();
    executeSceneBack(event(), IDs.GLOBAL_BACK, makeContext());
    expect(useCollimateStore.getState().maximizedOverlayCellId).toBeNull();
    expect(useCollimateStore.getState().commandPane.open).toBe(true);
    view.remove();
    executeSceneBack(event(), IDs.GLOBAL_BACK, makeContext());
    expect(useCollimateStore.getState().commandPane.open).toBe(false);
    expect(document.activeElement).toBe(inner);
    card.remove();
});

it('Escape never stops travelling: whatever else handles Esc still sees it (polish)', () => {
    useCollimateStore.getState().openCommandPane();
    const ev = event();
    const stop = vi.spyOn(ev, 'stopPropagation');
    executeSceneBack(ev, IDs.GLOBAL_BACK, makeContext());
    expect(useCollimateStore.getState().commandPane.open).toBe(false);
    expect(stop).not.toHaveBeenCalled();
});

it('Ctrl-←/→ steps the selection in the overview too; Alt-Shift-S duplicates a stack like its menu (C19)', () => {
    const zoomed = makeContext({ isZoomedOut: true });
    executeSceneMovement(event(), IDs.NAV_STACK_RIGHT, zoomed);
    expect(zoomed.selectDisplayId).toHaveBeenCalledWith('s2');
    const ctx = makeContext();
    executeSceneActions(event(), IDs.STACK_DUPLICATE_STACK, ctx);
    expect(ctx.actions.duplicateStack).toHaveBeenCalledWith('s1');
});

describe('on Capabilities', () => {
    const caps: Stack = {
        id: 'caps', name: 'Capabilities', stack_type: 'capabilities',
        cells: [{ id: 'card', name: 'Definitions', files: [], builtin: true }, { id: 'u', name: 'U', files: [] }],
    };
    // Its manage card (slot 0) in front, or its cell u (slot 2).
    const onCaps = (slot: number, changes: Partial<Context> = {}) => makeContext({
        mainStacks: [caps, stacks[1]], displayStacks: [caps, stacks[1]], selectedWindowId: 'caps', displaySelectedId: 'caps',
        visibleTopIndicesRef: { current: { caps: slot } },
        getSelectedFrontCell: () => (slot === 0 ? null : caps.cells[slot - 1]),
        ...changes,
    });

    it('Run, Duplicate + run, Fan out and Duplicate stack do nothing, whichever face is in front', async () => {
        const open = vi.spyOn(useCollimateStore.getState(), 'openPickSession').mockImplementation(() => {});
        for (const slot of [0, 2]) {
            const ctx = onCaps(slot);
            for (const command of [IDs.STACK_RUN, IDs.STACK_DUPLICATE_RUN, IDs.STACK_DUPLICATE_STACK]) {
                const e = event(); executeSceneActions(e, command, ctx); expect(e.defaultPrevented).toBe(true);
            }
            executeSceneModes(event(), IDs.SCENE_FAN_OUT, ctx);
            expect(ctx.actions.duplicateStack).not.toHaveBeenCalled();
            expect(ctx.actions.winDuplicateToNewStack).not.toHaveBeenCalled();
            expect(ctx.setFannedOutStackId).not.toHaveBeenCalled();
        }
        await Promise.resolve();
        expect(open).not.toHaveBeenCalled();
        expect(useCollimateStore.getState().pendingGeneratorRunnerStackId).toBeNull();
    });

    it('Remove cell and Remove stack hide the stack, from the manage card too, even with a run live elsewhere in it', () => {
        useCollimateStore.setState({
            runs: { r: { runId: 'r', cellId: 'u', channel: '11111111-1111-4111-8111-111111111111', runnerId: 'x', runnerName: 'X', generatorName: 'g', label: 'x', folder: '_runs/x' } },
        } as never);
        for (const slot of [0, 2]) {
            for (const command of [IDs.CELL_REMOVE, IDs.STACK_REMOVE]) {
                const ctx = onCaps(slot);
                executeSceneActions(event(), command, ctx);
                expect(ctx.handleRemoveStack).toHaveBeenCalledExactlyOnceWith('caps');
                expect(ctx.actions.winRemoveCell).not.toHaveBeenCalled();
            }
        }
    });

    it('Duplicate copies the front cell to a new stack; the cell move has no source while the manage card is in front', () => {
        const ctx = onCaps(2);
        executeSceneActions(event(), IDs.STACK_DUPLICATE, ctx);
        expect(ctx.actions.winDuplicateToNewStack).toHaveBeenCalledWith('caps');
        const manage = onCaps(0, { pendingMoveSource: () => null });
        const set = vi.fn(); manage.setPendingMove = set;
        executeSceneMovement(event(), IDs.NAV_CELL_MOVE_RIGHT, manage);
        expect(set).not.toHaveBeenCalled();
    });

    it('Alt-↑/↓ pages its faces like any stack', () => {
        const ctx = onCaps(0);
        executeSceneActions(event(), IDs.STACK_ROTATE_NEXT, ctx);
        expect(ctx.actions.winRotate).toHaveBeenCalledWith('caps', 1);
    });
});

it('Alt-↑/↓ asks the stack to step its front cell and writes nothing else: no selection, focus or store writes (C19)', () => {
    // winRotate is the same front-cell write a knob click or a drop makes, so
    // the head plays one knob snap for all of them
    // (StackGroup.knobSnap.browser.test samples it).
    const ctx = makeContext();
    const spatial = useSpatialStore.getState();
    const store = useCollimateStore.getState();
    executeSceneActions(event(), IDs.STACK_ROTATE_PREV, ctx);
    executeSceneActions(event(), IDs.STACK_ROTATE_NEXT, ctx);
    expect(ctx.actions.winRotate).toHaveBeenNthCalledWith(1, 's1', -1);
    expect(ctx.actions.winRotate).toHaveBeenNthCalledWith(2, 's1', 1);
    expect(ctx.selectDisplayId).not.toHaveBeenCalled();
    expect(ctx.setFocusArea).not.toHaveBeenCalled();
    expect(useSpatialStore.getState()).toBe(spatial);
    expect(useCollimateStore.getState()).toBe(store);
});
