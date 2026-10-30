import { describe, expect, it, vi } from 'vitest';
import type { StoredBlob, UploadOptions } from '@/lib/api/transfer';
import { uploadAll, type QueueProgress, type UploadItem } from './uploadQueue';

const item = (path: string, size = 10): UploadItem => ({ path, body: new Blob([new Uint8Array(size)]) });
const stored = (hash: string, encoding: StoredBlob['encoding'] = 'binary'): StoredBlob => ({ blob_hash: hash, size: 0, encoding });

/** An upload whose calls wait until the test settles them. */
function deferredUploads() {
    const calls: { body: Blob; options: UploadOptions; resolve: (b: StoredBlob) => void; reject: (e: unknown) => void }[] = [];
    let inFlight = 0;
    let peak = 0;
    const upload = vi.fn((body: Blob, options: UploadOptions) => new Promise<StoredBlob>((resolve, reject) => {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        const settle = <T,>(fn: (v: T) => void) => (v: T) => { inFlight -= 1; fn(v); };
        options.signal?.addEventListener('abort', () => settle(reject)(new DOMException('aborted', 'AbortError')));
        calls.push({ body, options, resolve: settle(resolve), reject: settle(reject) });
    }));
    return { upload, calls, peak: () => peak };
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

describe('uploadQueue (TR-07)', () => {
    it('keeps at most four uploads in flight and files each result under its path', async () => {
        const fake = deferredUploads();
        const items = Array.from({ length: 9 }, (_, i) => item(`f${i}`));
        const done = uploadAll(items, { upload: fake.upload });
        await flush();
        expect(fake.calls).toHaveLength(4);
        while (fake.calls.some(c => c)) {
            const next = fake.calls.findIndex(c => c);
            const call = fake.calls[next];
            delete fake.calls[next];
            call.resolve(stored(`h${items.findIndex(i => i.body === call.body)}`, next % 2 ? 'utf-8' : 'binary'));
            await flush();
        }
        const result = await done;
        expect(fake.peak()).toBe(4);
        expect(result.size).toBe(9);
        expect(result.get('f3')).toEqual({ blob_hash: 'h3', encoding: 'utf-8' });
    });

    it('reports bytes sent across uploads and the files done', async () => {
        const seen: QueueProgress[] = [];
        const upload = vi.fn(async (body: Blob, options: UploadOptions) => {
            options.onProgress?.(body.size / 2, body.size);
            return stored('h');
        });
        await uploadAll([item('a', 10), item('b', 30)], { upload, concurrency: 1, onProgress: p => seen.push(p) });
        expect(seen[0]).toEqual({ sentBytes: 0, totalBytes: 40, doneFiles: 0, totalFiles: 2 });
        expect(seen).toContainEqual({ sentBytes: 5, totalBytes: 40, doneFiles: 0, totalFiles: 2 });
        expect(seen.at(-1)).toEqual({ sentBytes: 40, totalBytes: 40, doneFiles: 2, totalFiles: 2 });
    });

    it('retries a network error once, and only once', async () => {
        const network = Object.assign(new Error('Network Error'), { code: 'ERR_NETWORK' });
        const flaky = vi.fn().mockRejectedValueOnce(network).mockResolvedValueOnce(stored('ok'));
        expect((await uploadAll([item('a')], { upload: flaky })).get('a')).toEqual({ blob_hash: 'ok', encoding: 'binary' });
        expect(flaky).toHaveBeenCalledTimes(2);

        const down = vi.fn().mockRejectedValue(network);
        await expect(uploadAll([item('a')], { upload: down })).rejects.toBe(network);
        expect(down).toHaveBeenCalledTimes(2);
    });

    it("a server's refusal is not retried; without onFileError it fails the batch and stops the rest", async () => {
        const refused = Object.assign(new Error('too big'), { response: { status: 413 } });
        const fake = deferredUploads();
        const done = uploadAll([item('a'), item('b'), item('c'), item('d'), item('e')], { upload: fake.upload });
        await flush();
        fake.calls[0].reject(refused);
        await expect(done).rejects.toBe(refused);
        // The other uploads in flight were cancelled, and no new one began.
        expect(fake.calls.slice(1).every(c => c.options.signal?.aborted)).toBe(true);
        expect(fake.upload).toHaveBeenCalledTimes(4);
    });

    it('with onFileError a failed file is reported and left out; the rest land', async () => {
        const refused = Object.assign(new Error('nope'), { response: { status: 400 } });
        const upload = vi.fn(async (body: Blob) => { if (body.size === 1) throw refused; return stored('h'); });
        const onFileError = vi.fn();
        const result = await uploadAll([item('bad', 1), item('good', 2)], { upload, onFileError });
        expect(onFileError).toHaveBeenCalledWith('bad', refused);
        expect([...result.keys()]).toEqual(['good']);
        expect(upload).toHaveBeenCalledTimes(2);
    });

    it('abort cancels the uploads in flight and rejects with an AbortError', async () => {
        const fake = deferredUploads();
        const controller = new AbortController();
        const done = uploadAll([item('a'), item('b'), item('c'), item('d'), item('e'), item('f')], { upload: fake.upload, signal: controller.signal });
        await flush();
        controller.abort();
        await expect(done).rejects.toMatchObject({ name: 'AbortError' });
        expect(fake.calls.every(c => c.options.signal?.aborted)).toBe(true);
        expect(fake.upload).toHaveBeenCalledTimes(4);
    });

    it('an already-aborted signal uploads nothing', async () => {
        const upload = vi.fn();
        const controller = new AbortController();
        controller.abort();
        await expect(uploadAll([item('a')], { upload, signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' });
        expect(upload).not.toHaveBeenCalled();
    });
});
