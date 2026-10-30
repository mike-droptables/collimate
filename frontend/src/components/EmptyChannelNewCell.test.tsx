import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { EmptyChannelNewCell } from './EmptyChannelNewCell';
import { retiredOr } from '@/test/retiredTokens';

afterEach(cleanup);

describe('EmptyChannelNewCell', () => {
    it('offers New stack on an empty canvas, in the primary button with its shortcut (C01)', () => {
        const onNew = vi.fn();
        const { container } = render(<EmptyChannelNewCell isEmpty isZoomedOut={false} onNew={onNew} />);
        expect(screen.getByText('Nothing on the canvas yet.').className).toContain('text-ink-3');
        const button = screen.getByRole('button', { name: /New stack/ });
        expect(button.className).toContain('[&_svg]:text-brass');
        expect(button.querySelector('kbd')?.textContent).toBeTruthy();
        fireEvent.click(button);
        expect(onNew).toHaveBeenCalledOnce();
        expect(screen.queryByText(/New empty cell/)).toBeNull();
        expect(container.innerHTML).not.toMatch(retiredOr('rgba', 'color-mix'));
    });

    it('shows nothing once the channel has a stack, or in the overview (C01)', () => {
        const { rerender, container } = render(<EmptyChannelNewCell isEmpty={false} isZoomedOut={false} onNew={vi.fn()} />);
        expect(container.innerHTML).toBe('');
        rerender(<EmptyChannelNewCell isEmpty isZoomedOut onNew={vi.fn()} />);
        expect(container.innerHTML).toBe('');
    });
});
