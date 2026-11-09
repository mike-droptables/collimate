// The Commands pane's column, shared with the channels page's docked
// settings panel: its width, its edge, its layer and its name row.

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PaneColumn } from './PaneColumn';
import { COMMAND_PANE_WIDTH } from '@/stores/collimate/commandPaneSlice';
import { PANE_COLUMN_PX } from '@/components/settings/paneColumn.testkit';

describe('PaneColumn', () => {
    it('is a 352px complementary column with data-pane-column, the title and the head extra in its name row', () => {
        render(<PaneColumn side="left" layer="canvas" title="Commands" label="Commands" dataAttrs={{ 'data-command-pane': '' }}
            headExtra={<button type="button">Dock</button>}><p>view</p></PaneColumn>);
        const column = screen.getByRole('complementary', { name: 'Commands' });
        expect(COMMAND_PANE_WIDTH).toBe(352);
        expect(PANE_COLUMN_PX).toBe(COMMAND_PANE_WIDTH);
        expect(column.style.width).toBe('352px');
        expect(column).toHaveAttribute('data-pane-column');
        expect(column).toHaveAttribute('data-command-pane');
        const head = column.firstElementChild!;
        expect(head).toHaveClass('h-[2.8125rem]', 'border-b');
        expect(head).toHaveTextContent('Commands');
        expect(head.querySelector('button')).toHaveTextContent('Dock');
        expect(head.nextElementSibling).toHaveTextContent('view');
    });

    it('lies flat against its side, with the border and shadow facing the content', () => {
        const { rerender } = render(<PaneColumn side="left" layer="canvas" title="T" label="T"><p /></PaneColumn>);
        const column = () => screen.getByRole('complementary');
        expect(column()).toHaveClass('left-0', 'border-r');
        expect(column()).not.toHaveClass('right-0');
        rerender(<PaneColumn side="right" layer="canvas" title="T" label="T"><p /></PaneColumn>);
        expect(column()).toHaveClass('right-0', 'border-l');
        expect(column()).not.toHaveClass('left-0');
    });

    it('takes its layer: absolute over the canvas, fixed and no-drag above a view, absolute inside the page', () => {
        const { rerender } = render(<PaneColumn side="left" layer="canvas" title="T" label="T"><p /></PaneColumn>);
        const column = () => screen.getByRole('complementary');
        expect(column()).toHaveClass('absolute', 'z-20', 'top-0', 'bottom-0');
        rerender(<PaneColumn side="left" layer="overlay" title="T" label="T"><p /></PaneColumn>);
        expect(column()).toHaveClass('fixed', 'z-[455]', 'top-0', 'bottom-0');
        expect(column().style.getPropertyValue('-webkit-app-region') || (column().style as unknown as Record<string, string>).WebkitAppRegion).toBe('no-drag');
        rerender(<PaneColumn side="left" layer="page" title="T" label="T"><p /></PaneColumn>);
        // Inside the channels page, below the blurred stand-in top bar's band.
        expect(column()).toHaveClass('absolute', 'z-10', 'top-9', 'bottom-0');
        expect(column()).not.toHaveClass('fixed');
        expect(column()).not.toHaveClass('top-0');
    });

    it('hands its root to paneRef and its presses and keys to the host', () => {
        const ref = { current: null as HTMLElement | null };
        const onMouseDown = vi.fn();
        const onKeyDown = vi.fn();
        render(<PaneColumn side="left" layer="page" title="T" label="T" paneRef={ref} onMouseDown={onMouseDown} onKeyDown={onKeyDown}>
            <button type="button">inside</button></PaneColumn>);
        expect(ref.current).toBe(screen.getByRole('complementary'));
        const inside = screen.getByRole('button', { name: 'inside' });
        inside.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
        inside.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        expect(onMouseDown).toHaveBeenCalledOnce();
        expect(onKeyDown).toHaveBeenCalledOnce();
    });
});
