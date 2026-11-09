// The cell's light (C16): its run state at a glance, first inside the status
// pill in the cell header and on the full-window tab strip, alone in a back
// card's pill slot, and on the overview's card. A 9px ring:
// hollow at rest, teal filled with a soft teal halo while a run is busy,
// brass while a run waits on the user (its next message, or an approval),
// a red ring while the cell's last finished run is a failure the user has
// not seen. Spinning up is the teal ring filled soft (live, not yet
// running); stopping the red ring filled soft (on its way down). It is a
// passive dot everywhere, never a control of its own: the status pill that
// holds it (StatusToggle's one button) is the control, a press on the light
// is a press on the pill, and the status section it opens marks the
// failure seen.

import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { dotStatus, runsOnCell } from '@/lib/runningCell';
import { LIGHT_CLASS, runStateText, type LightState } from '@/lib/runLight';
import { failureStatusLabel, isFailureStatus, truncateFailureReason } from '@/lib/runStop';
import { runDisplayName } from '@/lib/runLabels';

/** The mock's `.light`: 9px, a 1.5px ring that the state fills. */
const DOT = 'relative flex-none w-[0.5625rem] h-[0.5625rem] rounded-full border-[1.5px] -mr-0.5 transition-[background-color,border-color,box-shadow] duration-300';

/** The light's state: the live runs' while any is live, else `failed` for
 *  an unseen failure of the last run, else `idle`. */
function useLightState(cellId: string): LightState {
    const status = useCollimateStore(s => dotStatus(s, cellId));
    const unseenFailure = useCollimateStore(s => {
        const last = s.lastFinishedByCell[cellId];
        return !!last && !last.seen && isFailureStatus(last.status);
    });
    return status ?? (unseenFailure ? 'failed' : 'idle');
}

export interface RunLightProps {
    cellId: string;
    className?: string;
}

/** The cell's light: a status dot titled with its live runs (or its last). */
export function RunLight({ cellId, className }: RunLightProps) {
    const state = useLightState(cellId);
    const runCount = useCollimateStore(s => runsOnCell(s, cellId).length);
    // One line per live run. A string selector, so an idle light does not
    // re-render on other runs' phase or stop changes.
    const runLines = useCollimateStore(s => runsOnCell(s, cellId)
        .map(run => `${runDisplayName(run)} — ${runStateText(run, s.stoppingRunIds, s.runPhases)}`)
        .join('\n'));
    const lastFinished = useCollimateStore(s => s.lastFinishedByCell[cellId]);

    const lastRun = lastFinished
        ? `Last run: ${isFailureStatus(lastFinished.status)
            ? failureStatusLabel(lastFinished.status).toLowerCase()
            : lastFinished.status.replace(/_/g, ' ')}${lastFinished.reason ? ` · ${truncateFailureReason(lastFinished.reason)}` : ''}`
        : '';
    const summary = runCount > 0 ? `${runCount} running in this cell` : 'Nothing running in this cell';
    const title = [summary, runCount > 0 ? runLines : lastRun].filter(Boolean).join('\n');

    return (
        <span
            data-testid="run-light"
            data-light-state={state}
            data-run-count={runCount}
            title={title}
            className={cn(DOT, LIGHT_CLASS[state].dot, className)}
        />
    );
}
