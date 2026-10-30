import React, { useEffect, useMemo, useRef, useState } from 'https://esm.sh/react@19.3.1?target=es2022';
import { Terminal } from 'https://esm.sh/@xterm/xterm@6.0.0?target=es2022';
import { FitAddon } from 'https://esm.sh/@xterm/addon-fit@0.11.0?deps=@xterm/xterm@6.0.0&target=es2022';
import { WebLinksAddon } from 'https://esm.sh/@xterm/addon-web-links@0.12.0?deps=@xterm/xterm@6.0.0&target=es2022';
import {
    defineRenderer,
    resolveVar,
    toHex8,
    tokens,
    useCollimateFile,
    useDesktop,
    useTheme,
    useVisibility,
} from '/api/sdk/react.js';
import {
    clearDeadSession,
    isSessionDead,
    openSessionSocket,
    sessionWsUrl,
} from '/api/sdk/session.js';

/** Local shape of the /api/sdk/session.js socket handle (URL imports carry
 *  no types; see collimate-sdk/packages/renderer-hooks/src/session.ts). */
type SessionSocket = {
    readonly status: string;
    send(data: string | ArrayBufferLike | ArrayBufferView): boolean;
    sendJson(value: unknown): boolean;
    socket(): WebSocket | null;
    redial(): void;
    close(): void;
};

// A toolbar button from the pointer's optional `buttons`: a click emits `id`
// as a renderer action (the generator waits on it). Optional `title` is its
// tooltip; optional `confirm` is a question asked inline before the emit (for
// a button that cannot be undone).
export type ButtonSpec = { id: string; label: string; title?: string; confirm?: string };

function optText(v: unknown): string | undefined {
    return typeof v === 'string' && v ? v : undefined;
}

function parseButton(b: any): ButtonSpec | null {
    if (!b || typeof b.id !== 'string' || typeof b.label !== 'string') return null;
    const title = optText(b.title);
    const confirm = optText(b.confirm);
    return {
        id: b.id, label: b.label,
        ...(title ? { title } : {}),
        ...(confirm ? { confirm } : {}),
    };
}

export type Parsed = {
    terminal_id: string;
    ws_path: string;
    token: string;
    label: string;
    buttons: ButtonSpec[];
};

export function parseContent(raw: string): Parsed | null {
    try {
        const j = JSON.parse(raw || '');
        if (typeof j.terminal_id !== 'string' || !j.terminal_id) return null;
        if (typeof j.ws_path !== 'string' || !j.ws_path) return null;
        const buttons: ButtonSpec[] = Array.isArray(j.buttons)
            ? j.buttons.map(parseButton).filter((b: ButtonSpec | null): b is ButtonSpec => !!b)
            : [];
        return {
            terminal_id: j.terminal_id,
            ws_path: j.ws_path,
            token: typeof j.token === 'string' ? j.token : '',
            label: typeof j.label === 'string' ? j.label : 'terminal',
            buttons,
        };
    } catch {
        return null;
    }
}

/** The host's starting placeholder, `{"starting": true, "label"}`
 *  (collimate.pointers.starting_pointer), as its label: the run is accepted
 *  and its terminal does not exist yet. Null for anything else. */
export function parseStarting(raw: string): { label: string } | null {
    try {
        const j = JSON.parse(raw || '');
        if (!j || typeof j !== 'object' || Array.isArray(j) || j.starting !== true) return null;
        return { label: typeof j.label === 'string' ? j.label : '' };
    } catch {
        return null;
    }
}

// ---------------------------------------------------------------------------
// Input typed before the terminal exists. A starting pointer mounts xterm at
// once and the keystrokes typed into it are kept here — module-level, keyed
// by the mounted file (cell + path), so they survive the remount from the
// placeholder to the real pointer and live outside every per-session effect.
// The first real session sends them once, after its first output frame (a
// harness still booting its UI would swallow them earlier), and keystrokes
// typed into that session before then join the queue so the order holds.
// Bounded: past PENDING_INPUT_CAP bytes the rest is dropped, with a notice.
// This is separate from the 1013 rule: input on a dropped attach is never
// replayed, and a queue that has been sent is gone.
// ---------------------------------------------------------------------------

export const PENDING_INPUT_CAP = 64 * 1024;
export const PENDING_OVERFLOW_NOTICE =
    '\r\n[input buffer full — keystrokes typed before the terminal is live past 64 KiB are dropped]\r\n';

type PendingInput = { chunks: string[]; bytes: number; overflowed: boolean };

const PENDING_INPUT: Map<string, PendingInput> = new Map();
const utf8 = new TextEncoder();

/** The key a file's pending input lives under: its cell and its path. */
export function pendingInputKey(api: any): string {
    const cell = typeof api?.getCellId === 'function' ? api.getCellId() : '';
    const file = typeof api?.getFilename === 'function' ? api.getFilename() : '';
    return `${cell}:${file}`;
}

/** Keep `data` for the key's first session. 'queued' when it fits,
 *  'overflow' the first time the cap is crossed (show the notice once),
 *  'dropped' for every keystroke after that. */
export function queueInput(key: string, data: string): 'queued' | 'overflow' | 'dropped' {
    let pending = PENDING_INPUT.get(key);
    if (!pending) {
        pending = { chunks: [], bytes: 0, overflowed: false };
        PENDING_INPUT.set(key, pending);
    }
    if (pending.overflowed) return 'dropped';
    const size = utf8.encode(data).length;
    if (pending.bytes + size > PENDING_INPUT_CAP) {
        pending.overflowed = true;
        return 'overflow';
    }
    pending.chunks.push(data);
    pending.bytes += size;
    return 'queued';
}

/** Whether keystrokes are waiting for the key's first session. */
export function hasPendingInput(key: string): boolean {
    return PENDING_INPUT.has(key);
}

/** Send the key's queued input once, as one write, and forget it — a later
 *  session (a retry after 1013, a reconnect) never sees it again. True when
 *  there was input and `send` took it. */
export function flushPendingInput(key: string, send: (data: string) => boolean): boolean {
    const pending = PENDING_INPUT.get(key);
    if (!pending) return false;
    PENDING_INPUT.delete(key);
    const data = pending.chunks.join('');
    return data ? send(data) : false;
}

/** Forget the key's queued input because no session will ever take it (the
 *  run ended while starting). Returns how many bytes were dropped, for the
 *  notice that says so. */
export function dropPendingInput(key: string): number {
    const pending = PENDING_INPUT.get(key);
    PENDING_INPUT.delete(key);
    return pending ? pending.bytes : 0;
}

/** What the starting terminal says when its run ends before the terminal
 *  exists and `bytes` of typed input will never be sent. */
export function droppedInputNotice(bytes: number): string {
    return `\r\n\x1b[33m[the run ended before the terminal started — ${bytes} byte${bytes === 1 ? '' : 's'} of typed input not sent]\x1b[0m\r\n`;
}

/** A keystroke in a live session: queued behind input still waiting for
 *  the session's first output, else sent. */
export function routeInput(
    key: string, data: string, send: (data: string) => boolean,
): 'sent' | 'queued' | 'overflow' | 'dropped' {
    if (hasPendingInput(key)) return queueInput(key, data);
    send(data);
    return 'sent';
}

// ---------------------------------------------------------------------------
// xterm colours. xterm draws from its own colour theme and cannot read CSS
// variables, so its theme is built from the skin's snapshot (api.getTheme(), the host's
// :root tokens keyed by CSS name) resolved to concrete colours, and rebuilt
// on every onThemeChange. The ground is the skin's code ground, the text its
// ink, the cursor teal (live) and a selection the brass selected fill. The
// sixteen ANSI colours are the one palette in this renderer: programs pick
// them by number, so they are declared once per scheme, chosen for contrast
// on the skins' code grounds (dark: night and night-cool; light: paper).
// Colours outside that table (256-colour and truecolor output, often near
// white for a dark ground) are lifted by xterm's minimum contrast ratio on a
// light ground.
// ---------------------------------------------------------------------------

type Scheme = 'dark' | 'light';
type ThemeSnapshot = { scheme: Scheme; vars: Record<string, string> };

export const ANSI_KEYS = [
    'black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white',
    'brightBlack', 'brightRed', 'brightGreen', 'brightYellow',
    'brightBlue', 'brightMagenta', 'brightCyan', 'brightWhite',
] as const;
export type AnsiKey = typeof ANSI_KEYS[number];

// The palette table: the renderer's only colour literals.
export const ANSI_PALETTE: Readonly<Record<Scheme, Readonly<Record<AnsiKey, string>>>> = Object.freeze({
    dark: Object.freeze({
        black: '#2b2722', red: '#e07a6c', green: '#9fc28a', yellow: '#e0bb6c',
        blue: '#82a8d6', magenta: '#c398cf', cyan: '#7cbfb5', white: '#d6d0c5',
        brightBlack: '#7f786d', brightRed: '#f09585', brightGreen: '#b9d9a3', brightYellow: '#f0d48e',
        brightBlue: '#a3c2ea', brightMagenta: '#d8b3e2', brightCyan: '#9bd7ce', brightWhite: '#f3efe7',
    }),
    // On paper "white" is a mid grey: programs print white text expecting a
    // dark ground, and it must stay readable here.
    light: Object.freeze({
        black: '#1e2227', red: '#ac3a2e', green: '#2f6b3b', yellow: '#7f5700',
        blue: '#2c5a9e', magenta: '#86398a', cyan: '#1c6a6e', white: '#5f6368',
        brightBlack: '#4a4f55', brightRed: '#b8402f', brightGreen: '#357444', brightYellow: '#8a5f08',
        brightBlue: '#3565ac', brightMagenta: '#954497', brightCyan: '#217378', brightWhite: '#767a7f',
    }),
});

/** The xterm theme for a skin snapshot: the skin's tokens resolved to
 *  `#rrggbbaa` (rgba tokens such as --brass-soft keep their alpha) over the
 *  scheme's ANSI palette. A token the snapshot does not carry is left out,
 *  so xterm keeps its own value for it rather than an empty colour. */
export function buildXtermTheme(theme: ThemeSnapshot): Record<string, string> {
    const color = (name: string): string => {
        const value = resolveVar(theme as any, name as any);
        return value ? (toHex8(value) ?? value) : '';
    };
    const out: Record<string, string> = { ...ANSI_PALETTE[theme.scheme === 'light' ? 'light' : 'dark'] };
    const ground = color('--code-bg');
    const roles: Array<[string, string]> = [
        ['background', ground],
        ['foreground', color('--ink')],
        ['cursor', color('--teal')],
        ['cursorAccent', ground],
        ['selectionBackground', color('--brass-soft')],
        ['selectionInactiveBackground', color('--brass-soft')],
    ];
    for (const [key, value] of roles) if (value) out[key] = value;
    return out;
}

/** The xterm options that follow the skin: the theme, and on a light ground
 *  a 4.5:1 minimum contrast so program output outside the ANSI table stays
 *  legible (a dark ground leaves colours as the program sent them). */
export function xtermOptionsFor(theme: ThemeSnapshot): { theme: Record<string, string>; minimumContrastRatio: number } {
    return {
        theme: buildXtermTheme(theme),
        minimumContrastRatio: theme.scheme === 'light' ? 4.5 : 1,
    };
}

/** The host's current skin snapshot. */
function hostTheme(api: any): ThemeSnapshot {
    const t = api && typeof api.getTheme === 'function' ? api.getTheme() : null;
    return t && t.vars ? t : { scheme: 'dark', vars: {} };
}

// ---------------------------------------------------------------------------
// Font size — a user preference shared across all terminal cells and persisted
// so it survives reconnects, remounts, and brand-new sessions. Applied live to
// the cached xterm instance, then re-fit against the cell box (new cell metrics
// → new cols/rows → SIGWINCH to the PTY) and repainted.
// ---------------------------------------------------------------------------

const FONT_SIZE_KEY = 'collimate.terminal.fontSize';
const FONT_SIZE_MIN = 8;
const FONT_SIZE_MAX = 32;
const FONT_SIZE_DEFAULT = 13;

function readFontSize(): number {
    try {
        const v = parseInt(window.localStorage.getItem(FONT_SIZE_KEY) || '', 10);
        if (Number.isFinite(v) && v >= FONT_SIZE_MIN && v <= FONT_SIZE_MAX) return v;
    } catch { /* localStorage unavailable (private mode / sandbox) */ }
    return FONT_SIZE_DEFAULT;
}

function writeFontSize(n: number): void {
    try { window.localStorage.setItem(FONT_SIZE_KEY, String(n)); } catch { /* ignore */ }
}

// Font size is a single preference shared by every terminal cell. localStorage
// 'storage' events only fire in OTHER tabs, so to keep multiple terminal cells
// on the SAME page in lockstep we also fan out through a module-level listener
// set (all terminal cells in a page share this module + SESSION_CACHE).
const FONT_SIZE_LISTENERS = new Set<(n: number) => void>();
function broadcastFontSize(n: number): void {
    for (const cb of Array.from(FONT_SIZE_LISTENERS)) {
        try { cb(n); } catch { /* listener disposed mid-iterate */ }
    }
}

// ---------------------------------------------------------------------------
// Mono face. The skin's --font-mono names a bundled web face served with
// font-display: swap, and xterm measures its character cell once, when the
// family is set. Measured before the face has loaded, the grid is sized
// against the fallback face and the real face then draws off it. So xterm
// gets the family only once its faces have loaded (the fallback stack until
// then); setting it afterwards is a real option change, which makes xterm
// re-measure, and the caller refits so the PTY learns the new grid.
// ---------------------------------------------------------------------------

export const MONO_FALLBACK = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';

type FontSet = { check(font: string): boolean; load(font: string): Promise<unknown> };
type FontTarget = { options: { fontFamily?: string; fontSize?: number } };

/** The skin's mono family from the host's computed :root. */
function readMonoFamily(): string {
    return getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim() || MONO_FALLBACK;
}

/** The faces xterm draws with at `size`: regular and bold. */
export function monoFaceSpecs(family: string, size: number): string[] {
    return [`${size}px ${family}`, `bold ${size}px ${family}`];
}

/** The family xterm may measure right now: `family` when its faces have
 *  already loaded, otherwise the fallback stack. */
export function measurableFamily(fonts: FontSet, family: string, size: number): string {
    try {
        return monoFaceSpecs(family, size).every((spec) => fonts.check(spec)) ? family : MONO_FALLBACK;
    } catch {
        return MONO_FALLBACK;
    }
}

/** Load the faces of the family `current()` names, then hand it to `term`.
 *  Returns true when the family changed (xterm has re-measured and the grid
 *  needs a refit). A family that changed while loading is left to the newer
 *  call. A face that fails to load still gets set: the family names its own
 *  fallbacks, and the browser then draws and measures with those. */
export async function settleMonoFont(term: FontTarget, fonts: FontSet, current: () => string): Promise<boolean> {
    const family = current();
    const size = term.options.fontSize ?? FONT_SIZE_DEFAULT;
    try {
        await Promise.all(monoFaceSpecs(family, size).map((spec) => fonts.load(spec)));
    } catch { /* the family's own fallbacks draw */ }
    if (current() !== family || term.options.fontFamily === family) return false;
    term.options.fontFamily = family;
    return true;
}

/** Give a session's xterm the skin's mono face once it has loaded, then
 *  refit and repaint against the new cell size. */
async function applyMonoFont(session: CachedSession): Promise<void> {
    try {
        if (!(await settleMonoFont(session.term, document.fonts, readMonoFamily))) return;
    } catch {
        return; // term disposed while the face loaded
    }
    fitAndSync(session);
    refreshTerm(session);
}

// Breathing room between the cell's left edge and the terminal text — the
// header strip's own side padding, so the prompt lines up under the session
// label. Applied as padding on xterm's root element (term.element, `.xterm`):
// the fit addon subtracts that element's padding before it sizes the grid,
// so the columns still fit the box exactly. (Padding the HOST box instead
// cost a column at narrow widths.)
const TEXT_INSET_LEFT_PX = 8;

// The header strip's small tools: the skin's quiet bordered button, with
// the small-tool radius (four below the skin's radius).
const SMALL_RADIUS = `calc(${tokens.radius} - 4px)`;

const FONT_BTN_STYLE: React.CSSProperties = {
    height: 18,
    minWidth: 20,
    padding: '0 5px',
    background: 'transparent',
    border: `1px solid ${tokens.color.line2}`,
    borderRadius: SMALL_RADIUS,
    color: tokens.color.ink2,
    fontFamily: tokens.font.mono,
    fontSize: 11,
    lineHeight: 1,
};

// The toolbar's plain buttons (an inline confirm's Cancel, Reconnect).
const TOOL_BTN_STYLE: React.CSSProperties = {
    height: 18,
    padding: '0 8px',
    background: 'transparent',
    border: `1px solid ${tokens.color.line2}`,
    borderRadius: SMALL_RADIUS,
    color: tokens.color.ink2,
    fontFamily: tokens.font.mono,
    fontSize: 10,
    cursor: 'pointer',
};

/** A declared toolbar button (Capture Chat) is the strip's call to
 *  action, in the live colour; while its click is in flight it goes quiet. */
export function ctaStyle(busy: boolean, dimmed: boolean): React.CSSProperties {
    return {
        height: 18,
        padding: '0 8px',
        background: busy ? tokens.color.sheet2 : tokens.color.tealSoft,
        border: `1px solid ${busy ? tokens.color.line2 : tokens.color.teal}`,
        borderRadius: SMALL_RADIUS,
        color: busy ? tokens.color.ink3 : tokens.color.teal,
        fontFamily: tokens.font.mono,
        fontSize: 10,
        fontWeight: 600,
        cursor: busy ? 'wait' : 'pointer',
        opacity: dimmed ? 0.6 : 1,
    };
}

/** The status badge's colour: live while open, red when another view holds
 *  the session, quiet otherwise. */
export function statusColor(status: string): string {
    if (status === 'open') return tokens.color.teal;
    if (status === 'busy') return tokens.color.red;
    if (status === 'connecting' || status === 'reconnecting' || status === 'starting') return tokens.color.ink2;
    return tokens.color.ink3;
}

// ---------------------------------------------------------------------------
// Module-level session cache — keeps xterm + WebSocket alive across
// component unmount/remount so tab switches, fan-out toggles, and zoom
// transitions don't lose scrollback or drop the connection.
//
// Key: terminal_id. Value: CachedSession (below).
// The ``container`` is the xterm DOM root — when the component unmounts we
// re-parent it into a hidden graveyard div in document.body so xterm's
// internal renderer keeps painting (it computes dimensions off
// `getBoundingClientRect`, which still resolves on a detached-from-host but
// in-DOM element). On remount we move it back into the new hostRef.
//
// Sessions self-evict when their socket leaves 'open' for good — no point
// keeping a dead terminal in memory across the rest of the page lifetime.
// ---------------------------------------------------------------------------

// Final/busy close codes stay governed by the shared socket. Only the
// server's explicit backpressure close gets a renderer-local quiet retry.
type CachedStatus = 'connecting' | 'reconnecting' | 'open' | 'closed' | 'busy' | 'ended';

type CachedSession = {
    terminalId: string;
    term: Terminal;
    fit: FitAddon;
    sock: SessionSocket;
    container: HTMLDivElement;
    // The grid size we last *sent to the PTY* — NOT the size xterm currently
    // displays. Only advanced when a resize actually goes out over an open
    // socket, so the first resize after the socket opens always lands even if
    // a mount-time fit already computed the same dims (see fitAndSync).
    lastCols: number;
    lastRows: number;
    status: CachedStatus;
    statusListeners: Set<(s: CachedStatus) => void>;
    disposeOnData: { dispose: () => void };
    unsubTheme: (() => void) | null;
    // Per-mount visibility subscription (the cell scrolling on/off screen),
    // routed through the cached session like unsubTheme so the latest mount wins.
    unsubVisible: (() => void) | null;
};

const SESSION_CACHE: Map<string, CachedSession> = new Map();

function getGraveyard(): HTMLDivElement {
    const ID = '__collimate_terminal_graveyard__';
    let el = document.getElementById(ID) as HTMLDivElement | null;
    if (el) return el;
    el = document.createElement('div');
    el.id = ID;
    // Off-screen but in-DOM so xterm's renderer keeps a valid layout
    // and we don't lose its measured cell dimensions.
    el.style.position = 'fixed';
    el.style.left = '-99999px';
    el.style.top = '0';
    el.style.width = '1px';
    el.style.height = '1px';
    el.style.overflow = 'hidden';
    el.style.pointerEvents = 'none';
    document.body.appendChild(el);
    return el;
}

function notifyStatus(session: CachedSession) {
    for (const cb of Array.from(session.statusListeners)) {
        try { cb(session.status); } catch { /* listener disposed */ }
    }
}

/** Open a URL the program printed (an OAuth link, a docs pointer) where the
 *  user can actually use it. xterm's default is `window.open()`, which the
 *  desktop shell denies outright — and since the addon opens about:blank
 *  first and assigns the URL after, the shell never even learns which URL
 *  died. Native renderers get the shell's `desktop.openExternal` on the api
 *  surface; a plain browser tab has no such surface and window.open is the
 *  real thing there. */
function openLink(desktop: ReturnType<typeof useDesktop>, uri: string): void {
    if (desktop) {
        void desktop.openExternal(uri);
        return;
    }
    window.open(uri, '_blank', 'noopener');
}

/** What a server text frame writes into the terminal. PTY output always
 *  arrives as binary; a text frame is control JSON, and the server sends
 *  {"t":"error","msg":...} just before it closes the socket (the process
 *  already ended, docker missing, a spawn failure). That one renders as a
 *  red bracketed message; anything else is written through unchanged. */
export function textFrameOutput(s: string): string {
    try {
        const obj = JSON.parse(s);
        if (obj && typeof obj === 'object' && obj.t === 'error') {
            return `\r\n\x1b[31m[${String(obj.msg ?? 'terminal error')}]\x1b[0m\r\n`;
        }
    } catch { /* not JSON: plain text */ }
    return s;
}

export const BACKPRESSURE_NOTICE = '\r\n[input paused — reconnecting; pending input will not be replayed]\r\n';

/** Terminal-local policy, leaving the shared session API unchanged. */
export function openTerminalSocket(options: {
    onStatus?: (status: CachedStatus, event?: CloseEvent) => void;
    onRetry?: () => void;
    [key: string]: unknown;
}, open = openSessionSocket): SessionSocket {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let finished = false;
    let retrying = false;
    const cancel = () => {
        if (timer !== null) clearTimeout(timer);
        timer = null;
    };
    const socket: SessionSocket = open({
        ...options,
        transientRetryMs: false,
        busyRetryMs: false,
        onStatus: (status: CachedStatus, event?: CloseEvent) => {
            cancel();
            if (status === 'closed' && event?.code === 1013) {
                retrying = true;
                options.onStatus?.('reconnecting', event);
                if (finished) return;
                timer = setTimeout(() => {
                    timer = null;
                    if (finished) return;
                    options.onRetry?.();
                    socket.redial();
                }, 1000);
                return;
            }
            if (status !== 'connecting') retrying = false;
            options.onStatus?.(retrying ? 'reconnecting' : status, event);
        },
    });
    return {
        get status() { return socket.status; },
        send: (data) => socket.send(data),
        sendJson: (value) => socket.sendJson(value),
        socket: () => socket.socket(),
        redial() { cancel(); retrying = false; socket.redial(); },
        close() { finished = true; cancel(); socket.close(); },
    };
}

/** One xterm in its own container, parked in the graveyard until a mount
 *  moves the container into its host. */
function newTerm(api: unknown, desktop: ReturnType<typeof useDesktop>): {
    term: Terminal; fit: FitAddon; container: HTMLDivElement;
} {
    const container = document.createElement('div');
    container.style.width = '100%';
    container.style.height = '100%';
    container.style.background = tokens.color.codeBg;

    const fontSize = readFontSize();
    const term = new Terminal({
        cursorBlink: true,
        fontFamily: measurableFamily(document.fonts, readMonoFamily(), fontSize),
        fontSize,
        ...xtermOptionsFor(hostTheme(api)),
        convertEol: false,
        scrollback: 5000,
        allowProposedApi: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon((_event: MouseEvent, uri: string) => openLink(desktop, uri)));

    // Stash the container in the graveyard so xterm has a parent
    // when we call open(). We'll move it into the live hostRef
    // on mount.
    getGraveyard().appendChild(container);
    term.open(container);
    if (term.element) term.element.style.paddingLeft = `${TEXT_INSET_LEFT_PX}px`;
    return { term, fit, container };
}

// ---------------------------------------------------------------------------
// The starting terminal — the xterm a starting pointer mounts, keyed like the
// pending input (cell + path) so a remount keeps it. It has no socket: what
// is typed into it is queued (queueInput) for the first real session, which
// replaces it.
// ---------------------------------------------------------------------------

type StartingTerm = { term: Terminal; fit: FitAddon; container: HTMLDivElement; onData: { dispose: () => void } };

const STARTING_TERMS: Map<string, StartingTerm> = new Map();

function startingTerm(key: string, label: string, api: unknown, desktop: ReturnType<typeof useDesktop>): StartingTerm {
    const cached = STARTING_TERMS.get(key);
    if (cached) return cached;
    const { term, fit, container } = newTerm(api, desktop);
    term.write(`\x1b[2m[starting ${label || 'the terminal'} — what you type is sent when it is live]\x1b[0m\r\n`);
    const onData = term.onData((d: string) => {
        if (queueInput(key, d) === 'overflow') term.write(PENDING_OVERFLOW_NOTICE);
    });
    const starting = { term, fit, container, onData };
    STARTING_TERMS.set(key, starting);
    return starting;
}

function disposeStartingTerm(key: string): void {
    const starting = STARTING_TERMS.get(key);
    if (!starting) return;
    STARTING_TERMS.delete(key);
    try { starting.onData.dispose(); } catch {}
    try { starting.term.dispose(); } catch {}
    try { starting.container.remove(); } catch {}
}

function buildSession(
    parsed: Parsed, api: unknown, desktop: ReturnType<typeof useDesktop>, inputKey: string,
): CachedSession {
    const { term, fit, container } = newTerm(api, desktop);

    const encoder = new TextEncoder();
    // One-shot guard for the first-output repaint — see onMessage below.
    let firstPaint = false;
    // One-shot for the whole session (a 1013 retry does not reset it): the
    // first output frame is when input queued before the session existed
    // goes out.
    let outputSeen = false;
    const sendText = (d: string): boolean => session.sock.send(encoder.encode(d));

    const session: CachedSession = {
        terminalId: parsed.terminal_id,
        term,
        fit,
        sock: null as unknown as SessionSocket,  // assigned right below
        container,
        lastCols: 0,
        lastRows: 0,
        status: 'connecting',
        statusListeners: new Set(),
        disposeOnData: { dispose: () => {} },
        unsubTheme: null,
        unsubVisible: null,
    };

    // Only explicit stdin backpressure retries automatically. Every other
    // closed/busy/dead state retains the shared socket's terminal policy.
    session.sock = openTerminalSocket({
        url: sessionWsUrl(api, parsed.ws_path, parsed.token),
        connectionKey: parsed.terminal_id,
        binaryType: 'arraybuffer',
        onRetry: () => {
            // The server replays its bounded output ring. Start with a clean
            // terminal so old output is not duplicated; input is never queued.
            term.reset();
            firstPaint = false;
            session.lastCols = session.lastRows = 0;
        },
        onOpen: () => {
            // Push the real size now that the socket is up. By this point the
            // mount effect has re-parented the container into the live host, so
            // the box is measurable and this is the resize that takes the
            // program off the PTY's 24×80 default.
            fitAndSync(session);
            try { term.focus(); } catch {}
        },
        onMessage: (ev) => {
            if (ev.data instanceof ArrayBuffer) {
                term.write(new Uint8Array(ev.data));
            } else if (typeof ev.data === 'string') {
                term.write(textFrameOutput(ev.data));
            }
            // First output after connect. xterm was brought up while the
            // container was parked in the 1×1 graveyard, where its renderer
            // pauses, so the first render of fresh content can come up
            // unpainted until something forces a full repaint. Neither of the
            // other refresh paths (mount re-attach, fit) is guaranteed to run
            // *after* the program's first bytes have actually landed, so force
            // one here. Once is enough; the short follow-up covers a mount that
            // lays the box out just after this first batch.
            if (!firstPaint) {
                firstPaint = true;
                refreshTerm(session);
                setTimeout(() => refreshTerm(session), 250);
            }
            if (!outputSeen) {
                outputSeen = true;
                flushPendingInput(inputKey, sendText);
            }
        },
        onStatus: (st) => {
            const previous = session.status;
            session.status = st as CachedStatus;
            notifyStatus(session);
            if (st === 'reconnecting') {
                if (previous !== st) term.write(BACKPRESSURE_NOTICE);
            } else if (st === 'ended') {
                term.write('\r\n\x1b[33m[session ended — run the cell again to start a new terminal]\x1b[0m\r\n');
            } else if (st === 'busy') {
                term.write('\r\n\x1b[33m[already attached in another view — close it or hit Reconnect]\x1b[0m\r\n');
            } else if (st === 'closed') {
                term.write('\r\n\x1b[33m[connection closed]\x1b[0m\r\n');
            }
            // Backpressure keeps this cached session for its single retry.
            // Other final states are evicted; explicit Reconnect or a later
            // remount builds a fresh session.
            // A session parked in the graveyard has no mount showing it, so
            // it is torn down here; a mounted one is torn down by its mount's
            // cleanup, keeping the final message readable until then.
            if ((st === 'ended' || st === 'busy' || st === 'closed')
                && SESSION_CACHE.get(session.terminalId) === session) {
                SESSION_CACHE.delete(session.terminalId);
                if (session.container.parentElement === getGraveyard()) destroySession(session);
            }
        },
    });

    session.disposeOnData = term.onData((d: string) => {
        if (routeInput(inputKey, d, sendText) === 'overflow') term.write(PENDING_OVERFLOW_NOTICE);
    });

    void applyMonoFont(session);

    SESSION_CACHE.set(parsed.terminal_id, session);
    return session;
}

// Tear one session object down: its socket, xterm, DOM and host
// subscriptions. Works on an evicted session too (it is no longer in the
// cache, so a lookup by id would miss it).
function destroySession(session: CachedSession) {
    if (SESSION_CACHE.get(session.terminalId) === session) SESSION_CACHE.delete(session.terminalId);
    try { session.disposeOnData.dispose(); } catch {}
    if (session.unsubTheme) { try { session.unsubTheme(); } catch {} session.unsubTheme = null; }
    if (session.unsubVisible) { try { session.unsubVisible(); } catch {} session.unsubVisible = null; }
    try { session.sock.close(); } catch {}
    try { session.term.dispose(); } catch {}
    try { session.container.remove(); } catch {}
}

function disposeSession(terminalId: string) {
    const session = SESSION_CACHE.get(terminalId);
    if (session) destroySession(session);
}

// Re-measure the box and resize the PTY to match — the one place every fit
// path funnels through (WS open, ResizeObserver, mount). The two halves are
// deliberately decoupled:
//   * xterm's display grid is always refit to the box (fit.fit()), so the
//     rendered columns track the cell's width pixel-for-pixel; and
//   * the PTY winsize is only pushed when it actually differs from what we
//     last told the kernel, and lastCols/lastRows only advance when the send
//     actually goes out (sock.send is OPEN-guarded). That ordering is the fix for
//     the intermittent "stuck at 80×24" layout: a mount-time fit used to set
//     lastCols before the socket was open, so the onopen fit then saw an
//     unchanged size and never sent the real dimensions, leaving the program
//     rendering against the PTY's 24×80 default in a much larger cell.
function fitAndSync(session: CachedSession): void {
    // A box smaller than a cell can't be fit — during a zoom transition or
    // while parked in the graveyard the container measures 0–1px, and fitting
    // then would compute a 1-column grid and fire a bogus 1×1 resize at the
    // program. Wait for the ResizeObserver to fire again with a real box.
    const rect = session.container.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;
    try {
        session.fit.fit();
    } catch {
        return;
    }
    const cols = session.term.cols;
    const rows = session.term.rows;
    if (cols !== session.lastCols || rows !== session.lastRows) {
        // sock.send is OPEN-guarded and returns whether the frame went out —
        // only then advance last*, so a reconnect re-pushes the real size.
        if (session.sock.sendJson({ t: 'resize', cols, rows })) {
            session.lastCols = cols;
            session.lastRows = rows;
        }
    }
}

// Force xterm to repaint every visible row from its in-memory buffer. xterm
// draws with its DOM renderer (xterm 6 ships no canvas renderer; WebGL can
// lose its context in this shadow-DOM, GPU-composited scene), which pauses
// while the terminal is off screen: parked in the 1×1 graveyard, or hidden
// via an ancestor `display:none` (the frontend flips offscreen cells to
// display:none — see StackItem). A full-screen TUI only emits output on
// input or SIGWINCH, so a repaint missed while paused would leave the grid
// stale until a keystroke. Re-issuing refresh(0, rows-1) repaints the
// existing buffer with no PTY round-trip. Deferred to a rAF so the box has
// been laid out before we repaint.
function refreshTerm(session: CachedSession): void {
    requestAnimationFrame(() => {
        try {
            const rows = session.term.rows;
            if (rows > 0) session.term.refresh(0, rows - 1);
        } catch { /* term disposed mid-frame */ }
    });
}

/** The toolbar's action buttons for a session status: none once the session
 *  has ENDED (4404/4401 — nothing is left to take the action; Reconnect is
 *  the only way on), every declared one otherwise. */
export function toolbarButtons(buttons: ButtonSpec[], status: string): ButtonSpec[] {
    return status === 'ended' ? [] : buttons;
}

/** What a click on a toolbar button does: a button with a `confirm` asks it
 *  first ('confirm' — the toolbar swaps in "<confirm> [label] [Cancel]"),
 *  and the click on that confirm's own button emits ('emit'); a button with
 *  no confirm emits at once. `confirming` is the id being confirmed. */
export function clickStep(b: ButtonSpec, confirming: string | null): 'confirm' | 'emit' {
    return b.confirm && confirming !== b.id ? 'confirm' : 'emit';
}

/** A toolbar button's click: its id as a renderer action. False when the
 *  host did not take it (no active run, a failed request, no action
 *  channel), which the toolbar reports as "<label> failed". */
export async function emitButton(api: any, id: string): Promise<boolean> {
    if (!api || typeof api.emitAction !== 'function') return false;
    try {
        return !!(await api.emitAction(id));
    } catch {
        return false;
    }
}

function TerminalViewer() {
    const { content, api } = useCollimateFile();
    const desktop = useDesktop(api);
    const { onChange: onThemeChange } = useTheme(api);
    const { onChange: onVisibleChange } = useVisibility(api);
    const parsed = useMemo<Parsed | null>(() => parseContent(content), [content]);
    const starting = useMemo(() => (parsed ? null : parseStarting(content)), [parsed, content]);
    const inputKey = pendingInputKey(api);
    const hostRef = useRef<HTMLDivElement | null>(null);
    const [status, setStatus] = useState<CachedStatus | 'idle' | 'starting'>('idle');
    const [attempt, setAttempt] = useState(0);
    const [pendingAction, setPendingAction] = useState<string | null>(null);
    // The button whose `confirm` question is showing (clickStep).
    const [confirming, setConfirming] = useState<string | null>(null);
    const [actionStatus, setActionStatus] = useState<string>('');
    const [fontSize, setFontSize] = useState<number>(readFontSize);
    // The pending clear of actionStatus, so a later click's message is not
    // wiped early by an earlier click's timer.
    const statusTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    useEffect(() => () => { if (statusTimerRef.current) clearTimeout(statusTimerRef.current); }, []);

    // Step the shared font size and apply it live to the cached xterm: bump the
    // option, refit the grid to the cell box (which pushes a resize to the PTY),
    // then force a repaint so the new metrics land without waiting for output.
    const changeFontSize = (delta: number) => {
        const cur = readFontSize();
        const next = Math.max(FONT_SIZE_MIN, Math.min(FONT_SIZE_MAX, cur + delta));
        if (next === cur) return;
        writeFontSize(next);
        // Fan out to every terminal cell (incl. this one, via the listener
        // effect below) so the shared preference stays in lockstep; cross-tab is
        // covered by the 'storage' handler.
        broadcastFontSize(next);
    };

    // Live-apply the shared font size to this cell's cached xterm when ANY cell
    // changes it (same-page via broadcastFontSize, cross-tab via 'storage').
    useEffect(() => {
        const apply = (n: number) => {
            setFontSize(n);
            const session = parsed ? SESSION_CACHE.get(parsed.terminal_id) : undefined;
            if (session) {
                try { session.term.options.fontSize = n; } catch { /* term disposed */ }
                fitAndSync(session);
                refreshTerm(session);
            }
        };
        FONT_SIZE_LISTENERS.add(apply);
        const onStorage = (e: StorageEvent) => {
            if (e.key === FONT_SIZE_KEY) apply(readFontSize());
        };
        window.addEventListener('storage', onStorage);
        return () => {
            FONT_SIZE_LISTENERS.delete(apply);
            window.removeEventListener('storage', onStorage);
        };
    }, [parsed?.terminal_id]);

    const clickAction = async (b: ButtonSpec) => {
        if (pendingAction) return;
        if (clickStep(b, confirming) === 'confirm') {
            setConfirming(b.id);
            return;
        }
        const { id, label } = b;
        setConfirming(null);
        setPendingAction(id);
        if (statusTimerRef.current) { clearTimeout(statusTimerRef.current); statusTimerRef.current = null; }
        setActionStatus('');
        const ok = await emitButton(api, id);
        setActionStatus(ok ? `${label} sent` : `${label} failed`);
        setPendingAction(null);
        if (statusTimerRef.current) clearTimeout(statusTimerRef.current);
        statusTimerRef.current = setTimeout(() => { statusTimerRef.current = null; setActionStatus(''); }, 2500);
    };

    // The starting pointer: an xterm with no session yet, taking keystrokes
    // into the pending queue. The real pointer's session replaces it.
    useEffect(() => {
        if (!starting || !hostRef.current) return;
        api.loadCss('https://esm.sh/@xterm/xterm@6.0.0/css/xterm.css');
        const pre = startingTerm(inputKey, starting.label, api, desktop);
        const host = hostRef.current;
        host.appendChild(pre.container);
        setStatus('starting');
        let rafId = 0;
        const scheduleFit = () => {
            if (rafId) return;
            rafId = requestAnimationFrame(() => {
                rafId = 0;
                const r = pre.container.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) return;
                try { pre.fit.fit(); } catch {}
            });
        };
        const ro = new ResizeObserver(scheduleFit);
        ro.observe(host);
        scheduleFit();
        try { pre.term.focus(); } catch {}
        // The host releases the run's claim on this file when the run ends
        // without registering its terminal: what was typed will never be
        // sent, and the terminal says so rather than dropping it silently.
        let current = true;
        const offSurface = typeof api.onSurfaceChange === 'function' && typeof api.getSurface === 'function'
            ? api.onSurfaceChange(() => {
                Promise.resolve(api.getSurface()).then((surface: unknown) => {
                    if (!current || surface) return;
                    const bytes = dropPendingInput(inputKey);
                    if (bytes) pre.term.write(droppedInputNotice(bytes));
                }, () => { /* a failed lookup is not a release */ });
            })
            : null;
        return () => {
            current = false;
            try { offSurface && offSurface(); } catch {}
            if (rafId) cancelAnimationFrame(rafId);
            ro.disconnect();
            if (host.contains(pre.container)) {
                try { getGraveyard().appendChild(pre.container); } catch {}
            }
        };
    }, [starting !== null, inputKey]);

    useEffect(() => {
        if (!parsed || !hostRef.current) return;
        api.loadCss('https://esm.sh/@xterm/xterm@6.0.0/css/xterm.css');
        // The real session is here: the starting xterm has done its job (its
        // keystrokes wait in the pending queue for this session's first output).
        disposeStartingTerm(inputKey);

        // Reconnect button (Reconnect: bumps `attempt`) lets the user
        // explicitly recreate after a closed/errored socket. Kill the
        // cached session on a manual retry so buildSession reconstructs
        // a fresh WebSocket; the React effect cleanup below will then
        // re-attach the new session's container. An explicit click also
        // clears the dead-session marker: one deliberate dial, re-marked
        // if the server still rejects it.
        if (attempt > 0) {
            clearDeadSession(parsed.terminal_id);
            disposeSession(parsed.terminal_id);
        }

        let session = SESSION_CACHE.get(parsed.terminal_id);
        if (!session) {
            if (isSessionDead(parsed.terminal_id)) {
                // The server already told us this session is gone (4404/4401).
                // Don't redial on remount — that's the forever-retry loop.
                setStatus('ended');
                return;
            }
            session = buildSession(parsed, api, desktop, inputKey);
        }

        // Move the cached container into our host. document.adoptNode
        // isn't needed — appendChild on an in-DOM element relocates it
        // and preserves all internal state (event listeners, refs).
        // xterm's renderer recomputes layout via ResizeObserver below.
        const host = hostRef.current;
        host.appendChild(session.container);
        // Re-attaching from the graveyard (or un-hiding after the frontend
        // flipped this cell's subtree to display:none while offscreen) finds
        // the renderer paused. Force a repaint from the in-memory buffer so
        // the grid shows its current content immediately — a full-screen TUI
        // won't redraw on its own until the next keystroke/SIGWINCH.
        refreshTerm(session);

        // Status subscription. Surface the cached session's current
        // status immediately on mount so we don't show 'idle' for the
        // brief window before the first state change after remount.
        setStatus(session.status);
        const statusCb = (s: CachedStatus) => setStatus(s);
        session.statusListeners.add(statusCb);

        // Theme hot-swap is per-mount (each component mount has its
        // own `onThemeChange` subscription), but we route the new
        // listener through the cached session so the most recent
        // mount's subscriber wins. Replace any prior listener.
        if (session.unsubTheme) {
            try { session.unsubTheme(); } catch {}
        }
        session.unsubTheme = onThemeChange((theme: ThemeSnapshot) => {
            try {
                const next = xtermOptionsFor(theme);
                session!.term.options.theme = next.theme;
                session!.term.options.minimumContrastRatio = next.minimumContrastRatio;
            } catch {}
            // A skin can name another mono face.
            void applyMonoFont(session!);
        });

        // Visibility hot-swap — the first-class host signal for "this cell
        // scrolled back on screen." While offscreen the host display:none's
        // the subtree and xterm's renderer pauses; on return we repaint from
        // the in-memory buffer so the grid is current at once. Routed through
        // the cached session like the theme listener so the latest mount's
        // subscriber wins. (The ResizeObserver 0→box repaint below is a
        // belt-and-suspenders fallback for the same case; this is the
        // documented, first-class path.)
        if (session.unsubVisible) {
            try { session.unsubVisible(); } catch {}
        }
        session.unsubVisible = onVisibleChange((vis: boolean) => {
            if (vis) refreshTerm(session!);
        });

        // Coalesce resize work onto a single rAF. A cell drag fires the
        // ResizeObserver many times per frame; fitting synchronously inside
        // the RO callback both wastes layout passes and trips the
        // "ResizeObserver loop completed with undelivered notifications"
        // feedback — fit.fit() mutates the DOM (it resizes xterm's screen),
        // which re-notifies the very observer that called it. Deferring the
        // fit to a rAF runs it out-of-band, at most once per frame, which is
        // the documented way to avoid that loop and is the main cure for the
        // resize flicker. Each fit only pushes a SIGWINCH when the grid
        // actually crosses a row/column boundary, so a smooth drag reflows
        // the program live, exactly like dragging a native terminal's edge.
        let rafId = 0;
        // Tracks whether the box was measurable on the previous fit. The
        // frontend hides offscreen cells with `display:none` (StackItem), which
        // keeps this component mounted but collapses the box to 0×0 and pauses
        // the renderer. When the cell comes back the ResizeObserver fires
        // (0 → real box) but the PTY emits no output, so we repaint the
        // existing buffer ourselves rather than wait for the next keystroke.
        let wasMeasurable = false;
        const scheduleFit = () => {
            if (rafId) return;
            rafId = requestAnimationFrame(() => {
                rafId = 0;
                fitAndSync(session!);
                const r = session!.container.getBoundingClientRect();
                const measurable = r.width >= 2 && r.height >= 2;
                // Box just became measurable again (un-hidden / un-parked):
                // force a full repaint of the buffer.
                if (measurable && !wasMeasurable) refreshTerm(session!);
                wasMeasurable = measurable;
            });
        };
        const ro = new ResizeObserver(scheduleFit);
        ro.observe(host);
        scheduleFit();

        // Focus the terminal so keypresses land directly without an
        // explicit click — matches the prior behavior. Skip if the
        // socket isn't yet open (we'll focus from the onopen handler
        // in the build path on first connect).
        if (session.status === 'open') {
            try { session.term.focus(); } catch {}
        }

        return () => {
            if (rafId) cancelAnimationFrame(rafId);
            ro.disconnect();
            session!.statusListeners.delete(statusCb);
            // Another concurrent mount of the same session took the
            // container (the latest mount wins): it is that mount's to
            // park or destroy, not ours.
            if (!host.contains(session!.container)) return;
            // While the session is still cached, DO NOT dispose the
            // term/ws here — the whole point of the cache is that the
            // next mount re-attaches. Just park the container in the
            // graveyard so xterm keeps a valid in-DOM parent (its
            // renderer wants a layout-able ancestor for cell-size
            // measurement; an unparented div returns 0×0 from
            // getBoundingClientRect and the next remount's fit.fit()
            // would compute zero cols). A session evicted while shown
            // (ended/busy/closed) will never be re-attached: destroy it.
            if (SESSION_CACHE.get(session!.terminalId) === session) {
                try { getGraveyard().appendChild(session!.container); } catch {}
            } else {
                destroySession(session!);
            }
        };
    // attempt → forces effect re-run on Reconnect click
    }, [parsed?.terminal_id, parsed?.token, parsed?.ws_path, attempt]);

    if (!parsed && starting) {
        return (
            <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column', background: tokens.color.codeBg, boxSizing: 'border-box', minWidth: 0, overflow: 'hidden' }}>
                <div style={{ height: 24, background: tokens.color.sheet, borderBottom: `1px solid ${tokens.color.line}`, display: 'flex', alignItems: 'center', padding: '0 8px', gap: 8, flexShrink: 0, minWidth: 0, fontFamily: tokens.font.mono, fontSize: 11 }}>
                    <span title={starting.label} style={{ color: tokens.color.ink2, flex: '1 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{starting.label || 'terminal'}</span>
                    <span style={{ color: statusColor('starting'), flexShrink: 0, whiteSpace: 'nowrap' }}>● Starting…</span>
                </div>
                <div ref={hostRef} style={{ flex: 1, overflow: 'hidden', minWidth: 0, minHeight: 0, position: 'relative' }} />
            </div>
        );
    }

    if (!parsed) {
        return (
            <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', background: tokens.color.codeBg, color: tokens.color.ink3, fontFamily: tokens.font.mono, fontSize: 12, padding: 16, textAlign: 'center' }}>
                Invalid .terminal file — expected JSON with {`{ terminal_id, ws_path, token }`}.
            </div>
        );
    }

    return (
        <div style={{
            width: '100%',
            height: '100%',
            display: 'flex',
            flexDirection: 'column',
            background: tokens.color.codeBg,
            // Box-sizing: border-box so the host's reported width is the
            // outer width — matters for cell-width fitting; without it
            // any padding/border on a parent would push the terminal
            // wider than the cell.
            boxSizing: 'border-box',
            // Hard-cap to the parent's width. The cell can ask the
            // renderer for an exact pixel size; this prevents the
            // xterm screen from extending past the cell on a wider-than-
            // expected font fallback.
            minWidth: 0,
            overflow: 'hidden',
        }}>
            <div style={{ height: 24, background: tokens.color.sheet, borderBottom: `1px solid ${tokens.color.line}`, display: 'flex', alignItems: 'center', padding: '0 8px', gap: 8, flexShrink: 0, minWidth: 0, fontFamily: tokens.font.mono, fontSize: 11 }}>
                {/* The label is the one flexible item: it ellipsizes in a
                    narrow cell instead of overrunning the status badge and
                    the font controls (which never shrink). */}
                <span title={parsed.label} style={{ color: tokens.color.ink2, flex: '1 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{parsed.label}</span>
                <span style={{ color: statusColor(status), flexShrink: 0, whiteSpace: 'nowrap' }}>● {status}</span>
                <div style={{ display: 'flex', alignItems: 'center', gap: 2, flexShrink: 0 }} title="Terminal font size">
                    <button
                        onClick={() => changeFontSize(-1)}
                        disabled={fontSize <= FONT_SIZE_MIN}
                        title="Decrease font size"
                        style={{ ...FONT_BTN_STYLE, opacity: fontSize <= FONT_SIZE_MIN ? 0.4 : 1, cursor: fontSize <= FONT_SIZE_MIN ? 'default' : 'pointer' }}
                    >
                        A−
                    </button>
                    <span style={{ color: tokens.color.ink3, minWidth: 16, textAlign: 'center' }}>{fontSize}</span>
                    <button
                        onClick={() => changeFontSize(1)}
                        disabled={fontSize >= FONT_SIZE_MAX}
                        title="Increase font size"
                        style={{ ...FONT_BTN_STYLE, opacity: fontSize >= FONT_SIZE_MAX ? 0.4 : 1, cursor: fontSize >= FONT_SIZE_MAX ? 'default' : 'pointer' }}
                    >
                        A+
                    </button>
                </div>
                {actionStatus && (
                    <span style={{ color: actionStatus.endsWith(' failed') ? tokens.color.red : tokens.color.teal }}>{actionStatus}</span>
                )}
                <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6 }}>
                    {toolbarButtons(parsed.buttons, status).map(b => {
                        const busy = pendingAction === b.id;
                        const btn = (
                            <button
                                key={b.id}
                                onClick={() => void clickAction(b)}
                                disabled={busy || !!pendingAction}
                                title={b.title}
                                style={ctaStyle(busy, !!pendingAction && !busy)}
                            >
                                {busy ? '…' : b.label}
                            </button>
                        );
                        if (confirming !== b.id) return btn;
                        // Inline confirm: the question, the button itself
                        // (a second click emits), and Cancel.
                        return (
                            <span key={b.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                                <span style={{ color: tokens.color.red, whiteSpace: 'nowrap' }}>{b.confirm}</span>
                                {btn}
                                <button
                                    onClick={() => setConfirming(null)}
                                    style={TOOL_BTN_STYLE}
                                >
                                    Cancel
                                </button>
                            </span>
                        );
                    })}
                    {(status === 'closed' || status === 'busy' || status === 'ended') && (
                        <button
                            onClick={() => setAttempt(a => a + 1)}
                            style={TOOL_BTN_STYLE}
                        >
                            Reconnect
                        </button>
                    )}
                </div>
            </div>
            <div
                ref={hostRef}
                style={{
                    flex: 1,
                    // No padding here — xterm wants the full container
                    // box for accurate cell-size measurement, and the
                    // padding was eating ~1 column off the right side
                    // at narrow cell widths.
                    overflow: 'hidden',
                    minWidth: 0,
                    minHeight: 0,
                    position: 'relative',
                }}
            >
                {/* An overlay, not a flex sibling: a session that ends while
                    shown keeps its xterm container in this host underneath
                    (until the mount's cleanup destroys it). */}
                {status === 'ended' && (
                    <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', background: tokens.color.codeBg, zIndex: 1 }}>
                        <span style={{ color: tokens.color.ink3, fontFamily: tokens.font.mono, fontSize: 12, padding: 16, textAlign: 'center' }}>
                            Terminal session ended — run the cell again to start a new one.
                        </span>
                    </div>
                )}
            </div>
        </div>
    );
}

export default defineRenderer({ component: TerminalViewer });
