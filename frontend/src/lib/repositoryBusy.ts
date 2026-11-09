// One live run is the sole writer of a cell's git (the repository lease,
// api/collimate_server/repository_leases.py). While it holds the lease the host refuses with
// 409: a Git runner's start, a rescue route and a folder delete, rename or
// move that takes a repository along. The way out is a second working copy,
// so each refusal offers Duplicate.

import { duplicateCell } from '@/lib/api/cells';
import { describeApiError } from '@/lib/api/client';
import { currentChannelId } from '@/lib/currentChannel';
import { useCollimateStore } from '@/stores/useCollimateStore';

/** How RepositoryBusy.message begins; a folder operation's or a move's 409
 *  carries the message alone as its `detail`. */
const BUSY_DETAIL = 'git is in use by run ';

interface Refusal { status?: number; data?: unknown }

/** The HTTP refusal an error carries, its own or one it was raised from. */
function refusalOf(error: unknown): Refusal | null {
    for (let e = error, depth = 0; e && depth < 4; e = (e as { cause?: unknown }).cause, depth++) {
        const response = (e as { response?: Refusal }).response;
        if (response) return response;
    }
    return null;
}

/** The host's sentence when `error` is a repository-lease refusal: the
 *  `repository_busy` body (`{error, owner, run_id, message}`) of a run start
 *  or rescue route, or the `detail` of a folder operation or move. */
export function repositoryBusyMessage(error: unknown): string | null {
    const refusal = refusalOf(error);
    if (refusal?.status !== 409 || !refusal.data || typeof refusal.data !== 'object') return null;
    const body = refusal.data as { error?: unknown; message?: unknown; detail?: unknown };
    if (body.error === 'repository_busy' && typeof body.message === 'string') return body.message;
    if (typeof body.detail === 'string' && body.detail.startsWith(BUSY_DETAIL)) return body.detail;
    return null;
}

/** How the folder operations' own refusals begin (api/collimate_server/persistence.py
 *  `_folder_rows`, `_moved_rows`): a folder whose files changed under the
 *  operation, a path that cannot hold a repository or already holds one. */
const FOLDER_REFUSAL = /^(The folder's files changed |A repository cannot live at |(This|The target) cell already has a repository at )/;

/** Whether `error` is a refusal the repository work brings (a lease, a
 *  folder operation's): the host wrote nothing, and the same request would
 *  only be refused again. Any other 409 (a file lock, an ended run) keeps
 *  its operation for a retry. */
export function gitRefusal(error: unknown): boolean {
    if (repositoryBusyMessage(error) !== null) return true;
    const refusal = refusalOf(error);
    const detail = (refusal?.data as { detail?: unknown } | undefined)?.detail;
    return refusal?.status === 409 && typeof detail === 'string' && FOLDER_REFUSAL.test(detail);
}

/** Whether the host refused `error`'s request for good: a git refusal, or
 *  a request it cannot validate (422). It wrote nothing, and sending the
 *  same request again would only be refused again, so a journaled file
 *  operation is dropped rather than replayed. */
export function refusedForGood(error: unknown): boolean {
    return gitRefusal(error) || refusalOf(error)?.status === 422;
}

/** The host's own words for a refused request: its `message` (the git
 *  routes' bodies), else FastAPI's `detail`. */
export function refusalText(error: unknown): string {
    const data = refusalOf(error)?.data as { message?: unknown } | undefined;
    if (data && typeof data.message === 'string' && data.message.trim()) return data.message;
    return describeApiError(error);
}

/** Show a lease refusal and offer Duplicate: a copy of the cell, selected,
 *  whose repositories are its own to work in. */
export async function offerDuplicate(cellId: string, message: string, title = 'Git is in use'): Promise<void> {
    const store = useCollimateStore.getState();
    const choice = await store.openChoice({ title, body: message, options: [{ id: 'duplicate', label: 'Duplicate cell' }] });
    if (choice !== 'duplicate') return;
    try {
        const res = await duplicateCell(store.activeChannel?.id ?? currentChannelId(), cellId);
        useCollimateStore.getState().applyOptimisticChannel(res.channel);
        useCollimateStore.getState().requestSelectCell(res.new_cell_id);
    } catch (error) {
        await useCollimateStore.getState().openAlert({ title: 'Duplicate failed', body: describeApiError(error) });
    }
}

/** The cell of the live run a lease refusal names (`run <id8>`): a move
 *  between cells is refused for either one's git. */
function holderCell(message: string): string | undefined {
    const id8 = /^git is in use by run (\S+)/.exec(message)?.[1];
    if (!id8) return undefined;
    return Object.values(useCollimateStore.getState().runs).find(run => run.runId.startsWith(id8))?.cellId;
}

/** Report a failed file operation of `cellId`: a lease refusal offers
 *  Duplicate (of the cell whose run holds the git, when this client follows
 *  it), anything else is an alert titled `title` in the host's words. */
export async function reportFileError(cellId: string | undefined, title: string, error: unknown): Promise<void> {
    const busy = repositoryBusyMessage(error);
    const holder = busy ? holderCell(busy) ?? cellId : undefined;
    if (busy && holder) return offerDuplicate(holder, busy);
    await useCollimateStore.getState().openAlert({ cellId, title, body: String((error as Error)?.message ?? error) });
}
