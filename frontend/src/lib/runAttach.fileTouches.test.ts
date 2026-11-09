// @vitest-environment jsdom
//
// A run's file touches (the drilldown's timeline): attaching loads them
// (GET /runs/{id}/file_touches) beside the event backfill; every commit row
// that carries `changes`, backfilled or live, adds them, deduplicated by
// (version, path); the commit's log line is unchanged.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const fetchFileTouches = vi.hoisted(() => vi.fn());
vi.mock('@/lib/api/annotations', async (importOriginal) => ({ ...(await importOriginal<object>()), fetchFileTouches }));

import { attachToRun, dispatchRunEvents } from './runAttach';
import { formatLogLine } from './runLogFormat';
import { useCollimateStore } from '@/stores/useCollimateStore';

const commit = (id: number, cellId: number, changes: unknown[], at = '2026-10-24 10:00:00') => ({
    id, type: 'commit', created_at: at, payload: JSON.stringify({ cell_id: cellId, files: ['a.md'], changes }),
});

const stubBackfill = (events: unknown[]) => vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (String(url).includes('/events_since')) {
        return { ok: true, json: async () => ({ events, status: null, phase: null, is_active: true, is_terminal: false, last_event_id: 0, truncated: false }) };
    }
    throw new Error('unexpected fetch: ' + url);
}));

const attach = (runId: string) => attachToRun({
    runId, cellId: 'c1', channel: 'ch', runnerId: 'agent', runnerName: 'Agent', generatorName: 'agent',
    label: `agent-${runId}`, folder: `_runs/agent-${runId}`,
});

describe('attachToRun — file touches', () => {
    beforeEach(() => {
        useCollimateStore.setState({ runs: {}, runLogs: {}, runLogArrivals: {}, fileTouches: {}, cellLogRuns: {} });
        fetchFileTouches.mockReset();
    });
    afterEach(() => vi.unstubAllGlobals());

    it('loads the run\'s touches on attach; backfilled and live commits add theirs, deduplicated; the log line stays the commit\'s', async () => {
        fetchFileTouches.mockResolvedValue([{ at: '2026-10-24 09:59:00', path: 'a.md', op: 'create', folder: false, version_id: '7' }]);
        const backfilled = commit(1, 7, [{ path: 'a.md', op: 'create', folder: false }], '2026-10-24 09:59:00');
        stubBackfill([backfilled]);
        const off = attach('r1');
        expect(fetchFileTouches).toHaveBeenCalledWith('ch', 'r1');
        await vi.waitFor(() => expect(useCollimateStore.getState().runLogs.r1?.length).toBeGreaterThan(0));
        await vi.waitFor(() => expect(useCollimateStore.getState().fileTouches.r1).toHaveLength(1));
        dispatchRunEvents('r1', [commit(2, 8, [{ path: 'a.md', op: 'edit', folder: false }, { path: 'd', op: 'create', folder: true }, { bad: 1 }])]);
        expect(useCollimateStore.getState().fileTouches.r1.map(t => [t.version_id, t.path, t.op, t.folder])).toEqual([
            ['7', 'a.md', 'create', false], ['8', 'a.md', 'edit', false], ['8', 'd', 'create', true],
        ]);
        dispatchRunEvents('r1', [commit(3, 9, 'not a list' as never)]);
        expect(useCollimateStore.getState().fileTouches.r1).toHaveLength(3);
        const line = formatLogLine('commit', backfilled.payload, backfilled.created_at);
        expect(useCollimateStore.getState().runLogs.r1).toContain(line);
        off();
    });
});
