/**
 * The servers registry: the local server plus every saved remote, with
 * their statuses and channel catalogs, and the operations the page's
 * Settings → Servers tab and channel picker drive.
 *
 * One window shows one server. Picking a channel on another server makes
 * the shell select it there (POST /api/channels/{id}/select, with that
 * server's token) and recreate the window on that server's origin; the
 * local server is never stopped by it. The frontend therefore keeps its
 * single same-origin apiBase, and everything cross-server lives here.
 *
 * Free of Electron imports, like ServerController: the window, the local
 * controller, native dialogs, settings and the network arrive through
 * `deps`, so the security properties are assertable — tokens are never
 * returned (`hasToken` only), every poll carries its server's token as an
 * explicit header on that request alone, and the env token only ever goes
 * to the origin COLLIMATE_CONNECT_URL names.
 */

import {
  LOCAL_SERVER_ID,
  type ServerDraft,
  type ServerEntry,
  type ServerInfo,
  type ServerProbeResult,
  type ServerStatus,
  type ShellPrefs,
  type ServerChannel,
} from '../shared/bridge.js';
import {
  addServer,
  credentialFor,
  fetchCatalog,
  findServer,
  normalizeServerUrl,
  probeServer,
  readHealth,
  removeServer,
  resolveToken,
  updateServer,
  type DesktopSettings,
  type SavedServer,
} from './connect.js';
import { Emitter, LOCAL_SERVER_NAME, type FetchLike, type ServerState } from './server.js';
import { pinsFor, type PinMap, type ServerCredential } from './serverAccess.js';
import { errorMessage } from './errors.js';

/** How often servers are polled while the window has focus. */
export const POLL_INTERVAL_MS = 8_000;
/** Ceiling on one status or catalog request during a poll. */
export const POLL_TIMEOUT_MS = 4_000;

/** The local server as the controller sees it. */
export interface LocalServerView {
  state: ServerState;
  info: ServerInfo | null;
}

/** What the window is recreated on. */
export interface WindowTarget {
  info: ServerInfo;
  /** The window binding: the server's origin and token (null for the local server). */
  credential: ServerCredential | null;
}

/** The Electron side of the registry, implemented by index.ts. */
export interface RegistryHost {
  /** The local controller's view; null where there is no local server (Windows). */
  local(): LocalServerView | null;
  /** The server the window shows, or null with no window. */
  windowServerId(): string | null;
  /** Open `target` (bounds kept; editors already flushed by the page). A
   * local-server handoff resolves when the remote New channel page is ready. */
  openWindow(target: WindowTarget, options?: { newChannel: boolean }): Promise<void>;
  /** Start (or adopt) the local server; resolves once it is ready. */
  startLocal(): Promise<ServerInfo>;
  /** Stop the local server (already confirmed). */
  stopLocal(options?: { reportFailureToNewWindow: boolean }): Promise<void>;
  /** A saved server's token changed: rebind the window if it shows that server. */
  credentialChanged(id: string): void;
  /** A native confirmation in main; resolves true to go ahead. */
  confirm(request: { message: string; detail?: string; confirmLabel: string }): Promise<boolean>;
}

export interface ServerRegistryDeps {
  host: RegistryHost;
  /** Electron's net stack over the default session, so certificate pins apply. */
  fetchImpl: FetchLike;
  /** The current settings (index.ts owns the one copy). */
  settings(): DesktopSettings;
  /** Persist new settings; every other key is carried by the caller's object. */
  saveSettings(next: DesktopSettings): void;
  env: NodeJS.ProcessEnv;
  mintId(): string;
  /** Keep the session's certificate pins in step with the saved servers. */
  setPins(pins: PinMap): void;
  /** Whether the app window has focus (polling only runs while it does). */
  isFocused(): boolean;
  setTimeoutImpl(fn: () => void, ms: number): unknown;
  clearTimeoutImpl(handle: unknown): void;
  log?(line: string): void;
}

type RegistryEvents = { changed: [ServerEntry[]] };

/** The last poll of one server. */
interface Observation {
  status: ServerStatus;
  serverVersion: string | null;
  channels: ServerChannel[] | null;
  activeChannelId: string | null;
  error?: string;
}

/** A refusal the page shows as a sentence. */
export class RegistryError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RegistryError';
  }
}

/** The page-facing status of the local server for a controller state. */
export function localStatus(state: ServerState): ServerStatus {
  switch (state.name) {
    case 'provisioningUv':
    case 'spawning':
    case 'waitingHealth':
      return 'starting';
    case 'ready':
      return 'running';
    case 'failed':
      return 'failed';
    case 'idle':
    case 'crashed':
    case 'quitting':
    case 'stopped':
    case 'released':
      return 'stopped';
  }
}

/** A saved server's display name: its name, else its URL's host. */
export function serverName(server: Pick<SavedServer, 'name' | 'url'>): string {
  if (server.name) return server.name;
  try {
    return new URL(server.url).host;
  } catch {
    return server.url;
  }
}

/** The page's view of the saved remotes, for the splash picker: no secrets. */
export function serversForView(settings: DesktopSettings): { id: string; name?: string; url: string }[] {
  return settings.servers.map((server) => ({
    id: server.id,
    url: server.url,
    ...(server.name ? { name: server.name } : {}),
  }));
}

export class ServerRegistry {
  readonly events = new Emitter<RegistryEvents>();
  private readonly observations = new Map<string, Observation>();
  /** Ids minted for a candidate URL while it is probed, reused when it is added. */
  private readonly candidates = new Map<string, string>();
  /** Pins accepted for a candidate before it was saved, by id. */
  private readonly pendingPins = new Map<string, string>();
  private lastLocalUrl = '';
  private lastEmitted = '';
  private inFlight: Promise<void> | null = null;
  private timer: unknown = null;
  private running = false;

  constructor(private readonly deps: ServerRegistryDeps) {}

  // --- reading ---------------------------------------------------------------

  /** Every server, as last observed. Never carries a token. */
  entries(): ServerEntry[] {
    const windowId = this.deps.host.windowServerId();
    const settings = this.deps.settings();
    const out: ServerEntry[] = [];
    const local = this.deps.host.local();
    if (local) {
      if (local.info) this.lastLocalUrl = local.info.url;
      const status = localStatus(local.state);
      const seen = status === 'running' ? this.observations.get(LOCAL_SERVER_ID) : undefined;
      out.push({
        id: LOCAL_SERVER_ID,
        kind: 'local',
        name: LOCAL_SERVER_NAME,
        url: local.info?.url ?? this.lastLocalUrl,
        status,
        serverVersion: local.info?.serverVersion ?? null,
        hasToken: false,
        channels: seen?.channels ?? null,
        activeChannelId: seen?.activeChannelId ?? null,
        isWindowServer: windowId === LOCAL_SERVER_ID,
        ...(local.state.name === 'failed' ? { error: local.state.message } : {}),
      });
    }
    for (const server of settings.servers) {
      const seen = this.observations.get(server.id);
      out.push({
        id: server.id,
        kind: 'remote',
        name: serverName(server),
        url: server.url,
        status: seen?.status ?? 'starting',
        serverVersion: seen?.serverVersion ?? null,
        hasToken: Boolean(resolveToken(settings, this.deps.env, server.id)),
        channels: seen?.channels ?? null,
        activeChannelId: seen?.activeChannelId ?? null,
        isWindowServer: windowId === server.id,
        ...(seen?.error ? { error: seen.error } : {}),
      });
    }
    return out;
  }

  /** Poll every server now, then answer with fresh entries. */
  async list(): Promise<ServerEntry[]> {
    await this.refresh();
    return this.entries();
  }

  /**
   * Poll every server once: the local one's catalog when it runs, each
   * remote's /health then its catalog, each request with that server's
   * own token as an explicit header. Concurrent calls share one poll.
   */
  refresh(): Promise<void> {
    if (this.inFlight) return this.inFlight;
    const poll = this.pollAll().finally(() => {
      this.inFlight = null;
    });
    this.inFlight = poll;
    return poll;
  }

  /** Push `changed` when the entries differ from what was last pushed. */
  notify(): void {
    const entries = this.entries();
    const snapshot = JSON.stringify(entries);
    if (snapshot === this.lastEmitted) return;
    this.lastEmitted = snapshot;
    this.events.emit('changed', entries);
  }

  // --- polling -----------------------------------------------------------------

  /**
   * Poll once now, so no remote stays unchecked ('starting') for a whole
   * interval, then every POLL_INTERVAL_MS while the window has focus.
   */
  start(): void {
    if (this.running) return;
    this.running = true;
    this.syncPins();
    void this.refresh();
    this.schedule();
  }

  dispose(): void {
    this.running = false;
    if (this.timer !== null) this.deps.clearTimeoutImpl(this.timer);
    this.timer = null;
  }

  /** The window gained focus: a stale list is refreshed at once. */
  focused(): void {
    if (this.running) void this.refresh();
  }

  private schedule(): void {
    if (!this.running) return;
    this.timer = this.deps.setTimeoutImpl(() => {
      this.timer = null;
      const poll = this.deps.isFocused() ? this.refresh() : Promise.resolve();
      void poll.finally(() => this.schedule());
    }, POLL_INTERVAL_MS);
  }

  private async pollAll(): Promise<void> {
    const settings = this.deps.settings();
    const polls: Promise<void>[] = [];
    const local = this.deps.host.local();
    if (local?.info && localStatus(local.state) === 'running') {
      const url = local.info.url;
      polls.push(
        fetchCatalog(url, undefined, this.deps.fetchImpl, { timeoutMs: POLL_TIMEOUT_MS }).then((catalog) => {
          this.observations.set(
            LOCAL_SERVER_ID,
            catalog.ok
              ? { status: 'running', serverVersion: null, channels: catalog.channels, activeChannelId: catalog.activeChannelId }
              : { status: 'running', serverVersion: null, channels: null, activeChannelId: null, error: catalog.message },
          );
        }),
      );
    } else {
      this.observations.delete(LOCAL_SERVER_ID);
    }
    for (const server of settings.servers) polls.push(this.pollRemote(settings, server));
    await Promise.all(polls);
    // Forget observations of servers that are no longer saved.
    for (const id of [...this.observations.keys()]) {
      if (id !== LOCAL_SERVER_ID && !findServer(this.deps.settings(), id)) this.observations.delete(id);
    }
    this.notify();
  }

  private async pollRemote(settings: DesktopSettings, server: SavedServer): Promise<void> {
    const health = await readHealth(server.url, this.deps.fetchImpl, { timeoutMs: POLL_TIMEOUT_MS });
    if (!health.ok) {
      this.observations.set(server.id, {
        status: health.reason === 'mismatch' ? 'mismatch' : 'unreachable',
        serverVersion: null,
        channels: null,
        activeChannelId: null,
        error: health.message,
      });
      return;
    }
    const token = resolveToken(settings, this.deps.env, server.id);
    const catalog = await fetchCatalog(server.url, token, this.deps.fetchImpl, { timeoutMs: POLL_TIMEOUT_MS });
    this.observations.set(
      server.id,
      catalog.ok
        ? {
            status: 'running',
            serverVersion: health.version,
            channels: catalog.channels,
            activeChannelId: catalog.activeChannelId,
          }
        : {
            status: catalog.reason === 'unauthorized' ? 'unauthorized' : 'unreachable',
            serverVersion: health.version,
            channels: null,
            activeChannelId: null,
            error: catalog.message,
          },
    );
  }

  // --- saved remotes -----------------------------------------------------------

  /** Check a candidate without saving it (the Servers tab's Test). */
  async probe(candidate: { url: string; token?: string }): Promise<ServerProbeResult> {
    const url = normalizeServerUrl(candidate.url);
    if (url) this.watchCandidate(url);
    const result = await probeServer(candidate.url, this.deps.fetchImpl, candidate.token ? { token: candidate.token } : {});
    return result.ok
      ? { ok: true, serverVersion: result.serverVersion ?? 'unknown' }
      : { ok: false, message: result.message };
  }

  /**
   * Probe, then save a remote: a new one gets a minted id, a URL already
   * saved keeps its id and takes the new name and token — a token that
   * replaces the saved one only after the same native confirmation as
   * update(). The token is stored in settings.json and never handed back.
   */
  async add(draft: ServerDraft): Promise<ServerEntry> {
    const url = normalizeServerUrl(draft.url);
    if (!url) throw new RegistryError(`"${draft.url.trim()}" isn't a URL. Try something like http://127.0.0.1:5001.`);
    const candidateId = this.watchCandidate(url);
    const settings = this.deps.settings();
    const existing = settings.servers.find((server) => server.url === url);
    if (existing && draft.token && draft.token !== existing.token) {
      if (!(await this.confirmTokenChange(existing))) throw new RegistryError('The token was not changed.');
    }
    const token = draft.token || (existing ? resolveToken(settings, this.deps.env, existing.id) : undefined);
    const probe = await probeServer(url, this.deps.fetchImpl, token ? { token } : {});
    if (!probe.ok) throw new RegistryError(probe.message);
    const fingerprint = this.pendingPins.get(candidateId);
    const added = addServer(
      this.deps.settings(),
      {
        url,
        ...(draft.name ? { name: draft.name } : {}),
        ...(draft.token ? { token: draft.token } : {}),
        ...(fingerprint ? { fingerprint } : {}),
      },
      () => candidateId,
    );
    if (!added.ok) throw new RegistryError(added.message);
    this.candidates.delete(url);
    this.pendingPins.delete(candidateId);
    this.deps.saveSettings(added.settings);
    this.syncPins();
    this.deps.log?.(`saved server ${added.server.url} (${added.server.id})`);
    await this.pollRemote(this.deps.settings(), added.server);
    this.notify();
    if (existing && draft.token && draft.token !== existing.token) this.deps.host.credentialChanged(existing.id);
    return this.entryFor(added.server.id);
  }

  /** Rename a remote or replace its token; a token change is confirmed natively. */
  async update(patch: { id: string; name?: string; token?: string }): Promise<ServerEntry> {
    const server = findServer(this.deps.settings(), patch.id);
    if (!server) throw new RegistryError('That server is not saved.');
    if (patch.token !== undefined) {
      if (!(await this.confirmTokenChange(server))) throw new RegistryError('The token was not changed.');
      if (patch.token) {
        const probe = await probeServer(server.url, this.deps.fetchImpl, { token: patch.token });
        if (!probe.ok) throw new RegistryError(probe.message);
      }
    }
    this.deps.saveSettings(
      updateServer(this.deps.settings(), patch.id, {
        ...(patch.name !== undefined ? { name: patch.name } : {}),
        ...(patch.token !== undefined ? { token: patch.token } : {}),
      }),
    );
    if (patch.token !== undefined) this.deps.host.credentialChanged(patch.id);
    const saved = findServer(this.deps.settings(), patch.id);
    if (saved) await this.pollRemote(this.deps.settings(), saved);
    this.notify();
    return this.entryFor(patch.id);
  }

  /** Forget a saved remote, after a native confirmation. Not the window's own server. */
  async remove(id: string): Promise<void> {
    const server = findServer(this.deps.settings(), id);
    if (!server) throw new RegistryError('That server is not saved.');
    if (this.deps.host.windowServerId() === id) {
      throw new RegistryError(`This window shows ${serverName(server)}. Open another server before removing it.`);
    }
    const confirmed = await this.deps.host.confirm({
      message: `Remove ${serverName(server)} from Collimate?`,
      detail: `${server.url}\n\nIts saved token and certificate pin are forgotten. The server itself is not touched.`,
      confirmLabel: 'Remove Server',
    });
    if (!confirmed) return;
    this.deps.saveSettings(removeServer(this.deps.settings(), id));
    this.observations.delete(id);
    this.syncPins();
    this.notify();
  }

  // --- the window and the local server ----------------------------------------

  /** Recreate the window on server `id`. */
  async openServer(id: string): Promise<void> {
    const target = await this.targetFor(id);
    await this.deps.host.openWindow(target);
  }

  /**
   * Select `channelId` on server `id` with that server's token, then
   * recreate the window there. The local server keeps running whatever
   * the window shows.
   */
  async openChannel(id: string, channelId: string): Promise<void> {
    if (!channelId) throw new RegistryError('No channel was named.');
    const target = await this.targetFor(id);
    const token = target.credential?.token;
    try {
      const response = await this.deps.fetchImpl(
        `${target.info.url}/api/channels/${encodeURIComponent(channelId)}/select`,
        { method: 'POST', ...(token ? { headers: { authorization: `Bearer ${token}` } } : {}) },
      );
      if (!response.ok) throw new RegistryError(`${target.info.name} refused that channel (HTTP ${response.status}).`);
    } catch (error) {
      if (error instanceof RegistryError) throw error;
      throw new RegistryError(`Couldn't reach ${target.info.name}: ${errorMessage(error)}`);
    }
    await this.deps.host.openWindow(target);
  }

  async startLocal(): Promise<void> {
    if (!this.deps.host.local()) throw new RegistryError('This platform has no local server.');
    await this.deps.host.startLocal();
    this.notify();
  }

  /** Stop local after handing its window to the first reachable saved remote. */
  async stopLocal(): Promise<void> {
    const local = this.deps.host.local();
    if (!local) throw new RegistryError('This platform has no local server.');
    if (localStatus(local.state) === 'stopped') return;
    const windowId = this.deps.host.windowServerId();
    if (!windowId) throw new RegistryError('No app window is available.');
    if (windowId === LOCAL_SERVER_ID && this.deps.settings().servers.length === 0) {
      throw new RegistryError('Add a remote server before stopping the server this window uses.');
    }
    const confirmed = await this.deps.host.confirm({
      message: 'Stop the local Collimate server?',
      detail: 'Running generators and sandboxes on it stop too.',
      confirmLabel: 'Stop Server',
    });
    if (!confirmed) return;
    if (windowId === LOCAL_SERVER_ID) {
      let target: WindowTarget | null = null;
      for (const saved of this.deps.settings().servers) {
        try {
          target = await this.readyRemote(saved.id);
          break;
        } catch {
          // Try the next saved server; every target is probed before handoff.
        }
      }
      if (!target) throw new RegistryError('No saved remote server is available. The local server is still running.');
      if (this.deps.host.windowServerId() !== windowId) throw new RegistryError('The window changed servers. Try Stop again.');
      await this.deps.host.openWindow(target, { newChannel: true });
      if (this.deps.host.windowServerId() !== target.info.id) {
        throw new RegistryError('The remote window is no longer available. The local server is still running.');
      }
    } else {
      // A window already on a remote keeps its channel and does not navigate.
      await this.readyRemote(windowId);
      if (this.deps.host.windowServerId() !== windowId) throw new RegistryError('The window changed servers. Try Stop again.');
    }
    await this.deps.host.stopLocal(windowId === LOCAL_SERVER_ID ? { reportFailureToNewWindow: true } : undefined);
    this.notify();
  }

  getPrefs(): ShellPrefs {
    return { keepLocalRunning: this.deps.settings().keepLocalRunning };
  }

  setPrefs(patch: Partial<ShellPrefs>): ShellPrefs {
    if (typeof patch.keepLocalRunning === 'boolean') {
      this.deps.saveSettings({ ...this.deps.settings(), keepLocalRunning: patch.keepLocalRunning });
    }
    return this.getPrefs();
  }

  /**
   * A certificate pin the user accepted (ServerAccess.savePin): saved on
   * the server's entry, or held for a candidate until it is added.
   */
  savePin(id: string, fingerprint: string): void {
    if (findServer(this.deps.settings(), id)) {
      this.deps.saveSettings(updateServer(this.deps.settings(), id, { fingerprint }));
      return;
    }
    this.pendingPins.set(id, fingerprint);
  }

  /** The window binding for server `id` as saved now: its origin and token (null for local). */
  targetCredential(id: string): ServerCredential | null {
    if (id === LOCAL_SERVER_ID) return null;
    const settings = this.deps.settings();
    return credentialFor(settings, id, resolveToken(settings, this.deps.env, id));
  }

  /** What the window binds to for server `id`, checked before anything is torn down. */
  async targetFor(id: string): Promise<WindowTarget> {
    if (id === LOCAL_SERVER_ID) {
      const local = this.deps.host.local();
      if (!local) throw new RegistryError('This platform has no local server.');
      const info = local.info && localStatus(local.state) === 'running' ? local.info : await this.deps.host.startLocal();
      return { info, credential: null };
    }
    const settings = this.deps.settings();
    const server = findServer(settings, id);
    if (!server) throw new RegistryError('That server is not saved.');
    const token = resolveToken(settings, this.deps.env, id);
    const probe = await probeServer(server.url, this.deps.fetchImpl, token ? { token } : {});
    if (!probe.ok) throw new RegistryError(probe.message);
    return {
      info: {
        id,
        kind: 'remote',
        name: serverName(server),
        url: probe.url,
        port: probe.port,
        dataDir: null,
        serverVersion: probe.serverVersion,
      },
      credential: this.targetCredential(id),
    };
  }

  /** The new page needs a working channel API as well as a healthy origin. */
  private async readyRemote(id: string): Promise<WindowTarget> {
    const target = await this.targetFor(id);
    if (target.info.kind !== 'remote') throw new RegistryError('A remote server is required.');
    const catalog = await fetchCatalog(target.info.url, target.credential?.token, this.deps.fetchImpl,
      { timeoutMs: POLL_TIMEOUT_MS });
    if (!catalog.ok) throw new RegistryError(catalog.message);
    return target;
  }

  // --- plumbing ------------------------------------------------------------------

  /** The native confirmation every change of a saved server's token waits for. */
  private confirmTokenChange(server: SavedServer): Promise<boolean> {
    return this.deps.host.confirm({
      message: `Change the access token for ${serverName(server)}?`,
      detail: `${server.url}\n\nThe new token is stored by Collimate and sent only to this server.`,
      confirmLabel: 'Change Token',
    });
  }

  private entryFor(id: string): ServerEntry {
    const entry = this.entries().find((candidate) => candidate.id === id);
    if (!entry) throw new RegistryError('That server is not saved.');
    return entry;
  }

  /**
   * Cover a candidate's host with the pins while it is probed, under an id
   * minted now and reused if the candidate is then added — so a first-use
   * certificate accepted at Test is the one saved with the server.
   */
  private watchCandidate(url: string): string {
    const saved = this.deps.settings().servers.find((server) => server.url === url);
    if (saved) return saved.id;
    let id = this.candidates.get(url);
    if (!id) {
      id = this.deps.mintId();
      this.candidates.set(url, id);
    }
    this.syncPins();
    return id;
  }

  /** Pins for every saved https server plus the candidates being probed. */
  private syncPins(): void {
    const saved = this.deps.settings().servers;
    const candidates = [...this.candidates.entries()].map(([url, id]) => {
      const fingerprint = this.pendingPins.get(id);
      return { id, url, ...(fingerprint ? { fingerprint } : {}) };
    });
    this.deps.setPins(pinsFor([...saved, ...candidates]));
  }
}

// ---------------------------------------------------------------------------
// IPC payloads: everything the page sends is untrusted, and these are the
// only gates. Junk becomes a RegistryError, never a half-formed call.
// ---------------------------------------------------------------------------

function payloadRecord(payload: unknown): Record<string, unknown> {
  if (typeof payload !== 'object' || payload === null) throw new RegistryError('Malformed request.');
  return payload as Record<string, unknown>;
}

function requiredString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== 'string' || !value) throw new RegistryError(`Malformed request: ${key} is missing.`);
  return value;
}

function optionalString(record: Record<string, unknown>, key: string): string | undefined {
  const value = record[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'string') throw new RegistryError(`Malformed request: ${key} is not text.`);
  return value;
}

export function parseDraft(payload: unknown): ServerDraft {
  const record = payloadRecord(payload);
  const name = optionalString(record, 'name');
  const token = optionalString(record, 'token');
  return { url: requiredString(record, 'url'), ...(name ? { name } : {}), ...(token ? { token } : {}) };
}

export function parseUpdate(payload: unknown): { id: string; name?: string; token?: string } {
  const record = payloadRecord(payload);
  const name = optionalString(record, 'name');
  const token = optionalString(record, 'token');
  return {
    id: requiredString(record, 'id'),
    ...(name !== undefined ? { name } : {}),
    ...(token !== undefined ? { token } : {}),
  };
}

export function parseId(payload: unknown): string {
  return requiredString(payloadRecord(payload), 'id');
}

export function parseOpenChannel(payload: unknown): { id: string; channelId: string } {
  const record = payloadRecord(payload);
  return { id: requiredString(record, 'id'), channelId: requiredString(record, 'channelId') };
}

export function parseProbe(payload: unknown): { url: string; token?: string } {
  const record = payloadRecord(payload);
  const token = optionalString(record, 'token');
  return { url: requiredString(record, 'url'), ...(token ? { token } : {}) };
}

export function parsePrefs(payload: unknown): Partial<ShellPrefs> {
  const record = payloadRecord(payload);
  const keep = record['keepLocalRunning'];
  if (keep !== undefined && typeof keep !== 'boolean') {
    throw new RegistryError('Malformed request: keepLocalRunning is not a boolean.');
  }
  return keep === undefined ? {} : { keepLocalRunning: keep };
}
