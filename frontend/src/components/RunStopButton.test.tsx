import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RunStopButton } from './RunStopButton';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { stopRun } from '@/lib/api';
import { FORCE_STOP_ARM_MS } from '@/lib/runStop';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    stopRun: vi.fn().mockResolvedValue(undefined),
}));

// A live run this button addresses. The button reads only the stopping
// flag; the host decides whether to render it.
const mountRunning = () => {
    useCollimateStore.setState({
        stoppingRunIds: new Set(),
    } as never);
    return render(<RunStopButton channel="ch" runId="run-1" runner="web" />);
};
// The one Stop/Force-Stop button, whatever label it currently wears.
const stopButton = () => screen.getByRole('button', { name: /^(Stop|Force stop) ‘web’$/ });
const STOP = 'Stop ‘web’';
const FORCE = 'Force stop ‘web’';
// The arm cooldown is aria-disabled (see the component's docblock for why
// it is deliberately NOT the disabled attribute).
const isInert = (el: HTMLElement) => el.getAttribute('aria-disabled') === 'true';

describe('RunStopButton, the status pane\'s Stop (C16)', () => {
    beforeEach(() => {
        useCollimateStore.setState({ stoppingRunIds: new Set(),
        } as never);
        vi.mocked(stopRun).mockReset();
        vi.mocked(stopRun).mockResolvedValue(undefined);
    });

    afterEach(() => {
        // Fake-timer tests must never leak their clock into the rest of
        // the suite; a no-op when timers were never faked.
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it('first click grace-stops (no force) and flips the button to the inert Force stop (C16)', async () => {
        const user = userEvent.setup();
        mountRunning();

        // Idle running state: the button reads "Stop" and is live.
        const stop = screen.getByRole('button', { name: STOP });
        expect(stop).toBeEnabled();
        expect(isInert(stop)).toBe(false);
        expect(stop).toHaveAttribute('title', 'Stop web');

        await user.click(stopButton());

        // Grace stop: called with exactly (channel, runId) — no force arg.
        expect(stopRun).toHaveBeenCalledTimes(1);
        expect(vi.mocked(stopRun).mock.calls[0]).toEqual(['ch', 'run-1']);
        // Store flipped into "stopping".
        expect(useCollimateStore.getState().stoppingRunIds.has('run-1')).toBe(true);
        // Button now reads "Force Stop" but is inert until the cooldown arms.
        const forceBtn = screen.getByRole('button', { name: FORCE });
        expect(isInert(forceBtn)).toBe(true);
        expect(forceBtn).toHaveAttribute('title', 'Stopping… force stop available in a moment');
    });

    // The arm cooldown is a real setTimeout, so these tests fake the clock
    // to control it. user-event's async click deadlocks against faked
    // timers in this React 19 stack, so they drive clicks with the
    // synchronous fireEvent.
    it('a second click before the arm delay does not force-stop (C16)', () => {
        vi.useFakeTimers();
        mountRunning();

        fireEvent.click(stopButton()); // grace stop → arms the cooldown

        const forceBtn = screen.getByRole('button', { name: FORCE });
        expect(isInert(forceBtn)).toBe(true);
        // Deliberately NOT the disabled attribute: the click must still
        // reach the handler (so a host can count it) and be refused there.
        expect(forceBtn).not.toBeDisabled();

        fireEvent.click(forceBtn);
        act(() => { vi.advanceTimersByTime(FORCE_STOP_ARM_MS - 1); });
        fireEvent.click(forceBtn);

        expect(stopRun).toHaveBeenCalledTimes(1); // only the graceful call
        expect(stopRun).not.toHaveBeenCalledWith('ch', 'run-1', true);
        expect(isInert(screen.getByRole('button', { name: FORCE }))).toBe(true);
    });

    it('after the arm delay the second click force-stops (force=true) (C16)', () => {
        vi.useFakeTimers();
        mountRunning();

        fireEvent.click(stopButton()); // grace stop
        expect(isInert(screen.getByRole('button', { name: FORCE }))).toBe(true);

        // Past the cooldown → the effect arms force stop.
        act(() => { vi.advanceTimersByTime(FORCE_STOP_ARM_MS + 1); });

        const forceBtn = screen.getByRole('button', { name: FORCE });
        expect(isInert(forceBtn)).toBe(false);
        expect(forceBtn).toHaveAttribute('title', 'Force stop (kill immediately)');

        fireEvent.click(forceBtn);

        expect(stopRun).toHaveBeenCalledTimes(2);
        expect(stopRun).toHaveBeenNthCalledWith(2, 'ch', 'run-1', true);
    });

    it('a rejected force stop clears the stopping state (C16)', async () => {
        vi.useFakeTimers();
        // Grace call resolves; the force call rejects (e.g. host down / 404).
        vi.mocked(stopRun).mockImplementation((_ch: string, _id: string, force?: boolean) =>
            force ? Promise.reject(new Error('force kill failed')) : Promise.resolve(undefined),
        );
        const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

        mountRunning();

        fireEvent.click(stopButton()); // grace stop
        expect(useCollimateStore.getState().stoppingRunIds.has('run-1')).toBe(true);

        act(() => { vi.advanceTimersByTime(FORCE_STOP_ARM_MS + 1); }); // arm force stop
        fireEvent.click(screen.getByRole('button', { name: FORCE }));

        // The rejection's .catch runs on the microtask queue and calls
        // clearStopping so the cell isn't wedged in "Stopping…" forever.
        await act(async () => { await Promise.resolve(); await Promise.resolve(); });

        expect(stopRun).toHaveBeenCalledWith('ch', 'run-1', true);
        expect(useCollimateStore.getState().stoppingRunIds.has('run-1')).toBe(false);
        expect(errSpy).toHaveBeenCalled();
        // Back to a plain, live Stop so the user can try again.
        expect(isInert(screen.getByRole('button', { name: STOP }))).toBe(false);
    });

    it('a rejected graceful stop reverts to Stop so the user can retry (C16)', async () => {
        vi.mocked(stopRun).mockRejectedValue(new Error('host unreachable'));
        const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
        const user = userEvent.setup();
        mountRunning();

        await user.click(stopButton());
        await act(async () => { await Promise.resolve(); await Promise.resolve(); });

        expect(useCollimateStore.getState().stoppingRunIds.has('run-1')).toBe(false);
        expect(screen.getByRole('button', { name: STOP })).toBeInTheDocument();
        expect(errSpy).toHaveBeenCalled();
    });

    it('a click swallows the event before the run\'s row sees it (C16)', () => {
        const onCardClick = vi.fn();
        useCollimateStore.setState({ stoppingRunIds: new Set(),
        } as never);
        render(
            <div onClick={onCardClick}>
                <RunStopButton channel="ch" runId="run-1" runner="web" />
            </div>,
        );
        fireEvent.click(stopButton());
        expect(stopRun).toHaveBeenCalledTimes(1);
        expect(onCardClick).not.toHaveBeenCalled();
    });

    it('Stop is the raised primary that turns red on hover; Force stop stays red (C16)', () => {
        mountRunning();
        const stop = stopButton();
        expect(stop.className).toMatch(/\bborder-line-2\b/);
        expect(stop.className).toMatch(/\bbg-sheet\b/);
        expect(stop.className).toMatch(/\btext-ink\b/);
        expect(stop.className).toContain('h-[1.625rem]');
        expect(stop.className).toContain('hover:text-red');
        expect(stop.className).toContain('hover:bg-red-soft');
        expect(stop.className).not.toMatch(RETIRED_TOKENS);
        act(() => { useCollimateStore.setState({ stoppingRunIds: new Set(['run-1']) } as never); });
        const inert = stopButton();
        expect(inert.className).toMatch(/\btext-ink-3\b/);
    });

    it('armed Force stop is red on the skin tokens (C16)', () => {
        vi.useFakeTimers();
        mountRunning();
        act(() => { useCollimateStore.setState({ stoppingRunIds: new Set(['run-1']) } as never); });
        act(() => { vi.advanceTimersByTime(FORCE_STOP_ARM_MS); });
        const force = screen.getByRole('button', { name: FORCE });
        expect(isInert(force)).toBe(false);
        expect(force.className).toMatch(/\btext-red\b/);
        expect(force.className).toMatch(/\bborder-red\b/);
        expect(force.className).not.toMatch(RETIRED_TOKENS);
    });

    it('reads Stop ‘runner’ with the ↵ hint, arms the post-stop lock and reports the graceful stop (C16)', () => {
        vi.useFakeTimers();
        const onStopped = vi.fn();
        useCollimateStore.setState({ stoppingRunIds: new Set() } as never);
        render(<RunStopButton channel="ch" runId="run-1" runner="web" onStopped={onStopped} />);
        const stop = screen.getByTestId('run-stop');
        expect(stop).toBe(stopButton());
        expect(stop.textContent).toBe('Stop ‘web’↵');
        expect(stop.hasAttribute('data-arms-stop-lock')).toBe(true);
        fireEvent.click(stop);
        expect(onStopped).toHaveBeenCalledTimes(1);
        // The force kill is not a graceful stop.
        act(() => { vi.advanceTimersByTime(FORCE_STOP_ARM_MS + 1); });
        fireEvent.click(stopButton());
        expect(stopRun).toHaveBeenLastCalledWith('ch', 'run-1', true);
        expect(onStopped).toHaveBeenCalledTimes(1);
    });

    it('compact reads the glyph and the verb alone, Force stop once stopping; its name and title still say the runner', () => {
        vi.useFakeTimers();
        useCollimateStore.setState({ stoppingRunIds: new Set() } as never);
        render(<RunStopButton channel="ch" runId="run-1" runner="web" compact />);
        const stop = screen.getByTestId('run-stop');
        expect(stop.textContent).toBe('Stop');
        expect(stop.querySelector('svg')).not.toBeNull();
        expect(stop.querySelector('kbd')).toBeNull();
        expect(stop).toHaveAccessibleName(STOP);
        expect(stop).toHaveAttribute('title', 'Stop web');
        fireEvent.click(stop);
        expect(stopRun).toHaveBeenCalledWith('ch', 'run-1');
        expect(screen.getByTestId('run-stop').textContent).toBe('Force stop');
        expect(screen.getByTestId('run-stop')).toHaveAccessibleName(FORCE);
        act(() => { vi.advanceTimersByTime(FORCE_STOP_ARM_MS + 1); });
        expect(screen.getByTestId('run-stop').textContent).toBe('Force stop');
        expect(screen.getByTestId('run-stop')).toHaveAttribute('title', 'Force stop (kill immediately)');
    });
});
