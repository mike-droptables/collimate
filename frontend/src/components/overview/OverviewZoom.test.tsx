import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { motionValue } from 'motion/react';
import { OverviewZoom } from './OverviewZoom';

afterEach(cleanup);

/** The control inside an overview root, its box at the root's bottom right
 *  (x 900..1000, y 700..730). */
const mount = (props: Partial<Parameters<typeof OverviewZoom>[0]> = {}) => {
    const view = render(
        <div data-overview-root="" data-testid="root">
            <OverviewZoom zoom={motionValue(0.5)} onStep={vi.fn()} onFit={vi.fn()} right={14} paneRight={0} {...props} />
        </div>,
    );
    const group = view.container.querySelector<HTMLElement>('[data-overview-zoom]')!;
    group.getBoundingClientRect = () => ({
        left: 900, top: 700, right: 1000, bottom: 730, width: 100, height: 30, x: 900, y: 700, toJSON: () => ({}),
    });
    return { ...view, group, root: screen.getByTestId('root') };
};
const classes = (el: Element) => el.getAttribute('class')!.split(/\s+/);
const move = (el: Element, clientX: number, clientY: number) => fireEvent.pointerMove(el, { clientX, clientY });

it('the zoom control: −, the percentage in mono, +, Fit, on a sheet with a line-2 border once shown (O01)', () => {
    const zoom = motionValue(0.5);
    const onStep = vi.fn();
    const onFit = vi.fn();
    const { group, root } = mount({ zoom, onStep, onFit });
    move(root, 950, 715);
    expect(classes(group)).toEqual(expect.arrayContaining(['absolute', 'bottom-3.5', 'bg-sheet', 'border-line-2', 'rounded-lg']));
    expect(group.style.right).toBe('14px');
    expect(group.style.marginRight).toBe('0px');
    expect([...group.children].map(el => el.textContent)).toEqual(['−', '50%', '+', 'Fit']);
    expect(screen.getByText('50%').className).toContain('font-mono');
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(onStep.mock.calls).toEqual([[0.8], [1.25]]);
    expect(group.getAttribute('title')).toBe('Wheel zooms, Shift+wheel pans');
    fireEvent.click(screen.getByRole('button', { name: 'Fit' }));
    expect(onFit).toHaveBeenCalledTimes(1);
    act(() => zoom.set(1.234));
    expect(screen.getByText('123%')).toBeTruthy();
});

it('moves in by the Commands pane docked on the right, sliding over 160ms (polish)', () => {
    const { group } = mount({ paneRight: 352 });
    // Its own place is unchanged; the pane's width is a margin on top, the
    // only part that slides; its colours fade beside it.
    expect(group.style.right).toBe('14px');
    expect(group.style.marginRight).toBe('352px');
    expect(group.style.transition).toBe('border-color 120ms ease-out, background-color 120ms ease-out, margin-right 160ms ease');
});

it('shows only while the pointer is within 40px of its corner, fading by colour, never opacity (ZC-01)', () => {
    const { group, root } = mount();
    const buttons = [...group.querySelectorAll('button')];
    const hidden = () => {
        expect(group.hasAttribute('data-shown')).toBe(false);
        expect(classes(group)).toEqual(expect.arrayContaining(['border-transparent', 'bg-transparent']));
        expect(classes(group)).not.toContain('bg-sheet');
        for (const b of buttons) expect(classes(b)).toContain('text-transparent');
        expect(classes(screen.getByText('50%'))).toContain('text-transparent');
    };
    // At rest, and with the pointer over the board far from it.
    hidden();
    move(root, 400, 300);
    hidden();
    // 40px from its box, on any side, it shows.
    move(root, 860, 715);
    expect(group.hasAttribute('data-shown')).toBe(true);
    for (const b of buttons) expect(classes(b)).toContain('text-ink-2');
    move(root, 859, 715);
    hidden();
    move(root, 950, 660);
    expect(group.hasAttribute('data-shown')).toBe(true);
    // Leaving the overview hides it.
    fireEvent.pointerLeave(root);
    hidden();
    // Never by opacity: no opacity class or style on any of it.
    for (const el of [group, ...group.querySelectorAll('*')]) {
        expect(classes(el).some(c => c.startsWith('opacity-'))).toBe(false);
        expect((el as HTMLElement).style?.opacity ?? '').toBe('');
    }
    // It stays in the tab order while hidden.
    for (const b of buttons) expect(b.tabIndex).toBe(0);
});

it('keyboard focus shows it, wherever the pointer is (ZC-01)', () => {
    const { group } = mount();
    const out = screen.getByRole('button', { name: 'Zoom out' });
    const zin = screen.getByRole('button', { name: 'Zoom in' });
    const matches = vi.spyOn(Element.prototype, 'matches').mockImplementation(function (this: Element, sel: string) {
        return sel === ':focus-visible' ? this === document.activeElement : false;
    });
    act(() => out.focus());
    expect(group.hasAttribute('data-shown')).toBe(true);
    // Tabbing on inside the group keeps it.
    act(() => zin.focus());
    expect(group.hasAttribute('data-shown')).toBe(true);
    act(() => zin.blur());
    expect(group.hasAttribute('data-shown')).toBe(false);
    matches.mockRestore();
});
