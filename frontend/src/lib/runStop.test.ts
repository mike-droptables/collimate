import { describe, it, expect } from 'vitest';
import {
    failureStatusLabel, isFailureStatus, isTerminatedStatus, truncateFailureReason,
} from './runStop';

describe('isTerminatedStatus / isFailureStatus', () => {
    it('user stops terminate; timed_out / interrupted / failed / error are failures', () => {
        expect(isTerminatedStatus('stopped')).toBe(true);
        expect(isTerminatedStatus('force_stopped')).toBe(true);
        expect(isTerminatedStatus('gracefully_stopped')).toBe(true);
        for (const s of ['timed_out', 'interrupted', 'failed', 'error', 'completed']) {
            expect(isTerminatedStatus(s)).toBe(false);
        }
        for (const s of ['timed_out', 'interrupted', 'failed', 'error']) {
            expect(isFailureStatus(s)).toBe(true);
        }
        for (const s of ['completed', 'stopped', 'force_stopped', 'gracefully_stopped']) {
            expect(isFailureStatus(s)).toBe(false);
        }
    });
});

describe('failureStatusLabel', () => {
    it('labels the non-success terminal statuses', () => {
        expect(failureStatusLabel('timed_out')).toBe('Timed out');
        expect(failureStatusLabel('interrupted')).toBe('Interrupted');
        expect(failureStatusLabel('failed')).toBe('Failed');
        expect(failureStatusLabel('anything-else')).toBe('Failed');
    });
});

describe('truncateFailureReason', () => {
    it('passes short reasons through untouched', () => {
        expect(truncateFailureReason('rc=128')).toBe('rc=128');
    });

    it('clamps long reasons to ~120 chars with an ellipsis', () => {
        const long = 'x'.repeat(200);
        const out = truncateFailureReason(long);
        expect(out.length).toBeLessThanOrEqual(120);
        expect(out.endsWith('…')).toBe(true);
    });
});
