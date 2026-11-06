// The journal's words, shared by the pane and the journal-style card: local
// times from the routes' UTC ISO times, a run's mark and state in words, the
// chain's versions and a cell's runs.

import type { JournalEvent, JournalRun } from '@/lib/api/cells';
import { stampOf } from '@/lib/stackPips';
import type { HistoryResponse } from '@/lib/api/history';
import { isTerminalPhase } from '@/lib/runningCell';

const MON = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ');
const WDAY = 'Sun Mon Tue Wed Thu Fri Sat'.split(' ');

/** A wire time as a Date: ISO with an offset (the journal, the shelf, the
 *  lineage) or the offset-less UTC text the kept history route sends
 *  (`YYYY-MM-DD HH:MM:SS`), never read as local time. */
const local = (t: string | number): Date => new Date(typeof t === 'number' ? t : stampOf(t));
const two = (v: number): string => String(v).padStart(2, '0');

/** "14:05", local time. */
export const hm = (t: string | number): string => {
    const d = local(t);
    return Number.isNaN(d.getTime()) ? '' : `${two(d.getHours())}:${two(d.getMinutes())}`;
};

/** "4 Oct", local time. */
export const dmon = (t: string | number): string => {
    const d = local(t);
    return Number.isNaN(d.getTime()) ? '' : `${d.getDate()} ${MON[d.getMonth()]}`;
};

/** "4 Oct · 14:05". */
export const when = (t: string | number): string => `${dmon(t)} · ${hm(t)}`;

/** The local calendar day of a time. */
export const dayKey = (t: string | number): string => {
    const d = local(t);
    return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
};

/** A day head: "Sun 4 Oct", and " · today" on today (shown uppercase). */
export const dayHead = (t: string | number, now: number): string =>
    `${WDAY[local(t).getDay()]} ${dmon(t)}${dayKey(t) === dayKey(now) ? ' · today' : ''}`;

/** Events (newest first) grouped by their local day, in order. */
export function groupByDay<E extends { t: string }>(events: readonly E[], now: number): { key: string; label: string; events: E[] }[] {
    const days: { key: string; label: string; events: E[] }[] = [];
    for (const event of events) {
        const key = dayKey(event.t);
        const last = days[days.length - 1];
        if (last && last.key === key) last.events.push(event);
        else days.push({ key, label: dayHead(event.t, now), events: [event] });
    }
    return days;
}

/** "12m", "1h 5m" (at least a minute). */
export const mins = (ms: number): string => {
    const m = Math.max(1, Math.round(ms / 60000));
    return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
};

/** A run's mark: the cell light's grammar in small. */
export type RunMark = 'running' | 'pass' | 'fail' | 'stopped' | 'done';

export function runMark(run: JournalRun): RunMark {
    if (run.ended_at === null && !isTerminalPhase(run.status)) return 'running';
    if (run.outcome === 'pass') return 'pass';
    if (run.outcome === 'fail' || run.status === 'failed' || run.status === 'timed_out') return 'fail';
    if (run.status === 'stopped' || run.status === 'interrupted') return 'stopped';
    return 'done';
}

const MARK_WORD: Record<Exclude<RunMark, 'running'>, string> = {
    pass: 'passed', fail: 'failed', stopped: 'stopped', done: 'done',
};

/** A finished run in words: its note, else its state ("timed out" for a
 *  timeout, which marks as a failure). */
export function runStateWord(run: JournalRun): string {
    if (run.note) return run.note;
    if (run.status === 'timed_out') return 'timed out';
    if (run.status === 'interrupted') return 'interrupted';
    const mark = runMark(run);
    return mark === 'running' ? 'running' : MARK_WORD[mark];
}

/** The runner a run went through: its name, else its id. */
export const runnerName = (run: JournalRun): string => run.runner_name || run.runner_id || 'A run';

/** A file's base name. */
export const baseName = (path: string): string => path.split('/').pop() ?? path;

/** How a list names its files: each by its base name, and a base name two
 *  of them share with the shortest folder suffix that tells it from the
 *  others ("Scratch/scratch.txt", "Scratch (Copy)/scratch.txt"). In the
 *  input order. */
export function fileLabels(paths: readonly string[]): string[] {
    const parts = paths.map(path => path.split('/'));
    const tail = (segs: readonly string[], n: number): string => segs.slice(-n).join('/');
    const byBase = new Map<string, number[]>();
    parts.forEach((segs, i) => {
        const base = segs[segs.length - 1];
        byBase.set(base, [...(byBase.get(base) ?? []), i]);
    });
    return parts.map((segs, i) => {
        const others = byBase.get(segs[segs.length - 1])!.filter(j => j !== i && paths[j] !== paths[i]);
        let n = 1;
        while (n < segs.length && others.some(j => tail(parts[j], n) === tail(segs, n))) n++;
        return tail(segs, n);
    });
}


export interface CardVersion {
    nodeId: string;
    /** The version's cell id (a row on the cell's chain). */
    versionId: string;
    author: string;
    at: string | null;
}

/** The chain, newest first, each version with its author: a run node's
 *  runner (its name, else its id, else the generator), else the user. */
export function chainVersions(history: HistoryResponse): CardVersion[] {
    return history.nodes.flatMap(node => [...node.versions].reverse().map(version => ({
        nodeId: node.node_id,
        versionId: version.cell_id,
        author: node.kind === 'run' ? node.run?.runner_name || node.run?.runner_id || node.run?.generator_name || 'run' : 'you',
        at: version.created_at,
    })));
}

/** The runs on the cell (not the runs it was made by): running first, then
 *  by end, newest first. */
export function cellRuns(events: readonly JournalEvent[], cellId: string, now: number): JournalEvent[] {
    const end = (e: JournalEvent) => (e.run && runMark(e.run) === 'running' ? now : stampOf(e.t));
    return events.filter(e => e.run && e.cells[0]?.cell_id === cellId).sort((a, b) => end(b) - end(a));
}
