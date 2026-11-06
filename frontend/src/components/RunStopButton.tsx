import React, { useEffect, useState } from 'react';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { stopRun } from '@/lib/api';
import { FORCE_STOP_ARM_MS } from '@/lib/runStop';
import { kbd } from '@/components/ui/formStyles';
import { STOP_LOCK_ARMS_ATTR } from '@/hooks/usePostStopLock';

export interface RunStopButtonProps {
    /** The run's channel (RunRecord.channel) and id. */
    channel: string;
    runId: string;
    /** The run's name as the button reads it: Stop ‘<runner>’. */
    runner: string;
    /** Glyph and verb only (a narrow rail that names the run itself); the
     *  accessible name and title still name the runner. */
    compact?: boolean;
    /** A graceful stop went out (the first click; never the force kill). */
    onStopped?: () => void;
    className?: string;
    ref?: React.Ref<HTMLButtonElement>;
}

// The mock's `i-stop` glyph.
const StopGlyph = () => (
    <svg viewBox="0 0 16 16" className="w-3.5 h-3.5 flex-none" aria-hidden>
        <rect x="4" y="4" width="8" height="8" rx="1.2" fill="currentColor" />
    </svg>
);

/** The mock's `rp-go del`: the raised sheet with an inset edge, its glyph in
 *  ink-3; it turns red on hover. */
const stopLook = 'flex-auto min-w-0 inline-flex items-center justify-start gap-1.5 h-[1.625rem] px-2.5 rounded-sm border shadow-[inset_0_0_0_0.5px_var(--line)] bg-sheet text-[0.78125rem] font-semibold whitespace-nowrap transition-colors';

/**
 * A live run's Stop / Force stop, the status pane's primary (C16). First
 * click grace-stops (SIGTERM: the generator's `finally` runs); the button
 * then reads "Force stop" and sits inert for FORCE_STOP_ARM_MS, after which
 * a second click kills at once.
 *
 * The cooldown is `aria-disabled`, not `disabled`, on purpose: a disabled
 * button dispatches no click events, so a burst of impatient clicks during
 * the cooldown would be invisible to the section's post-stop lock
 * (usePostStopLock), which runs from the LAST click. The click handler
 * enforces the cooldown itself instead.
 */
export function RunStopButton({ channel, runId, runner, compact = false, onStopped, className, ref }: RunStopButtonProps) {
    const isStopping = useCollimateStore(s => s.stoppingRunIds.has(runId));
    const startStopping = useCollimateStore(s => s.startStopping);
    const clearStopping = useCollimateStore(s => s.clearStopping);

    const [forceStopArmed, setForceStopArmed] = useState(false);
    // Leaving the stopping state (the run ended, or a failed stop reverted
    // it) disarms — reset during render, React's sanctioned way to derive
    // a reset from a changed input, so the next Stop starts a fresh cooldown.
    if (!isStopping && forceStopArmed) setForceStopArmed(false);
    useEffect(() => {
        if (!isStopping) return;
        const t = setTimeout(() => setForceStopArmed(true), FORCE_STOP_ARM_MS);
        return () => clearTimeout(t);
    }, [isStopping]);

    const inert = isStopping && !forceStopArmed;

    const handleClick = (e: React.MouseEvent<HTMLButtonElement>) => {
        // The card selects and drags on pointer events, and the section's
        // rows select on click; a stop click is its own gesture.
        e.stopPropagation();
        if (isStopping) {
            // Second click — force kill, but only after the cooldown has
            // armed it (guards against an accidental double-click that
            // would otherwise skip graceful shutdown entirely).
            if (!forceStopArmed) return;
            stopRun(channel, runId, true).catch(err => {
                // Force-kill failed — the subprocess may already be dead
                // (common: user clicked Stop just as it exited naturally,
                // 404) or the host is unreachable. We can't rely on the
                // SSE endRun to clear the "stopping" flag when the host
                // is down, so clear it here too so the cell isn't wedged
                // in "Stopping…" forever.
                console.error('Force stopRun failed:', err);
                clearStopping(runId);
            });
        } else {
            // First click — graceful stop. startStopping flips the button
            // to "Force stop"; on failure we must revert so the user can
            // retry (otherwise the button lies about state).
            startStopping(runId);
            stopRun(channel, runId).catch(err => {
                console.error('stopRun failed:', err);
                clearStopping(runId);
            });
            onStopped?.();
        }
    };

    const verb = isStopping ? 'Force stop' : 'Stop';
    return (
        <button
            ref={ref}
            type="button"
            data-testid="run-stop"
            {...{ [STOP_LOCK_ARMS_ATTR]: '' }}
            onClick={handleClick}
            aria-disabled={inert || undefined}
            aria-label={`${verb} ‘${runner}’`}
            className={cn(
                stopLook,
                inert
                    ? 'border-line-2 text-ink-3 opacity-45 cursor-not-allowed [&_svg]:text-ink-3'
                    : isStopping
                        ? 'border-red text-red bg-red-soft [&_svg]:text-red'
                        : 'border-line-2 text-ink [&_svg]:text-ink-3 hover:bg-red-soft hover:text-red hover:[&_svg]:text-red',
                className,
            )}
            title={
                inert
                    ? 'Stopping… force stop available in a moment'
                    : isStopping
                        ? 'Force stop (kill immediately)'
                        : `Stop ${runner}`
            }
        >
            <StopGlyph />
            {compact ? <span>{verb}</span> : (
                <>
                    <span className="truncate">{verb} ‘{runner}’</span>
                    <kbd aria-hidden className={cn(kbd, 'ml-auto font-normal')}>↵</kbd>
                </>
            )}
        </button>
    );
}
