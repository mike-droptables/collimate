import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type React from 'react';
import {
    alignCameraX, resolvePanAlignSnap, PAN_ALIGN_BUFFER, type AlignGeometry,
    scrollIntoViewX, insetCameraX, useStackCamera,
} from './useStackCamera';
import { NO_INSET } from '@/lib/commandPane';
import { classifyHorizontalWheel, WHEEL_NOTCH_MIN_PX } from './useSwipeNavigate';
import { ENTRY_VIEW_TOLERANCE, slotInView } from '@/lib/stackEntry';
import type { StackLayoutResult } from '@/hooks/useStackLayout';
import type { Stack } from '@/lib/types';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { COMMAND_PANE_WIDTH, defaultCommandPane } from '@/stores/collimate/commandPaneSlice';

/**
 * Pan-align snap math. Geometry: viewWidth 1000, GAP 14, a
 * 500px stack centered at world x = 0. Targets (camera x):
 *   center →    0   (stack center on the centerline)
 *   left   →  236   (left edge at the 14px left inset:  -250 + 500 - 14)
 *   right  → -236   (right edge at the right inset:      250 - 500 + 14)
 */
const g: AlignGeometry = {
    stackCenter: 0,
    stackWidth: 500,
    viewWidth: 1000,
    gap: 14,
};

describe('alignCameraX', () => {
    it('computes the three camera targets', () => {
        expect(alignCameraX('center', g)).toBe(0);
        expect(alignCameraX('left', g)).toBe(236);
        expect(alignCameraX('right', g)).toBe(-236);
    });

});

describe('resolvePanAlignSnap', () => {
    it('arms the single target within the buffer', () => {
        expect(resolvePanAlignSnap(0, g)).toBe('center');
        expect(resolvePanAlignSnap(236, g)).toBe('left');
        expect(resolvePanAlignSnap(236 - PAN_ALIGN_BUFFER, g)).toBe('left');
        expect(resolvePanAlignSnap(-236, g)).toBe('right');
    });

    it('returns null away from every target', () => {
        expect(resolvePanAlignSnap(120, g)).toBeNull();
        expect(resolvePanAlignSnap(236 - PAN_ALIGN_BUFFER - 1, g)).toBeNull();
        expect(resolvePanAlignSnap(-500, g)).toBeNull();
    });

    it('collapses to center when the stack nearly fills the viewport (two+ hits)', () => {
        // A 900px stack in the 972px available span: targets are 36px
        // apart (left c+36, center c, right c-36) — parking exactly on
        // the LEFT target also hits center, so only center survives.
        const narrow = { ...g, stackWidth: 900 };
        expect(resolvePanAlignSnap(alignCameraX('left', narrow), narrow)).toBe('center');
        expect(resolvePanAlignSnap(alignCameraX('right', narrow), narrow)).toBe('center');
        expect(resolvePanAlignSnap(0, narrow)).toBe('center');
    });

    it('a wide-but-not-full stack keeps distinct edge targets', () => {
        // 880px stack: left target c+46, outside the 40px buffer of
        // center — parking there is an unambiguous left-align.
        const wide = { ...g, stackWidth: 880 };
        expect(resolvePanAlignSnap(alignCameraX('left', wide), wide)).toBe('left');
        expect(resolvePanAlignSnap(alignCameraX('right', wide), wide)).toBe('right');
    });
});

describe('classifyHorizontalWheel', () => {
    it('line/page delta modes are a real mouse wheel', () => {
        expect(classifyHorizontalWheel({ deltaMode: 1, deltaX: 3 })).toBe('wheel');
        expect(classifyHorizontalWheel({ deltaMode: 2, deltaX: 1 })).toBe('wheel');
    });

    it('big integer pixel notches are a tilt wheel (GTK 53 / Chromium 100 / legacy 120)', () => {
        expect(classifyHorizontalWheel({ deltaMode: 0, deltaX: 53 })).toBe('wheel');
        expect(classifyHorizontalWheel({ deltaMode: 0, deltaX: -100 })).toBe('wheel');
        expect(classifyHorizontalWheel({ deltaMode: 0, deltaX: 120 })).toBe('wheel');
        expect(classifyHorizontalWheel({ deltaMode: 0, deltaX: WHEEL_NOTCH_MIN_PX })).toBe('wheel');
    });

    it('small pixel deltas are a touchpad stream opening', () => {
        expect(classifyHorizontalWheel({ deltaMode: 0, deltaX: 4 })).toBe('touchpad');
        expect(classifyHorizontalWheel({ deltaMode: 0, deltaX: -(WHEEL_NOTCH_MIN_PX - 1) })).toBe('touchpad');
    });

    it('fractional pixel deltas are a touchpad even when large (macOS/precision flick)', () => {
        expect(classifyHorizontalWheel({ deltaMode: 0, deltaX: 86.5 })).toBe('touchpad');
        expect(classifyHorizontalWheel({ deltaMode: 0, deltaX: -120.25 })).toBe('touchpad');
    });
});

describe('scrollIntoViewX', () => {
    // Visible band at camera 0: [-486, 486] (1000px view, 14px inset each side).
    it('returns null while the stack sits inside the band give or take the tolerance', () => {
        expect(scrollIntoViewX(0, g, 0)).toBeNull();
        expect(scrollIntoViewX(236, g, 0)).toBeNull();           // left edge exactly on the inset
        expect(scrollIntoViewX(244, g, 8)).toBeNull();           // 8px past, within tolerance
        expect(scrollIntoViewX(-244, g, 8)).toBeNull();
    });

    it('left/right targets are the alignCameraX ones', () => {
        expect(scrollIntoViewX(300, g, 8)).toBe(alignCameraX('left', g));
        expect(scrollIntoViewX(-300, g, 8)).toBe(alignCameraX('right', g));
        expect(scrollIntoViewX(237, g, 0)).toBe(alignCameraX('left', g));
    });

    it('a stack wider than the view left-aligns, and rests there', () => {
        const wide = { ...g, stackWidth: 1400 };
        const cam = scrollIntoViewX(0, wide, 8)!;
        expect(cam).toBe(alignCameraX('left', wide));
        expect(scrollIntoViewX(cam, wide, 8)).toBeNull();
    });

    it('a stack wider than the band the pane leaves keeps one target across calls (K01)', () => {
        // 900px stack, 1000px view, 320px left pane: the band is 652px wide.
        const wide = { ...g, stackWidth: 900 };
        const left = { left: 320, right: 0 };
        const target = alignCameraX('left', wide) - 320;
        const first = scrollIntoViewX(0, wide, ENTRY_VIEW_TOLERANCE, left);
        expect(first).toBe(target);
        expect(scrollIntoViewX(first!, wide, ENTRY_VIEW_TOLERANCE, left)).toBeNull();
        expect(scrollIntoViewX(first! + 100, wide, ENTRY_VIEW_TOLERANCE, left)).toBe(target);
        const right = { left: 0, right: 320 };
        const r = scrollIntoViewX(0, wide, ENTRY_VIEW_TOLERANCE, right)!;
        expect(r).toBe(alignCameraX('left', wide));
        expect(scrollIntoViewX(r, wide, ENTRY_VIEW_TOLERANCE, right)).toBeNull();
    });

    it('an inset (the Commands pane) moves the band in and the camera only as far as it takes (K01)', () => {
        const left = { left: 320, right: 0 };
        const right = { left: 0, right: 320 };
        // The stack spans [-250, 250]; at camera 0 a left pane leaves the band
        // [-166, 486], a right one [-486, 166].
        expect(scrollIntoViewX(0, g, 0, left)).toBe(alignCameraX('left', g) - 320);
        const camLeft = scrollIntoViewX(0, g, 0, left)!;
        expect((g.stackCenter - g.stackWidth / 2) - camLeft + g.viewWidth / 2).toBe(320 + g.gap);
        expect(scrollIntoViewX(camLeft, g, 0, left)).toBeNull();
        const camRight = scrollIntoViewX(0, g, 0, right)!;
        expect((g.stackCenter + g.stackWidth / 2) - camRight + g.viewWidth / 2).toBe(g.viewWidth - 320 - g.gap);
        expect(scrollIntoViewX(camRight, g, 0, right)).toBeNull();
        // A stack the pane does not cover keeps the camera where it is.
        expect(scrollIntoViewX(-200, g, 0, left)).toBeNull();
        expect(scrollIntoViewX(300, g, 0, right)).toBe(alignCameraX('left', g));
    });

    it('the slide rides on the camera without the pane: none drops it, the other side reverses it (K01)', () => {
        const left = { left: 320, right: 0 };
        const right = { left: 0, right: 320 };
        // Open on the left: the base stays put, the target slides 320 + gap clear.
        expect(insetCameraX(0, g, 0, left)).toEqual({ base: 0, target: alignCameraX('left', g) - 320 });
        // Closed: back to the base.
        expect(insetCameraX(0, g, 0, NO_INSET)).toEqual({ base: 0, target: 0 });
        // Docked across: the same base, the slide the other way.
        expect(insetCameraX(0, g, 0, right)).toEqual({ base: 0, target: alignCameraX('right', g) + 320 });
        // A base that leaves the stack off screen tracks it first, as it would with no pane.
        expect(insetCameraX(600, g, 0, NO_INSET)).toEqual({ base: alignCameraX('left', g), target: alignCameraX('left', g) });
    });

    it("for a stack that fits the band its no-pan test is slotInView at zoom 1 with ENTRY_VIEW_TOLERANCE", () => {
        {
            for (let camX = -600; camX <= 600; camX += 37) {
                for (let stackCenter = -700; stackCenter <= 700; stackCenter += 53) {
                    for (const stackWidth of [200, 500, 900, 972]) {
                        const geom = { ...g, stackCenter, stackWidth };
                        const inView = slotInView(stackCenter - stackWidth / 2, stackCenter + stackWidth / 2, {
                            cameraX: camX, zoom: 1, viewWidth: geom.viewWidth, gap: geom.gap,
                        });
                        expect(scrollIntoViewX(camX, geom, ENTRY_VIEW_TOLERANCE) === null).toBe(inView);
                    }
                }
            }
        }
    });
});

describe('center-pan pointer lock', () => {
    const layout = {
        viewSize: { width: 1000, height: 800 }, GAP: 14,
        positions: [], widths: [], arrowWorldX: null, cellPendingMoveWorldX: null,
        overviewTarget: { x: 0, y: 0, zoom: 1 }, sceneSwapNonce: 0,
    } as unknown as StackLayoutResult;
    let locked: Element | null = null;
    const exitPointerLock = vi.fn();

    const setup = () => {
        locked = null;
        exitPointerLock.mockReset();
        Object.defineProperty(document, 'pointerLockElement', { configurable: true, get: () => locked });
        Object.defineProperty(document, 'exitPointerLock', { configurable: true, value: exitPointerLock });
        const { result } = renderHook(() => useStackCamera({
            layout, stacks: [], selectedId: null, isZoomedOut: false, topology: 'stable',
            pendingMove: null, centerRequested: 0, edgeAlignRequested: null, cameraHold: false,
            widthDragActive: false, cameraTargetRef: { current: 0 }, rootDivRef: { current: null },
        }));
        const lockEl = document.createElement('div');
        (lockEl as unknown as { requestPointerLock: () => void }).requestPointerLock = vi.fn();
        return { result, lockEl };
    };
    const grant = (el: Element | null) => {
        locked = el;
        act(() => { document.dispatchEvent(new Event('pointerlockchange')); });
    };

    afterEach(() => {
        delete (document as unknown as Record<string, unknown>).pointerLockElement;
        delete (document as unknown as Record<string, unknown>).exitPointerLock;
    });

    it('exits a lock whose grant lands after the release', () => {
        const { result, lockEl } = setup();
        const w = window.innerWidth;
        act(() => {
            result.current.handleCenterPanStart({ clientX: w - 200, currentTarget: lockEl } as unknown as React.PointerEvent);
            result.current.handleCenterPanMove({ clientX: w - 1, movementX: 0 } as unknown as React.PointerEvent);
        });
        expect(lockEl.requestPointerLock).toHaveBeenCalledTimes(1);
        act(() => { result.current.handleCenterPanEnd(); });
        expect(exitPointerLock).not.toHaveBeenCalled();
        grant(lockEl);
        expect(exitPointerLock).toHaveBeenCalledTimes(1);
    });

    it('keeps a lock granted mid-pan, and leaves a lock on another element alone', () => {
        const { result, lockEl } = setup();
        const w = window.innerWidth;
        act(() => {
            result.current.handleCenterPanStart({ clientX: w - 200, currentTarget: lockEl } as unknown as React.PointerEvent);
            result.current.handleCenterPanMove({ clientX: w - 1, movementX: 0 } as unknown as React.PointerEvent);
        });
        grant(lockEl);
        expect(exitPointerLock).not.toHaveBeenCalled();
        act(() => { result.current.handleCenterPanEnd(); });
        expect(exitPointerLock).toHaveBeenCalledTimes(1);
        grant(null);
        grant(document.createElement('canvas'));
        expect(exitPointerLock).toHaveBeenCalledTimes(1);
    });
});

describe('the Commands pane inset', () => {
    // Two 500px stacks at world 0 and 600 in a 1000px canvas with a 14px gap.
    const layout = {
        viewSize: { width: 1000, height: 800 }, GAP: 14,
        positions: [0, 600], widths: [500, 500], arrowWorldX: null, cellPendingMoveWorldX: null,
        gridTarget: { x: 0, y: 0, zoom: 1 }, sceneSwapNonce: 0,
    } as unknown as StackLayoutResult;
    const stacks = [{ id: 's1', name: 'A', cells: [] }, { id: 's2', name: 'B', cells: [] }] as unknown as Stack[];
    const one: AlignGeometry = { stackCenter: 0, stackWidth: 500, viewWidth: 1000, gap: 14 };
    const two: AlignGeometry = { ...one, stackCenter: 600 };
    const pane = () => useCollimateStore.getState();

    const setup = () => {
        const target = { current: 0 };
        type Props = { selectedId: string; edge: { mode: 'left' | 'right'; nonce: number } | null };
        const initialProps: Props = { selectedId: 's1', edge: null };
        const view = renderHook((p: Props) => useStackCamera({
            layout, stacks, selectedId: p.selectedId, isZoomedOut: false, topology: 'stable',
            pendingMove: null, centerRequested: 0, edgeAlignRequested: p.edge, cameraHold: false,
            widthDragActive: false, cameraTargetRef: target, rootDivRef: { current: null },
        }), { initialProps });
        // The camera arrives where it was sent, so each step starts from a
        // realistic camera rather than jsdom's motion value parked at 0.
        const settle = () => act(() => { view.result.current.cameraXMV.jump(target.current); });
        return { target, view, settle };
    };

    afterEach(() => { act(() => { useCollimateStore.setState({ commandPane: defaultCommandPane() }); }); });

    it('follows the pane: set on open, reversed on a dock across, gone on close (K01)', () => {
        const { target, settle } = setup();
        expect(target.current).toBe(0);
        act(() => { pane().setCommandPaneSide('left'); pane().openCommandPane(); });
        expect(target.current).toBe(alignCameraX('left', one) - COMMAND_PANE_WIDTH);
        settle();
        act(() => { pane().setCommandPaneSide('right'); });
        expect(target.current).toBe(alignCameraX('right', one) + COMMAND_PANE_WIDTH);
        settle();
        act(() => { pane().closeCommandPane(); });
        expect(target.current).toBe(0);
    });

    it('a selection moved under the open pane lands, on close, where it would without the pane (K01)', () => {
        const { target, view, settle } = setup();
        act(() => { pane().setCommandPaneSide('right'); pane().openCommandPane(); });
        settle();
        view.rerender({ selectedId: 's2', edge: null });
        settle();
        // s2's right edge comes in clear of the right pane, slid by its width.
        expect(target.current).toBe(alignCameraX('right', two) + COMMAND_PANE_WIDTH);
        act(() => { pane().closeCommandPane(); });
        expect(target.current).toBe(alignCameraX('right', two));
    });

    it('a reload keeps no camera: a pane saved open slides from the selection, and closing it drops the slide (K01)', () => {
        // The pane's open state and side come back with the channel's
        // settings; the camera itself is never written to browser storage,
        // so a fresh mount starts from the selection.
        const setItem = vi.spyOn(Storage.prototype, 'setItem');
        try {
            act(() => { pane().setCommandPaneSide('left'); pane().openCommandPane(); });
            const { target, settle } = setup();
            expect(target.current).toBe(alignCameraX('left', one) - COMMAND_PANE_WIDTH);
            settle();
            act(() => { pane().closeCommandPane(); });
            expect(target.current).toBe(0);
            expect(setItem).not.toHaveBeenCalled();
        } finally { setItem.mockRestore(); }
    });

    it('a destination chosen while the pane is open replaces its slide (K01)', () => {
        const { target, view, settle } = setup();
        act(() => { pane().setCommandPaneSide('left'); pane().openCommandPane(); });
        settle();
        view.rerender({ selectedId: 's1', edge: { mode: 'right', nonce: 1 } });
        expect(target.current).toBe(alignCameraX('right', one));
        settle();
        act(() => { pane().closeCommandPane(); });
        expect(target.current).toBe(alignCameraX('right', one));
    });
});

describe('a placement the user made holds through state pushes', () => {
    // Two 500px stacks at world 0 and 600 in a 1000px canvas with a 14px
    // gap, as in the pane tests above. A state push (an SSE delta, a save's
    // echo, the idle checkpoint) hands the camera new `stacks` and layout
    // arrays with the same geometry.
    const baseLayout = {
        viewSize: { width: 1000, height: 800 }, GAP: 14,
        positions: [0, 600], widths: [500, 500], arrowWorldX: null, cellPendingMoveWorldX: null,
        overviewTarget: { x: 0, y: 0, zoom: 1 }, sceneSwapNonce: 0,
    } as unknown as StackLayoutResult;
    const baseStacks = [{ id: 's1', name: 'A', cells: [] }, { id: 's2', name: 'B', cells: [] }] as unknown as Stack[];
    type Props = { selectedId: string; stacks: Stack[]; layout: StackLayoutResult; centerRequested: number };

    const setup = (initial: Partial<Props> = {}) => {
        const target = { current: 0 };
        const initialProps: Props = { selectedId: 's1', stacks: baseStacks, layout: baseLayout, centerRequested: 0, ...initial };
        const view = renderHook((p: Props) => useStackCamera({
            layout: p.layout, stacks: p.stacks, selectedId: p.selectedId, isZoomedOut: false, topology: 'stable',
            pendingMove: null, centerRequested: p.centerRequested, edgeAlignRequested: null, cameraHold: false,
            widthDragActive: false, cameraTargetRef: target, rootDivRef: { current: null },
        }), { initialProps });
        let props = initialProps;
        const rerender = (next: Partial<Props>) => { props = { ...props, ...next }; view.rerender(props); };
        // A state push: the same geometry under new identities.
        const push = () => rerender({
            stacks: [...props.stacks],
            layout: { ...props.layout, positions: [...props.layout.positions], widths: [...props.layout.widths] },
        });
        const settle = () => act(() => { view.result.current.cameraXMV.jump(target.current); });
        return { target, view, rerender, push, settle };
    };

    // Drag s1's head 400px left: the camera parks at 400, s1's left 164px
    // off the visible band (no snap target within PAN_ALIGN_BUFFER).
    const panS1Left = (view: ReturnType<typeof setup>['view']) => {
        const lockEl = document.createElement('div');
        act(() => {
            view.result.current.handleCenterPanStart({ clientX: 500, currentTarget: lockEl } as unknown as React.PointerEvent, 's1');
            view.result.current.handleCenterPanMove({ clientX: 100, movementX: -400 } as unknown as React.PointerEvent);
        });
        let moved = false;
        act(() => { moved = view.result.current.handleCenterPanEnd(); });
        expect(moved).toBe(true);
    };

    it('a manual pan that leaves the selection partly off screen stays put on the next state push', () => {
        const { target, view, push } = setup();
        expect(target.current).toBe(0);
        panS1Left(view);
        expect(target.current).toBe(400);
        // Without the hold, Effect 1 re-tracked here and sprang s1 back in
        // (target alignCameraX('left') = 236) with no input.
        for (let i = 0; i < 3; i++) push();
        expect(target.current).toBe(400);
        expect(view.result.current.cameraXMV.get()).toBe(400);
    });

    it('a new selection or a change to the held column resumes tracking', () => {
        const { target, view, rerender, push, settle } = setup();
        panS1Left(view);
        // s2 is in view at 400; selecting it and coming back tracks s1 again.
        rerender({ selectedId: 's2' });
        expect(target.current).toBe(400);
        rerender({ selectedId: 's1' });
        expect(target.current).toBe(alignCameraX('left', g));
        settle();

        panS1Left(view);
        expect(target.current).toBe(alignCameraX('left', g) + 400);
        push();
        expect(target.current).toBe(alignCameraX('left', g) + 400);
        // s1 resized by other means: tracking resumes.
        rerender({ layout: { ...baseLayout, widths: [520, 500] } });
        expect(target.current).toBe(alignCameraX('left', { ...g, stackWidth: 520 }));
    });

    it('Alt+c on a stack wider than the view stays centred on the next state push', () => {
        const wide = { ...baseLayout, positions: [0], widths: [1200] } as unknown as StackLayoutResult;
        const { target, rerender, push, settle } = setup({ stacks: [baseStacks[0]], layout: wide });
        const wideGeom: AlignGeometry = { ...g, stackWidth: 1200 };
        // Tracking rests a wide stack on its left edge.
        expect(target.current).toBe(alignCameraX('left', wideGeom));
        settle();
        rerender({ centerRequested: 1 });
        expect(target.current).toBe(0);
        settle();
        push();
        expect(target.current).toBe(0);
    });
});
