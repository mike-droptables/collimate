// SPDX-License-Identifier: MIT
//
// Session sockets for pointer renderers — the ONE reconnect state machine.
//
// A pointer renderer (`.terminal`, `.chat`, live `.webview` streams) opens a
// WebSocket against a host session at mount time. Getting the lifecycle right
// is subtle, and every renderer that hand-rolled it shipped the same bugs:
// redialing dead sessions forever on remount, escalating retries that
// ping-pong a single-attach session between two views of the same cell, and
// URL building that breaks on split-origin deployments. This module owns that
// policy so a renderer only supplies callbacks.
//
// The SDK build generates the module served at `/api/sdk/session.js` —
// renderers import from there. It is framework-free (no React), so it is safe
// for hook-based renderers AND for renderers that pin their own React version
// (e.g. assistant-ui) or use no framework at all (xterm).
//
// Close-code contract after an accepted upgrade. A pre-accept host rejection
// appears in browsers as 1006; its application close code is not observable:
//   4401 unauthorized / 4404 no such session → FINAL. The run stopped or the
//        server restarted; the pointer file outlives its in-memory session.
//        The session id is marked dead so remounts don't redial — only an
//        explicit `redial()` (a user's Reconnect click) tries again.
//   4409 busy → another mounted view of this cell holds the single-attach
//        session. Poll slowly to take over when the holder unmounts;
//        escalating retries just churn the server and ping-pong the session.
//   anything else → transient (network blip, sleep/resume, server restart).
//        Retry per `transientRetryMs`; a dead session resolves to 4404 on the
//        next attempt anyway.

export type SessionSocketStatus =
    | 'connecting' // a dial is in flight
    | 'open'       // live
    | 'closed'     // transient close; a redial may be scheduled per policy
    | 'busy'       // 4409 — held by another view; takeover poll may be scheduled
    | 'ended';     // final — 4401/4404, maxFailedOpens reached, or close()

export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_GONE = 4404;
export const CLOSE_BUSY = 4409;

export interface SessionSocketOptions {
    /** Full ws(s):// URL, or a builder invoked per dial. Build it with
     *  `sessionWsUrl(api, ws_path, token)` so split-origin deployments work. */
    url: string | (() => string);
    /** Enables the module-level dead-session registry: once the server says
     *  this id is gone (4401/4404), remounts render "ended" instead of
     *  redialing. Pass the pointer's terminal or chat id as an opaque key. */
    connectionKey?: string;
    /** e.g. 'arraybuffer' for binary PTY streams. */
    binaryType?: BinaryType;
    onMessage?: (ev: MessageEvent) => void;
    onStatus?: (status: SessionSocketStatus, ev?: CloseEvent) => void;
    /** Fires on every successful (re)open — reset per-connection state here. */
    onOpen?: (ws: WebSocket) => void;
    /** Delay before redialing a transient close: a fixed ms, a function of the
     *  attempt count (1-based), or `false` for no auto-retry (manual
     *  `redial()` only). Default: `min(500 * attempt, 5000)` backoff. */
    transientRetryMs?: number | ((attempt: number) => number) | false;
    /** Takeover-poll delay after a 4409, or `false` to park on 'busy'.
     *  Default 5000. */
    busyRetryMs?: number | false;
    /** Give up ('ended') after this many consecutive dials without a received frame — for proxied endpoints with no close-code contract, where
     *  repeated failure to open means the run is gone. Default: no limit. */
    maxFailedOpens?: number;
}

export interface SessionSocket {
    readonly status: SessionSocketStatus;
    /** OPEN-guarded send. Returns false (instead of throwing) when the socket
     *  isn't open — callers decide whether that means "queue", "drop", or
     *  "tell the user". */
    send(data: string | ArrayBuffer | Blob | ArrayBufferView<ArrayBuffer>): boolean;
    /** `send(JSON.stringify(value))`. */
    sendJson(value: unknown): boolean;
    /** Escape hatch: the live WebSocket (null between dials). */
    socket(): WebSocket | null;
    /** Manual reconnect — clears this session's dead marker, drops any
     *  scheduled retry, and dials once. The user's Reconnect button. */
    redial(): void;
    /** Final teardown. No further dials, callbacks, or timers. */
    close(): void;
}

// Session ids the server told us are gone for good. Module-level on purpose:
// the guarantee must survive unmount/remount cycles of the renderer.
const DEAD_SESSIONS = new Set<string>();

/** Has the server declared this session gone (4401/4404)? Check before
 *  building expensive per-session state (an xterm instance, a runtime) so a
 *  remount of a dead session renders "ended" without dialing. */
export function isSessionDead(connectionKey: string): boolean {
    return DEAD_SESSIONS.has(connectionKey);
}

/** Forget a dead marker — pair with rebuilding the session on an explicit
 *  user Reconnect. One deliberate dial per click; the server re-marks it if
 *  the session is still gone. */
export function clearDeadSession(connectionKey: string): void {
    DEAD_SESSIONS.delete(connectionKey);
}

/** The API origin — the app origin normally, or the configured split-origin
 *  API base. Derived from api.getFileUrl (absolute when an apiBase is set),
 *  so sockets and /proxy requests target the backend that actually hosts
 *  them, not the frontend origin. */
export function apiOrigin(api: unknown): string {
    try {
        const a = api as { getFileUrl?: (p: string) => string } | null;
        const u = new URL(a?.getFileUrl?.('_') || '', location.origin);
        return u.origin;
    } catch {
        return location.origin;
    }
}

/** Build the ws(s):// URL for a pointer's `ws_path` + `token` against the
 *  API origin. */
export function sessionWsUrl(api: unknown, wsPath: string, token?: string): string {
    const u = new URL(wsPath, apiOrigin(api));
    u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
    if (token) u.searchParams.set('token', token);
    return u.toString();
}

export function openSessionSocket(opts: SessionSocketOptions): SessionSocket {
    const {
        url, connectionKey, binaryType, onMessage, onStatus, onOpen,
        transientRetryMs, busyRetryMs = 5000, maxFailedOpens = Infinity,
    } = opts;

    let ws: WebSocket | null = null;
    let status: SessionSocketStatus = 'connecting';
    let attempts = 0;      // transient retries since the last received frame
    let failedOpens = 0;   // consecutive dials with no received frame
    let confirmedThisDial = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let finished = false;  // close() called — nothing runs after this
    let dialGen = 0;       // bumped per dial — lets onclose see a listener's redial()

    const setStatus = (s: SessionSocketStatus, ev?: CloseEvent) => {
        status = s;
        try { onStatus?.(s, ev); } catch { /* listener error is not ours */ }
    };

    const transientDelay = (attempt: number): number | false => {
        if (transientRetryMs === false) return false;
        if (typeof transientRetryMs === 'number') return transientRetryMs;
        if (typeof transientRetryMs === 'function') return transientRetryMs(attempt);
        return Math.min(500 * attempt, 5000);
    };

    const schedule = (delay: number) => {
        if (timer) clearTimeout(timer);
        timer = setTimeout(dial, delay);
    };

    const end = (ev?: CloseEvent) => {
        if (connectionKey) DEAD_SESSIONS.add(connectionKey);
        setStatus('ended', ev);
    };

    const detach = (sock: WebSocket | null) => {
        if (!sock) return;
        sock.onopen = sock.onmessage = sock.onerror = sock.onclose = null;
        try { sock.close(); } catch { /* already closing */ }
    };

    const dial = () => {
        if (finished) return;
        dialGen += 1;
        timer = null;
        if (ws) { const old = ws; ws = null; detach(old); }
        if (connectionKey && DEAD_SESSIONS.has(connectionKey)) { setStatus('ended'); return; }
        setStatus('connecting');
        confirmedThisDial = false;
        let sock: WebSocket;
        try {
            sock = new WebSocket(typeof url === 'function' ? url() : url);
        } catch {
            end();  // malformed URL — dialing again can't fix it
            return;
        }
        if (binaryType) sock.binaryType = binaryType;
        ws = sock;
        sock.onopen = () => {
            setStatus('open');
            try { onOpen?.(sock); } catch { /* listener error is not ours */ }
        };
        sock.onmessage = (ev) => {
            confirmedThisDial = true;
            attempts = 0;
            failedOpens = 0;
            try { onMessage?.(ev); } catch { /* listener error is not ours */ }
        };
        sock.onerror = () => { /* onclose always follows; policy lives there */ };
        sock.onclose = (ev: CloseEvent) => {
            if (ws === sock) ws = null;
            if (finished) return;
            // onStatus is user code: a close() or redial() there wins over our retry.
            const gen = dialGen;
            if (ev.code === CLOSE_GONE || ev.code === CLOSE_UNAUTHORIZED) { end(ev); return; }
            if (ev.code === CLOSE_BUSY) {
                setStatus('busy', ev);
                if (finished || gen !== dialGen) return;
                if (busyRetryMs !== false) schedule(busyRetryMs);
                return;
            }
            if (!confirmedThisDial) {
                failedOpens += 1;
                if (failedOpens >= maxFailedOpens) { end(ev); return; }
            }
            attempts += 1;
            const delay = transientDelay(attempts);
            setStatus('closed', ev);
            if (finished || gen !== dialGen) return;
            if (delay !== false) schedule(delay);
        };
    };

    const send = (data: string | ArrayBuffer | Blob | ArrayBufferView<ArrayBuffer>): boolean => {
        if (!ws || ws.readyState !== WebSocket.OPEN) return false;
        try { ws.send(data); return true; } catch { return false; }
    };

    dial();

    return {
        get status() { return status; },
        send,
        sendJson: (value: unknown) => send(JSON.stringify(value)),
        socket: () => ws,
        redial() {
            if (finished) return;
            if (connectionKey) DEAD_SESSIONS.delete(connectionKey);
            if (timer) { clearTimeout(timer); timer = null; }
            const old = ws; ws = null;
            detach(old);
            attempts = 0;
            failedOpens = 0;
            dial();
        },
        close() {
            if (finished) return;
            finished = true;
            if (timer) { clearTimeout(timer); timer = null; }
            const old = ws; ws = null;
            detach(old);
        },
    };
}
