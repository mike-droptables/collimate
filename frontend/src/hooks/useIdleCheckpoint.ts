import { useCallback, useEffect, useRef } from 'react';
import { checkpointCell } from '@/lib/api';
import { flushFileDocuments } from '@/lib/fileDocuments';

/**
 * Creates a version chain checkpoint after the user stops editing for
 * IDLE_MS. Also checkpoints on unmount (editor/renderer close).
 *
 * Active only when the caller passes `enabled=true` (CellEditor passes
 * `!readOnly`); a read-only cell never checkpoints from here.
 *
 * Explicit Save (Cmd+S / save button) is NOT a checkpoint — it just
 * waits for document edits to be accepted by the server. Versions land at idle,
 * editor close, or when a downstream consumer reads the cell as input
 * after the corresponding document save barrier.
 */
const IDLE_MS = 60_000;

export function useIdleCheckpoint(
    channel: string | undefined,
    cellId: string | undefined,
    enabled: boolean = true,
    /** Refetch history when this checkpoint creates a version. Live file
     *  revisions remain unchanged, so their events do not refresh history. */
    onCheckpointed?: () => void,
) {
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const dirtyRef = useRef(false);
    // onCheckpointed in a ref so doCheckpoint's closure stays identity-
    // stable (empty dep array) while still calling the latest callback.
    const onCheckpointedRef = useRef(onCheckpointed);
    onCheckpointedRef.current = onCheckpointed;

    // Hold channel/cellId on refs so the timer callback and unmount
    // cleanup always read the latest values. Without this, a timer
    // armed before activeChannel resolved would fire with an old
    // closure where channel === undefined and silently no-op — even
    // though by the time it fires, the channel IS known. Same for
    // cellId. The refs are updated synchronously here (not via
    // useEffect) so the very first render's ref already matches the
    // props.
    const channelRef = useRef(channel);
    const cellIdRef = useRef(cellId);
    channelRef.current = channel;
    cellIdRef.current = cellId;

    const doCheckpoint = useCallback(async () => {
        const ch = channelRef.current;
        const cid = cellIdRef.current;
        if (!ch || !cid || !dirtyRef.current) return;
        dirtyRef.current = false;
        if (timerRef.current) {
            clearTimeout(timerRef.current);
            timerRef.current = null;
        }
        try {
            await flushFileDocuments(ch,cid);
            const res = await checkpointCell(ch, cid);
            // Only refetch when a version actually landed — a no-op
            // checkpoint with unchanged accepted files returns {created: false}.
            if (res?.created) onCheckpointedRef.current?.();
        } catch (err) {
            // Best-effort — don't crash the UI if the checkpoint fails, but
            // stay dirty so the next edit, file close or unmount retries.
            // No timer re-arm: that would retry every 60s against a down
            // server.
            dirtyRef.current = true;
            console.warn('idle checkpoint failed:', err);
        }
    }, []);

    /** Call this whenever the user makes an edit. Resets the idle timer. */
    const notifyEdit = useCallback(() => {
        if (!enabled) return;
        dirtyRef.current = true;
        if (timerRef.current) clearTimeout(timerRef.current);
        timerRef.current = setTimeout(doCheckpoint, IDLE_MS);
    }, [enabled, doCheckpoint]);

    /** Force a checkpoint NOW. Fired by CellEditor when a file path leaves
     *  the open-tab set (a file tab closed), so closing a file commits a
     *  version of accepted files without waiting for the 60s idle timer.
     *  No-op when nothing is dirty. */
    const flushCheckpoint = useCallback(() => {
        void doCheckpoint();
    }, [doCheckpoint]);

    // Checkpoint on unmount (editor close) if there are pending edits.
    // Dep array is empty: the refs above carry the latest channel/cellId.
    useEffect(() => {
        return () => {
            if (timerRef.current) clearTimeout(timerRef.current);
            if (dirtyRef.current && channelRef.current && cellIdRef.current) {
                const ch = channelRef.current;
                const cid = cellIdRef.current;
                flushFileDocuments(ch,cid)
                    .then(() => checkpointCell(ch, cid))
                    .catch(err => console.warn('idle checkpoint failed:', err));
            }
        };
    }, []);

    return { notifyEdit, flushCheckpoint };
}
