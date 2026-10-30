import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useReducedMotion } from 'motion/react';
import { ChevronDown, PanelRight } from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import { useOverviewPersistence } from '@/hooks/useOverviewPersistence';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { JOURNAL_PANE_WIDTH, OVERVIEW_BAR_HEIGHT, connectedOn } from '@/stores/collimate/overviewSlice';
import { cn } from '@/lib/utils';
import { commandPaneInsetOf, paneInsetTransition } from '@/lib/commandPane';
import { SHELF_HOVER_OPEN_MS, useRefusalShake } from '@/hooks/useShelfDrag';
import { ZoneStrip } from './ZoneStrip';

// The Removed bar (the mock's `.rm-bar`): on the board's top edge, below
// the shelf, in the fan bracket's grammar (a lead, "Removed", the count, a
// line that answers the pointer). At its right end: Journal while the
// journal is shut; Only connected, and Clear while that is on, while it is
// open; a chevron that flips. A click opens or shuts the shelf; a drag
// resizes it (and opens it from shut); a drag that ends below 110px shuts
// it. The bar owns the shelf's animated height: it writes it as the CSS
// custom property `--ov-shelf` (px) on the overview root, which the shelf,
// the bar itself and the board read, so a frame of the animation never
// renders React.
//
// A board card dragged onto the bar (useStackDrag) is removed when it is let
// go, with the action of the remove zone under it: while a removable card
// is held the bar shows the zones as labelled segments (ZoneStrip, the same
// split as the shelf's drop area; thin dividers at rest when there are two
// or more), the one under the card lit, and with the shelf shut it opens
// the shelf after the card has hovered SHELF_HOVER_OPEN_MS. A running cell
// is refused: nothing lights, the bar says why in its title and shakes on
// the drop.
//
// The bar runs the overview's full width under an open Commands pane, its
// line going on under the pane, so the pane reads as laid over the page;
// its label and its right-end controls keep clear of the pane, sliding as
// it opens, closes or docks across.

/** The shelf opens and shuts over this long (none with reduced motion). */
const SHELF_ANIMATION_MS = 200;
/** A press that moves this far is a resize, not a click. */
const DRAG_THRESHOLD = 4;
/** A resize leaves at least this much of the overview to the board. */
const BOARD_KEEP = 140;

const easeOutCubic = (k: number) => 1 - (1 - k) ** 3;

export function RemovedBar() {
    useOverviewPersistence();
    const reduceMotion = useReducedMotion();
    const { shelfOpen, shelfHeight, journalOpen, onlyConnected } = useCollimateStore(useShallow(s => ({
        shelfOpen: s.overview.shelfOpen,
        shelfHeight: s.overview.shelfHeight,
        journalOpen: s.overview.journalOpen,
        onlyConnected: s.overview.onlyConnected,
    })));
    const cleared = useCollimateStore(s => s.overview.cleared && connectedOn(s.overview));
    const count = useCollimateStore(s => s.overviewData.removed?.length ?? null);
    const dropHere = useCollimateStore(s => (s.overview.drop?.kind === 'remove' && s.overview.drop.over === 'bar' ? s.overview.drop : null));
    const dropTarget = dropHere ? (dropHere.refusal ? 'refused' : 'on') : null;
    const held = useCollimateStore(s => s.overview.drop?.kind === 'remove');
    const zones = useCollimateStore(s => s.overview.removeZones);
    const paneInset = useCollimateStore(s => commandPaneInsetOf(s.commandPane));
    // An open journal docks against the pane's inner edge (JournalPane):
    // the bar ends at the journal, the pane's width further in.
    const paneRight = journalOpen ? 0 : paneInset.right;

    const barRef = useRef<HTMLDivElement>(null);
    const rootRef = useRef<HTMLElement | null>(null);
    /** The shelf's height this frame (null before the first layout). */
    const current = useRef<number | null>(null);
    const frame = useRef(0);
    const drag = useRef<{ y0: number; h0: number; moved: boolean } | null>(null);
    const swallowClick = useRef(false);
    const [resizing, setResizing] = useState(false);

    const write = (px: number) => {
        current.current = px;
        rootRef.current?.style.setProperty('--ov-shelf', `${px}px`);
    };

    // Follow the store: open to the kept height, shut to 0. The first
    // layout lands at once; a change slides (the mock's animShelf). A
    // resize in flight holds the height itself.
    useLayoutEffect(() => {
        rootRef.current = barRef.current?.closest<HTMLElement>('[data-overview-root]') ?? null;
        const target = shelfOpen ? shelfHeight : 0;
        const from = current.current;
        if (drag.current?.moved) return;
        cancelAnimationFrame(frame.current);
        if (from === null || reduceMotion || from === target) {
            write(target);
            return;
        }
        const t0 = performance.now();
        const step = (t: number) => {
            const k = Math.min(1, Math.max(0, (t - t0) / SHELF_ANIMATION_MS));
            write(from + (target - from) * easeOutCubic(k));
            if (k < 1) frame.current = requestAnimationFrame(step);
        };
        frame.current = requestAnimationFrame(step);
    }, [shelfOpen, shelfHeight, reduceMotion]);

    // A card that can be removed, held over the bar of a shut shelf, opens it.
    useEffect(() => {
        if (dropTarget !== 'on' || shelfOpen) return undefined;
        const timer = setTimeout(() => useCollimateStore.getState().setShelfOpen(true), SHELF_HOVER_OPEN_MS);
        return () => clearTimeout(timer);
    }, [dropTarget, shelfOpen]);
    // Why a held or dropped card cannot go: the bar's title while it holds.
    const refused = useRefusalShake(barRef, 'bar');
    const refusal = dropHere?.refusal ?? refused;

    useEffect(() => () => {
        cancelAnimationFrame(frame.current);
        rootRef.current?.style.removeProperty('--ov-shelf');
    }, []);

    const toggle = () => {
        const store = useCollimateStore.getState();
        store.setShelfOpen(!store.overview.shelfOpen);
    };

    const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
        // The overview pans on a press; a press on the bar is the bar's.
        e.stopPropagation();
        swallowClick.current = false;
        if (e.button !== 0 || (e.target as Element).closest('button')) return;
        e.preventDefault();
        drag.current = { y0: e.clientY, h0: shelfOpen ? current.current ?? 0 : 0, moved: false };
        try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* the pointer is gone */ }
    };

    const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
        const d = drag.current;
        if (!d) return;
        const dy = e.clientY - d.y0;
        if (!d.moved && Math.abs(dy) < DRAG_THRESHOLD) return;
        if (!d.moved) {
            d.moved = true;
            cancelAnimationFrame(frame.current);
            setResizing(true);
        }
        const room = rootRef.current?.clientHeight;
        const max = room ? room - BOARD_KEEP : Infinity;
        // Whole pixels: the height let go is the height the store keeps.
        write(Math.round(Math.max(0, Math.min(max, d.h0 + dy))));
        const store = useCollimateStore.getState();
        if (!store.overview.shelfOpen && (current.current ?? 0) > 0) store.setShelfOpen(true);
    };

    const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
        const d = drag.current;
        if (!d) return;
        drag.current = null;
        try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
        if (!d.moved) return;
        setResizing(false);
        swallowClick.current = true;
        // Below SHELF_MIN_OPEN the store shuts the shelf and the layout
        // effect slides it to 0; else it keeps (and saves) this height.
        useCollimateStore.getState().setShelfHeight(current.current ?? 0);
    };

    const onClick = (e: React.MouseEvent) => {
        if ((e.target as Element).closest('button')) return;
        if (swallowClick.current) {
            swallowClick.current = false;
            return;
        }
        toggle();
    };

    const store = useCollimateStore.getState;
    const lineHover = 'transition-[background-color,width,margin] duration-[120ms] ease-out motion-reduce:transition-none group-hover/rm:bg-ink-2';

    return (
        <div
            ref={barRef}
            data-rm-bar
            data-drop-target={dropTarget ?? undefined}
            title={refusal ?? undefined}
            data-overview-overlay
            className="group/rm absolute left-0 z-[6] flex select-none items-center gap-2 bg-canvas pl-[1.125rem]"
            style={{
                top: 'var(--ov-shelf, 0px)',
                right: journalOpen ? JOURNAL_PANE_WIDTH : 0,
                marginRight: journalOpen ? paneInset.right : 0,
                paddingRight: 10 + paneRight,
                transition: paneInsetTransition(['padding-right', 'margin-right'], reduceMotion),
                height: OVERVIEW_BAR_HEIGHT,
                touchAction: 'none',
                cursor: shelfOpen || resizing ? 'row-resize' : 'pointer',
            }}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onClick={onClick}
        >
            <ZoneStrip
                zones={zones}
                labelled={held}
                lit={dropTarget === 'on' ? dropHere!.zone : null}
                variant="bar"
                // While a card is held its segments cover the bar's own
                // label and controls.
                className={held ? 'z-[1]' : undefined}
                style={{ left: paneInset.left, right: paneRight, transition: paneInsetTransition(['left', 'right'], reduceMotion) }}
            />
            <div
                role="button"
                tabIndex={0}
                aria-expanded={shelfOpen}
                aria-label={count === null ? 'Removed cells' : `Removed cells: ${count}`}
                title={refusal ?? 'Removed cells · click to open or shut, drag to resize'}
                data-rm-track
                className="flex h-[1.125rem] min-w-0 flex-1 items-center rounded-xs outline-none transition-colors duration-[120ms] motion-reduce:transition-none group-hover/rm:bg-sheet-2 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-teal"
                onKeyDown={(e) => {
                    if ((e.key === 'Enter' || e.key === ' ') && e.target === e.currentTarget) {
                        e.preventDefault();
                        toggle();
                    }
                }}
            >
                <span
                    aria-hidden
                    data-rm-under
                    className="h-px flex-none bg-line-2 group-hover/rm:bg-ink-2"
                    style={{ width: paneInset.left, transition: paneInsetTransition(['width'], reduceMotion, ['background-color 120ms ease-out']) }}
                />
                <span className={cn('h-px w-4 flex-none bg-line-2 group-hover/rm:ml-2 group-hover/rm:w-2', lineHover)} />
                <span className="pl-2.5 pr-1.5 text-[0.75rem] font-medium leading-[1.125rem] text-ink-2 group-hover/rm:text-ink">Removed</span>
                <span data-rm-n className="pr-2.5 font-mono text-[0.6875rem] text-ink-3">{count ?? ''}</span>
                <span className={cn('h-px min-w-0 flex-1 bg-line-2 group-hover/rm:mr-2', lineHover)} />
            </div>
            {journalOpen ? (
                <>
                    <button
                        type="button"
                        data-rm-connected
                        aria-pressed={onlyConnected}
                        title="Only the cells with lines to the journal's cell, on the board and in Removed"
                        className={cn(
                            'inline-flex h-6 flex-none items-center gap-[0.4375rem] rounded-sm px-2 text-[0.78125rem] transition-colors',
                            onlyConnected ? 'bg-sheet-2 text-ink' : 'text-ink-2 hover:bg-sheet-2 hover:text-ink',
                        )}
                        onClick={() => store().toggleConnected()}
                    >
                        <i
                            aria-hidden
                            className={cn('relative h-3 w-[1.375rem] flex-none rounded-full transition-colors duration-[120ms] motion-reduce:transition-none', onlyConnected ? 'bg-ink' : 'bg-line-2')}
                        >
                            <span
                                className="absolute top-0.5 h-2 w-2 rounded-full bg-sheet transition-[left] duration-[120ms] motion-reduce:transition-none"
                                style={{ left: onlyConnected ? 12 : 2 }}
                            />
                        </i>
                        Only connected
                    </button>
                    {onlyConnected && (
                        <button
                            type="button"
                            data-rm-clear
                            disabled={cleared}
                            title="Show every cell again. Click a cell, then click it again, to follow its lines"
                            className="-ml-1 inline-flex h-6 flex-none items-center rounded-sm px-2 text-[0.75rem] text-ink-2 transition-colors enabled:hover:bg-sheet-2 enabled:hover:text-ink disabled:cursor-default disabled:opacity-45"
                            onClick={() => store().clearAnchor()}
                        >
                            Clear
                        </button>
                    )}
                </>
            ) : (
                <button
                    type="button"
                    data-rm-journal
                    title="The journal of the selected cell, on the right"
                    className="inline-flex h-[1.375rem] flex-none items-center gap-1.5 rounded-sm px-2 text-[0.75rem] font-medium text-ink-2 transition-colors hover:bg-sheet-2 hover:text-ink"
                    onClick={() => store().setJournalOpen(true)}
                >
                    <PanelRight className="h-3.5 w-3.5" strokeWidth={1.6} />
                    Journal
                </button>
            )}
            <ChevronDown
                aria-hidden
                data-rm-chev
                className={cn(
                    'h-3.5 w-3.5 flex-none text-ink-3 transition-transform duration-[160ms] motion-reduce:transition-none group-hover/rm:text-ink',
                    shelfOpen ? 'rotate-0' : 'rotate-180',
                )}
                strokeWidth={1.6}
            />
        </div>
    );
}
