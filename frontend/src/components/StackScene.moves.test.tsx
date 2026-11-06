import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { ComponentProps, CSSProperties, ReactNode, Ref } from 'react';
import { StackScene } from './StackScene';
import type { StackCallbacks } from './StackGroup';
import type { SceneActions } from '@/hooks/useSceneActions';
import type { Stack } from '@/lib/types';
import type { FanRange } from '@/lib/fanOut';
import type { useStackDrag } from '@/hooks/useStackDrag';
import { ORIGIN_DX_VAR, ORIGIN_DY_VAR } from '@/lib/deviceOrigin';

// Keep the actual scene callback wiring and operations, replacing unrelated
// camera/drag/layout surfaces so a server response can be held deterministically.
const fixture = vi.hoisted(() => ({
    callbacks: new Map<string, StackCallbacks>(),
    mv: { on: vi.fn((_event: string, _cb: () => void) => () => {}), get: () => 0, set: () => {}, isAnimating: () => false },
    pool: {
        registerOverlay: vi.fn(), bringCellsToFront: vi.fn(), invalidate: vi.fn(),
        markMoving: vi.fn(), registerMotionLayer: vi.fn((_el: HTMLElement) => () => {}),
    },
    cameraHandlers: { handleCenterPanStart: vi.fn(), handleCenterPanMove: vi.fn(), handleCenterPanEnd: vi.fn() },
    dragHandlers: { handleStackDragStart: vi.fn(), handleCellDragStart: vi.fn() },
    dragExtra: {} as Partial<ReturnType<typeof useStackDrag>>,
    fanProps: null as null | Record<string, unknown>,
    reduceMotion: false,
    layoutParams: [] as Record<string, unknown>[],
    stackTopOffset: 0,
}));
vi.mock('./StackGroup', () => ({
    StackGroup: ({ stack, callbacks }: { stack: Stack; callbacks: StackCallbacks }) => {
        fixture.callbacks.set(stack.id, callbacks);
        return <span data-testid={`group-${stack.id}`} />;
    },
    NO_OP_STACK_CALLBACKS: {}, FLOATING_PREVIEW_STACK_ID: 'floating',
}));
// A motion.div's own animation notices ride the DOM's animation events, so a
// test can fire them (see `animation`).
vi.mock('motion/react', () => ({
    motion: { div: ({ children, style, className, ref, onAnimationStart, onAnimationComplete }: {
        children?: ReactNode; style?: CSSProperties & { y?: number }; className?: string; ref?: Ref<HTMLDivElement>;
        onAnimationStart?: () => void; onAnimationComplete?: () => void;
    }) => <div ref={ref} className={className} data-motion-y={style?.y as number | undefined} style={{
        position: style?.position, top: style?.top, width: style?.width,
        height: style?.height, pointerEvents: style?.pointerEvents, border: style?.border,
    }} onAnimationStart={() => onAnimationStart?.()} onAnimationEnd={() => onAnimationComplete?.()}>{children}</div> },
    AnimatePresence: ({ children }: { children?: ReactNode }) => children,
    useTransform: (_value: unknown, transform: (n: number) => number) => transform(0),
    useReducedMotion: () => fixture.reduceMotion,
}));
vi.mock('@/contexts/GuestPoolContext', () => ({ useGuestPool: () => fixture.pool }));
vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: () => {} }));
vi.mock('@/hooks/useEntryHold', () => ({ useEntryHold: () => new Set() }));
vi.mock('@/hooks/useStackLayout', () => ({
    useStackLayout: (params: Record<string, unknown>) => (fixture.layoutParams.push(params), {
        viewSize: { width: 1200, height: 800 }, GAP: 20,
        stackTopOffset: fixture.stackTopOffset, stackContainerHeight: 700, cardPixelHeight: 600,
        cardFrame: { stripHeight: 20 },
        overviewBounds: { left: 0, right: 1000, top: 0, bottom: 700 }, overviewTarget: { x: 0, y: 0, zoom: 1 },
    }),
    columnCardRect: () => ({}),
    overviewColumnHeight: () => 700,
    overviewFit: () => ({ x: 0, y: 0, zoom: 1 }),
}));
// The overview's overlays have their own suites.
vi.mock('./overview/RemovedShelf', () => ({ RemovedShelf: () => null }));
vi.mock('./overview/RemovedBar', () => ({ RemovedBar: () => null }));
vi.mock('./overview/JournalPane', () => ({ JournalPane: () => null }));
vi.mock('./overview/OverviewArrows', () => ({ OverviewArrows: () => null }));
vi.mock('@/hooks/useStackCamera', () => ({ useStackCamera: () => ({
    cameraXMV: fixture.mv, cameraYMV: fixture.mv, zoomMV: fixture.mv,
    ...fixture.cameraHandlers,
}) }));
vi.mock('@/hooks/useStackDrag', () => ({ useStackDrag: ({ stacks }: { stacks: Stack[] }) => ({
    items: stacks.map((s, i) => ({ ...s, x: i * 500, computedWidth: 500 })),
    ghostXMV: fixture.mv, ghostYMV: fixture.mv, dragGhost: null,
    ...fixture.dragHandlers,
    ...fixture.dragExtra,
}) }));
vi.mock('./CardChromeOverlays', () => ({ CardChromeOverlays: () => null }));
vi.mock('./FanFrame', () => ({ FanFrame: (props: Record<string, unknown>) => { fixture.fanProps = props; return null; } }));

beforeEach(() => {
    fixture.callbacks.clear(); fixture.dragExtra = {}; fixture.fanProps = null; fixture.reduceMotion = false;
    fixture.layoutParams = []; fixture.stackTopOffset = 0;
    vi.useFakeTimers();
});
afterEach(() => { cleanup(); vi.runOnlyPendingTimers(); vi.useRealTimers(); });

const columns: Stack[] = ['a', 'b'].map((id, i) => ({
    id: `fan-${id}`, name: id, synthetic: true, sourceStackId: 'source', fanIndex: i,
    cells: [{ id, name: id, files: [] }],
}));
// jsdom has no AnimationEvent, so React DOM listens for the prefixed names.
const animation = (el: HTMLElement, phase: 'Start' | 'End') =>
    fireEvent(el, new Event(`webkitAnimation${phase}`, { bubbles: true }));
const fan: FanRange = { stackId: 'source', realIdx: 0, start: 0, end: 1, count: 2, name: 'Source' };
const mount = (applyCellOp: ReturnType<typeof vi.fn>) => {
    const actions = {
        applyCellOp, winSetTopCell: vi.fn(), winRemoveCell: vi.fn(), duplicateCellInStack: vi.fn(),
        renameStack: vi.fn(), duplicateStack: vi.fn(), mergeStack: vi.fn(), winClose: vi.fn(),
    };
    const props: ComponentProps<typeof StackScene> = {
        stacks: columns, selectedId: 'fan-a', actions: actions as unknown as SceneActions,
        transitionType: 'nav', topology: 'stable', isZoomedOut: true,
        fan, fanAnchor: null, onCollapseFan: vi.fn(), onSelectStack: vi.fn(),
    };
    return { ...render(<StackScene {...props} />), props, actions };
};

it('the camera promotes its layers as a spring starts and on every change of its values, and carries no promotion at rest', () => {
    fixture.mv.on.mockClear();
    fixture.pool.registerMotionLayer.mockClear();
    mount(vi.fn());
    // x, y and zoom (one shared stand-in value here) each announce a
    // spring before its first frame and a direct write with its change.
    const promotions = fixture.mv.on.mock.calls
        .filter(([, cb]) => cb === fixture.pool.markMoving)
        .map(([event]) => event);
    expect(promotions).toEqual(['animationStart', 'change', 'animationStart', 'change', 'animationStart', 'change']);
    // The scale wrapper, the pan container and every stack's slot are
    // motion layers, and none names the promotion in its own class list.
    const layers = fixture.pool.registerMotionLayer.mock.calls.map(([el]) => el);
    const pan = document.getElementById('collimate-surface-overlay')!.parentElement!;
    const slots = columns.map(c => screen.getByTestId(`group-${c.id}`).parentElement!);
    expect(new Set(layers)).toEqual(new Set([pan, pan.parentElement, ...slots]));
    for (const el of layers) expect(el.className).not.toMatch(/will-change|transform-gpu/);
});

it("a stack's slot hangs below the camera's origin by a transform, not by its layout offset", () => {
    // A promoted slot's layout offset is rounded to the device pixel and a
    // transform's translation is not: at 1.25× a 14px top is 17.5 device
    // px, and it moved the cards' text half a pixel while the camera moved.
    fixture.stackTopOffset = 14;
    mount(vi.fn());
    for (const c of columns) {
        const slot = screen.getByTestId(`group-${c.id}`).parentElement!;
        expect(slot.style.top).toBe('0px');
        expect(slot.dataset.motionY).toBe('14');
    }
});

it("a stack's slot holds no promotion at rest, takes it for its own animation and drops it at the animation's end", () => {
    fixture.pool.registerMotionLayer.mockClear();
    const untrack = vi.fn();
    fixture.pool.registerMotionLayer.mockImplementation(() => untrack);
    const view = mount(vi.fn());
    const slot = screen.getByTestId('group-fan-a').parentElement!;
    expect(fixture.pool.registerMotionLayer).toHaveBeenCalledWith(slot);
    // Its own stacking context, so the scene's z lifts the whole stack,
    // and no layer: an ancestor of every card must rest unpromoted.
    expect(slot.className.split(/\s+/)).toContain('isolate');
    expect(slot.className).not.toMatch(/will-change|transform-gpu/);
    expect(slot.style.willChange).toBe('');
    animation(slot, 'Start');
    expect(slot.style.willChange).toBe('transform, opacity');
    animation(slot, 'End');
    expect(slot.style.willChange).toBe('');
    // Each registration is released with its slot.
    view.unmount();
    expect(untrack).toHaveBeenCalledTimes(fixture.pool.registerMotionLayer.mock.calls.length);
    fixture.pool.registerMotionLayer.mockImplementation(() => () => {});
});

it('F0587 retains callback identities when only the selected display column changes', () => {
    const { props, rerender } = mount(vi.fn());
    const first = fixture.callbacks.get('fan-a');
    const second = fixture.callbacks.get('fan-b');
    rerender(<StackScene {...props} selectedId="fan-b" />);
    expect(fixture.callbacks.get('fan-a')).toBe(first);
    expect(fixture.callbacks.get('fan-b')).toBe(second);
});

it('F0587 fan callbacks select a display column but remove and duplicate its real source cell', () => {
    const { props, actions } = mount(vi.fn());
    const callbacks = fixture.callbacks.get('fan-b')!;
    act(() => { callbacks.onSelect(0); callbacks.onRemoveCell(0); callbacks.onDuplicateToNewStack!(); callbacks.onClose(); });
    expect(props.onSelectStack).toHaveBeenCalledWith('fan-b');
    expect(actions.winRemoveCell).toHaveBeenCalledExactlyOnceWith('source', 1);
    expect(actions.duplicateCellInStack).toHaveBeenCalledExactlyOnceWith('source', 1);
    // Closing a synthetic column never dispatches a source-stack removal.
    expect(actions.applyCellOp).not.toHaveBeenCalled();
});

it('F0587 world drop overlays preserve target geometry without mounting a second cell renderer', () => {
    fixture.dragExtra.previewCellData = {
        cell: columns[0].cells[0], x: 140, y: 70, width: 500, height: 400, inFan: false,
    };
    const { container, props, rerender } = mount(vi.fn());
    const slot = Array.from(container.querySelectorAll('div')).find(el => el.style.top === '70px' && el.style.width === '500px' && el.style.height === '400px');
    expect(slot?.style.top).toBe('70px');
    expect(slot?.style.width).toBe('500px');
    expect(slot?.style.height).toBe('400px');
    expect(slot?.style.pointerEvents).toBe('none');
    expect([...fixture.callbacks.keys()]).toEqual(['fan-a', 'fan-b']);
    fixture.dragExtra.previewCellData = null;
    rerender(<StackScene {...props} />);
    expect(Array.from(container.querySelectorAll('div')).some(el => el.style.top === '70px' && el.style.width === '500px' && el.style.height === '400px')).toBe(false);
});

it("the fan's bracket stands in its columns' head band and its menu acts on the real stack (C08)", () => {
    const { props, actions, rerender } = mount(vi.fn());
    expect(fixture.fanProps).toBeNull(); // zoomed out: no bracket
    rerender(<StackScene {...props} isZoomedOut={false} />);
    const fanProps = fixture.fanProps as unknown as {
        top: number; height?: number; selected: boolean; cellIds: string[]; count: number;
        onRename: (n: string) => void; onDuplicateStack: () => void; onMergeStack: () => void; onRemove: () => void;
    };
    expect(fanProps.top).toBe(0);
    expect(fanProps.height).toBeUndefined();
    expect(fanProps.selected).toBe(true);
    expect(fanProps.cellIds).toEqual(['a', 'b']);
    expect(fanProps.count).toBe(2);
    act(() => { fanProps.onRename('Renamed'); fanProps.onDuplicateStack(); fanProps.onMergeStack(); fanProps.onRemove(); });
    expect(actions.renameStack).toHaveBeenCalledExactlyOnceWith('source', 'Renamed');
    expect(actions.duplicateStack).toHaveBeenCalledExactlyOnceWith('source');
    expect(actions.mergeStack).toHaveBeenCalledExactlyOnceWith('source');
    expect(actions.winClose).toHaveBeenCalledExactlyOnceWith('source');
    rerender(<StackScene {...props} isZoomedOut={false} selectedId="elsewhere" />);
    expect((fixture.fanProps as unknown as { selected: boolean }).selected).toBe(false);
});

it("the fan's bracket spans exactly its outer columns' card edges, like a stack head (C08)", () => {
    const { props, rerender } = mount(vi.fn());
    rerender(<StackScene {...props} isZoomedOut={false} />);
    // The columns sit at x 0 and 500, each 500 wide: cards from -250 to 750.
    const { left, width } = fixture.fanProps as unknown as { left: number; width: number };
    expect(left).toBe(-250);
    expect(left + width).toBe(750);
});

describe('the overview root and its board region (O01)', () => {
    // The board's edges carry the shift that puts its top-left on a whole
    // device pixel (lib/deviceOrigin).
    const SHIFT_X = `var(${ORIGIN_DX_VAR}, 0px)`;
    const SHIFT_Y = `var(${ORIGIN_DY_VAR}, 0px)`;
    const rightOf = (px: number) => `calc(${px}px - ${SHIFT_X})`;
    const initial = useCollimateStore.getState();
    afterEach(() => { useCollimateStore.setState(initial, true); });
    const overview = (patch: Partial<ReturnType<typeof useCollimateStore.getState>['overview']>) =>
        useCollimateStore.setState(s => ({ overview: { ...s.overview, ...patch } }));

    it('zoomed out, the board sits under the shelf and the bar and left of an open journal pane (O01)', () => {
        const { container, props, rerender } = mount(vi.fn());
        const root = container.querySelector('[data-overview-root]') as HTMLElement;
        const board = root.querySelector('[data-overview-board]') as HTMLElement;
        expect(board.style.top).toBe(`calc(var(--ov-shelf, 0px) + 33px + ${SHIFT_Y})`);
        expect(board.style.bottom).toBe(`calc(-1 * ${SHIFT_Y})`);
        expect(board.style.left).toBe(SHIFT_X);
        expect(board.style.right).toBe(rightOf(0));
        expect(root.querySelector('[data-overview-zoom]')).not.toBeNull();
        act(() => overview({ journalOpen: true }));
        expect(board.style.right).toBe(rightOf(374));
        expect((root.querySelector('[data-overview-zoom]') as HTMLElement).style.right).toBe(`${15 + 374}px`);
        rerender(<StackScene {...props} isZoomedOut={false} />);
        expect(container.querySelector('[data-overview-root]')).toBeNull();
        expect(container.querySelector('[data-overview-zoom]')).toBeNull();
    });

    const dock = (side: 'left' | 'right' | null) => act(() => {
        const store = useCollimateStore.getState();
        if (!side) { store.closeCommandPane(); return; }
        store.setCommandPaneSide(side);
        store.openCommandPane();
    });
    const boardOf = (container: HTMLElement) => container.querySelector('[data-overview-board]') as HTMLElement;
    const zoomOf = (container: HTMLElement) => container.querySelector('[data-overview-zoom]') as HTMLElement;

    it('zoomed out, the board starts past the Commands pane docked left, sliding over 160ms (polish)', () => {
        const { container } = mount(vi.fn());
        expect(boardOf(container).style.marginLeft).toBe('0px');
        dock('left');
        const board = boardOf(container);
        expect(board.style.marginLeft).toBe('352px');
        expect(board.style.marginRight).toBe('0px');
        expect(board.style.right).toBe(rightOf(0));
        expect(board.style.transition).toBe('margin-left 160ms ease, margin-right 160ms ease');
        act(() => overview({ journalOpen: true }));
        // The journal is on the other side: the board ends at it as before.
        expect(board.style.marginLeft).toBe('352px');
        expect(board.style.right).toBe(rightOf(374));
        expect(board.style.marginRight).toBe('0px');
        dock(null);
        expect(board.style.marginLeft).toBe('0px');
    });

    it('zoomed out, the board ends before the Commands pane docked right, an open journal docked against it (polish)', () => {
        const { container } = mount(vi.fn());
        dock('right');
        const board = boardOf(container);
        expect(board.style.marginLeft).toBe('0px');
        expect(board.style.right).toBe(rightOf(0));
        expect(board.style.marginRight).toBe('352px');
        act(() => overview({ journalOpen: true }));
        expect(board.style.right).toBe(rightOf(374));
        expect(board.style.marginRight).toBe('352px');
    });

    it('the zoom control moves in by the pane docked right, not by one docked left (polish)', () => {
        const { container } = mount(vi.fn());
        dock('left');
        expect(zoomOf(container).style.right).toBe('15px');
        expect(zoomOf(container).style.marginRight).toBe('0px');
        dock('right');
        expect(zoomOf(container).style.marginRight).toBe('352px');
        act(() => overview({ journalOpen: true }));
        expect(zoomOf(container).style.right).toBe(`${15 + 374}px`);
        expect(zoomOf(container).style.marginRight).toBe('352px');
    });

    it('with reduced motion the board jumps clear of the pane (polish)', () => {
        fixture.reduceMotion = true;
        const { container } = mount(vi.fn());
        dock('left');
        expect(boardOf(container).style.marginLeft).toBe('352px');
        expect(boardOf(container).style.transition).toBe('');
        dock('right');
        expect(boardOf(container).style.marginLeft).toBe('0px');
        expect(boardOf(container).style.marginRight).toBe('352px');
        expect(boardOf(container).style.transition).toBe('');
    });

    it('the board region paints no ground and no edge of its own: the canvas runs on under the pane (polish)', () => {
        const { container, props, rerender } = mount(vi.fn());
        const bare = (el: HTMLElement) => {
            expect(el.className).toBe('');
            for (const prop of ['background', 'backgroundColor', 'border', 'borderLeft', 'borderRight', 'boxShadow', 'outline'] as const) {
                expect(el.style[prop]).toBe('');
            }
        };
        for (const side of ['left', 'right'] as const) {
            dock(side);
            bare(boardOf(container));
            bare(container.querySelector('[data-overview-root]') as HTMLElement);
        }
        rerender(<StackScene {...props} isZoomedOut={false} />);
        bare(container.firstElementChild!.firstElementChild as HTMLElement);
    });

    it('the main view keeps the whole canvas as its board and sizes the full width to what the pane leaves free (polish)', () => {
        const { container, props, rerender } = mount(vi.fn());
        dock('right');
        // Zoomed out the board is inset already: the layout takes no inset.
        expect(fixture.layoutParams.at(-1)!.paneInset).toEqual({ left: 0, right: 0 });
        rerender(<StackScene {...props} isZoomedOut={false} />);
        const board = container.firstElementChild!.firstElementChild as HTMLElement;
        // Its top-left shifted onto the device-pixel grid
        // (lib/deviceOrigin), its far edges by as much: the same size.
        expect(board.style.top).toBe(SHIFT_Y);
        expect(board.style.left).toBe(SHIFT_X);
        expect(board.style.bottom).toBe(`calc(-1 * ${SHIFT_Y})`);
        expect(board.style.right).toBe(`calc(-1 * ${SHIFT_X})`);
        expect(board.style.marginLeft).toBe('');
        expect(board.style.marginRight).toBe('');
        expect(fixture.layoutParams.at(-1)!.paneInset).toEqual({ left: 0, right: 352 });
        dock(null);
        expect(fixture.layoutParams.at(-1)!.paneInset).toEqual({ left: 0, right: 0 });
    });

    it('Only connected folds away the columns with no connected cell, and they stop dragging (O01)', () => {
        useCollimateStore.setState({
            activeChannel: { name: 'ch', main_stacks: [{ id: 'source', name: 'Source', cells: columns.flatMap(c => c.cells) }], nav_stacks: [] },
        } as never);
        act(() => overview({ journalOpen: true, onlyConnected: true, anchor: 'a' }));
        mount(vi.fn());
        expect([...fixture.callbacks.keys()]).toEqual(['fan-a']);
        expect(fixture.callbacks.get('fan-a')!.onCellDragStart).toBeUndefined();
    });

    it('with nothing connected on the board it says so (O01)', () => {
        useCollimateStore.setState({
            activeChannel: { name: 'ch', main_stacks: [{ id: 'other', name: 'Other', cells: [{ id: 'z', name: 'Zed', files: [] }] }], nav_stacks: [] },
        } as never);
        act(() => overview({ journalOpen: true, onlyConnected: true, anchor: 'z' }));
        mount(vi.fn());
        expect(fixture.callbacks.size).toBe(0);
        expect(screen.getByText('No cell on the board is connected to Zed.')).toBeTruthy();
    });
});
