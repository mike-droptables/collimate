// The canvas's NEW CELL target: a bar in the gap the drop will take, placed
// from the same measureDropSlot the drop uses, so it never shows a slot the
// stack will not land in (fan columns included).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { NewCellSlotGhost } from './NewCellSlotGhost';
import { useCanvasFileDrop } from '@/hooks/useCanvasFileDrop';
import type { FanRange } from '@/lib/fanOut';

/** On-screen `[data-stack-id]` columns at these [left, width] spans, all
 *  100px tall from y=40. */
function mountColumns(spans: Array<[number, number]>): () => void {
    const els = spans.map(([left, width], i) => {
        const el = document.createElement('div');
        el.setAttribute('data-stack-id', `st-${i}`);
        el.getBoundingClientRect = () => ({
            left, width, right: left + width, x: left, top: 40, height: 100, bottom: 140, y: 40, toJSON: () => ({}),
        }) as DOMRect;
        document.body.appendChild(el);
        return el;
    });
    return () => els.forEach(el => el.remove());
}

const bar = () => document.querySelector('[data-new-cell-slot]') as HTMLElement | null;

let unmountColumns: () => void = () => {};
beforeEach(() => {
    // The ghost's layer: the scene, 800x600 at the viewport's origin.
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({
        left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, x: 0, y: 0, toJSON: () => ({}),
    } as DOMRect);
});
afterEach(() => {
    unmountColumns();
    vi.restoreAllMocks();
});

describe('NewCellSlotGhost', () => {
    it('draws a 3px teal bar down the gap the drop will take, tagged NEW CELL', () => {
        unmountColumns = mountColumns([[0, 100], [120, 100], [240, 100]]);
        render(<NewCellSlotGhost pointerX={{ current: 130 }} fan={null} />);
        const b = bar()!;
        expect(b).not.toBeNull();
        expect(b.style.left).toBe('110px');
        expect(b.style.top).toBe('40px');
        expect(b.style.height).toBe('100px');
        expect(b.style.width).toBe('3px');
        expect(b.style.background).toBe('var(--teal)');
        expect(screen.getByText('NEW CELL')).toBeInTheDocument();
    });

    it('follows the pointer from frame to frame', async () => {
        unmountColumns = mountColumns([[0, 100], [120, 100], [240, 100]]);
        const pointerX = { current: 130 as number | null };
        render(<NewCellSlotGhost pointerX={pointerX} fan={null} />);
        expect(bar()!.style.left).toBe('110px');
        pointerX.current = 400;
        await waitFor(() => expect(bar()!.style.left).toBe('350px'));
    });

    it("between a fanned stack's cells it shows the outside gap the stack will really take", () => {
        unmountColumns = mountColumns([[0, 100], [120, 100], [240, 100], [360, 100], [480, 100]]);
        const fan: FanRange = { stackId: 's', name: 'S', realIdx: 0, start: 0, end: 2, count: 3 };
        render(<NewCellSlotGhost pointerX={{ current: 250 }} fan={fan} />);
        expect(bar()!.style.left).toBe('350px');
    });

    it('with no stack on the canvas it shows the tag alone (the cell is appended)', () => {
        render(<NewCellSlotGhost pointerX={{ current: 130 }} fan={null} />);
        expect(bar()).toBeNull();
        expect(screen.getByText('NEW CELL')).toBeInTheDocument();
    });

    it("is up only while the canvas owns the drag, as StackApp mounts it", () => {
        unmountColumns = mountColumns([[0, 100], [120, 100]]);
        function Scene() {
            const drop = useCanvasFileDrop('ch');
            return (
                <div data-testid="scene" {...drop.dragProps}>
                    {drop.isDragging && <NewCellSlotGhost pointerX={drop.pointerX} fan={null} />}
                </div>
            );
        }
        render(<Scene />);
        const scene = screen.getByTestId('scene');
        const files = () => ({ types: ['Files'], dropEffect: 'none' });
        expect(screen.queryByText('NEW CELL')).toBeNull();
        fireEvent.dragEnter(scene, { dataTransfer: files() });
        // jsdom has no DragEvent: the pointer rides on the plain event.
        const over = createEvent.dragOver(scene, { dataTransfer: files() });
        Object.defineProperty(over, 'clientX', { value: 300 });
        fireEvent(scene, over);
        expect(screen.getByText('NEW CELL')).toBeInTheDocument();
        expect(bar()!.style.left).toBe('230px');
        fireEvent.dragLeave(scene, { dataTransfer: files() });
        expect(screen.queryByText('NEW CELL')).toBeNull();
    });
});
