import { useState, type CSSProperties, type KeyboardEvent } from 'react';
import { ArrowRight } from 'lucide-react';
import type { CellDiffZoneGeometry } from '@/lib/cellDiff';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';

/** z of a resting zone: above the selected stack (100), the pooled surface
 *  overlay (110) and the pan-align glow (120), so the gap stays hoverable
 *  over a neighbour's surfaces. */
const ZONE_Z = 130;
/** z while hovered: above every stack lift (drag 1000), so the pill is
 *  never clipped by a neighbour. */
const ZONE_Z_HOVER = 1100;
/** Pill height and its clearance above the band. */
const PILL_H = 20;
const PILL_GAP = 3;
/** The in-gap pill's clearance from the gap's sides and the band's top. */
const PILL_GAP_INSET = 1;
const PILL_GAP_TOP = 6;

/** The lit zone is a live affordance: the mock's 20px chip in teal, on a
 *  sheet, in the UI face. */
const pillLook: CSSProperties = {
    position: 'absolute', display: 'flex', alignItems: 'center', gap: '0.25rem',
    borderRadius: 999, whiteSpace: 'nowrap', fontFamily: 'var(--font-ui)', fontWeight: 500, lineHeight: 1,
    color: 'var(--teal)',
    background: 'var(--sheet)',
    border: '1px solid var(--teal)',
    boxShadow: 'var(--shadow)',
    pointerEvents: 'none',
};

/** The hover-and-click target in the gap between two adjacent columns
 *  (world space, rendered by StackScene; the mock has none, D-C18a keeps
 *  it): hovering (or focusing) says the right column's visible cell will
 *  be diffed against the left one's; a click, Enter or Space enters that
 *  diff. The hit area is `geometry.rects` — the gap plus a few px into
 *  each card over its 36px header; the gap rect is the one focusable
 *  button, the fuzz slices only widen its pointer target. The pill never
 *  takes pointer events. Between two stacks, or two fan columns, it sits
 *  just above the band, in the stack heads' gap between their arrows and
 *  menus; beside a fan column the row above the band is the fan's
 *  bracket, whose name and Collapse run to its edge, so the pill stands
 *  upright inside the gap. */
export function CellDiffZone({
    geometry, leftCellId, rightCellId, leftName, rightName,
}: {
    geometry: CellDiffZoneGeometry;
    /** The left column's visible cell — the base. */
    leftCellId: string;
    /** The right column's visible cell — the one that enters the diff. */
    rightCellId: string;
    leftName: string;
    rightName: string;
}) {
    const [hover, setHover] = useState(false);
    const [focused, setFocused] = useState(false);
    const lit = hover || focused;
    const title = `Diff ${rightName || 'untitled cell'} against ${leftName || 'untitled cell'}`;
    const { centerX, top, bottom, gapWidth, pillInGap } = geometry;
    const enter = () => useCollimateStore.getState().enterCellDiff(rightCellId, leftCellId);
    return (
        <div
            data-cell-diff-zone
            style={{
                position: 'absolute', left: 0, top: 0, width: 0, height: 0,
                zIndex: lit ? ZONE_Z_HOVER : ZONE_Z,
                pointerEvents: 'none',
            }}
            onPointerEnter={() => setHover(true)}
            onPointerLeave={() => setHover(false)}
        >
            {geometry.rects.map((r, i) => (
                <div
                    key={i}
                    {...(i === 0
                        ? {
                            // Owns its keys (lib/keyboardOwner): Enter and
                            // Space enter the diff, Esc lets go of focus.
                            ...KEYBOARD_OWNER_PROPS,
                            role: 'button', tabIndex: 0, 'aria-label': title,
                            onFocus: () => setFocused(true),
                            onBlur: () => setFocused(false),
                            onKeyDown: (e: KeyboardEvent<HTMLDivElement>) => {
                                if (e.key === 'Escape') {
                                    e.currentTarget.blur();
                                } else if (e.key === 'Enter' || e.key === ' ') {
                                    enter();
                                } else {
                                    return;
                                }
                                e.preventDefault();
                                e.stopPropagation();
                            },
                        }
                        : { 'aria-hidden': true })}
                    title={title}
                    data-cell-diff-hit
                    style={{
                        position: 'absolute', left: r.left, top: r.top, width: r.width, height: r.height,
                        pointerEvents: 'auto', cursor: 'pointer', outline: 'none',
                    }}
                    // Its own pointerdown only: keeps the canvas from starting
                    // a pan (and the card underneath from selecting) under a
                    // click meant for the zone.
                    onPointerDown={(e) => e.stopPropagation()}
                    // A click doesn't take focus — focus lights the zone, and
                    // that is for the keyboard; the pointer has hover.
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={(e) => {
                        e.stopPropagation();
                        enter();
                    }}
                />
            ))}
            {lit && (
                <>
                    <div
                        aria-hidden
                        style={{
                            position: 'absolute', left: centerX - 1, top, width: 2, height: bottom - top,
                            background: 'var(--teal)',
                            boxShadow: '0 0 0 3px var(--teal-soft)',
                            borderRadius: 1, pointerEvents: 'none',
                        }}
                    />
                    {pillInGap ? (
                        <div
                            data-cell-diff-pill
                            aria-hidden
                            style={{
                                ...pillLook,
                                left: centerX, top: top + PILL_GAP_TOP,
                                width: Math.max(0, gapWidth - 2 * PILL_GAP_INSET),
                                transform: 'translateX(-50%)',
                                flexDirection: 'column', justifyContent: 'center',
                                padding: '0.3125rem 0', fontSize: '0.5625rem', boxSizing: 'border-box',
                            }}
                        >
                            <span style={{ writingMode: 'vertical-rl' }}>Compare</span>
                            <ArrowRight style={{ width: '0.5625rem', height: '0.5625rem', flexShrink: 0 }} strokeWidth={2} />
                        </div>
                    ) : (
                        <div
                            data-cell-diff-pill
                            aria-hidden
                            style={{
                                ...pillLook,
                                left: centerX, top: top - PILL_GAP - PILL_H, height: PILL_H,
                                transform: 'translateX(-50%)',
                                padding: '0 0.5rem', fontSize: '0.6875rem',
                            }}
                        >
                            Compare
                            <ArrowRight style={{ width: '0.6875rem', height: '0.6875rem' }} strokeWidth={2} />
                        </div>
                    )}
                </>
            )}
        </div>
    );
}
