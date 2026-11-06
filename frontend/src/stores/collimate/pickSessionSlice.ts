import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';
import type { PickKind, PickSession } from '@/lib/types';
import * as api from '@/lib/api';

// Debounced write-through for cell panel sessions: one timer per channel and cell so a
// fast-typing user PUTs at most a couple of times a second per panel.
// Fire-and-forget — the in-memory store is the live source of truth; the
// channel-KV write only has to win eventually (it exists for refresh
// restore). The channel UUID is captured at schedule time so a
// channel switch can't misroute a late flush.
const _pickSessionTimers: Record<string, ReturnType<typeof setTimeout>> = {};

const PICK_SESSION_DEBOUNCE_MS = 400;

// When each cell's session was closed HERE. A hydrate that read the server
// before that DELETE landed (a CellEditor mounting mid-flight re-hydrates —
// the clone a run in a new cell makes, while the runners panel's own close
// is still behind the run POST) must not bring the closed session back.
const _pickSessionClosedAt: Record<string, number> = {};

// Pick-session writes (PUT / DELETE) for one channel+cell go out one at a
// time, in call order, so a close can't be overtaken by an earlier save and
// the session come back after a reload.
const _pickSessionWrites: Record<string, Promise<unknown>> = {};

const sessionKey = (channel: string, cellId: string) => `${channel}\u0000${cellId}`;

function _enqueuePickSessionWrite(channel: string, cellId: string, write: () => Promise<unknown>) {
    const key = sessionKey(channel, cellId);
    const next = (_pickSessionWrites[key] ?? Promise.resolve())
        .then(write)
        .catch(() => { /* best effort */ });
    _pickSessionWrites[key] = next;
    void next.finally(() => { if (_pickSessionWrites[key] === next) delete _pickSessionWrites[key]; });
}

function _schedulePickSessionSave(channel: string, cellId: string, session: PickSession) {
    const key = sessionKey(channel, cellId);
    clearTimeout(_pickSessionTimers[key]);
    _pickSessionTimers[key] = setTimeout(() => {
        delete _pickSessionTimers[key];
        _enqueuePickSessionWrite(channel, cellId, () => api.savePickSession(channel, cellId, session));
    }, PICK_SESSION_DEBOUNCE_MS);
}

export interface PickSessionSlice {

  // Cell panel sessions, keyed by cell id: the panel over a cell's body
  // (components/cellPanel), one per cell and of one kind. Several cells
  // can hold sessions at once. Hydrated per channel from the channel DB's
  // ui_state KV (see useAppState) and written through on every edit
  // (debounced), so a session survives zoom out/in, offscreen unmounts and
  // browser refresh. Never serialized into .coll* bundles and never part
  // of the DAG: a session dies when its panel closes or its run starts.
  pickSessions: Record<string, PickSession>;

  /** Merge the server's session map into the store (channel hydration).
   *  Per cell, the newer `at` wins so a session opened before hydration
   *  lands isn't clobbered. A server entry that names no known panel kind
   *  is not a session and is left out. */
  hydratePickSessions: (channel: string, sessions: Record<string, PickSession>) => void;

  /** Open a cell's panel of `kind` with an empty query, replacing any
   *  session the cell holds (one panel per cell). Saves through to the
   *  channel KV. A no-op while the cell is in version history: the panels
   *  never cover history, so a session opened there would be invisible. */
  openPickSession: (cellId: string, kind: PickKind) => void;

  /** Shallow-merge a patch into the cell's session of `kind`; bumps `at`
   *  and write-through-saves (debounced). A no-op when the cell holds no
   *  session of that kind, so a panel that was replaced never writes into
   *  its successor. */
  updatePickSession: (cellId: string, kind: PickKind, patch: Partial<Pick<PickSession, 'query'>>) => void;

  /** Drop the cell's session of `kind` (Close, Esc, the scrim, a run
   *  started) and delete it from the channel KV. A no-op when the cell's
   *  session is of another kind. */
  closePickSession: (cellId: string, kind: PickKind) => void;
}

const PICK_KINDS: ReadonlySet<string> = new Set<PickKind>(['runners', 'files', 'transfer']);

export const createPickSessionSlice: StateCreator<CollimateState, [], [], PickSessionSlice> = (set, get) => ({

    pickSessions: {},


    hydratePickSessions: (channel, sessions) => {
      if (get().activeChannel?.id !== channel) return;
      const local = get().pickSessions;
      const merged: Record<string, PickSession> = {};
      for (const [cellId, remote] of Object.entries(sessions)) {
        if (PICK_KINDS.has(remote?.kind)) merged[cellId] = remote;
      }
      for (const cellId of Object.keys(merged)) {
        const closedAt = _pickSessionClosedAt[sessionKey(channel, cellId)];
        const remote = merged[cellId];
        if (remote && remote.at < closedAt) delete merged[cellId];
      }
      for (const [cellId, s] of Object.entries(local)) {
        const remote = merged[cellId];
        if (!remote || s.at >= remote.at) merged[cellId] = s;
      }
      set({ pickSessions: merged });
    },


    openPickSession: (cellId, kind) => {
      const prev = get();
      // No panel over version history: the cell editor hides panels there.
      // Leave history from the Files pane's
      // Tree / Folder tabs first.
      if (prev.cellHistory[cellId]) return;
      const key = sessionKey(prev.activeChannel?.id ?? '', cellId);
      delete _pickSessionClosedAt[key];
      clearTimeout(_pickSessionTimers[key]);
      delete _pickSessionTimers[key];
      const session: PickSession = { kind, query: '', at: Date.now() };
      set({ pickSessions: { ...prev.pickSessions, [cellId]: session } });
      const channel = prev.activeChannel?.id;
      if (channel) _enqueuePickSessionWrite(channel, cellId, () => api.savePickSession(channel, cellId, session));
    },


    updatePickSession: (cellId, kind, patch) => {
      const prev = get();
      const cur = prev.pickSessions[cellId];
      if (cur?.kind !== kind) return;
      const next: PickSession = { ...cur, ...patch, at: Date.now() };
      set({ pickSessions: { ...prev.pickSessions, [cellId]: next } });
      const channel = prev.activeChannel?.id;
      if (channel) _schedulePickSessionSave(channel, cellId, next);
    },


    closePickSession: (cellId, kind) => {
      const prev = get();
      if (prev.pickSessions[cellId]?.kind !== kind) return;
      const key = sessionKey(prev.activeChannel?.id ?? '', cellId);
      clearTimeout(_pickSessionTimers[key]);
      delete _pickSessionTimers[key];
      _pickSessionClosedAt[key] = Date.now();
      const next = { ...prev.pickSessions };
      delete next[cellId];
      set({ pickSessions: next });
      const channel = prev.activeChannel?.id;
      if (channel) _enqueuePickSessionWrite(channel, cellId, () => api.deletePickSession(channel, cellId));
    },
});
