import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AxiosProgressEvent } from 'axios';
import { api } from './client';
import { commitImport, expandZip, expandZipForNewChannel, exportFileUrl, exportZipUrl, fetchExportManifest, releaseExport, responseError, streamBlob, uploadRepository } from './transfer';
import { discardRescue, rescueExportUrl, retryRescue } from './repositories';

afterEach(() => vi.restoreAllMocks());

describe('the transfer client (TR-07…TR-10)', () => {
    it('streamBlob posts the Blob itself as the body and reports upload progress', async () => {
        const body = new Blob(['hello'], { type: 'text/markdown' });
        const post = vi.spyOn(api, 'post').mockImplementation(async (_url, _data, config) => {
            config?.onUploadProgress?.({ loaded: 3, total: 5 } as AxiosProgressEvent);
            return { data: { blob_hash: 'h', size: 5, encoding: 'utf-8' } };
        });
        const onProgress = vi.fn();
        const controller = new AbortController();
        expect(await streamBlob('ch', body, { onProgress, signal: controller.signal })).toEqual({ blob_hash: 'h', size: 5, encoding: 'utf-8' });
        const [url, data, config] = post.mock.calls[0];
        expect(url).toBe('/channels/ch/blob/stream');
        expect(data).toBe(body);
        expect(config).toMatchObject({ signal: controller.signal, headers: { 'Content-Type': 'text/markdown' } });
        expect(onProgress).toHaveBeenCalledWith(3, 5);
    });

    it('an untyped body goes as octet-stream; a progress event without a total uses the body size', async () => {
        const post = vi.spyOn(api, 'post').mockImplementation(async (_url, _data, config) => {
            config?.onUploadProgress?.({ loaded: 1 } as AxiosProgressEvent);
            return { data: {} };
        });
        const onProgress = vi.fn();
        await streamBlob('ch', new Blob(['ab']), { onProgress });
        expect(post.mock.calls[0][2]).toMatchObject({ headers: { 'Content-Type': 'application/octet-stream' } });
        expect(onProgress).toHaveBeenCalledWith(1, 2);
    });

    it('expandZip uploads the zip to the channel import/zip route; the new-channel page uses the channel-less one', async () => {
        const expansion = { entries: [{ path: 'a.txt', blob_hash: 'h', size: 1, encoding: 'utf-8' }], dirs: ['d'], skipped: [] };
        const post = vi.spyOn(api, 'post').mockResolvedValue({ data: expansion });
        const zip = new Blob(['PK'], { type: 'application/zip' });
        expect(await expandZip('ch', zip)).toEqual(expansion);
        expect(post.mock.calls[0][0]).toBe('/channels/ch/import/zip');
        expect(post.mock.calls[0][1]).toBe(zip);
        expect(await expandZipForNewChannel(zip)).toEqual(expansion);
        expect(post.mock.calls[1][0]).toBe('/import/zip');
    });

    it('commitImport posts the stacks and cells as JSON', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { stack_ids: ['s'], cell_ids: ['c'] } });
        const body = { insert_index: 2, stacks: [{ name: 'S', cells: [{ name: 'C', files: [{ path: 'a', blob_hash: 'h' }], dirs: ['e'] }] }] };
        expect(await commitImport('ch', body)).toEqual({ stack_ids: ['s'], cell_ids: ['c'] });
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'POST', url: '/channels/ch/import', data: body }));
    });

    it('the export manifest and zip address the scope and its id', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { root: 'r', entries: [], dirs: [] } });
        await fetchExportManifest('ch', { scope: 'stack', stackId: 'st 1' });
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'GET', url: '/channels/ch/export/manifest?scope=stack&stack_id=st+1' }));
        expect(exportZipUrl('ch', { scope: 'cell', cellId: 'c1' })).toMatch(/\/channels\/ch\/export\.zip\?scope=cell&cell_id=c1$/);
        expect(exportZipUrl('ch', { scope: 'all' })).toMatch(/\/channels\/ch\/export\.zip\?scope=all$/);
    });

    it('the new-channel scopes: packages, main, and tag with its tag', () => {
        expect(exportZipUrl('ch', { scope: 'packages' })).toMatch(/\/channels\/ch\/export\.zip\?scope=packages$/);
        expect(exportZipUrl('ch', { scope: 'main' })).toMatch(/\/channels\/ch\/export\.zip\?scope=main$/);
        expect(exportZipUrl('ch', { scope: 'tag', tag: 'To do & done' })).toMatch(/\/channels\/ch\/export\.zip\?scope=tag&tag=To\+do\+%26\+done$/);
    });

    it("responseError carries the server's detail, else the status", async () => {
        expect((await responseError(new Response('{"detail":"Unknown cell"}', { status: 404 }))).message).toBe('Unknown cell');
        const plain = await responseError(new Response('oops', { status: 502 }));
        expect(plain.message).toBe('Request failed (502)');
        expect(plain).toMatchObject({ status: 502 });
    });

    it('uploadRepository posts the .git zip as the multipart field file', async () => {
        const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { dir: 'proj', token: 't' } });
        const zip = new Blob(['PK'], { type: 'application/zip' });
        expect(await uploadRepository('ch', zip)).toEqual({ dir: 'proj', token: 't' });
        const [url, form] = post.mock.calls[0];
        expect(url).toBe('/channels/ch/import/repository');
        expect(form).toBeInstanceOf(FormData);
        expect((form as FormData).get('file')).toBeInstanceOf(Blob);
    });

    it('a folder export’s staged files and release go through its token; the history choice rides the query', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { root: 'r', entries: [], dirs: [] } });
        await fetchExportManifest('ch', { scope: 'all' }, undefined, { omitGitHistory: true });
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ url: '/channels/ch/export/manifest?scope=all&omit_git_history=true' }));
        expect(exportFileUrl('ch', 'to k', 'a/.git/HEAD')).toMatch(/\/channels\/ch\/export\/to%20k\/file\?path=a%2F.git%2FHEAD$/);
        await releaseExport('ch', 'tok');
        expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ method: 'DELETE', url: '/channels/ch/export/tok' }));
    });

    it('the rescue routes name the repository by path', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { version_id: 'v' } });
        expect(await retryRescue('ch', 'c1', 'vendor/lib')).toEqual({ version_id: 'v' });
        expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ method: 'POST', url: '/channels/ch/cells/c1/repositories/rescue/retry?path=vendor%2Flib' }));
        await discardRescue('ch', 'c1', '');
        expect(request).toHaveBeenLastCalledWith(expect.objectContaining({ method: 'DELETE', url: '/channels/ch/cells/c1/repositories/rescue?path=' }));
        expect(rescueExportUrl('ch', 'c1', 'a')).toMatch(/\/channels\/ch\/cells\/c1\/repositories\/rescue\/export\?path=a$/);
    });
});
