// Every run of the cell at once: each live run's messages, tagged with its
// label, and the cell's own notes (untagged; each can be sent to a live
// run), with every live run's file touches, in time order; or the same about
// the file last viewed (Current file); or the cell's merged log (Log). The
// composer writes notes to the cell's own context. Esc goes back.

import React, { useEffect, useId, useImperativeHandle, useMemo, useRef } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { ChevronLeft, FileText } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { NAV_KEYS_OWNER_PROPS } from '@/lib/keyboardOwner';
import { runsOnCell } from '@/lib/runningCell';
import { runDisplayName } from '@/lib/runLabels';
import { CELL_CONTEXT_ID, buildTimeline, filterTimelineByPath, threadsOf, type AnnotationContext, type FileTouch, type Thread } from '@/lib/annotations';
import { draftKey } from '@/lib/annotationDraft';
import type { RunsPaneOption } from '@/stores/collimate/types';
import { PaneRail } from './PaneRail';
import { railTitle } from './paneChrome';
import { PaneOptions } from './PaneOptions';
import { AnnotationTimeline } from './AnnotationTimeline';
import { AnnotationComposer, type AnnotationComposerHandle } from './AnnotationComposer';
import { RunLogView } from './RunLogView';
import type { RunViewHandle } from './RunDrilldown';

const NO_CONTEXTS: Readonly<Record<string, AnnotationContext>> = {};

export interface AllViewProps {
    cellId: string;
    option: RunsPaneOption;
    allowAnnotate: boolean;
    maximized: boolean;
    onBack: () => void;
    ref?: React.Ref<RunViewHandle>;
}

export function AllView({ cellId, option, allowAnnotate, maximized, onBack, ref }: AllViewProps) {
    const runs = useCollimateStore(useShallow(s => runsOnCell(s, cellId)));
    const contexts = useCollimateStore(s => s.annotationContexts[cellId]) ?? NO_CONTEXTS;
    const touchesByRun = useCollimateStore(useShallow(s => Object.fromEntries(runs.map(r => [r.runId, s.fileTouches[r.runId]] as const))));
    const file = useCollimateStore(s => s.lastFileTab[cellId] ?? null);
    const notice = useCollimateStore(s => s.annotationNotice[cellId] ?? null);
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const key = draftKey(cellId, { kind: 'all' });
    const expanded = useCollimateStore(s => !!s.composerDrafts[key]?.expanded) && allowAnnotate;
    const ids = useId();
    const panelId = `${ids}-panel`;
    const bodyRef = useRef<HTMLDivElement>(null);
    const composerRef = useRef<AnnotationComposerHandle>(null);

    useEffect(() => { void useCollimateStore.getState().loadCellAnnotations(cellId); }, [cellId]);
    const missing = runs.filter(r => touchesByRun[r.runId] === undefined).map(r => `${r.channel}\0${r.runId}`).join('\n');
    useEffect(() => {
        if (!missing) return;
        for (const entry of missing.split('\n')) {
            const [ch, runId] = entry.split('\0');
            void useCollimateStore.getState().loadFileTouches(ch, runId);
        }
    }, [missing]);

    useImperativeHandle(ref, () => ({
        focus: (target) => {
            if (target === 'composer' && composerRef.current) composerRef.current.focus();
            else bodyRef.current?.querySelector<HTMLElement>('[role="log"], [role="region"]')?.focus({ preventScroll: true });
        },
    }), []);

    const readers = useMemo(() => new Map(runs.map(r => [r.runId, runDisplayName(r)])), [runs]);
    const items = useMemo(() => {
        const threads: { thread: Thread; contextId: string; tag: string | null; sendable: boolean }[] = [];
        for (const run of runs) {
            const ctx = contexts[run.runId];
            if (!ctx) continue;
            for (const thread of threadsOf(ctx.messages)) threads.push({ thread, contextId: ctx.id, tag: ctx.label ?? run.label, sendable: false });
        }
        const cell = contexts[CELL_CONTEXT_ID];
        if (cell) for (const thread of threadsOf(cell.messages)) threads.push({ thread, contextId: CELL_CONTEXT_ID, tag: null, sendable: true });
        const touches: { touch: FileTouch; tag: string | null }[] = runs.flatMap(run => (touchesByRun[run.runId] ?? []).map(touch => ({ touch, tag: run.label })));
        const all = buildTimeline(threads, touches);
        return option === 'file' ? (file ? filterTimelineByPath(all, file) : []) : all;
    }, [runs, contexts, touchesByRun, option, file]);

    return (
        <div
            data-testid="run-all"
            {...NAV_KEYS_OWNER_PROPS}
            onKeyDown={(e) => {
                if (e.key !== 'Escape') return;
                e.preventDefault();
                e.stopPropagation();
                onBack();
            }}
            className="flex-1 min-h-0 min-w-0 flex flex-col bg-sheet"
        >
            <PaneRail>
                <button type="button" data-testid="all-back" aria-label="Back to status" title="Back to status" onClick={onBack}
                    className="flex-none inline-flex items-center h-[1.125rem] px-0.5 rounded-xs text-ink-3 hover:bg-sheet-2 hover:text-ink">
                    <ChevronLeft className="w-3.5 h-3.5" strokeWidth={1.8} />
                </button>
                <span className={railTitle}>All runs</span>
            </PaneRail>
            <PaneOptions
                testIdPrefix="all-option"
                value={option}
                onChange={(next) => useCollimateStore.getState().setRunsPaneView(cellId, { view: 'all', option: next })}
                fileLabel={file}
                panelId={panelId}
            />
            <div ref={bodyRef} role="tabpanel" id={panelId} className={cn('min-h-0 min-w-0 flex flex-col', expanded ? 'hidden' : 'flex-1')}>
                {option === 'file' && (
                    <div data-testid="drilldown-file-head" title={file ?? undefined} className="flex-none flex items-center gap-1.5 px-3 py-1 text-[0.75rem] text-ink-2 border-b border-line">
                        <FileText className="w-3.5 h-3.5 flex-none text-ink-3" strokeWidth={1.6} />
                        <span className="truncate">{file ?? 'No file is open in this cell.'}</span>
                    </div>
                )}
                {notice && <div data-testid="annotation-notice" role="status" className="flex-none px-3 py-1 text-[0.75rem] text-red">{notice}</div>}
                {option === 'log' ? (
                    <RunLogView cellId={cellId} runIds={null} showRunner />
                ) : (
                    <AnnotationTimeline
                        cellId={cellId}
                        channel={channel}
                        items={items}
                        readOnly={!allowAnnotate}
                        draftKey={key}
                        emptyText={option === 'file' ? 'Nothing about this file yet.' : 'No messages yet.'}
                        readerOf={(contextId) => readers.get(contextId) ?? 'the run'}
                    />
                )}
            </div>
            {allowAnnotate && (
                <AnnotationComposer
                    ref={composerRef}
                    cellId={cellId}
                    channel={channel}
                    scope={{ kind: 'all' }}
                    contextId={CELL_CONTEXT_ID}
                    context={contexts[CELL_CONTEXT_ID]}
                    barOn={false}
                    stopping={false}
                    maximized={maximized}
                    onEscape={() => bodyRef.current?.querySelector<HTMLElement>('[role="log"], [role="region"]')?.focus({ preventScroll: true })}
                />
            )}
        </div>
    );
}
