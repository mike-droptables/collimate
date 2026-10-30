import { requestJson } from './api/client';

export type ExampleExpect = 'valid' | 'invalid';
/** What an example declares: what it shows, the verdict its files should
 *  get and the feedback the verifier should give. */
export interface ExampleDeclaration { description: string; expect: ExampleExpect; feedback: string }
export const BLANK_EXAMPLE: ExampleDeclaration = Object.freeze({ description: '', expect: 'valid', feedback: '' });
export interface HandlerExample extends ExampleDeclaration {
    folder: string;
    /** A built-in handler's example this channel added (editable and
     *  removable); the app's own examples are read-only. */
    added?: boolean;
}
export interface HandlerInfo {
    id: string; name: string; description: string;
    types: { ext: string; render_as?: string | null }[];
    examples: HandlerExample[];
    cell_id: string; folder: string; definition_path: string;
    has_derived: boolean; built_hash: string | null; accepted: boolean;
    accepted_derived: string | null; disabled: boolean; clashes: string[];
    accepted_skills: string[]; added_skills: string[];
    /** One of the app's built-in handlers: its validation is the app's, the
     *  channel only adds examples and regenerates its skills. `cell_id` is
     *  the Definitions card. */
    builtin: boolean;
    /** A built-in handler's skills in force: the channel's accepted
     *  regeneration, else the app's. `accepted` is false while a
     *  regeneration (`built_hash`) waits to be accepted. */
    accepted_skills_hash?: string | null;
    /** A built-in whose skills in force are the app's own (no accepted
     *  regeneration of this channel); always false for a user handler. */
    uses_app_skills: boolean;
}
/** Add one definition package from a cell's Files panel to Capabilities:
 *  into the Capabilities cell `target_cell_id`, or a new cell (null). */
export interface ApprovalRequest { source_cell_id: string; definition_path: string; target_cell_id: string | null }
/** Where an approved package may go: a new cell (`cell_id` null), or a
 *  non-built-in Capabilities cell. `replaces`: that cell already holds a
 *  package of the same kind and id (the client preselects it, with Replace
 *  on). */
export interface ApprovalTarget { cell_id: string | null; name: string; replaces: boolean }
/** The summary facts of a package. `derived_hash` is the hash of the
 *  handler build (derived/) the package carries, or null; `kept_from_target`
 *  lists the target's paths a Replace keeps (a handler's added skills). */
export type ApprovalSummary = Record<string, unknown> & { derived_hash: string | null; kept_from_target: string[] };
export interface ApprovalPreview {
    targets: ApprovalTarget[];
    content_hash: string; source_version_id: string | null; kind: string; id: string; folder: string;
    files: string[]; summary: ApprovalSummary; replacement: Record<string, unknown> | null; target_revision: string;
}
export interface BuildInspection {
    content_hash: string; derived_hash: string | null;
    compile: { valid: boolean; error?: string };
    examples: { folder: string; expect: string; verdicts: { path: string; valid: boolean; error?: string }[]; error?: string | null }[];
}
export interface CellValidation {
    cell_id: string; source_version_id: string | null; source_revision: string;
    pending: boolean; verdicts: Record<string, {valid:boolean; error?:string}>;
}
const root = (channel: string) => `/channels/${encodeURIComponent(channel)}`;
const cellRoot = (channel: string, cell: string) => `${root(channel)}/cells/${encodeURIComponent(cell)}`;
export const fetchHandlers = () => requestJson<HandlerInfo[]>('/handlers');
export const fetchCellValidation = (channel:string,cell:string) => requestJson<CellValidation>(`${cellRoot(channel,cell)}/validation`);
export const previewApproval = (channel: string, request: ApprovalRequest) => requestJson<ApprovalPreview>(`${root(channel)}/packages/preview`, 'POST', request);
export const addApprovedPackage = (channel: string, request: ApprovalRequest & { expected_content_hash: string; expected_target_revision: string; replace: boolean }) => requestJson<{cell_id:string; message:string}>(`${root(channel)}/packages/add`, 'POST', request);
export const inspectHandler = (channel: string, cell: string, folder: string) => requestJson<BuildInspection>(`${cellRoot(channel, cell)}/handler-build/inspect`, 'POST', { folder });
export const acceptHandler = (channel: string, cell: string, folder: string, hash: string) => requestJson(`${cellRoot(channel, cell)}/handler-build/accept`, 'POST', { folder, expected_content_hash: hash });

// A built-in handler's channel additions: its added examples and its
// regenerated skills. Regeneration runs in a work stack
// (capabilitiesStack.spawnHandlerBuild); nothing runs on the Definitions
// card. The skills come back onto the card through
// capabilitiesStack.offerBuiltinSkills, wait for Review there, and
// acceptHandler accepts them (revertBuiltinSkills drops them).
const builtinRoot = (channel: string, id: string) => `${root(channel)}/builtin/handlers/${encodeURIComponent(id)}`;
/** Copy `paths` of `source_cell_id` (their current bodies) into a new added
 *  example of built-in handler `id`. */
export const addBuiltinExample = (channel: string, id: string, body: ExampleDeclaration & { source_cell_id: string; paths: string[] }) =>
    requestJson<{ folder: string }>(`${builtinRoot(channel, id)}/examples`, 'POST', body);
export const patchBuiltinExample = (channel: string, id: string, folder: string, patch: Partial<ExampleDeclaration>) =>
    requestJson(`${builtinRoot(channel, id)}/examples/${encodeURIComponent(folder)}`, 'PATCH', patch);
export const deleteBuiltinExample = (channel: string, id: string, folder: string) =>
    requestJson(`${builtinRoot(channel, id)}/examples/${encodeURIComponent(folder)}`, 'DELETE');
/** Back to the app's skills: drop the channel's regenerated skills. */
export const revertBuiltinSkills = (channel: string, id: string) =>
    requestJson(`${builtinRoot(channel, id)}/skills`, 'DELETE');

export interface OptionsMap { options: {name:string; files:Record<string,string>}[]; kept:string[]; retain_source:boolean }
export const fetchOptionsMap = (channel:string,cell:string,path:string,versionId?:string) => requestJson<OptionsMap>(`${cellRoot(channel,cell)}/options-map?${new URLSearchParams({path,...(versionId ? {version_id:versionId} : {})})}`);
