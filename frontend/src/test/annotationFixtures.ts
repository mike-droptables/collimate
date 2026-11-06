// Synthetic annotation contexts, messages, runs and the store seed the
// status pane's component tests share.

import { vi } from 'vitest';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { AnnotationContext, AnnotationMessage, FileTouch } from '@/lib/annotations';
import type { RunRecord } from '@/lib/types';
import type { FinishedRun } from '@/stores/collimate/types';

export const CHANNEL_ID = 'ch';
export const CELL_ID = 'c1';

export const RUN: RunRecord = {
    runId: 'run-1', cellId: CELL_ID, channel: CHANNEL_ID, runnerId: 'agent', runnerName: 'Agent',
    generatorName: 'agent', label: 'agent-11111111', folder: '_runs/agent-11111111',
};
export const RUN_2: RunRecord = { ...RUN, runId: 'run-2', runnerId: 'chat', runnerName: 'Chat', label: 'chat-22222222', folder: '_runs/chat-22222222' };

export const msg = (id: string, over: Partial<AnnotationMessage> = {}): AnnotationMessage => ({
    id, context_id: RUN.runId, parent_id: null, author: 'user', author_label: null, kind: 'message', body: `body of ${id}`,
    context_items: [], refs: [], mode: 'queued', position: 1, held: false, revision: 1,
    created_at: '2026-10-24 10:00:00', edited_at: null, read_at: null, ...over,
});

export const ctx = (id: string, messages: AnnotationMessage[] = [], over: Partial<AnnotationContext> = {}): AnnotationContext => ({
    id, cell_id: CELL_ID, run_id: id === 'cell' ? null : id, label: id === 'cell' ? null : id === RUN_2.runId ? RUN_2.label : RUN.label,
    bar_on: false, listening: true, interrupt_seq: 0, rev: 1, sealed: false, messages, ...over,
});

export const touch = (path: string, at: string, over: Partial<FileTouch> = {}): FileTouch => ({
    at, path, op: 'edit', folder: false, version_id: 'v1', ...over,
});

export const finishedRun = (run: RunRecord, over: Partial<FinishedRun> = {}): FinishedRun => {
    const { surfaces: _surfaces, ...rest } = run;
    return { ...rest, status: 'completed', endedAt: Date.UTC(2026, 9, 24, 11, 0), ...over };
};

/** The store as a status pane test starts: one channel holding the cell,
 *  nothing running, the pane's network actions stubbed. */
export function seedPaneStore(over: Record<string, unknown> = {}) {
    const channel = {
        id: CHANNEL_ID, name: 'Channel', nav_stacks: [],
        main_stacks: [{ id: 's1', name: 'Stack', cells: [{ id: CELL_ID, name: 'Cell', files: [{ name: 'page.html' }, { name: 'notes.md' }] }] }],
    };
    useCollimateStore.setState({
        activeChannel: channel, state: { channels: [channel] }, cellCache: {}, cellLayouts: {}, cellDisplaysReady: true,
        runs: {}, stoppingRunIds: new Set(), runPhases: {}, lastFinishedByCell: {}, activeRunsEverywhere: [],
        cellLogRuns: {}, runLogs: {}, runLogArrivals: {}, pickSessions: {}, confirmDialog: null, tabDrag: null, dockDrag: null,
        annotationContexts: {}, annotationNotice: {}, fileTouches: {}, lastFileTab: {}, finishedRuns: {}, runsPaneView: {},
        runsPaneFocus: {}, pendingRunsPaneOpen: null, composerDrafts: {}, annotationRequests: {},
        loadCellAnnotations: vi.fn(async () => {}), refreshAnnotationContext: vi.fn(async () => {}),
        loadFileTouches: vi.fn(async () => {}), readSavedCellLogs: vi.fn(async () => {}), markFailureSeen: vi.fn(),
        ...over,
    } as never);
}
