// Annotations: the messages between the user and a cell's runs, as the
// status pane shows them (components/runsPane). A cell has one context of
// its own (`'cell'`, the All view's notes) and one per run (its id); each
// holds top-level messages and one level of replies. The API is
// api/routes/annotations.py; these are its wire shapes and the pure rules
// the views apply to them: threads, the timeline beside a run's file
// touches, the queue (unread user messages in position order, split by the
// queue bar) and its reorder.

import type { DotStatus } from '@/lib/runningCell';
import { failureStatusLabel, isTerminatedStatus } from '@/lib/runStop';

/** The cell's own context id. */
export const CELL_CONTEXT_ID = 'cell';

/** At most this many context items ride on one message (the API's bound). */
export const MAX_CONTEXT_ITEMS = 16;

export type AnnotationAuthor = 'user' | 'run';
export type AnnotationMode = 'queued' | 'interrupt';
export type AnnotationKind = 'message' | 'prompt';

/** One attachment of a message: the part of a file a renderer answered
 *  with (its label, text and anchor), stamped with the file's path and the
 *  renderer's id by the host. */
export interface AnnotationContextItem {
    path: string;
    renderer_id: string;
    label: string;
    text: string;
    anchor: Record<string, unknown> | null;
}

export interface AnnotationMessage {
    id: string;
    context_id: string;
    parent_id: string | null;
    author: AnnotationAuthor;
    /** The run label of a run's post. */
    author_label: string | null;
    /** `prompt`: the run's prompt, seeded by the host, born read. */
    kind: AnnotationKind;
    body: string;
    context_items: AnnotationContextItem[];
    /** Cell paths the body links to. */
    refs: string[];
    mode: AnnotationMode;
    position: number;
    /** Below the queue bar: invisible to the run. A reply mirrors its root. */
    held: boolean;
    revision: number;
    /** Server UTC, `YYYY-MM-DD HH:MM:SS`. */
    created_at: string;
    edited_at: string | null;
    /** Set when the run pulled it; a read message never changes again. */
    read_at: string | null;
}

export interface AnnotationContext {
    id: string;
    cell_id: string;
    run_id: string | null;
    /** The run's label; null for the cell context. */
    label: string | null;
    bar_on: boolean;
    /** The run reads its context (it has polled it). */
    listening: boolean;
    interrupt_seq: number;
    /** Bumped on every change; the SSE doorbell carries it. */
    rev: number;
    /** The run ended: the context takes no more writes. */
    sealed: boolean;
    /** Roots by position, each followed by its replies. */
    messages: AnnotationMessage[];
}

/** A file one of the run's commits created, edited or deleted. */
export interface FileTouch {
    /** Server UTC, `YYYY-MM-DD HH:MM:SS`. */
    at: string;
    path: string;
    op: 'create' | 'edit' | 'delete';
    folder: boolean;
    /** The commit's version id. */
    version_id: string;
}

export interface Thread {
    root: AnnotationMessage;
    replies: AnnotationMessage[];
}

export type TimelineItem =
    | { kind: 'thread'; key: string; at: number; thread: Thread; contextId: string; tag: string | null; sendable: boolean }
    | { kind: 'touch'; key: string; at: number; touch: FileTouch; tag: string | null };

const SERVER_TIME = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(\.\d+)?$/;

/** A server time (UTC `YYYY-MM-DD HH:MM:SS`, or ISO with an offset) in ms;
 *  0 when it cannot be read. */
export function serverTime(s: string | null | undefined): number {
    if (!s) return 0;
    const m = SERVER_TIME.exec(s);
    const t = m
        ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], m[7] ? Math.round(parseFloat(m[7]) * 1000) : 0)
        : Date.parse(s);
    return Number.isFinite(t) ? t : 0;
}

/** An unread top-level message of the user's: one of the queue. */
export function isQueuedRoot(m: AnnotationMessage): boolean {
    return m.parent_id === null && m.author === 'user' && m.kind === 'message' && m.read_at === null;
}

/** A message the user can no longer edit, delete or move: read, a prompt,
 *  or a run's post. */
export function isLocked(m: AnnotationMessage): boolean {
    return m.read_at !== null || m.kind === 'prompt' || m.author !== 'user';
}

/** Threads in the roots' order; replies under their root by creation (ties
 *  keep the input order). A reply whose root is missing is dropped. */
export function threadsOf(messages: readonly AnnotationMessage[]): Thread[] {
    const threads: Thread[] = [];
    const byRoot = new Map<string, Thread>();
    for (const m of messages) {
        if (m.parent_id !== null) continue;
        const thread = { root: m, replies: [] as AnnotationMessage[] };
        threads.push(thread);
        byRoot.set(m.id, thread);
    }
    for (const m of messages) {
        if (m.parent_id === null) continue;
        byRoot.get(m.parent_id)?.replies.push(m);
    }
    for (const thread of threads) {
        thread.replies.sort((a, b) => serverTime(a.created_at) - serverTime(b.created_at));
    }
    return threads;
}

const byPosition = (a: Thread, b: Thread) => a.root.position - b.root.position;

/** A context's threads: the history (everything but the queue), the queue
 *  above the bar and the queue held below it, both in position order. */
export function splitQueue(ctx: AnnotationContext): { history: Thread[]; queue: Thread[]; held: Thread[] } {
    const history: Thread[] = [];
    const queue: Thread[] = [];
    const held: Thread[] = [];
    for (const thread of threadsOf(ctx.messages)) {
        if (!isQueuedRoot(thread.root)) history.push(thread);
        else if (thread.root.held) held.push(thread);
        else queue.push(thread);
    }
    queue.sort(byPosition);
    held.sort(byPosition);
    return { history, queue, held };
}

/** Where a thread sits in time: a user message when the run read it (its
 *  creation while unread), anything else when it was written. */
export function threadTime(t: Thread): number {
    const root = t.root;
    return serverTime(root.author === 'user' ? root.read_at ?? root.created_at : root.created_at);
}

/** Threads (at threadTime) and file touches in time order. On a tie a
 *  touch comes first, then the keys decide. An unread message sits at its
 *  creation, which is how All shows them (a drilldown keeps them in its
 *  queue instead). */
export function buildTimeline(
    threads: readonly { thread: Thread; contextId: string; tag: string | null; sendable: boolean }[],
    touches: readonly { touch: FileTouch; tag: string | null }[],
): TimelineItem[] {
    const items: TimelineItem[] = [
        ...threads.map(({ thread, contextId, tag, sendable }): TimelineItem => ({
            kind: 'thread',
            key: `t:${contextId}:${thread.root.id}`,
            at: threadTime(thread),
            thread, contextId, tag, sendable,
        })),
        ...touches.map(({ touch, tag }): TimelineItem => ({
            kind: 'touch',
            key: `f:${touch.version_id}:${touch.path}`,
            at: serverTime(touch.at),
            touch, tag,
        })),
    ];
    return items.sort((a, b) => a.at - b.at
        || (a.kind === b.kind ? 0 : a.kind === 'touch' ? -1 : 1)
        || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
}

/** Some message of the thread attaches part of `path`. */
export function threadMentions(t: Thread, path: string): boolean {
    return [t.root, ...t.replies].some(m => m.context_items.some(item => item.path === path));
}

/** The items about `path`: threads that attach part of it, touches of it. */
export function filterTimelineByPath(items: readonly TimelineItem[], path: string): TimelineItem[] {
    return items.filter(item => (item.kind === 'thread' ? threadMentions(item.thread, path) : item.touch.path === path));
}

export function filterThreadsByPath(threads: readonly Thread[], path: string): Thread[] {
    return threads.filter(t => threadMentions(t, path));
}

/** The queued (not interrupt) unread messages above the bar, in position
 *  order: what switching the composer to Interrupt offers to interrupt too. */
export function queuedInterruptCandidates(ctx: AnnotationContext): string[] {
    return splitQueue(ctx).queue.filter(t => t.root.mode === 'queued').map(t => t.root.id);
}

/** The queue bar's place among the queue's message ids. */
export const QUEUE_BAR: unique symbol = Symbol('queue-bar');
export type QueueToken = string | typeof QUEUE_BAR;

/** The queue as a list: the messages above the bar, the bar (when on),
 *  then the held ones. */
export function queueTokens(unheld: readonly string[], held: readonly string[], barOn: boolean): QueueToken[] {
    return barOn ? [...unheld, QUEUE_BAR, ...held] : [...unheld, ...held];
}

/** Move `dragId` to `slot`, its index among the `shown` tokens without it
 *  (0..length): the whole queue's new order and how many messages stay
 *  above the bar (undefined while the bar is off). `shown` is the part of
 *  the queue on screen (a filter's, in queue order; all of it by default):
 *  the message lands just before the shown token at that slot, or just
 *  after the last one, so the hidden messages keep their places. Null when
 *  nothing changes. */
export function reorderQueue(
    tokens: readonly QueueToken[], dragId: string, slot: number, shown: readonly QueueToken[] = tokens,
): { ids: string[]; heldFrom: number | undefined } | null {
    const others = shown.filter(t => t !== dragId);
    if (!tokens.includes(dragId) || others.length === shown.length || others.length === 0) return null;
    const rest = tokens.filter(t => t !== dragId);
    const at = Math.max(0, Math.min(others.length, slot));
    const index = at < others.length ? rest.indexOf(others[at]) : rest.indexOf(others[others.length - 1]) + 1;
    const next = [...rest.slice(0, index), dragId, ...rest.slice(index)];
    if (next.every((t, i) => t === tokens[i])) return null;
    const bar = next.indexOf(QUEUE_BAR);
    return { ids: next.filter((t): t is string => t !== QUEUE_BAR), heldFrom: bar < 0 ? undefined : bar };
}

/** Roots by position, each followed by its replies (in their order). */
function ordered(messages: readonly AnnotationMessage[]): AnnotationMessage[] {
    return threadsOf(messages).sort(byPosition).flatMap(t => [t.root, ...t.replies]);
}

/** The order a PUT /order asks for, applied before the server answers:
 *  positions after the read messages, `held` from `heldFrom` on (and held
 *  means queued); replies follow their root across the bar. */
export function applyOrderLocally(ctx: AnnotationContext, ids: readonly string[], heldFrom: number | undefined): AnnotationContext {
    const from = heldFrom ?? ids.length;
    const base = Math.max(0, ...ctx.messages.filter(m => m.parent_id === null && m.read_at !== null).map(m => m.position));
    const place = new Map(ids.map((id, i) => [id, i]));
    const heldRoot = new Map<string, boolean>();
    const messages = ctx.messages.map(m => {
        const i = place.get(m.id);
        if (i === undefined || m.parent_id !== null) return m;
        const held = i >= from;
        heldRoot.set(m.id, held);
        return { ...m, position: base + 1 + i, held, mode: held ? 'queued' as const : m.mode };
    }).map(m => (m.parent_id !== null && heldRoot.has(m.parent_id) ? { ...m, held: heldRoot.get(m.parent_id)! } : m));
    return { ...ctx, messages: ordered(messages) };
}

/** The context with `m` in it (replaced by id, or added). */
export function upsertMessage(ctx: AnnotationContext, m: AnnotationMessage): AnnotationContext {
    const known = ctx.messages.some(x => x.id === m.id);
    const messages = known ? ctx.messages.map(x => (x.id === m.id ? m : x)) : [...ctx.messages, m];
    return { ...ctx, messages: ordered(messages) };
}

/** The context without message `id` (a root takes its replies along). */
export function removeMessage(ctx: AnnotationContext, id: string): AnnotationContext {
    return { ...ctx, messages: ctx.messages.filter(m => m.id !== id && m.parent_id !== id) };
}

const isRecord = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);

function normalizeItem(raw: unknown): AnnotationContextItem | null {
    if (!isRecord(raw)) return null;
    const path = str(raw.path), rendererId = str(raw.renderer_id), label = str(raw.label), text = str(raw.text);
    if (!path || rendererId === null || label === null || text === null) return null;
    return { path, renderer_id: rendererId, label, text, anchor: isRecord(raw.anchor) ? raw.anchor : null };
}

function normalizeMessage(raw: unknown): AnnotationMessage | null {
    if (!isRecord(raw)) return null;
    const id = str(raw.id), contextId = str(raw.context_id), body = str(raw.body), createdAt = str(raw.created_at);
    if (!id || !contextId || body === null || !createdAt) return null;
    if (raw.author !== 'user' && raw.author !== 'run') return null;
    if (raw.kind !== 'message' && raw.kind !== 'prompt') return null;
    if (raw.mode !== 'queued' && raw.mode !== 'interrupt') return null;
    if (typeof raw.position !== 'number' || typeof raw.revision !== 'number') return null;
    return {
        id,
        context_id: contextId,
        parent_id: str(raw.parent_id),
        author: raw.author,
        author_label: str(raw.author_label),
        kind: raw.kind,
        body,
        context_items: Array.isArray(raw.context_items)
            ? raw.context_items.map(normalizeItem).filter((x): x is AnnotationContextItem => x !== null) : [],
        refs: Array.isArray(raw.refs) ? raw.refs.filter((r): r is string => typeof r === 'string') : [],
        mode: raw.mode,
        position: raw.position,
        held: raw.held === true,
        revision: raw.revision,
        created_at: createdAt,
        edited_at: str(raw.edited_at),
        read_at: str(raw.read_at),
    };
}

/** A context from the wire, or null when it is not one; malformed messages
 *  and context items are dropped. */
export function normalizeContext(raw: unknown): AnnotationContext | null {
    if (!isRecord(raw)) return null;
    const id = str(raw.id), cellId = str(raw.cell_id);
    if (!id || !cellId || typeof raw.rev !== 'number' || !Array.isArray(raw.messages)) return null;
    return {
        id,
        cell_id: cellId,
        run_id: str(raw.run_id),
        label: str(raw.label),
        bar_on: raw.bar_on === true,
        listening: raw.listening === true,
        interrupt_seq: typeof raw.interrupt_seq === 'number' ? raw.interrupt_seq : 0,
        rev: raw.rev,
        sealed: raw.sealed === true,
        messages: raw.messages.map(normalizeMessage).filter((m): m is AnnotationMessage => m !== null),
    };
}

/** A message from the wire, or null. */
export function normalizeAnnotationMessage(raw: unknown): AnnotationMessage | null {
    return normalizeMessage(raw);
}

const TOUCH_OPS: ReadonlySet<string> = new Set(['create', 'edit', 'delete']);

/** A file touch from the wire, or null. */
export function normalizeTouch(raw: unknown): FileTouch | null {
    if (!isRecord(raw)) return null;
    const at = str(raw.at), path = str(raw.path), op = str(raw.op);
    if (!at || !path || !op || !TOUCH_OPS.has(op) || raw.version_id === undefined || raw.version_id === null) return null;
    return { at, path, op: op as FileTouch['op'], folder: raw.folder === true, version_id: String(raw.version_id) };
}

/** The touches a live commit row carries (`payload.changes`, its version
 *  id `payload.cell_id`); bad entries are dropped. */
export function touchesFromCommit(payload: Record<string, unknown>, createdAt: string): FileTouch[] {
    if (!Array.isArray(payload.changes) || payload.cell_id === undefined || payload.cell_id === null) return [];
    const versionId = String(payload.cell_id);
    return payload.changes
        .map(change => normalizeTouch(isRecord(change) ? { ...change, at: createdAt, version_id: versionId } : null))
        .filter((t): t is FileTouch => t !== null);
}

const touchKey = (t: FileTouch) => `${t.version_id}\0${t.path}`;

/** Both lists, one touch per (version, path), oldest first (then by path).
 *  The SAME list when `next` adds nothing. */
export function mergeTouches(prev: readonly FileTouch[], next: readonly FileTouch[]): FileTouch[] {
    const seen = new Set(prev.map(touchKey));
    const fresh = next.filter(t => {
        const key = touchKey(t);
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    });
    if (fresh.length === 0) return prev as FileTouch[];
    return [...prev, ...fresh].sort((a, b) => serverTime(a.at) - serverTime(b.at) || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
}

/** How a finished run ended, in a word or two. */
export function finishedStatusLabel(status: string): string {
    if (status === 'completed') return 'Completed';
    if (isTerminatedStatus(status)) return 'Stopped';
    return failureStatusLabel(status);
}

/** What a live run is doing, for its drilldown's phase line. */
export function phaseLine(state: DotStatus): { phase: 'working' | 'waiting' | 'stopping'; text: string } {
    switch (state) {
        case 'spinning_up': return { phase: 'working', text: 'Starting' };
        case 'running': return { phase: 'working', text: 'Working' };
        case 'awaiting_approval': return { phase: 'waiting', text: 'Waiting for your approval' };
        case 'awaiting_input': return { phase: 'waiting', text: 'Waiting for messages' };
        case 'stopping': return { phase: 'stopping', text: 'Stopping' };
    }
}

/** A renderer's answer as a message's context item. */
export function contextItemFrom(r: { context: { label: string; text: string; anchor?: Record<string, unknown> }; path: string; rendererId: string }): AnnotationContextItem {
    return { path: r.path, renderer_id: r.rendererId, label: r.context.label, text: r.context.text, anchor: r.context.anchor ?? null };
}

/** Two context items attach the same part of the same file. */
export function sameContextItem(a: AnnotationContextItem, b: AnnotationContextItem): boolean {
    return a.path === b.path && a.text === b.text && JSON.stringify(a.anchor) === JSON.stringify(b.anchor);
}
