// The channel rename dialog sits on the shared dialog plate: the lift scrim,
// a sheet with the line-2 edge, the quiet Cancel and the primary Save. It
// renames only: deleting a channel lives on the channels page (plan 1105 P7).

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ChannelDialog } from './ChannelDialog';
import { retiredOr } from '@/test/retiredTokens';

const painted = () => [...document.body.querySelectorAll('*')]
    .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
const RETIRED = retiredOr('rgba?\\(', 'bg-destructive', 'bg-primary');

describe('ChannelDialog', () => {
    it('rename saves through the panel primary on the dialog plate (S04)', () => {
        render(<ChannelDialog channelName="alpha" value="beta" onChange={vi.fn()} onCancel={vi.fn()} onSubmit={vi.fn()} />);
        const heading = screen.getByRole('heading', { name: 'Rename Channel' });
        expect(heading).toHaveClass('text-ink');
        const plate = heading.parentElement!.parentElement!;
        expect(plate).toHaveClass('bg-sheet', 'border-line-2', 'shadow-(--shadow)');
        expect(plate.parentElement).toHaveClass('bg-(--lift)', 'z-[460]');
        expect(screen.getByRole('button', { name: 'Save' })).toHaveClass('bg-sheet', 'text-ink', 'font-semibold');
        expect(screen.getByRole('button', { name: 'Cancel' })).toHaveClass('border-line-2', 'text-ink-2');
        expect(screen.getByRole('textbox', { name: 'New name' })).toHaveClass('bg-sheet', 'border-line');
        expect(painted()).not.toMatch(RETIRED);
    });

    it('offers no delete, and saves only a changed, non-empty name (plan 1105 P7)', () => {
        const onSubmit = vi.fn();
        const { rerender } = render(<ChannelDialog channelName="alpha" value="alpha" onChange={vi.fn()} onCancel={vi.fn()} onSubmit={onSubmit} />);
        expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
        expect(screen.queryByText(/permanently delete/)).toBeNull();
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        rerender(<ChannelDialog channelName="alpha" value="  " onChange={vi.fn()} onCancel={vi.fn()} onSubmit={onSubmit} />);
        expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
        rerender(<ChannelDialog channelName="alpha" value="beta" onChange={vi.fn()} onCancel={vi.fn()} onSubmit={onSubmit} />);
        fireEvent.keyDown(screen.getByRole('textbox', { name: 'New name' }), { key: 'Enter' });
        expect(onSubmit).toHaveBeenCalledOnce();
    });
});
