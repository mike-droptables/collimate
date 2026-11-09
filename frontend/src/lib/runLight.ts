// The run light's vocabulary (C16), shared by the cell header's light and
// its status toggle's run count, the overview's status dot, the status
// section's rows, the annotation pane's run rail and the top bar's runs
// menu: a 9px ring hollow at rest, teal filled with a soft
// teal halo while busy, brass while waiting on the user, a red ring for an
// unseen failure; spinning up the teal ring filled soft, stopping the red
// ring filled soft.

import {
    AWAITING_APPROVAL_PHASE, AWAITING_INPUT_PHASE, isSpinningUpPhase, type DotStatus,
} from '@/lib/runningCell';
import type { RunRecord } from '@/lib/types';

/** What a light shows: the cell's dotStatus while runs are live, else
 *  `failed` (an unseen failure) or `idle`. */
export type LightState = DotStatus | 'failed' | 'idle';

export const LIGHT_CLASS: Record<LightState, { dot: string }> = {
    idle: { dot: 'border-ink-3 bg-transparent' },
    failed: { dot: 'border-red bg-transparent' },
    running: { dot: 'border-teal bg-teal shadow-[0_0_0_3px_var(--teal-soft)]' },
    spinning_up: { dot: 'border-teal bg-teal-soft' },
    awaiting_approval: { dot: 'border-brass bg-brass shadow-[0_0_0_3px_var(--brass-soft)]' },
    awaiting_input: { dot: 'border-brass bg-brass shadow-[0_0_0_3px_var(--brass-soft)]' },
    stopping: { dot: 'border-red bg-red-soft' },
};

/** A live state as ink (the run count beside the header's light):
 *  teal while busy, brass while waiting on the user, red while stopping. */
export const LIGHT_INK: Record<DotStatus, string> = {
    running: 'text-teal',
    spinning_up: 'text-teal',
    awaiting_approval: 'text-brass',
    awaiting_input: 'text-brass',
    stopping: 'text-red',
};

/** A live run's state from its phase and whether its stop is under way. */
export function lightStateOfPhase(phase: string | undefined, stopping: boolean): DotStatus {
    if (stopping) return 'stopping';
    if (phase === AWAITING_APPROVAL_PHASE) return 'awaiting_approval';
    if (phase === AWAITING_INPUT_PHASE) return 'awaiting_input';
    if (isSpinningUpPhase(phase)) return 'spinning_up';
    return 'running';
}

/** One live run's state. */
export function runLightState(
    run: Pick<RunRecord, 'runId'>,
    stopping: ReadonlySet<string>,
    phases: Record<string, { phase: string; message?: string }>,
): DotStatus {
    return lightStateOfPhase(phases[run.runId]?.phase, stopping.has(run.runId));
}

const STATE_TEXT: Record<DotStatus, string> = {
    stopping: 'stopping',
    awaiting_approval: 'awaiting approval',
    awaiting_input: 'awaiting your message',
    spinning_up: 'spinning up',
    running: 'running',
};

/** One live run's state, in words. */
export function runStateText(
    run: Pick<RunRecord, 'runId'>,
    stopping: ReadonlySet<string>,
    phases: Record<string, { phase: string; message?: string }>,
): string {
    return STATE_TEXT[runLightState(run, stopping, phases)];
}
