import React, { useRef, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { Stack } from '@/lib/types';
import { cn } from '@/lib/utils';
import { frontCellOf } from '@/lib/frontSlots';
import { useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { useKnobDrag } from '@/hooks/useKnobDrag';

// The channel scrubber in the top bar (the mock's `.pips` / `.tscrub`): one
// pip per stack on the canvas and, for a fanned stack, one smaller teal pip
// per column; the current column's pip is the 9px brass knob. Pointing at
// the scrubber swells the pips and rings the knob; the knob drags along
// them, snapping to the nearest pip and selecting each one it passes (the
// stack head's knob drag, useKnobDrag). A click on a pip selects it. The
// arrows step the selection one display column, the way Ctrl-←/→ do (fan
// columns included). The pips window themselves to `maxWidth` and the
// wheel steps the selection. The knob is its own layer over the pips: the
// selection moves at once and the knob slides to its new pip in a short
// ease (none under reduced motion).
//
// It stays on the window's server. In the desktop shell the channel menu
// also lists other servers' channels, but opening one recreates the window
// on that server's origin, so crossing servers is only ever an explicit
// pick from that menu, never a scrub or a wheel step.

/** Shared CSS-pixel geometry for drawing, fitting and moving the knob. */
const PIP_SLOT = 17;
const ARROW_SIZE = 24;
const CONTROL_GAP = 11;
const CONTROL_PADDING = 7;
const ARROWS = 2 * (ARROW_SIZE + CONTROL_GAP + CONTROL_PADDING);
/** One wheel notch moves the selection one pip. */
const WHEEL_STEP = 100;

/** A lit look, spelled out for both triggers (Tailwind only sees literal
 *  class names): pointing at the scrubber, and dragging the knob. */
interface Look { hover: string; drag: string }
const ARROW_LIT: Look = { hover: 'group-hover/pips:opacity-100', drag: 'opacity-100' };
const PIP_LIT: Look = {
    hover: 'group-hover/pips:size-1.5 group-hover/pips:bg-ink-2',
    drag: 'size-1.5 bg-ink-2',
};
const FAN_LIT: Look = { hover: 'group-hover/pips:size-[0.3125rem]', drag: 'size-[0.3125rem]' };
/** The knob while the scrubber is pointed at: a brass ring on the canvas. */
const KNOB_RING = 'group-hover/pips:size-[0.9375rem] group-hover/pips:bg-canvas group-hover/pips:shadow-[inset_0_0_0_8px_var(--canvas),0_0_0_1px_var(--brass)]';
/** The knob itself pointed at or dragged: an ink centre inside the ring. */
const KNOB_HOT = 'size-[0.9375rem] bg-ink shadow-[inset_0_0_0_2px_var(--canvas),0_0_0_1px_var(--brass)]';
/** The knob's slide to its new pip. */
const KNOB_SLIDE = 'transition-transform duration-[140ms] ease-[cubic-bezier(.2,.7,.2,1)] motion-reduce:transition-none';
/** A dot, centred in its pip column. */
const DOT = 'absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full transition-[width,height,background-color,box-shadow] duration-[180ms] ease-out motion-reduce:transition-none';

export interface ChannelScrubberProps {
    /** The display columns, left to right: real stacks, with a fanned stack
     *  replaced by its synthetic single-cell columns. */
    columns: Stack[];
    /** The display column that holds the selection. */
    selectedId: string | null;
    /** Each stack's front slot (lib/frontSlots; titles an unnamed stack's pip after its front cell). */
    visibleTopIndices: Record<string, number>;
    /** The fanned stack's name, for its columns' titles. */
    fanName?: string | null;
    /** Select a display column. */
    onSelect: (columnId: string) => void;
    /** px the whole scrubber may take; the pips window to fit. */
    maxWidth?: number;
}

function pipTitle(column: Stack, fanName: string | null | undefined, vti: Record<string, number>): string {
    if (column.synthetic) {
        const cell = column.cells[0];
        return [fanName, cell?.name].filter(Boolean).join(' · ') || `Cell ${(column.fanIndex ?? 0) + 1}`;
    }
    return column.name || frontCellOf(column, vti)?.name || 'Untitled';
}

export function ChannelScrubber({
    columns, selectedId, visibleTopIndices, fanName, onSelect, maxWidth = Infinity,
}: ChannelScrubberProps) {
    const [hoverId, setHoverId] = useState<string | null>(null);
    const wheelAccum = useRef(0);
    const prevTooltip = useShortcutTooltip(SHORTCUT_IDS.NAV_STACK_LEFT, 'Previous cell');
    const nextTooltip = useShortcutTooltip(SHORTCUT_IDS.NAV_STACK_RIGHT, 'Next cell');

    const fit = Math.max(1, Math.floor((maxWidth - ARROWS) / PIP_SLOT));
    const selectedIdx = selectedId === null ? -1 : columns.findIndex(c => c.id === selectedId);

    // Slide the window only at its edges, so the pips hold still while the
    // selection moves inside it. Derived while rendering: the window moves
    // when the selection, the room or the stack count does.
    const [win, setWin] = useState({ offset: 0, idx: -1, fit: 0, len: 0 });
    let offset = win.offset;
    if (win.idx !== selectedIdx || win.fit !== fit || win.len !== columns.length) {
        if (selectedIdx >= 0) {
            offset = Math.min(offset, Math.max(0, columns.length - 1));
            if (selectedIdx < offset) offset = selectedIdx;
            if (selectedIdx >= offset + fit) offset = selectedIdx - fit + 1;
        }
        setWin({ offset, idx: selectedIdx, fit, len: columns.length });
    }

    // Pull the window back so it ends on the last pip when it can.
    const start = Math.max(0, Math.min(offset, columns.length - fit));
    const visible = columns.slice(start, start + fit);

    const step = (dir: -1 | 1) => {
        if (selectedIdx < 0) return;
        const next = columns[selectedIdx + dir];
        if (next) onSelect(next.id);
    };
    const canPrev = selectedIdx > 0;
    const canNext = selectedIdx >= 0 && selectedIdx < columns.length - 1;

    const onWheel = (e: React.WheelEvent) => {
        if (selectedIdx < 0 || columns.length <= 1) return;
        wheelAccum.current += e.deltaY + e.deltaX;
        if (Math.abs(wheelAccum.current) < WHEEL_STEP) return;
        const dir = wheelAccum.current > 0 ? 1 : -1;
        wheelAccum.current = 0;
        step(dir);
    };

    // The knob drag selects each visible column it passes.
    const knobDrag = useKnobDrag(visible.length, k => onSelect(visible[k].id));
    const { trackRef, dragging } = knobDrag;
    const knobAt = visible.findIndex(c => c.id === selectedId);
    const startDrag = (e: React.PointerEvent) => { knobDrag.start(e, knobAt); };
    const knobHot = dragging || (selectedId !== null && hoverId === selectedId);

    // Every lit look applies on hover of the scrubber, or outright while the
    // knob is dragged.
    const lit = (look: Look) => (dragging ? look.drag : look.hover);

    const arrow = (dir: -1 | 1, enabled: boolean) => (
        <button
            type="button"
            onClick={() => step(dir)}
            disabled={!enabled}
            aria-label={dir < 0 ? 'Previous cell' : 'Next cell'}
            title={dir < 0 ? prevTooltip : nextTooltip}
            className={cn(
                'inline-flex flex-none items-center justify-center rounded-xs text-ink-3 transition-opacity duration-[120ms] motion-reduce:transition-none',
                enabled
                    ? cn('opacity-50 hover:bg-sheet-2 hover:text-ink', lit(ARROW_LIT))
                    : 'opacity-25 cursor-default',
            )}
            style={{ width: ARROW_SIZE, height: ARROW_SIZE }}
        >
            {dir < 0 ? <ChevronLeft className="size-3.5" strokeWidth={1.6} /> : <ChevronRight className="size-3.5" strokeWidth={1.6} />}
        </button>
    );

    if (columns.length === 0) {
        return (
            <div className="flex h-9 items-center px-1.5">
                <span className="text-[0.71875rem] text-ink-3 select-none">no stacks open</span>
            </div>
        );
    }

    return (
        <div
            data-dragging={dragging || undefined}
            onWheel={onWheel}
            className="group/pips flex h-9 items-center"
            style={{ gap: CONTROL_GAP, paddingInline: CONTROL_PADDING }}
        >
            {arrow(-1, canPrev)}
            <div
                ref={trackRef}
                role="group"
                aria-label="Stacks in this channel"
                onPointerMove={knobDrag.move}
                onPointerUp={knobDrag.end}
                onPointerCancel={knobDrag.end}
                onLostPointerCapture={knobDrag.end}
                className={cn('relative flex h-9 items-center', dragging && 'touch-none cursor-grabbing')}
            >
                {visible.map((column, k) => {
                    const isCurrent = column.id === selectedId;
                    const isFan = !!column.synthetic;
                    const title = pipTitle(column, fanName, visibleTopIndices);
                    return (
                        <button
                            key={column.id}
                            ref={knobDrag.slotRef(k)}
                            type="button"
                            tabIndex={-1}
                            title={title}
                            aria-label={title}
                            aria-current={isCurrent ? 'true' : undefined}
                            data-pip={isFan ? 'fan' : 'stack'}
                            onClick={isCurrent ? undefined : () => onSelect(column.id)}
                            onPointerDown={isCurrent ? startDrag : undefined}
                            onPointerEnter={() => setHoverId(column.id)}
                            onPointerLeave={() => setHoverId(h => (h === column.id ? null : h))}
                            className={cn('relative block h-9 flex-none', isCurrent ? (dragging ? 'cursor-grabbing' : 'cursor-grab') : 'cursor-pointer')}
                            style={{ width: PIP_SLOT }}
                        >
                            {/* The current pip's dot is the knob, in its own layer. */}
                            {!isCurrent && (
                                <span
                                    aria-hidden
                                    className={cn(DOT, isFan ? cn('size-1 bg-teal', lit(FAN_LIT)) : cn('size-[0.3125rem] bg-ink-3', lit(PIP_LIT)))}
                                />
                            )}
                        </button>
                    );
                })}
                {knobAt >= 0 && (
                    <span
                        aria-hidden
                        data-pip-knob
                        className={cn('pointer-events-none absolute left-0 top-0 h-9', KNOB_SLIDE)}
                        style={{ width: PIP_SLOT, transform: `translateX(${knobAt * PIP_SLOT}px)` }}
                    >
                        <span className={cn(DOT, knobHot ? KNOB_HOT : cn('size-[0.5625rem] bg-brass', KNOB_RING))} />
                    </span>
                )}
            </div>
            {arrow(1, canNext)}
        </div>
    );
}
