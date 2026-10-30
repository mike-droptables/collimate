// The channel rename / delete dialog sits on the shared dialog plate: the
// lift scrim, a sheet with the line-2 edge, the quiet Cancel and either the
// primary (rename) or the destructive action (delete).

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ChannelDialog } from './ChannelDialog';
import { retiredOr } from '@/test/retiredTokens';

const painted = () => [...document.body.querySelectorAll('*')]
    .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
const RETIRED = retiredOr('rgba?\\(', 'bg-destructive', 'bg-primary');

describe('ChannelDialog', () => {
    it('delete names the channel in red and arms the destructive action (S04)', () => {
        render(<ChannelDialog mode="delete" activeChannelName="alpha" value="alpha" onChange={vi.fn()} onCancel={vi.fn()} onSubmit={vi.fn()} />);
        const heading = screen.getByRole('heading', { name: 'Delete Channel' });
        expect(heading).toHaveClass('text-ink');
        const plate = heading.parentElement!.parentElement!;
        expect(plate).toHaveClass('bg-sheet', 'border-line-2', 'shadow-(--shadow)');
        expect(plate.parentElement).toHaveClass('bg-(--lift)');
        expect(screen.getAllByText('alpha').some(el => el.classList.contains('text-red'))).toBe(true);
        expect(screen.getByRole('button', { name: 'Delete' })).toHaveClass('text-red', 'hover:bg-red-soft');
        expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
        expect(screen.getByRole('textbox')).toHaveClass('bg-sheet', 'border-line');
        expect(painted()).not.toMatch(RETIRED);
    });

    it('rename saves through the panel primary (S04)', () => {
        render(<ChannelDialog mode="rename" activeChannelName="alpha" value="beta" onChange={vi.fn()} onCancel={vi.fn()} onSubmit={vi.fn()} />);
        expect(screen.getByRole('button', { name: 'Save' })).toHaveClass('bg-sheet', 'text-ink', 'font-semibold');
        expect(screen.getByRole('button', { name: 'Cancel' })).toHaveClass('border-line-2', 'text-ink-2');
        expect(painted()).not.toMatch(RETIRED);
    });
});
