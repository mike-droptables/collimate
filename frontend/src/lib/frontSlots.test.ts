import { describe, expect, it } from 'vitest';
import type { Cell, Stack } from '@/lib/types';
import {
    MANAGE_SLOT, frontCellIndexOf, frontCellOf, frontSlotOf, isManageFront, leadingSlots, slotCount, slotOfCellIndex,
} from './frontSlots';

const cell = (id: string, builtin = false): Cell => ({ id, name: id, files: [], ...(builtin ? { builtin } : {}) });
const standard: Stack = { id: 'W', name: 'Work', cells: [cell('w0'), cell('w1'), cell('w2')] };
const caps: Stack = {
    id: 'K', name: 'Capabilities', stack_type: 'capabilities',
    cells: [cell('d', true), cell('l', true), cell('t', true), cell('u0')],
};
const emptyStandard: Stack = { id: 'E', name: '', cells: [] };

describe('frontSlots', () => {
    it('only Capabilities leads with the manage slot', () => {
        expect(MANAGE_SLOT).toBe(0);
        expect(leadingSlots(caps)).toBe(1);
        expect(leadingSlots(standard)).toBe(0);
        expect(leadingSlots({ stack_type: 'standard' })).toBe(0);
        expect(leadingSlots(undefined)).toBe(0);
        expect(slotCount(caps)).toBe(5);
        expect(slotCount(standard)).toBe(3);
        expect(slotCount(emptyStandard)).toBe(0);
    });

    it('frontSlotOf wraps the stored slot over the slot count', () => {
        expect(frontSlotOf(standard, {})).toBe(0);
        expect(frontSlotOf(standard, { W: 4 })).toBe(1);
        expect(frontSlotOf(standard, { W: -1 })).toBe(2);
        expect(frontSlotOf(caps, { K: 5 })).toBe(0);
        expect(frontSlotOf(caps, { K: -1 })).toBe(4);
        expect(frontSlotOf(emptyStandard, { E: 3 })).toBe(0);
        expect(frontSlotOf(undefined, {})).toBe(0);
        expect(frontSlotOf(standard, undefined)).toBe(0);
    });

    it('frontCellIndexOf is the real index, null on the manage slot or an empty stack', () => {
        expect(frontCellIndexOf(standard, { W: 2 })).toBe(2);
        expect(frontCellIndexOf(caps, { K: 0 })).toBeNull();
        expect(frontCellIndexOf(caps, {})).toBeNull();
        expect(frontCellIndexOf(caps, { K: 1 })).toBe(0);
        expect(frontCellIndexOf(caps, { K: 4 })).toBe(3);
        expect(frontCellIndexOf(emptyStandard, {})).toBeNull();
        expect(frontCellIndexOf(undefined, {})).toBeNull();
    });

    it('frontCellOf names the front cell, null on the manage slot', () => {
        expect(frontCellOf(standard, { W: 1 })?.id).toBe('w1');
        expect(frontCellOf(caps, { K: 4 })?.id).toBe('u0');
        expect(frontCellOf(caps, { K: 1 })?.id).toBe('d');
        expect(frontCellOf(caps, { K: 0 })).toBeNull();
        expect(frontCellOf(emptyStandard, {})).toBeNull();
        expect(frontCellOf(null, {})).toBeNull();
    });

    it('a Capabilities stack with only its cards still has the manage slot', () => {
        const cards: Stack = { ...caps, cells: caps.cells.slice(0, 3) };
        expect(slotCount(cards)).toBe(4);
        expect(isManageFront(cards, {})).toBe(true);
        expect(frontCellOf(cards, { K: 3 })?.id).toBe('t');
        const bare: Stack = { ...caps, cells: [] };
        expect(slotCount(bare)).toBe(1);
        expect(isManageFront(bare, { K: 7 })).toBe(true);
        expect(frontCellIndexOf(bare, { K: 7 })).toBeNull();
    });

    it('isManageFront holds only for Capabilities on slot 0', () => {
        expect(isManageFront(caps, { K: 0 })).toBe(true);
        expect(isManageFront(caps, { K: 5 })).toBe(true);
        expect(isManageFront(caps, { K: 2 })).toBe(false);
        expect(isManageFront(standard, { W: 0 })).toBe(false);
        expect(isManageFront(undefined, {})).toBe(false);
    });

    it('slotOfCellIndex and frontCellIndexOf round-trip every real index', () => {
        for (const stack of [standard, caps]) {
            for (let i = 0; i < stack.cells.length; i++) {
                const slot = slotOfCellIndex(stack, i);
                expect(frontCellIndexOf(stack, { [stack.id]: slot })).toBe(i);
                expect(frontCellOf(stack, { [stack.id]: slot })).toBe(stack.cells[i]);
            }
        }
        expect(slotOfCellIndex(caps, 0)).toBe(1);
        expect(slotOfCellIndex(standard, 0)).toBe(0);
    });

    it('every slot maps to exactly one face: the manage card or one cell', () => {
        const faces = Array.from({ length: slotCount(caps) }, (_, slot) =>
            (isManageFront(caps, { K: slot }) ? 'manage' : frontCellOf(caps, { K: slot })?.id));
        expect(faces).toEqual(['manage', 'd', 'l', 't', 'u0']);
    });
});
