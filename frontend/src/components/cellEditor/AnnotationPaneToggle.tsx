import React, { useCallback } from 'react';
import type { CellLayout } from '@/stores/collimate/types';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { cycleRunsPaneSide, toggleRunsPane } from '@/lib/cellLayout';
import { RunsCaretIcon, runsCaretClass, runsDockHint } from '@/lib/runsDock';
import { useRunsDockDrag } from '@/hooks/useRunsDockDrag';
import { cn } from '@/lib/utils';

export interface AnnotationPaneToggleProps {
    cellId: string;
    /** The button's look: a tool on the tab strip's line. */
    className: string;
}

/**
 * The way to the cell's annotation pane (AnnotationPane) from outside it:
 * the List glyph, turned toward the pane's side and open or closed state
 * (runsCaretClass), in plain ink (the runs' state is the status toggle's).
 * A click toggles the pane, a right-click docks it on the other side, a
 * drag docks it on the nearer edge (the handlers sit on the wrapper span).
 * It lives on the cell's tab strip (TabStrip), directly left of the expand /
 * collapse button, on the canvas and in the full-window view alike; in a
 * split cell only the top-right leaf's strip carries it (TabStrip `tools`),
 * and the cell header carries none. The press bubbles to the cell card, which
 * selects the cell. The pane lives in the cell's layout in the store; a
 * cell the editor has not seeded yet starts from the default layout
 * (updateRunsPane).
 */
export function AnnotationPaneToggle({ cellId, className }: AnnotationPaneToggleProps) {
    const open = useCollimateStore(s => !!s.cellLayouts[cellId]?.runsPane.open);
    const side = useCollimateStore(s => s.cellLayouts[cellId]?.runsPane.side ?? 'right');
    const update = useCallback((fn: (l: CellLayout) => CellLayout) => {
        useCollimateStore.getState().updateRunsPane(cellId, fn);
    }, [cellId]);
    const dockDrag = useRunsDockDrag(cellId, update);
    const hint = `${open ? 'Hide' : 'Show'} the annotation pane`;
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
                data-testid="annotation-pane-toggle"
                aria-expanded={open}
                aria-label={hint}
                title={title}
                onClick={(e) => {
                    e.stopPropagation();
                    update(toggleRunsPane);
                }}
                className={className}
            >
                <RunsCaretIcon className={cn('w-3.5 h-3.5', runsCaretClass(side, open))} strokeWidth={1.6} />
            </button>
        </span>
    );
}
