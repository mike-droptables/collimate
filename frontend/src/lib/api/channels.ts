import { Channel } from '@/lib/types';
import { reportPackageSkips, reportRepositorySkips, type PackageSkip, type RepositorySkip } from '../packageReports';
import type { RunnerDraft } from '../runnerApi';
import { requestJson } from './client';
import type { ImportCell, ImportStack } from './transfer';

/** An existing channel a new channel's section copies from. */
export interface ChannelRef { channel_id: string }

/** The cells of `channel_id` carrying `tag` (live and removed), landing on
 *  the new channel's Removed shelf. */
export interface TagRef { channel_id: string; tag: string }

/** A packages zip's cells: `<root>/Capabilities/<cell>/…`. */
export interface PackagesZip { cells: ImportCell[] }

/** A main-cells zip's stacks: `<root>/<stack>/<cell>/…`. */
export interface MainZip { stacks: ImportStack[] }

/** A tag zip's cells (`<root>/<cell>/…`), all given `tag` ('' leaves them
 *  untagged). */
export interface HistoryZip { tag: string; cells: ImportCell[] }

/** The Config tab: each section from a channel, from a zip, or null
 *  (nothing: only the built-in Capabilities cards, no cells). A new
 *  channel's settings are always the defaults. */
export interface ConfigSelection {
    packages: ChannelRef | PackagesZip | null;
    main_cells: ChannelRef | MainZip | null;
    history_cells: TagRef | HistoryZip | null;
}

/** The Suggested tab: a catalog suggestion, its explanation cells or not,
 *  and the runner drafts kept (each one ready to run). */
export interface SuggestedSelection { id: string; tutorial: boolean; runners: RunnerDraft[] }

/** What a new channel starts with (POST /api/channels?name=). No body is an
 *  empty channel. */
/** A saved suggestion of the user catalog, with the complete runner drafts
 *  kept (api/collimate_server/user_catalog.py SavedSelection). */
export interface SavedSelection { id: string; runners: RunnerDraft[] }

export type CreateChannelBody = { suggested: SuggestedSelection } | { saved: SavedSelection } | { config: ConfigSelection };

export const createChannel = async (name: string, body?: CreateChannelBody): Promise<Channel> => {
    const result = await requestJson(`/channels?name=${encodeURIComponent(name)}`, 'POST', body) as
        Channel & { package_skips: PackageSkip[]; skipped?: RepositorySkip[] };
    await reportPackageSkips(result.package_skips);
    await reportRepositorySkips(result.skipped);
    return result;
};

export const selectChannel = async (cid: string) => requestJson(`/channels/${cid}/select`, 'POST');

export const renameChannel = async (cid: string, name: string): Promise<Channel> =>
    requestJson(`/channels/${cid}/rename?name=${encodeURIComponent(name)}`, 'PUT');

export const deleteChannel = async (cid: string) => requestJson(`/channels/${cid}`, 'DELETE');
