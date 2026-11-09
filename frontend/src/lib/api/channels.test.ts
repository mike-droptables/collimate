import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { createChannel, type CreateChannelBody } from './channels';

const { reportPackageSkips, reportRepositorySkips } = vi.hoisted(() => ({
    reportPackageSkips: vi.fn(async () => {}), reportRepositorySkips: vi.fn(async () => {}),
}));
vi.mock('../packageReports', () => ({ reportPackageSkips, reportRepositorySkips }));

describe('creating a channel', () => {
    afterEach(() => vi.restoreAllMocks());

    it('posts the name and the body, then reports the skipped packages', async () => {
        const skips = [{ path: 'Capabilities/x', id: 'x', reason: 'refused' }];
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { id: 'c1', name: 'Q&A', package_skips: skips } });
        const body: CreateChannelBody = { config: { packages: { channel_id: 'src' }, main_cells: null, history_cells: { channel_id: 'src', tag: 'done' } } };
        expect(await createChannel('Q&A', body)).toMatchObject({ id: 'c1' });
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'POST', url: '/channels?name=Q%26A', data: body }));
        expect(reportPackageSkips).toHaveBeenCalledWith(skips);
    });

    it('reports the repositories whose git history was not kept', async () => {
        const skipped = [{ path: 'Repo/proj/.git', reason: 'repository history not copied: git is not available on this host' }];
        vi.spyOn(api, 'request').mockResolvedValue({ data: { id: 'c3', name: 'Copy', package_skips: [], skipped } });
        await createChannel('Copy', { config: { packages: null, main_cells: { channel_id: 'src' }, history_cells: null } });
        expect(reportRepositorySkips).toHaveBeenCalledWith(skipped);
    });

    it('sends no body for an empty channel', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { id: 'c2', name: 'Empty', package_skips: [] } });
        await createChannel('Empty');
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'POST', url: '/channels?name=Empty', data: undefined }));
    });
});
