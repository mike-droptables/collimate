import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import {
    addFromCatalog, fetchCapabilityCatalog, fetchCapabilityProblems, offerBuiltinSkills, spawnCapabilityCreate, spawnHandlerBuild,
} from './capabilitiesStack';
import { reportPackageSkips } from '../packageReports';

vi.mock('../packageReports', () => ({ reportPackageSkips: vi.fn(async () => {}) }));

describe('the Capabilities stack clients', () => {
    afterEach(() => vi.restoreAllMocks());

    const layout = { id: 'ch', name: 'ch', main_stacks: [], nav_stacks: [], nav_selection_id: null, main_selection_id: 'w' };

    it.each([
        ['a handler build', () => spawnHandlerBuild('Q&A', { cell_id: 'c1', folder: 'memo' }),
            'POST', '/channels/Q%26A/capabilities/build', { cell_id: 'c1', folder: 'memo' }],
        ['a create', () => spawnCapabilityCreate('c', {
            title: 'Memo', make: ['handler', 'renderer'],
            answers: [{ question: 'What should it do?', answer: 'Check memos' }], related: [{ kind: 'handler', id: 'memo' }],
        }), 'POST', '/channels/c/capabilities/create', {
            title: 'Memo', make: ['handler', 'renderer'],
            answers: [{ question: 'What should it do?', answer: 'Check memos' }], related: [{ kind: 'handler', id: 'memo' }],
        }],
        ['a catalog add', () => addFromCatalog('c', { suggestion_id: 'synth set', folder: 'generators/synth-gen' }),
            'POST', '/channels/c/capabilities/add', { suggestion_id: 'synth set', folder: 'generators/synth-gen' }],
    ])('%s posts to its route and answers the layout', async (_label, call, method, url, data) => {
        const answer = { ...layout, stack_id: 'w', cell_id: 'new', brief_path: 'brief.md', package_skips: [] };
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: answer });
        expect(await call()).toEqual(answer);
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method, url, data }));
    });

    it('a catalog add reports the packages admission left out', async () => {
        const skips = [{ cell_id: 'new', path: 'beta/choice.generator', id: 'beta', reason: 'Definition id already exists elsewhere in Capabilities' }];
        vi.spyOn(api, 'request').mockResolvedValue({ data: { ...layout, cell_id: 'new', package_skips: skips } });
        expect((await addFromCatalog('c', { suggestion_id: 's', folder: 'gens/mixed' })).cell_id).toBe('new');
        expect(reportPackageSkips).toHaveBeenCalledWith(skips);
    });

    it('reads the channel’s catalog, each cell with whether it is held', async () => {
        const suggestions = [{ id: 'starter', name: 'Starter', description: '', cells: [{ folder: 'views/charts', name: 'Charts', section: 'renderer', held: true }] }];
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { suggestions } });
        expect(await fetchCapabilityCatalog('Q&A')).toEqual(suggestions);
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'GET', url: '/channels/Q%26A/capabilities/catalog' }));
    });

    it('reads the problems listing', async () => {
        const problems = [{ cell_id: 'c1', path: 'memo/memo.handler', kind: 'handler', error: 'bad yaml' }];
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { problems } });
        expect(await fetchCapabilityProblems('Q&A')).toEqual(problems);
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'GET', url: '/channels/Q%26A/capabilities/problems' }));
    });

    it('offers regenerated skills to a built-in handler and answers its record', async () => {
        const handler = { id: 'memo', builtin: true };
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { handler } });
        expect(await offerBuiltinSkills('c', 'memo', { source_cell_id: 'work', folder: 'memo' })).toEqual(handler);
        expect(request).toHaveBeenCalledWith(expect.objectContaining({
            method: 'POST', url: '/channels/c/builtin/handlers/memo/skills', data: { source_cell_id: 'work', folder: 'memo' },
        }));
    });
});
