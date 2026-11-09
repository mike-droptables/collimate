import React, { useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { FilePen, FilePlus, FileX, Folder, FolderPlus, FolderX, Plus, type LucideIcon } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { PooledIframeConcealContext, usePooledIframeConcealed } from '@/contexts/RendererConcealContext';
import { btn, chip } from '@/components/ui/formStyles';
import { serverTime, type AnnotationContext, type FileTouch, type Thread, type TimelineItem } from '@/lib/annotations';
import type { DraftScope } from '@/lib/annotationDraft';
import { clockOf } from '@/lib/cellLog';
import { revealCellFile } from '@/lib/revealFile';
import { replacementNote } from '@/lib/replacements';
import { AnnotationMessage } from './AnnotationMessage';
import { AnnotationComposer, type AnnotationComposerHandle } from './AnnotationComposer';
import { QueueSection } from './QueueBar';

/** The view is "at the bottom" this close to it: new items keep it there. */
const TAIL_SLACK = 24;

const TOUCH: Record<FileTouch['op'], { verb: string; file: LucideIcon; folder: LucideIcon }> = {
    create: { verb: 'Created', file: FilePlus, folder: FolderPlus },
    edit: { verb: 'Edited', file: FilePen, folder: Folder },
    delete: { verb: 'Deleted', file: FileX, folder: FolderX },
};

/** The view's composer, placed by the timeline: the add row at its end,
 *  or, while the draft is open, the composer there (or in place of the
 *  message it edits). */
export interface TimelineComposer {
    scope: DraftScope;
    contextId: string;
    context: AnnotationContext | undefined;
    barOn: boolean;
    /** The run is stopping: Send is off. */
    stopping: boolean;
    maximized: boolean;
    /** The add row's words: "Add message" (a run), "Add note" (All). */
    addLabel: string;
}

export interface AnnotationTimelineHandle {
    /** Put the keyboard in the composer, opening it first. */
    focusComposer: () => void;
}

export interface AnnotationTimelineProps {
    cellId: string;
    channel: string;
    items: readonly TimelineItem[];
    /** A run's queue, below the history and the phase line: all of it,
     *  with the messages a filter shows (all when `shown` is absent). */
    queue?: { contextId: string; unheld: readonly Thread[]; held: readonly Thread[]; shown?: ReadonlySet<string>; barOn: boolean };
    readOnly: boolean;
    draftKey: string;
    /** The composer (none while read-only). */
    composer?: TimelineComposer;
    /** The run's phase line. */
    phase?: ReactNode;
    emptyText: string;
    /** Who reads a context's messages (for the read lock). */
    readerOf: (contextId: string) => string;
    ref?: React.Ref<AnnotationTimelineHandle>;
}

function TouchRow({ cellId, touch, tag }: { cellId: string; touch: FileTouch; tag: string | null }) {
    const look = TOUCH[touch.op];
    const Icon = touch.folder ? look.folder : look.file;
    const opens = touch.op !== 'delete' && !touch.folder;
    return (
        <div
            data-testid="file-touch-row"
            data-op={touch.op}
            data-path={touch.path}
            data-folder={touch.folder}
            className="flex items-center gap-1.5 min-w-0 px-2 py-0.5 text-[0.75rem] text-ink-2"
        >
            <Icon aria-hidden className="w-3.5 h-3.5 flex-none text-ink-3" strokeWidth={1.6} />
            <span className="flex-none">{look.verb}</span>
            {opens ? (
                <button
                    type="button"
                    onClick={() => revealCellFile(cellId, touch.path, { newTab: true })}
                    title={`Open ${touch.path}`}
                    className="min-w-0 truncate font-mono text-[0.6875rem] text-ink underline decoration-line-2 underline-offset-2 hover:decoration-ink-2"
                >
                    {touch.path}
                </button>
            ) : (
                <span className="min-w-0 truncate font-mono text-[0.6875rem]">{touch.path}</span>
            )}
            {touch.replaced && (
                <span data-testid="file-touch-replaced" className="min-w-0 truncate text-brass">
                    {replacementNote(touch.replaced)}
                </span>
            )}
            {tag && <span className={chip}>{tag}</span>}
            <span className="ml-auto pl-2 flex-none font-mono text-[0.6875rem] text-ink-3">{clockOf(serverTime(touch.at))}</span>
        </div>
    );
}

/** Scroll the log just enough to show its inline composer whole (its top
 *  first, when it is taller than the log). */
function revealComposer(root: HTMLElement | null) {
    const box = root?.querySelector<HTMLElement>('[data-testid="annotation-composer"]');
    if (!root || !box || box.dataset.expanded === 'true') return;
    const r = root.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    if (b.bottom > r.bottom) root.scrollTop += Math.min(b.bottom - r.bottom, b.top - r.top);
    else if (b.top < r.top) root.scrollTop -= r.top - b.top;
}

/** The inline composer's editor box reaches past the log's visible rect.
 *  Its editor may be a pooled frame, placed over the box's whole rect from
 *  outside the log's DOM, so nothing clips it there: it would paint over
 *  (and take the clicks of) the rail, the options or whatever is above or
 *  below the pane. Expanded, the composer covers the view, outside the
 *  scroll. */
function composerClipped(root: HTMLElement | null): boolean {
    const composer = root?.querySelector<HTMLElement>('[data-testid="annotation-composer"]');
    const box = composer?.querySelector<HTMLElement>('[data-annotation-composer-box]');
    if (!root || !composer || !box || composer.dataset.expanded === 'true') return false;
    const r = root.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    return b.top < r.top - 0.5 || b.bottom > r.bottom + 0.5;
}

/** One thread as the tail sees it: a reply landing in it (a run answers
 *  inside the thread it was asked in) changes it. */
const threadTail = (t: Thread) => `${t.root.id}:${t.replies.length}:${t.replies.at(-1)?.id ?? ''}`;

/**
 * An annotation pane timeline: messages (with their replies) and the files
 * the runs touched, in time order, newest at the bottom, then the run's
 * phase line and its queue, then the composer's add row, or, while the
 * draft is open, the composer itself (in place of the message it edits when
 * that message is shown). Opening the composer (the add row, a reply, an
 * edit) puts the keyboard in it and scrolls it into view; closing it (Esc,
 * a send) hands the keyboard to the add row. It follows its tail while
 * scrolled to the bottom, and wakes the guest pool as it scrolls under an
 * open composer (its editor may be a pooled frame).
 */
export function AnnotationTimeline({ cellId, channel, items, queue, readOnly, draftKey, composer, phase, emptyText, readerOf, ref }: AnnotationTimelineProps) {
    const rootRef = useRef<HTMLDivElement>(null);
    const atBottom = useRef(true);
    const composerRef = useRef<AnnotationComposerHandle>(null);
    const addRef = useRef<HTMLButtonElement>(null);
    const refocusAdd = useRef(false);
    const { invalidate } = useGuestPool();
    const draft = useCollimateStore(s => s.composerDrafts[draftKey]);
    const writable = !!composer && !readOnly;
    const open = writable && !!draft?.open;
    const parentConceal = usePooledIframeConcealed();

    // The pooled frame is not clipped by the log: hide it while its box is
    // not wholly in view (checked on every commit, scroll and log resize).
    const [clipped, setClipped] = useState(false);
    const clippedRef = useRef(false);
    const checkClip = useCallback(() => {
        const next = composerClipped(rootRef.current);
        if (next === clippedRef.current) return;
        clippedRef.current = next;
        setClipped(next);
    }, []);
    useLayoutEffect(checkClip);
    useEffect(() => {
        const root = rootRef.current;
        if (!open || !root || typeof ResizeObserver === 'undefined') return;
        const observer = new ResizeObserver(checkClip);
        observer.observe(root);
        return () => observer.disconnect();
    }, [open, checkClip]);

    const queueShown = queue ? [...queue.unheld, ...queue.held].filter(t => !queue.shown || queue.shown.has(t.root.id)) : [];
    // What grows the timeline at its tail: an item, a reply in any thread,
    // a message of the queue.
    const tail = [...items.map(item => (item.kind === 'thread' ? threadTail(item.thread) : item.key)), '|', ...queueShown.map(threadTail)].join(' ');
    useLayoutEffect(() => {
        const root = rootRef.current;
        if (root && atBottom.current) root.scrollTop = root.scrollHeight;
    }, [tail]);

    // The message being edited, when it is shown: the composer takes its place.
    const editingId = open ? draft?.editing?.id ?? null : null;
    const shown = (t: Thread) => t.root.id === editingId || t.replies.some(r => r.id === editingId);
    const inPlace = !!editingId && (items.some(item => item.kind === 'thread' && shown(item.thread)) || queueShown.some(shown));

    useImperativeHandle(ref, () => ({
        focusComposer: () => {
            if (!writable) return;
            if (useCollimateStore.getState().composerDrafts[draftKey]?.open && composerRef.current) {
                composerRef.current.focus();
                revealComposer(rootRef.current);
            } else {
                useCollimateStore.getState().setComposerDraft(draftKey, { open: true });
            }
        },
    }), [writable, draftKey]);

    // Opening the composer, or loading another reply or edit into it, puts
    // the keyboard in it; closing it hands the keyboard to the add row when
    // it was in the composer (it unmounted with it) or Esc closed it. What
    // the timeline mounts with takes nothing.
    const signature = open ? `${draft?.editing?.id ?? ''}|${draft?.replyTo?.id ?? ''}` : null;
    const seen = useRef<string | null | undefined>(undefined);
    useEffect(() => {
        const was = seen.current;
        seen.current = signature;
        if (was === undefined || was === signature) return;
        if (signature !== null) {
            composerRef.current?.focus();
            revealComposer(rootRef.current);
            return;
        }
        const active = document.activeElement;
        if (refocusAdd.current || !active || active === document.body || rootRef.current?.contains(active)) {
            addRef.current?.focus({ preventScroll: true });
        }
        refocusAdd.current = false;
    }, [signature]);

    // Esc closes the inline composer to the add row, the draft text kept;
    // an edit is abandoned as its Cancel does (its text is the message's,
    // and the add row writes a new message, never that edit).
    const closeComposer = () => {
        refocusAdd.current = true;
        const store = useCollimateStore.getState();
        if (store.composerDrafts[draftKey]?.editing) store.resetComposerDraft(draftKey);
        else store.setComposerDraft(draftKey, { open: false });
    };

    const composerNode = open && composer ? (
        <PooledIframeConcealContext.Provider value={parentConceal || clipped}>
            <AnnotationComposer
                ref={composerRef}
                cellId={cellId}
                channel={channel}
                scope={composer.scope}
                contextId={composer.contextId}
                context={composer.context}
                barOn={composer.barOn}
                stopping={composer.stopping}
                maximized={composer.maximized}
                onEscape={closeComposer}
            />
        </PooledIframeConcealContext.Provider>
    ) : null;
    const inPlaceComposer = inPlace && composerNode && editingId ? { id: editingId, node: composerNode } : undefined;

    const message = (thread: Thread, contextId: string, tag: string | null, sendable: boolean, handle?: ReactNode) => (
        <AnnotationMessage
            cellId={cellId}
            channel={channel}
            thread={thread}
            tag={tag}
            sendable={sendable}
            readOnly={readOnly}
            draftKey={draftKey}
            reader={readerOf(contextId)}
            dragHandle={handle}
            composer={inPlaceComposer && shown(thread) ? inPlaceComposer : undefined}
        />
    );
    const queued = !!queue && (queueShown.length > 0 || queue.barOn);

    return (
        <div
            ref={rootRef}
            data-testid="annotation-timeline"
            role="log"
            tabIndex={-1}
            onScroll={(e) => {
                const el = e.currentTarget;
                atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight <= TAIL_SLACK;
                // The composer's editor may be a pooled frame over its box:
                // the box moved, so the pool repositions it.
                if (open) {
                    invalidate();
                    checkClip();
                }
            }}
            className="flex-1 min-h-0 overflow-y-auto px-2 py-1.5 outline-none"
        >
            {items.length === 0 && !queued && <div className="px-2 py-1 text-[0.78125rem] text-ink-3">{emptyText}</div>}
            {items.map(item => (item.kind === 'thread'
                ? <React.Fragment key={item.key}>{message(item.thread, item.contextId, item.tag, item.sendable)}</React.Fragment>
                : <TouchRow key={item.key} cellId={cellId} touch={item.touch} tag={item.tag} />))}
            {phase}
            {queue && (
                <QueueSection
                    cellId={cellId}
                    contextId={queue.contextId}
                    unheld={queue.unheld}
                    held={queue.held}
                    shown={queue.shown}
                    barOn={queue.barOn}
                    readOnly={readOnly}
                    render={(thread, handle) => message(thread, queue.contextId, null, false, handle)}
                />
            )}
            {open && !inPlace && composerNode}
            {composer && !readOnly && !open && (
                <button
                    ref={addRef}
                    type="button"
                    data-testid="composer-add"
                    onClick={() => useCollimateStore.getState().setComposerDraft(draftKey, { open: true })}
                    className={cn(btn, 'mt-1 w-full justify-start')}
                >
                    <Plus className="w-3.5 h-3.5" strokeWidth={1.6} />
                    {composer.addLabel}
                </button>
            )}
        </div>
    );
}
