import { Channel } from '@/lib/types';
import { reportPackageSkips, type PackageSkip } from '../packageReports';
import type { RunnerDraft } from '../runnerApi';
import { requestJson } from './client';
import type { ChannelSettings } from './settings';
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

/** A settings document read from a JSON file. */
export interface SettingsDoc { doc: ChannelSettings }

/** The Config tab: each section from a channel, from a file, or null
 *  (nothing: only the built-in Capabilities cards, default settings, no cells). */
export interface ConfigSelection {
    packages: ChannelRef | PackagesZip | null;
    settings: ChannelRef | SettingsDoc | null;
    main_cells: ChannelRef | MainZip | null;
    history_cells: TagRef | HistoryZip | null;
}

/** The Suggested tab: a catalog suggestion, its explanation cells or not,
 *  and the runner drafts kept (each one ready to run). */
export interface SuggestedSelection { id: string; tutorial: boolean; runners: RunnerDraft[] }

/** What a new channel starts with (POST /api/channels?name=). No body is an
 *  empty channel. */
export type CreateChannelBody = { suggested: SuggestedSelection } | { config: ConfigSelection };

export const createChannel = async (name: string, body?: CreateChannelBody): Promise<Channel> => {
    const result = await requestJson(`/channels?name=${encodeURIComponent(name)}`, 'POST', body) as Channel & { package_skips: PackageSkip[] };
    await reportPackageSkips(result.package_skips);
    return result;
};

export const selectChannel = async (cid: string) => requestJson(`/channels/${cid}/select`, 'POST');

export const renameChannel = async (cid: string, name: string): Promise<Channel> =>
    requestJson(`/channels/${cid}/rename?name=${encodeURIComponent(name)}`, 'PUT');

export const deleteChannel = async (cid: string) => requestJson(`/channels/${cid}`, 'DELETE');
