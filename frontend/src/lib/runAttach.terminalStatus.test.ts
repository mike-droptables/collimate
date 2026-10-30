// @vitest-environment jsdom
//
// attachToRun's terminal-status handling: the live label table (the
// `RUN finished · <label>` bookend in the run's own log), the 'timed_out' /
// 'interrupted' statuses, the zero-output "Completed · no output" label, and
// the failure_reason capture that feeds the finished-run records (the cell's
// run light and the top status).
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { attachToRun, dispatchRunStatus, dispatchRunEvents } from './runAttach';
import { useCollimateStore } from '@/stores/useCollimateStore';

interface BackfillBody {
    events?: Array<{ id: number; type: string; payload: string; created_at: string }>;
    status?: string | null;
    phase?: string | null;
    is_active?: boolean;
    is_terminal?: boolean;
    failure_reason?: string | null;
    last_event_id?: number;
    truncated?: boolean;
    dropped_logs?: number;
}

// Route events_since to the given body; anything else is unexpected.
const stubBackfill = (body: BackfillBody) => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
        if (String(url).includes('/events_since')) {
            return {
                ok: true,
                json: async () => ({
                    events: [], status: null, phase: null,
                    is_active: true, is_terminal: false,
                    ...body,
                }),
            };
        }
        throw new Error('unexpected fetch: ' + url);
    }));
};

const attach = (runId: string, cellId: string) => attachToRun({
    runId,
    cellId,
    channel: 'ch',
    runnerId: 'synthetic-runner',
    runnerName: 'Synthetic Runner',
    generatorName: 'synthetic-gen',
    label: `synthetic-runner-${runId}`,
    folder: `_runs/synthetic-runner-${runId}`,
});

const logsOf = (runId: string) => useCollimateStore.getState().runLogs[runId] ?? [];
const finished = (runId: string) => useCollimateStore.getState().endedRuns[runId];

describe('attachToRun — terminal labels and failure_reason', () => {
    beforeEach(() => {
        localStorage.clear();
        useCollimateStore.setState({
            runs: {},
            stoppingRunIds: new Set<string>(),
            runPhases: {},
            runLogs: {},
            cellCache: {},
            endedRuns: {},
            lastFinishedByCell: {},
        });
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it("a terminal 'timed_out' backfill labels the bookend 'Timed out' and records the reason", async () => {
        stubBackfill({
            status: 'timed_out', is_active: false, is_terminal: true,
            failure_reason: 'Exceeded the 600s wallclock limit',
        });
        attach('run-t', 'cell-t');
        await vi.waitFor(() => {
            expect(finished('run-t')?.status).toBe('timed_out');
        });
        expect(logsOf('run-t').some(l => l.includes('finished · Timed out · synthetic-runner'))).toBe(true);
        // A timeout is a failure outcome: an unseen failure on the cell,
        // carrying its reason.
        expect(finished('run-t')?.failureReason).toBe('Exceeded the 600s wallclock limit');
        expect(useCollimateStore.getState().lastFinishedByCell['cell-t']).toMatchObject({
            runId: 'run-t', status: 'timed_out', reason: 'Exceeded the 600s wallclock limit', seen: false,
        });
        expect(useCollimateStore.getState().runs['run-t']).toBeUndefined();
    });

    it("a live 'interrupted' run_status labels 'Interrupted' and carries its failure_reason", async () => {
        stubBackfill({ status: 'running', phase: 'running' });
        attach('run-i', 'cell-i');
        // Wait for the backfill to complete (phase seeded) before the live status.
        await vi.waitFor(() => {
            expect(useCollimateStore.getState().runPhases['run-i']?.phase).toBe('running');
        });
        dispatchRunStatus('run-i', 'interrupted', 'Server restarted while the run was active');
        await vi.waitFor(() => {
            expect(finished('run-i')?.status).toBe('interrupted');
        });
        expect(logsOf('run-i').some(l => l.includes('finished · Interrupted'))).toBe(true);
        expect(finished('run-i')?.failureReason).toBe('Server restarted while the run was active');
    });

    it("a completed run with a no_output event labels 'Completed · no output'", async () => {
        stubBackfill({
            events: [{
                id: 1, type: 'no_output',
                payload: 'Run completed without producing any output cells',
                created_at: '2026-01-01 00:00:00',
            }],
            status: 'completed', is_active: false, is_terminal: true,
        });
        attach('run-n', 'cell-n');
        await vi.waitFor(() => {
            expect(finished('run-n')?.status).toBe('completed');
        });
        expect(logsOf('run-n').some(l => l.includes('finished · Completed · no output · synthetic-runner'))).toBe(true);
        expect(useCollimateStore.getState().lastFinishedByCell['cell-n']?.seen).toBe(true);
    });

    it('keeps live and retained outcomes but removes unowned diagnostics__F0927_F0531_F0508', async () => {
        stubBackfill({ events: [{ id: 1, type: 'no_output', payload: 'No output', created_at: 'now' }], status: 'running' });
        const detach = attach('retention-run', 'retention-cell');
        // Let the backfill land before the statuses arrive.
        await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
        await new Promise(resolve => setTimeout(resolve, 0));
        dispatchRunStatus('retention-run', 'running', 'retained reason');
        dispatchRunStatus('retention-run', 'completed');
        expect(finished('retention-run')?.failureReason).toBe('retained reason');
        useCollimateStore.setState({ cellCache: { 'retention-cell': { id: 'retention-cell', name: 'kept', files: [] } } });
        useCollimateStore.setState({ endedRuns: {} });
        useCollimateStore.setState({ cellCache: {}, lastFinishedByCell: {} });
        detach();
        stubBackfill({ status: 'completed', is_active: false, is_terminal: true });
        attach('retention-run', 'retention-cell');
        await vi.waitFor(() => expect(finished('retention-run')).toBeDefined());
        expect(finished('retention-run')?.failureReason).toBeUndefined();
    });

    it("a failed finish event's error_message is the failure_reason fallback", async () => {
        stubBackfill({
            events: [{
                id: 1, type: 'finish',
                payload: JSON.stringify({ success: false, error_message: 'rc=128' }),
                created_at: '2026-01-01 00:00:00',
            }],
            status: 'failed', is_active: false, is_terminal: true,
        });
        attach('run-f', 'cell-f');
        await vi.waitFor(() => {
            expect(finished('run-f')?.status).toBe('failed');
        });
        expect(logsOf('run-f').some(l => l.includes('finished · Failed'))).toBe(true);
        expect(finished('run-f')?.failureReason).toBe('rc=128');
    });

    it('two runs on one cell keep separate logs', async () => {
        stubBackfill({
            events: [{ id: 1, type: 'stdout', payload: 'hello', created_at: '2026-01-01 00:00:00' }],
            status: 'running', phase: 'running',
        });
        attach('run-x', 'cell-2');
        attach('run-y', 'cell-2');
        await vi.waitFor(() => {
            expect(logsOf('run-x')).toHaveLength(1);
            expect(logsOf('run-y')).toHaveLength(1);
        });
        dispatchRunStatus('run-x', 'completed');
        await vi.waitFor(() => expect(finished('run-x')?.status).toBe('completed'));
        expect(logsOf('run-y')).toHaveLength(1);
        expect(Object.hasOwn(useCollimateStore.getState().runs, 'run-y')).toBe(true);
    });

    // events_since answers only when the test says so.
    const stubHeldBackfill = () => {
        const answers: Array<(body: BackfillBody) => void> = [];
        vi.stubGlobal('fetch', vi.fn((url: string) => {
            if (!String(url).includes('/events_since')) {
                return Promise.reject(new Error('unexpected fetch: ' + url));
            }
            return new Promise(resolve => {
                answers.push(body => resolve({
                    ok: true,
                    json: async () => ({
                        events: [], status: null, phase: null,
                        is_active: true, is_terminal: false, ...body,
                    }),
                }));
            });
        }));
        return answers;
    };
    const stdout = (id: number, text: string) =>
        ({ id, type: 'stdout', payload: text, created_at: '2026-01-01 00:00:00' });

    it('a run_status that beats the backfill is applied after it: one finish line, last', async () => {
        const answers = stubHeldBackfill();
        attach('run-r', 'cell-r');
        dispatchRunStatus('run-r', 'failed', 'boom');
        expect(Object.hasOwn(useCollimateStore.getState().runs, 'run-r')).toBe(true);
        expect(logsOf('run-r').some(l => /RUN\s+finished/.test(l))).toBe(false);
        answers[0]({ events: [stdout(1, 'early output')], status: 'failed', is_active: false, is_terminal: true });
        await vi.waitFor(() => expect(finished('run-r')?.status).toBe('failed'));
        const logs = logsOf('run-r');
        expect(logs.filter(l => /RUN\s+finished/.test(l))).toHaveLength(1);
        expect(logs.at(-1)).toMatch(/RUN\s+finished/);
        expect(logs.findIndex(l => l.includes('early output'))).toBe(logs.length - 2);
        expect(finished('run-r')?.failureReason).toBe('boom');
    });

    it('a superseded attach writes nothing once its backfill answers', async () => {
        const answers = stubHeldBackfill();
        attach('run-d', 'cell-d');
        attach('run-d', 'cell-d');
        const body = { events: [stdout(1, 'once')], status: 'running', phase: 'running' };
        answers[1](body);
        answers[0](body);
        await vi.waitFor(() => expect(logsOf('run-d').some(l => l.includes('once'))).toBe(true));
        await new Promise(r => setTimeout(r, 0));
        expect(logsOf('run-d').filter(l => l.includes('once'))).toHaveLength(1);
    });

    it('the backfill commits its lines in one store update', async () => {
        stubBackfill({
            events: [stdout(1, 'a'), stdout(2, 'b'), stdout(3, 'c')],
            status: 'running', phase: 'running',
        });
        let updates = 0;
        const unsubscribe = useCollimateStore.subscribe((next, prev) => {
            if (next.runLogs['run-b'] !== prev.runLogs['run-b']) updates += 1;
        });
        try {
            attach('run-b', 'cell-b');
            await vi.waitFor(() => expect(logsOf('run-b')).toHaveLength(3));
        } finally {
            unsubscribe();
        }
        expect(updates).toBe(1);
        expect(logsOf('run-b').map(l => l.slice(-1))).toEqual(['a', 'b', 'c']);
    });

    it('a failed backfill ends the run with an error', async () => {
        vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })));
        attach('run-e', 'cell-e');
        await vi.waitFor(() => expect(finished('run-e')?.status).toBe('error'));
        expect(logsOf('run-e').at(-1)).toMatch(/Could not load run events/);
    });
});


it('a bounded backfill ignores dropped_logs and rejects queued cursor overlap__F0343', async () => {
    useCollimateStore.setState({ runs: {}, runLogs: {} });
    const event = { id: 100, type: 'stdout', payload: 'tail text', created_at: '2026-10-03T00:00:00Z' };
    stubBackfill({ events: [event], last_event_id: 100, truncated: true, dropped_logs: 42 });
    const off = attach('bounded', 'cell');
    dispatchRunEvents('bounded', [event]);
    await vi.waitFor(() => expect(logsOf('bounded').length).toBe(1));
    dispatchRunEvents('bounded', [{ ...event, id: 99 }, { ...event, id: 101, payload: 'next' }]);
    expect(logsOf('bounded')).toHaveLength(2);
    off();
    vi.unstubAllGlobals();
});
