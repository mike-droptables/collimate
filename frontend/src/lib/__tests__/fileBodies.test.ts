import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FileDescriptor } from '../fileProtocol';

// A current body is read pinned: the request names the accepted revision, and
// the answer must carry that revision and that body hash, or the read fails
// rather than hand a renderer bytes from another revision.
vi.mock('../api/client', () => ({ getApiUrl: () => 'http://app/api' }));
import { readPinnedText } from '../fileBodies';

const descriptor = (body_hash: string, file_revision = 4): FileDescriptor => ({
    path: 'docs/a b.md', file_revision, body_hash, deleted: false, writer_id: 'w', edit_seq: 1, language: 'markdown', encoding: 'utf-8',
});
const answer = (revision: string, hash: string) => new Response('pinned body', { headers: { 'X-File-Revision': revision, 'X-Body-Hash': hash } });

describe('readPinnedText', () => {
    afterEach(() => { vi.unstubAllGlobals(); });

    it('asks for the accepted revision and reads the body that answers with it', async () => {
        const fetcher = vi.fn(async () => answer('4', 'hash-ok'));
        vi.stubGlobal('fetch', fetcher);
        await expect(readPinnedText('ch', 'cell', descriptor('hash-ok'))).resolves.toBe('pinned body');
        expect(fetcher).toHaveBeenCalledWith('http://app/api/channels/ch/cells/cell/files/docs/a%20b.md?revision=4', { cache: 'no-store' });
    });

    it.each([
        ['another revision', '5', 'hash-revision'],
        ['another body', '4', 'hash-other'],
    ])('refuses an answer carrying %s', async (_case, revision, served) => {
        vi.stubGlobal('fetch', vi.fn(async () => answer(revision, served === 'hash-other' ? 'not-the-hash' : served)));
        await expect(readPinnedText('ch', 'cell', descriptor(served))).rejects.toThrow('File revision changed during pinned read.');
    });
});
