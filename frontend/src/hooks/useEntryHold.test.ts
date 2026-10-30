import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type { MotionValue } from 'motion/react';
import { useEntryHold, type EntryItem } from './useEntryHold';
import { ENTRY_HOLD_MAX_MS, ENTRY_HOLD_LIMIT_MS } from '@/lib/stackEntry';

// A 1600px view at zoom 1: with the camera at x the visible band is
// [x − 786, x + 786]. Stacks are 500 wide at a 514 pitch.
const cam = { x: 0, animating: false };
const cameraXMV = { get: () => cam.x, isAnimating: () => cam.animating } as unknown as MotionValue<number>;
const zoomMV = { get: () => 1, isAnimating: () => false } as unknown as MotionValue<number>;
const settling = { value: false };
const item = (id: string, x: number, synthetic = false): EntryItem => ({ id, x, computedWidth: 500, synthetic });

const baseProps = {
    sceneSwapNonce: 0,
    cameraXMV,
    zoomMV,
    isCameraSettling: () => settling.value || cam.animating,
    viewWidth: 1600,
    gap: 14,
};
const A = item('a', -514);
const B = item('b', 0);

const frames = (n: number) => act(() => { for (let i = 0; i < n; i++) vi.advanceTimersByTime(16); });

beforeEach(() => {
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame', 'performance', 'setTimeout'] });
    cam.x = 0; cam.animating = false; settling.value = false;
});
afterEach(() => vi.useRealTimers());

describe('useEntryHold', () => {
    it('the starting population is not an entry', () => {
        const { result } = renderHook((p) => useEntryHold(p), { initialProps: { ...baseProps, items: [A, B] } });
        expect(result.current.size).toBe(0);
    });

    it('a stack landing in view with the camera at rest is never held', () => {
        const { result, rerender } = renderHook((p) => useEntryHold(p), { initialProps: { ...baseProps, items: [A, B] } });
        rerender({ ...baseProps, items: [A, B, item('n', 514)] });   // [264, 764]
        expect(result.current.has('n')).toBe(false);
    });

    it('an off-screen slot is held through the pan and released once the camera settles on it', () => {
        const { result, rerender } = renderHook((p) => useEntryHold(p), { initialProps: { ...baseProps, items: [A, B] } });
        const N = item('n', 1028);                        // [778, 1278] — off the right edge
        rerender({ ...baseProps, items: [A, B, N] });
        expect(result.current.has('n')).toBe(true);
        // The add path selects it: the camera pans right.
        cam.animating = true;
        cam.x = 300;
        frames(3);
        expect(result.current.has('n')).toBe(true);
        cam.x = 514;                                      // slot now in view, still moving
        frames(3);
        expect(result.current.has('n')).toBe(true);
        cam.animating = false;                            // settled
        frames(1);
        expect(result.current.has('n')).toBe(false);
    });

    it('a pending camera target (not yet animating) still holds it', () => {
        const { result, rerender } = renderHook((p) => useEntryHold(p), { initialProps: { ...baseProps, items: [A, B] } });
        settling.value = true;
        rerender({ ...baseProps, items: [A, B, item('n', 514)] });   // in view, but the camera is about to move
        expect(result.current.has('n')).toBe(true);
        settling.value = false;
        frames(1);
        expect(result.current.has('n')).toBe(false);
    });

    it('a background arrival off-screen becomes an ordinary stack after the hold runs out', () => {
        const { result, rerender } = renderHook((p) => useEntryHold(p), { initialProps: { ...baseProps, items: [A, B] } });
        rerender({ ...baseProps, items: [A, B, item('n', 1028)] });
        frames(Math.floor((ENTRY_HOLD_MAX_MS - 20) / 16));
        expect(result.current.has('n')).toBe(true);
        frames(4);
        expect(result.current.has('n')).toBe(false);
    });

    it('a camera that never reports settled cannot hold a stack past the limit', () => {
        const { result, rerender } = renderHook((p) => useEntryHold(p), { initialProps: { ...baseProps, items: [A, B] } });
        settling.value = true;
        rerender({ ...baseProps, items: [A, B, item('n', 514)] });
        frames(Math.floor((ENTRY_HOLD_LIMIT_MS - 40) / 16));
        expect(result.current.has('n')).toBe(true);
        frames(4);
        expect(result.current.has('n')).toBe(false);
    });

    it('fan columns and a scene swap never enter', () => {
        const { result, rerender } = renderHook((p) => useEntryHold(p), { initialProps: { ...baseProps, items: [A, B] } });
        rerender({ ...baseProps, items: [A, B, item('fan-x', 1028, true)] });
        expect(result.current.size).toBe(0);
        rerender({ ...baseProps, sceneSwapNonce: 1, items: [item('p', 1028), item('q', 1542)] });
        expect(result.current.size).toBe(0);
    });

    it('a held stack that leaves the scene is dropped', () => {
        const { result, rerender } = renderHook((p) => useEntryHold(p), { initialProps: { ...baseProps, items: [A, B] } });
        rerender({ ...baseProps, items: [A, B, item('n', 1028)] });
        expect(result.current.has('n')).toBe(true);
        rerender({ ...baseProps, items: [A, B] });
        expect(result.current.size).toBe(0);
    });
});
