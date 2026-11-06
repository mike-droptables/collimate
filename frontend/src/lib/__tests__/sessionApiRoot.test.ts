/**
 * @vitest-environment jsdom
 */
// The served session runtime's URL helpers, from their source in the SDK's
// renderer-hooks package: a pointer's server-root path resolves against the
// server's root, which keeps the prefix of a Collimate served behind a proxy.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiOrigin, apiRoot, sessionWsUrl } from '../../../../sdk/packages/renderer-hooks/src/session';

const at = (href: string) => vi.stubGlobal('location', new URL(href));
const fileBase = (base: string) => ({ getFileUrl: (p: string) => `${base}${p}` });

afterEach(() => vi.unstubAllGlobals());

describe('apiRoot and sessionWsUrl', () => {
    it('is the site root for the app served there', () => {
        at('http://host:5001/');
        const api = fileBase('./api/channels/ch/versions/v1/files/');
        expect(apiRoot(api)).toBe('http://host:5001/');
        expect(sessionWsUrl(api, '/api/chats/c1', 't')).toBe('ws://host:5001/api/chats/c1?token=t');
    });

    it('keeps the prefix of a Collimate served behind the host app\'s proxy', () => {
        at('http://host:5002/proxy/ch/run/5001/');
        const api = fileBase('./api/cellcap/CAP/channels/ch/cells/c/files/');
        expect(apiRoot(api)).toBe('http://host:5002/proxy/ch/run/5001/');
        expect(apiOrigin(api)).toBe('http://host:5002');
        expect(sessionWsUrl(api, '/api/terminals/t1', 'tok')).toBe('ws://host:5002/proxy/ch/run/5001/api/terminals/t1?token=tok');
        // The sandbox page of an iframe renderer sits beside index.html.
        at('http://host:5002/proxy/ch/run/5001/sandbox.html?renderer=x');
        expect(apiRoot(api)).toBe('http://host:5002/proxy/ch/run/5001/');
    });

    it('follows a split-origin API base, in wss', () => {
        at('http://page.test/');
        const api = fileBase('https://api.example.test/api/cellcap/CAP/channels/ch/cells/c/files/');
        expect(apiRoot(api)).toBe('https://api.example.test/');
        expect(sessionWsUrl(api, '/api/chats/c1', 't')).toBe('wss://api.example.test/api/chats/c1?token=t');
    });

    it('falls back to the origin without a file base it can read', () => {
        at('http://host:5002/proxy/ch/run/5001/');
        expect(apiRoot(null)).toBe('http://host:5002/');
        expect(apiRoot(fileBase('https://api.custom/v1/channels/ch/cells/c/files/'))).toBe('https://api.custom/');
        expect(sessionWsUrl({ getFileUrl: () => { throw new Error('no cell'); } }, '/api/chats/c1')).toBe('ws://host:5002/api/chats/c1');
    });
});
