import { useEffect, useCallback } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';

interface UseRunRequestParams {
    /** The column this cell renders in (display id: a real stack id, or
     *  `fan-<cellId>` for a fan column). Matched against the STACK_RUN
     *  request, which names the display selection — so a fanned stack
     *  opens the runners panel on exactly its selected column. */
    displayStackId: string;
    /** The cell this column renders — the one the runners panel opens on. */
    cellId: string;
    isFront: boolean;
    /** The host renders this cell read-only (review / cell diff) — the
     *  STACK_RUN shortcut must not open a panel on it. */
    readOnly?: boolean;
}

export function useRunRequest({
    displayStackId,
    cellId,
    isFront,
    readOnly,
}: UseRunRequestParams) {
    const openPickSession = useCollimateStore(s => s.openPickSession);

    const pendingGeneratorRunnerStackId = useCollimateStore(s => s.pendingGeneratorRunnerStackId);
    const clearGeneratorRunnerRequest = useCollimateStore(s => s.clearGeneratorRunnerRequest);

    /**
     * Open the runners panel over the cell's body (the cell's panel of kind
     * `runners`). Available while runs are live on the cell: several runs
     * may share it (the panel warns about collisions and offers a new cell).
     */
    const openRunnersPanel = useCallback(() => {
        // Read-only hosts (review / cell diff) never mount the surface —
        // opening would strand an invisible persisted session.
        if (readOnly) return;
        openPickSession(cellId, 'runners');
    }, [cellId, openPickSession, readOnly]);

    useEffect(() => {
        if (pendingGeneratorRunnerStackId === displayStackId && isFront) {
            openRunnersPanel();
            clearGeneratorRunnerRequest();
        }
    }, [pendingGeneratorRunnerStackId, displayStackId, isFront, openRunnersPanel, clearGeneratorRunnerRequest]);

    // openRunnersPanel is fired internally by the STACK_RUN useEffect
    // above; nothing outside this hook calls it. The hook is consumed
    // purely for that side-effect, so it exposes no return value.
}
