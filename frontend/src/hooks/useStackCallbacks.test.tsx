import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { PointerEvent } from 'react';
import type { Stack } from '@/lib/types';
import type { FanRange } from '@/lib/fanOut';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { useStackCallbacks } from './useStackCallbacks';

type Inputs = Parameters<typeof useStackCallbacks>[0];
const stack = (id: string): Stack => ({ id, name: id, cells: [{ id: `${id}-cell`, name: id, files: [] }] });
function inputs(stacks: Stack[], overrides: Partial<Inputs> = {}) {
    const actions = {
        winClose: vi.fn(), winRemoveCell: vi.fn(), winSetTopCell: vi.fn(), winSetTopSlot: vi.fn(), winShowManage: vi.fn(), updateCell: vi.fn(),
        renameStack: vi.fn(), duplicateCellInStack: vi.fn(), winDuplicateToNewStack: vi.fn(),
        duplicateStack: vi.fn(), mergeStack: vi.fn(), splitCell: vi.fn(),
        moveStackToIdx: vi.fn().mockResolvedValue(undefined),
    };
    const props: Inputs = {
        stacks, actions: actions as unknown as Inputs['actions'], fan: null, isZoomedOut: true,
        onSelectStack: vi.fn(), onFanOut: vi.fn(), onZoomToCell: vi.fn(),
        handleStackDragStart: vi.fn(), handleCellDragStart: vi.fn(),
        handleCenterPanStart: vi.fn(), handleCenterPanMove: vi.fn(), handleCenterPanEnd: vi.fn(),
        ...overrides,
    };
    const hook = renderHook((p: Inputs) => ({ callbacks: useStackCallbacks(p) }), { initialProps: props });
    return { ...hook, props, actions };
}
afterEach(cleanup);

it('routes every real-stack command to its selected subject and preserves gesture arguments (F0587)', () => {
    const { result, props, actions } = inputs([stack('left'), stack('target'), stack('right')]);
    const c = result.current.callbacks.get('target')!;
    const e = { pointerId: 7 } as PointerEvent;
    const rect = new DOMRect(10, 20, 30, 40);
    const cell = stack('target').cells[0];
    const before = useSpatialStore.getState().centerRequested;
    act(() => {
        c.onSelect(2); c.onClose(); c.onRemoveCell(1); c.onSetTopSlot!(3);
        c.onUpdate(cell); c.onRenameStack!('renamed'); c.onFanOut!();
        c.onStackDragStart!(e); c.onCellDragStart!(1, cell.id, e, rect); c.onZoomToCell!(2);
        c.onCenterStack!(); c.onCenterPanStart!(e); c.onCenterPanMove!(e); c.onCenterPanEnd!();
        c.onDuplicateToNewStack!(); c.onDuplicateStack!(); c.onMergeStack!();
    });
    expect(props.onSelectStack).toHaveBeenCalledWith('target', 2);
    // onSelect names a REAL cell index; the pips a slot.
    expect(actions.winSetTopCell.mock.calls).toEqual([['target', 2]]);
    expect(actions.winSetTopSlot).toHaveBeenCalledExactlyOnceWith('target', 3);
    // Only Capabilities pages to a manage card or shows a cell from it.
    expect(c.onShowCell).toBeUndefined();
    expect(c.onShowManage).toBeUndefined();
    expect(actions.winClose).toHaveBeenCalledExactlyOnceWith('target');
    expect(actions.winRemoveCell).toHaveBeenCalledExactlyOnceWith('target', 1);
    expect(actions.updateCell).toHaveBeenCalledExactlyOnceWith(cell);
    expect(actions.renameStack).toHaveBeenCalledExactlyOnceWith('target', 'renamed');
    expect(props.onFanOut).toHaveBeenCalledExactlyOnceWith('target');
    expect(props.handleStackDragStart).toHaveBeenCalledExactlyOnceWith('target', e);
    expect(props.handleCellDragStart).toHaveBeenCalledExactlyOnceWith('target', 1, cell.id, e, rect);
    expect(props.onZoomToCell).toHaveBeenCalledExactlyOnceWith('target', 2);
    expect(props.handleCenterPanStart).toHaveBeenCalledExactlyOnceWith(e, 'target');
    expect(props.handleCenterPanMove).toHaveBeenCalledExactlyOnceWith(e);
    expect(props.handleCenterPanEnd).toHaveBeenCalledTimes(1);
    expect(useSpatialStore.getState().centerRequested).toBe(before + 1);
    expect(actions.winDuplicateToNewStack).toHaveBeenCalledExactlyOnceWith('target');
    expect(actions.duplicateStack).toHaveBeenCalledExactlyOnceWith('target');
    expect(actions.mergeStack).toHaveBeenCalledExactlyOnceWith('target');
});

it('bounds real-stack moves and only offers zoom gestures in the overview (F0587)', async () => {
    const stacks = [stack('left'), stack('middle'), stack('right')];
    const { result, props, actions, rerender } = inputs(stacks);
    await act(async () => {
        await result.current.callbacks.get('left')!.onMoveStack!(-1);
        await result.current.callbacks.get('right')!.onMoveStack!(1);
        await result.current.callbacks.get('middle')!.onMoveStack!(1);
        await result.current.callbacks.get('middle')!.onMoveStack!(-1);
    });
    expect(actions.moveStackToIdx.mock.calls).toEqual([['middle', 3], ['middle', 0]]);
    rerender({ ...props, isZoomedOut: false, onFanOut: undefined, onZoomToCell: undefined });
    const c = result.current.callbacks.get('middle')!;
    act(() => { c.onSelect(); c.onSelect(1); });
    expect(actions.winSetTopCell).not.toHaveBeenCalled();
    expect(props.onSelectStack).toHaveBeenCalledWith('middle', 1);
    expect(c.onFanOut).toBeUndefined();
    expect(c.onStackDragStart).toBeUndefined();
    expect(c.onZoomToCell).toBeUndefined();
});

it('keeps fan display selection separate from real-cell commands; a fan column has no move arrows (F0587) (C08)', () => {
    const column = { ...stack('display'), synthetic: true, sourceStackId: 'source', fanIndex: 1 };
    const fan = { stackId: 'source', start: 0, count: 2 } as FanRange;
    const { result, props, actions, rerender } = inputs([column], { fan });
    const c = result.current.callbacks.get('display')!;
    const e = { pointerId: 8 } as PointerEvent;
    const cell = column.cells[0];
    const rect = new DOMRect(1, 2, 3, 4);
    const before = useSpatialStore.getState().centerRequested;
    act(() => {
        c.onSelect(); c.onClose(); c.onSetTopSlot!(0); c.onRemoveCell(0); c.onUpdate(cell);
        c.onRenameStack!('renamed'); c.onCellDragStart!(0, cell.id, e, rect);
        c.onCenterStack!(); c.onCenterPanStart!(e); c.onDuplicateToNewStack!();
    });
    expect(props.onSelectStack).toHaveBeenCalledWith('display');
    expect(actions.winClose).not.toHaveBeenCalled();
    expect(actions.winSetTopCell).not.toHaveBeenCalled();
    expect(actions.winSetTopSlot).not.toHaveBeenCalled();
    expect(actions.winRemoveCell).toHaveBeenCalledExactlyOnceWith('source', 1);
    expect(actions.updateCell).toHaveBeenCalledExactlyOnceWith(cell);
    expect(actions.renameStack).toHaveBeenCalledExactlyOnceWith('source', 'renamed');
    expect(actions.duplicateCellInStack).toHaveBeenCalledExactlyOnceWith('source', 1);
    expect(props.handleCellDragStart).toHaveBeenCalledExactlyOnceWith('display', 0, cell.id, e, rect);
    expect(props.handleCenterPanStart).toHaveBeenCalledExactlyOnceWith(e, 'display');
    expect(useSpatialStore.getState().centerRequested).toBe(before + 1);
    // The fan's bracket stands in the head's place; the Move tool reorders.
    expect(c.onMoveStack).toBeUndefined();
    rerender({ ...props, fan: null, stacks: [{ ...column, fanIndex: undefined }] });
    act(() => { result.current.callbacks.get('display')!.onRemoveCell(0); });
    expect(actions.winRemoveCell).toHaveBeenLastCalledWith('source', 0);
});

it('Capabilities keeps its name and its one stack: it pages to its manage card and shows its cells by REAL index', () => {
    const caps: Stack = { ...stack('caps'), stack_type: 'capabilities' };
    const { result, actions } = inputs([caps]);
    const c = result.current.callbacks.get('caps')!;
    expect(c.onRenameStack).toBeUndefined();
    expect(c.onFanOut).toBeUndefined();
    expect(c.onDuplicateStack).toBeUndefined();
    expect(c.onMergeStack).toBeUndefined();
    act(() => { c.onShowCell!(0); c.onShowManage!(); c.onSetTopSlot!(1); c.onClose(); });
    expect(actions.winSetTopCell).toHaveBeenCalledExactlyOnceWith('caps', 0);
    expect(actions.winShowManage).toHaveBeenCalledExactlyOnceWith('caps');
    expect(actions.winSetTopSlot).toHaveBeenCalledExactlyOnceWith('caps', 1);
    expect(actions.winClose).toHaveBeenCalledExactlyOnceWith('caps');
});
