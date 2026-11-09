// The overview board's camera and clicks, with the real camera (useStackCamera
// over usePanZoomCamera) under StackScene: a press on a card that does not
// drag pans, the reveal of a card selected elsewhere, the journal subject's
// reveal, a new kept set at the board's origin, the zoom control, the entry
// fit landing in the board's own region, a selection made in the overview
// never recentring the zoom-in, and the double click acting on the card its
// first click hit.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps, PointerEvent as ReactPointerEvent } from 'react';
import { animate, type MotionValue } from 'motion/react';
import type { Channel, Stack } from '@/lib/types';
import type { SceneActions } from '@/hooks/useSceneActions';
import type { StackCallbacks } from '../StackGroup';
import type { useStackCamera as UseStackCamera } from '@/hooks/useStackCamera';
import { scrollIntoViewX } from '@/hooks/useStackCamera';
import { overviewFit, type OverviewBounds } from '@/hooks/useStackLayout';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { StackScene } from '../StackScene';

interface Layout {
    viewSize: { width: number; height: number };
    positions: number[];
    widths: number[];
    overviewBounds: OverviewBounds;
    overviewTarget: { x: number; y: number; zoom: number };
}

const fixture = vi.hoisted(() => ({
    layout: null as unknown as Layout,
    camera: null as unknown as ReturnType<typeof UseStackCamera>,
    /** The centerRequested StackScene handed the camera, render by render. */
    centerRequested: [] as number[],
    /** The scene's card-drag start (a card's left press). */
    cellDragStart: (() => {}) as (...args: unknown[]) => void,
    pool: {
        registerOverlay: () => {}, bringCellsToFront: () => {}, invalidate: () => {},
        markMoving: () => {}, registerMotionLayer: () => () => {},
    },
}));

vi.mock('motion/react', async importOriginal => {
    const actual = await importOriginal<typeof import('motion/react')>();
    return { ...actual, animate: vi.fn(actual.animate) };
});
vi.mock('@/hooks/useStackCamera', async importOriginal => {
    const actual = await importOriginal<typeof import('@/hooks/useStackCamera')>();
    function useWatchedCamera(params: Parameters<typeof actual.useStackCamera>[0]) {
        fixture.centerRequested.push(params.centerRequested);
        const camera = actual.useStackCamera(params);
        fixture.camera = camera;
        return camera;
    }
    return { ...actual, useStackCamera: useWatchedCamera };
});
vi.mock('@/hooks/useStackLayout', async importOriginal => {
    const actual = await importOriginal<typeof import('@/hooks/useStackLayout')>();
    const rest = {
        GAP: 14, FRONT_CARD_OFFSET: 30, naturalWidths: [], arrowWorldX: null, isCancelMove: false,
        cellPendingMoveWorldX: null, cardPixelHeight: 248, stackContainerHeight: 600, stackTopOffset: 14,
        cardFrame: { stackTopOffset: 14, stripHeight: 30, cardPixelHeight: 248 }, screenMax: 2000, sceneSwapNonce: 0,
    };
    // One layout object per fixture layout, as the real hook memoises it.
    const cache = new WeakMap<object, object>();
    const useLayout = () => {
        if (!cache.has(fixture.layout)) cache.set(fixture.layout, { ...rest, ...fixture.layout });
        return cache.get(fixture.layout);
    };
    return { ...actual, useStackLayout: useLayout };
});
vi.mock('@/hooks/useStackDrag', async () => {
    const { motionValue } = await import('motion/react');
    const ghost = motionValue(0);
    const useDrag = ({ stacks }: { stacks: Stack[] }) => ({
        items: stacks.map((s, i) => ({
            ...s, x: fixture.layout.positions[i] ?? 0, computedWidth: fixture.layout.widths[i] ?? 272, isOffscreen: false,
        })),
        ghostXMV: ghost, ghostYMV: ghost, dragGhost: null, dropTargetIdx: null, intraStackCellIdx: null,
        mergeTargetIdx: null, recentlyDroppedId: null, recentlyDroppedStackId: null, previewCellData: null,
        floatingCellData: null, arrowState: null, insertLineX: null,
        handleStackDragStart: () => {}, handleCellDragStart: (...args: unknown[]) => fixture.cellDragStart(...args),
        handleDragPointerMove: () => {}, handleDragPointerUp: () => {},
    });
    return { useStackDrag: useDrag };
});
// The column: its cards, as StackGroup lays them out in zoom-out.
vi.mock('../StackGroup', async () => {
    const { OverviewCard, useOverviewBoard } = await import('./OverviewCard');
    const store = await import('@/stores/useCollimateStore');
    function Column({ stack, callbacks }: { stack: Stack; callbacks: StackCallbacks }) {
        const keep = useOverviewBoard()?.keep ?? null;
        const selected = store.useCollimateStore(s => s.selectedCellId);
        return (
            <div data-ov-column>
                {stack.cells.filter(c => !keep || keep.has(c.id)).map(c => {
                    const idx = stack.cells.indexOf(c);
                    return (
                        <OverviewCard key={c.id} cell={c} selected={selected === c.id} isOffscreen={false}
                            onDragStart={callbacks.onCellDragStart
                                ? (e: ReactPointerEvent) => callbacks.onCellDragStart!(idx, c.id, e) : undefined}
                            onOpen={callbacks.onZoomToCell ? () => callbacks.onZoomToCell!(idx) : undefined} />
                    );
                })}
            </div>
        );
    }
    return { StackGroup: Column, NO_OP_STACK_CALLBACKS: {}, FLOATING_PREVIEW_STACK_ID: 'floating' };
});
vi.mock('../SceneDragOverlays', () => ({ SceneDragOverlays: () => null, SceneCursorOverlays: () => null }));
vi.mock('@/contexts/GuestPoolContext', () => ({ useGuestPool: () => fixture.pool }));
vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: () => {} }));
vi.mock('@/hooks/useEntryHold', () => ({ useEntryHold: () => new Set() }));
// The overview's overlays have their own suites.
vi.mock('./RemovedShelf', () => ({ RemovedShelf: () => null }));
vi.mock('./RemovedBar', () => ({ RemovedBar: () => null }));
vi.mock('./JournalPane', () => ({ JournalPane: () => null }));
vi.mock('./OverviewArrows', () => ({ OverviewArrows: () => null }));

const VIEW = { width: 1200, height: 800 };
const stacks: Stack[] = [
    { id: 's0', name: 'One', cells: [{ id: 'a', name: 'Frame', files: [] }, { id: 'b', name: 'Notes', files: [] }] },
    { id: 's1', name: 'Two', cells: [{ id: 'c', name: 'Op log', files: [] }] },
    { id: 's2', name: 'Three', cells: [{ id: 'x', name: 'Far', files: [] }] },
];
const channel = { id: 'ch', name: 'ch', main_stacks: stacks, nav_stacks: [] } as unknown as Channel;
const BOUNDS: OverviewBounds = { left: -154, right: 1354, top: 0, bottom: 700 };
const layoutFor = (view: { width: number; height: number }): Layout => ({
    viewSize: view, positions: [0, 600, 1200], widths: [272, 272, 272],
    overviewBounds: BOUNDS, overviewTarget: overviewFit(BOUNDS, view, true),
});

const initialStore = useCollimateStore.getState();
const initialSpatial = useSpatialStore.getState();
beforeEach(() => {
    useCollimateStore.setState(initialStore, true);
    useSpatialStore.setState(initialSpatial, true);
    useCollimateStore.setState({ activeChannel: channel, selectedCellId: null, pendingSelectCellId: null, runs: {} } as never);
    useCollimateStore.setState(s => ({ overview: defaultOverviewView(), overviewData: { ...s.overviewData, lineage: { cells: [], moves: [] } } }));
    fixture.layout = layoutFor(VIEW);
    fixture.centerRequested = [];
    fixture.cellDragStart = vi.fn();
    vi.mocked(animate).mockClear();
});
afterEach(cleanup);

const mount = (over: Partial<ComponentProps<typeof StackScene>> = {}) => {
    const props: ComponentProps<typeof StackScene> = {
        stacks, selectedId: 's0', actions: {} as SceneActions, transitionType: 'nav', topology: 'stable',
        isZoomedOut: true, fan: null, fanAnchor: null, onCollapseFan: vi.fn(), onSelectStack: vi.fn(),
        onZoomToCell: vi.fn(), ...over,
    };
    const view = render(<StackScene {...props} />);
    const board = view.container.querySelector('[data-overview-board]') as HTMLElement | null;
    return { ...view, props, board };
};
const cam = () => fixture.camera;
/** Stop the camera's springs and put it at a known place. */
const park = (x: number, y: number, zoom: number) => act(() => {
    cam().cameraXMV.jump(x);
    cam().cameraYMV.jump(y);
    cam().zoomMV.jump(zoom);
});
const card = (id: string) => document.querySelector(`[data-ov-card="${id}"]`) as HTMLElement;
const rect = (el: HTMLElement, left: number, top: number, width: number, height: number) => {
    el.getBoundingClientRect = () => new DOMRect(left, top, width, height);
};
/** The board region on screen: 1200x800 at the client's (0, 30). */
const boardRect = (board: HTMLElement) => rect(board, 0, 30, VIEW.width, VIEW.height);
// The mock's call type is animate's last overload; a MotionValue<number>'s target is a number.
const lastTarget = (mv: MotionValue<number>) =>
    (vi.mocked(animate).mock.calls as unknown[][]).filter(([value]) => value === mv).at(-1)?.[1] as number | undefined;

describe('the overview camera (O01)', () => {
    it('with Only connected on, a press on a card that does not drag pans the board after 4px (O01)', () => {
        useCollimateStore.setState({ selectedCellId: 'a' });
        useCollimateStore.setState(s => ({ overview: { ...s.overview, journalOpen: true, onlyConnected: true } }));
        mount();
        park(100, 0, 0.5);
        const a = card('a');
        expect(a.hasAttribute('data-stack-card')).toBe(false);
        fireEvent.pointerDown(a, { button: 0, pointerId: 1, clientX: 300, clientY: 300 });
        fireEvent.pointerMove(a, { pointerId: 1, clientX: 305, clientY: 300 });
        // 5 screen px at 50% is 10 world px, against the drag.
        expect(cam().cameraXMV.get()).toBe(90);
        fireEvent.pointerUp(a, { pointerId: 1, clientX: 305, clientY: 300 });
    });

    it('a card that drags does not pan the board (O01)', () => {
        mount();
        park(100, 0, 0.5);
        const a = card('a');
        expect(a.hasAttribute('data-stack-card')).toBe(true);
        fireEvent.pointerDown(a, { button: 0, pointerId: 1, clientX: 300, clientY: 300 });
        fireEvent.pointerMove(a, { pointerId: 1, clientX: 340, clientY: 300 });
        expect(cam().cameraXMV.get()).toBe(100);
    });

    it.each([['a card that drags', 'a'], ['the background', null]] as const)(
        'a middle drag from %s pans exactly as a right drag does, selecting and lifting nothing (O01)', (_where, from) => {
            const { board } = mount();
            const run = (button: number) => {
                park(100, 0, 0.5);
                const at = from ? card(from) : board!;
                if (from) expect(at.hasAttribute('data-stack-card')).toBe(true);
                const down = fireEvent.pointerDown(at, { button, pointerId: 1, clientX: 400, clientY: 300 });
                fireEvent.pointerMove(at, { pointerId: 1, clientX: 300, clientY: 240 });
                fireEvent.pointerMove(at, { pointerId: 1, clientX: 200, clientY: 180 });
                fireEvent.pointerUp(at, { button, pointerId: 1, clientX: 200, clientY: 180 });
                return { down, cam: [cam().cameraXMV.get(), cam().cameraYMV.get()] };
            };
            const right = run(2);
            // (-200, -120) screen px at 50% is (400, 240) world px against it.
            expect(right.cam).toEqual([500, 240]);
            const middle = run(1);
            expect(middle.cam).toEqual(right.cam);
            // The middle press is cancelled (no autoscroll, no paste).
            expect(middle.down).toBe(false);
            const s = useCollimateStore.getState();
            expect(s.selectedCellId).toBeNull();
            expect(s.pendingSelectCellId).toBeNull();
            expect(fixture.cellDragStart).not.toHaveBeenCalled();
        });

    it.each([['a card that drags', 'a', false], ['a card that does not', 'a', true], ['the background', null, false]] as const)(
        'a plain middle click on %s does what a plain right click does: nothing, no browser default (O01)', (_where, from, connected) => {
            if (connected) useCollimateStore.setState(s => ({ overview: { ...s.overview, journalOpen: true, onlyConnected: true, anchor: 'a' } }));
            const { board } = mount();
            park(100, 0, 0.5);
            const at = from ? card(from) : board!;
            if (from) expect(at.hasAttribute('data-stack-card')).toBe(!connected);
            const click = (button: number) => {
                const down = fireEvent.pointerDown(at, { button, pointerId: 1, clientX: 400, clientY: 300 });
                fireEvent.pointerUp(at, { button, pointerId: 1, clientX: 400, clientY: 300 });
                // The release's default: the right button's context menu,
                // the middle button's auxclick (autoscroll, X11 paste).
                const release = button === 2
                    ? fireEvent.contextMenu(at, { button })
                    : fireEvent(at, new MouseEvent('auxclick', { button, bubbles: true, cancelable: true }));
                return { down, release };
            };
            expect(click(2)).toEqual({ down: true, release: false });
            expect(click(1)).toEqual({ down: false, release: false });
            expect(cam().cameraXMV.get()).toBe(100);
            const s = useCollimateStore.getState();
            expect(s.selectedCellId).toBeNull();
            expect(s.pendingSelectCellId).toBeNull();
            expect(fixture.cellDragStart).not.toHaveBeenCalled();
        });

    it('a card selected elsewhere comes 12px inside the region, moving only as far as it must (O01)', async () => {
        const { board } = mount();
        park(500, 0, 0.5);
        boardRect(board!);
        // Off the region's right edge: right at 1372 against 1200 - 12.
        rect(card('x'), 1100, 100, 272, 248);
        // Inside already: no move.
        rect(card('c'), 400, 100, 272, 248);
        act(() => useCollimateStore.getState().requestSelectCell('c'));
        await act(() => new Promise(requestAnimationFrame));
        expect(cam().cameraXMV.get()).toBe(500);
        act(() => useCollimateStore.getState().requestSelectCell('x'));
        // dx = 1188 - 1372 = -184 screen px, 368 world px at 50%.
        await waitFor(() => expect(cam().cameraXMV.get()).toBe(500 + 368));
        expect(cam().cameraYMV.get()).toBe(0);
    });

    it('above the region: the card lands 8px under its top (O01)', async () => {
        const { board } = mount();
        park(500, 0, 0.5);
        boardRect(board!);
        rect(card('x'), 100, -200, 272, 248);
        act(() => useCollimateStore.getState().requestSelectCell('x'));
        // dy = 38 - (-200) = 238 screen px, 476 world px at 50%.
        await waitFor(() => expect(cam().cameraYMV.get()).toBe(-476));
        expect(cam().cameraXMV.get()).toBe(500);
    });

    it('a click on a card does not move the board under the pointer (O01)', async () => {
        const { board } = mount();
        park(500, 0, 0.5);
        boardRect(board!);
        rect(card('x'), 1100, 100, 272, 248);
        fireEvent.pointerDown(card('x'), { button: 0, pointerId: 1, clientX: 1150, clientY: 150 });
        fireEvent.pointerUp(card('x'), { pointerId: 1, clientX: 1150, clientY: 150 });
        fireEvent.click(card('x'), { detail: 1 });
        expect(useCollimateStore.getState().pendingSelectCellId).toBe('x');
        await act(() => new Promise(requestAnimationFrame));
        await act(() => new Promise(requestAnimationFrame));
        expect(cam().cameraXMV.get()).toBe(500);
    });

    it('the journal\'s subject comes into view when the journal opens (O01)', async () => {
        useCollimateStore.setState({ selectedCellId: 'x' });
        const { board } = mount();
        park(500, 0, 0.5);
        boardRect(board!);
        rect(card('x'), 1100, 100, 272, 248);
        act(() => useCollimateStore.getState().setJournalOpen(true));
        await waitFor(() => expect(cam().cameraXMV.get()).toBe(500 + 368));
    });

    it('a new kept set starts the board at its top-left, at the current zoom (O01)', () => {
        useCollimateStore.setState({ selectedCellId: 'a' });
        mount();
        park(500, 200, 0.5);
        act(() => useCollimateStore.setState(s => ({ overview: { ...s.overview, journalOpen: true, onlyConnected: true } })));
        // Screen x = W/2 + z(world - camX), y = H/2 + z(world - camY - H/2):
        // the bounds' left and top at (0, 0).
        expect(cam().cameraXMV.get()).toBe(BOUNDS.left + VIEW.width / (2 * 0.5));
        expect(cam().cameraYMV.get()).toBe(BOUNDS.top - VIEW.height / 2 + VIEW.height / (2 * 0.5));
        expect(cam().zoomMV.get()).toBe(0.5);
    });

    it('the zoom control: − and + zoom about the board\'s centre by 0.8 and 1.25; Fit fits the whole board (O01)', () => {
        mount();
        park(300, 40, 1);
        fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
        expect(cam().zoomMV.get()).toBeCloseTo(0.8);
        // The world point at the centre stays put.
        expect(cam().cameraXMV.get()).toBe(300);
        expect(cam().cameraYMV.get()).toBe(40);
        fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
        expect(cam().zoomMV.get()).toBeCloseTo(1);
        fireEvent.click(screen.getByRole('button', { name: 'Fit' }));
        const fit = overviewFit(BOUNDS, VIEW, false);
        expect(cam().zoomMV.get()).toBe(fit.zoom);
        expect(cam().cameraXMV.get()).toBe(fit.x);
        expect(cam().cameraYMV.get()).toBe(fit.y);
    });

    it('entering with the journal open lands the board fitted to its narrower region (O01)', () => {
        const { props, rerender } = mount({ isZoomedOut: false });
        // The entry springs to the fit of the board's main-view size ...
        rerender(<StackScene {...props} isZoomedOut />);
        expect(lastTarget(cam().zoomMV)).toBe(overviewFit(BOUNDS, VIEW, true).zoom);
        // ... and the overview's region, measured a frame later, re-aims it.
        const region = { width: VIEW.width - 374, height: VIEW.height - 33 };
        fixture.layout = layoutFor(region);
        rerender(<StackScene {...props} isZoomedOut />);
        const x = lastTarget(cam().cameraXMV) as number;
        const y = lastTarget(cam().cameraYMV) as number;
        const z = lastTarget(cam().zoomMV) as number;
        const w = BOUNDS.right - BOUNDS.left;
        expect(z).toBeCloseTo(region.width / w);
        // The board's top-left on screen: (sx, 0) of the region.
        const sx = Math.max(0, (region.width - w * z) / 2);
        expect(region.width / 2 + z * (BOUNDS.left - x)).toBeCloseTo(sx);
        expect(region.height / 2 + z * (BOUNDS.top - y - region.height / 2)).toBeCloseTo(0);
    });

    it('a selection made in the overview does not recentre the zoom-in: it scrolls into view (O01)', () => {
        fixture.layout = { ...layoutFor(VIEW), widths: [500, 500, 500] };
        const { props, rerender } = mount({ isZoomedOut: false });
        const initial = fixture.centerRequested.at(-1)!;
        rerender(<StackScene {...props} isZoomedOut />);
        // A card selected in the overview: StackApp selects its stack and
        // asks for a re-frame.
        act(() => useSpatialStore.getState().setCenterRequested(n => n + 1));
        rerender(<StackScene {...props} isZoomedOut selectedId="s2" />);
        expect(fixture.centerRequested.at(-1)).toBe(initial);
        vi.mocked(animate).mockClear();
        rerender(<StackScene {...props} isZoomedOut={false} selectedId="s2" />);
        const reveal = scrollIntoViewX(0, { stackCenter: 1200, stackWidth: 500, viewWidth: VIEW.width, gap: 14 }, 0);
        expect(reveal).not.toBeNull();
        expect(lastTarget(cam().cameraXMV)).toBe(reveal);
        // A request in the main view still reaches the camera.
        act(() => useSpatialStore.getState().setCenterRequested(n => n + 1));
        expect(fixture.centerRequested.at(-1)).toBe(initial + 2);
        expect(lastTarget(cam().cameraXMV)).toBe(1200);
    });
});

describe('the board before its first measurement (O01)', () => {
    // useStackLayout observes the board div it found on mount; the measured
    // tree must keep that node, or the scene stays unmeasured and blank.
    it.each([true, false])('keeps the measured board node when the scene fills in (zoomed out: %s) (O01)', (isZoomedOut) => {
        fixture.layout = layoutFor({ width: 0, height: 0 });
        const { props, rerender, container } = mount({ isZoomedOut });
        const unmeasured = container.firstElementChild!.firstElementChild;
        expect(unmeasured).not.toBeNull();
        fixture.layout = layoutFor(VIEW);
        rerender(<StackScene {...props} />);
        expect(container.firstElementChild!.firstElementChild).toBe(unmeasured);
        expect(container.querySelectorAll('[data-ov-card]').length).toBeGreaterThan(0);
    });
});

describe('the overview double click (O01)', () => {
    const fakeNow = (t: number) => vi.spyOn(performance, 'now').mockReturnValue(t);
    afterEach(() => { vi.restoreAllMocks(); });

    it('acts on the card its first click hit when the second lands on another card (O01)', () => {
        const { props } = mount();
        fakeNow(1000);
        fireEvent.click(card('a'), { detail: 1 });
        expect(useCollimateStore.getState().pendingSelectCellId).toBe('a');
        fakeNow(1300);
        // The board moved: the second click lands on another card.
        fireEvent.click(card('c'), { detail: 2 });
        expect(props.onZoomToCell).toHaveBeenCalledExactlyOnceWith('s0', 0);
        // The second click is the double click's alone: c is not selected.
        expect(useCollimateStore.getState().pendingSelectCellId).toBe('a');
    });

    it('a second click later than 800ms, or after a first click on a button, opens nothing (O01)', () => {
        useCollimateStore.setState({ selectedCellId: 'a', removeFromBoard: vi.fn(async () => true) } as never);
        const { props } = mount();
        fakeNow(1000);
        fireEvent.click(card('b'), { detail: 1 });
        fakeNow(1900);
        fireEvent.click(card('b'), { detail: 2 });
        expect(props.onZoomToCell).not.toHaveBeenCalled();
        act(() => useCollimateStore.setState({ selectedCellId: 'a' }));
        fakeNow(3000);
        fireEvent.click(screen.getByRole('button', { name: 'Remove' }), { detail: 1 });
        fakeNow(3100);
        fireEvent.click(card('a'), { detail: 2 });
        expect(props.onZoomToCell).not.toHaveBeenCalled();
    });

    it('with Only connected on it follows the first card\'s lines unless the first click already did (O01)', () => {
        useCollimateStore.setState({ selectedCellId: 'a' });
        useCollimateStore.setState(s => ({ overview: { ...s.overview, journalOpen: true, onlyConnected: true, anchor: 'a' } }));
        // b is connected to a, so it stays on the board.
        const end = (cell_id: string) => ({ cell_id, version_id: null });
        useCollimateStore.setState(s => ({ overviewData: { ...s.overviewData, lineage: { cells: [], moves: [
            { id: 1, op: 'duplicate', at: '2026-10-05T08:00:00Z', op_id: 'o1', from: [end('a')], to: [end('b')] },
        ] } } }));
        const { props } = mount();
        const anchorTo = vi.fn();
        useCollimateStore.setState({ anchorTo } as never);
        fakeNow(1000);
        fireEvent.click(card('b'), { detail: 1 });
        // The first click only selected b (the card says "Click again").
        expect(useCollimateStore.getState().overview.anchor).toBe('a');
        fakeNow(1200);
        fireEvent.click(card('b'), { detail: 2 });
        expect(anchorTo).toHaveBeenCalledExactlyOnceWith('b');
        expect(props.onZoomToCell).not.toHaveBeenCalled();
    });
});
