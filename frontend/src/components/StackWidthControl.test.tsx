import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StackWidthControl } from "./StackWidthControl";
import { useSpatialStore } from "@/stores/useSpatialStore";
import * as stackWidth from "@/lib/stackWidth";
import { useCollimateStore } from "@/stores/useCollimateStore";
import { COMMAND_PANE_WIDTH, defaultCommandPane } from "@/stores/collimate/commandPaneSlice";
import { useChannelSettingsStore } from "@/stores/useChannelSettingsStore";
import { useKeybindingsStore } from "@/lib/keybindings/store";
import { putChannelSettings, type ChannelSettings } from "@/lib/api";
import { RETIRED_TOKENS } from '@/test/retiredTokens';
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// No network: the settings load the active channel triggers never settles
// (tests seed the store's doc themselves), and saves resolve at once.
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchChannelSettings: vi.fn(() => new Promise(() => {})),
  putChannelSettings: vi.fn(() => Promise.resolve({})),
}));

/**
 * StackWidthControl is a per-stack width handle whose popup (portaled to
 * document.body) routes adjustments by the channel's "all stacks" toggle:
 * ON sets the shared minCellSize + clears overrides; OFF writes only the
 * target stack. jsdom's innerWidth is 1024 so screenMax = 1024 - 2*STACK_GAP(20) = 984
 * (maxStackWidthFor — a max-width stack's edges land exactly on the
 * left/right-align positions, so no neighbor peeks through).
 */

const CHANNEL_ID = "11111111-1111-4111-8111-111111111111";
const STACK_ID = "stack-A";

const seedStores = () => {
  useSpatialStore.setState({
    minCellSize: 500,
    stackWidths: {},
    allStacksByChannel: {},
    maxColumns: {},
    isMaxWidth: false,
    widthDrag: null,
  });
  // Minimal active channel — the control only reads `activeChannel?.id`.
  useCollimateStore.setState({
    activeChannel: { id: CHANNEL_ID, name: "test", main_stacks: [], nav_stacks: [] },
    commandPane: defaultCommandPane(),
  } as never);
  // The settings stores a test seeds start empty in every test, whatever
  // order the tests run in.
  useChannelSettingsStore.setState({ doc: null, loadedFor: null });
  useKeybindingsStore.setState({ overrides: {} });
};

// The channel's settings loaded with this saved default width: the max
// toggle and "Set current" store into it.
const loadSettings = (defaultStackWidth: number | null) => {
  useChannelSettingsStore.setState({
    doc: { version: 1, kind: "channel-settings", layout: { defaultStackWidth } } as unknown as ChannelSettings,
    loadedFor: CHANNEL_ID,
  });
  vi.mocked(putChannelSettings).mockClear();
};
const channelDefault = () => useChannelSettingsStore.getState().doc?.layout?.defaultStackWidth;

const openPopup = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(screen.getByRole("button", { name: "Stack width" }));
};

describe("StackWidthControl", () => {
  beforeEach(seedStores);

  it("click opens a popup labelled 'Stack width' showing the resolved width (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    await openPopup(user);
    expect(screen.getByRole("slider")).toBeInTheDocument();
    expect(screen.getByText(/Stack width:/)).toHaveTextContent("500px");
  });

  it("the handle is a 24px tool, as the cell head's other tools (C11)", () => {
    render(<StackWidthControl stackId={STACK_ID} />);
    expect(screen.getByRole("button", { name: "Stack width" }).className.split(/\s+/))
      .toEqual(expect.arrayContaining(["h-6", "w-6"]));
  });

  it("a press inside the popup never reaches the card under it, so the stack is not selected (C17)", async () => {
    const user = userEvent.setup();
    const cardPointerDown = vi.fn();
    render(<div data-stack-card onPointerDown={cardPointerDown}><StackWidthControl stackId={STACK_ID} /></div>);
    await openPopup(user);
    cardPointerDown.mockClear();
    fireEvent.pointerDown(screen.getByRole("slider"));
    fireEvent.pointerDown(screen.getByRole("switch"));
    fireEvent.pointerDown(screen.getByText(/Stack width:/));
    expect(cardPointerDown).not.toHaveBeenCalled();
  });

  it("defaults the 'all stacks' toggle on when the channel has no entry (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    expect(screen.getByRole("switch")).toBeChecked();
  });

  it("all-stacks ON: Max sets the shared width and clears per-stack overrides (C17)", async () => {
    const user = userEvent.setup();
    useSpatialStore.getState().setStackWidth("other", 700); // a stray override
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.click(screen.getByTitle(/Max width/));
    expect(useSpatialStore.getState().minCellSize).toBe(984); // screenMax
    expect(useSpatialStore.getState().stackWidths).toEqual({});
  });

  it("Max is the max toggle Alt+F and the head's double-click share: it stores the width before as the channel default, reads pressed, and a second press returns to it (polish)", async () => {
    const user = userEvent.setup();
    loadSettings(640);
    const toggle = vi.spyOn(stackWidth, "toggleMaxWidth");
    useSpatialStore.setState({ minCellSize: 620 });
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    const max = () => screen.getByRole("button", { name: "Max" });
    expect(max()).toHaveAttribute("aria-pressed", "false");
    await user.click(max());
    expect(toggle).toHaveBeenLastCalledWith(STACK_ID, CHANNEL_ID);
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: 984, isMaxWidth: true });
    expect(channelDefault()).toBe(620);
    expect(max()).toHaveAttribute("aria-pressed", "true");
    expect(max().className.split(/\s+/)).toEqual(expect.arrayContaining(["aria-pressed:bg-sheet-2", "aria-pressed:text-ink"]));
    await user.click(max());
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: 620, isMaxWidth: false });
    expect(max()).toHaveAttribute("aria-pressed", "false");
    toggle.mockRestore();
  });

  it("maximising from 420 shows 420 as the default at once, and Reset after the maximise goes to 420 (polish)", async () => {
    const user = userEvent.setup();
    loadSettings(640);
    useSpatialStore.setState({ minCellSize: 420 });
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    expect(screen.getByTitle(/Reset to default/)).toHaveAttribute("title", expect.stringContaining("640px"));
    await user.click(screen.getByRole("button", { name: "Max" }));
    expect(screen.getByTitle(/Reset to default/)).toHaveAttribute("title", expect.stringContaining("420px"));
    expect(putChannelSettings).toHaveBeenCalledWith(CHANNEL_ID, { layout: { defaultStackWidth: 420 } });
    await user.click(screen.getByTitle(/Reset to default/));
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: 420, isMaxWidth: false });
  });

  it("Set current calls the store function the max toggle uses, and both write the same save (polish)", async () => {
    const user = userEvent.setup();
    loadSettings(null);
    const store = vi.spyOn(stackWidth, "setChannelDefaultWidth");
    useSpatialStore.setState({ minCellSize: 560 });
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.click(screen.getByRole("button", { name: "Set current" }));
    expect(store).toHaveBeenCalledExactlyOnceWith(560, CHANNEL_ID);
    store.mockRestore();
    // The toggle's own call is internal to lib/stackWidth (a spy cannot see
    // it): it writes exactly what Set current writes.
    useSpatialStore.setState({ minCellSize: 480 });
    await user.click(screen.getByRole("button", { name: "Max" }));
    expect(vi.mocked(putChannelSettings).mock.calls).toEqual([
      [CHANNEL_ID, { layout: { defaultStackWidth: 560 } }],
      [CHANNEL_ID, { layout: { defaultStackWidth: 480 } }],
    ]);
    expect(channelDefault()).toBe(480);
  });

  it("with the Commands pane open the full width is the canvas it leaves: Max, the slider's max and the clamp all take it (polish)", async () => {
    const user = userEvent.setup();
    useCollimateStore.setState({ commandPane: { ...defaultCommandPane(), open: true } } as never);
    const full = 984 - COMMAND_PANE_WIDTH; // 1024 - 352 - 2*STACK_GAP
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    expect(screen.getByRole("slider")).toHaveAttribute("max", String(full));
    await user.click(screen.getByRole("button", { name: "Max" }));
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: full, isMaxWidth: true });
    expect(screen.getByRole("button", { name: "Max" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Max" }));
    expect(useSpatialStore.getState()).toMatchObject({ minCellSize: 550, isMaxWidth: false });
  });

  it("all-stacks OFF: Max toggles this stack alone, its width before becomes the channel default, and it comes back to it (polish)", async () => {
    const user = userEvent.setup();
    loadSettings(640);
    useSpatialStore.getState().setAllStacks(CHANNEL_ID, false);
    useSpatialStore.setState({ stackWidths: { [STACK_ID]: 540 } });
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.click(screen.getByRole("button", { name: "Max" }));
    expect(useSpatialStore.getState().stackWidths[STACK_ID]).toBe(984);
    expect(useSpatialStore.getState().maxColumns).toEqual({ [STACK_ID]: true });
    expect(channelDefault()).toBe(540);
    expect(screen.getByRole("button", { name: "Max" })).toHaveAttribute("aria-pressed", "true");
    expect(useSpatialStore.getState().minCellSize).toBe(500);
    await user.click(screen.getByRole("button", { name: "Max" }));
    expect(useSpatialStore.getState().stackWidths[STACK_ID]).toBe(540);
  });

  it("all-stacks ON: +/- step the shared width by 50 (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.click(screen.getByTitle(/Increase width/));
    expect(useSpatialStore.getState().minCellSize).toBe(550);
    await user.click(screen.getByTitle(/Decrease width/));
    expect(useSpatialStore.getState().minCellSize).toBe(500);
  });

  it("all-stacks OFF: adjustments write only the target stack's override (C17)", async () => {
    const user = userEvent.setup();
    useSpatialStore.getState().setAllStacks(CHANNEL_ID, false);
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.click(screen.getByTitle(/Max width/));
    expect(useSpatialStore.getState().stackWidths[STACK_ID]).toBe(984);
    expect(useSpatialStore.getState().minCellSize).toBe(500); // untouched
  });

  it("toggling 'all stacks' flips scope without rewriting widths (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.click(screen.getByRole("switch"));
    expect(useSpatialStore.getState().allStacksByChannel[CHANNEL_ID]).toBe(false);
    // Flipping alone must not touch the widths.
    expect(useSpatialStore.getState().minCellSize).toBe(500);
    expect(useSpatialStore.getState().stackWidths).toEqual({});
  });

  it("all-stacks ON: drag resizes only this stack live, then commits to the shared width on release (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    const handle = screen.getByRole("button", { name: "Stack width" });
    await user.pointer({ keys: "[MouseLeft>]", target: handle, coords: { clientX: 100, clientY: 0 } });
    await user.pointer({ coords: { clientX: 160, clientY: 0 } }); // +60 → 560
    // Mid-drag: only this stack is overridden; the shared width and every
    // other stack are untouched (so left-neighbours don't shove it).
    expect(useSpatialStore.getState().stackWidths[STACK_ID]).toBe(560);
    expect(useSpatialStore.getState().minCellSize).toBe(500);
    await user.pointer({ keys: "[/MouseLeft]", coords: { clientX: 160, clientY: 0 } });
    // Release: committed to the shared width, temp override cleared.
    expect(useSpatialStore.getState().minCellSize).toBe(560);
    expect(useSpatialStore.getState().stackWidths).toEqual({});
  });

  it("all-stacks OFF: drag writes only the target stack's override, shared width untouched (C17)", async () => {
    const user = userEvent.setup();
    useSpatialStore.getState().setAllStacks(CHANNEL_ID, false);
    render(<StackWidthControl stackId={STACK_ID} />);
    const handle = screen.getByRole("button", { name: "Stack width" });
    await user.pointer({ keys: "[MouseLeft>]", target: handle, coords: { clientX: 100, clientY: 0 } });
    await user.pointer({ coords: { clientX: 50, clientY: 0 } }); // -50 → 450
    await user.pointer({ keys: "[/MouseLeft]", coords: { clientX: 50, clientY: 0 } });
    expect(useSpatialStore.getState().stackWidths[STACK_ID]).toBe(450);
    expect(useSpatialStore.getState().minCellSize).toBe(500);
  });

  it("drag marks this stack as the width drag (left edge anchored), cleared a frame after release (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} onDragStart={() => {}} />);
    const handle = screen.getByRole("button", { name: "Stack width" });
    await user.pointer({ keys: "[MouseLeft>]", target: handle, coords: { clientX: 100, clientY: 0 } });
    expect(useSpatialStore.getState().widthDrag).toBeNull(); // not yet past threshold
    await user.pointer({ coords: { clientX: 160, clientY: 0 } });
    // Mid-drag: the anchor is set so useStackLayout pins this stack's left
    // edge and useStackCamera freezes.
    expect(useSpatialStore.getState().widthDrag).toEqual({ id: STACK_ID, anchor: 'left' });
    // Hold the release frame explicitly: user.pointer can yield long enough
    // for a real animation frame to run before its promise resolves.
    const frames: FrameRequestCallback[] = [];
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => {
      frames.push(callback);
      return frames.length;
    });
    try {
      await user.pointer({ keys: "[/MouseLeft]", coords: { clientX: 160, clientY: 0 } });
      // The all-stacks commit reflows around the anchored edge before the
      // next frame clears the temporary anchor.
      expect(useSpatialStore.getState().widthDrag).toEqual({ id: STACK_ID, anchor: 'left' });
      expect(frames).not.toHaveLength(0);
      act(() => frames.forEach(callback => callback(performance.now())));
      expect(useSpatialStore.getState().widthDrag).toBeNull();
    } finally {
      raf.mockRestore();
    }
  });

  it("a tap (no movement past threshold) opens the popup instead of resizing (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    const handle = screen.getByRole("button", { name: "Stack width" });
    await user.pointer({ keys: "[MouseLeft>]", target: handle, coords: { clientX: 100, clientY: 0 } });
    await user.pointer({ keys: "[/MouseLeft]", coords: { clientX: 100, clientY: 0 } });
    expect(screen.getByRole("slider")).toBeInTheDocument();
    expect(useSpatialStore.getState().stackWidths).toEqual({});
    expect(useSpatialStore.getState().minCellSize).toBe(500);
  });

  it("a cancelled press (no movement past threshold) neither opens the popup nor resizes (C17)", () => {
    render(<StackWidthControl stackId={STACK_ID} />);
    const handle = screen.getByRole("button", { name: "Stack width" });
    // jsdom has no PointerEvent, so init fields are dropped — set them by hand.
    const pointer = (ev: Event, fields: Record<string, number>) => {
      for (const [k, v] of Object.entries(fields)) Object.defineProperty(ev, k, { value: v });
      fireEvent(handle, ev);
    };
    pointer(createEvent.pointerDown(handle), { button: 0, pointerId: 1, clientX: 100 });
    pointer(createEvent.pointerCancel(handle), { pointerId: 1, clientX: 100 });
    expect(screen.queryByText(/Stack width:/)).toBeNull();
    expect(useSpatialStore.getState().widthDrag).toBeNull();
    expect(useSpatialStore.getState().stackWidths).toEqual({});
  });

  it("Reset returns to the native default (clamped to screenMax) (C17)", async () => {
    const user = userEvent.setup();
    useSpatialStore.setState({ minCellSize: 800 });
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.click(screen.getByTitle(/Reset to default/));
    expect(useSpatialStore.getState().minCellSize).toBe(550);
  });

  it("Set current saves the width as the channel default and re-adopts no other section (F0903) (C17)", async () => {
    const user = userEvent.setup();
    useSpatialStore.setState({ minCellSize: 640 });
    const loaded = {
      version: 1,
      kind: "channel-settings",
      keyboard: { overrides: { LOADED: "Ctrl+K" } },
      theme: { activeThemeId: null, userThemes: [] },
      editing: { spellcheck: true },
        layout: { defaultStackWidth: null },
    } as unknown as ChannelSettings;
    useChannelSettingsStore.setState({ doc: loaded, loadedFor: CHANNEL_ID });
    // Edits made since the load: the doc snapshot does not carry them.
    const liveOverrides = { EDITED: "Ctrl+L" };
    useKeybindingsStore.setState({ overrides: liveOverrides });
    vi.mocked(putChannelSettings).mockClear();

    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.click(screen.getByRole("button", { name: "Set current" }));
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });

    expect(putChannelSettings).toHaveBeenCalledTimes(1);
    expect(putChannelSettings).toHaveBeenCalledWith(CHANNEL_ID, { layout: { defaultStackWidth: 640 } });
    const doc = useChannelSettingsStore.getState().doc!;
    expect(doc.layout?.defaultStackWidth).toBe(640);
    expect(doc.theme).toBe(loaded.theme);
    expect(doc.keyboard).toBe(loaded.keyboard);
    expect(doc.editing).toBe(loaded.editing);
    // Nothing fanned the stale snapshot back out over the live edits.
    expect(useKeybindingsStore.getState().overrides).toBe(liveOverrides);
    // Reset returns to the new default from now on.
    expect(screen.getByTitle(/Reset to default/)).toHaveAttribute("title", expect.stringContaining("640px"));
  });

  it("Set current keeps the layout section's saved Commands pane (K01)", async () => {
    const user = userEvent.setup();
    useSpatialStore.setState({ minCellSize: 640 });
    const commandPane = { open: true, side: "right" as const, query: "", recents: ["fan"] };
    useChannelSettingsStore.setState({
      doc: { version: 1, kind: "channel-settings", layout: { defaultStackWidth: null, commandPane } } as unknown as ChannelSettings,
      loadedFor: CHANNEL_ID,
    });
    vi.mocked(putChannelSettings).mockClear();
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.click(screen.getByRole("button", { name: "Set current" }));
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(putChannelSettings).toHaveBeenCalledWith(CHANNEL_ID, { layout: { defaultStackWidth: 640, commandPane } });
    expect(useChannelSettingsStore.getState().doc!.layout).toEqual({ defaultStackWidth: 640, commandPane });
  });
});

it('Escape belongs to the focused width popup and restores focus __F0548 (C17)', async () => {
  seedStores();
  const { sceneKeyboardOwned } = await import('@/lib/keyboardOwner');
  const sceneEscape = vi.fn();
  const capture = (event: KeyboardEvent) => {
    if (event.key === 'Escape' && !sceneKeyboardOwned(event)) sceneEscape();
  };
  window.addEventListener('keydown', capture, true);
  const input = document.createElement('input');
  document.body.append(input);
  input.focus();
  try {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await user.keyboard('{Escape}');
    expect(sceneEscape).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('slider')).not.toBeInTheDocument());
    expect(input).toHaveFocus();
  } finally {
    window.removeEventListener('keydown', capture, true);
    input.remove();
  }
});

describe("StackWidthControl — the popup on the shared Menu (C17)", () => {
  beforeEach(seedStores);

  it("the popup is the shared Menu as a dialog that owns the keyboard (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    const button = screen.getByRole("button", { name: "Stack width" });
    expect(button.getAttribute("aria-haspopup")).toBe("dialog");
    await openPopup(user);
    const popup = screen.getByRole("dialog", { name: "Stack width settings" });
    expect(popup.hasAttribute("data-owns-keyboard")).toBe(true);
    expect(popup.style.minWidth).toBe("256px");
    expect(button.getAttribute("aria-expanded")).toBe("true");
  });

  it("the steps move the button, and the popup stays open under the pointer (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    const button = screen.getByRole("button", { name: "Stack width" });
    let x = 500;
    button.getBoundingClientRect = () => ({ left: x, right: x + 22, top: 10, bottom: 32, width: 22, height: 22, x, y: 10, toJSON() {} }) as DOMRect;
    await openPopup(user);
    x = 525; // the stack grew: the button moved
    await user.click(screen.getByTitle(/Increase width/));
    await act(async () => { await new Promise((r) => requestAnimationFrame(() => r(null))); });
    await user.click(screen.getByTitle(/Increase width/));
    expect(useSpatialStore.getState().minCellSize).toBe(600);
    expect(screen.getByRole("dialog", { name: "Stack width settings" })).toBeInTheDocument();
  });

  it("a second click on the button closes the popup; an outside press closes it too (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    await openPopup(user);
    expect(screen.queryByRole("dialog")).toBeNull();
    await openPopup(user);
    await user.click(document.body);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("the keyboard opens it from the focused button (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    screen.getByRole("button", { name: "Stack width" }).focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("dialog", { name: "Stack width settings" })).toBeInTheDocument();
  });

  it("stack width never depends on a files pane's state (C17)", () => {
    const here = path.dirname(fileURLToPath(import.meta.url));
    for (const file of ["StackWidthControl.tsx", "../hooks/useStackLayout.ts"]) {
      const source = readFileSync(path.join(here, file), "utf8");
      expect(source).not.toMatch(/filesPane|FilesPane|cellLayouts|filesOpen|paneOpen/);
    }
  });
});

describe("StackWidthControl — skin tokens (S04)", () => {
  beforeEach(seedStores);

  it("the popup is a sheet dialog with quiet buttons and a teal slider (S04) (C17)", async () => {
    const user = userEvent.setup();
    render(<StackWidthControl stackId={STACK_ID} />);
    await openPopup(user);
    const popup = screen.getByRole("dialog", { name: "Stack width settings" });
    expect(popup.className.split(/\s+/)).toEqual(expect.arrayContaining(["bg-sheet", "border-line-2", "shadow-(--shadow)"]));
    expect(screen.getByRole("slider").className).toContain("accent-teal");
    expect(screen.getByRole("button", { name: "Max" }).className.split(/\s+/)).toContain("border-line-2");
    expect(popup.outerHTML).not.toMatch(RETIRED_TOKENS);
  });
});
