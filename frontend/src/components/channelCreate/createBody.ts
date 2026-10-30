// The new-channel page's pure half: an expanded zip read as the section it
// was picked for (the app's own export layouts), the create body each tab
// sends, and what still keeps Create disabled.

import type { ConfigSelection, MainZip, PackagesZip } from '@/lib/api/channels';
import type { ImportStack, ZipExpansion } from '@/lib/api/transfer';
import type { Suggestion, SuggestedRunner } from '@/lib/api/suggested';
import type { Account } from '@/lib/accountsApi';
import { newSlugId, type RunnerDraft } from '@/lib/runnerApi';
import { isRunnerReady, runnerReadiness, type RunnerReadiness } from '@/lib/runnerReadiness';
import { splitLevels, type SplitLevelId } from '@/lib/transfer/splitLevels';
import { SECTION_LABELS, type ConfigState, type HistoryFile, type PickedFile, type SectionState, type SuggestedState } from './state';

export const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export type ZipRead<T> = { ok: true; value: T; summary: string } | { ok: false; error: string };

/** A zip's tree split at `level`: its one root folder (the export's root)
 *  and its stacks of cells, each file by its blob hash. */
function splitZip(expansion: ZipExpansion, level: SplitLevelId, layout: string): { ok: true; root: string; stacks: ImportStack[]; leftOut: number } | { ok: false; error: string } {
    const paths = expansion.entries.map(entry => entry.path);
    if (!paths.length) return { ok: false, error: 'The zip holds no files.' };
    const roots = new Set([...paths, ...expansion.dirs].map(path => path.split('/')[0]));
    if (roots.size !== 1) return { ok: false, error: `The zip must hold one top folder: ${layout}.` };
    const loose = paths.find(path => path.split('/').length <= level);
    if (loose) return { ok: false, error: `${loose} sits outside a cell folder: ${layout}.` };
    const [root] = roots;
    const split = splitLevels(paths, expansion.dirs, root).find(candidate => candidate.level === level)!;
    const hashes = new Map(expansion.entries.map(entry => [entry.path, entry.blob_hash]));
    const stacks = split.stacks.map(stack => ({
        name: stack.name,
        cells: stack.cells.map(cell => ({
            name: cell.name,
            files: cell.files.map(file => ({ path: file.path, blob_hash: hashes.get(file.source)! })),
            dirs: cell.dirs,
        })),
    }));
    return { ok: true, root, stacks, leftOut: split.leftOut.length + expansion.skipped.length };
}

const withLeftOut = (summary: string, leftOut: number) => leftOut ? `${summary}; ${plural(leftOut, 'member')} left out` : summary;

const PACKAGE_STACKS = ['Capabilities'];

/** A packages export: `<channel>/Capabilities/<cell>/…`. */
export function packagesFromZip(expansion: ZipExpansion): ZipRead<PackagesZip> {
    const split = splitZip(expansion, 3, '<channel>/Capabilities/<cell>/…');
    if (!split.ok) return split;
    const others = split.stacks.map(stack => stack.name).filter(name => !PACKAGE_STACKS.includes(name));
    if (others.length) return { ok: false, error: `Only a Capabilities folder belongs in a packages zip; it also holds ${others.join(', ')}.` };
    const value = { cells: split.stacks.flatMap(stack => stack.cells) };
    return { ok: true, value, summary: withLeftOut(plural(value.cells.length, 'Capabilities cell'), split.leftOut) };
}

/** A main-cells export: `<channel>/<stack>/<cell>/…`. */
export function mainFromZip(expansion: ZipExpansion): ZipRead<MainZip> {
    const split = splitZip(expansion, 3, '<channel>/<stack>/<cell>/…');
    if (!split.ok) return split;
    const cells = split.stacks.reduce((n, stack) => n + stack.cells.length, 0);
    return { ok: true, value: { stacks: split.stacks }, summary: withLeftOut(`${plural(split.stacks.length, 'stack')}, ${plural(cells, 'cell')}`, split.leftOut) };
}

/** A tag export: `<tag>/<cell>/…`. */
export function historyFromZip(expansion: ZipExpansion): ZipRead<HistoryFile> {
    const split = splitZip(expansion, 2, '<tag>/<cell>/…');
    if (!split.ok) return split;
    const cells = split.stacks.flatMap(stack => stack.cells);
    return { ok: true, value: { root: split.root, cells }, summary: withLeftOut(plural(cells.length, 'cell'), split.leftOut) };
}

/** The suggestion's runner packages as drafts, each under a fresh id. */
export const suggestedDrafts = (suggestion: Suggestion): SuggestedRunner[] =>
    suggestion.runners.map(({ draft, quick_fields }) => ({
        draft: { runner: { ...draft.runner, id: newSlugId(draft.runner.name) }, files: draft.files },
        quick_fields,
    }));

/** What keeps a draft from running; a generator the suggestion lacks is
 *  itself the gap. */
export function draftReadiness(draft: RunnerDraft, suggestion: Suggestion, accounts: Account[]): RunnerReadiness {
    const generator = suggestion.runner_generators.find(g => g.id === draft.runner.generator);
    return generator ? runnerReadiness(draft, generator, accounts) : { missing: [`its generator (${draft.runner.generator})`], invalid: [] };
}

/** Why the Suggested tab cannot create yet, first reason first. */
export function suggestedBlockers(state: SuggestedState, suggestion: Suggestion | undefined, accounts: Account[]): string[] {
    if (!suggestion) return [state.loading ? 'The suggestions are loading.' : 'No suggestion is available.'];
    return state.drafts.filter(({ draft }) => !isRunnerReady(draftReadiness(draft, suggestion, accounts)))
        .map(({ draft }) => `Configure or remove ${draft.runner.name || draft.runner.id}.`);
}

const fileKind = (key: 'packages' | 'settings' | 'main' | 'history') => key === 'settings' ? 'a JSON file' : 'a zip';

function sectionBlocker<T>(key: 'packages' | 'settings' | 'main' | 'history', section: SectionState<T>): string | null {
    const label = SECTION_LABELS[key];
    if (section.mode === 'channel' && !section.channelId) return `Choose a channel for ${label}.`;
    if (section.mode !== 'file') return null;
    const file: PickedFile<T> | null = section.file;
    if (file?.status === 'reading') return `${label}: ${file.name} is still uploading.`;
    if (file?.status !== 'ready') return `Choose ${fileKind(key)} for ${label}.`;
    return null;
}

/** Why the Config tab cannot create yet. */
export function configBlockers(state: ConfigState): string[] {
    const blockers = [
        sectionBlocker('packages', state.packages),
        sectionBlocker('settings', state.settings),
        sectionBlocker('main', state.main),
        sectionBlocker('history', state.history),
        state.history.mode === 'channel' && state.history.channelId && !state.history.tag ? 'Choose a tag for History cells.' : null,
    ];
    return blockers.filter((blocker): blocker is string => blocker !== null);
}

const readyValue = <T,>(section: SectionState<T>): T | null => section.file?.status === 'ready' ? section.file.value : null;

/** The Config tab's selection: each section from its channel, its file,
 *  or null for None. Call only once configBlockers is empty. */
export function configSelection(state: ConfigState): ConfigSelection {
    const { packages, settings, main, history } = state;
    const historyFile = readyValue(history);
    const settingsDoc = readyValue(settings);
    return {
        packages: packages.mode === 'channel' ? { channel_id: packages.channelId } : packages.mode === 'file' ? readyValue(packages) : null,
        settings: settings.mode === 'channel' ? { channel_id: settings.channelId } : settings.mode === 'file' && settingsDoc ? { doc: settingsDoc } : null,
        main_cells: main.mode === 'channel' ? { channel_id: main.channelId } : main.mode === 'file' ? readyValue(main) : null,
        history_cells: history.mode === 'channel' ? { channel_id: history.channelId, tag: history.tag }
            : history.mode === 'file' && historyFile ? { tag: history.fileTag.trim(), cells: historyFile.cells } : null,
    };
}
