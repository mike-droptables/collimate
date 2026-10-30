import { describe, expect, it, vi } from 'vitest';
import { isLoopbackUrl, isSameOrigin, shellCapabilities } from '../src/shared/bridge.js';
import {
  pinsFor,
  ServerAccess,
  SHELL_REQUEST_HEADER,
  shellRequestInit,
  type PinMap,
  type ServerAccessDeps,
  type SessionLike,
} from '../src/main/serverAccess.js';

/**
 * The session seam, against a fake session. Two properties carry the
 * security weight: the bearer header lands on the window's server origin
 * and nowhere else — never on another saved server's — and certificate
 * pinning only ever loosens trust for a host the user explicitly accepted.
 */

type HeaderListener =
  | ((
      details: { url: string; requestHeaders: Record<string, string> },
      callback: (response: { requestHeaders: Record<string, string> }) => void,
    ) => void)
  | null;

type VerifyProc =
  | ((
      request: { hostname: string; certificate: { fingerprint: string }; errorCode: number },
      callback: (result: number) => void,
    ) => void)
  | null;

function fakeSession(): SessionLike & { headerListener: HeaderListener; verifyProc: VerifyProc } {
  const ses = {
    headerListener: null as HeaderListener,
    verifyProc: null as VerifyProc,
    webRequest: {
      onBeforeSendHeaders(_filter: { urls: string[] }, listener: HeaderListener) {
        ses.headerListener = listener;
      },
    },
    setCertificateVerifyProc(proc: VerifyProc) {
      ses.verifyProc = proc;
    },
  };
  return ses;
}

function deps(overrides: Partial<ServerAccessDeps> = {}): ServerAccessDeps {
  return {
    confirmPin: vi.fn().mockResolvedValue(false),
    savePin: vi.fn(),
    ...overrides,
  };
}

/** Run the installed header listener for `url` and return the headers. */
function inject(
  ses: ReturnType<typeof fakeSession>,
  url: string,
  requestHeaders: Record<string, string> = {},
): Record<string, string> {
  let out: Record<string, string> = {};
  ses.headerListener?.({ url, requestHeaders: { ...requestHeaders } }, ({ requestHeaders }) => {
    out = requestHeaders;
  });
  return out;
}

/** Run the installed verify proc and return its verdict. */
function verify(
  ses: ReturnType<typeof fakeSession>,
  request: { hostname: string; fingerprint: string; errorCode: number },
): Promise<number> {
  return new Promise((resolve) => {
    ses.verifyProc?.(
      {
        hostname: request.hostname,
        certificate: { fingerprint: request.fingerprint },
        errorCode: request.errorCode,
      },
      resolve,
    );
  });
}

/** Pins for these saved servers, as index.ts builds them from settings. */
function pins(...servers: { id: string; url: string; fingerprint?: string }[]): PinMap {
  return pinsFor(servers);
}

describe('bindWindow (MS-02)', () => {
  it('adds the bearer header on the window origin only — exact port included (MS-02)', () => {
    const ses = fakeSession();
    new ServerAccess(ses, deps()).bindWindow({ origin: 'https://box.example:8443', token: 'secret' });

    expect(inject(ses, 'https://box.example:8443/api/state')).toEqual({
      Authorization: 'Bearer secret',
    });
    expect(inject(ses, 'https://box.example:9999/api/state')).toEqual({});
    expect(inject(ses, 'https://other.example:8443/')).toEqual({});
    expect(inject(ses, 'http://box.example:8443/')).toEqual({});
  });

  it('other saved servers get no header, however many are pinned (MS-02)', () => {
    const ses = fakeSession();
    const access = new ServerAccess(ses, deps());
    access.setPins(pins({ id: 'a', url: 'https://a.example' }, { id: 'b', url: 'https://b.example' }));
    access.bindWindow({ origin: 'https://a.example', token: 'token-a' });

    expect(inject(ses, 'https://a.example/api')).toEqual({ Authorization: 'Bearer token-a' });
    expect(inject(ses, 'https://b.example/api')).toEqual({});
    expect(access.windowCredential).toEqual({ origin: 'https://a.example', token: 'token-a' });
  });

  it("the shell's own request to the window's server keeps the token it chose (MS-02)", () => {
    // A probe of the window's server with a new token — Change token, Test,
    // re-add — goes through the same session as the page. It must test
    // that token, not the bound one.
    const ses = fakeSession();
    new ServerAccess(ses, deps()).bindWindow({ origin: 'https://box.example', token: 'old' });
    const probe = shellRequestInit({ headers: { authorization: 'Bearer new' } });

    expect(inject(ses, 'https://box.example/auth/session', probe.headers)).toEqual({ authorization: 'Bearer new' });
  });

  it("the shell's own token-less request gets no token either, and loses its marker (MS-02)", () => {
    const ses = fakeSession();
    new ServerAccess(ses, deps()).bindWindow({ origin: 'https://box.example', token: 'old' });

    expect(inject(ses, 'https://box.example/health', shellRequestInit().headers)).toEqual({});
    expect(inject(ses, 'https://box.example/health', { 'x-collimate-shell': '1' })).toEqual({});
  });

  it('an explicit authorization header on a window request is left as it is', () => {
    const ses = fakeSession();
    new ServerAccess(ses, deps()).bindWindow({ origin: 'https://box.example', token: 'old' });

    expect(inject(ses, 'https://box.example/api', { AUTHORIZATION: 'Basic abc' })).toEqual({ AUTHORIZATION: 'Basic abc' });
  });

  it('shellRequestInit keeps the caller\'s init and headers', () => {
    expect(shellRequestInit({ method: 'POST', headers: { authorization: 'Bearer t' } })).toEqual({
      method: 'POST',
      headers: { authorization: 'Bearer t', [SHELL_REQUEST_HEADER]: '1' },
    });
    expect(shellRequestInit()).toEqual({ headers: { [SHELL_REQUEST_HEADER]: '1' } });
  });

  it('no token clears the listener entirely', () => {
    const ses = fakeSession();
    const access = new ServerAccess(ses, deps());
    access.bindWindow({ origin: 'http://box:5001', token: 'secret' });
    expect(ses.headerListener).not.toBeNull();

    access.bindWindow(null);
    expect(ses.headerListener).toBeNull();

    access.bindWindow({ origin: 'http://box:5001' });
    expect(ses.headerListener).toBeNull();
  });

  it('a stale listener from a replaced window binding injects nothing', () => {
    const ses = fakeSession();
    const access = new ServerAccess(ses, deps());
    access.bindWindow({ origin: 'http://old:5001', token: 'old-secret' });
    const stale = ses.headerListener;

    access.bindWindow({ origin: 'http://new:5001', token: 'new-secret' });

    let out: Record<string, string> = { preexisting: 'yes' };
    stale?.({ url: 'http://old:5001/api', requestHeaders: out }, ({ requestHeaders }) => {
      out = requestHeaders;
    });
    expect(out).toEqual({ preexisting: 'yes' });
  });
});

describe('setPins (MS-02)', () => {
  it('pinsFor covers saved https servers only, one entry per host', () => {
    const map = pins(
      { id: 'a', url: 'https://box.example:8443', fingerprint: 'sha256/a' },
      { id: 'b', url: 'http://plain.example:5001' },
      { id: 'c', url: 'https://nas.example' },
      { id: 'd', url: 'https://box.example:9443' },
    );
    expect([...map.entries()]).toEqual([
      ['box.example', { id: 'a', origin: 'https://box.example:8443', fingerprint: 'sha256/a' }],
      ['nas.example', { id: 'c', origin: 'https://nas.example' }],
    ]);
  });

  it('no verify proc while no https server is saved', () => {
    const ses = fakeSession();
    const access = new ServerAccess(ses, deps());
    access.setPins(pins({ id: 'a', url: 'http://box:5001' }));
    expect(ses.verifyProc).toBeNull();
    access.setPins(pins({ id: 'a', url: 'https://box.example' }));
    expect(ses.verifyProc).not.toBeNull();
    access.setPins(new Map());
    expect(ses.verifyProc).toBeNull();
  });

  it("chromium's verdict stands for unsaved hosts and for valid certificates", async () => {
    const ses = fakeSession();
    new ServerAccess(ses, deps()).setPins(pins({ id: 'a', url: 'https://box.example:8443' }));

    await expect(
      verify(ses, { hostname: 'elsewhere.example', fingerprint: 'sha256/x', errorCode: -202 }),
    ).resolves.toBe(-3);
    await expect(
      verify(ses, { hostname: 'box.example', fingerprint: 'sha256/x', errorCode: 0 }),
    ).resolves.toBe(-3);
  });

  it('covers several hosts at once, each with its own stored pin (MS-02)', async () => {
    const ses = fakeSession();
    new ServerAccess(ses, deps()).setPins(
      pins(
        { id: 'a', url: 'https://a.example', fingerprint: 'sha256/a' },
        { id: 'b', url: 'https://b.example', fingerprint: 'sha256/b' },
      ),
    );

    await expect(verify(ses, { hostname: 'a.example', fingerprint: 'sha256/a', errorCode: -202 })).resolves.toBe(0);
    await expect(verify(ses, { hostname: 'b.example', fingerprint: 'sha256/b', errorCode: -202 })).resolves.toBe(0);
    // A's pin is not B's: a changed certificate fails hard.
    await expect(verify(ses, { hostname: 'b.example', fingerprint: 'sha256/a', errorCode: -202 })).resolves.toBe(-2);
  });

  it('first use asks once; acceptance pins live and saves under the server id (MS-02)', async () => {
    const ses = fakeSession();
    const confirmPin = vi.fn().mockResolvedValue(true);
    const savePin = vi.fn();
    const access = new ServerAccess(ses, deps({ confirmPin, savePin }));
    access.setPins(pins({ id: 'server-a', url: 'https://box.example:8443' }));

    await expect(
      verify(ses, { hostname: 'box.example', fingerprint: 'sha256/new', errorCode: -202 }),
    ).resolves.toBe(0);
    expect(confirmPin).toHaveBeenCalledWith('box.example', 'sha256/new');
    expect(savePin).toHaveBeenCalledWith('server-a', 'sha256/new');
    expect(access.pinFor('box.example')?.fingerprint).toBe('sha256/new');

    // The accepted pin is live immediately — no second prompt.
    await expect(
      verify(ses, { hostname: 'box.example', fingerprint: 'sha256/new', errorCode: -202 }),
    ).resolves.toBe(0);
    expect(confirmPin).toHaveBeenCalledTimes(1);
  });

  it('declining leaves nothing pinned', async () => {
    const ses = fakeSession();
    const savePin = vi.fn();
    new ServerAccess(ses, deps({ savePin })).setPins(pins({ id: 'a', url: 'https://box.example:8443' }));

    await expect(
      verify(ses, { hostname: 'box.example', fingerprint: 'sha256/new', errorCode: -202 }),
    ).resolves.toBe(-2);
    expect(savePin).not.toHaveBeenCalled();
  });

  it('one prompt at a time across hosts: the rest are rejected, not queued (MS-02)', async () => {
    const ses = fakeSession();
    let resolvePrompt: (v: boolean) => void = () => {};
    const confirmPin = vi.fn().mockImplementation(
      () => new Promise<boolean>((resolve) => (resolvePrompt = resolve)),
    );
    new ServerAccess(ses, deps({ confirmPin })).setPins(
      pins({ id: 'a', url: 'https://a.example' }, { id: 'b', url: 'https://b.example' }),
    );

    const first = verify(ses, { hostname: 'a.example', fingerprint: 'sha256/a', errorCode: -202 });
    await expect(
      verify(ses, { hostname: 'a.example', fingerprint: 'sha256/a', errorCode: -202 }),
    ).resolves.toBe(-2);
    await expect(
      verify(ses, { hostname: 'b.example', fingerprint: 'sha256/b', errorCode: -202 }),
    ).resolves.toBe(-2);

    resolvePrompt(true);
    await expect(first).resolves.toBe(0);
    expect(confirmPin).toHaveBeenCalledTimes(1);
    // Once answered, the next host gets its own prompt.
    const second = verify(ses, { hostname: 'b.example', fingerprint: 'sha256/b', errorCode: -202 });
    resolvePrompt(false);
    await expect(second).resolves.toBe(-2);
    expect(confirmPin).toHaveBeenCalledTimes(2);
  });

  it('a pin accepted while the map was replaced does not land on another server', async () => {
    const ses = fakeSession();
    let resolvePrompt: (v: boolean) => void = () => {};
    const savePin = vi.fn();
    const access = new ServerAccess(
      ses,
      deps({ confirmPin: () => new Promise<boolean>((resolve) => (resolvePrompt = resolve)), savePin }),
    );
    access.setPins(pins({ id: 'old', url: 'https://box.example' }));
    const pending = verify(ses, { hostname: 'box.example', fingerprint: 'sha256/x', errorCode: -202 });
    access.setPins(pins({ id: 'new', url: 'https://box.example' }));

    resolvePrompt(true);
    await expect(pending).resolves.toBe(0);
    // Saved under the server the user was asked about; the new entry is untouched.
    expect(savePin).toHaveBeenCalledWith('old', 'sha256/x');
    expect(access.pinFor('box.example')).toEqual({ id: 'new', origin: 'https://box.example' });
  });
});

describe('isSameOrigin', () => {
  it.each([
    ['https://box.example:8443/api/x', 'https://box.example:8443', true],
    ['https://box.example:9443/', 'https://box.example:8443', false],
    ['not a url', 'https://box.example:8443', false],
  ])('%s on %s → %s', (url, origin, expected) => {
    expect(isSameOrigin(url, origin)).toBe(expected);
  });
});

describe('computed capabilities', () => {
  it.each([
    ['http://127.0.0.1:5001', true],
    ['http://localhost:5001', true],
    ['http://[::1]:5001', true],
    ['http://app.localhost:5001', true],
    ['https://box.example:8443', false],
    ['http://192.168.1.20:5001', false],
    ['not a url', false],
  ])('%s → directLoopback %s', (url, expected) => {
    expect(isLoopbackUrl(url)).toBe(expected);
    expect(shellCapabilities(url).directLoopback).toBe(expected);
    // The static four ride along unchanged.
    expect(shellCapabilities(url).nativeGuests).toBe(true);
  });

  // The page draws the title bar exactly where the shell hid the native
  // one (window.ts titleBarWindowOptions) — macOS keeps its bar, so a
  // shell there must not advertise the capability. The window controls
  // are the page's job only where the window is fully frameless (Linux);
  // Windows gets native overlay controls from the shell.
  it.each([
    ['darwin', false, false],
    ['linux', true, true],
    ['win32', true, false],
  ] as const)('%s → customTitleBar %s, customWindowControls %s', (platform, bar, controls) => {
    const caps = shellCapabilities('http://127.0.0.1:5001', platform);
    expect(caps.customTitleBar).toBe(bar);
    expect(caps.customWindowControls).toBe(controls);
  });
});
