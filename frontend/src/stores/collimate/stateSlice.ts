import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';
import { AppState, ScopedAppState, Channel, ChannelLayout, Cell, VirtualFile } from '@/lib/types';
import type { StateDelta, StateSnapshot } from '@/generated/channelEvents';
import * as api from '@/lib/api';
import { capabilitiesSourceFingerprint } from './types';
import { reconcileAppState, reconcileChannel, mergeScopedState, applyStatePatches } from './reconcile';
import { pruneCellDiffs } from '@/lib/cellDiff';
import { observeCellDocuments } from '@/lib/fileDocuments';

/** The channel stream's state records: a scoped baseline, or keyed patches to it. */
export type StateEvent = StateSnapshot | StateDelta;

/** Per-file revisions alone determine whether an accepted descriptor is newer. */
function mergeCellFiles(_cellId:string,cached:VirtualFile[],incoming:VirtualFile[]):VirtualFile[]{
  const old=new Map(cached.map(file=>[file.name,file]));
  const files=incoming.map(file=>{
    const previous=old.get(file.name);
    return previous && previous.file_revision>=file.file_revision?previous:file;
  });
  return files.length===cached.length && files.every((file,index)=>file===cached[index])?cached:files;
}

export interface StateSlice {

  // Raw state from server (layout + entities combined for now)
  state: AppState | null;

  activeChannel: Channel | null;

  hasInitialized: boolean;

  apiError: string | null;

  // True between receiving a `shutdown` SSE event and the window
  // actually closing — drives the ShutdownOverlay.
  isShuttingDown: boolean;

  setShuttingDown: (v: boolean) => void;


  // Entity cache — normalized cells by ID, fed by SSE
  cellCache: Record<string, Cell>;


  // Whether SSE is connected (reduces polling frequency)
  sseConnected: boolean;


  // Fingerprint of the Capabilities stack's cells (capabilitiesSourceFingerprint):
  // the renderer inventory, the runner catalog (useRunnerCatalog) and the
  // manage view refetch when a capability cell is added, moved on or off,
  // or edited, without a channel switch or refresh.
  _capabilitiesFingerprint: string;


  // Actions
  fetchState: () => Promise<void>;

  _stateStreamSnapshot: ScopedAppState | null;

  _stateStreamRevision: string | null;

  _stateGeneration: number;

  resetStateStream: () => void;

  applyStateEvent: (event: StateEvent) => boolean;

  applyServerState: (data: AppState | ScopedAppState, stream?: { snapshot: ScopedAppState; revision: string }) => void;

  applyOptimisticChannel: (channel: Channel) => void;

  applyOptimisticLayout: (channel: ChannelLayout) => void;

  patchCell: (cellId: string, patch: Partial<Cell>) => void;

  setSseConnected: (connected: boolean) => void;
}

export const createStateSlice: StateCreator<CollimateState, [], [], StateSlice> = (set, get) => ({

    state: null,

    _stateStreamSnapshot: null,

    _stateStreamRevision: null,

    _stateGeneration: 0,

    resetStateStream: () => set({ _stateStreamSnapshot: null, _stateStreamRevision: null }),

    applyStateEvent: (event: StateEvent) => {
      const current = get();
      if (event.revision === current._stateStreamRevision) return true;
      let snapshot: ScopedAppState;
      try {
        if (event.type === 'state_snapshot') snapshot = event.state;
        else {
          if (!current._stateStreamSnapshot || event.base_revision !== current._stateStreamRevision
              || event.channel_scope !== current._stateStreamSnapshot.channel_scope) return false;
          snapshot = applyStatePatches(current._stateStreamSnapshot, event.patches);
        }
        if (!snapshot || !Array.isArray(snapshot.channels) || typeof event.revision !== 'string') return false;
        current.applyServerState(snapshot, { snapshot, revision: event.revision });
        return true;
      } catch {
        return false;
      }
    },

    activeChannel: null,

    hasInitialized: false,

    apiError: null,

    isShuttingDown: false,

    setShuttingDown: (v: boolean) => set({ isShuttingDown: v }),

    cellCache: {},

    sseConnected: false,

    _capabilitiesFingerprint: '',


    fetchState: async () => {
      const generation = get()._stateGeneration;
      try {
        const data = await api.fetchState();
        if (get()._stateGeneration === generation) get().applyServerState(data);
      } catch (e) {
        if (get()._stateGeneration !== generation) return;
        set({
          apiError: (e instanceof Error && e.message) || 'Failed to connect to backend',
          hasInitialized: true,
        });
      }
    },


    applyServerState: (incoming: AppState | ScopedAppState, stream) => {
      const data = mergeScopedState(get().state, incoming);
      // Extract all cells into the entity cache
      const prev = get();
      const cellCache = { ...prev.cellCache };
      let cellCacheChanged = false;
      for(const ch of data.channels){
        for(const stacks of [ch.main_stacks,ch.nav_stacks])for(const stack of stacks)for(const cell of stack.cells){
          const existing=cellCache[cell.id];
          const merged=existing?.files?{...cell,files:mergeCellFiles(cell.id,existing.files,cell.files)}:cell;
          cellCache[cell.id]=merged;cellCacheChanged=true;
          observeCellDocuments(ch.id,cell.id,cell.files);
        }
      }

      // Structural sharing: reuse previous object identities for unchanged
      // channels/stacks/cells so downstream memos (StackGroup/StackItem
      // comparators, StackScene's callback map) hold across SSE pushes —
      // without this, every run-progress tick re-rendered the whole scene.
      // activeChannel is picked from the reconciled tree so its identity
      // matches state.channels.
      const nextState = reconcileAppState(prev.state, data);

      let activeChannel: Channel | null = null;
      if (nextState.active_channel_id) {
        activeChannel =
          nextState.channels.find((c) => c.id === nextState.active_channel_id) ||
          (nextState.channels.length > 0 ? nextState.channels[0] : null);
      } else if (nextState.channels.length > 0) {
        activeChannel = nextState.channels[0];
      }


      // A channel switch, by a manual select or a server-initiated move
      // through applyServerState, drops the channel-scoped client state.
      const prevActiveId = get().activeChannel?.id;
      const nextActiveId = activeChannel?.id;
      const channelSwitched = prevActiveId !== nextActiveId;

      set({
        state: nextState,
        _stateGeneration: prev._stateGeneration + 1,
        _stateStreamSnapshot: stream?.snapshot ?? null,
        _stateStreamRevision: stream?.revision ?? null,
        activeChannel,
        cellCache: cellCacheChanged ? cellCache : prev.cellCache,
        hasInitialized: true,
        apiError: null,
        _capabilitiesFingerprint: capabilitiesSourceFingerprint(activeChannel),
        // Pick sessions are channel-scoped (cell ids belong to the
        // channel); useAppState re-hydrates the new channel's map.
        ...(channelSwitched ? { pickSessions: {}, cellLayouts:{}, cellHistory:{}, cellDisplaysReady:false, cellDisplayLoadError:null, cellDisplayErrors:{}, _cellDisplayEdits:{}, _cellDisplayValues:{}, _cellDisplayFetch:prev._cellDisplayFetch+1 } : {}),
        // Cell diffs never cross a channel switch; in-channel pushes drop
        // any pair whose cell or base cell is gone.
        cellDiffs: channelSwitched ? {} : pruneCellDiffs(prev.cellDiffs, activeChannel),
      });
    },


    applyOptimisticLayout: (layout: ChannelLayout) => {
      const current = get();
      const previous = current.state?.channels.find(channel => channel.id === layout.id);
      const priorCells = new Map([...(previous?.main_stacks ?? []), ...(previous?.nav_stacks ?? [])]
        .flatMap(stack => stack.cells.map(cell => [cell.id, cell] as const)));
      const stacks = (items: ChannelLayout['main_stacks']) => items.map(stack => ({
        ...stack,
        cells: stack.cells.map(cell => ({ ...cell, files: current.cellCache[cell.id]?.files ?? priorCells.get(cell.id)?.files ?? [] })),
      }));
      current.applyOptimisticChannel({ ...layout, main_stacks: stacks(layout.main_stacks), nav_stacks: stacks(layout.nav_stacks) });
    },


    applyOptimisticChannel: (channel: Channel) => {
      // Update layout optimistically without waiting for server

      set((prev) => {
        if (!prev.state) return {};

        // Update cell cache with any cells from this channel.
        const cellCache = { ...prev.cellCache };
        let cellCacheChanged = false;
          for (const stacks of [channel.main_stacks, channel.nav_stacks]) {
          for (const stack of stacks) {
            for (const cell of stack.cells) {
              const existing = cellCache[cell.id];
              if (!existing) {
                cellCache[cell.id] = cell;
                cellCacheChanged = true;
                continue;
              }
              // Optimistic replies contain accepted file metadata, like
              // state pushes. Document owners retain local editor bytes.
              cellCache[cell.id] = existing.files && Array.isArray(cell.files)
                ? { ...cell, files: mergeCellFiles(cell.id, existing.files, cell.files) }
                : cell;
              cellCacheChanged = true;
            }
          }
        }

        // Structural sharing — same reasoning as applyServerState: a PUT
        // response echoes the whole channel, but usually only one cell
        // actually changed. Reconcile against the previous channel so
        // untouched stacks/cells keep their identities (and, when nothing
        // changed at all, the state/activeChannel references are stable and
        // zustand's Object.is bail skips the re-render entirely).
        const prevChannel = prev.state.channels.find((c) => c.id === channel.id);
        const nextChannel = reconcileChannel(prevChannel, channel);
        const newChannels = nextChannel === prevChannel
          ? prev.state.channels
          : prev.state.channels.map((c) => (c.id === channel.id ? nextChannel : c));
        const newState = newChannels === prev.state.channels
          ? prev.state
          : { ...prev.state, channels: newChannels };
        const newActiveChannel =
          prev.activeChannel?.id === channel.id
            ? nextChannel
            : prev.activeChannel;

        return {
          state: newState,
          activeChannel: newActiveChannel,
          cellDiffs: pruneCellDiffs(prev.cellDiffs, newActiveChannel),
          cellCache: cellCacheChanged ? cellCache : prev.cellCache,
          _capabilitiesFingerprint: capabilitiesSourceFingerprint(newActiveChannel),
        };
      });
    },


    patchCell: (cellId: string, patch: Partial<Cell>) => {
      set((prev) => {
        const existing = prev.cellCache[cellId] || ({} as Cell);
        // Don't bump updated_at here: a client wall-clock stamp would
        // beat the server's older microsecond stamp on the next
        // state push (applyServerState's serverMs >= existingMs gate)
        // and drop a genuine run commit. Callers that need the gate
        // (applyCellFiles) bump it explicitly.
        return {
          cellCache: {
            ...prev.cellCache,
            [cellId]: {
              ...existing,
              ...patch,
            },
          },
        };
      });
    },


    setSseConnected: (connected: boolean) => {
      set({ sseConnected: connected });
    },
});
