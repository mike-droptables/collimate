import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { RunLight } from './RunLight';
import runLightSource from './RunLight.tsx?raw';
import runLightVocabulary from '@/lib/runLight.ts?raw';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { RunRecord } from '@/lib/types';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

/** The retired palette and colour literals: none may come back (S04). */
const COLOUR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(rgba?|hsla?)\(|\b(bg|text|border)-(white|black)\b|\b(slate|zinc|gray|neutral|stone)-\d/;

const run = (key: 'a' | 'b'): RunRecord => ({
    runId: `run-${key}`, cellId: 'cell-1', channel: 'Alpha', runnerId: `runner-${key}`, runnerName: `Runner ${key.toUpperCase()}`,
    generatorName: 'synthetic', label: `runner-${key}-${key.repeat(8)}`, folder: `_runs/runner-${key}-${key.repeat(8)}`,
});
const RUN_A = run('a');
const RUN_B = run('b');

const seed = (over: Record<string, unknown> = {}) => useCollimateStore.setState({
    runs: {}, stoppingRunIds: new Set(), runPhases: {}, lastFinishedByCell: {}, ...over,
} as never);

const light = () => screen.getByTestId('run-light');
const classes = () => light().className.split(/\s+/);

beforeEach(() => seed());
afterEach(cleanup);

describe('RunLight states (C16)', () => {
    it('idle is a 9px hollow ring on ink-3 (C16)', () => {
        render(<RunLight cellId="cell-1" />);
        expect(light().getAttribute('data-light-state')).toBe('idle');
        expect(classes()).toEqual(expect.arrayContaining(['w-[0.5625rem]', 'h-[0.5625rem]', 'rounded-full', 'border-[1.5px]', 'border-ink-3', 'bg-transparent']));
    });

    it('a busy run fills it teal with a teal-soft halo (C16)', () => {
        seed({ runs: { 'run-a': RUN_A } });
        render(<RunLight cellId="cell-1" />);
        expect(light().getAttribute('data-light-state')).toBe('running');
        expect(classes()).toEqual(expect.arrayContaining(['border-teal', 'bg-teal', 'shadow-[0_0_0_3px_var(--teal-soft)]']));
    });

    it.each([
        ['awaiting input', 'awaiting_input'],
        ['awaiting approval', 'awaiting_approval'],
    ])('a run %s fills it brass (C16)', (_label, phase) => {
        seed({ runs: { 'run-a': RUN_A }, runPhases: { 'run-a': { phase } } });
        render(<RunLight cellId="cell-1" />);
        expect(classes()).toEqual(expect.arrayContaining(['border-brass', 'bg-brass', 'shadow-[0_0_0_3px_var(--brass-soft)]']));
    });

    it('a busy run outranks one waiting on the user: teal (C16)', () => {
        seed({ runs: { 'run-a': RUN_A, 'run-b': RUN_B }, runPhases: { 'run-a': { phase: 'awaiting_input' } } });
        render(<RunLight cellId="cell-1" />);
        expect(classes()).toContain('bg-teal');
    });

    it('an unseen failure of the last run is the red ring; seen, it is the plain ring again (C16)', () => {
        seed({ lastFinishedByCell: { 'cell-1': { runId: 'x', label: 'x-1', status: 'timed_out', reason: 'watchdog', seen: false, at: 1 } } });
        render(<RunLight cellId="cell-1" />);
        expect(light().getAttribute('data-light-state')).toBe('failed');
        expect(classes()).toEqual(expect.arrayContaining(['border-red', 'bg-transparent']));
        expect(light().getAttribute('title')).toContain('Last run: timed out · watchdog');
        cleanup();
        seed({ lastFinishedByCell: { 'cell-1': { runId: 'x', label: 'x-1', status: 'failed', seen: true, at: 1 } } });
        render(<RunLight cellId="cell-1" />);
        expect(classes()).toContain('border-ink-3');
    });

    it('spinning up is the teal ring filled soft; stopping the red ring filled soft (C16)', () => {
        seed({ runs: { 'run-a': RUN_A }, runPhases: { 'run-a': { phase: 'spinning_up' } } });
        render(<RunLight cellId="cell-1" />);
        expect(classes()).toEqual(expect.arrayContaining(['border-teal', 'bg-teal-soft']));
        cleanup();
        seed({ runs: { 'run-a': RUN_A }, stoppingRunIds: new Set(['run-a']) });
        render(<RunLight cellId="cell-1" />);
        expect(classes()).toEqual(expect.arrayContaining(['border-red', 'bg-red-soft']));
    });

    it('its title counts the runs and names each with its state (C16)', () => {
        seed({ runs: { 'run-a': RUN_A, 'run-b': RUN_B }, runPhases: { 'run-b': { phase: 'awaiting_input' } } });
        render(<RunLight cellId="cell-1" />);
        const title = light().getAttribute('title') ?? '';
        expect(title).toContain('2 running in this cell');
        expect(title).toContain('Runner A aaaaaaaa — running');
        expect(title).toContain('Runner B bbbbbbbb — awaiting your message');
        expect(light().getAttribute('data-run-count')).toBe('2');
    });
});

describe('RunLight is a passive dot (C16)', () => {
    it('is a span with no button mode, no hover collar and no toggle in its title, live or idle (C16)', () => {
        render(<RunLight cellId="cell-1" />);
        expect(light().tagName).toBe('SPAN');
        expect(classes().some(c => c.startsWith('hover:') || c.startsWith('after:'))).toBe(false);
        expect(light().getAttribute('title')).toBe('Nothing running in this cell');
        cleanup();
        seed({ runs: { 'run-a': RUN_A } });
        render(<RunLight cellId="cell-1" />);
        expect(light().tagName).toBe('SPAN');
        expect(classes()).toContain('bg-teal');
        expect(light().getAttribute('title')).not.toMatch(/status|show|hide/i);
        expect(runLightSource).not.toMatch(/<button|onToggle|aria-expanded/);
    });

    it('a press on it reaches the card beneath: the toggle that holds it owns the gesture (C16)', () => {
        const onCard = vi.fn();
        render(<div onPointerDown={onCard} onClick={onCard}><RunLight cellId="cell-1" /></div>);
        fireEvent.pointerDown(light());
        fireEvent.click(light());
        expect(onCard).toHaveBeenCalledTimes(2);
    });
});

describe('RunLight on the skin tokens (S04)', () => {
    it('the light and its vocabulary name no retired palette token and no colour literal (S04)', () => {
        for (const source of [runLightSource, runLightVocabulary]) {
            expect(source).not.toMatch(RETIRED_TOKENS);
            expect(source).not.toMatch(COLOUR_LITERAL);
        }
    });
});
