/** Session module identity and Blob lifetime for the trusted native host. */
import { loadRendererBundle } from './rendererBundle';
import { registerRendererModule } from './rendererInputContainment';

// Session module cache preserves renderer instances across host remounts.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
interface CachedRenderer { module: any; blobUrl: string | null; moduleUrl: string; }
const rendererCache = new Map<string, Promise<CachedRenderer>>();

async function loadCachedRenderer(
  rendererId: string,
  rendererHash: string | undefined,
  apiBase: string,
): Promise<CachedRenderer> {
  // Build an absolute URL up front. Dynamic import() resolves relative
  // URLs against the *calling module's* URL (the bundled React JS),
  // not the document URL, so a relative ``../api/...`` would land next
  // to the chunk instead of at the site root. fetch() doesn't have this
  // problem (document base), but using the same absolute URL for both
  // keeps fetch and import in sync.
  const fullApiBase = apiBase.startsWith('http')
    ? apiBase
    : new URL(apiBase + '/', window.location.href).toString().replace(/\/$/, '');
  const sourceCode = await loadRendererBundle(rendererId, rendererHash, fullApiBase);
  // Blob imports have no root-relative base. Keep the SDK externals on the
  // selected API origin, exactly as the iframe shell does.
  const code = sourceCode.replace(/['"]\/api\/sdk\/(.*?)['"]/g, `"${fullApiBase}/api/sdk/$1"`);
  const blobUrl = URL.createObjectURL(new Blob([code], { type: 'application/javascript' }));
  const finalUrl = blobUrl;

  // Input containment: attribute this URL's stack frames to a renderer
  // module BEFORE it executes, so even import-time listener registrations
  // get focus-gated.
  registerRendererModule(finalUrl);
  try {
    const module = await import(/* @vite-ignore */ finalUrl);
    return { module, blobUrl, moduleUrl: finalUrl };
  } catch (err) {
    // The cache eviction in getCachedRenderer drops the entry but never
    // sees the blob URL — revoke it here or it leaks on every failed
    // mount/retry cycle.
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    throw err;
  }
}

export function getCachedRenderer(
  rendererId: string,
  rendererHash: string | undefined,
  apiBase: string,
): Promise<CachedRenderer> {
  const key = `${apiBase}::${rendererId}::${rendererHash ?? ''}`;
  let entry = rendererCache.get(key);
  if (!entry) {
    // A new hash for this renderer means any prior-hash entries are now
    // stale: nothing in the app will ever ask for them again, so their
    // blob URLs (and the modules they back) just sit in memory forever.
    // Revoke + drop them when we register the new version.
    evictStaleVersions(`${apiBase}::${rendererId}::`, key);
    entry = loadCachedRenderer(rendererId, rendererHash, apiBase).catch((err) => {
      // Evict failed entries so a retry can re-fetch instead of sticking
      // to the rejection forever.
      rendererCache.delete(key);
      throw err;
    });
    rendererCache.set(key, entry);
  }
  return entry;
}

function evictStaleVersions(prefix: string, keepKey: string): void {
  for (const [k, promise] of Array.from(rendererCache.entries())) {
    if (k === keepKey) continue;
    if (!k.startsWith(prefix)) continue;
    rendererCache.delete(k);
    promise.then(({ blobUrl }) => {
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    }).catch(() => { /* failed entries already evicted by their own catch */ });
  }
}

