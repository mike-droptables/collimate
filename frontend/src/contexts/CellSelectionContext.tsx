/**
 * CellSelectionContext — "this cell is the selected cell".
 *
 * StackItem provides it around its CellEditor from `isSelectedCell` (the
 * selected stack's front cell, focused or not). Deep chrome reads it with
 * useCellSelected() to choose the selection accent: the open tab's rule
 * (TabStrip), the files rail's on-state (FilesPane) and the pin are brass in
 * the selected cell and teal elsewhere (C10, C12, C13). Brass is the
 * selection accent only, so nothing outside a provider reads as selected.
 */

import { createContext, useContext } from 'react';

export const CellSelectionContext = createContext(false);

export function useCellSelected(): boolean {
    return useContext(CellSelectionContext);
}
