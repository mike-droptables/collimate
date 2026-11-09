// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { attachToRun, dispatchRunStatus } from './runAttach';
import type { RunRecord } from './types';
import { useCollimateStore } from '@/stores/useCollimateStore';

it('F0958: moved run bus attaches and finishes the actual run record', async () => {
    const run: RunRecord = {
        runId: 'ownership-run', cellId: 'ownership-cell', channel: 'ownership',
        runnerId: 'runner', runnerName: 'Runner', generatorName: 'agent',
        label: 'runner-ownership', folder: '_runs/runner-ownership',
    };
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({
        events: [], status: 'running', phase: 'running', is_active: true, is_terminal: false,
    }) })));
    // The run's file touches are not under test; a real request would settle
    // (and log its failure) after the test ends.
    useCollimateStore.setState({ loadFileTouches: async () => {} });
    const detach = attachToRun(run);
    try {
        await vi.waitFor(() => expect(useCollimateStore.getState().runPhases[run.runId]?.phase).toBe('running'));
        expect(useCollimateStore.getState().runs[run.runId]).toEqual(run);
        dispatchRunStatus(run.runId, 'failed', 'ownership failure');
        expect(useCollimateStore.getState().endedRuns[run.runId]).toMatchObject({
            cellId: run.cellId, status: 'failed', failureReason: 'ownership failure',
        });
    } finally {
        detach();
        vi.unstubAllGlobals();
    }
});
