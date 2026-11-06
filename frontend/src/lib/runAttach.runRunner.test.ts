import { routeApiToFetchStub } from '@/test/apiTransport';
// @vitest-environment jsdom
//
// runRunner: the single client-side run start. No busy guard (several runs
// may share a cell); every pending edit is saved first (editor autosaves,
// then drafts); the wire body is the RunRequest; the answer's record is
// attached with its RUN started bookend and its first surface file is
// revealed, focused; a run with no surface file opens its own drilldown in
// the cell's status pane (the run card is never opened). Run in new cell
// selects the clone and seeds its tabs from the source minus the source's
// run files.
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { runRunner } from './runAttach';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { registerCellSaveBarrier } from '@/lib/cellSaveBarrier';
import { allTabs, createDefaultLayout, openFileTab } from '@/lib/cellLayout';
import type { Channel, RunRecord } from '@/lib/types';

const answer = (over: Record<string, unknown> = {}) => ({
    run_id: 'run-new', status: 'queued', cell_id: 'cell-1', opens: [],
    runner_id: 'synthetic-runner', runner_name: 'Synthetic Runner',
    label: 'synthetic-runner-aaaaaaaa', folder: '_runs/synthetic-runner-aaaaaaaa',
    generator_name: 'synthetic-gen', ...over,
});

type FetchInit = { method?: string; body?: string };

/** Route POST /runs to `post`, events_since to an empty live backfill. */
const stubServer = (post: (init: FetchInit) => unknown) => {
    const fetchSpy = vi.fn(async (url: RequestInfo | URL, init?: FetchInit) => {
        if (String(url).includes('/events_since')) {
            return { ok: true, json: async () => ({ events: [], status: null, phase: 'running', is_active: true, is_terminal: false }) };
        }
        if (String(url).endsWith('/runs') && init?.method === 'POST') return post(init);
        return new Promise(() => { /* the end-of-run state refetch: never answers */ });
    });
    vi.stubGlobal('fetch', fetchSpy);
    return fetchSpy;
};
const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
const postBodies = (spy: ReturnType<typeof stubServer>) => spy.mock.calls
    .filter(([url, init]) => String(url).endsWith('/runs') && (init as FetchInit)?.method === 'POST')
    .map(([, init]) => JSON.parse((init as FetchInit).body!));

const liveRun: RunRecord = {
    runId: 'run-old', cellId: 'cell-1', channel: '11111111-1111-4111-8111-111111111111', runnerId: 'other', runnerName: 'Other',
    generatorName: 'synthetic-gen', label: 'other-bbbbbbbb', folder: '_runs/other-bbbbbbbb',
};

describe('runRunner', () => {
    let restore: () => void;
    beforeEach(() => {
        restore = routeApiToFetchStub();
        localStorage.clear();
        useCollimateStore.setState({
            runs: {}, stoppingRunIds: new Set<string>(),
            runLogs: {}, runPhases: {}, pendingReveals: {}, cellLayouts: {}, cellCache: {},
            pendingSelectCellId: null, activeChannel: { id: '11111111-1111-4111-8111-111111111111', name: 'Shared label' } as Channel, state: null,
        } as never);
    });

    afterEach(() => {
        restore();
        vi.unstubAllGlobals();
    });

    it('posts exactly the run request fields through the actual run transport', async () => {
        const spy = stubServer(() => ok(answer()));
        await runRunner({ runner: 'synthetic-runner', runnerRevision: 'rev-1', cellId: 'cell-1', selectedFile: 'notes.md' });
        expect(postBodies(spy)[0]).toEqual({ runner: 'synthetic-runner', runner_revision: 'rev-1', cell_id: 'cell-1', selected_file: 'notes.md' });
    });

    it('starts a second run on a cell that already has a live one', async () => {
        useCollimateStore.getState().startRun(liveRun);
        const spy = stubServer(() => ok(answer()));
        await runRunner({ runner: 'synthetic-runner', cellId: 'cell-1' });
        const s = useCollimateStore.getState();
        expect(postBodies(spy)).toHaveLength(1);
        expect(Object.hasOwn(s.runs, 'run-old')).toBe(true);
        expect(Object.hasOwn(s.runs, 'run-new')).toBe(true);
    });

    it('two starts in flight on one cell both go through', async () => {
        let n = 0;
        const spy = stubServer(() => ok(answer({ run_id: `run-${++n}`, label: `synthetic-runner-${n}0000000` })));
        await Promise.all([
            runRunner({ runner: 'synthetic-runner', cellId: 'cell-1' }),
            runRunner({ runner: 'synthetic-runner', cellId: 'cell-1' }),
        ]);
        expect(postBodies(spy)).toHaveLength(2);
        expect(Object.keys(useCollimateStore.getState().runs).sort()).toEqual(['run-1', 'run-2']);
    });

    it("serializes the cell's editors before the POST", async () => {
        const order: string[] = [];
        const offEditor = registerCellSaveBarrier('cell-1', () => { order.push('editor'); });
        const offOther = registerCellSaveBarrier('cell-9', () => { order.push('other-editor'); });
        stubServer(() => { order.push('post'); return ok(answer()); });
        try {
            await runRunner({ runner: 'synthetic-runner', cellId: 'cell-1' });
        } finally {
            offEditor(); offOther();
        }
        expect(order).toEqual(['editor', 'post']);
    });

    it('attaches the answered run with its record and its RUN started bookend, and opens its drilldown when it has no surface file', async () => {
        useCollimateStore.getState().setCellLayout('cell-1', createDefaultLayout({ filePaths: [], pinnedPath: null }));
        stubServer(() => ok(answer()));
        await runRunner({ runner: 'synthetic-runner', cellId: 'cell-1' });
        const s = useCollimateStore.getState();
        expect(s.runs['run-new']).toEqual({
            runId: 'run-new', cellId: 'cell-1', channel: '11111111-1111-4111-8111-111111111111', runnerId: 'synthetic-runner',
            runnerName: 'Synthetic Runner', generatorName: 'synthetic-gen',
            label: 'synthetic-runner-aaaaaaaa', folder: '_runs/synthetic-runner-aaaaaaaa',
        });
        expect(s.runLogs['run-new']?.[0]).toMatch(/RUN\s+started · synthetic-runner/);
        // No surface file: nothing is revealed (never the run card); the
        // cell's status pane opens on the run's drilldown.
        expect(s.pendingReveals['cell-1']).toBeUndefined();
        expect(s.runsPaneView['cell-1']).toEqual({ view: 'run', runId: 'run-new', option: 'default' });
        // In place: the camera stays where the user is.
        expect(s.pendingSelectCellId).toBeNull();
    });

    it('sends the selected file and reveals the first surface the host opened, focused (GN-01, GN-03)', async () => {
        const opens = ['_runs/synthetic-runner-aaaaaaaa/chat/conversation.chat', 'status.chat'];
        const spy = stubServer(() => ok(answer({ opens })));
        await runRunner({ runner: 'synthetic-runner', cellId: 'cell-1', selectedFile: 'docs/brief.md' });
        expect(postBodies(spy)[0]).toEqual({ runner: 'synthetic-runner', cell_id: 'cell-1', selected_file: 'docs/brief.md' });
        expect(useCollimateStore.getState().pendingReveals['cell-1']).toEqual([expect.objectContaining({
            path: opens[0], runId: 'run-new', activate: true,
        })]);
    });

    it('Run in new cell selects the clone and seeds its tabs minus the source run files', async () => {
        let layout = createDefaultLayout({ filePaths: [], pinnedPath: null });
        layout = openFileTab(layout, 'notes.md', null);
        layout = openFileTab(layout, '_runs/other-bbbbbbbb/chat.chat', null, undefined, { newTab: true });
        useCollimateStore.getState().setCellLayout('cell-1', layout);
        const spy = stubServer(() => ok(answer({ cell_id: 'clone-1', stack_id: 'stack-clone' })));
        await runRunner({ runner: 'synthetic-runner', cellId: 'cell-1', newCell: true });
        expect(postBodies(spy)[0]).toEqual({ runner: 'synthetic-runner', cell_id: 'cell-1', new_cell: true });
        const s = useCollimateStore.getState();
        expect(s.pendingSelectCellId).toBe('clone-1');
        expect(s.runs['run-new'].cellId).toBe('clone-1');
        expect(allTabs(s.cellLayouts['clone-1']).map(t => (t.kind === 'file' ? t.path : t.kind))).toEqual(['notes.md']);
        // No surface file: the clone's status pane opens on the run, no tab.
        expect(s.pendingReveals['clone-1']).toBeUndefined();
        expect(s.runsPaneView['clone-1']).toEqual({ view: 'run', runId: 'run-new', option: 'default' });
        // The source keeps its own tabs.
        expect(allTabs(s.cellLayouts['cell-1'])).toHaveLength(2);
    });

    it('a new cell run already attached by its broadcast is not attached twice', async () => {
        const spy = stubServer(() => {
            useCollimateStore.getState().startRun({
                runId: 'run-new', cellId: 'clone-1', channel: '11111111-1111-4111-8111-111111111111', runnerId: 'synthetic-runner',
                runnerName: 'Synthetic Runner', generatorName: 'synthetic-gen',
                label: 'synthetic-runner-aaaaaaaa', folder: '_runs/synthetic-runner-aaaaaaaa',
            });
            return ok(answer({ cell_id: 'clone-1' }));
        });
        await runRunner({ runner: 'synthetic-runner', cellId: 'cell-1', newCell: true });
        const backfills = spy.mock.calls.filter(([url]) => String(url).includes('/events_since'));
        expect(backfills).toHaveLength(0);
        expect(useCollimateStore.getState().runLogs['run-new']).toBeUndefined();
        expect(useCollimateStore.getState().pendingReveals['clone-1']).toBeUndefined();
        expect(useCollimateStore.getState().runsPaneView['clone-1']).toEqual({ view: 'run', runId: 'run-new', option: 'default' });
    });

    it('sends the runner identity, revision and cell only', async () => {
        const spy = stubServer(() => ({ ok: false, status: 400, json: async () => ({ detail: 'fixture rejection' }) }));
        await expect(runRunner({ runner: 'saved-runner', runnerRevision: 'revision-1', cellId: 'native-cell' }))
            .rejects.toThrow('fixture rejection');
        await expect(runRunner({ runner: 'saved-runner', cellId: 'cell-p' }))
            .rejects.toThrow('fixture rejection');
        expect(postBodies(spy)).toEqual([
            { runner: 'saved-runner', runner_revision: 'revision-1', cell_id: 'native-cell' },
            { runner: 'saved-runner', cell_id: 'cell-p' },
        ]);
    });

    it('surfaces the missing-required listing and a transport failure as sentences', async () => {
        stubServer(() => ({ ok: false, status: 422, json: async () => ({
            code: 'missing_required', fields: [{ label: 'Prompt' }, { key: 'model' }] }) }));
        await expect(runRunner({ runner: 'r', cellId: 'c' }))
            .rejects.toThrow('Cannot run — required fields are empty: Prompt, model');
        stubServer(() => ({ ok: false, status: 502, json: async () => { throw new Error('html'); } }));
        await expect(runRunner({ runner: 'r', cellId: 'c' })).rejects.toThrow(/Couldn't reach the server/);
        expect(Object.keys(useCollimateStore.getState().runs).length).toBe(0);
    });
    it('captures the exact UUID before the save barrier across rename and same-label switch', async () => {
        let release!: () => void;
        const off = registerCellSaveBarrier('cell-1', () => new Promise<void>(resolve => { release = resolve; }));
        const spy = stubServer(() => ok(answer()));
        try {
            const pending = runRunner({ runner: 'synthetic-runner', cellId: 'cell-1' });
            await Promise.resolve();
            useCollimateStore.setState({ activeChannel: { id: '11111111-1111-4111-8111-111111111111', name: 'Renamed' } as Channel });
            useCollimateStore.setState({ activeChannel: { id: '22222222-2222-4222-8222-222222222222', name: 'Shared label' } as Channel });
            release();
            await pending;
            const post = spy.mock.calls.find(([url, init]) => String(url).endsWith('/runs') && init?.method === 'POST');
            expect(String(post![0])).toContain('/channels/11111111-1111-4111-8111-111111111111/runs');
            expect(useCollimateStore.getState().runs['run-new'].channel).toBe('11111111-1111-4111-8111-111111111111');
        } finally { off(); }
    });


    it('a broadcast start with no surface file opens the drilldown only where the user is looking, never the run card', async () => {
        const { attachStartedRun } = await import('./runAttach');
        useCollimateStore.getState().setCellLayout('cell-1', createDefaultLayout({ filePaths: [], pinnedPath: null }));
        useCollimateStore.setState({ selectedCellId: 'cell-1' } as never);
        stubServer(() => ok(answer()));
        const info = { run_id: 'run-b', cell_id: 'cell-1', runner_id: 'synthetic-runner', runner_name: 'Synthetic Runner',
            label: 'synthetic-runner-bbbbbbbb', folder: '_runs/synthetic-runner-bbbbbbbb', generator_name: 'synthetic-gen', opens: [] as string[] };
        attachStartedRun(info, '11111111-1111-4111-8111-111111111111');
        let s = useCollimateStore.getState();
        expect(s.pendingReveals['cell-1']).toBeUndefined();
        expect(s.runsPaneView['cell-1']).toEqual({ view: 'run', runId: 'run-b', option: 'default' });
        // Another cell's run, or one with a surface file: the pane is left alone.
        useCollimateStore.setState({ selectedCellId: 'cell-9', runsPaneView: {} } as never);
        attachStartedRun({ ...info, run_id: 'run-c', label: 'synthetic-runner-cccccccc', folder: '_runs/synthetic-runner-cccccccc' }, '11111111-1111-4111-8111-111111111111');
        s = useCollimateStore.getState();
        expect(s.runsPaneView['cell-1']).toBeUndefined();
        expect(s.pendingReveals['cell-1']).toBeUndefined();
        attachStartedRun({ ...info, run_id: 'run-d', label: 'synthetic-runner-dddddddd', folder: '_runs/synthetic-runner-dddddddd',
            opens: ['_runs/synthetic-runner-dddddddd/chat/conversation.chat'] }, '11111111-1111-4111-8111-111111111111');
        expect(useCollimateStore.getState().pendingReveals['cell-1']?.[0]).toMatchObject({ path: '_runs/synthetic-runner-dddddddd/chat/conversation.chat', runId: 'run-d', activate: false });
    });
});
