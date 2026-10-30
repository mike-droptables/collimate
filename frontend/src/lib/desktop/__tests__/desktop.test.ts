/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import {
    desktopBridge,
    isDesktop,
    desktopCaps,
    hasCap,
    openExternal,
    describeClient,
    desktopServers,
    isWindowServer,
    localFolderPicker,
    serverIsRemote,
    type CollimateDesktop,
} from '../index';
import { ALL_DESKTOP_CAPS, installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';

const ALL_CAPS = ALL_DESKTOP_CAPS;

function installShell(overrides: Partial<CollimateDesktop> = {}): CollimateDesktop {
    return installDesktopShell(overrides).shell;
}

afterEach(() => {
    uninstallDesktopShell();
});

describe('isDesktop', () => {
    it('is false with no bridge on the window', () => {
        expect(isDesktop()).toBe(false);
        expect(desktopBridge()).toBeUndefined();
    });

    it('is true once the shell has exposed its bridge', () => {
        installShell();
        expect(isDesktop()).toBe(true);
    });
});

describe('desktopCaps', () => {
    it('is empty in a browser', () => {
        expect(desktopCaps()).toEqual({});
    });

    it('passes the shell capability map through untouched', () => {
        installShell();
        expect(desktopCaps()).toEqual(ALL_CAPS);
    });
});

describe('hasCap', () => {
    it('is false for every capability in a browser', () => {
        expect(hasCap('nativeGuests')).toBe(false);
        expect(hasCap('externalOpen')).toBe(false);
    });

    it('reads individual flags off the shell', () => {
        installShell({ capabilities: { ...ALL_CAPS, guestCapture: false } });
        expect(hasCap('nativeGuests')).toBe(true);
        expect(hasCap('guestCapture')).toBe(false);
    });

    it('reads a capability the shell omits as absent, not as an error', () => {
        // A shell older than a capability ships no key for it at all —
        // the case that would throw if callers indexed the map directly.
        installShell({ capabilities: { nativeGuests: true } as CollimateDesktop['capabilities'] });
        expect(hasCap('nativeGuests')).toBe(true);
        expect(hasCap('externalOpen')).toBe(false);
    });
});

describe('openExternal', () => {
    it('is a resolved no-op in a browser', async () => {
        await expect(openExternal('https://example.test/')).resolves.toBeUndefined();
    });

    it('hands the URL to the shell', async () => {
        const shell = installShell();
        await openExternal('https://example.test/docs');
        expect(shell.openExternal).toHaveBeenCalledExactlyOnceWith('https://example.test/docs');
    });
});

describe('guest surface mirror', () => {
    // This interface is a deliberate structural duplicate of GuestOps in
    // desktop/src/shared/bridge.ts (see the module header). The shell leg
    // is frozen in desktop/tests/bridgeSurface.test.ts; this closes the
    // page leg, so the two declarations can't drift apart silently.
    it('the guest surface is includes the private placeholder verb beside the fifteen frozen verbs (GN-04)', () => {
        const shell = installShell();
        expect(Object.keys(shell.guest).sort()).toEqual([
            'capturePlaceholder',
            'capturePng',
            'clearPartition',
            'createGuest',
            'currentUrl',
            'destroyGuest',
            'evaluate',
            'goBack',
            'goForward',
            'layoutGuests',
            'navigate',
            'openDevTools',
            'raiseGuest',
            'reload',
            'setGuestDevice',
            'setGuestVisible',
        ]);
        // The four SDK-facing verbs (the web-session RPC rides these; the
        // RPC's fifth verb, html, is answered renderer-side via evaluate).
        for (const op of ['evaluate', 'capturePng', 'currentUrl', 'navigate'] as const) {
            expect(typeof shell.guest[op]).toBe('function');
        }
    });
});

describe('describeClient', () => {
    it('reports a plain browser with no capabilities', () => {
        expect(describeClient()).toEqual({ client: 'browser', capabilities: {} });
    });

    it('reports the shell version and bridge alongside the capabilities __F0985', () => {
        installShell();
        expect(describeClient()).toEqual({
            client: 'electron',
            shellVersion: '0.4.1',
            bridge: 6,
            capabilities: ALL_CAPS,
        });
    });

    it('still reports a browser when a tab is opened on a shell-spawned server', () => {
        // The server may have been started with COLLIMATE_CLIENT=electron;
        // only the page knows there is no bridge in it. Posting this is what
        // corrects the server's env seed.
        expect(describeClient().client).toBe('browser');
    });
});

describe('servers surface (MS-05)', () => {
    it('desktopServers is undefined in a browser, and no id is the window\'s server (MS-05)', () => {
        expect(desktopServers()).toBeUndefined();
        expect(isWindowServer('local')).toBe(false);
    });

    it('desktopServers is the shell\'s registry; isWindowServer matches the window\'s server id (MS-05)', () => {
        const shell = installShell();
        expect(desktopServers()).toBe(shell.servers);
        expect(isWindowServer('local')).toBe(true);
        expect(isWindowServer('lab')).toBe(false);
        installShell({ server: { ...shell.server, id: 'lab', kind: 'remote', name: 'Lab', dataDir: null } });
        expect(isWindowServer('lab')).toBe(true);
        expect(isWindowServer('local')).toBe(false);
    });
});

describe('the folder picker and the server kind', () => {
    it('offer nothing in a browser', () => {
        expect(localFolderPicker()).toBeUndefined();
        expect(serverIsRemote()).toBe(false);
    });

    it("pick through the shell's dialog while the window is on the local server", async () => {
        const pickFolder = vi.fn(async () => '/home/me/sdk');
        installShell({ pickFolder });
        expect(serverIsRemote()).toBe(false);
        expect(await localFolderPicker()?.()).toBe('/home/me/sdk');
        expect(pickFolder).toHaveBeenCalledOnce();
    });

    it('offer no picker on a remote server, nor from a shell without one', () => {
        const shell = installShell({ pickFolder: vi.fn(async () => null) });
        shell.server = { ...shell.server, id: 'box', kind: 'remote' };
        expect(serverIsRemote()).toBe(true);
        expect(localFolderPicker()).toBeUndefined();
        uninstallDesktopShell();
        installShell();
        expect(localFolderPicker()).toBeUndefined();
    });
});
