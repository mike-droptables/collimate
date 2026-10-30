/**
 * Server credentials at the network layer of the app's own session.
 *
 * Two jobs, kept apart on purpose:
 *
 * - `bindWindow(credential)`: the bearer token of the server the WINDOW
 *   shows, injected for that one origin. Every request the window makes
 *   there (documents, fetch, EventSource, WebSockets) picks up the
 *   `Authorization: Bearer` header here, so the served frontend contains
 *   zero auth code and never sees the token. Other saved servers' tokens
 *   never ride a page or frame request: the shell's own requests to them
 *   (status and catalog polls, probes) carry the header explicitly, one
 *   request at a time (connect.ts). Those requests go through this same
 *   session, so they are marked ({@link shellRequestInit}) and the
 *   injection leaves them alone — a probe of the window's own server with
 *   a new token, or with none, must test exactly that.
 * - `setPins(pins)`: trust-on-first-use certificate pinning for every
 *   saved https server, so the shell's polls and probes of several servers
 *   can each verify or pin their host. Chromium's verdict stands for every
 *   other host; for a pinned host a failed verification is matched against
 *   the stored pin, or (no pin yet) put to the user once and pinned on
 *   acceptance. A later mismatch fails hard — a changed certificate on a
 *   pinned server is exactly the event pinning exists to catch.
 *
 * Guest views live in `persist:collimate-*` partitions — different
 * sessions — and are never touched by anything in this module.
 */

import { isSameOrigin } from '../shared/bridge.js';

/**
 * Marks a request as the shell's own (net.fetch from main) rather than the
 * window's: bindWindow strips it and injects nothing into that request.
 */
export const SHELL_REQUEST_HEADER = 'X-Collimate-Shell';

/** `init` with the shell's marker added; every main-process request to a server goes through this. */
export function shellRequestInit<T extends { headers?: Record<string, string> }>(
  init?: T,
): T & { headers: Record<string, string> } {
  return { ...(init ?? ({} as T)), headers: { ...init?.headers, [SHELL_REQUEST_HEADER]: '1' } };
}

/** The header's actual key in `headers`, matched case-insensitively. */
function headerKey(headers: Record<string, string>, name: string): string | undefined {
  const wanted = name.toLowerCase();
  return Object.keys(headers).find((key) => key.toLowerCase() === wanted);
}

/** What the window's server needs at the network layer. */
export interface ServerCredential {
  /** Normalized origin, e.g. `https://box.example:8443`. */
  origin: string;
  token?: string;
}

/** One saved https server's pin, keyed by hostname in a {@link PinMap}. */
export interface PinEntry {
  /** The saved server's id: an accepted pin is saved under it. */
  id: string;
  origin: string;
  /** Stored TOFU pin (`sha256/…`) for a self-signed certificate. */
  fingerprint?: string;
}

export type PinMap = Map<string, PinEntry>;

/** The slice of Electron's Session this module drives (DI for tests). */
export interface SessionLike {
  webRequest: {
    onBeforeSendHeaders(
      filter: { urls: string[] },
      listener:
        | ((
            details: { url: string; requestHeaders: Record<string, string> },
            callback: (response: { requestHeaders: Record<string, string> }) => void,
          ) => void)
        | null,
    ): void;
  };
  setCertificateVerifyProc(
    proc:
      | ((
          request: {
            hostname: string;
            certificate: { fingerprint: string };
            errorCode: number;
          },
          callback: (verificationResult: number) => void,
        ) => void)
      | null,
  ): void;
}

export interface ServerAccessDeps {
  /**
   * Put a first-use certificate to the user. Resolves true to trust and
   * pin it. Serialized here — at most one prompt in flight, whatever host.
   */
  confirmPin(host: string, fingerprint: string): Promise<boolean>;
  /** Persist an accepted pin under the saved server's id. */
  savePin(serverId: string, fingerprint: string): void;
  log?(line: string): void;
}

// Chromium verify-proc results: 0 = trust, -2 = reject, -3 = use
// Chromium's own verdict.
const VERIFY_TRUST = 0;
const VERIFY_REJECT = -2;
const VERIFY_USE_CHROMIUM = -3;

/** The pins for a set of saved servers: every https one, keyed by hostname. */
export function pinsFor(servers: readonly { id: string; url: string; fingerprint?: string }[]): PinMap {
  const pins: PinMap = new Map();
  for (const server of servers) {
    let parsed: URL;
    try {
      parsed = new URL(server.url);
    } catch {
      continue;
    }
    if (parsed.protocol !== 'https:' || pins.has(parsed.hostname)) continue;
    pins.set(parsed.hostname, {
      id: server.id,
      origin: parsed.origin,
      ...(server.fingerprint ? { fingerprint: server.fingerprint } : {}),
    });
  }
  return pins;
}

export class ServerAccess {
  private window: ServerCredential | null = null;
  private pins: PinMap = new Map();
  private prompting = false;
  private verifyInstalled = false;

  constructor(
    private readonly session: SessionLike,
    private readonly deps: ServerAccessDeps,
  ) {}

  /** The window's credential (null = no window, or an open server). */
  get windowCredential(): ServerCredential | null {
    return this.window;
  }

  /**
   * Bind the window's server (or none). Installing unconditionally —
   * rather than only when a token exists — keeps one code path and makes
   * `bindWindow(null)` genuinely revoke a previous token.
   */
  bindWindow(credential: ServerCredential | null): void {
    this.window = credential;
    if (!credential?.token) {
      this.session.webRequest.onBeforeSendHeaders({ urls: ['http://*/*', 'https://*/*'] }, null);
      return;
    }
    // The broad URL filter is deliberate: Chromium match patterns can't
    // express ports, so the exact origin check (scheme + host + port)
    // lives in the listener. Everything off-origin passes untouched, and
    // so does the shell's own request (marked) and any request that
    // already names its credential: an explicit header always wins.
    this.session.webRequest.onBeforeSendHeaders(
      { urls: ['http://*/*', 'https://*/*'] },
      (details, callback) => {
        const headers = details.requestHeaders;
        const marker = headerKey(headers, SHELL_REQUEST_HEADER);
        if (marker !== undefined) {
          delete headers[marker];
        } else if (
          this.window === credential &&
          credential.token &&
          headerKey(headers, 'authorization') === undefined &&
          isSameOrigin(details.url, credential.origin)
        ) {
          headers['Authorization'] = `Bearer ${credential.token}`;
        }
        callback({ requestHeaders: headers });
      },
    );
  }

  /** The pin entry for `hostname`, if any (accepted pins included). */
  pinFor(hostname: string): PinEntry | undefined {
    return this.pins.get(hostname);
  }

  /**
   * Replace the pinned hosts: every saved https server (plus a candidate
   * being probed). The verify proc reads the live map, so a pin accepted
   * for one server never waits for another call here.
   */
  setPins(pins: PinMap): void {
    this.pins = new Map(pins);
    if (this.pins.size === 0) {
      if (this.verifyInstalled) this.session.setCertificateVerifyProc(null);
      this.verifyInstalled = false;
      return;
    }
    if (this.verifyInstalled) return;
    this.verifyInstalled = true;
    this.session.setCertificateVerifyProc((request, callback) => this.verify(request, callback));
  }

  private verify(
    request: { hostname: string; certificate: { fingerprint: string }; errorCode: number },
    callback: (verificationResult: number) => void,
  ): void {
    const host = request.hostname;
    const pin = this.pins.get(host);
    // Not a saved server, or a certificate Chromium already trusts: no
    // opinion. Pinning is only for a saved server's failures.
    if (!pin || request.errorCode === 0) {
      callback(VERIFY_USE_CHROMIUM);
      return;
    }
    const presented = request.certificate.fingerprint;
    if (pin.fingerprint) {
      if (presented === pin.fingerprint) {
        callback(VERIFY_TRUST);
      } else {
        this.deps.log?.(
          `certificate for ${host} changed (pinned ${pin.fingerprint}, presented ${presented}) — refusing`,
        );
        callback(VERIFY_REJECT);
      }
      return;
    }
    // First use: ask once, one host at a time. Verifications while a
    // dialog is up — this host's or another's — are rejected rather than
    // queued: the caller retries after the user answers, and a stack of
    // dialogs is noise.
    if (this.prompting) {
      callback(VERIFY_REJECT);
      return;
    }
    this.prompting = true;
    void this.deps
      .confirmPin(host, presented)
      .then((accepted) => {
        if (!accepted) {
          callback(VERIFY_REJECT);
          return;
        }
        const current = this.pins.get(host);
        if (current?.id === pin.id) this.pins.set(host, { ...current, fingerprint: presented });
        this.deps.savePin(pin.id, presented);
        callback(VERIFY_TRUST);
      })
      .catch(() => callback(VERIFY_REJECT))
      .finally(() => {
        this.prompting = false;
      });
  }
}
