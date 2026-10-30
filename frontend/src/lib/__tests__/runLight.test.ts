import { describe, expect, it } from 'vitest';
import { lightStateOfPhase, runLightState, runStateText } from '../runLight';
import { runElapsed } from '../runLabels';

describe('a run\'s light state', () => {
    it('stopping wins, then approval, input, spinning up; anything else runs', () => {
        expect(lightStateOfPhase('awaiting_approval', true)).toBe('stopping');
        expect(lightStateOfPhase('awaiting_approval', false)).toBe('awaiting_approval');
        expect(lightStateOfPhase('awaiting_input', false)).toBe('awaiting_input');
        expect(lightStateOfPhase('queued', false)).toBe('spinning_up');
        expect(lightStateOfPhase('spinning_up', false)).toBe('spinning_up');
        expect(lightStateOfPhase('running', false)).toBe('running');
        expect(lightStateOfPhase(undefined, false)).toBe('running');
    });

    it('runLightState reads the run\'s phase and stop', () => {
        const phases = { r: { phase: 'awaiting_input' } };
        expect(runLightState({ runId: 'r' }, new Set(), phases)).toBe('awaiting_input');
        expect(runLightState({ runId: 'r' }, new Set(['r']), phases)).toBe('stopping');
        expect(runStateText({ runId: 'x' }, new Set(), phases)).toBe('running');
    });
});

describe('runElapsed', () => {
    const start = '2026-10-24T10:00:00Z';
    const at = (min: number) => Date.parse(start) + min * 60_000;
    it('reads <1m, minutes, then hours and minutes; nothing without a start', () => {
        expect(runElapsed(start, at(0.2))).toBe('<1m');
        expect(runElapsed(start, at(12))).toBe('12m');
        expect(runElapsed(start, at(65))).toBe('1h 5m');
        expect(runElapsed(start, at(-5))).toBe('<1m');
        expect(runElapsed(null, at(5))).toBe('');
        expect(runElapsed('not a time', at(5))).toBe('');
    });
});
