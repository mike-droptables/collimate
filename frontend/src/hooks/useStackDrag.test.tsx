import { describe, it, expect, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { renderHook, act } from '@testing-library/react';
import { motionValue } from 'motion/react';
import { useStackDrag } from './useStackDrag';
import { FocusActionsContext, type FocusActions } from '@/contexts/FocusContext';
import { OVERVIEW_CARD_PITCH, overviewCardTop, type StackLayoutResult } from '@/hooks/useStackLayout';
import type { SceneActions } from '@/hooks/useSceneActions';
import type { PendingMoveState } from '@/stores/useSpatialStore';
import type { Channel, Stack } from '@/lib/types';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { OVERVIEW_CARD_HEIGHT, REMOVE_RUNNING, defaultOverviewView } from '@/stores/collimate/overviewSlice';
import { sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { MOVE_INTO_CAPABILITIES_RUNNING } from '@/lib/fanOut';

const stacks: Stack[] = [
    { id: 's1', name: '', cells: [{ id: 'c1', name: 'c1', files: [] }] },
    { id: 's2', name: '', cells: [{ id: 'c2', name: 'c2', files: [] }] },
];

const layout = {
    viewSize: { width: 1000, height: 800 },
    GAP: 14,
    FRONT_CARD_OFFSET: 0,
    positions: [250, 764],
    widths: [500, 500],
    naturalWidths: [500, 500],
    arrowWorldX: null,
    isCancelMove: false,
    cellPendingMoveWorldX: null,
    cardPixelHeight: 500,
    stackContainerHeight: 600,
    stackTopOffset: 0,
    cardFrame: { stackTopOffset: 0, stripHeight: 0, cardPixelHeight: 500 },
    overviewTarget: { x: 0, y: 0, zoom: 1 },
    screenMax: 1000,
    sceneSwapNonce: 0,
} as StackLayoutResult;

const focusActions = {
    setFocusArea: vi.fn(),
    setSelectedWindowId: vi.fn(),
    setMainFocusMode: vi.fn(),

} as unknown as FocusActions;

const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(FocusActionsContext.Provider, { value: focusActions }, children);

const render = (over: Partial<Parameters<typeof useStackDrag>[0]> = {}) => renderHook(() => useStackDrag({
    layout,
    cameraXMV: motionValue(0),
    cameraYMV: motionValue(0),
    zoomMV: motionValue(1),
    cameraTargetRef: { current: 0 },
    commitCameraTarget: () => {},
    rootDivRef: { current: null },
    stacks,
    fan: null,
    onSelectDisplay: () => {},
    selectedId: null,
    actions: {} as SceneActions,
    isZoomedOut: true,
    pendingMove: null,
    ...over,
}), { wrapper });

const press = (button: number) => ({
    button, clientX: 0, clientY: 0,
    currentTarget: document.createElement('div'),
    stopPropagation: () => {},
}) as unknown as React.PointerEvent;

const moveTo = (clientX: number) => ({ clientX, clientY: 0 }) as unknown as React.PointerEvent<HTMLDivElement>;

describe('F0651 scene column characterization before extraction', () => {
    const columns: Stack[] = [0, 1, 2, 3].map(i => ({
        id: `column-${i}`, name: '', cells: [{ id: `cell-${i}`, name: '', files: [] }],
    }));
    const columnLayout = { ...layout, positions: [0, 3000, 6000, 9000], widths: [500, 500, 500, 500], naturalWidths: [500, 500, 500, 500] };

    it('F0651 keeps both the tracked destination and the manually panned viewport awake', () => {
        const cameraXMV = motionValue(0);
        const { result, unmount } = render({ stacks: columns, layout: columnLayout, cameraXMV, selectedId: 'column-0', isZoomedOut: false });
        expect(result.current.items.map(item => item.isOffscreen)).toEqual([false, true, true, true]);
        act(() => { cameraXMV.set(6000); });
        expect(result.current.items.map(item => item.isOffscreen)).toEqual([false, true, false, true]);
        unmount();
        // The subscription must not update a departed scene.
        act(() => { cameraXMV.set(9000); });
    });

    it('F0651 widens the awake extent as the zoom-out camera reveals more world space', () => {
        const zoomMV = motionValue(0.25);
        const { result, unmount } = render({ stacks: columns, layout: columnLayout, zoomMV });
        expect(result.current.items.map(item => item.isOffscreen)).toEqual([false, false, true, true]);
        act(() => { zoomMV.set(0.125); });
        expect(result.current.items.map(item => item.isOffscreen)).toEqual([false, false, false, true]);
        unmount();
    });

    it('F0651 makes room for a keyboard insert using source natural width while retaining the real column widths', () => {
        const varied = { ...columnLayout, naturalWidths: [700, 500, 500, 500] };
        const { result, unmount } = render({ stacks: columns, layout: varied, pendingMove: {
            type: 'cell', sourceId: 'column-0', originalIndex: 1, targetIndex: 2, mode: 'insert',
        } });
        expect(result.current.items.map(item => item.x)).toEqual([0, 3714, 6714, 9714]);
        expect(result.current.items.map(item => item.computedWidth)).toEqual([500, 500, 500, 500]);
        expect(result.current.previewCellData?.cell?.id).toBe('cell-0');
        expect(result.current.previewCellData?.width).toBe(700);
        unmount();
    });
});

describe('useStackDrag — drag start button', () => {
    it('a primary-button press past the threshold starts a stack drag', () => {
        const { result } = render();
        act(() => result.current.handleStackDragStart('s1', press(0)));
        act(() => result.current.handleDragPointerMove(moveTo(50)));
        expect(result.current.dragGhost?.stackId).toBe('s1');
    });

    it('a right-button press never arms a stack drag (it is the canvas pan)', () => {
        const { result } = render();
        act(() => result.current.handleStackDragStart('s1', press(2)));
        act(() => result.current.handleDragPointerMove(moveTo(50)));
        expect(result.current.dragGhost).toBeNull();
    });

    it('a middle-button press never arms a cell drag', () => {
        const { result } = render();
        act(() => result.current.handleCellDragStart('s1', 0, 'c1', press(1)));
        act(() => result.current.handleDragPointerMove(moveTo(50)));
        expect(result.current.dragGhost).toBeNull();
    });
});

describe('useStackDrag — keyboard move preview', () => {
    // Two 2-cell stacks whose stored front indices outran their counts
    // (a removal on a rotated stack): every other site wraps them.
    const two: Stack[] = [
        { id: 's1', name: '', cells: [{ id: 'a0', name: 'a0', files: [] }, { id: 'a1', name: 'a1', files: [] }] },
        { id: 's2', name: '', cells: [{ id: 'b0', name: 'b0', files: [] }, { id: 'b1', name: 'b1', files: [] }] },
    ];
    const onto: PendingMoveState = {
        type: 'cell', sourceId: 's1', originalIndex: 1, targetIndex: 3, mode: 'stack',
    };

    it('wraps a stale front slot for the source cell', () => {
        const { result } = render({ stacks: two, pendingMove: onto, visibleTopIndices: { s1: 2, s2: 3 }, isZoomedOut: false });
        const preview = result.current.previewCellData;
        expect(preview?.cell?.id).toBe('a0');
        expect(preview?.x).toBe(layout.positions[1] - layout.widths[1] / 2);
    });
});

it('cursor motion does not replace structural drag state__F0602_F0663', () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => { frames.push(fn); return frames.length; });
    vi.stubGlobal('cancelAnimationFrame', () => {});
    const hook = render();
    act(() => hook.result.current.handleStackDragStart('s1', press(0)));
    act(() => hook.result.current.handleDragPointerMove(moveTo(300)));
    act(() => { frames.splice(0).forEach(fn => fn(0)); });
    const structure = hook.result.current.dragGhost;
    for (const x of [305, 310, 315]) {
        act(() => hook.result.current.handleDragPointerMove(moveTo(x)));
        act(() => { frames.splice(0).forEach(fn => fn(0)); });
        expect(hook.result.current.dragGhost).toBe(structure);
    }
    hook.unmount();
    vi.unstubAllGlobals();
});


describe('main-view cell onto landing __F0615', () => {
  const two:Stack[]=[{id:'s1',name:'',cells:[{id:'a0',name:'',files:[]},{id:'a1',name:'',files:[]}]},{id:'s2',name:'',cells:[{id:'b0',name:'',files:[]},{id:'b1',name:'',files:[]}]}];
  it.each([['s1',750],['s2',1264]] as const)('drops onto %s without pointer-depth reordering',async(target,x)=>{
    const frames:FrameRequestCallback[]=[];
    vi.stubGlobal('requestAnimationFrame',(fn:FrameRequestCallback)=>{frames.push(fn);return frames.length;});
    vi.stubGlobal('cancelAnimationFrame',()=>{});
    const apply=vi.fn(async()=>null);
    const hook=render({stacks:two,isZoomedOut:false,visibleTopIndices:{s1:0,s2:1},actions:{applyCellOp:apply} as unknown as SceneActions});
    try {
      act(()=>hook.result.current.handleCellDragStart('s1',0,'a0',press(0)));
      act(()=>hook.result.current.handleDragPointerMove({clientX:x,clientY:1500} as React.PointerEvent<HTMLDivElement>));
      act(()=>{frames.splice(0).forEach(fn=>fn(0));});
      await act(async()=>{hook.result.current.handleDragPointerUp({clientX:x,clientY:1500} as React.PointerEvent<HTMLDivElement>);await Promise.resolve();});
      if(target==='s1')expect(apply).not.toHaveBeenCalled();
      else expect(apply).toHaveBeenCalledWith({kind:'intoStack',srcStackId:'s1',srcCellIdx:0,targetRealIdx:1,tgtCellIdx:1,cellId:'a0'});
    }finally{hook.unmount();vi.unstubAllGlobals();}
  });
});

describe('main-view landings on and off Capabilities', () => {
  const caps:Stack={id:'caps',name:'Capabilities',stack_type:'capabilities',cells:[{id:'card',name:'Definitions',files:[],builtin:true},{id:'u',name:'u',files:[]}]};
  const board:Stack[]=[{id:'s1',name:'',cells:[{id:'a0',name:'',files:[]}]},caps];
  const drag=async(stackId:string,cellIdx:number,cellId:string,x:number,over:Partial<Parameters<typeof useStackDrag>[0]>={})=>{
    const frames:FrameRequestCallback[]=[];
    vi.stubGlobal('requestAnimationFrame',(fn:FrameRequestCallback)=>{frames.push(fn);return frames.length;});
    vi.stubGlobal('cancelAnimationFrame',()=>{});
    const apply=vi.fn(async()=>null);
    const hook=render({stacks:board,isZoomedOut:false,visibleTopIndices:{caps:0},actions:{applyCellOp:apply,winSetTopCell:vi.fn()} as unknown as SceneActions,...over});
    try {
      act(()=>hook.result.current.handleCellDragStart(stackId,cellIdx,cellId,press(0)));
      act(()=>hook.result.current.handleDragPointerMove({clientX:x,clientY:1500} as React.PointerEvent<HTMLDivElement>));
      act(()=>{frames.splice(0).forEach(fn=>fn(0));});
      const lifted=hook.result.current.dragGhost;
      const shownTarget=hook.result.current.dropTargetIdx;
      await act(async()=>{hook.result.current.handleDragPointerUp({clientX:x,clientY:1500} as React.PointerEvent<HTMLDivElement>);await Promise.resolve();});
      return {apply,lifted,shownTarget};
    }finally{hook.unmount();vi.unstubAllGlobals();}
  };

  it('a cell dropped on Capabilities while its manage card is in front lands right after the built-in cards',async()=>{
    const {apply,shownTarget}=await drag('s1',0,'a0',1264);
    expect(shownTarget).toBe(1);
    expect(apply).toHaveBeenCalledWith({kind:'intoStack',srcStackId:'s1',srcCellIdx:0,targetRealIdx:1,tgtCellIdx:1,cellId:'a0'});
  });

  it('a cell dropped on Capabilities while a built-in card is in front never lands before the cards',async()=>{
    const {apply}=await drag('s1',0,'a0',1264,{visibleTopIndices:{caps:1}});
    expect(apply).toHaveBeenCalledWith(expect.objectContaining({kind:'intoStack',targetRealIdx:1,tgtCellIdx:1}));
  });

  it('a running cell over Capabilities shows no landing, and letting go sends nothing and says why',async()=>{
    const openAlert=vi.fn().mockResolvedValue(undefined);
    useCollimateStore.setState({openAlert,runs:{r:{runId:'r',cellId:'a0',channel:'ch',runnerId:'x',runnerName:'X',generatorName:'g',label:'x',folder:'_runs/x'}},stoppingRunIds:new Set(),runPhases:{}} as never);
    try {
      const {apply,shownTarget}=await drag('s1',0,'a0',1264);
      expect(shownTarget).toBeNull();
      expect(apply).not.toHaveBeenCalled();
      expect(openAlert).toHaveBeenCalledExactlyOnceWith({title:'Move refused',body:MOVE_INTO_CAPABILITIES_RUNNING});
    } finally { useCollimateStore.setState({runs:{}} as never); }
  });

  it('the last capability cell drags off Capabilities like any cell (no last-cell guard)',async()=>{
    const {apply,lifted}=await drag('caps',1,'u',750,{visibleTopIndices:{caps:2}});
    expect(lifted?.cellId).toBe('u');
    expect(apply).toHaveBeenCalledWith({kind:'intoStack',srcStackId:'caps',srcCellIdx:1,targetRealIdx:0,tgtCellIdx:0,cellId:'u'});
  });
});

describe('an overview drop onto a column lands where the pointer points', () => {
    // Overview, zoom 1, camera at 0, view 1000 x 800, no scene offset:
    // world x = client x - 500, world y = client y. s2's cards stand at
    // overviewCardTop(0..2); `mid(i)` is card i's midpoint at rest.
    const mid = (slot: number) => overviewCardTop(slot) + OVERVIEW_CARD_HEIGHT / 2;
    const board: Stack[] = [
        { id: 's1', name: '', cells: [{ id: 'a0', name: '', files: [] }, { id: 'a1', name: '', files: [] }] },
        { id: 's2', name: '', cells: [
            { id: 'b0', name: '', files: [] }, { id: 'b1', name: '', files: [] }, { id: 'b2', name: '', files: [] },
        ] },
    ];
    const frames: FrameRequestCallback[] = [];
    const mount = () => {
        frames.length = 0;
        vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => { frames.push(fn); return frames.length; });
        vi.stubGlobal('cancelAnimationFrame', () => {});
        const apply = vi.fn(async () => null);
        const hook = render({ stacks: board, actions: { applyCellOp: apply, winSetTopCell: vi.fn() } as unknown as SceneActions });
        const shown = () => ({ drop: hook.result.current.dropTargetIdx, slot: hook.result.current.intraStackCellIdx });
        const move = (x: number, y: number) => {
            act(() => hook.result.current.handleDragPointerMove({ clientX: x, clientY: y } as React.PointerEvent<HTMLDivElement>));
            act(() => { frames.splice(0).forEach(fn => fn(0)); });
        };
        const letGo = (x: number, y: number) => act(async () => {
            hook.result.current.handleDragPointerUp({ clientX: x, clientY: y } as React.PointerEvent<HTMLDivElement>);
            await Promise.resolve();
        });
        return { hook, apply, shown, move, letGo, cleanup: () => { hook.unmount(); vi.unstubAllGlobals(); } };
    };

    it('another column takes the card above its first card, between two, or below its last, as pointed, and the move sends that position (polish)', async () => {
        for (const [y, index] of [[overviewCardTop(0) - 10, 0], [mid(0) + 4, 1], [mid(1) + 4, 2], [mid(2) + 4, 3]] as const) {
            const t = mount();
            try {
                act(() => t.hook.result.current.handleCellDragStart('s1', 0, 'a0', press(0)));
                t.move(1264, y);
                expect(t.shown()).toEqual({ drop: 1, slot: index });
                await t.letGo(1264, y);
                expect(t.apply).toHaveBeenCalledExactlyOnceWith({
                    kind: 'intoStack', srcStackId: 's1', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: index, cellId: 'a0',
                });
            } finally { t.cleanup(); }
        }
    });

    it('the gap follows the pointer with hysteresis, and the drop lands where the gap shows (polish)', async () => {
        const t = mount();
        try {
            act(() => t.hook.result.current.handleCellDragStart('s1', 0, 'a0', press(0)));
            t.move(1264, mid(1) + 4);
            expect(t.shown()).toEqual({ drop: 1, slot: 2 });
            // Back over card 1's upper half: the gap moves above it, and card 1
            // steps down a pitch.
            t.move(1264, mid(1) - 4);
            expect(t.shown()).toEqual({ drop: 1, slot: 1 });
            // Back where it was, the pointer is now in the gap: it stays.
            t.move(1264, mid(1) + 4);
            expect(t.shown()).toEqual({ drop: 1, slot: 1 });
            // Past card 1's shown midpoint the gap moves below it again.
            t.move(1264, mid(1) + OVERVIEW_CARD_PITCH + 4);
            expect(t.shown()).toEqual({ drop: 1, slot: 2 });
            // Card 1 is back above the gap: the pointer back at the first
            // height is under its midpoint, so the gap stays below it.
            t.move(1264, mid(1) + 4);
            expect(t.shown()).toEqual({ drop: 1, slot: 2 });
            await t.letGo(1264, mid(1) + 4);
            expect(t.apply).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: 'intoStack', tgtCellIdx: 2 }));
        } finally { t.cleanup(); }
    });

    it('a card moved within its own column lands at the pointed position; on its own place the column shows its gap there and the drop sends nothing (polish)', async () => {
        const below = mount();
        try {
            act(() => below.hook.result.current.handleCellDragStart('s2', 0, 'b0', press(0)));
            // The column shows b1 and b2 without the held card: below b2.
            below.move(1264, mid(1) + 4);
            expect(below.shown()).toEqual({ drop: 1, slot: 2 });
            await below.letGo(1264, mid(1) + 4);
            expect(below.apply).toHaveBeenCalledExactlyOnceWith({ kind: 'reorder', stackId: 's2', realIdx: 1, from: 0, to: 2, cellId: 'b0' });
        } finally { below.cleanup(); }
        const own = mount();
        try {
            act(() => own.hook.result.current.handleCellDragStart('s2', 0, 'b0', press(0)));
            // The column shows b1 and b2 without the held card: above b1,
            // b0's own place.
            own.move(1264, overviewCardTop(0) + 10);
            expect(own.shown()).toEqual({ drop: 1, slot: 0 });
            await own.letGo(1264, overviewCardTop(0) + 10);
            expect(own.apply).not.toHaveBeenCalled();
        } finally { own.cleanup(); }
    });
});

describe('carrying cards between the overview board and the Removed shelf', () => {
    const rect = (left: number, top: number, right: number, bottom: number) => ({
        left, top, right, bottom, x: left, y: top, width: right - left, height: bottom - top, toJSON: () => ({}),
    }) as DOMRect;
    // The overview 1000 x 800: the shelf open to 316, the bar under it, the board from 346.
    const overviewDom = () => {
        const root = document.createElement('div');
        root.setAttribute('data-overview-root', '');
        root.getBoundingClientRect = () => rect(0, 0, 1000, 800);
        const board = document.createElement('div');
        board.getBoundingClientRect = () => rect(0, 346, 1000, 800);
        board.setPointerCapture = vi.fn();
        board.releasePointerCapture = vi.fn();
        root.append(board);
        document.body.append(root);
        return { root, board };
    };
    const channel: Channel = {
        id: 'ch', name: 'chan', nav_stacks: [], nav_selection_id: null, main_selection_id: 's1', main_stacks: stacks,
    };
    const frames: FrameRequestCallback[] = [];
    const flush = () => act(() => { frames.splice(0).forEach(fn => fn(0)); });
    const removeFromBoard = vi.fn(async () => true);
    const at = (board: HTMLElement, clientX: number, clientY: number) =>
        ({ clientX, clientY, pointerId: 3, currentTarget: board }) as unknown as React.PointerEvent<HTMLDivElement>;

    const setup = () => {
        frames.length = 0;
        vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => { frames.push(fn); return frames.length; });
        vi.stubGlobal('cancelAnimationFrame', () => {});
        removeFromBoard.mockClear();
        useCollimateStore.setState({
            activeChannel: channel, runs: {}, overview: defaultOverviewView(), removeFromBoard,
        });
        const dom = overviewDom();
        const apply = vi.fn(async () => null);
        const hook = render({ rootDivRef: { current: dom.board }, actions: { applyCellOp: apply, winSetTopCell: vi.fn() } as unknown as SceneActions });
        const cleanup = () => { hook.unmount(); dom.root.remove(); vi.unstubAllGlobals(); };
        return { ...dom, hook, apply, cleanup };
    };
    const liftTo = (t: ReturnType<typeof setup>, x: number, y: number) => {
        act(() => t.hook.result.current.handleCellDragStart('s1', 0, 'c1', press(0)));
        act(() => t.hook.result.current.handleDragPointerMove(at(t.board, x, y)));
        flush();
    };

    it('holds the pointer once a card lifts, and a card carried over the open shelf is removed when let go (polish)', async () => {
        const t = setup();
        try {
            liftTo(t, 300, 200);
            expect(t.board.setPointerCapture).toHaveBeenCalledWith(3);
            expect(useCollimateStore.getState().overview.drop).toEqual({ kind: 'remove', cellId: 'c1', over: 'shelf', zone: null, refusal: null });
            expect(t.hook.result.current.dropTargetIdx).toBeNull();
            await act(async () => { t.hook.result.current.handleDragPointerUp(at(t.board, 300, 200)); await Promise.resolve(); });
            expect(removeFromBoard).toHaveBeenCalledWith('c1');
            expect(t.apply).not.toHaveBeenCalled();
            expect(t.board.releasePointerCapture).toHaveBeenCalledWith(3);
            expect(useCollimateStore.getState().overview.drop).toBeNull();
        } finally { t.cleanup(); }
    });

    it('the 30px band right above the board is the bar; back over the board the card is the board\'s again (polish)', async () => {
        const t = setup();
        try {
            liftTo(t, 300, 330);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ over: 'bar' });
            act(() => t.hook.result.current.handleDragPointerMove(at(t.board, 1264, 600)));
            flush();
            expect(useCollimateStore.getState().overview.drop).toEqual({ kind: 'remove', cellId: 'c1', over: null, zone: null, refusal: null });
            act(() => t.hook.result.current.handleDragPointerMove(at(t.board, 300, 340)));
            flush();
            await act(async () => { t.hook.result.current.handleDragPointerUp(at(t.board, 300, 340)); await Promise.resolve(); });
            expect(removeFromBoard).toHaveBeenCalledWith('c1');
        } finally { t.cleanup(); }
    });

    it('over the zone strip the hold names the zone under the pointer, and the drop removes with that zone\'s action (TG-06)', async () => {
        const t = setup();
        try {
            // The strip spans the bar's extent, here 100..900.
            const strip = document.createElement('div');
            strip.setAttribute('data-rm-zone-strip', '');
            strip.getBoundingClientRect = () => rect(100, 316, 900, 346);
            t.root.append(strip);
            const zones = [
                { id: 'z1', width: 0.25, action: { kind: 'add_tag' as const, tag: 'done' } },
                { id: 'z2', width: 0.5, action: { kind: 'remove' as const } },
                { id: 'z3', width: 0.25, action: { kind: 'remove_tag' as const, tag: 'wip' } },
            ];
            useCollimateStore.getState().adoptRemoveZones(zones);
            liftTo(t, 150, 330);
            expect(useCollimateStore.getState().overview.drop).toEqual({ kind: 'remove', cellId: 'c1', over: 'bar', zone: 0, refusal: null });
            act(() => t.hook.result.current.handleDragPointerMove(at(t.board, 500, 200)));
            flush();
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ over: 'shelf', zone: 1 });
            // Past the strip's right end: the last zone.
            act(() => t.hook.result.current.handleDragPointerMove(at(t.board, 990, 200)));
            flush();
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ over: 'shelf', zone: 2 });
            await act(async () => { t.hook.result.current.handleDragPointerUp(at(t.board, 990, 200)); await Promise.resolve(); });
            expect(removeFromBoard).toHaveBeenCalledExactlyOnceWith('c1', { kind: 'remove_tag', tag: 'wip' });
            // With no zones set the strip is one plain remove.
            useCollimateStore.getState().adoptRemoveZones([]);
            removeFromBoard.mockClear();
            liftTo(t, 150, 330);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ zone: 0 });
            await act(async () => { t.hook.result.current.handleDragPointerUp(at(t.board, 150, 330)); await Promise.resolve(); });
            expect(removeFromBoard).toHaveBeenCalledExactlyOnceWith('c1', { kind: 'remove' });
        } finally { t.cleanup(); }
    });

    it('a removable card held over the board keeps a remove hold over nothing, so the bar shows the zones before it gets there (TG-06)', async () => {
        const t = setup();
        try {
            liftTo(t, 1264, 600);
            expect(useCollimateStore.getState().overview.drop).toEqual({ kind: 'remove', cellId: 'c1', over: null, zone: null, refusal: null });
            await act(async () => { t.hook.result.current.handleDragPointerUp(at(t.board, 1264, 600)); await Promise.resolve(); });
            expect(removeFromBoard).not.toHaveBeenCalled();
            expect(useCollimateStore.getState().overview.drop).toBeNull();
            // A running cell over the board is not refused until it reaches the shelf or the bar.
            useCollimateStore.setState({ runs: { r: { runId: 'r', cellId: 'c1', channel: 'chan', runnerId: null, runnerName: null,
                generatorName: 'g', label: 'g-r', folder: '_runs/g-r' } } as never });
            liftTo(t, 1264, 600);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ over: null, refusal: null });
            act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
            expect(useCollimateStore.getState().overview.drop).toBeNull();
        } finally { t.cleanup(); }
    });

    it('refuses a running cell where it is let go, without a request (polish)', async () => {
        const t = setup();
        try {
            useCollimateStore.setState({ runs: { r: { runId: 'r', cellId: 'c1', channel: 'chan', runnerId: null, runnerName: null,
                generatorName: 'g', label: 'g-r', folder: '_runs/g-r' } } as never });
            liftTo(t, 300, 330);
            expect(useCollimateStore.getState().overview.drop).toEqual({ kind: 'remove', cellId: 'c1', over: 'bar', zone: null, refusal: REMOVE_RUNNING });
            await act(async () => { t.hook.result.current.handleDragPointerUp(at(t.board, 300, 330)); await Promise.resolve(); });
            expect(removeFromBoard).not.toHaveBeenCalled();
            expect(useCollimateStore.getState().overview.refusal).toMatchObject({ reason: REMOVE_RUNNING, at: 'bar' });
            expect(t.hook.result.current.dragGhost).toBeNull();
        } finally { t.cleanup(); }
    });

    it('Escape puts the card back: nothing moves and the release does nothing (polish)', async () => {
        const t = setup();
        try {
            liftTo(t, 300, 200);
            expect(t.hook.result.current.dragGhost).not.toBeNull();
            // The carried card owns the keyboard: the scene's Esc stands back.
            expect(sceneKeyboardOwned(new KeyboardEvent('keydown', { key: 'Escape' }))).toBe(true);
            act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
            expect(sceneKeyboardOwned(new KeyboardEvent('keydown', { key: 'Escape' }))).toBe(false);
            expect(t.hook.result.current.dragGhost).toBeNull();
            expect(useCollimateStore.getState().overview.drop).toBeNull();
            expect(t.board.releasePointerCapture).toHaveBeenCalledWith(3);
            await act(async () => { t.hook.result.current.handleDragPointerUp(at(t.board, 300, 200)); await Promise.resolve(); });
            expect(removeFromBoard).not.toHaveBeenCalled();
            expect(t.apply).not.toHaveBeenCalled();
        } finally { t.cleanup(); }
    });

    it('a shelf card held over the board gets the column slot under the pointer, and the board opens a gap there (polish)', () => {
        const t = setup();
        try {
            const restore = (point: { x: number; y: number } | null) => act(() => {
                const drop = useCollimateStore.getState().overview.drop;
                useCollimateStore.getState().setOverviewDrop({
                    kind: 'restore', cellId: 'd', point, target: drop?.kind === 'restore' ? drop.target : null,
                });
            });
            // World x = client x - 500 at zoom 1 with the camera at 0: 507 lies in the gap between the columns at 250 and 764.
            restore({ x: 1007, y: 500 });
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ target: { kind: 'column', slot: 1 } });
            expect(t.hook.result.current.items.map(item => item.x)).toEqual([250, 764 + 500 + 14]);
            restore({ x: 100, y: 500 });
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ target: { kind: 'column', slot: 0 } });
            expect(t.hook.result.current.items.map(item => item.x)).toEqual([250 + 514, 764 + 514]);
            // Off the board: no target, no gap.
            restore(null);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ target: null });
            expect(t.hook.result.current.items.map(item => item.x)).toEqual([250, 764]);
        } finally { t.cleanup(); }
    });
});

describe('a board card dropped back on its own place, or next to it', () => {
    // The overview unless a test mounts the main view; zoom 1, camera 0,
    // view 1000 wide: world x = client x - 500. Columns at world 250 and
    // 764 (500 wide, gap 14); s1 holds one card.
    const lone: Stack[] = [
        { id: 's1', name: '', cells: [{ id: 'c1', name: '', files: [] }] },
        { id: 's2', name: '', cells: [{ id: 'c2', name: '', files: [] }] },
    ];
    const multi: Stack[] = [
        { id: 's1', name: '', cells: [{ id: 'a0', name: '', files: [] }, { id: 'a1', name: '', files: [] }] },
        { id: 's2', name: '', cells: [{ id: 'c2', name: '', files: [] }] },
    ];
    const frames: FrameRequestCallback[] = [];
    const flush = () => act(() => { frames.splice(0).forEach(fn => fn(0)); });
    const at = (clientX: number) => ({ clientX, clientY: 400 }) as React.PointerEvent<HTMLDivElement>;
    // The real layout's row for `n` columns: 500 wide, 14 apart, the first at 250.
    const rowOf = (n: number) => {
        const positions = Array.from({ length: n }, (_, i) => 250 + i * 514);
        const widths = positions.map(() => 500);
        return { ...layout, positions, widths, naturalWidths: widths } as StackLayoutResult;
    };
    const mountBoard = (board: Stack[], apply = vi.fn(async (): Promise<Channel | null> => null), isZoomedOut = true) => {
        frames.length = 0;
        vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => { frames.push(fn); return frames.length; });
        vi.stubGlobal('cancelAnimationFrame', () => {});
        const actions = { applyCellOp: apply, winSetTopCell: vi.fn() } as unknown as SceneActions;
        const hook = renderHook(({ list }: { list: Stack[] }) => useStackDrag({
            layout: rowOf(list.length), cameraXMV: motionValue(0), cameraYMV: motionValue(0), zoomMV: motionValue(1), cameraTargetRef: { current: 0 },
            commitCameraTarget: () => {}, rootDivRef: { current: null }, stacks: list, fan: null,
            onSelectDisplay: () => {}, selectedId: null, actions, isZoomedOut, pendingMove: null,
        }), { wrapper, initialProps: { list: board } });
        return { hook, apply, cleanup: () => { hook.unmount(); vi.unstubAllGlobals(); } };
    };
    // `grab`: the card's rect a main-view grab handle passes, which makes
    // the drag a thumbnail one (each column's outer quarter is a gap).
    const hold = (t: ReturnType<typeof mountBoard>, stackId: string, cellIndex: number, cellId: string, x: number, grab?: DOMRect) => {
        act(() => t.hook.result.current.handleCellDragStart(stackId, cellIndex, cellId, press(0), grab));
        act(() => t.hook.result.current.handleDragPointerMove(at(x)));
        flush();
    };
    const letGo = (t: ReturnType<typeof mountBoard>, x: number) => act(async () => {
        t.hook.result.current.handleDragPointerUp(at(x));
        await Promise.resolve();
    });

    it('a card held over its own lone column, or either gap beside it, shows its own place in its column and sends no move (polish)', async () => {
        // Its own column, the gap right after it, the gap before the row:
        // each shows the column's own gap (no board preview, no column
        // moves).
        for (const x of [750, 1007, 200]) {
            const t = mountBoard(lone);
            try {
                hold(t, 's1', 0, 'c1', x);
                expect(t.hook.result.current.dropTargetIdx).toBe(0);
                expect(t.hook.result.current.intraStackCellIdx).toBe(0);
                expect(t.hook.result.current.previewCellData).toBeNull();
                expect(t.hook.result.current.items.map(item => item.x)).toEqual([250, 764]);
                await letGo(t, x);
                expect(t.apply).not.toHaveBeenCalled();
                expect(t.hook.result.current.dropTargetIdx).toBeNull();
                expect(t.hook.result.current.items.map(item => item.x)).toEqual([250, 764]);
                expect(t.hook.result.current.dragGhost).toBeNull();
            } finally { t.cleanup(); }
        }
    });

    it('main view: a lone card held by its grab handle over its own column, its outer quarters or either gap beside it shows the slot over its own column; nothing moves and the drop sends nothing (polish)', async () => {
        // World x = client x - 500; s1 spans world 0..500, its outer
        // quarters 0..125 and 375..500.
        const grab = { left: 0, top: 0, width: 100, height: 100 } as DOMRect;
        // Left quarter, centre, right quarter, the gap after it, the gap before the row.
        for (const x of [560, 750, 980, 1007, 200]) {
            const t = mountBoard(lone, undefined, false);
            try {
                hold(t, 's1', 0, 'c1', x, grab);
                expect(t.hook.result.current.dropTargetIdx).toBe(0);
                expect(t.hook.result.current.intraStackCellIdx).toBe(0);
                expect(t.hook.result.current.previewCellData).toMatchObject({ x: 0, width: 500, inFan: false });
                expect(t.hook.result.current.items.map(item => item.x)).toEqual([250, 764]);
                await letGo(t, x);
                expect(t.apply).not.toHaveBeenCalled();
                expect(t.hook.result.current.previewCellData).toBeNull();
                expect(t.hook.result.current.items.map(item => item.x)).toEqual([250, 764]);
                expect(t.hook.result.current.dragGhost).toBeNull();
            } finally { t.cleanup(); }
        }
        // A gap away from its own place is still a move: past the row's end.
        const far = mountBoard(lone, undefined, false);
        try {
            hold(far, 's1', 0, 'c1', 1600, grab);
            expect(far.hook.result.current.dropTargetIdx).toBeNull();
            expect(far.hook.result.current.previewCellData).not.toBeNull();
            await letGo(far, 1600);
            expect(far.apply).toHaveBeenCalledExactlyOnceWith({ kind: 'newStack', srcStackId: 's1', srcCellIdx: 0, insertRealIdx: 2, cellId: 'c1' });
        } finally { far.cleanup(); }
    });

    it('a lone card dropped on the slot after its neighbour moves by one, its gap opened right there (polish)', async () => {
        const t = mountBoard(lone);
        try {
            // World 1100: past the second column, the end of the row.
            hold(t, 's1', 0, 'c1', 1600);
            expect(t.hook.result.current.previewCellData).toMatchObject({ x: 764 + 250 + 14, width: 500 });
            await letGo(t, 1600);
            expect(t.apply).toHaveBeenCalledExactlyOnceWith({ kind: 'newStack', srcStackId: 's1', srcCellIdx: 0, insertRealIdx: 2, cellId: 'c1' });
        } finally { t.cleanup(); }
    });

    it('a card of a multi-cell column held over its own place shows it there and the drop moves nothing; the gap beside the column is a real move (polish)', async () => {
        const own = mountBoard(multi);
        try {
            // The column shows a0 without the held card: below it, a1's own place.
            hold(own, 's1', 1, 'a1', 750);
            expect(own.hook.result.current.dropTargetIdx).toBe(0);
            expect(own.hook.result.current.intraStackCellIdx).toBe(1);
            expect(own.hook.result.current.previewCellData).toBeNull();
            await letGo(own, 750);
            expect(own.apply).not.toHaveBeenCalled();
        } finally { own.cleanup(); }
        const beside = mountBoard(multi);
        try {
            hold(beside, 's1', 1, 'a1', 1007);
            expect(beside.hook.result.current.previewCellData).not.toBeNull();
            await letGo(beside, 1007);
            expect(beside.apply).toHaveBeenCalledExactlyOnceWith({ kind: 'newStack', srcStackId: 's1', srcCellIdx: 1, insertRealIdx: 1, cellId: 'a1' });
        } finally { beside.cleanup(); }
    });

    it('the gap goes in the frame the moved board arrives, not when the move\'s promise settles: no column is pushed one place too far (polish)', async () => {
        // A card of s1 carried out of its column to the gap between s1 and
        // s2, the way back to where it was before it was dropped into s1.
        let settle: (value: Channel | null) => void = () => {};
        const apply = vi.fn(() => new Promise<Channel | null>(resolve => { settle = resolve; }));
        const three: Stack[] = [...multi, { id: 's3', name: '', cells: [{ id: 'c3', name: '', files: [] }] }];
        const t = mountBoard(three, apply);
        try {
            hold(t, 's1', 1, 'a1', 1007);
            // The gap opens between s1 and s2: s2 and s3 make room.
            expect(t.hook.result.current.items.map(item => item.x)).toEqual([250, 764 + 514, 1278 + 514]);
            await letGo(t, 1007);
            expect(apply).toHaveBeenCalledOnce();
            // Until the result lands the gap stays open where the card goes.
            expect(t.hook.result.current.previewCellData).not.toBeNull();
            // The result lands (a1 in its own new column) before the promise settles.
            const landed: Stack[] = [
                { id: 's1', name: '', cells: [multi[0].cells[0]] },
                { id: 'n', name: '', cells: [multi[0].cells[1]] },
                multi[1], three[2],
            ];
            t.hook.rerender({ list: landed });
            expect(t.hook.result.current.previewCellData).toBeNull();
            // Every column at its own place: none pushed on by a second gap.
            expect(t.hook.result.current.items.map(item => item.x)).toEqual([250, 764, 1278, 1792]);
            await act(async () => { settle(null); await Promise.resolve(); });
            expect(t.hook.result.current.dragGhost).toBeNull();
        } finally { t.cleanup(); }
    });
});

describe('a Removed shelf card held over the board gets a board card\'s preview', () => {
    const rowOf = (n: number) => {
        const positions = Array.from({ length: n }, (_, i) => 250 + i * 514);
        const widths = positions.map(() => 500);
        return { ...layout, positions, widths, naturalWidths: widths } as StackLayoutResult;
    };
    const board = document.createElement('div');
    board.getBoundingClientRect = () => ({ left: 0, top: 0, right: 1000, bottom: 800 }) as DOMRect;
    const mount = () => {
        useCollimateStore.setState({ overview: defaultOverviewView() });
        return renderHook(({ list }: { list: Stack[] }) => useStackDrag({
            layout: rowOf(list.length), cameraXMV: motionValue(0), cameraYMV: motionValue(0), zoomMV: motionValue(1), cameraTargetRef: { current: 0 },
            commitCameraTarget: () => {}, rootDivRef: { current: board }, stacks: list, fan: null,
            onSelectDisplay: () => {}, selectedId: null, actions: {} as SceneActions, isZoomedOut: true, pendingMove: null,
        }), { wrapper, initialProps: { list: stacks } });
    };
    const hover = (x: number | null) => act(() => {
        const drop = useCollimateStore.getState().overview.drop;
        useCollimateStore.getState().setOverviewDrop({
            kind: 'restore', cellId: 'd', point: x === null ? null : { x, y: 500 }, target: drop?.kind === 'restore' ? drop.target : null,
        });
    });

    it('the marked gap opens at the landing column, the columns reflow round it, and it follows the pointer (polish)', () => {
        const hook = mount();
        try {
            // World 507: in the gap between the columns at 250 and 764.
            hover(1007);
            expect(hook.result.current.previewCellData).toEqual({
                x: 250 + 250 + 14, y: overviewCardTop(0), width: 500, height: OVERVIEW_CARD_HEIGHT, inFan: false,
            });
            expect(hook.result.current.items.map(item => item.x)).toEqual([250, 764 + 514]);
            // Left of every column: the gap opens at the start of the row.
            hover(100);
            expect(hook.result.current.previewCellData?.x).toBe(0);
            expect(hook.result.current.items.map(item => item.x)).toEqual([250 + 514, 764 + 514]);
            // Past the last column: at the end, nothing moves.
            hover(1600);
            expect(hook.result.current.previewCellData?.x).toBe(764 + 250 + 14);
            expect(hook.result.current.items.map(item => item.x)).toEqual([250, 764]);
            // Off the board: no gap.
            hover(null);
            expect(hook.result.current.previewCellData).toBeNull();
            expect(hook.result.current.items.map(item => item.x)).toEqual([250, 764]);
        } finally { hook.unmount(); }
    });

    it('over a column\'s cards it joins that stack at the pointed position, the column opening its own gap; between columns it still makes a new column (polish)', () => {
        const hook = mount();
        try {
            // World x 250: over the first column, under its one card (world y 500).
            hover(750);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ target: { kind: 'stack', stackId: 's1', index: 1 } });
            expect(hook.result.current.dropTargetIdx).toBe(0);
            expect(hook.result.current.intraStackCellIdx).toBe(1);
            // No new column: no board gap, no reflow.
            expect(hook.result.current.previewCellData).toBeNull();
            expect(hook.result.current.items.map(item => item.x)).toEqual([250, 764]);
            // Into the gap between the columns: a new column again.
            hover(1007);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ target: { kind: 'column', slot: 1 } });
            expect(hook.result.current.dropTargetIdx).toBeNull();
            expect(hook.result.current.items.map(item => item.x)).toEqual([250, 764 + 514]);
        } finally { hook.unmount(); }
    });

    it('above a column\'s first card it joins at the top; the Capabilities column takes no shelf card (polish)', () => {
        const hook = mount();
        try {
            const drop = useCollimateStore.getState().overview.drop;
            const at = (x: number, y: number) => act(() => {
                useCollimateStore.getState().setOverviewDrop({ kind: 'restore', cellId: 'd', point: { x, y }, target: drop?.kind === 'restore' ? drop.target : null });
            });
            at(750, overviewCardTop(0) + 10);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ target: { kind: 'stack', stackId: 's1', index: 0 } });
            hook.rerender({ list: [stacks[0], { ...stacks[1], stack_type: 'capabilities' }] });
            at(1264, 500);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ target: null });
            expect(hook.result.current.dropTargetIdx).toBeNull();
        } finally { hook.unmount(); }
    });

    it('the gap closes in the frame the restored column lands in it, though the drop has not cleared yet (polish)', () => {
        const hook = mount();
        try {
            hover(1007);
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ target: { kind: 'column', slot: 1 } });
            // The restore lands: the new column at slot 1. The store still
            // holds the drop (it clears when the restore call settles).
            const landed: Stack[] = [stacks[0], { id: 'r', name: '', cells: [{ id: 'd', name: '', files: [] }] }, stacks[1]];
            hook.rerender({ list: landed });
            // An unrelated store write meanwhile moves no slot.
            act(() => { useCollimateStore.getState().setOverviewDrop({ ...useCollimateStore.getState().overview.drop! }); });
            expect(useCollimateStore.getState().overview.drop).toMatchObject({ target: { kind: 'column', slot: 1 } });
            expect(hook.result.current.previewCellData).toBeNull();
            expect(hook.result.current.items.map(item => item.x)).toEqual([250, 764, 1278]);
        } finally { hook.unmount(); }
    });
});

it('tells the layout which gap the columns show, and that none shows once the drag ends (polish)', () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal('requestAnimationFrame', (fn: FrameRequestCallback) => { frames.push(fn); return frames.length; });
    vi.stubGlobal('cancelAnimationFrame', () => {});
    const multi: Stack[] = [
        { id: 's1', name: '', cells: [{ id: 'a0', name: '', files: [] }, { id: 'a1', name: '', files: [] }] },
        { id: 's2', name: '', cells: [{ id: 'c2', name: '', files: [] }] },
    ];
    const heldGapRef = { current: null as { slot: number; shift: number } | null };
    const hook = render({ stacks: multi, heldGapRef, actions: { applyCellOp: vi.fn(async () => null), winSetTopCell: vi.fn() } as unknown as SceneActions });
    try {
        act(() => hook.result.current.handleCellDragStart('s1', 1, 'a1', press(0)));
        // World 507: the gap between the columns.
        act(() => hook.result.current.handleDragPointerMove(moveTo(1007)));
        act(() => { frames.splice(0).forEach(fn => fn(0)); });
        expect(heldGapRef.current).toEqual({ slot: 1, shift: 514 });
        act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
        expect(heldGapRef.current).toBeNull();
    } finally { hook.unmount(); vi.unstubAllGlobals(); }
});
