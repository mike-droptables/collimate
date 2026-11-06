// The status pane's session state: contexts follow the doorbell and the
// stream's connects, drafts send (and retry under one operation id), the
// queue reorders optimistically, finished runs cap and clear, the pane opens
// now or once its cell is reachable, and Add annotation lands its answer.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
    fetchCellAnnotations: vi.fn(), fetchAnnotationContext: vi.fn(), postAnnotation: vi.fn(), patchAnnotation: vi.fn(),
    deleteAnnotation: vi.fn(), sendAnnotationToRun: vi.fn(), orderAnnotations: vi.fn(), setAnnotationBar: vi.fn(),
    interruptAnnotations: vi.fn(), fetchFileTouches: vi.fn(), fetchRunInbox: vi.fn(),
}));
vi.mock('@/lib/api/annotations', async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));
const renderer = vi.hoisted(() => ({ requestAnnotation: vi.fn() }));
vi.mock('@/lib/rendererAnnotation', async (importOriginal) => ({ ...(await importOriginal<object>()), ...renderer }));

import { useCollimateStore } from '@/stores/useCollimateStore';
import { MAX_CONTEXT_ITEMS } from '@/lib/annotations';
import { FINISHED_RUNS_PER_CELL, STATUS_VIEW, listeningOf } from './annotationState';
import { createDefaultLayout } from '@/lib/cellLayout';
import type { AnnotationContext, AnnotationMessage, FileTouch } from '@/lib/annotations';
import type { FinishedRun } from './types';

const store = () => useCollimateStore.getState();
const msg = (id: string, over: Partial<AnnotationMessage> = {}): AnnotationMessage => ({
    id, context_id: 'run-1', parent_id: null, author: 'user', author_label: null, kind: 'message', body: id,
    context_items: [], refs: [], mode: 'queued', position: 1, held: false, revision: 1,
    created_at: '2026-10-24 10:00:00', edited_at: null, read_at: null, ...over,
});
const ctx = (id: string, rev: number, messages: AnnotationMessage[] = [], over: Partial<AnnotationContext> = {}): AnnotationContext => ({
    id, cell_id: 'c1', run_id: id === 'cell' ? null : id, label: id === 'cell' ? null : `agent-${id}`, bar_on: false,
    listening: true, interrupt_seq: 0, rev, sealed: false, messages, ...over,
});
const refusal = (code: string) => Object.assign(new Error(code), { response: { status: 409, data: { error: code, detail: `${code} words` } } });
const finished = (runId: string, over: Partial<FinishedRun> = {}): FinishedRun => ({
    runId, cellId: 'c1', channel: 'ch', runnerId: 'agent', runnerName: 'Agent', generatorName: 'agent',
    label: `agent-${runId}`, folder: `_runs/agent-${runId}`, status: 'completed', endedAt: 1, ...over,
});
const deferred = <T,>() => {
    let resolve!: (v: T) => void;
    const promise = new Promise<T>(r => { resolve = r; });
    return { promise, resolve };
};

const CHANNEL = { id: 'ch', name: 'ch', main_stacks: [{ id: 's', name: '', cells: [{ id: 'c1', name: 'C', files: [{ name: 'page.html' }] }] }], nav_stacks: [] };

beforeEach(() => {
    for (const fn of [...Object.values(api), ...Object.values(renderer)]) fn.mockReset();
    useCollimateStore.setState({
        activeChannel: CHANNEL, state: { channels: [CHANNEL] }, cellCache: {}, cellLayouts: {}, cellDisplaysReady: true,
        annotationContexts: {}, annotationNotice: {}, fileTouches: {}, lastFileTab: {}, finishedRuns: {}, runsPaneView: {},
        runsPaneFocus: {}, pendingRunsPaneOpen: null, composerDrafts: {}, annotationRequests: {}, activeRunsEverywhere: [],
    } as never);
});
afterEach(() => { vi.useRealTimers(); });

describe('contexts', () => {
    it('loadCellAnnotations replaces the answered contexts, keeps a finished run\'s, drops the rest, and shares one request', async () => {
        useCollimateStore.setState({
            annotationContexts: { c1: { 'run-old': ctx('run-old', 5), 'run-gone': ctx('run-gone', 2), 'run-1': ctx('run-1', 9) } },
            finishedRuns: { c1: [finished('run-old')] },
        });
        const answer = deferred<AnnotationContext[]>();
        api.fetchCellAnnotations.mockReturnValue(answer.promise);
        const a = store().loadCellAnnotations('c1');
        const b = store().loadCellAnnotations('c1');
        expect(api.fetchCellAnnotations).toHaveBeenCalledTimes(1);
        expect(api.fetchCellAnnotations).toHaveBeenCalledWith('ch', 'c1');
        answer.resolve([ctx('cell', 0), ctx('run-1', 3), ctx('run-2', 1)]);
        await Promise.all([a, b]);
        const held = store().annotationContexts.c1;
        expect(Object.keys(held).sort()).toEqual(['cell', 'run-1', 'run-2', 'run-old']);
        // A newer context held over an older answer stays.
        expect(held['run-1'].rev).toBe(9);
    });

    it('loadCellAnnotations warns and keeps the contexts on a failure; nothing without a channel', async () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
        api.fetchCellAnnotations.mockRejectedValue(new Error('down'));
        await store().loadCellAnnotations('c1');
        expect(warn).toHaveBeenCalled();
        useCollimateStore.setState({ activeChannel: null });
        await store().loadCellAnnotations('c1');
        await store().refreshAnnotationContext('c1', 'cell');
        expect(api.fetchCellAnnotations).toHaveBeenCalledTimes(1);
        expect(api.fetchAnnotationContext).not.toHaveBeenCalled();
        warn.mockRestore();
    });

    it('refreshAnnotationContext ignores an older rev, deletes on null, and coalesces calls in flight into one more fetch', async () => {
        useCollimateStore.setState({ annotationContexts: { c1: { 'run-1': ctx('run-1', 5) } } });
        api.fetchAnnotationContext.mockResolvedValueOnce(ctx('run-1', 4, [msg('old')]));
        await store().refreshAnnotationContext('c1', 'run-1');
        expect(store().annotationContexts.c1['run-1'].messages).toEqual([]);

        const first = deferred<AnnotationContext | null>();
        api.fetchAnnotationContext.mockReset().mockReturnValueOnce(first.promise).mockResolvedValueOnce(ctx('run-1', 8));
        const p = store().refreshAnnotationContext('c1', 'run-1');
        void store().refreshAnnotationContext('c1', 'run-1');
        void store().refreshAnnotationContext('c1', 'run-1');
        first.resolve(ctx('run-1', 7));
        await p;
        expect(api.fetchAnnotationContext).toHaveBeenCalledTimes(2);
        expect(store().annotationContexts.c1['run-1'].rev).toBe(8);

        api.fetchAnnotationContext.mockReset().mockResolvedValueOnce(null);
        await store().refreshAnnotationContext('c1', 'run-1');
        expect(store().annotationContexts.c1['run-1']).toBeUndefined();
    });

    it('refreshAnnotationContext warns on a failure', async () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
        api.fetchAnnotationContext.mockRejectedValue(new Error('down'));
        await store().refreshAnnotationContext('c1', 'run-1');
        expect(warn).toHaveBeenCalled();
        warn.mockRestore();
    });

    it('annotationsChanged refetches only a viewed cell\'s context, and only when the rev moved on', () => {
        api.fetchAnnotationContext.mockResolvedValue(null);
        store().annotationsChanged('c1', 'run-1', 3);
        expect(api.fetchAnnotationContext).not.toHaveBeenCalled();
        useCollimateStore.setState({ annotationContexts: { c1: { 'run-1': ctx('run-1', 3) } } });
        store().annotationsChanged('c1', 'run-1', 3);
        expect(api.fetchAnnotationContext).not.toHaveBeenCalled();
        store().annotationsChanged('c1', 'run-1', 4);
        store().annotationsChanged('c1', 'run-new', 1);
        expect(api.fetchAnnotationContext.mock.calls.map(c => c[2])).toEqual(['run-1', 'run-new']);
    });

    it('resyncAnnotations reloads only the viewed cells of the active channel', () => {
        api.fetchCellAnnotations.mockResolvedValue([]);
        useCollimateStore.setState({ annotationContexts: { c1: {}, elsewhere: {} } });
        store().resyncAnnotations();
        expect(api.fetchCellAnnotations.mock.calls).toEqual([['ch', 'c1']]);
        useCollimateStore.setState({ activeChannel: null });
        store().resyncAnnotations();
        expect(api.fetchCellAnnotations).toHaveBeenCalledTimes(1);
    });

    it('listeningOf reads the context, else the live row, else null', () => {
        const s = { annotationContexts: { c1: { r: ctx('r', 1, [], { listening: false }) } },
            activeRunsEverywhere: [{ channel: 'ch', channel_name: 'ch', runs: [{ run_id: 'q', listening: true }, { run_id: 'z' }] }] } as never;
        expect(listeningOf(s, 'c1', 'r')).toBe(false);
        expect(listeningOf(s, 'c1', 'q')).toBe(true);
        expect(listeningOf(s, 'c1', 'z')).toBeNull();
        expect(listeningOf(s, 'c1', 'nope')).toBeNull();
    });
});

describe('sendDraft', () => {
    beforeEach(() => {
        useCollimateStore.setState({ annotationContexts: { c1: { 'run-1': ctx('run-1', 1), cell: ctx('cell', 0) } } });
    });

    it('posts with the draft\'s operation id, held only with the bar on, below and queued; success upserts and empties the draft', async () => {
        store().setComposerDraft('k', { below: true, contextItems: [{ path: 'page.html', renderer_id: 'html', label: 'L', text: 'T', anchor: null }] });
        const op = store().composerDrafts.k.operationId;
        api.postAnnotation.mockResolvedValue(msg('a1', { body: 'hello' }));
        expect(await store().sendDraft('c1', 'k', 'run-1', 'hello')).toBe(true);
        expect(api.postAnnotation).toHaveBeenCalledWith('ch', 'c1', {
            operation_id: op, context_id: 'run-1', body: 'hello', mode: 'queued',
            context_items: [{ path: 'page.html', renderer_id: 'html', label: 'L', text: 'T', anchor: null }],
        });
        expect(store().annotationContexts.c1['run-1'].messages.map(m => m.id)).toEqual(['a1']);
        const after = store().composerDrafts.k;
        expect(after).toMatchObject({ text: '', contextItems: [], below: true, generation: 0, sending: false, notice: null });
        expect(after.operationId).not.toBe(op);

        useCollimateStore.setState({ annotationContexts: { c1: { 'run-1': ctx('run-1', 2, [], { bar_on: true }) } } });
        api.postAnnotation.mockResolvedValue(msg('a2'));
        await store().sendDraft('c1', 'k', 'run-1', 'two');
        expect(api.postAnnotation.mock.calls[1][2]).toMatchObject({ held: true });
        store().setComposerDraft('k', { mode: 'interrupt' });
        await store().sendDraft('c1', 'k', 'run-1', 'three');
        expect(api.postAnnotation.mock.calls[2][2]).not.toHaveProperty('held');
    });

    it('a reply posts to the replied message\'s context with its parent, never held', async () => {
        useCollimateStore.setState({ annotationContexts: { c1: { 'run-1': ctx('run-1', 2, [msg('root')], { bar_on: true }) } } });
        store().setComposerDraft('k', { below: true });
        store().setDraftReply('k', msg('root', { author: 'run', author_label: 'agent-1', body: 'a  long\n reply to  quote' }));
        expect(store().composerDrafts.k.replyTo).toEqual({ id: 'root', contextId: 'run-1', author: 'agent-1', excerpt: 'a long reply to quote' });
        api.postAnnotation.mockResolvedValue(msg('r1', { parent_id: 'root' }));
        await store().sendDraft('c1', 'k', 'cell', 'yes');
        expect(api.postAnnotation.mock.calls[0][2]).toMatchObject({ context_id: 'run-1', parent_id: 'root' });
        expect(api.postAnnotation.mock.calls[0][2]).not.toHaveProperty('held');
        store().setDraftReply('k', null);
        expect(store().composerDrafts.k.replyTo).toBeNull();
    });

    it('a failure keeps the draft and its operation id, says why, and refetches a context that changed', async () => {
        store().setComposerDraft('k', { text: 'kept' });
        const op = store().composerDrafts.k.operationId;
        api.postAnnotation.mockRejectedValueOnce(refusal('sealed'));
        api.fetchAnnotationContext.mockResolvedValue(ctx('run-1', 2, [], { sealed: true }));
        expect(await store().sendDraft('c1', 'k', 'run-1', 'kept')).toBe(false);
        expect(store().composerDrafts.k).toMatchObject({ text: 'kept', operationId: op, sending: false, notice: 'This run has ended; your message was not sent.' });
        await vi.waitFor(() => expect(api.fetchAnnotationContext).toHaveBeenCalledWith('ch', 'c1', 'run-1'));
        api.postAnnotation.mockRejectedValueOnce(new Error('Network Error'));
        await store().sendDraft('c1', 'k', 'run-1', 'kept');
        expect(api.postAnnotation.mock.calls[1][2].operation_id).toBe(op);
        expect(store().composerDrafts.k.notice).toBe('Network Error');
        expect(api.fetchAnnotationContext).toHaveBeenCalledTimes(1);
    });

    it('an edit patches with its expected revision; an unheld context is refetched instead', async () => {
        store().loadDraftForEdit('k', msg('m1', { body: 'old', revision: 3, mode: 'interrupt', context_id: 'run-9' }));
        expect(store().composerDrafts.k).toMatchObject({ text: 'old', mode: 'interrupt', editing: { id: 'm1', contextId: 'run-9', revision: 3 }, generation: 1 });
        api.patchAnnotation.mockResolvedValue(msg('m1', { body: 'new', context_id: 'run-9' }));
        api.fetchAnnotationContext.mockResolvedValue(null);
        expect(await store().sendDraft('c1', 'k', 'run-1', 'new')).toBe(true);
        expect(api.patchAnnotation).toHaveBeenCalledWith('ch', 'c1', 'm1', { expected_revision: 3, body: 'new', context_items: [], mode: 'interrupt' });
        expect(api.fetchAnnotationContext).toHaveBeenCalledWith('ch', 'c1', 'run-9');
        expect(store().composerDrafts.k).toMatchObject({ editing: null, generation: 1 });
        useCollimateStore.setState({ activeChannel: null });
        expect(await store().sendDraft('c1', 'k', 'run-1', 'x')).toBe(false);
    });

    it('a failed post keeps its key for the same request only: changed content, a new target or a reused key take a new one', async () => {
        store().setComposerDraft('k', { text: 'v1' });
        const op = store().composerDrafts.k.operationId;
        api.postAnnotation.mockRejectedValue(new Error('Network Error'));
        await store().sendDraft('c1', 'k', 'run-1', 'v1');
        await store().sendDraft('c1', 'k', 'run-1', 'v1');
        const sentKeys = () => api.postAnnotation.mock.calls.map(c => c[2].operation_id);
        expect(sentKeys()).toEqual([op, op]);
        // The text changed after the failure: the server may hold the first one under that key.
        await store().sendDraft('c1', 'k', 'run-1', 'v2');
        const second = sentKeys()[2];
        expect(second).not.toBe(op);
        expect(store().composerDrafts.k.operationId).toBe(second);
        // An attachment added (or another target) is another request too.
        store().setComposerDraft('k', { contextItems: [{ path: 'page.html', renderer_id: 'html', label: 'L', text: 'T', anchor: null }] });
        await store().sendDraft('c1', 'k', 'run-1', 'v2');
        const third = sentKeys()[3];
        expect(third).not.toBe(second);
        await store().sendDraft('c1', 'k', 'cell', 'v2');
        expect(sentKeys()[4]).not.toBe(third);
        // The server holds the key for another request: it is spent, the retry takes a new one.
        api.postAnnotation.mockRejectedValueOnce(refusal('operation_reused'));
        await store().sendDraft('c1', 'k', 'cell', 'v2');
        const reused = sentKeys()[5];
        expect(store().composerDrafts.k).toMatchObject({ failedRequest: null, notice: 'That message was already sent differently; send it again.' });
        expect(store().composerDrafts.k.operationId).not.toBe(reused);
        api.postAnnotation.mockResolvedValueOnce(msg('a1', { context_id: 'cell' }));
        expect(await store().sendDraft('c1', 'k', 'cell', 'v2')).toBe(true);
        expect(sentKeys()[6]).not.toBe(reused);
    });

    it('a success clears only what it sent: text typed, a pick that landed, another reply or a newer edit meanwhile stay', async () => {
        const sentItem = { path: 'page.html', renderer_id: 'html', label: 'L', text: 'T', anchor: null };
        const laterItem = { ...sentItem, text: 'later' };
        useCollimateStore.setState({ annotationContexts: { c1: { 'run-1': ctx('run-1', 1, [msg('root'), msg('other')]) } } });
        store().setComposerDraft('k', { text: 'hello', contextItems: [sentItem], mode: 'interrupt' });
        store().setDraftReply('k', msg('root'));
        const answer = deferred<AnnotationMessage>();
        api.postAnnotation.mockReturnValue(answer.promise);
        const sending = store().sendDraft('c1', 'k', 'run-1', 'hello');
        store().setComposerDraft('k', { text: 'hello, and more', contextItems: [sentItem, laterItem] });
        store().setDraftReply('k', msg('other'));
        answer.resolve(msg('r1', { parent_id: 'root' }));
        expect(await sending).toBe(true);
        expect(store().composerDrafts.k).toMatchObject({
            text: 'hello, and more', contextItems: [laterItem], mode: 'queued', sending: false,
            replyTo: { id: 'other' },
        });

        // An edit loaded while a post is on its way stays whole.
        const posting = deferred<AnnotationMessage>();
        api.postAnnotation.mockReturnValue(posting.promise);
        const again = store().sendDraft('c1', 'k', 'run-1', 'hello, and more');
        store().loadDraftForEdit('k', msg('m9', { body: 'to edit', revision: 2 }));
        const loaded = store().composerDrafts.k;
        posting.resolve(msg('a2'));
        expect(await again).toBe(true);
        expect(store().composerDrafts.k).toEqual({ ...loaded, sending: false });
    });

    it('an edit follows its message: a refetch takes its new revision, and a read or deleted message ends the edit with a notice', async () => {
        const K = 'c1:run:run-1';
        useCollimateStore.setState({ annotationContexts: { c1: { 'run-1': ctx('run-1', 1, [msg('m1', { revision: 2 })]) } } });
        store().loadDraftForEdit(K, msg('m1', { revision: 2 }));
        store().setComposerDraft(K, { text: 'edited' });
        // A reorder elsewhere bumped the revision: the Save is refused once, the refetch carries the new one.
        api.patchAnnotation.mockRejectedValueOnce(refusal('revision'));
        api.fetchAnnotationContext.mockResolvedValueOnce(ctx('run-1', 2, [msg('m1', { revision: 3, position: 9 })]));
        expect(await store().sendDraft('c1', K, 'run-1', 'edited')).toBe(false);
        await vi.waitFor(() => expect(store().composerDrafts[K].editing).toEqual({ id: 'm1', contextId: 'run-1', revision: 3 }));
        api.patchAnnotation.mockResolvedValueOnce(msg('m1', { body: 'edited', revision: 4 }));
        expect(await store().sendDraft('c1', K, 'run-1', 'edited')).toBe(true);
        expect(api.patchAnnotation.mock.calls[1][3]).toMatchObject({ expected_revision: 3 });

        // Any answered context moves the revision on (the bar, an interrupt), and a full load does too.
        store().loadDraftForEdit(K, msg('m1', { revision: 4 }));
        api.setAnnotationBar.mockResolvedValueOnce(ctx('run-1', 3, [msg('m1', { revision: 5 })], { bar_on: true }));
        await store().setQueueBar('c1', 'run-1', true);
        expect(store().composerDrafts[K].editing?.revision).toBe(5);
        api.fetchCellAnnotations.mockResolvedValueOnce([ctx('run-1', 4, [msg('m1', { revision: 6 })])]);
        await store().loadCellAnnotations('c1');
        expect(store().composerDrafts[K].editing?.revision).toBe(6);
        // Another cell's context with the same ids leaves this cell's edit alone.
        api.fetchCellAnnotations.mockResolvedValueOnce([{ ...ctx('run-1', 9, [msg('m1', { revision: 99, read_at: 'x' })]), cell_id: 'c2' }]);
        await store().loadCellAnnotations('c2');
        expect(store().composerDrafts[K].editing?.revision).toBe(6);

        // The run read it: the edit ends, its text stays to send as a new message.
        api.fetchAnnotationContext.mockResolvedValueOnce(ctx('run-1', 5, [msg('m1', { revision: 6, read_at: '2026-10-24 10:09:00' })]));
        await store().refreshAnnotationContext('c1', 'run-1');
        expect(store().composerDrafts[K]).toMatchObject({ editing: null, text: 'm1', notice: expect.stringContaining('read this message') });
        // Deleted elsewhere: the same.
        store().loadDraftForEdit(K, msg('m2', { revision: 1 }));
        api.fetchAnnotationContext.mockResolvedValueOnce(ctx('run-1', 6, []));
        await store().refreshAnnotationContext('c1', 'run-1');
        expect(store().composerDrafts[K]).toMatchObject({ editing: null, text: 'm2', notice: expect.stringContaining('is gone') });
    });

    it('resetComposerDraft empties the draft and remounts its editor', () => {
        store().setComposerDraft('k', { text: 'x', below: true, expanded: true });
        store().resetComposerDraft('k');
        expect(store().composerDrafts.k).toMatchObject({ text: '', below: true, expanded: true, generation: 1 });
        store().resetComposerDraft('fresh');
        expect(store().composerDrafts.fresh.generation).toBe(1);
    });
});

describe('message and queue writes', () => {
    beforeEach(() => {
        useCollimateStore.setState({ annotationContexts: { c1: { 'run-1': ctx('run-1', 1, [msg('a', { position: 1 }), msg('b', { position: 2, revision: 4 })]) } } });
    });

    it('deleteMessage removes locally; a refusal says so and refetches', async () => {
        api.deleteAnnotation.mockResolvedValue(undefined);
        await store().deleteMessage('c1', msg('b', { revision: 4 }));
        expect(api.deleteAnnotation).toHaveBeenCalledWith('ch', 'c1', 'b', 4);
        expect(store().annotationContexts.c1['run-1'].messages.map(m => m.id)).toEqual(['a']);
        api.deleteAnnotation.mockRejectedValue(refusal('locked'));
        api.fetchAnnotationContext.mockResolvedValue(ctx('run-1', 5));
        await store().deleteMessage('c1', msg('a'));
        expect(store().annotationNotice.c1).toBe('The run has already read this message; it can no longer change.');
        await vi.waitFor(() => expect(store().annotationContexts.c1['run-1'].rev).toBe(5));
    });

    it('reorderContextQueue applies the order at once, takes the answer, and refetches after a refusal', async () => {
        const answer = deferred<AnnotationContext>();
        api.orderAnnotations.mockReturnValue(answer.promise);
        const p = store().reorderContextQueue('c1', 'run-1', ['b', 'a']);
        expect(store().annotationContexts.c1['run-1'].messages.map(m => m.id)).toEqual(['b', 'a']);
        answer.resolve(ctx('run-1', 2, [msg('b'), msg('a')]));
        await p;
        expect(api.orderAnnotations).toHaveBeenCalledWith('ch', 'c1', 'run-1', ['b', 'a'], undefined);
        expect(store().annotationContexts.c1['run-1'].rev).toBe(2);
        api.orderAnnotations.mockRejectedValue(refusal('stale'));
        api.fetchAnnotationContext.mockResolvedValue(ctx('run-1', 9));
        await store().reorderContextQueue('c1', 'run-1', ['a', 'b'], 1);
        expect(store().annotationNotice.c1).toBe('The queue changed elsewhere; look again and retry.');
        await vi.waitFor(() => expect(store().annotationContexts.c1['run-1'].rev).toBe(9));
    });

    it('setQueueBar and interruptQueued take the answered context; a refusal is said', async () => {
        api.setAnnotationBar.mockResolvedValue(ctx('run-1', 3, [], { bar_on: true }));
        await store().setQueueBar('c1', 'run-1', true);
        expect(api.setAnnotationBar).toHaveBeenCalledWith('ch', 'c1', 'run-1', true);
        expect(store().annotationContexts.c1['run-1'].bar_on).toBe(true);
        api.interruptAnnotations.mockResolvedValue(ctx('run-1', 4, [], { interrupt_seq: 1 }));
        await store().interruptQueued('c1', 'run-1', ['a']);
        expect(api.interruptAnnotations).toHaveBeenCalledWith('ch', 'c1', 'run-1', ['a']);
        expect(store().annotationContexts.c1['run-1'].interrupt_seq).toBe(1);
        await store().interruptQueued('c1', 'run-1', []);
        expect(api.interruptAnnotations).toHaveBeenCalledTimes(1);
        api.setAnnotationBar.mockRejectedValue(refusal('sealed'));
        await store().setQueueBar('c1', 'run-1', false);
        expect(store().annotationNotice.c1).toBe('This run has ended; your message was not sent.');
        api.interruptAnnotations.mockRejectedValue(refusal('bar_off'));
        await store().interruptQueued('c1', 'run-1', ['a']);
        expect(store().annotationNotice.c1).toBe('Turn the queue bar on first.');
    });

    it('sendMessageToRun moves a cell note and refetches both contexts, even after a refusal', async () => {
        api.sendAnnotationToRun.mockResolvedValue(msg('n', { context_id: 'run-1' }));
        api.fetchAnnotationContext.mockResolvedValue(null);
        await store().sendMessageToRun('c1', 'n', 'run-1');
        expect(api.sendAnnotationToRun).toHaveBeenCalledWith('ch', 'c1', 'n', 'run-1');
        expect(api.fetchAnnotationContext.mock.calls.map(c => c[2]).sort()).toEqual(['cell', 'run-1']);
        api.sendAnnotationToRun.mockRejectedValue(refusal('run_not_live'));
        await store().sendMessageToRun('c1', 'n', 'run-1');
        expect(store().annotationNotice.c1).toBe('That run is no longer live.');
        expect(api.fetchAnnotationContext).toHaveBeenCalledTimes(4);
    });

    it('a notice clears itself after 6 s; a newer one restarts the clock; null clears at once', () => {
        vi.useFakeTimers();
        store().setAnnotationNotice('c1', 'one');
        vi.advanceTimersByTime(5000);
        store().setAnnotationNotice('c1', 'two');
        vi.advanceTimersByTime(5000);
        expect(store().annotationNotice.c1).toBe('two');
        vi.advanceTimersByTime(1001);
        expect(store().annotationNotice.c1).toBeUndefined();
        store().setAnnotationNotice('c1', 'three');
        store().setAnnotationNotice('c1', null);
        expect(store().annotationNotice.c1).toBeUndefined();
    });

    it('nothing is written without an active channel', async () => {
        useCollimateStore.setState({ activeChannel: null });
        await store().deleteMessage('c1', msg('a'));
        await store().reorderContextQueue('c1', 'run-1', ['a']);
        await store().setQueueBar('c1', 'run-1', true);
        await store().interruptQueued('c1', 'run-1', ['a']);
        await store().sendMessageToRun('c1', 'a', 'run-1');
        for (const fn of [api.deleteAnnotation, api.orderAnnotations, api.setAnnotationBar, api.interruptAnnotations, api.sendAnnotationToRun]) {
            expect(fn).not.toHaveBeenCalled();
        }
    });
});

describe('file touches and the file last viewed', () => {
    const t = (path: string, at = '2026-10-24 10:00:00'): FileTouch => ({ at, path, op: 'edit', folder: false, version_id: 'v' });

    it('addFileTouches merges and keeps the list when nothing is new; loadFileTouches fills a run once, sharing its request', async () => {
        store().addFileTouches('r', []);
        expect(store().fileTouches.r).toBeUndefined();
        store().addFileTouches('r', [t('a.md')]);
        const list = store().fileTouches.r;
        store().addFileTouches('r', [t('a.md')]);
        expect(store().fileTouches.r).toBe(list);
        const answer = deferred<FileTouch[]>();
        api.fetchFileTouches.mockReturnValue(answer.promise);
        const a = store().loadFileTouches('ch', 'r');
        void store().loadFileTouches('ch', 'r');
        answer.resolve([t('b.md', '2026-10-24 09:00:00'), t('a.md')]);
        await a;
        expect(api.fetchFileTouches).toHaveBeenCalledTimes(1);
        expect(store().fileTouches.r.map(x => x.path)).toEqual(['b.md', 'a.md']);
        api.fetchFileTouches.mockResolvedValue([]);
        await store().loadFileTouches('ch', 'empty');
        expect(store().fileTouches.empty).toEqual([]);
        const unchanged = store().fileTouches.r;
        api.fetchFileTouches.mockResolvedValue([t('a.md')]);
        await store().loadFileTouches('ch', 'r');
        expect(store().fileTouches.r).toBe(unchanged);
    });

    it('loadFileTouches warns on a failure', async () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
        api.fetchFileTouches.mockRejectedValue(new Error('down'));
        await store().loadFileTouches('ch', 'r');
        expect(warn).toHaveBeenCalled();
        warn.mockRestore();
    });

    it('noteFileTab keeps the same state for the same path', () => {
        store().noteFileTab('c1', 'a.md');
        const before = store().lastFileTab;
        store().noteFileTab('c1', 'a.md');
        expect(store().lastFileTab).toBe(before);
        store().noteFileTab('c1', 'b.md');
        expect(store().lastFileTab.c1).toBe('b.md');
    });
});

describe('finished runs', () => {
    it('addFinishedRun puts the newest first, once, and caps the cell at 50, dropping the oldest runs\' state', () => {
        useCollimateStore.setState({
            annotationContexts: { c1: { 'run-0': ctx('run-0', 1) } }, fileTouches: { 'run-0': [] },
            composerDrafts: { 'c1:run:run-0': {} as never }, runsPaneView: { c1: { view: 'run', runId: 'run-0', option: 'log' } },
        });
        for (let i = 0; i <= FINISHED_RUNS_PER_CELL; i++) store().addFinishedRun(finished(`run-${i}`));
        store().addFinishedRun(finished('run-50', { status: 'failed' }));
        const list = store().finishedRuns.c1;
        expect(list).toHaveLength(FINISHED_RUNS_PER_CELL);
        expect(list[0]).toMatchObject({ runId: 'run-50', status: 'failed' });
        expect(list.some(r => r.runId === 'run-0')).toBe(false);
        expect(store().annotationContexts.c1['run-0']).toBeUndefined();
        expect(store().fileTouches['run-0']).toBeUndefined();
        expect(store().composerDrafts['c1:run:run-0']).toBeUndefined();
        expect(store().runsPaneView.c1).toEqual(STATUS_VIEW);
    });

    it('clearFinishedRuns drops the cell\'s finished runs with their contexts, touches and drafts, and leaves the cell log', () => {
        useCollimateStore.setState({
            finishedRuns: { c1: [finished('run-1')], c2: [finished('run-9', { cellId: 'c2' })] },
            annotationContexts: { c1: { 'run-1': ctx('run-1', 1), 'run-live': ctx('run-live', 1) } },
            fileTouches: { 'run-1': [], 'run-live': [] }, composerDrafts: { 'c1:run:run-1': {} as never, 'c1:all': {} as never },
            runsPaneView: { c1: { view: 'all', option: 'default' } },
            cellLogRuns: { c1: [{ runId: 'run-1', channel: 'ch', runner: 'Agent', startedAt: 1 }] },
        });
        store().clearFinishedRuns('c1');
        expect(store().finishedRuns).toEqual({ c2: [finished('run-9', { cellId: 'c2' })] });
        expect(Object.keys(store().annotationContexts.c1)).toEqual(['run-live']);
        expect(Object.keys(store().fileTouches)).toEqual(['run-live']);
        expect(Object.keys(store().composerDrafts)).toEqual(['c1:all']);
        expect(store().runsPaneView.c1).toEqual({ view: 'all', option: 'default' });
        expect(store().cellLogRuns.c1).toHaveLength(1);
        store().clearFinishedRuns('none');
        expect(store().finishedRuns.c2).toHaveLength(1);
    });
});

describe('opening the pane', () => {
    it('applies at once on a held layout, with a focus request that counts up', () => {
        useCollimateStore.setState({ cellLayouts: { c1: createDefaultLayout({ filePaths: [], pinnedPath: null }) } });
        store().openRunsPaneOn('c1', { view: 'all', option: 'log' });
        expect(store().cellLayouts.c1.runsPane.open).toBe(true);
        expect(store().runsPaneView.c1).toEqual({ view: 'all', option: 'log' });
        expect(store().runsPaneFocus.c1).toMatchObject({ target: 'view', seq: 1 });
        store().openRunsPaneOn('c1', STATUS_VIEW, { focus: 'composer', channel: 'ch' });
        expect(store().runsPaneFocus.c1).toMatchObject({ target: 'composer', seq: 2 });
    });

    it('waits (pendingRunsPaneOpen) while the displays are not loaded, or for another channel', () => {
        useCollimateStore.setState({ cellDisplaysReady: false });
        store().openRunsPaneOn('c1', STATUS_VIEW);
        expect(store().pendingRunsPaneOpen).toMatchObject({ channel: 'ch', cellId: 'c1', view: STATUS_VIEW, focus: 'view' });
        expect(store().runsPaneView.c1).toBeUndefined();
        useCollimateStore.setState({ cellDisplaysReady: true, pendingRunsPaneOpen: null });
        store().openRunsPaneOn('c1', STATUS_VIEW, { channel: 'other' });
        expect(store().pendingRunsPaneOpen).toMatchObject({ channel: 'other', cellId: 'c1' });
        useCollimateStore.setState({ activeChannel: null, pendingRunsPaneOpen: null });
        store().openRunsPaneOn('c1', STATUS_VIEW);
        expect(store().pendingRunsPaneOpen).toBeNull();
    });
});

describe('Add annotation', () => {
    const answer = (status: string, extra: object = {}) => ({ status, ...extra });

    it('asks the file\'s view for a pick and attaches its answer once', async () => {
        renderer.requestAnnotation.mockResolvedValue(answer('ok', { path: 'page.html', rendererId: 'html', context: { label: 'page.html › #x', text: 'el', anchor: { css: '#x' } } }));
        const p = store().requestDraftContext('c1', 'k', 'page.html');
        expect(store().annotationRequests.c1).toMatchObject({ draftKey: 'k', path: 'page.html' });
        await p;
        expect(renderer.requestAnnotation).toHaveBeenCalledWith('c1', 'page.html', 'pick', expect.objectContaining({ signal: expect.any(AbortSignal), hint: expect.any(String) }));
        expect(store().annotationRequests.c1).toBeUndefined();
        expect(store().composerDrafts.k.contextItems).toEqual([{ path: 'page.html', renderer_id: 'html', label: 'page.html › #x', text: 'el', anchor: { css: '#x' } }]);
        await store().requestDraftContext('c1', 'k', 'page.html');
        expect(store().composerDrafts.k.contextItems).toHaveLength(1);
    });

    it('a cancel does nothing; unavailable links the file into the draft; error says its message; a throw says itself', async () => {
        renderer.requestAnnotation.mockResolvedValueOnce(answer('cancelled'));
        await store().requestDraftContext('c1', 'k', 'page.html');
        expect(store().composerDrafts.k).toBeUndefined();
        renderer.requestAnnotation.mockResolvedValueOnce(answer('unavailable', { reason: 'not-capable', message: 'page.html’s renderer does not take annotations.', view: { rendererId: 'html', selection: '' } }));
        await store().requestDraftContext('c1', 'k', 'page.html');
        expect(store().composerDrafts.k).toMatchObject({ text: '[page.html](page.html)\n', contextItems: [], notice: null, generation: 1 });
        // A second answer for a file the draft already links adds nothing; text
        // already written stays above the link, on its own line.
        renderer.requestAnnotation.mockResolvedValueOnce(answer('unavailable', { reason: 'not-capable', message: 'page.html’s renderer does not take annotations.', view: null }));
        await store().requestDraftContext('c1', 'k', 'page.html');
        expect(store().composerDrafts.k.text).toBe('[page.html](page.html)\n');
        store().setComposerDraft('k', { text: 'see this' });
        renderer.requestAnnotation.mockResolvedValueOnce(answer('unavailable', { reason: 'read-only', message: 'locked', view: null }));
        await store().requestDraftContext('c1', 'k', 'docs/a b.md');
        expect(store().composerDrafts.k).toMatchObject({ text: 'see this\n[a b.md](docs/a%20b.md)\n', notice: null, generation: 2 });
        renderer.requestAnnotation.mockResolvedValueOnce(answer('error', { message: 'boom' }));
        await store().requestDraftContext('c1', 'k', 'page.html');
        expect(store().composerDrafts.k.notice).toBe('boom');
        renderer.requestAnnotation.mockRejectedValueOnce(new Error('thrown'));
        await store().requestDraftContext('c1', 'k', 'page.html');
        expect(store().composerDrafts.k.notice).toBe('thrown');
    });

    it('unavailable with a selection attaches it under the file link, once per selection, within the bound', async () => {
        const selected = (selection: string, rendererId = 'milkdown') =>
            renderer.requestAnnotation.mockResolvedValueOnce(answer('unavailable', { reason: 'not-capable', message: 'no', view: { rendererId, selection } }));
        selected('the first paragraph');
        await store().requestDraftContext('c1', 'k', 'notes.md');
        const quoted = { path: 'notes.md', renderer_id: 'milkdown', label: 'notes.md › selection', text: 'the first paragraph', anchor: null };
        expect(store().composerDrafts.k).toMatchObject({ text: '[notes.md](notes.md)\n', contextItems: [quoted], notice: null });
        // The same selection again adds nothing; another one is another block; the link stays single.
        selected('the first paragraph');
        await store().requestDraftContext('c1', 'k', 'notes.md');
        expect(store().composerDrafts.k.contextItems).toEqual([quoted]);
        selected('a second part');
        await store().requestDraftContext('c1', 'k', 'notes.md');
        expect(store().composerDrafts.k.contextItems.map(i => i.text)).toEqual(['the first paragraph', 'a second part']);
        expect(store().composerDrafts.k.text).toBe('[notes.md](notes.md)\n');
        // The bound holds for these as for picks: the link is still added, the block is not.
        store().setComposerDraft('k', { text: '', contextItems: Array.from({ length: MAX_CONTEXT_ITEMS }, (_, i) => ({ ...quoted, text: `part ${i}` })) });
        selected('one too many');
        await store().requestDraftContext('c1', 'k', 'notes.md');
        expect(store().composerDrafts.k.contextItems).toHaveLength(MAX_CONTEXT_ITEMS);
        expect(store().composerDrafts.k.text).toBe('[notes.md](notes.md)\n');
        expect(store().composerDrafts.k.notice).toBe(`At most ${MAX_CONTEXT_ITEMS} attachments per message.`);
    });

    it('a second request aborts the first, whose late answer is ignored; cancel aborts and clears', async () => {
        const first = deferred<object>();
        renderer.requestAnnotation.mockReturnValueOnce(first.promise);
        const p1 = store().requestDraftContext('c1', 'k', 'page.html');
        const signal1 = renderer.requestAnnotation.mock.calls[0][3].signal as AbortSignal;
        renderer.requestAnnotation.mockReturnValueOnce(new Promise(() => {}));
        void store().requestDraftContext('c1', 'other', 'page.html');
        expect(signal1.aborted).toBe(true);
        first.resolve(answer('ok', { path: 'page.html', rendererId: 'html', context: { label: 'l', text: 't' } }));
        await p1;
        expect(store().composerDrafts.k).toBeUndefined();
        expect(store().annotationRequests.c1.draftKey).toBe('other');
        const signal2 = renderer.requestAnnotation.mock.calls[1][3].signal as AbortSignal;
        store().cancelDraftContextRequest('c1');
        expect(signal2.aborted).toBe(true);
        expect(store().annotationRequests.c1).toBeUndefined();
        store().cancelDraftContextRequest('c1');
    });

    it('a full draft says so instead of attaching a 17th item', async () => {
        const items = Array.from({ length: 16 }, (_, i) => ({ path: `f${i}`, renderer_id: 'r', label: 'l', text: `${i}`, anchor: null }));
        store().setComposerDraft('k', { contextItems: items });
        renderer.requestAnnotation.mockResolvedValue(answer('ok', { path: 'page.html', rendererId: 'html', context: { label: 'l', text: 'new' } }));
        await store().requestDraftContext('c1', 'k', 'page.html');
        expect(store().composerDrafts.k.contextItems).toHaveLength(16);
        expect(store().composerDrafts.k.notice).toBe('At most 16 attachments per message.');
    });
});
