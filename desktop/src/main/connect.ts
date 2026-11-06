import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { API_CONTRACT } from '../shared/apiContract.js';
import {
  BRIDGE_PROTOCOL,
  isInterfaceScale,
  LOCAL_SERVER_ID,
  SHELL_VERSION,
  type InterfaceScale,
  type ServerChannel,
} from '../shared/bridge.js';
import type { FetchLike } from './server.js';
import type { ServerCredential } from './serverAccess.js';
import { errorMessage } from './errors.js';

/**
 * The shell's settings and the remote-server plumbing that reads them.
 *
 * One window shows one server at a time: the local server this shell
 * starts itself (POSIX only — the spawn path relies on POSIX process
 * groups to reap uv's uvicorn grandchild, which Windows has no equivalent
 * for), or a saved remote someone else runs. A remote is never ours: we
 * don't start it and we don't stop it, we just render it. The registry
 * that lists them all lives in servers.ts; this module is the pure layer
 * underneath it — settings on disk, URL normalization, the health/auth
 * probe and the channel catalog read.
 */

export const DEFAULT_CONNECT_URL = 'http://127.0.0.1:5001';

/** Saved-servers cap — a picker, not a database. */
export const MAX_SAVED_SERVERS = 10;

/**
 * A remembered remote server. The shell owns the credential: the token is
 * injected at the network layer for the window's server only, or sent
 * explicitly on the shell's own requests, and the fingerprint is the TOFU
 * pin for a self-signed certificate.
 */
export interface SavedServer {
  /** Minted by the shell when the server is added (a uuid). */
  id: string;
  name?: string;
  /** Normalized origin. */
  url: string;
  token?: string;
  /** Pinned certificate fingerprint (`sha256/…`) accepted on first use. */
  fingerprint?: string;
}

export interface DesktopSettings {
  /** Saved remote servers, in the order they were added. */
  servers: SavedServer[];
  /** Which server the window opens on next launch: `local` or a saved id. */
  activeServer: string;
  /** Quitting the app releases the local server instead of stopping it. */
  keepLocalRunning: boolean;
  /**
   * Updates check, download and offer themselves (main/updater.ts). Off:
   * nothing happens until the user asks (Check for Updates…). Default on.
   */
  automaticUpdates: boolean;
  /**
   * The local server's last port, tried first at the next spawn so the
   * local origin — and with it the page's localStorage boot cache and
   * IndexedDB drafts — stays the same across launches. A taken port falls
   * back to an ephemeral one.
   */
  localPort?: number;
  /**
   * This device's interface scale: the app window's relative size, one
   * of INTERFACE_SCALES (shared/bridge.ts). Absent means the default, 1.
   * Set from Settings → This device's "Interface size" row and the View menu.
   */
  interfaceScale?: InterfaceScale;
}

export function defaultSettings(): DesktopSettings {
  return { servers: [], activeServer: LOCAL_SERVER_ID, keepLocalRunning: false, automaticUpdates: true };
}

function settingsPath(runtimeDir: string): string {
  return path.join(runtimeDir, 'settings.json');
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function sanitizeServer(entry: unknown): SavedServer | null {
  if (typeof entry !== 'object' || entry === null) return null;
  const record = entry as Record<string, unknown>;
  const id = nonEmptyString(record['id']);
  const raw = nonEmptyString(record['url']);
  if (!id || !raw) return null;
  const url = normalizeServerUrl(raw);
  if (!url) return null;
  const server: SavedServer = { id, url };
  const name = nonEmptyString(record['name']);
  if (name) server.name = name;
  const token = nonEmptyString(record['token']);
  if (token) server.token = token;
  const fingerprint = nonEmptyString(record['fingerprint']);
  if (fingerprint) server.fingerprint = fingerprint;
  return server;
}

function isPort(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 && value < 65_536;
}

/** settings.json, sanitized; the defaults for anything missing or junk. */
export function readSettings(runtimeDir: string): DesktopSettings {
  const settings = defaultSettings();
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(settingsPath(runtimeDir), 'utf8'));
  } catch {
    // Missing or corrupt settings are not worth surfacing — the defaults
    // are exactly what a first launch wants.
    return settings;
  }
  if (typeof parsed !== 'object' || parsed === null) return settings;
  const record = parsed as Record<string, unknown>;
  if (Array.isArray(record['servers'])) {
    const ids = new Set<string>();
    const urls = new Set<string>();
    for (const entry of record['servers']) {
      const server = sanitizeServer(entry);
      // A hand-edited duplicate id or origin would make "which one did
      // the user mean" unanswerable; the first one wins.
      if (!server || ids.has(server.id) || urls.has(server.url)) continue;
      ids.add(server.id);
      urls.add(server.url);
      settings.servers.push(server);
      if (settings.servers.length === MAX_SAVED_SERVERS) break;
    }
  }
  const active = record['activeServer'];
  if (typeof active === 'string' && settings.servers.some((server) => server.id === active)) {
    settings.activeServer = active;
  }
  settings.keepLocalRunning = record['keepLocalRunning'] === true;
  // Only an explicit false turns updates off: anything else is the default.
  settings.automaticUpdates = record['automaticUpdates'] !== false;
  if (isPort(record['localPort'])) settings.localPort = record['localPort'];
  // Only a listed scale: anything else (a hand-edited 1.5, a string)
  // would zoom the window to a factor no control can step back from.
  const interfaceScale = record['interfaceScale'];
  if (isInterfaceScale(interfaceScale)) settings.interfaceScale = interfaceScale;
  return settings;
}

export function writeSettings(runtimeDir: string, settings: DesktopSettings): void {
  const target = settingsPath(runtimeDir);
  // Write-then-rename: an in-place write cut short by a crash would leave
  // truncated JSON, which readSettings silently turns into the defaults
  // (losing the saved servers and their tokens).
  const tmp = `${target}.${process.pid}.tmp`;
  try {
    mkdirSync(runtimeDir, { recursive: true });
    writeFileSync(tmp, `${JSON.stringify(settings, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
    renameSync(tmp, target);
  } catch {
    // Not being able to remember a server is a papercut, not a failure.
    try {
      rmSync(tmp, { force: true });
    } catch {
      // Nothing left to tidy.
    }
  }
}

/** The saved server with this id. */
export function findServer(settings: DesktopSettings, id: string): SavedServer | undefined {
  return settings.servers.find((server) => server.id === id);
}

/** The saved server on `url`'s origin, however the URL is spelled. */
export function findServerByUrl(settings: DesktopSettings, url: string): SavedServer | undefined {
  const normalized = normalizeServerUrl(url);
  if (!normalized) return undefined;
  return settings.servers.find((server) => server.url === normalized);
}

/** What `addServer` was given: a typed server, before it has an id. */
export interface ServerFields {
  name?: string;
  url: string;
  token?: string;
  fingerprint?: string;
}

export type AddServerResult =
  | { ok: true; settings: DesktopSettings; server: SavedServer }
  | { ok: false; message: string };

/**
 * Save a remote server. A URL already saved (compared as a normalized
 * origin) is the same server: its id stays and the given fields merge over
 * it, so re-adding with a new token replaces the token. A new one gets
 * `mintId()` and goes last; the list is capped and a full list refuses
 * rather than silently forgetting a server.
 */
export function addServer(
  settings: DesktopSettings,
  fields: ServerFields,
  mintId: () => string,
): AddServerResult {
  const url = normalizeServerUrl(fields.url);
  if (!url) {
    return { ok: false, message: `"${fields.url.trim()}" isn't a URL. Try something like ${DEFAULT_CONNECT_URL}.` };
  }
  const extra: Omit<SavedServer, 'id' | 'url'> = {};
  const name = fields.name?.trim();
  if (name) extra.name = name;
  if (fields.token) extra.token = fields.token;
  if (fields.fingerprint) extra.fingerprint = fields.fingerprint;
  const existing = findServerByUrl(settings, url);
  if (existing) {
    const server: SavedServer = { ...existing, ...extra };
    return {
      ok: true,
      settings: { ...settings, servers: settings.servers.map((s) => (s.id === existing.id ? server : s)) },
      server,
    };
  }
  if (settings.servers.length >= MAX_SAVED_SERVERS) {
    return { ok: false, message: `At most ${MAX_SAVED_SERVERS} servers can be saved. Remove one first.` };
  }
  const server: SavedServer = { id: mintId(), url, ...extra };
  return { ok: true, settings: { ...settings, servers: [...settings.servers, server] }, server };
}

/**
 * Change a saved server's name, token or pin. An empty name clears it
 * (the Servers list then shows the host). Unknown ids change nothing.
 */
export function updateServer(
  settings: DesktopSettings,
  id: string,
  patch: { name?: string; token?: string; fingerprint?: string },
): DesktopSettings {
  return {
    ...settings,
    servers: settings.servers.map((server) => {
      if (server.id !== id) return server;
      const next: SavedServer = { ...server };
      if (patch.name !== undefined) {
        const name = patch.name.trim();
        if (name) next.name = name;
        else delete next.name;
      }
      if (patch.token !== undefined) {
        if (patch.token) next.token = patch.token;
        else delete next.token;
      }
      if (patch.fingerprint !== undefined) next.fingerprint = patch.fingerprint;
      return next;
    }),
  };
}

/** Forget a saved server; a launch target that named it falls back to local. */
export function removeServer(settings: DesktopSettings, id: string): DesktopSettings {
  return {
    ...settings,
    servers: settings.servers.filter((server) => server.id !== id),
    activeServer: settings.activeServer === id ? LOCAL_SERVER_ID : settings.activeServer,
  };
}

/**
 * Save the server COLLIMATE_CONNECT_URL names, so a launch pointed at it by
 * automation has an id to open. Never its token: the env pair is
 * per-invocation (envTokenFor offers it to that origin at request time),
 * and disk is for what the user typed.
 */
export function withEnvServer(
  settings: DesktopSettings,
  env: NodeJS.ProcessEnv,
  mintId: () => string,
): DesktopSettings {
  const envUrl = env['COLLIMATE_CONNECT_URL'];
  if (!envUrl || !normalizeServerUrl(envUrl) || findServerByUrl(settings, envUrl)) return settings;
  const added = addServer(settings, { url: envUrl }, mintId);
  return added.ok ? added.settings : settings;
}

/**
 * Which server the window opens on at launch. `null` means none can be
 * opened: the splash shows the connect form.
 *
 * COLLIMATE_CONNECT_URL (saved by withEnvServer first) forces that remote,
 * for automation. Otherwise the server the window was last on. Windows has
 * no local server, so there a launch that would open it opens the first
 * saved remote instead, or none.
 */
export function bootTarget(options: {
  platform: NodeJS.Platform;
  env: NodeJS.ProcessEnv;
  settings: DesktopSettings;
}): { serverId: string | null } {
  const { platform, env, settings } = options;
  // Truthiness, not presence: an exported-but-empty variable is unset.
  const envUrl = env['COLLIMATE_CONNECT_URL'];
  if (envUrl) return { serverId: findServerByUrl(settings, envUrl)?.id ?? null };
  if (settings.activeServer !== LOCAL_SERVER_ID && findServer(settings, settings.activeServer)) {
    return { serverId: settings.activeServer };
  }
  if (platform === 'win32') return { serverId: settings.servers[0]?.id ?? null };
  return { serverId: LOCAL_SERVER_ID };
}

/**
 * The automation token for `url`: `COLLIMATE_CONNECT_TOKEN`, but only when
 * `url` is the origin `COLLIMATE_CONNECT_URL` names.
 *
 * The env pair is one credential for one server. Offered anywhere else —
 * another saved server, a server typed on the Servers tab — it would hand
 * that server's token to a different server, so a token set without a URL
 * is offered to nobody. Both sides are compared as normalized origins, like
 * findServerByUrl.
 */
export function envTokenFor(env: NodeJS.ProcessEnv, url: string): string | undefined {
  const token = env['COLLIMATE_CONNECT_TOKEN'];
  const envUrl = env['COLLIMATE_CONNECT_URL'];
  if (!token || !envUrl) return undefined;
  const origin = normalizeServerUrl(envUrl);
  return origin !== null && origin === normalizeServerUrl(url) ? token : undefined;
}

/**
 * Tidy up what someone typed into the URL field. Returns null if it can't
 * be read as an http(s) URL at all.
 */
export function normalizeServerUrl(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  // A non-http(s) scheme must be rejected here, not http-prefixed into a
  // mangled host ("ftp://x" → "http://ftp") that fails later with the
  // wrong error.
  const hasScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed);
  if (hasScheme && !/^https?:\/\//i.test(trimmed)) return null;
  // "localhost:5001" and "127.0.0.1:5001" are what people actually type.
  const withScheme = hasScheme ? trimmed : `http://${trimmed}`;
  let parsed: URL;
  try {
    parsed = new URL(withScheme);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  if (!parsed.hostname) return null;
  // Origin only: the shell always loads the server's root document.
  return parsed.origin;
}

/**
 * What /health said, when it said anything a Collimate server would. An
 * `incompatible` server answered, so its version and contract are known:
 * the Servers list shows them beside this app's.
 */
export type HealthResult =
  | { ok: true; url: string; port: number; version: string | null; bridge: number; contract: string }
  | { ok: false; reason: 'invalid' | 'unreachable'; message: string }
  | { ok: false; reason: 'incompatible'; message: string; version: string | null; contract: string | null };

/**
 * -1, 0 or 1 as release version `a` is older than, the same as or newer
 * than `b`, by their leading numeric dotted parts (`0.4.1`, `0.5.0rc1`);
 * null when either has none.
 */
export function compareVersions(a: string, b: string): -1 | 0 | 1 | null {
  const parts = (version: string): number[] | null => {
    const match = /^v?(\d+(?:\.\d+)*)/.exec(version.trim());
    return match ? match[1]!.split('.').map(Number) : null;
  };
  const left = parts(a);
  const right = parts(b);
  if (!left || !right) return null;
  for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) return difference < 0 ? -1 : 1;
  }
  return 0;
}

/**
 * Why this app cannot speak a server's API contract, or null when it can.
 * Contracts are compared for equality (API_CONTRACT is the digest of this
 * build's channel-events schema and IPC op table); the versions only say
 * which side is older, and so which one to update. Mixed versions with one
 * contract are fine.
 */
export function contractProblem(
  url: string,
  server: { version: string | null; contract: string | null },
  app: { version: string; contract: string } = { version: SHELL_VERSION, contract: API_CONTRACT },
): string | null {
  if (server.contract === app.contract) return null;
  const named = server.version ? `Collimate ${server.version} at ${url}` : url;
  if (server.contract === null) {
    return `${named} reports no API contract: it is older than this app (Collimate ${app.version}). ` +
      'Update that server on its own machine.';
  }
  const order = server.version ? compareVersions(server.version, app.version) : null;
  const theirs = `API contract ${server.contract}`;
  const ours = `this app's (Collimate ${app.version}, ${app.contract})`;
  if (order === -1) return `${named} speaks an older ${theirs} than ${ours}. Update that server on its own machine.`;
  if (order === 1) return `${named} speaks a newer ${theirs} than ${ours}. Update this app (Check for updates).`;
  return `${named} speaks ${theirs}, not ${ours}: the two are builds of different code.`;
}

export type ProbeResult =
  | { ok: true; url: string; port: number; serverVersion: string | null }
  | { ok: false; message: string };

const PROBE_TIMEOUT_MS = 5_000;

function bearer(token: string | undefined): { headers?: Record<string, string> } {
  return token ? { headers: { authorization: `Bearer ${token}` } } : {};
}

/**
 * Something the clock must not run against: the certificate prompt the
 * user is reading, whose answer the request's TLS handshake is parked on.
 * Returns that wait, or null when nothing holds the request.
 */
export type HoldFor = () => Promise<unknown> | null;

/**
 * A timeout-bounded request: `run` gets the signal, the timer is always
 * cleared. When the timer fires while `holdFor` reports a hold, the clock
 * restarts once the hold ends instead of aborting.
 */
async function withTimeout<T>(
  timeoutMs: number,
  run: (signal: AbortSignal) => Promise<T>,
  onError: (aborted: boolean, error: unknown) => T,
  holdFor?: HoldFor,
): Promise<T> {
  const controller = new AbortController();
  let settled = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const arm = (): void => {
    if (settled) return;
    timer = setTimeout(() => {
      const hold = holdFor?.();
      if (hold) {
        void hold.then(arm, arm);
        return;
      }
      controller.abort();
    }, timeoutMs);
  };
  arm();
  try {
    return await run(controller.signal);
  } catch (error) {
    return onError(controller.signal.aborted, error);
  } finally {
    settled = true;
    clearTimeout(timer);
  }
}

/**
 * GET `<url>/health` and check it is a Collimate server speaking this
 * shell's bridge and API contract. Public on every server, so no token rides it.
 */
export async function readHealth(
  rawUrl: string,
  fetchImpl: FetchLike,
  options: { timeoutMs?: number; holdFor?: HoldFor } = {},
): Promise<HealthResult> {
  const timeoutMs = options.timeoutMs ?? PROBE_TIMEOUT_MS;
  const url = normalizeServerUrl(rawUrl);
  if (!url) {
    return {
      ok: false,
      reason: 'invalid',
      message: `"${rawUrl.trim()}" isn't a URL. Try something like ${DEFAULT_CONNECT_URL}.`,
    };
  }
  return withTimeout<HealthResult>(
    timeoutMs,
    async (signal) => {
      const response = await fetchImpl(`${url}/health`, { signal });
      if (!response.ok) {
        return {
          ok: false,
          reason: 'unreachable',
          message: `${url}/health returned HTTP ${response.status}. Is that a Collimate server?`,
        };
      }
      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        return { ok: false, reason: 'unreachable', message: `${url}/health didn't return JSON. Is that a Collimate server?` };
      }
      if (typeof body !== 'object' || body === null) {
        return { ok: false, reason: 'unreachable', message: `${url}/health didn't return a Collimate health response.` };
      }
      const record = body as Record<string, unknown>;
      const version = typeof record['version'] === 'string' ? record['version'] : null;
      const contract = typeof record['contract'] === 'string' ? record['contract'] : null;
      const incompatible = (message: string): HealthResult => ({ ok: false, reason: 'incompatible', message, version, contract });
      const bridge = record['bridge'];
      if (typeof bridge !== 'number') {
        return incompatible(
          `${url} answered, but didn't report a bridge version. It's probably an older Collimate ` +
            `than this app (which speaks bridge ${BRIDGE_PROTOCOL}).`,
        );
      }
      if (bridge !== BRIDGE_PROTOCOL) {
        return incompatible(`${url} speaks bridge ${bridge}; this app speaks ${BRIDGE_PROTOCOL}. Update whichever one is older.`);
      }
      const problem = contractProblem(url, { version, contract });
      if (problem !== null) return incompatible(problem);
      const parsed = new URL(url);
      const port = Number(parsed.port || (parsed.protocol === 'https:' ? 443 : 80));
      // No problem means the server's contract is this app's.
      return { ok: true, url, port, version, bridge, contract: API_CONTRACT };
    },
    (aborted, error) => ({
      ok: false,
      reason: 'unreachable',
      message: aborted
        ? `${url} didn't respond within ${Math.round(timeoutMs / 1000)}s. Is the server running?`
        : `Couldn't reach ${url}: ${errorMessage(error)}. Is the server running?`,
    }),
    options.holdFor,
  );
}

/**
 * Health-check a candidate server before a window is pointed at it, so a
 * typo or a server that isn't up yet produces a sentence instead of a
 * blank window with a Chromium error page.
 *
 * When the health check passes, the probe also settles the auth question
 * via POST /auth/session — /health is deliberately public on a tokened
 * server, so health alone would happily bless a server that will 401 the
 * first real page load. 404 there means auth is off — an open server;
 * 204 validates the offered token; 401 is a missing or wrong token,
 * reported as a sentence. The token travels as an explicit header on this
 * one request, never through the window's session binding.
 */
export async function probeServer(
  rawUrl: string,
  fetchImpl: FetchLike,
  options: { token?: string; timeoutMs?: number; holdFor?: HoldFor } = {},
): Promise<ProbeResult> {
  const timeoutMs = options.timeoutMs ?? PROBE_TIMEOUT_MS;
  const health = await readHealth(rawUrl, fetchImpl, { timeoutMs, ...(options.holdFor ? { holdFor: options.holdFor } : {}) });
  if (!health.ok) return { ok: false, message: health.message };
  const { url } = health;
  return withTimeout<ProbeResult>(
    timeoutMs,
    async (signal) => {
      // Only 401 stops us: the endpoint 404s when auth is off, which reads
      // as "open server".
      const authResponse = await fetchImpl(`${url}/auth/session`, {
        method: 'POST',
        signal,
        ...bearer(options.token),
      });
      if (authResponse.status === 401) {
        return {
          ok: false,
          message: options.token ? `${url} rejected the access token.` : `${url} requires an access token.`,
        };
      }
      return { ok: true, url, port: health.port, serverVersion: health.version };
    },
    (aborted, error) => ({
      ok: false,
      message: aborted
        ? `${url} didn't respond within ${Math.round(timeoutMs / 1000)}s. Is the server running?`
        : `Couldn't reach ${url}: ${errorMessage(error)}. Is the server running?`,
    }),
    options.holdFor,
  );
}

/** A server's channel catalog (`GET /api/channels`), or why it isn't readable. */
export type CatalogResult =
  | { ok: true; channels: ServerChannel[]; activeChannelId: string | null }
  | { ok: false; reason: 'unauthorized' | 'unreachable'; message: string };

/**
 * Read the light channel catalog of the server at `url`, with `token` as an
 * explicit Bearer header on this request alone. The shell polls every
 * saved server this way; a session-wide header filter would instead put
 * every saved token on every page and frame request to those origins.
 */
export async function fetchCatalog(
  url: string,
  token: string | undefined,
  fetchImpl: FetchLike,
  options: { timeoutMs?: number } = {},
): Promise<CatalogResult> {
  const timeoutMs = options.timeoutMs ?? PROBE_TIMEOUT_MS;
  return withTimeout<CatalogResult>(
    timeoutMs,
    async (signal) => {
      const response = await fetchImpl(`${url}/api/channels`, { signal, ...bearer(token) });
      if (response.status === 401 || response.status === 403) {
        return {
          ok: false,
          reason: 'unauthorized',
          message: token ? `${url} rejected the access token.` : `${url} requires an access token.`,
        };
      }
      if (!response.ok) {
        return { ok: false, reason: 'unreachable', message: `${url}/api/channels returned HTTP ${response.status}.` };
      }
      const body: unknown = await response.json();
      const record = typeof body === 'object' && body !== null ? (body as Record<string, unknown>) : {};
      const raw = Array.isArray(record['channels']) ? record['channels'] : [];
      const channels: ServerChannel[] = [];
      for (const entry of raw) {
        if (typeof entry !== 'object' || entry === null) continue;
        const { id, name } = entry as Record<string, unknown>;
        if (typeof id === 'string' && id && typeof name === 'string') channels.push({ id, name });
      }
      const active = record['active_channel_id'];
      return {
        ok: true,
        channels,
        activeChannelId: typeof active === 'string' && channels.some((c) => c.id === active) ? active : null,
      };
    },
    (aborted, error) => ({
      ok: false,
      reason: 'unreachable',
      message: aborted
        ? `${url} didn't respond within ${Math.round(timeoutMs / 1000)}s.`
        : `Couldn't read ${url}/api/channels: ${errorMessage(error)}.`,
    }),
  );
}

/**
 * The token to offer the saved server `id`: what the user just typed, else
 * the saved one, else the automation env var's — and that one only for the
 * origin COLLIMATE_CONNECT_URL names (envTokenFor). The shell owns the
 * credential — nothing here ever reaches the served frontend.
 */
export function resolveToken(
  settings: DesktopSettings,
  env: NodeJS.ProcessEnv,
  id: string,
  explicit?: string,
): string | undefined {
  if (explicit) return explicit;
  const saved = findServer(settings, id);
  if (!saved) return undefined;
  return saved.token ?? envTokenFor(env, saved.url);
}

/** What to bind the window to for the saved server `id`; null for an unknown id. */
export function credentialFor(settings: DesktopSettings, id: string, token?: string): ServerCredential | null {
  const saved = findServer(settings, id);
  if (!saved) return null;
  return { origin: saved.url, ...(token ? { token } : {}) };
}
