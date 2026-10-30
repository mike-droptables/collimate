import { describe, it, expect, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import { scrubSpacing, useScrubTrack } from '../useScrubTrack';

describe('scrubSpacing', () => {
    it('spreads items across the extent when it fits under the cap', () => {
        // 100px usable across 5 gaps = 20px — under the 28px cap.
        expect(scrubSpacing(120, 6, 10, 28)).toBe(20);
    });

    it('caps the gap so a short list terminates early on a tall rail', () => {
        // 3 items on an 800px rail would be 390px apart uncapped — the cap
        // keeps adjacent items one short mouse move apart.
        expect(scrubSpacing(800, 3, 10, 28)).toBe(28);
    });

    it('degenerates safely', () => {
        expect(scrubSpacing(800, 1, 10, 28)).toBe(0);
        expect(scrubSpacing(0, 4, 10, 28)).toBe(0);
    });
});

describe('useScrubTrack with maxGap', () => {
    const track = (extent: number) => ({
        getBoundingClientRect: () => ({
            top: 0, left: 0, height: extent, width: 24,
            right: 24, bottom: extent, x: 0, y: 0, toJSON: () => ({}),
        }),
    }) as unknown as HTMLDivElement;

    const pointer = (clientY: number, buttons = 1) => ({
        button: 0,
        buttons,
        clientY,
        clientX: 12,
        pointerId: 7,
        stopPropagation: vi.fn(),
        preventDefault: vi.fn(),
        currentTarget: {
            setPointerCapture: vi.fn(),
            hasPointerCapture: vi.fn(() => true),
            releasePointerCapture: vi.fn(),
        },
    }) as unknown as React.PointerEvent;

    it('history rail keeps snaps local and commits once on release __F0580', () => {
        const onCommit = vi.fn();
        const { result } = renderHook(() =>
            useScrubTrack({ axis: 'y', count: 3, pad: 10, maxGap: 28, onCommit }));
        result.current.trackRef.current = track(800);

        // Track spans 10..66px (2 gaps × 28). 40px is nearest index 1.
        act(() => { result.current.handlers.onPointerDown(pointer(40)); });
        expect(result.current.dragIndex).toBe(1);

        // 700px is far past the terminated track — clamps to the last item.
        act(() => { result.current.handlers.onPointerMove(pointer(700)); });
        expect(result.current.dragIndex).toBe(2);

        expect(onCommit).not.toHaveBeenCalled();

        // Release commits ONCE, with the clamped index.
        act(() => { result.current.handlers.onPointerUp(pointer(700)); });
        expect(onCommit).toHaveBeenCalledTimes(1);
        expect(onCommit).toHaveBeenCalledWith(2);
        expect(result.current.dragging).toBe(false);
    });

    it('without a cap the items spread across the full extent', () => {
        const onCommit = vi.fn();
        const { result } = renderHook(() =>
            useScrubTrack({ axis: 'y', count: 3, pad: 10, onCommit }));
        result.current.trackRef.current = track(800);

        // Midpoint of the full 780px track is index 1.
        act(() => { result.current.handlers.onPointerDown(pointer(400)); });
        expect(result.current.dragIndex).toBe(1);
        act(() => { result.current.handlers.onPointerUp(pointer(400)); });
        expect(onCommit).toHaveBeenCalledWith(1);
    });

    it('onEnd fires once on release, before onCommit', () => {
        const order: string[] = [];
        const { result } = renderHook(() => useScrubTrack({
            axis: 'y', count: 3, pad: 10,
            onCommit: () => { order.push('commit'); },
            onEnd: () => { order.push('end'); },
        }));
        result.current.trackRef.current = track(800);
        act(() => { result.current.handlers.onPointerDown(pointer(400)); });
        act(() => { result.current.handlers.onPointerUp(pointer(400)); });
        expect(order).toEqual(['end', 'commit']);
    });

    it('onEnd fires on pointercancel without a commit', () => {
        const onCommit = vi.fn();
        const onEnd = vi.fn();
        const { result } = renderHook(() =>
            useScrubTrack({ axis: 'y', count: 3, pad: 10, onCommit, onEnd }));
        result.current.trackRef.current = track(800);
        act(() => { result.current.handlers.onPointerDown(pointer(400)); });
        act(() => { result.current.handlers.onPointerCancel(pointer(400)); });
        expect(onEnd).toHaveBeenCalledTimes(1);
        expect(onCommit).not.toHaveBeenCalled();
        expect(result.current.dragging).toBe(false);
    });

    it('onEnd fires on a buttonless move without a commit', () => {
        const onCommit = vi.fn();
        const onEnd = vi.fn();
        const { result } = renderHook(() =>
            useScrubTrack({ axis: 'y', count: 3, pad: 10, onCommit, onEnd }));
        result.current.trackRef.current = track(800);
        act(() => { result.current.handlers.onPointerDown(pointer(400)); });
        act(() => { result.current.handlers.onPointerMove(pointer(500, 0)); });
        expect(onEnd).toHaveBeenCalledTimes(1);
        expect(onCommit).not.toHaveBeenCalled();
        expect(result.current.dragging).toBe(false);
    });
});

describe('useScrubTrack edges', () => {
    const rect = (top: number, left: number, height: number, width: number) => ({
        getBoundingClientRect: () => ({
            top, left, height, width, right: left + width, bottom: top + height,
            x: left, y: top, toJSON: () => ({}),
        }),
    }) as unknown as HTMLDivElement;
    const event = (at: { x?: number; y?: number; button?: number; buttons?: number; captured?: boolean }) => {
        const target = {
            setPointerCapture: vi.fn(),
            hasPointerCapture: vi.fn(() => at.captured ?? true),
            releasePointerCapture: vi.fn(),
        };
        return {
            button: at.button ?? 0, buttons: at.buttons ?? 1,
            clientX: at.x ?? 0, clientY: at.y ?? 0, pointerId: 3,
            stopPropagation: vi.fn(), preventDefault: vi.fn(), currentTarget: target,
        } as unknown as React.PointerEvent & { currentTarget: typeof target };
    };

    it('a horizontal track snaps along x from its left edge', () => {
        const onCommit = vi.fn();
        const { result } = renderHook(() => useScrubTrack({ axis: 'x', count: 5, pad: 0, onCommit }));
        // 400px wide from x=100: items every 100px. y must not matter.
        result.current.trackRef.current = rect(9999, 100, 24, 400);
        act(() => { result.current.handlers.onPointerDown(event({ x: 290, y: 0 })); });
        expect(result.current.dragIndex).toBe(2);
        act(() => { result.current.handlers.onPointerMove(event({ x: 50 })); });
        expect(result.current.dragIndex).toBe(0);
        act(() => { result.current.handlers.onPointerUp(event({ x: 50 })); });
        expect(onCommit).toHaveBeenCalledWith(0);
    });

    it('ignores a non-primary press, and moves or releases with no gesture under way', () => {
        const onCommit = vi.fn();
        const onEnd = vi.fn();
        const { result } = renderHook(() => useScrubTrack({ axis: 'y', count: 3, pad: 0, onCommit, onEnd }));
        result.current.trackRef.current = rect(0, 0, 200, 24);
        const secondary = event({ y: 100, button: 2 });
        act(() => { result.current.handlers.onPointerDown(secondary); });
        expect(secondary.currentTarget.setPointerCapture).not.toHaveBeenCalled();
        expect(secondary.preventDefault).not.toHaveBeenCalled();
        act(() => { result.current.handlers.onPointerMove(event({ y: 100 })); });
        act(() => { result.current.handlers.onPointerMove(event({ y: 100, buttons: 0 })); });
        act(() => { result.current.handlers.onPointerUp(event({ y: 100 })); });
        expect(result.current.dragging).toBe(false);
        expect(onEnd).not.toHaveBeenCalled();
        expect(onCommit).not.toHaveBeenCalled();
    });

    it('a press with no mounted track drags nowhere and commits nothing', () => {
        const onCommit = vi.fn();
        const onEnd = vi.fn();
        const { result } = renderHook(() => useScrubTrack({ axis: 'y', count: 3, pad: 0, onCommit, onEnd }));
        act(() => { result.current.handlers.onPointerDown(event({ y: 100 })); });
        expect(result.current.dragIndex).toBeNull();
        act(() => { result.current.handlers.onPointerMove(event({ y: 150 })); });
        expect(result.current.dragIndex).toBeNull();
        act(() => { result.current.handlers.onPointerUp(event({ y: 150 })); });
        expect(onEnd).toHaveBeenCalledTimes(1);
        expect(onCommit).not.toHaveBeenCalled();
    });

    it('an empty track yields no index; a single item always snaps to it', () => {
        const onCommit = vi.fn();
        const { result, rerender } = renderHook(({ count }) => useScrubTrack({ axis: 'y', count, pad: 0, onCommit }), { initialProps: { count: 0 } });
        result.current.trackRef.current = rect(0, 0, 200, 24);
        act(() => { result.current.handlers.onPointerDown(event({ y: 150 })); });
        expect(result.current.dragIndex).toBeNull();
        act(() => { result.current.handlers.onPointerUp(event({ y: 150 })); });
        expect(onCommit).not.toHaveBeenCalled();
        rerender({ count: 1 });
        act(() => { result.current.handlers.onPointerDown(event({ y: 190 })); });
        expect(result.current.dragIndex).toBe(0);
        act(() => { result.current.handlers.onPointerUp(event({ y: 190 })); });
        expect(onCommit).toHaveBeenCalledWith(0);
    });

    it('a move within the same snap keeps the index, and release frees only a held capture', () => {
        const onCommit = vi.fn();
        const { result } = renderHook(() => useScrubTrack({ axis: 'y', count: 3, pad: 0, onCommit }));
        result.current.trackRef.current = rect(0, 0, 200, 24);
        const down = event({ y: 95 });
        act(() => { result.current.handlers.onPointerDown(down); });
        expect(down.currentTarget.setPointerCapture).toHaveBeenCalledWith(3);
        const before = result.current;
        act(() => { result.current.handlers.onPointerMove(event({ y: 105 })); });
        expect(result.current).toBe(before);
        const held = event({ y: 105 });
        act(() => { result.current.handlers.onPointerUp(held); });
        expect(held.currentTarget.releasePointerCapture).toHaveBeenCalledWith(3);
        act(() => { result.current.handlers.onPointerDown(event({ y: 195 })); });
        const lost = event({ y: 195, captured: false });
        act(() => { result.current.handlers.onPointerCancel(lost); });
        expect(lost.currentTarget.releasePointerCapture).not.toHaveBeenCalled();
        expect(onCommit.mock.calls).toEqual([[1]]);
    });
});
