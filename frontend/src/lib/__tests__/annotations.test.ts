// The status pane's pure annotation rules: server times, threads, the
// timeline beside file touches, the queue and its reorder, the wire's
// normalisation, and the words for a run's phase and end.

import { describe, expect, it } from 'vitest';
import {
    QUEUE_BAR, applyOrderLocally, buildTimeline, contextItemFrom, filterThreadsByPath, filterTimelineByPath,
    finishedStatusLabel, isLocked, isQueuedRoot, mergeTouches, normalizeAnnotationMessage, normalizeContext, normalizeTouch,
    phaseLine, queueTokens, queuedInterruptCandidates, removeMessage, reorderQueue, sameContextItem, serverTime, splitQueue,
    threadMentions, threadTime, threadsOf, touchesFromCommit, upsertMessage,
    type AnnotationContext, type AnnotationMessage, type FileTouch,
} from '../annotations';

const msg = (id: string, over: Partial<AnnotationMessage> = {}): AnnotationMessage => ({
    id, context_id: 'run-1', parent_id: null, author: 'user', author_label: null, kind: 'message', body: id,
    context_items: [], refs: [], mode: 'queued', position: 1, held: false, revision: 1,
    created_at: '2026-10-24 10:00:00', edited_at: null, read_at: null, ...over,
});
const ctx = (messages: AnnotationMessage[], over: Partial<AnnotationContext> = {}): AnnotationContext => ({
    id: 'run-1', cell_id: 'c1', run_id: 'run-1', label: 'agent-11111111', bar_on: false, listening: true,
    interrupt_seq: 0, rev: 1, sealed: false, messages, ...over,
});
const touch = (path: string, at: string, over: Partial<FileTouch> = {}): FileTouch => ({ at, path, op: 'edit', folder: false, version_id: 'v1', ...over });
const item = (path: string, text = 't') => ({ path, renderer_id: 'html', label: path, text, anchor: null });

describe('serverTime', () => {
    it('reads the UTC wire form and ISO, and 0 for nothing', () => {
        expect(serverTime('2026-10-24 10:00:05')).toBe(Date.UTC(2026, 9, 24, 10, 0, 5));
        expect(serverTime('2026-10-24T10:00:05.250')).toBe(Date.UTC(2026, 9, 24, 10, 0, 5, 250));
        expect(serverTime('2026-10-24T12:00:05+02:00')).toBe(Date.UTC(2026, 9, 24, 10, 0, 5));
        expect(serverTime('soon')).toBe(0);
        expect(serverTime(null)).toBe(0);
        expect(serverTime(undefined)).toBe(0);
    });
});

describe('roots, locks and threads', () => {
    it('a queued root is an unread top-level user message', () => {
        expect(isQueuedRoot(msg('a'))).toBe(true);
        expect(isQueuedRoot(msg('a', { read_at: '2026-10-24 10:00:01' }))).toBe(false);
        expect(isQueuedRoot(msg('a', { parent_id: 'r' }))).toBe(false);
        expect(isQueuedRoot(msg('a', { author: 'run' }))).toBe(false);
        expect(isQueuedRoot(msg('a', { kind: 'prompt' }))).toBe(false);
    });

    it('read messages, prompts and run posts are locked', () => {
        expect(isLocked(msg('a'))).toBe(false);
        expect(isLocked(msg('a', { read_at: 'x' }))).toBe(true);
        expect(isLocked(msg('a', { kind: 'prompt' }))).toBe(true);
        expect(isLocked(msg('a', { author: 'run' }))).toBe(true);
    });

    it('threads keep one level: replies under their root by creation; an orphan reply is dropped', () => {
        const threads = threadsOf([
            msg('r1'), msg('x', { parent_id: 'r1', created_at: '2026-10-24 10:00:09' }),
            msg('y', { parent_id: 'r1', created_at: '2026-10-24 10:00:02' }),
            msg('r2'), msg('orphan', { parent_id: 'gone' }),
        ]);
        expect(threads.map(t => [t.root.id, t.replies.map(r => r.id)])).toEqual([['r1', ['y', 'x']], ['r2', []]]);
    });

    it('splitQueue: prompts, run posts and read roots are history; the queue and the held, each by position', () => {
        const c = ctx([
            msg('prompt', { kind: 'prompt', read_at: 'x', position: 1 }),
            msg('post', { author: 'run', author_label: 'agent-1', read_at: 'x', position: 2 }),
            msg('read', { read_at: '2026-10-24 10:01:00', position: 3 }),
            msg('q2', { position: 6 }), msg('q1', { position: 5 }),
            msg('h2', { position: 9, held: true }), msg('h1', { position: 8, held: true }),
        ]);
        const split = splitQueue(c);
        expect(split.history.map(t => t.root.id)).toEqual(['prompt', 'post', 'read']);
        expect(split.queue.map(t => t.root.id)).toEqual(['q1', 'q2']);
        expect(split.held.map(t => t.root.id)).toEqual(['h1', 'h2']);
    });
});

describe('the timeline', () => {
    it('places a read user root at its read time, others at creation; a touch wins a tie', () => {
        const read = { root: msg('read', { created_at: '2026-10-24 09:00:00', read_at: '2026-10-24 10:05:00' }), replies: [] };
        const post = { root: msg('post', { author: 'run', created_at: '2026-10-24 10:02:00', read_at: '2026-10-24 10:02:00' }), replies: [] };
        expect(threadTime(read)).toBe(serverTime('2026-10-24 10:05:00'));
        expect(threadTime(post)).toBe(serverTime('2026-10-24 10:02:00'));
        const items = buildTimeline(
            [{ thread: read, contextId: 'run-1', tag: null, sendable: false }, { thread: post, contextId: 'run-1', tag: null, sendable: false }],
            [{ touch: touch('a.md', '2026-10-24 10:02:00'), tag: null }, { touch: touch('b.md', '2026-10-24 10:01:00'), tag: 'agent' }],
        );
        expect(items.map(i => (i.kind === 'thread' ? i.thread.root.id : i.touch.path))).toEqual(['b.md', 'a.md', 'post', 'read']);
        expect(items[0]).toMatchObject({ kind: 'touch', tag: 'agent', key: 'f:v1:b.md' });
        expect(items[2]).toMatchObject({ kind: 'thread', key: 't:run-1:post' });
    });

    it('an unread user message sits at its creation (All interleaves them)', () => {
        const post = { root: msg('p', { author: 'run', created_at: '2026-10-24 09:30:00', read_at: '2026-10-24 09:30:00' }), replies: [] };
        const ids = (created: string) => buildTimeline(
            [{ thread: post, contextId: 'c', tag: null, sendable: false },
                { thread: { root: msg('u', { created_at: created }), replies: [] }, contextId: 'cell', tag: null, sendable: true }], [],
        ).map(i => (i.kind === 'thread' ? i.thread.root.id : ''));
        expect(ids('2026-10-24 09:00:00')).toEqual(['u', 'p']);
        expect(ids('2026-10-24 11:00:00')).toEqual(['p', 'u']);
    });

    it('Current file keeps the threads that attach part of the file (a reply counts) and the file\'s touches', () => {
        const about = { root: msg('a', { context_items: [item('page.html')] }), replies: [] };
        const viaReply = { root: msg('b'), replies: [msg('b1', { parent_id: 'b', context_items: [item('page.html')] })] };
        const pathless = { root: msg('c', { refs: ['page.html'] }), replies: [] };
        expect(threadMentions(viaReply, 'page.html')).toBe(true);
        expect(threadMentions(pathless, 'page.html')).toBe(false);
        expect(filterThreadsByPath([about, viaReply, pathless], 'page.html').map(t => t.root.id)).toEqual(['a', 'b']);
        const items = buildTimeline(
            [about, viaReply, pathless].map(thread => ({ thread, contextId: 'run-1', tag: null, sendable: false })),
            [{ touch: touch('page.html', '2026-10-24 10:00:00'), tag: null }, { touch: touch('other.md', '2026-10-24 10:00:00'), tag: null }],
        );
        expect(filterTimelineByPath(items, 'page.html').map(i => (i.kind === 'thread' ? i.thread.root.id : i.touch.path)))
            .toEqual(['page.html', 'a', 'b']);
    });
});

describe('the queue', () => {
    it('interrupt candidates are the queued messages above the bar, in position order', () => {
        const c = ctx([
            msg('q2', { position: 3 }), msg('q1', { position: 2 }), msg('int', { position: 1, mode: 'interrupt' }),
            msg('held', { position: 5, held: true }), msg('read', { position: 0, read_at: 'x' }),
        ]);
        expect(queuedInterruptCandidates(c)).toEqual(['q1', 'q2']);
    });

    it('tokens carry the bar only while it is on', () => {
        expect(queueTokens(['a', 'b'], ['c'], true)).toEqual(['a', 'b', QUEUE_BAR, 'c']);
        expect(queueTokens(['a', 'b'], [], false)).toEqual(['a', 'b']);
    });

    it('reorders within the queue, across the bar both ways, and says null for no change', () => {
        const tokens = queueTokens(['a', 'b'], ['c'], true);
        expect(reorderQueue(tokens, 'b', 0)).toEqual({ ids: ['b', 'a', 'c'], heldFrom: 2 });
        expect(reorderQueue(tokens, 'b', 2)).toEqual({ ids: ['a', 'b', 'c'], heldFrom: 1 });
        expect(reorderQueue(tokens, 'c', 2)).toEqual({ ids: ['a', 'b', 'c'], heldFrom: 3 });
        expect(reorderQueue(tokens, 'c', 0)).toEqual({ ids: ['c', 'a', 'b'], heldFrom: 3 });
        expect(reorderQueue(tokens, 'a', 0)).toBeNull();
        expect(reorderQueue(tokens, 'gone', 0)).toBeNull();
        expect(reorderQueue(queueTokens(['a', 'b'], [], false), 'a', 9)).toEqual({ ids: ['b', 'a'], heldFrom: undefined });
        expect(reorderQueue(queueTokens(['a', 'b'], [], false), 'b', -3)).toEqual({ ids: ['b', 'a'], heldFrom: undefined });
    });

    it('a move in a filtered view orders the whole queue, the hidden messages keeping their places', () => {
        // The queue a, x, b | bar | y, h with x and y hidden (Current file shows a, b, the bar and h).
        const all = queueTokens(['a', 'x', 'b'], ['y', 'h'], true);
        const shown = all.filter(t => t !== 'x' && t !== 'y');
        // b up one: before a; x stays where it was.
        expect(reorderQueue(all, 'b', 0, shown)).toEqual({ ids: ['b', 'a', 'x', 'y', 'h'], heldFrom: 3 });
        // a down one: just after b, past the hidden x.
        expect(reorderQueue(all, 'a', 1, shown)).toEqual({ ids: ['x', 'b', 'a', 'y', 'h'], heldFrom: 3 });
        // h up across the bar: just before it, so after b.
        expect(reorderQueue(all, 'h', 2, shown)).toEqual({ ids: ['a', 'x', 'b', 'h', 'y'], heldFrom: 4 });
        // b down to the end: just after h, the last one shown.
        expect(reorderQueue(all, 'b', 3, shown)).toEqual({ ids: ['a', 'x', 'y', 'h', 'b'], heldFrom: 2 });
        // Where it already is, a hidden id, or the only one shown: nothing.
        expect(reorderQueue(all, 'b', 1, shown)).toBeNull();
        expect(reorderQueue(all, 'x', 0, shown)).toBeNull();
        expect(reorderQueue(queueTokens(['a', 'x'], [], false), 'a', 1, ['a'])).toBeNull();
    });

    it('applies an order locally: positions after the read messages, held from heldFrom on (queued), replies following', () => {
        const c = ctx([
            msg('read', { position: 4, read_at: 'x' }),
            msg('a', { position: 5, mode: 'interrupt' }), msg('b', { position: 6 }),
            msg('b1', { parent_id: 'b', position: 0 }),
        ]);
        const next = applyOrderLocally(c, ['b', 'a'], 1);
        const by = (id: string) => next.messages.find(m => m.id === id)!;
        expect(next.messages.map(m => m.id)).toEqual(['read', 'b', 'b1', 'a']);
        expect(by('b')).toMatchObject({ position: 5, held: false });
        expect(by('a')).toMatchObject({ position: 6, held: true, mode: 'queued' });
        expect(by('b1').held).toBe(false);
        expect(applyOrderLocally(c, ['b', 'a'], 0).messages.find(m => m.id === 'b1')!.held).toBe(true);
        expect(applyOrderLocally(ctx([msg('a')]), ['a'], undefined).messages[0]).toMatchObject({ position: 1, held: false });
    });

    it('upserts by id or appends in place, and removes a root with its replies', () => {
        const c = ctx([msg('a', { position: 1 }), msg('b', { position: 2 }), msg('b1', { parent_id: 'b' })]);
        expect(upsertMessage(c, msg('a', { body: 'new', position: 1 })).messages.map(m => [m.id, m.body])).toEqual([['a', 'new'], ['b', 'b'], ['b1', 'b1']]);
        expect(upsertMessage(c, msg('c', { position: 1.5 })).messages.map(m => m.id)).toEqual(['a', 'c', 'b', 'b1']);
        expect(upsertMessage(c, msg('a1', { parent_id: 'a' })).messages.map(m => m.id)).toEqual(['a', 'a1', 'b', 'b1']);
        expect(removeMessage(c, 'b').messages.map(m => m.id)).toEqual(['a']);
        expect(removeMessage(c, 'b1').messages.map(m => m.id)).toEqual(['a', 'b']);
    });
});

describe('the wire', () => {
    it('normalizeContext takes a good context, drops malformed messages and items, and refuses a bad shape', () => {
        const good = ctx([
            msg('ok', { context_items: [item('a.md'), { nope: 1 } as never, { ...item('b.md'), anchor: { css: '#x' } }] }),
            { id: 'bad' } as never,
            msg('mode', { mode: 'loud' as never }),
        ]);
        const c = normalizeContext(good)!;
        expect(c.messages.map(m => m.id)).toEqual(['ok']);
        expect(c.messages[0].context_items.map(i => [i.path, i.anchor])).toEqual([['a.md', null], ['b.md', { css: '#x' }]]);
        expect(normalizeContext(null)).toBeNull();
        expect(normalizeContext({ ...good, rev: 'x' })).toBeNull();
        expect(normalizeContext({ ...good, messages: null })).toBeNull();
        expect(normalizeContext({ id: 'cell', cell_id: 'c1', rev: 0, messages: [] })).toMatchObject({ run_id: null, interrupt_seq: 0, bar_on: false, sealed: false });
        expect(normalizeAnnotationMessage(msg('m', { refs: ['a.md', 3 as never], held: 'yes' as never }))).toMatchObject({ refs: ['a.md'], held: false });
        expect(normalizeAnnotationMessage(msg('m', { author: 'robot' as never }))).toBeNull();
        expect(normalizeAnnotationMessage(msg('m', { kind: 'other' as never }))).toBeNull();
        expect(normalizeAnnotationMessage({ ...msg('m'), position: 'x' })).toBeNull();
        expect(normalizeAnnotationMessage({ ...msg('m'), context_items: 'x' })).toMatchObject({ context_items: [], refs: [] });
    });

    it('normalizeTouch refuses a bad op or a missing version', () => {
        expect(normalizeTouch({ at: 'x', path: 'a', op: 'edit', folder: true, version_id: 7 })).toEqual({ at: 'x', path: 'a', op: 'edit', folder: true, version_id: '7' });
        expect(normalizeTouch({ at: 'x', path: 'a', op: 'rename', version_id: 7 })).toBeNull();
        expect(normalizeTouch({ at: 'x', path: 'a', op: 'edit' })).toBeNull();
        expect(normalizeTouch('a')).toBeNull();
    });

    it('a live commit row\'s changes are touches with its version id; bad entries go', () => {
        expect(touchesFromCommit({ cell_id: 42, changes: [{ path: 'a.md', op: 'create', folder: false }, { path: 'd', op: 'delete', folder: true }, 'x', { op: 'edit' }] }, '2026-10-24 10:00:00'))
            .toEqual([touch('a.md', '2026-10-24 10:00:00', { op: 'create', version_id: '42' }), touch('d', '2026-10-24 10:00:00', { op: 'delete', folder: true, version_id: '42' })]);
        expect(touchesFromCommit({ cell_id: 1 }, 'x')).toEqual([]);
        expect(touchesFromCommit({ changes: [] }, 'x')).toEqual([]);
    });

    it('mergeTouches dedupes by version and path, sorts by time then path, and keeps the list when nothing is new', () => {
        const a = touch('b.md', '2026-10-24 10:00:02'), b = touch('a.md', '2026-10-24 10:00:02'), c = touch('z.md', '2026-10-24 10:00:01', { version_id: 'v0' });
        const merged = mergeTouches([a], [b, a, c]);
        expect(merged.map(t => t.path)).toEqual(['z.md', 'a.md', 'b.md']);
        expect(mergeTouches(merged, [b])).toBe(merged);
        expect(mergeTouches([a], [touch('b.md', '2026-10-24 10:00:02', { version_id: 'v2' })])).toHaveLength(2);
    });

    it('a renderer answer becomes a context item; items compare by path, text and anchor', () => {
        const fromAnswer = contextItemFrom({ context: { label: 'p › #x', text: 'el' }, path: 'p.html', rendererId: 'html' });
        expect(fromAnswer).toEqual({ path: 'p.html', renderer_id: 'html', label: 'p › #x', text: 'el', anchor: null });
        expect(contextItemFrom({ context: { label: 'l', text: 't', anchor: { css: 'a' } }, path: 'p', rendererId: 'r' }).anchor).toEqual({ css: 'a' });
        expect(sameContextItem(fromAnswer, { ...fromAnswer, label: 'other' })).toBe(true);
        expect(sameContextItem(fromAnswer, { ...fromAnswer, anchor: { css: 'b' } })).toBe(false);
        expect(sameContextItem(fromAnswer, { ...fromAnswer, path: 'q' })).toBe(false);
    });
});

describe('words', () => {
    it('a finished run\'s status', () => {
        // The runs row's and the run_status stream's words.
        expect(finishedStatusLabel('completed')).toBe('Completed');
        expect(finishedStatusLabel('stopped')).toBe('Stopped');
        expect(finishedStatusLabel('failed')).toBe('Failed');
        // The stop types a client that saw the stop records.
        expect(finishedStatusLabel('gracefully_stopped')).toBe('Stopped');
        expect(finishedStatusLabel('force_stopped')).toBe('Stopped');
        expect(finishedStatusLabel('timed_out')).toBe('Timed out');
        expect(finishedStatusLabel('interrupted')).toBe('Interrupted');
        expect(finishedStatusLabel('failed')).toBe('Failed');
    });

    it('a live run\'s phase line', () => {
        expect(phaseLine('spinning_up')).toEqual({ phase: 'working', text: 'Starting' });
        expect(phaseLine('running')).toEqual({ phase: 'working', text: 'Working' });
        expect(phaseLine('awaiting_approval')).toEqual({ phase: 'waiting', text: 'Waiting for your approval' });
        expect(phaseLine('awaiting_input')).toEqual({ phase: 'waiting', text: 'Waiting for messages' });
        expect(phaseLine('stopping')).toEqual({ phase: 'stopping', text: 'Stopping' });
    });
});
