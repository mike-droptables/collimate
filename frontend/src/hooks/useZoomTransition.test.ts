import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  useZoomTransitionStore,
  useIsZoomTransitioning,
} from "./useZoomTransition";

describe("useZoomTransitionStore", () => {
  beforeEach(() => {
    useZoomTransitionStore.setState({ isTransitioning: false });
  });

  it("setTransitioning updates the flag", () => {
    useZoomTransitionStore.getState().setTransitioning(true);
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(true);
    useZoomTransitionStore.getState().setTransitioning(false);
    expect(useZoomTransitionStore.getState().isTransitioning).toBe(false);
  });

  it("useIsZoomTransitioning subscribes to the flag", () => {
    const { result, rerender } = renderHook(() => useIsZoomTransitioning());
    expect(result.current).toBe(false);
    act(() => useZoomTransitionStore.getState().setTransitioning(true));
    rerender();
    expect(result.current).toBe(true);
  });
});
