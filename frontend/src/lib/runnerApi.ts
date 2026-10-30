import { requestJson } from './api/client';
import type { AccountRequirement } from './accountsApi';
import { computeVisibility, matches, normalizeOptions, type SetYamlCondition, type SetYamlField } from './setYaml';

export interface RunnerPermissions {
    update_packages: boolean; docker: string; internet: string;
    internet_allow: string[]; commands: string;
}


export interface RunnerDefinition {
    id: string; name: string; description: string; tags: string[]; generator: string;
    prompt: string; settings: Record<string, Record<string, unknown>>;
    accounts: Record<string, string>; folders: string[]; permissions: Partial<RunnerPermissions>;
    packages: Record<string, unknown>;
}

export interface RunnerFile { name: string; content: string; language?: string; blob_hash?: string; encoding?: 'base64' }

export interface RunnerDraft { runner: RunnerDefinition; files: RunnerFile[]; revision?: string }

export interface RunnerTemplate { revision?: string; runner: Omit<RunnerDefinition, 'id' | 'generator'>; files: RunnerFile[] }

export interface RunnerGenerator {
    id: string; name: string; cell_id?: string; main_config: string;
    fields: (SetYamlField & { top_level?: boolean; advanced?: boolean; section?: string; account_role?: string; account_property?: string })[];
    settings_files: Record<string, string>; account_requirements: Record<string, AccountRequirement>;
    config_folders: { name: string; description?: string }[];
    prompt_intent?: string; prompt_required?: boolean;
    prompt_show_if?: SetYamlCondition; prompt_required_if?: SetYamlCondition;
    /** The run gets the cell's selected file (`gen.selected_file`): always
     *  (`takes_selected_file`) or when its main config matches
     *  `takes_selected_file_if`. */
    takes_selected_file?: boolean; takes_selected_file_if?: SetYamlCondition;
    /** The runner needs a Docker sandbox when its main config matches. */
    requires_docker_if?: SetYamlCondition;
    /** The grant axes the generator enforces itself (its manifest's
     *  `enforces`); a runner's level on such an axis means something only here. */
    enforces: string[];
    configs: { id: string; name: string; revision?: string }[]; default: RunnerTemplate;
}

export interface RunnerEntry extends RunnerDefinition {
    revision: string; cell_id: string; icon?: string; invalid?: { reason: string };
    missing_required?: { key: string; label: string }[];
    invalid_options?: { key: string; label: string }[];
    /** A run of this runner gets the cell's selected file. */
    takes_selected_file?: boolean;
}

export interface RunnerEditorPayload extends RunnerDraft {
    revision: string; generator: RunnerGenerator;
    /** The Capabilities cell the runner lives in (the one chosen in Keep it
     *  in, else its generator's cell): edits, deletes and copies act there. */
    cell_id: string;
}

const enc = encodeURIComponent;

export const fetchRunners = () => requestJson<RunnerEntry[]>('/runners');

export const fetchRunner = (id: string) => requestJson<RunnerEditorPayload>(`/runners/${enc(id)}`);

/** Where a NEW runner is kept: a Capabilities cell by id, or a new cell
 *  the server appends to Capabilities, named after the runner (`new_cell`).
 *  None: its generator's cell. */
export type RunnerHome = { cellId: string } | { newCell: true };

/** Commit a runner. A NEW runner is saved where `home` says (the Keep it in
 *  choice), else into its generator's cell. An existing runner is saved
 *  where it lives. */
export const saveRunner = (draft: RunnerDraft, existing: boolean, home?: RunnerHome) => requestJson<RunnerEditorPayload>(
    existing ? `/runners/${enc(draft.runner.id)}` : '/runners', existing ? 'PUT' : 'POST',
    {
        runner: draft.runner, files: draft.files, ...(draft.revision ? { revision: draft.revision } : {}),
        ...(!existing && home ? ('newCell' in home ? { new_cell: true } : { cell_id: home.cellId }) : {}),
    });

export const deleteRunner = (id: string) => requestJson(`/runners/${enc(id)}`, 'DELETE');

export interface RunnerVocabulary {
    enums: Record<string, string[]>;
    defaults: RunnerPermissions;
    labels: Record<keyof RunnerPermissions, string>;
    descriptions: Record<string, Record<string, string>>;
}

export const fetchRunnerVocabulary = () => requestJson<RunnerVocabulary>('/runner_vocabulary');

/** The active channel's generators, as the runner editor binds them. */
export const fetchRunnerGenerators = () => requestJson<RunnerGenerator[]>('/runner_generators');

/** One named config of an active-channel generator, as a runner template. */
export const fetchRunnerConfig = (gen: string, id: string) => requestJson<RunnerTemplate>(`/runner_generators/${enc(gen)}/configs/${enc(id)}`);

export const runnerSlug = (name: string) => {
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'runner';
    return /^[a-z]/.test(slug) ? slug : `runner-${slug}`;
};


/** A fresh, collision-free runner id derived from a display name. */
export const newSlugId = (name: string) => `${runnerSlug(name)}-${crypto.randomUUID().slice(0, 8)}`;


/** Whether the generator's prompt is shown, and whether it is then required —
 *  the browser-side mirror of the server's prompt gate. */
export function promptGate(
    generator: Pick<RunnerGenerator, 'fields' | 'prompt_required' | 'prompt_show_if' | 'prompt_required_if'>,
    values: Record<string, unknown>,
): { shown: boolean; required: boolean } {
    const fields = generator.fields.map(field => ({ ...field, options: normalizeOptions(field.options) }));
    const visible = computeVisibility(fields, values);
    const types = Object.fromEntries(fields.map(field => [field.key, field.type]));
    const shown = !generator.prompt_show_if || matches(generator.prompt_show_if, values, visible, types);
    const required = shown && (generator.prompt_required === true
        || (!!generator.prompt_required_if && matches(generator.prompt_required_if, values, visible, types)));
    return { shown, required };
}


/** Save a copy of a stored runner under its own id, in the cell the
 *  original lives in. */
export async function duplicateRunner(id: string, taken: Iterable<string> = []) {
    const { runner, files, cell_id } = await fetchRunner(id);
    const name = copyName(runner.name, taken);
    return saveRunner({ runner: { ...runner, id: newSlugId(name), name }, files }, false, { cellId: cell_id });
}


/** The name a copy of `name` gets among `taken`: "<name> copy", then
 *  "<name> copy 2", "<name> copy 3", … — the first one nobody has. */
export function copyName(name: string, taken: Iterable<string>): string {
    const names = new Set(Array.from(taken, n => n.trim().toLowerCase()));
    const base = `${name.trim()} copy`;
    if (!names.has(base.toLowerCase())) return base;
    for (let n = 2; ; n++) {
        const candidate = `${base} ${n}`;
        if (!names.has(candidate.toLowerCase())) return candidate;
    }
}
