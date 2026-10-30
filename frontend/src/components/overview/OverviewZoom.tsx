import { useEffect, useState } from 'react';
import { useReducedMotion, type MotionValue } from 'motion/react';
import { cn } from '@/lib/utils';
import { paneInsetTransition } from '@/lib/commandPane';
import { OVERVIEW_ZOOM_STEP } from '@/stores/collimate/overviewSlice';
import { useCornerReach } from '@/hooks/useCornerReach';

// The overview's zoom control (the mock's `.ov-zoom`), bottom-right of the
// board: −, the zoom in percent, +, Fit. − and + zoom about the board's
// centre by the step (OVERVIEW_ZOOM_STEP, a plain wheel's notch too); Fit
// fits the whole board in view. It shows only while the pointer is within
// reach of its corner or it holds keyboard focus (useCornerReach, the
// renderer StyleControl's rule); at rest its sheet, border and glyphs are
// transparent, a colour fade (canvas chrome never fades by opacity), and it
// stays in the tab order. It keeps clear of the Commands pane docked on the
// right, sliding in as the pane opens.

const button = (shown: boolean) => cn(
    'inline-flex h-6 min-w-[1.625rem] items-center justify-center rounded-md px-[0.4375rem] text-[0.75rem] transition-colors duration-[120ms] motion-reduce:transition-none',
    shown ? 'text-ink-2 hover:bg-sheet-2 hover:text-ink' : 'text-transparent',
);

/** The group's own colour fade, beside the pane's slide. */
const FADE = ['border-color 120ms ease-out', 'background-color 120ms ease-out'];

export function OverviewZoom({ zoom, onStep, onFit, right, paneRight }: {
    /** The camera's zoom, read live. */
    zoom: MotionValue<number>;
    onStep: (factor: number) => void;
    onFit: () => void;
    /** px from the overview's right edge (the journal pane widens it). */
    right: number;
    /** The Commands pane's width while it is open on the right, else 0. */
    paneRight: number;
}) {
    const reduceMotion = useReducedMotion();
    const [pct, setPct] = useState(() => Math.round(zoom.get() * 100));
    useEffect(() => zoom.on('change', z => setPct(Math.round(z * 100))), [zoom]);
    const { attach, shown, onFocus, onBlur } = useCornerReach('[data-overview-root]');
    return (
        <div
            ref={attach}
            data-overview-zoom
            data-shown={shown || undefined}
            role="group"
            aria-label="Zoom"
            title="Wheel zooms, Shift+wheel pans"
            onFocus={onFocus}
            onBlur={onBlur}
            className={cn(
                'absolute bottom-3.5 z-[2] inline-flex cursor-default items-center gap-0.5 rounded-lg border p-0.5 transition-colors duration-[120ms] motion-reduce:transition-none',
                shown ? 'border-line-2 bg-sheet' : 'border-transparent bg-transparent',
            )}
            style={{ right, marginRight: paneRight, transition: paneInsetTransition(['margin-right'], reduceMotion, FADE) }}
        >
            <button type="button" className={button(shown)} aria-label="Zoom out" title="Zoom out" onClick={() => onStep(1 / OVERVIEW_ZOOM_STEP)}>−</button>
            <span className={cn('min-w-[2.75rem] text-center font-mono text-[0.71875rem] transition-colors duration-[120ms] motion-reduce:transition-none', shown ? 'text-ink-2' : 'text-transparent')} aria-live="polite">{pct}%</span>
            <button type="button" className={button(shown)} aria-label="Zoom in" title="Zoom in" onClick={() => onStep(OVERVIEW_ZOOM_STEP)}>+</button>
            <button type="button" className={button(shown)} title="Fit the board in view" onClick={onFit}>Fit</button>
        </div>
    );
}
