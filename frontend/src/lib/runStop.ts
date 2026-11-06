// Run stop contracts: the Stop timings, and the status labels the cell's run
// light and its status pane share.

/** After a graceful Stop, the second click on the run's Stop / Force Stop
 *  button (RunStopButton) stays inert this long before it is honoured as a
 *  force-kill — an accidental double-click must never skip graceful
 *  shutdown. */
export const FORCE_STOP_ARM_MS = 1000;

/** Stop and Stop all in the cell's status pane stay inert this long after
 *  a Stop click, re-armed by every swallowed click: when the run ends a
 *  different Stop lands under the pointer, and a burst of clicks must never
 *  reach it. */
export const POST_STOP_LOCK_MS = 500;

/** Terminal statuses of a user stop, never a failure: the runs row's and
 *  the run_status stream's 'stopped', or the stop type a client that saw
 *  the stop event records (lib/runAttach). Anything else non-completed —
 *  including 'timed_out' and 'interrupted' — is a genuine failure. */
export function isTerminatedStatus(status: string): boolean {
    return status === 'stopped' || status === 'force_stopped' || status === 'gracefully_stopped';
}

/** A terminal status the user should be told about: neither a success nor
 *  a user stop. */
export function isFailureStatus(status: string): boolean {
    return status !== 'completed' && !isTerminatedStatus(status);
}

/** Human label for a non-success terminal status — the prefix of the
 *  "<label> · <failure_reason>" line the cell's run light and the top
 *  status show. Anything unrecognized reads as a plain failure. */
export function failureStatusLabel(status: string): string {
    if (status === 'timed_out') return 'Timed out';
    if (status === 'interrupted') return 'Interrupted';
    return 'Failed';
}

/** Clamp a failure_reason for inline display (~120 chars); the full text
 *  belongs in the element's title. */
export function truncateFailureReason(reason: string, max = 120): string {
    return reason.length > max ? `${reason.slice(0, max - 1).trimEnd()}…` : reason;
}
