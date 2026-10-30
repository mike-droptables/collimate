import { useEffect, useRef, useState } from 'react';
import type { ChannelEvent } from '@/generated/channelEvents';
import { getApiUrl, fetchState } from '@/lib/api';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { removeRunProxyWorkers } from '@/lib/proxyWorkers';
import { isTerminalPhase } from '@/lib/runningCell';
import { getFileDocument } from '@/lib/fileDocuments';
import { applyMutationResult, recoverFileOperations } from '@/lib/fileCommands';
import {
  attachStartedRun, dispatchRunEvents, dispatchRunPhase, dispatchRunStatus, recoverActiveRuns,
} from '@/lib/runAttach';

/** FILES_CHANGED for one cell's paths: the pooled iframes hosting the cell
 *  and the window broadcast (native renderers, the standalone frame,
 *  FullFileEditor). */
function deliverFilesChanged(broadcastToCell: (cellId: string, message: object) => void, cellId: string, changedFiles: string[], files: import('@/lib/fileProtocol').FileDescriptor[] = []) {
  broadcastToCell(cellId, { type: 'FILES_CHANGED', cellId, changedFiles, files });
  window.dispatchEvent(new CustomEvent(
    'collimate:files-changed-broadcast',
    { detail: { cellId, changedFiles, files } },
  ));
}

/**
 * Connects to the SSE state stream for a channel.
 *
 * - Applies revisioned state snapshots and keyed deltas
 * - Observes accepted descriptors on file_changes and notifies the cell
 *   views; document owners retain local edits and classify competing revisions
 * - Signals SSE connectivity to the store
 * - Recovers the channel's active runs on every connect (the first one
 *   after load included), reconnect and overflow
 * - Keeps every channel's live runs (the top bar's running count) fresh:
 *   refetched on every connect and on runs_active_changed, which the server
 *   sends on every channel's stream when any run starts or ends
 * - Keeps the overview's data fresh: lineage_changed rereads the shelf, the
 *   lineage and the named cells' journals; a run's start and end reread the
 *   journals that tell it; every (re)connect rereads whatever is loaded
 * - Keeps the status panes' annotation contexts fresh: annotations_changed
 *   refetches the context it names (a cell this session has viewed), and
 *   every (re)connect refetches the viewed cells' contexts
 */
export function useChannelSSE(channel: string | undefined) {
  const { broadcastToCell } = useGuestPool();
  const setSseConnected = useCollimateStore(s => s.setSseConnected);
  const applyStateEvent = useCollimateStore(s => s.applyStateEvent);
  // Bumped to rebuild the stream after the browser gives up on it.
  const [reconnect, setReconnect] = useState(0);
  const backoffRef = useRef(1000);


  useEffect(() => {
    if (!channel) return;
    let cancelled = false;
    let retired = false;
    let hydratingChannel: string | null = null;
    let retryTimer: number | undefined;
    let surfaceTimer: number | undefined;

    const apiBase = getApiUrl();
    const url = `${apiBase}/channels/${encodeURIComponent(channel)}/state/stream`;
    const source = new EventSource(url);

    // Active runs, re-read whenever this stream may have missed their
    // broadcasts: a run_started (another window's start), a run_phase
    // change. Fetched while the stream is open, so a run registered after
    // the fetch still arrives as run_started. Only once the store has
    // loaded; the stream is only created after that (the channel comes from
    // the loaded state), so the first onopen is the load's recovery.
    const recover = () => {
      if (!useCollimateStore.getState().hasInitialized) return;
      recoverActiveRuns(channel, () => !cancelled)
        .catch(err => console.error('active-run recovery failed:', err));
    };

    // Every channel's live runs. This stream may have missed a
    // runs_active_changed while it was down, so each (re)connect reads them.
    const refreshEverywhere = () => {
      useCollimateStore.getState().refreshActiveRunsEverywhere()
        .catch(err => console.error('active runs (every channel) fetch failed:', err));
    };

    const restartStream = () => {
      if (cancelled || retired) return;
      retired = true;
      source.close();
      useCollimateStore.getState().resetStateStream();
      setSseConnected(false);
      setReconnect(n => n + 1);
    };

    source.onopen = () => {
      if (cancelled || retired) return;
      useCollimateStore.getState().resetStateStream();
      backoffRef.current = 1000;
      setSseConnected(true);
      recover();
      void recoverFileOperations(channel).catch(error=>useCollimateStore.getState().openAlert({title:'File recovery needs attention',body:String(error?.message??error)}));
      refreshEverywhere();
      // The overview's shelf, lineage and journals may have missed a
      // lineage_changed or a run's end while the stream was down; the
      // status panes, an annotations_changed.
      useCollimateStore.getState().overviewResync();
      useCollimateStore.getState().resyncAnnotations();
    };

    source.onmessage = (event) => {
      if (cancelled || retired) return;
      try {
        const data: ChannelEvent = JSON.parse(event.data);

        // One case per SDK record (the union is generated from
        // channel_events.py): a record added there fails typecheck at the
        // `never` below until it is handled here.
        switch (data.type) {
          case 'connected':
            break;

          case 'surface_changed':
            broadcastToCell(data.cell_id, { type: 'SURFACE_CHANGED', cellId: data.cell_id, path: data.path });
            window.dispatchEvent(new CustomEvent('collimate:surface-changed', {
              detail: { channel, cellId: data.cell_id, path: data.path },
            }));
            window.clearTimeout(surfaceTimer);
            surfaceTimer = window.setTimeout(recover, 50);
            break;

          // Recovery is a fresh subscribed snapshot, never an unversioned
          // HTTP fetch racing later deltas on the interrupted stream.
          case 'overflow':
            restartStream();
            recover();
            break;

          case 'runs_active_changed':
            refreshEverywhere();
            break;

          case 'run_events':
            if (data.run_id && data.events) dispatchRunEvents(data.run_id, data.events);
            break;

          case 'annotations_changed':
            useCollimateStore.getState().annotationsChanged(data.cell_id, data.context_id, data.rev);
            break;

          case 'lineage_changed':
            useCollimateStore.getState().overviewLineageChanged(data.cell_ids);
            break;

          case 'run_status': {
            if (!data.run_id) break;
            if (isTerminalPhase(data.status || 'completed')) {
              void removeRunProxyWorkers(channel, data.run_id);
            }
            // The run's cell, read before its status ends the run's record.
            const store = useCollimateStore.getState();
            const cellId = store.runs[data.run_id]?.cellId ?? null;
            dispatchRunStatus(data.run_id, data.status || 'completed', data.failure_reason ?? undefined);
            // The journal tells how it ended (ended_at, note, outcome): reread.
            store.overviewRunChanged(data.run_id, cellId);
            break;
          }

          case 'run_phase':
            if (data.run_id) dispatchRunPhase(data.run_id, data.phase || 'running', data.message || '');
            break;

          // A run started on this channel — attach unless this client is
          // already attached or is starting it itself (attachStartedRun).
          case 'run_started':
            if (!(data.run_id && data.cell_id && data.folder)) break;
            attachStartedRun({
              run_id: data.run_id,
              cell_id: data.cell_id,
              stack_id: data.stack_id || undefined,
              runner_id: data.runner_id ?? null,
              runner_name: data.runner_name ?? null,
              label: data.label,
              folder: data.folder,
              generator_name: data.generator_name || '',
              opens: data.opens,
            }, channel);
            useCollimateStore.getState().overviewRunChanged(data.run_id, data.cell_id);
            break;

          // A run asked to show one of its files. The file itself may land
          // only with the next (coalesced) state push — the store's pending
          // reveals apply it once it does.
          case 'reveal_file':
            if (data.run_id && data.cell_id && data.path) {
              useCollimateStore.getState().queueReveal(data.cell_id, {
                path: data.path, runId: data.run_id,
              });
            }
            break;

          case 'state_snapshot':
          case 'state_delta': {
            if (!applyStateEvent(data)) {
              restartStream();
              break;
            }
            const store = useCollimateStore.getState();
            const snapshot = store._stateStreamSnapshot;
            // A renamed/deleted old scope can announce another active channel.
            // Its metadata is preserved until that channel's full baseline arrives.
            if (snapshot?.active_channel_id && snapshot.active_channel_id !== snapshot.channel_scope
                && hydratingChannel !== snapshot.active_channel_id) {
              hydratingChannel = snapshot.active_channel_id;
              const generation = store._stateGeneration;
              void fetchState().then(state => {
                if (!cancelled && !retired && useCollimateStore.getState()._stateGeneration === generation)
                  useCollimateStore.getState().applyServerState(state);
              }).catch(() => { hydratingChannel = null; });
            }
            break;
          }

          case 'file_changes':
            applyMutationResult(data.cell_id,data);
            for(const file of data.files){const owner=getFileDocument(channel,data.cell_id,file.path);if(owner){void owner.observe(file).catch(()=>undefined);owner.noteConflicts(data.conflicts,file);}}
            deliverFilesChanged(broadcastToCell,data.cell_id,data.files.map(file=>file.path),data.files);
            break;

          case 'dag_commit':
            useCollimateStore.getState().overviewRunChanged(data.run_id,data.cell_id);
            break;

          case 'shutdown':
            useCollimateStore.getState().setShuttingDown(true);
            break;

          default: {
            // A record this client does not know (a newer server) is ignored.
            const unhandled: never = data;
            void unhandled;
          }
        }
      } catch {
        // Ignore parse errors (keepalives, etc.)
      }
    };

    source.onerror = () => {
      if (cancelled || retired) return;
      setSseConnected(false);
      // CLOSED: the browser gave up (a non-200 or wrong Content-Type on
      // reconnect) and will not retry — rebuild the stream with backoff.
      if (!cancelled && source.readyState === 2) {
        const delay = backoffRef.current;
        backoffRef.current = Math.min(delay * 2, 30000);
        window.clearTimeout(retryTimer);
        retryTimer = window.setTimeout(() => setReconnect(n => n + 1), delay);
      }
    };

    return () => {
      cancelled = true;
      window.clearTimeout(retryTimer);
      window.clearTimeout(surfaceTimer);
      source.close();
      setSseConnected(false);
    };
  }, [channel, broadcastToCell, setSseConnected, applyStateEvent, reconnect]);
}
