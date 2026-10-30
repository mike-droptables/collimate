// Front slots: the one index space behind `visibleTopIndices`.
//
// Every stack's front is held as a SLOT. For a standard stack (fan columns
// included) slot k is cell k. The Capabilities stack leads with one extra
// slot, the manage card (MANAGE_SLOT), so its cell k sits at slot k + 1.
// The manage card exists only here, on the client: every server call names
// a cell by id or by a REAL cell index, so every read of `visibleTopIndices`
// goes through these helpers. Pure; no React, no store.
import { isCapabilitiesStack, type Stack } from '@/lib/types';

/** Any stack shape: a full Stack, a layout stack without file lists, a fan column. */
type SlotStack<C = unknown> = { id: string; stack_type?: Stack['stack_type']; cells: readonly C[] };

const mod = (x: number, n: number): number => ((x % n) + n) % n;

/** The Capabilities stack's slot 0: its manage card. */
export const MANAGE_SLOT = 0;

/** Slots ahead of the first cell: 1 for Capabilities (the manage card), else 0. */
export const leadingSlots = (stack?: Pick<Stack, 'stack_type'> | null): number =>
    (isCapabilitiesStack(stack) ? 1 : 0);

/** Every slot the stack pages through: its cells, plus the manage card on
 *  Capabilities. */
export const slotCount = (stack: Pick<SlotStack, 'stack_type' | 'cells'>): number =>
    stack.cells.length + leadingSlots(stack);

/** The stack's front slot, `visibleTopIndices[stack.id]` wrapped into
 *  0..slotCount-1. 0 for a missing stack or one with no slots. */
export const frontSlotOf = (
    stack: SlotStack | null | undefined, vti: Record<string, number> | undefined,
): number => {
    if (!stack) return 0;
    const n = slotCount(stack);
    return n === 0 ? 0 : mod(vti?.[stack.id] ?? 0, n);
};

/** The REAL index of the front cell, or null when the manage card is in
 *  front or the stack has no cells. */
export const frontCellIndexOf = (
    stack: SlotStack | null | undefined, vti: Record<string, number> | undefined,
): number | null => {
    if (!stack) return null;
    const k = frontSlotOf(stack, vti) - leadingSlots(stack);
    return k >= 0 && k < stack.cells.length ? k : null;
};

/** The front cell, or null when the manage card is in front or the stack
 *  has no cells. */
export const frontCellOf = <C>(
    stack: SlotStack<C> | null | undefined,
    vti: Record<string, number> | undefined,
): C | null => {
    const k = frontCellIndexOf(stack, vti);
    return k === null || !stack ? null : stack.cells[k];
};

/** The slot that shows the cell at REAL index `cellIdx`. */
export const slotOfCellIndex = (stack: Pick<Stack, 'stack_type'> | null | undefined, cellIdx: number): number =>
    cellIdx + leadingSlots(stack);

/** True while the Capabilities stack's manage card is in front. */
export const isManageFront = (
    stack: SlotStack | null | undefined, vti: Record<string, number> | undefined,
): boolean => !!stack && leadingSlots(stack) === 1 && frontSlotOf(stack, vti) === MANAGE_SLOT;
