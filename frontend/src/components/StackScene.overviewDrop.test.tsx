// A card dragged on the overview board, through StackScene with the real
// drag (useStackDrag) and the real columns (StackGroup): the column under
// the pointer opens its marked gap where the pointer points, and the drop
// sends the move to that stack position; over its own place the column
// shows its gap there, and let go there nothing is sent; over the Removed
// bar or shelf the remove zone under the pointer gives the removal its action.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import type { ComponentProps } from 'react';
import type { Channel, Stack } from '@/lib/types';
import type { SceneActions } from '@/hooks/useSceneActions';
import type { useStackCamera as UseStackCamera } from '@/hooks/useStackCamera';
import { overviewCardTop, type OverviewBounds } from '@/hooks/useStackLayout';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { OVERVIEW_CARD_HEIGHT, defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { FocusActionsContext, FocusAreaContext, type FocusActions } from '@/contexts/FocusContext';
import { StackScene } from './StackScene';

const fixture = vi.hoisted(() => ({
    camera: null as unknown as ReturnType<typeof UseStackCamera>,
    pool: {
        registerOverlay: () => {}, bringCellsToFront: () => {}, invalidate: () => {},
        markMoving: () => {}, registerMotionLayer: () => () => {},
    },
}));

vi.mock('./CellEditor', () => ({ default: () => null }));
vi.mock('@/lib/api/history', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api/history')>()),
    fetchCellHistory: vi.fn(() => new Promise(() => {})),
}));
vi.mock('@/hooks/useStackCamera', async importOriginal => {
    const actual = await importOriginal<typeof import('@/hooks/useStackCamera')>();
    function useWatchedCamera(params: Parameters<typeof actual.useStackCamera>[0]) {
        const camera = actual.useStackCamera(params);
        fixture.camera = camera;
        return camera;
    }
    return { ...actual, useStackCamera: useWatchedCamera };
});
// Two 272px columns 18px apart, the first from world x 0, in a 1200 x 800 view.
vi.mock('@/hooks/useStackLayout', async importOriginal => {
    const actual = await importOriginal<typeof import('@/hooks/useStackLayout')>();
    const bounds: OverviewBounds = { left: 0, right: 562, top: 0, bottom: 900 };
    const layout = {
        viewSize: { width: 1200, height: 800 }, positions: [136, 426], widths: [272, 272], naturalWidths: [272, 272],
        GAP: 18, FRONT_CARD_OFFSET: 0, arrowWorldX: null, isCancelMove: false, cellPendingMoveWorldX: null,
        cardPixelHeight: 248, stackContainerHeight: 900, stackTopOffset: 14,
        cardFrame: { stackTopOffset: 14, stripHeight: 30, cardPixelHeight: 248 }, screenMax: 2000, sceneSwapNonce: 0,
        overviewBounds: bounds, overviewTarget: { x: 600, y: 0, zoom: 1 },
    };
    return { ...actual, useStackLayout: () => layout };
});
vi.mock('./SceneDragOverlays', () => ({ SceneDragOverlays: () => null, SceneCursorOverlays: () => null }));
vi.mock('@/contexts/GuestPoolContext', () => ({ useGuestPool: () => fixture.pool }));
vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: () => {} }));
vi.mock('@/hooks/useEntryHold', () => ({ useEntryHold: () => new Set() }));
vi.mock('./overview/RemovedShelf', () => ({ RemovedShelf: () => null }));
vi.mock('./overview/RemovedBar', () => ({ RemovedBar: () => null }));
vi.mock('./overview/JournalPane', () => ({ JournalPane: () => null }));
vi.mock('./overview/OverviewArrows', () => ({ OverviewArrows: () => null }));

const cell = (id: string) => ({ id, name: id, files: [] });
const stacks: Stack[] = [
    { id: 's1', name: 'One', cells: [cell('a0'), cell('a1')] },
    { id: 's2', name: 'Two', cells: [cell('b0'), cell('b1'), cell('b2')] },
];
const channel = { id: 'ch', name: 'ch', main_stacks: stacks, nav_stacks: [] } as unknown as Channel;

const frames: FrameRequestCallback[] = [];
const initialStore = useCollimateStore.getState();
const initialSpatial = useSpatialStore.getState();
beforeEach(() => {
    useCollimateStore.setState(initialStore, true);
    useSpatialStore.setState(initialSpatial, true);
    useCollimateStore.setState({ activeChannel: channel, selectedCellId: null, pendingSelectCellId: null, runs: {} } as never);
    useCollimateStore.setState(s => ({ overview: defaultOverviewView(), overviewData: { ...s.overviewData, lineage: { cells: [], moves: [] } } }));
    frames.length = 0;
    vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => { frames.push(fn); return frames.length; });
    vi.stubGlobal('cancelAnimationFrame', () => {});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const mount = () => {
    const applyCellOp = vi.fn(async () => null);
    const actions = { applyCellOp, winSetTopCell: vi.fn() } as unknown as SceneActions;
    const props: ComponentProps<typeof StackScene> = {
        stacks, selectedId: 's1', actions, transitionType: 'nav', topology: 'stable',
        isZoomedOut: true, fan: null, fanAnchor: null, onCollapseFan: vi.fn(), onSelectStack: vi.fn(),
    };
    const focus = { setFocusArea: vi.fn(), setSelectedWindowId: vi.fn(), setMainFocusMode: vi.fn() } as unknown as FocusActions;
    const view = render(
        <FocusActionsContext.Provider value={focus}>
            <FocusAreaContext.Provider value="main">
                <StackScene {...props} />
            </FocusAreaContext.Provider>
        </FocusActionsContext.Provider>,
    );
    // The camera at world (600, 0), zoom 1, the board at the client's
    // origin: a client point is its world point.
    act(() => {
        fixture.camera.cameraXMV.jump(600);
        fixture.camera.cameraYMV.jump(0);
        fixture.camera.zoomMV.jump(1);
    });
    const board = view.container.querySelector('[data-overview-board]') as HTMLElement;
    const flush = () => act(() => { frames.splice(0).forEach(fn => fn(0)); });
    const lift = (cellId: string) => {
        fireEvent.pointerDown(view.container.querySelector(`[data-ov-card="${cellId}"]`)!, { button: 0, clientX: 100, clientY: 100, pointerId: 1 });
    };
    const moveTo = (x: number, y: number) => {
        fireEvent.pointerMove(board, { clientX: x, clientY: y, pointerId: 1 });
        flush();
    };
    const letGo = (x: number, y: number) => act(async () => {
        fireEvent.pointerUp(board, { clientX: x, clientY: y, pointerId: 1 });
        await Promise.resolve();
    });
    const column = (stackId: string) => [...view.container.querySelector(`[data-stack-id="${stackId}"] [data-ov-column]`)!.children]
        .map(el => el.getAttribute('data-ov-card') ?? (el.hasAttribute('data-drop-slot') ? 'gap' : '?'));
    return { ...view, applyCellOp, lift, moveTo, letGo, column };
};

// Card i's midpoint at rest in a column.
const mid = (slot: number) => overviewCardTop(slot) + OVERVIEW_CARD_HEIGHT / 2;

describe('an overview drop through the scene', () => {
    it('the column opens its gap where the pointer points and the drop sends that stack position (polish)', async () => {
        const t = mount();
        t.lift('a0');
        // Over the second column, between b0 and b1.
        t.moveTo(426, mid(0) + 4);
        expect(t.column('s2')).toEqual(['b0', 'gap', 'b1', 'b2']);
        // Lower: below b2, the bottom of the stack (b2 stands a card lower
        // while the gap is above it).
        t.moveTo(426, mid(3) + 4);
        expect(t.column('s2')).toEqual(['b0', 'b1', 'b2', 'gap']);
        // Higher: above b0, its top.
        t.moveTo(426, overviewCardTop(0) - 6);
        expect(t.column('s2')).toEqual(['gap', 'b0', 'b1', 'b2']);
        await t.letGo(426, overviewCardTop(0) - 6);
        expect(t.applyCellOp).toHaveBeenCalledExactlyOnceWith({
            kind: 'intoStack', srcStackId: 's1', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: 0, cellId: 'a0',
        });
    });

    it('a card moved down its own column sends the reorder; over its own place the column shows its gap there and the drop sends nothing (polish)', async () => {
        const down = mount();
        down.lift('a0');
        // Its column shows a1 alone: below it.
        down.moveTo(136, mid(0) + 4);
        expect(down.column('s1')).toEqual(['a1', 'gap']);
        await down.letGo(136, mid(0) + 4);
        expect(down.applyCellOp).toHaveBeenCalledExactlyOnceWith({ kind: 'reorder', stackId: 's1', realIdx: 0, from: 0, to: 1, cellId: 'a0' });
        cleanup();

        const own = mount();
        own.lift('a0');
        own.moveTo(136, overviewCardTop(0) + 6);
        expect(own.column('s1')).toEqual(['gap', 'a1']);
        await own.letGo(136, overviewCardTop(0) + 6);
        expect(own.applyCellOp).not.toHaveBeenCalled();
    });
});

describe('an overview drop onto the remove zones', () => {
    it('a card let go over zone i of the Removed bar is removed with zone i\'s action, and the hold names the zone (TG-06)', async () => {
        const removeFromBoard = vi.fn(async () => true);
        const zones = [
            { id: 'z1', width: 0.5, action: { kind: 'add_tag' as const, tag: 'done' } },
            { id: 'z2', width: 0.5, action: { kind: 'remove_tag' as const, tag: 'wip' } },
        ];
        useCollimateStore.setState({ removeFromBoard } as never);
        useCollimateStore.getState().adoptRemoveZones(zones);
        const t = mount();
        const box = (left: number, top: number, right: number, bottom: number) => () => ({
            left, top, right, bottom, x: left, y: top, width: right - left, height: bottom - top, toJSON: () => ({}),
        }) as DOMRect;
        // The shelf open to 316 above the 30px bar, the board below; the
        // zone strip spans the bar.
        const root = t.container.querySelector<HTMLElement>('[data-overview-root]')!;
        const board = t.container.querySelector<HTMLElement>('[data-overview-board]')!;
        root.getBoundingClientRect = box(0, 0, 1200, 800);
        board.getBoundingClientRect = box(0, 346, 1200, 800);
        const strip = document.createElement('div');
        strip.setAttribute('data-rm-zone-strip', '');
        strip.getBoundingClientRect = box(0, 316, 1200, 346);
        root.append(strip);

        t.lift('a0');
        t.moveTo(300, 330);
        expect(useCollimateStore.getState().overview.drop).toMatchObject({ kind: 'remove', cellId: 'a0', over: 'bar', zone: 0 });
        t.moveTo(900, 330);
        expect(useCollimateStore.getState().overview.drop).toMatchObject({ over: 'bar', zone: 1 });
        await t.letGo(900, 330);
        expect(removeFromBoard).toHaveBeenCalledExactlyOnceWith('a0', { kind: 'remove_tag', tag: 'wip' });
        expect(t.applyCellOp).not.toHaveBeenCalled();
        expect(useCollimateStore.getState().overview.drop).toBeNull();
    });
});

describe('a right-click on the overview board', () => {
    it('the board keeps the browser menu off its background, but a field on it (the Removed shelf search) keeps its own', () => {
        const t = mount();
        const board = t.container.querySelector('[data-overview-board]') as HTMLElement;
        expect(fireEvent.contextMenu(board)).toBe(false);
        const field = document.createElement('input');
        board.appendChild(field);
        expect(fireEvent.contextMenu(field)).toBe(true);
    });
});
