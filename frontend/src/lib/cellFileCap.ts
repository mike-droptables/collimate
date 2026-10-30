import { getApiUrl } from '@/lib/api';

/**
 * The URLs renderers get from `api.getFileUrl` (native host here; the iframe
 * host builds the same shape in public/sandbox.html).
 *
 * A renderer's inner preview frame (the html renderer's page) is sandboxed
 * without `allow-same-origin`, so its reads
 * of sibling cell files (module scripts, fonts, `fetch()`) carry
 * `Origin: null`, exactly like a sandboxed frame on any other web page. The
 * server answers such a frame only on a capability URL,
 * `…/api/cellcap/<cap>/channels/<channel>/cells/<id>/files/<path>`, whose cap
 * it mints per cell on a same-origin `GET …/file_cap`
 * (api/auth_middleware.py CellFileCaps). Every getFileUrl URL is built on it,
 * so a renderer never has to know where its URLs will be read from.
 *
 * A cap stays valid for the data dir, so one request per cell per page is
 * enough; a failed request is not remembered, so a retry asks again. Both
 * hosts await the cap before mounting any renderer with a cell context, so a
 * failed mint fails that mount the way a failed fetch of the renderer's own
 * module does (the same server answers both), and the next mount asks again.
 */
const caps = new Map<string, Promise<string>>();

export function cellFileCap(channel: string, cellId: string, versionId?: string): Promise<string> {
  cellId = versionId ?? cellId;
  const key = `${channel}\n${cellId}`;
  let cap = caps.get(key);
  if (!cap) {
    cap = fetch(
      `${getApiUrl()}/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/file_cap`,
    ).then(async (res) => {
      if (!res.ok) throw new Error(`file_cap: HTTP ${res.status}`);
      const body: unknown = await res.json();
      const value = (body as { cap?: unknown } | null)?.cap;
      if (typeof value !== 'string' || !value) throw new Error('file_cap: no cap in the answer');
      return value;
    });
    caps.set(key, cap);
    cap.catch(() => {
      if (caps.get(key) === cap) caps.delete(key);
    });
  }
  return cap;
}

/** The URL every file is read under: append a path encoded per segment.
 *  A version address always reads that committed snapshot. */
export function cellFileBase(channel: string, cellId: string, cap: string, versionId?: string): string {
  const address = versionId ? `versions/${encodeURIComponent(versionId)}` : `cells/${encodeURIComponent(cellId)}`;
  return `${getApiUrl()}/cellcap/${encodeURIComponent(cap)}/channels/${encodeURIComponent(channel)}/${address}/files/`;
}
