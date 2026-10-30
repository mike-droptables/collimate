import { describe, it, expect } from 'vitest';
import { nearestSlot } from '../useKnobDrag';

const slotAt = (left: number, width: number): HTMLElement => {
    const el = document.createElement('div');
    el.getBoundingClientRect = () => ({ left, width, right: left + width, top: 0, bottom: 10, height: 10, x: left, y: 0, toJSON: () => ({}) });
    return el;
};

describe('useKnobDrag — the pip scrubbers\' one snapping', () => {
    it('snaps to the slot whose centre is nearest the pointer, whatever the spacing (C03) (C06)', () => {
        // The channel scrubber's 15px slots side by side.
        const packed = [slotAt(100, 15), slotAt(115, 15), slotAt(130, 15)];
        expect(nearestSlot(packed, 101)).toBe(0);
        expect(nearestSlot(packed, 117)).toBe(1);
        expect(nearestSlot(packed, 400)).toBe(2);
        // The stack head's slots spread across its track.
        const spread = [slotAt(100, 15), slotAt(140, 15), slotAt(180, 15)];
        expect(nearestSlot(spread, 150)).toBe(1);
        expect(nearestSlot(spread, 0)).toBe(0);
    });

    it('skips slots not laid out, and finds none on an empty track (C03) (C06)', () => {
        expect(nearestSlot([null, slotAt(140, 15)], 100)).toBe(1);
        expect(nearestSlot([], 100)).toBe(-1);
    });
});
