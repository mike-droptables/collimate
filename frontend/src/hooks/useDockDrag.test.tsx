// Drag-to-dock on a cell pane's dock icon (FP-04), driven here as the runs
// pane's (right or bottom, its own region attribute): a press below the
// threshold stays a click; past it, the store names the pane and the nearest
// of its allowed edges of the drag's region, and a release there docks it.
// Esc, a release outside the region, a cancelled pointer and an unmount dock
// nothing, and every exit clears the store's drag. Moves and releases are
// sent where a browser sends an uncaptured pointer's events: to whatever
// lies under it, here the page body, never the icon.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/react';

import { useDockDrag } from './useDockDrag';
import { RUNS_PANE_SIDES } from '@/lib/cellLayout';
import { sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { RunsPaneSide } from '@/stores/collimate/types';

const REGION = { left: 100, top: 50, right: 500, bottom: 350, width: 400, height: 300, x: 100, y: 50, toJSON: () => ({}) } as DOMRect;

function Harness({ onDock, onClick }: { onDock: (side: RunsPaneSide) => void; onClick: () => void }) {
    const handlers = useDockDrag({ cellId: 'cell-1', pane: 'runs', regionAttr: 'data-runs-dock-region', sides: RUNS_PANE_SIDES, onDock });
    return (
        <div data-runs-dock-region data-testid="region">
            <span data-testid="handle" {...handlers}>
                <button type="button" onClick={onClick}>Runs</button>
            </span>
        </div>
    );
}

function mount() {
    const onDock = vi.fn();
    const onClick = vi.fn();
    const view = render(<Harness onDock={onDock} onClick={onClick} />);
    vi.spyOn(view.getByTestId('region'), 'getBoundingClientRect').mockReturnValue(REGION);
    const button = view.getByRole('button');
    return { ...view, onDock, onClick, button };
}

const drag = () => useCollimateStore.getState().dockDrag;

beforeEach(() => { useCollimateStore.setState({ dockDrag: null }); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); useCollimateStore.setState({ dockDrag: null }); });

describe('useDockDrag (FP-04)', () => {
    it('a press that moves no further than the threshold stays a click (FP-04)', () => {
        const { button, onClick, onDock } = mount();
        fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 123, clientY: 63 });
        expect(drag()).toBeNull();
        fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 123, clientY: 63 });
        fireEvent.click(button);
        expect(onClick).toHaveBeenCalledTimes(1);
        expect(onDock).not.toHaveBeenCalled();
    });

    it.each([
        ['right', 'right', 490, 200],
        ['bottom', 'bottom', 300, 340],
        // A point nearer a side the pane never docks on goes to the nearer
        // of its own.
        ['left', 'bottom', 110, 200],
        ['top', 'right', 300, 60],
    ] as const)('a release near the %s edge docks the pane on the %s (FP-04)', (_edge, side, x, y) => {
        const { button, onClick, onDock } = mount();
        fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: x, clientY: y });
        expect(drag()).toEqual({ cellId: 'cell-1', pane: 'runs', side });
        fireEvent.pointerUp(document.body, { pointerId: 1, clientX: x, clientY: y });
        // The click that ends the drag is not a click on the button.
        fireEvent.click(button);
        expect(onClick).not.toHaveBeenCalled();
        expect(drag()).toBeNull();
        expect(onDock).toHaveBeenCalledTimes(1);
        expect(onDock).toHaveBeenCalledWith(side);
        // The next real click goes through again.
        fireEvent.pointerDown(button, { button: 0, pointerId: 2, clientX: 120, clientY: 60 });
        fireEvent.pointerUp(document.body, { pointerId: 2, clientX: 120, clientY: 60 });
        fireEvent.click(button);
        expect(onClick).toHaveBeenCalledTimes(1);
    });

    it('takes the keyboard from the scene while lifted; Esc cancels and docks nothing (FP-04)', () => {
        const { button, onClick, onDock } = mount();
        fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 490, clientY: 200 });
        expect(drag()?.side).toBe('right');
        expect(sceneKeyboardOwned(new KeyboardEvent('keydown', { key: 'x' }))).toBe(true);
        fireEvent.keyDown(window, { key: 'Escape' });
        expect(drag()).toBeNull();
        expect(sceneKeyboardOwned(new KeyboardEvent('keydown', { key: 'x' }))).toBe(false);
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 110, clientY: 200 });
        fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 110, clientY: 200 });
        fireEvent.click(button);
        expect(onDock).not.toHaveBeenCalled();
        expect(onClick).not.toHaveBeenCalled();
        expect(drag()).toBeNull();
    });

    it('a release outside the region docks nothing (FP-04)', () => {
        const { button, onDock } = mount();
        fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 700, clientY: 200 });
        expect(drag()).toEqual({ cellId: 'cell-1', pane: 'runs', side: null });
        fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 700, clientY: 200 });
        expect(onDock).not.toHaveBeenCalled();
        expect(drag()).toBeNull();
        expect(sceneKeyboardOwned(new KeyboardEvent('keydown', { key: 'x' }))).toBe(false);
    });

    it('a cancelled pointer and an unmount mid-drag dock nothing and clear the drag (FP-04)', () => {
        const first = mount();
        fireEvent.pointerDown(first.button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 490, clientY: 200 });
        expect(drag()?.side).toBe('right');
        fireEvent.pointerCancel(document.body, { pointerId: 1 });
        expect(drag()).toBeNull();
        fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 110, clientY: 200 });
        expect(first.onDock).not.toHaveBeenCalled();
        first.unmount();

        const second = mount();
        fireEvent.pointerDown(second.button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 300, clientY: 340 });
        expect(drag()?.side).toBe('bottom');
        second.unmount();
        expect(drag()).toBeNull();
        expect(second.onDock).not.toHaveBeenCalled();
        expect(sceneKeyboardOwned(new KeyboardEvent('keydown', { key: 'x' }))).toBe(false);
    });

    it('a first move sample already far outside the icon lifts the drag (FP-04)', () => {
        const { button, onDock } = mount();
        fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 300, clientY: 340 });
        expect(drag()).toEqual({ cellId: 'cell-1', pane: 'runs', side: 'bottom' });
        fireEvent.pointerUp(document.body, { pointerId: 1, clientX: 300, clientY: 340 });
        expect(onDock).toHaveBeenCalledTimes(1);
    });

    it('a press whose release never arrived is dropped: the next plain click hides, it never docks (FP-04)', () => {
        const { button, onClick, onDock } = mount();
        // The release lands outside the window and is never delivered.
        fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        // A later click with a little jitter, far from the stale press's origin.
        fireEvent.pointerDown(button, { button: 0, pointerId: 2, clientX: 300, clientY: 200 });
        fireEvent.pointerMove(document.body, { pointerId: 2, clientX: 302, clientY: 201 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 490, clientY: 200 });
        expect(drag()).toBeNull();
        fireEvent.pointerUp(document.body, { pointerId: 2, clientX: 302, clientY: 201 });
        fireEvent.click(button);
        expect(onClick).toHaveBeenCalledTimes(1);
        expect(onDock).not.toHaveBeenCalled();
    });

    it('Esc before the press lifts is left to the scene (FP-04)', () => {
        const { button } = mount();
        fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        const esc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
        window.dispatchEvent(esc);
        expect(esc.defaultPrevented).toBe(false);
    });

    it('only the primary button starts a drag (FP-04)', () => {
        const { button } = mount();
        fireEvent.pointerDown(button, { button: 2, pointerId: 1, clientX: 120, clientY: 60 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: 490, clientY: 200 });
        expect(drag()).toBeNull();
    });
});
