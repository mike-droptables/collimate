import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cellFileBase, cellFileCap } from '@/lib/cellFileCap';
import { getApiUrl } from '@/lib/api';

// F1941: a renderer's opaque-origin preview frame reads cell files only on
// the capability URL, so api.getFileUrl is built on a cap the host mints once
// per cell with a same-origin GET …/file_cap.
describe('cellFileCap (F1941)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('mints the cap once per cell with a same-origin GET of file_cap', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ cap: 'k1' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(cellFileCap('My Channel', 'cell-once')).resolves.toBe('k1');
    await expect(cellFileCap('My Channel', 'cell-once')).resolves.toBe('k1');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(`${getApiUrl()}/channels/My%20Channel/cells/cell-once/file_cap`);
  });

  it('does not remember a failed request, so a retry asks again', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('', { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ cap: 'k2' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(cellFileCap('ch', 'cell-retry')).rejects.toThrow('HTTP 503');
    await expect(cellFileCap('ch', 'cell-retry')).resolves.toBe('k2');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('refuses an answer without a cap', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 200 })));
    await expect(cellFileCap('ch', 'cell-empty')).rejects.toThrow('no cap');
  });

  it('puts the cap in the path, so relative URLs inside a served file keep it', () => {
    const base = cellFileBase('My Channel', 'c#1', 'abc_-9');
    expect(base).toBe(`${getApiUrl()}/cellcap/abc_-9/channels/My%20Channel/cells/c%231/files/`);
    expect(new URL('./util.js', new URL(`${base}js/main.js`, 'http://127.0.0.1:5001/')).pathname)
      .toBe('/api/cellcap/abc_-9/channels/My%20Channel/cells/c%231/files/js/util.js');
  });

  // The iframe host builds the same URLs in public/sandbox.html, pinned
  // here by source; the live check is an html cell's preview loading its
  // assets under /api/cellcap/.
  it('the iframe host (public/sandbox.html) builds getFileUrl on the same cap', () => {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const html = readFileSync(path.resolve(here, '../../public/sandbox.html'), 'utf8');
    const code = html.replace(/^\s*\/\/.*$/gm, '');
    expect(code).toContain('fetch(`${apiBase}/api/${capPath}/file_cap`)');
    expect(code).toContain(
      'if (cap) fileBase = `${apiBase}/api/cellcap/${encodeURIComponent(cap)}/${cellPath}/files/`;',
    );
    expect(code).toContain('const realPath=_realPath(path);');
    expect(code).toContain("`${fileBase}${realPath.split('/').map(encodeURIComponent).join('/')}`");
    expect(code).toContain('`${url}?revision=${revision}`');
    // The cap is in fileBase before any renderer mounts.
    const capAwaited = code.indexOf('const cap = await capRequest;');
    expect(capAwaited).toBeGreaterThan(-1);
    expect(capAwaited).toBeLessThan(code.indexOf('renderer.mountDiff(mountEl, api)'));
    expect(capAwaited).toBeLessThan(code.indexOf('renderer.mount(mountEl, api)'));
  });
});


it('scopes a History capability to the exact version __P0F02', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ cap: 'history-cap' }))));
  await expect(cellFileCap('ch', 'canvas', 'v#1')).resolves.toBe('history-cap');
  expect(fetch).toHaveBeenCalledWith(`${getApiUrl()}/channels/ch/cells/v%231/file_cap`);
  expect(cellFileBase('ch', 'canvas', 'history-cap', 'v#1')).toBe(
    `${getApiUrl()}/cellcap/history-cap/channels/ch/versions/v%231/files/`,
  );
  vi.unstubAllGlobals();
});
