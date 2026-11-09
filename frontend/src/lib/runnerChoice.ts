import type { CollimateState } from '@/stores/useCollimateStore';

/** The runner the cell ran last: its newest live run's, else the one its
 *  last finished run names. A finished run keeps only its label, which the
 *  host mints as `<runner id>-<run id[:8]>`; the caller matches the id
 *  against the catalog, so a runner-less run's `run-…` label selects
 *  nothing. */
export function lastRunnerOf(state: CollimateState, cellId: string): string | null {
    const live = Object.values(state.runs).filter(run => run.cellId === cellId && run.runnerId);
    if (live.length) return live[live.length - 1].runnerId;
    const last = state.lastFinishedByCell[cellId];
    const suffix = last ? `-${last.runId.slice(0, 8)}` : '';
    return last && last.label.endsWith(suffix) ? last.label.slice(0, -suffix.length) : null;
}
