import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useSceneActions } from './useSceneActions';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useSpatialStore } from '@/stores/useSpatialStore';
import type { FocusContextType } from '@/contexts/FocusContext';
import type { Channel, Cell, Stack } from '@/lib/types';
import * as api from '@/lib/api';
import { takeSelectionHandOff } from '@/lib/stackEntry';
import { createDefaultLayout } from '@/lib/cellLayout';

vi.mock('@/lib/api');

const cell = (id: string): Cell => ({ id, name: id, files: [] });
const stack = (id: string, cells: Cell[]): Stack => ({ id, name: '', cells });

const channel = (name: string, mainStacks: Stack[] = []): Channel => ({
    id: '11111111-1111-4111-8111-111111111111',
    name,
    nav_stacks: [],
    main_stacks: mainStacks,
    nav_selection_id: null,
    main_selection_id: null,
});

const makeFocus = (selectedWindowId: string | null = null): FocusContextType => ({
    setFocusArea: vi.fn(),
    setSelectedWindowId: vi.fn(),
    setMainFocusMode: vi.fn(),

    selectedWindowId,
} as unknown as FocusContextType);

const performAction = (p: Promise<unknown>) => p;

// The focus context is built once per render() and closed over, so its
// spies stay the same objects across the hook's re-renders — tests can
// assert on them.
const render = (mainStacks: Stack[], ch: Channel | null, focus: FocusContextType = makeFocus()) =>
    renderHook(() => useSceneActions({
        performAction,
        focusState: focus,
        mainStacks,
        activeChannel: ch,
        isZoomedOut: false,
    }));

// Prop-driven variant for the fan-out cases, which rerender with a changed
// stack list / zoom state.
interface FanProps { mainStacks: Stack[]; isZoomedOut?: boolean }
const renderFan = (init: FanProps, focus: FocusContextType = makeFocus()) =>
    renderHook((p: FanProps) => useSceneActions({
        performAction,
        focusState: focus,
        mainStacks: p.mainStacks,
        activeChannel: channel('chan', p.mainStacks),
        isZoomedOut: p.isZoomedOut ?? false,
    }), { initialProps: init });

describe('useSceneActions — merge / split / newEmptyStackRight', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        useCollimateStore.setState({
            state: null,
            pendingSelectCellId: null,
            runs: {},
            stoppingRunIds: new Set<string>(),
        });
    });

    // A synthetic live run on `cellId` (its files are its lock).
    const liveRun = (cellId: string) => useCollimateStore.setState({
        runs: { 'run-1': { runId: 'run-1', cellId, channel: '11111111-1111-4111-8111-111111111111', runnerId: 'r', runnerName: 'R', generatorName: 'g', label: 'r-11111111', folder: '_runs/r-11111111' } },
    });

    describe('mergeStack', () => {
        it('refuses while a run is live in the stack — no request', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch });
            liveRun('b');
            const stacks = [stack('s1', [cell('a'), cell('b')])];

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.actions.mergeStack('s1'); });

            expect(api.mergeStack).not.toHaveBeenCalled();
        });

        it('POSTs, applies the returned channel, selects the new cell', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch });
            const stacks = [stack('s1', [cell('a'), cell('b')])];
            vi.mocked(api.mergeStack).mockResolvedValue({
                ok: true, new_cell_id: 'merged', new_stack_id: 'ns', channel: ch,
            });

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.actions.mergeStack('s1'); });

            expect(api.mergeStack).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 's1');
            expect(useCollimateStore.getState().pendingSelectCellId).toBe('merged');
        });

        it('guards a <2-cell stack — no request', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch });
            const stacks = [stack('s1', [cell('a')])];

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.actions.mergeStack('s1'); });

            expect(api.mergeStack).not.toHaveBeenCalled();
        });

        it('resyncs on the error path', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch });
            const stacks = [stack('s1', [cell('a'), cell('b')])];
            vi.mocked(api.mergeStack).mockRejectedValue(new Error('boom'));
            vi.mocked(api.fetchState).mockResolvedValue({
                channels: [], active_channel_id: null, channel_scope: null,
            });

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.actions.mergeStack('s1'); });

            // The catch resyncs via the store's fetchState → api.fetchState.
            expect(api.fetchState).toHaveBeenCalled();
        });
    });

    describe('splitCell', () => {
        it('keeps the retained source display while hydrating revived cells', async () => {
            const stacks = [stack('s1', [cell('collapsed')])];
            const ch = channel('chan', stacks);
            const retained = { ...ch, main_stacks: [stack('s1', [cell('collapsed'), cell('e0')])] };
            const display = createDefaultLayout({ filePaths: [], pinnedPath: null });
            const hydrate = vi.spyOn(useCollimateStore.getState(), 'hydrateCellDisplays').mockResolvedValue();
            useCollimateStore.setState({ activeChannel: ch, cellLayouts: { collapsed: display } });
            vi.mocked(api.splitCell).mockResolvedValue({
                ok: true, new_cell_ids: ['e0'], new_stack_id: 's1', channel: retained,
            });

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.actions.splitCell('collapsed', 's1'); });

            expect(useCollimateStore.getState().cellLayouts.collapsed).toBe(display);
            expect(hydrate).toHaveBeenCalledWith(['e0']);
            hydrate.mockRestore();
        });

        it('refuses while a run is live on the cell — no request', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch });
            liveRun('collapsed');
            const stacks = [stack('s1', [cell('collapsed')])];

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.actions.splitCell('collapsed', 's1'); });

            expect(api.splitCell).not.toHaveBeenCalled();
        });

        it('POSTs, applies the channel, selects the first resulting cell', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch });
            const stacks = [stack('s1', [cell('collapsed')])];
            vi.mocked(api.splitCell).mockResolvedValue({
                ok: true, new_cell_ids: ['e0', 'e1'], new_stack_id: 'ns', channel: ch,
            });

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.actions.splitCell('collapsed', 's1'); });

            expect(api.splitCell).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 'collapsed');
            expect(useCollimateStore.getState().pendingSelectCellId).toBe('e0');
        });

        it('resyncs on the error path', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch });
            const stacks = [stack('s1', [cell('collapsed')])];
            vi.mocked(api.splitCell).mockRejectedValue(new Error('boom'));
            vi.mocked(api.fetchState).mockResolvedValue({
                channels: [], active_channel_id: null, channel_scope: null,
            });

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.actions.splitCell('collapsed', 's1'); });

            expect(api.fetchState).toHaveBeenCalled();
        });
    });

    describe('handleRemoveStack', () => {
        const armConfirm = (answer: boolean) => {
            const openConfirm = vi.fn().mockResolvedValue(answer);
            useCollimateStore.setState({ openConfirm } as never);
            return openConfirm;
        };

        it('removes without any confirmation dialog (the cells go to the Removed shelf)', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch } as never);
            const openConfirm = armConfirm(true);
            const stacks = [{ ...stack('s1', [cell('a')]), name: 'My stack' }];

            const { result } = render(stacks, ch);
            let ok: boolean | undefined;
            await act(async () => { ok = await result.current.handleRemoveStack('s1'); });

            expect(openConfirm).not.toHaveBeenCalled();
            expect(api.removeStack).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 's1');
            expect(ok).toBe(true);
        });

        it('resolves false when the request failed (performAction yields null)', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch } as never);
            vi.mocked(api.removeStack).mockResolvedValueOnce(null as never);
            const stacks = [stack('s1', [cell('a')])];

            const { result } = render(stacks, ch);
            let ok: boolean | undefined;
            await act(async () => { ok = await result.current.handleRemoveStack('s1'); });

            expect(ok).toBe(false);
        });

        it('Capabilities hides to its pill: no confirm, and no run holds it back', async () => {
            const ch = channel('chan');
            const openConfirm = armConfirm(true);
            const stacks = [{ ...stack('s1', [{ ...cell('card'), builtin: true }, cell('a')]), stack_type: 'capabilities' as const }];
            liveRun('a');
            useCollimateStore.setState({ activeChannel: ch } as never);
            vi.mocked(api.minimizeStack).mockResolvedValueOnce(ch as never);

            const { result } = render(stacks, ch);
            let ok: boolean | undefined;
            await act(async () => { ok = await result.current.handleRemoveStack('s1'); });

            expect(ok).toBe(true);
            expect(openConfirm).not.toHaveBeenCalled();
            expect(api.minimizeStack).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 's1');
            expect(api.removeStack).not.toHaveBeenCalled();
        });

        const liveRunOn = (ch: Channel, cellId: string, phase?: string) => useCollimateStore.setState({
            activeChannel: ch,
            runs: { 'run-1': { runId: 'run-1', cellId, channel: '11111111-1111-4111-8111-111111111111', runnerId: 'r', runnerName: 'R', generatorName: 'g', label: 'r-11111111', folder: '_runs/r-11111111' } },
            stoppingRunIds: new Set<string>(),
            runPhases: phase ? { 'run-1': { phase } } : {},
        } as never);

        it('a live run in the stack blocks removal before the confirm', async () => {
            const ch = channel('chan');
            liveRunOn(ch, 'a');
            const openConfirm = armConfirm(true);
            const stacks = [stack('s1', [cell('a')])];

            const { result } = render(stacks, ch);
            let ok: boolean | undefined;
            await act(async () => { ok = await result.current.handleRemoveStack('s1'); });

            expect(openConfirm).not.toHaveBeenCalled();
            expect(api.removeStack).not.toHaveBeenCalled();
            expect(ok).toBe(false);
        });

        it('a run idling in awaiting_input blocks removal too — every live run holds its files', async () => {
            const ch = channel('chan');
            liveRunOn(ch, 'a', 'awaiting_input');
            armConfirm(true);
            const stacks = [stack('s1', [cell('a')])];

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.handleRemoveStack('s1'); });

            expect(api.removeStack).not.toHaveBeenCalled();
        });

        it('keeps `actions` stable across selection changes and removes against the latest selection__F0657_F0662', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch } as never);
            const stacks = [stack('s1', [cell('a')]), stack('s2', [cell('b')])];
            // Same setters on every render (as FocusContext provides); only
            // the selection changes.
            const base = makeFocus();
            const setSelectedWindowId = vi.mocked(base.setSelectedWindowId);
            const focusFor = (sel: string | null) => ({ ...base, selectedWindowId: sel } as FocusContextType);
            const { result, rerender } = renderHook((p: { focus: FocusContextType }) => useSceneActions({
                performAction,
                focusState: p.focus,
                mainStacks: stacks,
                activeChannel: ch,
                isZoomedOut: false,
            }), { initialProps: { focus: focusFor('s1') } });
            const before = result.current.actions;

            rerender({ focus: focusFor('s2') });
            expect(result.current.actions).toBe(before);

            setSelectedWindowId.mockClear();
            await act(async () => { await result.current.actions.winClose('s2'); });
            // s2 was selected: the selection follows to the left neighbour.
            expect(setSelectedWindowId).toHaveBeenCalledWith('s1');
            // ...and the layout anchor learns s1 is s2's heir (F0631).
            expect(takeSelectionHandOff('s1', ['s1'])).toBe('s2');
        });

        it('a failed removal of the selected stack drops its selection hand-off (F0631)', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch } as never);
            vi.mocked(api.removeStack).mockResolvedValueOnce(null as never);
            const stacks = [stack('s1', [cell('a')]), stack('s2', [cell('b')])];

            const { result } = render(stacks, ch, makeFocus('s2'));
            await act(async () => { await result.current.actions.winClose('s2'); });

            expect(takeSelectionHandOff('s1', ['s1'])).toBeNull();
        });
    });

    describe('newEmptyStackRight', () => {
        it('creates a blank cell and returns the new ids without opening the runners panel', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch });
            // Empty channel → createStack path yields a Standard blank cell.
            vi.mocked(api.createStack).mockResolvedValue({
                main_selection_id: 'stack-new',
                main_stacks: [{ id: 'stack-new', cells: [{ id: 'cell-new' }] }],
            } as unknown as Channel);

            const focus = makeFocus();
            const { result } = render([], ch, focus);
            let created: { stackId: string; cellId: string } | null = null;
            await act(async () => {
                created = await result.current.actions.newEmptyStackRight(null);
            });

            expect(api.createStack).toHaveBeenCalled();
            expect(created).toEqual({ stackId: 'stack-new', cellId: 'cell-new' });
            expect(focus.setSelectedWindowId).toHaveBeenCalledWith('stack-new');
            // Creating a cell never opens its runners panel — that's a
            // separate gesture.
            expect(useCollimateStore.getState().pickSessions['cell-new']).toBeUndefined();
        });

        it('inserts beside a real anchor stack instead of appending', async () => {
            const ch = channel('chan');
            useCollimateStore.setState({ activeChannel: ch });
            const stacks = [stack('s1', [cell('a')])];
            vi.mocked(api.insertBlankCell).mockResolvedValue({
                main_selection_id: 'stack-new',
                main_stacks: [
                    { id: 's1', cells: [{ id: 'a' }] },
                    { id: 'stack-new', cells: [{ id: 'cell-new' }] },
                ],
            } as unknown as Channel);

            const { result } = render(stacks, ch);
            await act(async () => { await result.current.actions.newEmptyStackRight('s1'); });

            expect(api.insertBlankCell).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 's1');
            expect(api.createStack).not.toHaveBeenCalled();
        });
    });
});

describe('useSceneActions — winRotate', () => {
    it('steps the front cell and stops at the first and the last cell, no wrap (C19)', () => {
        const stacks = [stack('s1', [cell('a'), cell('b'), cell('c')])];
        const { result } = render(stacks, channel('chan', stacks));
        act(() => result.current.actions.winRotate('s1', -1));
        expect(result.current.visibleTopIndices.s1 ?? 0).toBe(0);
        const atStart = result.current.visibleTopIndices;
        act(() => result.current.actions.winRotate('s1', -1));
        expect(result.current.visibleTopIndices).toBe(atStart);
        act(() => result.current.actions.winRotate('s1', 1));
        act(() => result.current.actions.winRotate('s1', 1));
        expect(result.current.visibleTopIndices.s1).toBe(2);
        const atEnd = result.current.visibleTopIndices;
        act(() => result.current.actions.winRotate('s1', 1));
        expect(result.current.visibleTopIndices).toBe(atEnd);
        act(() => result.current.actions.winRotate('s1', -1));
        expect(result.current.visibleTopIndices.s1).toBe(1);
    });
});

describe('useSceneActions — fan-out', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        useCollimateStore.setState({
            state: null,
            pendingSelectCellId: null,
        });
    });

    const twoCells = () => [stack('s1', [cell('a'), cell('b')]), stack('s2', [cell('c'), cell('d'), cell('e')])];

    it('fans a 2-cell stack out: columns, range, selection; the Commands pane stays open (K01)', () => {
        useCollimateStore.getState().openCommandPane();
        const focus = makeFocus();
        const stacks = twoCells();
        const { result } = renderFan({ mainStacks: stacks }, focus);
        expect(result.current.displayStacks).toBe(stacks);
        expect(result.current.fanRange).toBeNull();

        act(() => { result.current.setFannedOutStackId('s1'); });

        expect(result.current.fannedOutStackId).toBe('s1');
        expect(result.current.fanRange).toEqual({
            stackId: 's1', name: '', realIdx: 0, start: 0, end: 1, count: 2,
        });
        expect(result.current.displayStacks.map(s => s.id)).toEqual(['fan-a', 'fan-b', 's2']);
        expect(result.current.displayStacks[0]).toMatchObject({
            synthetic: true, sourceCellId: 'a', sourceStackId: 's1', fanIndex: 0, fanCount: 2,
        });
        expect(result.current.displayStacks[2]).toBe(stacks[1]);
        expect(focus.setFocusArea).toHaveBeenCalledWith('main');
        expect(focus.setSelectedWindowId).toHaveBeenCalledWith('s1');
        expect(useCollimateStore.getState().commandPane.open).toBe(true);
        useCollimateStore.getState().closeCommandPane();
        // The anchor names the stack and its front column.
        expect(result.current.fanAnchor).toEqual({ realId: 's1', columnId: 'fan-a' });
    });

    it('is a no-op on a 1-cell stack', () => {
        const focus = makeFocus();
        const stacks = [stack('s1', [cell('a')]), stack('s2', [cell('c'), cell('d')])];
        const { result } = renderFan({ mainStacks: stacks }, focus);

        act(() => { result.current.setFannedOutStackId('s1'); });

        expect(result.current.fannedOutStackId).toBeNull();
        expect(result.current.fanRange).toBeNull();
        expect(result.current.displayStacks).toBe(stacks);
        expect(focus.setSelectedWindowId).not.toHaveBeenCalled();
    });

    it('fanning a second stack replaces the first', () => {
        const { result } = renderFan({ mainStacks: twoCells() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        act(() => { result.current.setFannedOutStackId('s2'); });
        expect(result.current.fannedOutStackId).toBe('s2');
        expect(result.current.displayStacks.map(s => s.id)).toEqual(['s1', 'fan-c', 'fan-d', 'fan-e']);
        expect(result.current.fanRange?.realIdx).toBe(1);
    });

    it('collapseFan clears the fan and a pending move', () => {
        const { result } = renderFan({ mainStacks: twoCells() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        useSpatialStore.getState().setPendingMove({
            type: 'cell', sourceId: 'fan-a', originalIndex: 0, targetIndex: 4, mode: 'insert',
        });
        act(() => { result.current.collapseFan(); });
        expect(result.current.fannedOutStackId).toBeNull();
        expect(result.current.displayStacks.map(s => s.id)).toEqual(['s1', 's2']);
        expect(useSpatialStore.getState().pendingMove).toBeNull();
    });

    it('zooming out renders every stack normally and clears the fan', () => {
        const stacks = twoCells();
        const { result, rerender } = renderFan({ mainStacks: stacks });
        act(() => { result.current.setFannedOutStackId('s1'); });
        rerender({ mainStacks: stacks, isZoomedOut: true });
        expect(result.current.fanRange).toBeNull();
        expect(result.current.displayStacks).toBe(stacks);
        expect(result.current.fannedOutStackId).toBeNull();
        // The last-fanned stack still anchors the layout alias.
        expect(result.current.fanAnchor).toEqual({ realId: 's1', columnId: 'fan-a' });
        // Fanning out while zoomed out is refused.
        act(() => { result.current.setFannedOutStackId('s1'); });
        expect(result.current.fannedOutStackId).toBeNull();
    });

    it('auto-collapses when the stack drops to 1 cell', () => {
        const { result, rerender } = renderFan({ mainStacks: twoCells() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        useSpatialStore.getState().setPendingMove({
            type: 'cell', sourceId: 'fan-a', originalIndex: 0, targetIndex: 4, mode: 'insert',
        });
        const shrunk = [stack('s1', [cell('a')]), stack('s2', [cell('c'), cell('d'), cell('e')])];
        rerender({ mainStacks: shrunk });
        expect(result.current.fanRange).toBeNull();
        expect(result.current.displayStacks).toBe(shrunk);
        expect(result.current.fannedOutStackId).toBeNull();
        expect(useSpatialStore.getState().pendingMove).toBeNull();
    });

    it('auto-collapses when the stack vanishes', () => {
        const { result, rerender } = renderFan({ mainStacks: twoCells() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        const gone = [stack('s2', [cell('c'), cell('d'), cell('e')])];
        rerender({ mainStacks: gone });
        expect(result.current.fanRange).toBeNull();
        expect(result.current.fannedOutStackId).toBeNull();
        expect(result.current.fanAnchor).toBeNull();
    });

    it('selectDisplayId resolves a fan column to its stack + top index', () => {
        const focus = makeFocus();
        const { result } = renderFan({ mainStacks: twoCells() }, focus);
        act(() => { result.current.setFannedOutStackId('s1'); });
        vi.mocked(focus.setSelectedWindowId).mockClear();

        act(() => { result.current.selectDisplayId('fan-b'); });

        expect(focus.setFocusArea).toHaveBeenCalledWith('main');
        expect(focus.setSelectedWindowId).toHaveBeenCalledWith('s1');

        expect(result.current.visibleTopIndices).toEqual({ s1: 1 });
        // A `fan-` id never reaches the focus context.
        for (const call of vi.mocked(focus.setSelectedWindowId).mock.calls) {
            expect(String(call[0])).not.toMatch(/^fan-/);
        }
        // The frame header hands back the real id: selected, no top write.
        act(() => { result.current.selectDisplayId('s1'); });
        expect(focus.setSelectedWindowId).toHaveBeenLastCalledWith('s1');
        expect(result.current.visibleTopIndices).toEqual({ s1: 1 });
    });

    it('selectDisplayId on a real stack preserves its visible front', () => {
        const focus = makeFocus();
        const { result } = renderFan({ mainStacks: twoCells() }, focus);

        act(() => { result.current.selectDisplayId('s2'); });

        expect(focus.setSelectedWindowId).toHaveBeenCalledWith('s2');

        expect(result.current.visibleTopIndices).toEqual({});

        act(() => { result.current.selectDisplayId('nope'); });
        expect(focus.setSelectedWindowId).toHaveBeenCalledTimes(1);
    });

    it('displaySelectedId is the front column while the fanned stack is selected', () => {
        const focus = makeFocus('s1');
        const { result } = renderFan({ mainStacks: twoCells() }, focus);
        expect(result.current.displaySelectedId).toBe('s1');
        act(() => { result.current.setFannedOutStackId('s1'); });
        expect(result.current.displaySelectedId).toBe('fan-a');
        act(() => { result.current.setVisibleTopIndices({ s1: 3 }); });
        expect(result.current.displaySelectedId).toBe('fan-b');
        expect(result.current.fanAnchor).toEqual({ realId: 's1', columnId: 'fan-b' });
    });

    it('applyCellOp sends each op kind as pre-pop move_cell_to args', async () => {
        const ch = channel('chan');
        vi.mocked(api.moveCellTo).mockResolvedValue(ch);
        const { result } = renderFan({ mainStacks: twoCells() });

        let res: Channel | null = null;
        await act(async () => {
            res = await result.current.actions.applyCellOp({ kind: 'reorder', stackId: 's1', realIdx: 0, from: 1, to: 0, cellId: 'b' });
        });
        expect(api.moveCellTo).toHaveBeenLastCalledWith('11111111-1111-4111-8111-111111111111', 's1', 0, 'stack', 'b', 0);
        expect(res).toBe(ch);

        await act(async () => {
            await result.current.actions.applyCellOp({ kind: 'intoStack', srcStackId: 's2', srcCellIdx: 2, targetRealIdx: 0, tgtCellIdx: 1, cellId: 'e' });
        });
        expect(api.moveCellTo).toHaveBeenLastCalledWith('11111111-1111-4111-8111-111111111111', 's2', 0, 'stack', 'e', 1);

        await act(async () => {
            await result.current.actions.applyCellOp({ kind: 'newStack', srcStackId: 's1', srcCellIdx: 1, insertRealIdx: 1, cellId: 'b' });
        });
        expect(api.moveCellTo).toHaveBeenLastCalledWith('11111111-1111-4111-8111-111111111111', 's1', 1, 'insert', 'b', 0);
        expect(result.current.transitionType).toBe('cell');
    });

    it('applyCellOp shows a 409 refusal as "Move refused" and resolves null', async () => {
        const openAlert = vi.fn().mockResolvedValue(undefined);
        useCollimateStore.setState({ openAlert } as never);
        const refusal = Object.assign(new Error('refused'), { response: { status: 409, data: { detail: 'The cell moved — try again' } } });
        vi.mocked(api.moveCellTo).mockRejectedValueOnce(refusal);
        vi.mocked(api.describeApiError).mockReturnValueOnce('The cell moved — try again');
        const caught = vi.fn();
        const { result } = renderHook(() => useSceneActions({
            performAction: (p: Promise<unknown>) => p.catch(e => { caught(e); return null; }),
            focusState: makeFocus(), mainStacks: twoCells(), activeChannel: channel('chan', twoCells()), isZoomedOut: false,
        }));
        let res: Channel | null = channel('x');
        await act(async () => {
            res = await result.current.actions.applyCellOp({ kind: 'intoStack', srcStackId: 's2', srcCellIdx: 2, targetRealIdx: 0, tgtCellIdx: 1, cellId: 'e' });
        });
        expect(res).toBeNull();
        expect(caught).toHaveBeenCalledExactlyOnceWith(refusal);
        expect(openAlert).toHaveBeenCalledExactlyOnceWith({ title: 'Move refused', body: 'The cell moved — try again' });

        // Any other failure is only resynced, never shown as a refusal.
        vi.mocked(api.moveCellTo).mockRejectedValueOnce(Object.assign(new Error('boom'), { response: { status: 500 } }));
        await act(async () => {
            await result.current.actions.applyCellOp({ kind: 'newStack', srcStackId: 's1', srcCellIdx: 1, insertRealIdx: 1, cellId: 'b' });
        });
        expect(openAlert).toHaveBeenCalledTimes(1);
    });

    it('duplicateCellInStack duplicates in place and selects the copy', async () => {
        const after = channel('chan', [stack('s1', [cell('a'), cell('a-copy'), cell('b')]), stack('s2', [cell('c')])]);
        vi.mocked(api.duplicateCellInStack).mockResolvedValue(after);
        const focus = makeFocus();
        const { result } = renderFan({ mainStacks: twoCells() }, focus);

        let out: { stackId: string; cellId: string } | null = null;
        await act(async () => { out = await result.current.actions.duplicateCellInStack('s1', 0); });

        expect(api.duplicateCellInStack).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 's1', 0);
        expect(out).toEqual({ stackId: 's1', cellId: 'a-copy' });
        expect(focus.setSelectedWindowId).toHaveBeenCalledWith('s1');
        expect(result.current.visibleTopIndices).toEqual({ s1: 1 });
    });

    it('duplicateCellInStack returns null when the copy did not land', async () => {
        vi.mocked(api.duplicateCellInStack).mockResolvedValue(null as unknown as Channel);
        const focus = makeFocus();
        const { result } = renderFan({ mainStacks: twoCells() }, focus);
        let out: { stackId: string; cellId: string } | null = { stackId: 'x', cellId: 'y' };
        await act(async () => { out = await result.current.actions.duplicateCellInStack('s1', 0); });
        expect(out).toBeNull();
        expect(focus.setSelectedWindowId).not.toHaveBeenCalled();
    });

    it('winRemoveCell removes the cell by its id and, on the fanned stack, keeps the selected column on the same cell (O04)', async () => {
        vi.mocked(api.removeCellById).mockResolvedValue(channel('chan'));
        const stacks = [stack('s1', [cell('a'), cell('b'), cell('c')])];
        const { result } = renderFan({ mainStacks: stacks });
        act(() => { result.current.setFannedOutStackId('s1'); });
        act(() => { result.current.setVisibleTopIndices({ s1: 2 }); });

        await act(async () => { await result.current.actions.winRemoveCell('s1', 0); });

        expect(api.removeCellById).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 'a');
        expect(result.current.visibleTopIndices).toEqual({ s1: 1 });
    });

    it('winRemoveCell moves the selected column to the left neighbour when the last column goes', async () => {
        vi.mocked(api.removeCellById).mockResolvedValue(channel('chan'));
        const stacks = [stack('s1', [cell('a'), cell('b'), cell('c')])];
        const { result } = renderFan({ mainStacks: stacks });
        act(() => { result.current.setFannedOutStackId('s1'); });
        act(() => { result.current.setVisibleTopIndices({ s1: 2 }); });

        await act(async () => { await result.current.actions.winRemoveCell('s1', 2); });

        expect(result.current.visibleTopIndices).toEqual({ s1: 1 });
    });

    it('winRemoveCell removes by id and leaves the top index alone on a stack that is not fanned (O04)', async () => {
        vi.mocked(api.removeCellById).mockResolvedValue(channel('chan'));
        const stacks = [stack('s1', [cell('a'), cell('b'), cell('c')])];
        const { result } = renderFan({ mainStacks: stacks });
        act(() => { result.current.setVisibleTopIndices({ s1: 2 }); });
        await act(async () => { await result.current.actions.winRemoveCell('s1', 0); });
        expect(api.removeCellById).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 'a');
        expect(result.current.visibleTopIndices).toEqual({ s1: 2 });
    });

    it('winRemoveCell on a Capabilities cell (a card or a capability) hides the stack and removes nothing', async () => {
        const stacks = [{ ...stack('caps', [{ ...cell('d'), builtin: true }, cell('u')]), stack_type: 'capabilities' as const }];
        vi.mocked(api.minimizeStack).mockResolvedValue(channel('chan') as never);
        const { result } = renderFan({ mainStacks: stacks });
        for (const idx of [0, 1]) {
            await act(async () => { await result.current.actions.winRemoveCell('caps', idx); });
        }
        expect(api.removeCellById).not.toHaveBeenCalled();
        expect(api.minimizeStack).toHaveBeenCalledTimes(2);
        expect(api.minimizeStack).toHaveBeenLastCalledWith('11111111-1111-4111-8111-111111111111', 'caps');
    });

    it('winRemoveCell resolves to the result: the channel on success, null when the removal failed', async () => {
        const stacks = [stack('s1', [cell('a'), cell('b')])];
        const { result } = renderFan({ mainStacks: stacks });
        const ch = channel('chan');
        vi.mocked(api.removeCellById).mockResolvedValueOnce(ch);
        let res: unknown;
        await act(async () => { res = await result.current.actions.winRemoveCell('s1', 0); });
        expect(res).toBe(ch);
        // performAction yields null on a failed request.
        vi.mocked(api.removeCellById).mockResolvedValueOnce(null as never);
        await act(async () => { res = await result.current.actions.winRemoveCell('s1', 0); });
        expect(res).toBeNull();
    });

    it('winDuplicateToNewStack duplicates the visible front cell of a rotated stack by its id (O04)', async () => {
        const ch = channel('chan');
        useCollimateStore.setState({ activeChannel: ch } as never);
        vi.mocked(api.duplicateCell).mockResolvedValue({
            ok: true, new_stack_id: 'ns', channel: { ...ch, main_stacks: [stack('ns', [cell('b-copy')])] },
        } as never);
        const stacks = [stack('s1', [cell('a'), cell('b'), cell('c')])];
        const { result } = renderFan({ mainStacks: stacks });
        act(() => { result.current.setVisibleTopIndices({ s1: 4 }); });   // 4 mod 3 = 1 → 'b'

        let out: { stackId: string; cellId: string } | null = null;
        await act(async () => { out = await result.current.actions.winDuplicateToNewStack('s1') ?? null; });

        expect(api.duplicateCell).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 'b', 's1');
        expect(out).toEqual({ stackId: 'ns', cellId: 'b-copy' });
    });

    it('winDuplicateToNewStack duplicates nothing while the manage card is in front, and the front cell after it', async () => {
        const ch = channel('chan');
        useCollimateStore.setState({ activeChannel: ch } as never);
        vi.mocked(api.duplicateCell).mockResolvedValue({
            ok: true, new_stack_id: 'ns', channel: { ...ch, main_stacks: [stack('ns', [cell('u-copy')])] },
        } as never);
        const stacks = [{ ...stack('caps', [{ ...cell('d'), builtin: true }, cell('u')]), stack_type: 'capabilities' as const }];
        const { result } = renderFan({ mainStacks: stacks });
        let out: unknown = 'unset';
        await act(async () => { out = await result.current.actions.winDuplicateToNewStack('caps'); });
        expect(out).toBeNull();
        expect(api.duplicateCell).not.toHaveBeenCalled();
        act(() => { result.current.actions.winSetTopCell('caps', 1); });
        await act(async () => { await result.current.actions.winDuplicateToNewStack('caps'); });
        expect(api.duplicateCell).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', 'u', 'caps');
    });

    it('Capabilities never fans out', () => {
        const stacks = [{ ...stack('caps', [{ ...cell('d'), builtin: true }, cell('u'), cell('v')]), stack_type: 'capabilities' as const }];
        const focus = makeFocus();
        const { result } = renderFan({ mainStacks: stacks }, focus);
        act(() => { result.current.setFannedOutStackId('caps'); });
        expect(result.current.fannedOutStackId).toBeNull();
        expect(result.current.fanRange).toBeNull();
        expect(focus.setSelectedWindowId).not.toHaveBeenCalled();
    });

    it('renameStack and mergeStackIntoStack refuse Capabilities on either side — no request', async () => {
        const caps = { ...stack('caps', [{ ...cell('d'), builtin: true }]), stack_type: 'capabilities' as const };
        const stacks = [caps, stack('s1', [cell('a'), cell('b')])];
        const { result } = renderFan({ mainStacks: stacks });
        act(() => { result.current.actions.renameStack('caps', 'Mine'); });
        await act(async () => {
            await result.current.actions.mergeStackIntoStack('caps', 's1');
            await result.current.actions.mergeStackIntoStack('s1', 'caps');
        });
        expect(api.renameStack).not.toHaveBeenCalled();
        expect(api.moveStackTo).not.toHaveBeenCalled();
    });
});

describe('useSceneActions — front slots on Capabilities', () => {
    const caps = (): Stack => ({ ...stack('caps', [{ ...cell('d'), builtin: true }, cell('u')]), stack_type: 'capabilities' });

    it('winSetTopCell takes a REAL index and stores its slot; winShowManage fronts slot 0; winSetTopSlot stores a slot', () => {
        const stacks = [caps(), stack('s1', [cell('a'), cell('b')])];
        const { result } = renderFan({ mainStacks: stacks });
        act(() => { result.current.actions.winSetTopCell('caps', 1); });
        expect(result.current.visibleTopIndices.caps).toBe(2);
        act(() => { result.current.actions.winSetTopCell('s1', 1); });
        expect(result.current.visibleTopIndices.s1).toBe(1);
        act(() => { result.current.actions.winShowManage('caps'); });
        expect(result.current.visibleTopIndices.caps).toBe(0);
        act(() => { result.current.actions.winSetTopSlot('caps', 1); });
        expect(result.current.visibleTopIndices.caps).toBe(1);
        // A stack a move just made (not in mainStacks yet) is standard.
        act(() => { result.current.actions.winSetTopCell('fresh', 0); });
        expect(result.current.visibleTopIndices.fresh).toBe(0);
    });

    it('winRotate steps from the manage card through every cell and stops at either end', () => {
        const stacks = [caps()];
        const { result } = renderFan({ mainStacks: stacks });
        act(() => result.current.actions.winRotate('caps', -1));
        expect(result.current.visibleTopIndices.caps ?? 0).toBe(0);
        act(() => result.current.actions.winRotate('caps', 1));
        act(() => result.current.actions.winRotate('caps', 1));
        expect(result.current.visibleTopIndices.caps).toBe(2);
        act(() => result.current.actions.winRotate('caps', 1));
        expect(result.current.visibleTopIndices.caps).toBe(2);
        act(() => result.current.actions.winRotate('caps', -1));
        act(() => result.current.actions.winRotate('caps', -1));
        expect(result.current.visibleTopIndices.caps).toBe(0);
    });

    it('a Capabilities stack with only its manage card pages nowhere', () => {
        const empty: Stack = { ...stack('caps', []), stack_type: 'capabilities' };
        const { result } = renderFan({ mainStacks: [empty] });
        const before = result.current.visibleTopIndices;
        act(() => result.current.actions.winRotate('caps', 1));
        expect(result.current.visibleTopIndices).toBe(before);
    });
});

describe('useSceneActions — winSetTopCell', () => {
    it('keeps the visibleTopIndices reference when the index is unchanged', () => {
        const stacks = [stack('s1', [cell('a'), cell('b')])];
        const { result } = renderFan({ mainStacks: stacks });

        act(() => { result.current.actions.winSetTopCell('s1', 1); });
        const first = result.current.visibleTopIndices;
        expect(first.s1).toBe(1);

        act(() => { result.current.actions.winSetTopCell('s1', 1); });
        expect(result.current.visibleTopIndices).toBe(first);

        act(() => { result.current.actions.winSetTopCell('s1', 0); });
        expect(result.current.visibleTopIndices).not.toBe(first);
        expect(result.current.visibleTopIndices.s1).toBe(0);
    });
});

describe('useSceneActions — fan width rules', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        useCollimateStore.setState({ state: null, pendingSelectCellId: null });
        useSpatialStore.setState({ minCellSize: 500, stackWidths: {}, fanCollapse: null, pendingMove: null });
    });

    const stacks = () => [stack('s1', [cell('a'), cell('b')]), stack('s2', [cell('c'), cell('d'), cell('e')])];
    const widths = () => useSpatialStore.getState().stackWidths;
    const persisted = () => JSON.parse(localStorage.getItem('collimate.stackWidths') ?? '{}');

    it('fanning out starts every column at the stack\'s width and drops stale fan widths', () => {
        useSpatialStore.setState({ stackWidths: { s1: 640, 'fan-old': 900, 'fan-a': 333 } });
        const { result } = renderFan({ mainStacks: stacks() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        expect(widths()).toEqual({ s1: 640, 'fan-a': 640, 'fan-b': 640 });
    });

    it('the frame\'s Collapse gives the stack its front column\'s width; other stacks keep theirs', () => {
        useSpatialStore.setState({ stackWidths: { s1: 640, s2: 420 } });
        const { result } = renderFan({ mainStacks: stacks() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        // Select the second column (it becomes the front) and resize it alone.
        act(() => { result.current.selectDisplayId('fan-b'); });
        act(() => { useSpatialStore.getState().setStackWidth('fan-b', 710); });
        expect(widths()).toEqual({ s1: 640, s2: 420, 'fan-a': 640, 'fan-b': 710 });
        act(() => { result.current.collapseFan(); });
        expect(widths()).toEqual({ s1: 710, s2: 420 });
    });

    it('zooming out collapses with the same rule', () => {
        useSpatialStore.setState({ stackWidths: { s1: 640 } });
        const list = stacks();
        const { result, rerender } = renderFan({ mainStacks: list });
        act(() => { result.current.setFannedOutStackId('s1'); });
        act(() => { useSpatialStore.getState().setStackWidth('fan-a', 555); });
        rerender({ mainStacks: list, isZoomedOut: true });
        expect(result.current.fannedOutStackId).toBeNull();
        expect(widths()).toEqual({ s1: 555 });
    });

    it('the stack dropping below 2 cells collapses onto the remaining cell\'s column width', () => {
        const { result, rerender } = renderFan({ mainStacks: stacks() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        // Shared default (no override) → the columns carry none either.
        expect(widths()).toEqual({});
        act(() => { useSpatialStore.getState().setStackWidth('fan-b', 760); });
        rerender({ mainStacks: [stack('s1', [cell('b')]), stacks()[1]] });
        expect(result.current.fannedOutStackId).toBeNull();
        expect(widths()).toEqual({ s1: 760 });
    });

    it('a channel switch collapses the fan by the front cell recorded while it was open', () => {
        useSpatialStore.setState({ stackWidths: { s1: 500 } });
        const { result, rerender } = renderFan({ mainStacks: stacks() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        act(() => { result.current.selectDisplayId('fan-b'); });
        act(() => { useSpatialStore.getState().setStackWidth('fan-b', 650); });
        // The other channel's stacks: s1 is nowhere in them.
        rerender({ mainStacks: [stack('t1', [cell('x')])] });
        expect(result.current.fannedOutStackId).toBeNull();
        expect(widths()).toEqual({ s1: 650 });
        expect(useSpatialStore.getState().fanCollapse).toBeNull();
    });

    it('the persisted widths always hold the open fan collapsed (what a reload finds)', () => {
        useSpatialStore.setState({ stackWidths: { s1: 500, s2: 420 } });
        const { result } = renderFan({ mainStacks: stacks() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        act(() => { result.current.selectDisplayId('fan-b'); });
        act(() => { useSpatialStore.getState().setStackWidth('fan-b', 650); });
        // Live: the fan columns carry their own widths.
        expect(widths()).toEqual({ s1: 500, s2: 420, 'fan-a': 500, 'fan-b': 650 });
        // Stored: the fan already collapsed onto its front column.
        expect(persisted()).toEqual({ s1: 650, s2: 420 });
        // Another front column re-projects the stored map.
        act(() => { result.current.selectDisplayId('fan-a'); });
        expect(persisted()).toEqual({ s1: 500, s2: 420 });
    });

    it('fanning a second stack collapses the first by the rule, then starts the new columns', () => {
        useSpatialStore.setState({ stackWidths: { s2: 420 } });
        const { result } = renderFan({ mainStacks: stacks() });
        act(() => { result.current.setFannedOutStackId('s1'); });
        act(() => { useSpatialStore.getState().setStackWidth('fan-a', 610); });
        act(() => { result.current.setFannedOutStackId('s2'); });
        expect(widths()).toEqual({ s1: 610, s2: 420, 'fan-c': 420, 'fan-d': 420, 'fan-e': 420 });
    });
});
