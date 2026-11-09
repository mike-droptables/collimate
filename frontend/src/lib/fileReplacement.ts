// The file view's side of a run's deliberate replacement: when the revision
// an editor shows is the one a run wrote over a newer save by naming it
// (`replace_revision`), the editor says so and points at History, where the
// replaced save is the version just before. The mark lives on the cell's
// history (`replaced` with the revision the write became); a revision's mark
// never changes, so each lookup is cached by that revision.

import { fetchCellHistory, type HistoryResponse } from '@/lib/api/history';
import { versionReplacements, type Replacement } from '@/lib/replacements';

export interface FileReplacement {
    mark: Replacement;
    /** The run that replaced it, as the history rail names it. */
    run: string | null;
}

/** The mark of the version whose write of `path` became `revision`, if any. */
export function findFileReplacement(history: HistoryResponse, path: string, revision: number): FileReplacement | null {
    for (const node of history.nodes) {
        for (const version of node.versions) {
            const mark = versionReplacements(version.replaced).find(r => r.path === path && r.file_revision === revision);
            if (mark) return { mark, run: node.run?.runner_id ?? node.run?.generator_name ?? null };
        }
    }
    return null;
}

const lookups = new Map<string, Promise<FileReplacement | null>>();

/** Whether `writerId` is a run's writer: only a run replaces a save. */
export const isRunWriter = (writerId: string | null | undefined): boolean => !!writerId?.startsWith('run:');

/** The replacement that produced `revision` of `path`, read once from the
 *  cell's history. A failed read is not cached. */
export function fileReplacement(channel: string, cellId: string, path: string, revision: number): Promise<FileReplacement | null> {
    const key = JSON.stringify([channel, cellId, path, revision]);
    let pending = lookups.get(key);
    if (!pending) {
        pending = fetchCellHistory(channel, cellId).then(history => findFileReplacement(history, path, revision));
        pending.catch(() => lookups.delete(key));
        lookups.set(key, pending);
    }
    return pending;
}

/** Forget cached lookups (tests). */
export const resetFileReplacements = (): void => { lookups.clear(); };
