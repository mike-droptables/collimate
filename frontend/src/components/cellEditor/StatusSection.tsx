// The cell's status section: a band across the top of the cell's panes
// (above the tab strip, the Files pane and the annotation pane, on the card
// and in the full-window view alike), open while
// the store's session-only `statusSection[cellId].open` says so. It lists
// the cell's runs: one row per live run (its state dot, runner and short
// id, whether it reads its messages, its chips, how long it has run, Open
// for a run with a UI file, and the icon that opens the run in the
// annotation pane), then the runs that ended this session, dimmed, until
// Clear finished. A click or ↵ on a row expands it in place: the run's
// state and its grant, CLI and git lines, its Stop and its live options
// (RunOptions) while it runs; how it ended once it has. The footer opens
// the runners panel (Run) and the annotation pane on All at its left, and
// stops every live run (Stop all N) at its right. ↑↓ move over the rows, Esc closes the section and hands the
// keyboard back to the status toggle. A click on a Stop (a row's or Stop
// all) arms the post-stop lock over the whole section: the rows that slide
// under the pointer as a run ends take none of the burst's clicks. Its
// height is its content's, capped at 40% of the cell's pane region,
// scrolling inside.
// Mounting it is seeing the cell's last finished run.

import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { ExternalLink, List, MessageSquare } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { ALL_VIEW, listeningOf } from '@/stores/collimate/annotationState';
import { runUiPath, runsOnCell } from '@/lib/runningCell';
import { runElapsed, runShortId } from '@/lib/runLabels';
import { LIGHT_CLASS, runLightState, runStateText } from '@/lib/runLight';
import { isFailureStatus } from '@/lib/runStop';
import { finishedStatusLabel } from '@/lib/annotations';
import { clockOf } from '@/lib/cellLog';
import { revealCellFile } from '@/lib/revealFile';
import { basename } from '@/lib/fileTransfer';
import { stopRun } from '@/lib/api';
import { NAV_KEYS_OWNER_PROPS } from '@/lib/keyboardOwner';
import { focusStatusToggle } from '@/lib/commandPane';
import { FULL_WINDOW_ATTR, focusPaneTarget } from '@/lib/fullWindowView';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { STOP_LOCK_ARMS_ATTR, usePostStopLock } from '@/hooks/usePostStopLock';
import { btn, chip, sectionLabel } from '@/components/ui/formStyles';
import { PlayGlyph } from '@/components/ui/PlayGlyph';
import { RunStopButton } from '@/components/RunStopButton';
import { useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { ListeningGlyph } from '@/components/runsPane/ListeningGlyph';
import { RunOptions } from '@/components/runsPane/RunOptions';
import { adoptedGrantText, gitChip, hostCliText, runnerOf } from '@/components/runsPane/paneRun';
import type { FinishedRun } from '@/stores/collimate/types';
import type { RunRecord } from '@/lib/types';
import type { ActiveRunInfo } from '@/lib/api';

const NO_FINISHED: readonly FinishedRun[] = [];
const arms = { [STOP_LOCK_ARMS_ATTR]: '' };

/** A focus request this recent still applies when the section mounts with
 *  it (opened from the runs menu on another channel's cell, after the
 *  switch). */
const FRESH_FOCUS_MS = 2000;

const ROW_SELECTOR = '[data-testid="status-run-row"]';
const runRow = 'flex items-center gap-[0.5625rem] min-h-7 py-0.5 px-2 rounded-md text-[0.8125rem] text-left cursor-pointer outline-none text-ink-2 hover:bg-sheet-2 hover:text-ink focus-visible:shadow-[inset_0_0_0_1px_var(--line-2)] focus:bg-sheet-2 focus:text-ink';
const dot = 'block flex-none w-1.5 h-1.5 rounded-full border shadow-none';
const meta = 'ml-auto pl-2.5 flex-none font-mono text-[0.6875rem] text-ink-3';
const rowIcon = 'flex-none inline-flex items-center justify-center w-5 h-5 rounded-xs text-ink-3 hover:bg-sheet-3 hover:text-ink';
/** The expanded body, indented to the row's name. */
const detail = 'flex flex-col gap-1 pl-[1.4375rem] pr-2 pt-0.5 pb-2 text-[0.75rem] text-ink-2';

export interface StatusSectionProps {
    cellId: string;
    /** A run may start here (a writable cell, not in version history): the
     *  footer offers Run. */
    allowRun: boolean;
}

/** The every-channel live list's rows of this cell's runs, by run id. */
function useActiveRows(cellId: string): Record<string, ActiveRunInfo> {
    return useCollimateStore(useShallow(s => {
        const out: Record<string, ActiveRunInfo> = {};
        for (const entry of s.activeRunsEverywhere) {
            for (const run of entry.runs) if (run.cell_id === cellId) out[run.run_id] = run;
        }
        return out;
    }));
}

export function StatusSection({ cellId, allowRun }: StatusSectionProps) {
    const runs = useCollimateStore(useShallow(s => runsOnCell(s, cellId)));
    const stopping = useCollimateStore(s => s.stoppingRunIds);
    const phases = useCollimateStore(s => s.runPhases);
    const finishedAll = useCollimateStore(s => s.finishedRuns[cellId]) ?? NO_FINISHED;
    const expandedRunId = useCollimateStore(s => s.statusSection[cellId]?.expandedRunId ?? null);
    const listening = useCollimateStore(useShallow(s => Object.fromEntries(runs.map(r => [r.runId, listeningOf(s, cellId, r.runId)]))));
    const filePaths = useCollimateStore(useShallow(s => (s.locateCell(cellId)?.cell.files ?? []).map(f => f.name)));
    // The run's start, its adopted grant, its host CLI, its git and its
    // live options, from the every-channel live list (the run record has
    // none of them).
    const rows = useActiveRows(cellId);
    const [now, setNow] = useState(() => Date.now());
    const rootRef = useRef<HTMLDivElement>(null);
    const { invalidate } = useGuestPool();
    const runTitle = useShortcutTooltip(SHORTCUT_IDS.STACK_RUN, 'Run a runner in this cell');
    const { onClickCapture } = usePostStopLock();

    const liveIds = useMemo(() => new Set(runs.map(r => r.runId)), [runs]);
    const finished = useMemo(() => finishedAll.filter(r => !liveIds.has(r.runId)), [finishedAll, liveIds]);
    const stoppable = runs.filter(r => !stopping.has(r.runId));

    useEffect(() => {
        const store = useCollimateStore.getState();
        store.markFailureSeen(cellId);
        void store.loadCellAnnotations(cellId);
    }, [cellId]);

    // The elapsed times move on while the section is open (a clock, not a
    // fetch).
    useEffect(() => {
        const timer = window.setInterval(() => setNow(Date.now()), 15_000);
        return () => window.clearInterval(timer);
    }, []);

    // The section moves the panes below it (and the renderer frames over
    // them) as it opens, closes and changes height: wake the guest pool.
    useLayoutEffect(() => {
        invalidate();
        const root = rootRef.current;
        let observer: ResizeObserver | null = null;
        if (root && typeof ResizeObserver !== 'undefined') {
            observer = new ResizeObserver(() => invalidate());
            observer.observe(root);
        }
        return () => {
            observer?.disconnect();
            invalidate();
        };
    }, [invalidate]);

    const rowEls = () => Array.from(rootRef.current?.querySelectorAll<HTMLElement>(ROW_SELECTOR) ?? []);

    // A focus request (the status toggle, the runs menu, the Commands pane,
    // a run start without a surface tab): the first row, or
    // the section with none, takes the keys once it has rendered. One
    // fresher than FRESH_FOCUS_MS applies when the section mounts with it.
    const focusRequest = useCollimateStore(s => s.statusSectionFocus[cellId]);
    const lastFocusSeq = useRef<number | null>(null);
    useEffect(() => {
        if (!focusRequest) return;
        if (lastFocusSeq.current === null) {
            lastFocusSeq.current = Date.now() - focusRequest.at < FRESH_FOCUS_MS ? focusRequest.seq - 1 : focusRequest.seq;
        }
        if (focusRequest.seq <= lastFocusSeq.current) return;
        lastFocusSeq.current = focusRequest.seq;
        const frame = requestAnimationFrame(() => {
            const target = rowEls()[0] ?? rootRef.current;
            target?.focus({ preventScroll: true });
        });
        return () => cancelAnimationFrame(frame);
    }, [focusRequest]);

    const toggleRow = (runId: string) => {
        useCollimateStore.getState().setStatusExpandedRun(cellId, expandedRunId === runId ? null : runId);
    };
    const annotate = (runId: string) => {
        useCollimateStore.getState().openAnnotationPaneOn(cellId, { view: 'run', runId, option: 'default' }, { focus: 'view' });
    };
    const stopAll = () => {
        const store = useCollimateStore.getState();
        for (const run of stoppable) {
            store.startStopping(run.runId);
            stopRun(run.channel, run.runId).catch((err: unknown) => {
                console.error('stopRun failed:', err);
                useCollimateStore.getState().clearStopping(run.runId);
            });
        }
    };

    // Closing hands the keyboard to the status toggle: the header's, or, in
    // the full-window view, whose region covers the header, its tab strip's.
    const close = () => {
        const fullWindow = rootRef.current?.closest<HTMLElement>(`[${FULL_WINDOW_ATTR}]`) ?? null;
        useCollimateStore.getState().closeStatusSection(cellId);
        if (!fullWindow) {
            focusStatusToggle(cellId);
            return;
        }
        const strip = fullWindow.querySelector<HTMLElement>('[data-testid="cell-status-toggle-strip"]');
        strip?.focus({ preventScroll: true });
        if (!strip || document.activeElement !== strip) focusPaneTarget(cellId);
    };

    const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            close();
            return;
        }
        // ↑↓ ↵ belong to the rows (and the section itself): a select or a
        // switch in an expanded row keeps its own.
        const target = e.target as HTMLElement;
        const onRow = target.matches(ROW_SELECTOR);
        if (!onRow && target !== rootRef.current) return;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            const all = rowEls();
            if (all.length === 0) return;
            const k = all.indexOf(target);
            const next = e.key === 'ArrowDown' ? (k + 1) % all.length : (k <= 0 ? all.length - 1 : k - 1);
            all[next].focus({ preventScroll: true });
        } else if (e.key === 'Enter' && onRow) {
            e.preventDefault();
            target.click();
        }
    };

    const annotateButton = (run: RunRecord | FinishedRun) => (
        <button
            type="button"
            data-testid="status-run-annotate"
            aria-label={`Open ${runnerOf(run)} in the annotation pane`}
            title="Open in the annotation pane"
            onClick={(e) => { e.stopPropagation(); annotate(run.runId); }}
            className={rowIcon}
        >
            <MessageSquare className="w-3 h-3" strokeWidth={1.8} />
        </button>
    );

    const liveRow = (run: RunRecord) => {
        const name = runnerOf(run);
        const row = rows[run.runId];
        const uiPath = runUiPath(run, filePaths);
        const grant = adoptedGrantText(row);
        const here = hostCliText(row);
        const git = gitChip(row);
        const state = runStateText(run, stopping, phases);
        const expanded = expandedRunId === run.runId;
        return (
            <div key={run.runId} data-run-item>
                <div
                    data-testid="status-run-row"
                    data-run-id={run.runId}
                    data-expanded={expanded || undefined}
                    role="button"
                    aria-expanded={expanded}
                    tabIndex={-1}
                    onClick={() => toggleRow(run.runId)}
                    className={runRow}
                >
                    <i aria-hidden className={cn(dot, LIGHT_CLASS[runLightState(run, stopping, phases)].dot)} />
                    <span className="min-w-0 truncate" title={`${name} — ${state}${grant ? ` · ${grant}` : ''}${here ? ` · ${here}` : ''}${git ? ` · ${git.title}` : ''}`}>
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
                    {git && (
                        <span data-testid={git.testId} title={git.title} className={cn(chip, 'flex-none h-4 px-1.5 text-[0.625rem]')}>
                            {git.text}
                        </span>
                    )}
                    <span className={meta}>{stopping.has(run.runId) ? 'stopping' : runElapsed(row?.started_at, now)}</span>
                    {uiPath && (
                        <button
                            type="button"
                            data-testid="status-run-open-ui"
                            aria-label={`Open ${basename(uiPath)}`}
                            title={`Open ${uiPath}`}
                            onClick={(e) => { e.stopPropagation(); revealCellFile(cellId, uiPath, { newTab: true }); }}
                            className={rowIcon}
                        >
                            <ExternalLink className="w-3 h-3" strokeWidth={1.8} />
                        </button>
                    )}
                    {annotateButton(run)}
                </div>
                {expanded && (
                    <div data-testid="status-run-detail" data-run-id={run.runId} className={detail}>
                        <span data-testid="status-run-state" className="first-letter:uppercase">{state}</span>
                        {grant && <span>{grant}</span>}
                        {here && <span>{here}</span>}
                        {git && git.title && <span>{git.title}</span>}
                        <div className="flex pt-0.5">
                            <RunStopButton channel={run.channel} runId={run.runId} runner={name} compact className="flex-none h-[1.375rem] px-2" />
                        </div>
                        {!!row?.options?.length && (
                            <div className="-mx-2 border-t border-line">
                                <RunOptions key={run.runId} channel={run.channel} runId={run.runId} options={row.options} rev={row.options_rev ?? 0} />
                            </div>
                        )}
                    </div>
                )}
            </div>
        );
    };

    const finishedRow = (run: FinishedRun) => {
        const expanded = expandedRunId === run.runId;
        return (
            <div key={run.runId} data-run-item>
                <div
                    data-testid="status-run-row"
                    data-run-id={run.runId}
                    data-ended="true"
                    data-expanded={expanded || undefined}
                    role="button"
                    aria-expanded={expanded}
                    tabIndex={-1}
                    onClick={() => toggleRow(run.runId)}
                    className={cn(runRow, 'opacity-[0.55] focus:opacity-100 hover:opacity-100', expanded && 'opacity-100')}
                >
                    <i aria-hidden className={cn(dot, isFailureStatus(run.status) ? 'border-red bg-transparent' : 'border-ink-3 bg-transparent')} />
                    <span className="min-w-0 truncate" title={run.failureReason ? `${runnerOf(run)} — ${run.failureReason}` : runnerOf(run)}>
                        {runnerOf(run)}
                        <span className="ml-1.5 font-mono text-[0.6875rem] text-ink-3">{runShortId(run)}</span>
                    </span>
                    <span className={meta}>{finishedStatusLabel(run.status)} · {clockOf(run.endedAt)}</span>
                    {annotateButton(run)}
                </div>
                {expanded && (
                    <div data-testid="status-run-detail" data-run-id={run.runId} className={detail}>
                        <span data-testid="status-run-state">{finishedStatusLabel(run.status)}</span>
                        {run.failureReason && <span className="break-words">{run.failureReason}</span>}
                        <span className="text-ink-3">Ended {clockOf(run.endedAt)}</span>
                    </div>
                )}
            </div>
        );
    };

    return (
        <div
            ref={rootRef}
            {...NAV_KEYS_OWNER_PROPS}
            data-testid="status-section"
            role="region"
            aria-label="Runs in this cell"
            tabIndex={-1}
            onKeyDown={onKeyDown}
            onClickCapture={onClickCapture}
            className="flex-none max-h-[40%] min-h-0 flex flex-col border-b border-line bg-sheet outline-none"
        >
            <div className="min-h-0 overflow-y-auto px-1.5 py-1">
                {runs.length === 0 && (
                    <div className="px-2 pt-1 pb-1 text-[0.78125rem] text-ink-3">Nothing is running in this cell.</div>
                )}
                {runs.map(liveRow)}
                {finished.length > 0 && (
                    <>
                        <div className="flex items-center gap-2 px-2 pt-2 pb-1">
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
            <div className="flex-none flex flex-wrap items-center gap-1.5 px-2 py-1.5 border-t border-line">
                {allowRun && (
                    <button
                        type="button"
                        data-testid="status-new-run"
                        title={runTitle}
                        onClick={() => useCollimateStore.getState().openPickSession(cellId, 'runners')}
                        className={cn(btn, 'flex-none')}
                    >
                        <PlayGlyph className="w-3.5 h-3.5 text-brass" />
                        Run
                    </button>
                )}
                <button
                    type="button"
                    data-testid="status-all"
                    title="Every run's messages, your notes and the cell's log, in the annotation pane"
                    onClick={() => useCollimateStore.getState().openAnnotationPaneOn(cellId, ALL_VIEW, { focus: 'view' })}
                    className={cn(btn, 'flex-none')}
                >
                    <List className="w-3.5 h-3.5" strokeWidth={1.6} />
                    All
                </button>
                {stoppable.length > 0 && (
                    <button
                        type="button"
                        data-testid="status-stop-all"
                        {...arms}
                        title={`Stop the ${stoppable.length === 1 ? 'run' : `${stoppable.length} runs`} live in this cell`}
                        onClick={stopAll}
                        className={cn(btn, 'flex-none ml-auto hover:bg-red-soft hover:text-red')}
                    >
                        Stop all {stoppable.length}
                    </button>
                )}
            </div>
        </div>
    );
}
