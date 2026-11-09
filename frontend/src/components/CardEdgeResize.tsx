import { useEffect, useRef, useState } from 'react';
import type { MotionValue } from 'motion/react';
import { useSpatialStore } from '@/stores/useSpatialStore';
import {
    EDGE_GLOW_FADE_MS, EDGE_HOVER_ARM_MS, anchorFor, edgeDragWidth, type EdgeResizeSideGeometry,
} from '@/lib/edgeResize';
import { MIN_STACK_WIDTH } from '@/hooks/useStackLayout';

/** z of the strips and their glow: above the selected stack (100), the
 *  pooled surface overlay (110 — so a renderer iframe or shadow surface
 *  never swallows the grab) and the pan-align glow (120); below the
 *  cell-diff zones (130, which own the header rows the strips never
 *  reach) and every moving / key-request stack lift (500+). */
const STRIP_Z = 125;

/** One side of one column's card: the resize grab below the header
 *  (world space, rendered by StackScene). A pointer resting on the side
 *  (EDGE_HOVER_ARM_MS) fades it in as a line that answers the pointer
 *  (ink-2, as a stack head's rail goes a step lighter), and leaving fades
 *  it out; a drag turns it teal (live) and sets this column's width so
 *  the side stays exactly under the pointer while the opposite edge and
 *  the camera stand still (useSpatialStore.widthDrag). The write is always
 *  this column's own override — never the shared width, never another
 *  column's. */
export function CardEdgeResize({
    columnId, geometry, width, screenMax, zoom, onDragStart,
}: {
    /** Display id of the column (a real stack id or `fan-<cellId>`). */
    columnId: string;
    geometry: EdgeResizeSideGeometry;
    /** The column's current laid-out width (world px). */
    width: number;
    /** The layout's widest column. */
    screenMax: number;
    /** The scene zoom — screen px / zoom = world px. */
    zoom: MotionValue<number>;
    /** Pointer went down on the strip (StackScene selects the column, so
     *  the camera's tracking after release follows it). */
    onDragStart?: () => void;
}) {
    const { side, rects, glow } = geometry;
    const [hover, setHover] = useState(false);
    const [dragging, setDragging] = useState(false);
    const dragRef = useRef<{ pointerId: number; startX: number; startWidth: number; last: number } | null>(null);
    const armRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const disarm = () => {
        if (armRef.current === null) return;
        clearTimeout(armRef.current);
        armRef.current = null;
    };

    // Unmounting mid-drag (the column went away) must not leave the layout
    // anchored and the camera parked on a ghost, nor an arming timer firing
    // into a gone strip.
    useEffect(() => () => {
        if (armRef.current !== null) clearTimeout(armRef.current);
        if (!dragRef.current) return;
        const s = useSpatialStore.getState();
        if (s.widthDrag?.id === columnId) s.setWidthDrag(null);
    }, [columnId]);

    const end = (e: React.PointerEvent) => {
        const d = dragRef.current;
        if (!d || e.pointerId !== d.pointerId) return;
        e.stopPropagation();
        dragRef.current = null;
        try { (e.target as Element).releasePointerCapture(e.pointerId); } catch { /* already released */ }
        setDragging(false);
        const s = useSpatialStore.getState();
        if (s.widthDrag?.id === columnId) s.setWidthDrag(null);
    };

    const label = `Resize width (${side} edge)`;
    const lit = hover || dragging;
    return (
        <div
            data-edge-resize={side}
            data-edge-resize-column={columnId}
            style={{
                position: 'absolute', left: 0, top: 0, width: 0, height: 0,
                zIndex: STRIP_Z, pointerEvents: 'none',
            }}
            onPointerEnter={() => {
                disarm();
                armRef.current = setTimeout(() => {
                    armRef.current = null;
                    setHover(true);
                }, EDGE_HOVER_ARM_MS);
            }}
            onPointerLeave={() => {
                disarm();
                setHover(false);
            }}
            onPointerDown={(e) => {
                if (e.button !== 0) return;
                // Its own pointerdown only: no canvas pan, no card select.
                e.stopPropagation();
                e.preventDefault();
                // A grab is intent: lit now, and still lit after the release
                // while the pointer rests on the strip.
                disarm();
                setHover(true);
                dragRef.current = { pointerId: e.pointerId, startX: e.clientX, startWidth: width, last: width };
                // Captured on the hit rect itself so the drag keeps its
                // events over iframes and after the pointer leaves the strip.
                try { (e.target as Element).setPointerCapture(e.pointerId); } catch { /* unsupported (jsdom) */ }
                setDragging(true);
                useSpatialStore.getState().setWidthDrag({ id: columnId, anchor: anchorFor(side) });
                onDragStart?.();
            }}
            onPointerMove={(e) => {
                const d = dragRef.current;
                if (!d || e.pointerId !== d.pointerId) return;
                e.stopPropagation();
                const w = edgeDragWidth(d.startWidth, e.clientX - d.startX, zoom.get(), side, MIN_STACK_WIDTH, screenMax);
                if (w === d.last) return;
                d.last = w;
                useSpatialStore.getState().setStackWidth(columnId, w);
            }}
            onPointerUp={end}
            onPointerCancel={end}
        >
            {rects.map((r, i) => (
                <div
                    key={i}
                    data-edge-resize-hit
                    role="separator"
                    aria-orientation="vertical"
                    aria-label={label}
                    title="Drag to resize"
                    style={{
                        position: 'absolute', left: r.left, top: r.top, width: r.width, height: r.height,
                        pointerEvents: 'auto', cursor: 'ew-resize', touchAction: 'none',
                    }}
                />
            ))}
            <div
                data-edge-resize-glow
                aria-hidden
                style={{
                    position: 'absolute', left: glow.left, top: glow.top, width: glow.width, height: glow.height,
                    // Hidden is transparent, so the fade is a colour fade:
                    // an opacity fade runs on the compositor and turns the
                    // card's text grayscale while it runs.
                    background: !lit ? 'transparent' : dragging ? 'var(--teal)' : 'var(--ink-2)',
                    borderRadius: 1, pointerEvents: 'none',
                    transition: `background-color ${EDGE_GLOW_FADE_MS}ms ease`,
                }}
            />
        </div>
    );
}
