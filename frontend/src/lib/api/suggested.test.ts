import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { fetchSuggested, setSuggestedSource } from './suggested';

// The new-channel page's Suggested tab against GET /api/suggested and its
// source route.
describe('the suggested catalog clients', () => {
    afterEach(() => vi.restoreAllMocks());

    it.each([
        ['the catalog of its source', () => fetchSuggested(), 'GET', '/suggested?refresh=0', undefined],
        ['a refresh from the latest release', () => fetchSuggested(true), 'GET', '/suggested?refresh=1', undefined],
        ['a folder on the server', () => setSuggestedSource('/home/me/sdk'), 'PUT', '/suggested/source', { path: '/home/me/sdk' }],
        ['no folder again', () => setSuggestedSource(null), 'PUT', '/suggested/source', { path: null }],
    ])('%s calls its route', async (_label, call, method, url, data) => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { ok: true } });
        expect(await call()).toEqual({ ok: true });
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method, url, data }));
    });
});
