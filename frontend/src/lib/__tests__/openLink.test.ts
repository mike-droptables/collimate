// @vitest-environment jsdom
// lib/openLink: what an href names (classifyLink) and what following it does
// (openLink), with the store, the reveal, the sites route and the desktop
// shell replaced at their edges.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
    openChoice: vi.fn(),
    openAlert: vi.fn(async () => true),
    files: ['notes/a.md', 'b.md', 'docs/guide.md', 'a b.md'] as string[],
    reveal: vi.fn(),
    postCellSite: vi.fn(),
    queueReveal: vi.fn(),
    desktop: false,
    openExternal: vi.fn(async () => {}),
}));
vi.mock('@/stores/useCollimateStore', () => ({
    useCollimateStore: {
        getState: () => ({
            openChoice: h.openChoice,
            openAlert: h.openAlert,
            queueReveal: h.queueReveal,
            locateCell: (id: string) => (id === 'c1' ? { cell: { files: h.files.map(name => ({ name })) } } : undefined),
            activeChannel: { id: 'active-ch' },
        }),
    },
}));
vi.mock('@/lib/revealFile', () => ({ revealCellFile: h.reveal }));
vi.mock('@/lib/api/runs', () => ({ postCellSite: h.postCellSite }));
vi.mock('@/lib/desktop', () => ({ isDesktop: () => h.desktop, openExternal: h.openExternal }));

import { classifyLink, openLink } from '../openLink';

const APP = window.location.origin;

describe('classifyLink', () => {
    it('a path resolves against the document folder; a leading slash from the cell root; . and .. normalise', () => {
        const doc = { documentPath: 'docs/guide.md' };
        expect(classifyLink('a.md', doc)).toEqual({ kind: 'file', path: 'docs/a.md' });
        expect(classifyLink('./img/p.png', doc)).toEqual({ kind: 'file', path: 'docs/img/p.png' });
        expect(classifyLink('../notes/a.md', doc)).toEqual({ kind: 'file', path: 'notes/a.md' });
        expect(classifyLink('/notes/a.md', doc)).toEqual({ kind: 'file', path: 'notes/a.md' });
        expect(classifyLink('sub/../b.md', doc)).toEqual({ kind: 'file', path: 'docs/b.md' });
        expect(classifyLink('a.md?x=1#part', doc)).toEqual({ kind: 'file', path: 'docs/a.md' });
        expect(classifyLink('  a.md  ', doc)).toEqual({ kind: 'file', path: 'docs/a.md' });
    });

    it('without a document a path resolves from the cell root', () => {
        expect(classifyLink('notes/a.md', {})).toEqual({ kind: 'file', path: 'notes/a.md' });
        expect(classifyLink('notes/a.md', { documentPath: 'top.md' })).toEqual({ kind: 'file', path: 'notes/a.md' });
        expect(classifyLink('notes/a.md', { documentPath: '' })).toEqual({ kind: 'file', path: 'notes/a.md' });
    });

    it('percent-decodes each segment; a malformed escape is read as written', () => {
        expect(classifyLink('a%20b.md', {})).toEqual({ kind: 'file', path: 'a b.md' });
        expect(classifyLink('100%.md', {})).toEqual({ kind: 'file', path: '100%.md' });
        expect(classifyLink('docs%2Fguide.md', {})).toEqual({ kind: 'file', path: 'docs/guide.md' });
        expect(classifyLink('x/%2e%2e/b.md', {})).toEqual({ kind: 'file', path: 'b.md' });
    });

    it('a path that climbs out of the cell is refused, encoded or not', () => {
        for (const href of ['../x.md', 'docs/../../x.md', '%2e%2e/x.md', '..%2Fx.md', '..\\x.md', '/../x.md']) {
            expect(classifyLink(href, {}), href).toEqual({ kind: 'outside' });
        }
        expect(classifyLink('../../x.md', { documentPath: 'docs/guide.md' })).toEqual({ kind: 'outside' });
    });

    it('an empty href, or one of only a query, names the document itself', () => {
        expect(classifyLink('', { documentPath: 'docs/guide.md' })).toEqual({ kind: 'file', path: 'docs/guide.md' });
        expect(classifyLink('?v=2', { documentPath: 'docs/guide.md' })).toEqual({ kind: 'file', path: 'docs/guide.md' });
    });

    it('an http(s) URL on this machine is a local site, its port defaulted by scheme', () => {
        expect(classifyLink('http://localhost:5173/app?x=1', {})).toEqual({ kind: 'local-site', url: 'http://localhost:5173/app?x=1', port: 5173 });
        expect(classifyLink('http://127.0.0.1:8000', {})).toEqual({ kind: 'local-site', url: 'http://127.0.0.1:8000/', port: 8000 });
        expect(classifyLink('http://[::1]:3001/', {})).toEqual({ kind: 'local-site', url: 'http://[::1]:3001/', port: 3001 });
        expect(classifyLink('http://0.0.0.0:9000/', {})).toEqual({ kind: 'local-site', url: 'http://0.0.0.0:9000/', port: 9000 });
        expect(classifyLink('http://app.localhost:4000/', {})).toEqual({ kind: 'local-site', url: 'http://app.localhost:4000/', port: 4000 });
        expect(classifyLink('HTTP://LOCALHOST:4001/', {})).toEqual({ kind: 'local-site', url: 'http://localhost:4001/', port: 4001 });
        expect(classifyLink('http://localhost/', {})).toEqual({ kind: 'local-site', url: 'http://localhost/', port: 80 });
        expect(classifyLink('https://localhost/', {})).toEqual({ kind: 'local-site', url: 'https://localhost/', port: 443 });
        expect(classifyLink('https://[::1]/', {})).toEqual({ kind: 'local-site', url: 'https://[::1]/', port: 443 });
    });

    it("a URL on the app's own origin is a website, not a local site", () => {
        expect(new URL(APP).hostname).toBe('localhost');
        expect(classifyLink(`${APP}/proxy/ch/run/5173/`, {})).toEqual({ kind: 'website', url: `${APP}/proxy/ch/run/5173/` });
        const otherPort = new URL(APP);
        otherPort.port = String(Number(otherPort.port || 80) + 1);
        expect(classifyLink(otherPort.href, {}).kind).toBe('local-site');
    });

    it('any other http(s) URL, a protocol-relative one and mailto: are websites', () => {
        expect(classifyLink('https://example.com/docs', {})).toEqual({ kind: 'website', url: 'https://example.com/docs' });
        expect(classifyLink('http://localhost.example.com/', {})).toEqual({ kind: 'website', url: 'http://localhost.example.com/' });
        expect(classifyLink('http://127.0.0.2/', {})).toEqual({ kind: 'website', url: 'http://127.0.0.2/' });
        expect(classifyLink('//cdn.example.com/a.js', {})).toEqual({ kind: 'website', url: `${window.location.protocol}//cdn.example.com/a.js` });
        expect(classifyLink('mailto:someone@example.com', {})).toEqual({ kind: 'website', url: 'mailto:someone@example.com' });
        expect(classifyLink('MAILTO:someone@example.com', {})).toEqual({ kind: 'website', url: 'MAILTO:someone@example.com' });
    });

    it('every other scheme, an anchor and a malformed URL are unsupported', () => {
        for (const href of ['file:///etc/passwd', 'javascript:alert(1)', 'vscode://file/x', 'data:text/plain,x', 'ftp://example.com/', '#top', 'http://[bad']) {
            expect(classifyLink(href, {}), href).toEqual({ kind: 'unsupported' });
        }
    });
});

describe('openLink', () => {
    const ctx = { cellId: 'c1', channel: 'ch', documentPath: 'docs/guide.md' };
    let open: ReturnType<typeof vi.spyOn>;
    beforeEach(() => {
        h.openChoice.mockReset();
        h.openAlert.mockClear();
        h.reveal.mockClear();
        h.postCellSite.mockReset();
        h.queueReveal.mockClear();
        h.openExternal.mockClear();
        h.desktop = false;
        open = vi.spyOn(window, 'open').mockReturnValue(null);
    });
    afterEach(() => { open.mockRestore(); vi.unstubAllGlobals(); });

    it('a file of the cell opens in a new tab of the cell', async () => {
        await openLink('../notes/a.md', ctx);
        expect(h.reveal).toHaveBeenCalledExactlyOnceWith('c1', 'notes/a.md', { newTab: true });
        expect(h.openAlert).not.toHaveBeenCalled();
        expect(h.openChoice).not.toHaveBeenCalled();
    });

    it('a path that is no file of the cell is said in an alert in the cell', async () => {
        await openLink('missing.md', ctx);
        expect(h.reveal).not.toHaveBeenCalled();
        expect(h.openAlert).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ title: 'File not found', cellId: 'c1', body: expect.stringContaining('docs/missing.md') }));
    });

    it('a path out of the cell is refused with an alert', async () => {
        await openLink('../../etc/passwd', ctx);
        expect(h.reveal).not.toHaveBeenCalled();
        expect(h.openAlert).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ cellId: 'c1', body: expect.stringContaining('outside this cell') }));
    });

    it('a local site offers View site in the cell; choosing it asks the host for the port and shows its pointer in front', async () => {
        h.openChoice.mockResolvedValue('view');
        h.postCellSite.mockResolvedValue({ run_id: 'r1', label: 'launch-1', path: '_runs/launch-1/site-5173.website', url: '/proxy/ch/r1/5173/' });
        await openLink('http://localhost:5173/', ctx);
        expect(h.openChoice).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
            title: 'http://localhost:5173/', cellId: 'c1', options: [{ id: 'view', label: 'View site' }],
        }));
        expect(h.postCellSite).toHaveBeenCalledExactlyOnceWith('ch', 'c1', 5173);
        expect(h.queueReveal).toHaveBeenCalledExactlyOnceWith('c1', { path: '_runs/launch-1/site-5173.website', runId: 'r1', activate: true });
        expect(h.openAlert).not.toHaveBeenCalled();
    });

    it('a local site cancelled asks nothing; a refusal shows the server sentence', async () => {
        h.openChoice.mockResolvedValue(null);
        await openLink('http://127.0.0.1:8000/', ctx);
        expect(h.postCellSite).not.toHaveBeenCalled();
        h.openChoice.mockResolvedValue('view');
        h.postCellSite.mockRejectedValue(new Error('No running runner on this cell serves port 8000'));
        await openLink('http://127.0.0.1:8000/', ctx);
        expect(h.openAlert).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
            title: 'Cannot view site', body: 'No running runner on this cell serves port 8000', cellId: 'c1',
        }));
        expect(h.queueReveal).not.toHaveBeenCalled();
    });

    it('a local site takes the active channel when the context names none', async () => {
        h.openChoice.mockResolvedValue('view');
        h.postCellSite.mockResolvedValue({ run_id: 'r1', label: 'l', path: '_runs/l/site-3001.website', url: '/proxy/active-ch/r1/3001/' });
        await openLink('http://localhost:3001/', { cellId: 'c1' });
        expect(h.postCellSite).toHaveBeenCalledWith('active-ch', 'c1', 3001);
    });

    it('a website offers Open in browser and Copy URL; open is a new tab in a browser', async () => {
        h.openChoice.mockResolvedValue('open');
        await openLink('https://example.com/docs', ctx);
        expect(h.openChoice).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
            title: 'https://example.com/docs', cellId: 'c1',
            options: [{ id: 'open', label: 'Open in browser' }, { id: 'copy', label: 'Copy URL' }],
        }));
        expect(open).toHaveBeenCalledExactlyOnceWith('https://example.com/docs', '_blank', 'noopener');
        expect(h.openExternal).not.toHaveBeenCalled();
    });

    it('in the desktop shell, open hands the URL to the system (mailto: too)', async () => {
        h.desktop = true;
        h.openChoice.mockResolvedValue('open');
        await openLink('mailto:someone@example.com', ctx);
        expect(h.openExternal).toHaveBeenCalledExactlyOnceWith('mailto:someone@example.com');
        expect(open).not.toHaveBeenCalled();
    });

    it('copy puts the URL on the clipboard; a refusal is said with the URL to copy by hand', async () => {
        const writeText = vi.fn(async () => {});
        vi.stubGlobal('navigator', { clipboard: { writeText } });
        h.openChoice.mockResolvedValue('copy');
        await openLink('https://example.com/a', ctx);
        expect(writeText).toHaveBeenCalledExactlyOnceWith('https://example.com/a');
        expect(h.openAlert).not.toHaveBeenCalled();
        writeText.mockRejectedValueOnce(new Error('denied'));
        await openLink('https://example.com/a', ctx);
        expect(h.openAlert).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ title: 'Copy failed', cellId: 'c1', body: expect.stringContaining('https://example.com/a') }));
    });

    it('a cancelled website choice does nothing', async () => {
        h.openChoice.mockResolvedValue(null);
        await openLink('https://example.com/', ctx);
        expect(open).not.toHaveBeenCalled();
        expect(h.openAlert).not.toHaveBeenCalled();
    });

    it('an unsupported link is refused with an alert', async () => {
        await openLink('javascript:alert(1)', ctx);
        expect(h.openChoice).not.toHaveBeenCalled();
        expect(h.openAlert).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ body: 'Links of this kind cannot be opened here.', cellId: 'c1' }));
    });
});
