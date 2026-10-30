import { useState, useEffect, useLayoutEffect, useCallback, useMemo, useRef } from 'react';
import * as api from '@/lib/api';
import {flushCellDisplays} from '@/lib/cellDisplayWrites';
import { isCapabilitiesStack, type Stack, type Cell } from '@/lib/types';
import { Channel } from '@/lib/types';
import { FocusContextType } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { cellHasLiveRun } from '@/lib/runningCell';
import { recordSelectionHandOff, dropSelectionHandOff } from '@/lib/stackEntry';
import { MANAGE_SLOT, frontCellIndexOf, frontSlotOf, slotCount, slotOfCellIndex } from '@/lib/frontSlots';
import {
    FanRange, CellOp,
    getFanRange, createDisplayStacksBuilder, displaySelectedIdFor, fanAnchorFor,
    resolveDisplayId, adjustTopIndexAfterRemove, fanWidthsFor, fanCollapseOf,
} from '@/lib/fanOut';

export interface SceneActions {
    /** Step the stack's front slot by `dir` (lib/frontSlots: the manage
     *  card, then the cells, on Capabilities), stopping at either end. */
    winRotate: (sid: string, dir: number) => void;
    updateCell: (c: Cell) => void;
    /** Remove the stack, or hide Capabilities to its top-bar pill.
     *  Resolves false when the removal was refused (a live run) or the
     *  request failed, so the stack is still on the canvas. */
    winClose: (sid: string) => Promise<boolean>;
    /** Remove the cell at REAL index `cellIndex`; on Capabilities, whose
     *  cells are never removed, hide the stack. Resolves to the removal's
     *  result, null when it failed or was not sent. */
    winRemoveCell: (sid: string, cellIndex: number) => Promise<unknown>;
    /** Insert a blank Standard cell in its own new stack, to the right of
     *  the given anchor stack (or appended when there's no anchor). Powers
     *  the top-bar "+" button, the empty-channel CTA and Alt+n. Resolves to
     *  the new stack + cell ids (from the returned channel), or null if the
     *  create didn't land. */
    newEmptyStackRight: (anchorSid: string | null) => Promise<{ stackId: string; cellId: string } | null>;
    /** Merge the focused ≥2-cell stack into a single merged cell (a
     *  folder per input). Selects the new cell. */
    mergeStack: (sid: string) => Promise<void>;
    /** Split the declared options into cells. Selects the first resulting cell. */
    splitCell: (cellId: string, sid: string) => Promise<void>;
    /** Copy the whole stack into a fresh stack one slot to the right.
     *  Powers the stack menu's Duplicate item and STACK_DUPLICATE_STACK. */
    duplicateStack: (sid: string) => void;
    /** Duplicate the focused cell into a new stack; resolves to the new
     *  stack + cell ids (or null when the duplicate didn't land) so the
     *  duplicate+run flow can open the runners panel on the copy. */
    winDuplicateToNewStack: (sid: string) => Promise<{ stackId: string; cellId: string } | null>;
    /** Execute a REAL-space cell move (lib/fanOut.ts resolves display
     *  targets into one) against move_cell_to, naming the cell by id. A
     *  refusal (409: the cell moved, or a run holds it) is shown. Resolves
     *  to the returned channel so the caller can find where the cell
     *  landed (landingOf). */
    applyCellOp: (op: CellOp) => Promise<Channel | null>;
    /** Duplicate the cell at `cellIdx` in place — the copy lands in the
     *  same stack, one slot after the original — then select the stack
     *  with the copy on top (a fanned stack's next column). Resolves to
     *  the copy's ids, or null when the duplicate didn't land. */
    duplicateCellInStack: (sid: string, cellIdx: number) => Promise<{ stackId: string; cellId: string } | null>;
    moveStackToIdx: (sourceStackId: string, targetStackIdx: number) => Promise<void>;
    /** Merge every cell from the source stack into the target stack
     *  (appended at the end) and remove the source stack. Invoked by
     *  the overview's drag when a column is dropped onto another. Never
     *  with Capabilities on either side. */
    mergeStackIntoStack: (sourceStackId: string, targetStackId: string) => Promise<void>;
    /** Rename a stack (Capabilities keeps its name). */
    renameStack: (sid: string, name: string) => void;
    /** Front the cell at REAL index `idx` (stored as its slot). */
    winSetTopCell: (sid: string, idx: number) => void;
    /** Front a slot (the head's pips and knob). */
    winSetTopSlot: (sid: string, slot: number) => void;
    /** Front Capabilities' manage card (its slot 0). */
    winShowManage: (sid: string) => void;
}

interface UseSceneActionsParams {
    performAction: (action: Promise<unknown>) => Promise<unknown>;
    focusState: FocusContextType;
    mainStacks: Stack[];
    activeChannel: Channel | null;
    /** Zoom-out renders every stack normally, so no fan applies there. */
    isZoomedOut: boolean;
}

export function useSceneActions({
    performAction, focusState, mainStacks, activeChannel, isZoomedOut,
}: UseSceneActionsParams) {
    const channelId = activeChannel?.id;
    const { setFocusArea, setSelectedWindowId, setMainFocusMode, selectedWindowId } = focusState;

    const [transitionType, setTransitionType] = useState<'nav' | 'cell'>('nav');
    const [topology, setTopology] = useState<'added' | 'removed' | 'stable'>('stable');
    const [visibleTopIndices, setVisibleTopIndices] = useState<Record<string, number>>({});
    const [mergeNonces, setMergeNonces] = useState<Record<string, number>>({});

    const visibleTopIndicesRef = useRef<Record<string, number>>({});

    const prevStackCount = useRef(0);

    useEffect(() => { visibleTopIndicesRef.current = visibleTopIndices; }, [visibleTopIndices]);

    // ── Fan-out ────────────────────────────────────────────────────────
    // One stack at a time is shown fanned out: its cells side by side as
    // `fan-<cellId>` columns in place of the stack. `fannedOutStackId` is
    // the only state; everything else below derives from it and mainStacks.
    // Invariants: fanRange !== null ⇒ the stack exists with ≥ 2 cells and
    // the overview is shut; displayStacks === mainStacks (same
    // reference) when there is no fan; a `fan-` id never reaches
    // selectedWindowId — the selected column is visibleTopIndices[S].
    const [fannedOutStackId, setFannedOutStackIdRaw] = useState<string | null>(null);
    const fannedOutRef = useRef<string | null>(null);
    useEffect(() => { fannedOutRef.current = fannedOutStackId; }, [fannedOutStackId]);
    // The stack most recently fanned — outlives the fan so the layout alias
    // can anchor S where its front column was on collapse (useStackLayout).
    const [lastFannedStackId, setLastFannedStackId] = useState<string | null>(null);
    // Callbacks below read these instead of closing over props, so they
    // stay referentially stable across renders.
    const mainStacksRef = useRef<Stack[]>(mainStacks);
    useEffect(() => { mainStacksRef.current = mainStacks; }, [mainStacks]);
    const isZoomedOutRef = useRef(isZoomedOut);
    useEffect(() => { isZoomedOutRef.current = isZoomedOut; }, [isZoomedOut]);
    const selectedWindowIdRef = useRef(selectedWindowId);
    useEffect(() => { selectedWindowIdRef.current = selectedWindowId; }, [selectedWindowId]);

    const fanRange = useMemo(
        () => (isZoomedOut ? null : getFanRange(mainStacks, fannedOutStackId)),
        [isZoomedOut, mainStacks, fannedOutStackId],
    );
    // Fan-out collapses itself when it can no longer apply: the stack vanished
    // (removed, minimized, channel switch), dropped below 2 cells, or the
    // overview opened (it always renders every stack normally). fanRange is
    // computed synchronously, so no render ever shows a fan that can't apply;
    // the state resets in the same pass (React re-runs the render at once).
    if (fannedOutStackId && !fanRange) setFannedOutStackIdRaw(null);
    // Every collapse — explicit or automatic — drops a pending keyboard move:
    // its slots are display-space. A move started with no fan is untouched
    // (this only runs when the fanned id changes).
    useEffect(() => {
        if (fannedOutStackId) return;
        const sp = useSpatialStore.getState();
        if (sp.pendingMove) sp.setPendingMove(null);
    }, [fannedOutStackId]);

    // Column reuse: a column wrapping an unchanged cell keeps its object
    // identity across rebuilds, so StackGroup's memo stays cheap.
    const [buildDisplay] = useState(() => createDisplayStacksBuilder());
    const displayStacks = useMemo(() => buildDisplay(mainStacks, fanRange), [buildDisplay, mainStacks, fanRange]);
    const displaySelectedId = useMemo(
        () => displaySelectedIdFor(mainStacks, fanRange, selectedWindowId, visibleTopIndices),
        [mainStacks, fanRange, selectedWindowId, visibleTopIndices],
    );
    const fanAnchor = useMemo(
        () => fanAnchorFor(mainStacks, fanRange?.stackId ?? lastFannedStackId, visibleTopIndices),
        [mainStacks, fanRange, lastFannedStackId, visibleTopIndices],
    );
    const displayStacksRef = useRef<Stack[]>([]);
    useEffect(() => { displayStacksRef.current = displayStacks; }, [displayStacks]);
    const fanRef = useRef<FanRange | null>(null);
    useEffect(() => { fanRef.current = fanRange; }, [fanRange]);

    // Fan width rules (lib/fanOut.ts fanWidthsFor): fanning out starts every
    // column at the stack's width; a collapse gives the stack its front
    // column's width. `fanWidthsStackRef` is the stack whose fan the width
    // overrides were last set up for, so each transition applies once
    // whichever path runs it first: setFannedOutStackId applies it in the
    // same event as the fan change (the first frame already has the right
    // widths), and the layout effect below catches every collapse that
    // happens without it — the overview, the stack dropping below 2 cells or
    // leaving the channel's stacks — before paint. A fanned stack no longer
    // in `stacks` (a channel switch, a minimize) collapses by the front
    // cell the store recorded while it was fanned (spatial fanCollapse,
    // which the layout effect keeps current — and which also keeps the
    // persisted widths collapsed for a reload).
    const fanWidthsStackRef = useRef<string | null>(null);
    const applyFanWidths = useCallback((
        nextId: string | null, stacks: Stack[], topIndices: Record<string, number>,
    ) => {
        const prevId = fanWidthsStackRef.current;
        if (prevId === nextId) return;
        fanWidthsStackRef.current = nextId;
        const sp = useSpatialStore.getState();
        const prevStack = prevId ? stacks.find(s => s.id === prevId) : undefined;
        const collapsing = prevStack
            ? fanCollapseOf(prevStack, topIndices[prevStack.id] ?? 0)
            : (prevId !== null && sp.fanCollapse?.stackId === prevId ? sp.fanCollapse : null);
        const fanning = nextId ? stacks.find(s => s.id === nextId) ?? null : null;
        sp.setFanCollapse(fanning ? fanCollapseOf(fanning, topIndices[fanning.id] ?? 0) : null);
        sp.replaceStackWidths(fanWidthsFor(sp.stackWidths, collapsing, fanning));
    }, []);
    const shownFanId = fanRange?.stackId ?? null;
    useLayoutEffect(() => {
        applyFanWidths(shownFanId, mainStacks, visibleTopIndices);
        const shown = shownFanId ? mainStacks.find(s => s.id === shownFanId) : undefined;
        if (shown) useSpatialStore.getState().setFanCollapse(fanCollapseOf(shown, visibleTopIndices[shown.id] ?? 0));
    }, [applyFanWidths, shownFanId, mainStacks, visibleTopIndices]);

    // Fan a stack out (id) or collapse the fan (null). Fanning selects the
    // stack — its current front cell is the selected column — and replaces
    // any other fan. A pending keyboard move is dropped because its slots
    // are display-space.
    const setFannedOutStackId = useCallback((id: string | null) => {
        const sp = useSpatialStore.getState();
        if (sp.pendingMove) sp.setPendingMove(null);
        setTransitionType('nav');
        if (id !== null) {
            // Capabilities never fans out (lib/fanOut getFanRange).
            const stack = mainStacksRef.current.find(s => s.id === id);
            if (!stack || isCapabilitiesStack(stack) || stack.cells.length < 2 || isZoomedOutRef.current) return;
            setFocusArea('main');
            setSelectedWindowId(id);
            setLastFannedStackId(id);
        }
        applyFanWidths(id, mainStacksRef.current, visibleTopIndicesRef.current);
        setFannedOutStackIdRaw(id);
    }, [setFocusArea, setSelectedWindowId, applyFanWidths]);
    const collapseFan = useCallback(() => setFannedOutStackId(null), [setFannedOutStackId]);

    // Resolve a display id (a real stack id, a `fan-<cellId>` column, or the
    // fanned stack's own id from the frame header) to the real stack plus
    // visible top index, and select it. Every scene-side selection write
    // goes through here so FocusContext only ever holds real stack ids.
    const selectDisplayId = useCallback((id: string) => {
        setFocusArea('main');
        const r = resolveDisplayId(displayStacksRef.current, fanRef.current, id);
        if (!r) return;
        setSelectedWindowId(r.stackId);
        if (r.fanCellIdx !== null) {
            const cellIdx = r.fanCellIdx;

            setVisibleTopIndices(p => (p[r.stackId] === cellIdx ? p : { ...p, [r.stackId]: cellIdx }));
        }
    }, [setFocusArea, setSelectedWindowId]);

    // Track topology (added/removed/stable) based on stack count changes
    useEffect(() => {
        const count = activeChannel?.main_stacks.length || 0;
        if (count > prevStackCount.current) setTopology('added');
        else if (count < prevStackCount.current) setTopology('removed');
        else setTopology('stable');
        prevStackCount.current = count;
    }, [activeChannel?.main_stacks]);

    // Sync initial selectedWindowId from server state
    useEffect(() => {
        if (activeChannel && activeChannel.main_selection_id && !selectedWindowId) {
            setSelectedWindowId(activeChannel.main_selection_id);
        }
    }, [activeChannel, selectedWindowId, setSelectedWindowId]);

    // Reads mainStacks / selectedWindowId through refs so `actions` (which
    // holds this as winClose) keeps its identity across selection changes.
    const handleRemoveStack = useCallback(async (sid: string): Promise<boolean> => {
        if (!channelId) return false;
        const stacks = mainStacksRef.current;
        const stack = stacks.find(s => s.id === sid);
        // Capabilities is hidden to its top-bar pill, never removed. Nothing
        // runs on it and its cells stay in the channel, so nothing waits on
        // a run and no display is flushed.
        const hides = isCapabilitiesStack(stack);
        const store = useCollimateStore.getState();
        if (stack && !hides) {
            // A stack with any live run can't be removed (the run's files
            // are its lock) — backstop for paths that bypass the greyed-out
            // button (keyboard shortcut, programmatic winClose).
            if (stack.cells.some(c => cellHasLiveRun(store, String(c.id)))) return false;
            try {
                await flushCellDisplays(channelId, stack.cells.map(c => String(c.id)));
            } catch (error) {
                void store.openAlert({ title: 'Remove failed', body: String((error as Error).message) });
                return false;
            }
        }
        // No confirm dialog: removal only clears the canvas — every cell
        // goes to the overview's Removed shelf with its history, restorable,
        // so a prompt would guard nothing.
        if (selectedWindowIdRef.current === sid) {
            const idx = stacks.findIndex(s => s.id === sid);
            // Selection follows the stack to the left; fall back to the
            // right when the stack was leftmost. The record lets the layout
            // anchor slide this heir into the vacated slot, where an
            // untouched selection holds still (lib/stackEntry).
            const next = stacks[idx - 1] || stacks[idx + 1];
            if (next) {
                recordSelectionHandOff(sid, next.id);
                setSelectedWindowId(next.id);
            }
        }
        setTransitionType('nav');
        // performAction resolves null when the request failed.
        const res = await performAction(hides ? api.minimizeStack(channelId, sid) : api.removeStack(channelId, sid));
        if (res === null) dropSelectionHandOff(sid);
        else if (stack && !hides) store.dropCellDisplays(channelId, stack.cells.map(c => String(c.id)));
        return res !== null;
    }, [performAction, setSelectedWindowId, channelId]);

    // Front a slot of a stack (lib/frontSlots), writing only a change.
    const setTopSlot = useCallback((sid: string, slot: number) => {
        setVisibleTopIndices(prev => (prev[sid] === slot ? prev : { ...prev, [sid]: slot }));
    }, []);

    const actions: SceneActions = useMemo(() => ({
        // Step the front slot; it stops at the first and the last slot.
        winRotate: (sid: string, dir: number) => {
            const stack = mainStacks.find(s => s.id === sid);
            if (!stack || slotCount(stack) < 2) return;
            setVisibleTopIndices(prev => {
                const current = frontSlotOf(stack, prev);
                const next = Math.min(Math.max(current + dir, 0), slotCount(stack) - 1);
                return next === current ? prev : { ...prev, [sid]: next };
            });
        },
        updateCell: (c: Cell) => {
            if (!channelId) return;
            return api.patchCellMeta(channelId,c.id,{name:c.name,icon:c.icon??null,pinned:c.pinned??null,settings:c.settings??null}).then(patch=>useCollimateStore.getState().patchCell(c.id,patch));
        },
        renameStack: (sid: string, name: string) => {
            if (!channelId) return;
            // Capabilities keeps its name: the overview's column label is
            // read-only for it, and the backend refuses the rename.
            if (isCapabilitiesStack(mainStacks.find(s => s.id === sid))) return;
            performAction(api.renameStack(channelId, sid, name));
        },
        winClose: handleRemoveStack,
        winRemoveCell: async (sid: string, cellIndex: number) => {
            if (!channelId) return null;
            const stack = mainStacks.find(s => s.id === sid);
            // Capabilities' Remove is Hide: its cells leave only by a move.
            if (isCapabilitiesStack(stack)) return handleRemoveStack(sid);
            const nBefore = stack?.cells.length ?? 0;
            const cell = stack?.cells[cellIndex];
            if (!cell) return null;
            setTransitionType('cell');
            try {
                await flushCellDisplays(channelId, [String(cell.id)]);
            } catch (error) {
                void useCollimateStore.getState().openAlert({ title: 'Remove failed', body: String((error as Error).message) });
                return null;
            }
            const res = await performAction(api.removeCellById(channelId, String(cell.id)));
            if (res) useCollimateStore.getState().dropCellDisplays(channelId, [String(cell.id)]);
            // On a fanned stack the selected column is its visible top
            // index: keep it on the same cell, or move it to the right
            // neighbour (the left one when the last column went).
            if (res && sid === fannedOutRef.current) {
                setVisibleTopIndices(p => ({ ...p, [sid]: adjustTopIndexAfterRemove(p[sid] ?? 0, cellIndex, nBefore) }));
            }
            return res;
        },
        // The backend finds the cell by id, pops it, removes an emptied
        // standard source stack and decrements a target index that was past
        // it, so every CellOp index is sent as is (pre-pop, real-space).
        applyCellOp: async (op: CellOp) => {
            if (!channelId) return null;
            setTransitionType('cell');
            const request = op.kind === 'reorder'
                ? api.moveCellTo(channelId, op.stackId, op.realIdx, 'stack', op.cellId, op.to)
                : op.kind === 'intoStack'
                    ? api.moveCellTo(channelId, op.srcStackId, op.targetRealIdx, 'stack', op.cellId, op.tgtCellIdx)
                    : api.moveCellTo(channelId, op.srcStackId, op.insertRealIdx, 'insert', op.cellId, 0);
            // A refusal the user can act on (the cell moved meanwhile, or a
            // run started on it before it reached Capabilities) is shown;
            // performAction then resyncs.
            const refused = (error: unknown): never => {
                if ((error as { response?: { status?: number } }).response?.status === 409) {
                    void useCollimateStore.getState().openAlert({ title: 'Move refused', body: api.describeApiError(error) });
                }
                throw error;
            };
            return (await performAction(request.catch(refused))) as Channel | null;
        },
        duplicateCellInStack: async (sid: string, cellIdx: number) => {
            if (!channelId) return null;
            setTransitionType('cell');
            const res = await performAction(api.duplicateCellInStack(channelId, sid, cellIdx)) as Channel | null;
            const copy = res?.main_stacks?.find(s => s.id === sid)?.cells[cellIdx + 1];
            if (!copy) return null;
            setSelectedWindowId(sid);
            setVisibleTopIndices(p => ({ ...p, [sid]: cellIdx + 1 }));
            return { stackId: sid, cellId: String(copy.id) };
        },
        moveStackToIdx: async (sourceStackId: string, targetStackIdx: number) => {
            if (!channelId) return;
            setTransitionType('nav');
            await performAction(api.moveStackTo(channelId, sourceStackId, targetStackIdx, 'insert'));
        },
        // Zoom-out drag-merge: dragging a column onto another column
        // appends the source's cells to the target and removes the
        // source column. Capabilities refuses the merge on either side
        // (enforced both here and in the backend).
        mergeStackIntoStack: async (sourceStackId: string, targetStackId: string) => {
            if (!channelId) return;
            if (sourceStackId === targetStackId) return;
            const src = mainStacks.find(s => s.id === sourceStackId);
            const tgt = mainStacks.find(s => s.id === targetStackId);
            if (!src || !tgt) return;
            if (isCapabilitiesStack(src) || isCapabilitiesStack(tgt)) return;
            const tgtIdx = mainStacks.findIndex(s => s.id === targetStackId);
            if (tgtIdx < 0) return;
            setTransitionType('cell');
            await performAction(api.moveStackTo(channelId, sourceStackId, tgtIdx, 'stack'));
        },
        // Insert a blank Standard cell in its own new stack (right of the
        // anchor, or appended) and select it. Creation is the whole gesture —
        // the cell lands empty and inert; picking a generator for it is a
        // separate, deliberate step.
        newEmptyStackRight: async (anchorSid: string | null) => {
            if (!channelId) return null;
            setTransitionType('nav');
            setFocusArea('main');
            setMainFocusMode('window');
            // Drop the blank cell's stack right after a real anchor stack. A
            // null — or stale — anchor (an empty channel whose selection still
            // points at a just-deleted stack) falls back to appending a fresh
            // stack, so the top-bar "+" matches the empty-channel "New" CTA.
            const anchor = anchorSid && mainStacks.some(s => s.id === anchorSid) ? anchorSid : null;
            const res = await performAction(
                anchor ? api.insertBlankCell(channelId, anchor) : api.createStack(channelId),
            ) as Channel | null;
            const selId = res?.main_selection_id
                ?? (res?.main_stacks?.length ? res.main_stacks[res.main_stacks.length - 1].id : null);
            if (!selId) return null;
            setSelectedWindowId(selId);
            const stack = res?.main_stacks?.find(s => s.id === selId);
            const cell = stack?.cells[0];
            return stack && cell ? { stackId: stack.id, cellId: String(cell.id) } : null;
        },
        // Merge the focused ≥2-cell stack and split declared options — both via the dedicated
        // endpoints, applying the returned channel + selecting the result,
        // mirroring winDuplicateToNewStack's optimistic pattern.
        mergeStack: async (sid: string) => {
            const stack = mainStacks.find(s => s.id === sid);
            const channel = channelId;
            if (!stack || stack.cells.length < 2 || !channel) return;
            // A live run's files never move (the menu greys this out too).
            if (stack.cells.some(c => cellHasLiveRun(useCollimateStore.getState(), String(c.id)))) return;
            setTransitionType('cell');
            try {
                await flushCellDisplays(channel,stack.cells.map(c=>String(c.id)));
                const res = await api.mergeStack(channel, sid);
                useCollimateStore.getState().dropCellDisplays(channel,stack.cells.map(c=>String(c.id)));
                useCollimateStore.getState().applyOptimisticChannel(res.channel);
                useCollimateStore.getState().requestSelectCell(res.new_cell_id);
            } catch (e) {
                void useCollimateStore.getState().openAlert({title:'Merge failed',body:String((e as Error).message)});
                console.error('merge stack failed', e);
                await useCollimateStore.getState().fetchState();
            }
        },
        splitCell: async (cellId: string, sid: string) => {
            const stack = mainStacks.find(s => s.id === sid);
            const channel = channelId;
            if (!stack || !channel) return;
            if (cellHasLiveRun(useCollimateStore.getState(), cellId)) return;
            setTransitionType('cell');
            try {
                await flushCellDisplays(channel,[cellId]);
                const res = await api.splitCell(channel, cellId);
                const sourceRetained = [...res.channel.main_stacks, ...res.channel.nav_stacks]
                    .some(resultStack => resultStack.cells.some(cell => String(cell.id) === cellId));
                if (!sourceRetained) useCollimateStore.getState().dropCellDisplays(channel,[cellId]);
                useCollimateStore.getState().applyOptimisticChannel(res.channel);
                if(useCollimateStore.getState().activeChannel?.id===channel)await useCollimateStore.getState().hydrateCellDisplays(res.new_cell_ids);
                if (res.new_cell_ids[0]) {
                    useCollimateStore.getState().requestSelectCell(res.new_cell_ids[0]);
                }
            } catch (e) {
                void useCollimateStore.getState().openAlert({title:'Split failed',body:String((e as Error).message)});
                console.error('split cell failed', e);
                await useCollimateStore.getState().fetchState();
            }
        },
        duplicateStack: async (sid: string) => {
            if (!channelId) return;
            setTransitionType('nav');
            const res = await performAction(api.duplicateStack(channelId, sid)) as Channel | null;
            if (res?.main_selection_id) setSelectedWindowId(res.main_selection_id);
        },
        // Duplicate the focused stack's VISIBLE front cell (the rotated top,
        // not cells[0]) into a fresh stack beside it. Applies the returned
        // channel + selects the new stack, mirroring duplicateStack's
        // optimistic pattern. Nothing while the manage card is in front.
        winDuplicateToNewStack: async (sid: string) => {
            const stack = mainStacks.find(s => s.id === sid);
            const frontIdx = frontCellIndexOf(stack, visibleTopIndicesRef.current);
            const topCell = stack && frontIdx !== null ? stack.cells[frontIdx] : undefined;
            const channel = channelId;
            if (!topCell || !channel) return null;
            setTransitionType('cell');
            try {
                const res = await api.duplicateCell(channel, String(topCell.id), sid);
                useCollimateStore.getState().applyOptimisticChannel(res.channel);
                setSelectedWindowId(res.new_stack_id);
                const newStack = res.channel.main_stacks.find(st => st.id === res.new_stack_id);
                const newCell = newStack?.cells[0];
                return newStack && newCell
                    ? { stackId: newStack.id, cellId: String(newCell.id) }
                    : null;
            } catch (e) {
                console.error('duplicate cell failed', e);
                // Resync so a partial mutation can't strand the UI.
                await useCollimateStore.getState().fetchState();
                return null;
            }
        },
        // A real cell index, stored as its slot. A stack not yet in
        // mainStacks (one a move just made) is standard: slot = index.
        winSetTopCell: (sid: string, idx: number) => {
            setTopSlot(sid, slotOfCellIndex(mainStacksRef.current.find(s => s.id === sid), idx));
        },
        winSetTopSlot: setTopSlot,
        winShowManage: (sid: string) => setTopSlot(sid, MANAGE_SLOT),
    }), [performAction, setFocusArea, handleRemoveStack, setTopSlot, setSelectedWindowId, setMainFocusMode, mainStacks, setVisibleTopIndices, channelId]);

    return {
        transitionType, setTransitionType,
        topology,
        fannedOutStackId, setFannedOutStackId, collapseFan,
        fanRange, displayStacks, displaySelectedId, selectDisplayId, fanAnchor,
        visibleTopIndices, setVisibleTopIndices,
        mergeNonces, setMergeNonces,
        visibleTopIndicesRef,
        handleRemoveStack,
        actions,
    };
}
