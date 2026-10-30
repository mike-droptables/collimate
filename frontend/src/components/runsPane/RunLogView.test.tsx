// A log in the status pane: one run's lines or every run's, merged by time,
// with or without the runner column; a finished run's saved log read when it
// opens; the tail; the empty text.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { RunLogView } from './RunLogView';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CELL_ID, seedPaneStore } from '@/test/annotationFixtures';

const at = (h: number, m: number) => new Date(2026, 9, 24, h, m).getTime();

beforeEach(() => {
    seedPaneStore({
        cellLogRuns: { [CELL_ID]: [
            { runId: 'r1', channel: 'ch', runner: 'Agent', startedAt: at(9, 0), ended: { lines: ['[09:05:00] INFO   one ended'], arrivals: [at(9, 5)] } },
            { runId: 'r2', channel: 'ch', runner: 'Chat', startedAt: at(9, 1) },
            { runId: 'r3', channel: 'ch', runner: 'Saved', startedAt: at(8, 0), saved: { state: 'read', lines: ['[08:30:00] ERR    went wrong'], at: at(9, 59) } },
        ] },
        runLogs: { r2: ['[09:02:00] INFO   chat says'] }, runLogArrivals: { r2: [at(9, 2)] },
    });
});
afterEach(cleanup);

describe('RunLogView', () => {
    it('merges every run\'s lines by time with their runners, reading the saved logs on open', () => {
        render(<RunLogView cellId={CELL_ID} runIds={null} showRunner />);
        expect(useCollimateStore.getState().readSavedCellLogs).toHaveBeenCalledWith(CELL_ID);
        const text = screen.getByTestId('run-log').textContent!;
        expect(text).toBe('08:30  Saved  ERR went wrong\n09:02  Chat  INFO chat says\n09:05  Agent  INFO one ended');
        expect(screen.getByText('ERR went wrong').className).toBe('text-red');
    });

    it('shows one run\'s lines without the runner column', () => {
        render(<RunLogView cellId={CELL_ID} runIds={['r2']} showRunner={false} />);
        expect(screen.getByTestId('run-log').textContent).toBe('09:02  INFO chat says');
    });

    it('reads from its tail, and says when there is nothing yet', () => {
        vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(400);
        render(<RunLogView cellId={CELL_ID} runIds={null} showRunner />);
        expect(screen.getByTestId('run-log').querySelector('pre')!.scrollTop).toBe(400);
        cleanup();
        vi.restoreAllMocks();
        render(<RunLogView cellId={CELL_ID} runIds={['nothing']} showRunner />);
        expect(screen.getByText('No log yet.')).toBeInTheDocument();
        cleanup();
        render(<RunLogView cellId="other" runIds={null} showRunner />);
        expect(screen.getByTestId('run-log')).toHaveAttribute('role', 'region');
        expect(screen.getByText('No log yet.')).toBeInTheDocument();
    });
});
