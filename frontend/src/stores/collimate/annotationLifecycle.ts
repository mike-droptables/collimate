import type { useCollimateStore } from '../useCollimateStore';
import { activeTab } from '@/lib/cellLayout';

/** A deferred annotation-pane or status-section open is dropped after this long. */
export const PENDING_OPEN_MS = 15_000;

/**
 * The annotation slice's store subscriber (installed once by
 * useCollimateStore):
 * - every layout change notes each cell's active file tab (`lastFileTab`:
 *   Current file and Add annotation's file); an empty, history or compare
 *   tab keeps the file before it;
 * - a channel switch abandons every Add annotation in flight, clears the
 *   notices and drops every cell's status section (open and expanded row);
 * - a deferred open runs once its channel is active and its cell is there,
 *   or is dropped after PENDING_OPEN_MS: a status section
 *   (`pendingStatusSectionOpen`, after a channel switch from the top bar's
 *   runs menu, or a run start in a cell not yet on the canvas) with its run
 *   row and focus as asked; an annotation pane (`pendingAnnotationPaneOpen`)
 *   asked for while its cell was not yet located in the active channel;
 * - a status section's expanded row whose run the section no longer lists
 *   (neither live on the cell, nor among its finished runs, nor just ended
 *   on its way there) is collapsed.
 */
export function createAnnotationLifecycle(getStore: () => typeof useCollimateStore) {
    function subscribe() {
        getStore().subscribe((state, prev) => {
            const store = getStore();
            if (state.cellLayouts !== prev.cellLayouts) {
                let next: Record<string, string> | null = null;
                for (const [cellId, layout] of Object.entries(state.cellLayouts)) {
                    if (layout === prev.cellLayouts[cellId]) continue;
                    const tab = activeTab(layout);
                    if (tab?.kind !== 'file' || state.lastFileTab[cellId] === tab.path) continue;
                    next ??= { ...state.lastFileTab };
                    next[cellId] = tab.path;
                }
                if (next) store.setState({ lastFileTab: next });
            }

            if (state.activeChannel?.id !== prev.activeChannel?.id) {
                const requests = Object.values(state.annotationRequests);
                if (requests.length > 0 || Object.keys(state.annotationNotice).length > 0 || Object.keys(state.statusSection).length > 0) {
                    store.setState({ annotationRequests: {}, annotationNotice: {}, statusSection: {} });
                    for (const request of requests) request.controller.abort();
                }
            }

            const reachable = (p: { channel: string; cellId: string }) =>
                state.activeChannel?.id === p.channel && state.cellDisplaysReady && !!state.locateCell(p.cellId);
            const settling = state.state !== prev.state || state.cellDisplaysReady !== prev.cellDisplaysReady
                || state.activeChannel !== prev.activeChannel || state.cellLayouts !== prev.cellLayouts;

            const pending = state.pendingAnnotationPaneOpen;
            if (pending && (pending !== prev.pendingAnnotationPaneOpen || settling)) {
                if (Date.now() - pending.at > PENDING_OPEN_MS) {
                    store.setState({ pendingAnnotationPaneOpen: null });
                } else if (reachable(pending)) {
                    store.setState({ pendingAnnotationPaneOpen: null });
                    store.getState().openAnnotationPaneOn(pending.cellId, pending.view, { focus: pending.focus });
                }
            }

            const pendingSection = state.pendingStatusSectionOpen;
            if (pendingSection && (pendingSection !== prev.pendingStatusSectionOpen || settling)) {
                if (Date.now() - pendingSection.at > PENDING_OPEN_MS) {
                    store.setState({ pendingStatusSectionOpen: null });
                } else if (reachable(pendingSection)) {
                    store.setState({ pendingStatusSectionOpen: null });
                    store.getState().openStatusSection(pendingSection.cellId, { expandRunId: pendingSection.expandRunId, focus: pendingSection.focus });
                }
            }

            if (state.runs !== prev.runs || state.finishedRuns !== prev.finishedRuns || state.statusSection !== prev.statusSection) {
                const cur = store.getState();
                let next: typeof cur.statusSection | null = null;
                for (const [cellId, section] of Object.entries(cur.statusSection)) {
                    const runId = section.expandedRunId;
                    // A run that ended with a status is in endedRuns before
                    // it reaches finishedRuns (endRun, then addFinishedRun).
                    if (!runId || cur.runs[runId]?.cellId === cellId || cur.endedRuns[runId]?.cellId === cellId
                        || cur.finishedRuns[cellId]?.some(r => r.runId === runId)) continue;
                    next ??= { ...cur.statusSection };
                    next[cellId] = { ...section, expandedRunId: null };
                }
                if (next) store.setState({ statusSection: next });
            }
        });
    }
    return { subscribe };
}
