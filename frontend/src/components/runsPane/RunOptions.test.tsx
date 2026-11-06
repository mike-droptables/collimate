// A live run's options at the foot of its drilldown: a switch per boolean,
// a compact select per select; a change is sent to the run and shows as
// pending until the run's answer to it (a published set) settles it.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';

const put = vi.hoisted(() => ({ fn: vi.fn(async (..._args: unknown[]) => {}) }));
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<object>()),
    putRunOption: (...args: unknown[]) => put.fn(...args),
}));

import { RunOptions } from './RunOptions';
import type { RunOption } from '@/lib/api';

const speed = (current: string): RunOption => ({
    id: 'speed', name: 'Speed', description: 'How fast it goes', category: 'model', type: 'select', currentValue: current,
    options: [{ value: 'slow', name: 'Slow' }, { value: 'fast', name: 'Fast', description: 'Quick' }],
});
const loud = (current: boolean): RunOption => ({ id: 'loud', name: 'Loud', type: 'boolean', currentValue: current });

const mount = (options: RunOption[], rev = 1) => render(<RunOptions channel="ch" runId="run-1" options={options} rev={rev} />);
const rerender = (view: ReturnType<typeof mount>, options: RunOption[], rev: number) =>
    view.rerender(<RunOptions channel="ch" runId="run-1" options={options} rev={rev} />);

beforeEach(() => { put.fn.mockReset(); put.fn.mockResolvedValue(undefined); });
afterEach(cleanup);

describe('RunOptions', () => {
    it('shows one row per option: a select for a select, a switch for a boolean', () => {
        mount([speed('slow'), loud(true)]);
        const select = within(screen.getByTestId('run-option-speed')).getByRole('combobox') as HTMLSelectElement;
        expect(select).toHaveAccessibleName('Speed');
        expect(select.value).toBe('slow');
        expect([...select.options].map(o => o.textContent)).toEqual(['Slow', 'Fast']);
        const toggle = within(screen.getByTestId('run-option-loud')).getByRole('switch');
        expect(toggle).toHaveAccessibleName('Loud');
        expect(toggle).toHaveAttribute('aria-checked', 'true');
        expect(screen.getByText('Speed')).toHaveAttribute('title', 'How fast it goes');
    });

    it('a value in effect the set does not list still shows', () => {
        mount([speed('turbo')]);
        const select = screen.getByRole('combobox') as HTMLSelectElement;
        expect(select.value).toBe('turbo');
        expect([...select.options].map(o => o.value)).toEqual(['turbo', 'slow', 'fast']);
    });

    it('a pick is sent and pending until the run publishes again, which settles it', async () => {
        const view = mount([speed('slow'), loud(false)], 3);
        await act(async () => { fireEvent.change(screen.getByRole('combobox'), { target: { value: 'fast' } }); });
        expect(put.fn).toHaveBeenCalledWith('ch', 'run-1', 'speed', 'fast');
        const row = screen.getByTestId('run-option-speed');
        expect(row).toHaveAttribute('data-pending', 'true');
        expect(screen.getByRole('combobox')).toBeDisabled();
        expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('fast');
        // A refetch of the same set keeps it pending.
        rerender(view, [speed('slow'), loud(false)], 3);
        expect(screen.getByTestId('run-option-speed')).toHaveAttribute('data-pending', 'true');
        // The run's answer: the value now in effect.
        rerender(view, [speed('fast'), loud(false)], 4);
        expect(screen.getByTestId('run-option-speed')).not.toHaveAttribute('data-pending');
        expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('fast');
        expect(screen.getByRole('combobox')).toBeEnabled();
    });

    it('a change the run could not apply settles back to the value in effect', async () => {
        const view = mount([loud(false)], 1);
        await act(async () => { fireEvent.click(screen.getByRole('switch')); });
        expect(put.fn).toHaveBeenCalledWith('ch', 'run-1', 'loud', true);
        expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'true');
        expect(screen.getByRole('switch')).toBeDisabled();
        rerender(view, [loud(false)], 2);
        expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
        expect(screen.getByRole('switch')).toBeEnabled();
    });

    it('changes sent before the run answers wait for their own answers, in order', async () => {
        const view = mount([speed('slow'), loud(false)], 5);
        await act(async () => { fireEvent.click(screen.getByRole('switch')); });
        await act(async () => { fireEvent.change(screen.getByRole('combobox'), { target: { value: 'fast' } }); });
        // The answer to the first change leaves the second waiting.
        rerender(view, [speed('slow'), loud(true)], 6);
        expect(screen.getByTestId('run-option-loud')).not.toHaveAttribute('data-pending');
        expect(screen.getByTestId('run-option-speed')).toHaveAttribute('data-pending', 'true');
        expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('fast');
        expect(screen.getByRole('combobox')).toBeDisabled();
        // The second's answer: refused, so the value in effect is back.
        rerender(view, [speed('slow'), loud(true)], 7);
        expect(screen.getByTestId('run-option-speed')).not.toHaveAttribute('data-pending');
        expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('slow');
    });

    it('a change already in effect settles at once', async () => {
        const view = mount([speed('slow'), loud(false)], 1);
        await act(async () => { fireEvent.click(screen.getByRole('switch')); });
        await act(async () => { fireEvent.change(screen.getByRole('combobox'), { target: { value: 'fast' } }); });
        rerender(view, [speed('fast'), loud(true)], 2);
        expect(screen.getByTestId('run-option-speed')).not.toHaveAttribute('data-pending');
        expect(screen.getByTestId('run-option-loud')).not.toHaveAttribute('data-pending');
    });

    it('a change the latest set no longer takes settles: the host dropped it unanswered', async () => {
        const view = mount([speed('slow'), loud(false)], 1);
        await act(async () => { fireEvent.click(screen.getByRole('switch')); });
        await act(async () => { fireEvent.change(screen.getByRole('combobox'), { target: { value: 'fast' } }); });
        // The answer to the switch is a set that no longer offers fast.
        const slowOnly = { ...speed('slow'), options: [{ value: 'slow', name: 'Slow' }] } as RunOption;
        rerender(view, [slowOnly, loud(true)], 2);
        expect(screen.getByTestId('run-option-loud')).not.toHaveAttribute('data-pending');
        expect(screen.getByTestId('run-option-speed')).not.toHaveAttribute('data-pending');
        expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('slow');
    });

    it('a refused change drops its pending value and says why', async () => {
        put.fn.mockRejectedValueOnce(new Error('That run is ending'));
        mount([loud(false)]);
        await act(async () => { fireEvent.click(screen.getByRole('switch')); });
        expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false');
        expect(screen.getByRole('switch')).toBeEnabled();
        expect(screen.getByTestId('run-options-error').textContent).toBe('Loud: That run is ending');
    });

    it('an empty set shows nothing', () => {
        const { container } = mount([]);
        expect(container.innerHTML).toBe('');
    });
});
