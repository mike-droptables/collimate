import { activeTab } from '@/lib/cellLayout';
import { isRunPath } from '@/lib/runningCell';
import type { CellLayout } from '@/stores/collimate/types';

/** The cell's selected file — what a run that takes one reads as
 *  `gen.selected_file`: the active tab when it shows a cell file, else the
 *  Files pane's selection when it is a file, else none. Run output
 *  (`_runs/…`) is never the selection. */
export function selectedFileOf(layout: CellLayout | null | undefined): string | null {
    if (!layout) return null;
    const tab = activeTab(layout);
    if (tab?.kind === 'file' && tab.path && !isRunPath(tab.path)) return tab.path;
    const selection = layout.filesPane.selection;
    if (selection && !selection.isFolder && selection.nodePath && !isRunPath(selection.nodePath)) {
        return selection.nodePath;
    }
    return null;
}
