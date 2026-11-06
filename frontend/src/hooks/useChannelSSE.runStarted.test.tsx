import { routeApiToFetchStub } from '@/test/apiTransport';
// @vitest-environment jsdom
//
// The channel SSE's run routing for runs this client did not start itself:
// `run_started` attaches the run (a run another window started) and opens
// its run card, unless this client is already attached or is starting a
// run on that cell (then it waits for that POST); `reveal_file` queues a
// layout-only reveal; and every (re)connect recovers the channel's active
// runs, so a run whose run_started the stream missed is attached anyway.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    fetchActiveRuns: vi.fn(),
    fetchActiveRunsEverywhere: vi.fn(async () => []),
    // The overflow resync's state fetch: no server here, so it never answers.
    fetchState: vi.fn(() => new Promise(() => {})),
}));

import { fetchActiveRuns, type ActiveRunInfo } from '@/lib/api';
import { useChannelSSE } from './useChannelSSE';
import { runRunner } from '../lib/runAttach';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { createDefaultLayout } from '@/lib/cellLayout';

class FakeEventSource {
    static last: FakeEventSource | null = null;
    readyState = 0;
    onopen: (() => void) | null = null;
    onmessage: ((e: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    constructor(public url: string) { FakeEventSource.last = this; }
    close() { /* nothing to release */ }
    emit(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
}

const label = (runId: string) => `synthetic-runner-${runId}`;
const started = (runId: string, cellId: string) => ({
    type: 'run_started', run_id: runId, cell_id: cellId,
    runner_id: 'synthetic-runner', runner_name: 'Synthetic Runner',
    label: label(runId), folder: `_runs/${label(runId)}`, generator_name: 'synthetic-gen',
});
const active = (runId: string, cellId: string, over: Partial<ActiveRunInfo> = {}): ActiveRunInfo => ({
    run_id: runId, cell_id: cellId, cell_name: '', runner_id: 'synthetic-runner', runner_name: 'Synthetic Runner',
    label: label(runId), folder: `_runs/${label(runId)}`, generator_name: 'synthetic-gen', started_at: null, ...over,
});

const logsOf = (runId: string) => useCollimateStore.getState().runLogs[runId] ?? [];

describe('useChannelSSE — runs this client did not start', () => {
    let restore: () => void;
    beforeEach(() => {
        restore = routeApiToFetchStub();
        localStorage.clear();
        useCollimateStore.setState({
            runs: {},
            stoppingRunIds: new Set<string>(),
            runPhases: {},
            runLogs: {},
            cellCache: {},
            cellLayouts: {},
            pendingReveals: {},
            endedRuns: {},
            lastFinishedByCell: {},
            selectedCellId: null,
            activeChannel: null,
            hasInitialized: true,
        });
        vi.stubGlobal('EventSource', FakeEventSource);
        // attachToRun's backfill: a live run with no events yet.
        vi.stubGlobal('fetch', vi.fn(async (url: string) => {
            if (String(url).includes('/events_since')) {
                return {
                    ok: true,
                    json: async () => ({
                        events: [], status: null, phase: 'spinning_up',
                        is_active: true, is_terminal: false,
                    }),
                };
            }
            return new Promise(() => { /* a run start that never answers */ });
        }));
    });

    afterEach(() => {
        restore();
        vi.unstubAllGlobals();
        vi.mocked(fetchActiveRuns).mockReset();
    });

    it('reconnect cleans ended worker scopes using registrations captured before the live-run snapshot P4F06', async () => {
        const order: string[] = [];
        const paths = ['/proxy/ch/ended/5100/', '/proxy/ch/live/5100/', '/proxy/elsewhere/ended/5100/', '/'];
        const registrations = paths.map(path => ({scope: new URL(path, location.origin).href, unregister: vi.fn(async () => true)}));
        vi.stubGlobal('navigator', {serviceWorker: {getRegistrations: vi.fn(async () => {order.push('workers');return [...registrations];})}});
        vi.mocked(fetchActiveRuns).mockImplementation(async () => {
            order.push('runs');
            registrations.push({scope: new URL('/proxy/ch/new/5100/', location.origin).href, unregister: vi.fn(async () => true)});
            return [active('live', 'cell-live')];
        });
        const view = renderHook(() => useChannelSSE('ch'));
        try {
            await act(async () => FakeEventSource.last!.onopen?.());
            expect(order).toEqual(['workers', 'runs']);
            expect(registrations.map(registration => registration.unregister.mock.calls.length)).toEqual([1, 0, 0, 0, 0]);
        } finally { view.unmount(); }
    });

    it('a reconnect attaches a run whose run_started the dropped stream missed', async () => {
        vi.mocked(fetchActiveRuns).mockResolvedValue([active('run-5', 'cell-e', { phase: 'awaiting_input', phase_message: '' })]);
        renderHook(() => useChannelSSE('ch'));
        const stream = FakeEventSource.last!;
        stream.onerror?.();
        stream.onopen?.();
        await vi.waitFor(() =>
            expect(Object.hasOwn(useCollimateStore.getState().runs, 'run-5')).toBe(true));
        const s = useCollimateStore.getState();
        expect(fetchActiveRuns).toHaveBeenCalledWith('ch');
        expect(s.runs['run-5']).toMatchObject({ cellId: 'cell-e', label: label('run-5'), channel: 'ch' });
        // A recovered run's tabs are whatever the user left open: no card.
        expect(s.pendingReveals).toEqual({});
        await vi.waitFor(() =>
            expect(useCollimateStore.getState().runPhases['run-5']?.phase).toBeDefined());
        // A later reconnect re-seeds but does not attach the run again.
        const backfills = () => vi.mocked(fetch).mock.calls
            .filter(([url]) => String(url).includes('/run-5/events_since')).length;
        expect(backfills()).toBe(1);
        stream.onopen?.();
        await vi.waitFor(() => expect(fetchActiveRuns).toHaveBeenCalledTimes(2));
        await Promise.resolve();
        expect(backfills()).toBe(1);
    });

    it("the first connect re-seeds an attached run's record and phase, but never a terminal phase", async () => {
        // Recovery re-seeds what a disconnected stream (or a reload) may have
        // missed for every active run, even for a run already attached.
        useCollimateStore.setState({
            runs: {
                'run-1': {
                    runId: 'run-1', cellId: 'cell-1', channel: 'ch', runnerId: 'synthetic-runner',
                    runnerName: 'Synthetic Runner (old)', generatorName: 'synthetic-gen',
                    label: label('run-1'), folder: `_runs/${label('run-1')}`,
                },
            },
            runPhases: { 'run-1': { phase: 'running', message: '' } },
        });
        useCollimateStore.getState().setRun({ ...useCollimateStore.getState().runs['run-1'], runId: 'run-2', cellId: 'cell-2', label: label('run-2'), folder: `_runs/${label('run-2')}` });
        vi.mocked(fetchActiveRuns).mockResolvedValue([
            active('run-1', 'cell-1', { phase: 'awaiting_input', phase_message: 'session open' }),
            active('run-2', 'cell-2', { phase: 'completed', phase_message: '' }),
        ]);
        renderHook(() => useChannelSSE('ch'));
        FakeEventSource.last!.onopen?.();
        await vi.waitFor(() =>
            expect(useCollimateStore.getState().runPhases['run-1']).toEqual(
                { phase: 'awaiting_input', message: 'session open' }));
        const s = useCollimateStore.getState();
        expect(s.runPhases['run-2']).toBeUndefined();
        expect(s.runs['run-1'].runnerName).toBe('Synthetic Runner');
        expect(s.runs['run-2']).toMatchObject({ cellId: 'cell-2', folder: `_runs/${label('run-2')}` });
    });

    it('a run whose end is under way is never attached as live (F0321)', async () => {
        // The server keeps a stopped (or finished) run listed until its
        // run-end cleanup is done. One still stopping is attached as
        // stopping; one whose status is final has sent its run_status, so
        // attaching it would leave it live forever: it is skipped.
        vi.mocked(fetchActiveRuns).mockResolvedValue([
            active('run-6', 'cell-6', { phase: 'running', stopping: true }),
            active('run-7', 'cell-7', { phase: 'stopped', stopping: true }),
            active('run-9', 'cell-9', { phase: 'running' }),
        ]);
        renderHook(() => useChannelSSE('ch'));
        FakeEventSource.last!.onopen?.();
        await vi.waitFor(() =>
            expect(Object.hasOwn(useCollimateStore.getState().runs, 'run-9')).toBe(true));
        const s = useCollimateStore.getState();
        expect(Object.hasOwn(s.runs, 'run-6')).toBe(true);
        expect(s.stoppingRunIds.has('run-6')).toBe(true);
        expect(Object.hasOwn(s.runs, 'run-7')).toBe(false);
        expect(s.runs['run-7']).toBeUndefined();
        expect(s.stoppingRunIds.has('run-7')).toBe(false);
        expect(s.stoppingRunIds.has('run-9')).toBe(false);
    });

    it('a followed run whose final run_status the stream dropped is ended on reconnect (F0321)', async () => {
        // The run finished while the stream was down; the server still lists
        // it (its run-end cleanup is under way) with its final status as the
        // phase. The reconnect ends it with that status instead of leaving
        // it live or wedged on Stopping.
        vi.mocked(fetchActiveRuns).mockResolvedValue([
            active('run-8', 'cell-8', { phase: 'running' }),
        ]);
        renderHook(() => useChannelSSE('ch'));
        const stream = FakeEventSource.last!;
        stream.onopen?.();
        await vi.waitFor(() =>
            expect(Object.hasOwn(useCollimateStore.getState().runs, 'run-8')).toBe(true));
        vi.mocked(fetchActiveRuns).mockResolvedValue([
            active('run-8', 'cell-8', { phase: 'completed', stopping: true }),
        ]);
        stream.onerror?.();
        stream.onopen?.();
        await vi.waitFor(() =>
            expect(Object.hasOwn(useCollimateStore.getState().runs, 'run-8')).toBe(false));
        const s = useCollimateStore.getState();
        expect(s.stoppingRunIds.has('run-8')).toBe(false);
        expect(s.endedRuns['run-8']?.status).toBe('completed');
    });

    it('a stream the browser gave up on (CLOSED) is rebuilt after a backoff; a retrying one is not', () => {
        vi.useFakeTimers();
        try {
            vi.mocked(fetchActiveRuns).mockResolvedValue([]);
            renderHook(() => useChannelSSE('ch'));
            const first = FakeEventSource.last!;
            // CONNECTING: the browser retries on its own.
            first.onerror?.();
            act(() => { vi.advanceTimersByTime(60000); });
            expect(FakeEventSource.last).toBe(first);
            first.readyState = 2;
            first.onerror?.();
            act(() => { vi.advanceTimersByTime(999); });
            expect(FakeEventSource.last).toBe(first);
            act(() => { vi.advanceTimersByTime(1); });
            expect(FakeEventSource.last).not.toBe(first);
        } finally {
            vi.useRealTimers();
        }
    });

    it('recovery waits for the store to load; overflow recovers too', async () => {
        vi.mocked(fetchActiveRuns).mockResolvedValue([]);
        useCollimateStore.setState({ hasInitialized: false });
        renderHook(() => useChannelSSE('ch'));
        FakeEventSource.last!.onopen?.();
        expect(fetchActiveRuns).not.toHaveBeenCalled();
        useCollimateStore.setState({ hasInitialized: true });
        FakeEventSource.last!.emit({ type: 'overflow' });
        await vi.waitFor(() => expect(fetchActiveRuns).toHaveBeenCalledWith('ch'));
    });

    it('run_started attaches the run with its bookend; with no surface file and the user elsewhere it opens nothing', async () => {
        renderHook(() => useChannelSSE('ch'));
        FakeEventSource.last!.emit(started('run-2', 'cell-a'));
        const s = useCollimateStore.getState();
        expect(Object.hasOwn(s.runs, 'run-2')).toBe(true);
        expect(s.runs['run-2']).toMatchObject({ cellId: 'cell-a', runnerName: 'Synthetic Runner', folder: `_runs/${label('run-2')}` });
        expect(logsOf('run-2')[0]).toMatch(/RUN\s+started · synthetic-runner/);
        // The run card is never opened; the pane of an unselected cell is left alone.
        expect(s.pendingReveals['cell-a']).toBeUndefined();
        expect(s.runsPaneView['cell-a']).toBeUndefined();
        await vi.waitFor(() =>
            expect(useCollimateStore.getState().runPhases['run-2']?.phase).toBe('spinning_up'));
    });

    it('run_started reveals the first surface the host created instead of the card (GN-03)', () => {
        useCollimateStore.setState({ selectedCellId: 'cell-o' });
        renderHook(() => useChannelSSE('ch'));
        const opens = [`_runs/${label('run-o')}/chat/conversation.chat`, 'status.chat'];
        FakeEventSource.last!.emit({ ...started('run-o', 'cell-o'), opens });
        expect(useCollimateStore.getState().pendingReveals['cell-o']).toEqual([expect.objectContaining({
            path: opens[0], runId: 'run-o', activate: true,
        })]);
        // No surface, another cell: nothing opens, never the run card.
        FakeEventSource.last!.emit({ ...started('run-p', 'cell-p'), opens: [] });
        expect(useCollimateStore.getState().pendingReveals['cell-p']).toBeUndefined();
        expect(useCollimateStore.getState().runsPaneView['cell-p']).toBeUndefined();
    });

    it("run_started on the selected cell with nothing running opens the run's drilldown; a sibling run leaves the pane alone", () => {
        useCollimateStore.setState({ selectedCellId: 'cell-s', activeChannel: { id: 'ch', name: 'ch' } } as never);
        useCollimateStore.getState().setCellLayout('cell-s', createDefaultLayout({ filePaths: [], pinnedPath: null }));
        renderHook(() => useChannelSSE('ch'));
        FakeEventSource.last!.emit(started('run-6', 'cell-s'));
        expect(useCollimateStore.getState().pendingReveals['cell-s']).toBeUndefined();
        expect(useCollimateStore.getState().runsPaneView['cell-s']).toEqual({ view: 'run', runId: 'run-6', option: 'default' });
        FakeEventSource.last!.emit(started('run-7', 'cell-s'));
        expect(useCollimateStore.getState().runsPaneView['cell-s']).toEqual({ view: 'run', runId: 'run-6', option: 'default' });
        expect(useCollimateStore.getState().pendingReveals['cell-s']).toBeUndefined();
    });

    it('run_started for a run already attached changes nothing', () => {
        useCollimateStore.getState().setRun({runId:'run-3',cellId:'cell-b',channel:'ch',runnerId:'synthetic-runner',runnerName:'Synthetic Runner',generatorName:'synthetic-gen',label:label('run-3'),folder:`_runs/${label('run-3')}`});
        renderHook(() => useChannelSSE('ch'));
        FakeEventSource.last!.emit(started('run-3', 'cell-b'));
        expect(logsOf('run-3')).toEqual([]);
        expect(useCollimateStore.getState().pendingReveals).toEqual({});
        expect(fetch).not.toHaveBeenCalled();
    });

    it("run_started on a cell this client is POSTing a start for waits for that POST", async () => {
        // The broadcast may be this POST's own run or another start: either
        // way it is attached once the POST settles — not dropped.
        let answer: (value: unknown) => void = () => {};
        const events = vi.mocked(fetch).getMockImplementation()!;
        vi.stubGlobal('fetch', vi.fn((url: string, init?: { method?: string }) =>
            String(url).endsWith('/runs') && init?.method === 'POST'
                ? new Promise(resolve => { answer = resolve; })
                : events(url)));
        const pending = runRunner({ runner: 'synthetic-runner', cellId: 'cell-c' });
        await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
        renderHook(() => useChannelSSE('ch'));
        FakeEventSource.last!.emit(started('run-4', 'cell-c'));
        expect(Object.hasOwn(useCollimateStore.getState().runs, 'run-4')).toBe(false);
        answer({ ok: false, status: 409, json: async () => ({ detail: 'fixture refusal' }) });
        await expect(pending).rejects.toThrow(/fixture refusal/);
        expect(Object.hasOwn(useCollimateStore.getState().runs, 'run-4')).toBe(true);
        expect(useCollimateStore.getState().runs['run-4'].cellId).toBe('cell-c');
    });

    it('reveal_file queues a reveal for the run', () => {
        renderHook(() => useChannelSSE('ch'));
        FakeEventSource.last!.emit({
            type: 'reveal_file', run_id: 'run-8', cell_id: 'cell-r', path: '_runs/x-88888888/tab-2.webview',
        });
        expect(useCollimateStore.getState().pendingReveals['cell-r']).toEqual([expect.objectContaining({
            path: '_runs/x-88888888/tab-2.webview', runId: 'run-8',
        })]);
        expect(useCollimateStore.getState().pendingReveals['cell-r'][0].activate).toBeUndefined();
    });
});
