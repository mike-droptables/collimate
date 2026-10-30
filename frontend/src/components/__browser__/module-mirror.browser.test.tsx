import { expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { NativeRenderer } from '../NativeRenderer';
import { MIRRORED_HOSTS } from '@/lib/moduleMirror';
import { FIXTURE_MIRROR_PROBE_ID, MIRROR_PROBE_CSS, MIRROR_PROBE_MODULE, rendererFixtureSource } from '@/test/rendererFixtures';

// The module mirror in both hosts: a renderer's import of a module CDN and
// its api.loadCss of one resolve to /api/mirror/<host>/…, which the test
// mirror (vitest.config.ts) answers for a module and a stylesheet that exist
// nowhere else. The browser cannot resolve either CDN host in this suite.

const PROBE_BODY_MODULE = 'https://esm.sh/mirror-probe@1.0.0/es2022/mirror-probe.mjs';
const onCdn = (url: string) => MIRRORED_HOSTS.has(new URL(url).host);
const hits = async (): Promise<Record<string, number>> => (await fetch('/__mirror_hits')).json();
const probeIn = (root: ParentNode | null | undefined) => (root?.querySelector('.mirror-probe') ?? null) as HTMLElement | null;
const mirrorMap = (base: string) => ({ imports: Object.fromEntries([...MIRRORED_HOSTS].map(host => [`https://${host}/`, `${base}${host}/`])) });

it('a native renderer bundle imports a module and loads a stylesheet only the mirror has, each fetched once', async () => {
    const before = await hits();
    const view = await render(<NativeRenderer rendererId={FIXTURE_MIRROR_PROBE_ID} fileContent="" fileName="probe.txt" isActiveFocus />);
    try {
        const probe = () => [...document.querySelectorAll('*')].map(node => probeIn(node.shadowRoot)).find(Boolean) ?? null;
        await vi.waitFor(() => expect(probe()?.textContent).toBe('served by the mirror'), { timeout: 15000 });
        await vi.waitFor(() => expect(getComputedStyle(probe()!).color).toBe('rgb(1, 2, 3)'));
        const after = await hits();
        const fetched = (key: string) => (after[key] ?? 0) - (before[key] ?? 0);
        expect([fetched(MIRROR_PROBE_MODULE), fetched(PROBE_BODY_MODULE), fetched(MIRROR_PROBE_CSS)]).toEqual([1, 1, 1]);
        expect(performance.getEntriesByType('resource').map(entry => entry.name).filter(onCdn)).toEqual([]);
    } finally {
        await view.unmount();
    }
}, 30000);

it('sandbox.html adds its import map first and maps loadCss, so an iframe renderer loads from the mirror', async () => {
    const frame = document.createElement('iframe');
    const answer = (event: MessageEvent) => {
        if (event.source !== frame.contentWindow || event.data?.type !== 'RENDERER_BUNDLE_REQUEST') return;
        frame.contentWindow!.postMessage({
            type: 'RENDERER_BUNDLE_RESULT', token: event.data.token, code: rendererFixtureSource(FIXTURE_MIRROR_PROBE_ID),
        }, location.origin);
    };
    window.addEventListener('message', answer);
    frame.src = `/sandbox.html?renderer=${FIXTURE_MIRROR_PROBE_ID}&hash=&apiBase=`;
    document.body.append(frame);
    try {
        await vi.waitFor(() => expect(probeIn(frame.contentDocument)?.textContent).toBe('served by the mirror'), { timeout: 15000 });
        const doc = frame.contentDocument!;
        const base = `${location.origin}/api/mirror/`;
        const maps = [...doc.querySelectorAll('script[type="importmap"]')];
        expect(maps).toHaveLength(1);
        expect(JSON.parse(maps[0].textContent!)).toEqual(mirrorMap(base));
        expect(doc.head.firstElementChild?.nextElementSibling).toBe(maps[0]);
        expect([...doc.querySelectorAll('link')].map(link => link.getAttribute('href'))).toContain(`${base}cdn.jsdelivr.net/npm/mirror-probe@1.0.0/probe.css`);
        expect(frame.contentWindow!.getComputedStyle(probeIn(doc)!).color).toBe('rgb(1, 2, 3)');
        const requested = frame.contentWindow!.performance.getEntriesByType('resource').map(entry => entry.name);
        expect(requested).toContain(`${base}esm.sh/mirror-probe@1.0.0?target=es2022`);
        expect(requested.filter(onCdn)).toEqual([]);
    } finally {
        window.removeEventListener('message', answer);
        frame.remove();
    }
}, 30000);
