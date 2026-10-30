import type { GeneratorEntry, RendererEntry } from './api/catalog';
import type { HandlerInfo } from './definitionApi';
import type { RunnerEntry } from './runnerApi';
import type { CapabilityKind } from './types';

/** The manage view's groups, in their fixed order. */
export const CAPABILITY_KINDS: readonly CapabilityKind[] = ['generator', 'runner', 'renderer', 'handler'];

/** What each kind's entry carries. */
export interface CapabilityItems {
    generator: GeneratorEntry;
    runner: RunnerEntry;
    renderer: RendererEntry;
    handler: HandlerInfo;
}

/** One generator, runner, renderer or handler of the channel, with the words
 *  the manage view's search matches. */
export interface CapabilityEntry<K extends CapabilityKind = CapabilityKind> {
    kind: K;
    /** Unique within its kind. */
    key: string;
    /** One of the app's own (a built-in card holds it). */
    builtin: boolean;
    /** Name, id, summary or description, file types, a runner's generator
     *  name and the holding cell's name. */
    terms: string[];
    item: CapabilityItems[K];
}

export type CapabilityEntries = { [K in CapabilityKind]: CapabilityEntry<K>[] };

/** One group of the list: the user's entries that match, then the
 *  built-ins that match, out of `total`. */
export interface CapabilityGroup<K extends CapabilityKind = CapabilityKind> {
    kind: K;
    total: number;
    user: CapabilityEntry<K>[];
    builtin: CapabilityEntry<K>[];
}

/** A group of any one kind: switch on `kind` to narrow its entries. */
export type AnyCapabilityGroup = { [K in CapabilityKind]: CapabilityGroup<K> }[CapabilityKind];

export interface CapabilityFilter {
    query: string;
    /** The pressed kind chips; none pressed shows every kind. */
    kinds: readonly CapabilityKind[];
}

export interface GroupedCapabilities {
    /** A search is narrowing the groups (their headers read 'n of m'). */
    filtering: boolean;
    /** Each kind's matches under the search, whichever chips are pressed
     *  (the chips' counts). */
    counts: Record<CapabilityKind, number>;
    /** The groups the chips show, in CAPABILITY_KINDS order. */
    groups: AnyCapabilityGroup[];
    /** Every match of the shown groups. */
    matched: number;
}

/** A search's terms: casefolded, split on whitespace. */
export function searchTerms(query: string): string[] {
    return query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
}

/** Every term appears in one of the entry's words. */
export function matchesTerms(entry: Pick<CapabilityEntry, 'terms'>, terms: readonly string[]): boolean {
    if (!terms.length) return true;
    const words = entry.terms.map(term => term.toLocaleLowerCase());
    return terms.every(term => words.some(word => word.includes(term)));
}

/** Group the entries by kind for the manage view: the search narrows every
 *  group, the chips choose which groups show. */
export function groupCapabilities(entries: CapabilityEntries, filter: CapabilityFilter): GroupedCapabilities {
    const terms = searchTerms(filter.query);
    const shownKinds = filter.kinds.length ? CAPABILITY_KINDS.filter(kind => filter.kinds.includes(kind)) : CAPABILITY_KINDS;
    const counts = {} as Record<CapabilityKind, number>;
    const all = {} as Record<CapabilityKind, AnyCapabilityGroup>;
    for (const kind of CAPABILITY_KINDS) {
        const list = entries[kind] as CapabilityEntry[];
        const hits = list.filter(entry => matchesTerms(entry, terms));
        counts[kind] = hits.length;
        all[kind] = { kind, total: list.length, user: hits.filter(entry => !entry.builtin), builtin: hits.filter(entry => entry.builtin) } as AnyCapabilityGroup;
    }
    const groups = shownKinds.map(kind => all[kind]);
    return {
        filtering: terms.length > 0,
        counts,
        groups,
        matched: groups.reduce((sum, group) => sum + group.user.length + group.builtin.length, 0),
    };
}

/** What the catalogs hold, each list absent while it loads or fails. */
export interface CapabilityCatalogData {
    generators?: GeneratorEntry[];
    runners?: RunnerEntry[];
    renderers?: RendererEntry[];
    handlers?: HandlerInfo[];
}

const words = (...values: (string | null | undefined)[]): string[] => values.filter((value): value is string => !!value);

/** The catalogs as manage-view entries. `cellName` names the cell that
 *  holds an entry (its search matches it too). */
export function capabilityEntries(data: CapabilityCatalogData, cellName: (cellId: string | undefined) => string): CapabilityEntries {
    const generatorName = new Map((data.generators ?? []).map(generator => [generator.name, generator.display_name]));
    return {
        generator: (data.generators ?? []).map(generator => ({
            kind: 'generator', key: generator.name, builtin: false, item: generator,
            terms: words(generator.display_name, generator.name, generator.summary, cellName(generator.cell_id)),
        })),
        runner: (data.runners ?? []).map(runner => ({
            kind: 'runner', key: runner.id, builtin: false, item: runner,
            terms: words(runner.name, runner.id, runner.description, runner.generator, generatorName.get(runner.generator), cellName(runner.cell_id), ...runner.tags),
        })),
        renderer: (data.renderers ?? []).map(renderer => ({
            kind: 'renderer', key: renderer.id, builtin: !!renderer.builtin, item: renderer,
            terms: words(renderer.name, renderer.id, renderer.summary, ...(renderer.extensions ?? []), cellName(renderer.cell_id)),
        })),
        handler: (data.handlers ?? []).map(handler => ({
            kind: 'handler', key: `${handler.cell_id}:${handler.folder}`, builtin: handler.builtin, item: handler,
            terms: words(handler.name, handler.id, handler.description, ...handler.types.map(type => type.ext), cellName(handler.cell_id)),
        })),
    };
}

/** A runner's status chip: ready to run, missing settings, or broken. */
export function runnerReadinessLabel(runner: Pick<RunnerEntry, 'invalid' | 'missing_required' | 'invalid_options'>): { tone: 'ready' | 'setup' | 'broken'; label: string } {
    if (runner.invalid) return { tone: 'broken', label: `Broken: ${runner.invalid.reason}` };
    const fields = [...(runner.missing_required ?? []), ...(runner.invalid_options ?? [])].map(field => field.label);
    if (fields.length) return { tone: 'setup', label: `Needs setup: ${[...new Set(fields)].join(', ')}` };
    return { tone: 'ready', label: 'Ready' };
}
