/** Shared bounded accelerator for immutable history resources and live file
 * bodies. Callers own address keys and mounted views retain their needed data. */
// Conservative retained payload weight, not an exact JavaScript heap census.
// Pending requests and the resources a mounted view needs are caller-owned.
function payloadBytes(value: unknown, seen = new Set<object>()): number {
    if (typeof value === 'string') return value.length * 2;
    if (typeof value === 'number' || typeof value === 'boolean') return 8;
    if (value === null || typeof value !== 'object' || seen.has(value)) return 0;
    seen.add(value);
    if (ArrayBuffer.isView(value)) return value.byteLength;
    if (value instanceof ArrayBuffer) return value.byteLength;
    return Object.entries(value).reduce((bytes, [key, item]) => bytes + key.length * 2 + payloadBytes(item, seen), 0);
}
export function createFileFetchCache(capacity = 256, maxBytes = 64 * 1024 * 1024) {
    const values = new Map<string, unknown>();
    const weights = new Map<string, number>();
    let retainedBytes = 0;
    const pending = new Map<string, Promise<unknown>>();
    const failures = new Map<string, number>();
    const trim = (map: Map<string, unknown>) => {
        while (map.size > capacity) map.delete(map.keys().next().value!);
    };
    const put = <T,>(key: string, value: T) => {
        retainedBytes -= weights.get(key) ?? 0;
        weights.delete(key);
        values.delete(key);
        const bytes = payloadBytes(value);
        // The fetch promise still delivers an oversized value to its caller.
        if (bytes > maxBytes) return;
        values.set(key, value);
        weights.set(key, bytes);
        retainedBytes += bytes;
        while (values.size > capacity || retainedBytes > maxBytes) {
            const oldest = values.keys().next().value!;
            retainedBytes -= weights.get(oldest)!;
            weights.delete(oldest);
            values.delete(oldest);
        }
    };
    return {
        get: <T,>(key: string): T | undefined => values.get(key) as T | undefined,
        put,
        isPending: (key: string) => pending.has(key),
        failedAt: (key: string) => failures.get(key),
        canFetch: (key: string) => !pending.has(key)
            && (failures.get(key) === undefined || Date.now() - failures.get(key)! >= 3000),
        fetch<T>(key: string, load: () => Promise<T>): Promise<T> {
            const existing = pending.get(key);
            if (existing) return existing as Promise<T>;
            const request = load().then(value => {
                put(key, value);
                failures.delete(key);
                return value;
            }, error => {
                failures.set(key, Date.now());
                trim(failures);
                throw error;
            }).finally(() => {
                if (pending.get(key) === request) pending.delete(key);
            });
            pending.set(key, request);
            return request;
        },
    };
}
export const fileFetchCache = createFileFetchCache();
