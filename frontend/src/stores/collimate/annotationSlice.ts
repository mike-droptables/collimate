// The status pane's session state (components/runsPane): each cell's
// annotation contexts (its own and its runs'), the runs' file touches, the
// file last viewed in each cell, the runs that ended this session, what the
// pane shows, the composers' drafts and the Add annotation in flight. The
// contexts follow the channel stream's `annotations_changed` doorbell
// (refetch that context), resync on every stream connect, and only for
// cells this session has viewed.

import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';
import type {
    ComposerDraft, FinishedRun, PendingAnnotationRequest, PendingRunsPaneOpen, RunsPaneFocusRequest, RunsPaneView,
} from './types';
import {
    MAX_CONTEXT_ITEMS, applyOrderLocally, contextItemFrom, isLocked, mergeTouches, removeMessage, sameContextItem, upsertMessage,
    type AnnotationContext, type AnnotationContextItem, type AnnotationMessage, type FileTouch,
} from '@/lib/annotations';
import {
    ANNOTATION_REFRESH_CODES, annotationErrorCode, annotationErrorText, deleteAnnotation, fetchAnnotationContext,
    fetchCellAnnotations, fetchFileTouches, interruptAnnotations, orderAnnotations, patchAnnotation, postAnnotation,
    sendAnnotationToRun, setAnnotationBar,
} from '@/lib/api/annotations';
import { isCellDraft } from '@/lib/annotationDraft';
import { requestAnnotation, type AnnotationOutcome } from '@/lib/rendererAnnotation';
import { openRunsPane } from '@/lib/cellLayout';
import { ANNOTATION_NOTICE_MS, FINISHED_RUNS_PER_CELL, STATUS_VIEW, newDraft } from './annotationState';

export interface AnnotationSlice {
    /** Per cell, per context id (`'cell'` or a run id). */
    annotationContexts: Record<string, Record<string, AnnotationContext>>;
    /** Per cell: a write that failed, said once (it clears itself). */
    annotationNotice: Record<string, string>;
    /** Per run, oldest first. */
    fileTouches: Record<string, FileTouch[]>;
    /** Per cell: the file tab most recently active (Current file, and the
     *  file Add annotation picks from). */
    lastFileTab: Record<string, string>;
    /** Per cell: the runs that ended this session, newest first. */
    finishedRuns: Record<string, FinishedRun[]>;
    /** Per cell: what the status pane shows (missing = the status view). */
    runsPaneView: Record<string, RunsPaneView>;
    /** Per cell: the latest request for the pane's view to take the keys. */
    runsPaneFocus: Record<string, RunsPaneFocusRequest>;
    pendingRunsPaneOpen: PendingRunsPaneOpen | null;
    /** Per draft key (lib/annotationDraft draftKey). */
    composerDrafts: Record<string, ComposerDraft>;
    /** Per cell: the Add annotation in flight. */
    annotationRequests: Record<string, PendingAnnotationRequest>;

    /** Fetch the cell's contexts (its own and its live runs'). */
    loadCellAnnotations: (cellId: string) => Promise<void>;
    /** Fetch one context (one request in flight; a call meanwhile fetches
     *  once more after it). */
    refreshAnnotationContext: (cellId: string, contextId: string) => Promise<void>;
    /** The `annotations_changed` doorbell. */
    annotationsChanged: (cellId: string, contextId: string, rev: number) => void;
    /** Refetch every viewed cell of the active channel (a stream connect). */
    resyncAnnotations: () => void;
    /** Post (or save the edit of) a draft; true on success. */
    sendDraft: (cellId: string, key: string, contextId: string, text: string) => Promise<boolean>;
    deleteMessage: (cellId: string, m: AnnotationMessage) => Promise<void>;
    reorderContextQueue: (cellId: string, contextId: string, ids: string[], heldFrom?: number) => Promise<void>;
    setQueueBar: (cellId: string, contextId: string, on: boolean) => Promise<void>;
    interruptQueued: (cellId: string, contextId: string, ids: string[]) => Promise<void>;
    sendMessageToRun: (cellId: string, messageId: string, runId: string) => Promise<void>;
    setAnnotationNotice: (cellId: string, text: string | null) => void;
    addFileTouches: (runId: string, touches: readonly FileTouch[]) => void;
    loadFileTouches: (channel: string, runId: string) => Promise<void>;
    noteFileTab: (cellId: string, path: string) => void;
    addFinishedRun: (run: FinishedRun) => void;
    /** Clear finished: drops the cell's finished runs with their contexts,
     *  touches and drafts (the cell's log keeps their lines). */
    clearFinishedRuns: (cellId: string) => void;
    setRunsPaneView: (cellId: string, view: RunsPaneView) => void;
    /** Open the cell's status pane on `view` and give it the keys; deferred
     *  (pendingRunsPaneOpen) while the cell is not reachable yet. */
    openRunsPaneOn: (cellId: string, view: RunsPaneView, opts?: { focus?: 'view' | 'composer'; channel?: string }) => void;
    setComposerDraft: (key: string, patch: Partial<ComposerDraft>) => void;
    /** Empty the draft (its editor remounts empty). */
    resetComposerDraft: (key: string) => void;
    loadDraftForEdit: (key: string, m: AnnotationMessage) => void;
    setDraftReply: (key: string, m: AnnotationMessage | null) => void;
    /** Add annotation: ask the file's renderer for the part to attach. */
    requestDraftContext: (cellId: string, key: string, path: string) => Promise<void>;
    cancelDraftContextRequest: (cellId: string) => void;
}

/** What a pick asks the user to do, shown over the page. */
const PICK_HINT = 'Click what to attach to your message · Esc cancels';

const basename = (path: string) => path.slice(path.lastIndexOf('/') + 1);
/** The link target the server reads back as a cell path (`body_refs`):
 *  the cell-relative path, percent-encoded where markdown needs it. */
const linkTarget = (path: string) => path.split('/').map(encodeURIComponent).join('/');
/** Whether `text` already links the cell file `path` (same decoded target). */
function bodyLinks(text: string, path: string): boolean {
    const re = /!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/g;
    for (const m of text.matchAll(re)) {
        let target = m[1];
        try { target = decodeURIComponent(target.split(/[#?]/, 1)[0]); } catch { continue; }
        if (target === path) return true;
    }
    return false;
}

/** An edit that can no longer be saved; its text stays in the composer. */
const EDIT_READ = 'The run read this message before your edit was saved; send it as a new message or discard it.';
const EDIT_GONE = 'The message you were editing is gone; send it as a new message or discard it.';

/** The cell's drafts that edit a message of these contexts, following it:
 *  the edit takes the message's revision (a reorder, an interrupt or the
 *  queue bar bumps it), and an edit of a message read or gone ends with a
 *  notice. Null when no draft changes. */
function followEdits(
    drafts: Record<string, ComposerDraft>, cellId: string, contexts: readonly AnnotationContext[],
): Record<string, ComposerDraft> | null {
    let next: Record<string, ComposerDraft> | null = null;
    for (const [key, draft] of Object.entries(drafts)) {
        const editing = draft.editing;
        if (!editing || !isCellDraft(key, cellId)) continue;
        const ctx = contexts.find(c => c.id === editing.contextId);
        if (!ctx) continue;
        const m = ctx.messages.find(x => x.id === editing.id);
        const patch: Partial<ComposerDraft> | null = !m || isLocked(m) ? { editing: null, notice: m ? EDIT_READ : EDIT_GONE }
            : m.revision !== editing.revision ? { editing: { ...editing, revision: m.revision } } : null;
        if (!patch) continue;
        next ??= { ...drafts };
        next[key] = { ...draft, ...patch };
    }
    return next;
}

/** The draft once `sent` went out with `text`: what it sent leaves (the
 *  text unless retyped since, the attachments it carried, the reply or the
 *  edit it was, the mode back to queued) and the next post takes a new key;
 *  what came meanwhile stays (text typed, a pick that finished, another
 *  reply, a newer edit). */
function afterSend(cur: ComposerDraft, sent: ComposerDraft, text: string): ComposerDraft {
    if (cur.editing && cur.editing.id !== sent.editing?.id) return { ...cur, sending: false };
    return {
        ...cur,
        text: cur.text.trim() === text ? '' : cur.text,
        contextItems: cur.contextItems.filter(item => !sent.contextItems.some(x => sameContextItem(x, item))),
        mode: cur.mode === sent.mode ? 'queued' : cur.mode,
        replyTo: cur.replyTo && cur.replyTo.id !== sent.replyTo?.id ? cur.replyTo : null,
        editing: null,
        operationId: crypto.randomUUID(),
        failedRequest: null,
        notice: null,
        sending: false,
    };
}

// Per cell, the cell listing in flight; per (cell, context), the refetch in
// flight and whether another was asked for meanwhile; per run, the touches
// fetch in flight; per cell, the notice's clear timer.
const cellLoads = new Map<string, Promise<void>>();
const refreshes = new Map<string, { again: boolean; promise: Promise<void> }>();
const touchLoads = new Map<string, Promise<void>>();
const noticeTimers = new Map<string, ReturnType<typeof setTimeout>>();

/** The state without what these runs of the cell held: their contexts,
 *  touches and drafts, and a drilldown naming one of them. */
function withoutRuns(s: CollimateState, cellId: string, runIds: ReadonlySet<string>): Partial<CollimateState> {
    if (runIds.size === 0) return {};
    const contexts = { ...(s.annotationContexts[cellId] ?? {}) };
    for (const id of runIds) delete contexts[id];
    const fileTouches = { ...s.fileTouches };
    const composerDrafts = { ...s.composerDrafts };
    for (const id of runIds) {
        delete fileTouches[id];
        delete composerDrafts[`${cellId}:run:${id}`];
    }
    const view = s.runsPaneView[cellId];
    const runsPaneView = view?.view === 'run' && runIds.has(view.runId)
        ? { ...s.runsPaneView, [cellId]: STATUS_VIEW } : s.runsPaneView;
    return {
        annotationContexts: s.annotationContexts[cellId] ? { ...s.annotationContexts, [cellId]: contexts } : s.annotationContexts,
        fileTouches, composerDrafts, runsPaneView,
    };
}

type AnnotationSliceCreator = StateCreator<CollimateState, [['zustand/subscribeWithSelector', never]], [], AnnotationSlice>;

// A function declaration, not a const: rendererAnnotation (imported here)
// reaches the store through revealFile, so the store may be created while
// this module is still evaluating; a declaration is callable by then.
export function createAnnotationSlice(...[set, get]: Parameters<AnnotationSliceCreator>): AnnotationSlice {
    const channel = () => get().activeChannel?.id ?? null;

    const putContext = (cellId: string, ctx: AnnotationContext) => set(prev => {
        const held = prev.annotationContexts[cellId]?.[ctx.id];
        if (held && held.rev > ctx.rev) return {};
        const composerDrafts = followEdits(prev.composerDrafts, cellId, [ctx]);
        return {
            annotationContexts: { ...prev.annotationContexts, [cellId]: { ...(prev.annotationContexts[cellId] ?? {}), [ctx.id]: ctx } },
            ...(composerDrafts ? { composerDrafts } : {}),
        };
    });

    const editContext = (cellId: string, contextId: string, fn: (ctx: AnnotationContext) => AnnotationContext): boolean => {
        const held = get().annotationContexts[cellId]?.[contextId];
        if (!held) return false;
        set(prev => ({ annotationContexts: { ...prev.annotationContexts, [cellId]: { ...prev.annotationContexts[cellId], [contextId]: fn(held) } } }));
        return true;
    };

    const patchDraft = (key: string, patch: Partial<ComposerDraft>) => set(prev => ({
        composerDrafts: { ...prev.composerDrafts, [key]: { ...(prev.composerDrafts[key] ?? newDraft()), ...patch } },
    }));

    /** A failed write: its sentence, then a refetch when the context
     *  changed elsewhere. */
    const refused = (cellId: string, contextId: string, error: unknown) => {
        get().setAnnotationNotice(cellId, annotationErrorText(error));
        const code = annotationErrorCode(error);
        if (code && ANNOTATION_REFRESH_CODES.has(code)) void get().refreshAnnotationContext(cellId, contextId);
    };

    return {
        annotationContexts: {},
        annotationNotice: {},
        fileTouches: {},
        lastFileTab: {},
        finishedRuns: {},
        runsPaneView: {},
        runsPaneFocus: {},
        pendingRunsPaneOpen: null,
        composerDrafts: {},
        annotationRequests: {},

        loadCellAnnotations: (cellId) => {
            const ch = channel();
            if (!ch) return Promise.resolve();
            const inFlight = cellLoads.get(cellId);
            if (inFlight) return inFlight;
            const load = (async () => {
                try {
                    const contexts = await fetchCellAnnotations(ch, cellId);
                    set(prev => {
                        const held = prev.annotationContexts[cellId] ?? {};
                        const finished = new Set((prev.finishedRuns[cellId] ?? []).map(r => r.runId));
                        const next: Record<string, AnnotationContext> = {};
                        for (const [id, ctx] of Object.entries(held)) if (finished.has(id)) next[id] = ctx;
                        for (const ctx of contexts) {
                            const mine = held[ctx.id];
                            next[ctx.id] = mine && mine.rev > ctx.rev ? mine : ctx;
                        }
                        const composerDrafts = followEdits(prev.composerDrafts, cellId, Object.values(next));
                        return { annotationContexts: { ...prev.annotationContexts, [cellId]: next }, ...(composerDrafts ? { composerDrafts } : {}) };
                    });
                } catch (error) {
                    console.warn('loading the cell\'s annotations failed', cellId, error);
                } finally {
                    cellLoads.delete(cellId);
                }
            })();
            cellLoads.set(cellId, load);
            return load;
        },

        refreshAnnotationContext: (cellId, contextId) => {
            const ch = channel();
            if (!ch) return Promise.resolve();
            const key = `${cellId}\0${contextId}`;
            const flight = refreshes.get(key);
            if (flight) {
                flight.again = true;
                return flight.promise;
            }
            const entry = { again: false, promise: Promise.resolve() };
            entry.promise = (async () => {
                try {
                    do {
                        entry.again = false;
                        const ctx = await fetchAnnotationContext(ch, cellId, contextId);
                        if (ctx) putContext(cellId, ctx);
                        else if (get().annotationContexts[cellId]?.[contextId]) {
                            set(prev => {
                                const contexts = { ...prev.annotationContexts[cellId] };
                                delete contexts[contextId];
                                return { annotationContexts: { ...prev.annotationContexts, [cellId]: contexts } };
                            });
                        }
                    } while (entry.again);
                } catch (error) {
                    console.warn('refreshing an annotation context failed', cellId, contextId, error);
                } finally {
                    refreshes.delete(key);
                }
            })();
            refreshes.set(key, entry);
            return entry.promise;
        },

        annotationsChanged: (cellId, contextId, rev) => {
            const contexts = get().annotationContexts[cellId];
            if (!contexts) return;
            const held = contexts[contextId];
            if (held && held.rev >= rev) return;
            void get().refreshAnnotationContext(cellId, contextId);
        },

        resyncAnnotations: () => {
            const ch = channel();
            if (!ch) return;
            for (const cellId of Object.keys(get().annotationContexts)) {
                if (get().locateCell(cellId)?.channel === ch) void get().loadCellAnnotations(cellId);
            }
        },

        sendDraft: async (cellId, key, contextId, text) => {
            const ch = channel();
            if (!ch) return false;
            const draft = get().composerDrafts[key] ?? newDraft();
            const target = draft.editing?.contextId ?? draft.replyTo?.contextId ?? contextId;
            const ctx = get().annotationContexts[cellId]?.[target];
            const held = !draft.replyTo && !!ctx?.bar_on && draft.below && draft.mode === 'queued';
            const request = {
                context_id: target,
                ...(draft.replyTo ? { parent_id: draft.replyTo.id } : {}),
                body: text,
                context_items: draft.contextItems,
                mode: draft.mode,
                ...(held ? { held: true } : {}),
            };
            const requestKey = JSON.stringify(request);
            // Only the very request that failed retries under its key.
            const operationId = draft.editing || draft.failedRequest === null || draft.failedRequest === requestKey
                ? draft.operationId : crypto.randomUUID();
            patchDraft(key, { sending: true, notice: null, operationId });
            try {
                const posted = draft.editing
                    ? await patchAnnotation(ch, cellId, draft.editing.id, {
                        expected_revision: draft.editing.revision, body: text, context_items: draft.contextItems, mode: draft.mode,
                    })
                    : await postAnnotation(ch, cellId, { operation_id: operationId, ...request });
                if (!editContext(cellId, posted.context_id, c => upsertMessage(c, posted))) {
                    void get().refreshAnnotationContext(cellId, posted.context_id);
                }
                set(prev => ({ composerDrafts: { ...prev.composerDrafts, [key]: afterSend(prev.composerDrafts[key] ?? newDraft(), draft, text) } }));
                return true;
            } catch (error) {
                const code = annotationErrorCode(error);
                set(prev => {
                    const cur = prev.composerDrafts[key] ?? newDraft();
                    // The post's key stays for a retry of this request, unless
                    // the server holds it for another (then it is spent).
                    const keyed = !draft.editing && cur.operationId === operationId;
                    const retry = !keyed ? {} : code === 'operation_reused'
                        ? { operationId: crypto.randomUUID(), failedRequest: null } : { failedRequest: requestKey };
                    return { composerDrafts: { ...prev.composerDrafts, [key]: { ...cur, ...retry, sending: false, notice: annotationErrorText(error) } } };
                });
                if (code && ANNOTATION_REFRESH_CODES.has(code)) void get().refreshAnnotationContext(cellId, target);
                return false;
            }
        },

        deleteMessage: async (cellId, m) => {
            const ch = channel();
            if (!ch) return;
            try {
                await deleteAnnotation(ch, cellId, m.id, m.revision);
                editContext(cellId, m.context_id, ctx => removeMessage(ctx, m.id));
            } catch (error) {
                refused(cellId, m.context_id, error);
                void get().refreshAnnotationContext(cellId, m.context_id);
            }
        },

        reorderContextQueue: async (cellId, contextId, ids, heldFrom) => {
            const ch = channel();
            if (!ch) return;
            editContext(cellId, contextId, ctx => applyOrderLocally(ctx, ids, heldFrom));
            try {
                putContext(cellId, await orderAnnotations(ch, cellId, contextId, ids, heldFrom));
            } catch (error) {
                refused(cellId, contextId, error);
                void get().refreshAnnotationContext(cellId, contextId);
            }
        },

        setQueueBar: async (cellId, contextId, on) => {
            const ch = channel();
            if (!ch) return;
            try {
                putContext(cellId, await setAnnotationBar(ch, cellId, contextId, on));
            } catch (error) {
                refused(cellId, contextId, error);
            }
        },

        interruptQueued: async (cellId, contextId, ids) => {
            const ch = channel();
            if (!ch || ids.length === 0) return;
            try {
                putContext(cellId, await interruptAnnotations(ch, cellId, contextId, ids));
            } catch (error) {
                refused(cellId, contextId, error);
            }
        },

        sendMessageToRun: async (cellId, messageId, runId) => {
            const ch = channel();
            if (!ch) return;
            try {
                await sendAnnotationToRun(ch, cellId, messageId, runId);
            } catch (error) {
                get().setAnnotationNotice(cellId, annotationErrorText(error));
            }
            await Promise.all([
                get().refreshAnnotationContext(cellId, 'cell'),
                get().refreshAnnotationContext(cellId, runId),
            ]);
        },

        setAnnotationNotice: (cellId, text) => {
            const timer = noticeTimers.get(cellId);
            if (timer) clearTimeout(timer);
            noticeTimers.delete(cellId);
            set(prev => {
                const annotationNotice = { ...prev.annotationNotice };
                if (text) annotationNotice[cellId] = text;
                else delete annotationNotice[cellId];
                return { annotationNotice };
            });
            if (text) {
                noticeTimers.set(cellId, setTimeout(() => {
                    noticeTimers.delete(cellId);
                    if (get().annotationNotice[cellId] === text) get().setAnnotationNotice(cellId, null);
                }, ANNOTATION_NOTICE_MS));
            }
        },

        addFileTouches: (runId, touches) => {
            const prev = get().fileTouches[runId];
            if (touches.length === 0) return;
            const merged = mergeTouches(prev ?? [], touches);
            if (prev && merged === prev) return;
            set(s => ({ fileTouches: { ...s.fileTouches, [runId]: merged } }));
        },

        loadFileTouches: (ch, runId) => {
            const inFlight = touchLoads.get(runId);
            if (inFlight) return inFlight;
            const load = (async () => {
                try {
                    const touches = await fetchFileTouches(ch, runId);
                    set(s => {
                        const prev = s.fileTouches[runId];
                        const merged = mergeTouches(prev ?? [], touches);
                        return prev && merged === prev ? {} : { fileTouches: { ...s.fileTouches, [runId]: merged } };
                    });
                } catch (error) {
                    console.warn('loading the run\'s file touches failed', runId, error);
                } finally {
                    touchLoads.delete(runId);
                }
            })();
            touchLoads.set(runId, load);
            return load;
        },

        noteFileTab: (cellId, path) => {
            if (get().lastFileTab[cellId] === path) return;
            set(prev => ({ lastFileTab: { ...prev.lastFileTab, [cellId]: path } }));
        },

        addFinishedRun: (run) => {
            set(prev => {
                const list = [run, ...(prev.finishedRuns[run.cellId] ?? []).filter(r => r.runId !== run.runId)];
                const dropped = new Set(list.slice(FINISHED_RUNS_PER_CELL).map(r => r.runId));
                return {
                    ...withoutRuns(prev, run.cellId, dropped),
                    finishedRuns: { ...prev.finishedRuns, [run.cellId]: list.slice(0, FINISHED_RUNS_PER_CELL) },
                };
            });
        },

        clearFinishedRuns: (cellId) => {
            set(prev => {
                const runIds = new Set((prev.finishedRuns[cellId] ?? []).map(r => r.runId));
                const finishedRuns = { ...prev.finishedRuns };
                delete finishedRuns[cellId];
                return { ...withoutRuns(prev, cellId, runIds), finishedRuns };
            });
        },

        setRunsPaneView: (cellId, view) => set(prev => ({ runsPaneView: { ...prev.runsPaneView, [cellId]: view } })),

        openRunsPaneOn: (cellId, view, opts = {}) => {
            const s = get();
            const here = s.activeChannel?.id ?? null;
            const focus = opts.focus ?? 'view';
            const ready = (!opts.channel || opts.channel === here)
                && (!!s.cellLayouts[cellId] || (s.cellDisplaysReady && !!s.locateCell(cellId)));
            if (!ready) {
                const target = opts.channel ?? here;
                if (target) set({ pendingRunsPaneOpen: { channel: target, cellId, view, focus, at: Date.now() } });
                return;
            }
            set(prev => ({
                runsPaneView: { ...prev.runsPaneView, [cellId]: view },
                runsPaneFocus: { ...prev.runsPaneFocus, [cellId]: { target: focus, seq: (prev.runsPaneFocus[cellId]?.seq ?? 0) + 1, at: Date.now() } },
                pendingRunsPaneOpen: prev.pendingRunsPaneOpen?.cellId === cellId ? null : prev.pendingRunsPaneOpen,
            }));
            get().updateRunsPane(cellId, openRunsPane);
        },

        setComposerDraft: (key, patch) => patchDraft(key, patch),

        resetComposerDraft: (key) => set(prev => {
            const cur = prev.composerDrafts[key] ?? newDraft();
            return { composerDrafts: { ...prev.composerDrafts, [key]: { ...newDraft(), below: cur.below, expanded: cur.expanded, generation: cur.generation + 1 } } };
        }),

        loadDraftForEdit: (key, m) => set(prev => {
            const cur = prev.composerDrafts[key] ?? newDraft();
            return {
                composerDrafts: {
                    ...prev.composerDrafts,
                    [key]: {
                        ...cur, text: m.body, contextItems: m.context_items, mode: m.mode, replyTo: null,
                        editing: { id: m.id, contextId: m.context_id, revision: m.revision },
                        generation: cur.generation + 1, notice: null, operationId: crypto.randomUUID(), failedRequest: null,
                    },
                },
            };
        }),

        setDraftReply: (key, m) => set(prev => {
            const cur = prev.composerDrafts[key] ?? newDraft();
            const replyTo = m ? {
                id: m.parent_id ?? m.id,
                contextId: m.context_id,
                author: m.author === 'user' ? 'You' : m.author_label ?? 'the run',
                excerpt: m.body.replace(/\s+/g, ' ').trim().slice(0, 80),
            } : null;
            return { composerDrafts: { ...prev.composerDrafts, [key]: { ...cur, replyTo, editing: null, notice: null } } };
        }),

        requestDraftContext: async (cellId, key, path) => {
            /** `item` onto the draft's attachments, once, within the API's bound. */
            const attach = (draftKey: string, item: AnnotationContextItem) => {
                const draft = get().composerDrafts[draftKey] ?? newDraft();
                if (draft.contextItems.some(x => sameContextItem(x, item))) return;
                if (draft.contextItems.length >= MAX_CONTEXT_ITEMS) {
                    patchDraft(draftKey, { notice: `At most ${MAX_CONTEXT_ITEMS} attachments per message.` });
                    return;
                }
                patchDraft(draftKey, { contextItems: [...draft.contextItems, item], notice: null });
            };
            get().annotationRequests[cellId]?.controller.abort();
            const controller = new AbortController();
            set(prev => ({ annotationRequests: { ...prev.annotationRequests, [cellId]: { draftKey: key, path, controller } } }));
            let outcome: AnnotationOutcome;
            try {
                outcome = await requestAnnotation(cellId, path, 'pick', { signal: controller.signal, hint: PICK_HINT });
            } catch (error) {
                outcome = { status: 'error', message: String((error as Error)?.message ?? error) };
            }
            if (get().annotationRequests[cellId]?.controller !== controller) return;
            set(prev => {
                const annotationRequests = { ...prev.annotationRequests };
                delete annotationRequests[cellId];
                return { annotationRequests };
            });
            if (outcome.status === 'cancelled') return;
            if (outcome.status === 'unavailable') {
                // The file's view cannot pick a part (its renderer does not take
                // annotations, the view is locked or gone): link the whole file
                // instead, on its own line, so the message is written under it,
                // and attach what the view had selected as the link's block.
                set(prev => {
                    const cur = prev.composerDrafts[key] ?? newDraft();
                    if (bodyLinks(cur.text, path)) return { composerDrafts: { ...prev.composerDrafts, [key]: { ...cur, notice: null } } };
                    const head = cur.text.length === 0 || cur.text.endsWith('\n') ? cur.text : cur.text + '\n';
                    const text = `${head}[${basename(path)}](${linkTarget(path)})\n`;
                    return { composerDrafts: { ...prev.composerDrafts, [key]: { ...cur, text, generation: cur.generation + 1, notice: null } } };
                });
                const { view } = outcome;
                if (view && view.selection) {
                    attach(key, { path, renderer_id: view.rendererId, label: `${basename(path)} › selection`, text: view.selection, anchor: null });
                }
                return;
            }
            if (outcome.status !== 'ok') {
                patchDraft(key, { notice: outcome.message });
                return;
            }
            attach(key, contextItemFrom(outcome));
        },

        cancelDraftContextRequest: (cellId) => {
            const pending = get().annotationRequests[cellId];
            if (!pending) return;
            set(prev => {
                const annotationRequests = { ...prev.annotationRequests };
                delete annotationRequests[cellId];
                return { annotationRequests };
            });
            pending.controller.abort();
        },
    };
}
