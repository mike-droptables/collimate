import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

// ── Two-finger swipe navigation ─────────────────────────────────────
/** Wheel-swipe travel (px of accumulated deltaX) per selection step. */
export const SWIPE_STEP_PX = 140;
/** Touchscreen finger travel (px) per selection step. */
export const TOUCH_SWIPE_STEP_PX = 90;
/** Horizontal wheel events further apart than this start a new gesture
 *  (and get re-classified). */
export const SWIPE_GESTURE_GAP_MS = 140;
/** Smallest |deltaX| an INTEGER pixel-mode wheel event needs to read as a
 *  mouse tilt-wheel notch. Notches land well above it on every platform
 *  (±53 GTK, ±100 Chromium default, ±120 legacy); touchpad streams open
 *  small — and are fractional on most platforms anyway. */
export const WHEEL_NOTCH_MIN_PX = 40;

/** Classify the FIRST horizontal wheel event of a burst: a mouse tilt
 *  wheel ('wheel' — its discrete notches must do NOTHING) vs a touchpad
 *  two-finger swipe ('touchpad' — steps the selection). The verdict
 *  sticks for the whole burst. */
export function classifyHorizontalWheel(e: { deltaMode: number; deltaX: number }): 'wheel' | 'touchpad' {
    if (e.deltaMode !== 0) return 'wheel';               // line/page = real wheel
    if (!Number.isInteger(e.deltaX)) return 'touchpad';  // fractional px = touchpad
    return Math.abs(e.deltaX) >= WHEEL_NOTCH_MIN_PX ? 'wheel' : 'touchpad';
}

// Walk up from `start` to (but not including) `until`, looking for an element
// that can still absorb a horizontal scroll of `deltaX`. Lets a two-finger pan
// defer to a horizontally-scrollable cell region (code, canvas, …) until it
// reaches its edge, then take over — i.e. native scroll chaining.
function horizontalScrollConsumer(start: Element | null, until: Element, deltaX: number): boolean {
    let node: Element | null = start;
    while (node && node !== until) {
        if (node instanceof HTMLElement) {
            const max = node.scrollWidth - node.clientWidth;
            if (max > 1) {
                const ox = getComputedStyle(node).overflowX;
                if (ox === 'auto' || ox === 'scroll') {
                    if (deltaX > 0 && node.scrollLeft < max - 1) return true;
                    if (deltaX < 0 && node.scrollLeft > 1) return true;
                }
            }
        }
        node = node.parentElement;
    }
    return false;
}

/** Owns main-view swipe listeners; camera tracking stays with useStackCamera. */
export function useSwipeNavigate(
    rootDivRef: RefObject<HTMLDivElement | null>,
    isZoomedOut: boolean,
    onSwipeNavigate?: (delta: -1 | 1) => void,
) {
    // Two-finger swipe navigation — trackpad (horizontal wheel stream) and
    // touchscreen (two-finger drag), normal stack mode only. The old free
    // horizontal pan is gone: a mouse's tilt wheel does NOTHING here (see
    // classifyHorizontalWheel — its burst is ignored wholesale), and a
    // touchpad swipe steps the SELECTION one stack per SWIPE_STEP_PX of
    // travel instead of panning; the camera follows through the ordinary
    // selection-tracking effect. Vertical wheels fall through to native
    // cell scrolling untouched, and a horizontally-scrollable cell region
    // still consumes the gesture until it hits its edge (scroll chaining).
    // Trackpad keeps scroll semantics (swipe right steps to the stack on
    // the right); touch keeps grab semantics (fingers left = look right),
    // matching the directions the old pans moved the view. The zoom-out
    // canvas attaches its own wheel handler in usePanZoomCamera; swipe
    // listeners are attached only in the main view.
    const onSwipeNavigateRef = useRef(onSwipeNavigate);
    useLayoutEffect(() => { onSwipeNavigateRef.current = onSwipeNavigate; }, [onSwipeNavigate]);
    useEffect(() => {
        const el = rootDivRef.current;
        if (!el || isZoomedOut) return;

        let wheelKind: 'wheel' | 'touchpad' | null = null;
        let wheelAccum = 0;
        let wheelEndTimer: ReturnType<typeof setTimeout> | null = null;
        const endWheel = () => { wheelKind = null; wheelAccum = 0; wheelEndTimer = null; };
        const onWheel = (e: WheelEvent) => {
            if (isZoomedOut) return;
            if (e.ctrlKey) return;                                  // pinch-zoom — leave alone
            if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return;  // vertical — native scroll
            if (horizontalScrollConsumer(e.target as Element, el, e.deltaX)) return;
            if (wheelKind === null) wheelKind = classifyHorizontalWheel(e);
            if (wheelEndTimer) clearTimeout(wheelEndTimer);
            wheelEndTimer = setTimeout(endWheel, SWIPE_GESTURE_GAP_MS);
            if (wheelKind === 'wheel') return;                      // tilt wheel — dead
            e.preventDefault();
            wheelAccum += e.deltaX;
            while (wheelAccum >= SWIPE_STEP_PX) { wheelAccum -= SWIPE_STEP_PX; onSwipeNavigateRef.current?.(1); }
            while (wheelAccum <= -SWIPE_STEP_PX) { wheelAccum += SWIPE_STEP_PX; onSwipeNavigateRef.current?.(-1); }
        };

        let touchActive = false;
        let touchLastAvgX = 0;
        let touchAccum = 0;
        const avg2 = (t: TouchList) => (t[0].clientX + t[1].clientX) / 2;
        const onTouchStart = (e: TouchEvent) => {
            if (isZoomedOut || e.touches.length !== 2) return;
            touchActive = true;
            touchLastAvgX = avg2(e.touches);
            touchAccum = 0;
            e.preventDefault();
        };
        const onTouchMove = (e: TouchEvent) => {
            if (!touchActive || e.touches.length < 2) return;
            e.preventDefault();
            const x = avg2(e.touches);
            touchAccum += touchLastAvgX - x;                        // grab semantics
            touchLastAvgX = x;
            while (touchAccum >= TOUCH_SWIPE_STEP_PX) { touchAccum -= TOUCH_SWIPE_STEP_PX; onSwipeNavigateRef.current?.(1); }
            while (touchAccum <= -TOUCH_SWIPE_STEP_PX) { touchAccum += TOUCH_SWIPE_STEP_PX; onSwipeNavigateRef.current?.(-1); }
        };
        const onTouchEnd = (e: TouchEvent) => {
            if (!touchActive || e.touches.length >= 2) return;
            touchActive = false;
        };

        el.addEventListener('wheel', onWheel, { passive: false });
        el.addEventListener('touchstart', onTouchStart, { passive: false });
        el.addEventListener('touchmove', onTouchMove, { passive: false });
        el.addEventListener('touchend', onTouchEnd);
        el.addEventListener('touchcancel', onTouchEnd);
        return () => {
            el.removeEventListener('wheel', onWheel);
            el.removeEventListener('touchstart', onTouchStart);
            el.removeEventListener('touchmove', onTouchMove);
            el.removeEventListener('touchend', onTouchEnd);
            el.removeEventListener('touchcancel', onTouchEnd);
            if (wheelEndTimer) clearTimeout(wheelEndTimer);
        };
    }, [isZoomedOut, rootDivRef]);

}
