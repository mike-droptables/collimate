// The Capabilities stack's own routes (api/routes/capabilities_stack.py, and
// the built-in handler skills offer in api/routes/builtins.py): the work
// stacks Build and Create open beside Capabilities, the problems listing,
// the suggested-catalog door, and the return path of regenerated skills.
import type { ChannelLayout, CapabilityKind } from '@/lib/types';
import type { HandlerInfo } from '@/lib/definitionApi';
import { reportPackageSkips, type PackageSkip } from '../packageReports';
import { requestJson } from './client';

const capabilitiesPath = (channel: string) => `/channels/${encodeURIComponent(channel)}/capabilities`;

/** A spawn's answer: the channel layout with the new standard work stack
 *  (right of Capabilities, or at the end of the board while it is hidden)
 *  selected, its one cell, and the cell-relative brief the user runs from
 *  (pinned in that cell). */
export type SpawnResult = ChannelLayout & { stack_id: string; cell_id: string; brief_path: string };

/** Build a user handler, or regenerate a built-in handler's skills: `cell_id`
 *  holds the handler at `folder` (for a built-in, the Definitions card and
 *  the handler's id). */
export interface CapabilityBuildRequest { cell_id: string; folder: string }

/** One question of the Create questionnaire, as the user saw and answered it. */
export interface CapabilityAnswer { question: string; answer: string }

/** What the Create questionnaire hands the work stack: a title, the kinds to
 *  make (none: let the work stack decide), every answer shown, and the
 *  related capabilities already in the channel. */
export interface CapabilityCreateRequest {
    title: string;
    make: CapabilityKind[];
    answers: CapabilityAnswer[];
    related: { kind: CapabilityKind; id: string }[];
}

/** A file on Capabilities that is not a working capability. */
export interface CapabilityProblem {
    cell_id: string;
    path: string;
    kind: 'definition' | CapabilityKind;
    error: string;
}

/** A cell of the suggested catalog, by its suggestion and its folder there
 *  (as the channel's catalog read lists it). */
export interface CapabilityCatalogAdd { suggestion_id: string; folder: string }

/** One cell of a suggestion, as the channel sees it. `held`: the channel
 *  already holds this catalog cell (the add would answer 409). */
export interface CapabilityCatalogCell {
    folder: string;
    name: string;
    section: 'generator' | 'renderer';
    held: boolean;
}

/** A suggestion of the catalog with its generator and renderer cells. */
export interface CapabilityCatalogSuggestion {
    id: string;
    name: string;
    description: string;
    cells: CapabilityCatalogCell[];
}

/** POST …/capabilities/add's answer: the layout, the added cell, and the
 *  packages admission left out (reported to the user). */
export type CapabilityCatalogAddResult = ChannelLayout & { cell_id: string; package_skips: PackageSkip[] };

/** Skills regenerated in the standard cell `source_cell_id`, under
 *  `<folder>/derived/skills/`, offered to a built-in handler. */
export interface BuiltinSkillsOffer { source_cell_id: string; folder: string }

/** POST …/capabilities/build: open a work stack holding a copy of the
 *  handler, its build brief and the authoring guides. */
export const spawnHandlerBuild = (channel: string, request: CapabilityBuildRequest): Promise<SpawnResult> =>
    requestJson<SpawnResult>(`${capabilitiesPath(channel)}/build`, 'POST', request);

/** POST …/capabilities/create: open a work stack holding the Create brief
 *  (the answers) and the guides for each kind to make. */
export const spawnCapabilityCreate = (channel: string, request: CapabilityCreateRequest): Promise<SpawnResult> =>
    requestJson<SpawnResult>(`${capabilitiesPath(channel)}/create`, 'POST', request);

/** GET …/capabilities/problems: every file on Capabilities that fails to
 *  load as a definition, generator, renderer or handler. */
export const fetchCapabilityProblems = async (channel: string): Promise<CapabilityProblem[]> =>
    (await requestJson<{ problems: CapabilityProblem[] }>(`${capabilitiesPath(channel)}/problems`, 'GET')).problems;

/** GET …/capabilities/catalog: the suggested catalog's cells, each with
 *  whether the channel already holds it. */
export const fetchCapabilityCatalog = async (channel: string): Promise<CapabilityCatalogSuggestion[]> =>
    (await requestJson<{ suggestions: CapabilityCatalogSuggestion[] }>(`${capabilitiesPath(channel)}/catalog`, 'GET')).suggestions;

/** POST …/capabilities/add: add a suggested-catalog cell onto Capabilities,
 *  reporting any package admission left out. 409 when the channel already
 *  holds that cell, or when admission left out every package. */
export const addFromCatalog = async (channel: string, request: CapabilityCatalogAdd): Promise<CapabilityCatalogAddResult> => {
    const result = await requestJson<CapabilityCatalogAddResult>(`${capabilitiesPath(channel)}/add`, 'POST', request);
    await reportPackageSkips(result.package_skips);
    return result;
};

/** POST …/builtin/handlers/{id}/skills: adopt the regenerated skills into the
 *  built-in handler's overlay, where they wait for Review and accept. */
export const offerBuiltinSkills = async (channel: string, handlerId: string, offer: BuiltinSkillsOffer): Promise<HandlerInfo> =>
    (await requestJson<{ handler: HandlerInfo }>(
        `/channels/${encodeURIComponent(channel)}/builtin/handlers/${encodeURIComponent(handlerId)}/skills`, 'POST', offer,
    )).handler;
