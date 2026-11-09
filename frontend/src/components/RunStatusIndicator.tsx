import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Hash } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { Channel, Stack } from '@/lib/types';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { stopAllRuns, stopChannelRuns, type ActiveRunInfo } from '@/lib/api';
import { STOP_LOCK_ARMS_ATTR, usePostStopLock } from '@/hooks/usePostStopLock';
import { Menu } from '@/components/ui/Menu';
import { btn } from '@/components/ui/formStyles';

interface RunStatusIndicatorProps {
    activeChannel: Channel;
    isZoomedOut: boolean;
    /** Leave the zoomed-out overview: a cell row opens its cell. */
    onExitZoom: () => void;
    /** Switch to another channel (a row of another channel's cell). */
    onSelectChannel: (id: string) => void | Promise<void>;
}

/** One running cell: its listed runs and how many of them are not
 *  stopping. */
interface CellGroup {
    cellId: string;
    cellName: string;
    runs: ActiveRunInfo[];
    live: number;
}

/** One channel's running cells, in list order; `live` its runs not
 *  stopping (what its Stop all asks to stop). */
interface ChannelGroup {
    channel: string;
    channelName: string;
    here: boolean;
    cells: CellGroup[];
    live: number;
}

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
const cellRow = 'w-full flex items-center gap-2 h-7 pl-5 pr-2.5 rounded-md text-[0.78125rem] font-medium text-left text-ink hover:bg-sheet-2 disabled:text-ink-2 disabled:hover:bg-transparent [&_svg]:text-ink-3 outline-none focus-visible:shadow-[inset_0_0_0_1px_var(--line-2)]';
const sub = 'ml-auto pl-2.5 text-[0.75rem] font-normal text-ink-3 whitespace-nowrap';
const countChip = 'flex-none min-w-[1.125rem] h-[1.125rem] px-1 inline-flex items-center justify-center rounded-full text-[0.6875rem] font-semibold tabular-nums';
const quietStop = 'min-w-0 max-w-full truncate h-6 px-2 leading-[1.375rem] rounded-sm border border-line-2 text-[0.75rem] text-ink-2 transition-colors hover:bg-red-soft hover:text-red outline-none focus-visible:shadow-[inset_0_0_0_1px_var(--line-2)]';
const arms = { [STOP_LOCK_ARMS_ATTR]: '' };

/**
 * The top bar's running status: "N running (M)", N this channel's live runs
 * and M every other channel's, always shown. It opens the runs menu (a
 * dialog): the running cells grouped by channel (this one first), each with
 * how many runs it holds; no run is named and nothing stops a single run
 * here (the cell's status section does). A cell row goes to that cell and
 * opens its status section (on another channel, after switching to it).
 * Each channel ends with its own Stop all (POST …/runs/stop_all), and the
 * footer stops every run on the server. ↑↓ move over the cell rows and the
 * stops, ↵ is the focused one's click. The runs are the store's
 * every-channel list, which the channel stream keeps fresh
 * (runs_active_changed); nothing here polls.
 */
export const RunStatusIndicator: React.FC<RunStatusIndicatorProps> = ({ activeChannel, isZoomedOut, onExitZoom, onSelectChannel }) => {
    const everywhere = useCollimateStore(s => s.activeRunsEverywhere);
    const stoppingRunIds = useCollimateStore(s => s.stoppingRunIds);
    // The menu's open state is the store's, so the Commands pane's
    // "Running…" opens the same menu as this button.
    const open = useCollimateStore(s => s.runsMenuOpen);
    const setRunsMenuOpen = useCollimateStore(s => s.setRunsMenuOpen);
    // Other channels' runs a stop was sent for, hidden until the list stops
    // naming them live.
    const [sent, setSent] = useState<ReadonlySet<string>>(() => new Set());
    const buttonRef = useRef<HTMLButtonElement>(null);
    const bodyRef = useRef<HTMLDivElement>(null);
    const { onClickCapture } = usePostStopLock();

    const here = activeChannel.id;

    // This channel lists its stopping runs too (their cell stays reachable
    // until the run ends); another channel's leave once their end is under way.
    const { groups, mine, others } = useMemo(() => {
        const groups: ChannelGroup[] = [];
        let mine = 0, others = 0;
        const entries = [...everywhere.filter(e => e.channel === here), ...everywhere.filter(e => e.channel !== here)];
        for (const entry of entries) {
            const isHere = entry.channel === here;
            const runs = entry.runs.filter(run => isHere || (!run.stopping && !sent.has(run.run_id)));
            if (runs.length === 0) continue;
            const cells = new Map<string, CellGroup>();
            let live = 0;
            for (const run of runs) {
                let cell = cells.get(run.cell_id);
                if (!cell) {
                    cell = { cellId: run.cell_id, cellName: run.cell_name, runs: [], live: 0 };
                    cells.set(run.cell_id, cell);
                }
                cell.runs.push(run);
                if (!run.stopping && !(isHere && stoppingRunIds.has(run.run_id))) {
                    cell.live++;
                    live++;
                }
            }
            if (isHere) mine += live;
            else others += live;
            groups.push({ channel: entry.channel, channelName: entry.channel_name, here: isHere, cells: [...cells.values()], live });
        }
        return { groups, mine, others };
    }, [everywhere, stoppingRunIds, sent, here]);

    // Open from the button or the Commands pane: the first cell row takes
    // the keyboard. This effect runs after the menu's own (a child's)
    // first-control focus.
    const focusOnOpen = useRef(open);
    useEffect(() => useCollimateStore.subscribe((state, prev) => {
        if (state.runsMenuOpen && !prev.runsMenuOpen) focusOnOpen.current = true;
    }), []);
    useEffect(() => {
        if (!open) return;
        if (focusOnOpen.current) {
            focusOnOpen.current = false;
            const first = bodyRef.current?.querySelector<HTMLElement>('[data-testid="runs-menu-cell"]:not(:disabled)') ?? bodyRef.current;
            first?.focus({ preventScroll: true });
            return;
        }
        // A Stop whose group left with its runs took the focus with it: the
        // menu body keeps the keyboard (↑↓, Esc).
        if (document.activeElement === document.body) bodyRef.current?.focus({ preventScroll: true });
    });

    // The cell rows and the stops in document order.
    const rows = () => Array.from(bodyRef.current?.querySelectorAll<HTMLElement>(
        '[data-testid="runs-menu-cell"]:not(:disabled), [data-testid="runs-menu-stop-channel"], [data-testid="runs-menu-stop-all"]') ?? []);

    // A sent stop is forgotten once the list no longer names its run live.
    const markSent = (ids: string[]) => setSent(prev => {
        const live = new Set(everywhere.flatMap(e => e.runs.filter(r => !r.stopping).map(r => r.run_id)));
        return new Set([...[...prev].filter(id => live.has(id)), ...ids]);
    });
    const unmarkSent = (ids: string[]) => setSent(prev => new Set([...prev].filter(id => !ids.includes(id))));

    // The runs a stop asks for: this channel's turn stopping in the store
    // (their cells stay listed, their light red), another channel's leave
    // the list. A refused stop puts both back.
    const stopping = (targets: ChannelGroup[]) => {
        const store = useCollimateStore.getState();
        const ids = (g: ChannelGroup) => g.cells.flatMap(c => c.runs
            .filter(r => !r.stopping && !(g.here && stoppingRunIds.has(r.run_id))).map(r => r.run_id));
        const mineIds = targets.filter(g => g.here).flatMap(ids);
        const otherIds = targets.filter(g => !g.here).flatMap(ids);
        for (const id of mineIds) store.startStopping(id);
        markSent(otherIds);
        return (what: string) => (err: unknown) => {
            console.warn(`${what} failed`, err);
            for (const id of mineIds) useCollimateStore.getState().clearStopping(id);
            unmarkSent(otherIds);
        };
    };

    /** One channel's live runs, gracefully (the per-run DELETE's stop). */
    const stopChannel = (group: ChannelGroup) => {
        const undo = stopping([group]);
        stopChannelRuns(group.channel).catch(undo('stop channel'));
    };

    // Every run listed is every live run there is: the server-wide stop.
    const stopEverything = () => {
        const undo = stopping(groups);
        stopAllRuns().catch(undo('stop all'));
    };

    const go = async (channel: string, cellId: string) => {
        setRunsMenuOpen(false);
        if (isZoomedOut) onExitZoom();
        if (channel !== here) await onSelectChannel(channel);
        const store = useCollimateStore.getState();
        store.requestSelectCell(cellId);
        store.openStatusSection(cellId, { channel });
    };

    // ↑↓ move over the cell rows and the stops; ↵ is the row's own click;
    // the menu does Esc and Tab.
    const onBodyKeyDown = (e: React.KeyboardEvent) => {
        if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
        e.preventDefault();
        const all = rows();
        if (all.length === 0) return;
        const k = all.indexOf(document.activeElement as HTMLElement);
        const next = e.key === 'ArrowDown' ? (k + 1) % all.length : (k <= 0 ? all.length - 1 : k - 1);
        all[next].focus({ preventScroll: true });
    };

    const cellButton = (group: ChannelGroup, cell: CellGroup) => {
        const place = group.here ? onCanvas(activeChannel, cell.cellId) : null;
        const name = place?.name || cell.cellName || 'Untitled cell';
        const count = cell.runs.length;
        const ending = count - cell.live;
        return (
            <button
                key={cell.cellId}
                type="button"
                data-testid="runs-menu-cell"
                data-cell-id={cell.cellId}
                title={group.here && !place ? 'This cell is not on the canvas' : 'Go to this cell'}
                disabled={group.here && !place}
                onClick={() => void go(group.channel, cell.cellId)}
                className={cellRow}
            >
                <CellGlyph />
                <span className="min-w-0 truncate">{name}</span>
                {place?.stack.name && <span className={sub}>{place.stack.name}</span>}
                <span
                    data-run-count={count}
                    title={ending > 0 ? `${cell.live} running, ${ending} stopping` : `${count} running`}
                    className={cn(countChip, !place?.stack.name && 'ml-auto',
                        cell.live > 0 ? 'bg-teal-soft text-teal' : 'bg-sheet-2 text-ink-3')}
                >
                    {count}
                </span>
            </button>
        );
    };

    const n = mine;
    const m = others;
    const title = `${n} running in this channel, ${m} in other channels`;
    const total = mine + others;

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
                    {groups.map(group => {
                        const channelName = group.here ? activeChannel.name : group.channelName;
                        return (
                            <div key={group.channel} data-testid="runs-menu-channel" data-channel={group.channel} className="pb-1">
                                <div className={channelHead}>
                                    <Hash className="w-3.5 h-3.5 flex-none" strokeWidth={1.6} />
                                    <span className="min-w-0 truncate">{channelName}</span>
                                    {group.here && <span className={sub}>This channel</span>}
                                </div>
                                {group.cells.map(cell => cellButton(group, cell))}
                                {group.live > 0 && (
                                    <div className="flex justify-end min-w-0 pl-5 pr-1.5 pt-0.5">
                                        <button type="button" data-testid="runs-menu-stop-channel" data-channel={group.channel} {...arms}
                                            title={`Stop every run in ${channelName}`}
                                            onClick={() => stopChannel(group)} className={quietStop}>
                                            Stop all {group.live} in {channelName}
                                        </button>
                                    </div>
                                )}
                            </div>
                        );
                    })}
                    {total > 0 && (
                        <div className="flex gap-1.5 mt-1 -mx-1 -mb-1 p-2 border-t border-line">
                            <button type="button" data-testid="runs-menu-stop-all" {...arms} onClick={stopEverything}
                                className={cn(btn, 'ml-auto flex-none hover:bg-red-soft hover:text-red')}>
                                Stop all {total} on this server
                            </button>
                        </div>
                    )}
                </div>
            </Menu>
        </>
    );
};
