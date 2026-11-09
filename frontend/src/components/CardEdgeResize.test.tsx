import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { motionValue } from "motion/react";
import { CardEdgeResize } from "./CardEdgeResize";
import { useSpatialStore } from "@/stores/useSpatialStore";
import { EDGE_GLOW_FADE_MS, EDGE_HOVER_ARM_MS, edgeResizeGeometry } from "@/lib/edgeResize";

// Two 500px cards (the second a column in the middle of the row).
const [a, b] = edgeResizeGeometry([
  { id: "a", left: 14, right: 514, cardTop: 77, cardBottom: 894, headerBottomWorldY: 115 },
  { id: "b", left: 528, right: 1028, cardTop: 77, cardBottom: 894, headerBottomWorldY: 115 },
], 14);

const seed = () => useSpatialStore.setState({
  minCellSize: 500,
  stackWidths: { c: 450 },
  allStacksByChannel: {},
  widthDrag: null,
});

const glowEl = () => document.querySelector<HTMLElement>("[data-edge-resize-glow]")!;

// The hover tests run the strip's arm timer on fake timers, so they dispatch
// pointer events directly: user-event's own waits ride the same clock.
const pointer = { button: 0, pointerId: 1, clientX: 1025, clientY: 400 };

describe("CardEdgeResize", () => {
  beforeEach(seed);
  afterEach(() => { vi.useRealTimers(); });

  it("right strip: pointerdown → move → up writes only this column's override, anchored left mid-drag (C17)", async () => {
    const user = userEvent.setup();
    const onDragStart = vi.fn();
    render(
      <CardEdgeResize columnId="b" geometry={b.right} width={500} screenMax={1500}
        zoom={motionValue(1)} onDragStart={onDragStart} />,
    );
    const hit = screen.getByRole("separator", { name: "Resize width (right edge)" });
    await user.pointer({ keys: "[MouseLeft>]", target: hit, coords: { clientX: 1025, clientY: 400 } });
    expect(onDragStart).toHaveBeenCalledOnce();
    expect(useSpatialStore.getState().widthDrag).toEqual({ id: "b", anchor: "left" });
    await user.pointer({ coords: { clientX: 1145, clientY: 400 } });
    expect(useSpatialStore.getState().stackWidths).toEqual({ c: 450, b: 620 });
    // The side lights at the press and stays lit for the whole drag.
    expect(glowEl().style.background).not.toBe("transparent");
    await user.pointer({ keys: "[/MouseLeft]", coords: { clientX: 1145, clientY: 400 } });
    expect(useSpatialStore.getState().widthDrag).toBeNull();
    // Never the shared width, never another column's.
    expect(useSpatialStore.getState().minCellSize).toBe(500);
    expect(useSpatialStore.getState().stackWidths).toEqual({ c: 450, b: 620 });
  });

  it("left strip anchors the right edge and grows with the pointer moving left", async () => {
    const user = userEvent.setup();
    render(<CardEdgeResize columnId="b" geometry={b.left} width={500} screenMax={1500} zoom={motionValue(1)} />);
    const hit = screen.getByRole("separator", { name: "Resize width (left edge)" });
    await user.pointer({ keys: "[MouseLeft>]", target: hit, coords: { clientX: 530, clientY: 400 } });
    expect(useSpatialStore.getState().widthDrag).toEqual({ id: "b", anchor: "right" });
    await user.pointer({ coords: { clientX: 410, clientY: 400 } });
    expect(useSpatialStore.getState().stackWidths.b).toBe(620);
    await user.pointer({ coords: { clientX: 730, clientY: 400 } });
    expect(useSpatialStore.getState().stackWidths.b).toBe(330); // clamped at the floor
    await user.pointer({ keys: "[/MouseLeft]", coords: { clientX: 730, clientY: 400 } });
    expect(useSpatialStore.getState().widthDrag).toBeNull();
  });

  it("writes a per-column override even when the channel's 'all stacks' toggle is on", async () => {
    useSpatialStore.setState({ allStacksByChannel: { ch: true } });
    const user = userEvent.setup();
    render(<CardEdgeResize columnId="fan-x" geometry={a.right} width={500} screenMax={1500} zoom={motionValue(1)} />);
    const hit = screen.getByRole("separator", { name: "Resize width (right edge)" });
    await user.pointer({ keys: "[MouseLeft>]", target: hit, coords: { clientX: 510, clientY: 400 } });
    await user.pointer({ coords: { clientX: 560, clientY: 400 } });
    await user.pointer({ keys: "[/MouseLeft]", coords: { clientX: 560, clientY: 400 } });
    expect(useSpatialStore.getState().stackWidths).toEqual({ c: 450, "fan-x": 550 });
    expect(useSpatialStore.getState().minCellSize).toBe(500);
  });

  it("a pointer resting on the side fades it in as a lighter line and leaving fades it out; a drag turns it teal; the hit rects cover only the rows below the header (S04) (C17)", () => {
    vi.useFakeTimers();
    render(<CardEdgeResize columnId="b" geometry={b.right} width={500} screenMax={1500} zoom={motionValue(1)} />);
    const hit = screen.getByRole("separator", { name: "Resize width (right edge)" });
    expect(hit.style.top).toBe("115px");
    expect(hit.style.cursor).toBe("ew-resize");
    const glow = glowEl();
    expect(glow.style.background).toBe("transparent");
    // A colour fade (transparent when out), never opacity: an opacity fade
    // runs on the compositor and turns the card's text grayscale.
    expect(glow.style.transition).toBe(`background-color ${EDGE_GLOW_FADE_MS}ms ease`);
    expect(glow.style.opacity).toBe("");
    fireEvent.pointerEnter(hit);
    // Arriving is not resting: nothing lights until the pointer has stayed.
    expect(glow.style.background).toBe("transparent");
    act(() => { vi.advanceTimersByTime(EDGE_HOVER_ARM_MS - 1); });
    expect(glow.style.background).toBe("transparent");
    act(() => { vi.advanceTimersByTime(1); });
    expect(glow.style.background).not.toBe("transparent");
    expect(glow.style.top).toBe("77px");
    expect(glow.style.left).toBe(`${1028 - 3}px`);
    expect(glow.style.background).toBe("var(--ink-2)");
    expect(glow.style.boxShadow).toBe("");
    expect(glow.outerHTML).not.toMatch(/--primary|#[0-9a-fA-F]{3,8}\b|color-mix|rgba?\(/);
    fireEvent.pointerDown(hit, pointer);
    expect(glow.style.background).toBe("var(--teal)");
    fireEvent.pointerUp(hit, pointer);
    expect(glow.style.background).toBe("var(--ink-2)");
    expect(glow.style.background).not.toBe("transparent");
    fireEvent.pointerLeave(hit);
    // The line fades out in place rather than vanishing.
    expect(glow.style.background).toBe("transparent");
    expect(glowEl()).toBe(glow);
  });

  it("a pointer crossing the side never lights it; a press lights it at once and keeps it lit while the pointer rests there", () => {
    vi.useFakeTimers();
    render(<CardEdgeResize columnId="b" geometry={b.right} width={500} screenMax={1500} zoom={motionValue(1)} />);
    const hit = screen.getByRole("separator", { name: "Resize width (right edge)" });
    fireEvent.pointerEnter(hit);
    act(() => { vi.advanceTimersByTime(EDGE_HOVER_ARM_MS / 2); });
    fireEvent.pointerLeave(hit);
    act(() => { vi.advanceTimersByTime(EDGE_HOVER_ARM_MS * 2); });
    expect(glowEl().style.background).toBe("transparent");
    fireEvent.pointerEnter(hit);
    fireEvent.pointerDown(hit, pointer);
    expect(glowEl().style.background).not.toBe("transparent");
    expect(glowEl().style.background).toBe("var(--teal)");
    fireEvent.pointerUp(hit, pointer);
    expect(glowEl().style.background).not.toBe("transparent");
    expect(glowEl().style.background).toBe("var(--ink-2)");
    act(() => { vi.advanceTimersByTime(EDGE_HOVER_ARM_MS * 2); });
    expect(glowEl().style.background).not.toBe("transparent");
    fireEvent.pointerLeave(hit);
    expect(glowEl().style.background).toBe("transparent");
  });

  it("unmounting mid-drag drops the width drag", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<CardEdgeResize columnId="b" geometry={b.right} width={500} screenMax={1500} zoom={motionValue(1)} />);
    const hit = screen.getByRole("separator", { name: "Resize width (right edge)" });
    await user.pointer({ keys: "[MouseLeft>]", target: hit, coords: { clientX: 1025, clientY: 400 } });
    expect(useSpatialStore.getState().widthDrag).not.toBeNull();
    unmount();
    expect(useSpatialStore.getState().widthDrag).toBeNull();
  });
});
