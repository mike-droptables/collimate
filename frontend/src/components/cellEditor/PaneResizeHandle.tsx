import React, { useRef } from 'react';
import { cn } from '@/lib/utils';

/** The edge of a docked pane its handle sits on: the one facing the content. */
export type PaneEdge = 'left' | 'right' | 'top' | 'bottom';

type Axis = 'x' | 'y';
const axisOf = (edge: PaneEdge): Axis => (edge === 'left' || edge === 'right' ? 'x' : 'y');

/** A move toward the content grows the pane: rightward or downward for a
 *  right or bottom edge, leftward or upward for a left or top edge. */
const GROWS: Record<PaneEdge, 1 | -1> = { right: 1, bottom: 1, left: -1, top: -1 };

const EDGE_CLASS: Record<PaneEdge, string> = {
    right: 'top-0 bottom-0 -right-px w-1.5 cursor-col-resize',
    left: 'top-0 bottom-0 -left-px w-1.5 cursor-col-resize',
    bottom: 'left-0 right-0 -bottom-px h-1.5 cursor-row-resize',
    top: 'left-0 right-0 -top-px h-1.5 cursor-row-resize',
};

/** The arrow key that moves each edge outward, toward the content: it grows
 *  the pane (the opposite arrow shrinks it). */
const GROW_KEY: Record<PaneEdge, string> = { right: 'ArrowRight', left: 'ArrowLeft', bottom: 'ArrowDown', top: 'ArrowUp' };
const SHRINK_KEY: Record<PaneEdge, string> = { right: 'ArrowLeft', left: 'ArrowRight', bottom: 'ArrowUp', top: 'ArrowDown' };

/** One arrow press on the focused handle moves the edge this far. */
const KEY_STEP = 8;
const CLICK_MAX_MS = 250;
const CLICK_MOVE_PX = 4;

/** The largest a pane may be along `axis`: two thirds of its flex container
 *  (the cell's pane row; owner 2026-10-09, over the earlier half), and no
 *  more than leaves the container `reserve` (lib/cellLayout paneReserve) —
 *  where the pane's style holds it. */
function maxSizeFor(pane: HTMLElement | null, axis: Axis, min: number, reserve: (room: number) => number): number {
    const rect = pane?.parentElement?.getBoundingClientRect();
    const room = (axis === 'x' ? rect?.width : rect?.height) ?? 0;
    return Math.max(min, Math.min(room * 2 / 3, room - reserve(room)));
}

const clampSize = (v: number, min: number, max: number) => Math.round(Math.min(max, Math.max(min, v)));

export interface PaneResizeHandleProps {
    /** The pane: its rendered size starts a drag, its parent bounds it. */
    paneRef: React.RefObject<HTMLElement | null>;
    /** The pane's content-facing edge. */
    edge: PaneEdge;
    /** The pane's size along the edge's axis (a column's width, a band's height). */
    size: number;
    /** The smallest size along that axis. */
    min: number;
    /** What the pane leaves the rest of its container, for a container
     *  `room` pixels long (lib/cellLayout paneReserve's `px`). */
    reserve: (room: number) => number;
    /** What the pane holds, in the handle's name and tooltip ("files"). */
    label: string;
    /** The size mid-drag; null when the gesture ends. */
    onPreview: (v: number | null) => void;
    /** The new size, once: on release, or per arrow key. */
    onCommit: (v: number) => void;
    /** A quick stationary press hides the pane (absent: it never hides). */
    onCollapse?: () => void;
}

/** The resize handle on a docked pane's content-facing edge (the Files
 *  pane's right or bottom, the status pane's left or top): a 6px strip over
 *  the border, line-2 on hover, with the matching resize cursor. Dragging
 *  the edge toward the content grows the pane, held between `min` and two
 *  thirds of the pane's container, short of its `reserve`. The size rides on local state during the
 *  gesture and commits ONCE on release — a store write per pointermove would
 *  re-render every tab's renderer. A quick stationary press (250ms, 4px)
 *  hides the pane; a drag out and back, a cancel or a long hold does not. */
export function PaneResizeHandle({ paneRef, edge, size, min, reserve, label, onPreview, onCommit, onCollapse }: PaneResizeHandleProps) {
    const axis = axisOf(edge);
    const grows = GROWS[edge];
    const drag = useRef<{ pointerId: number; start: number; startX: number; startY: number; startedAt: number; moved: boolean; startSize: number; max: number; last: number } | null>(null);
    const along = (e: React.PointerEvent) => (axis === 'x' ? e.clientX : e.clientY);
    const sizeAt = (e: React.PointerEvent, d: NonNullable<typeof drag.current>) =>
        clampSize(d.startSize + grows * (along(e) - d.start), min, d.max);
    const end = (e: React.PointerEvent, commit: boolean) => {
        const d = drag.current;
        if (!d || e.pointerId !== d.pointerId) return;
        drag.current = null;
        e.currentTarget.releasePointerCapture?.(e.pointerId);
        onPreview(null);
        if (!commit) return;
        if (Math.hypot(e.clientX - d.startX, e.clientY - d.startY) > CLICK_MOVE_PX) d.moved = true;
        if (d.moved) d.last = sizeAt(e, d);
        if (!d.moved && Date.now() - d.startedAt <= CLICK_MAX_MS) onCollapse?.();
        else if (d.moved && d.last !== d.startSize) onCommit(d.last);
    };
    return (
        <div
            role="separator"
            aria-orientation={axis === 'x' ? 'vertical' : 'horizontal'}
            aria-label={`Resize ${label}`}
            aria-valuenow={size}
            aria-valuemin={min}
            tabIndex={0}
            title={onCollapse ? `Drag to resize or click to hide ${label}` : `Drag to resize the ${label}`}
            data-resize-edge={edge}
            onPointerDown={(e) => {
                if (e.button !== 0) return;
                e.preventDefault();
                e.stopPropagation();
                const rect = paneRef.current?.getBoundingClientRect();
                const startSize = (axis === 'x' ? rect?.width : rect?.height) || size;
                drag.current = { pointerId: e.pointerId, start: along(e), startX: e.clientX, startY: e.clientY,
                    startedAt: Date.now(), moved: false, startSize, max: maxSizeFor(paneRef.current, axis, min, reserve), last: startSize };
                e.currentTarget.setPointerCapture?.(e.pointerId);
            }}
            onPointerMove={(e) => {
                const d = drag.current;
                if (!d || e.pointerId !== d.pointerId) return;
                if (Math.hypot(e.clientX - d.startX, e.clientY - d.startY) > CLICK_MOVE_PX) d.moved = true;
                if (!d.moved) return;
                d.last = sizeAt(e, d);
                onPreview(d.last);
            }}
            onPointerUp={(e) => end(e, true)}
            onPointerCancel={(e) => end(e, false)}
            onKeyDown={(e) => {
                const dir = e.key === GROW_KEY[edge] ? 1 : e.key === SHRINK_KEY[edge] ? -1 : 0;
                if (!dir) return;
                e.preventDefault();
                const next = clampSize(size + dir * KEY_STEP, min, maxSizeFor(paneRef.current, axis, min, reserve));
                if (next !== size) onCommit(next);
            }}
            className={cn(
                'absolute z-10 transition-colors hover:bg-line-2 focus-visible:bg-line-2 focus-visible:outline-none',
                EDGE_CLASS[edge],
            )}
            style={{ touchAction: 'none' }}
        />
    );
}
