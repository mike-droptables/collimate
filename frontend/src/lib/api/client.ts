import axios, { type AxiosRequestConfig } from 'axios';
import { archivedRefusalMessage, reportArchivedRefusal } from '../channelArchived';

// Runtime config the server (api/collimate_server/main.py) injects inline into index.html;
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
      // FastAPI serves the UI's index.html with the API beside it, so the
      // base is path-relative to the document: `/api` for the app at the
      // site root, and `<prefix>/api` for the same app served under a
      // prefix, as a Collimate server inside a cell is at the host app's
      // `/proxy/<channel>/<run>/<port>/`. A UI served apart from its API
      // sets COLLIMATE_PUBLIC_API_BASE (above) instead.
      if (window.location.port !== '5173' && window.location.port !== '5174') {
          return './api';
      }
  }
  return import.meta.env.VITE_REMOTE_API_URL || 'http://127.0.0.1:5001/api';
};

export const api = axios.create({
  baseURL: getApiUrl(),
});

/** The server's own sentence for a failed request (FastAPI's `detail`), so a
 *  409 reads "A channel named 'test' already exists" rather than a status code.
 *  A coded refusal (`{code, message}`, the archived-channel guard's) gives its
 *  message. */
export function describeApiError(error: unknown): string {
    const sentence = detailSentence(error);
    if (sentence) return sentence;
    return error instanceof Error ? error.message : String(error);
}

function detailSentence(error: unknown): string | null {
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail) && detail.length && typeof detail[0]?.msg === 'string') return detail[0].msg;
    const coded = detail as { message?: unknown } | null | undefined;
    if (coded && typeof coded === 'object' && typeof coded.message === 'string' && coded.message.trim()) return coded.message;
    return null;
}

api.interceptors.response.use(response => response, (error: unknown) => {
    if (error instanceof Error) {
      const status = (error as { response?: { status?: number } }).response?.status;
      error.message = status && !detailSentence(error) ? `Request failed (${status})` : describeApiError(error);
  }
    // A control the page did not disable on an archived channel: the
    // server's sentence is shown once (lib/channelArchived).
    const archived = archivedRefusalMessage(error);
    if (archived) reportArchivedRefusal(archived);
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
