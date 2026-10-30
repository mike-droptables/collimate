import React, { useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { PooledIframeConcealContext, usePooledIframeConcealed } from '@/contexts/RendererConcealContext';
import { useRendererId } from '@/hooks/useRendererId';
import { NativeRenderer } from '@/components/NativeRenderer';
import { NativeRendererErrorBoundary } from '@/components/NativeRendererErrorBoundary';
import { SandboxIframe } from '@/components/SandboxIframe';
import { StandaloneSandboxIframe } from '@/components/StandaloneSandboxIframe';
import { formTextarea } from '@/components/ui/formStyles';
import { cn } from '@/lib/utils';
import { composerFilename } from '@/lib/annotationDraft';
import { MessageDraft, useMessageDraft } from '@/lib/messageDraft';
import { boundedFlush, flushRendererEditors } from '@/lib/rendererFlush';
import { focusEditorIn } from '@/lib/selectionFocus';
import type { DocumentViewEdit } from '@/lib/fileDocument';
import type { RendererRecoverySnapshot } from '@/lib/fileSnapshotCodec';

/** How many frames focus() keeps trying while the editor mounts. */
const FOCUS_FRAMES = 30;

export interface DraftEditorHandle {
    /** Put the keyboard in the editor (retried while it mounts). */
    focus: () => boolean;
    /** The draft's text, once the editor has handed over every edit. */
    text: () => Promise<string>;
    /** Empty the editor in place (after a send). */
    reset: () => void;
}

export interface DraftEditorProps {
    cellId: string;
    channel: string;
    /** The text the editor opens with (the parent remounts it to load new
     *  text: an edit, a reply). */
    initialText: string;
    /** Every change of the draft's text. */
    onText: (text: string) => void;
    /** The cell is selected: the editor may take the keyboard. */
    active: boolean;
    /** In the full-window view: an unpooled frame. */
    fullscreen: boolean;
    ref?: React.Ref<DraftEditorHandle>;
}

/**
 * The composer's editor: the channel's default `.md` renderer over an
 * in-memory draft (lib/messageDraft), mounted by the ordinary renderer
 * hosts under a root file name the cell does not hold
 * (lib/annotationDraft composerFilename), so its links come out
 * cell-relative and Insert lists the cell's files, while every edit goes
 * through the draft. A plain textarea stands in while there is no renderer.
 */
export function DraftEditor({ cellId, channel, initialText, onText, active, fullscreen, ref }: DraftEditorProps) {
    const [fileName] = useState(() => composerFilename((useCollimateStore.getState().locateCell(cellId)?.cell.files ?? []).map(f => f.name)));
    const [draft] = useState(() => new MessageDraft(initialText));
    const snapshot = useMessageDraft(draft);
    const { renderer, error } = useRendererId(fileName);
    const boxRef = useRef<HTMLDivElement>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const { containerOf, invalidate } = useGuestPool();

    // Mirror the text to the store (it outlives this mount).
    const mirrored = useRef(initialText);
    const onTextRef = useRef(onText);
    useEffect(() => { onTextRef.current = onText; }, [onText]);
    useEffect(() => {
        if (snapshot.content === mirrored.current) return;
        mirrored.current = snapshot.content;
        onTextRef.current(snapshot.content);
    }, [snapshot.content]);

    const documentSnapshot = useMemo(() => ({
        ownerId: snapshot.ownerId, content: snapshot.content, recovery: snapshot.recovery,
        editSeq: snapshot.editSeq, materializedSeq: snapshot.materializedSeq, viewAcks: snapshot.viewAcks,
    }), [snapshot]);
    const onContentChange = useCallback((body: string, view?: DocumentViewEdit) => draft.edit(body, view), [draft]);
    const onSnapshotChange = useCallback((recovery: RendererRecoverySnapshot, view?: DocumentViewEdit) => draft.editSnapshot(recovery, view), [draft]);
    const onGetContent = useCallback(() => draft.materializedBody(), [draft]);

    useImperativeHandle(ref, () => ({
        focus: () => {
            const tryFocus = () => {
                if (textareaRef.current) {
                    textareaRef.current.focus({ preventScroll: true });
                    return true;
                }
                return !!boxRef.current && focusEditorIn(boxRef.current, containerOf);
            };
            if (tryFocus()) return true;
            let frames = 0;
            const again = () => {
                if (tryFocus() || ++frames >= FOCUS_FRAMES) return;
                requestAnimationFrame(again);
            };
            requestAnimationFrame(again);
            return false;
        },
        text: async () => {
            await boundedFlush(() => flushRendererEditors(cellId, fileName));
            return draft.materializedBody();
        },
        reset: () => draft.reset(''),
    }), [cellId, fileName, draft, containerOf]);

    // The box moves and resizes with the pane: the pooled frame follows.
    useLayoutEffect(() => { invalidate(); });
    useEffect(() => {
        const box = boxRef.current;
        if (!box || typeof ResizeObserver === 'undefined') return;
        const observer = new ResizeObserver(() => invalidate());
        observer.observe(box);
        return () => observer.disconnect();
    }, [invalidate]);

    // Pooled frames live over the page: hide this one while something of
    // the cell paints over the pane (its confirm, a tab or dock drag).
    const parentConceal = usePooledIframeConcealed();
    const covered = useCollimateStore(s => s.confirmDialog?.cellId === cellId || s.tabDrag?.cellId === cellId || s.dockDrag?.cellId === cellId);

    const fallback = !renderer || !!error || renderer.id === 'binary';
    const documentId = renderer ? JSON.stringify(['message-draft', channel, cellId, draft.id, renderer.id, renderer.contentHash, fullscreen ? 'fullscreen' : 'pane']) : '';
    const IframeHost = fullscreen ? StandaloneSandboxIframe : SandboxIframe;
    const iframe = renderer ? (
        <IframeHost
            fileHash=""
            documentId={documentId}
            fileContent={snapshot.content}
            documentSnapshot={documentSnapshot}
            onSnapshotChange={onSnapshotChange}
            onGetContent={onGetContent}
            onContentChange={onContentChange}
            fileName={fileName}
            rendererId={renderer.id}
            rendererHash={renderer.contentHash}
            isActiveFocus={active}
            cellId={cellId}
            channel={channel}
        />
    ) : null;

    return (
        <div ref={boxRef} data-annotation-composer-box className="relative flex-1 min-h-0 min-w-0 flex flex-col">
            {fallback ? (
                <textarea
                    ref={textareaRef}
                    data-testid="composer-fallback"
                    aria-label="Message"
                    placeholder="Write a message…"
                    value={snapshot.content}
                    onChange={(e) => draft.edit(e.target.value)}
                    className={cn(formTextarea, 'flex-1 min-h-[4.5rem] resize-none rounded-none border-0')}
                />
            ) : (
                <PooledIframeConcealContext.Provider value={parentConceal || covered}>
                    {renderer.embed === 'native' ? (
                        <NativeRendererErrorBoundary resetKey={`${renderer.id}::${renderer.contentHash}::${fileName}`} fallback={iframe}>
                            <NativeRenderer
                                rendererId={renderer.id}
                                rendererHash={renderer.contentHash}
                                fileContent={snapshot.content}
                                documentSnapshot={documentSnapshot}
                                onSnapshotChange={onSnapshotChange}
                                onGetContent={onGetContent}
                                onContentChange={onContentChange}
                                fileName={fileName}
                                cellId={cellId}
                                channel={channel}
                                isActiveFocus={active}
                            />
                        </NativeRendererErrorBoundary>
                    ) : iframe}
                </PooledIframeConcealContext.Provider>
            )}
        </div>
    );
}
