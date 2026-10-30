import { useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import { useMotionValue, useTransform, type MotionValue } from 'motion/react';
import { OVERVIEW_ZOOM, OVERVIEW_ZOOM_STEP } from '@/stores/collimate/overviewSlice';
import { WHEEL_LINE_PX } from '@/lib/wheelReplay';

interface UsePanZoomCameraParams {
    /** Pointer events attach to this element: the board's viewport (the
     *  whole canvas in the main view). Zoom-about-cursor reads its rect. */
    rootDivRef: React.RefObject<HTMLDivElement | null>;
    /** The wheel listens here (else on `rootDivRef`): the overview's whole
     *  area, its overlays included, so the wheel works the board everywhere
     *  but over the parts that keep it (the shelf's cards, the journal
     *  pane), which stop its propagation. */
    wheelRootRef?: React.RefObject<HTMLElement | null>;
    /** A wheel over an element matching this selector never zooms: it pans
     *  (the Removed shelf's control row and the bar). */
    wheelPansSelector?: string;
    /** The viewport's size: drives the panX transform's centring offset and
     *  the root-local screen→world conversion. */
    viewSize: { width: number; height: number };
    /** The overview's gestures: off in the main view, where the wheel falls
     *  through to native scrolling and the drags belong to the cards. */
    enabled?: boolean;
    /** A left-button pan is not started when the pointer-down target is
     *  inside an element matching this selector (a card that drags on its
     *  own); the PAN_BUTTONS pan over it all the same. */
    panBlockSelector?: string;
}

/** A press that travels this far (|dx| + |dy|, px) is a pan, not a click. */
export const PAN_THRESHOLD = 4;

/** The buttons that pan from anywhere on the board, a card included, and
 *  never select or drag one: middle (1) and right (2). The left button
 *  (0) pans only off the cards that drag. */
export const PAN_BUTTONS: ReadonlySet<number> = new Set([1, 2]);

const clampZoom = (z: number) => Math.min(OVERVIEW_ZOOM.max, Math.max(OVERVIEW_ZOOM.min, z));

/** A plain wheel's zoom per px of travel: a 100px notch is the zoom
 *  control's step (OVERVIEW_ZOOM_STEP), in or out. */
export const WHEEL_ZOOM_PER_PX = Math.log(OVERVIEW_ZOOM_STEP) / 100;
/** A pinch's (ctrl/cmd+wheel's) zoom per px: its deltas are small. */
export const PINCH_ZOOM_PER_PX = 0.01;

const SCROLLS = /^(auto|scroll|overlay)$/;

/** Whether an element between the wheel's target and `root` can still
 *  scroll the way the wheel goes: it takes the wheel natively. */
export function innerScrollTakes(e: WheelEvent, root: Element): boolean {
    const vertical = Math.abs(e.deltaY) >= Math.abs(e.deltaX);
    const delta = vertical ? e.deltaY : e.deltaX;
    if (delta === 0) return false;
    for (let el = e.target instanceof Element ? e.target : null; el && el !== root; el = el.parentElement) {
        const style = getComputedStyle(el);
        if (!SCROLLS.test(vertical ? style.overflowY : style.overflowX)) continue;
        const [pos, size, view] = vertical
            ? [el.scrollTop, el.scrollHeight, el.clientHeight]
            : [el.scrollLeft, el.scrollWidth, el.clientWidth];
        if (delta > 0 ? pos + view < size - 1 : pos > 0) return true;
    }
    return false;
}

/** The camera's motion values. */
export interface CameraValues {
    cameraXMV: MotionValue<number>;
    cameraYMV: MotionValue<number>;
    zoomMV: MotionValue<number>;
}

/** Zoom by `factor` (clamped to the overview's range) about a
 *  viewport-local point, the world point under it staying put (the mock's
 *  zoomAt). The wheel and the zoom control both use it. */
export function zoomCameraAt(
    camera: CameraValues, view: { width: number; height: number },
    localX: number, localY: number, factor: number,
): void {
    const { cameraXMV, cameraYMV, zoomMV } = camera;
    const oldZoom = zoomMV.get();
    const newZoom = clampZoom(oldZoom * factor);
    if (newZoom === oldZoom) return;
    const relX = localX - view.width / 2;
    const relY = localY - view.height / 2;
    cameraXMV.set(cameraXMV.get() + relX / oldZoom - relX / newZoom);
    cameraYMV.set(cameraYMV.get() + relY / oldZoom - relY / newZoom);
    zoomMV.set(newZoom);
}

/** Pan + zoom primitive of the scene's camera ([useStackCamera]). Owns the
 *  cameraXMV/cameraYMV/zoomMV motion values and the panX/panY transforms
 *  (StackScene scales about the viewport's centre, then pans). In the
 *  overview (`enabled`) it carries the overview's gestures: a plain
 *  (vertical) wheel zooms about the cursor, a notch the zoom control's
 *  step; Ctrl/Cmd+wheel (and a trackpad pinch, which Chromium sends as
 *  ctrl+wheel) zooms there too, at the pinch's rate; both between 0.2 and
 *  1.5. Shift+wheel, and a sideways wheel (a tilt wheel, a trackpad's
 *  horizontal swipe), pans. A wheel over `wheelPansSelector` pans and never
 *  zooms, and an inner element that can still scroll the wheel's way takes
 *  it natively. A drag on the background, or a middle- or right-button drag
 *  anywhere (PAN_BUTTONS), pans once it has travelled 4px. A middle press
 *  and its auxclick are cancelled there: no autoscroll, no X11 primary
 *  paste. Fires `canvas-pan-start` / `canvas-pan-end` around a pan
 *  so [MovingContext] can disable iframe pointer events meanwhile. While
 *  enabled, a viewport resize (the Removed shelf opening, the journal pane)
 *  keeps the board's top-left where it is, like the mock's origin-0 world. */
export function usePanZoomCamera({
    rootDivRef,
    wheelRootRef,
    viewSize,
    enabled = true,
    panBlockSelector,
    wheelPansSelector,
}: UsePanZoomCameraParams) {
    const cameraXMV = useMotionValue(0);
    const cameraYMV = useMotionValue(0);
    const zoomMV = useMotionValue(1);

    const panX = useTransform(cameraXMV, (x) => viewSize.width / 2 - x);
    const panY = useTransform(cameraYMV, (y) => -y);

    // The wheel: a native listener so it can preventDefault.
    useEffect(() => {
        const el = rootDivRef.current;
        const target = wheelRootRef?.current ?? el;
        if (!el || !target || !enabled) return;
        const onWheel = (e: WheelEvent) => {
            if (innerScrollTakes(e, target)) return;
            e.preventDefault();
            const unit = e.deltaMode === 1 ? WHEEL_LINE_PX : 1;
            const pansHere = !!wheelPansSelector && e.target instanceof Element && !!e.target.closest(wheelPansSelector);
            const pinch = e.ctrlKey || e.metaKey;
            const zooms = !pansHere && (pinch || (!e.shiftKey && Math.abs(e.deltaY) >= Math.abs(e.deltaX)));
            if (zooms) {
                // Root-local coords: the root sits below the top bar (and,
                // in the overview, the Removed shelf and bar).
                const r = el.getBoundingClientRect();
                const rate = pinch ? PINCH_ZOOM_PER_PX : WHEEL_ZOOM_PER_PX;
                zoomCameraAt({ cameraXMV, cameraYMV, zoomMV }, viewSize,
                    e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * unit * rate));
                return;
            }
            // A pinch over a part that keeps the board still is swallowed
            // (the page never zooms).
            if (pinch) return;
            const z = zoomMV.get();
            cameraXMV.set(cameraXMV.get() + (e.deltaX * unit) / z);
            cameraYMV.set(cameraYMV.get() + (e.deltaY * unit) / z);
        };
        target.addEventListener('wheel', onWheel, { passive: false });
        return () => target.removeEventListener('wheel', onWheel);
    }, [enabled, viewSize, zoomMV, cameraXMV, cameraYMV, rootDivRef, wheelRootRef, wheelPansSelector]);

    // A viewport resize in the overview keeps the board's top-left on
    // screen at the same zoom: the centre-origin transform would otherwise
    // move it by half the change (the overview's world stays put through a
    // resize, useStackLayout) and, vertically, by (1 - zoom) / 2 of it (the
    // world's top is the view's). Skipped while a zoom springs (entering
    // the overview), whose fit lands it anyway.
    const lastViewRef = useRef(viewSize);
    useLayoutEffect(() => {
        const prev = lastViewRef.current;
        lastViewRef.current = viewSize;
        if (!enabled || prev.width === 0 || viewSize.width === 0 || zoomMV.isAnimating()) return;
        const z = zoomMV.get();
        if (prev.width !== viewSize.width) cameraXMV.set(cameraXMV.get() + (viewSize.width - prev.width) / (2 * z));
        if (prev.height !== viewSize.height) cameraYMV.set(cameraYMV.get() + (viewSize.height - prev.height) * (1 - z) / (2 * z));
    }, [enabled, viewSize, cameraXMV, cameraYMV, zoomMV]);

    // The middle button's own defaults (Chromium's autoscroll on the press,
    // the X11 primary-selection paste on the release) never run on the
    // board: its auxclick is cancelled here, its press in
    // handlePanPointerDown. A right click's context menu is StackScene's.
    useEffect(() => {
        const el = rootDivRef.current;
        if (!el || !enabled) return;
        const onAuxClick = (e: MouseEvent) => { if (e.button === 1) e.preventDefault(); };
        el.addEventListener('auxclick', onAuxClick);
        return () => el.removeEventListener('auxclick', onAuxClick);
    }, [enabled, rootDivRef]);

    // The pan drag: a PAN_BUTTONS press anywhere, or a left press off the
    // cards that drag.
    const dragRef = useRef<{
        startScreenX: number;
        startScreenY: number;
        startCamX: number;
        startCamY: number;
        pointerId: number;
        moved: boolean;
        /** The root's own cursor, put back when the pan ends. */
        cursor: string;
    } | null>(null);

    const handlePanPointerDown = useCallback(
        (e: React.PointerEvent<HTMLDivElement>) => {
            if (!enabled) return;
            const pansAnywhere = PAN_BUTTONS.has(e.button);
            const pansHere = e.button === 0
                && !(panBlockSelector && (e.target as Element).closest(panBlockSelector));
            if (!pansAnywhere && !pansHere) return;
            if (e.button === 1) e.preventDefault();
            dragRef.current = {
                startScreenX: e.clientX,
                startScreenY: e.clientY,
                startCamX: cameraXMV.get(),
                startCamY: cameraYMV.get(),
                pointerId: e.pointerId,
                moved: false,
                cursor: '',
            };
        },
        [enabled, panBlockSelector, cameraXMV, cameraYMV],
    );

    const handlePanPointerMove = useCallback(
        (e: React.PointerEvent<HTMLDivElement>) => {
            const drag = dragRef.current;
            if (!drag) return;
            const deltaX = e.clientX - drag.startScreenX;
            const deltaY = e.clientY - drag.startScreenY;
            if (!drag.moved) {
                if (Math.abs(deltaX) + Math.abs(deltaY) <= PAN_THRESHOLD) return;
                // Captured only once it is a pan: a click on a card or a
                // button under the pointer stays theirs.
                drag.moved = true;
                try { e.currentTarget.setPointerCapture(drag.pointerId); } catch { /* the pointer is gone */ }
                drag.cursor = e.currentTarget.style.cursor;
                e.currentTarget.style.cursor = 'grabbing';
                window.dispatchEvent(new Event('canvas-pan-start'));
            }
            const z = zoomMV.get();
            cameraXMV.set(drag.startCamX - deltaX / z);
            cameraYMV.set(drag.startCamY - deltaY / z);
        },
        [cameraXMV, cameraYMV, zoomMV],
    );

    /** Ends a pan. True when the press never travelled (a click on the
     *  background). */
    const handlePanPointerUp = useCallback((e?: React.PointerEvent<HTMLDivElement>) => {
        const drag = dragRef.current;
        dragRef.current = null;
        if (!drag) return false;
        if (drag.moved) {
            if (e) e.currentTarget.style.cursor = drag.cursor;
            window.dispatchEvent(new Event('canvas-pan-end'));
        }
        return !drag.moved;
    }, []);

    return {
        cameraXMV,
        cameraYMV,
        zoomMV,
        panX,
        panY,
        handlePanPointerDown,
        handlePanPointerMove,
        handlePanPointerUp,
    };
}
