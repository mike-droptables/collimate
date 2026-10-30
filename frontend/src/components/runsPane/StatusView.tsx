// The status pane's status view: the cell's runs. One row per live run (its
// state dot, runner and short id, whether it reads its messages, how long it
// has run, and Open for a run with a UI file of its own); the runs that
// ended this session below them, dimmed, until Clear finished. A row opens
// the run's drilldown. The footer opens All and, where a run may start, the
// runners panel. ↑↓ move over the rows, ↵ opens one, Esc closes the pane.
// Mounting it is seeing the cell's last finished run.

import React, { useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { ExternalLink, List } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { listeningOf } from '@/stores/collimate/annotationState';
import { runUiPath, runsOnCell } from '@/lib/runningCell';
import { runElapsed, runShortId } from '@/lib/runLabels';
import { LIGHT_CLASS, runLightState, runStateText } from '@/lib/runLight';
import { isFailureStatus } from '@/lib/runStop';
import { finishedStatusLabel } from '@/lib/annotations';
import { clockOf } from '@/lib/cellLog';
import { revealCellFile } from '@/lib/revealFile';
import { basename } from '@/lib/fileTransfer';
import { NAV_KEYS_OWNER_PROPS } from '@/lib/keyboardOwner';
import { btn, chip, sectionLabel } from '@/components/ui/formStyles';
import { PlayGlyph } from '@/components/ui/PlayGlyph';
import { useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { PaneRail } from './PaneRail';
import { railTitle } from './paneChrome';
import { ListeningGlyph } from './ListeningGlyph';
import { adoptedGrantText, hostCliText, runnerOf } from './paneRun';
import type { FinishedRun } from '@/stores/collimate/types';
import type { RunRecord } from '@/lib/types';

const NO_FINISHED: readonly FinishedRun[] = [];

const runRow = 'flex items-center gap-[0.5625rem] min-h-7 py-0.5 px-2 rounded-md text-[0.8125rem] text-left cursor-pointer outline-none text-ink-2 hover:bg-sheet-2 hover:text-ink focus-visible:shadow-[inset_0_0_0_1px_var(--line-2)] focus:bg-sheet-2 focus:text-ink';
const dot = 'block flex-none w-1.5 h-1.5 rounded-full border shadow-none';
const meta = 'ml-auto pl-2.5 flex-none font-mono text-[0.6875rem] text-ink-3';

export interface StatusViewHandle {
    focus: () => void;
}

export interface StatusViewProps {
    cellId: string;
    /** A run may start here: the footer offers New run. */
    allowRun: boolean;
    /** The row to focus (the run the drilldown came back from). */
    focusRunId: string | null;
    onClose: () => void;
    ref?: React.Ref<StatusViewHandle>;
}

export function StatusView({ cellId, allowRun, focusRunId, onClose, ref }: StatusViewProps) {
    const runs = useCollimateStore(useShallow(s => runsOnCell(s, cellId)));
    const stopping = useCollimateStore(s => s.stoppingRunIds);
    const phases = useCollimateStore(s => s.runPhases);
    const finishedAll = useCollimateStore(s => s.finishedRuns[cellId]) ?? NO_FINISHED;
    const listening = useCollimateStore(useShallow(s => Object.fromEntries(runs.map(r => [r.runId, listeningOf(s, cellId, r.runId)]))));
    const filePaths = useCollimateStore(useShallow(s => (s.locateCell(cellId)?.cell.files ?? []).map(f => f.name)));
    // The run's start, from the every-channel live list (the run record
    // has none).
    const startedAt = useCollimateStore(useShallow(s => {
        const out: Record<string, string> = {};
        for (const entry of s.activeRunsEverywhere) {
            for (const run of entry.runs) if (run.cell_id === cellId && run.started_at) out[run.run_id] = run.started_at;
        }
        return out;
    }));
    // The adopted-grant line of each run whose grant was raised to its
    // harness's minimum, from the same list, and the line of each CLI run
    // whose harness CLI runs on this machine.
    const adopted = useCollimateStore(useShallow(s => {
        const out: Record<string, string> = {};
        for (const entry of s.activeRunsEverywhere) {
            for (const run of entry.runs) {
                const text = run.cell_id === cellId ? adoptedGrantText(run) : null;
                if (text) out[run.run_id] = text;
            }
        }
        return out;
    }));
    const hostCli = useCollimateStore(useShallow(s => {
        const out: Record<string, string> = {};
        for (const entry of s.activeRunsEverywhere) {
            for (const run of entry.runs) {
                const text = run.cell_id === cellId ? hostCliText(run) : null;
                if (text) out[run.run_id] = text;
            }
        }
        return out;
    }));
    const [now, setNow] = useState(() => Date.now());
    const rootRef = useRef<HTMLDivElement>(null);
    const runTitle = useShortcutTooltip(SHORTCUT_IDS.STACK_RUN, 'Run a runner in this cell');

    const liveIds = useMemo(() => new Set(runs.map(r => r.runId)), [runs]);
    const finished = useMemo(() => finishedAll.filter(r => !liveIds.has(r.runId)), [finishedAll, liveIds]);

    useEffect(() => {
        const store = useCollimateStore.getState();
        store.markFailureSeen(cellId);
        void store.loadCellAnnotations(cellId);
    }, [cellId]);

    // The elapsed times move on while the view is open (a clock, not a fetch).
    useEffect(() => {
        const timer = window.setInterval(() => setNow(Date.now()), 15_000);
        return () => window.clearInterval(timer);
    }, []);

    const rows = () => Array.from(rootRef.current?.querySelectorAll<HTMLElement>('[data-testid="status-run-row"]') ?? []);
    useImperativeHandle(ref, () => ({
        focus: () => {
            const root = rootRef.current;
            if (!root) return;
            const all = rows();
            const target = (focusRunId && all.find(r => r.dataset.runId === focusRunId)) || all[0] || root;
            target.focus({ preventScroll: true });
        },
    }));

    const open = (runId: string) => useCollimateStore.getState().setRunsPaneView(cellId, { view: 'run', runId, option: 'default' });

    const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            const all = rows();
            if (all.length === 0) return;
            const k = all.indexOf(document.activeElement as HTMLElement);
            const next = e.key === 'ArrowDown' ? (k + 1) % all.length : (k <= 0 ? all.length - 1 : k - 1);
            all[next].focus({ preventScroll: true });
        } else if (e.key === 'Enter') {
            const row = (document.activeElement as HTMLElement | null)?.closest<HTMLElement>('[data-testid="status-run-row"]');
            if (!row || row !== document.activeElement) return;
            e.preventDefault();
            row.click();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            onClose();
        }
    };

    const liveRow = (run: RunRecord) => {
        const name = runnerOf(run);
        const uiPath = runUiPath(run, filePaths);
        const grant = adopted[run.runId];
        const here = hostCli[run.runId];
        return (
            <div
                key={run.runId}
                data-testid="status-run-row"
                data-run-id={run.runId}
                role="button"
                tabIndex={-1}
                onClick={() => open(run.runId)}
                className={runRow}
            >
                <i aria-hidden className={cn(dot, LIGHT_CLASS[runLightState(run, stopping, phases)].dot)} />
                <span className="min-w-0 truncate" title={`${name} — ${runStateText(run, stopping, phases)}${grant ? ` · ${grant}` : ''}${here ? ` · ${here}` : ''}`}>
                    {name}
                    <span className="ml-1.5 font-mono text-[0.6875rem] text-ink-3">{runShortId(run)}</span>
                </span>
                <ListeningGlyph listening={listening[run.runId] ?? null} />
                {grant && (
                    <span data-testid="status-run-adopted" title={grant} className={cn(chip, 'flex-none h-4 px-1.5 text-[0.625rem]')}>
                        {grant.slice(0, grant.indexOf(' adopted for '))}
                    </span>
                )}
                {here && (
                    <span data-testid="status-run-host-cli" title={here} className={cn(chip, 'flex-none h-4 px-1.5 text-[0.625rem]')}>
                        This machine
                    </span>
                )}
                <span className={meta}>{stopping.has(run.runId) ? 'stopping' : runElapsed(startedAt[run.runId], now)}</span>
                {uiPath && (
                    <button
                        type="button"
                        data-testid="status-run-open-ui"
                        aria-label={`Open ${basename(uiPath)}`}
                        title={`Open ${uiPath}`}
                        onClick={(e) => { e.stopPropagation(); revealCellFile(cellId, uiPath, { newTab: true }); }}
                        className="flex-none inline-flex items-center justify-center w-5 h-5 rounded-xs text-ink-3 hover:bg-sheet-3 hover:text-ink"
                    >
                        <ExternalLink className="w-3 h-3" strokeWidth={1.8} />
                    </button>
                )}
            </div>
        );
    };

    const finishedRow = (run: FinishedRun) => (
        <div
            key={run.runId}
            data-testid="status-run-row"
            data-run-id={run.runId}
            data-ended="true"
            role="button"
            tabIndex={-1}
            onClick={() => open(run.runId)}
            className={cn(runRow, 'opacity-[0.55]')}
        >
            <i aria-hidden className={cn(dot, isFailureStatus(run.status) ? 'border-red bg-transparent' : 'border-ink-3 bg-transparent')} />
            <span className="min-w-0 truncate" title={run.failureReason ? `${runnerOf(run)} — ${run.failureReason}` : runnerOf(run)}>
                {runnerOf(run)}
                <span className="ml-1.5 font-mono text-[0.6875rem] text-ink-3">{runShortId(run)}</span>
            </span>
            <span className={meta}>{finishedStatusLabel(run.status)} · {clockOf(run.endedAt)}</span>
        </div>
    );

    return (
        <div
            ref={rootRef}
            {...NAV_KEYS_OWNER_PROPS}
            data-testid="status-view"
            role="region"
            aria-label="Runs in this cell"
            tabIndex={-1}
            onKeyDown={onKeyDown}
            className="flex-1 min-h-0 min-w-0 flex flex-col bg-sheet outline-none"
        >
            <PaneRail><span className={railTitle}>Status</span></PaneRail>
            <div className="flex-1 min-h-0 overflow-y-auto px-1.5 py-1">
                {runs.length === 0 && (
                    <div className="px-2 pt-1 pb-2 text-[0.78125rem] text-ink-3">Nothing is running in this cell.</div>
                )}
                {runs.map(liveRow)}
                {finished.length > 0 && (
                    <>
                        <div className="flex items-center gap-2 px-2 pt-2.5 pb-1">
                            <span className={sectionLabel}>Finished</span>
                            <button
                                type="button"
                                data-testid="status-clear-finished"
                                onClick={() => useCollimateStore.getState().clearFinishedRuns(cellId)}
                                className="ml-auto text-[0.75rem] text-ink-3 hover:text-ink rounded-xs px-1 hover:bg-sheet-2"
                            >
                                Clear finished
                            </button>
                        </div>
                        {finished.map(finishedRow)}
                    </>
                )}
            </div>
            <div className="flex-none flex flex-wrap gap-1.5 p-2 border-t border-line">
                <button
                    type="button"
                    data-testid="status-all"
                    title="Every run's messages, your notes and the cell's log"
                    onClick={() => useCollimateStore.getState().setRunsPaneView(cellId, { view: 'all', option: 'default' })}
                    className={cn(btn, 'flex-none')}
                >
                    <List className="w-3.5 h-3.5" strokeWidth={1.6} />
                    All
                </button>
                {allowRun && (
                    <button
                        type="button"
                        data-testid="status-new-run"
                        title={runTitle}
                        onClick={() => useCollimateStore.getState().openPickSession(cellId, 'runners')}
                        className={cn(btn, 'flex-none')}
                    >
                        <PlayGlyph className="w-3.5 h-3.5 text-brass" />
                        New run
                    </button>
                )}
            </div>
        </div>
    );
}
