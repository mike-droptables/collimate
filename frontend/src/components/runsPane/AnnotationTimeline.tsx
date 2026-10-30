import React, { useImperativeHandle, useLayoutEffect, useRef, type ReactNode } from 'react';
import { FilePen, FilePlus, FileX, Folder, FolderPlus, FolderX, type LucideIcon } from 'lucide-react';
import { chip } from '@/components/ui/formStyles';
import { serverTime, type FileTouch, type Thread, type TimelineItem } from '@/lib/annotations';
import { clockOf } from '@/lib/cellLog';
import { revealCellFile } from '@/lib/revealFile';
import { AnnotationMessage } from './AnnotationMessage';
import { QueueSection } from './QueueBar';

/** The view is "at the bottom" this close to it: new items keep it there. */
const TAIL_SLACK = 24;

const TOUCH: Record<FileTouch['op'], { verb: string; file: LucideIcon; folder: LucideIcon }> = {
    create: { verb: 'Created', file: FilePlus, folder: FolderPlus },
    edit: { verb: 'Edited', file: FilePen, folder: Folder },
    delete: { verb: 'Deleted', file: FileX, folder: FolderX },
};

export interface AnnotationTimelineProps {
    cellId: string;
    channel: string;
    items: readonly TimelineItem[];
    /** A run's queue, below the history and the phase line: all of it,
     *  with the messages a filter shows (all when `shown` is absent). */
    queue?: { contextId: string; unheld: readonly Thread[]; held: readonly Thread[]; shown?: ReadonlySet<string>; barOn: boolean };
    readOnly: boolean;
    draftKey: string;
    /** The run's phase line. */
    phase?: ReactNode;
    emptyText: string;
    /** Who reads a context's messages (for the read lock). */
    readerOf: (contextId: string) => string;
    ref?: React.Ref<HTMLDivElement>;
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
            {tag && <span className={chip}>{tag}</span>}
            <span className="ml-auto pl-2 flex-none font-mono text-[0.6875rem] text-ink-3">{clockOf(serverTime(touch.at))}</span>
        </div>
    );
}

/** One thread as the tail sees it: a reply landing in it (a run answers
 *  inside the thread it was asked in) changes it. */
const threadTail = (t: Thread) => `${t.root.id}:${t.replies.length}:${t.replies.at(-1)?.id ?? ''}`;

/**
 * A status pane timeline: messages (with their replies) and the files the
 * runs touched, in time order, newest at the bottom, then the run's phase
 * line and its queue. It follows its tail while scrolled to the bottom.
 */
export function AnnotationTimeline({ cellId, channel, items, queue, readOnly, draftKey, phase, emptyText, readerOf, ref }: AnnotationTimelineProps) {
    const rootRef = useRef<HTMLDivElement>(null);
    const atBottom = useRef(true);
    useImperativeHandle(ref, () => rootRef.current!, []);

    const queueShown = queue ? [...queue.unheld, ...queue.held].filter(t => !queue.shown || queue.shown.has(t.root.id)) : [];
    // What grows the timeline at its tail: an item, a reply in any thread,
    // a message of the queue.
    const tail = [...items.map(item => (item.kind === 'thread' ? threadTail(item.thread) : item.key)), '|', ...queueShown.map(threadTail)].join(' ');
    useLayoutEffect(() => {
        const root = rootRef.current;
        if (root && atBottom.current) root.scrollTop = root.scrollHeight;
    }, [tail]);

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
        </div>
    );
}
