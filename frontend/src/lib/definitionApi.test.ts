import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api/client';
import {
    acceptHandler, addApprovedPackage, addBuiltinExample, deleteBuiltinExample, patchBuiltinExample, previewApproval, revertBuiltinSkills,
} from './definitionApi';

// A built-in handler's channel additions, against the host's routes.
describe('built-in handler clients', () => {
    afterEach(() => vi.restoreAllMocks());

    it.each([
        ['add an example', () => addBuiltinExample('Q&A', 'renderer', { source_cell_id: 'w', paths: ['a.renderer'], description: 'd', expect: 'invalid', feedback: 'f' }),
            'POST', '/channels/Q%26A/builtin/handlers/renderer/examples', { source_cell_id: 'w', paths: ['a.renderer'], description: 'd', expect: 'invalid', feedback: 'f' }],
        ['edit an added example', () => patchBuiltinExample('c', 'renderer', 'example-1', { feedback: 'g' }),
            'PATCH', '/channels/c/builtin/handlers/renderer/examples/example-1', { feedback: 'g' }],
        ['remove an added example', () => deleteBuiltinExample('c', 'generator', 'example-1'),
            'DELETE', '/channels/c/builtin/handlers/generator/examples/example-1', undefined],
        ['go back to the app\'s skills', () => revertBuiltinSkills('c', 'handler'), 'DELETE', '/channels/c/builtin/handlers/handler/skills', undefined],
        ['accept regenerated skills on the Definitions card', () => acceptHandler('c', 'defs', 'renderer', 'h'),
            'POST', '/channels/c/cells/defs/handler-build/accept', { folder: 'renderer', expected_content_hash: 'h' }],
    ])('%s calls its route', async (_label, call, method, url, data) => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: {} });
        await call();
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method, url, data }));
    });
});

// Approve and add: a definition from a cell's Files panel into Capabilities,
// named by its target cell only (Capabilities is the one target stack).
describe('approval clients', () => {
    afterEach(() => vi.restoreAllMocks());

    const request = { source_cell_id: 'work', definition_path: 'memo/memo.handler', target_cell_id: 'caps-cell' };

    it('previews with the source and the target cell, and reads the replacing target and the build facts', async () => {
        const preview = {
            targets: [
                { cell_id: null, name: 'New cell', replaces: false },
                { cell_id: 'caps-cell', name: 'Memo', replaces: true },
            ],
            content_hash: 'c', source_version_id: null, kind: 'handler', id: 'memo', folder: 'memo', files: ['memo/memo.handler'],
            summary: { name: 'Memo', derived_hash: 'd1', kept_from_target: ['memo/skills/tips/SKILL.md'] },
            replacement: null, target_revision: 'r',
        };
        const spy = vi.spyOn(api, 'request').mockResolvedValue({ data: preview });
        const answer = await previewApproval('Q&A', request);
        expect(spy).toHaveBeenCalledWith(expect.objectContaining({ method: 'POST', url: '/channels/Q%26A/packages/preview', data: request }));
        expect(answer.targets.filter(t => t.replaces).map(t => t.cell_id)).toEqual(['caps-cell']);
        expect(answer.summary.derived_hash).toBe('d1');
        expect(answer.summary.kept_from_target).toEqual(['memo/skills/tips/SKILL.md']);
    });

    it('adds with the expected hashes and the replace choice', async () => {
        const spy = vi.spyOn(api, 'request').mockResolvedValue({ data: { cell_id: 'caps-cell', message: 'Added' } });
        const body = { ...request, expected_content_hash: 'c', expected_target_revision: 'r', replace: true };
        expect(await addApprovedPackage('c', body)).toEqual({ cell_id: 'caps-cell', message: 'Added' });
        expect(spy).toHaveBeenCalledWith(expect.objectContaining({ method: 'POST', url: '/channels/c/packages/add', data: body }));
    });
});
