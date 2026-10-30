/** One parent-owned loader. Iframes receive code; they never compile or fetch it. */
import { getApiUrl } from './api';

export interface CompiledBundle { source_hash: string; code: string; bundle_hash: string; }
interface PackageSnapshot { source_hash: string; entrypoint: string; files: Record<string, string>; }
const loads = new Map<string, Promise<string>>();
let worker: Worker | null = null;
let nextJob = 0;
const pending = new Map<number, { resolve: (value: CompiledBundle) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();

function stopWorker(error: Error): void {
  worker?.terminate();
  worker = null;
  for (const job of pending.values()) { clearTimeout(job.timer); job.reject(error); }
  pending.clear();
}
export function compileRendererPackage(snapshot: PackageSnapshot): Promise<CompiledBundle> {
  const files = Object.entries(snapshot.files).map(([name, encoded]) => {
    const raw = atob(encoded);
    return [name, Uint8Array.from(raw, c => c.charCodeAt(0))] as const;
  });
  return compileRendererFiles(Object.fromEntries(files), snapshot.entrypoint).then(result => {
    if (result.source_hash !== snapshot.source_hash) throw new Error('Renderer package identity differs from compiler identity');
    return result;
  });
}
/** Pure worker compilation; it neither loads modules nor admits trusted bundles. */
export function compileRendererFiles(input: Record<string, Uint8Array>, entrypoint: string): Promise<CompiledBundle> {
  const files = Object.entries(input).map(([name, bytes]) => [name, bytes.slice()] as const);
  if (!worker) {
    worker = new Worker(new URL('./rendererBundle.worker.ts', import.meta.url), { type: 'module' });
    worker.onmessage = (event: MessageEvent) => {
      const job = pending.get(event.data?.id);
      if (!job) return;
      pending.delete(event.data.id);
      clearTimeout(job.timer);
      if (typeof event.data.error === 'string') job.reject(new Error(event.data.error));
      else job.resolve(event.data.result);
    };
    worker.onerror = () => stopWorker(new Error('Renderer compiler worker failed'));
    worker.onmessageerror = () => stopWorker(new Error('Renderer compiler response could not be read'));
  }
  return new Promise<CompiledBundle>((resolve, reject) => {
    const id = ++nextJob;
    const timer = setTimeout(() => stopWorker(new Error('Renderer compilation timed out')), 30_000);
    pending.set(id, { resolve, reject, timer });
    try { worker!.postMessage({ id, files, entrypoint }); }
    catch (error) { stopWorker(error instanceof Error ? error : new Error(String(error))); }
  });
}

const absoluteBase = (base: string) => new URL(base.replace(/\/$/, '') + '/', window.location.href).href.replace(/\/$/, '');
async function readCode(response: Response): Promise<string> {
  if (!response.ok) throw Object.assign(new Error(`Renderer bundle: HTTP ${response.status}`), { status: response.status });
  return response.text();
}
export function loadRendererBundle(rendererId: string, hash: string | undefined, apiBase: string): Promise<string> {
  const base = absoluteBase(apiBase);
  const key = `${base}\n${rendererId}\n${hash ?? ''}`;
  let load = loads.get(key);
  if (load) return load;
  load = (async () => {
    const path = `${base}/api/renderers/${encodeURIComponent(rendererId)}`;
    const version = hash ? `?v=${encodeURIComponent(hash)}` : '';
    const source = await fetch(`${path}/source${version}`);
    if (source.status !== 404) return readCode(source);
    const packageResponse = await fetch(`${path}/package${version}`, { cache: 'no-store' });
    if (!packageResponse.ok) throw Object.assign(new Error(`Renderer package: HTTP ${packageResponse.status}`), { status: packageResponse.status });
    const snapshot: PackageSnapshot = await packageResponse.json();
    if (hash && snapshot.source_hash !== hash) throw new Error('Renderer package is no longer current');
    const compiled = await compileRendererPackage(snapshot);
    const publish = await fetch(`${path}/bundle`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_hash: compiled.source_hash, code: compiled.code }),
    });
    // A conflicting upload retains the first admitted bundle. Deterministic
    // compilers should agree; always read the server winner rather than local code.
    if (!publish.ok && publish.status !== 409) throw new Error(`Renderer cache admission: HTTP ${publish.status}`);
    return readCode(await fetch(`${path}/source?v=${compiled.source_hash}`));
  })();
  loads.set(key, load);
  const settled = () => { if (loads.get(key) === load) loads.delete(key); };
  void load.then(settled, settled);
  return load;
}

/** Called only after host source/origin routing. The token belongs to one shell. */
export function answerRendererBundleRequest(event: MessageEvent, rendererId: string, hash: string | undefined,
  target: Window, isCurrent: () => boolean): void {
  if (event.source !== target || event.origin !== window.location.origin) return;
  const message = event.data;
  if (message?.type !== 'RENDERER_BUNDLE_REQUEST' || typeof message.token !== 'string' || !/^[a-f0-9]{32}$/.test(message.token)
    || message.rendererId !== rendererId || message.rendererHash !== (hash ?? '')) return;
  const apiBase = getApiUrl().replace(/\/api\/?$/, '');
  void loadRendererBundle(rendererId, hash, apiBase).then(code => {
    if (isCurrent()) target.postMessage({ type: 'RENDERER_BUNDLE_RESULT', token: message.token, code }, window.location.origin);
  }, error => {
    if (isCurrent()) target.postMessage({ type: 'RENDERER_BUNDLE_RESULT', token: message.token, error: String(error?.message ?? error) }, window.location.origin);
  });
}
