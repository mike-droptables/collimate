import { describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { insideRect, shelfZoneAt, useShelfDrag } from './useShelfDrag';

// The overview from y 0, the shelf open to 316, the bar 316-349, the board
// from 349, 660 wide (the journal pane on the right).
const overview = { left: 0, right: 1000, top: 0, bottom: 800 };
const board = { left: 0, right: 660, top: 349, bottom: 800 };

describe('the line between the board and the shelf', () => {
    it('reads the 33px band right above the board as the bar and the rest above it as the shelf (polish)', () => {
        expect(shelfZoneAt(300, 345, board, overview)).toBe('bar');
        expect(shelfZoneAt(300, 316, board, overview)).toBe('bar');
        expect(shelfZoneAt(300, 315, board, overview)).toBe('shelf');
        expect(shelfZoneAt(300, 0, board, overview)).toBe('shelf');
        // The board, the journal pane, above the overview: neither.
        expect(shelfZoneAt(300, 348, board, overview)).toBe('bar');
        expect(shelfZoneAt(300, 349, board, overview)).toBeNull();
        expect(shelfZoneAt(700, 200, board, overview)).toBeNull();
        expect(shelfZoneAt(300, -5, board, overview)).toBeNull();
        // With the shelf shut the board starts under the bar at 33.
        expect(shelfZoneAt(300, 10, { ...board, top: 33 }, overview)).toBe('bar');
    });

    it('reads a point on the board edge as inside it (polish)', () => {
        expect(insideRect(0, 349, board)).toBe(true);
        expect(insideRect(661, 400, board)).toBe(false);
    });
});

describe('a shelf card carried onto the board', () => {
    // The overview with the shelf grid at the top and the board from 349.
    const dom = () => {
        const root = document.createElement('div');
        root.setAttribute('data-overview-root', '');
        const boardEl = document.createElement('div');
        boardEl.setAttribute('data-overview-board', '');
        boardEl.getBoundingClientRect = () => ({ ...board, x: 0, y: 349, width: 660, height: 451, toJSON: () => ({}) }) as DOMRect;
        const grid = document.createElement('div');
        grid.setPointerCapture = vi.fn();
        grid.releasePointerCapture = vi.fn();
        const card = document.createElement('div');
        card.dataset.rmCard = 'd';
        card.getBoundingClientRect = () => ({ left: 10, top: 10, right: 250, bottom: 150, x: 10, y: 10, width: 240, height: 140, toJSON: () => ({}) }) as DOMRect;
        const label = document.createElement('span');
        card.append(label);
        grid.append(card);
        root.append(grid, boardEl);
        document.body.append(root);
        return { root, grid, label };
    };
    const ev = (target: Element, currentTarget: Element, clientX: number, clientY: number) =>
        ({ button: 0, pointerId: 7, target, currentTarget, clientX, clientY }) as unknown as React.PointerEvent<HTMLElement>;

    const setup = () => {
        const restoreRemoved = vi.fn(async () => true);
        useCollimateStore.setState({ overview: defaultOverviewView(), restoreRemoved });
        const d = dom();
        const hook = renderHook(() => useShelfDrag(true));
        const lift = () => {
            act(() => hook.result.current.gridHandlers.onPointerDown(ev(d.label, d.grid, 50, 50)));
            act(() => hook.result.current.gridHandlers.onPointerMove(ev(d.label, d.grid, 60, 60)));
        };
        const over = (x: number, y: number) => act(() => hook.result.current.gridHandlers.onPointerMove(ev(d.label, d.grid, x, y)));
        const up = (x: number, y: number) => act(() => hook.result.current.gridHandlers.onPointerUp(ev(d.label, d.grid, x, y)));
        return { ...d, hook, restoreRemoved, lift, over, up, cleanup: () => { hook.unmount(); d.root.remove(); } };
    };

    it('lifts out of the grid (which reflows without it) and, over the board, tells the board where the pointer is (polish)', async () => {
        const t = setup();
        try {
            t.lift();
            expect(t.hook.result.current.lifted).toMatchObject({ cellId: 'd', width: 240, height: 140 });
            expect(t.grid.setPointerCapture).toHaveBeenCalledWith(7);
            expect(useCollimateStore.getState().overview.drop).toEqual({ kind: 'restore', cellId: 'd', point: null, target: null });
            t.over(300, 500);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ point: { x: 300, y: 500 } });
            // The board answers with the target (useStackDrag); let go there, the card is restored into it.
            const target = { kind: 'stack', stackId: 's1', index: 2 } as const;
            act(() => useCollimateStore.getState().setOverviewDrop({ ...useCollimateStore.getState().overview.drop!, target } as never));
            await act(async () => { t.up(300, 500); await Promise.resolve(); });
            expect(t.restoreRemoved).toHaveBeenCalledExactlyOnceWith('d', target);
            expect(t.hook.result.current.lifted).toBeNull();
        } finally { t.cleanup(); }
    });

    it('Escape puts the card back on the shelf: no gap, no restore, and the release does nothing (polish)', async () => {
        const t = setup();
        try {
            t.lift();
            t.over(300, 500);
            act(() => useCollimateStore.getState().setOverviewDrop({ ...useCollimateStore.getState().overview.drop!, target: { kind: 'column', slot: 1 } } as never));
            // The lifted card owns the keyboard: the scene's Esc (leave the
            // overview) stands back, whichever listener the window runs first.
            const esc = new KeyboardEvent('keydown', { key: 'Escape' });
            expect(sceneKeyboardOwned(esc)).toBe(true);
            act(() => { window.dispatchEvent(esc); });
            expect(sceneKeyboardOwned(new KeyboardEvent('keydown', { key: 'Escape' }))).toBe(false);
            expect(t.hook.result.current.lifted).toBeNull();
            expect(useCollimateStore.getState().overview.drop).toBeNull();
            expect(t.grid.releasePointerCapture).toHaveBeenCalledWith(7);
            await act(async () => { t.up(300, 500); await Promise.resolve(); });
            expect(t.restoreRemoved).not.toHaveBeenCalled();
        } finally { t.cleanup(); }
    });

    it('let go over the board where the board names no landing (as over a generator\'s column), the card goes back to the shelf without a restore (polish)', async () => {
        const t = setup();
        try {
            t.lift();
            t.over(300, 500);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ point: { x: 300, y: 500 }, target: null });
            await act(async () => { t.up(300, 500); await Promise.resolve(); });
            expect(t.restoreRemoved).not.toHaveBeenCalled();
            expect(useCollimateStore.getState().overview.drop).toBeNull();
        } finally { t.cleanup(); }
    });

    it('let go off the board, the card goes back to the shelf without a restore (polish)', async () => {
        const t = setup();
        try {
            t.lift();
            t.over(300, 500);
            t.over(700, 500);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ point: null, target: null });
            await act(async () => { t.up(700, 500); await Promise.resolve(); });
            expect(t.restoreRemoved).not.toHaveBeenCalled();
            expect(useCollimateStore.getState().overview.drop).toBeNull();
        } finally { t.cleanup(); }
    });
});
