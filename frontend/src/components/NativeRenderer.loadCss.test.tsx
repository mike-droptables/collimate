import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('@/lib/nativeRendererCache', () => ({ getCachedRenderer: vi.fn() }));

import { NativeRenderer } from './NativeRenderer';
import { getCachedRenderer } from '@/lib/nativeRendererCache';
import { getApiUrl } from '@/lib/api';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('api.loadCss loads a module-CDN stylesheet from the mirror and dedupes it by the mapped href', async () => {
    let api: { loadCss(href: string): Promise<void>; ready?(): void } | null = null;
    let root: ShadowRoot | null = null;
    vi.mocked(getCachedRenderer).mockResolvedValueOnce({
        module: { mount: (el: Element, a: typeof api) => { api = a; root = el.getRootNode() as ShadowRoot; a!.ready?.(); } },
        blobUrl: null, moduleUrl: 'blob:load-css-test',
    } as never);
    render(<NativeRenderer rendererId="load-css" fileContent="" fileName="a.md" isActiveFocus />);
    await waitFor(() => expect(api).not.toBeNull());
    const hrefs = () => Array.from(root!.querySelectorAll('link')).map(link => link.getAttribute('href'));

    void api!.loadCss('https://esm.sh/@xterm/xterm@6.0.0/css/xterm.css');
    void api!.loadCss('https://cdn.jsdelivr.net/npm/katex@0.19.1/dist/katex.min.css');
    void api!.loadCss('https://esm.sh/@xterm/xterm@6.0.0/css/xterm.css');
    void api!.loadCss('https://fonts.example/a.css');
    const base = getApiUrl().replace(/\/+$/, '');
    expect(hrefs()).toEqual([
        `${base}/mirror/esm.sh/@xterm/xterm@6.0.0/css/xterm.css`,
        `${base}/mirror/cdn.jsdelivr.net/npm/katex@0.19.1/dist/katex.min.css`,
        'https://fonts.example/a.css',
    ]);
});
