import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';
import { createCellCacheGc } from './collimate/cellCacheGc';
import { createRunSlice, type RunSlice } from './collimate/runSlice';
import { createRunLifecycle } from './collimate/runLifecycle';
import { createStateSlice, type StateSlice } from './collimate/stateSlice';
import { createSelectionSlice, type SelectionSlice } from './collimate/selectionSlice';
import { createPickSessionSlice, type PickSessionSlice } from './collimate/pickSessionSlice';
import { createLayoutSlice, type LayoutSlice } from './collimate/layoutSlice';
import { createFileTransferSlice, type FileTransferSlice } from './collimate/fileTransferSlice';
import { createDialogSlice, type DialogSlice } from './collimate/dialogSlice';
import { createCompareSlice, type CompareSlice } from './collimate/compareSlice';
import { createCommandPaneSlice, type CommandPaneSlice } from './collimate/commandPaneSlice';
import { createOverviewSlice, type OverviewSlice } from './collimate/overviewSlice';
import { createAnnotationSlice, type AnnotationSlice } from './collimate/annotationSlice';
import { createAnnotationLifecycle } from './collimate/annotationLifecycle';

export interface CollimateState extends RunSlice, StateSlice, SelectionSlice, PickSessionSlice, LayoutSlice, FileTransferSlice, DialogSlice, CompareSlice, CommandPaneSlice, OverviewSlice, AnnotationSlice {}

const runLifecycle = createRunLifecycle(() => useCollimateStore);
const annotationLifecycle = createAnnotationLifecycle(() => useCollimateStore);

export const useCollimateStore = create<CollimateState>()(
  subscribeWithSelector((set, get, store) => ({
    ...createRunSlice(runLifecycle.scheduleEndedPrune, runLifecycle.drainPendingReveals)(set, get, store),
    ...createStateSlice(set, get, store),
    ...createSelectionSlice(set, get, store),
    ...createPickSessionSlice(set, get, store),
    ...createLayoutSlice(set, get, store),
    ...createFileTransferSlice(set, get, store),
    ...createDialogSlice(set, get, store),
    ...createCompareSlice(set, get, store),
    ...createCommandPaneSlice(set, get, store),
    ...createOverviewSlice(set, get, store),
    ...createAnnotationSlice(set, get, store),
  }))
);

const pruneCellCache = createCellCacheGc(
  () => useCollimateStore.getState(),
  cache => useCollimateStore.setState({cellCache: cache}),
);
useCollimateStore.subscribe((state, prev) => {
  if (state.state !== prev.state || state.runs !== prev.runs) pruneCellCache();
});

runLifecycle.subscribe();
annotationLifecycle.subscribe();

// Debug hook — expose the store on window so it's inspectable from the
// browser console. Useful for diagnosing live state when the UI seems
// to disagree with the data. Dev builds only: production pages should
// not hand third-party scripts a mutable handle to app state.
declare global {
  interface Window { __store?: typeof useCollimateStore }
}
if (import.meta.env.DEV && typeof window !== 'undefined') {
  window.__store = useCollimateStore;
}
