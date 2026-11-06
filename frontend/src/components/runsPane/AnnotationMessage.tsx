import { useRef, useState, type ReactNode } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { Lock, Pencil, Reply, Send, Trash2, Zap } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { CollimateState } from '@/stores/useCollimateStore';
import { MarkdownView } from '@/components/MarkdownView';
import { Menu } from '@/components/ui/Menu';
import { chip } from '@/components/ui/formStyles';
import { isLocked, serverTime, type AnnotationContextItem, type AnnotationMessage as Message, type Thread } from '@/lib/annotations';
import { clockOf } from '@/lib/cellLog';
import { revealCellFile } from '@/lib/revealFile';
import { runDisplayName } from '@/lib/runLabels';
import { runsOnCell } from '@/lib/runningCell';

export interface AnnotationMessageProps {
    cellId: string;
    channel: string;
    thread: Thread;
    /** The run label it is tagged with (All); null for none. */
    tag: string | null;
    /** A cell-context message: it can be sent to a live run. */
    sendable: boolean;
    /** An ended run's or a read-only cell's: no actions but the chips. */
    readOnly: boolean;
    /** The composer edits and replies load into. */
    draftKey: string;
    /** Who reads the messages here (the run's name), for the read lock. */
    reader: string;
    /** The queue's drag handle, for an unread message of the queue. */
    dragHandle?: ReactNode;
}

const action = 'inline-flex items-center justify-center w-5 h-5 rounded-xs text-ink-3 transition-colors hover:bg-sheet-3 hover:text-ink aria-disabled:opacity-45 aria-disabled:cursor-default';

/** A run label's display name, from the runs this session knows. */
function nameOfLabel(s: CollimateState, cellId: string, label: string): string {
    const known = Object.values(s.runs).find(r => r.label === label)
        ?? s.finishedRuns[cellId]?.find(r => r.label === label);
    return known ? runDisplayName(known) : label;
}

const fullTime = (at: string) => {
    const t = serverTime(at);
    return t ? new Date(t).toLocaleString() : at;
};

function ContextChip({ cellId, item }: { cellId: string; item: AnnotationContextItem }) {
    const [open, setOpen] = useState(false);
    return (
        <div className="min-w-0">
            <button
                type="button"
                data-testid="annotation-context-chip"
                aria-expanded={open}
                title={item.path}
                onClick={() => setOpen(v => !v)}
                className={cn(chip, 'max-w-full cursor-pointer hover:bg-sheet-2 hover:text-ink')}
            >
                <span className="truncate">{item.label}</span>
            </button>
            {open && (
                <div className="mt-1">
                    <pre className="m-0 max-h-40 overflow-auto px-2 py-1.5 font-mono text-[0.6875rem] leading-[1.5] text-ink-2 bg-code-bg border border-line rounded-md whitespace-pre-wrap break-words">{item.text}</pre>
                    <button
                        type="button"
                        onClick={() => revealCellFile(cellId, item.path, { newTab: true })}
                        className="mt-0.5 text-[0.71875rem] text-ink-3 underline decoration-line-2 underline-offset-2 hover:text-ink"
                    >
                        Open file
                    </button>
                </div>
            )}
        </div>
    );
}

/**
 * One message of a status pane timeline, with its replies (one level):
 * the author and time, the run tag in All, its mode, the markdown body (a
 * cell-relative link opens the file), its context chips (each opens to the
 * text it attaches), whether its run has read it, and while unread its
 * edit, delete and drag handle. Reply works on any top-level message; a
 * cell message can be sent to a live run.
 */
export function AnnotationMessage(props: AnnotationMessageProps) {
    const { thread, sendable, readOnly } = props;
    const root = thread.root;
    return (
        <div data-testid="annotation-thread" data-id={root.id}>
            <OneMessage {...props} m={root} isRoot replies={thread.replies.length} sendable={sendable} readOnly={readOnly} />
            {thread.replies.length > 0 && (
                <div className="ml-3 pl-2 border-l border-line">
                    {thread.replies.map(reply => (
                        <OneMessage key={reply.id} {...props} m={reply} isRoot={false} replies={0} sendable={false} dragHandle={undefined} />
                    ))}
                </div>
            )}
        </div>
    );
}

function OneMessage({ cellId, channel, m, isRoot, replies, tag, sendable, readOnly, draftKey, reader, dragHandle }:
    AnnotationMessageProps & { m: Message; isRoot: boolean; replies: number }) {
    const author = useCollimateStore(s => (m.author === 'user' ? 'You' : m.author_label ? nameOfLabel(s, cellId, m.author_label) : 'Run'));
    const liveRuns = useCollimateStore(useShallow(s => (sendable ? runsOnCell(s, cellId) : [])));
    const [menuOpen, setMenuOpen] = useState(false);
    const sendRef = useRef<HTMLButtonElement>(null);
    const store = useCollimateStore.getState;
    const editable = !readOnly && !isLocked(m);

    const edit = async () => {
        const draft = store().composerDrafts[draftKey];
        if (draft?.text.trim() && !draft.editing) {
            const ok = await store().openConfirm({ title: 'Edit message', body: 'Discard your draft to edit this message?', confirmLabel: 'Discard', cellId });
            if (!ok) return;
        }
        store().loadDraftForEdit(draftKey, m);
    };
    const remove = async () => {
        const ok = await store().openConfirm({
            title: 'Delete message',
            body: replies ? `Delete this message and its ${replies} ${replies === 1 ? 'reply' : 'replies'}?` : 'Delete this message?',
            confirmLabel: 'Delete', destructive: true, cellId,
        });
        if (ok) await store().deleteMessage(cellId, m);
    };

    return (
        <div
            data-testid="annotation-message"
            data-id={m.id}
            data-read={m.read_at !== null}
            data-author={m.author}
            data-held={m.held}
            data-reply={isRoot ? undefined : 'true'}
            className="group rounded-md px-2 py-1.5 hover:bg-sheet-2"
        >
            <div className="flex items-center gap-1.5 min-w-0 text-[0.75rem]">
                <span className="font-semibold text-ink truncate">{author}</span>
                {tag && <span data-testid="message-tag" className={chip}>{tag}</span>}
                {m.kind === 'prompt' && <span className={chip}>Prompt</span>}
                {m.mode === 'interrupt' && (
                    <span data-testid="message-mode" title="Interrupt" className="inline-flex text-brass"><Zap className="w-3 h-3" strokeWidth={1.8} /></span>
                )}
                <span className="text-ink-3 font-mono text-[0.6875rem]" title={fullTime(m.created_at)}>{clockOf(serverTime(m.created_at))}</span>
                {m.edited_at && <span className="text-ink-3 text-[0.6875rem]">edited</span>}
                <span className="ml-auto flex items-center gap-0.5 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100">
                    {editable && (
                        <>
                            <button type="button" data-testid="message-edit" aria-label="Edit message" title="Edit" onClick={() => void edit()} className={action}>
                                <Pencil className="w-3 h-3" strokeWidth={1.8} />
                            </button>
                            <button type="button" data-testid="message-delete" aria-label="Delete message" title="Delete" onClick={() => void remove()}
                                className={cn(action, 'hover:bg-red-soft hover:text-red')}>
                                <Trash2 className="w-3 h-3" strokeWidth={1.8} />
                            </button>
                        </>
                    )}
                    {dragHandle}
                    {!readOnly && isRoot && (
                        <button type="button" data-testid="message-reply" aria-label="Reply" title="Reply"
                            onClick={() => store().setDraftReply(draftKey, m)} className={action}>
                            <Reply className="w-3 h-3" strokeWidth={1.8} />
                        </button>
                    )}
                    {sendable && isRoot && (
                        <>
                            <button
                                ref={sendRef}
                                type="button"
                                data-testid="message-send-to"
                                aria-label="Send to a run"
                                aria-haspopup="menu"
                                aria-disabled={liveRuns.length === 0 || undefined}
                                title={liveRuns.length === 0 ? 'No run is live in this cell' : 'Send to a run'}
                                onClick={() => { if (liveRuns.length > 0) setMenuOpen(true); }}
                                className={action}
                            >
                                <Send className="w-3 h-3" strokeWidth={1.8} />
                            </button>
                            <Menu
                                anchor={sendRef}
                                open={menuOpen}
                                onClose={() => setMenuOpen(false)}
                                label="Send to run"
                                align="end"
                                items={liveRuns.map(run => ({
                                    kind: 'item' as const,
                                    id: run.runId,
                                    label: runDisplayName(run),
                                    onSelect: () => { setMenuOpen(false); void store().sendMessageToRun(cellId, m.id, run.runId); },
                                }))}
                            />
                        </>
                    )}
                </span>
            </div>
            <MarkdownView content={m.body} cell={{ channel, cellId }} className="text-[0.8125rem] mt-0.5" />
            {m.context_items.length > 0 && (
                <div className="mt-1 flex flex-col items-start gap-1">
                    {m.context_items.map((item, i) => <ContextChip key={`${item.path}:${i}`} cellId={cellId} item={item} />)}
                </div>
            )}
            {m.author === 'user' && m.kind === 'prompt' && (
                <div data-testid="message-read-lock" className="mt-1 flex items-center gap-1 text-[0.6875rem] text-ink-3">
                    <Lock className="w-3 h-3" strokeWidth={1.8} />Prompt · read at start
                </div>
            )}
            {m.author === 'user' && m.kind === 'message' && m.read_at && (
                <div data-testid="message-read-lock" className="mt-1 flex items-center gap-1 text-[0.6875rem] text-ink-3" title={fullTime(m.read_at)}>
                    <Lock className="w-3 h-3" strokeWidth={1.8} />Read by {reader} at {clockOf(serverTime(m.read_at))}
                </div>
            )}
        </div>
    );
}
