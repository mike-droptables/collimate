import { createDefaultLayout, openFileTab, resolvePinnedPath } from '@/lib/cellLayout';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { HandlerStep } from '@/stores/collimate/selectionSlice';

/**
 * Reveal a file that lives in a cell ON A USER GESTURE: select the cell and
 * open (or focus) a content tab showing that file in its renderer. Selection
 * never recentres the camera (requestSelectCell).
 *
 * Behind `api.revealFile` in every host (NativeRenderer calls it directly,
 * the iframe hosts route their renderers' REVEAL_FILE postMessage here), the
 * run light's overlay rows and the top status's file rows. The file must
 * already exist on the cell's file list; an unresolvable cell or path is a
 * silent no-op, so a caller can wire it to user clicks without
 * existence-checking first.
 *
 * Tab semantics are openFileTab's (lib/cellLayout.ts): one file = at most one
 * tab per cell, so a file already open is focused rather than re-opened; a
 * non-pinned active tab is taken over in place unless `opts.newTab` (the
 * overlay's rows pass it, so a pick never replaces another run's tab). A
 * cell whose editor has never mounted has no layout yet, so a bare default
 * is seeded here — CellEditor's reconcile fills in the pinned tab on mount.
 *
 * `opts.focus` also takes the main focus area (requestSelectCell's `focus`,
 * applied by StackApp's pending-select consumer), so the cell is the focused
 * window and its view takes pointer input at once: an annotation pick asks for
 * it (lib/rendererAnnotation), since an unfocused view's focus blocker would
 * eat the pick's click. It still never forces keyboard focus into the editor
 * (setMainFocusMode lives in the React-only FocusContext, out of reach of
 * non-React callers); the file content is shown regardless.
 *
 * A RUN's reveal (reveal_file, its run card) never comes here: it goes
 * through the store's applyReveal (useCollimateStore; focus rule in
 * lib/cellLayout revealFileTab), which never selects the cell.
 */
export function revealCellFile(
    cellId: string | undefined,
    path: string,
    opts?: { newTab?: boolean; focus?: boolean },
): void {
    if (!cellId || !path) return;
    const store = useCollimateStore.getState();
    const cell = store.locateCell(cellId)?.cell;
    const files = cell?.files ?? [];
    if (!files.some((f) => f.name === path)) return;
    store.requestSelectCell(cellId, opts?.focus ? { focus: true } : undefined);
    // Pinned-ness is derived from the cell's preview pointer, and only counts
    // when it still resolves to a file (a dangling pointer pins nothing).
    const pinnedPath = resolvePinnedPath(cell?.pinned, files);
    store.updateCellLayout(
        cellId,
        (layout) => openFileTab(layout, path, pinnedPath, undefined, opts?.newTab ? { newTab: true } : undefined),
        () => createDefaultLayout({ filePaths: files.map((f) => f.name), pinnedPath }),
    );
}

/**
 * Show a handler's card ON A USER GESTURE: raise a Capabilities manage
 * request, so the manage card (restored first when Capabilities is hidden)
 * narrows to Handlers, scrolls to the card held at (cellId, folder),
 * flashes it and opens `step` there.
 */
export function revealHandler(cellId: string, folder: string, step?: HandlerStep): void {
    useCollimateStore.getState().requestCapabilitiesManage({
        kind: 'handler',
        handler: { cellId, folder, ...(step ? { step } : {}) },
    });
}
