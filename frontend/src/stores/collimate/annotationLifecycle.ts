import type { useCollimateStore } from '../useCollimateStore';
import { activeTab } from '@/lib/cellLayout';

/** A deferred status-pane open is dropped after this long. */
export const PENDING_RUNS_PANE_OPEN_MS = 15_000;

/**
 * The annotation slice's store subscriber (installed once by
 * useCollimateStore):
 * - every layout change notes each cell's active file tab (`lastFileTab`:
 *   Current file and Add annotation's file); an empty, history or compare
 *   tab keeps the file before it;
 * - a status pane deferred by the runs menu (`pendingRunsPaneOpen`, after a
 *   channel switch) opens once its channel is active and its cell is
 *   there, or is dropped after PENDING_RUNS_PANE_OPEN_MS;
 * - a channel switch abandons every Add annotation in flight and clears the
 *   notices.
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

            const pending = state.pendingRunsPaneOpen;
            if (pending && (pending !== prev.pendingRunsPaneOpen || state.state !== prev.state
                || state.cellDisplaysReady !== prev.cellDisplaysReady || state.activeChannel !== prev.activeChannel
                || state.cellLayouts !== prev.cellLayouts)) {
                if (Date.now() - pending.at > PENDING_RUNS_PANE_OPEN_MS) {
                    store.setState({ pendingRunsPaneOpen: null });
                } else if (state.activeChannel?.id === pending.channel && state.cellDisplaysReady && state.locateCell(pending.cellId)) {
                    store.setState({ pendingRunsPaneOpen: null });
                    store.getState().openRunsPaneOn(pending.cellId, pending.view, { focus: pending.focus });
                }
            }

            if (state.activeChannel?.id !== prev.activeChannel?.id) {
                const requests = Object.values(state.annotationRequests);
                if (requests.length > 0 || Object.keys(state.annotationNotice).length > 0) {
                    store.setState({ annotationRequests: {}, annotationNotice: {} });
                    for (const request of requests) request.controller.abort();
                }
            }
        });
    }
    return { subscribe };
}
