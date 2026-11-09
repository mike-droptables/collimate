import { useCallback, useEffect, useRef, useState, type FocusEvent } from 'react';

// A corner control that shows only when it is wanted: while the pointer is
// near it (within CORNER_REACH of its box, on any side), or while it holds
// keyboard focus. The renderer StyleControl's reach rule (the SDK's
// renderer-hooks `STYLE_CONTROL_REACH`), here for the app's own canvas
// chrome: never merely because the pointer is somewhere over the canvas. At
// rest the control stays in the tab order, so Tab reaches it and shows it.
// The host fades it by colour, never by opacity (canvas chrome,
// project_camera_motion_layers).

/** How near, in CSS px, the pointer must come to the control's box. */
export const CORNER_REACH = 40;

/** Whether (x, y) lies within `reach` px of the box, on any side. */
export function withinReach(
    box: { left: number; top: number; right: number; bottom: number },
    x: number, y: number, reach: number = CORNER_REACH,
): boolean {
    return x >= box.left - reach && x <= box.right + reach && y >= box.top - reach && y <= box.bottom + reach;
}

const focusVisible = (el: Element): boolean => {
    try {
        return el.matches(':focus-visible');
    } catch {
        return false;
    }
};

/** Watches the pointer over `watchSelector`'s nearest match round the
 *  control (else its parent), in the capture phase so a part that stops a
 *  move's propagation still reports it. Returns whether the control shows
 *  and the focus handlers to put on its root (focus inside it, shown by the
 *  keyboard, shows it). A pointer move re-renders only when the answer
 *  changes. */
export function useCornerReach(watchSelector?: string) {
    const [el, setEl] = useState<HTMLElement | null>(null);
    const attach = useCallback((node: HTMLElement | null) => setEl(node), []);
    const [near, setNearState] = useState(false);
    const nearRef = useRef(false);
    const setNear = useCallback((next: boolean) => {
        if (nearRef.current === next) return;
        nearRef.current = next;
        setNearState(next);
    }, []);
    const [focused, setFocused] = useState(false);

    useEffect(() => {
        if (!el) return undefined;
        const target = (watchSelector ? el.parentElement?.closest<HTMLElement>(watchSelector) : null) ?? el.parentElement;
        if (!target) return undefined;
        const onMove = (e: PointerEvent) => setNear(withinReach(el.getBoundingClientRect(), e.clientX, e.clientY));
        const onLeave = () => setNear(false);
        const opts: AddEventListenerOptions = { capture: true, passive: true };
        target.addEventListener('pointermove', onMove, opts);
        target.addEventListener('pointerleave', onLeave);
        return () => {
            target.removeEventListener('pointermove', onMove, opts);
            target.removeEventListener('pointerleave', onLeave);
        };
    }, [el, watchSelector, setNear]);

    const onFocus = useCallback((e: FocusEvent<HTMLElement>) => setFocused(focusVisible(e.target)), []);
    const onBlur = useCallback((e: FocusEvent<HTMLElement>) => {
        const to = e.relatedTarget;
        if (!(to instanceof Node) || !e.currentTarget.contains(to)) setFocused(false);
    }, []);

    return { attach, shown: near || focused, onFocus, onBlur };
}
