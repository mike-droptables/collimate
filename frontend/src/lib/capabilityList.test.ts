import { describe, expect, it } from 'vitest';
import type { GeneratorEntry, RendererEntry } from './api/catalog';
import type { LibraryEntry } from './api/capabilitiesStack';
import type { HandlerInfo } from './definitionApi';
import type { RunnerEntry } from './runnerApi';
import {
    CAPABILITY_KINDS, capabilityEntries, catalogInCell, groupCapabilities, inCell, matchesTerms, runnerReadinessLabel, searchTerms,
} from './capabilityList';

const generator = (over: Partial<GeneratorEntry> = {}): GeneratorEntry => ({
    name: 'synth-gen', display_name: 'Synth generator', summary: 'Makes synthetic files.', cell_id: 'tools', group: 'Tools', group_icon: null,
    daemon: false, chat: false, config_folders: [], live_files_mode: 'none', version: null, entrypoint: null, ui_files: [], requires_docker: false, ...over,
});
const runner = (over: Partial<RunnerEntry> = {}): RunnerEntry => ({
    id: 'synth-run', name: 'Synth run', description: 'Runs it nightly.', tags: ['nightly'], generator: 'synth-gen', prompt: '', settings: {},
    accounts: {}, folders: [], permissions: {}, packages: {}, revision: 'r1', cell_id: 'runs', ...over,
});
const renderer = (over: Partial<RendererEntry> = {}): RendererEntry => ({
    id: 'memo-view', name: 'Memo view', version: '1', summary: 'Shows memos.', extensions: ['.memo'], default_for: [], supports_diff: false, embed: 'native',
    content_hash: 'h', cell_id: 'views', available: true, ...over,
});
const handler = (over: Partial<HandlerInfo> = {}): HandlerInfo => ({
    id: 'memo', name: 'Memo', description: 'A memo with a heading.', types: [{ ext: '.memo' }], examples: [], cell_id: 'views', folder: 'memo',
    definition_path: 'memo/memo.handler', has_derived: false, built_hash: null, accepted: false, accepted_derived: null, disabled: false,
    clashes: [], accepted_skills: [], added_skills: [], builtin: false, uses_app_skills: false, ...over,
});
const library = (over: Partial<LibraryEntry> = {}): LibraryEntry => ({
    id: 'shared-lib', name: 'Shared lib', description: 'Helpers for charts.', version: '1', app: false, cell_id: 'views', folder: 'shared-lib', named_by: ['tools'], ...over,
});
const NAMES: Record<string, string> = { tools: 'Tools', runs: 'Run box', views: 'Views', card: 'Definitions' };
const cellName = (id: string | undefined) => (id ? NAMES[id] ?? '' : '');

const entries = () => capabilityEntries({
    generators: [generator(), generator({ name: 'other-gen', display_name: 'Other', summary: '', cell_id: 'views' })],
    runners: [runner()],
    renderers: [renderer(), renderer({ id: 'text', name: 'Text', summary: '', extensions: ['.txt'], cell_id: 'card', builtin: true })],
    handlers: [handler(), handler({ id: 'status', name: 'Status', description: '', types: [{ ext: '.status' }], cell_id: 'card', folder: 'status', builtin: true })],
}, cellName);

describe('the manage view’s search', () => {
    it('casefolds and splits on whitespace', () => {
        expect(searchTerms('  Synth   RUN ')).toEqual(['synth', 'run']);
        expect(searchTerms('')).toEqual([]);
    });

    it('needs every term, each in any of the entry’s words', () => {
        const entry = { terms: ['Synth run', 'Runs it nightly.'] };
        expect(matchesTerms(entry, ['synth', 'nightly'])).toBe(true);
        expect(matchesTerms(entry, ['synth', 'weekly'])).toBe(false);
        expect(matchesTerms(entry, [])).toBe(true);
    });

    it('matches name, id, summary, file types, a runner’s generator name and the holding cell', () => {
        const all = entries();
        const hits = (query: string) => {
            const grouped = groupCapabilities(all, { query, kinds: [] });
            return grouped.groups.flatMap(group => [...group.user, ...group.builtin].map(entry => `${entry.kind}:${entry.key}`));
        };
        expect(hits('synthetic')).toEqual(['generator:synth-gen']);
        expect(hits('.status')).toEqual(['handler:card:status']);
        expect(hits('memo-view')).toEqual(['renderer:memo-view']);
        // A runner matches its generator's display name and its tags.
        expect(hits('synth generator')).toEqual(['generator:synth-gen', 'runner:synth-run']);
        expect(hits('nightly')).toEqual(['runner:synth-run']);
        // The holding cell's name.
        expect(hits('run box')).toEqual(['runner:synth-run']);
        expect(hits('views')).toEqual(['generator:other-gen', 'renderer:memo-view', 'handler:views:memo']);
    });
});

describe('groupCapabilities', () => {
    it('keeps the fixed group order, the user’s entries before the built-ins', () => {
        const grouped = groupCapabilities(entries(), { query: '', kinds: [] });
        expect(grouped.groups.map(group => group.kind)).toEqual(CAPABILITY_KINDS);
        const renderers = grouped.groups[2];
        expect(renderers.user.map(entry => entry.key)).toEqual(['memo-view']);
        expect(renderers.builtin.map(entry => entry.key)).toEqual(['text']);
        expect(grouped.filtering).toBe(false);
        expect(grouped.matched).toBe(7);
    });

    it('the chips choose which groups show; the counts follow the search, whatever is pressed', () => {
        const grouped = groupCapabilities(entries(), { query: 'memo', kinds: ['handler', 'renderer'] });
        expect(grouped.groups.map(group => group.kind)).toEqual(['renderer', 'handler']);
        expect(grouped.counts).toEqual({ generator: 0, runner: 0, renderer: 1, handler: 1, library: 0 });
        expect(grouped.filtering).toBe(true);
        expect(grouped.groups.map(group => [group.total, group.user.length + group.builtin.length])).toEqual([[2, 1], [2, 1]]);
        expect(grouped.matched).toBe(2);
    });

    it('lists nothing for a kind still loading', () => {
        const grouped = groupCapabilities(capabilityEntries({}, cellName), { query: '', kinds: [] });
        expect(grouped.groups.every(group => group.total === 0)).toBe(true);
        expect(grouped.counts).toEqual({ generator: 0, runner: 0, renderer: 0, handler: 0, library: 0 });
    });
});

describe('a runner’s status chip', () => {
    it('reads Ready, Needs setup with the fields, or Broken with the reason', () => {
        expect(runnerReadinessLabel(runner())).toEqual({ tone: 'ready', label: 'Ready' });
        expect(runnerReadinessLabel(runner({ missing_required: [{ key: 'k', label: 'API key' }], invalid_options: [{ key: 'm', label: 'Model' }, { key: 'k', label: 'API key' }] })))
            .toEqual({ tone: 'setup', label: 'Needs setup: API key, Model' });
        expect(runnerReadinessLabel(runner({ invalid: { reason: 'Target generator is unavailable' }, missing_required: [{ key: 'k', label: 'API key' }] })))
            .toEqual({ tone: 'broken', label: 'Broken: Target generator is unavailable' });
    });
});

describe('library entries', () => {
    it('a channel library is not built in, the app\'s is; the terms hold the id, name, description and cells', () => {
        const all = capabilityEntries({ libraries: [library(), library({ id: 'app-lib', app: true, cell_id: undefined, named_by: [] })] }, cellName);
        expect(all.library.map(e => [e.kind, e.builtin])).toEqual([['library', false], ['library', true]]);
        expect(all.library[0].terms).toEqual(expect.arrayContaining(['shared-lib', 'Shared lib', 'Helpers for charts.', 'Views', 'Tools']));
    });
});

describe('a cell\'s Manage tab narrows the catalogs to that cell', () => {
    it('inCell keeps the records naming the cell, problems included', () => {
        const problems = [{ cell_id: 'views', path: 'x.renderer', error: 'bad' }, { cell_id: 'tools', path: 'y.generator', error: 'bad' }];
        expect(inCell(problems, 'views')).toEqual([problems[0]]);
        expect(inCell([renderer({ cell_id: undefined })], 'views')).toEqual([]);
    });

    it('catalogInCell narrows every list and leaves a loading list absent', () => {
        const data = {
            generators: [generator(), generator({ name: 'other-gen', cell_id: 'views' })],
            runners: [runner(), runner({ id: 'here', cell_id: 'views' })],
            renderers: [renderer(), renderer({ id: 'plain', cell_id: 'card', builtin: true })],
            handlers: undefined,
            libraries: [library(), library({ id: 'app-lib', cell_id: undefined, app: true })],
        };
        const views = catalogInCell(data, 'views');
        expect(views.generators?.map(g => g.name)).toEqual(['other-gen']);
        expect(views.runners?.map(r => r.id)).toEqual(['here']);
        expect(views.renderers?.map(r => r.id)).toEqual(['memo-view']);
        expect(views.handlers).toBeUndefined();
        expect(views.libraries?.map(l => l.id)).toEqual(['shared-lib']);
        // A built-in card holds its built-ins.
        const card = catalogInCell(data, 'card');
        expect(card.renderers?.map(r => r.id)).toEqual(['plain']);
        expect(card.generators).toEqual([]);
        // Grouped, the narrowed entries count only what the cell holds.
        const grouped = groupCapabilities(capabilityEntries(views, cellName), { query: '', kinds: [] });
        expect(grouped.counts).toEqual({ generator: 1, runner: 1, renderer: 1, handler: 0, library: 1 });
    });
});
