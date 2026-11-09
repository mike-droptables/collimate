import type { Cell, Stack } from '@/lib/types';
import type { PendingMoveState } from '@/stores/useSpatialStore';
import {
    OVERVIEW_CARD_PITCH, cardSpan, overviewCardTop, slotGhostCenterX, type StackLayoutResult,
} from '@/hooks/useStackLayout';
import { OVERVIEW_CARD_HEIGHT, OVERVIEW_GAP } from '@/stores/collimate/overviewSlice';
import { frontCellIndexOf, slotOfCellIndex } from './frontSlots';

// An overview column shows its stack's cells in the stack's own order (the
// order the stack view's pips and Alt+Up/Down step through). A card held
// over a column lands where the pointer points: above its first card,
// between two, or below its last.

/** A card an overview column shows: its cell id and its height (the
 *  journal-style card can stand taller than the rest). */
export interface ColumnCard {
    id: string;
    height: number;
}

/** The slot among a column's `shown` cards (Only connected may hide some)
 *  where stack position `index` of `cells` falls: the shown cards before it. */
export function shownSlotOf(index: number, cells: readonly string[], shown: readonly string[]): number {
    return shown.filter(id => cells.indexOf(id) < index).length;
}

/** The shown slot the pointer at world `worldY` points to over a column of
 *  cards `heights` tall, top down: the number of cards whose midpoint, as
 *  the column shows them, lies above the pointer. `gap`: the slot the
 *  column's marked gap stands in now (one card pitch), or null. Measuring
 *  against the column as shown is the hysteresis: once the gap moves past
 *  a card, that card moves the other way, so the pointer has to travel a
 *  whole card back before the gap returns, and a pointer resting on a
 *  boundary never flickers between two slots. */
export function overviewPointedSlot(worldY: number, heights: readonly number[], gap: number | null): number {
    let top = overviewCardTop(0);
    for (let i = 0; i < heights.length; i++) {
        if (gap === i) top += OVERVIEW_CARD_PITCH;
        if (top + heights[i] / 2 >= worldY) return i;
        top += heights[i] + OVERVIEW_GAP;
    }
    return heights.length;
}

/** Where a card held at world `worldY` over an overview column lands: its
 *  position in the column's stack once it is in. `cells`: the stack's cell
 *  ids in order, the held card left out; `shown`: the cards the column
 *  shows, top down; `current`: the position the column's gap shows now, or
 *  null. Below the last shown card is right after it. */
export function overviewColumnLanding(
    worldY: number, cells: readonly string[], shown: readonly ColumnCard[], current: number | null,
): number {
    const ids = shown.map(card => card.id);
    const gap = current === null ? null : shownSlotOf(current, cells, ids);
    const slot = overviewPointedSlot(worldY, shown.map(card => card.height), gap);
    if (slot < ids.length) return cells.indexOf(ids[slot]);
    return ids.length > 0 ? cells.indexOf(ids[ids.length - 1]) + 1 : cells.length;
}

/** The board's arrangement: which cells sit in which columns, in order. A
 *  drag's drop preview (its gap, its slot) is drawn against one
 *  arrangement and means nothing on another: once a drop's result lands
 *  the preview is gone in that same frame, or it would open its gap a
 *  second time in a board that already holds the moved card. */
export function boardSignature(stacks: readonly Stack[]): string {
    return stacks.map(stack => `${stack.id}:${stack.cells.map(cell => cell.id).join(',')}`).join('|');
}

/** The marked gap a card opens between overview columns: a card-sized
 *  slot at the head of the board, at the column position it lands in. */
export interface SlotPreview {
    x: number;
    y: number;
    width: number;
    height: number;
    /** The gap lies inside the fanned stack's border (main view only). */
    inFan: boolean;
    /** The card being moved, for a board card. */
    cell?: Cell;
}

/** The overview's gap for a new column at `slot`, `width` wide (a board
 *  card's drag and a shelf card's alike). */
export function overviewInsertPreview(
    slot: number, positions: number[], widths: number[], width: number, GAP: number,
): SlotPreview {
    const x = slotGhostCenterX(slot, positions, widths, width, GAP) - width / 2;
    return { x, y: overviewCardTop(0), width, height: OVERVIEW_CARD_HEIGHT, inFan: false };
}

interface SceneDropPreviewInputs {
    dragType: 'cell' | 'stack' | null;
    dragStackId: string | null;
    dragCellIndex: number | undefined;
    pendingMove: PendingMoveState | null;
    stacks: Stack[];
    positions: number[];
    widths: number[];
    naturalWidths: number[];
    insertTargetIdx: number | null;
    insertInFan: boolean;
    dropTargetIdx: number | null;
    cardFrame: StackLayoutResult['cardFrame'];
    isZoomedOut: boolean;
    visibleTopIndices: Record<string, number> | undefined;
    GAP: number;
}

/** Resolve a pointer or keyboard cell drop into the target card's world
 *  geometry. In the overview a drop onto a column draws nothing here: the
 *  column opens its own marked gap where the cell lands (StackGroup). */
export function resolveSceneDropPreview({
    dragType, dragStackId, dragCellIndex, pendingMove, stacks, positions, widths, naturalWidths,
    insertTargetIdx, insertInFan, dropTargetIdx,
    cardFrame, isZoomedOut, visibleTopIndices, GAP,
}: SceneDropPreviewInputs): SlotPreview | null {
    let isCell = false;
    let sourceStackId = '';
    let cellIndex = 0;
    let targetIns: number | null = null;
    let targetDrop: number | null = null;
    // Gap targets only: the gap lies inside the fanned stack's border.
    let inFan = false;

    if (dragType === 'cell') {
        isCell = true;
        sourceStackId = dragStackId ?? '';
        cellIndex = dragCellIndex ?? 0;
        targetIns = insertTargetIdx;
        targetDrop = dropTargetIdx;
        inFan = insertTargetIdx !== null && insertInFan;
    } else if (pendingMove?.type === 'cell') {
        isCell = true;
        sourceStackId = pendingMove.sourceId;
        // The moving cell is the source's front cell (a pending move never
        // starts on the manage card, which is no cell).
        cellIndex = frontCellIndexOf(stacks.find(s => s.id === pendingMove.sourceId), visibleTopIndices) ?? 0;
        if (pendingMove.mode === 'insert') {
            targetIns = pendingMove.targetIndex / 2;
            inFan = !!pendingMove.inFan;
        } else {
            targetDrop = (pendingMove.targetIndex - 1) / 2;
        }
    }

    if (!isCell) return null;

    const sourceIdx = stacks.findIndex(s => s.id === sourceStackId);
    if (sourceIdx < 0) return null;
    const sourceStack = stacks[sourceIdx];
    if (sourceStack.cells.length === 0) return null;

    cellIndex = Math.min(cellIndex, sourceStack.cells.length - 1);
    const cell = sourceStack.cells[cellIndex];
    if (!cell) return null;

    const cellWidth = naturalWidths[sourceIdx];

    if (targetIns !== null) {
        if (isZoomedOut) return { ...overviewInsertPreview(targetIns, positions, widths, cellWidth, GAP), cell };
        const ghostCenterX = slotGhostCenterX(targetIns, positions, widths, cellWidth, GAP);
        const card = cardSpan(cardFrame);
        const previewH = Math.min(cellWidth, card.height);
        const y = card.top + Math.max(0, (card.height - previewH) / 2);
        return { cell, x: ghostCenterX - cellWidth / 2, y, width: cellWidth, height: previewH, inFan };
    } else if (targetDrop !== null) {
        const targetWidth = widths[targetDrop];
        if (isZoomedOut) return null;
        const card = cardSpan(cardFrame);
        const previewH = Math.min(targetWidth, card.height);
        const y = card.top + Math.max(0, (card.height - previewH) / 2);
        return { cell, x: positions[targetDrop] - targetWidth / 2, y, width: targetWidth, height: previewH, inFan: false };
    }
    return null;
}

/** The front slot a column shows (lib/frontSlots): its stored slot, or,
 *  while a pending keyboard cell move lifts its front cell off a column of
 *  2+ cells, the slot of the cell after it (wrapping), so the column never
 *  reads as empty under the move. */
export function pendingMovePeekSlot(
    stack: Pick<Stack, 'id' | 'stack_type' | 'cells'>,
    pendingMove: PendingMoveState | null,
    visibleTopIndices: Record<string, number> | undefined,
): number {
    const stored = visibleTopIndices?.[stack.id] ?? 0;
    if (pendingMove?.type !== 'cell' || pendingMove.sourceId !== stack.id || stack.cells.length < 2) return stored;
    const front = frontCellIndexOf(stack, visibleTopIndices);
    return front === null ? stored : slotOfCellIndex(stack, (front + 1) % stack.cells.length);
}
