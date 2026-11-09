// One run in the annotation pane: the run picker and the run's Stop (a
// finished run's status chip) on the rail; the options (Default: its
// messages with the files it touched; Current file: the same about the file
// last viewed in the cell; Log: its lines); what it is doing (the phase
// line), the messages it has not read yet (its queue, split by the queue
// bar), the composer's add row or the composer inline at the end, and its
// open approval cards. An ended run's drilldown is read-only: its messages
// and log stay viewable. Esc closes the pane, from the options' controls too.

import React, { useEffect, useId, useImperativeHandle, useMemo, useRef } from 'react';
import { EarOff, FileText } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { usePostStopLock } from '@/hooks/usePostStopLock';
import { useRunInbox } from '@/hooks/useRunInbox';
import { NAV_KEYS_OWNER_PROPS } from '@/lib/keyboardOwner';
import { runDisplayName } from '@/lib/runLabels';
import { runLightState } from '@/lib/runLight';
import {
    buildTimeline, filterThreadsByPath, filterTimelineByPath, finishedStatusLabel, phaseLine, splitQueue,
    type FileTouch, type TimelineItem,
} from '@/lib/annotations';
import { draftKey } from '@/lib/annotationDraft';
import { RunStopButton } from '@/components/RunStopButton';
import { chip } from '@/components/ui/formStyles';
import type { AnnotationPaneOption } from '@/stores/collimate/types';
import { PaneRail } from './PaneRail';
import type { PaneViewHandle } from './paneChrome';
import { PaneOptions } from './PaneOptions';
import { AnnotationTimeline, type AnnotationTimelineHandle } from './AnnotationTimeline';
import { ApprovalCards } from './ApprovalCards';
import { RunLogView } from './RunLogView';
import { RunPicker } from './RunPicker';
import { finishedOf, runnerOf, useAdoptedGrant, useHostCli, usePaneRun } from './paneRun';

const NO_TOUCHES: readonly FileTouch[] = [];

const PHASE_DOT: Record<'working' | 'waiting' | 'stopping', string> = {
    working: 'bg-teal',
    waiting: 'bg-brass',
    stopping: 'bg-red-soft border border-red',
};

export interface RunDrilldownProps {
    cellId: string;
    runId: string;
    option: AnnotationPaneOption;
    /** The cell takes messages (not read-only). */
    allowAnnotate: boolean;
    maximized: boolean;
    /** Esc: the pane closes. */
    onClose: () => void;
    ref?: React.Ref<PaneViewHandle>;
}

export function RunDrilldown({ cellId, runId, option, allowAnnotate, maximized, onClose, ref }: RunDrilldownProps) {
    const paneRun = usePaneRun(cellId, runId);
    const adoptedGrant = useAdoptedGrant(runId);
    const hostCli = useHostCli(runId);
    const context = useCollimateStore(s => s.annotationContexts[cellId]?.[runId]);
    const touches = useCollimateStore(s => s.fileTouches[runId]) ?? NO_TOUCHES;
    const touchesLoaded = useCollimateStore(s => runId in s.fileTouches);
    const file = useCollimateStore(s => s.lastFileTab[cellId] ?? null);
    const notice = useCollimateStore(s => s.annotationNotice[cellId] ?? null);
    const stopping = useCollimateStore(s => s.stoppingRunIds);
    const phases = useCollimateStore(s => s.runPhases);
    const live = !!paneRun?.live;
    const run = paneRun?.run;
    const channel = run?.channel ?? '';
    const inbox = useRunInbox(channel, runId, live);
    const { onClickCapture } = usePostStopLock();
    const ids = useId();
    const panelId = `${ids}-panel`;
    const bodyRef = useRef<HTMLDivElement>(null);
    const timelineRef = useRef<AnnotationTimelineHandle>(null);
    const key = draftKey(cellId, { kind: 'run', runId });

    useEffect(() => {
        const store = useCollimateStore.getState();
        void store.refreshAnnotationContext(cellId, runId);
        if (!live) store.markFailureSeen(cellId);
    }, [cellId, runId, live]);
    useEffect(() => {
        if (channel && !touchesLoaded) void useCollimateStore.getState().loadFileTouches(channel, runId);
    }, [channel, runId, touchesLoaded]);

    useImperativeHandle(ref, () => ({
        focus: (target) => {
            if (target === 'composer' && timelineRef.current) timelineRef.current.focusComposer();
            else bodyRef.current?.querySelector<HTMLElement>('[role="log"], [role="region"]')?.focus({ preventScroll: true });
        },
    }), []);

    const reader = run ? runDisplayName(run) : 'the run';
    const sections = useMemo(() => {
        const split = context ? splitQueue(context) : { history: [], queue: [], held: [] };
        let items: TimelineItem[] = buildTimeline(
            split.history.map(thread => ({ thread, contextId: runId, tag: null, sendable: false })),
            touches.map(touch => ({ touch, tag: null })),
        );
        // Current file shows the part of the queue about the file, yet a
        // move orders the whole queue (the server takes no less).
        let shown: ReadonlySet<string> | undefined;
        if (option === 'file') {
            items = file ? filterTimelineByPath(items, file) : [];
            shown = new Set(file ? filterThreadsByPath([...split.queue, ...split.held], file).map(t => t.root.id) : []);
        }
        return { items, queue: split.queue, held: split.held, shown };
    }, [context, touches, option, file, runId]);

    if (!run) return null;
    const finished = finishedOf(run);
    const readOnly = !live || !allowAnnotate || !!context?.sealed;
    const state = live ? runLightState(run, stopping, phases) : null;
    const line = state ? phaseLine(state) : null;

    const phase = (
        <div data-testid="drilldown-phase" data-phase={line?.phase ?? 'ended'} className="flex flex-wrap items-center gap-x-1.5 px-2 py-1 text-[0.75rem] text-ink-2">
            <i aria-hidden className={cn('block w-1.5 h-1.5 rounded-full', line ? PHASE_DOT[line.phase] : 'border border-ink-3')} />
            {line ? line.text : `Ended · ${finishedStatusLabel(finished?.status ?? 'completed')}`}
            {live && adoptedGrant && (
                <span data-testid="drilldown-adopted" title="The runner's grant was raised to what this harness needs" className="basis-full pl-3 min-w-0 truncate text-[0.6875rem] text-ink-3">
                    {adoptedGrant}
                </span>
            )}
            {live && hostCli && (
                <span data-testid="drilldown-host-cli" title="The harness's own CLI runs on this machine, outside any sandbox, under its own sign-in" className="basis-full pl-3 min-w-0 truncate text-[0.6875rem] text-ink-3">
                    {hostCli}
                </span>
            )}
        </div>
    );

    return (
        <div
            data-testid="run-drilldown"
            data-run-id={runId}
            data-ended={!live || undefined}
            {...NAV_KEYS_OWNER_PROPS}
            onKeyDown={(e) => {
                if (e.key !== 'Escape') return;
                e.preventDefault();
                e.stopPropagation();
                onClose();
            }}
            className="flex-1 min-h-0 min-w-0 flex flex-col bg-sheet"
        >
            <PaneRail>
                <span className="min-w-0 flex items-end gap-1" onClickCapture={onClickCapture}>
                    <RunPicker cellId={cellId} runId={runId} option={option} />
                    {live ? (
                        <RunStopButton channel={run.channel} runId={runId} runner={runnerOf(run)} compact className="flex-none h-[1.375rem] px-2" />
                    ) : (
                        <span className={cn(chip, 'flex-none')}>{finishedStatusLabel(finished?.status ?? 'completed')}</span>
                    )}
                </span>
            </PaneRail>
            <PaneOptions
                testIdPrefix="drilldown-option"
                value={option}
                onChange={(next) => useCollimateStore.getState().setAnnotationPaneView(cellId, { view: 'run', runId, option: next })}
                fileLabel={file}
                panelId={panelId}
            />
            {/* The expanded composer covers this body, its containing block. */}
            <div ref={bodyRef} role="tabpanel" id={panelId} className="relative flex-1 min-h-0 min-w-0 flex flex-col">
                {option === 'file' && (
                    <div data-testid="drilldown-file-head" title={file ?? undefined} className="flex-none flex items-center gap-1.5 px-3 py-1 text-[0.75rem] text-ink-2 border-b border-line">
                        <FileText className="w-3.5 h-3.5 flex-none text-ink-3" strokeWidth={1.6} />
                        <span className="truncate">{file ?? 'No file is open in this cell.'}</span>
                    </div>
                )}
                {option !== 'log' && live && context && !context.listening && (
                    <div data-testid="annotation-not-listening" className="flex-none flex items-start gap-1.5 px-3 py-1.5 bg-sheet-2 text-[0.75rem] text-ink-2">
                        <EarOff className="w-3.5 h-3.5 flex-none mt-px text-ink-3" strokeWidth={1.6} />
                        <span>This run isn&apos;t reading annotations yet. Your messages wait here until it does.</span>
                    </div>
                )}
                {notice && <div data-testid="annotation-notice" role="status" className="flex-none px-3 py-1 text-[0.75rem] text-red">{notice}</div>}
                {option === 'log' ? (
                    <RunLogView cellId={cellId} runIds={[runId]} showRunner={false} />
                ) : (
                    <AnnotationTimeline
                        ref={timelineRef}
                        cellId={cellId}
                        channel={channel}
                        items={sections.items}
                        queue={{ contextId: runId, unheld: sections.queue, held: sections.held, shown: sections.shown, barOn: !!context?.bar_on }}
                        readOnly={readOnly}
                        draftKey={key}
                        composer={readOnly ? undefined : {
                            scope: { kind: 'run', runId }, contextId: runId, context, barOn: !!context?.bar_on,
                            stopping: stopping.has(runId), maximized, addLabel: 'Add message',
                        }}
                        phase={phase}
                        emptyText={option === 'file' ? 'Nothing about this file yet.' : 'No messages yet.'}
                        readerOf={() => reader}
                    />
                )}
            </div>
            {live && <ApprovalCards cards={inbox.cards} onAnswer={inbox.answer} />}
        </div>
    );
}
