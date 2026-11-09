import {act} from 'react';
import {render} from 'vitest-browser-react';
import {afterEach, expect, it} from 'vitest';
import {useStackCamera} from './useStackCamera';
import type {StackLayoutResult} from './useStackLayout';
import type {Stack} from '@/lib/types';
import {useCollimateStore} from '@/stores/useCollimateStore';
import {COMMAND_PANE_WIDTH, defaultCommandPane} from '@/stores/collimate/commandPaneSlice';

afterEach(() => { useCollimateStore.setState({commandPane: defaultCommandPane()}); });

// One 500px stack centred at world 0 in a 1000px canvas with a 14px gap: at
// camera 0 it sits in the middle, its left edge 250px in from the canvas's.
const target = {current: 0};
const stacks = [{id: 's1', name: 'Frame', cells: []}] as Stack[];
const layout = {
    viewSize: {width: 1000, height: 800}, GAP: 14, positions: [0], widths: [500],
    arrowWorldX: null, cellPendingMoveWorldX: null, overviewTarget: {x: 0, y: 0, zoom: 1}, sceneSwapNonce: 0,
} as unknown as StackLayoutResult;
function Probe() {
    useStackCamera({
        layout, stacks, selectedId: 's1', isZoomedOut: false, topology: 'stable', pendingMove: null,
        centerRequested: 0, edgeAlignRequested: null, cameraHold: false, widthDragActive: false,
        cameraTargetRef: target, rootDivRef: {current: null},
    });
    return null;
}
// Where the stack's edges land on screen for the camera's target.
const screenLeft = () => -250 - target.current + 500;
const screenRight = () => 250 - target.current + 500;
const pane = () => useCollimateStore.getState();

it('the canvas slides just far enough for the selected cell to clear the Commands pane, and only when covered (K01)', async () => {
    target.current = 0;
    const view = await render(<Probe />);
    try {
        expect(target.current).toBe(0);
        act(() => pane().openCommandPane());
        await expect.poll(() => screenLeft()).toBe(COMMAND_PANE_WIDTH + 14);
        act(() => {
            pane().closeCommandPane();
            pane().setCommandPaneSide('right');
            pane().openCommandPane();
        });
        await expect.poll(() => screenRight()).toBe(1000 - COMMAND_PANE_WIDTH - 14);
    } finally {await view.unmount();}
});

it('closing the Commands pane slides the canvas back, and docking across slides it the other way (K01)', async () => {
    target.current = 0;
    const view = await render(<Probe />);
    try {
        act(() => pane().openCommandPane());
        await expect.poll(() => screenLeft()).toBe(COMMAND_PANE_WIDTH + 14);
        act(() => pane().setCommandPaneSide('right'));
        await expect.poll(() => screenRight()).toBe(1000 - COMMAND_PANE_WIDTH - 14);
        act(() => pane().setCommandPaneSide('left'));
        await expect.poll(() => screenLeft()).toBe(COMMAND_PANE_WIDTH + 14);
        act(() => pane().closeCommandPane());
        await expect.poll(() => screenLeft()).toBe(250);
    } finally {await view.unmount();}
});

// A full-width stack: the layout sizes it to what the pane leaves free
// (screenMax), keeping its centre at world 0.
const fullLayout = (paneOpen: boolean) => ({
    ...layout, widths: [1000 - 2 * 14 - (paneOpen ? COMMAND_PANE_WIDTH : 0)],
}) as unknown as StackLayoutResult;
const FULL = {open: fullLayout(true), shut: fullLayout(false)};
function FullProbe() {
    const open = useCollimateStore(s => s.commandPane.open);
    useStackCamera({
        layout: open ? FULL.open : FULL.shut, stacks, selectedId: 's1', isZoomedOut: false, topology: 'stable', pendingMove: null,
        centerRequested: 0, edgeAlignRequested: null, cameraHold: false, widthDragActive: false,
        cameraTargetRef: target, rootDivRef: {current: null},
    });
    return null;
}
const fullEdges = () => {
    const w = (pane().commandPane.open ? FULL.open : FULL.shut).widths[0];
    return [-w / 2 - target.current + 500, w / 2 - target.current + 500];
};

it('the camera centres a full-width stack in the band the Commands pane leaves, on either side, and gives it the whole width back (polish)', async () => {
    target.current = 0;
    const view = await render(<FullProbe />);
    try {
        expect(fullEdges()).toEqual([14, 986]);
        act(() => pane().openCommandPane());
        await expect.poll(() => fullEdges()).toEqual([COMMAND_PANE_WIDTH + 14, 986]);
        act(() => pane().setCommandPaneSide('right'));
        await expect.poll(() => fullEdges()).toEqual([14, 1000 - COMMAND_PANE_WIDTH - 14]);
        act(() => pane().closeCommandPane());
        await expect.poll(() => fullEdges()).toEqual([14, 986]);
    } finally {await view.unmount();}
});
