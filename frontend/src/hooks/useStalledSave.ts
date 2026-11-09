import { useEffect, useState } from 'react';

/** How long edits may wait for the server before the editor says so. */
export const SAVE_STALL_MS = 5000;

/**
 * True once `pending` has held for SAVE_STALL_MS with no change of
 * `progress` (the document's savedSeq); false again the moment either
 * moves. While true, `retry` runs at once and then every SAVE_STALL_MS, so
 * a banner reading "still trying" is literally so.
 *
 * Saving is otherwise silent: no status row, no receipt. The stall is the
 * one moment the user should hear about it, and only then.
 */
export function useStalledSave(pending: boolean, progress: number, retry: () => Promise<void>): boolean {
    // The progress value the stall timer fired at; a later progress value
    // means a save got through since, which un-stalls without a reset.
    const [stalledAt, setStalledAt] = useState<number | null>(null);
    useEffect(() => {
        if (!pending) return;
        const timer = setTimeout(() => setStalledAt(progress), SAVE_STALL_MS);
        return () => clearTimeout(timer);
    }, [pending, progress]);
    const stalled = pending && stalledAt === progress;
    useEffect(() => {
        if (!stalled) return;
        const attempt = () => void retry().catch(() => undefined);
        attempt();
        const interval = setInterval(attempt, SAVE_STALL_MS);
        return () => clearInterval(interval);
    }, [stalled, retry]);
    return stalled;
}
