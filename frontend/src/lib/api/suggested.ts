// The suggested catalog a new channel can start from (api/collimate_server/routes/suggested.py):
// the `suggested/<id>.json` files of the content tree installed with the
// server (a checkout's own `content/` when it runs from one).
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
    /** The library cells: the shared code its generators name under `shared:`. */
    libraries: SuggestedCellRef[];
    /** The explanation cells, each a main stack of its own. */
    tutorial: SuggestedCellRef[];
    /** The suggestion's generators, as the runner editor binds them. */
    runner_generators: RunnerGenerator[];
    /** The runner packages shipped inside its generator cells, offered as
     *  drafts to configure or remove before the channel is created. */
    runners: SuggestedRunner[];
}

export interface SuggestedCatalog {
    suggestions: Suggestion[];
    /** Why the content tree could not be read (then there are no suggestions). */
    error: string | null;
}

/** GET /api/suggested: the server answers from its own content tree and
 *  never touches the network. */
export const fetchSuggested = (): Promise<SuggestedCatalog> => requestJson('/suggested', 'GET');
