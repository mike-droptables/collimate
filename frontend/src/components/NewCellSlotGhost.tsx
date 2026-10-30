import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import type { FanRange } from '@/lib/fanOut';
import { measureDropSlot } from '@/hooks/useCanvasFileDrop';
import { cn } from '@/lib/utils';

/** The pending-move tag's look (SceneDragOverlays' arrowTag), placed here in
 *  screen space instead of under a disc. */
const TAG = 'bg-sheet px-3 h-[1.625rem] inline-flex items-center rounded-sm border border-teal text-ink text-[0.75rem] font-semibold uppercase tracking-[0.08em] shadow-(--shadow) whitespace-nowrap';
const TAG_HEIGHT = 26;
/** Keeps the tag whole at the layer's left and right edges. */
const TAG_HALF_WIDTH = 52;

/** Where the slot sits in the layer: the bar's x and vertical extent. */
interface Place { x: number; top: number; bottom: number; width: number }

const samePlace = (a: Place | null | undefined, b: Place | null) =>
    a === b || (!!a && !!b && a.x === b.x && a.top === b.top && a.bottom === b.bottom && a.width === b.width);

/** The canvas's NEW CELL target while a drag the canvas takes (OS files, a
 *  cell file dropped off every browser) is over it: a 3px teal bar in the
 *  gap where the new stack will be inserted, tagged NEW CELL, over a 2px
 *  inset ring on the scene. It lives in StackApp's screen-space layer and
 *  places itself from the same client rects the drop uses (measureDropSlot),
 *  so it never shows a slot the drop would not take, in the main row, a fan
 *  or the overview. The pointer is read from a ref each frame, so only this
 *  component re-renders, and only when the slot moves. A cell's Files pane
 *  takes its own drags with its own target (an inset "Add to <cell>" frame),
 *  so this never draws inside a cell. */
export function NewCellSlotGhost({ pointerX, fan }: { pointerX: RefObject<number | null>; fan: FanRange | null }) {
    const layerRef = useRef<HTMLDivElement>(null);
    // undefined until the first frame measures; null = no column to place
    // the slot against (an empty channel): the stack is appended.
    const [place, setPlace] = useState<Place | null | undefined>(undefined);
    const fanRef = useRef(fan);
    useEffect(() => { fanRef.current = fan; }, [fan]);

    useLayoutEffect(() => {
        let frame = 0;
        const measure = () => {
            const layer = layerRef.current;
            const x = pointerX.current;
            if (!layer || x === null) return;
            const origin = layer.getBoundingClientRect();
            const slot = measureDropSlot(x, fanRef.current);
            const clampY = (y: number) => Math.round(Math.min(origin.height, Math.max(0, y - origin.top)));
            const next = slot && {
                x: Math.round(slot.gapX - origin.left),
                top: clampY(slot.top),
                bottom: clampY(slot.bottom),
                width: Math.round(origin.width),
            };
            setPlace(prev => (samePlace(prev, next) ? prev : next));
        };
        const tick = () => {
            measure();
            frame = requestAnimationFrame(tick);
        };
        tick();
        return () => cancelAnimationFrame(frame);
    }, [pointerX]);

    const tagLeft = place ? Math.min(Math.max(place.x, TAG_HALF_WIDTH), Math.max(TAG_HALF_WIDTH, place.width - TAG_HALF_WIDTH)) : 0;
    // Above the bar when there is room, else just inside its top.
    const tagTop = place ? (place.top >= TAG_HEIGHT + 12 ? place.top - TAG_HEIGHT - 8 : place.top + 8) : 0;

    return (
        <div ref={layerRef} className="pointer-events-none absolute inset-0 z-[60] ring-2 ring-inset ring-teal">
            {place && (
                <>
                    <div
                        data-new-cell-slot
                        style={{
                            position: 'absolute',
                            left: place.x,
                            top: place.top,
                            height: Math.max(0, place.bottom - place.top),
                            width: 3,
                            background: 'var(--teal)',
                            boxShadow: '0 0 0 3px var(--teal-soft)',
                            transform: 'translateX(-50%)',
                            borderRadius: 2,
                        }}
                    />
                    <div className={cn(TAG, 'absolute')} style={{ left: tagLeft, top: tagTop, transform: 'translateX(-50%)' }}>
                        NEW CELL
                    </div>
                </>
            )}
            {place === null && (
                <div className={cn(TAG, 'absolute left-1/2 top-6 -translate-x-1/2')}>NEW CELL</div>
            )}
        </div>
    );
}
