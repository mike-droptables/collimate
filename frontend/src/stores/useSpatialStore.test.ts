import { describe, it, expect, beforeEach, vi } from "vitest";
import { useSpatialStore, withSettlingMove, SETTLE_MOVE_MS } from "./useSpatialStore";

/**
 * Zustand store tests run in jsdom so react imports resolve, but they
 * don't render any components — we drive the store directly.
 *
 * Each test resets the store by re-initialising the pieces it touches;
 * create() is a module-singleton so state leaks between tests otherwise.
 */

describe("useSpatialStore", () => {
  beforeEach(() => {
    useSpatialStore.setState({
      pendingMove: null,
      cameraHold: false,
      centerRequested: 0,
      minCellSize: 500,
      stackWidths: {},
      allStacksByChannel: {},
      maxColumns: {},
      isMaxWidth: false,
      fanCollapse: null,
      cellChrome: {},
    });
  });

  it("setPendingMove accepts a direct value", () => {
    useSpatialStore.getState().setPendingMove({
      type: "cell",
      sourceId: "c1",
      originalIndex: 0,
      targetIndex: 1,
      mode: "insert",
    });
    expect(useSpatialStore.getState().pendingMove?.sourceId).toBe("c1");
  });

  it("setPendingMove accepts an updater function", () => {
    useSpatialStore.getState().setPendingMove({
      type: "cell",
      sourceId: "s",
      originalIndex: 2,
      targetIndex: 5,
      mode: "stack",
    });
    useSpatialStore.getState().setPendingMove((prev) =>
      prev ? { ...prev, targetIndex: prev.targetIndex + 1 } : null,
    );
    expect(useSpatialStore.getState().pendingMove?.targetIndex).toBe(6);
  });

  it("setCameraHold toggles the flag", () => {
    useSpatialStore.getState().setCameraHold(true);
    expect(useSpatialStore.getState().cameraHold).toBe(true);
    useSpatialStore.getState().setCameraHold(false);
    expect(useSpatialStore.getState().cameraHold).toBe(false);
  });

  it("setMinCellSize supports functional updates", () => {
    useSpatialStore.getState().setMinCellSize(400);
    useSpatialStore.getState().setMinCellSize((x) => x + 50);
    expect(useSpatialStore.getState().minCellSize).toBe(450);
  });

  it("a column maximised on its own is flagged per stack, the flag dropped by any other write of its width and by clearing the overrides; never persisted (polish)", () => {
    const s = () => useSpatialStore.getState();
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    s().setColumnMax("a", true);
    s().setColumnMax("b", true);
    expect(s().maxColumns).toEqual({ a: true, b: true });
    expect(setItem).not.toHaveBeenCalled();
    s().setStackWidth("a", 700);
    expect(s().maxColumns).toEqual({ b: true });
    s().setColumnMax("b", false);
    expect(s().maxColumns).toEqual({});
    s().setColumnMax("c", true);
    s().clearStackWidths();
    expect(s().maxColumns).toEqual({});
    setItem.mockRestore();
  });

  it("centerRequested bumps via functional updater", () => {
    useSpatialStore.getState().setCenterRequested((n) => n + 1);
    useSpatialStore.getState().setCenterRequested((n) => n + 1);
    expect(useSpatialStore.getState().centerRequested).toBe(2);
  });

  it("setStackWidth merges a per-stack override without disturbing others", () => {
    useSpatialStore.getState().setStackWidth("a", 700);
    useSpatialStore.getState().setStackWidth("b", 400);
    expect(useSpatialStore.getState().stackWidths).toEqual({ a: 700, b: 400 });
  });

  it("persists each changed drag width immediately and retains the final value__F0625", () => {
    localStorage.removeItem('collimate.stackWidths');
    for (const width of [520, 540, 580]) {
      useSpatialStore.getState().setStackWidth('dragged', width);
      expect(JSON.parse(localStorage.getItem('collimate.stackWidths')!)).toEqual({ dragged: width });
    }
    useSpatialStore.getState().setStackWidth('dragged', 580);
    expect(JSON.parse(localStorage.getItem('collimate.stackWidths')!)).toEqual({ dragged: 580 });
  });

  it("setStackWidth keeps the same reference on an unchanged width (memo-stable)", () => {
    useSpatialStore.getState().setStackWidth("a", 700);
    const before = useSpatialStore.getState().stackWidths;
    useSpatialStore.getState().setStackWidth("a", 700);
    expect(useSpatialStore.getState().stackWidths).toBe(before);
  });

  it("clearStackWidths empties the override map", () => {
    useSpatialStore.getState().setStackWidth("a", 700);
    useSpatialStore.getState().clearStackWidths();
    expect(useSpatialStore.getState().stackWidths).toEqual({});
  });

  it("clearStackWidths keeps the same reference when already empty (memo-stable)", () => {
    const before = useSpatialStore.getState().stackWidths;
    useSpatialStore.getState().clearStackWidths();
    expect(useSpatialStore.getState().stackWidths).toBe(before);
  });

  it("setAllStacks keys the toggle per channel", () => {
    useSpatialStore.getState().setAllStacks("chan-1", false);
    expect(useSpatialStore.getState().allStacksByChannel).toEqual({ "chan-1": false });
    useSpatialStore.getState().setAllStacks("chan-2", true);
    expect(useSpatialStore.getState().allStacksByChannel).toEqual({
      "chan-1": false,
      "chan-2": true,
    });
  });

  it("setIsMaxWidth toggles the shared max-track flag", () => {
    useSpatialStore.getState().setIsMaxWidth(true);
    expect(useSpatialStore.getState().isMaxWidth).toBe(true);
    useSpatialStore.getState().setIsMaxWidth(false);
    expect(useSpatialStore.getState().isMaxWidth).toBe(false);
  });

  it("reportCellChrome writes a header bottom, and keeps the same map when it is unchanged", () => {
    useSpatialStore.getState().reportCellChrome("a", 38);
    const before = useSpatialStore.getState().cellChrome;
    expect(before).toEqual({ a: 38 });
    useSpatialStore.getState().reportCellChrome("a", 38);
    expect(useSpatialStore.getState().cellChrome).toBe(before);
    useSpatialStore.getState().reportCellChrome("a", 42);
    expect(useSpatialStore.getState().cellChrome).toEqual({ a: 42 });
  });

  it("pruneCellChrome keeps only the given cells, and the same map when nothing drops", () => {
    useSpatialStore.setState({ cellChrome: { a: 38, b: 38 } });
    const before = useSpatialStore.getState().cellChrome;
    useSpatialStore.getState().pruneCellChrome(new Set(["a", "b", "c"]));
    expect(useSpatialStore.getState().cellChrome).toBe(before);
    useSpatialStore.getState().pruneCellChrome(new Set(["b"]));
    expect(useSpatialStore.getState().cellChrome).toEqual({ b: 38 });
  });

  it("a width write persists the map with the open fan collapsed onto its front column", () => {
    useSpatialStore.getState().setFanCollapse({ stackId: "S", frontCellId: "c2" });
    useSpatialStore.setState({ stackWidths: { S: 500, "fan-c1": 500, "fan-c2": 500 } });
    useSpatialStore.getState().setStackWidth("fan-c2", 640);
    expect(useSpatialStore.getState().stackWidths["fan-c2"]).toBe(640);
    expect(JSON.parse(localStorage.getItem("collimate.stackWidths") ?? "{}")).toEqual({ S: 640 });
  });
});

describe('shared settling move lifecycle __F0577', () => {
  it('returns results, propagates failures, and an older timer keeps the newer bump __F0577', async () => {
    vi.useFakeTimers();
    const settled = vi.fn();
    try {
      expect(await withSettlingMove('first', async () => 42, settled)).toBe(42);
      vi.advanceTimersByTime(200);
      await expect(withSettlingMove('second', async () => { throw new Error('refused'); })).rejects.toThrow('refused');
      vi.advanceTimersByTime(SETTLE_MOVE_MS - 200);
      expect(settled).not.toHaveBeenCalled();
      expect(useSpatialStore.getState().settlingMoveSourceId).toBe('second');
      vi.advanceTimersByTime(200);
      expect(useSpatialStore.getState().settlingMoveSourceId).toBeNull();
      expect(settled).toHaveBeenCalledOnce();
    } finally {
      vi.runOnlyPendingTimers();
      vi.useRealTimers();
    }
  });
});
