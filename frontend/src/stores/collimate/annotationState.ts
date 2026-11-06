// The status pane's session state rules shared by the annotation slice and
// the pane's views: the default view, the finished-runs cap, an empty draft
// and whether a run reads its messages. Kept apart from the slice, which
// reaches the renderer hosts (lib/rendererAnnotation).

import type { CollimateState } from '../useCollimateStore';
import type { ComposerDraft, RunsPaneView } from './types';

/** The status pane's view when none is stored: the cell's runs. */
export const STATUS_VIEW: RunsPaneView = { view: 'status' };

/** Finished runs kept per cell (newest first); older ones are cleared. */
export const FINISHED_RUNS_PER_CELL = 50;

/** A notice under the pane's view clears itself after this long. */
export const ANNOTATION_NOTICE_MS = 6000;

/** A fresh draft: empty, queued, a new idempotency key. */
export function newDraft(): ComposerDraft {
    return {
        text: '', contextItems: [], mode: 'queued', below: false, replyTo: null, editing: null, expanded: false,
        operationId: crypto.randomUUID(), failedRequest: null, generation: 0, notice: null, sending: false,
    };
}

/** Whether the run reads its annotation context: its context's flag once
 *  loaded, else the live-runs row's; null when neither is known. */
export function listeningOf(s: Pick<CollimateState, 'annotationContexts' | 'activeRunsEverywhere'>, cellId: string, runId: string): boolean | null {
    const ctx = s.annotationContexts[cellId]?.[runId];
    if (ctx) return ctx.listening;
    for (const entry of s.activeRunsEverywhere) {
        const row = entry.runs.find(r => r.run_id === runId);
        if (row) return row.listening ?? null;
    }
    return null;
}
