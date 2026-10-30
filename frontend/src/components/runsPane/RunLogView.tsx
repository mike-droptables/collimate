import React, { useEffect, useLayoutEffect, useMemo, useRef } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { cellLogSource, clockOf, mergeCellLog, type CellLogRun } from '@/lib/cellLog';

const NO_RUNS: readonly CellLogRun[] = [];

export interface RunLogViewProps {
    cellId: string;
    /** These runs' lines only; null for every run the cell saw start. */
    runIds: readonly string[] | null;
    /** Show each line's runner (the merged cell log). */
    showRunner: boolean;
    ref?: React.Ref<HTMLDivElement>;
}

/**
 * A log in the status pane (row C15): every run this client saw start on
 * the cell (All's Log), or one run's lines (its drilldown's Log), live and
 * finished, interleaved by time as `HH:MM  runner  message` and read from
 * the tail. A finished run's saved logs.txt is read once, the first time a
 * log opens after it ended. Esc is the view's (it goes back).
 */
export function RunLogView({ cellId, runIds, showRunner, ref }: RunLogViewProps) {
    const all = useCollimateStore(s => s.cellLogRuns[cellId]) ?? NO_RUNS;
    const runLogs = useCollimateStore(s => s.runLogs);
    const runLogArrivals = useCollimateStore(s => s.runLogArrivals);
    const readSavedCellLogs = useCollimateStore(s => s.readSavedCellLogs);
    const wanted = runIds === null ? null : runIds.join('\0');
    const runs = useMemo(() => {
        if (wanted === null) return all;
        const ids = new Set(wanted.split('\0'));
        return all.filter(r => ids.has(r.runId));
    }, [all, wanted]);

    // Opening a log reads the saved logs of the runs that ended since.
    useEffect(() => { void readSavedCellLogs(cellId); }, [cellId, readSavedCellLogs]);

    const lines = useMemo(
        () => mergeCellLog(runs.map(r => cellLogSource(r, runLogs[r.runId], runLogArrivals[r.runId]))),
        [runs, runLogs, runLogArrivals],
    );

    // The log reads from its tail.
    const preRef = useRef<HTMLPreElement>(null);
    useLayoutEffect(() => {
        const pre = preRef.current;
        if (pre) pre.scrollTop = pre.scrollHeight;
    }, [lines]);

    return (
        <div
            ref={ref}
            data-testid="run-log"
            role="region"
            aria-label="Log"
            tabIndex={-1}
            className="flex-1 min-h-0 min-w-0 flex flex-col bg-sheet outline-none"
        >
            {lines.length > 0 ? (
                <pre
                    ref={preRef}
                    className="flex-1 min-h-0 m-0 px-3.5 py-2 overflow-y-auto font-mono text-[0.6875rem] leading-[1.6] text-ink-2 whitespace-pre-wrap break-words"
                >
                    {lines.map((line, i) => (
                        <React.Fragment key={i}>
                            {i > 0 && '\n'}
                            <span className="text-ink-3">{clockOf(line.at)}</span>
                            {'  '}
                            {showRunner && <><span className="text-ink">{line.runner}</span>{'  '}</>}
                            <span className={line.error ? 'text-red' : undefined}>{line.message}</span>
                        </React.Fragment>
                    ))}
                </pre>
            ) : (
                <div className="px-3.5 pt-2.5 text-[0.78125rem] text-ink-3">No log yet.</div>
            )}
        </div>
    );
}
