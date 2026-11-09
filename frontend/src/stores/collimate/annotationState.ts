// The annotation pane's and the status section's session state rules
// shared by the annotation slice and the components: the pane's default
// view, the finished-runs cap, an empty draft and whether a run reads its
// messages. Kept apart from the slice, which
// reaches the renderer hosts (lib/rendererAnnotation).

import type { CollimateState } from '../useCollimateStore';
import type { ComposerDraft, AnnotationPaneView } from './types';

/** The annotation pane's view when none is stored, or when the stored run
 *  view names a run the pane no longer knows: All, Default. */
export const ALL_VIEW: AnnotationPaneView = { view: 'all', option: 'default' };

/** Finished runs kept per cell (newest first); older ones are cleared. */
export const FINISHED_RUNS_PER_CELL = 50;

/** A notice under the pane's view clears itself after this long. */
export const ANNOTATION_NOTICE_MS = 6000;

/** A fresh draft: empty, queued, closed, a new idempotency key. */
export function newDraft(): ComposerDraft {
    return {
        text: '', contextItems: [], mode: 'queued', below: false, replyTo: null, editing: null, open: false, expanded: false,
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
