import { describe, it, expect, beforeEach, vi } from "vitest";
import { useCollimateStore } from "./useCollimateStore";
import { LOG_TAIL_LINES } from "./collimate/runSlice";
import { capabilitiesSourceFingerprint } from "./collimate/types";
import type { AppState, Cell, Stack, Channel, PickSession, RunRecord, VirtualFile } from "@/lib/types";

/**
 * Store-action tests for the main app store.
 *
 * Every test resets the fields it touches in beforeEach — Zustand
 * `create` is a module singleton and state leaks across tests otherwise.
 */

const resetFields = () => {
  useCollimateStore.setState({
    runs: {},
    stoppingRunIds: new Set<string>(),
    runPhases: {},
    runLogs: {},
    endedRuns: {},
    lastFinishedByCell: {},
    pendingReveals: {},
    cellCache: {},
    commandPane: { open: false, side: "left", query: "", recents: [], view: { kind: "commands" } },
    commandPanePanelSlot: null,
    runsMenuOpen: false,
    cellLayouts: {},
    tabDrag: null,
  });
};

const runRecord = (runId: string, cellId: string, label = `synthetic-${runId}`): RunRecord => ({
  runId, cellId, channel: "chan", runnerId: "synthetic", runnerName: "Synthetic",
  generatorName: "synthetic-gen", label, folder: `_runs/${label}`,
});

describe("useCollimateStore — run lifecycle", () => {
  beforeEach(() => {
    localStorage.clear();
    resetFields();
    useCollimateStore.setState({ finishedRuns: {} });
  });

  it("endRun with a status records the run among the cell's finished runs; without one, nothing", () => {
    const st = useCollimateStore.getState();
    st.startRun(runRecord("r1", "c1"));
    st.startRun(runRecord("r2", "c1"));
    st.endRun("r1", "failed", "boom");
    const [entry] = useCollimateStore.getState().finishedRuns.c1;
    expect(entry).toEqual({
      runId: "r1", cellId: "c1", channel: "chan", runnerId: "synthetic", runnerName: "Synthetic", generatorName: "synthetic-gen",
      label: "synthetic-r1", folder: "_runs/synthetic-r1", status: "failed", failureReason: "boom", endedAt: expect.any(Number),
    });
    st.endRun("r2");
    expect(useCollimateStore.getState().finishedRuns.c1).toHaveLength(1);
    st.endRun("never-started", "completed");
    expect(useCollimateStore.getState().finishedRuns.c1).toHaveLength(1);
  });

  it("startRun records the run, marks it active and clears the cell's last finished run", () => {
    useCollimateStore.setState({
      lastFinishedByCell: { c1: { runId: "old", label: "l", status: "failed", seen: false, at: 1 } },
    });
    useCollimateStore.getState().startRun(runRecord("r1", "c1"));
    const s = useCollimateStore.getState();
    expect(Object.hasOwn(s.runs, "r1")).toBe(true);
    expect(s.runs.r1.cellId).toBe("c1");
    expect(s.lastFinishedByCell.c1).toBeUndefined();
  });

  it("two runs on one cell are independent", () => {
    const { startRun, endRun } = useCollimateStore.getState();
    startRun(runRecord("r1", "c1"));
    startRun(runRecord("r2", "c1"));
    endRun("r1", "completed");
    const s = useCollimateStore.getState();
    expect(Object.hasOwn(s.runs, "r2")).toBe(true);
    expect(s.runs.r2).toBeDefined();
    expect(s.runs.r1).toBeUndefined();
  });

  it("endRun snapshots the run for the top status and the cell's light, then drops its live state", () => {
    useCollimateStore.setState({ cellCache: { c1: { id: "c1", name: "Cell One", files: [] } } });
    const st = useCollimateStore.getState();
    st.startRun(runRecord("r1", "c1"));
    st.startStopping("r1");
    st.setRunPhase("r1", "running");
    st.queueReveal("c1", { path: "_runs/synthetic-r1/a.chat", runId: "r1" });
    st.endRun("r1", "failed", "rc=1");
    const s = useCollimateStore.getState();
    expect(Object.hasOwn(s.runs, "r1")).toBe(false);
    expect(s.stoppingRunIds.has("r1")).toBe(false);
    expect(s.runs.r1).toBeUndefined();
    expect(s.runPhases.r1).toBeUndefined();
    expect(s.pendingReveals).toEqual({});
    expect(s.endedRuns.r1).toMatchObject({
      cellId: "c1", label: "synthetic-r1",
      status: "failed", failureReason: "rc=1",
    });
    expect(s.lastFinishedByCell.c1).toMatchObject({ runId: "r1", status: "failed", reason: "rc=1", seen: false });
    // Persisted so the red light survives a reload.
    expect(JSON.parse(localStorage.getItem("collimate.lastFinishedByCell.v1")!).c1.runId).toBe("r1");
  });

  it("a success or a user stop is born seen", () => {
    const st = useCollimateStore.getState();
    st.startRun(runRecord("r1", "c1"));
    st.endRun("r1", "completed");
    st.startRun(runRecord("r2", "c2"));
    st.endRun("r2", "gracefully_stopped");
    const s = useCollimateStore.getState();
    expect(s.lastFinishedByCell.c1.seen).toBe(true);
    expect(s.lastFinishedByCell.c2.seen).toBe(true);
  });

  it("an unseen failure outlives a sibling run's later success on the same cell (F0484)", () => {
    const st = useCollimateStore.getState();
    st.startRun(runRecord("r1", "c1"));
    st.startRun(runRecord("r2", "c1"));
    st.endRun("r1", "failed", "rc=1");
    st.endRun("r2", "completed");
    const s = useCollimateStore.getState();
    // The light stays red until the user has seen the failure.
    expect(s.lastFinishedByCell.c1).toMatchObject({ runId: "r1", status: "failed", reason: "rc=1", seen: false });
    expect(JSON.parse(localStorage.getItem("collimate.lastFinishedByCell.v1")!).c1.runId).toBe("r1");
  });

  it("a newer failure replaces an unseen one; a seen failure yields to the next outcome (F0484)", () => {
    const st = useCollimateStore.getState();
    st.startRun(runRecord("r1", "c1"));
    st.startRun(runRecord("r2", "c1"));
    st.startRun(runRecord("r3", "c1"));
    st.endRun("r1", "failed", "rc=1");
    st.endRun("r2", "timed_out");
    expect(useCollimateStore.getState().lastFinishedByCell.c1).toMatchObject({ runId: "r2", status: "timed_out", seen: false });
    st.markFailureSeen("c1");
    st.endRun("r3", "completed");
    expect(useCollimateStore.getState().lastFinishedByCell.c1).toMatchObject({ runId: "r3", status: "completed", seen: true });
  });

  it("markFailureSeen marks the cell's last failure seen", () => {
    const st = useCollimateStore.getState();
    st.startRun(runRecord("r1", "c1"));
    st.endRun("r1", "timed_out");
    st.markFailureSeen("c1");
    expect(useCollimateStore.getState().lastFinishedByCell.c1.seen).toBe(true);
    st.markFailureSeen("unknown");
  });

  it("endRun without a status keeps no snapshot", () => {
    const st = useCollimateStore.getState();
    st.startRun(runRecord("r1", "c1"));
    st.endRun("r1");
    const s = useCollimateStore.getState();
    expect(Object.hasOwn(s.runs, "r1")).toBe(false);
    expect(s.endedRuns).toEqual({});
    expect(s.lastFinishedByCell).toEqual({});
  });

  it("appendLog keeps one log per run; it outlives the run while the run is in endedRuns", () => {
    const st = useCollimateStore.getState();
    st.startRun(runRecord("r1", "c1"));
    st.startRun(runRecord("r2", "c1"));
    st.appendLog("r1", "one");
    st.appendLog("r2", "two");
    st.appendLog("r1", "three");
    expect(useCollimateStore.getState().runLogs).toEqual({ r1: ["one", "three"], r2: ["two"] });
    st.endRun("r1", "completed");
    st.endRun("r2", "completed");
    // The log outlives the run while it is in endedRuns.
    expect(useCollimateStore.getState().runLogs).toEqual({ r1: ["one", "three"], r2: ["two"] });
  });

  it("appendLogs appends lines in order in one update; an empty batch is a no-op", () => {
    const st = useCollimateStore.getState();
    st.appendLog("r1", "one");
    st.appendLogs("r1", ["two", "three"]);
    expect(useCollimateStore.getState().runLogs.r1).toEqual(["one", "two", "three"]);
    const before = useCollimateStore.getState();
    st.appendLogs("r1", []);
    expect(useCollimateStore.getState()).toBe(before);
  });

  it("ended runs (and their logs) age out after five minutes", () => {
    vi.useFakeTimers();
    try {
      const st = useCollimateStore.getState();
      st.startRun(runRecord("r1", "c1"));
      st.appendLog("r1", "old");
      st.endRun("r1", "completed");
      vi.advanceTimersByTime(5 * 60_000 + 1);
      st.startRun(runRecord("r2", "c2"));
      st.endRun("r2", "completed");
      const s = useCollimateStore.getState();
      expect(Object.keys(s.endedRuns)).toEqual(["r2"]);
      expect(s.runLogs.r1).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("a finished run (and its log) ages out after five minutes with no later run ending", () => {
    vi.useFakeTimers();
    try {
      const st = useCollimateStore.getState();
      st.startRun(runRecord("r1", "c1"));
      st.appendLog("r1", "old");
      st.endRun("r1", "completed");
      vi.advanceTimersByTime(4 * 60_000);
      expect(Object.keys(useCollimateStore.getState().endedRuns)).toEqual(["r1"]);
      vi.advanceTimersByTime(60_000 + 10);
      const s = useCollimateStore.getState();
      expect(s.endedRuns).toEqual({});
      expect(s.runLogs.r1).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("startStopping preserves the live run record", () => {
    useCollimateStore.getState().startRun(runRecord("r1", "c1"));
    useCollimateStore.getState().startStopping("r1");
    expect(useCollimateStore.getState().stoppingRunIds.has("r1")).toBe(true);
    expect(Object.hasOwn(useCollimateStore.getState().runs, "r1")).toBe(true);
  });

  it("setRun recovers a live run record", () => {
    const st = useCollimateStore.getState();
    st.setRun(runRecord("r1", "c1"));
    expect(useCollimateStore.getState().runs.r1).toBeDefined();
    expect(Object.hasOwn(useCollimateStore.getState().runs, "r1")).toBe(true);
  });

  it("setRun keeps its map when the value is unchanged", () => {
    const st = useCollimateStore.getState();
    st.setRun(runRecord("r1", "c1"));
    const runs = useCollimateStore.getState().runs;
    st.setRun(runRecord("r1", "c1"));
    expect(useCollimateStore.getState().runs).toBe(runs);
    st.setRun({ ...runRecord("r1", "c1"), label: "renamed" });
    expect(useCollimateStore.getState().runs).not.toBe(runs);
  });
});

describe("useCollimateStore — the cell log's runs", () => {
  beforeEach(() => {
    resetFields();
    useCollimateStore.setState({ cellLogRuns: {}, runLogs: {}, runLogArrivals: {} });
  });

  it("appendLogs keeps one arrival per line through the tail cap; pruning frees them with the log (C15)", () => {
    const st = useCollimateStore.getState();
    st.startRun(runRecord("r1", "c1"));
    st.appendLogs("r1", Array.from({ length: LOG_TAIL_LINES + 3 }, (_, i) => `l${i}`));
    st.appendLog("r1", "last");
    const s1 = useCollimateStore.getState();
    expect(s1.runLogs.r1).toHaveLength(LOG_TAIL_LINES);
    expect(s1.runLogArrivals.r1).toHaveLength(LOG_TAIL_LINES);
    st.endRun("r1", "completed");
    useCollimateStore.setState(prev => ({
      endedRuns: { r1: { ...prev.endedRuns.r1, endedAt: 0 } },
    }));
    st.pruneEndedRuns();
    const s2 = useCollimateStore.getState();
    expect(s2.runLogs.r1).toBeUndefined();
    expect(s2.runLogArrivals.r1).toBeUndefined();
    // The cell log kept its snapshot.
    expect(s2.cellLogRuns.c1[0].ended?.lines.at(-1)).toBe("last");
  });
});

describe("useCollimateStore — patchCell", () => {
  beforeEach(resetFields);

  it("merges the patch into the cached cell without bumping updated_at", () => {
    // patchCell deliberately does NOT mint a fresh client-wallclock
    // timestamp on every patch — log-stream patches arriving during a
    // chat turn would otherwise advance updated_at past the server's
    // gen.commit microsecond timestamp, causing applyServerState to
    // reject the freshly-committed messages.json.
    useCollimateStore.setState({
      cellCache: {
        c1: { id: "c1", name: "orig", files: [], updated_at: "2024-06-01T12:00:00.000Z" } as unknown as Cell,
      },
    });
    useCollimateStore.getState().patchCell("c1", { name: "renamed" });

    const cell = useCollimateStore.getState().cellCache.c1;
    expect(cell.name).toBe("renamed");
    expect(cell.updated_at).toBe("2024-06-01T12:00:00.000Z");
  });

  it("creates a cell entry when patching an unknown id", () => {
    useCollimateStore.getState().patchCell("new-id", { name: "seed" });
    expect(useCollimateStore.getState().cellCache["new-id"].name).toBe("seed");
  });
});

// --------------------------------------------------------------------
// The Commands pane slice and the surfaces its commands open.
// --------------------------------------------------------------------

describe("useCollimateStore — Commands pane", () => {
  beforeEach(resetFields);

  it("the cell search's state and actions are gone (K02)", () => {
    const state = useCollimateStore.getState() as unknown as Record<string, unknown>;
    for (const gone of ["paletteState", "openPalette", "setPaletteQuery", "setPalettePreviewCell", "closePalette"]) {
      expect(state).not.toHaveProperty(gone);
    }
  });

  it("opens, toggles and closes; closing clears the query, the side stays (K01)", () => {
    const s = () => useCollimateStore.getState();
    s().openCommandPane();
    s().setCommandPaneQuery("fan");
    s().setCommandPaneSide("right");
    expect(s().commandPane).toEqual({ open: true, side: "right", query: "fan", recents: [], view: { kind: "commands" } });
    s().toggleCommandPane();
    expect(s().commandPane).toEqual({ open: false, side: "right", query: "", recents: [], view: { kind: "commands" } });
    s().toggleCommandPane();
    expect(s().commandPane.open).toBe(true);
    s().closeCommandPane();
    expect(s().commandPane.open).toBe(false);
  });

  it("opens on settings, switches views, and every close returns to the commands with the query cleared (CP-3)", () => {
    const s = () => useCollimateStore.getState();
    s().openCommandPaneSettings(null);
    expect(s().commandPane).toMatchObject({ open: true, view: { kind: "settings", section: null } });
    s().setCommandPaneView({ kind: "settings", section: "theme" });
    expect(s().commandPane.view).toEqual({ kind: "settings", section: "theme" });
    s().setCommandPaneQuery("x");
    s().closeCommandPane();
    expect(s().commandPane).toMatchObject({ open: false, query: "", view: { kind: "commands" } });
    s().openCommandPaneSettings("servers");
    s().toggleCommandPane();
    expect(s().commandPane).toMatchObject({ open: false, view: { kind: "commands" } });
    s().toggleCommandPane();
    expect(s().commandPane).toMatchObject({ open: true, view: { kind: "commands" } });
  });

  it("keeps the last three recents, most recent first, without duplicates (K01)", () => {
    const s = () => useCollimateStore.getState();
    for (const key of ["run", "log", "fan", "log", "zoom"]) s().recordCommandPaneRecent(key);
    expect(s().commandPane.recents).toEqual(["zoom", "log", "fan"]);
  });

  it("adopts a channel's saved pane, capped to three recents (K01)", () => {
    useCollimateStore.getState().adoptCommandPane({ open: true, side: "right", query: "x", recents: ["a", "b", "c", "d"] });
    expect(useCollimateStore.getState().commandPane).toEqual({ open: true, side: "right", query: "x", recents: ["a", "b", "c"], view: { kind: "commands" } });
  });

  it("the runs menu is a flag its controls share (K01)", () => {
    const s = () => useCollimateStore.getState();
    s().setRunsMenuOpen(true);
    expect(s().runsMenuOpen).toBe(true);
    s().setRunsMenuOpen(false);
    expect(s().runsMenuOpen).toBe(false);
  });
});

// --------------------------------------------------------------------
// Cell panel sessions — one persisted panel per cell, of one kind.
// --------------------------------------------------------------------

describe("useCollimateStore — pickSessions", () => {
  beforeEach(() => {
    resetFields();
    useCollimateStore.setState({
      pickSessions: {},
      cellHistory: {},
      activeChannel: { id: '11111111-1111-4111-8111-111111111111', name: 'Shared label' } as Channel,
    });
    vi.restoreAllMocks();
  });

  it("openPickSession creates a fresh session of its kind with an empty query (P01)", () => {
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    const s = useCollimateStore.getState().pickSessions["cell-1"];
    expect(s.kind).toBe("runners");
    expect(s.query).toBe("");
    expect(Object.keys(s).sort()).toEqual(["at", "kind", "query"]);
    expect(typeof s.at).toBe("number");
  });

  it("one panel per cell: opening another kind replaces the cell's session, and reopening resets the query (P01)", () => {
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    useCollimateStore.getState().updatePickSession("cell-1", "runners", { query: "stale" });
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    expect(useCollimateStore.getState().pickSessions["cell-1"].query).toBe("");
    useCollimateStore.getState().openPickSession("cell-1", "files");
    expect(useCollimateStore.getState().pickSessions["cell-1"]).toMatchObject({ kind: "files", query: "" });
  });

  it("openPickSession is refused while the cell is in version history (P01)", () => {
    useCollimateStore.getState().openCellHistory("cell-1");
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    useCollimateStore.getState().openPickSession("cell-1", "files");
    expect(useCollimateStore.getState().pickSessions["cell-1"]).toBeUndefined();
    // Out of history, it opens as usual.
    useCollimateStore.getState().closeCellHistory("cell-1");
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    expect(useCollimateStore.getState().pickSessions["cell-1"]).toBeDefined();
  });

  it("sessions are independent per cell (P01)", () => {
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    useCollimateStore.getState().openPickSession("cell-2", "transfer");
    useCollimateStore.getState().updatePickSession("cell-1", "runners", { query: "one" });
    const all = useCollimateStore.getState().pickSessions;
    expect(all["cell-1"].query).toBe("one");
    expect(all["cell-2"]).toMatchObject({ kind: "transfer", query: "" });
  });

  it("updatePickSession merges the patch and bumps `at` (P01)", () => {
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    const before = useCollimateStore.getState().pickSessions["cell-1"].at;
    vi.spyOn(Date, "now").mockReturnValue(before + 1000);
    useCollimateStore.getState().updatePickSession("cell-1", "runners", { query: "wri" });
    const s = useCollimateStore.getState().pickSessions["cell-1"];
    expect(s.query).toBe("wri");
    expect(s.at).toBe(before + 1000);
  });

  it("update and close touch only a session of their kind, so a replaced panel never reaches its successor (P01)", () => {
    useCollimateStore.getState().updatePickSession("ghost", "runners", { query: "x" });
    expect(useCollimateStore.getState().pickSessions.ghost).toBeUndefined();
    useCollimateStore.getState().openPickSession("cell-1", "files");
    useCollimateStore.getState().updatePickSession("cell-1", "runners", { query: "late" });
    useCollimateStore.getState().closePickSession("cell-1", "runners");
    expect(useCollimateStore.getState().pickSessions["cell-1"]).toMatchObject({ kind: "files", query: "" });
    useCollimateStore.getState().closePickSession("cell-1", "files");
    expect(useCollimateStore.getState().pickSessions["cell-1"]).toBeUndefined();
  });

  it("closePickSession drops only that cell's session (P01)", () => {
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    useCollimateStore.getState().openPickSession("cell-2", "runners");
    useCollimateStore.getState().closePickSession("cell-2", "runners");
    expect(useCollimateStore.getState().pickSessions["cell-2"]).toBeUndefined();
    expect(useCollimateStore.getState().pickSessions["cell-1"]).toBeDefined();
  });

  it("hydratePickSessions keeps the LOCAL session when it is newer (P01)", () => {
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    const local = useCollimateStore.getState().pickSessions["cell-1"];
    useCollimateStore.getState().hydratePickSessions(useCollimateStore.getState().activeChannel!.id, {
      "cell-1": { ...local, query: "stale", at: local.at - 5000 },
      "cell-2": { ...local, kind: "files", query: "server", at: local.at },
    });
    const all = useCollimateStore.getState().pickSessions;
    // Local edits made before hydration landed must survive…
    expect(all["cell-1"].query).toBe("");
    // …and cells with no local session take the server copy.
    expect(all["cell-2"]).toMatchObject({ kind: "files", query: "server" });
  });

  it("hydratePickSessions leaves out a server entry that names no panel kind (P01)", () => {
    useCollimateStore.getState().hydratePickSessions(useCollimateStore.getState().activeChannel!.id, {
      "cell-1": { query: "kindless", at: 5 } as unknown as PickSession,
      "cell-2": { kind: "palette", query: "", at: 5 } as unknown as PickSession,
      "cell-3": { kind: "transfer", query: "", at: 5 },
    });
    expect(Object.keys(useCollimateStore.getState().pickSessions)).toEqual(["cell-3"]);
  });

  it("hydratePickSessions drops a stale handlers session: handlers are managed on Capabilities", () => {
    useCollimateStore.getState().hydratePickSessions(useCollimateStore.getState().activeChannel!.id, {
      "renderers-cell": { kind: "handlers", query: "", handlerFolder: "memo", at: Date.now() } as unknown as PickSession,
      "work-cell": { kind: "files", query: "", at: Date.now() },
    });
    expect(Object.keys(useCollimateStore.getState().pickSessions)).toEqual(["work-cell"]);
  });

  it("hydratePickSessions never resurrects a session closed here from a server copy that predates the close (P01)", () => {
    // Run in new cell: the runners panel's close runs after the run POST,
    // while the clone's CellEditor mounting re-hydrates from a server that
    // still holds the session — the panel must stay closed on the source cell.
    const now = vi.spyOn(Date, "now");
    try {
      now.mockReturnValue(1_000);
      useCollimateStore.getState().openPickSession("cell-1", "runners");
      useCollimateStore.getState().updatePickSession("cell-1", "runners", { query: "chat" });
      const saved = useCollimateStore.getState().pickSessions["cell-1"];
      now.mockReturnValue(3_000);
      useCollimateStore.getState().closePickSession("cell-1", "runners");
      useCollimateStore.getState().hydratePickSessions(useCollimateStore.getState().activeChannel!.id, { "cell-1": { ...saved } });
      expect(useCollimateStore.getState().pickSessions["cell-1"]).toBeUndefined();
      // A session opened again later (another client, a later refresh) is newer than the close and lands.
      useCollimateStore.getState().hydratePickSessions(useCollimateStore.getState().activeChannel!.id, { "cell-1": { kind: "runners", query: "again", at: 5_000 } });
      expect(useCollimateStore.getState().pickSessions["cell-1"].query).toBe("again");
      // Re-opening here clears the tombstone: a hydrate then merges as before.
      now.mockReturnValue(6_000);
      useCollimateStore.getState().closePickSession("cell-1", "runners");
      now.mockReturnValue(7_000);
      useCollimateStore.getState().openPickSession("cell-1", "runners");
      expect(useCollimateStore.getState().pickSessions["cell-1"]).toBeDefined();
    } finally {
      now.mockRestore();
    }
  });

  it("hydratePickSessions takes the SERVER session when it is newer (P01)", () => {
    useCollimateStore.getState().openPickSession("cell-1", "runners");
    const local = useCollimateStore.getState().pickSessions["cell-1"];
    useCollimateStore.getState().hydratePickSessions(useCollimateStore.getState().activeChannel!.id, {
      "cell-1": { ...local, query: "fresher", at: local.at + 5000 },
    });
    expect(useCollimateStore.getState().pickSessions["cell-1"].query).toBe("fresher");
  });
});

// ---------------------------------------------------------------------------
// Capabilities fingerprint: the renderer inventory, the runner catalog and
// the manage view refetch when a Capabilities cell is added, moved on or off,
// or edited, without a channel switch or refresh.
// ---------------------------------------------------------------------------

type StampedCell = { id: string; updated_at: string };
const stampedCells = (cells: StampedCell[]): Cell[] =>
  cells.map((c) => ({ id: c.id, name: c.id, files: [], updated_at: c.updated_at } as unknown as Cell));

function makeChannel(args: {
  id: string;
  capabilitiesCells?: StampedCell[];
  workCells?: StampedCell[];
  hidden?: boolean;
}): Channel {
  const capabilitiesStack: Stack = {
    id: `caps-${args.id}`,
    name: "Capabilities",
    stack_type: "capabilities",
    cells: stampedCells(args.capabilitiesCells ?? []),
  };
  const work: Stack = { id: `work-${args.id}`, name: "", cells: stampedCells(args.workCells ?? []) };
  return {
    id: args.id,
    name: args.id,
    nav_stacks: args.hidden ? [capabilitiesStack] : [],
    main_stacks: args.hidden ? [work] : [capabilitiesStack, work],
    nav_selection_id: null,
    main_selection_id: null,
  };
}

function makeAppState(channel: Channel): AppState {
  return {
    channels: [channel],
    active_channel_id: channel.id,
  };
}

describe("useCollimateStore — capabilities fingerprint", () => {
  beforeEach(() => {
    resetFields();
    useCollimateStore.setState({
      _capabilitiesFingerprint: "",
      activeChannel: null,
      state: null,
    });
  });

  it("names the channel, then each Capabilities cell's id and stamp, cards included, shown or hidden", () => {
    const cells = [{ id: "card", updated_at: "t0" }, { id: "g1", updated_at: "t1" }];
    expect(capabilitiesSourceFingerprint(makeChannel({ id: "ch", capabilitiesCells: cells }))).toBe("ch|card:t0|g1:t1");
    expect(capabilitiesSourceFingerprint(makeChannel({ id: "ch", capabilitiesCells: cells, hidden: true }))).toBe("ch|card:t0|g1:t1");
    expect(capabilitiesSourceFingerprint(null)).toBe("");
  });

  it("bumps when a Capabilities cell's updated_at changes, and ignores standard cells", () => {
    const ch1 = makeChannel({
      id: "ch-1",
      capabilitiesCells: [{ id: "g1", updated_at: "2026-04-30T00:00:00.000Z" }],
      workCells: [{ id: "w1", updated_at: "2026-04-30T00:00:00.000Z" }],
    });
    useCollimateStore.getState().applyServerState(makeAppState(ch1));
    const fp1 = useCollimateStore.getState()._capabilitiesFingerprint;
    expect(fp1).toContain("g1:2026-04-30T00:00:00.000Z");
    expect(fp1).not.toContain("w1");

    const edited = makeChannel({
      id: "ch-1",
      capabilitiesCells: [{ id: "g1", updated_at: "2026-04-30T01:00:00.000Z" }],
      workCells: [{ id: "w1", updated_at: "2026-04-30T00:00:00.000Z" }],
    });
    useCollimateStore.getState().applyServerState(makeAppState(edited));
    const fp2 = useCollimateStore.getState()._capabilitiesFingerprint;
    expect(fp2).not.toBe(fp1);

    const workEdited = makeChannel({
      id: "ch-1",
      capabilitiesCells: [{ id: "g1", updated_at: "2026-04-30T01:00:00.000Z" }],
      workCells: [{ id: "w1", updated_at: "2026-04-30T02:00:00.000Z" }],
    });
    useCollimateStore.getState().applyServerState(makeAppState(workEdited));
    expect(useCollimateStore.getState()._capabilitiesFingerprint).toBe(fp2);
  });

  it("applyOptimisticChannel moves the fingerprint when a cell moves onto Capabilities", () => {
    const before = makeChannel({ id: "ch-1", workCells: [{ id: "moved", updated_at: "t" }] });
    useCollimateStore.getState().applyServerState(makeAppState(before));
    const fp1 = useCollimateStore.getState()._capabilitiesFingerprint;
    const after = makeChannel({ id: "ch-1", capabilitiesCells: [{ id: "moved", updated_at: "t" }] });
    useCollimateStore.getState().applyOptimisticChannel(after);
    expect(useCollimateStore.getState()._capabilitiesFingerprint).not.toBe(fp1);
    expect(useCollimateStore.getState()._capabilitiesFingerprint).toContain("moved");
  });

  it("moves between channels whose Capabilities stacks are alike", () => {
    const a = makeChannel({ id: "ch-a" });
    const b = makeChannel({ id: "ch-b" });
    useCollimateStore.getState().applyServerState({ channels: [a, b], active_channel_id: "ch-a" });
    const fpA = useCollimateStore.getState()._capabilitiesFingerprint;
    useCollimateStore.getState().applyServerState({ channels: [a, b], active_channel_id: "ch-b" });
    const fpB = useCollimateStore.getState()._capabilitiesFingerprint;
    expect(fpA).toContain("ch-a");
    expect(fpB).toContain("ch-b");
    expect(fpB).not.toBe(fpA);
  });
});

describe("accepted per-file revisions",()=>{
  beforeEach(resetFields);
  const file=(revision:number,hash:string):VirtualFile=>({name:'notes.txt',language:'text',file_revision:revision,body_hash:hash,encoding:'utf-8',writer_id:'writer',edit_seq:revision});
  const state=(entry:VirtualFile,stamp:string):AppState=>({active_channel_id:'ch',channels:[{id:'ch',name:'channel',nav_stacks:[],main_stacks:[{id:'stack',name:'',cells:[{id:'cell',name:'cell',files:[entry],updated_at:stamp}]}],nav_selection_id:null,main_selection_id:'stack'}]});
  it('accepts a new revision when display timestamps collide below a millisecond',()=>{
    const store=useCollimateStore.getState();store.applyServerState(state(file(1,'old'),'2026-10-08T10:00:00.123001Z'));store.applyServerState(state(file(2,'new'),'2026-10-08T10:00:00.123999Z'));
    expect(useCollimateStore.getState().cellCache.cell.files[0].body_hash).toBe('new');
  });
  it('accepts server bytes despite a future client display clock',()=>{
    const store=useCollimateStore.getState();store.applyServerState(state(file(1,'old'),'2026-10-08T10:01:00Z'));store.applyServerState(state(file(2,'new'),'2026-10-08T10:00:00Z'));
    expect(useCollimateStore.getState().cellCache.cell.files[0].body_hash).toBe('new');
  });
  it('retains a newer descriptor when an old mutation response arrives',()=>{
    const store=useCollimateStore.getState();store.applyServerState(state(file(2,'new'),'2026-10-08T10:00:00Z'));store.applyOptimisticChannel(state(file(1,'old'),'2026-10-08T10:02:00Z').channels[0]);
    expect(useCollimateStore.getState().cellCache.cell.files[0].body_hash).toBe('new');
  });
});

describe("useCollimateStore — Capabilities manage requests", () => {
  beforeEach(() => useCollimateStore.setState({ capabilitiesManageRequest: null }));

  it("mints a fresh seq per request, and a newer request replaces an older one", () => {
    const store = useCollimateStore.getState();
    store.requestCapabilitiesManage({ kind: "runner", runner: { action: "add", generatorId: "synth-gen" } });
    const first = useCollimateStore.getState().capabilitiesManageRequest!;
    expect(first).toMatchObject({ kind: "runner", runner: { action: "add", generatorId: "synth-gen" } });
    store.requestCapabilitiesManage({ create: true });
    const second = useCollimateStore.getState().capabilitiesManageRequest!;
    expect(second.seq).toBeGreaterThan(first.seq);
    expect(second).toEqual({ create: true, seq: second.seq });
  });

  it("clears only the request it names", () => {
    const store = useCollimateStore.getState();
    store.requestCapabilitiesManage({ addFromCatalog: true });
    const stale = useCollimateStore.getState().capabilitiesManageRequest!.seq;
    store.requestCapabilitiesManage({ query: "memo" });
    store.clearCapabilitiesManageRequest(stale);
    expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({ query: "memo" });
    store.clearCapabilitiesManageRequest(useCollimateStore.getState().capabilitiesManageRequest!.seq);
    expect(useCollimateStore.getState().capabilitiesManageRequest).toBeNull();
  });
});
