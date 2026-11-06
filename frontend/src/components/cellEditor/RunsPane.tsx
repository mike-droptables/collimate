import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CellLayout, RunsPaneSide } from '@/stores/collimate/types';
import {
    RUNS_PANE_MIN_HEIGHT, RUNS_PANE_MIN_WIDTH,
    closeRunsPane, cycleRunsPaneSide, paneReserve, paneSizeStyle, runsPaneAxis, runsPaneSize, setRunsPane,
} from '@/lib/cellLayout';
import { RUNS_PANE_ATTR, runsDockHint } from '@/lib/runsDock';
import { useRunsDockDrag } from '@/hooks/useRunsDockDrag';
import { focusRunsPaneToggle } from '@/lib/commandPane';
import { FULL_WINDOW_ATTR, focusPaneTarget } from '@/lib/fullWindowView';
import { useCellSelected } from '@/contexts/CellSelectionContext';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { STATUS_VIEW } from '@/stores/collimate/annotationState';
import { cn } from '@/lib/utils';
import { RunsPaneChromeContext, type RunsPaneChrome } from '@/components/runsPane/paneChrome';
import { StatusView, type StatusViewHandle } from '@/components/runsPane/StatusView';
import { RunDrilldown, type RunViewHandle } from '@/components/runsPane/RunDrilldown';
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

export interface RunsPaneProps {
    cellId: string;
    layout: CellLayout;
    /** Wraps updateCellLayout(cellId, fn): every write goes through a pure op. */
    onLayoutChange: (fn: (l: CellLayout) => CellLayout) => void;
    /** A run may start here (a writable cell, not in version history): the
     *  status view offers New run. */
    allowRun: boolean;
    /** The cell takes messages (not read-only): the composers show. */
    allowAnnotate: boolean;
    /** The cell is in the full-window view (unpooled editor frames). */
    maximized: boolean;
}

/**
 * The cell's status pane (the runs pane, `layout.runsPane`): docked inside
 * the cell on the right (a 280px column) or across the bottom (a 200px
 * band), resizable by its content-facing edge between its minimum and two
 * thirds of the cell, its width and height persisted apart. It renders only
 * while open, one of three views (the store's session-only
 * `runsPaneView`): the cell's runs (StatusView), one run's drilldown
 * (RunDrilldown) or All (AllView). Each view draws its own rail on the tab
 * strip's icon line, with the Hide toggle (an X: a right-click docks on
 * the other side, a drag on the nearer edge) at its far end. As the pane
 * opens, and as its view changes, the view takes the keyboard; Esc steps
 * back (a drilldown or All to the status view) and, on the status view,
 * closes the pane and hands the keyboard to the header's caret (the
 * full-window view's region while the cell is maximised).
 */
export function RunsPane({ cellId, layout, onLayoutChange, allowRun, allowAnnotate, maximized }: RunsPaneProps) {
    const pane = layout.runsPane;
    const { open, side } = pane;
    const selected = useCellSelected();
    const asideRef = useRef<HTMLElement>(null);
    const viewRef = useRef<StatusViewHandle & RunViewHandle>(null);
    const dockDrag = useRunsDockDrag(cellId, onLayoutChange);
    const { invalidate } = useGuestPool();
    const axis = runsPaneAxis(side);
    // The size mid-drag; null at rest, when the layout's size shows.
    const [dragSize, setDragSize] = useState<number | null>(null);
    const size = dragSize ?? runsPaneSize(layout);
    const minSize = axis === 'y' ? RUNS_PANE_MIN_HEIGHT : RUNS_PANE_MIN_WIDTH;
    const reserve = paneReserve(layout, 'runs', axis);

    const stored = useCollimateStore(s => s.runsPaneView[cellId]) ?? STATUS_VIEW;
    const drilled = usePaneRun(cellId, stored.view === 'run' ? stored.runId : '');
    // A drilldown of a run the pane knows no more shows the status view.
    const view = stored.view === 'run' && !drilled ? STATUS_VIEW : stored;
    useEffect(() => {
        if (stored.view === 'run' && !drilled) useCollimateStore.getState().setRunsPaneView(cellId, STATUS_VIEW);
    }, [stored, drilled, cellId]);
    const viewKey = view.view + (view.view === 'run' ? `:${view.runId}` : '') + ('option' in view ? `:${view.option}` : '');
    const kindKey = view.view + (view.view === 'run' ? `:${view.runId}` : '');

    // The run the drilldown came back from: its status row takes the keys.
    const [returnRunId, setReturnRunId] = useState<string | null>(null);

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

    // A focus request (the Commands pane, the runs menu): the view or its
    // composer takes the keys once it has rendered.
    const focusRequest = useCollimateStore(s => s.runsPaneFocus[cellId]);
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

    // Closing hands the keyboard to the header's caret; in the full-window
    // view, whose region covers the header, to the view.
    const close = () => {
        const inFullWindow = !!asideRef.current?.closest(`[${FULL_WINDOW_ATTR}]`);
        onLayoutChange(closeRunsPane);
        if (inFullWindow) focusPaneTarget(cellId);
        else focusRunsPaneToggle(cellId);
    };
    const back = () => {
        setReturnRunId(view.view === 'run' ? view.runId : null);
        useCollimateStore.getState().setRunsPaneView(cellId, STATUS_VIEW);
    };
    const hideTitle = `Hide status · ${runsDockHint(side)}`;
    const closeRef = useRef(close);
    useEffect(() => { closeRef.current = close; });
    const chrome: RunsPaneChrome = useMemo(() => ({
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
            aria-label="Status"
            {...{ [RUNS_PANE_ATTR]: '' }}
            data-runs-side={side}
            data-runs-view={view.view}
            className={cn(
                'relative flex-none flex flex-col min-w-0 min-h-0 border-line bg-sheet text-[0.78125rem] text-ink-2',
                ASIDE_CLASS[side],
            )}
            // The stored size, held between the minimum and two thirds of the
            // cell as the cell itself narrows or widens, and short of the
            // content's minimum and the Files pane's share beside it.
            style={paneSizeStyle(axis, size, minSize, reserve.css)}
            // Esc on the rail or the resize handle: back to the status
            // view, or, there, close the pane (each view's own region
            // handles its own Esc).
            onKeyDown={(e) => {
                if (e.key !== 'Escape') return;
                e.preventDefault();
                e.stopPropagation();
                if (view.view === 'status') close();
                else back();
            }}
        >
            {/* The content-facing edge's resize strip, out of the flow. */}
            <PaneResizeHandle
                paneRef={asideRef}
                edge={CONTENT_EDGE[side]}
                size={size}
                min={minSize}
                reserve={reserve.px}
                label="status"
                onPreview={setDragSize}
                onCommit={(v) => onLayoutChange(l => setRunsPane(l, axis === 'y' ? { height: v } : { width: v }))}
                onCollapse={close}
            />
            <RunsPaneChromeContext.Provider value={chrome}>
                {view.view === 'status' && (
                    <StatusView ref={viewRef} cellId={cellId} allowRun={allowRun} focusRunId={returnRunId} onClose={close} />
                )}
                {view.view === 'run' && (
                    <RunDrilldown
                        ref={viewRef}
                        key={view.runId}
                        cellId={cellId}
                        runId={view.runId}
                        option={view.option}
                        allowAnnotate={allowAnnotate}
                        maximized={maximized}
                        onBack={back}
                    />
                )}
                {view.view === 'all' && (
                    <AllView ref={viewRef} cellId={cellId} option={view.option} allowAnnotate={allowAnnotate} maximized={maximized} onBack={back} />
                )}
            </RunsPaneChromeContext.Provider>
        </aside>
    );
}
