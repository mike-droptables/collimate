import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api/client';
import { saveRunner, type RunnerDraft } from './runnerApi';

const draft = { runner: { id: 'nightly', name: 'Nightly', generator: 'synth-gen' }, files: [] } as unknown as RunnerDraft;
const sent = (request: ReturnType<typeof vi.spyOn>) => (request.mock.calls[0][0] as { method: string; url: string; data: Record<string, unknown> });

describe('saveRunner', () => {
    afterEach(() => vi.restoreAllMocks());

    it.each([
        ['its generator’s cell', undefined, {}],
        ['a Capabilities cell', { cellId: 'views' } as const, { cell_id: 'views' }],
        ['a new cell on Capabilities', { newCell: true } as const, { new_cell: true }],
    ])('a new runner kept in %s posts where it goes', async (_label, home, where) => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: {} });
        await saveRunner(draft, false, home);
        expect(sent(request)).toMatchObject({ method: 'POST', url: '/runners' });
        expect(sent(request).data).toEqual({ runner: draft.runner, files: [], ...where });
    });

    it('an existing runner is saved where it lives, never moved', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: {} });
        await saveRunner({ ...draft, revision: 'r1' }, true, { newCell: true });
        expect(sent(request)).toMatchObject({ method: 'PUT', url: '/runners/nightly' });
        expect(sent(request).data).toEqual({ runner: draft.runner, files: [], revision: 'r1' });
    });
});
