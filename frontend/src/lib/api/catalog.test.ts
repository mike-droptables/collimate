import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { fetchGenerators } from './catalog';

describe('the catalog client', () => {
    afterEach(() => vi.restoreAllMocks());

    it('fetchGenerators reads GET /generators and returns its rows (polish)', async () => {
        const rows = [{ name: 'agent', display_name: 'Agent', summary: '', cell_id: 'c', group: 'Agent', group_icon: null,
            daemon: false, chat: true, config_folders: [], live_files_mode: 'none',
            version: '0.5.0', entrypoint: 'agent.py', ui_files: ['.chat'], requires_docker: false }];
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: rows });
        expect(await fetchGenerators()).toEqual(rows);
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'GET', url: '/generators' }));
    });
});
