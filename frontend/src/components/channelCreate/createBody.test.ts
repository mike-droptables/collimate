import { describe, expect, it } from 'vitest';
import type { ZipExpansion } from '@/lib/api/transfer';
import type { Suggestion } from '@/lib/api/suggested';
import type { RunnerDraft, RunnerGenerator } from '@/lib/runnerApi';
import {
    configBlockers, configSelection, draftReadiness, historyFromZip, mainFromZip, packagesFromZip, suggestedBlockers, suggestedDrafts,
} from './createBody';
import { initialConfig, initialSuggested, initialTab, type ConfigState } from './state';

const entry = (path: string, blob_hash = `h:${path}`) => ({ path, blob_hash, size: 1, encoding: 'utf-8' as const });
const zip = (paths: string[], extra: Partial<ZipExpansion> = {}): ZipExpansion => ({ entries: paths.map(path => entry(path)), dirs: [], skipped: [], ...extra });

describe('reading an export zip as a section', () => {
    it('a packages zip: its Capabilities cells, empty folders kept, reserved paths left out', () => {
        const read = packagesFromZip(zip(['Ch/Capabilities/Tool/tool/tool.generator', 'Ch/Capabilities/Tool/_runs/r1/log.txt'], { dirs: ['Ch/Capabilities/Tool/empty', 'Ch/Capabilities/Blank'], skipped: [{ path: '../x', reason: 'escapes' }] }));
        expect(read).toEqual({ ok: true, value: { cells: [
            { name: 'Tool', files: [{ path: 'tool/tool.generator', blob_hash: 'h:Ch/Capabilities/Tool/tool/tool.generator' }], dirs: ['empty'] },
            { name: 'Blank', files: [], dirs: [] },
        ] }, summary: '2 Capabilities cells; 2 members left out' });
    });

    it('refuses an empty zip, several top folders, loose files and folders that are no package kind', () => {
        expect(packagesFromZip(zip([]))).toEqual({ ok: false, error: 'The zip holds no files.' });
        expect(packagesFromZip(zip(['A/Capabilities/X/f', 'B/Capabilities/Y/f']))).toMatchObject({ ok: false, error: expect.stringContaining('one top folder') });
        expect(packagesFromZip(zip(['A/Capabilities/f']))).toMatchObject({ ok: false, error: expect.stringMatching(/^A\/Capabilities\/f sits outside a cell folder: <channel>\/Capabilities\/<cell>\/…/) });
        expect(packagesFromZip(zip(['A/Capabilities/X/f', 'A/Generators/Y/f', 'A/Main/Z/f']))).toEqual({ ok: false, error: 'Only a Capabilities folder belongs in a packages zip; it also holds Generators, Main.' });
    });

    it('a main zip: its stacks in order with their cells', () => {
        expect(mainFromZip(zip(['Ch/Notes/Ideas/a.md', 'Ch/Plans/Q1/b.md', 'Ch/Notes/Todo/c.md']))).toEqual({ ok: true, value: { stacks: [
            { name: 'Notes', cells: [{ name: 'Ideas', files: [{ path: 'a.md', blob_hash: 'h:Ch/Notes/Ideas/a.md' }], dirs: [] }, { name: 'Todo', files: [{ path: 'c.md', blob_hash: 'h:Ch/Notes/Todo/c.md' }], dirs: [] }] },
            { name: 'Plans', cells: [{ name: 'Q1', files: [{ path: 'b.md', blob_hash: 'h:Ch/Plans/Q1/b.md' }], dirs: [] }] },
        ] }, summary: '2 stacks, 3 cells' });
        expect(mainFromZip(zip(['Ch/Notes/a.md']))).toMatchObject({ ok: false });
    });

    it('a tag zip: its cells and the root folder the tag is read from', () => {
        expect(historyFromZip(zip(['done/Ideas/a.md', 'done/Plans/sub/b.md']))).toEqual({ ok: true, value: { root: 'done', cells: [
            { name: 'Ideas', files: [{ path: 'a.md', blob_hash: 'h:done/Ideas/a.md' }], dirs: [] },
            { name: 'Plans', files: [{ path: 'sub/b.md', blob_hash: 'h:done/Plans/sub/b.md' }], dirs: [] },
        ] }, summary: '2 cells' });
        expect(historyFromZip(zip(['done/a.md']))).toMatchObject({ ok: false, error: expect.stringContaining('<tag>/<cell>/…') });
    });
});

const generator: RunnerGenerator = {
    id: 'agent', name: 'Agent', main_config: 'settings.set_yaml', fields: [], settings_files: {}, config_folders: [], enforces: [],
    account_requirements: { model: { label: 'Agent model', kind: 'model' } }, configs: [],
    default: { runner: { name: '', description: '', tags: [], prompt: '', settings: {}, accounts: {}, folders: [], permissions: {}, packages: {} }, files: [] },
};
const draft = (id: string, gen: string): RunnerDraft => ({ runner: { ...generator.default.runner, id, generator: gen, name: id }, files: [] });
const suggestion: Suggestion = {
    id: 'standard', name: 'Standard', description: '', generators: [], renderers: [], tutorial: [],
    runner_generators: [generator], runners: [
        { draft: draft('agent-chat', 'agent'), quick_fields: [{ account: 'model' }] },
        { draft: draft('agent-chat', 'agent'), quick_fields: [{ prompt: true }] },
    ],
};

describe('the Suggested tab', () => {
    it('offers each runner package under a fresh id', () => {
        const [a, b] = suggestedDrafts(suggestion);
        expect(a.draft.runner.id).toMatch(/^agent-chat-[0-9a-f]{8}$/);
        expect(b.draft.runner.id).not.toBe(a.draft.runner.id);
        expect(a.quick_fields).toEqual([{ account: 'model' }]);
        expect(b.quick_fields).toEqual([{ prompt: true }]);
        expect(suggestion.runners[0].draft.runner.id).toBe('agent-chat');
    });

    it('counts a generator the suggestion lacks as a gap', () => {
        expect(draftReadiness(draft('x', 'gone'), suggestion, [])).toEqual({ missing: ['its generator (gone)'], invalid: [] });
    });

    it('waits for the catalog and every draft being ready', () => {
        const state = { ...initialSuggested(), drafts: [{ draft: draft('agent-chat', 'agent'), quick_fields: [{ account: 'model' as const }] }] };
        expect(suggestedBlockers(state, undefined, [])).toEqual(['The suggestions are loading.']);
        expect(suggestedBlockers({ ...state, loading: false }, undefined, [])).toEqual(['No suggestion is available.']);
        expect(suggestedBlockers(state, suggestion, [])).toEqual(['Configure or remove agent-chat.']);
        expect(suggestedBlockers({ ...state, drafts: [] }, suggestion, [])).toEqual([]);
    });
});

describe('the Config tab', () => {
    const channels = [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }];

    it('opens on Config with channels, copying packages and settings from the source channel', () => {
        expect(initialTab(channels)).toBe('config');
        expect(initialTab([])).toBe('suggested');
        const state = initialConfig(channels, 'b');
        expect([state.packages.mode, state.packages.channelId, state.settings.mode, state.main.mode, state.history.mode]).toEqual(['channel', 'b', 'channel', 'none', 'none']);
        expect(initialConfig(channels, 'gone').packages.channelId).toBe('a');
        expect(initialConfig([], null).packages.mode).toBe('none');
    });

    it('names what each unfinished section still needs, and builds the selection once none remain', () => {
        const base = initialConfig(channels, 'a');
        const state: ConfigState = {
            ...base,
            packages: { ...base.packages, mode: 'file', file: { status: 'reading', name: 'p.zip' } },
            settings: { ...base.settings, mode: 'file', file: { status: 'refused', name: 's.json', error: 'bad' } },
            main: { ...base.main, mode: 'channel', channelId: '' },
            history: { ...base.history, mode: 'channel' },
        };
        expect(configBlockers(state)).toEqual([
            'Capabilities: p.zip is still uploading.',
            'Choose a JSON file for Settings.',
            'Choose a channel for Main cells.',
            'Choose a tag for History cells.',
        ]);
        const packages = { cells: [] };
        const ready: ConfigState = {
            ...state,
            packages: { ...state.packages, file: { status: 'ready', name: 'p.zip', value: packages, summary: '' } },
            settings: { ...state.settings, mode: 'none' },
            main: { ...state.main, mode: 'file', file: { status: 'ready', name: 'm.zip', value: { stacks: [] }, summary: '' } },
            history: { ...state.history, mode: 'file', fileTag: '  done ', file: { status: 'ready', name: 'h.zip', value: { root: 'done', cells: [] }, summary: '' } },
        };
        expect(configBlockers(ready)).toEqual([]);
        expect(configSelection(ready)).toEqual({ packages, settings: null, main_cells: { stacks: [] }, history_cells: { tag: 'done', cells: [] } });
        expect(configBlockers({ ...ready, main: { ...ready.main, file: null } })).toEqual(['Choose a zip for Main cells.']);
    });
});
