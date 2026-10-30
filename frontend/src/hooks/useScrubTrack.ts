// Axis-agnostic scrub-track mechanics for the history rail: pointer
// capture, endScrub and snap index, run vertically (left-dock) or
// horizontally (top-dock). The pip scrubbers' knob drag (useKnobDrag)
// commits on every snap instead.
//
// Commit discipline: LOCAL state during the gesture, one onCommit on
// release (the sash convention — the history selection lives in a zustand
// slice, and a store write per snap step would re-render every mounted
// renderer in the cell for the whole gesture). `dragIndex` is the live
// visual position for the component to render mid-gesture. `onEnd` fires
// whenever a gesture ends — release, pointercancel, or a buttonless move —
// before any onCommit, so a caller can drop per-gesture state even when the
// gesture ends without a commit.

import { useCallback, useEffect, useRef, useState } from 'react';

export interface ScrubTrack {
    trackRef: React.RefObject<HTMLDivElement | null>;
    /** Index the gesture is currently over, or null when not dragging. */
    dragIndex: number | null;
    dragging: boolean;
    handlers: {
        onPointerDown: (e: React.PointerEvent) => void;
        onPointerMove: (e: React.PointerEvent) => void;
        onPointerUp: (e: React.PointerEvent) => void;
        onPointerCancel: (e: React.PointerEvent) => void;
    };
}

/** Per-item spacing in px along the track: the available extent divided
 *  among the items, CAPPED at `maxGap` — so a rail with few items keeps a
 *  short, comfortable scrub distance and its track terminates early
 *  instead of stretching item gaps across the whole pane. */
export const scrubSpacing = (
    extent: number, count: number, pad: number, maxGap: number,
): number => {
    if (count <= 1) return 0;
    const usable = Math.max(0, extent - 2 * pad);
    return Math.min(maxGap, usable / (count - 1));
};

export function useScrubTrack(opts: {
    axis: 'x' | 'y';
    count: number;
    pad: number;
    /** Cap on the px distance between adjacent items (default: none — the
     *  items spread across the full track). */
    maxGap?: number;
    onCommit: (index: number) => void;
    /** Called when a gesture ends, committed or not (before onCommit). */
    onEnd?: () => void;
}): ScrubTrack {
    const { axis, count, pad, maxGap = Infinity, onCommit } = opts;
    const trackRef = useRef<HTMLDivElement | null>(null);
    // A ref so endScrub stays stable across renders.
    const onEndRef = useRef(opts.onEnd);
    useEffect(() => { onEndRef.current = opts.onEnd; });
    const [dragIndex, setDragIndex] = useState<number | null>(null);
    const draggingRef = useRef(false);
    const lastIdxRef = useRef<number | null>(null);

    const indexFromEvent = useCallback((e: React.PointerEvent): number | null => {
        const el = trackRef.current;
        if (!el || count <= 0) return null;
        const rect = el.getBoundingClientRect();
        const rectExtent = axis === 'y' ? rect.height : rect.width;
        const spacing = scrubSpacing(rectExtent, count, pad, maxGap);
        const trackLen = spacing * (count - 1);
        const origin = axis === 'y' ? rect.top : rect.left;
        const client = axis === 'y' ? e.clientY : e.clientX;
        // A pointer past a terminated track clamps to the last item.
        const rel = Math.min(Math.max(client - origin - pad, 0), trackLen);
        return spacing > 0
            ? Math.min(count - 1, Math.max(0, Math.round(rel / spacing)))
            : 0;
    }, [axis, count, pad, maxGap]);

    const endScrub = useCallback((e: React.PointerEvent) => {
        onEndRef.current?.();
        draggingRef.current = false;
        setDragIndex(null);
        const el = e.currentTarget as HTMLElement;
        if (el.hasPointerCapture?.(e.pointerId)) el.releasePointerCapture(e.pointerId);
    }, []);

    const onPointerDown = useCallback((e: React.PointerEvent) => {
        if (e.button !== 0) return;
        e.stopPropagation();
        e.preventDefault();
        (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
        draggingRef.current = true;
        const idx = indexFromEvent(e);
        lastIdxRef.current = idx;
        setDragIndex(idx);
    }, [indexFromEvent]);

    const onPointerMove = useCallback((e: React.PointerEvent) => {
        if (!draggingRef.current) return;
        // A lost/cancelled gesture must never leave the dot stuck "grabbed".
        if (e.buttons === 0) { endScrub(e); return; }
        const idx = indexFromEvent(e);
        if (idx !== null && idx !== lastIdxRef.current) {
            lastIdxRef.current = idx;
            setDragIndex(idx);
        }
    }, [indexFromEvent, endScrub]);

    const onPointerUp = useCallback((e: React.PointerEvent) => {
        if (!draggingRef.current) return;
        const idx = lastIdxRef.current;
        endScrub(e);
        if (idx !== null) onCommit(idx);
    }, [endScrub, onCommit]);

    return {
        trackRef,
        dragIndex,
        dragging: dragIndex !== null,
        handlers: {
            onPointerDown,
            onPointerMove,
            onPointerUp,
            onPointerCancel: endScrub,
        },
    };
}
