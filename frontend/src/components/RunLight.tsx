// The cell header's light (C16): the cell's run state at a glance, and a
// way into its status pane. A 9px ring: hollow at rest, teal filled with a
// soft teal halo while a run is busy, brass while a run waits on the user
// (its next message, or an approval), a red ring while the cell's last
// finished run is a failure the user has not seen. Spinning up is the teal
// ring filled soft (live, not yet running); stopping the red ring filled
// soft (on its way down). As a button (a front card's header) it carries a
// 7px hit halo; a click, Enter or Space selects the cell and toggles the
// cell's status pane (components/cellEditor/RunsPane), whose status view
// marks the failure seen. Without a toggle (a back card, the overview) it
// is a plain status dot.

import React from 'react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { dotStatus, runsOnCell } from '@/lib/runningCell';
import { LIGHT_CLASS, runStateText, type LightState } from '@/lib/runLight';
import { failureStatusLabel, isFailureStatus, truncateFailureReason } from '@/lib/runStop';
import { runDisplayName } from '@/lib/runLabels';

/** The mock's `.light`: 9px, a 1.5px ring that the state fills. */
const DOT = 'relative flex-none w-[0.5625rem] h-[0.5625rem] rounded-full border-[1.5px] -mr-0.5 transition-[background-color,border-color,box-shadow] duration-300';
/** The button's 7px hit halo (`.light::after`) and its focus ring. */
const BUTTON = 'cursor-pointer outline-none after:absolute after:-inset-[0.4375rem] after:rounded-full focus-visible:outline-2 focus-visible:outline-teal focus-visible:outline-offset-[3px]';

export interface RunLightProps {
    cellId: string;
    /** The cell's status pane is open. */
    open?: boolean;
    /** Select the cell and toggle its status pane. Without it the light is
     *  a plain status dot (a back card, the overview). */
    onToggle?: () => void;
}

/** The header's light; the overview's status dot. */
export function RunLight({ cellId, open = false, onToggle }: RunLightProps) {
    const status = useCollimateStore(s => dotStatus(s, cellId));
    const runCount = useCollimateStore(s => runsOnCell(s, cellId).length);
    // One line per live run. A string selector, so an idle light does not
    // re-render on other runs' phase or stop changes.
    const runLines = useCollimateStore(s => runsOnCell(s, cellId)
        .map(run => `${runDisplayName(run)} — ${runStateText(run, s.stoppingRunIds, s.runPhases)}`)
        .join('\n'));
    const lastFinished = useCollimateStore(s => s.lastFinishedByCell[cellId]);
    const unseenFailure = !!lastFinished && !lastFinished.seen && isFailureStatus(lastFinished.status);
    const state: LightState = status ?? (unseenFailure ? 'failed' : 'idle');

    const lastRun = lastFinished
        ? `Last run: ${isFailureStatus(lastFinished.status)
            ? failureStatusLabel(lastFinished.status).toLowerCase()
            : lastFinished.status.replace(/_/g, ' ')}${lastFinished.reason ? ` · ${truncateFailureReason(lastFinished.reason)}` : ''}`
        : '';
    const summary = runCount > 0 ? `${runCount} running in this cell` : 'Nothing running in this cell';
    const title = [
        onToggle ? `${summary} · ${open ? 'hide' : 'show'} the status pane` : summary,
        runCount > 0 ? runLines : lastRun,
    ].filter(Boolean).join('\n');

    const look = LIGHT_CLASS[state];
    const data = { 'data-light-state': state, 'data-run-count': runCount };
    if (!onToggle) {
        return <span {...data} data-testid="run-light" title={title} className={cn(DOT, look.dot)} />;
    }
    const detail = runCount > 0 ? ` · ${summary}` : unseenFailure ? ' · last run failed' : '';
    return (
        <button
            type="button"
            {...data}
            data-testid="run-light"
            aria-expanded={open}
            aria-label={`${open ? 'Hide' : 'Show'} the status pane${detail}`}
            title={title}
            // The card selects and drags on pointerdown; the light is its
            // own gesture (its click selects the cell itself).
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => { e.stopPropagation(); onToggle(); }}
            className={cn(DOT, BUTTON, look.dot, look.hover)}
        />
    );
}
