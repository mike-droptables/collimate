import axios, { type AxiosRequestConfig } from 'axios';

// Runtime config the server (api/main.py) injects inline into index.html;
// /api/config.js serves the same payload as an introspection endpoint. When
// present, it overrides build-time defaults — letting the same UI bundle
// target different API hosts without re-baking the assets. The shape is
// intentionally flat ({apiBase}); add fields here as runtime needs them.
// `window.collimateDesktop` is declared alongside its type in lib/desktop,
// the module that owns the shell contract.
declare global {
  interface Window {
    __COLLIMATE_CONFIG__?: {
      apiBase?: string;
    };
  }
}

export const getApiUrl = () => {
  if (typeof window !== 'undefined') {
      const stored = localStorage.getItem('apiUrl');
      if (stored) {
          return stored;
      }
      // Runtime config takes precedence over the path-relative heuristic
      // below. Operators who deploy the bundled UI behind a proxy that
      // points at a different API host can set this without rebuilding.
      const runtime = window.__COLLIMATE_CONFIG__?.apiBase;
      if (runtime) {
          return runtime;
      }
      // Served as static assets by FastAPI's StaticFiles at root — use a
      // path-relative URL that resolves to the site root's `api/`. The
      // browser clamps above-root resolution, so `../api` → /api from
      // anywhere under the origin root.
      if (window.location.port !== '5173' && window.location.port !== '5174') {
          return '../api';
      }
  }
  return import.meta.env.VITE_REMOTE_API_URL || 'http://127.0.0.1:5001/api';
};

export const api = axios.create({
  baseURL: getApiUrl(),
});

/** The server's own sentence for a failed request (FastAPI's `detail`), so a
 *  409 reads "A channel named 'test' already exists" rather than a status code. */
export function describeApiError(error: unknown): string {
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail) && detail.length && typeof detail[0]?.msg === 'string') return detail[0].msg;
    return error instanceof Error ? error.message : String(error);
}

api.interceptors.response.use(response => response, (error: unknown) => {
    if (error instanceof Error) {
      const status = (error as { response?: { status?: number } }).response?.status;
      const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
      error.message = status && !(typeof detail === 'string' && detail.trim()) && !(Array.isArray(detail) && typeof detail[0]?.msg === 'string')
          ? `Request failed (${status})` : describeApiError(error);
  }
    return Promise.reject(error);
});

api.interceptors.request.use((config) => {
    config.baseURL = getApiUrl();
    return config;
});

/** Shared JSON request path; binary/form requests use the same axios client. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- retain untyped endpoint response inference
export async function requestJson<T = any>(endpoint: string, method = 'GET', data?: unknown, options?: AxiosRequestConfig): Promise<T> {
    return (await api.request<T>({ ...options, method, url: endpoint, data })).data;
}
