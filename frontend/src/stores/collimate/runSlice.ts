import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';
import type { RunRecord } from '@/lib/types';
import type { LastFinishedRun, EndedRun, PendingReveal } from './types';
import { loadLastFinished, saveLastFinished } from './persistence';
import { isFailureStatus } from '@/lib/runStop';
import { isRunCardPath, lockOwner } from '@/lib/runningCell';
import { createDefaultLayout, resolvePinnedPath, revealFileTab } from '@/lib/cellLayout';
import { fetchActiveRunsEverywhere, type ChannelActiveRuns } from '@/lib/api';
import { readSavedRunLog, type CellLogRun } from '@/lib/cellLog';

export const REVEAL_TTL_MS = 5000;
/** How long an ended run's in-memory log (and its endedRuns entry) is kept. */
export const ENDED_RUN_LOG_MS = 5 * 60_000;
export const LOG_TAIL_LINES = 5000;

// The latest refreshActiveRunsEverywhere request; an older answer is dropped.
let everywhereRequest = 0;

/** A per-run record kept only for runs whose log is still held. */
function retainForLogs<T>(record: Record<string, T>, logs: Record<string, string[]>): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([id]) => id in logs));
}

export interface RunSlice {

  // Every channel's live runs (GET /api/runs/active, the ones whose end is
  // under way included, flagged `stopping`): the top bar's running count and
  // its runs menu. Refetched on the channel stream's runs_active_changed and
  // on every stream (re)connect; never polled.
  activeRunsEverywhere: ChannelActiveRuns[];
  /** Refetch activeRunsEverywhere; the latest request's answer wins. */
  refreshActiveRunsEverywhere: () => Promise<void>;

  // Every live run (active or stopping), keyed by run id: its cell, runner
  // identity, label and run folder. Set from the POST /runs response, the
  // run_started broadcast and /runs/active; dropped by endRun. Several runs
  // may share a cell — see lib/runningCell.ts for the selectors.
  runs: Record<string, RunRecord>;

  // Track runs where the user has clicked Stop (waiting for actual termination)
  stoppingRunIds: Set<string>;

  // Live lifecycle phase per active run (run_id → {phase, message}). Drives the
  // run light's "spinning up" vs "running". Session-only + ephemeral: seeded
  // from the events_since phase column on attach and updated by run_phase
  // SSE; cleared on endRun. NOT persisted — backfill/SSE is the single
  // authority so a stale phase can't survive a reload.
  runPhases: Record<string, { phase: string; message?: string }>;

  // Per-run log lines: the RUN started bookend, the backfilled and live
  // events, the RUN finished bookend. Session-only; freed when the run
  // leaves endedRuns.
  runLogs: Record<string, string[]>;
  // When each line of runLogs arrived (ms, parallel to it): the cell log
  // dates a line without a `[HH:MM:SS]` prefix by it.
  runLogArrivals: Record<string, number[]>;

  // The cell log (C15): per cell, every run this client saw start on it,
  // in start order. A run's live lines are snapshotted into its entry when
  // it ends, so the log keeps them after runLogs lets go; its saved
  // logs.txt is read once, the first time the log opens after it ended.
  // Session-only. The status pane shows it in All's Log (every run) and in
  // a run's drilldown Log (that run's lines).
  cellLogRuns: Record<string, CellLogRun[]>;
  /** Read the saved logs.txt of every ended run in the cell's log that has
   *  not been read yet (once per run; a failure is not retried). */
  readSavedCellLogs: (cellId: string) => Promise<void>;

  // Runs that ended while the app was open, snapshotted at the end: an
  // entry holds the run's in-memory log (runLogs) and its failure reason
  // (lib/runAttach) for ENDED_RUN_LOG_MS. Session-only (NOT persisted).
  // Entries age out on a timer and are also pruned on endRun.
  endedRuns: Record<string, EndedRun>;

  // Each cell's last finished run (set by endRun, cleared when a run starts
  // on the cell): the idle run light's failure signal until the user has
  // seen it, so an unseen failure is kept over a sibling run's later
  // non-failure. Persisted (localStorage).
  lastFinishedByCell: Record<string, LastFinishedRun>;

  // Files runs asked to show (reveal_file, a surface file on start), per cell,
  // waiting for the path to land in the cell's files. A module subscriber
  // below applies each entry once it does (layout-only — see applyReveal);
  // entries expire after REVEAL_TTL_MS or when their run ends.
  pendingReveals: Record<string, PendingReveal[]>;
  /** A run started (or was found live): record it, mark it active and
   *  clear its cell's last-finished entry. */
  startRun: (run: RunRecord) => void;
  /** Refresh a run's record without touching its live state (the
   *  /runs/active re-seed of a run this client is already attached to). */
  setRun: (run: RunRecord) => void;
  /** The run ended: snapshot it into endedRuns, its cell's
   *  lastFinishedByCell (unless that holds an unseen failure and this run
   *  did not fail) and the status view's finished runs, then drop its
   *  record, phase, pending reveals and settings requests. `failureReason`
   *  rides into all three. */
  endRun: (runId: string, status?: string, failureReason?: string) => void;
  /** Append one line to a run's log. */
  appendLog: (runId: string, line: string) => void;
  /** Append several lines to a run's log in one update. */
  appendLogs: (runId: string, lines: string[]) => void;
  /** Drop endedRuns entries (and their logs) older than
   *  ENDED_RUN_LOG_MS, then re-arm the age-out timer. */
  pruneEndedRuns: () => void;
  /** The user has seen the cell's last finished run (opened the cell's
   *  status view, or a finished run's drilldown). */
  markFailureSeen: (cellId: string) => void;
  startStopping: (runId: string) => void;
  clearStopping: (runId: string) => void;
  /** Set the live lifecycle phase for a run (from run_phase SSE / attach seed). */
  setRunPhase: (runId: string, phase: string, message?: string) => void;
  /** Queue a file a run asked to show; replaces a queued entry for the same
   *  path. Applied as soon as the path is in the cell's files. */
  queueReveal: (cellId: string, entry: { path: string; runId: string; activate?: boolean }) => void;
  /** Layout-only reveal of a cell file on behalf of a run (never selects
   *  the cell): see lib/cellLayout revealFileTab for the focus rule. False
   *  when the cell or the path is not there (yet). */
  applyReveal: (cellId: string, path: string, runId: string, activate?: boolean) => boolean;
}

export const createRunSlice = (
  scheduleEndedPrune: () => void,
  drainPendingReveals: () => void,
): StateCreator<CollimateState, [['zustand/subscribeWithSelector', never]], [], RunSlice> => (set, get) => ({
    runs: {},
    stoppingRunIds: new Set<string>(),
    runPhases: {},
    runLogs: {},
    runLogArrivals: {},
    cellLogRuns: {},
    endedRuns: {},
    activeRunsEverywhere: [],
    refreshActiveRunsEverywhere: async () => {
      const request = ++everywhereRequest;
      const list = await fetchActiveRunsEverywhere();
      if (request === everywhereRequest) set({ activeRunsEverywhere: list });
    },
    lastFinishedByCell: loadLastFinished(),
    pendingReveals: {},

    startRun: (run: RunRecord) => {
      set((prev) => {
        let lastFinishedByCell = prev.lastFinishedByCell;
        if (run.cellId in lastFinishedByCell) {
          lastFinishedByCell = { ...lastFinishedByCell };
          delete lastFinishedByCell[run.cellId];
          saveLastFinished(lastFinishedByCell);
        }
        const logRuns = prev.cellLogRuns[run.cellId] ?? [];
        const cellLogRuns = logRuns.some(r => r.runId === run.runId) ? prev.cellLogRuns : {
          ...prev.cellLogRuns,
          [run.cellId]: [...logRuns, {
            runId: run.runId, channel: run.channel, runner: run.runnerName ?? run.label, startedAt: Date.now(),
          }],
        };
        return { runs: { ...prev.runs, [run.runId]: run }, lastFinishedByCell, cellLogRuns };
      });
    },

    setRun: (run: RunRecord) => {
      set((prev) => {
        // An identical record keeps the map (reconnect recovery re-sends it).
        const cur = prev.runs[run.runId];
        const keys = Object.keys(run) as (keyof RunRecord)[];
        if (cur && keys.length === Object.keys(cur).length && keys.every((k) => cur[k] === run[k])) return {};
        return { runs: { ...prev.runs, [run.runId]: run } };
      });
    },

    endRun: (runId: string, status?: string, failureReason?: string) => {
      const ended = get().runs[runId];
      set((prev) => {
        const stoppingRunIds = new Set(prev.stoppingRunIds);
        stoppingRunIds.delete(runId);
        const run = prev.runs[runId];
        const runs = { ...prev.runs };
        delete runs[runId];

        // Snapshot the ended run, which holds its log. Entries older than
        // ENDED_RUN_LOG_MS age out here (with their in-memory logs) so the
        // record can't grow unbounded.
        let endedRuns = prev.endedRuns;
        let runLogs = prev.runLogs;
        let lastFinishedByCell = prev.lastFinishedByCell;
        if (status && run) {
          const now = Date.now();
          const cutoff = now - ENDED_RUN_LOG_MS;
          endedRuns = {};
          const dropped: string[] = [];
          for (const [id, rec] of Object.entries(prev.endedRuns)) {
            if (rec.endedAt >= cutoff) endedRuns[id] = rec;
            else dropped.push(id);
          }
          endedRuns[runId] = {
            runId,
            cellId: run.cellId,
            label: run.label,
            status,
            ...(failureReason ? { failureReason } : {}),
            endedAt: now,
          };
          if (dropped.length) {
            runLogs = { ...prev.runLogs };
            for (const id of dropped) delete runLogs[id];
          }
          // An unseen failure outlives a sibling run on the same cell that
          // ends after it without failing: the light is how the user learns
          // of it. A newer failure replaces it.
          const cur = prev.lastFinishedByCell[run.cellId];
          const keepUnseenFailure = !!cur && !cur.seen && isFailureStatus(cur.status)
            && !isFailureStatus(status);
          if (!keepUnseenFailure) {
            lastFinishedByCell = {
              ...prev.lastFinishedByCell,
              [run.cellId]: {
                runId,
                label: run.label,
                status,
                ...(failureReason ? { reason: failureReason } : {}),
                seen: !isFailureStatus(status),
                at: now,
              },
            };
            saveLastFinished(lastFinishedByCell);
          }
        }

        // The run is terminal — drop its live phase so a lingering
        // indicator can't outlive it.
        let runPhases = prev.runPhases;
        if (runId in prev.runPhases) {
          runPhases = { ...prev.runPhases };
          delete runPhases[runId];
        }
        // The cell log keeps the run's live lines past runLogs' age-out.
        let cellLogRuns = prev.cellLogRuns;
        const logRuns = run ? prev.cellLogRuns[run.cellId] : undefined;
        if (run && logRuns?.some(r => r.runId === runId && !r.ended)) {
          const ended = { lines: prev.runLogs[runId] ?? [], arrivals: prev.runLogArrivals[runId] ?? [] };
          cellLogRuns = {
            ...prev.cellLogRuns,
            [run.cellId]: logRuns.map(r => (r.runId === runId ? { ...r, ended } : r)),
          };
        }
        // Its pending reveals and settings requests die with it.
        let pendingReveals = prev.pendingReveals;
        for (const [cellId, list] of Object.entries(prev.pendingReveals)) {
          const kept = list.filter(e => e.runId !== runId);
          if (kept.length === list.length) continue;
          if (pendingReveals === prev.pendingReveals) pendingReveals = { ...prev.pendingReveals };
          if (kept.length) pendingReveals[cellId] = kept;
          else delete pendingReveals[cellId];
        }


        return {
          runs, stoppingRunIds, runPhases, runLogs,
          runLogArrivals: retainForLogs(prev.runLogArrivals, runLogs),
          endedRuns, lastFinishedByCell, pendingReveals, cellLogRuns,
        };
      });
      if (status && ended) {
        get().addFinishedRun({
          runId, cellId: ended.cellId, channel: ended.channel, runnerId: ended.runnerId, runnerName: ended.runnerName,
          generatorName: ended.generatorName, label: ended.label, folder: ended.folder, status,
          ...(failureReason ? { failureReason } : {}), endedAt: Date.now(),
        });
      }
      if (status) scheduleEndedPrune();
    },

    appendLog: (runId, line) => get().appendLogs(runId, [line]),

    appendLogs: (runId, lines) => {
      if (!lines.length) return;
      set(prev => {
        const now = Date.now();
        const prior = prev.runLogs[runId] ?? [];
        const priorAt = prev.runLogArrivals[runId] ?? [];
        const overflow = Math.max(0, prior.length + lines.length - LOG_TAIL_LINES);
        const tail = lines.length >= LOG_TAIL_LINES
          ? lines.slice(-LOG_TAIL_LINES)
          : [...prior.slice(Math.max(0, overflow)), ...lines];
        // The arrivals keep pace with the lines from the tail end.
        const tailAt = [...priorAt, ...lines.map(() => now)].slice(-tail.length);
        return {
          runLogs: { ...prev.runLogs, [runId]: tail },
          runLogArrivals: { ...prev.runLogArrivals, [runId]: tailAt },
        };
      });
    },

    pruneEndedRuns: () => {
      set((prev) => {
        const cutoff = Date.now() - ENDED_RUN_LOG_MS;
        const dropped = Object.values(prev.endedRuns)
          .filter((rec) => rec.endedAt < cutoff)
          .map((rec) => rec.runId);
        if (dropped.length === 0) return {};
        const endedRuns = { ...prev.endedRuns };
        const runLogs = { ...prev.runLogs };
        for (const id of dropped) {
          delete endedRuns[id];
          delete runLogs[id];
        }
        return {
          endedRuns, runLogs,
          runLogArrivals: retainForLogs(prev.runLogArrivals, runLogs),
        };
      });
      scheduleEndedPrune();
    },

    markFailureSeen: (cellId: string) => {
      set((prev) => {
        const cur = prev.lastFinishedByCell[cellId];
        if (!cur || cur.seen) return {};
        const lastFinishedByCell = { ...prev.lastFinishedByCell, [cellId]: { ...cur, seen: true } };
        saveLastFinished(lastFinishedByCell);
        return { lastFinishedByCell };
      });
    },

    startStopping: (runId: string) => {
      set((prev) => {
        const next = new Set(prev.stoppingRunIds);
        next.add(runId);
        return { stoppingRunIds: next };
      });
    },

    // Revert a startStopping that never led to a real stop. Called when
    // stopRun() throws (network down, 404, etc.) — otherwise the Stop
    // button stays in "Stopping…" forever and the user can't retry.
    // Safe no-op if the run already finished via the normal endRun path.
    clearStopping: (runId: string) => {
      set((prev) => {
        if (!prev.stoppingRunIds.has(runId)) return {};
        const next = new Set(prev.stoppingRunIds);
        next.delete(runId);
        return { stoppingRunIds: next };
      });
    },

    setRunPhase: (runId: string, phase: string, message?: string) => {
      set((prev) => {
        const cur = prev.runPhases[runId];
        if (cur && cur.phase === phase && (cur.message ?? '') === (message ?? '')) return {};
        return { runPhases: { ...prev.runPhases, [runId]: { phase, message } } };
      });
    },

    readSavedCellLogs: async (cellId) => {
      const due = (get().cellLogRuns[cellId] ?? []).filter(r => r.ended && !r.saved);
      if (due.length === 0) return;
      const mark = (runId: string, saved: NonNullable<CellLogRun['saved']>) => set((prev) => {
        const logRuns = prev.cellLogRuns[cellId];
        if (!logRuns) return {};
        return { cellLogRuns: { ...prev.cellLogRuns, [cellId]: logRuns.map(r => (r.runId === runId ? { ...r, saved } : r)) } };
      });
      for (const r of due) mark(r.runId, { state: 'pending' });
      await Promise.all(due.map(async (r) => {
        try {
          const lines = await readSavedRunLog(r.channel, r.runId);
          mark(r.runId, { state: 'read', lines, at: Date.now() });
        } catch {
          mark(r.runId, { state: 'failed' });
        }
      }));
    },

    queueReveal: (cellId, entry) => {
      // A run card is the run's record, never a view: a reveal of one is dropped.
      if (isRunCardPath(entry.path)) return;
      set((prev) => {
        const list = (prev.pendingReveals[cellId] ?? []).filter(e => e.path !== entry.path);
        const next: PendingReveal = {
          path: entry.path,
          runId: entry.runId,
          at: Date.now(),
          ...(entry.activate !== undefined ? { activate: entry.activate } : {}),
        };
        return { pendingReveals: { ...prev.pendingReveals, [cellId]: [...list, next] } };
      });
      // Expire it even if nothing else ever changes in the store.
      setTimeout(() => drainPendingReveals(), REVEAL_TTL_MS + 1);
    },

    applyReveal: (cellId, path, runId, activate) => {
      const s = get();
      const cell = s.locateCell(cellId)?.cell ?? s.cellCache[cellId];
      const files = cell?.files ?? [];
      if (!files.some((f) => f.name === path)) return false;
      const pinnedPath = resolvePinnedPath(cell?.pinned, files);
      const runLabel = s.runs[runId]?.label ?? '';
      const lockOwnerOf = (p: string) => lockOwner(get(), cellId, p)?.label ?? null;
      // A cell with no layout yet has no user content to protect: its seed
      // (one empty tab) is taken over whatever the focus request.
      const focus = s.cellLayouts[cellId] ? activate : undefined;
      s.updateCellLayout(
        cellId,
        (layout) => revealFileTab(layout, path, { runLabel, pinnedPath, lockOwnerOf, activate: focus }),
        () => createDefaultLayout({ filePaths: files.map((f) => f.name), pinnedPath }),
      );
      return true;
    }
});
