import { useMemo } from 'react';
import { isCapabilitiesStack, type Stack } from '@/lib/types';
import type { StackCallbacks } from '@/components/StackGroup';
import type { SceneActions } from './useSceneActions';
import type { useStackDrag } from './useStackDrag';
import type { useStackCamera } from './useStackCamera';
import { useSpatialStore, withSettlingMove } from '@/stores/useSpatialStore';
import { displayToReal, realStackCount, type FanRange } from '@/lib/fanOut';

interface StackCallbacksInputs {
    stacks: Stack[];
    actions: SceneActions;
    fan: FanRange | null;
    isZoomedOut: boolean;
    onFanOut?: (stackId: string) => void;
    onZoomToCell?: (stackId: string, cellIndex: number) => void;
    onSelectStack: (displayId: string, displayCellIdx?: number) => void;
    handleStackDragStart: ReturnType<typeof useStackDrag>['handleStackDragStart'];
    handleCellDragStart: ReturnType<typeof useStackDrag>['handleCellDragStart'];
    handleCenterPanStart: ReturnType<typeof useStackCamera>['handleCenterPanStart'];
    handleCenterPanMove: ReturnType<typeof useStackCamera>['handleCenterPanMove'];
    handleCenterPanEnd: ReturnType<typeof useStackCamera>['handleCenterPanEnd'];
}

/** The display-to-real operation boundary, with stable per-column callback identities. */
export function useStackCallbacks({
    stacks, actions, fan, isZoomedOut, onFanOut, onZoomToCell, onSelectStack,
    handleStackDragStart, handleCellDragStart, handleCenterPanStart, handleCenterPanMove, handleCenterPanEnd,
}: StackCallbacksInputs) {
    const setCenterRequested = useSpatialStore(s => s.setCenterRequested);

    // Stable per-stack callback bundle. Rebuilt only when its deps change.
    // `actions` (rebuilt when mainStacks changes)
    // and the drag start handlers (which depend on `stacks`) change only on
    // server pushes, the same events that change `stacks`; `onZoomToCell`
    // is a memoized StackApp callback; `isZoomedOut` is stable unless the
    // user toggles it. During a drag
    // pointermove none of these change, so the Map reference is stable and
    // every StackGroup's `callbacks` prop is the same reference, letting
    // the memo bail in one quick referential check.
    //
    // This map is the display → real boundary: `stacks` is the display
    // list, and a fan column's callbacks act on its REAL stack S and cell
    // index j. SceneActions only ever sees real ids.
    const stackCallbacks = useMemo(() => {
        const map = new Map<string, StackCallbacks>();
        const noop = () => {};
        for (let i = 0; i < stacks.length; i++) {
            const stack = stacks[i];
            const id = stack.id;
            const stackIdx = i;
            if (stack.synthetic && stack.sourceStackId !== undefined) {
                const S = stack.sourceStackId;
                const j = stack.fanIndex ?? 0;
                const displayId = id;
                map.set(id, {
                    onSelect: () => onSelectStack(displayId),
                    // A column's burn must never remove S.
                    onClose: noop,
                    onRemoveCell: () => actions.winRemoveCell(S, j),
                    onSetTopSlot: noop,
                    onUpdate: (c) => actions.updateCell(c),
                    onRenameStack: (name) => actions.renameStack(S, name),
                    onCellDragStart: (cellIdx, cellId, e, sourceRect) => handleCellDragStart(displayId, cellIdx, cellId, e, sourceRect),
                    onCenterStack: () => {
                        onSelectStack(displayId);
                        setCenterRequested((n: number) => n + 1);
                    },
                    onCenterPanStart: (e) => {
                        onSelectStack(displayId);
                        handleCenterPanStart(e, displayId);
                    },
                    onCenterPanMove: handleCenterPanMove,
                    onCenterPanEnd: handleCenterPanEnd,
                    // In place: the copy becomes the next column.
                    onDuplicateToNewStack: () => actions.duplicateCellInStack(S, j),
                });
                continue;
            }
            // Capabilities keeps its name and its one stack: no rename, fan
            // out, duplicate or merge. Its manage card pages like a cell.
            const isCapabilitiesColumn = isCapabilitiesStack(stack);
            map.set(id, {
                // `idx`: a REAL cell index.
                onSelect: (idx) => {
                    onSelectStack(id, idx);
                    if (isZoomedOut && idx !== undefined) {
                        actions.winSetTopCell(id, idx);
                    }
                },
                onClose: () => actions.winClose(id),
                onRemoveCell: (cellIndex: number) => actions.winRemoveCell(id, cellIndex),
                onSetTopSlot: (slot) => actions.winSetTopSlot(id, slot),
                onShowCell: isCapabilitiesColumn ? (cellIndex) => actions.winSetTopCell(id, cellIndex) : undefined,
                onShowManage: isCapabilitiesColumn ? () => actions.winShowManage(id) : undefined,
                onUpdate: (c) => actions.updateCell(c),
                onRenameStack: isCapabilitiesColumn ? undefined : (name) => actions.renameStack(id, name),
                onFanOut: onFanOut && !isCapabilitiesColumn ? () => onFanOut(id) : undefined,
                onStackDragStart: isZoomedOut ? (e) => handleStackDragStart(id, e) : undefined,
                // Always wired: zoom-out uses the whole card as a drag handle,
                // the main view uses the per-cell grab icon (gated in StackItem).
                onCellDragStart: (cellIdx, cellId, e, sourceRect) => handleCellDragStart(id, cellIdx, cellId, e, sourceRect),
                onZoomToCell: isZoomedOut && onZoomToCell ? (cellIdx) => onZoomToCell(id, cellIdx) : undefined,
                onMoveStack: async (dir) => {
                    // Real indices: a fanned group counts as one stack and
                    // moves as one unit. Backend `move_stack_to` with
                    // mode=insert pops the stack first, then inserts at the
                    // target slot (with an internal decrement when target >
                    // src). To move "one slot right" we need target =
                    // realIdx + 2 so the post-pop adjust lands us at
                    // realIdx + 1. Leftward moves are straightforward.
                    const realIdx = displayToReal(fan, stackIdx).realIdx;
                    const count = realStackCount(stacks.length, fan);
                    if (dir > 0 && realIdx >= count - 1) return;
                    if (dir < 0 && realIdx <= 0) return;
                    const target = dir > 0 ? realIdx + 2 : realIdx - 1;
                    // Keep the moving stack (content + surfaces) above the
                    // neighbour it slides past for the whole settle spring —
                    // same signal the keyboard move paths set. Token-guarded
                    // so rapid repeat clicks don't clear each other's bumps.
                    await withSettlingMove(id, () => actions.moveStackToIdx(id, target));
                },
                onCenterStack: () => {
                    onSelectStack(id);
                    setCenterRequested((n: number) => n + 1);
                },
                // Drag the stack head left/right to slide the viewport so
                // this stack lands where you drop it (a pan, not a reorder).
                // Selecting it on grab means the post-pan tracking effect keeps
                // *this* stack on-screen rather than yanking back to another.
                onCenterPanStart: (e) => {
                    onSelectStack(id);
                    // The id names the snap-target subject (edge-align glow).
                    handleCenterPanStart(e, id);
                },
                onCenterPanMove: handleCenterPanMove,
                onCenterPanEnd: handleCenterPanEnd,
                onDuplicateToNewStack: () => actions.winDuplicateToNewStack(id),
                onDuplicateStack: isCapabilitiesColumn ? undefined : () => actions.duplicateStack(id),
                // The stack menu's structural edit.
                onMergeStack: isCapabilitiesColumn ? undefined : () => actions.mergeStack(id),
            });
        }
        return map;
    }, [stacks, actions, fan, isZoomedOut, onFanOut, onZoomToCell, handleStackDragStart, handleCellDragStart, handleCenterPanStart, handleCenterPanMove, handleCenterPanEnd, setCenterRequested, onSelectStack]);
    return stackCallbacks;
}
