/** The MIME type of a drag the files pane starts, so internal cell-to-cell
 *  drags are distinguishable from OS-file drops. Its value is the JSON
 *  `{cellId, paths}` (the dragged files' cell-relative paths), readable at
 *  drop time only: a renderer in the same cell (the SDK's CELL_FILES_MIME,
 *  the same string) inserts the paths where they land. The host's own drops
 *  read the richer payload from the store (`fileDrag`), because getData()
 *  is unreadable during dragover; there only the type is visible. */
export const COLLIMATE_DND_MIME = 'application/x-collimate-files';

/** Whether a drag carries files from the OS (the native 'Files' type). No
 *  drag the app starts itself (cell files, tabs, stacks) does. */
export const carriesOsFiles = (dt: { types: ArrayLike<string> } | null | undefined): boolean =>
    !!dt?.types && Array.from(dt.types).includes('Files');
