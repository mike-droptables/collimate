// The journal (O06): the subject cell's events, newest first, in a 374px pane
// docked on the overview's right. A sticky head (the cell, its stack, a
// "removed" tag on a shelf cell, the event count, Hide) and sticky day heads
// in local time; one row per event: its time, a glyph, the sentence. Every
// cell name walks to that cell. After Clear (Only connected on) the pane is
// the All cells journal. The pane scrolls on its own; the keyboard stays the
// scene's. With the Commands pane docked on the right it docks against the
// pane's inner edge, sliding as the pane opens and closes.

import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useReducedMotion } from 'motion/react';
import { useShallow } from 'zustand/react/shallow';
import type { CellActor, JournalCellRef, JournalEvent } from '@/lib/api/cells';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { cn } from '@/lib/utils';
import { commandPaneInsetOf, paneInsetTransition } from '@/lib/commandPane';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { CollimateState } from '@/stores/useCollimateStore';
import { ALL_CELLS, JOURNAL_PANE_WIDTH, journalKey } from '@/stores/collimate/overviewSlice';
import { MoveGlyph, Pip, RunRing } from './journalGlyphs';
import { fileLabels, groupByDay, hm, mins, runMark, runStateWord, runnerName } from './journalText';

/** Where a cell named in the journal is, and what the overview knows of it. */
interface CellInfo {
    place: 'board' | 'shelf';
    name: string;
    stack: string | null;
}

/** Every board and shelf cell, by id (one pass per change, not per name). */
function useCellInfo(): Map<string, CellInfo> {
    const stacks = useCollimateStore(s => s.activeChannel?.main_stacks);
    const removed = useCollimateStore(s => s.overviewData.removed);
    return useMemo(() => {
        const info = new Map<string, CellInfo>();
        for (const entry of removed ?? []) {
            info.set(entry.cell_id, {
                place: 'shelf', name: entry.name,
                stack: entry.stack_cell_count > 1 ? entry.stack_name : null,
            });
        }
        for (const stack of stacks ?? []) {
            for (const cell of stack.cells) {
                info.set(String(cell.id), { place: 'board', name: cell.name, stack: stack.name || null });
            }
        }
        return info;
    }, [stacks, removed]);
}

/** The head's cell: its name, its stack (when it says more than the name)
 *  and whether it is on the shelf. */
const headOf = (s: CollimateState, key: string | null, info: Map<string, CellInfo>) => {
    if (key === null) return { title: 'Journal', stack: null, removed: false };
    if (key === ALL_CELLS) return { title: 'All cells', stack: null, removed: false };
    const known = info.get(key);
    const title = known?.name ?? s.overviewData.lineage?.cells.find(c => c.cell_id === key)?.name ?? key;
    const stack = known?.stack && known.stack !== title ? known.stack : null;
    return { title, stack, removed: known?.place === 'shelf' };
};

const NAME = 'text-left font-medium text-ink';
const NAME_BUTTON = 'hover:underline hover:decoration-ink-3 hover:underline-offset-[0.1875rem]';
const NAME_GONE = 'text-ink-2 underline decoration-dashed decoration-line-2 underline-offset-[0.1875rem] hover:decoration-ink-3';

/** A cell's name: a button that walks to it (on the board or the shelf), a
 *  dashed underline while it is removed. */
function CellName({ cell, info, walk }: { cell: JournalCellRef; info: Map<string, CellInfo>; walk: (id: string) => void }) {
    const known = info.get(cell.cell_id);
    const name = known?.name ?? cell.name;
    const gone = known ? known.place === 'shelf' : cell.status === 'removed';
    if (!known) {
        return <span data-cell-name={cell.cell_id} className={cn(NAME, gone && NAME_GONE)}>{name}</span>;
    }
    return (
        <button
            type="button"
            data-cell-name={cell.cell_id}
            className={cn(NAME, NAME_BUTTON, gone && NAME_GONE)}
            title={gone ? `${name} · in Removed` : name}
            onClick={(e) => { e.stopPropagation(); walk(cell.cell_id); }}
        >
            {name}
        </button>
    );
}

const Runner = ({ name }: { name: string }) => (
    <span data-runner-name className="inline-block font-medium text-ink-2">{name}</span>
);

const File = ({ label }: { label: string }) => (
    <span data-file-name className="inline-block font-mono text-[0.88em] font-normal text-ink-2">{label}</span>
);

/** "a", "a and b", "a, b and c". */
function andList(nodes: ReactNode[]): ReactNode {
    return nodes.map((node, i) => (
        <Fragment key={i}>
            {i > 0 && (i === nodes.length - 1 ? ' and ' : ', ')}
            {node}
        </Fragment>
    ));
}

/** The files a run wrote: three names, then how many more. */
const WROTE_SHOWN = 3;
function wroteList(paths: readonly string[], total: number): ReactNode {
    const labels = fileLabels(paths);
    const shown: ReactNode[] = paths.slice(0, WROTE_SHOWN).map((path, i) => <File key={path} label={labels[i]} />);
    const more = Math.max(total, paths.length) - shown.length;
    return andList(more > 0 ? [...shown, `${more} more`] : shown);
}

function Sub({ parts }: { parts: ReactNode[] }) {
    if (!parts.length) return null;
    return (
        <span className="block text-[0.75rem] text-ink-3">
            {parts.map((part, i) => <Fragment key={i}>{i > 0 && ' · '}{part}</Fragment>)}
        </span>
    );
}

/** One event as the mock's evHtml writes it. */
function JournalSentence({ event, now, info, walk }: {
    event: JournalEvent; now: number; info: Map<string, CellInfo>; walk: (id: string) => void;
}) {
    const C = (cell: JournalCellRef | undefined) => (cell ? <CellName cell={cell} info={info} walk={walk} /> : '…');
    const who = (by: CellActor) => (by.kind === 'user' ? 'You' : <Runner name={by.runner_name ?? by.runner_id ?? 'A runner'} />);
    const [first, ...rest] = event.cells;
    switch (event.kind) {
        case 'make':
            return <>{who(event.by)} made {C(first)}</>;
        case 'edit':
            return <>{who(event.by)} edited {C(first)}</>;
        case 'run':
        case 'run_cell': {
            const run = event.run;
            if (!run) return <>{who(event.by)} ran on {C(first)}</>;
            const running = runMark(run) === 'running';
            const parts: ReactNode[] = [];
            if (running) {
                const since = run.started_at ? Date.parse(run.started_at) : Number.NaN;
                parts.push(Number.isNaN(since) ? 'running' : `running for ${mins(now - since)}`);
            } else {
                parts.push(runStateWord(run));
            }
            if (event.kind === 'run_cell') {
                if (rest.length) parts.push(<>made {andList(rest.map(C))}</>);
            } else {
                if (event.version_id) parts.push('new version');
                if (run.files_written.length) parts.push(<>wrote {wroteList(run.files_written, event.files_total)}</>);
            }
            return (
                <>
                    <Runner name={runnerName(run)} />{running ? ' is running on ' : ' ran on '}{C(first)}
                    <Sub parts={parts} />
                </>
            );
        }
        case 'cell_renamed':
            return <>{who(event.by)} renamed “{event.old_name}” to “{event.new_name}”<Sub parts={[C(first)]} /></>;
        case 'duplicate':
            return <>{who(event.by)} duplicated {C(first)} as {andList(rest.map(C))}</>;
        case 'merge': {
            const into = event.cells[event.cells.length - 1];
            return <>{who(event.by)} merged {andList(event.cells.slice(0, -1).map(C))} into {C(into)}</>;
        }
        case 'split':
            return <>{who(event.by)} split {andList(rest.map(C))} out of {C(first)}</>;
    }
}

function EventGlyph({ event }: { event: JournalEvent }) {
    if (event.run) return <RunRing mark={runMark(event.run)} />;
    if (event.kind === 'duplicate' || event.kind === 'merge' || event.kind === 'split') return <MoveGlyph kind={event.kind} />;
    return <Pip />;
}

const EMPTY_EVENTS: JournalEvent[] = [];

/** The journal pane; nothing while the journal is shut. */
export function JournalPane() {
    const open = useCollimateStore(s => s.overview.journalOpen);
    useGuestOccluder('all', open);
    const key = useCollimateStore(journalKey);
    const page = useCollimateStore(s => (key ? s.overviewData.journals[key] : undefined));
    const { showJournal, loadMoreJournal, setJournalOpen, walkTo } = useCollimateStore(useShallow(s => ({
        showJournal: s.showJournal, loadMoreJournal: s.loadMoreJournal, setJournalOpen: s.setJournalOpen, walkTo: s.walkTo,
    })));
    const info = useCellInfo();
    const head = useCollimateStore(useShallow(s => headOf(s, key, info)));
    const paneRef = useRef<HTMLElement>(null);
    const dockRight = useCollimateStore(s => commandPaneInsetOf(s.commandPane).right);
    const reduceMotion = useReducedMotion();

    useEffect(() => { void showJournal(open ? key : null); }, [open, key, showJournal]);

    const events = page?.events ?? EMPTY_EVENTS;
    const running = events.some(e => e.run && runMark(e.run) === 'running');
    // "running for 12m" and "today" follow the clock while the pane is open.
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        if (!open) return undefined;
        const timer = setInterval(() => setNow(Date.now()), running ? 30_000 : 60_000);
        return () => clearInterval(timer);
    }, [open, running]);

    // The pane's own scroll and drags: the wheel scrolls it and a press in it
    // never pans the board (the board's handlers listen on the overview root).
    useEffect(() => {
        const pane = paneRef.current;
        if (!pane) return undefined;
        const stop = (e: Event) => e.stopPropagation();
        pane.addEventListener('wheel', stop, { passive: true });
        pane.addEventListener('pointerdown', stop);
        return () => {
            pane.removeEventListener('wheel', stop);
            pane.removeEventListener('pointerdown', stop);
        };
    }, [open]);

    const days = useMemo(() => groupByDay(events, now), [events, now]);
    if (!open) return null;

    const onScroll = () => {
        const pane = paneRef.current;
        if (!pane || !key || !page || page.loading || page.complete) return;
        if (pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 120) void loadMoreJournal(key);
    };
    const count = `${events.length}${page && !page.complete ? '+' : ''} ${events.length === 1 && page?.complete ? 'event' : 'events'}`;
    const quiet = (text: string) => <div className="px-3 py-3.5 text-[0.8125rem] text-ink-3">{text}</div>;

    return (
        <aside
            ref={paneRef}
            data-overview-overlay
            data-journal-pane
            aria-label="Journal"
            onScroll={onScroll}
            className={cn(
                'absolute top-0 bottom-0 z-[9] cursor-default overflow-y-auto border-l border-line bg-canvas px-1.5 pb-6 select-text',
            )}
            style={{ width: JOURNAL_PANE_WIDTH, right: dockRight, transition: paneInsetTransition(['right'], reduceMotion) }}
        >
            <div className="sticky top-0 z-[2] -mx-1.5 flex h-9 items-center gap-2 border-b border-line bg-canvas pr-3.5 pl-[1.125rem]">
                <h3 className="min-w-0 truncate text-[0.8125rem] font-semibold text-ink">{head.title}</h3>
                {head.stack && <span className="flex-none text-[0.75rem] text-ink-3">{head.stack}</span>}
                {head.removed && (
                    <span data-removed-tag className="flex-none rounded-xs border border-dashed border-line-2 px-[0.3125rem] text-[0.6875rem] leading-4 text-ink-3">
                        removed
                    </span>
                )}
                {key && <span className="ml-auto flex-none text-[0.75rem] text-ink-3">{count}</span>}
                <button
                    type="button"
                    aria-label="Hide the journal"
                    title="Hide the journal"
                    onClick={(e) => { e.stopPropagation(); setJournalOpen(false); }}
                    className={cn(
                        '-mr-1.5 inline-flex h-[1.375rem] w-[1.375rem] flex-none items-center justify-center rounded-xs text-ink-3 hover:bg-sheet-2 hover:text-ink',
                        !key && 'ml-auto',
                    )}
                >
                    <svg
                        data-journal-hide-glyph
                        viewBox="0 0 16 16"
                        aria-hidden
                        className="h-3.5 w-3.5 flex-none"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth={1.6}
                        strokeLinecap="round"
                    >
                        <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" />
                    </svg>
                </button>
            </div>
            {!key && quiet('Select a cell to read its journal.')}
            {key && page?.error && !events.length && quiet(`The journal did not load: ${page.error}`)}
            {key && !page?.error && !events.length && (page?.complete
                ? quiet(key === ALL_CELLS ? 'Nothing has happened in this channel yet.' : 'Nothing has happened to this cell yet.')
                : quiet('Loading…'))}
            {days.map(day => (
                <section key={day.key} data-journal-day={day.key}>
                    <div className="sticky top-9 z-[1] bg-canvas pt-3.5 pr-2 pb-1.5 pl-3 text-[0.6875rem] font-semibold tracking-[0.08em] text-ink-3 uppercase">
                        {day.label}
                    </div>
                    {day.events.map((event, i) => (
                        <div
                            key={`${event.t}-${event.kind}-${i}`}
                            data-journal-row={event.kind}
                            className="grid grid-cols-[2.375rem_0.75rem_minmax(0,1fr)_auto] items-start gap-x-2 rounded-md py-1.5 pr-2 pl-3 text-[0.8125rem] leading-[1.125rem] text-ink-2"
                        >
                            <span className="font-mono text-[0.6875rem] text-ink-3">{hm(event.t)}</span>
                            <span className="inline-flex h-[1.125rem] items-center justify-center text-ink-3"><EventGlyph event={event} /></span>
                            <span data-journal-sentence><JournalSentence event={event} now={now} info={info} walk={walkTo} /></span>
                            <span />
                        </div>
                    ))}
                </section>
            ))}
            {key && page?.loading && events.length > 0 && quiet('Loading…')}
        </aside>
    );
}
