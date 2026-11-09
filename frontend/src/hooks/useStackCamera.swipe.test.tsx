import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useStackCamera } from './useStackCamera';
import type { StackLayoutResult } from './useStackLayout';

const layout = {
    viewSize: { width: 1000, height: 800 }, GAP: 14,
    positions: [], widths: [], arrowWorldX: null, cellPendingMoveWorldX: null,
    overviewTarget: { x: 0, y: 0, zoom: 1 }, sceneSwapNonce: 0,
} as unknown as StackLayoutResult;

function setup() {
    const root = document.createElement('div');
    document.body.append(root);
    const rootDivRef = { current: root }, cameraTargetRef = { current: 0 };
    const navigate = vi.fn();
    const hook = renderHook(({ zoomed, onNavigate }) => useStackCamera({
        layout, stacks: [], selectedId: null, isZoomedOut: zoomed, topology: 'stable',
        pendingMove: null, centerRequested: 0, edgeAlignRequested: null, cameraHold: false,
        widthDragActive: false, cameraTargetRef, rootDivRef, onSwipeNavigate: onNavigate,
    }), { initialProps: { zoomed: false, onNavigate: navigate } });
    return { root, navigate, ...hook };
}

function wheel(target: HTMLElement, deltaX: number, options: WheelEventInit = {}) {
    const event = new WheelEvent('wheel', { deltaX, bubbles: true, cancelable: true, ...options });
    act(() => { target.dispatchEvent(event); });
    return event;
}

function touch(target: HTMLElement, kind: string, xs: number[]) {
    const event = new Event(kind, { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'touches', { value: xs.map(clientX => ({ clientX })) });
    act(() => { target.dispatchEvent(event); });
    return event;
}

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); document.body.replaceChildren(); });

describe('F0652 swipe navigation characterization before extraction', () => {
    it('F0652 classifies each wheel burst once and uses the latest callback without resetting its travel', () => {
        vi.useFakeTimers();
        const scheduled = vi.spyOn(globalThis, 'setTimeout');
        const cleared = vi.spyOn(globalThis, 'clearTimeout');
        const { root, navigate, rerender, unmount } = setup();
        expect(wheel(root, 100).defaultPrevented).toBe(false);
        wheel(root, 20); // A small event does not reclassify a tilt-wheel burst.
        expect(navigate).not.toHaveBeenCalled();
        act(() => { vi.advanceTimersByTime(141); });
        expect(wheel(root, 20).defaultPrevented).toBe(true);
        const next = vi.fn();
        rerender({ zoomed: false, onNavigate: next });
        wheel(root, 120); // Finish the existing touchpad burst using the new callback.
        expect(navigate).not.toHaveBeenCalled();
        expect(next).toHaveBeenCalledExactlyOnceWith(1);
        wheel(root, -280);
        expect(next.mock.calls).toEqual([[1], [-1], [-1]]);
        const gapTimer = scheduled.mock.results[scheduled.mock.calls.findLastIndex(([, delay]) => delay === 140)].value;
        unmount();
        expect(cleared).toHaveBeenCalledWith(gapTimer);
        wheel(root, 20);
        expect(next).toHaveBeenCalledTimes(3);
    });

    it('F0652 lets nested horizontal regions consume travel until the edge and preserves vertical and pinch scroll', () => {
        const { root, navigate, unmount } = setup();
        const child = document.createElement('div');
        child.style.overflowX = 'auto'; root.append(child);
        Object.defineProperties(child, { scrollWidth: { value: 600 }, clientWidth: { value: 200 } });
        expect(wheel(child, 20).defaultPrevented).toBe(false);
        child.scrollLeft = 400;
        expect(wheel(child, 20).defaultPrevented).toBe(true);
        wheel(child, 120);
        expect(navigate).toHaveBeenCalledExactlyOnceWith(1);
        expect(wheel(root, 10, { deltaY: 30 }).defaultPrevented).toBe(false);
        expect(wheel(root, 20, { ctrlKey: true }).defaultPrevented).toBe(false);
        expect(navigate).toHaveBeenCalledTimes(1);
        unmount();
    });

    it('F0652 two-finger drag uses grab direction, ends on finger release and detaches in zoom-out mode', () => {
        const { root, navigate, rerender, unmount } = setup();
        expect(touch(root, 'touchstart', [300]).defaultPrevented).toBe(false);
        expect(touch(root, 'touchstart', [300, 400]).defaultPrevented).toBe(true);
        touch(root, 'touchmove', [210, 310]);
        expect(navigate).toHaveBeenCalledExactlyOnceWith(1);
        touch(root, 'touchend', [210]);
        touch(root, 'touchmove', [0, 100]);
        expect(navigate).toHaveBeenCalledTimes(1);
        rerender({ zoomed: true, onNavigate: navigate });
        expect(touch(root, 'touchstart', [300, 400]).defaultPrevented).toBe(false);
        touch(root, 'touchmove', [0, 100]);
        expect(navigate).toHaveBeenCalledTimes(1);
        unmount();
    });
});
