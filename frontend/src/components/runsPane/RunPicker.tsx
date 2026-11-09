// The annotation pane's run picker, first on its rail: which run the pane
// shows (a dot and the run's name), or All. It opens the shared Menu: All,
// then each live run of the cell (its dot, runner and short id); a finished
// run the pane is showing is listed too, dimmed. Picking one keeps the
// current option (Default, Current file, Log).

import React, { useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { Menu } from '@/components/ui/Menu';
import { menuItem } from '@/components/ui/menuStyles';
import { runsOnCell } from '@/lib/runningCell';
import { runDisplayName, runShortId } from '@/lib/runLabels';
import { LIGHT_CLASS, runLightState, type LightState } from '@/lib/runLight';
import { isFailureStatus } from '@/lib/runStop';
import type { AnnotationPaneOption } from '@/stores/collimate/types';
import { railTitle } from './paneChrome';
import { finishedOf, runnerOf, usePaneRun, type PaneRun } from './paneRun';

const dot = 'block flex-none w-1.5 h-1.5 rounded-full border shadow-none';

export interface RunPickerProps {
    cellId: string;
    /** The run the pane shows; null for All. */
    runId: string | null;
    option: AnnotationPaneOption;
}

/** A pane run's light: a live run's state, a finished run's outcome. */
function lightOf(paneRun: PaneRun, stopping: ReadonlySet<string>, phases: Parameters<typeof runLightState>[2]): LightState {
    const finished = finishedOf(paneRun.run);
    if (finished || !paneRun.live) return finished && isFailureStatus(finished.status) ? 'failed' : 'idle';
    return runLightState(paneRun.run, stopping, phases);
}

export function RunPicker({ cellId, runId, option }: RunPickerProps) {
    const current = usePaneRun(cellId, runId ?? '');
    const runs = useCollimateStore(useShallow(s => runsOnCell(s, cellId)));
    const stopping = useCollimateStore(s => s.stoppingRunIds);
    const phases = useCollimateStore(s => s.runPhases);
    const light: LightState = runId && current ? lightOf(current, stopping, phases) : 'idle';
    const [open, setOpen] = useState(false);
    const anchorRef = useRef<HTMLButtonElement>(null);
    const bodyRef = useRef<HTMLDivElement>(null);

    const name = runId && current ? runDisplayName(current.run) : 'All';
    // The finished run on show, listed after the live ones.
    const ended = runId && current && !runs.some(r => r.runId === runId) ? current.run : null;

    const pick = (next: string | null) => {
        setOpen(false);
        if (next === runId) {
            anchorRef.current?.focus({ preventScroll: true });
            return;
        }
        useCollimateStore.getState().setAnnotationPaneView(cellId, next === null ? { view: 'all', option } : { view: 'run', runId: next, option });
    };

    const onBodyKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
        if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Home' && e.key !== 'End') return;
        e.preventDefault();
        e.stopPropagation();
        const rows = Array.from(bodyRef.current?.querySelectorAll<HTMLElement>('[data-pick-row]') ?? []);
        if (rows.length === 0) return;
        const k = rows.indexOf(document.activeElement as HTMLElement);
        const next = e.key === 'Home' ? 0 : e.key === 'End' ? rows.length - 1
            : e.key === 'ArrowDown' ? (k + 1) % rows.length : (k <= 0 ? rows.length - 1 : k - 1);
        rows[next].focus();
    };

    return (
        <>
            <button
                ref={anchorRef}
                type="button"
                data-testid="annotation-run-picker"
                aria-haspopup="dialog"
                aria-expanded={open}
                aria-label={`Showing ${name} · pick a run`}
                title="Pick the run to show"
                onClick={() => setOpen(v => !v)}
                className={cn(railTitle, 'min-w-0 flex items-center gap-1.5 rounded-xs hover:bg-sheet-2')}
            >
                {runId && <i aria-hidden className={cn(dot, LIGHT_CLASS[light].dot)} />}
                <span className="truncate">{name}</span>
                <ChevronDown aria-hidden className="w-3 h-3 flex-none text-ink-3" strokeWidth={1.8} />
            </button>
            <Menu anchor={anchorRef} open={open} onClose={() => setOpen(false)} role="dialog" label="Show run" align="start" minWidth={200}>
                <div ref={bodyRef} onKeyDown={onBodyKeyDown} className="flex flex-col outline-none">
                    <button
                        type="button"
                        data-testid="annotation-pick-all"
                        data-pick-row
                        aria-current={runId === null || undefined}
                        onClick={() => pick(null)}
                        className={cn(menuItem, runId === null && 'text-ink')}
                    >
                        All
                    </button>
                    {runs.map(run => (
                        <button
                            key={run.runId}
                            type="button"
                            data-testid="annotation-pick-run"
                            data-run-id={run.runId}
                            data-pick-row
                            aria-current={run.runId === runId || undefined}
                            onClick={() => pick(run.runId)}
                            className={cn(menuItem, run.runId === runId && 'text-ink')}
                        >
                            <i aria-hidden className={cn(dot, LIGHT_CLASS[runLightState(run, stopping, phases)].dot)} />
                            <span className="min-w-0 truncate">
                                {runnerOf(run)}
                                <span className="ml-1.5 font-mono text-[0.6875rem] text-ink-3">{runShortId(run)}</span>
                            </span>
                        </button>
                    ))}
                    {ended && (
                        <button
                            type="button"
                            data-testid="annotation-pick-run"
                            data-run-id={ended.runId}
                            data-ended="true"
                            data-pick-row
                            aria-current
                            onClick={() => pick(ended.runId)}
                            className={cn(menuItem, 'opacity-[0.55] text-ink')}
                        >
                            <i aria-hidden className={cn(dot, LIGHT_CLASS[light].dot)} />
                            <span className="min-w-0 truncate">
                                {runnerOf(ended)}
                                <span className="ml-1.5 font-mono text-[0.6875rem] text-ink-3">{runShortId(ended)}</span>
                            </span>
                        </button>
                    )}
                </div>
            </Menu>
        </>
    );
}
