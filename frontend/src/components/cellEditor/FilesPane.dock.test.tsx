// The Files pane on each dock side (FP-02, FP-03): a column on the left, a
// band across the top; its border, its stored size per axis, and its resize
// handle on the edge facing the content, growing the pane as that edge
// moves toward the content.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

import { FilesPane } from './FilesPane';
import { createDefaultLayout, setFilesPane } from '@/lib/cellLayout';
import type { Cell } from '@/lib/types';
import type { CellLayout, FilesPaneSide } from '@/stores/collimate/types';

const cell: Cell = { id: 'cell-1', name: 'Test', files: [] };

const layoutOn = (side: FilesPaneSide, size: Partial<Pick<CellLayout['filesPane'], 'width' | 'height'>> = {}): CellLayout =>
    setFilesPane(createDefaultLayout({ filePaths: new Set<string>(), pinnedPath: null }), { collapsed: false, side, ...size });

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function renderPane(layout: CellLayout) {
    const onLayoutChange = vi.fn();
    render(
        <div style={{ display: 'flex' }}>
            <FilesPane
                cell={cell}
                layout={layout}
                onLayoutChange={onLayoutChange}
                onOpenFile={vi.fn()}
                onUpdateFiles={vi.fn()}
                readOnly={false}
                activePath={null}
            />
        </div>,
    );
    return { onLayoutChange };
}

const pane = () => screen.getByRole('complementary', { name: 'Files' });
const handle = () => screen.getByRole('separator', { name: 'Resize files' });
const last = (fn: ReturnType<typeof vi.fn>, from: CellLayout) =>
    (fn.mock.calls.at(-1)![0] as (l: CellLayout) => CellLayout)(from).filesPane;

/** The cell is 900 x 600; the pane is its own inline size. */
function mockBoxes() {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
        const isPane = this.tagName === 'ASIDE';
        const width = isPane && this.style.width ? parseFloat(this.style.width) : 900;
        const height = isPane && this.style.height ? parseFloat(this.style.height) : 600;
        return { x: 0, y: 0, top: 0, left: 0, right: width, bottom: height, width, height, toJSON: () => ({}) } as DOMRect;
    });
}

describe('the aside per dock side (FP-02)', () => {
    it.each([
        ['left', ['h-full', 'border-r'], ['border-l', 'border-t', 'border-b', 'w-full']],
        ['top', ['w-full', 'border-b'], ['border-t', 'border-l', 'border-r', 'h-full']],
    ] as const)('docked %s, the pane carries its border (FP-02)', (side, has, hasNot) => {
        renderPane(layoutOn(side));
        const classes = pane().className.split(/\s+/);
        for (const c of has) expect(classes).toContain(c);
        for (const c of hasNot) expect(classes).not.toContain(c);
        expect(classes.some(c => c.startsWith('pb-'))).toBe(false);
        expect(pane()).toHaveAttribute('data-files-side', side);
    });

    it('the column takes the stored width, the band the stored height, each defaulting apart (FP-01, FP-02)', () => {
        const sized = { width: 230, height: 140 };
        renderPane(layoutOn('left', sized));
        expect(pane().style.width).toBe('230px');
        expect(pane().style.height).toBe('');
        expect(pane().style.maxWidth).toBe('min(66.6667%, 100% - 160px)');
        cleanup();
        renderPane(layoutOn('left'));
        expect(pane().style.width).toBe('194px');
        cleanup();
        renderPane(layoutOn('top', sized));
        expect(pane().style.height).toBe('140px');
        expect(pane().style.width).toBe('');
        expect(pane().style.maxHeight).toBe('min(66.6667%, 100% - 120px)');
        cleanup();
        renderPane(layoutOn('top'));
        expect(pane().style.height).toBe('198px');
    });
});

describe('the resize handle per dock side (FP-02)', () => {
    it.each(['left', 'top'] as const)('docked %s, a quick stationary release hides the pane and keeps both stored sizes', (side) => {
        const layout = layoutOn(side, { width: 220, height: 150 });
        const { onLayoutChange } = renderPane(layout);
        const now = vi.spyOn(Date, 'now');
        now.mockReturnValue(1000);
        const before = onLayoutChange.mock.calls.length;
        fireEvent.pointerDown(handle(), { clientX: 300, clientY: 200, button: 0, pointerId: 1 });
        now.mockReturnValue(1100);
        fireEvent.pointerUp(handle(), { clientX: 300, clientY: 200, pointerId: 1 });
        expect(onLayoutChange).toHaveBeenCalledTimes(before + 1);
        expect(last(onLayoutChange, layout)).toMatchObject({ collapsed: true, width: 220, height: 150, side });
        cleanup();
    });

    it('small pointer jitter still counts as a click, but a long hold and a cancelled press do not', () => {
        const layout = layoutOn('left', { width: 220 });
        const { onLayoutChange } = renderPane(layout);
        const now = vi.spyOn(Date, 'now');
        now.mockReturnValue(1000);
        const before = onLayoutChange.mock.calls.length;
        fireEvent.pointerDown(handle(), { clientX: 220, clientY: 100, button: 0, pointerId: 1 });
        fireEvent.pointerMove(handle(), { clientX: 222, clientY: 101, pointerId: 1 });
        now.mockReturnValue(1100);
        fireEvent.pointerUp(handle(), { clientX: 222, clientY: 101, pointerId: 1 });
        expect(onLayoutChange).toHaveBeenCalledTimes(before + 1);
        expect(last(onLayoutChange, layout).collapsed).toBe(true);

        onLayoutChange.mockClear();
        now.mockReturnValue(2000);
        fireEvent.pointerDown(handle(), { clientX: 220, clientY: 100, button: 0, pointerId: 2 });
        fireEvent.pointerMove(handle(), { clientX: 222, clientY: 101, pointerId: 2 });
        expect(pane().style.width).toBe('220px');
        now.mockReturnValue(2300);
        fireEvent.pointerUp(handle(), { clientX: 222, clientY: 101, pointerId: 2 });
        expect(onLayoutChange).not.toHaveBeenCalled();

        now.mockReturnValue(3000);
        fireEvent.pointerDown(handle(), { clientX: 220, clientY: 100, button: 0, pointerId: 3 });
        fireEvent.pointerCancel(handle(), { pointerId: 3 });
        fireEvent.pointerUp(handle(), { clientX: 220, clientY: 100, pointerId: 3 });
        expect(onLayoutChange).not.toHaveBeenCalled();
    });

    it('a drag out and back does not hide the pane', () => {
        mockBoxes();
        const layout = layoutOn('left', { width: 220 });
        const { onLayoutChange } = renderPane(layout);
        const before = onLayoutChange.mock.calls.length;
        fireEvent.pointerDown(handle(), { clientX: 220, clientY: 100, button: 0, pointerId: 1 });
        fireEvent.pointerMove(handle(), { clientX: 260, clientY: 100, pointerId: 1 });
        fireEvent.pointerMove(handle(), { clientX: 220, clientY: 100, pointerId: 1 });
        fireEvent.pointerUp(handle(), { clientX: 220, clientY: 100, pointerId: 1 });
        expect(onLayoutChange).toHaveBeenCalledTimes(before);
    });

    it.each([
        ['left', 'right', 'vertical', /-right-px\b/],
        ['top', 'bottom', 'horizontal', /-bottom-px\b/],
    ] as const)('docked %s, the handle sits on the %s edge, facing the content (FP-02)', (side, edge, orientation, place) => {
        renderPane(layoutOn(side));
        expect(handle()).toHaveAttribute('data-resize-edge', edge);
        expect(handle()).toHaveAttribute('aria-orientation', orientation);
        expect(handle().className).toMatch(place);
        expect(handle().className).toMatch(orientation === 'vertical' ? /\bcursor-col-resize\b/ : /\bcursor-row-resize\b/);
    });

    it.each([
        // side, pointer axis, from, to, the stored key and value
        ['left', 'clientX', 194, 254, 'width', 254],
        ['top', 'clientY', 198, 258, 'height', 258],
    ] as const)('docked %s, moving the edge toward the content grows the pane (FP-02)', (side, axis, from, to, key, value) => {
        mockBoxes();
        const layout = layoutOn(side);
        const { onLayoutChange } = renderPane(layout);
        fireEvent.pointerDown(handle(), { [axis]: from, button: 0, pointerId: 1 });
        fireEvent.pointerMove(handle(), { [axis]: to, pointerId: 1 });
        fireEvent.pointerUp(handle(), { [axis]: to, pointerId: 1 });
        const stored = last(onLayoutChange, layout);
        expect(stored[key]).toBe(value);
        // The other axis's size is left alone.
        expect(stored[key === 'width' ? 'height' : 'width']).toBeNull();
    });

    it.each([
        ['left', 'ArrowRight', 'ArrowLeft', 'width'],
        ['top', 'ArrowDown', 'ArrowUp', 'height'],
    ] as const)('docked %s, %s grows the pane 8px and %s shrinks it (FP-02)', (side, grow, shrink, key) => {
        mockBoxes();
        const layout = layoutOn(side, { width: 200, height: 200 });
        const { onLayoutChange } = renderPane(layout);
        fireEvent.keyDown(handle(), { key: grow });
        expect(last(onLayoutChange, layout)[key]).toBe(208);
        fireEvent.keyDown(handle(), { key: shrink });
        expect(last(onLayoutChange, layout)[key]).toBe(192);
    });
});
