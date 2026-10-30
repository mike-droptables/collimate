import React, { useCallback } from 'react';
import type { CellLayout } from '@/stores/collimate/types';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { cycleRunsPaneSide, toggleRunsPane } from '@/lib/cellLayout';
import { RunsCaretIcon, runsCaretClass, runsDockHint } from '@/lib/runsDock';
import { useRunsDockDrag } from '@/hooks/useRunsDockDrag';
import { dotStatus, runsOnCell } from '@/lib/runningCell';
import { LIGHT_INK } from '@/lib/runLight';
import { cn } from '@/lib/utils';

export interface RunsPaneToggleProps {
    cellId: string;
    /** Runs before the toggle (the header selects its cell). */
    onBeforeToggle?: () => void;
    /** The button's look: a head tool in the cell header, a strip tool in
     *  the full-window view's tab strip. */
    className: string;
    'data-testid'?: string;
}

/**
 * The status caret: the way to the cell's status pane (RunsPane) from
 * outside it. Its List glyph is turned toward the pane's side and open or
 * closed state (runsCaretClass); while runs are live it takes their state's
 * ink, with their count beside it, and reads plain ink-3 otherwise. A click
 * toggles the pane, a right-click docks it on the other side, a drag docks
 * it on the nearer edge (the handlers sit on the wrapper span). The cell
 * header shows it left of Remove; the full-window view, whose header is
 * out of sight, shows it on the tab strip left of the collapse button. The
 * pane lives in the cell's layout in the store, so either reaches it; a
 * cell the editor has not seeded yet starts from the default layout
 * (updateRunsPane).
 */
export function RunsPaneToggle({ cellId, onBeforeToggle, className, 'data-testid': testId = 'runs-pane-toggle' }: RunsPaneToggleProps) {
    const open = useCollimateStore(s => !!s.cellLayouts[cellId]?.runsPane.open);
    const side = useCollimateStore(s => s.cellLayouts[cellId]?.runsPane.side ?? 'right');
    const update = useCallback((fn: (l: CellLayout) => CellLayout) => {
        useCollimateStore.getState().updateRunsPane(cellId, fn);
    }, [cellId]);
    const dockDrag = useRunsDockDrag(cellId, update);
    // The ink and count: the live runs' state, as the light's.
    const liveStatus = useCollimateStore(s => dotStatus(s, cellId));
    const liveCount = useCollimateStore(s => runsOnCell(s, cellId).length);
    const ink = liveStatus && liveCount > 0 ? LIGHT_INK[liveStatus] : undefined;
    const hint = `${open ? 'Hide' : 'Show'} the status pane`;
    const title = `${hint} · ${runsDockHint(side)}`;

    return (
        <span
            data-runs-dock-handle
            className="inline-flex"
            title={title}
            onContextMenu={(e) => { e.preventDefault(); update(cycleRunsPaneSide); }}
            {...dockDrag}
        >
            <button
                type="button"
                data-testid={testId}
                aria-expanded={open}
                aria-label={liveCount > 0 ? `${hint} · ${liveCount} running in this cell` : hint}
                title={title}
                onClick={(e) => {
                    e.stopPropagation();
                    onBeforeToggle?.();
                    update(toggleRunsPane);
                }}
                className={cn(className, ink && 'gap-px px-1')}
            >
                <RunsCaretIcon className={cn('w-3.5 h-3.5', runsCaretClass(side, open), ink)} strokeWidth={1.6} />
                {ink && (
                    <span data-run-count className={cn('text-[0.6875rem] font-medium leading-none tabular-nums', ink)}>
                        {liveCount}
                    </span>
                )}
            </button>
        </span>
    );
}
