import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useZoomActions } from "./useZoomActions";
import { useCollimateStore } from "@/stores/useCollimateStore";
import { useZoomTransitionStore } from "./useZoomTransition";

describe("useZoomActions", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useZoomTransitionStore.setState({ isTransitioning: false });
    useCollimateStore.setState({ commandPane: { open: false, side: "left", query: "", recents: [], view: { kind: "commands" } } });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts zoomed-in (isZoomedOut=false)", () => {
    const { result } = renderHook(() => useZoomActions());
    expect(result.current.isZoomedOut).toBe(false);
  });

  it("toggleZoom zoom-in→zoom-out keeps the Commands pane open and does NOT set transitioning (K01)", () => {
    useCollimateStore.getState().openCommandPane();
    const { result } = renderHook(() => useZoomActions());
    act(() => result.current.toggleZoom());
    expect(result.current.isZoomedOut).toBe(true);
    expect(useCollimateStore.getState().commandPane.open).toBe(true);
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(false);
  });

  it("toggleZoom zoom-out→zoom-in raises the transitioning flag for ~350ms", () => {
    const { result } = renderHook(() => useZoomActions());
    act(() => result.current.toggleZoom()); // → zoomed out
    act(() => result.current.toggleZoom()); // zoom-in transition fires
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(true);

    act(() => {
      vi.advanceTimersByTime(349);
    });
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(true);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(false);
  });

  it("a quick in/out/in keeps transitioning until 350ms after the last zoom-in", () => {
    const { result } = renderHook(() => useZoomActions());
    act(() => result.current.setIsZoomedOut(true)); // start zoomed out
    act(() => result.current.toggleZoom()); // zoom in at t=0
    act(() => {
      vi.advanceTimersByTime(100);
    });
    act(() => result.current.toggleZoom()); // zoom out at t=100
    act(() => {
      vi.advanceTimersByTime(100);
    });
    act(() => result.current.toggleZoom()); // zoom in again at t=200
    act(() => {
      vi.advanceTimersByTime(150); // t=350: the first timer would have fired
    });
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(true);

    act(() => {
      vi.advanceTimersByTime(199); // t=549
    });
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(true);
    act(() => {
      vi.advanceTimersByTime(1); // t=550
    });
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(false);
  });

  it("setIsZoomedOut updates state directly", () => {
    const { result } = renderHook(() => useZoomActions());
    act(() => result.current.setIsZoomedOut(true));
    expect(result.current.isZoomedOut).toBe(true);
  });
});


it('defers every direct zoom-in edge and releases a superseded window __F0645', () => {
  vi.useFakeTimers();
  useZoomTransitionStore.setState({ isTransitioning: false });
  const { result, unmount } = renderHook(() => useZoomActions());
  try {
    act(() => result.current.setIsZoomedOut(true));
    act(() => result.current.setIsZoomedOut(false));
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(true);
    act(() => { vi.advanceTimersByTime(349); });
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(true);
    act(() => result.current.setIsZoomedOut(true));
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(false);
    act(() => result.current.setIsZoomedOut(false));
    act(() => { vi.advanceTimersByTime(350); });
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(false);
  } finally { unmount(); vi.useRealTimers(); }
});

describe("useZoomActions: the overview's data (O01)", () => {
  const load = vi.fn(async () => {});
  const drop = vi.fn();
  const channel = (id: string) => ({ id, name: "Shared label", main_stacks: [], nav_stacks: [] }) as never;
  beforeEach(() => {
    load.mockClear();
    drop.mockClear();
    useCollimateStore.setState({ loadOverview: load, dropOverviewData: drop, activeChannel: channel("11111111-1111-4111-8111-111111111111") } as never);
  });

  it("zooming out loads the shelf and the lineage for the channel; zooming in forgets them (O01)", () => {
    const { result } = renderHook(() => useZoomActions());
    expect(load).not.toHaveBeenCalled();
    act(() => result.current.toggleZoom());
    expect(load).toHaveBeenCalledExactlyOnceWith("11111111-1111-4111-8111-111111111111");
    expect(drop).not.toHaveBeenCalled();
    act(() => result.current.toggleZoom());
    expect(drop).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("a channel switch in the overview loads the new channel's (O01)", () => {
    const { result } = renderHook(() => useZoomActions());
    act(() => result.current.setIsZoomedOut(true));
    act(() => { useCollimateStore.setState({ activeChannel: channel("22222222-2222-4222-8222-222222222222") } as never); });
    expect(drop).toHaveBeenCalledTimes(1);
    expect(load.mock.calls.map(c => (c as unknown[])[0])).toEqual(["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]);
  });

  it("the main view loads nothing on a channel switch (O01)", () => {
    renderHook(() => useZoomActions());
    act(() => { useCollimateStore.setState({ activeChannel: channel("22222222-2222-4222-8222-222222222222") } as never); });
    expect(load).not.toHaveBeenCalled();
  });
});
