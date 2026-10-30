import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { defaultSettings, type DesktopSettings } from '../src/main/connect.js';
import type { FetchLike, FetchResponseLike, ServerState } from '../src/main/server.js';
import type { PinMap } from '../src/main/serverAccess.js';
import {
  localStatus,
  parseDraft,
  parseId,
  parseOpenChannel,
  parsePrefs,
  parseProbe,
  parseUpdate,
  POLL_INTERVAL_MS,
  RegistryError,
  ServerRegistry,
  serverName,
  serversForView,
  type LocalServerView,
  type WindowTarget,
} from '../src/main/servers.js';
import { BRIDGE_PROTOCOL, type ServerEntry, type ServerInfo } from '../src/shared/bridge.js';

/**
 * The servers registry, against fakes for everything Electron owns.
 *
 * The properties that carry weight: a token never leaves the shell (the
 * page sees `hasToken` only), each poll carries exactly its own server's
 * token as an explicit header — the env token only for the origin
 * COLLIMATE_CONNECT_URL names — picking a channel elsewhere never stops
 * the local server, destructive and credential-changing ops wait for a
 * native confirmation, and every settings write carries the keys it does
 * not own (serverSpec, pins, the interface scale) across.
 */

const LOCAL_URL = 'http://127.0.0.1:51000';
const LOCAL_INFO: ServerInfo = {
  id: 'local',
  kind: 'local',
  name: 'Local',
  url: LOCAL_URL,
  port: 51000,
  dataDir: '/data',
  serverVersion: '0.1.0',
};

/** How one fake server behaves. */
interface FakeServer {
  health: 'ok' | 'down' | { bridge: number };
  version?: string;
  /** The token /api/channels and /auth/session accept; none = open server. */
  token?: string;
  channels?: { id: string; name: string }[];
  active?: string;
  /** POST /api/channels/{id}/select answers with this status. */
  selectStatus?: number;
}

interface Call {
  url: string;
  method: string;
  authorization?: string;
}

function response(status: number, body: unknown): FetchResponseLike {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

class Rig {
  settings: DesktopSettings;
  readonly saved: DesktopSettings[] = [];
  readonly calls: Call[] = [];
  readonly servers = new Map<string, FakeServer>();
  readonly pins: PinMap[] = [];
  readonly opened: WindowTarget[] = [];
  readonly openOptions: Array<{ newChannel: boolean } | undefined> = [];
  readonly lifecycle: string[] = [];
  readonly stopOptions: Array<{ reportFailureToNewWindow: boolean } | undefined> = [];
  openWindowError: Error | null = null;
  openWindowDoesNotSwitch = false;
  readonly confirms: string[] = [];
  readonly credentialChanges: string[] = [];
  readonly changes: ServerEntry[][] = [];
  local: LocalServerView | null = { state: { name: 'ready', info: LOCAL_INFO }, info: LOCAL_INFO };
  windowId: string | null = 'local';
  confirmAnswer = true;
  focused = true;
  env: NodeJS.ProcessEnv = {};
  stopLocal = vi.fn(async () => {
    this.lifecycle.push('stop local');
    this.local = { state: { name: 'stopped' }, info: null };
  });
  startLocal = vi.fn(async () => {
    this.local = { state: { name: 'ready', info: LOCAL_INFO }, info: LOCAL_INFO };
    return LOCAL_INFO;
  });
  private nextId = 0;
  readonly registry: ServerRegistry;

  constructor(settings: Partial<DesktopSettings> = {}) {
    this.settings = { ...defaultSettings(), ...settings };
    this.servers.set(LOCAL_URL, { health: 'ok', channels: [{ id: 'l1', name: 'Local one' }], active: 'l1' });
    this.registry = new ServerRegistry({
      host: {
        local: () => this.local,
        windowServerId: () => this.windowId,
        openWindow: async (target, options) => {
          this.lifecycle.push(`open ${target.info.id}`);
          if (this.openWindowError) throw this.openWindowError;
          this.opened.push(target);
          this.openOptions.push(options);
          if (!this.openWindowDoesNotSwitch) this.windowId = target.info.id;
        },
        startLocal: () => this.startLocal(),
        stopLocal: (options) => { this.stopOptions.push(options); return this.stopLocal(); },
        credentialChanged: (id) => this.credentialChanges.push(id),
        confirm: async ({ message }) => {
          this.confirms.push(message);
          return this.confirmAnswer;
        },
      },
      fetchImpl: this.fetchImpl,
      settings: () => this.settings,
      saveSettings: (next) => {
        this.settings = next;
        this.saved.push(next);
      },
      env: this.env,
      mintId: () => `minted-${++this.nextId}`,
      setPins: (pins) => this.pins.push(pins),
      isFocused: () => this.focused,
      setTimeoutImpl: (fn, ms) => setTimeout(fn, ms),
      clearTimeoutImpl: (handle) => clearTimeout(handle as NodeJS.Timeout),
    });
    this.registry.events.on('changed', (entries) => this.changes.push(entries));
  }

  /** Requests (other than to the local server) that carried a header. */
  headersTo(origin: string): (string | undefined)[] {
    return this.calls.filter((call) => call.url.startsWith(origin)).map((call) => call.authorization);
  }

  private readonly fetchImpl: FetchLike = (url, init) => {
    const authorization = init?.headers?.['authorization'];
    this.calls.push({ url, method: init?.method ?? 'GET', ...(authorization ? { authorization } : {}) });
    const parsed = new URL(url);
    const server = this.servers.get(parsed.origin);
    if (!server || server.health === 'down') return Promise.reject(new Error('ECONNREFUSED'));
    const authorized = !server.token || authorization === `Bearer ${server.token}`;
    if (parsed.pathname === '/health') {
      const bridge = typeof server.health === 'object' ? server.health.bridge : BRIDGE_PROTOCOL;
      return Promise.resolve(response(200, { status: 'ok', version: server.version ?? '0.1.0', bridge }));
    }
    if (parsed.pathname === '/auth/session') {
      if (!server.token) return Promise.resolve(response(404, {}));
      return Promise.resolve(response(authorized ? 204 : 401, {}));
    }
    if (parsed.pathname === '/api/channels') {
      if (!authorized) return Promise.resolve(response(401, {}));
      return Promise.resolve(
        response(200, { channels: server.channels ?? [], active_channel_id: server.active ?? null }),
      );
    }
    const select = parsed.pathname.match(/^\/api\/channels\/([^/]+)\/select$/);
    if (select && init?.method === 'POST') {
      if (!authorized) return Promise.resolve(response(401, {}));
      server.active = decodeURIComponent(select[1]!);
      return Promise.resolve(response(server.selectStatus ?? 200, {}));
    }
    return Promise.resolve(response(404, {}));
  };
}

const BOX = 'https://box.example:8443';
const NAS = 'http://nas.example:5001';

function twoRemotes(extra: Partial<DesktopSettings> = {}): Rig {
  const rig = new Rig({
    servers: [
      { id: 'box', name: 'Box', url: BOX, token: 'box-token', fingerprint: 'sha256/box' },
      { id: 'nas', url: NAS },
    ],
    serverSpec: 'local:/home/dev/collimate',
    interfaceScale: 1.1,
    localPort: 51000,
    ...extra,
  });
  rig.servers.set(BOX, { health: 'ok', token: 'box-token', channels: [{ id: 'b1', name: 'Box one' }, { id: 'b2', name: 'Box two' }], active: 'b1', version: '0.2.0' });
  rig.servers.set(NAS, { health: 'ok', channels: [{ id: 'n1', name: 'Nas one' }], active: 'n1' });
  return rig;
}

beforeEach(() => {
  vi.useFakeTimers({ now: 1_700_000_000_000 });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('entries and statuses (MS-02)', () => {
  it('lists the local server first, then the saved remotes, with fresh catalogs (MS-02)', async () => {
    const rig = twoRemotes();

    const entries = await rig.registry.list();

    expect(entries).toEqual([
      {
        id: 'local', kind: 'local', name: 'Local', url: LOCAL_URL, status: 'running', serverVersion: '0.1.0',
        hasToken: false, channels: [{ id: 'l1', name: 'Local one' }], activeChannelId: 'l1', isWindowServer: true,
      },
      {
        id: 'box', kind: 'remote', name: 'Box', url: BOX, status: 'running', serverVersion: '0.2.0',
        hasToken: true, channels: [{ id: 'b1', name: 'Box one' }, { id: 'b2', name: 'Box two' }],
        activeChannelId: 'b1', isWindowServer: false,
      },
      {
        id: 'nas', kind: 'remote', name: 'nas.example:5001', url: NAS, status: 'running', serverVersion: '0.1.0',
        hasToken: false, channels: [{ id: 'n1', name: 'Nas one' }], activeChannelId: 'n1', isWindowServer: false,
      },
    ]);
  });

  it('never returns a token, only whether one is held (MS-02)', async () => {
    const rig = twoRemotes();
    const entries = await rig.registry.list();
    const text = JSON.stringify(entries);
    expect(text).not.toContain('box-token');
    expect(text).not.toContain('sha256/box');
    for (const entry of entries) expect(entry).not.toHaveProperty('token');
  });

  it('reports unreachable, unauthorized and mismatched remotes with a sentence (MS-02)', async () => {
    const rig = twoRemotes();
    rig.servers.set(NAS, { health: 'down' });
    rig.servers.get(BOX)!.token = 'rotated-on-the-server';
    rig.servers.set('http://old.example:5001', { health: { bridge: BRIDGE_PROTOCOL - 1 } });
    rig.settings = { ...rig.settings, servers: [...rig.settings.servers, { id: 'old', url: 'http://old.example:5001' }] };

    const entries = await rig.registry.list();
    const byId = new Map(entries.map((entry) => [entry.id, entry]));

    expect(byId.get('nas')).toMatchObject({ status: 'unreachable', channels: null });
    expect(byId.get('box')).toMatchObject({ status: 'unauthorized', channels: null, error: `${BOX} rejected the access token.` });
    expect(byId.get('old')).toMatchObject({ status: 'mismatch' });
    expect(byId.get('old')?.error).toContain(`this app speaks ${BRIDGE_PROTOCOL}`);
  });

  it('a remote not polled yet is starting, not invented as running', () => {
    const rig = twoRemotes();
    expect(rig.registry.entries().find((entry) => entry.id === 'nas')?.status).toBe('starting');
  });

  it.each([
    [{ name: 'idle' }, 'stopped'],
    [{ name: 'provisioningUv', detail: '' }, 'starting'],
    [{ name: 'spawning', command: 'uv' }, 'starting'],
    [{ name: 'waitingHealth', command: 'uv', elapsedMs: 1 }, 'starting'],
    [{ name: 'ready', info: LOCAL_INFO }, 'running'],
    [{ name: 'crashed', code: 1, signal: null, tail: [] }, 'stopped'],
    [{ name: 'quitting', phase: 'graceful' }, 'stopped'],
    [{ name: 'stopped' }, 'stopped'],
    [{ name: 'released' }, 'stopped'],
    [{ name: 'failed', reason: 'uvMissing', message: 'no uv', command: null, tail: [] }, 'failed'],
  ] as [ServerState, string][])('maps the local state %j to %s', (state, status) => {
    expect(localStatus(state)).toBe(status);
  });

  it('a crashed local server reads as stopped with no channels; a failed one says why (MS-02)', async () => {
    const rig = twoRemotes();
    rig.local = { state: { name: 'crashed', code: 1, signal: null, tail: [] }, info: null };
    const [local] = await rig.registry.list();
    expect(local).toMatchObject({ id: 'local', status: 'stopped', channels: null, url: '' });
    rig.local = { state: { name: 'failed', reason: 'uvMissing', message: 'No uv.', command: null, tail: [] }, info: null };
    expect(rig.registry.entries()[0]).toMatchObject({ status: 'failed', error: 'No uv.' });
  });

  it('has no local entry where there is no local server (Windows)', async () => {
    const rig = twoRemotes();
    rig.local = null;
    rig.windowId = 'box';
    const entries = await rig.registry.list();
    expect(entries.map((entry) => entry.id)).toEqual(['box', 'nas']);
    expect(entries[0]?.isWindowServer).toBe(true);
  });

  it('pushes changed only when something changed (MS-02)', async () => {
    const rig = twoRemotes();
    await rig.registry.list();
    await rig.registry.list();
    expect(rig.changes).toHaveLength(1);
    rig.servers.get(NAS)!.channels = [{ id: 'n1', name: 'Nas one' }, { id: 'n2', name: 'Nas two' }];
    await rig.registry.list();
    expect(rig.changes).toHaveLength(2);
  });

  it('names a server by its saved name, else its host', () => {
    expect(serverName({ url: 'https://box.example:8443' })).toBe('box.example:8443');
    expect(serverName({ name: 'Box', url: 'https://box.example:8443' })).toBe('Box');
  });

  it('shapes the splash picker by id, without any credentials __F1015', () => {
    const rig = twoRemotes();
    expect(serversForView(rig.settings)).toEqual([
      { id: 'box', name: 'Box', url: BOX },
      { id: 'nas', url: NAS },
    ]);
  });
});

describe('catalog polling (MS-02)', () => {
  it('sends each server its own token as an explicit header, and none to the others (MS-02)', async () => {
    const rig = twoRemotes();

    await rig.registry.list();

    // /health is public: no token. The catalog gets exactly this server's.
    expect(rig.calls.filter((call) => call.url.startsWith(BOX))).toEqual([
      { url: `${BOX}/health`, method: 'GET' },
      { url: `${BOX}/api/channels`, method: 'GET', authorization: 'Bearer box-token' },
    ]);
    expect(rig.headersTo(NAS)).toEqual([undefined, undefined]);
    expect(rig.headersTo(LOCAL_URL)).toEqual([undefined]);
  });

  it('offers the env token only to the origin COLLIMATE_CONNECT_URL names (MS-02)', async () => {
    const rig = twoRemotes();
    rig.env['COLLIMATE_CONNECT_URL'] = NAS;
    rig.env['COLLIMATE_CONNECT_TOKEN'] = 'env-token';
    rig.servers.get(NAS)!.token = 'env-token';

    const entries = await rig.registry.list();

    expect(rig.headersTo(NAS)).toEqual([undefined, 'Bearer env-token']);
    expect(rig.headersTo(BOX)).not.toContain('Bearer env-token');
    expect(rig.headersTo(LOCAL_URL)).not.toContain('Bearer env-token');
    expect(entries.find((entry) => entry.id === 'nas')).toMatchObject({ status: 'running', hasToken: true });
    // Held in the environment, never written down.
    expect(JSON.stringify(rig.saved)).not.toContain('env-token');
  });

  it('polls every interval while the window has focus, and not while it does not (MS-02)', async () => {
    const rig = twoRemotes();
    rig.registry.start();
    const polls = () => rig.calls.filter((call) => call.url === `${NAS}/health`).length;

    // Once at start, so no remote reads 'starting' for a whole interval.
    await vi.advanceTimersByTimeAsync(0);
    expect(polls()).toBe(1);
    expect(rig.changes.at(-1)?.find((entry) => entry.id === 'nas')?.status).toBe('running');
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(polls()).toBe(2);
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    expect(polls()).toBe(3);

    rig.focused = false;
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(polls()).toBe(3);

    // Focus comes back: refreshed at once, then on the interval again.
    rig.focused = true;
    rig.registry.focused();
    await vi.advanceTimersByTimeAsync(0);
    expect(polls()).toBe(4);

    rig.registry.dispose();
    await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    expect(polls()).toBe(4);
  });

  it('shares one poll between concurrent lists', async () => {
    const rig = twoRemotes();
    await Promise.all([rig.registry.list(), rig.registry.list(), rig.registry.list()]);
    expect(rig.calls.filter((call) => call.url === `${NAS}/health`)).toHaveLength(1);
  });

  it('pins every saved https server when it starts (MS-02)', () => {
    const rig = twoRemotes();
    rig.registry.start();
    expect([...rig.pins.at(-1)!.entries()]).toEqual([
      ['box.example', { id: 'box', origin: BOX, fingerprint: 'sha256/box' }],
    ]);
    rig.registry.dispose();
  });
});

describe('adding, probing, updating and removing (MS-02)', () => {
  it('probes first, mints an id, stores the token and hands back an entry without it (MS-02)', async () => {
    const rig = twoRemotes();
    rig.servers.set('http://lab.example', { health: 'ok', token: 'lab-token', channels: [{ id: 'x', name: 'X' }] });

    // Typed the way people type it: no scheme.
    const entry = await rig.registry.add({ name: 'Lab', url: 'lab.example', token: 'lab-token' });

    expect(entry).toMatchObject({ id: 'minted-1', name: 'Lab', status: 'running', hasToken: true, channels: [{ id: 'x', name: 'X' }] });
    expect(JSON.stringify(entry)).not.toContain('lab-token');
    expect(rig.settings.servers.at(-1)).toEqual({ id: 'minted-1', name: 'Lab', url: 'http://lab.example', token: 'lab-token' });
  });

  it('refuses a server whose probe fails, saving nothing (MS-02)', async () => {
    const rig = twoRemotes();
    rig.servers.set('http://locked.example:5001', { health: 'ok', token: 'right' });

    await expect(rig.registry.add({ url: 'http://locked.example:5001' })).rejects.toThrow(
      'http://locked.example:5001 requires an access token.',
    );
    await expect(rig.registry.add({ url: 'http://locked.example:5001', token: 'wrong' })).rejects.toThrow(
      'rejected the access token',
    );
    await expect(rig.registry.add({ url: 'ftp://nope' })).rejects.toBeInstanceOf(RegistryError);
    expect(rig.saved).toEqual([]);
  });

  it('dedupes by URL: re-adding keeps the id and takes the new token after a native confirmation (MS-02)', async () => {
    const rig = twoRemotes();
    rig.servers.get(BOX)!.token = 'new-box-token';

    const entry = await rig.registry.add({ url: 'https://BOX.example:8443/', token: 'new-box-token' });

    expect(rig.confirms).toEqual(['Change the access token for Box?']);
    expect(entry.id).toBe('box');
    expect(rig.settings.servers.filter((server) => server.url === BOX)).toEqual([
      { id: 'box', name: 'Box', url: BOX, token: 'new-box-token', fingerprint: 'sha256/box' },
    ]);
    expect(rig.credentialChanges).toEqual(['box']);
  });

  it('a re-add the user declines changes no token and probes nothing (MS-02)', async () => {
    const rig = twoRemotes();
    rig.confirmAnswer = false;

    await expect(rig.registry.add({ url: BOX, token: 'page-chosen-token' })).rejects.toThrow('The token was not changed.');

    expect(rig.confirms).toEqual(['Change the access token for Box?']);
    expect(rig.saved).toEqual([]);
    expect(rig.credentialChanges).toEqual([]);
    expect(rig.calls.some((call) => call.authorization === 'Bearer page-chosen-token')).toBe(false);
  });

  it('re-adding with the saved token, or none, asks nothing and rebinds nothing (MS-02)', async () => {
    const rig = twoRemotes();

    await rig.registry.add({ url: BOX, token: 'box-token' });
    await rig.registry.add({ url: BOX, name: 'Box again' });

    expect(rig.confirms).toEqual([]);
    expect(rig.credentialChanges).toEqual([]);
    expect(rig.settings.servers.find((server) => server.id === 'box')).toMatchObject({ name: 'Box again', token: 'box-token' });
  });

  it('a probe checks without saving, and its accepted pin is saved with the server it then adds (MS-02)', async () => {
    const rig = twoRemotes();
    rig.servers.set('https://self-signed.example', { health: 'ok' });

    expect(await rig.registry.probe({ url: 'https://self-signed.example' })).toEqual({ ok: true, serverVersion: '0.1.0' });
    expect(rig.saved).toEqual([]);
    // The candidate's host was pinned for the probe, under a minted id…
    const candidate = rig.pins.at(-1)!.get('self-signed.example');
    expect(candidate).toEqual({ id: 'minted-1', origin: 'https://self-signed.example' });
    // …the user trusted its certificate during the probe…
    rig.registry.savePin('minted-1', 'sha256/self');
    expect(rig.saved).toEqual([]);
    // …and that pin lands with the server once it is added.
    const entry = await rig.registry.add({ url: 'https://self-signed.example' });
    expect(entry.id).toBe('minted-1');
    expect(rig.settings.servers.at(-1)).toEqual({ id: 'minted-1', url: 'https://self-signed.example', fingerprint: 'sha256/self' });
  });

  it('reports a failed probe as a sentence', async () => {
    const rig = twoRemotes();
    expect(await rig.registry.probe({ url: 'http://nobody.example:5001' })).toEqual({
      ok: false,
      message: "Couldn't reach http://nobody.example:5001: ECONNREFUSED. Is the server running?",
    });
  });

  it('saves a pin accepted for a saved server under its id (MS-02)', () => {
    const rig = twoRemotes();
    rig.registry.savePin('nas', 'sha256/nas');
    expect(rig.settings.servers.find((server) => server.id === 'nas')?.fingerprint).toBe('sha256/nas');
  });

  it('renames without asking; a token change asks natively and is probed first (MS-02)', async () => {
    const rig = twoRemotes();

    await rig.registry.update({ id: 'nas', name: 'The NAS' });
    expect(rig.confirms).toEqual([]);
    expect(rig.settings.servers.find((server) => server.id === 'nas')?.name).toBe('The NAS');

    rig.confirmAnswer = false;
    await expect(rig.registry.update({ id: 'box', token: 'other' })).rejects.toThrow('The token was not changed.');
    expect(rig.confirms).toEqual(['Change the access token for Box?']);
    expect(rig.settings.servers.find((server) => server.id === 'box')?.token).toBe('box-token');

    rig.confirmAnswer = true;
    await expect(rig.registry.update({ id: 'box', token: 'wrong' })).rejects.toThrow('rejected the access token');
    rig.servers.get(BOX)!.token = 'rotated';
    const entry = await rig.registry.update({ id: 'box', token: 'rotated' });
    expect(entry).toMatchObject({ id: 'box', status: 'running', hasToken: true });
    expect(rig.settings.servers.find((server) => server.id === 'box')?.token).toBe('rotated');
    expect(rig.credentialChanges).toEqual(['box']);
  });

  it('removes only after a native confirmation, and never the window server (MS-02)', async () => {
    const rig = twoRemotes({ activeServer: 'nas' });

    rig.confirmAnswer = false;
    await rig.registry.remove('nas');
    expect(rig.settings.servers.map((server) => server.id)).toEqual(['box', 'nas']);

    rig.confirmAnswer = true;
    await rig.registry.remove('nas');
    expect(rig.confirms).toEqual(['Remove nas.example:5001 from Collimate?', 'Remove nas.example:5001 from Collimate?']);
    expect(rig.settings.servers.map((server) => server.id)).toEqual(['box']);
    // The launch target it was falls back to local.
    expect(rig.settings.activeServer).toBe('local');

    rig.windowId = 'box';
    await expect(rig.registry.remove('box')).rejects.toThrow('This window shows Box');
    await expect(rig.registry.remove('nope')).rejects.toThrow('not saved');
  });

  it('carries serverSpec, pins, the scale and the local port across every write (MS-02)', async () => {
    const rig = twoRemotes({ keepLocalRunning: true });
    rig.servers.set('http://lab.example', { health: 'ok' });

    await rig.registry.add({ url: 'http://lab.example' });
    await rig.registry.update({ id: 'nas', name: 'NAS' });
    rig.registry.setPrefs({ keepLocalRunning: false });
    rig.registry.savePin('nas', 'sha256/nas');
    await rig.registry.remove('minted-1');

    expect(rig.saved.length).toBeGreaterThanOrEqual(5);
    for (const written of rig.saved) {
      expect(written.serverSpec).toBe('local:/home/dev/collimate');
      expect(written.interfaceScale).toBe(1.1);
      expect(written.localPort).toBe(51000);
      expect(written.servers.find((server) => server.id === 'box')).toEqual({
        id: 'box', name: 'Box', url: BOX, token: 'box-token', fingerprint: 'sha256/box',
      });
    }
    expect(rig.settings.keepLocalRunning).toBe(false);
  });
});

describe('the window and the local server (MS-01)', () => {
  it('picking a channel on another server selects it there with that token, then recreates the window — local keeps running (MS-01)', async () => {
    const rig = twoRemotes();

    await rig.registry.openChannel('box', 'b2');

    const select = rig.calls.find((call) => call.method === 'POST' && call.url.includes('/select'));
    expect(select).toEqual({ url: `${BOX}/api/channels/b2/select`, method: 'POST', authorization: 'Bearer box-token' });
    expect(rig.servers.get(BOX)!.active).toBe('b2');
    expect(rig.opened).toEqual([
      {
        info: { id: 'box', kind: 'remote', name: 'Box', url: BOX, port: 8443, dataDir: null, serverVersion: '0.2.0' },
        credential: { origin: BOX, token: 'box-token' },
      },
    ]);
    expect(rig.stopLocal).not.toHaveBeenCalled();
    expect(rig.local?.state.name).toBe('ready');
  });

  it('a refused select leaves the window where it is (MS-01)', async () => {
    const rig = twoRemotes();
    rig.servers.get(NAS)!.selectStatus = 404;

    await expect(rig.registry.openChannel('nas', 'ghost')).rejects.toThrow('refused that channel (HTTP 404)');
    expect(rig.opened).toEqual([]);
  });

  it('an unreachable or unauthorized server is refused before the window is touched (MS-01)', async () => {
    const rig = twoRemotes();
    rig.servers.set(NAS, { health: 'down' });
    await expect(rig.registry.openServer('nas')).rejects.toThrow("Couldn't reach");
    rig.servers.get(BOX)!.token = 'rotated';
    await expect(rig.registry.openChannel('box', 'b1')).rejects.toThrow('rejected the access token');
    expect(rig.opened).toEqual([]);
    expect(rig.calls.some((call) => call.method === 'POST' && call.url.includes('/select'))).toBe(false);
  });

  it('opens the local server with no credential, starting it when it is down (MS-01)', async () => {
    const rig = twoRemotes();
    rig.windowId = 'box';
    rig.local = { state: { name: 'stopped' }, info: null };

    await rig.registry.openChannel('local', 'l1');

    expect(rig.startLocal).toHaveBeenCalledOnce();
    expect(rig.opened).toEqual([{ info: LOCAL_INFO, credential: null }]);
    expect(rig.calls.find((call) => call.method === 'POST')).toEqual({ url: `${LOCAL_URL}/api/channels/l1/select`, method: 'POST' });
  });

  it('stops the local server only after a native confirmation (MS-02)', async () => {
    const rig = twoRemotes();
    rig.confirmAnswer = false;
    await rig.registry.stopLocal();
    expect(rig.stopLocal).not.toHaveBeenCalled();

    rig.confirmAnswer = true;
    await rig.registry.stopLocal();
    expect(rig.stopLocal).toHaveBeenCalledOnce();
    expect(rig.lifecycle).toEqual(['open box', 'stop local']);
    expect(rig.openOptions).toEqual([{ newChannel: true }]);
    expect(rig.stopOptions).toEqual([{ reportFailureToNewWindow: true }]);
    expect(rig.confirms).toEqual(['Stop the local Collimate server?', 'Stop the local Collimate server?']);

    // Already stopped: nothing to confirm.
    await rig.registry.stopLocal();
    expect(rig.confirms).toHaveLength(2);
  });

  it('refuses Stop on the local window without an available remote, keeping local usable', async () => {
    const rig = new Rig();
    await expect(rig.registry.stopLocal()).rejects.toThrow('Add a remote server');
    expect(rig.confirms).toEqual([]);
    expect(rig.stopLocal).not.toHaveBeenCalled();
    expect(rig.windowId).toBe('local');

    const withRemotes = twoRemotes();
    withRemotes.servers.set(BOX, { health: 'down' });
    withRemotes.servers.set(NAS, { health: 'down' });
    await expect(withRemotes.registry.stopLocal()).rejects.toThrow('No saved remote server is available');
    expect(withRemotes.opened).toEqual([]);
    expect(withRemotes.stopLocal).not.toHaveBeenCalled();
    expect(withRemotes.windowId).toBe('local');
  });

  it('uses the first reachable saved remote, opens New channel, then stops local', async () => {
    const rig = twoRemotes();
    rig.servers.set(BOX, { health: 'down' });
    await rig.registry.stopLocal();
    expect(rig.lifecycle).toEqual(['open nas', 'stop local']);
    expect(rig.openOptions).toEqual([{ newChannel: true }]);
    expect(rig.windowId).toBe('nas');
    expect(rig.servers.get(NAS)?.active).toBe('n1');
  });

  it('keeps local active when the replacement window fails to open', async () => {
    const rig = twoRemotes();
    rig.openWindowError = new Error('The remote window did not paint.');
    await expect(rig.registry.stopLocal()).rejects.toThrow('did not paint');
    expect(rig.lifecycle).toEqual(['open box']);
    expect(rig.windowId).toBe('local');
    expect(rig.stopLocal).not.toHaveBeenCalled();
    expect(rig.local?.state.name).toBe('ready');
  });

  it('does not stop local if a completed handoff no longer owns the remote window', async () => {
    const rig = twoRemotes();
    rig.openWindowDoesNotSwitch = true;
    await expect(rig.registry.stopLocal()).rejects.toThrow('remote window is no longer available');
    expect(rig.lifecycle).toEqual(['open box']);
    expect(rig.stopLocal).not.toHaveBeenCalled();
    expect(rig.local?.state.name).toBe('ready');
  });

  it('keeps a remote window on its current channel while stopping local', async () => {
    const rig = twoRemotes();
    rig.windowId = 'box';
    await rig.registry.stopLocal();
    expect(rig.lifecycle).toEqual(['stop local']);
    expect(rig.opened).toEqual([]);
    expect(rig.stopOptions).toEqual([undefined]);
    expect(rig.servers.get(BOX)?.active).toBe('b1');

    const unavailable = twoRemotes();
    unavailable.windowId = 'box';
    unavailable.servers.set(BOX, { health: 'down' });
    await expect(unavailable.registry.stopLocal()).rejects.toThrow("Couldn't reach");
    expect(unavailable.stopLocal).not.toHaveBeenCalled();
  });

  it('refuses local-server ops where there is none (Windows)', async () => {
    const rig = twoRemotes();
    rig.local = null;
    await expect(rig.registry.startLocal()).rejects.toThrow('no local server');
    await expect(rig.registry.stopLocal()).rejects.toThrow('no local server');
    await expect(rig.registry.openServer('local')).rejects.toThrow('no local server');
  });

  it('reads and writes the keep-alive preference (MS-04)', () => {
    const rig = twoRemotes();
    expect(rig.registry.getPrefs()).toEqual({ keepLocalRunning: false });
    expect(rig.registry.setPrefs({ keepLocalRunning: true })).toEqual({ keepLocalRunning: true });
    expect(rig.settings.keepLocalRunning).toBe(true);
    expect(rig.registry.setPrefs({})).toEqual({ keepLocalRunning: true });
  });

  it('the window binding for a saved server is its origin and its own token only (MS-02)', () => {
    const rig = twoRemotes();
    rig.env['COLLIMATE_CONNECT_URL'] = BOX;
    rig.env['COLLIMATE_CONNECT_TOKEN'] = 'env-token';
    expect(rig.registry.targetCredential('box')).toEqual({ origin: BOX, token: 'box-token' });
    expect(rig.registry.targetCredential('nas')).toEqual({ origin: NAS });
    expect(rig.registry.targetCredential('local')).toBeNull();
  });
});

describe('IPC payloads (MS-02)', () => {
  it('accepts well-formed payloads and drops empty optional fields', () => {
    expect(parseDraft({ url: 'http://box', name: '', token: 't', extra: 1 })).toEqual({ url: 'http://box', token: 't' });
    expect(parseUpdate({ id: 'a', name: '' })).toEqual({ id: 'a', name: '' });
    expect(parseUpdate({ id: 'a', token: 'new' })).toEqual({ id: 'a', token: 'new' });
    expect(parseId({ id: 'a' })).toBe('a');
    expect(parseOpenChannel({ id: 'a', channelId: 'c' })).toEqual({ id: 'a', channelId: 'c' });
    expect(parseProbe({ url: 'http://box' })).toEqual({ url: 'http://box' });
    expect(parsePrefs({ keepLocalRunning: true })).toEqual({ keepLocalRunning: true });
    expect(parsePrefs({})).toEqual({});
  });

  it.each([
    [() => parseDraft(null)],
    [() => parseDraft({ url: 42 })],
    [() => parseDraft({ url: 'http://box', token: 7 })],
    [() => parseUpdate({ name: 'x' })],
    [() => parseId('a')],
    [() => parseOpenChannel({ id: 'a' })],
    [() => parseProbe({})],
    [() => parsePrefs({ keepLocalRunning: 'yes' })],
  ])('refuses junk %#', (parse) => {
    expect(parse).toThrow(RegistryError);
  });
});
