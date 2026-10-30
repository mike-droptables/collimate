import type { useCollimateStore } from '../useCollimateStore';
import type { PendingReveal } from './types';
import { ENDED_RUN_LOG_MS, REVEAL_TTL_MS } from './runSlice';

/** Own the single run expiry timer and reveal drain for the composed store. */
export function createRunLifecycle(getStore: () => typeof useCollimateStore) {
// Age-out timer for endedRuns: one timer, armed for the oldest
// entry's expiry, so ended runs (and their logs) leave after
// ENDED_RUN_LOG_MS even when no later run ends.
let _endedPruneTimer: ReturnType<typeof setTimeout> | null = null;
function scheduleEndedPrune(): void {
  if (_endedPruneTimer !== null) clearTimeout(_endedPruneTimer);
  _endedPruneTimer = null;
  const recs = Object.values(getStore().getState().endedRuns);
  if (recs.length === 0) return;
  const oldest = Math.min(...recs.map((r) => r.endedAt));
  const delay = Math.max(0, oldest + ENDED_RUN_LOG_MS - Date.now() + 1);
  _endedPruneTimer = setTimeout(() => {
    _endedPruneTimer = null;
    getStore().getState().pruneEndedRuns();
  }, delay);
}

// Pending reveals: a run's reveal_file (or a surface file on start) arrives
// BEFORE the state push that carries the file (state push is coalesced).
// Apply each queued entry as soon as its path is in the cell's files, drop
// it after REVEAL_TTL_MS; endRun drops a finished run's entries. Module-
// level: runs once when the store is first imported.
let _drainingReveals = false;
function _drainPendingReveals(): void {
  if (_drainingReveals) return;
  const s = getStore().getState();
  if (Object.keys(s.pendingReveals).length === 0) return;
  _drainingReveals = true;
  try {
    const now = Date.now();
    const next: Record<string, PendingReveal[]> = {};
    let changed = false;
    for (const [cellId, list] of Object.entries(s.pendingReveals)) {
      const kept: PendingReveal[] = [];
      for (const e of list) {
        if (now - e.at > REVEAL_TTL_MS) { changed = true; continue; }
        if (s.applyReveal(cellId, e.path, e.runId, e.activate)) { changed = true; continue; }
        kept.push(e);
      }
      if (kept.length) next[cellId] = kept;
    }
    if (changed) getStore().setState({ pendingReveals: next });
  } finally {
    _drainingReveals = false;
  }
}


function subscribe() {
getStore().subscribe((state, prev) => {
  if (Object.keys(state.pendingReveals).length === 0) return;
  if (state.pendingReveals === prev.pendingReveals
      && state.cellCache === prev.cellCache
      && state.state === prev.state) return;
  _drainPendingReveals();
});

}
return { scheduleEndedPrune, drainPendingReveals: _drainPendingReveals, subscribe };
}
