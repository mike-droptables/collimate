import React, { useEffect, useImperativeHandle, useRef } from 'react';
import { Crosshair, ListFilter, Maximize2, Minimize2, Send, X, Zap } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { newDraft } from '@/stores/collimate/annotationState';
import { useCellSelected } from '@/contexts/CellSelectionContext';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { MAX_CONTEXT_ITEMS, queuedInterruptCandidates, type AnnotationContext } from '@/lib/annotations';
import { draftKey, type DraftScope } from '@/lib/annotationDraft';
import { hasPendingAnnotationPick } from '@/lib/rendererAnnotation';
import { keysInside } from '@/lib/selectionFocus';
import { basename } from '@/lib/fileTransfer';
import { btn, btnPrimary, chip, segmentOff, segmentOn } from '@/components/ui/formStyles';
import { DraftEditor, type DraftEditorHandle } from './DraftEditor';
import type { ComposerDraft } from '@/stores/collimate/types';

/** A draft with nothing in it yet (read when the store holds none). */
const EMPTY: ComposerDraft = newDraft();

const segment = 'h-[1.375rem] px-2 inline-flex items-center gap-1 text-[0.75rem]';
const tool = 'inline-flex items-center justify-center gap-1 h-[1.375rem] px-1.5 rounded-sm text-[0.75rem] text-ink-2 transition-colors hover:bg-sheet-2 hover:text-ink aria-disabled:opacity-45 aria-disabled:cursor-default aria-disabled:hover:bg-transparent aria-pressed:bg-sheet-2 aria-pressed:text-ink';

export interface AnnotationComposerHandle {
    focus: () => void;
}

export interface AnnotationComposerProps {
    cellId: string;
    channel: string;
    /** A run's drilldown or the All view (the cell's own context). */
    scope: DraftScope;
    contextId: string;
    context: AnnotationContext | undefined;
    barOn: boolean;
    /** The run is stopping: Send is off. */
    stopping: boolean;
    /** In the full-window view. */
    maximized: boolean;
    /** Esc with nothing to collapse: the view takes the keys back. */
    onEscape: () => void;
    ref?: React.Ref<AnnotationComposerHandle>;
}

/**
 * The status pane's composer: a message to a run (its drilldown) or a note
 * of the cell (All), written in the channel's `.md` editor (DraftEditor).
 * Queued or Interrupt (switching a run's composer to Interrupt offers to
 * interrupt the messages already queued), the queue bar and whether the
 * message goes above or below it, Add annotation (the file last viewed
 * answers with the part the user picks), expand, Send. Edit and reply load
 * into it with a banner. Esc collapses it, then hands the keys back; a
 * pending pick's own Esc wins over both.
 */
export function AnnotationComposer({ cellId, channel, scope, contextId, context, barOn, stopping, maximized, onEscape, ref }: AnnotationComposerProps) {
    const key = draftKey(cellId, scope);
    const draft = useCollimateStore(s => s.composerDrafts[key]) ?? EMPTY;
    const pending = useCollimateStore(s => (s.annotationRequests[cellId]?.draftKey === key ? s.annotationRequests[cellId] : null));
    const file = useCollimateStore(s => s.lastFileTab[cellId] ?? null);
    const active = useCellSelected();
    const { containerOf } = useGuestPool();
    const rootRef = useRef<HTMLDivElement>(null);
    const editorRef = useRef<DraftEditorHandle>(null);
    const store = useCollimateStore.getState;
    const runScope = scope.kind === 'run';

    useImperativeHandle(ref, () => ({ focus: () => { editorRef.current?.focus(); } }), []);

    const collapseOrLeave = () => {
        if (store().composerDrafts[key]?.expanded) store().setComposerDraft(key, { expanded: false });
        else onEscape();
    };
    const collapseRef = useRef(collapseOrLeave);
    useEffect(() => { collapseRef.current = collapseOrLeave; });

    // An iframe editor forwards its Esc on the window: take it before the
    // focused tab's own window listener would close that tab. Only this
    // composer's: every mounted one listens.
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            // A forwarded key is dispatched on the window itself, not on an element.
            if (e.key !== 'Escape' || e.target instanceof Node || hasPendingAnnotationPick()) return;
            const root = rootRef.current;
            if (!root || !keysInside(root, containerOf)) return;
            e.stopImmediatePropagation();
            e.preventDefault();
            collapseRef.current();
        };
        window.addEventListener('keydown', onKey, true);
        return () => window.removeEventListener('keydown', onKey, true);
    }, [containerOf]);

    // Leaving the composer abandons its pick.
    useEffect(() => () => {
        if (useCollimateStore.getState().annotationRequests[cellId]?.draftKey === key) {
            useCollimateStore.getState().cancelDraftContextRequest(cellId);
        }
    }, [cellId, key]);

    const chooseMode = async (mode: 'queued' | 'interrupt') => {
        if (mode === draft.mode) return;
        store().setComposerDraft(key, { mode, ...(mode === 'interrupt' ? { below: false } : {}) });
        if (mode !== 'interrupt' || !runScope || !context) return;
        const ids = queuedInterruptCandidates(context);
        if (ids.length === 0) return;
        const ok = await store().openConfirm({
            title: 'Interrupt',
            body: `Send the ${ids.length} queued ${ids.length === 1 ? 'message' : 'messages'} as interrupt too?`,
            confirmLabel: 'Yes', cancelLabel: 'Cancel', cellId,
        });
        if (ok) await store().interruptQueued(cellId, contextId, ids);
    };

    const send = async () => {
        if (draft.sending || stopping) return;
        store().cancelDraftContextRequest(cellId);
        const editor = editorRef.current;
        if (!editor) return;
        let text: string;
        try {
            text = (await editor.text()).trim();
        } catch (error) {
            store().setComposerDraft(key, { notice: `Still saving the draft: ${String((error as Error)?.message ?? error)}` });
            return;
        }
        if (!text) {
            store().setComposerDraft(key, { notice: 'Write a message first.' });
            return;
        }
        const sent = await store().sendDraft(cellId, key, contextId, text);
        const after = editorRef.current;
        if (!sent || !after) return;
        // Empty the editor only if it still holds what went out: text typed
        // while the message was on its way stays (a flush that fails defers
        // to the draft, which the send emptied only if it matched).
        const now = await after.text().then(t => t.trim(), () => (store().composerDrafts[key]?.text ? null : text));
        if (now === text) after.reset();
    };

    const addFull = draft.contextItems.length >= MAX_CONTEXT_ITEMS;
    const addDisabled = !file || !!pending || addFull;
    const addTitle = addFull ? `At most ${MAX_CONTEXT_ITEMS} attachments per message`
        : file ? `Pick from ${basename(file)}` : 'Open a file in this cell to pick from it';
    const notice = draft.notice ?? (stopping ? 'This run is stopping.' : null);

    return (
        <div
            ref={rootRef}
            data-testid="annotation-composer"
            data-expanded={draft.expanded}
            onKeyDown={(e) => {
                if (e.key !== 'Escape' || hasPendingAnnotationPick()) return;
                e.preventDefault();
                e.stopPropagation();
                collapseOrLeave();
            }}
            // Collapsed it takes at most a little over half the view, so a
            // short bottom band keeps some of its timeline.
            className={cn('flex flex-col min-h-0 border-t border-line bg-sheet', draft.expanded ? 'flex-1' : 'flex-none max-h-[55%]')}
        >
            {draft.editing && (
                <div data-testid="composer-editing" className="flex items-center gap-2 px-2 pt-1.5 text-[0.75rem] text-ink-2">
                    <span>Editing a message</span>
                    <button type="button" data-testid="composer-edit-cancel" onClick={() => store().resetComposerDraft(key)} className="ml-auto text-ink-3 hover:text-ink">Cancel</button>
                </div>
            )}
            {draft.replyTo && (
                <div data-testid="composer-reply-to" className="flex items-center gap-2 px-2 pt-1.5 text-[0.75rem] text-ink-2 min-w-0">
                    <span className="truncate">Replying to {draft.replyTo.author}: {draft.replyTo.excerpt}</span>
                    <button type="button" data-testid="composer-reply-cancel" onClick={() => store().setDraftReply(key, null)} className="ml-auto flex-none text-ink-3 hover:text-ink">Cancel</button>
                </div>
            )}
            {draft.contextItems.length > 0 && (
                <div className="flex flex-wrap gap-1 px-2 pt-1.5">
                    {draft.contextItems.map((item, i) => (
                        // An attachment: its label and remove button; one with text
                        // is a block that shows the text, to read, not to edit.
                        <div key={`${item.path}:${i}`} data-testid="composer-context-chip" title={item.path}
                            className={item.text ? 'flex flex-col basis-full min-w-0 rounded-md border border-line-2 px-2 py-1 text-[0.6875rem] text-ink-2' : cn(chip, 'max-w-full pr-1')}>
                            <div className="flex items-center gap-1 min-w-0">
                                <span className="truncate">{item.label}</span>
                                <button
                                    type="button"
                                    data-testid="composer-context-remove"
                                    aria-label={`Remove ${item.label}`}
                                    onClick={() => store().setComposerDraft(key, { contextItems: draft.contextItems.filter((_, j) => j !== i) })}
                                    className={cn('inline-flex items-center justify-center w-3.5 h-3.5 rounded-full text-ink-3 hover:bg-sheet-3 hover:text-ink', item.text && 'ml-auto')}
                                >
                                    <X className="w-2.5 h-2.5" strokeWidth={2} />
                                </button>
                            </div>
                            {item.text && (
                                <div data-testid="composer-context-text" className="mt-0.5 max-h-20 overflow-y-auto whitespace-pre-wrap break-words text-ink-3">
                                    {item.text}
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            )}
            {pending && (
                <div data-testid="composer-waiting" className="flex items-center gap-2 px-2 pt-1.5 text-[0.75rem] text-ink-2">
                    <span aria-hidden className="w-3 h-3 rounded-full border-2 border-teal border-t-transparent animate-spin" />
                    <span className="truncate">waiting on {basename(pending.path)}</span>
                    <button type="button" data-testid="composer-waiting-cancel" onClick={() => store().cancelDraftContextRequest(cellId)} className="ml-auto text-ink-3 hover:text-ink">Cancel</button>
                </div>
            )}
            {notice && (
                <div data-testid="composer-notice" role="alert" className="px-2 pt-1.5 text-[0.75rem] text-red">{notice}</div>
            )}
            <div className={cn('relative flex flex-col min-h-12', draft.expanded ? 'flex-1' : 'flex-[0_1_6.5rem]')}>
                <DraftEditor
                    key={`${key}:${draft.generation}`}
                    ref={editorRef}
                    cellId={cellId}
                    channel={channel}
                    initialText={draft.text}
                    onText={(text) => store().setComposerDraft(key, { text, notice: null })}
                    active={active}
                    fullscreen={maximized}
                />
            </div>
            <div className="flex-none flex flex-wrap items-center gap-1 px-1.5 py-1 border-t border-line">
                <div data-testid="composer-mode" role="radiogroup" aria-label="Mode" className="inline-flex gap-0.5">
                    <button type="button" role="radio" data-mode="queued" aria-checked={draft.mode === 'queued'}
                        onClick={() => void chooseMode('queued')} className={cn(draft.mode === 'queued' ? segmentOn : segmentOff, segment)}>
                        Queued
                    </button>
                    <button type="button" role="radio" data-mode="interrupt" aria-checked={draft.mode === 'interrupt'}
                        onClick={() => void chooseMode('interrupt')} className={cn(draft.mode === 'interrupt' ? segmentOn : segmentOff, segment)}>
                        <Zap className="w-3 h-3" strokeWidth={1.8} />Interrupt
                    </button>
                </div>
                {runScope && (
                    <button type="button" data-testid="queue-bar" aria-pressed={barOn}
                        title="Show the queue bar: messages below it wait until you move them up"
                        onClick={() => void store().setQueueBar(cellId, contextId, !barOn)} className={tool}>
                        <ListFilter className="w-3.5 h-3.5" strokeWidth={1.6} />Hold
                    </button>
                )}
                {runScope && barOn && (
                    <div data-testid="composer-place" data-place={draft.below ? 'below' : 'above'} role="radiogroup" aria-label="Place"
                        aria-disabled={draft.mode === 'interrupt' || undefined}
                        title={draft.mode === 'interrupt' ? 'An interrupt goes first, above the bar' : undefined}
                        className="inline-flex gap-0.5 aria-disabled:opacity-45">
                        <button type="button" role="radio" aria-checked={!draft.below}
                            onClick={() => store().setComposerDraft(key, { below: false })} className={cn(!draft.below ? segmentOn : segmentOff, segment)}>
                            Above bar
                        </button>
                        <button type="button" role="radio" aria-checked={draft.below} disabled={draft.mode === 'interrupt'}
                            onClick={() => store().setComposerDraft(key, { below: true })} className={cn(draft.below ? segmentOn : segmentOff, segment)}>
                            Below bar
                        </button>
                    </div>
                )}
                <button type="button" data-testid="composer-add-annotation" aria-label="Add annotation" title={addTitle}
                    // The press keeps the focus, and with it the file view's
                    // selection, where it was: a view that cannot pick attaches it.
                    onMouseDown={e => e.preventDefault()}
                    aria-disabled={addDisabled || undefined}
                    onClick={() => { if (!addDisabled && file) void store().requestDraftContext(cellId, key, file); }} className={tool}>
                    <Crosshair className="w-3.5 h-3.5" strokeWidth={1.6} />Add annotation
                </button>
                <button type="button" data-testid="composer-expand" aria-pressed={draft.expanded} aria-label={draft.expanded ? 'Collapse the composer' : 'Expand the composer'}
                    onClick={() => store().setComposerDraft(key, { expanded: !draft.expanded })} className={tool}>
                    {draft.expanded ? <Minimize2 className="w-3.5 h-3.5" strokeWidth={1.6} /> : <Maximize2 className="w-3.5 h-3.5" strokeWidth={1.6} />}
                </button>
                <button type="button" data-testid="composer-send" aria-disabled={draft.sending || stopping || undefined}
                    onClick={() => void send()}
                    className={cn(draft.editing ? btn : btnPrimary, 'ml-auto flex-none aria-disabled:opacity-45 aria-disabled:cursor-default')}>
                    <Send className="w-3.5 h-3.5" strokeWidth={1.6} />{draft.editing ? 'Save' : 'Send'}
                </button>
            </div>
        </div>
    );
}
