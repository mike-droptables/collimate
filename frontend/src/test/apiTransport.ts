import { AxiosError, type AxiosAdapter } from 'axios';
import { api } from '@/lib/api/client';

/** Route axios through a test's existing response fixture, keeping its real
 * serialization and response interceptors. No request helper is mocked. */
export function routeApiToFetchStub(): () => void {
    const prior = api.defaults.adapter;
    const adapter: AxiosAdapter = async config => {
        const response = await fetch(`${config.baseURL}${config.url}`, {
            method: config.method?.toUpperCase(),
            headers: { 'Content-Type': 'application/json' },
            ...(config.data === undefined ? {} : { body: config.data }),
            signal: config.signal as AbortSignal | undefined,
        });
        const data = await response.json().catch(() => null);
        const result = { data, status: response.status ?? 200, statusText: '', headers: {}, config };
        if (!response.ok) throw new AxiosError(`Request failed (${result.status})`, undefined, config, undefined, result);
        return result;
    };
    api.defaults.adapter = adapter;
    return () => { api.defaults.adapter = prior; };
}
