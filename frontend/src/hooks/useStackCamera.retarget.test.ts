import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { animate } from 'motion/react';
import { useStackCamera } from './useStackCamera';
import type { StackLayoutResult } from './useStackLayout';

vi.mock('motion/react', async importOriginal => {
    const actual = await importOriginal<typeof import('motion/react')>();
    return { ...actual, animate: vi.fn(actual.animate) };
});

function setup() {
    const layout = {
        viewSize: { width: 1000, height: 800 }, GAP: 14,
        positions: [], widths: [], arrowWorldX: null, cellPendingMoveWorldX: null,
        overviewTarget: { x: 0, y: 0, zoom: 1 }, sceneSwapNonce: 0,
    } as unknown as StackLayoutResult;
    const cameraTargetRef = { current: 0 };
    const rootDivRef = { current: null };
    return renderHook(() => useStackCamera({
        layout, stacks: [], selectedId: null, isZoomedOut: false, topology: 'stable',
        pendingMove: null, centerRequested: 0, edgeAlignRequested: null, cameraHold: false,
        widthDragActive: false, cameraTargetRef, rootDivRef,
    }));
}

describe('F0668 pan spring target continuity', () => {
    it('F0668 keeps the same running pan across repeated stacks updates', () => {
        const { result, rerender, unmount } = setup();
        act(() => result.current.commitCameraTarget(600));
        expect(result.current.cameraXMV.isAnimating()).toBe(true);
        expect(result.current.cameraXMV.get()).not.toBe(600);
        vi.mocked(animate).mockClear();
        for (let i = 0; i < 20; i++) rerender();
        expect(animate).not.toHaveBeenCalled();
        unmount();
    });

    it('F0668 retargets a running pan and resumes a stopped pan short of its target', () => {
        const { result, rerender, unmount } = setup();
        act(() => result.current.commitCameraTarget(600));
        vi.mocked(animate).mockClear();
        act(() => result.current.commitCameraTarget(900));
        expect(animate).toHaveBeenCalledWith(result.current.cameraXMV, 900, expect.any(Object));
        act(() => result.current.cameraXMV.stop());
        expect(result.current.cameraXMV.get()).not.toBe(900);
        vi.mocked(animate).mockClear();
        rerender();
        expect(animate).toHaveBeenCalledWith(result.current.cameraXMV, 900, expect.any(Object));
        unmount();
    });
});

describe('F0612 active gesture listeners', () => {
    it('F0612 registers only the active mode and preserves native scroll versus zoom', () => {
        const root = document.createElement('div'); document.body.append(root);
        root.getBoundingClientRect = () => new DOMRect(0, 0, 1000, 800);
        const added = vi.spyOn(root, 'addEventListener');
        const rootDivRef = { current: root }, cameraTargetRef = { current: 0 };
        const layout = {
            viewSize: { width: 1000, height: 800 }, GAP: 14,
            positions: [], widths: [], arrowWorldX: null, cellPendingMoveWorldX: null,
            overviewTarget: { x: 0, y: 0, zoom: 1 }, sceneSwapNonce: 0,
        } as unknown as StackLayoutResult;
        const { result, rerender, unmount } = renderHook(({ zoomed }) => useStackCamera({
            layout, stacks: [], selectedId: null, isZoomedOut: zoomed, topology: 'stable',
            pendingMove: null, centerRequested: 0, edgeAlignRequested: null, cameraHold: false,
            widthDragActive: false, cameraTargetRef, rootDivRef,
        }), { initialProps: { zoomed: false } });
        expect(added.mock.calls.filter(([event]) => event === 'wheel')).toHaveLength(1);
        const native = new WheelEvent('wheel', { deltaY: 100, cancelable: true });
        act(() => { root.dispatchEvent(native); });
        expect(native.defaultPrevented).toBe(false);
        added.mockClear();
        rerender({ zoomed: true });
        expect(added.mock.calls.filter(([event]) => event === 'wheel')).toHaveLength(1);
        expect(added.mock.calls.filter(([event]) => event.startsWith('touch'))).toHaveLength(0);
        // In the overview Shift+wheel pans, a plain wheel and Ctrl/Cmd+wheel zoom (D-O01, WZ-01).
        const pan = new WheelEvent('wheel', { deltaY: 100, shiftKey: true, cancelable: true });
        const zoomBefore = result.current.zoomMV.get();
        const yBefore = result.current.cameraYMV.get();
        act(() => { root.dispatchEvent(pan); });
        expect(pan.defaultPrevented).toBe(true);
        expect(result.current.zoomMV.get()).toBe(zoomBefore);
        expect(result.current.cameraYMV.get()).not.toBe(yBefore);
        const plain = new WheelEvent('wheel', { deltaY: 100, cancelable: true });
        act(() => { root.dispatchEvent(plain); });
        expect(plain.defaultPrevented).toBe(true);
        const zoomed = result.current.zoomMV.get();
        expect(zoomed).toBeLessThan(zoomBefore);
        const zoom = new WheelEvent('wheel', { deltaY: 100, ctrlKey: true, cancelable: true });
        act(() => { root.dispatchEvent(zoom); });
        expect(zoom.defaultPrevented).toBe(true);
        expect(result.current.zoomMV.get()).toBeLessThan(zoomed);
        unmount(); root.remove();
    });
});
