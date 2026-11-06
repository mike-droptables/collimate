// The journal-style card body (the mock's jbody), for the journal's subject
// on the board or on the shelf: the meta row (the current version's time and
// author), the cell's versions (its chain, from the files-pane history route,
// D-O09), its files and its runs (from its journal). The card around it is
// the overview's or the shelf's.

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { CircleDot, Clock, File, History } from 'lucide-react';
import { fetchCellHistory, type HistoryResponse } from '@/lib/api/history';
import type { JournalEvent } from '@/lib/api/cells';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { FileRows, Pip, RunRing } from './journalGlyphs';
import { cellRuns, chainVersions, dmon, hm, mins, runMark, runStateWord, runnerName, when } from './journalText';

const SHOWN_VERSIONS = 6;
/** Every file is listed up to the shelf route's bound on a cell's files;
 *  a larger cell says how many more it has. */
const SHOWN_FILES = 200;
const NO_EVENTS: JournalEvent[] = [];

/** "running · 12m". */
const runningFor = (startedAt: string | null, now: number): string => {
    const since = startedAt ? Date.parse(startedAt) : Number.NaN;
    return Number.isNaN(since) ? 'running' : `running · ${mins(now - since)}`;
};

const H = 'mt-2.5 flex items-center gap-1.5 text-[0.6875rem] font-semibold tracking-[0.08em] text-ink-2 uppercase';
const H_ICON = 'h-3 w-3 text-ink-3';
const LIST = 'm-0 mt-[0.1875rem] list-none pl-[1.125rem]';
const ROW = 'flex h-[1.1875rem] min-w-0 items-center gap-[0.4375rem] whitespace-nowrap';
const TIME = 'ml-auto flex-none font-mono text-[0.65625rem] text-ink-3';
const MORE = 'text-ink-3';

function Heading({ icon, children }: { icon: ReactNode; children: ReactNode }) {
    return <div className={H}>{icon}{children}</div>;
}

export interface JournalCardBodyProps {
    cellId: string;
    /** The card's own date and author, shown until the chain names the
     *  current version's. */
    at: string | null;
    by: string;
    /** The cell's files (as the card already shows them) and their count. */
    files: readonly string[];
    fileCount: number;
    /** A version row was picked: its rail node and version cell id. Without
     *  it the rows are not pickable. */
    onPickVersion?: (nodeId: string, versionId: string) => void;
}

export function JournalCardBody({ cellId, at, by, files, fileCount, onPickVersion }: JournalCardBodyProps) {
    const channel = useCollimateStore(s => s.overviewData.channel);
    const events = useCollimateStore(s => s.overviewData.journals[cellId]?.events ?? NO_EVENTS);
    const [history, setHistory] = useState<{ cellId: string; data: HistoryResponse } | null>(null);
    // A new event on the cell (a version, a run) refetches its chain.
    const newest = events[0]?.t ?? '';

    useEffect(() => {
        if (!channel) return undefined;
        const abort = new AbortController();
        fetchCellHistory(channel, cellId, abort.signal)
            .then(data => setHistory({ cellId, data }))
            .catch(() => { /* the versions stay out; the card still reads */ });
        return () => abort.abort();
    }, [channel, cellId, newest]);

    const [now] = useState(() => Date.now());
    const versions = useMemo(() => (history?.cellId === cellId ? chainVersions(history.data) : []), [history, cellId]);
    const current = history?.cellId === cellId ? history.data.current.version_cell_id : null;
    const currentVersion = versions.find(version => version.versionId === current);
    const metaAt = currentVersion ? currentVersion.at : at;
    const metaBy = currentVersion ? currentVersion.author : by;
    const runs = useMemo(() => cellRuns(events, cellId, now), [events, cellId, now]);
    const shownFiles = files.slice(0, SHOWN_FILES);
    const moreFiles = Math.max(fileCount, files.length) - shownFiles.length;

    return (
        <div data-journal-card-body={cellId}>
            <div className="flex items-center gap-1.5 whitespace-nowrap text-ink-3">
                <Clock className="h-3 w-3" />
                {metaAt ? when(metaAt) : ''}
                <span className="ml-auto truncate">{metaBy}</span>
            </div>
            {versions.length > 1 && (
                <>
                    <Heading icon={<History className={H_ICON} />}>Versions ({versions.length})</Heading>
                    <ul className={LIST} data-card-versions>
                        {versions.slice(0, SHOWN_VERSIONS).map(version => {
                            const body = (
                                <>
                                    <Pip current={version.versionId === current} />
                                    <span className="flex-none text-ink-2 group-hover:text-ink">{version.author}</span>
                                    <span className={TIME}>{version.at ? `${dmon(version.at)} ${hm(version.at)}` : ''}</span>
                                </>
                            );
                            return (
                                <li key={version.versionId} className={ROW}>
                                    {onPickVersion ? (
                                        <button
                                            type="button"
                                            data-version-row={version.versionId}
                                            className={cn(ROW, 'group w-full cursor-pointer text-left')}
                                            onClick={(e) => { e.stopPropagation(); onPickVersion(version.nodeId, version.versionId); }}
                                        >
                                            {body}
                                        </button>
                                    ) : body}
                                </li>
                            );
                        })}
                        {versions.length > SHOWN_VERSIONS && <li className={cn(ROW, MORE)}>+{versions.length - SHOWN_VERSIONS} more</li>}
                    </ul>
                </>
            )}
            <Heading icon={<File className={H_ICON} />}>Files ({Math.max(fileCount, files.length)})</Heading>
            <ul className={cn(LIST, 'leading-[1.6] whitespace-nowrap')}>
                <FileRows paths={shownFiles} among={files} />
                {moreFiles > 0 && <li className={MORE}>+{moreFiles} more</li>}
            </ul>
            {runs.length > 0 && (
                <>
                    <Heading icon={<CircleDot className={H_ICON} />}>Runs ({runs.length})</Heading>
                    <ul className={LIST} data-card-runs>
                        {runs.map(event => {
                            const run = event.run!;
                            const mark = runMark(run);
                            return (
                                <li key={run.run_id} className={ROW} data-card-run={mark}>
                                    <RunRing mark={mark} />
                                    <span className="flex-none text-ink-2">{runnerName(run)}</span>
                                    <span className="min-w-0 flex-1 truncate text-ink-3">{mark === 'running' ? runningFor(run.started_at, now) : runStateWord(run)}</span>
                                    <span className={TIME}>{mark === 'running' ? 'now' : `${dmon(event.t)} ${hm(event.t)}`}</span>
                                </li>
                            );
                        })}
                    </ul>
                </>
            )}
        </div>
    );
}
