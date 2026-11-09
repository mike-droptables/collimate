import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { fetchSuggested } from './suggested';

// The new-channel page's Suggested tab against GET /api/suggested.
describe('the suggested catalog client', () => {
    afterEach(() => vi.restoreAllMocks());

    it('reads the catalog from its one route, with no query', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { ok: true } });
        expect(await fetchSuggested()).toEqual({ ok: true });
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'GET', url: '/suggested', data: undefined }));
    });
});
