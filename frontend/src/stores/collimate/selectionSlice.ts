import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';
import { defaultCellHistory } from './types';
import type { CellHistoryState } from './types';
import type { CapabilityKind } from '@/lib/types';

/** The steps a handler card opens on a reveal. */
export type HandlerStep = 'validate' | 'review' | 'addExample';

/** A one-shot request to show the Capabilities manage card (the stack's
 *  slot 0) and what it should show there. Any door may raise it (Commands,
 *  the stack menu, the Files panel's 'Show in Capabilities', the empty
 *  runner picker, revealHandler). StackApp shows the stack (restoring it
 *  when hidden), selects it and fronts the manage slot; the card applies the
 *  payload and clears it by `seq`. */
export interface CapabilitiesManageRequest {
  /** Minted by requestCapabilitiesManage; a newer request replaces an older one. */
  seq: number;
  /** Narrow the list to one kind. */
  kind?: CapabilityKind;
  /** Prefill the search. */
  query?: string;
  /** Scroll to, flash and unfold one handler's card (matched on cell and
   *  folder), opening `step` there. */
  handler?: { cellId: string; folder: string; step?: HandlerStep };
  /** Open the runner editor: a new runner (optionally for one generator,
   *  with a description carried over) or an existing one. */
  runner?: { action: 'add' | 'edit'; runnerId?: string; generatorId?: string; description?: string };
  /** Open the Create questionnaire. */
  create?: boolean;
  /** Open the 'Add from catalog' step. */
  addFromCatalog?: boolean;
}


export interface SelectionSlice {



  // The cell the user has selected on the canvas (the stack view's front
  // cell of the selected column), mirrored here by StackApp so non-React
  // code can ask "is this the selected cell" (a broadcast-started run's
  // card opens focused only there).
  selectedCellId: string | null;


  // Pending request to open the generator runner for a specific stack (replaces DOM event)
  pendingGeneratorRunnerStackId: string | null;


  // Cells whose Files pane is in history mode (the run-node rail), keyed
  // by cell id. Presence of a key = history mode; the content pane stays
  // live. Display-owned, so the selection survives editor
  // open/close and CellEditor remounts.
  cellHistory: Record<string, CellHistoryState>;


  // Cell id whose tab is expanded to the body-level fullscreen overlay
  // (CellEditor's maximize). The global Esc ladder consults it so a
  // capture-phase rung (e.g. exit-history) never fires while an overlay is
  // up — the overlay's own EDITOR_CLOSE collapses it first.
  maximizedOverlayCellId: string | null;


  // Pending request to focus on a freshly-created cell. The cell may
  // not exist in the channel state yet at request time (a Run in new
  // cell has just been POSTed, its clone lands a tick later via the
  // state push); the effect that consumes this watches for the cell's
  // stack to appear and then sets selection + bumps the camera.
  // Cleared once consumed. Lives in the store so any code path (run
  // dispatch, cell duplication, draft promotion) can request "focus my
  // new cell" without threading FocusContext through every call site.
  pendingSelectCellId: string | null;
  // Whether that request also takes the main focus area (a pick asks for it
  // through revealCellFile's `focus`), so the cell is the focused window.
  pendingSelectFocus: boolean;

  // The pending Capabilities manage request (see CapabilitiesManageRequest),
  // or null once the manage card has applied it.
  capabilitiesManageRequest: CapabilitiesManageRequest | null;

  setSelectedCellId: (cellId: string | null) => void;

  /** Enter version-history mode for a cell. Seeds {nodeId: null (track the
   *  current node until scrubbed), versionId: null, sectionsOpen: {config:
   *  false, files: true, logs: false}}. */
  openCellHistory: (cellId: string) => void;

  setMaximizedOverlayCellId: (cellId: string | null) => void;

  /** Exit version-history diff mode for a cell (deletes its entry). */
  closeCellHistory: (cellId: string) => void;

  /** Shallow-merge a patch into a cell's history state. */
  setCellHistory: (cellId: string, patch: Partial<CellHistoryState>) => void;

  requestOpenGeneratorRunner: (stackId: string) => void;

  clearGeneratorRunnerRequest: () => void;

  /** Ask StackApp to select the stack containing this cell as soon as
   *  that stack is in the channel state (at once if it already is). It never
   *  recentres the camera (owner, 2026-10-13): the camera's selection follow
   *  scrolls only as far as it takes to bring the stack into view. Used for
   *  a run in a new cell, a duplicate or restore, jumps to a run's cell and
   *  opening a file's tab in a cell. `opts.focus` also gives the main
   *  focus area to the canvas, so the cell becomes the focused window. */
  requestSelectCell: (cellId: string, opts?: { focus?: boolean }) => void;

  clearPendingSelectCell: () => void;

  /** Raise a Capabilities manage request (a fresh `seq` each time). */
  requestCapabilitiesManage: (request: Omit<CapabilitiesManageRequest, 'seq'>) => void;

  /** Drop the request the manage card just applied; a newer one (another
   *  `seq`) stays. */
  clearCapabilitiesManageRequest: (seq: number) => void;
}

let manageRequestSeq = 0;

export const createSelectionSlice: StateCreator<CollimateState, [], [], SelectionSlice> = (set, get) => ({

    selectedCellId: null,

    pendingGeneratorRunnerStackId: null,

    cellHistory: {},

    maximizedOverlayCellId: null,

    pendingSelectCellId: null,

    pendingSelectFocus: false,

    capabilitiesManageRequest: null,


    setSelectedCellId: (cellId) => {
      set((prev) => (prev.selectedCellId === cellId ? {} : { selectedCellId: cellId }));
    },


    setMaximizedOverlayCellId: (cellId: string | null) => {
      set((prev) => (prev.maximizedOverlayCellId === cellId ? {} : { maximizedOverlayCellId: cellId }));
    },


    openCellHistory: (cellId) => {
      set((prev) => ({
        cellHistory: {
          ...prev.cellHistory,
          [cellId]: defaultCellHistory(), // nodeId null = track the current node until scrubbed
        },
      }));
      get().saveCellDisplay(cellId);
    },


    closeCellHistory: (cellId) => {
      set((prev) => {
        if (!(cellId in prev.cellHistory)) return {};
        const next = { ...prev.cellHistory };
        delete next[cellId];
        return { cellHistory: next };
      });
      get().saveCellDisplay(cellId);
    },


    setCellHistory: (cellId, patch) => {
      set((prev) => {
        const existing = prev.cellHistory[cellId] ?? defaultCellHistory();
        return {
          cellHistory: { ...prev.cellHistory, [cellId]: { ...existing, ...patch } },
        };
      });
      get().saveCellDisplay(cellId);
    },


    requestOpenGeneratorRunner: (stackId: string) => {
      set({ pendingGeneratorRunnerStackId: stackId });
    },


    clearGeneratorRunnerRequest: () => {
      set({ pendingGeneratorRunnerStackId: null });
    },


    requestSelectCell: (cellId, opts) => {
      set({ pendingSelectCellId: cellId, pendingSelectFocus: !!opts?.focus });
    },


    clearPendingSelectCell: () => {
      set({ pendingSelectCellId: null, pendingSelectFocus: false });
    },


    requestCapabilitiesManage: (request) => {
      set({ capabilitiesManageRequest: { ...request, seq: ++manageRequestSeq } });
    },


    clearCapabilitiesManageRequest: (seq) => {
      set((prev) => (prev.capabilitiesManageRequest?.seq === seq ? { capabilitiesManageRequest: null } : {}));
    },
});
