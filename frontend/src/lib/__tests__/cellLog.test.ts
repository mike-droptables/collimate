// readSavedRunLog (C15): a finished run's saved log is its `log`/logs.txt
// artifact, fetched through GET /runs/{id}/artifacts and the blob store.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const get = vi.hoisted(() => vi.fn());
vi.mock('@/lib/api/client', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    api: { get },
}));

import { cellLogSource, readSavedRunLog, type CellLogRun } from '../cellLog';

beforeEach(() => { get.mockReset(); });
afterEach(() => { vi.unstubAllGlobals(); });

describe('readSavedRunLog', () => {
    it('reads logs.txt from the run\'s artifacts, as lines (C15)', async () => {
        get.mockResolvedValue({ data: { artifacts: [
            { category: 'config', path: 'logs.txt', blob_hash: 'not-this', size: 1 },
            { category: 'log', path: 'logs.txt', blob_hash: 'abc123', size: 30 },
        ] } });
        const fetchMock = vi.fn(async () => new Response('[10:00:00] RUN    started\n[10:00:01] OUT    hi\n'));
        vi.stubGlobal('fetch', fetchMock);
        await expect(readSavedRunLog('My channel', 'run/1')).resolves.toEqual(['[10:00:00] RUN    started', '[10:00:01] OUT    hi']);
        expect(get).toHaveBeenCalledWith('/channels/My%20channel/runs/run%2F1/artifacts');
        expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toMatch(/\/blob\/abc123$/);
    });

    it('throws when the run has no saved log (C15)', async () => {
        get.mockResolvedValue({ data: { artifacts: [] } });
        await expect(readSavedRunLog('ch', 'r')).rejects.toThrow(/logs\.txt/);
    });
});

describe('cellLogSource', () => {
    const run: CellLogRun = { runId: 'r', channel: 'ch', runner: 'Agent', startedAt: 5 };
    it('prefers the saved log once read, then the lines snapshotted at the end, then the live lines', () => {
        const ended = { lines: ['e'], arrivals: [7] };
        expect(cellLogSource({ ...run, ended, saved: { state: 'read', lines: ['s'], at: 9 } }, ['l'], [6]))
            .toEqual({ runner: 'Agent', lines: ['s'], readAt: 9, startedAt: 5 });
        expect(cellLogSource({ ...run, ended, saved: { state: 'failed' } }, ['l'], [6]))
            .toEqual({ runner: 'Agent', lines: ['e'], arrivals: [7], startedAt: 5 });
        expect(cellLogSource(run, ['l'], [6])).toEqual({ runner: 'Agent', lines: ['l'], arrivals: [6], startedAt: 5 });
        expect(cellLogSource(run)).toEqual({ runner: 'Agent', lines: [], arrivals: undefined, startedAt: 5 });
    });
});
