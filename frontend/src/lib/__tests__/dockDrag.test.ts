import { describe, expect, it } from 'vitest';
import { nearestDockSide } from '@/lib/dockDrag';

describe('nearestDockSide (FP-04)', () => {
    const rect = { left: 0, top: 0, right: 400, bottom: 100 };
    const ALL = ['left', 'top', 'right', 'bottom'] as const;

    it('with every side allowed, picks the edge whose triangle holds the point; outside is null', () => {
        expect(nearestDockSide(rect, 10, 50, ALL)).toBe('left');
        expect(nearestDockSide(rect, 390, 50, ALL)).toBe('right');
        expect(nearestDockSide(rect, 200, 10, ALL)).toBe('top');
        expect(nearestDockSide(rect, 200, 90, ALL)).toBe('bottom');
        // Normalised, not absolute: 60px from the left of a 400px row
        // (0.15) beats 30px from the bottom of a 100px one (0.3).
        expect(nearestDockSide(rect, 60, 70, ALL)).toBe('left');
        expect(nearestDockSide(rect, 100, 10, ALL)).toBe('top');
        expect(nearestDockSide(rect, -1, 50, ALL)).toBeNull();
        expect(nearestDockSide(rect, 200, 101, ALL)).toBeNull();
        expect(nearestDockSide({ left: 0, top: 0, right: 0, bottom: 0 }, 0, 0, ALL)).toBeNull();
    });

    it('only an allowed edge wins: the Files pane\'s left or top', () => {
        const files = ['left', 'top'] as const;
        expect(nearestDockSide(rect, 390, 50, files)).toBe('top');
        expect(nearestDockSide(rect, 200, 90, files)).toBe('left');
        expect(nearestDockSide(rect, 10, 50, files)).toBe('left');
        expect(nearestDockSide(rect, 200, 10, files)).toBe('top');
        expect(nearestDockSide(rect, 401, 50, files)).toBeNull();
    });

    it('only an allowed edge wins: the runs pane\'s right or bottom', () => {
        const runs = ['right', 'bottom'] as const;
        expect(nearestDockSide(rect, 10, 50, runs)).toBe('bottom');
        expect(nearestDockSide(rect, 200, 10, runs)).toBe('right');
        expect(nearestDockSide(rect, 390, 50, runs)).toBe('right');
        expect(nearestDockSide(rect, 200, 90, runs)).toBe('bottom');
    });

    it('a tie goes to the side listed first; no sides is null', () => {
        const square = { left: 0, top: 0, right: 100, bottom: 100 };
        expect(nearestDockSide(square, 50, 50, ['left', 'top'] as const)).toBe('left');
        expect(nearestDockSide(square, 50, 50, ['top', 'left'] as const)).toBe('top');
        expect(nearestDockSide(square, 50, 50, ['right', 'bottom'] as const)).toBe('right');
        expect(nearestDockSide(square, 50, 50, [])).toBeNull();
    });
});
