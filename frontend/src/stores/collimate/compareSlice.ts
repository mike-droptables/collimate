import type { StateCreator } from 'zustand';
import type { CollimateState } from '../useCollimateStore';
import { type CellDiffs, channelHasCells } from '@/lib/cellDiff';


// The cell-pair diff. A file's "Compare with…" is a tab in the cell's
// layout instead (lib/cellLayout openCompareTab).
export interface CompareSlice {


  // ─── Cell-pair diff (self-contained slice) ──────────────────────────
  // CELL id → base CELL id: the cell renders read-only, each file diffed
  // against the base cell's same-named file (live — the base's current
  // files, wherever it sits). Entered from the scene's hover zone between
  // two columns (StackScene's CellDiffZone); several cells may diff at
  // once. Session-only (never persisted), cleared on a channel switch, and
  // an entry drops as soon as either cell leaves the active channel.
  cellDiffs: CellDiffs;

  /** Put `cellId` in diff against `baseCellId` (replacing any earlier
   *  base). A no-op for a cell diffing against itself, an unchanged pair,
   *  or a cell missing from the active channel. */
  enterCellDiff: (cellId: string, baseCellId: string) => void;

  exitCellDiff: (cellId: string) => void;
}

export const createCompareSlice: StateCreator<CollimateState, [], [], CompareSlice> = (set, get) => ({


    // ─── Cell-pair diff (self-contained slice) ──────────────────────────
    cellDiffs: {},

    enterCellDiff: (cellId, baseCellId) => {
      const prev = get();
      if (cellId === baseCellId || prev.cellDiffs[cellId] === baseCellId) return;
      if (!channelHasCells(prev.activeChannel, cellId, baseCellId)) return;
      set({ cellDiffs: { ...prev.cellDiffs, [cellId]: baseCellId } });
    },

    exitCellDiff: (cellId) => {
      const prev = get().cellDiffs;
      if (!(cellId in prev)) return;
      const next = { ...prev };
      delete next[cellId];
      set({ cellDiffs: next });
    },
});
