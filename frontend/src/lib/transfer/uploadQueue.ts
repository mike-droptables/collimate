// Many files into the CAS: a few uploads at a time, each body streamed from
// disk by the browser (lib/api/transfer.streamBlob). One queue serves the
// transfer panel's imports and every drop and paste (lib/fileIngest).
import { streamBlob, type BlobEncoding, type StoredBlob, type UploadOptions } from '@/lib/api/transfer';
import { isAbortError } from '@/lib/utils';

/** Uploads in flight at once. */
export const UPLOAD_CONCURRENCY = 4;

export interface UploadItem {
    /** The key the result is filed under: the file's path in the import. */
    path: string;
    body: Blob;
}

export interface UploadedBlob {
    blob_hash: string;
    encoding: BlobEncoding;
}

export interface QueueProgress {
    sentBytes: number;
    totalBytes: number;
    doneFiles: number;
    totalFiles: number;
}

export interface UploadQueueOptions {
    signal?: AbortSignal;
    onProgress?: (progress: QueueProgress) => void;
    concurrency?: number;
    /** With this, a file that fails is reported here and left out of the
     *  result; without it, the first failure cancels the rest and rejects. */
    onFileError?: (path: string, error: unknown) => void;
    /** The upload itself; tests pass a fake. */
    upload?: (body: Blob, options: UploadOptions) => Promise<StoredBlob>;
}

/** A request that never got an answer (the connection dropped, the server
 *  restarted): worth one more try. A server's refusal is not. */
function isNetworkError(error: unknown): boolean {
    if (isAbortError(error)) return false;
    const e = error as { response?: unknown; code?: string; name?: string };
    return !e?.response && (e?.code === 'ERR_NETWORK' || e?.name === 'TypeError');
}

const abortError = () => new DOMException('The upload was cancelled.', 'AbortError');

/** Upload every item, at most `concurrency` at a time, and return each
 *  path's blob hash and the server's text-or-binary verdict. A network error
 *  is retried once. Aborting `signal` cancels the uploads in flight and
 *  rejects with an AbortError. */
export async function uploadAll(items: readonly UploadItem[], options: UploadQueueOptions = {}): Promise<Map<string, UploadedBlob>> {
    const { signal, onProgress, onFileError, upload = streamBlob } = options;
    const concurrency = Math.max(1, options.concurrency ?? UPLOAD_CONCURRENCY);
    const results = new Map<string, UploadedBlob>();
    const sent = new Map<number, number>();
    const totalBytes = items.reduce((sum, item) => sum + item.body.size, 0);
    let doneFiles = 0;
    const report = () => onProgress?.({
        sentBytes: [...sent.values()].reduce((a, b) => a + b, 0), totalBytes, doneFiles, totalFiles: items.length,
    });
    // One controller for the batch: the caller's abort and the first fatal
    // failure both stop every upload still running.
    const batch = new AbortController();
    const onAbort = () => batch.abort();
    if (signal?.aborted) throw abortError();
    signal?.addEventListener('abort', onAbort);
    let failure: unknown = null;
    let next = 0;

    const uploadOne = async (index: number) => {
        const item = items[index];
        const options: UploadOptions = {
            signal: batch.signal,
            onProgress: loaded => { sent.set(index, Math.min(loaded, item.body.size)); report(); },
        };
        try {
            return await upload(item.body, options);
        } catch (error) {
            if (!isNetworkError(error) || batch.signal.aborted) throw error;
            sent.set(index, 0);
            return await upload(item.body, options);
        }
    };

    const worker = async () => {
        while (next < items.length && !batch.signal.aborted) {
            const index = next++;
            const item = items[index];
            try {
                const stored = await uploadOne(index);
                results.set(item.path, { blob_hash: stored.blob_hash, encoding: stored.encoding });
            } catch (error) {
                if (batch.signal.aborted) return;
                if (!onFileError) { failure ??= error; batch.abort(); return; }
                onFileError(item.path, error);
            }
            sent.set(index, item.body.size);
            doneFiles += 1;
            report();
        }
    };

    try {
        report();
        await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
    } finally {
        signal?.removeEventListener('abort', onAbort);
    }
    if (signal?.aborted) throw abortError();
    if (failure !== null) throw failure;
    return results;
}
