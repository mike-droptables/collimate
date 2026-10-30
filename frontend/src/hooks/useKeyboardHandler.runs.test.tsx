// Shortcut parity with the cell header and the stack menu while runs are
// live: removing the focused cell (or its stack) is blocked while a run is
// live there — any live run, idle ones included — while every Duplicate
// shortcut stays available (the copy leaves the runs' files behind) and the
// runners panel opens on a cell that already runs.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, cleanup, act } from '@testing-library/react';
import { useKeyboardHandler } from './useKeyboardHandler';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { FocusContextType } from '@/contexts/FocusContext';
import type { SceneActions } from './useSceneActions';
import { useKeybindingsStore } from '@/lib/keybindings/store';
import * as api from '@/lib/api';
import { useSpatialStore } from '@/stores/useSpatialStore';
import type { Cell, Channel, Stack } from '@/lib/types';

// The guest pool maps a pooled frame to the slot it is positioned over.
const pool = vi.hoisted(() => ({ containerOf: (() => null) as (el: Element) => HTMLElement | null }));
vi.mock('@/contexts/GuestPoolContext', async (importOriginal) => {
    const actual = await importOriginal<typeof import('@/contexts/GuestPoolContext')>();
    return { ...actual, useGuestPool: () => ({ ...actual.useGuestPool(), containerOf: (el: Element) => pool.containerOf(el) }) };
});
const pick = vi.hoisted(() => ({ pending: false }));
vi.mock('@/lib/rendererAnnotation', async (importOriginal) => ({
    ...(await importOriginal<object>()),
    hasPendingAnnotationPick: () => pick.pending,
}));

// Every movement owns a delayed settle callback, including retained tests.
// Drain them before a later test starts another window with a different clock.
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanup(); if (vi.isFakeTimers()) vi.runOnlyPendingTimers(); vi.useRealTimers(); });

const cell = (id: string): Cell => ({ id, name: id, files: [] });
const STACKS: Stack[] = [{ id: 's1', name: '', cells: [cell('a'), cell('b')] }];

const focus = {
    focusArea: 'main', selectedWindowId: 's1', mainFocusMode: 'window',
    setFocusArea: vi.fn(), setSelectedWindowId: vi.fn(), setMainFocusMode: vi.fn(),
} as unknown as FocusContextType;

const makeActions = () => ({
    winRemoveCell: vi.fn(),
    winDuplicateToNewStack: vi.fn(async () => null),
    duplicateCellInStack: vi.fn(async () => null),
    duplicateStack: vi.fn(),
    winRotate: vi.fn(),
    newEmptyStackRight: vi.fn(async () => null),
}) as unknown as SceneActions & Record<string, ReturnType<typeof vi.fn>>;

const mount = (overrides: Partial<Parameters<typeof useKeyboardHandler>[0]> = {}) => {
    const actions = makeActions();
    const handleRemoveStack = vi.fn();
    const hook = renderHook(() => useKeyboardHandler({
        focusState: focus, mainStacks: STACKS, activeChannel: null,
        performAction: (p) => p, isZoomedOut: false, setIsZoomedOut: vi.fn(), toggleZoom: vi.fn(),
        setTransitionType: vi.fn(), setVisibleTopIndices: vi.fn(), setMergeNonces: vi.fn(),
        visibleTopIndicesRef: { current: {} }, handleRemoveStack, actions,
        fannedOutStackId: null, setFannedOutStackId: vi.fn(), fan: null,
        displayStacks: STACKS, displaySelectedId: 's1', selectDisplayId: vi.fn(),
        ...overrides,
    }));
    return { actions, handleRemoveStack, ...hook };
};

const press = (key: string, mods: { alt?: boolean; shift?: boolean } = {}) =>
    window.dispatchEvent(new KeyboardEvent('keydown', { key, altKey: !!mods.alt, shiftKey: !!mods.shift, bubbles: true }));

const liveRunOn = (cellId: string, phase?: string) => useCollimateStore.setState({
    runs: { 'run-1': { runId: 'run-1', cellId, channel: 'ch', runnerId: 'r', runnerName: 'R', generatorName: 'g', label: 'r-11111111', folder: '_runs/r-11111111' } }, stoppingRunIds: new Set(),
    runPhases: phase ? { 'run-1': { phase } } : {},
} as never);

describe('useKeyboardHandler — live runs', () => {
    beforeEach(() => {
        useCollimateStore.setState({
            runs: {}, stoppingRunIds: new Set(), runPhases: {},
            pendingGeneratorRunnerStackId: null, pickSessions: {},
        } as never);
    });
    afterEach(cleanup);

    it('Remove cell is blocked while a run is live on the front cell, even an idle one', () => {
        liveRunOn('a', 'awaiting_input');
        const { actions } = mount();
        press('Backspace', { alt: true });
        expect(actions.winRemoveCell).not.toHaveBeenCalled();
    });

    it('Remove cell fires when the run is on another cell of the stack', () => {
        liveRunOn('b');
        const { actions } = mount();
        press('Backspace', { alt: true });
        expect(actions.winRemoveCell).toHaveBeenCalledWith('s1', 0);
    });

    it('Remove stack is blocked while a run is live on any of its cells', () => {
        liveRunOn('b');
        const { handleRemoveStack } = mount();
        press('Backspace', { alt: true, shift: true });
        expect(handleRemoveStack).not.toHaveBeenCalled();
    });

    it('every Duplicate shortcut stays available while runs are live', () => {
        liveRunOn('a');
        const { actions } = mount();
        press('d', { alt: true });
        expect(actions.winDuplicateToNewStack).toHaveBeenCalledWith('s1');
        press('d', { alt: true, shift: true });
        expect(actions.winDuplicateToNewStack).toHaveBeenCalledTimes(2);
        press('s', { alt: true, shift: true });
        expect(actions.duplicateStack).toHaveBeenCalledWith('s1');
    });

    it('the run shortcut opens the runners panel on a cell that already runs (P02)', () => {
        liveRunOn('a');
        mount();
        press('r', { alt: true });
        expect(useCollimateStore.getState().pendingGeneratorRunnerStackId).toBe('s1');
    });
});

describe('useKeyboardHandler — Esc under a modal', () => {
    afterEach(cleanup);

    it('Esc with a modal (aria-modal) open leaves the scene alone', () => {
        const setFannedOutStackId = vi.fn();
        const modal=document.createElement('div');modal.setAttribute('aria-modal','true');document.body.append(modal);
        mount({ fannedOutStackId: 's1', setFannedOutStackId });
        press('Escape');
        expect(setFannedOutStackId).not.toHaveBeenCalled();
        document.querySelector('[aria-modal]')?.remove();
    });

    it('Esc inside a cell\'s status pane is the pane\'s own: the fan stays out; another key still reaches the scene (C15, C19)', () => {
        const setFannedOutStackId = vi.fn();
        const log = document.createElement('div');
        log.setAttribute('data-runs-pane', '');
        const line = document.createElement('div');
        line.tabIndex = -1;
        log.append(line);
        document.body.append(log);
        const { actions } = mount({ fannedOutStackId: 's1', setFannedOutStackId });
        line.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        expect(setFannedOutStackId).not.toHaveBeenCalled();
        line.dispatchEvent(new KeyboardEvent('keydown', { key: 'd', altKey: true, bubbles: true }));
        expect(actions.winDuplicateToNewStack).toHaveBeenCalledWith('s1');
        log.remove();
    });

    it('a window Esc forwarded by a pooled frame that sits in a status pane is the pane\'s; over a file tab it reaches the scene', () => {
        const setFannedOutStackId = vi.fn();
        const pane = document.body.appendChild(document.createElement('aside'));
        pane.setAttribute('data-runs-pane', '');
        const composerSlot = pane.appendChild(document.createElement('div'));
        const tabSlot = document.body.appendChild(document.createElement('div'));
        const overlay = document.body.appendChild(document.createElement('div'));
        overlay.id = 'collimate-surface-overlay';
        const frame = overlay.appendChild(document.createElement('iframe'));
        let slot: HTMLElement = composerSlot;
        pool.containerOf = (el) => (el === frame ? slot : null);
        try {
            mount({ fannedOutStackId: 's1', setFannedOutStackId });
            frame.focus();
            press('Escape');
            expect(setFannedOutStackId).not.toHaveBeenCalled();
            slot = tabSlot;
            press('Escape');
            expect(setFannedOutStackId).toHaveBeenCalledWith(null);
        } finally {
            pool.containerOf = () => null;
            frame.blur();
            pane.remove(); tabSlot.remove(); overlay.remove();
        }
    });

    it('Esc while an element pick is pending is the pick\'s own', () => {
        const setFannedOutStackId = vi.fn();
        pick.pending = true;
        try {
            mount({ fannedOutStackId: 's1', setFannedOutStackId });
            press('Escape');
            expect(setFannedOutStackId).not.toHaveBeenCalled();
        } finally {
            pick.pending = false;
        }
    });

    it('Esc with no modal open collapses the fan', () => {
        const setFannedOutStackId = vi.fn();
        mount({ fannedOutStackId: 's1', setFannedOutStackId });
        press('Escape');
        expect(setFannedOutStackId).toHaveBeenCalledWith(null);
    });
});


describe('workbench keyboard ownership', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        useCollimateStore.setState({runs:{},pickSessions:{},pendingGeneratorRunnerStackId:null} as never);
        useSpatialStore.setState({pendingMove:null});
        useKeybindingsStore.getState().adoptOverrides({});
    });
    afterEach(() => {cleanup();document.body.innerHTML='';vi.unstubAllGlobals();});

    it('first shortcut wakes dim focus and acts once __F0912', () => {
        mount({focusState:{...focus,focusArea:'nav'}});
        press('r',{alt:true});
        expect(focus.setFocusArea).toHaveBeenCalledWith('main');
        expect(useCollimateStore.getState().pendingGeneratorRunnerStackId).toBe('s1');
    });
    it('modal DOM ownership suppresses scene even when event targets window __F0931', () => {
        const dialog=document.createElement('div');dialog.setAttribute('aria-modal','true');document.body.append(dialog);
        const {actions}=mount();press('d',{alt:true});
        expect(actions.winDuplicateToNewStack).not.toHaveBeenCalled();
        dialog.remove();press('d',{alt:true});expect(actions.winDuplicateToNewStack).toHaveBeenCalledWith('s1');
    });
    it('Meta move binding owns only the current pending shape and commits on required release __F0913_F0658', async () => {
        vi.stubGlobal('navigator',{platform:'MacIntel',userAgent:''});
        useKeybindingsStore.getState().adoptOverrides({'nav.cellMoveRight':'Mod+ArrowRight'});
        const actions={...makeActions(),applyCellOp:vi.fn(async()=>null)};
        mount({actions,displayStacks:[...STACKS,{id:'s2',name:'',cells:[cell('c')]}]});
        await act(async()=>{window.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',metaKey:true,bubbles:true}));});
        expect(useSpatialStore.getState().pendingMove).not.toBeNull();
        expect(Object.keys(useSpatialStore.getState().pendingMove!).sort()).toEqual(['inFan','mode','originalIndex','sourceId','targetIndex','type'].sort());
        await act(async()=>{window.dispatchEvent(new KeyboardEvent('keyup',{key:'Alt',metaKey:true}));});
        expect(actions.applyCellOp).not.toHaveBeenCalled();
        await act(async()=>{window.dispatchEvent(new KeyboardEvent('keyup',{key:'Meta'}));});
        expect(actions.applyCellOp).toHaveBeenCalledTimes(1);
        await act(async()=>{window.dispatchEvent(new KeyboardEvent('keyup',{key:'Meta'}));});
        expect(actions.applyCellOp).toHaveBeenCalledTimes(1);
    });
});

describe('F0936 ordered scene keyboard characterization before extraction', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        useCollimateStore.setState({ runs: {}, pickSessions: {}, cellDiffs: {}, cellHistory: {}, maximizedOverlayCellId: null } as never);
        useSpatialStore.setState({ pendingMove: null });
        useKeybindingsStore.getState().adoptOverrides({});
    });
    afterEach(() => { vi.restoreAllMocks(); });

    it('F0936 keeps one capture listener pair and refreshes its action snapshot on selection change', () => {
        const added = vi.spyOn(window, 'addEventListener');
        const removed = vi.spyOn(window, 'removeEventListener');
        const overrides = { mainStacks: [...STACKS, { id: 's2', name: '', cells: [cell('c')] }], focusState: focus };
        const { actions, rerender, unmount } = mount(overrides);
        const keyCalls = () => added.mock.calls.filter(([kind]) => kind === 'keydown' || kind === 'keyup');
        expect(keyCalls().map(([kind, , options]) => [kind, options])).toEqual([
            ['keydown', { capture: true }], ['keyup', { capture: true }],
        ]);
        press('d', { alt: true });
        expect(actions.winDuplicateToNewStack).toHaveBeenLastCalledWith('s1');
        overrides.focusState = { ...focus, selectedWindowId: 's2' };
        rerender();
        press('d', { alt: true });
        expect(actions.winDuplicateToNewStack).toHaveBeenLastCalledWith('s2');
        expect(removed.mock.calls.filter(([kind]) => kind === 'keydown' || kind === 'keyup')).toHaveLength(2);
        unmount();
        expect(removed.mock.calls.filter(([kind]) => kind === 'keydown' || kind === 'keyup')).toHaveLength(4);
    });

    it('F0936 Escape clears a pending move before the visible cell diff and only then collapses the fan', () => {
        const collapse = vi.fn();
        mount({ fannedOutStackId: 's1', setFannedOutStackId: collapse });
        useSpatialStore.setState({ pendingMove: { type: 'cell', sourceId: 's1', originalIndex: 1, targetIndex: 2, mode: 'insert' } });
        useCollimateStore.setState({ cellDiffs: { a: 'base' } } as never);
        press('Escape');
        expect(useSpatialStore.getState().pendingMove).toBeNull();
        expect(useCollimateStore.getState().cellDiffs.a).toBe('base');
        expect(collapse).not.toHaveBeenCalled();
        press('Escape');
        expect(useCollimateStore.getState().cellDiffs.a).toBeUndefined();
        expect(collapse).not.toHaveBeenCalled();
        press('Escape');
        expect(collapse).toHaveBeenCalledExactlyOnceWith(null);
    });
});


it('legacy Ctrl Alt Arrow no longer bypasses registry __F0934',()=>{
 const move=vi.spyOn(api,'moveStackTo').mockResolvedValue(null as never);
 mount({activeChannel:{id:'ch',name:'channel',main_stacks:STACKS,nav_stacks:[]} as never});
 window.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',altKey:true,ctrlKey:true}));
 expect(move).not.toHaveBeenCalled();move.mockRestore();cleanup();
});


it('Alt move binding advances repeatedly then commits on Alt __F0913',async()=>{
 useKeybindingsStore.getState().adoptOverrides({'nav.cellMoveRight':'Alt+ArrowRight'});useSpatialStore.setState({pendingMove:null});
 const actions={...makeActions(),applyCellOp:vi.fn(async()=>null)};
 mount({actions,displayStacks:[...STACKS,{id:'s2',name:'',cells:[cell('c')]}]});
 try {
  await act(async()=>{press('ArrowRight',{alt:true});});const first=useSpatialStore.getState().pendingMove;
  await act(async()=>{press('ArrowRight',{alt:true});});expect(useSpatialStore.getState().pendingMove).not.toEqual(first);
  await act(async()=>{window.dispatchEvent(new KeyboardEvent('keyup',{key:'Alt'}));});expect(actions.applyCellOp).toHaveBeenCalledTimes(1);
 }finally{cleanup();useKeybindingsStore.getState().adoptOverrides({});useSpatialStore.setState({pendingMove:null});}
});

it('focused inline iframe owner blocks forwarded window chords without blocking another cell __F0931',()=>{
 const owner=document.createElement('div');owner.setAttribute('data-owns-keyboard','');const frame=document.createElement('iframe');owner.append(frame);document.body.append(owner);frame.focus();
 const {actions}=mount();
 try {press('d',{alt:true});expect(actions.winDuplicateToNewStack).not.toHaveBeenCalled();owner.remove();press('d',{alt:true});expect(actions.winDuplicateToNewStack).toHaveBeenCalledWith('s1');}
 finally{owner.remove();cleanup();}
});

it('overlapping moves keep the camera held until both settle __P3F04', async () => {
    vi.useFakeTimers();
    useKeybindingsStore.getState().adoptOverrides({});
    useSpatialStore.setState({ pendingMove: null, cameraHold: false });
    const finishes: Array<() => void> = [];
    const actions = { ...makeActions(), applyCellOp: vi.fn(() => new Promise<null>(resolve => {
        finishes.push(() => resolve(null));
    })) };
    mount({ actions, displayStacks: [...STACKS, { id: 's2', name: '', cells: [cell('c')] }] });
    try {
        for (let i = 0; i < 2; i++) {
            await act(async () => {
                window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', ctrlKey: true, shiftKey: true, bubbles: true }));
                window.dispatchEvent(new KeyboardEvent('keyup', { key: 'Shift', ctrlKey: true, bubbles: true }));
            });
        }
        expect(actions.applyCellOp).toHaveBeenCalledTimes(2);
        await act(async () => { finishes[0](); });
        act(() => { vi.advanceTimersByTime(600); });
        expect(useSpatialStore.getState().cameraHold).toBe(true);
        await act(async () => { finishes[1](); });
        act(() => { vi.advanceTimersByTime(600); });
        expect(useSpatialStore.getState().cameraHold).toBe(false);
    } finally {
        for (const finish of finishes) finish();
        await act(async () => {});
        cleanup();
        vi.runOnlyPendingTimers();
        vi.useRealTimers();
        useSpatialStore.setState({ pendingMove: null, cameraHold: false });
    }
});

describe('a keyboard cell move onto Capabilities', () => {
    const caps: Stack = { id: 'caps', name: 'Capabilities', stack_type: 'capabilities', cells: [{ ...cell('card'), builtin: true }, cell('u')] };
    const board = [...STACKS, caps];
    // Start a move from s1's front cell, aim it onto Capabilities, release.
    const commitOntoCapabilities = async (over: Partial<Parameters<typeof useKeyboardHandler>[0]> = {}) => {
        useKeybindingsStore.getState().adoptOverrides({});
        useSpatialStore.setState({ pendingMove: null, cameraHold: false });
        const landed = { id: 'ch', name: 'ch', nav_stacks: [], main_stacks: [{ ...STACKS[0], cells: [cell('b')] }, { ...caps, cells: [caps.cells[0], cell('a'), cell('u')] }] };
        const actions = { ...makeActions(), applyCellOp: vi.fn(async () => landed as unknown as Channel), winSetTopCell: vi.fn() };
        mount({ actions, mainStacks: board, displayStacks: board, ...over });
        await act(async () => {
            window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', ctrlKey: true, shiftKey: true, bubbles: true }));
        });
        act(() => { useSpatialStore.setState({ pendingMove: { type: 'cell', sourceId: 's1', originalIndex: 0, targetIndex: 3, mode: 'stack' } }); });
        await act(async () => {
            window.dispatchEvent(new KeyboardEvent('keyup', { key: 'Shift', ctrlKey: true, bubbles: true }));
        });
        return actions;
    };
    afterEach(() => { useSpatialStore.setState({ pendingMove: null, cameraHold: false }); useCollimateStore.setState({ runs: {} } as never); });

    it('lands after the built-in cards and fronts the cell by its REAL index', async () => {
        const actions = await commitOntoCapabilities();
        expect(actions.applyCellOp).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: 'intoStack', targetRealIdx: 1, tgtCellIdx: 1, cellId: 'a' }));
        expect(actions.winSetTopCell).toHaveBeenCalledWith('caps', 1);
    });

    it('a running cell is refused with the reason, and nothing is sent', async () => {
        const openAlert = vi.fn().mockResolvedValue(undefined);
        useCollimateStore.setState({ openAlert } as never);
        liveRunOn('a');
        const actions = await commitOntoCapabilities();
        expect(actions.applyCellOp).not.toHaveBeenCalled();
        expect(openAlert).toHaveBeenCalledExactlyOnceWith({ title: 'Move refused', body: 'Stop its run before moving it onto Capabilities.' });
    });

    it('a running cell’s keyboard move stops on Capabilities, and releasing there says why instead of skipping past it', async () => {
        const openAlert = vi.fn().mockResolvedValue(undefined);
        useCollimateStore.setState({ openAlert } as never);
        liveRunOn('a');
        useKeybindingsStore.getState().adoptOverrides({});
        useSpatialStore.setState({ pendingMove: null, cameraHold: false });
        const actions = { ...makeActions(), applyCellOp: vi.fn(async () => null) };
        mount({ actions, mainStacks: board, displayStacks: board });
        await act(async () => {
            window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', ctrlKey: true, shiftKey: true, bubbles: true }));
        });
        // The first stop right of s1 is onto Capabilities (display column 1).
        expect(useSpatialStore.getState().pendingMove).toMatchObject({ targetIndex: 3, mode: 'stack' });
        await act(async () => {
            window.dispatchEvent(new KeyboardEvent('keyup', { key: 'Shift', ctrlKey: true, bubbles: true }));
        });
        expect(actions.applyCellOp).not.toHaveBeenCalled();
        expect(openAlert).toHaveBeenCalledExactlyOnceWith({ title: 'Move refused', body: 'Stop its run before moving it onto Capabilities.' });
    });

    it('no move starts while the manage card is in front: it is no cell', async () => {
        useSpatialStore.setState({ pendingMove: null });
        mount({ mainStacks: board, displayStacks: board, displaySelectedId: 'caps', focusState: { ...focus, selectedWindowId: 'caps' } as FocusContextType, visibleTopIndicesRef: { current: { caps: 0 } } });
        await act(async () => {
            window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', ctrlKey: true, shiftKey: true, bubbles: true }));
        });
        expect(useSpatialStore.getState().pendingMove).toBeNull();
    });
});
