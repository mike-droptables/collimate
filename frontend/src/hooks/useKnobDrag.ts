// The knob drag of the pip scrubbers: the stack head's (StackCellScrubber)
// and the top bar's channel scrubber (ChannelScrubber). A press on the knob
// hands the pointer to the track; every move snaps to the slot whose centre
// is nearest the pointer and reports it once, as the knob passes it (the
// scrubber shows that cell at once, never on release). A release, a cancel,
// a lost capture or a buttonless move ends the drag, so a lost gesture never
// leaves the knob held. useScrubTrack (the history rail) commits on release
// instead.

import { useCallback, useRef, useState } from 'react';

/** The slot whose centre is nearest `clientX`, else -1 (no slot laid out). */
export function nearestSlot(slots: readonly (HTMLElement | null)[], clientX: number): number {
    let best = -1;
    let bestDistance = Infinity;
    slots.forEach((slot, i) => {
        if (!slot) return;
        const r = slot.getBoundingClientRect();
        const d = Math.abs(clientX - (r.left + r.width / 2));
        if (d < bestDistance) { bestDistance = d; best = i; }
    });
    return best;
}

export interface KnobDrag {
    /** On the track: it holds the pointer for the whole drag. */
    trackRef: React.RefObject<HTMLDivElement | null>;
    /** Slot `i`'s element, set from each slot's ref callback. */
    slotRef: (i: number) => (el: HTMLElement | null) => void;
    dragging: boolean;
    /** The knob's pointerdown, with the index it starts on. True when a
     *  drag started (a primary press). */
    start: (e: React.PointerEvent, from: number) => boolean;
    /** The track's pointermove. True when the move belongs to the drag. */
    move: (e: React.PointerEvent) => boolean;
    /** The track's pointerup / pointercancel / lostpointercapture. True
     *  when it ended the drag. */
    end: (e: React.PointerEvent) => boolean;
}

/**
 * @param count  the slots on the track now (slot refs past it are stale)
 * @param onSnap the knob reached slot `index` (once per slot it passes)
 */
export function useKnobDrag(count: number, onSnap: (index: number) => void): KnobDrag {
    const trackRef = useRef<HTMLDivElement | null>(null);
    const slots = useRef<(HTMLElement | null)[]>([]);
    const pointer = useRef<number | null>(null);
    const last = useRef(-1);
    const [dragging, setDragging] = useState(false);

    const slotRef = useCallback((i: number) => (el: HTMLElement | null) => { slots.current[i] = el; }, []);

    const end = useCallback((e: React.PointerEvent) => {
        if (pointer.current === null || e.pointerId !== pointer.current) return false;
        pointer.current = null;
        const track = trackRef.current;
        if (track?.hasPointerCapture?.(e.pointerId)) track.releasePointerCapture(e.pointerId);
        setDragging(false);
        return true;
    }, []);

    const start = useCallback((e: React.PointerEvent, from: number) => {
        if (e.button !== 0) return false;
        // The knob owns the press: nothing under it pans, selects or clicks.
        e.stopPropagation();
        e.preventDefault();
        pointer.current = e.pointerId;
        last.current = from;
        try { trackRef.current?.setPointerCapture?.(e.pointerId); } catch { /* the pointer is already gone */ }
        setDragging(true);
        return true;
    }, []);

    const move = useCallback((e: React.PointerEvent) => {
        if (pointer.current === null || e.pointerId !== pointer.current) return false;
        if (e.buttons === 0) { end(e); return true; }
        const k = nearestSlot(slots.current.slice(0, count), e.clientX);
        if (k >= 0 && k !== last.current) {
            last.current = k;
            onSnap(k);
        }
        return true;
    }, [count, end, onSnap]);

    return { trackRef, slotRef, dragging, start, move, end };
}
