import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { createChannel, type CreateChannelBody } from './channels';

const reportPackageSkips = vi.hoisted(() => vi.fn(async () => {}));
vi.mock('../packageReports', () => ({ reportPackageSkips }));

describe('creating a channel', () => {
    afterEach(() => vi.restoreAllMocks());

    it('posts the name and the body, then reports the skipped packages', async () => {
        const skips = [{ path: 'Capabilities/x', id: 'x', reason: 'refused' }];
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { id: 'c1', name: 'Q&A', package_skips: skips } });
        const body: CreateChannelBody = { config: { packages: { channel_id: 'src' }, settings: null, main_cells: null, history_cells: { channel_id: 'src', tag: 'done' } } };
        expect(await createChannel('Q&A', body)).toMatchObject({ id: 'c1' });
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'POST', url: '/channels?name=Q%26A', data: body }));
        expect(reportPackageSkips).toHaveBeenCalledWith(skips);
    });

    it('sends no body for an empty channel', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { id: 'c2', name: 'Empty', package_skips: [] } });
        await createChannel('Empty');
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'POST', url: '/channels?name=Empty', data: undefined }));
    });
});
