// The cell's status toggle: the way into its status section (StatusSection)
// from outside it. ONE pill button holding the cell's light (RunLight,
// passive, first), then "▶ Run" where a run may start, "Status" where
// none may (read-only, version history, a built-in card), or "N running"
// while runs are live, then a chevron down while the section is closed, up
// while it is open (the chevron is the only open/closed signal: the pill
// never takes a fill). Idle, it is in plain ink on every cell (ink-2: the
// label is small text on the card, and ink-3 falls short of the 4.5:1 AA
// contrast the accessibility smoke test holds the app's chrome to);
// running, in the light's ink. Its border is the hairline, lifted to the stronger
// line-2 edge while its cell is selected or hovered (as the header's tools
// brighten) and under the pointer; never brass. A click anywhere on it, the
// light included, toggles the section and never starts a run: Alt+R and
// the section's own Run open the runners panel. The pill keeps one width,
// sized to its widest content ("● 99 running ▾") by an invisible sizer, so
// the title (or the tab strip's tabs) never moves between states or cells;
// a back card holds the same frame with only the light in it
// (StatusPillSlot). The cell header shows the 'header' size, the
// full-window view's tab strip the 'strip' size, at its tools' height.

import { ChevronDown, ChevronUp } from 'lucide-react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { dotStatus, runsOnCell } from '@/lib/runningCell';
import { LIGHT_INK } from '@/lib/runLight';
import { cn } from '@/lib/utils';
import { RunLight } from '@/components/RunLight';
import { PlayGlyph } from '@/components/ui/PlayGlyph';

export type StatusPillVariant = 'header' | 'strip';

/** The pill's height per size (the strip's: its tools' 18px line). */
const PILL_HEIGHT: Record<StatusPillVariant, string> = { header: 'h-[1.375rem]', strip: 'h-[1.125rem]' };
/** Its content's padding, gap and text per size: the in-flow sizer and the
 *  overlaid content share it, so they line up exactly. */
const PILL_INNER: Record<StatusPillVariant, string> = {
    header: 'px-2 gap-1.5 text-[0.6875rem]',
    strip: 'px-1.5 gap-1 text-[0.65625rem]',
};
const PILL_BASE = 'relative flex-none inline-flex items-center rounded-full border font-medium leading-none tabular-nums whitespace-nowrap';
const CHEVRON: Record<StatusPillVariant, string> = { header: 'w-3 h-3', strip: 'w-2.5 h-2.5' };
const PLAY: Record<StatusPillVariant, string> = { header: 'w-3 h-3', strip: 'w-2.5 h-2.5' };

/** The widest content the pill ever shows; it sets the pill's width. */
const WIDEST = '99 running';

/** The light's box (RunLight's DOT: 9px; inside the pill it keeps its
 *  margin, so a busy light's halo clears the text), for the sizer. */
const LIGHT_BOX = 'flex-none w-[0.5625rem] h-[0.5625rem]';
/** RunLight's own -mr-0.5 is for the header rows that held it bare. */
const LIGHT_IN_PILL = 'mr-0';

/** The pill's width-setting content: invisible, in flow; the light's box,
 *  the widest text and the chevron. */
function PillSizer({ variant }: { variant: StatusPillVariant }) {
    return (
        <span aria-hidden data-pill-sizer className={cn('invisible inline-flex items-center', PILL_INNER[variant], 'px-0')}>
            <span className={LIGHT_BOX} />
            <span>{WIDEST}</span>
            <ChevronDown className={CHEVRON[variant]} />
        </span>
    );
}

/** What the pill says. */
export type StatusPillState = 'run' | 'status' | 'running';

export interface StatusToggleProps {
    cellId: string;
    /** 'header' on the cell header, 'strip' on the full-window tab strip. */
    variant: StatusPillVariant;
    /** A run may start in this cell here: the idle pill reads Run, else
     *  Status. */
    canRun: boolean;
    /** The selected cell: its pill keeps the stronger border, in every
     *  state (an unselected cell's takes it while the card is hovered). */
    selected: boolean;
    /** Runs before the toggle (the header selects its cell). */
    onBeforeToggle?: () => void;
    /** The pill's place in its row. */
    className?: string;
    'data-testid'?: string;
}

export function StatusToggle({
    cellId, variant, canRun, selected, onBeforeToggle, className, 'data-testid': testId = 'cell-status-toggle',
}: StatusToggleProps) {
    const open = useCollimateStore(s => !!s.statusSection[cellId]?.open);
    const liveStatus = useCollimateStore(s => dotStatus(s, cellId));
    const liveCount = useCollimateStore(s => runsOnCell(s, cellId).length);
    const label = `${open ? 'Hide' : 'Show'} status${liveCount > 0 ? ` · ${liveCount} running` : ''}`;
    const running = !!liveStatus && liveCount > 0;
    const state: StatusPillState = running ? 'running' : canRun ? 'run' : 'status';
    const Chevron = open ? ChevronUp : ChevronDown;

    const ink = running ? LIGHT_INK[liveStatus] : 'text-ink-2 hover:text-ink';
    // The hairline, lifted like the header's tools: the card's hover lifts
    // an unselected cell's, selection holds it, the pill's own hover too.
    const edge = selected ? 'border-line-2' : 'border-line group-hover/card:border-line-2 hover:border-line-2';

    return (
        <button
            type="button"
            data-testid={testId}
            data-pill={state}
            data-selected={selected || undefined}
            data-pill-variant={variant}
            aria-expanded={open}
            aria-label={label}
            title={label}
            // The card selects and drags on pointerdown; the toggle is its
            // own gesture (its click selects the cell itself).
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
                e.stopPropagation();
                onBeforeToggle?.();
                useCollimateStore.getState().toggleStatusSection(cellId);
            }}
            className={cn(
                PILL_BASE, PILL_HEIGHT[variant], PILL_INNER[variant], ink, edge, 'bg-transparent hover:bg-sheet-2',
                'transition-colors outline-none focus-visible:outline-2 focus-visible:outline-teal focus-visible:outline-offset-1',
                className,
            )}
        >
            <PillSizer variant={variant} />
            <span data-pill-content className={cn('absolute inset-0 inline-flex items-center', PILL_INNER[variant])}>
                <RunLight cellId={cellId} className={LIGHT_IN_PILL} />
                {state === 'run' && (
                    <>
                        <PlayGlyph className={cn(PLAY[variant], '-ml-0.5')} />
                        <span>Run</span>
                    </>
                )}
                {state === 'status' && <span>Status</span>}
                {state === 'running' && <span data-pill-count={liveCount}>{liveCount} running</span>}
                <Chevron aria-hidden data-chevron={open ? 'up' : 'down'} className={cn(CHEVRON[variant], 'ml-auto')} />
            </span>
        </button>
    );
}

/** A back card's slot: the pill's frame, borderless, holding only the
 *  passive light where the pill's sits (the same sizer sets the width), so
 *  the lights and the titles of a stack's cards line up with the front's. */
export function StatusPillSlot({ cellId, variant }: { cellId: string; variant: StatusPillVariant }) {
    return (
        <span data-run-slot className={cn(PILL_BASE, PILL_HEIGHT[variant], PILL_INNER[variant], 'border-transparent')}>
            <PillSizer variant={variant} />
            <span className={cn('absolute inset-0 inline-flex items-center', PILL_INNER[variant])}>
                <RunLight cellId={cellId} className={LIGHT_IN_PILL} />
            </span>
        </span>
    );
}
