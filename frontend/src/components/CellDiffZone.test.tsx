import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { CellDiffZone } from './CellDiffZone';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { cellDiffZoneGeometry } from '@/lib/cellDiff';
import { retiredOr } from '@/test/retiredTokens';

// The 36px cell head under the card's 1px top border.
const headerBottomOffsetPx = 37;
const geometry = cellDiffZoneGeometry(
    { left: 0, right: 400, top: 77, headerBottomOffsetPx },
    { left: 414, right: 814, top: 77, headerBottomOffsetPx },
)!;

describe('CellDiffZone', () => {
    let enterCellDiff: ReturnType<typeof vi.fn>;
    beforeEach(() => {
        enterCellDiff = vi.fn();
        useCollimateStore.setState({ enterCellDiff } as never);
    });

    const renderZone = () => render(
        <CellDiffZone geometry={geometry} leftCellId="L" rightCellId="R" leftName="Left" rightName="Right" />,
    );

    it('lays its hit rects out from the geometry; the gap rect is the one button', () => {
        const { container } = renderZone();
        const hits = [...container.querySelectorAll<HTMLElement>('[data-cell-diff-hit]')];
        expect(hits.map(h => h.style.left)).toEqual(['400px', '394px', '414px']);
        expect(hits[0].style.width).toBe('14px');
        const button = screen.getByRole('button', { name: 'Diff Right against Left' });
        expect(button).toBe(hits[0]);
        expect(button.tabIndex).toBe(0);
        expect(hits[1].getAttribute('aria-hidden')).toBe('true');
    });

    it('focus lights it; Enter / Space enter the diff', () => {
        const { container } = renderZone();
        const button = screen.getByRole('button');
        fireEvent.focus(button);
        expect(container.querySelector('[data-cell-diff-pill]')).not.toBeNull();
        fireEvent.keyDown(button, { key: 'Enter' });
        fireEvent.keyDown(button, { key: ' ' });
        expect(enterCellDiff).toHaveBeenCalledTimes(2);
        expect(enterCellDiff).toHaveBeenCalledWith('R', 'L');
        fireEvent.blur(button);
        expect(container.querySelector('[data-cell-diff-pill]')).toBeNull();
        // A mouse press doesn't focus it (so a click leaves no lit zone).
        expect(fireEvent.mouseDown(button)).toBe(false);
    });

    it('beside a fan column the pill stands inside the gap, not above the band', () => {
        const mixed = cellDiffZoneGeometry(
            { left: 0, right: 400, top: 77, headerBottomOffsetPx },
            { left: 414, right: 814, top: 110, headerBottomOffsetPx },
        )!;
        const { container } = render(
            <CellDiffZone geometry={mixed} leftCellId="L" rightCellId="R" leftName="Left" rightName="Right" />,
        );
        fireEvent.pointerEnter(screen.getByRole('button'));
        const pill = container.querySelector('[data-cell-diff-pill]') as HTMLElement;
        expect(pill.style.width).toBe('12px');
        expect(pill.style.top).toBe(`${77 + 6}px`);
    });

    it('shows the Compare pill and gap line only while hovered, above every stack lift', () => {
        const { container } = renderZone();
        const root = container.querySelector('[data-cell-diff-zone]') as HTMLElement;
        expect(container.querySelector('[data-cell-diff-pill]')).toBeNull();
        fireEvent.pointerEnter(screen.getByRole('button'));
        const pill = container.querySelector('[data-cell-diff-pill]') as HTMLElement;
        expect(pill.textContent).toContain('Compare');
        expect(pill.style.pointerEvents).toBe('none');
        expect(Number(root.style.zIndex)).toBeGreaterThan(1000);
        fireEvent.pointerLeave(screen.getByRole('button'));
        expect(container.querySelector('[data-cell-diff-pill]')).toBeNull();
    });

    it('a click puts the RIGHT cell in diff against the LEFT; its pointerdown stays its own', () => {
        const outer = vi.fn();
        render(
            <div onPointerDown={outer}>
                <CellDiffZone geometry={geometry} leftCellId="L" rightCellId="R" leftName="Left" rightName="Right" />
            </div>,
        );
        const hit = document.querySelectorAll<HTMLElement>('[data-cell-diff-hit]')[1];
        fireEvent.pointerDown(hit);
        fireEvent.click(hit);
        expect(outer).not.toHaveBeenCalled();
        expect(enterCellDiff).toHaveBeenCalledWith('R', 'L');
    });
});

describe('CellDiffZone — skin tokens (S04)', () => {
    it('the lit zone is a teal line and a teal pill on a sheet (S04)', () => {
        useCollimateStore.setState({ enterCellDiff: vi.fn() } as never);
        const { container } = render(
            <CellDiffZone geometry={geometry} leftCellId="L" rightCellId="R" leftName="Left" rightName="Right" />,
        );
        fireEvent.pointerEnter(screen.getByRole('button'));
        const pill = container.querySelector('[data-cell-diff-pill]') as HTMLElement;
        expect(pill.style.color).toBe('var(--teal)');
        expect(pill.style.background).toBe('var(--sheet)');
        expect(container.innerHTML).not.toMatch(retiredOr('color-mix', 'rgba'));
    });
});

describe('CellDiffZone restyled to the workbench (C18)', () => {
    it('the Compare pill is the 20px chip: teal on a sheet, in the UI face at 11px (C18)', () => {
        useCollimateStore.setState({ enterCellDiff: vi.fn() } as never);
        const { container } = render(
            <CellDiffZone geometry={geometry} leftCellId="L" rightCellId="R" leftName="Left" rightName="Right" />,
        );
        fireEvent.pointerEnter(screen.getByRole('button'));
        const pill = container.querySelector('[data-cell-diff-pill]') as HTMLElement;
        expect(pill.style.height).toBe('20px');
        expect(pill.style.fontFamily).toBe('var(--font-ui)');
        expect(pill.style.fontSize).toBe('0.6875rem');
        expect(pill.style.borderRadius).toBe('999px');
        expect(pill.style.border).toBe('1px solid var(--teal)');
    });
});
