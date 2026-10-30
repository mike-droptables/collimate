import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_CONNECT_URL,
  MAX_SAVED_SERVERS,
  addServer,
  bootTarget,
  credentialFor,
  defaultSettings,
  envTokenFor,
  fetchCatalog,
  findServer,
  findServerByUrl,
  normalizeServerUrl,
  probeServer,
  readSettings,
  removeServer,
  resolveToken,
  updateServer,
  withEnvServer,
  writeSettings,
  type DesktopSettings,
} from '../src/main/connect.js';
import type { FetchLike, FetchResponseLike } from '../src/main/server.js';
import { BRIDGE_PROTOCOL } from '../src/shared/bridge.js';

/**
 * The shell's settings and the remote-server plumbing under the registry:
 * a URL the user typed, one HTTP probe, the channel catalog read, and
 * settings.json. The probe exists to turn "blank window with a Chromium
 * error page" into a sentence — so those tests are mostly about what the
 * failures say.
 */

const dirs: string[] = [];

afterEach(() => {
  vi.useRealTimers();
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function tempDir(): string {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'collimate-connect-'));
  dirs.push(dir);
  return dir;
}

/** A fetch that answers /health with `body`, and records what it was asked. */
function fakeFetch(
  answer: FetchResponseLike | Error | 'hang',
): FetchLike & { calls: string[] } {
  const calls: string[] = [];
  const impl = ((url, init) => {
    calls.push(url);
    if (answer === 'hang') {
      return new Promise<FetchResponseLike>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new Error('The operation was aborted.')));
      });
    }
    if (answer instanceof Error) return Promise.reject(answer);
    return Promise.resolve(answer);
  }) as FetchLike & { calls: string[] };
  impl.calls = calls;
  return impl;
}

function json(status: number, body: unknown): FetchResponseLike {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

const healthy = () => json(200, { status: 'ok', version: '0.1.0', bridge: BRIDGE_PROTOCOL });

/** Settings with these saved servers and everything else default. */
function withServers(servers: DesktopSettings['servers'], extra: Partial<DesktopSettings> = {}): DesktopSettings {
  return { ...defaultSettings(), servers, ...extra };
}

/** Sequential ids for addServer. */
function counter(prefix = 'id'): () => string {
  let next = 0;
  return () => `${prefix}-${++next}`;
}

// ---------------------------------------------------------------------------

describe('normalizeServerUrl', () => {
  it.each([
    ['localhost:5001', 'http://localhost:5001'],
    ['127.0.0.1:5001', 'http://127.0.0.1:5001'],
    ['  127.0.0.1:5001  ', 'http://127.0.0.1:5001'],
    ['http://127.0.0.1:5001', 'http://127.0.0.1:5001'],
    ['HTTP://127.0.0.1:5001', 'http://127.0.0.1:5001'],
    ['https://collimate.example.com', 'https://collimate.example.com'],
    // Origin only: the shell always loads the server's root document.
    ['http://127.0.0.1:5001/', 'http://127.0.0.1:5001'],
    ['http://127.0.0.1:5001/channels/abc?x=1#y', 'http://127.0.0.1:5001'],
    ['http://user:pw@127.0.0.1:5001', 'http://127.0.0.1:5001'],
    ['example.com', 'http://example.com'],
  ])('reads %s as %s', (input, expected) => {
    expect(normalizeServerUrl(input)).toBe(expected);
  });

  it.each([
    ['', 'empty'],
    ['   ', 'whitespace'],
    ['http://', 'a scheme with no host'],
    ['://nope', 'garbage'],
  ])('refuses %s (%s)', (input) => {
    expect(normalizeServerUrl(input)).toBeNull();
  });

  it('rejects non-http(s) schemes outright', () => {
    for (const input of ['file:///etc/passwd', 'ftp://example.com', 'ws://example.com']) {
      expect(normalizeServerUrl(input)).toBeNull();
    }
    // No "//" after the colon means it's not scheme-shaped at all; treated
    // as a bare host like everything else people type, and the URL parse
    // decides. The invariant: output is an http(s) origin or null.
    const normalized = normalizeServerUrl('javascript:alert(1)');
    expect(normalized === null || /^https?:\/\//.test(normalized)).toBe(true);
  });
});

describe('probeServer', () => {
  it('accepts a Collimate server and says where it is', async () => {
    const fetchImpl = fakeFetch(healthy());

    const result = await probeServer('127.0.0.1:5001', fetchImpl);

    // Health first, then the auth question — /health is public on a
    // tokened server, so health alone can't bless a connect target.
    expect(fetchImpl.calls).toEqual([
      'http://127.0.0.1:5001/health',
      'http://127.0.0.1:5001/auth/session',
    ]);
    expect(result).toEqual({ ok: true, url: 'http://127.0.0.1:5001', port: 5001, serverVersion: '0.1.0' });
  });

  it.each([
    ['https://collimate.example.com', 443],
    ['http://collimate.example.com', 80],
  ])('infers the default port for %s', async (url, port) => {
    const result = await probeServer(url, fakeFetch(healthy()));
    expect(result.ok && result.port).toBe(port);
  });

  it('reports a missing server version rather than inventing one', async () => {
    const result = await probeServer('127.0.0.1:5001', fakeFetch(json(200, { bridge: BRIDGE_PROTOCOL })));
    expect(result.ok && result.serverVersion).toBeNull();
  });

  it('refuses a URL it cannot parse without calling out', async () => {
    const fetchImpl = fakeFetch(healthy());

    const result = await probeServer('  ', fetchImpl);

    expect(result).toEqual({
      ok: false,
      message: `"" isn't a URL. Try something like ${DEFAULT_CONNECT_URL}.`,
    });
    expect(fetchImpl.calls).toEqual([]);
  });

  it('reports the status code when /health is not 200', async () => {
    const result = await probeServer('127.0.0.1:5001', fakeFetch(json(404, {})));
    expect(result).toEqual({
      ok: false,
      message: 'http://127.0.0.1:5001/health returned HTTP 404. Is that a Collimate server?',
    });
  });

  it('rejects a 200 that is not JSON', async () => {
    const result = await probeServer(
      '127.0.0.1:5001',
      fakeFetch({ ok: true, status: 200, json: () => Promise.reject(new Error('<!doctype html>')) }),
    );
    expect(result.ok).toBe(false);
    expect(!result.ok && result.message).toContain("didn't return JSON");
  });

  it('rejects a 200 whose body is not an object', async () => {
    const result = await probeServer('127.0.0.1:5001', fakeFetch(json(200, 'ok')));
    expect(!result.ok && result.message).toContain("didn't return a Collimate health response");
  });

  it('explains an older server that reports no bridge at all', async () => {
    const result = await probeServer('127.0.0.1:5001', fakeFetch(json(200, { status: 'ok' })));
    expect(!result.ok && result.message).toContain("didn't report a bridge version");
    expect(!result.ok && result.message).toContain(`this app (which speaks bridge ${BRIDGE_PROTOCOL})`);
  });

  it('refuses a bridge version this shell does not implement', async () => {
    const result = await probeServer('127.0.0.1:5001', fakeFetch(json(200, { bridge: 99 })));
    expect(result).toEqual({
      ok: false,
      message: `http://127.0.0.1:5001 speaks bridge 99; this app speaks ${BRIDGE_PROTOCOL}. Update whichever one is older.`,
    });
  });

  it('turns a refused connection into a sentence', async () => {
    const result = await probeServer('127.0.0.1:5001', fakeFetch(new Error('fetch failed: ECONNREFUSED')));
    expect(!result.ok && result.message).toBe(
      "Couldn't reach http://127.0.0.1:5001: fetch failed: ECONNREFUSED. Is the server running?",
    );
  });

  it('gives up on a socket that never answers', async () => {
    vi.useFakeTimers();
    const pending = probeServer('127.0.0.1:5001', fakeFetch('hang'), { timeoutMs: 5_000 });

    await vi.advanceTimersByTimeAsync(4_000);
    await vi.advanceTimersByTimeAsync(2_000);
    const result = await pending;

    expect(!result.ok && result.message).toBe(
      "http://127.0.0.1:5001 didn't respond within 5s. Is the server running?",
    );
  });
});

describe('bootTarget', () => {
  const saved = withServers([{ id: 'box', url: 'http://box:5001' }, { id: 'nas', url: 'https://nas:8443' }]);

  it('opens the local server by default on POSIX', () => {
    expect(bootTarget({ platform: 'linux', env: {}, settings: defaultSettings() })).toEqual({ serverId: 'local' });
    expect(bootTarget({ platform: 'darwin', env: {}, settings: saved })).toEqual({ serverId: 'local' });
  });

  it('reopens the server the window was last on', () => {
    expect(bootTarget({ platform: 'linux', env: {}, settings: { ...saved, activeServer: 'nas' } })).toEqual({
      serverId: 'nas',
    });
  });

  it('falls back to local when the last server is no longer saved', () => {
    expect(bootTarget({ platform: 'linux', env: {}, settings: { ...saved, activeServer: 'gone' } })).toEqual({
      serverId: 'local',
    });
  });

  it('has no local server on Windows: the first saved remote, or the connect form', () => {
    expect(bootTarget({ platform: 'win32', env: {}, settings: saved })).toEqual({ serverId: 'box' });
    expect(bootTarget({ platform: 'win32', env: {}, settings: { ...saved, activeServer: 'nas' } })).toEqual({
      serverId: 'nas',
    });
    expect(bootTarget({ platform: 'win32', env: {}, settings: defaultSettings() })).toEqual({ serverId: null });
  });

  it('lets COLLIMATE_CONNECT_URL force a remote for automation, once withEnvServer saved it', () => {
    const env = { COLLIMATE_CONNECT_URL: 'http://ci-box:5001' };
    const settings = withEnvServer({ ...saved, activeServer: 'nas' }, env, counter('env'));
    expect(findServerByUrl(settings, 'ci-box:5001')).toEqual({ id: 'env-1', url: 'http://ci-box:5001' });
    expect(bootTarget({ platform: 'linux', env, settings })).toEqual({ serverId: 'env-1' });
    // An already saved URL keeps its id; nothing new is minted.
    expect(withEnvServer(saved, { COLLIMATE_CONNECT_URL: 'box:5001' }, counter('env'))).toBe(saved);
    expect(bootTarget({ platform: 'linux', env: { COLLIMATE_CONNECT_URL: 'box:5001' }, settings: saved })).toEqual({
      serverId: 'box',
    });
  });

  it('never saves the automation token with the automation URL', () => {
    const env = { COLLIMATE_CONNECT_URL: 'http://ci-box:5001', COLLIMATE_CONNECT_TOKEN: 'secret' };
    const settings = withEnvServer(defaultSettings(), env, counter());
    expect(JSON.stringify(settings)).not.toContain('secret');
  });

  it('treats an exported-but-empty COLLIMATE_CONNECT_URL as unset', () => {
    expect(bootTarget({ platform: 'linux', env: { COLLIMATE_CONNECT_URL: '' }, settings: saved })).toEqual({
      serverId: 'local',
    });
    expect(withEnvServer(saved, { COLLIMATE_CONNECT_URL: '' }, counter())).toBe(saved);
  });

  it('an unusable COLLIMATE_CONNECT_URL opens the connect form, not some other server', () => {
    const env = { COLLIMATE_CONNECT_URL: 'ftp://nope' };
    const settings = withEnvServer(saved, env, counter());
    expect(settings).toBe(saved);
    expect(bootTarget({ platform: 'linux', env, settings })).toEqual({ serverId: null });
  });
});

describe('envTokenFor', () => {
  const env = { COLLIMATE_CONNECT_URL: 'https://box.example:8443', COLLIMATE_CONNECT_TOKEN: 'automation-token' };

  it('offers the automation token to the origin COLLIMATE_CONNECT_URL names, however it is spelled__F1012', () => {
    expect(envTokenFor(env, 'https://box.example:8443')).toBe('automation-token');
    expect(envTokenFor(env, 'https://BOX.example:8443/some/page ')).toBe('automation-token');
  });

  it('offers it to no other origin: another host, port or scheme__F1012', () => {
    expect(envTokenFor(env, 'https://other.example:8443')).toBeUndefined();
    expect(envTokenFor(env, 'https://box.example:9443')).toBeUndefined();
    expect(envTokenFor(env, 'http://box.example:8443')).toBeUndefined();
    // A server remembered in settings.json (the URL a launch without the
    // env URL resolves) is another server as far as this token knows.
    expect(envTokenFor(env, DEFAULT_CONNECT_URL)).toBeUndefined();
  });

  it('offers a token set without a URL to nobody__F1012', () => {
    const tokenOnly = { COLLIMATE_CONNECT_TOKEN: 'automation-token' };
    expect(envTokenFor(tokenOnly, DEFAULT_CONNECT_URL)).toBeUndefined();
    expect(envTokenFor({ ...tokenOnly, COLLIMATE_CONNECT_URL: '' }, DEFAULT_CONNECT_URL)).toBeUndefined();
  });

  it('offers nothing when the URL is set but the token is not, or either side is not a URL', () => {
    expect(envTokenFor({ COLLIMATE_CONNECT_URL: 'https://box.example:8443' }, 'https://box.example:8443')).toBeUndefined();
    expect(envTokenFor({ ...env, COLLIMATE_CONNECT_TOKEN: '' }, 'https://box.example:8443')).toBeUndefined();
    expect(envTokenFor({ ...env, COLLIMATE_CONNECT_URL: 'ftp://box.example' }, 'ftp://box.example')).toBeUndefined();
    expect(envTokenFor(env, 'not a url at all')).toBeUndefined();
  });

  it('is the one place the shell reads COLLIMATE_CONNECT_TOKEN, so resolveToken goes through it__F1012', () => {
    // index.ts cannot be imported under test; this pins its wiring instead:
    // a resolveToken that read the env var itself would offer it anywhere.
    const src = fileURLToPath(new URL('../src', import.meta.url));
    const reads = /env(?:\[\s*['"]COLLIMATE_CONNECT_TOKEN['"]\s*\]|\.COLLIMATE_CONNECT_TOKEN\b)/;
    const readers = readdirSync(src, { recursive: true, encoding: 'utf8' })
      .filter((file) => /\.(ts|html)$/.test(file))
      .filter((file) => reads.test(readFileSync(path.join(src, file), 'utf8')));
    expect(readers).toEqual([path.join('main', 'connect.ts')]);
  });
});

describe('probeServer auth check', () => {
  /** A fetch that routes by URL suffix and records request headers. */
  function routedFetch(
    authStatus: number,
  ): FetchLike & { authHeaders: (string | undefined)[] } {
    const authHeaders: (string | undefined)[] = [];
    const impl = ((url: string, init?: Parameters<FetchLike>[1]) => {
      if (url.endsWith('/auth/session')) {
        authHeaders.push(init?.headers?.['authorization']);
        return Promise.resolve(json(authStatus, {}));
      }
      return Promise.resolve(healthy());
    }) as FetchLike & { authHeaders: (string | undefined)[] };
    impl.authHeaders = authHeaders;
    return impl;
  }

  it('an open server (404 from /auth/session) connects without a token', async () => {
    const result = await probeServer('127.0.0.1:5001', routedFetch(404));
    expect(result.ok).toBe(true);
  });

  it('an old server (405 off the static mount) also reads as open', async () => {
    const result = await probeServer('127.0.0.1:5001', routedFetch(405));
    expect(result.ok).toBe(true);
  });

  it('a tokened server without a token is a sentence, not a blank window', async () => {
    const result = await probeServer('127.0.0.1:5001', routedFetch(401));
    expect(result).toEqual({
      ok: false,
      message: 'http://127.0.0.1:5001 requires an access token.',
    });
  });

  it('a wrong token is called out as rejected', async () => {
    const result = await probeServer('127.0.0.1:5001', routedFetch(401), {
      token: 'nope',
    });
    expect(result).toEqual({
      ok: false,
      message: 'http://127.0.0.1:5001 rejected the access token.',
    });
  });

  it('sends the token as a bearer header on the auth check', async () => {
    const fetchImpl = routedFetch(204);
    const result = await probeServer('127.0.0.1:5001', fetchImpl, { token: 't0k3n' });
    expect(result.ok).toBe(true);
    expect(fetchImpl.authHeaders).toEqual(['Bearer t0k3n']);
  });
});

describe('saved servers', () => {
  it('round-trip through disk by id, junk and duplicate entries dropped', () => {
    const dir = tempDir();
    writeSettings(dir, withServers([{ id: 'a', url: 'https://box:8443', name: 'box', token: 't', fingerprint: 'sha256/abc' }]));
    // Corrupt entries by hand to prove sanitization.
    const raw = JSON.parse(readFileSync(path.join(dir, 'settings.json'), 'utf8'));
    raw.servers.push(
      { name: 'no-url', id: 'b' },
      { url: 'http://no-id:5001' },
      { id: 'c', url: 'ftp://not-http' },
      { id: 'a', url: 'http://same-id:5001' },
      { id: 'd', url: 'https://BOX:8443/path' },
      42,
      null,
    );
    writeFileSync(path.join(dir, 'settings.json'), JSON.stringify(raw));

    expect(readSettings(dir).servers).toEqual([
      { id: 'a', url: 'https://box:8443', name: 'box', token: 't', fingerprint: 'sha256/abc' },
    ]);
  });

  it('addServer mints an id for a new URL and keeps the id of a saved one', () => {
    const mint = counter();
    const first = addServer(defaultSettings(), { url: 'box:5001', token: 'old' }, mint);
    expect(first).toMatchObject({ ok: true, server: { id: 'id-1', url: 'http://box:5001', token: 'old' } });
    if (!first.ok) return;
    // The same origin, however spelled: same entry, fields merged over it.
    const again = addServer(first.settings, { url: 'http://box:5001/x', name: 'Box', token: 'new' }, mint);
    expect(again.ok && again.settings.servers).toEqual([{ id: 'id-1', url: 'http://box:5001', name: 'Box', token: 'new' }]);
    const other = addServer(first.settings, { url: 'http://nas:5001' }, mint);
    // Merging into a saved URL minted nothing.
    expect(other.ok && other.settings.servers.map((server) => server.id)).toEqual(['id-1', 'id-2']);
  });

  it('addServer refuses a junk URL and a full list rather than forgetting a server', () => {
    expect(addServer(defaultSettings(), { url: 'ftp://x' }, counter())).toMatchObject({ ok: false });
    let settings = defaultSettings();
    const mint = counter();
    for (let i = 0; i < MAX_SAVED_SERVERS; i += 1) {
      const added = addServer(settings, { url: `http://box-${i}:5001` }, mint);
      if (added.ok) settings = added.settings;
    }
    expect(settings.servers).toHaveLength(MAX_SAVED_SERVERS);
    const refused = addServer(settings, { url: 'http://one-too-many:5001' }, mint);
    expect(refused).toEqual({ ok: false, message: `At most ${MAX_SAVED_SERVERS} servers can be saved. Remove one first.` });
  });

  it('findServer is by id, findServerByUrl by normalized origin', () => {
    const settings = withServers([{ id: 'a', url: 'http://box:5001', token: 't' }]);
    expect(findServer(settings, 'a')?.token).toBe('t');
    expect(findServer(settings, 'http://box:5001')).toBeUndefined();
    expect(findServerByUrl(settings, 'box:5001')?.id).toBe('a');
    expect(findServerByUrl(settings, 'http://other:5001')).toBeUndefined();
  });

  it('updateServer renames, replaces or clears a token, pins; unknown ids change nothing', () => {
    const settings = withServers([{ id: 'a', url: 'http://box:5001', name: 'Box', token: 't' }]);
    expect(updateServer(settings, 'a', { name: ' Lab ' }).servers[0]).toEqual({ id: 'a', url: 'http://box:5001', name: 'Lab', token: 't' });
    expect(updateServer(settings, 'a', { name: '' }).servers[0]).toEqual({ id: 'a', url: 'http://box:5001', token: 't' });
    expect(updateServer(settings, 'a', { token: 'u' }).servers[0]?.token).toBe('u');
    expect(updateServer(settings, 'a', { token: '' }).servers[0]).not.toHaveProperty('token');
    expect(updateServer(settings, 'a', { fingerprint: 'sha256/x' }).servers[0]?.fingerprint).toBe('sha256/x');
    expect(updateServer(settings, 'zzz', { name: 'x' })).toEqual(settings);
  });

  it('removeServer forgets it and a launch target naming it falls back to local', () => {
    const settings = withServers([{ id: 'a', url: 'http://box:5001' }, { id: 'b', url: 'http://nas:5001' }], { activeServer: 'a' });
    expect(removeServer(settings, 'a')).toEqual(withServers([{ id: 'b', url: 'http://nas:5001' }]));
    expect(removeServer(settings, 'b').activeServer).toBe('a');
  });
});

describe('settings', () => {
  it('round-trips through disk', () => {
    const dir = tempDir();
    const settings = withServers([{ id: 'a', url: 'http://127.0.0.1:5001' }], {
      activeServer: 'a',
      keepLocalRunning: true,
      localPort: 51234,
    });
    writeSettings(dir, settings);
    expect(readSettings(dir)).toEqual(settings);
    expect(readFileSync(path.join(dir, 'settings.json'), 'utf8').endsWith('\n')).toBe(true);
  });

  it('writes atomically and owner-only: no temp file left behind', () => {
    const dir = tempDir();
    writeSettings(dir, withServers([{ id: 'a', url: 'https://box:8443', token: 't' }]));
    expect(readdirSync(dir)).toEqual(['settings.json']);
    if (process.platform !== 'win32') {
      expect(statSync(path.join(dir, 'settings.json')).mode & 0o777).toBe(0o600);
    }
  });

  it('replaces an existing settings file', () => {
    const dir = tempDir();
    writeSettings(dir, withServers([{ id: 'a', url: 'http://a:5001' }]));
    writeSettings(dir, { ...defaultSettings(), serverSpec: 'local:/home/dev/collimate' });
    expect(readSettings(dir)).toEqual({ ...defaultSettings(), serverSpec: 'local:/home/dev/collimate' });
    expect(readdirSync(dir)).toEqual(['settings.json']);
  });

  it('defaults to the local server, keep-alive off, nothing saved', () => {
    expect(readSettings(tempDir())).toEqual({ servers: [], activeServer: 'local', keepLocalRunning: false });
  });

  it.each([
    ['corrupt', '{ not json'],
    ['not an object', '"hello"'],
  ])('falls back to the defaults for a %s settings file', (_label, contents) => {
    const dir = tempDir();
    writeFileSync(path.join(dir, 'settings.json'), contents);
    expect(readSettings(dir)).toEqual(defaultSettings());
  });

  it('reads only the current keys: the retired mode and serverUrl are simply gone', () => {
    const dir = tempDir();
    writeFileSync(
      path.join(dir, 'settings.json'),
      JSON.stringify({ mode: 'connect', serverUrl: 'http://box:5001', servers: [{ url: 'http://box:5001', token: 't' }] }),
    );
    const settings = readSettings(dir);
    expect(settings).toEqual(defaultSettings());
    expect(settings).not.toHaveProperty('mode');
    expect(settings).not.toHaveProperty('serverUrl');
  });

  it.each([
    ['a non-boolean keepLocalRunning', { keepLocalRunning: 'yes' }],
    ['a localPort that is not a port', { localPort: 70000 }],
    ['a fractional localPort', { localPort: 5001.5 }],
    ['an activeServer that is not saved', { activeServer: 'gone' }],
  ])('ignores %s', (_label, junk) => {
    const dir = tempDir();
    writeFileSync(path.join(dir, 'settings.json'), JSON.stringify(junk));
    expect(readSettings(dir)).toEqual(defaultSettings());
  });

  it('round-trips serverSpec — the installed-app pointer at a checkout', () => {
    // build/build_desktop.sh merges this key in after it installs a
    // locally built app; the shell reads it back into the controller
    // (server.ts, buildSpawnPlan) because a launcher-started app has no
    // environment to carry COLLIMATE_SERVER_SPEC in.
    const dir = tempDir();
    writeSettings(dir, { ...defaultSettings(), serverSpec: 'local:/home/dev/collimate' });
    expect(readSettings(dir).serverSpec).toBe('local:/home/dev/collimate');
  });

  it.each([
    ['a non-string', 42],
    ['an empty string', ''],
    ['whitespace', '   '],
  ])('drops %s serverSpec', (_label, serverSpec) => {
    const dir = tempDir();
    writeFileSync(path.join(dir, 'settings.json'), JSON.stringify({ serverSpec }));
    expect(readSettings(dir)).toEqual(defaultSettings());
  });
});

it('refuses the retired shell contract that lacks live zoom __F0985', async () => {
  const result = await probeServer('127.0.0.1:5001', fakeFetch(json(200, { bridge: 2 })));
  expect(result.ok).toBe(false);
  expect(!result.ok && result.message).toContain(`this app speaks ${BRIDGE_PROTOCOL}`);
});

describe('pure credential resolution __F1015', () => {
  const settings = withServers([
    { id: 'one', url: 'https://one.test', token: 'saved', fingerprint: 'sha256/pin' },
    { id: 'two', url: 'https://two.test' },
    { id: 'three', url: 'https://three.test' },
  ]);
  const env = { COLLIMATE_CONNECT_URL: 'https://two.test', COLLIMATE_CONNECT_TOKEN: 'environment' };
  it('keeps explicit, saved, then matching environment precedence, by id __F1015', () => {
    expect(resolveToken(settings, env, 'one', 'typed')).toBe('typed');
    expect(resolveToken(settings, env, 'one', '')).toBe('saved');
    expect(resolveToken(settings, env, 'two')).toBe('environment');
    // The env token belongs to its own origin only.
    expect(resolveToken(settings, env, 'three')).toBeUndefined();
    expect(resolveToken(settings, env, 'unknown')).toBeUndefined();
  });
  it('binds the window to the server origin and the supplied token only __F1015', () => {
    expect(credentialFor(settings, 'one', 'typed')).toEqual({ origin: 'https://one.test', token: 'typed' });
    expect(credentialFor(settings, 'two')).toEqual({ origin: 'https://two.test' });
    expect(credentialFor(settings, 'unknown')).toBeNull();
  });
});

describe('fetchCatalog (MS-02)', () => {
  function catalogFetch(status: number, body: unknown): FetchLike & { calls: { url: string; headers?: Record<string, string> }[] } {
    const calls: { url: string; headers?: Record<string, string> }[] = [];
    const impl = ((url: string, init?: Parameters<FetchLike>[1]) => {
      calls.push({ url, ...(init?.headers ? { headers: init.headers } : {}) });
      return Promise.resolve(json(status, body));
    }) as FetchLike & { calls: typeof calls };
    impl.calls = calls;
    return impl;
  }

  it('reads ids, names and the active channel, with the token as an explicit header (MS-02)', async () => {
    const fetchImpl = catalogFetch(200, {
      channels: [{ id: 'c1', name: 'One' }, { id: 'c2', name: 'Two', extra: 1 }, { id: 3 }, null],
      active_channel_id: 'c2',
    });
    const result = await fetchCatalog('https://box.test', 'tok', fetchImpl);
    expect(result).toEqual({
      ok: true,
      channels: [{ id: 'c1', name: 'One' }, { id: 'c2', name: 'Two' }],
      activeChannelId: 'c2',
    });
    expect(fetchImpl.calls).toEqual([{ url: 'https://box.test/api/channels', headers: { authorization: 'Bearer tok' } }]);
  });

  it('sends no header without a token, and drops an active id it was not given (MS-02)', async () => {
    const fetchImpl = catalogFetch(200, { channels: [], active_channel_id: 'ghost' });
    expect(await fetchCatalog('http://127.0.0.1:5001', undefined, fetchImpl)).toEqual({
      ok: true,
      channels: [],
      activeChannelId: null,
    });
    expect(fetchImpl.calls[0]).toEqual({ url: 'http://127.0.0.1:5001/api/channels' });
  });

  it('tells a refused token from an unreachable server (MS-02)', async () => {
    expect(await fetchCatalog('https://box.test', undefined, catalogFetch(401, {}))).toMatchObject({
      ok: false,
      reason: 'unauthorized',
      message: 'https://box.test requires an access token.',
    });
    expect(await fetchCatalog('https://box.test', 'bad', catalogFetch(401, {}))).toMatchObject({
      ok: false,
      reason: 'unauthorized',
      message: 'https://box.test rejected the access token.',
    });
    expect(await fetchCatalog('https://box.test', undefined, catalogFetch(500, {}))).toMatchObject({
      ok: false,
      reason: 'unreachable',
    });
    expect(await fetchCatalog('https://box.test', undefined, fakeFetch(new Error('ECONNREFUSED')))).toMatchObject({
      ok: false,
      reason: 'unreachable',
    });
  });
});

describe('interface scale setting (RS-05)', () => {
  it('round-trips a listed scale and drops anything else (RS-05)', () => {
    const dir = tempDir();
    for (const scale of [0.7, 0.8, 0.9, 1, 1.1, 1.25, 1.5] as const) {
      writeSettings(dir, { ...defaultSettings(), interfaceScale: scale });
      expect(readSettings(dir).interfaceScale).toBe(scale);
    }
    for (const junk of [1.75, 0, -1, '1.1', null, true, 1.1000001]) {
      writeFileSync(path.join(dir, 'settings.json'), JSON.stringify({ interfaceScale: junk }));
      const settings = readSettings(dir);
      expect(settings).toEqual(defaultSettings());
      expect('interfaceScale' in settings).toBe(false);
    }
  });
});
