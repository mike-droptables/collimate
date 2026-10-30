// The suggested catalog a new channel can start from (api/routes/suggested.py):
// a content tree's `suggested/<id>.json` files, read from the content
// installed with the app, from the latest release's content a Refresh
// fetched, or from a folder on the server that stands in for them.
import type { RunnerDraft, RunnerGenerator } from '../runnerApi';
import { requestJson } from './client';

/** One `[cellName, folder]` pair of a suggestion, the folder relative to
 *  the catalog's root. */
export interface SuggestedCellRef { name: string; folder: string }

/** Fields a suggestion exposes directly on a runner's card. Setting keys
 *  name the generator's main config; account keys name its account roles. */
export type SuggestedQuickField = { account: string } | { setting: string } | { prompt: true };

export interface SuggestedRunner {
    draft: RunnerDraft;
    quick_fields: SuggestedQuickField[];
}

export interface Suggestion {
    id: string;
    name: string;
    description: string;
    generators: SuggestedCellRef[];
    renderers: SuggestedCellRef[];
    /** The explanation cells, each a main stack of its own. */
    tutorial: SuggestedCellRef[];
    /** The suggestion's generators, as the runner editor binds them. */
    runner_generators: RunnerGenerator[];
    /** The runner packages shipped inside its generator cells, offered as
     *  drafts to configure or remove before the channel is created. */
    runners: SuggestedRunner[];
}

/** Where the catalog was read: the content installed with the app
 *  (`bundled`, the default; `path` is its directory), the latest release's
 *  content fetched by a Refresh (`github`; `path` is the asset URL and
 *  `fetched_at` when it was taken), or a folder on the server (`folder`). */
export interface SuggestedSource { kind: 'bundled' | 'github' | 'folder'; path: string; fetched_at: string | null }

export interface SuggestedCatalog {
    source: SuggestedSource;
    suggestions: Suggestion[];
    /** Why the last read failed. A failed Refresh still carries the
     *  suggestions of the source it had (the copy fetched before it, or the
     *  installed content). */
    error: string | null;
}

/** GET /api/suggested. `refresh` fetches the latest release's content from
 *  GitHub first, for the channels created after it; without it the server
 *  answers from its source and never touches the network. */
export const fetchSuggested = (refresh = false): Promise<SuggestedCatalog> =>
    requestJson(`/suggested?refresh=${refresh ? 1 : 0}`, 'GET');

/** PUT /api/suggested/source: read the catalog from a folder on the server,
 *  or (null) stop reading a folder. Answers the catalog from the new source. */
export const setSuggestedSource = (path: string | null): Promise<SuggestedCatalog> =>
    requestJson('/suggested/source', 'PUT', { path });
