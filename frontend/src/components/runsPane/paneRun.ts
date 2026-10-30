import { useMemo } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { runRecordFrom } from '@/lib/runningCell';
import type { RunRecord } from '@/lib/types';
import type { ActiveRunInfo } from '@/lib/api';
import type { FinishedRun } from '@/stores/collimate/types';

/** A run the status pane shows: live (attached, or listed live and not yet
 *  attached) or finished this session. */
export interface PaneRun {
    run: RunRecord | FinishedRun;
    live: boolean;
}

/** The run's name as the pane reads it. */
export const runnerOf = (r: Pick<RunRecord, 'runnerName' | 'label'>): string => r.runnerName || r.label;

/** The run of the cell's drilldown: its live record, else its finished
 *  record (an ended run's row can linger in the live list, stopping, until
 *  the list refreshes), else its row in the active channel's live runs (a
 *  run not attached yet); null when the pane knows it no more. */
export function usePaneRun(cellId: string, runId: string): PaneRun | null {
    const record = useCollimateStore(s => s.runs[runId]);
    const channel = useCollimateStore(s => s.activeChannel?.id ?? null);
    const row = useCollimateStore(s => s.activeRunsEverywhere.find(e => e.channel === channel)?.runs.find(r => r.run_id === runId));
    const finished = useCollimateStore(s => s.finishedRuns[cellId]?.find(r => r.runId === runId));
    return useMemo(() => {
        if (record) return { run: record, live: true };
        if (finished) return { run: finished, live: false };
        if (row && channel && row.cell_id === cellId) return { run: runRecordFrom(row, channel), live: true };
        return null;
    }, [record, row, channel, cellId, finished]);
}

/** The finished record's status, when the run has ended. */
export const finishedOf = (r: RunRecord | FinishedRun): FinishedRun | null => ('status' in r ? r : null);

/** "Docker full adopted for <harness>": what a run whose grant was raised
 *  to its harness's minimum says about it (an Annotation run adopts
 *  `docker: full` for a harness that needs it); null for any other run. */
export function adoptedGrantText(
    row: Pick<ActiveRunInfo, 'adopted' | 'effective_grant' | 'harness' | 'harness_label'> | undefined,
): string | null {
    if (!row?.adopted || !row.effective_grant) return null;
    return `Docker ${row.effective_grant.docker} adopted for ${row.harness_label || row.harness || 'its harness'}`;
}

/** "<harness> CLI on this machine": what a CLI run whose harness CLI runs on
 *  this machine (not in its container) says about it; null for any other
 *  run. */
export function hostCliText(
    row: Pick<ActiveRunInfo, 'cli_locus' | 'harness' | 'harness_label'> | undefined,
): string | null {
    if (row?.cli_locus !== 'host') return null;
    return `${row.harness_label || row.harness || 'Its'} CLI on this machine`;
}

/** The adopted-grant line of a live run, from its row in the every-channel
 *  live list; null once the run has left it or when nothing was adopted. */
export function useAdoptedGrant(runId: string): string | null {
    return useCollimateStore(s => {
        for (const entry of s.activeRunsEverywhere) {
            const row = entry.runs.find(r => r.run_id === runId);
            if (row) return adoptedGrantText(row);
        }
        return null;
    });
}

/** The host-CLI line of a live CLI run whose harness CLI runs on this
 *  machine; null once the run has left the live list, or for any other run. */
export function useHostCli(runId: string): string | null {
    return useCollimateStore(s => {
        for (const entry of s.activeRunsEverywhere) {
            const row = entry.runs.find(r => r.run_id === runId);
            if (row) return hostCliText(row);
        }
        return null;
    });
}
