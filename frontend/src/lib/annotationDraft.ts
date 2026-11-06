// The status pane's composer edits an in-memory draft (lib/messageDraft)
// through the channel's default `.md` renderer, mounted by the ordinary
// renderer hosts under a file name that is never a cell file: it sits at the
// cell root, so the editor's links come out cell-relative, and every edit
// goes through the host's draft callbacks, so nothing is ever written.

/** The composer's file name, before any de-duplication. */
export const COMPOSER_FILENAME_BASE = '.collimate-message';

/** `.collimate-message.md`, or `-2`, `-3`… the first name the cell holds no
 *  file under. Computed once per composer mount. */
export function composerFilename(cellFiles: readonly string[]): string {
    const taken = new Set(cellFiles);
    for (let n = 1; ; n++) {
        const name = n === 1 ? `${COMPOSER_FILENAME_BASE}.md` : `${COMPOSER_FILENAME_BASE}-${n}.md`;
        if (!taken.has(name)) return name;
    }
}

/** Which composer a draft belongs to: a run's drilldown, or the cell's All
 *  view. */
export type DraftScope = { kind: 'run'; runId: string } | { kind: 'all' };

/** The store key of a composer's draft (`composerDrafts`). */
export function draftKey(cellId: string, scope: DraftScope): string {
    return scope.kind === 'run' ? `${cellId}:run:${scope.runId}` : `${cellId}:all`;
}

/** Whether a draft key is one of the cell's composers. */
export function isCellDraft(key: string, cellId: string): boolean {
    return key.startsWith(`${cellId}:`);
}
