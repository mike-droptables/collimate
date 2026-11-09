import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CellLayout, RunsPaneSide } from '@/stores/collimate/types';
import {
    RUNS_PANE_MIN_HEIGHT, RUNS_PANE_MIN_WIDTH,
    closeRunsPane, cycleRunsPaneSide, paneReserve, paneSizeStyle, runsPaneAxis, runsPaneSize, setRunsPane,
} from '@/lib/cellLayout';
import { ANNOTATION_PANE_ATTR, runsDockHint } from '@/lib/runsDock';
import { useRunsDockDrag } from '@/hooks/useRunsDockDrag';
import { focusAnnotationPaneToggle } from '@/lib/commandPane';
import { FULL_WINDOW_ATTR, focusPaneTarget } from '@/lib/fullWindowView';
import { useCellSelected } from '@/contexts/CellSelectionContext';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { ALL_VIEW } from '@/stores/collimate/annotationState';
import { cn } from '@/lib/utils';
import { AnnotationPaneChromeContext, type AnnotationPaneChrome, type PaneViewHandle } from '@/components/runsPane/paneChrome';
import { RunDrilldown } from '@/components/runsPane/RunDrilldown';
import { AllView } from '@/components/runsPane/AllView';
import { usePaneRun } from '@/components/runsPane/paneRun';
import { PaneResizeHandle, type PaneEdge } from './PaneResizeHandle';

/** Where the pane sits per side: the right column runs the cell's height,
 *  the bottom band its width. */
const ASIDE_CLASS: Record<RunsPaneSide, string> = {
    right: 'h-full border-l',
    bottom: 'w-full border-t',
};

/** The pane edge facing the content, where its resize handle sits. */
const CONTENT_EDGE: Record<RunsPaneSide, PaneEdge> = { right: 'left', bottom: 'top' };

/** A focus request this recent still applies when the pane mounts with it
 *  (the pane opened on another channel's cell after the switch). */
const FRESH_FOCUS_MS = 2000;

export interface AnnotationPaneProps {
    cellId: string;
    layout: CellLayout;
    /** Wraps updateCellLayout(cellId, fn): every write goes through a pure op. */
    onLayoutChange: (fn: (l: CellLayout) => CellLayout) => void;
    /** The cell takes messages (not read-only): the composers show. */
    allowAnnotate: boolean;
    /** The cell is in the full-window view (unpooled editor frames). */
    maximized: boolean;
}

/**
 * The cell's annotation pane (`layout.runsPane`): docked inside the cell on
 * the right (a 280px column) or across the bottom (a 200px band), resizable
 * by its content-facing edge between its minimum and two thirds of the
 * cell, its width and height persisted apart. It renders only while open,
 * one of two views (the store's session-only `annotationPaneView`): All
 * (AllView, the default, also for a run the pane knows no more) or one
 * run's drilldown (RunDrilldown). Each view draws its own rail on the tab
 * strip's icon line: the run picker first, the Hide toggle (an X: a
 * right-click docks on the other side, a drag on the nearer edge) at its
 * far end. As the pane opens, and as its view changes, the view takes the
 * keyboard; Esc in a view (once its composer has closed) closes the pane
 * and hands the keyboard to its toggle on the tab strip (the full-window
 * view's region while the cell is maximised).
 */
export function AnnotationPane({ cellId, layout, onLayoutChange, allowAnnotate, maximized }: AnnotationPaneProps) {
    const pane = layout.runsPane;
    const { open, side } = pane;
    const selected = useCellSelected();
    const asideRef = useRef<HTMLElement>(null);
    const viewRef = useRef<PaneViewHandle>(null);
    const dockDrag = useRunsDockDrag(cellId, onLayoutChange);
    const { invalidate } = useGuestPool();
    const axis = runsPaneAxis(side);
    // The size mid-drag; null at rest, when the layout's size shows.
    const [dragSize, setDragSize] = useState<number | null>(null);
    const size = dragSize ?? runsPaneSize(layout);
    const minSize = axis === 'y' ? RUNS_PANE_MIN_HEIGHT : RUNS_PANE_MIN_WIDTH;
    const reserve = paneReserve(layout, 'runs', axis);

    const stored = useCollimateStore(s => s.annotationPaneView[cellId]) ?? ALL_VIEW;
    const drilled = usePaneRun(cellId, stored.view === 'run' ? stored.runId : '');
    // A drilldown of a run the pane knows no more shows All.
    const view = stored.view === 'run' && !drilled ? ALL_VIEW : stored;
    useEffect(() => {
        if (stored.view === 'run' && !drilled) useCollimateStore.getState().setAnnotationPaneView(cellId, ALL_VIEW);
    }, [stored, drilled, cellId]);
    const kindKey = view.view === 'run' ? `run:${view.runId}` : 'all';
    const viewKey = `${kindKey}:${view.option}`;

    // Opening the pane, or changing its view, puts the keyboard in the
    // view, so ↑↓ ↵ Esc work at once. A pane already open when the cell's
    // editor mounts (a reload, a zoom back in) takes nothing.
    const seen = useRef<{ open: boolean; kind: string } | null>(null);
    useLayoutEffect(() => {
        const was = seen.current;
        seen.current = { open, kind: kindKey };
        if (!was || !open || (was.open && was.kind === kindKey)) return;
        viewRef.current?.focus('view');
    }, [open, kindKey]);

    // A focus request (the status section, the Commands pane, the runs
    // menu): the view or its composer takes the keys once it has rendered.
    const focusRequest = useCollimateStore(s => s.annotationPaneFocus[cellId]);
    const lastFocusSeq = useRef<number | null>(null);
    useEffect(() => {
        if (!focusRequest) return;
        if (lastFocusSeq.current === null) {
            lastFocusSeq.current = Date.now() - focusRequest.at < FRESH_FOCUS_MS ? focusRequest.seq - 1 : focusRequest.seq;
        }
        if (focusRequest.seq <= lastFocusSeq.current) return;
        lastFocusSeq.current = focusRequest.seq;
        const frame = requestAnimationFrame(() => viewRef.current?.focus(focusRequest.target));
        return () => cancelAnimationFrame(frame);
    }, [focusRequest]);

    // A view swap moves pooled frames (the composer's editor): wake the pool.
    useEffect(() => { invalidate(); }, [viewKey, invalidate]);

    // Closing hands the keyboard to the annotation toggle on the cell's tab
    // strip; in the full-window view (portaled out of the card), to the
    // view.
    const close = () => {
        const inFullWindow = !!asideRef.current?.closest(`[${FULL_WINDOW_ATTR}]`);
        onLayoutChange(closeRunsPane);
        if (inFullWindow) focusPaneTarget(cellId);
        else focusAnnotationPaneToggle(cellId);
    };
    const hideTitle = `Hide annotation · ${runsDockHint(side)}`;
    const closeRef = useRef(close);
    useEffect(() => { closeRef.current = close; });
    const chrome: AnnotationPaneChrome = useMemo(() => ({
        close: () => closeRef.current(),
        hideTitle,
        dockDrag,
        cycleSide: () => onLayoutChange(cycleRunsPaneSide),
        selected,
    }), [hideTitle, dockDrag, onLayoutChange, selected]);

    if (!open) return null;

    return (
        <aside
            ref={asideRef}
            aria-label="Annotation"
            {...{ [ANNOTATION_PANE_ATTR]: '' }}
            data-annotation-side={side}
            data-annotation-view={view.view}
            className={cn(
                'relative flex-none flex flex-col min-w-0 min-h-0 border-line bg-sheet text-[0.78125rem] text-ink-2',
                ASIDE_CLASS[side],
            )}
            // The stored size, held between the minimum and two thirds of the
            // cell as the cell itself narrows or widens, and short of the
            // content's minimum and the Files pane's share beside it.
            style={paneSizeStyle(axis, size, minSize, reserve.css)}
            // Esc on the rail or the resize handle closes the pane (each
            // view's own region handles its own Esc).
            onKeyDown={(e) => {
                if (e.key !== 'Escape') return;
                e.preventDefault();
                e.stopPropagation();
                close();
            }}
        >
            {/* The content-facing edge's resize strip, out of the flow. */}
            <PaneResizeHandle
                paneRef={asideRef}
                edge={CONTENT_EDGE[side]}
                size={size}
                min={minSize}
                reserve={reserve.px}
                label="annotation"
                onPreview={setDragSize}
                onCommit={(v) => onLayoutChange(l => setRunsPane(l, axis === 'y' ? { height: v } : { width: v }))}
                onCollapse={close}
            />
            <AnnotationPaneChromeContext.Provider value={chrome}>
                {view.view === 'run' ? (
                    <RunDrilldown
                        ref={viewRef}
                        key={view.runId}
                        cellId={cellId}
                        runId={view.runId}
                        option={view.option}
                        allowAnnotate={allowAnnotate}
                        maximized={maximized}
                        onClose={close}
                    />
                ) : (
                    <AllView ref={viewRef} cellId={cellId} option={view.option} allowAnnotate={allowAnnotate} maximized={maximized} onClose={close} />
                )}
            </AnnotationPaneChromeContext.Provider>
        </aside>
    );
}
