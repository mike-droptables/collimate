import * as layoutModule from "./useStackLayout";
import { describe, it, expect, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useStackLayout, maxStackWidthFor, MIN_STACK_WIDTH, slotGhostCenterX, slotGapCenterX, STACK_GAP, widthDragAnchorShift, cardSpan, columnCardRect, FAN_HEADER_HEIGHT, CANVAS_TOP_PADDING, STACK_HEAD_HEIGHT, OVERVIEW_CARD_PITCH, overviewCardTop, overviewColumnHeight, overviewFit } from "./useStackLayout";
import { OVERVIEW_CARD_HEIGHT, OVERVIEW_COLUMN_WIDTH, OVERVIEW_GAP, OVERVIEW_ZOOM } from "@/stores/collimate/overviewSlice";
import type { Stack } from "@/lib/types";
import type { PendingMoveState, WidthDrag } from "@/stores/useSpatialStore";
import { recordSelectionHandOff } from "@/lib/stackEntry";

/**
 * Layout-shift compensation around width changes. The hook measures its
 * scene container (rootRef), stubbed here at 1024px wide; the row
 * starts at startX = -1024/2 + GAP(20) = -492, so two 500px stacks sit at
 * centers -242 and 278.
 *
 * Two anchoring modes:
 *  - default: the selected stack's CENTER is pinned (resize-in-place,
 *    both edges move);
 *  - width drag (widthDrag set): the dragged stack's ANCHORED edge is
 *    pinned — the left one for the header handle and the right-side
 *    strip, the right one for the left-side strip — so the dragged edge
 *    tracks the pointer 1:1, through the drag and the handle's all-stacks
 *    release commit.
 */

const mkStack = (id: string): Stack => ({ id, name: id, cells: [] });

// Stand-in for the scene's root div (jsdom elements measure 0×0; the
// ResizeObserver stub in src/test/setup.ts is inert, so the hook's one
// synchronous measurement reads these).
const rootRef = {
  current: { clientWidth: 1024, clientHeight: 768 } as unknown as HTMLDivElement,
};

const baseProps = {
  rootRef,
  stacks: [mkStack("a"), mkStack("b")],
  selectedId: "a" as string | null,
  pendingMove: null,
  minCellSize: 500,
  stackWidths: {} as Record<string, number>,
  isZoomedOut: false,
  cameraTargetRef: { current: 0 },
  widthDrag: null as WidthDrag | null,
};
const LEFT_A: WidthDrag = { id: "a", anchor: "left" };

const leftEdge = (r: { positions: number[]; widths: number[] }, i: number) =>
  r.positions[i] - r.widths[i] / 2;

describe("useStackLayout width-change anchoring", () => {
  it("no drag: a width change anchors the selected stack's center (resize-in-place)", () => {
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps },
    });
    expect(result.current.positions[0]).toBe(-242);
    rerender({ ...baseProps, stackWidths: { a: 600 } });
    // Center pinned: the stack expands out of both sides.
    expect(result.current.positions[0]).toBe(-242);
    expect(leftEdge(result.current, 0)).toBe(-542);
  });

  it("width drag: pins the dragged stack's left edge through drag and all-stacks commit", () => {
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps },
    });
    expect(leftEdge(result.current, 0)).toBe(-492);
    // Mid-drag: only the dragged stack carries a temp override.
    rerender({ ...baseProps, stackWidths: { a: 600 }, widthDrag: LEFT_A });
    expect(result.current.widths[0]).toBe(600);
    expect(leftEdge(result.current, 0)).toBe(-492);
    // Release commit, anchor still set: the shared width absorbs the
    // override and every stack reflows around the same locked edge.
    rerender({ ...baseProps, minCellSize: 600, stackWidths: {}, widthDrag: LEFT_A });
    expect(result.current.widths[1]).toBe(600);
    expect(leftEdge(result.current, 0)).toBe(-492);
    // Anchor cleared a frame later: nothing moves.
    rerender({ ...baseProps, minCellSize: 600, stackWidths: {}, widthDrag: null });
    expect(leftEdge(result.current, 0)).toBe(-492);
  });

  it("right-edge anchor (left side dragged): the right edge and every stack right of it stay put", () => {
    const three = [mkStack("a"), mkStack("b"), mkStack("c")];
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, stacks: three, selectedId: "a" },
    });
    const rightEdge = (i: number) => result.current.positions[i] + result.current.widths[i] / 2;
    const rightB = rightEdge(1);
    const leftA = leftEdge(result.current, 0);
    const leftC = leftEdge(result.current, 2);
    const drag: WidthDrag = { id: "b", anchor: "right" };
    rerender({ ...baseProps, stacks: three, selectedId: "a", stackWidths: { b: 620 }, widthDrag: drag });
    expect(result.current.widths[1]).toBe(620);
    expect(rightEdge(1)).toBe(rightB);
    expect(leftEdge(result.current, 1)).toBe(rightB - 620);
    // The anchored side's neighbour is still; the other side slides by the growth.
    expect(leftEdge(result.current, 2)).toBe(leftC);
    expect(leftEdge(result.current, 0)).toBe(leftA - 120);
    expect(result.current.widths[0]).toBe(500);
    expect(result.current.widths[2]).toBe(500);
    // Shrinking back keeps the same right edge.
    rerender({ ...baseProps, stacks: three, selectedId: "a", stackWidths: { b: 400 }, widthDrag: drag });
    expect(rightEdge(1)).toBe(rightB);
    expect(leftEdge(result.current, 2)).toBe(leftC);
  });

  it("left-edge anchor on a middle stack: its left edge and every stack left of it stay put", () => {
    const three = [mkStack("a"), mkStack("b"), mkStack("c")];
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, stacks: three, selectedId: "c" },
    });
    const leftA = leftEdge(result.current, 0);
    const leftB = leftEdge(result.current, 1);
    const leftC = leftEdge(result.current, 2);
    rerender({ ...baseProps, stacks: three, selectedId: "c", stackWidths: { b: 620 }, widthDrag: { id: "b", anchor: "left" } });
    expect(leftEdge(result.current, 1)).toBe(leftB);
    expect(leftEdge(result.current, 0)).toBe(leftA);
    expect(leftEdge(result.current, 2)).toBe(leftC + 120);
  });

  it("width drag on a non-selected stack pins ITS left edge, not the selection's center", () => {
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, selectedId: "b" },
    });
    const initialLeftA = leftEdge(result.current, 0);
    rerender({ ...baseProps, selectedId: "b", stackWidths: { a: 700 }, widthDrag: LEFT_A });
    expect(result.current.widths[0]).toBe(700);
    expect(leftEdge(result.current, 0)).toBe(initialLeftA);
  });
});

describe("useStackLayout full width under the Commands pane", () => {
  it("screenMax shrinks by the pane's width while it is open, and a full-width stack with it (polish)", () => {
    const full: Parameters<typeof useStackLayout>[0] = { ...baseProps, minCellSize: maxStackWidthFor(1024) };
    const { result, rerender } = renderHook((p) => useStackLayout(p), { initialProps: full });
    expect(result.current.screenMax).toBe(1024 - 2 * STACK_GAP);
    expect(result.current.widths[0]).toBe(1024 - 2 * STACK_GAP);
    rerender({ ...full, paneInset: { left: 320, right: 0 } });
    expect(result.current.screenMax).toBe(1024 - 320 - 2 * STACK_GAP);
    expect(result.current.widths[0]).toBe(1024 - 320 - 2 * STACK_GAP);
    // The selected stack keeps its centre: the camera slides it clear.
    expect(result.current.positions[0]).toBe(-1024 / 2 + STACK_GAP + (1024 - 2 * STACK_GAP) / 2);
    rerender({ ...full, paneInset: { left: 0, right: 320 } });
    expect(result.current.screenMax).toBe(1024 - 320 - 2 * STACK_GAP);
    // Closed, the stack takes the whole width back.
    rerender({ ...full, paneInset: { left: 0, right: 0 } });
    expect(result.current.widths[0]).toBe(1024 - 2 * STACK_GAP);
  });
});

describe("widthDragAnchorShift", () => {
  it("left anchor: no shift for the column's own growth, the offset of a moved left edge otherwise", () => {
    // 500 wide at center 0 grows to 620 with its left edge in place (center 60).
    expect(widthDragAnchorShift(0, 500, 60, 620, "left")).toBe(0);
    // Something before it grew by 40: its raw left edge moved +40 — shift back.
    expect(widthDragAnchorShift(0, 500, 40, 500, "left")).toBe(-40);
  });

  it("right anchor: shifts the row back by the column's growth", () => {
    expect(widthDragAnchorShift(0, 500, 60, 620, "right")).toBe(-120);
    expect(widthDragAnchorShift(0, 500, -50, 400, "right")).toBe(100);
  });
});

/**
 * Fan toggle alias. Fanning S out replaces it in place with one column per
 * cell; with fanAnchor naming S and its front column, the column that
 * appears lands exactly where S was (and S, on collapse, where that column
 * was) — never a scene swap, never a nearest-survivor shuffle. 500px
 * stacks at 1024px: raw centers -242, 278, 798, 1318.
 */
describe("useStackLayout fan toggle alias", () => {
  const ids = (...xs: string[]) => xs.map(mkStack);
  const at = (r: { positions: number[] }, list: Stack[], id: string) =>
    r.positions[list.findIndex((s) => s.id === id)];
  const anchor = { realId: "S", columnId: "fan-s1" };

  it("fan-out puts the front column where the stack was", () => {
    const before = ids("A", "S", "B");
    const after = ids("A", "fan-s0", "fan-s1", "B");
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, stacks: before, selectedId: "S", fanAnchor: anchor },
    });
    const sAt = at(result.current, before, "S");
    rerender({ ...baseProps, stacks: after, selectedId: "fan-s1", fanAnchor: anchor });
    expect(at(result.current, after, "fan-s1")).toBe(sAt);
    expect(result.current.widths).toEqual([500, 500, 500, 500]);
  });

  it("collapse puts the stack where its front column was", () => {
    const fanned = ids("A", "fan-s0", "fan-s1", "B");
    const collapsed = ids("A", "S", "B");
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, stacks: fanned, selectedId: "fan-s1", fanAnchor: anchor },
    });
    const colAt = at(result.current, fanned, "fan-s1");
    rerender({ ...baseProps, stacks: collapsed, selectedId: "S", fanAnchor: anchor });
    expect(at(result.current, collapsed, "S")).toBe(colAt);
  });

  it("a single-stack channel toggles without a scene swap", () => {
    const single = ids("S");
    const fanned = ids("fan-s0", "fan-s1");
    const soloAnchor = { realId: "S", columnId: "fan-s0" };
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, stacks: single, selectedId: "S", fanAnchor: soloAnchor },
    });
    const nonce = result.current.sceneSwapNonce;
    const sAt = result.current.positions[0];
    rerender({ ...baseProps, stacks: fanned, selectedId: "fan-s0", fanAnchor: soloAnchor });
    expect(result.current.sceneSwapNonce).toBe(nonce);
    expect(result.current.positions[0]).toBe(sAt);
    rerender({ ...baseProps, stacks: single, selectedId: "S", fanAnchor: soloAnchor });
    expect(result.current.sceneSwapNonce).toBe(nonce);
    expect(result.current.positions[0]).toBe(sAt);
  });

  it("without an anchor the nearest surviving stack holds still", () => {
    const before = ids("A", "S", "B");
    const after = ids("A", "fan-s0", "fan-s1", "B");
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, stacks: before, selectedId: "S" },
    });
    expect(result.current.positions[0]).toBe(-242);
    rerender({ ...baseProps, stacks: after, selectedId: "fan-s1" });
    // A (nearest the camera at 0) is the anchor; nothing aliases.
    expect(result.current.positions[0]).toBe(-242);
    expect(result.current.positions[3]).toBe(1318);
  });

  it("an unrelated id change with an anchor set keeps the other stacks in place", () => {
    // C lands on the LEFT, so the raw positions of A and S move one pitch
    // right — only the nearest-survivor compensation keeps them put.
    const before = ids("A", "S");
    const after = ids("C", "A", "S");
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, stacks: before, selectedId: "S", fanAnchor: anchor },
    });
    const nonce = result.current.sceneSwapNonce;
    const prev = [...result.current.positions];
    expect(prev).toEqual([-242, 278]);
    rerender({ ...baseProps, stacks: after, selectedId: "S", fanAnchor: anchor });
    expect(result.current.positions.slice(1)).toEqual(prev);
    expect(result.current.positions[0]).toBe(-762);
    expect(result.current.widths).toEqual([500, 500, 500]);
    expect(result.current.sceneSwapNonce).toBe(nonce);
  });
});

/**
 * Adds keep the selection pinned (chooseLayoutAnchor, lib/stackEntry). At
 * 1024px the raw centers run -242, 278, 798, 1318, …; the camera target
 * sits nearer the selection's right neighbour than the selection itself,
 * so anchoring the survivor nearest the camera would shift the selection
 * a slot left and the camera would chase it.
 */
describe("useStackLayout insert anchoring", () => {
  const ids = (...xs: string[]) => xs.map(mkStack);
  const at = (r: { positions: number[] }, list: Stack[], id: string) =>
    r.positions[list.findIndex((s) => s.id === id)];

  it("an insert right of the selected stack leaves it where it was; the new one takes the next slot", () => {
    const before = ids("A", "S", "B");
    const after = ids("A", "S", "N", "B");
    const cameraTargetRef = { current: 760 };           // nearer B (792) than S (274)
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, cameraTargetRef, stacks: before, selectedId: "S" },
    });
    const sAt = at(result.current, before, "S");
    rerender({ ...baseProps, cameraTargetRef, stacks: after, selectedId: "S" });
    expect(at(result.current, after, "S")).toBe(sAt);
    expect(at(result.current, after, "N")).toBe(sAt + 520);
    expect(at(result.current, after, "B")).toBe(sAt + 2 * 520);
  });

  it("an add that selects the new stack in the same render anchors the previous selection", () => {
    const before = ids("A", "S", "B");
    const after = ids("A", "S", "N", "B");
    const cameraTargetRef = { current: 760 };
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, cameraTargetRef, stacks: before, selectedId: "S" },
    });
    const sAt = at(result.current, before, "S");
    rerender({ ...baseProps, cameraTargetRef, stacks: after, selectedId: "N" });
    expect(at(result.current, after, "S")).toBe(sAt);
  });

  it("removing the selected stack still slides its left neighbour into the vacated slot", () => {
    const before = ids("A", "S", "B");
    const after = ids("A", "B");
    const cameraTargetRef = { current: 274 };
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, cameraTargetRef, stacks: before, selectedId: "S" },
    });
    const bAt = at(result.current, before, "B");
    // The remove path records the hand-off and moves the selection to A
    // first (useSceneActions handleRemoveStack), then the stack goes.
    recordSelectionHandOff("S", "A");
    rerender({ ...baseProps, cameraTargetRef, stacks: before, selectedId: "A" });
    rerender({ ...baseProps, cameraTargetRef, stacks: after, selectedId: "A" });
    expect(at(result.current, after, "B")).toBe(bAt);
    expect(at(result.current, after, "A")).toBe(bAt - 520);
  });

  it("another client removing the stack beside an untouched selection holds the selection still (F0631)", () => {
    const before = ids("A", "S", "B");
    const after = ids("A", "B");
    const cameraTargetRef = { current: 274 };
    const { result, rerender } = renderHook((p) => useStackLayout(p), {
      initialProps: { ...baseProps, cameraTargetRef, stacks: before, selectedId: "A" },
    });
    const aAt = at(result.current, before, "A");
    rerender({ ...baseProps, cameraTargetRef, stacks: after, selectedId: "A" });
    expect(at(result.current, after, "A")).toBe(aAt);
    expect(at(result.current, after, "B")).toBe(aAt + 520);
  });
});

describe("cell insert ghost position (slotGhostCenterX)", () => {
  const GAP = STACK_GAP;

  it("sits one gap right of the nearest visible column, skipping a collapsed one", () => {
    // Columns 0 and 1 visible, column 2 a collapsed single-cell source
    // (width 0, parked at column 3's left edge), column 3 visible.
    const positions = [250, 764, 1028, 1278];
    const widths = [500, 500, 0, 500];
    // Slot 3 (after the collapsed column): right after column 1.
    expect(slotGhostCenterX(3, positions, widths, 500, GAP)).toBe(1014 + GAP + 250);
    // Slot 2 is the same place (the collapsed column adds no space).
    expect(slotGhostCenterX(2, positions, widths, 500, GAP)).toBe(1014 + GAP + 250);
    // Slot N walks back past nothing collapsed: after column 3.
    expect(slotGhostCenterX(4, positions, widths, 500, GAP)).toBe(1528 + GAP + 250);
  });

  it("starts at the row's left edge when nothing visible precedes the slot", () => {
    // Collapsed source first: parked at the row start (x=0).
    const positions = [0, 300];
    const widths = [0, 600];
    expect(slotGhostCenterX(0, positions, widths, 400, GAP)).toBe(200);
    expect(slotGhostCenterX(1, positions, widths, 400, GAP)).toBe(200);
    // Nothing collapsed: slot 0 starts at column 0's left edge, sized to the ghost.
    expect(slotGhostCenterX(0, [300, 914], [600, 600], 400, GAP)).toBe(200);
  });

  it("keyboard push-out right from the last fan column lands right after the previous column", () => {
    const one = (id: string): Stack => ({ ...mkStack(id), cells: [{ id: `${id}-c` } as Stack["cells"][number]] });
    const stacks = [one("A"), one("fan-s0"), one("fan-s1"), one("fan-s2"), one("B")];
    const props = { ...baseProps, stacks, selectedId: "fan-s2", pendingMove: null as PendingMoveState | null };
    const { result, rerender } = renderHook((p) => useStackLayout(p), { initialProps: props });
    rerender({
      ...props,
      pendingMove: {
        type: "cell", sourceId: "fan-s2", originalIndex: 3,
        targetIndex: 8, mode: "insert", inFan: false,
      },
    });
    const r = result.current;
    expect(r.widths[3]).toBe(0);
    const s1Right = r.positions[2] + r.widths[2] / 2;
    expect(r.cellPendingMoveWorldX).toBe(s1Right + GAP + 250);
  });
});

describe("insert gap centre (slotGapCenterX)", () => {
  const positions = [250, 764, 1278];
  const widths = [500, 500, 500];

  it("sits half a gap outside the outer columns and midway between neighbours", () => {
    expect(slotGapCenterX(0, positions, widths, 14)).toBe(-7);
    expect(slotGapCenterX(1, positions, widths, 14)).toBe(507);
    expect(slotGapCenterX(3, positions, widths, 14)).toBe(1535);
  });
});

describe("useStackLayout card height", () => {
  // The scene region is 768px tall and starts under the top bar (StackApp
  // lays the bar out in flow), so the layout reserves no bar height: only
  // the canvas's top padding and the head band, then the card runs to
  // the bottom padding.
  it("uses native canvas padding, stack gap, and head-band dimensions (C01, C06)", () => {
    expect(CANVAS_TOP_PADDING).toBe(15);
    expect(STACK_GAP).toBe(20);
    expect(STACK_HEAD_HEIGHT).toBe(33);
    expect(FAN_HEADER_HEIGHT).toBe(33);
    const { result } = renderHook(() => useStackLayout({ ...baseProps, isZoomedOut: false }));
    expect(result.current.stackTopOffset).toBe(15);
    expect(result.current.cardFrame).toEqual({ stackTopOffset: 15, stripHeight: 33, cardPixelHeight: 768 - 15 - 33 - 10 });
  });

  it("puts the front card's top at the canvas padding plus the head band, with no bar term (C01)", () => {
    const { result } = renderHook(() => useStackLayout({ ...baseProps, isZoomedOut: false }));
    expect(cardSpan(result.current.cardFrame).top).toBe(CANVAS_TOP_PADDING + STACK_HEAD_HEIGHT);
  });

  it("gives the cards everything below the top strip (compact main view) (C01)", () => {
    const { result } = renderHook(() => useStackLayout({ ...baseProps, isZoomedOut: false }));
    expect(result.current.cardPixelHeight).toBe(768 - CANVAS_TOP_PADDING - STACK_HEAD_HEIGHT - 10);
    expect(result.current.stackContainerHeight).toBe(result.current.cardPixelHeight + STACK_HEAD_HEIGHT + 120);
  });
});

describe("cardSpan / columnCardRect — where a front card sits", () => {
    const frame = { stackTopOffset: 36, stripHeight: 30, cardPixelHeight: 700 };

    it("a stack's card starts under the head band and runs the card height (C06)", () => {
        expect(cardSpan(frame)).toEqual({ top: 66, height: 700 });
        expect(columnCardRect({ x: 250, computedWidth: 500 }, frame))
            .toEqual({ left: 0, right: 500, top: 66, bottom: 766 });
    });

    it("a fan's bracket stands in its columns' head band: the bracket is the head's height (C08)", () => {
        expect(FAN_HEADER_HEIGHT).toBe(STACK_HEAD_HEIGHT);
    });
});

describe("useStackLayout view size", () => {
  it("a resize notification with an unchanged size keeps the same viewSize object", () => {
    let notify = () => {};
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: () => void) { notify = cb; }
      observe() {}
      disconnect() {}
    });
    try {
      const { result } = renderHook(() => useStackLayout(baseProps));
      const before = result.current.viewSize;
      expect(before).toEqual({ width: 1024, height: 768 });
      act(() => notify());
      expect(result.current.viewSize).toBe(before);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("the overview's columns keep their world place as its region narrows and widens (the Commands pane's slide); the main view's shift by half the change (polish)", () => {
    let notify = () => {};
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: () => void) { notify = cb; }
      observe() {}
      disconnect() {}
    });
    const el = { clientWidth: 1200, clientHeight: 700 };
    const ref = { current: el as unknown as HTMLDivElement };
    const resize = (width: number) => act(() => { el.clientWidth = width; notify(); });
    try {
      const over = renderHook(() => useStackLayout({ ...baseProps, rootRef: ref, isZoomedOut: true }));
      const before = over.result.current.positions;
      for (const width of [1168, 1040, 880, 1000, 1200]) {
        resize(width);
        expect(over.result.current.viewSize.width).toBe(width);
        expect(over.result.current.positions).toEqual(before);
      }
      over.unmount();
      el.clientWidth = 1200;
      const main = renderHook(() => useStackLayout({ ...baseProps, rootRef: ref }));
      const at = main.result.current.positions;
      resize(880);
      expect(main.result.current.positions).toEqual(at.map(x => x + 160));
    } finally {
      vi.unstubAllGlobals();
    }
  });
});


it('uses the shared screen maximum for measured scene widths__F0579', () => {
  expect(maxStackWidthFor(1024)).toBe(1024 - 2 * STACK_GAP);
  expect(maxStackWidthFor(0)).toBe(MIN_STACK_WIDTH);
});


it('keeps the head band in both views __F0567_F0648_F0649_F0658 (C06) (O01)', () => {
  const {result,rerender}=renderHook(p=>useStackLayout(p),{initialProps:{...baseProps}});
  expect(result.current.cardFrame.stripHeight).toBe(STACK_HEAD_HEIGHT);
  rerender({...baseProps,isZoomedOut:true});
  expect(result.current.cardFrame.stripHeight).toBe(STACK_HEAD_HEIGHT);
});

describe("useStackLayout in the overview", () => {
  const cellsOf = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `c${i}`, name: `c${i}`, files: [] }));
  const board = [
    { id: "a", name: "a", cells: cellsOf(1) },
    { id: "b", name: "b", cells: cellsOf(3) },
    { id: "c", name: "c", cells: cellsOf(2) },
  ] as Stack[];

  it("lays every stack out as a fixed native column and gap, whatever its main-view width (O01)", () => {
    expect(OVERVIEW_COLUMN_WIDTH).toBe(299);
    expect(OVERVIEW_GAP).toBe(20);
    const { result } = renderHook(() => useStackLayout({
      ...baseProps, stacks: board, isZoomedOut: true, stackWidths: { a: 900, b: 320 }, minCellSize: 700,
    }));
    expect(result.current.widths).toEqual([299, 299, 299]);
    const [a, b, c] = result.current.positions;
    expect(b - a).toBe(OVERVIEW_COLUMN_WIDTH + OVERVIEW_GAP);
    expect(c - b).toBe(OVERVIEW_COLUMN_WIDTH + OVERVIEW_GAP);
  });

  it("the main view keeps its own widths, clamped to the minimum (O01)", () => {
    const { result } = renderHook(() => useStackLayout({
      ...baseProps, stacks: board, isZoomedOut: false, stackWidths: { a: 200 }, minCellSize: 500,
    }));
    expect(result.current.widths).toEqual([MIN_STACK_WIDTH, 500, 500]);
  });

  it("cards use the native overview height and gap below the head (O01)", () => {
    expect(OVERVIEW_CARD_HEIGHT).toBe(273);
    expect(OVERVIEW_CARD_PITCH).toBe(OVERVIEW_CARD_HEIGHT + OVERVIEW_GAP);
    expect(overviewCardTop(0)).toBe(CANVAS_TOP_PADDING + STACK_HEAD_HEIGHT);
    expect(overviewCardTop(2)).toBe(CANVAS_TOP_PADDING + STACK_HEAD_HEIGHT + 2 * OVERVIEW_CARD_PITCH);
    expect(overviewColumnHeight(3)).toBe(STACK_HEAD_HEIGHT + 3 * OVERVIEW_CARD_HEIGHT + 2 * OVERVIEW_GAP);
    expect(overviewColumnHeight(0)).toBe(STACK_HEAD_HEIGHT);
    const { result } = renderHook(() => useStackLayout({ ...baseProps, stacks: board, isZoomedOut: true }));
    expect(result.current.cardPixelHeight).toBe(OVERVIEW_CARD_HEIGHT);
    expect(result.current.cardFrame).toEqual({ stackTopOffset: CANVAS_TOP_PADDING, stripHeight: STACK_HEAD_HEIGHT, cardPixelHeight: OVERVIEW_CARD_HEIGHT });
    // The tallest column, for the stack containers.
    expect(result.current.stackContainerHeight).toBe(overviewColumnHeight(3));
  });

  it("the board's bounds include its columns and native world padding (O01)", () => {
    const { result } = renderHook(() => useStackLayout({ ...baseProps, stacks: board, isZoomedOut: true }));
    const { positions, overviewBounds } = result.current;
    expect(overviewBounds.left).toBe(positions[0] - OVERVIEW_COLUMN_WIDTH / 2 - OVERVIEW_GAP);
    expect(overviewBounds.right).toBe(positions[2] + OVERVIEW_COLUMN_WIDTH / 2 + OVERVIEW_GAP);
    expect(overviewBounds.top).toBe(0);
    expect(overviewBounds.bottom).toBe(CANVAS_TOP_PADDING + overviewColumnHeight(3) + 53);
  });

  // StackScene draws world (x, y) at view/2 + zoom * (x - camX) across and
  // view/2 + zoom * (y - camY - view/2) down.
  const screen = (cam: { x: number; y: number; zoom: number }, view: { width: number; height: number }, x: number, y: number) => ({
    x: view.width / 2 + cam.zoom * (x - cam.x),
    y: view.height / 2 + cam.zoom * (y - cam.y - view.height / 2),
  });

  it("zoom-out lands the board fitted to the width, from the top, never above 100% (O01)", () => {
    const view = { width: 1000, height: 600 };
    const wide = { left: -1000, right: 1000, top: 0, bottom: 3000 };
    const fit = overviewFit(wide, view, true);
    expect(fit.zoom).toBeCloseTo(0.5);
    const tl = screen(fit, view, wide.left, wide.top);
    expect(tl.x).toBeCloseTo(0);
    expect(tl.y).toBeCloseTo(0);
    // A narrow board: 100%, centred across, still from the top.
    const narrow = { left: 0, right: 400, top: 0, bottom: 3000 };
    const fit2 = overviewFit(narrow, view, true);
    expect(fit2.zoom).toBe(1);
    expect(screen(fit2, view, 0, 0)).toEqual({ x: 300, y: 0 });
    const { result } = renderHook(() => useStackLayout({ ...baseProps, stacks: board, isZoomedOut: true }));
    expect(result.current.overviewTarget).toEqual(overviewFit(result.current.overviewBounds, result.current.viewSize, true));
  });

  it("Fit fits both ways and centres the board, within the zoom range (O01)", () => {
    const view = { width: 1000, height: 600 };
    const tall = { left: 0, right: 500, top: 0, bottom: 1200 };
    const fit = overviewFit(tall, view, false);
    expect(fit.zoom).toBeCloseTo(0.5);
    const tl = screen(fit, view, 0, 0);
    expect(tl.x).toBeCloseTo(375);
    expect(tl.y).toBeCloseTo(0);
    const huge = { left: 0, right: 100000, top: 0, bottom: 100 };
    expect(overviewFit(huge, view, false).zoom).toBe(OVERVIEW_ZOOM.min);
  });
});

it('consumes real removal handoff before a fanned heir display anchor __P0F23', () => {
  const fan=(id:string):Stack=>({...mkStack(id),synthetic:true,sourceStackId:'H'});
  const before=[mkStack('A'),mkStack('R'),fan('fan-h0'),fan('fan-h1'),mkStack('B')];
  const after=[mkStack('A'),fan('fan-h0'),fan('fan-h1'),mkStack('B')];
  const props={...baseProps,stacks:before,selectedId:'fan-h0',cameraTargetRef:{current:-242}};
  const {result,rerender}=renderHook(p=>useStackLayout(p),{initialProps:props});
  const anchor=result.current.positions[0];
  recordSelectionHandOff('R','H');
  rerender({...props,stacks:after});
  expect(result.current.positions[0]).toBe(anchor);
  expect(result.current.positions[1]).toBe(278);
});


it('reduces compensation from one immutable prior track without cumulative replay __F0647_F0627', () => {
  const reduce=(layoutModule as unknown as {nextStackLayoutTrack: (prior:unknown,input:unknown)=>{offset:number}}).nextStackLayoutTrack;
  expect(typeof reduce).toBe('function');
  const prior=Object.freeze({offset:0,sceneSwapNonce:0,ids:Object.freeze(['a']),selectedId:'a',positions:Object.freeze([10]),widths:Object.freeze([500]),viewWidth:1000});
  const input=Object.freeze({ids:Object.freeze(['a']),selectedId:'a',positions:Object.freeze([20]),widths:Object.freeze([500]),viewWidth:1000,cameraTarget:0,pendingMove:null,widthDrag:null,fanAnchor:null,inheritedFrom:null});
  const first=reduce(prior,input),again=reduce(prior,input);
  expect(first.offset).toBe(-10);
  expect(again).toEqual(first);
  expect(prior.offset).toBe(0);
});

it('a column dropped into an open gap lands there: the arrival anchors on where the columns stood, gap included (polish)', () => {
  const reduce=(layoutModule as unknown as {nextStackLayoutTrack: (prior:unknown,input:unknown)=>{offset:number}}).nextStackLayoutTrack;
  // Three 272px columns 18 apart, C selected. A gap opened at slot 0 pushed
  // all three right by 290; the drop's result puts a new column N there.
  const prior={offset:0,sceneSwapNonce:0,ids:['a','b','c'],selectedId:'c',positions:[136,426,716],widths:[272,272,272],viewWidth:1000};
  const input={ids:['n','a','b','c'],selectedId:'c',positions:[136,426,716,1006],widths:[272,272,272,272],viewWidth:1000,
    zoomedOut:true,cameraTarget:0,pendingMove:null,widthDrag:null,fanAnchor:null,inheritedFrom:null};
  // Every column stays where it stood on screen, N where the gap was.
  expect(reduce(prior,{...input,heldGap:{slot:0,shift:290}}).offset).toBe(0);
  // With no gap shown the selected column keeps its own place instead.
  expect(reduce(prior,{...input,heldGap:null}).offset).toBe(-290);
});
