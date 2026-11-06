import {flushFileDocuments} from '@/lib/fileDocuments';
import {boundedFlush} from '@/lib/rendererFlush';
import type { RunRecord } from '@/lib/types';
import { useCollimateStore } from '@/stores/useCollimateStore';
import * as api from '@/lib/api';
const { getApiUrl } = api;
import { flushCellSaves } from '@/lib/cellSaveBarrier';
import { cloneLayout } from '@/lib/cellLayout';
import { cellHasLiveRun, isRunPath, isTerminalPhase, runRecordFrom } from '@/lib/runningCell';
import { formatLogLine } from '@/lib/runLogFormat';
import { touchesFromCommit } from '@/lib/annotations';
import { proxyWorkers, removeProxyWorkers } from '@/lib/proxyWorkers';

/**
 * Run-event bus: every run now multiplexes its persisted events through
 * the per-channel state SSE (as ``run_events`` / ``run_status`` messages),
 * instead of opening its own EventSource. That avoids burning an HTTP/1.1
 * connection slot per chat cell — opening a handful of chat cells used
 * to saturate the browser's ~6-per-origin cap and stall every subsequent
 * fetch. useChannelSSE fans inbound run_events batches out via these
 * registries to whichever attachToRun() call is tracking that run.
 */
type RunEventHandler = (events: RunEvent[]) => void;
type RunStatusHandler = (status: string) => void;
type RunPhaseHandler = (phase: string, message: string) => void;

export interface RunEvent {
    id: number;
    type: string;
    payload: string;
    created_at: string;
}

const runEventHandlers: Map<string, RunEventHandler> = new Map();
const runStatusHandlers: Map<string, RunStatusHandler> = new Map();
// Live phase handlers, parallel to runStatusHandlers. The cell's spinning-up
// vs running indicator is driven straight off these run_phase SSE messages
// (NOT off the replayed type=phase run_events, which only feed the log drawer)
// — see the clean-separation note in dispatchOne.
const runPhaseHandlers: Map<string, RunPhaseHandler> = new Map();
// Detach fn per runId so a re-attach (or the run's own terminal handler)
// tears down the PRIOR attach's event/status handlers instead of orphaning
// them when a caller ignores the returned detach.
const runDetachers: Map<string, () => void> = new Map();

export function dispatchRunEvents(runId: string, events: RunEvent[]): void {
    const handler = runEventHandlers.get(runId);
    if (handler && events.length) handler(events);
}

// Terminal failure detail per run (run_id → failure_reason). Event/status
// handlers capture it before endRun snapshots the reason into finished-run
// store records. Sources may be absent: run_status.failure_reason, a failed
// finish's error_message, or the timeout event's text.
const runFailureReasons = new Map<string, string>();

// Results outlive endRun while the run is live or in endedRuns, or its owning cell
// retains its last-finished outcome. Cache expiry/removal ends that ownership.
useCollimateStore.subscribe((state, previous) => {
    if (state.runs === previous.runs && state.endedRuns === previous.endedRuns
        && state.lastFinishedByCell === previous.lastFinishedByCell && state.cellCache === previous.cellCache
        && state.state === previous.state) return;
    if (!runFailureReasons.size) return;
    const kept = new Set([...Object.keys(state.runs), ...Object.keys(state.endedRuns)]);
    const cells = new Set(Object.keys(state.cellCache));
    for (const channel of state.state?.channels ?? []) {
        for (const stack of [...channel.nav_stacks, ...channel.main_stacks]) {
            for (const cell of stack.cells) cells.add(cell.id);
        }
    }
    for (const [cellId, result] of Object.entries(state.lastFinishedByCell)) {
        if (cells.has(cellId)) kept.add(result.runId);
    }
    for (const id of runFailureReasons.keys()) if (!kept.has(id)) runFailureReasons.delete(id);
});

export function dispatchRunStatus(runId: string, status: string, failureReason?: string): void {
    if (typeof failureReason === 'string' && failureReason) {
        runFailureReasons.set(runId, failureReason);
    }
    const handler = runStatusHandlers.get(runId);
    if (handler) handler(status);
}

export function dispatchRunPhase(runId: string, phase: string, message: string): void {
    const handler = runPhaseHandlers.get(runId);
    if (handler) handler(phase, message);
}

/**
 * Subscribe to a run's events (backfilled + live) and wire the run into
 * Zustand state: its record, its log (runLogs[runId]), phase, and terminal
 * status. Used by the run-start path (after POST /runs), the reload-recovery
 * path (after GET /runs/active) and the run_started broadcast.
 *
 * Returns a cleanup function; callers may ignore it (runs self-terminate
 * via the ``run_status`` broadcast), but tests and reload paths can call
 * it defensively.
 */
export function attachToRun(run: RunRecord): () => void {
    const { runId, channel } = run;
    const store = useCollimateStore.getState();
    store.startRun(run);

    const appendLog = (line: string) => useCollimateStore.getState().appendLog(runId, line);

    // Per-attachment state — closure-local so re-attaching the same run
    // never leaks the previous attempt's stop type or seen-event cache
    // into the new subscription.
    const perAttach = {
        lastStopType: null as string | null,
        // The backend appended a no_output event — a completed run that
        // produced zero commits labels "Completed · no output" at finish.
        sawNoOutput: false,
        seenEventIds: new Set<number>(),
        cursorFloor: 0,
        detached: false,
        backfillDone: false,
        liveQueue: [] as RunEvent[],
        // A run_status that arrived before the backfill finished — applied
        // once it has, so the finish line comes after the replayed events.
        pendingStatus: null as string | null,
        finished: false,
    };

    // Parse a persisted event JSON payload defensively — a malformed payload must
    // not throw out of dispatchOne and wedge the whole SSE handler.
    const safeParse = (payload: string): Record<string, unknown> => {
        try {
            const v = JSON.parse(payload);
            return v && typeof v === 'object' ? v : {};
        } catch {
            return {};
        }
    };

    // `sink` collects the lines instead of appending each one — the backfill
    // commits its lines in one store update.
    const dispatchOne = (evtData: RunEvent, sink?: string[]) => {
        if (evtData.id) {
            if (evtData.id <= perAttach.cursorFloor || perAttach.seenEventIds.has(evtData.id)) return;
            perAttach.seenEventIds.add(evtData.id);
            if (perAttach.seenEventIds.size > 5000) {
                const oldest = perAttach.seenEventIds.values().next().value!;
                perAttach.seenEventIds.delete(oldest);
                perAttach.cursorFloor = Math.max(perAttach.cursorFloor, oldest);
            }
        }

        // Type-specific side-effects (modals, stop tracking). The log LINE
        // for every type is rendered once, below, via the shared
        // formatLogLine — so the live log matches the persisted logs.txt
        // byte-for-byte.
        switch (evtData.type) {
            case 'stop':
                perAttach.lastStopType = (safeParse(evtData.payload).stop_type as string) || null;
                break;
            case 'no_output':
                perAttach.sawNoOutput = true;
                break;
            case 'finish': {
                // A failed finish's error_message is the failure_reason
                // fallback when the run_status broadcast doesn't carry one
                // (the broadcast, arriving later, overwrites this).
                const f = safeParse(evtData.payload);
                if (f.success === false && typeof f.error_message === 'string' && f.error_message) {
                    runFailureReasons.set(runId, f.error_message);
                }
                break;
            }
            case 'timeout': {
                // Wallclock-watchdog reason text (plain string payload).
                const reason = String(evtData.payload ?? '').trim();
                if (reason) runFailureReasons.set(runId, reason);
                break;
            }
            case 'commit': {
                // The commit's own file changes are the run's file touches
                // (the drilldown's timeline); its log line is unchanged.
                const p = safeParse(evtData.payload);
                if (Array.isArray(p.changes)) {
                    useCollimateStore.getState().addFileTouches(runId, touchesFromCommit(p, evtData.created_at));
                }
                break;
            }
            // phase, status, progress, stdout, stderr, memory, ipc_error: no
            // side-effect — they render purely as a log line below.
            // NB: the phase row is a log line ONLY; the run's live phase
            // INDICATOR is driven by the run_phase SSE (handlePhase), so a
            // replayed phase row on reattach can't overwrite the live phase.
        }

        // Single source of truth, allow-list enforced (unknown/internal ops
        // return null and are dropped — matching the backend so live ==
        // saved).
        const line = formatLogLine(evtData.type, evtData.payload, evtData.created_at);
        if (line !== null) {
            if (sink) sink.push(line);
            else appendLog(line);
        }
    };

    // Public handler that state-SSE routes into. Until the backfill
    // finishes, queue live events — that way dedup IDs from the backfill
    // are seeded before any overlapping live event is processed twice.
    const handleEvent = (events: RunEvent[]) => {
        if (!perAttach.backfillDone) {
            perAttach.liveQueue.push(...events);
            return;
        }
        const lines: string[] = [];
        for (const event of events) dispatchOne(event, lines);
        useCollimateStore.getState().appendLogs(runId, lines);
    };

    const detach = () => {
        if (perAttach.detached) return;
        perAttach.detached = true;
        runEventHandlers.delete(runId);
        runStatusHandlers.delete(runId);
        runPhaseHandlers.delete(runId);
        // Only clear the registry slot if it's still ours (a newer attach may
        // have replaced it).
        if (runDetachers.get(runId) === detach) runDetachers.delete(runId);
    };

    const handleStatus = (status: string) => {
        if (perAttach.finished) return;
        perAttach.finished = true;
        const stop = perAttach.lastStopType;
        let statusLabel: string;
        let finalStatus: string;
        if (status === 'completed' && !stop) {
            statusLabel = perAttach.sawNoOutput ? 'Completed · no output' : 'Completed';
            finalStatus = 'completed';
        } else if (status === 'timed_out') {
            // Wallclock watchdog killed the run. Checked before the stop
            // branches — the kill may have logged a stop event, but the
            // authoritative outcome is the timeout.
            statusLabel = 'Timed out';
            finalStatus = 'timed_out';
        } else if (status === 'interrupted') {
            // Crash recovery: the run was still 'running' when the server
            // restarted.
            statusLabel = 'Interrupted';
            finalStatus = 'interrupted';
        } else if (stop === 'force_stopped') {
            statusLabel = 'Stopped (forced)';
            finalStatus = 'force_stopped';
        } else if (stop === 'gracefully_stopped') {
            statusLabel = 'Stopped (graceful)';
            finalStatus = 'gracefully_stopped';
        } else if (status === 'failed') {
            statusLabel = 'Failed';
            finalStatus = 'failed';
        } else {
            statusLabel = status || 'Unknown';
            finalStatus = status || 'failed';
        }
        // The `RUN finished · <label> · <what ran>` bookend. Client-
        // fabricated for the LIVE log (not a persisted event); finalization bakes
        // its own equivalent into logs.txt from the runs row, so live and
        // persisted read the same.
        const finishedLine = formatLogLine(
            'run_finished',
            JSON.stringify({ status: statusLabel, runner: run.runnerId ?? undefined }),
            new Date().toISOString(),
        );
        if (finishedLine) appendLog(finishedLine);
        useCollimateStore.getState().endRun(runId, finalStatus, runFailureReasons.get(runId));
        // Resync the channel state once the run is over so the cell's name
        // and files are the server's final word (the host's folder sweep
        // included) — a state push that lost the timestamp race mid-run
        // would otherwise leave the cell showing pre-run content until the
        // next push. applyServerState keeps its newer-wins gate, so this
        // can't clobber a local edit.
        api.fetchState()
            .then(state => useCollimateStore.getState().applyServerState(state))
            .catch(() => { /* the next SSE push covers it */ });
        detach();
    };

    // Drive the run's live phase indicator. Fed ONLY by run_phase SSE (and the
    // backfill seed below) — never by replayed type=phase run_events — so a
    // stale replayed row can't overwrite the current live phase on reattach.
    const handlePhase = (phase: string, message: string) => {
        // The indicator only tracks active phases (spinning_up/running/…).
        // The end is endRun's (via handleStatus), which clears runPhases —
        // so ignore a terminal phase here to avoid re-adding an entry just
        // after endRun dropped it.
        if (isTerminalPhase(phase)) return;
        useCollimateStore.getState().setRunPhase(runId, phase, message);
    };

    // A status is held, like events, until the backfill is done.
    const onStatus = (status: string) => {
        if (!perAttach.backfillDone) {
            perAttach.pendingStatus = status;
            return;
        }
        handleStatus(status);
    };

    // Tear down any prior attach for this run before replacing its handlers,
    // so its handlers can't leak when a caller re-attaches without having
    // called the previous detach.
    runDetachers.get(runId)?.();
    runEventHandlers.set(runId, handleEvent);
    runStatusHandlers.set(runId, onStatus);
    runPhaseHandlers.set(runId, handlePhase);
    runDetachers.set(runId, detach);
    // The run's file touches so far, beside its events (deduplicated
    // against the commits the events replay).
    void useCollimateStore.getState().loadFileTouches(channel, runId);

    (async () => {
        try {
            const res = await fetch(
                `${getApiUrl()}/channels/${encodeURIComponent(channel)}/runs/${runId}/events_since?since_id=0`,
            );
            if (perAttach.detached) return;
            if (!res.ok) throw new Error(`events_since returned ${res.status}`);
            const data = await res.json() as {
                events: RunEvent[];
                status: string | null;
                phase: string | null;
                is_active: boolean;
                is_terminal: boolean;
                failure_reason?: string | null;
                last_event_id: number;
                truncated: boolean;
            };
            if (perAttach.detached) return;
            if (typeof data.failure_reason === 'string' && data.failure_reason) {
                runFailureReasons.set(runId, data.failure_reason);
            }
            // Seed the live phase indicator from the authoritative runs.phase
            // column before replaying events, so a reload mid-run recovers
            // spinning_up/running immediately (and isn't left to a possibly-
            // pruned phase event). Only for still-active runs — endRun clears
            // the phase for terminated ones.
            if (data.phase && !data.is_terminal) {
                useCollimateStore.getState().setRunPhase(runId, data.phase);
            }
            const lines: string[] = [];
            for (const evt of data.events) dispatchOne(evt, lines);
            if (Number.isSafeInteger(data.last_event_id)) {
                perAttach.cursorFloor = Math.max(perAttach.cursorFloor, data.last_event_id);
                perAttach.seenEventIds.clear();
            }
            // Release the live queue after backfill seeds the dedup set.
            perAttach.backfillDone = true;
            const queued = perAttach.liveQueue.splice(0);
            for (const evt of queued) dispatchOne(evt, lines);
            useCollimateStore.getState().appendLogs(runId, lines);
            // Live events from here on arrive through the per-channel SSE
            // (dispatchRunEvents / dispatchRunStatus / dispatchRunPhase).
            const terminal = perAttach.pendingStatus ?? (data.is_terminal ? data.status : null);
            if (terminal) handleStatus(terminal);
        } catch (err) {
            if (perAttach.detached) return;
            console.error('attachToRun backfill failed:', err);
            appendLog('[ERROR] Could not load run events — the server may be unreachable.');
            // The run's own status already arrived: finish with it.
            if (perAttach.pendingStatus) {
                perAttach.backfillDone = true;
                for (const evt of perAttach.liveQueue.splice(0)) dispatchOne(evt);
                handleStatus(perAttach.pendingStatus);
                return;
            }
            // End the run with an error status rather than silently removing
            // it from the active set.
            useCollimateStore.getState().endRun(runId, 'error', 'Could not load run events');
            detach();
        }
    })();

    return detach;
}

/** The `RUN started` bookend, in the canonical line format — the same line
 *  finalization bakes into logs.txt, so the live log and the saved one open
 *  identically. */
function startedLine(runnerId: string | null): string | null {
    return formatLogLine('run_started', JSON.stringify({ runner: runnerId ?? undefined }), new Date().toISOString());
}

export interface RunnerRunArgs {
    runner: string;
    runnerRevision?: string;
    /** The cell to run in — or, with `newCell`, the cell to clone first. */
    cellId: string;
    /** Run in a new cell: the server clones the cell's exact file state
     *  (minus `_runs/`) into a new stack to its right and runs there. */
    newCell?: boolean;
    /** The cell-relative path the run reads as `gen.selected_file`. */
    selectedFile?: string;
}

// Run starts this client has POSTed and not yet heard back from, by a
// client-minted token → the cell the POST targets. Several may be in
// flight for one cell (no busy guard: many runs may share a cell).
const inFlightStarts = new Map<string, string>();
let _startSeq = 0;

// Runs discovered (run_started, or active-run recovery) on a cell this
// client has a POST in flight for, by cell. A discovered run may be that
// POST's own (attached from the response, with this client's focus) or
// another start: each is replayed once the cell's last in-flight POST
// settles, and skipped then if it is already attached.
const deferredStarts = new Map<string, DiscoveredRun[]>();

const cellHasStartInFlight = (cellId: string): boolean =>
    [...inFlightStarts.values()].includes(cellId);

/** What a run's start shows: the first surface file the host created for
 *  it (`opens`) as a tab, else the run's own drilldown in the cell's status
 *  pane. The run card (`<label>.run`) is never opened: it is the run's
 *  record, not a user-facing view. */
function showStartedRun(run: RunRecord, opens: readonly string[] | undefined, focus: boolean): void {
    const store = useCollimateStore.getState();
    const surface = opens?.[0];
    if (surface) {
        store.queueReveal(run.cellId, { path: surface, runId: run.runId, activate: focus });
    } else if (focus) {
        store.openRunsPaneOn(run.cellId, { view: 'run', runId: run.runId, option: 'default' }, { focus: 'view', channel: run.channel });
    }
}

/** Kick off a runner run. The single client-side run entry point: saves
 *  every pending edit, POSTs the RunRequest and wires the run into Zustand
 *  via attachToRun (shared with the reload re-attach path and the
 *  run_started broadcast). The run's first surface opens focused in the
 *  run's cell, else its drilldown opens in the cell's status pane. With
 *  `newCell` the clone is selected
 *  and seeded with the source cell's tabs (minus the source's runs). */
export async function runRunner(args: RunnerRunArgs): Promise<void> {
    const store = useCollimateStore.getState();
    const { cellId } = args;
    const channel = store.activeChannel?.id ?? '';

    const token = `start-${++_startSeq}`;
    inFlightStarts.set(token, cellId);
    try {
// File operations use pinned bodies and conditional durable receipts.
        await boundedFlush(async()=>{await flushCellSaves(cellId);await flushFileDocuments();});

        const data = await api.postRun(channel, {
            runner: args.runner,
            runnerRevision: args.runnerRevision,
            cellId,
            newCell: args.newCell,
            selectedFile: args.selectedFile,
        });
        const run = runRecordFrom(data, channel);

        if (args.newCell) {
            // A genuinely NEW cell: select it (the camera follows) and give
            // it the source's tabs, minus the source's run files.
            const sourceLayout = useCollimateStore.getState().cellLayouts[cellId];
            if (sourceLayout && !useCollimateStore.getState().cellLayouts[run.cellId]) {
                useCollimateStore.getState().setCellLayout(
                    run.cellId,
                    cloneLayout(sourceLayout, tab => tab.kind === 'file' && isRunPath(tab.path)),
                );
            }
            useCollimateStore.getState().requestSelectCell(run.cellId);
        }
        // An in-place run deliberately does NOT select the cell: the user
        // is already there, and the camera must not move on every start.

        // The broadcast may have attached a new cell's run already (only an
        // in-place start defers it behind this POST).
        if (!Object.hasOwn(useCollimateStore.getState().runs, run.runId)) {
            const line = startedLine(run.runnerId);
            if (line) useCollimateStore.getState().appendLog(run.runId, line);
            attachToRun(run);
        }
        // This client started it: its surface focused, else its drilldown.
        showStartedRun(run, data.opens, true);
    } finally {
        inFlightStarts.delete(token);
        if (!cellHasStartInFlight(cellId)) {
            const deferred = deferredStarts.get(cellId) ?? [];
            deferredStarts.delete(cellId);
            for (const d of deferred) attachDiscoveredRun(d);
        }
    }
}

/** A run this client did not start itself: announced by the run_started
 *  broadcast, or found by recoverActiveRuns. `bookend` appends the
 *  `RUN started` line (a broadcast start is news as it happens); a
 *  recovered run's backfill replays its own trail. */
interface DiscoveredRun {
    run: RunRecord;
    bookend: boolean;
    /** Show the run's first surface, else its drilldown (a broadcast start,
     *  where the user is looking); a recovered run's tabs and pane are
     *  whatever the user left. */
    revealCard: boolean;
    /** The surfaces the host created at the start (run_started `opens`). */
    opens?: string[];
}

/** Attach a run discovered through a broadcast or reconnect. Already
 *  attached runs stay attached; a run on a cell this client is starting
 *  waits until its POST settles (see deferredStarts). */
function attachDiscoveredRun(d: DiscoveredRun): void {
    const store = useCollimateStore.getState();
    if (Object.hasOwn(store.runs, d.run.runId)) return;
    const { cellId } = d.run;
    if (cellHasStartInFlight(cellId)) {
        const list = deferredStarts.get(cellId) ?? [];
        deferredStarts.set(cellId, [...list.filter(x => x.run.runId !== d.run.runId), d]);
        return;
    }
    // Focus it only where the user is looking and nothing else runs
    // (read BEFORE attaching, which makes this run live on the cell).
    const focus = store.selectedCellId === cellId && !cellHasLiveRun(store, cellId);
    if (d.bookend) {
        const line = startedLine(d.run.runnerId);
        if (line) store.appendLog(d.run.runId, line);
    }
    attachToRun(d.run);
    if (d.revealCard) showStartedRun(d.run, d.opens, focus);
}

/** The run_started broadcast's payload (§ SSE run_started). */
export interface StartedRunInfo {
    run_id: string;
    cell_id: string;
    stack_id?: string;
    runner_id: string | null;
    runner_name: string | null;
    label: string;
    folder: string;
    generator_name: string;
    surfaces?: RunRecord['surfaces'];
    /** The surface files the host created at the start, first to reveal. */
    opens: string[];
}

/** The run_started broadcast: attach the announced run with its
 *  `RUN started` bookend and show its first surface (focused when the cell
 *  is the selected one and nothing else runs there, else in the
 *  background) or, with no surface, its drilldown where the user is
 *  looking. */
export function attachStartedRun(info: StartedRunInfo, channel: string): void {
    attachDiscoveredRun({ run: runRecordFrom(info, channel), bookend: true, revealCard: true, opens: info.opens });
}

/** Recover the channel's active runs from the server: re-seed every active
 *  run's record and non-terminal phase (even a run this client is attached
 *  to — a change broadcast while the channel's SSE was down is otherwise
 *  lost for the rest of the run), and attach each run this client is not
 *  attached to — a run being stopped (`stopping`) as stopping, and never
 *  one whose status is already final (one this client follows is ended
 *  with that status). Run on load and on every SSE (re)connect: a run whose
 *  run_started this client missed (a reload or a reconnect) is found here,
 *  and one started after the fetch arrives as run_started on the open
 *  stream. `isCurrent` drops a result that arrives after the caller moved
 *  on (a channel switch). */
export async function recoverActiveRuns(
    channel: string, isCurrent: () => boolean = () => true,
): Promise<void> {
    // Snapshot registrations before live runs. Enumerating after the fetch
    // could remove a new run's worker using an older list of live runs.
    const workers = await proxyWorkers(channel);
    const runs = await api.fetchActiveRuns(channel);
    if (!isCurrent()) return;
    const liveIds = new Set(runs.filter(info => !isTerminalPhase(info.phase ?? '')).map(info => info.run_id));
    void removeProxyWorkers(workers.filter(worker => !liveIds.has(worker.runId)));
    const store = useCollimateStore.getState();
    for (const info of runs) {
        const attached = Object.hasOwn(store.runs, info.run_id);
        // A run whose end is under way is never attached as live. One whose
        // status is already final (a terminal phase) is only finishing its
        // run-end cleanup and has sent its run_status, so it is not attached
        // at all; where this client follows it, that final status (which
        // the stream may have dropped) ends it now.
        if (info.stopping && info.phase && isTerminalPhase(info.phase)) {
            if (attached) dispatchRunStatus(info.run_id, info.phase);
            continue;
        }
        const run = runRecordFrom(info, channel);
        if (attached) store.setRun(run);
        if (info.phase && !isTerminalPhase(info.phase)) {
            store.setRunPhase(info.run_id, info.phase, info.phase_message ?? '');
        }
        attachDiscoveredRun({ run, bookend: false, revealCard: false });
        // A stop under way: attached as stopping; its final run_status
        // ends it.
        if (info.stopping && Object.hasOwn(useCollimateStore.getState().runs, info.run_id)) {
            store.startStopping(info.run_id);
        }
    }
}
