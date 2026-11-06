import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Hash } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { Channel, Stack } from '@/lib/types';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { STATUS_VIEW } from '@/stores/collimate/annotationState';
import type { RunsPaneView } from '@/stores/collimate/types';
import { stopAllRuns, stopRun, type ActiveRunInfo } from '@/lib/api';
import { LIGHT_CLASS, lightStateOfPhase } from '@/lib/runLight';
import { runElapsed, runShortId } from '@/lib/runLabels';
import { usePostStopLock } from '@/hooks/usePostStopLock';
import { Menu } from '@/components/ui/Menu';
import { btn } from '@/components/ui/formStyles';
import { RunStopButton } from './RunStopButton';
import { ListeningGlyph } from './runsPane/ListeningGlyph';

interface RunStatusIndicatorProps {
    activeChannel: Channel;
    isZoomedOut: boolean;
    /** Leave the zoomed-out overview: a row opens its cell. */
    onExitZoom: () => void;
    /** Switch to another channel (a row of another channel's run). */
    onSelectChannel: (id: string) => void | Promise<void>;
}

/** The runs of one cell, under its head. */
interface CellGroup {
    cellId: string;
    cellName: string;
    runs: ActiveRunInfo[];
}

/** One channel's cells, in list order. */
interface ChannelGroup {
    channel: string;
    channelName: string;
    here: boolean;
    cells: CellGroup[];
}

const runnerOf = (run: ActiveRunInfo) => run.runner_name || run.label;

/** The canvas stack holding a cell of the active channel, with that cell. */
function onCanvas(channel: Channel, cellId: string): { stack: Stack; name: string } | null {
    for (const stack of channel.main_stacks) {
        const cell = stack.cells.find(c => String(c.id) === cellId);
        if (cell) return { stack, name: cell.name };
    }
    return null;
}

// The mock's `i-square` glyph.
const CellGlyph = () => (
    <svg viewBox="0 0 16 16" className="w-3.5 h-3.5 flex-none" fill="none" stroke="currentColor" strokeWidth={1.6} aria-hidden>
        <rect x="3" y="3" width="10" height="10" rx="1.5" />
    </svg>
);

// The status keeps one width while its counts change, up to two digits
// each: the runs menu lines up with its right edge and closes once its anchor
// moves, and the top bar's right column is end-aligned, so a label that grew
// or shrank (after Stop all) would shift the button's left edge under an open
// menu. An invisible ::after in the label's grid cell reserves the widest
// label, the dot and its gap included; its alt text is empty, so the button's
// name stays the live label.
const STATUS_SIZER = "after:[grid-area:1/1] after:invisible after:pl-4 after:content-['00_running_(00)'_/_'']";

const channelHead = 'flex items-center gap-2 h-7 px-2.5 text-[0.78125rem] font-medium text-ink-2 [&_svg]:text-ink-3';
const cellHead = 'w-full flex items-center gap-2 h-7 pl-5 pr-2.5 rounded-md text-[0.78125rem] font-medium text-left text-ink hover:bg-sheet-2 disabled:text-ink-2 disabled:hover:bg-transparent [&_svg]:text-ink-3 outline-none focus-visible:shadow-[inset_0_0_0_1px_var(--line-2)]';
const sub = 'ml-auto pl-2.5 text-[0.75rem] font-normal text-ink-3 whitespace-nowrap';
const runButton = 'flex-1 min-w-0 flex items-center gap-[0.5625rem] h-7 pl-8 pr-1 rounded-md text-[0.8125rem] text-left text-ink-2 hover:bg-sheet-2 hover:text-ink outline-none focus-visible:shadow-[inset_0_0_0_1px_var(--line-2)]';
const quietStop = 'flex-none inline-flex items-center h-6 px-2 rounded-sm border border-line-2 text-[0.75rem] text-ink-2 transition-colors hover:bg-red-soft hover:text-red';

/**
 * The top bar's running status: "N running (M)", N this channel's live runs
 * and M every other channel's, always shown. It opens the runs menu (a
 * dialog): every live run grouped by channel (this one first), then cell,
 * each with its state, whether it reads its messages, how long it has run
 * and its own Stop (this channel's runs keep theirs while they stop, so
 * Force stop stays in reach). A cell row goes to that cell and opens its
 * status pane; a run row opens the run's drilldown there (on another
 * channel, after switching to it). ↑↓ move over the rows, ↵ opens one; the
 * footer stops every run on the server. The runs are the store's
 * every-channel list, which the channel stream keeps fresh
 * (runs_active_changed); nothing here polls.
 */
export const RunStatusIndicator: React.FC<RunStatusIndicatorProps> = ({ activeChannel, isZoomedOut, onExitZoom, onSelectChannel }) => {
    const everywhere = useCollimateStore(s => s.activeRunsEverywhere);
    const stoppingRunIds = useCollimateStore(s => s.stoppingRunIds);
    const runPhases = useCollimateStore(s => s.runPhases);
    const contexts = useCollimateStore(s => s.annotationContexts);
    // The menu's open state is the store's, so the Commands pane's
    // "Running…" opens the same menu as this button.
    const open = useCollimateStore(s => s.runsMenuOpen);
    const setRunsMenuOpen = useCollimateStore(s => s.setRunsMenuOpen);
    // Other channels' runs a stop was sent for, hidden until the list stops
    // naming them live.
    const [sent, setSent] = useState<ReadonlySet<string>>(() => new Set());
    const [now, setNow] = useState(() => Date.now());
    const buttonRef = useRef<HTMLButtonElement>(null);
    const bodyRef = useRef<HTMLDivElement>(null);
    const { onClickCapture } = usePostStopLock();

    const here = activeChannel.id;

    const { groups, mine, others, listed } = useMemo(() => {
        const groups: ChannelGroup[] = [];
        let mine = 0, others = 0, listed = 0;
        const entries = [...everywhere.filter(e => e.channel === here), ...everywhere.filter(e => e.channel !== here)];
        for (const entry of entries) {
            const isHere = entry.channel === here;
            const runs = entry.runs.filter(run => isHere || (!run.stopping && !sent.has(run.run_id)));
            if (runs.length === 0) continue;
            const cells = new Map<string, CellGroup>();
            for (const run of runs) {
                let cell = cells.get(run.cell_id);
                if (!cell) {
                    cell = { cellId: run.cell_id, cellName: run.cell_name, runs: [] };
                    cells.set(run.cell_id, cell);
                }
                cell.runs.push(run);
                listed++;
                if (isHere ? !run.stopping && !stoppingRunIds.has(run.run_id) : true) {
                    if (isHere) mine++;
                    else others++;
                }
            }
            groups.push({ channel: entry.channel, channelName: entry.channel_name, here: isHere, cells: [...cells.values()] });
        }
        return { groups, mine, others, listed };
    }, [everywhere, stoppingRunIds, sent, here]);

    // The elapsed times are fresh on open, from the button or the Commands
    // pane, and move on while the menu is open (a clock, not a fetch).
    const focusOnOpen = useRef(open);
    useEffect(() => useCollimateStore.subscribe((state, prev) => {
        if (state.runsMenuOpen && !prev.runsMenuOpen) {
            focusOnOpen.current = true;
            setNow(Date.now());
        }
    }), []);
    useEffect(() => {
        if (!open) return;
        const timer = window.setInterval(() => setNow(Date.now()), 15_000);
        return () => window.clearInterval(timer);
    }, [open]);

    // The cell heads and run rows in document order.
    const rows = () => Array.from(bodyRef.current?.querySelectorAll<HTMLElement>(
        '[data-testid="runs-menu-cell"] > button:not(:disabled), [data-testid="runs-menu-run"]') ?? [])
        .sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));

    // On open the first run row takes focus. This effect runs after the
    // menu's own (a child's) first-control focus.
    useEffect(() => {
        if (!focusOnOpen.current || !open) return;
        focusOnOpen.current = false;
        const first = bodyRef.current?.querySelector<HTMLElement>('[data-testid="runs-menu-run"]') ?? bodyRef.current;
        first?.focus({ preventScroll: true });
    });

    // A sent stop is forgotten once the list no longer names its run live.
    const markSent = (ids: string[]) => setSent(prev => {
        const live = new Set(everywhere.flatMap(e => e.runs.filter(r => !r.stopping).map(r => r.run_id)));
        return new Set([...[...prev].filter(id => live.has(id)), ...ids]);
    });
    const unmarkSent = (ids: string[]) => setSent(prev => new Set([...prev].filter(id => !ids.includes(id))));

    /** Another channel's run: a graceful stop, its row hidden meanwhile. */
    const stopElsewhere = (channel: string, runId: string) => {
        markSent([runId]);
        stopRun(channel, runId).catch(err => {
            console.warn('stop failed', err);
            unmarkSent([runId]);
        });
    };

    // Every run listed is every live run there is: the server-wide stop.
    const stopEverything = () => {
        const store = useCollimateStore.getState();
        const mineIds = groups.filter(g => g.here).flatMap(g => g.cells.flatMap(c => c.runs.map(r => r.run_id)));
        const otherIds = groups.filter(g => !g.here).flatMap(g => g.cells.flatMap(c => c.runs.map(r => r.run_id)));
        for (const id of mineIds) store.startStopping(id);
        markSent(otherIds);
        stopAllRuns().catch(err => {
            console.warn('stop all failed', err);
            for (const id of mineIds) useCollimateStore.getState().clearStopping(id);
            unmarkSent(otherIds);
        });
    };

    const go = async (channel: string, cellId: string, view: RunsPaneView) => {
        setRunsMenuOpen(false);
        if (isZoomedOut) onExitZoom();
        if (channel !== here) await onSelectChannel(channel);
        const store = useCollimateStore.getState();
        store.requestSelectCell(cellId);
        store.openRunsPaneOn(cellId, view, { focus: 'view', channel });
    };

    // ↑↓ move over the cell and run rows; ↵ is the row's own click; the
    // menu does Esc and Tab.
    const onBodyKeyDown = (e: React.KeyboardEvent) => {
        if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
        e.preventDefault();
        const all = rows();
        if (all.length === 0) return;
        const k = all.indexOf(document.activeElement as HTMLElement);
        const next = e.key === 'ArrowDown' ? (k + 1) % all.length : (k <= 0 ? all.length - 1 : k - 1);
        all[next].focus({ preventScroll: true });
    };

    const listeningFor = (cellId: string, run: ActiveRunInfo): boolean | null =>
        contexts[cellId]?.[run.run_id]?.listening ?? run.listening ?? null;

    const runRow = (group: ChannelGroup, cell: CellGroup, run: ActiveRunInfo) => {
        const phase = group.here ? runPhases[run.run_id]?.phase ?? run.phase : run.phase;
        const isStopping = group.here ? stoppingRunIds.has(run.run_id) || !!run.stopping : !!run.stopping;
        const state = lightStateOfPhase(phase || undefined, isStopping);
        return (
            <div key={run.run_id} data-run={run.run_id} className="flex items-center gap-1 pr-1.5">
                <button
                    type="button"
                    data-testid="runs-menu-run"
                    title={`Open ${runnerOf(run)} in its cell`}
                    onClick={() => void go(group.channel, cell.cellId, { view: 'run', runId: run.run_id, option: 'default' })}
                    className={runButton}
                >
                    <i aria-hidden className={cn('block w-1.5 h-1.5 flex-none rounded-full border shadow-none', LIGHT_CLASS[state].dot)} />
                    <span className="min-w-0 truncate">{runnerOf(run)}</span>
                    <span className="flex-none font-mono text-[0.6875rem] text-ink-3">{runShortId(run)}</span>
                    <ListeningGlyph listening={listeningFor(cell.cellId, run)} />
                    <span className="ml-auto pl-2 flex-none font-mono text-[0.6875rem] text-ink-3">
                        {isStopping ? 'stopping' : runElapsed(run.started_at, now)}
                    </span>
                </button>
                {group.here ? (
                    <RunStopButton channel={group.channel} runId={run.run_id} runner={runnerOf(run)} className="flex-none h-6 px-2" />
                ) : (
                    <button type="button" data-testid="runs-menu-stop" aria-label={`Stop ‘${runnerOf(run)}’`}
                        onClick={() => stopElsewhere(group.channel, run.run_id)} className={quietStop}>
                        Stop
                    </button>
                )}
            </div>
        );
    };

    const n = mine;
    const m = others;
    const title = `${n} running in this channel, ${m} in other channels`;

    return (
        <>
            <button
                ref={buttonRef}
                type="button"
                onClick={() => setRunsMenuOpen(!open)}
                title={title}
                aria-haspopup="dialog"
                aria-expanded={open}
                className={cn(
                    'flex-none inline-grid items-center justify-items-end h-[1.625rem] px-2 rounded-md text-[0.78125rem] font-medium tabular-nums whitespace-nowrap transition-colors hover:bg-sheet-2 aria-expanded:bg-sheet-2',
                    STATUS_SIZER,
                    n > 0 ? 'text-teal' : 'text-ink-2',
                )}
            >
                <span data-testid="run-status-label" className="[grid-area:1/1] inline-flex items-center gap-[0.4375rem]">
                    <i
                        data-testid="run-status-dot"
                        className={cn('block w-[0.4375rem] h-[0.4375rem] rounded-full',
                            n > 0 ? 'bg-teal' : 'bg-transparent shadow-[inset_0_0_0_1.5px_var(--ink-3)]')}
                    />
                    <span>{n} running</span>
                    <span className="-ml-[0.1875rem] font-normal text-ink-2">({m})</span>
                </span>
            </button>
            <Menu
                anchor={buttonRef}
                open={open}
                onClose={() => setRunsMenuOpen(false)}
                role="dialog"
                label="Running"
                minWidth={300}
                maxWidth={360}
                gap={6}
            >
                <div ref={bodyRef} tabIndex={-1} onKeyDown={onBodyKeyDown} onClickCapture={onClickCapture} className="outline-none">
                    {groups.length === 0 && (
                        <div className="px-2.5 pt-2 pb-2 text-[0.78125rem] text-ink-3">Nothing is running.</div>
                    )}
                    {groups.map(group => (
                        <div key={group.channel} data-testid="runs-menu-channel" data-channel={group.channel} className="pb-1">
                            <div className={channelHead}>
                                <Hash className="w-3.5 h-3.5 flex-none" strokeWidth={1.6} />
                                <span className="min-w-0 truncate">{group.here ? activeChannel.name : group.channelName}</span>
                                {group.here && <span className={sub}>This channel</span>}
                            </div>
                            {group.cells.map(cell => {
                                const place = group.here ? onCanvas(activeChannel, cell.cellId) : null;
                                const name = place?.name || cell.cellName || 'Untitled cell';
                                return (
                                    <div key={cell.cellId} data-testid="runs-menu-cell" data-cell-id={cell.cellId} className="pt-px pb-[0.1875rem]">
                                        <button
                                            type="button"
                                            title={group.here && !place ? 'This cell is not on the canvas' : 'Go to this cell'}
                                            disabled={group.here && !place}
                                            onClick={() => void go(group.channel, cell.cellId, STATUS_VIEW)}
                                            className={cellHead}
                                        >
                                            <CellGlyph />
                                            <span className="min-w-0 truncate">{name}</span>
                                            {place?.stack.name && <span className={sub}>{place.stack.name}</span>}
                                        </button>
                                        {cell.runs.map(run => runRow(group, cell, run))}
                                    </div>
                                );
                            })}
                        </div>
                    ))}
                    {listed > 0 && (
                        <div className="flex gap-1.5 mt-1 -mx-1 -mb-1 p-2 border-t border-line">
                            <button type="button" data-testid="runs-menu-stop-all" onClick={stopEverything}
                                className={cn(btn, 'ml-auto flex-none hover:bg-red-soft hover:text-red')}>
                                Stop all {listed}
                            </button>
                        </div>
                    )}
                </div>
            </Menu>
        </>
    );
};
