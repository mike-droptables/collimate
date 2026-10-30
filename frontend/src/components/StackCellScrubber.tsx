// The stack head's pips (the mock's `.scrub`): one per front slot
// (lib/frontSlots: a cell, or the Capabilities stack's manage card ahead of
// its cells), the front slot's the knob. At rest each pip is a quiet 4px
// dot on a row under the stack's name, the front one 6px in the knob colour
// (teal; brass while the stack holds the selection). With the pointer on
// the head the knob rises to the arrows' line as a 15px ring on a hairline
// rail and the other dots step aside; with the pointer on the track a
// sheet-2 field comes up behind the rail; with the pointer on the knob (or
// while it is dragged) every pip rises onto the line and the knob takes its
// centre dot. Fixed 15px slots spread across the track, so the pips never
// shift sideways.
//
// Only the knob moves, by dragging (useKnobDrag): it snaps to whichever pip
// is nearest the pointer and the stack shows that slot as the knob passes
// it. The knob is its own column over the slots, so a new front slot
// (Alt-↑/↓, the Commands pane's step, a drop) slides it to its pip with a
// magnet's snap: a gentle start, then a hard pull into place (KNOB_SLIDE).
// Under a drag it follows the pointer quickly instead, and under reduced
// motion it jumps. The front slot itself changes at once. The pips are not
// buttons; each is titled `<when> · <cell>`, the manage card's 'Manage'.
// The track is pointer-transparent at rest, so the head's own gestures
// (click to recentre, drag to pan, double-click for full width) work
// everywhere else on it.
//
// At rest the row ends with the stack's slot count, right of the last pip
// (mono, quiet); it fades out as the pips rise. The track ends a digit
// further in per extra digit of the count, so it never reaches the right
// arrow.
//
// A single slot has nothing to drag and nothing to count: no pip, no knob
// and no count. Its head keeps the hairline rail all the same
// (StackSingleRail), so every head's line answers the pointer alike.

import { useEffect, useRef, useState } from 'react';
import { useReducedMotion } from 'motion/react';
import { useKnobDrag } from '@/hooks/useKnobDrag';

/** The track's insets inside the head: clear of the left arrow, and of the
 *  right arrow and the … button. */
const SCRUB_INSET_LEFT = 36;
const SCRUB_INSET_RIGHT = 58;
/** One pip's slot, and the knob's ring. */
const SLOT = 15;
/** The at-rest row's centre, under the name's 18px line. */
const REST_TOP = 23;
/** The track field's height (the arrows' height) and its overhang past the
 *  end pips, so the knob fits at either end. */
const FIELD_HEIGHT = 22;
const FIELD_OVERHANG = 6;
/** The knob's magnet: a gentle first stretch, then a hard pull that seats it
 *  on its pip with no overshoot (both control points' y stay inside 0..1).
 *  A fifth of the way at a third of the time, half way at 42%, 84% by 58%,
 *  then home: as if the pip drew the knob in once it came close. */
const KNOB_SNAP_EASE = 'cubic-bezier(.65,0,.15,1)';
/** The knob's slide to a new front cell (Alt-↑/↓, a step, a drop): 220ms,
 *  about 1.5x the drag's follow, so the magnet's pull has time to read. */
const KNOB_SLIDE = `left 220ms ${KNOB_SNAP_EASE}`;
/** While the knob is dragged the pointer leads and the knob follows quickly:
 *  a slow-starting magnet would restart at every pip and trail a fast drag. */
const KNOB_DRAG_SLIDE = 'left 140ms cubic-bezier(.2,.7,.2,1)';
/** The at-rest pip, and the gap between the last one and the count. */
const REST_PIP = 4;
const COUNT_GAP = 6;
/** One digit of the count (mono 11px is 6.6px wide): the base right inset
 *  fits a one-digit count left of the right arrow; each further digit moves
 *  the track's right end in by one, so a count of 10 or more never runs
 *  under the arrow. */
const COUNT_DIGIT = 7;
const trackInsetRight = (slotCount: number) => SCRUB_INSET_RIGHT + (String(slotCount).length - 1) * COUNT_DIGIT;
/** The pips' rise and swell. Showing and hiding are colours (a pip's
 *  background to transparent), never opacity: Chromium runs an opacity
 *  transition on the compositor and squashes what paints after it (the
 *  name, the knob) into an Overlap layer, whose text is grayscale while it
 *  runs. */
const PIP_MOTION = 'top 180ms cubic-bezier(.2,.7,.2,1), width 180ms ease, height 180ms ease, background-color 160ms ease, box-shadow 160ms ease';

/** The track's box inside the head, shared by the scrubber and the single rail. */
const TRACK_BOX: React.CSSProperties = {
    position: 'absolute', left: SCRUB_INSET_LEFT, right: SCRUB_INSET_RIGHT, top: 0, bottom: 0,
    display: 'flex', alignItems: 'stretch', justifyContent: 'space-between', zIndex: 1,
};

type Ease = (css: string) => string;

/** The sheet-2 field behind the rail while the pointer is on the track. */
function TrackField({ on, ease }: { on: boolean; ease: Ease }) {
    return (
        <div
            data-scrub-field
            style={{
                position: 'absolute', zIndex: -1, left: -FIELD_OVERHANG, right: -FIELD_OVERHANG,
                top: '50%', height: FIELD_HEIGHT, marginTop: -FIELD_HEIGHT / 2, borderRadius: 4,
                background: on ? 'var(--sheet-2)' : 'transparent', pointerEvents: 'none',
                transition: ease('background-color 120ms ease'),
            }}
        />
    );
}

/** The rail's looks: hidden, quiet (ink-3 at 55%) and lit (ink-2, the
 *  pointer on the track). */
type RailTone = 'hidden' | 'quiet' | 'lit';
const RAIL_TONE: Record<RailTone, string> = { hidden: 'bg-transparent', quiet: 'bg-ink-3/55', lit: 'bg-ink-2' };

/** The hairline from the first slot's centre to the last's. */
function TrackRail({ tone, ease }: { tone: RailTone; ease: Ease }) {
    return (
        <div
            data-scrub-rail
            data-tone={tone}
            className={RAIL_TONE[tone]}
            style={{
                position: 'absolute', zIndex: 0, left: SLOT / 2, right: SLOT / 2,
                top: '50%', height: 1, marginTop: -0.5, pointerEvents: 'none',
                transition: ease('background-color 160ms ease'),
            }}
        />
    );
}

/** A pip's dot, centred in its column. */
const PIP_BASE: React.CSSProperties = {
    position: 'absolute', left: '50%', transform: 'translate(-50%, -50%)',
    borderRadius: '50%', pointerEvents: 'none',
};

interface KnobLook {
    /** Risen onto the rail (the pointer on the head, a drag, a reveal). */
    up: boolean;
    /** The centre dot: the pointer on the knob, or a drag. */
    dot: boolean;
    /** The knob colour: brass on the stack holding the selection, else teal. */
    knob: string;
    /** Whatever lies under the knob: the field while it shows, else the canvas. */
    under: string;
}

/** The knob's dot: 6px in the knob colour at rest; risen, a ring on the
 *  rail, hollow in the colour under it, with a 1px collar in the knob
 *  colour, and its centre dot while the pointer is on it or it is dragged. */
function knobDotStyle({ up, dot, knob, under }: KnobLook, ease: Ease): React.CSSProperties {
    const base = { ...PIP_BASE, transition: ease(PIP_MOTION) };
    if (!up) return { ...base, top: REST_TOP, width: 6, height: 6, background: knob };
    return {
        ...base, top: '50%', width: SLOT, height: SLOT,
        background: dot ? 'var(--ink)' : under,
        boxShadow: `inset 0 0 0 ${dot ? 2 : 8}px ${under}, 0 0 0 1px ${knob}, 0 0 0 3px ${under}`,
    };
}

/** The knob's column over the slots: a layer from the first slot's centre to
 *  the last's, the column at `at` (0..1) along it. Its `left` eases to a new
 *  pip (`slide`); the selection itself has already moved. */
function KnobColumn({ at, slide, children }: { at: number; slide: string; children: React.ReactNode }) {
    return (
        <div style={{ position: 'absolute', left: SLOT / 2, right: SLOT / 2, top: 0, bottom: 0, zIndex: 2, pointerEvents: 'none' }}>
            <div
                data-scrub-knob-column
                style={{
                    position: 'absolute', top: 0, bottom: 0, width: SLOT, marginLeft: -SLOT / 2,
                    left: `${at * 100}%`, transition: slide,
                }}
            >
                {children}
            </div>
        </div>
    );
}

/** A single cell's head: nothing to drag and nothing to count, so no pip
 *  and no knob. Over the head the hairline rail comes up, the field with
 *  the pointer on the track. Presses and clicks (the double-click too) go
 *  through to the head. */
export function StackSingleRail({ raised }: { raised: boolean }) {
    const reduceMotion = useReducedMotion();
    const [onTrack, setOnTrack] = useState(false);
    const ease: Ease = (css) => (reduceMotion ? 'none' : css);
    return (
        <div
            data-stack-single-rail
            aria-hidden="true"
            onPointerEnter={() => setOnTrack(true)}
            onPointerLeave={() => setOnTrack(false)}
            style={{ ...TRACK_BOX, pointerEvents: raised ? 'auto' : 'none' }}
        >
            <TrackField on={raised && onTrack} ease={ease} />
            <TrackRail tone={!raised ? 'hidden' : onTrack ? 'lit' : 'quiet'} ease={ease} />
        </div>
    );
}

interface StackCellScrubberProps {
    /** The stack's front slots (lib/frontSlots slotCount). */
    slotCount: number;
    /** Each pip's title, by slot (lib/stackPips stackPipTitles). */
    titles: readonly string[];
    /** The front slot (already normalized into [0, slotCount)). */
    topSlot: number;
    /** The stack holds the selection: the knob is brass, else teal. */
    selected: boolean;
    /** The pointer is on the head: the knob rises onto the rail and the
     *  track takes the pointer. */
    raised: boolean;
    /** A front change just happened (useStackScrubberReveal): every pip
     *  stands on the rail for a moment so the move reads. */
    revealed?: boolean;
    /** Set the stack's front slot. */
    onSetTopSlot: (slot: number) => void;
    /** Grab and release of the knob: the head keeps its raised look for the
     *  whole drag (pointer capture can carry it off the head). */
    onDraggingChange?: (dragging: boolean) => void;
}

export function StackCellScrubber({
    slotCount, titles, topSlot, selected, raised, revealed = false, onSetTopSlot, onDraggingChange,
}: StackCellScrubberProps) {
    const reduceMotion = useReducedMotion();
    const knobDrag = useKnobDrag(slotCount, onSetTopSlot);
    const { trackRef, dragging } = knobDrag;
    // A press on the knob ended: the click that follows is the knob's and
    // never recentres the stack. Cleared by the next press on the track.
    const swallowClickRef = useRef(false);
    const [onTrack, setOnTrack] = useState(false);
    // The pip the knob stood on when the pointer came onto it: once the knob
    // moves to another pip it has slid out from under the pointer, whatever
    // its own enter/leave last said.
    const [knobAt, setKnobAt] = useState<number | null>(null);
    const onKnob = knobAt === topSlot;
    useEffect(() => { onDraggingChange?.(dragging); }, [dragging, onDraggingChange]);
    // Unmounting mid-drag (the stack dropped to one cell) must not leave the
    // head thinking the knob is still held.
    useEffect(() => () => { onDraggingChange?.(false); }, [onDraggingChange]);

    const up = raised || dragging || revealed;
    const fieldOn = onTrack || dragging;
    const pipsUp = onKnob || dragging || revealed;
    const ease: Ease = (css) => (reduceMotion ? 'none' : css);
    const look: KnobLook = {
        up, dot: onKnob || dragging,
        knob: selected ? 'var(--brass)' : 'var(--teal)',
        under: fieldOn ? 'var(--sheet-2)' : 'var(--canvas)',
    };

    const onKnobPointerDown = (e: React.PointerEvent) => {
        if (knobDrag.start(e, topSlot)) swallowClickRef.current = true;
    };
    // The drag's own moves and release stay off the head (its pan).
    const onPointerMove = (e: React.PointerEvent) => { if (knobDrag.move(e)) e.stopPropagation(); };
    const onPointerUp = (e: React.PointerEvent) => { if (knobDrag.end(e)) e.stopPropagation(); };

    // Every cell's pip, the front one included: the knob, opaque and in its
    // own column above them, covers the pip it stands on, so a pip it slides
    // toward stays until the knob passes over it rather than vanishing as
    // the front cell changes. Over the head only the knob rises and the
    // others step aside; with the pointer on the knob, a drag or a reveal
    // they all rise onto the line.
    // Its colour is a class: risen ink-2, stepped aside transparent, at rest
    // ink-3 at 60%.
    const pip = (): React.CSSProperties => {
        const base = { ...PIP_BASE, transition: ease(PIP_MOTION) };
        if (pipsUp) return { ...base, top: '50%', width: 5, height: 5 };
        return { ...base, top: REST_TOP, width: 4, height: 4 };
    };
    const pipTone = pipsUp ? 'bg-ink-2' : up ? 'bg-transparent' : 'bg-ink-3/60';

    // A hairline from the first pip to the last; it goes a step lighter on
    // the track and disappears with the pointer on the knob.
    const railTone: RailTone = onKnob || dragging ? 'hidden' : onTrack ? 'lit' : up ? 'quiet' : 'hidden';

    return (
        <div
            ref={trackRef}
            data-stack-scrubber
            aria-hidden="true"
            onPointerEnter={() => setOnTrack(true)}
            onPointerLeave={() => setOnTrack(false)}
            onPointerDownCapture={() => { swallowClickRef.current = false; }}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={knobDrag.end}
            onLostPointerCapture={knobDrag.end}
            onClick={(e) => {
                if (!swallowClickRef.current) return;
                swallowClickRef.current = false;
                e.stopPropagation();
            }}
            style={{ ...TRACK_BOX, right: trackInsetRight(slotCount), pointerEvents: up ? 'auto' : 'none', touchAction: 'none' }}
        >
            <TrackField on={fieldOn} ease={ease} />
            <TrackRail tone={railTone} ease={ease} />
            {Array.from({ length: slotCount }, (_, i) => (
                <div
                    key={i}
                    ref={knobDrag.slotRef(i)}
                    data-scrub-slot={i}
                    data-current={i === topSlot || undefined}
                    title={titles[i]}
                    style={{ position: 'relative', zIndex: 1, width: SLOT, flex: 'none' }}
                >
                    <div data-scrub-pip className={pipTone} style={pip()} />
                </div>
            ))}
            {/* The count, right of the last pip at rest, centred on the
                dots; it fades as they rise (under reduced motion it simply
                goes), a colour fade to transparent. */}
            <span
                data-stack-count
                style={{
                    position: 'absolute', top: REST_TOP, transform: 'translateY(-50%)',
                    left: `calc(100% - ${(SLOT - REST_PIP) / 2}px + ${COUNT_GAP}px)`,
                    fontFamily: 'var(--font-mono)', fontSize: '0.6875rem', lineHeight: 1,
                    color: up ? 'transparent' : 'var(--ink-3)', whiteSpace: 'nowrap', pointerEvents: 'none',
                    transition: ease('color 160ms ease'),
                }}
            >
                {slotCount}
            </span>
            <KnobColumn
                at={slotCount > 1 ? topSlot / (slotCount - 1) : 0.5}
                slide={ease(dragging ? KNOB_DRAG_SLIDE : KNOB_SLIDE)}
            >
                <div data-scrub-pip data-current style={knobDotStyle(look, ease)} />
                {/* The knob's grab target: its column across the field's
                    full height, so the pointer finds it above or below the
                    ring too. */}
                <div
                    data-scrub-knob
                    title={titles[topSlot]}
                    onPointerEnter={() => setKnobAt(topSlot)}
                    onPointerLeave={() => setKnobAt(null)}
                    onPointerDown={onKnobPointerDown}
                    style={{
                        position: 'absolute', left: -1, right: -1, top: '50%',
                        height: FIELD_HEIGHT, marginTop: -FIELD_HEIGHT / 2,
                        pointerEvents: up ? 'auto' : 'none',
                        cursor: dragging ? 'grabbing' : 'grab',
                    }}
                />
            </KnobColumn>
        </div>
    );
}
