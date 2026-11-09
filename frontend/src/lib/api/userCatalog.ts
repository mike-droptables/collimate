// The user catalog (api/collimate_server/routes/user_catalog.py and the publish
// routes of routes/capability_cells.py): definitions published from a
// channel for any channel to add, and saved suggestions for new channels.
import type { ChannelLayout, CapabilityKind } from '@/lib/types';
import type { PackageSkip } from '../packageReports';
import type { Suggestion } from './suggested';
import type { PackageRow } from './capabilityCells';
import { requestJson } from './client';

/** One published definition. `section` is what the catalog lists it
 *  under; `libraries` the names its cell.yaml lists under shared:. */
export interface CatalogEntry {
    id: string;
    name: string;
    section: 'generator' | 'renderer' | 'library';
    libraries: string[];
    packages: { kind: CapabilityKind; id: string; version: string | null }[];
    revision: number;
    published_at: string;
    app_version: string;
}

/** An entry as a channel's Add from catalog sees it: `held` when the
 *  channel already holds it. */
export type ChannelCatalogEntry = CatalogEntry & { held: boolean };

/** Where a library the published cell names resolves for a channel that
 *  adds it: the cell itself, the app, a catalog entry (`entry`), or
 *  nowhere yet (`cell`: this channel's cell holding it, to publish too). */
export interface PublishLibrary {
    id: string;
    resolves: 'self' | 'app' | 'catalog' | null;
    entry: string | null;
    cell: string | null;
}

/** POST …/publish/review. `entry` is the entry this publish writes over
 *  (null: a new one); `unchanged` when it already holds these files. */
export interface PublishReview {
    name: string;
    entry: CatalogEntry | null;
    unchanged: boolean;
    packages: PackageRow[];
    libraries: PublishLibrary[];
    token: string;
}

export type CatalogAddResult = ChannelLayout & { cell_id: string; cell_ids: string[]; package_skips: PackageSkip[] };

/** One cell a saved suggestion lists: a cell of the app's suggestions by
 *  its section and folder, or a catalog entry. */
export type SuggestionItem =
    | { source: 'app'; section: 'generator' | 'renderer' | 'library'; folder: string; name: string }
    | { source: 'catalog'; id: string };

/** A saved suggestion as the New channel page reads a suggestion, with its
 *  items; `problem` says why it no longer reads (then it lists nothing). */
export type SavedSuggestion = Suggestion & { saved: true; items: SuggestionItem[]; problem: string | null };

export interface SuggestionBody {
    name: string;
    description: string;
    items: SuggestionItem[];
    channel_id: string | null;
    runner_ids: string[];
}

const cellPath = (channel: string, cellId: string) =>
    `/channels/${encodeURIComponent(channel)}/capabilities/cells/${encodeURIComponent(cellId)}`;
const entriesPath = (channel: string) => `/channels/${encodeURIComponent(channel)}/capabilities/catalog/entries`;

export const reviewPublish = (channel: string, cellId: string): Promise<PublishReview> =>
    requestJson(`${cellPath(channel, cellId)}/publish/review`, 'POST');

export const publishToCatalog = (channel: string, cellId: string, token: string): Promise<CatalogEntry> =>
    requestJson(`${cellPath(channel, cellId)}/publish`, 'POST', { token });

export const fetchCatalogEntries = async (channel: string): Promise<ChannelCatalogEntry[]> =>
    (await requestJson<{ entries: ChannelCatalogEntry[] }>(entriesPath(channel), 'GET')).entries;

export const addCatalogEntry = (channel: string, entryId: string): Promise<CatalogAddResult> =>
    requestJson(`${entriesPath(channel)}/${encodeURIComponent(entryId)}/add`, 'POST');

export const removeCatalogEntry = (entryId: string): Promise<void> =>
    requestJson(`/catalog/entries/${encodeURIComponent(entryId)}`, 'DELETE');

export const fetchSavedSuggestions = async (): Promise<SavedSuggestion[]> =>
    (await requestJson<{ suggestions: SavedSuggestion[] }>('/catalog/suggestions', 'GET')).suggestions;

export const saveSuggestion = (body: SuggestionBody, id?: string): Promise<{ id: string; added: string[] }> =>
    id ? requestJson(`/catalog/suggestions/${encodeURIComponent(id)}`, 'PUT', body)
        : requestJson('/catalog/suggestions', 'POST', body);

export const deleteSuggestion = (id: string): Promise<void> =>
    requestJson(`/catalog/suggestions/${encodeURIComponent(id)}`, 'DELETE');

/** The app's catalog answered by `catalog`, with the saved suggestions that
 *  read after its own (one that no longer reads is left out; the catalog
 *  view says why). The saved ones failing to load leaves the app's. */
export async function withSavedSuggestions<T extends { suggestions: Suggestion[] }>(catalog: Promise<T>): Promise<T> {
    const [answer, saved] = await Promise.all([catalog, fetchSavedSuggestions().catch(() => [] as SavedSuggestion[])]);
    return { ...answer, suggestions: [...answer.suggestions, ...saved.filter(item => !item.problem)] };
}
