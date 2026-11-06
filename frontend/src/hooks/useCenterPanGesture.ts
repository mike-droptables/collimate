import { useEffect, useMemo, useRef } from 'react';
import { blockNativeSelection } from '@/lib/nativeSelection';

/** How long a click's recentre waits for a second click: the head's
 *  double-click maximises the stack, and its first click must not set the
 *  camera moving before the second one lands. */
export const CENTER_CLICK_DELAY_MS = 125;

interface CenterPanCallbacks {
    onCenterStack?: () => void;
    onCenterPanStart?: (e: React.PointerEvent) => void;
    onCenterPanMove?: (e: React.PointerEvent) => void;
    /** Returns whether the gesture actually moved, so the trailing click
     *  can be swallowed instead of firing a recenter. */
    onCenterPanEnd?: () => boolean;
}

/**
 * Center-stack control gesture: click recenters the stack, click-and-drag
 * pans the viewport so the stack tracks the cursor. Shared by the stack
 * head's empty space (StackGroup) and the fan bracket (FanFrame, drag
 * only) so the pointer-capture / click-suppression dance lives in one
 * place.
 *
 * A pointer click's recentre waits ``CENTER_CLICK_DELAY_MS``: a second
 * press in that window (the second half of a double-click, or a drag)
 * cancels it. A keyboard click (``detail`` 0) recentres at once.
 */
export function useCenterPanGesture({
    onCenterStack, onCenterPanStart, onCenterPanMove, onCenterPanEnd,
}: CenterPanCallbacks) {
    // Set when a drag actually moves; reset at the start of the next gesture.
    const suppressClickRef = useRef(false);
    // The click's recentre waiting out the double-click window.
    const pendingRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    useEffect(() => () => {
        if (pendingRef.current !== null) clearTimeout(pendingRef.current);
    }, []);

    const handlers = useMemo(() => {
        const cancelPending = () => {
            if (pendingRef.current === null) return;
            clearTimeout(pendingRef.current);
            pendingRef.current = null;
        };
        return {
            onPointerDown: (e: React.PointerEvent) => {
                // A new press inside the window is a double-click's second
                // half or a drag: the waiting recentre never runs.
                cancelPending();
                // A quick press-and-drag must slide the view, never sweep a text
                // selection across the cells it crosses.
                blockNativeSelection(e);
                if (e.button !== 0 || !onCenterPanStart) return;
                e.stopPropagation();
                (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
                suppressClickRef.current = false;
                onCenterPanStart(e);
            },
            onPointerMove: (e: React.PointerEvent) => onCenterPanMove?.(e),
            onPointerUp: (e: React.PointerEvent) => {
                if (e.button !== 0) return;
                const el = e.currentTarget as HTMLElement;
                if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
                suppressClickRef.current = onCenterPanEnd?.() ?? false;
            },
            // A cancelled pointer (touch takeover, OS gesture) ends the pan the
            // same way, so no gesture state outlives it.
            onPointerCancel: () => { onCenterPanEnd?.(); },
            onClick: (e: React.MouseEvent) => {
                e.stopPropagation();
                cancelPending();
                if (suppressClickRef.current) {
                    suppressClickRef.current = false;
                    return;
                }
                if (!onCenterStack) return;
                // A keyboard click has no double-click to wait for.
                if (e.detail === 0) { onCenterStack(); return; }
                // The second click of a double-click: the double-click owns it.
                if (e.detail > 1) return;
                pendingRef.current = setTimeout(() => {
                    pendingRef.current = null;
                    onCenterStack();
                }, CENTER_CLICK_DELAY_MS);
            },
        };
    }, [onCenterStack, onCenterPanStart, onCenterPanMove, onCenterPanEnd]);

    return { handlers };
}
