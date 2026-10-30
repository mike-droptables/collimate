import { expect, it, vi } from 'vitest';
import { createFileFetchCache } from './fileFetchCache';
it('shares bounded values and in-flight work while retaining failure backoff__F0792', async () => {
    const cache = createFileFetchCache(2);
    let resolve!: (text: string) => void;
    const load = vi.fn(() => new Promise<string>(r => { resolve = r; }));
    const first = cache.fetch('live:channel:cell:path:stamp', load);
    const second = cache.fetch('live:channel:cell:path:stamp', load);
    expect(second).toBe(first);
    expect(load).toHaveBeenCalledTimes(1);
    resolve('live body');
    await first;
    cache.put('tree:immutable-version', ['immutable tree']);
    cache.put('blob:hash', 'immutable blob');
    expect(cache.get('live:channel:cell:path:stamp')).toBeUndefined();
    expect(cache.get('tree:immutable-version')).toEqual(['immutable tree']);
    await expect(cache.fetch('failed', async () => { throw new Error('unavailable'); })).rejects.toThrow('unavailable');
    expect(cache.canFetch('failed')).toBe(false);
    expect(cache.isPending('failed')).toBe(false);
});

it('bounds retained payload bytes below the entry cap and skips oversized accelerator values__F0762', async () => {
    const cache = createFileFetchCache();
    const body = 'x'.repeat(2 * 1024 * 1024);
    for (let index = 0; index < 64; index++) cache.put(`blob:${index}`, body);
    let bytes = 0;
    for (let index = 0; index < 64; index++) bytes += (cache.get<string>(`blob:${index}`)?.length ?? 0) * 2;
    expect(bytes).toBeLessThanOrEqual(64 * 1024 * 1024);
    const large = 'z'.repeat(33 * 1024 * 1024);
    expect(await cache.fetch('oversized', async () => large)).toBe(large);
    expect(cache.get('oversized')).toBeUndefined();
});
