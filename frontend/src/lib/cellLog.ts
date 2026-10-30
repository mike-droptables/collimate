// The cell's one log (row C15): every run that started on the cell this
// session, live and finished, its lines merged by time into
// `HH:MM  runner  message`.
//
// Each run's lines are the canonical run-log lines (lib/runLogFormat): an
// optional `[HH:MM:SS] ` local wall-clock prefix, then a tag and the text.
// The prefix carries no date, so it is placed on the day of a reference
// time known to come at or after the line (its arrival, or the moment the
// saved log was read); a line without a prefix takes its arrival time, or
// failing that the time of the line before it.

import { api } from '@/lib/api/client';
import { fetchBlobText } from '@/lib/api/history';

/** One run in a cell's log. */
export interface CellLogRun {
    runId: string;
    channel: string;
    /** The runner's display name (the run's label for a runner-less run). */
    runner: string;
    /** When this client first saw the run. */
    startedAt: number;
    /** Set when the run ends: its live lines and their arrival times. */
    ended?: { lines: readonly string[]; arrivals: readonly number[] };
    /** The run's saved `logs.txt`, read once after the run ended. A failed
     *  read keeps the live lines and is not retried. */
    saved?: { state: 'pending' } | { state: 'failed' } | { state: 'read'; lines: readonly string[]; at: number };
}

/** One merged line of a cell's log. */
export interface CellLogLine {
    at: number;
    runner: string;
    message: string;
    /** An explicitly leveled error line (ERR, IPCERR, TIMOUT). */
    error: boolean;
}

/** The lines one run contributes and how to date them. */
export interface CellLogSource {
    runner: string;
    lines: readonly string[];
    /** Arrival time per line (parallel to `lines`); absent for a saved log. */
    arrivals?: readonly number[];
    /** A time at or after every line: the saved log's read time. */
    readAt?: number;
    /** Dates a first line that has neither a prefix nor an arrival. */
    startedAt: number;
}

const PREFIX = /^\[(\d{2}):(\d{2}):(\d{2})\] /;
const ERROR_TAGS = new Set(['ERR', 'IPCERR', 'TIMOUT']);
/** A line's wall-clock may run this far ahead of its reference (clock skew
 *  between the server and this client) before it is read as yesterday's. */
const SKEW_MS = 5 * 60_000;

/** Split a line into its `[HH:MM:SS]` time (placed on `ref`'s day, or the
 *  day before when that would put it after `ref`) and the rest. */
export function parseLogLine(line: string, ref: number): { at: number | null; rest: string } {
    const m = PREFIX.exec(line);
    if (!m) return { at: null, rest: line };
    const d = new Date(ref);
    d.setHours(Number(m[1]), Number(m[2]), Number(m[3]), 0);
    let at = d.getTime();
    if (at > ref + SKEW_MS) at -= 24 * 60 * 60_000;
    return { at, rest: line.slice(m[0].length) };
}

/** `TAG    text` → the message as the log shows it: the tag's padding
 *  folds to one space. */
function messageOf(rest: string): { message: string; error: boolean } {
    const m = /^([A-Z]+)\s+(.*)$/s.exec(rest);
    if (!m) return { message: rest, error: false };
    return { message: `${m[1]} ${m[2]}`, error: ERROR_TAGS.has(m[1]) };
}

/** Every source's lines, ordered by time; a tie keeps source order, then
 *  line order. */
export function mergeCellLog(sources: readonly CellLogSource[]): CellLogLine[] {
    const out: (CellLogLine & { s: number; i: number })[] = [];
    sources.forEach((src, s) => {
        let prev = src.startedAt;
        src.lines.forEach((line, i) => {
            const arrival = src.arrivals?.[i];
            const ref = arrival ?? src.readAt ?? Date.now();
            const { at, rest } = parseLogLine(line, ref);
            const when = at ?? arrival ?? prev;
            prev = when;
            const { message, error } = messageOf(rest);
            out.push({ at: when, runner: src.runner, message, error, s, i });
        });
    });
    out.sort((a, b) => a.at - b.at || a.s - b.s || a.i - b.i);
    return out.map(({ at, runner, message, error }) => ({ at, runner, message, error }));
}

/** `HH:MM` in local time. */
export function clockOf(at: number): string {
    const d = new Date(at);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** The lines one run contributes: a saved logs.txt once read, else the
 *  lines snapshotted when the run ended, else the live lines. */
export function cellLogSource(run: CellLogRun, live?: readonly string[], liveAt?: readonly number[]): CellLogSource {
    if (run.saved?.state === 'read') {
        return { runner: run.runner, lines: run.saved.lines, readAt: run.saved.at, startedAt: run.startedAt };
    }
    if (run.ended) {
        return { runner: run.runner, lines: run.ended.lines, arrivals: run.ended.arrivals, startedAt: run.startedAt };
    }
    return { runner: run.runner, lines: live ?? [], arrivals: liveAt, startedAt: run.startedAt };
}

/** The host's own run log in a run's artifacts. */
const RUN_LOG_PATH = 'logs.txt';

interface RunArtifacts {
    artifacts: { category: string; path: string; blob_hash: string; size: number }[];
}

/** A finished run's saved `logs.txt`, as lines (GET /runs/{id}/artifacts,
 *  then the blob). Throws when the run has none. */
export async function readSavedRunLog(channel: string, runId: string): Promise<string[]> {
    const { data } = await api.get<RunArtifacts>(
        `/channels/${encodeURIComponent(channel)}/runs/${encodeURIComponent(runId)}/artifacts`,
    );
    const log = data.artifacts.find(a => a.category === 'log' && a.path === RUN_LOG_PATH);
    if (!log) throw new Error(`run ${runId} has no ${RUN_LOG_PATH}`);
    const text = await fetchBlobText(log.blob_hash);
    return text.split('\n').filter(line => line.length > 0);
}
