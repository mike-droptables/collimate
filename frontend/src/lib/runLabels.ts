// How a run names itself in the UI: "<runner name> <short id>" (the tab
// labels, the cell's status pane, the run light and the top status), and
// how long it has been going (the status view and the runs menu). The
// short id is the label's run-id suffix (`<runner id>-<run id[:8]>`), so
// the two read the same wherever they meet.

import type { RunRecord } from '@/lib/types';

/** The run's short id: the 8-char run-id suffix of its label. */
export function runShortId(run: Pick<RunRecord, 'label'>): string {
    const dash = run.label.lastIndexOf('-');
    return dash >= 0 ? run.label.slice(dash + 1) : run.label;
}

/** "<runner name> <short id>", or the bare label for a runner-less run. */
export function runDisplayName(run: Pick<RunRecord, 'label' | 'runnerName'>): string {
    return run.runnerName ? `${run.runnerName} ${runShortId(run)}` : run.label;
}

const MINUTE = 60_000;

/** How long a run has been going: `<1m`, `12m`, `1h 5m`; '' without a
 *  readable start. */
export function runElapsed(startedAt: string | null | undefined, now: number): string {
    const start = startedAt ? Date.parse(startedAt) : NaN;
    if (Number.isNaN(start)) return '';
    const m = Math.max(0, Math.round((now - start) / MINUTE));
    return m < 1 ? '<1m' : m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}
