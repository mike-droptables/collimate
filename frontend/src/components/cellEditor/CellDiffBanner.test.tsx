import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { CellDiffBanner } from './CellDiffBanner';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

describe('CellDiffBanner', () => {
    it('names the base cell and exits through a labelled Exit button', () => {
        const onExit = vi.fn();
        render(<CellDiffBanner baseName="Alpha one" loading={false} onExit={onExit} />);
        expect(screen.getByText('Alpha one')).toBeTruthy();
        expect(screen.getByTitle('Diff against Alpha one')).toBeTruthy();
        expect(screen.queryByText(/loading/)).toBeNull();
        const exit = screen.getByRole('button', { name: 'Exit diff' });
        expect(exit.textContent).toBe('Exit');
        fireEvent.click(exit);
        expect(onExit).toHaveBeenCalledTimes(1);
    });

    it('says so while the base files load, and names an untitled base', () => {
        render(<CellDiffBanner baseName="" loading onExit={() => {}} />);
        expect(screen.getByText('untitled cell')).toBeTruthy();
        expect(screen.getByText(/loading/)).toBeTruthy();
    });

    it('the band is the live teal fill with a quiet Exit, on skin tokens only (S04)', () => {
        const { container } = render(<CellDiffBanner baseName="Alpha" loading onExit={() => {}} />);
        const band = container.querySelector('[data-cell-diff-banner]') as HTMLElement;
        expect(band.className).toMatch(/\bbg-teal-soft\b/);
        expect(band.className).toMatch(/\bborder-line\b/);
        const exit = screen.getByRole('button', { name: 'Exit diff' });
        expect(exit.className).toMatch(/\bborder-line-2\b/);
        expect(exit.className).toMatch(/\btext-ink-2\b/);
        expect(band.outerHTML).not.toMatch(RETIRED_TOKENS);
    });

    it('keeps its pointerdown from reaching the card / canvas', () => {
        const outer = vi.fn();
        render(
            <div onPointerDown={outer}>
                <CellDiffBanner baseName="Alpha" loading={false} onExit={() => {}} />
            </div>,
        );
        fireEvent.pointerDown(screen.getByRole('button', { name: 'Exit diff' }));
        expect(outer).not.toHaveBeenCalled();
    });
});
