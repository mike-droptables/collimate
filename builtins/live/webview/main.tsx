import React, { useEffect, useMemo, useRef, useState } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, tokens, useCollimateFile } from '/api/sdk/react.js';
import { apiOrigin, openSessionSocket, sessionWsUrl } from '/api/sdk/session.js';

// ===========================================================================
// The artifact — a `tab-<n>.webview` file, v4
// ===========================================================================
//
// One construction site on the generator side (_web_shared/protocol.py's
// `_tab_artifact`), one parser here. Each file is ONE tab of a web run; a run
// has one or more, all in its run folder:
//
//   {"v": 4, "tab",                          // "tab-<n>", named in the hello
//    "url",                                  // "" when no site was named
//    "device",                               // desktop | phone | tablet
//    "viewport": {"width"} | null,           // the letterbox; null = full width
//    "control": {"wsBase"},                  // the control WebSocket's base
//    "requires": ["nativeGuests"],           // unconditional
//    "native": {"partition","device"},       // the mountGuest opts
//    "buttons": [{"id","label"}]}            // e.g. Capture, New tab
//
// Before the run's daemon exists the host writes the first tab as a starting
// placeholder, `{"starting": true, "label"}` (collimate.pointers
// .starting_pointer): the tab shows its chrome disabled with "Starting…" until
// the daemon writes the real artifact over it.
//
// The site is optional: a tab opened without one is still a whole tab — the
// guest opens blank, the canvas shows an Enter page form until it is on a
// page, and that form or the address bar is how the user names one. Only
// `control.wsBase` and `tab` decide whether a session exists at all.
//
// There is one way a tab is fully live: the CLIENT hosts the page in its own
// native guest view and this renderer answers the daemon's RPC out of it. No
// browser on the server — which is why `requires` is unconditional. A client
// without a native view (a browser tab) gets a best-effort iframe fallback:
// the page itself where the site allows framing, an address bar that still
// navigates, and honest rpc_error refusals for the capture verbs only a
// native view can serve.

export type ArtifactButton = { id: string; label: string };

/** What a tab can present as — the generator's `DEVICES` keys, in the
 *  toolbar toggle's order. */
export const DEVICES = ['desktop', 'phone', 'tablet'] as const;
export type Device = typeof DEVICES[number];

/** The toggle's labels. */
export const DEVICE_LABELS: Record<Device, string> = {
    desktop: 'Desktop', phone: 'Phone', tablet: 'Tablet',
};

const isDevice = (v: unknown): v is Device =>
    typeof v === 'string' && (DEVICES as readonly string[]).includes(v);

export type Artifact = {
    v: number;
    /** The tab this file is (`tab-<n>`): said in the control socket's hello,
     *  and part of the guest's session key. */
    tab: string;
    /** The site the tab opens on, or `''` when the user named none — the
     *  guest starts blank and the address bar does the naming. */
    url: string;
    /** `control.wsBase` — the host-relative base the control WebSocket
     *  (`<wsBase>__collimate/ws`) resolves off; shared by the run's tabs. */
    wsBase: string;
    /** Client capabilities the session cannot run without. */
    requires: string[];
    /** `native.partition` — the persistent login bucket the guest mounts in. */
    partition: string;
    /** What the tab presents as; the shell composes its user agent and
     *  mobile emulation from it (`native.device`). */
    device: Device;
    /** `viewport.width` — the width the guest is letterboxed to, centred on
     *  the canvas; null for the cell's full width. */
    viewportWidth: number | null;
    buttons: ArtifactButton[];
};

/** What an absent / unparseable / wrong-version artifact reads as: no control
 *  base, which is what puts the "waiting for the generator" panel on screen. */
export const EMPTY_ARTIFACT: Artifact = {
    v: 0, tab: '', url: '', wsBase: '', requires: [],
    partition: '', device: 'desktop', viewportWidth: null, buttons: [],
};

const asStr = (v: unknown): string => (typeof v === 'string' ? v.trim() : '');
const asObj = (v: unknown): any => (v && typeof v === 'object' && !Array.isArray(v) ? v : null);

export function parseArtifact(raw: string): Artifact {
    let doc: any;
    try { doc = JSON.parse((raw || '').trim()); } catch { return EMPTY_ARTIFACT; }
    const j = asObj(doc);
    if (!j) return EMPTY_ARTIFACT;
    // A `v` that isn't 4 means the generator and this renderer shipped out of
    // sync, and the honest render for that is the waiting panel.
    if (j.v !== 4) return EMPTY_ARTIFACT;

    const control = asObj(j.control);
    const tab = asStr(j.tab);
    const wsBase = asStr(control?.wsBase);
    // A tab is addressed by (wsBase, tab): without both there is no socket
    // this file could say hello on.
    if (!tab || !wsBase) return EMPTY_ARTIFACT;
    // `native` stays nested on the wire — it is the mountGuest opts grouping
    // — and is flattened here, because nothing else reads it.
    const native = asObj(j.native);
    // The tab's `device` is the one the toggle shows; `native.device` is the
    // same name for the shell. An unknown one is desktop.
    const device: Device = isDevice(j.device) ? j.device : 'desktop';
    const width = asObj(j.viewport)?.width;

    return {
        v: 4,
        tab,
        url: asStr(j.url),
        wsBase,
        requires: Array.isArray(j.requires)
            ? j.requires.filter((r: unknown): r is string => typeof r === 'string' && !!r)
            : [],
        partition: asStr(native?.partition),
        device,
        viewportWidth: typeof width === 'number' && Number.isFinite(width) && width > 0
            ? Math.round(width) : null,
        buttons: Array.isArray(j.buttons)
            ? j.buttons
                .filter((a: any) => a && typeof a.id === 'string' && typeof a.label === 'string' && a.label)
                .map((a: any) => ({ id: a.id, label: a.label }))
            : [],
    };
}

/** The host's starting placeholder (`{"starting": true, "label"}`) as the
 *  runner's name, or null for anything else — a real artifact included. */
export function startingLabelOf(raw: string): string | null {
    let doc: any;
    try { doc = JSON.parse((raw || '').trim()); } catch { return null; }
    const j = asObj(doc);
    if (!j || j.starting !== true) return null;
    return asStr(j.label);
}

/** The guest's session key: the run's control base plus the tab. A re-run is
 *  a new wsBase (it carries the run id) and a second tab of the same run a
 *  new tab, so each opens its own page instead of reattaching another's
 *  parked guest; a remount of the same tab reattaches its live page. */
export function guestSessionKeyOf(artifact: Artifact): string {
    return `${artifact.wsBase}#${artifact.tab}`;
}

/** The control socket's first frame: which tab this socket serves, what kind
 *  of client it is, and its capabilities (camelCase, verbatim off
 *  api.desktop — the daemon snake_cases them). */
export function helloFrame(
    tab: string, client: string, caps: Record<string, unknown> | null | undefined,
): { kind: 'hello'; tab: string; client: string; caps: Record<string, unknown> } {
    return { kind: 'hello', tab, client, caps: caps ?? {} };
}

/** The `requires` gate: which of the artifact's required capabilities this
 *  client does not have. `caps` is a Partial — a browser tab has no
 *  capability map at all, so read every flag as falsy-means-no (a truthiness
 *  test rather than `=== true`, which an explicit `false` must also fail). */
export function unmetRequirements(
    requires: string[],
    caps: Record<string, unknown> | null | undefined,
): string[] {
    return (requires || []).filter((name) => !(caps && caps[name]));
}

// ===========================================================================
// The desktop shell surface (`api.desktop`)
// ===========================================================================
//
// Declared structurally because the renderer cannot import app-host types. Undefined in a browser tab and in an
// iframe-embedded renderer — which IS the feature test.

/** Exactly the events this renderer subscribes — the shell emits more. */
type GuestEventData = {
    navigate: { url: string; inPage: boolean };
    title: { title: string };
    crashed: { reason: string; exitCode: number };
};

type GuestHandle = {
    loadURL(url: string): Promise<void>;
    goBack(): Promise<boolean>;
    goForward(): Promise<boolean>;
    reload(): Promise<void>;
    evaluate(js: string): Promise<unknown>;
    capturePng(): Promise<ArrayBuffer>;
    currentUrl(): Promise<string>;
    on<K extends keyof GuestEventData>(kind: K, cb: (data: GuestEventData[K]) => void): () => void;
    setInteractive(interactive: boolean): void;
    /** Switch the device in place: same guest, same session, page reloaded. */
    setDevice(device: Device): Promise<void>;
    destroy(): void;
};

type DesktopSurface = {
    capabilities: Record<string, boolean | undefined>;
    openExternal(url: string): Promise<void>;
    mountGuest?(hostEl: HTMLElement, opts: {
        src: string; partition?: string; device?: Device;
        keepAlive?: boolean; interactive?: boolean;
        /** Names the guest's logical session: a mount whose key matches the
         *  parked guest's reattaches it, a different one replaces it. */
        guestSessionKey?: string;
    }): GuestHandle;
};

const desktopOf = (api: unknown): DesktopSurface | undefined =>
    (api as { desktop?: DesktopSurface } | null | undefined)?.desktop;

// ===========================================================================
// The RPC — daemon → this client's guest, over the control WS
// ===========================================================================
//
// Symmetric JSON-RPC over the control socket (the shapes live in
// _web_shared/protocol.py):
//
//   down  {"kind":"rpc","id","method":evaluate|html|screenshot|url|navigate,"params"}
//   up    {"kind":"rpc_result","id","value"} | {"kind":"rpc_error","id","message"}
//   up    {"kind":"hello","tab","client":"electron","caps":{…}}   (on connect)
//   up    {"kind":"url","url"}                                    (guest navigated)
//
// The mapping is deliberately a pure function over an INJECTED guest so it
// can be exercised without a shell (see artifact.test.ts): everything
// stateful — which handle is mounted, which socket to answer on — is the
// caller's, and `answerRpc` only turns one message into one reply.

export type RpcReply =
    | { kind: 'rpc_result'; id: string; value: unknown }
    | { kind: 'rpc_error'; id: string; message: string };

/** The five verbs the RPC needs, as the renderer can serve them. */
export type RpcGuest = {
    evaluate(js: string): Promise<unknown>;
    html(): Promise<string>;
    /** base64 PNG, no `data:` prefix. */
    screenshot(): Promise<string>;
    url(): Promise<string>;
    navigate(url: string): Promise<void>;
};

/** `html` has no verb of its own on a GuestHandle, so it is one evaluate of
 *  the serialised document — in the daemon's `_evaluate_expression` shape (a
 *  zero-arg function source, invoked). */
export const HTML_EXPRESSION = '(() => document.documentElement.outerHTML)()';

/** One `kind:"rpc"` message in, one reply out (null for anything else).
 *  Never throws: a guest that
 *  refuses becomes an `rpc_error`, because the caller is a future on the
 *  other end that would otherwise hang until its timeout. */
export async function answerRpc(msg: any, guest: RpcGuest): Promise<RpcReply | null> {
    if (!msg || typeof msg !== 'object' || msg.kind !== 'rpc') return null;
    const id = String(msg.id ?? '');
    const method = String(msg.method ?? '');
    const params = (msg.params && typeof msg.params === 'object') ? msg.params : {};
    try {
        switch (method) {
            case 'evaluate':
                // params.js is a COMPLETE expression, already wrapped by the
                // daemon — it goes to the guest verbatim.
                return { kind: 'rpc_result', id, value: await guest.evaluate(String(params.js ?? '')) };
            case 'html':
                return { kind: 'rpc_result', id, value: await guest.html() };
            case 'screenshot':
                return { kind: 'rpc_result', id, value: await guest.screenshot() };
            case 'url':
                return { kind: 'rpc_result', id, value: await guest.url() };
            case 'navigate':
                await guest.navigate(String(params.url ?? ''));
                return { kind: 'rpc_result', id, value: null };
            default:
                return { kind: 'rpc_error', id, message: `unknown rpc method '${method}'` };
        }
    } catch (err: any) {
        return {
            kind: 'rpc_error', id,
            message: String((err && err.message) || err || 'the guest refused the call'),
        };
    }
}

/** Raw bytes → base64 with no `data:` prefix, which is what `screenshot`
 *  returns (the wire is JSON, so the PNG has to ride encoded). Chunked:
 *  `String.fromCharCode(...bytes)` on a whole PNG overruns the argument
 *  limit on anything but a tiny capture. */
export function bytesToBase64(buf: ArrayBuffer | Uint8Array): string {
    const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    const native = (bytes as Uint8Array & {toBase64?: () => string}).toBase64;
    if (typeof native === 'function') return native.call(bytes);
    const CHUNK = 0x8000;
    let binary = '';
    for (let i = 0; i < bytes.length; i += CHUNK) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK) as unknown as number[]);
    }
    return btoa(binary);
}

/** The RPC's guest, served out of a mounted guest view. A null handle (the
 *  cell is open but the guest hasn't been created, or has been destroyed) refuses
 *  every call, which `answerRpc` turns into an `rpc_error` the capture
 *  reports as unavailable. */
export function guestFromHandle(handle: GuestHandle | null | undefined): RpcGuest {
    const need = (): GuestHandle => {
        if (!handle) throw new Error('no guest is mounted in this cell');
        return handle;
    };
    return {
        evaluate: async (js) => need().evaluate(js),
        html: async () => String((await need().evaluate(HTML_EXPRESSION)) ?? ''),
        screenshot: async () => bytesToBase64(await need().capturePng()),
        url: async () => String((await need().currentUrl()) ?? ''),
        navigate: async (url) => { await need().loadURL(url); },
    };
}

// ===========================================================================
// Shared bits
// ===========================================================================

// control.wsBase is host-relative (`/proxy/<ch>/<run>/<port>/`); sessionWsUrl
// resolves it against the API origin (split-origin aware — /proxy is served by
// the backend, not necessarily the frontend origin) and swaps http(s) → ws(s).
// The control channel lives under the reserved /__collimate/ prefix so it
// never collides with anything else served off that proxied port.
const CONTROL_WS_SUFFIX = '__collimate/ws';

/** A hand-typed address-bar entry as a navigable URL. Only `scheme://` (or
 *  `about:`) counts as already schemed — a bare `host:port` is not a scheme —
 *  and anything else defaults to https, as _web_shared/protocol.py's
 *  `_normalise_url` does. */
export const withScheme = (target: string): string =>
    (/^[a-z][a-z0-9+.-]*:\/\//i.test(target) || /^about:/i.test(target) ? target : 'https://' + target);

/** Where a guest opens when the tab named no site. */
export const BLANK_PAGE = 'about:blank';

// Host names that reach this machine's loopback; any of them on the app
// server's port is the app server (its origin guard treats them as one).
const LOOPBACK_NAMES = new Set(['localhost', '127.0.0.1', '[::1]']);

const portOf = (u: URL): string => u.port || (u.protocol === 'https:' ? '443' : '80');

/** Whether the browser fallback may hand `src` to its frame. The frame keeps
 *  `allow-same-origin` so a framed site keeps its own cookies and storage; a
 *  document on the app's origin in that frame could lift its own sandbox and
 *  script the app. So the URL the frame is given must be an absolute http(s)
 *  URL that is not the app server under any loopback name: a relative path, a
 *  `/proxy` page, the app itself, and `javascript:` / `about:` / `data:` /
 *  `blob:` URLs are refused. This checks only that URL. The framed site can
 *  still navigate or redirect its own frame onto the app's origin (a
 *  run-served `/proxy` page included), and that document then runs as the
 *  app; nothing in a renderer can stop it. `appOrigins` are the page's and
 *  the API's origins (they differ in a split-origin setup). */
export function frameableUrl(src: string, appOrigins: string[]): boolean {
    let u: URL;
    try {
        u = new URL(src);
    } catch {
        return false;
    }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
    for (const origin of appOrigins) {
        let app: URL;
        try {
            app = new URL(origin);
        } catch {
            continue;
        }
        if (u.origin === app.origin) return false;
        if (LOOPBACK_NAMES.has(u.hostname) && LOOPBACK_NAMES.has(app.hostname)
            && portOf(u) === portOf(app)) return false;
    }
    return true;
}

/** What the browser fallback's pane shows for `frameSrc`: the Enter page form
 *  while no site is named (src="" would load the app's own origin), the page
 *  in the sandboxed frame when `frameableUrl` allows its URL, and a notice
 *  otherwise. */
export function fallbackPane(frameSrc: string, appOrigins: string[]): 'enter' | 'frame' | 'notice' {
    if (!frameSrc) return 'enter';
    return frameableUrl(frameSrc, appOrigins) ? 'frame' : 'notice';
}

/** A guest's real URL as the address bar shows it. The blank page is how
 *  "started without a site" is spelled to the guest, not an address anyone
 *  typed — showing it would bury the field's placeholder under a string the
 *  user then has to clear. */
export const displayUrl = (u: string): string => (u === BLANK_PAGE ? '' : u);

/** True while a tab shows no page — it opened without a site and nothing has
 *  been entered yet. The canvas then holds the Enter page form, with the
 *  native host collapsed so the blank guest is not what shows. */
export const isBlankPage = (u: string): boolean => displayUrl(u) === '';

type Status =
    | { kind: 'idle' }
    | { kind: 'starting' }
    | { kind: 'connecting' }
    | { kind: 'live' }
    | { kind: 'closed'; reason: string };

/** Maps the session socket's status onto the dot. */
const statusFromSocket = (st: string): Status =>
    st === 'open' ? { kind: 'live' } :
    st === 'connecting' ? { kind: 'connecting' } :
    st === 'ended' ? { kind: 'closed', reason: 'session ended' } :
    { kind: 'closed', reason: 'disconnected — retrying' };

// ===========================================================================
// The chrome's look
// ===========================================================================
//
// Every colour, font and radius is a skin token (`tokens` from the SDK), so
// the toolbar, the Enter page form and the notices follow a skin change with
// no subscription. The page in the guest (or the fallback frame) is the
// site's own and is never themed. A native renderer runs in a shadow root,
// so styles are inline objects; the few pseudo-class rules a style object
// cannot carry go in through api.injectCss (CHROME_CSS).

type Style = React.CSSProperties;

const c = tokens.color;
/** The mock's radii, stepped down from the skin's --radius. */
const RADIUS_BUTTON = `calc(${tokens.radius} - 3px)`;
const RADIUS_FIELD = `calc(${tokens.radius} - 2px)`;
const RADIUS_TOOL = `calc(${tokens.radius} - 4px)`;

/** Rules a style object cannot carry: the fields' placeholder and the
 *  keyboard focus ring (teal, the on-state). */
export const CHROME_CSS = [
    `input::placeholder { color: ${c.ink3}; }`,
    `button:focus-visible { outline: 2px solid ${c.teal}; outline-offset: 2px; }`,
].join('\n');

/** The status dot: teal while the control socket is live (the on-state),
 *  red once it has closed, quiet while it connects. */
export const statusColorOf = (status: Status): string =>
    status.kind === 'live' ? c.teal :
    status.kind === 'closed' ? c.red :
    c.ink3;

/** The renderer's ground, behind the toolbar and the page box. */
export const groundStyle: Style = {
    width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
    position: 'relative', background: c.sheet, color: c.ink, fontFamily: tokens.font.ui,
};

/** The address toolbar: a 36px raised bar under the cell's tabs. */
export const toolbarStyle: Style = {
    height: 36, boxSizing: 'border-box', flexShrink: 0,
    display: 'flex', alignItems: 'center', gap: 6, padding: '0 8px',
    background: c.sheet2, borderBottom: `1px solid ${c.line}`,
    fontFamily: tokens.font.ui, fontSize: 12.5, color: c.ink2,
};

/** An address field: a mono address on the sheet, its edge darkening while
 *  it has focus. `height` is 26 in the toolbar, 30 in the Enter page form. */
export function addressStyle(focused: boolean, height: 26 | 30 = 26): Style {
    return {
        flex: 1, minWidth: height === 26 ? 60 : 0, height, boxSizing: 'border-box', padding: '0 10px',
        background: c.sheet, color: c.ink,
        border: `1px solid ${focused ? c.line2 : c.line}`, borderRadius: RADIUS_FIELD,
        fontFamily: tokens.font.mono, fontSize: height === 26 ? 12 : 12.5, outline: 'none',
    };
}

/** A chrome button. `tool` is a borderless icon tool (back, forward,
 *  reload); `quiet` the bordered secondary button (a generator's action,
 *  unfilled so the address bar stays the toolbar's focal point); `primary`
 *  the one action of a panel (Open, Reload), on the sheet in ink. `busy` is
 *  the one that was pressed. */
export function buttonStyle(
    kind: 'tool' | 'quiet' | 'primary',
    state: { hover?: boolean; busy?: boolean; height?: number } = {},
): Style {
    const { hover = false, busy = false } = state;
    const height = state.height ?? 24;
    const base: Style = {
        flexShrink: 0, height, boxSizing: 'border-box',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
        fontFamily: tokens.font.ui, fontSize: 12.5, lineHeight: 1, whiteSpace: 'nowrap',
        cursor: busy ? 'wait' : 'pointer', opacity: busy ? 0.45 : 1,
    };
    const lit = hover && !busy;
    if (kind === 'tool') {
        return {
            ...base, width: height, padding: 0,
            background: lit ? c.sheet3 : 'transparent', color: lit ? c.ink : c.ink3,
            border: 'none', borderRadius: RADIUS_TOOL, fontSize: 13,
        };
    }
    if (kind === 'quiet') {
        return {
            ...base, padding: '0 9px',
            background: lit ? c.sheet3 : 'transparent', color: lit ? c.ink : c.ink2,
            border: `1px solid ${c.line2}`, borderRadius: RADIUS_BUTTON,
        };
    }
    return {
        ...base, padding: '0 12px',
        background: lit ? c.sheet3 : c.sheet, color: c.ink, fontWeight: 600,
        border: `1px solid ${c.line2}`, boxShadow: `inset 0 0 0 0.5px ${c.line}`,
        borderRadius: RADIUS_BUTTON,
    };
}

/** The strip under the toolbar that says what the browser fallback cannot do. */
export const fallbackBannerStyle: Style = {
    flexShrink: 0, padding: '4px 10px',
    background: c.sheet, borderBottom: `1px solid ${c.line}`,
    color: c.ink3, fontFamily: tokens.font.ui, fontSize: 11.5, lineHeight: 1.45,
};

/** A refused navigation, under the toolbar: red on its soft fill. */
export const navigationErrorStyle: Style = {
    flexShrink: 0, padding: '6px 10px',
    background: c.redSoft, borderBottom: `1px solid ${c.line}`,
    color: c.red, fontFamily: tokens.font.ui, fontSize: 12,
};

/** The page box (the guest host, the fallback frame). Its ground is the
 *  sheet, which shows only while a page is blank or still painting. */
export const pageBoxStyle: Style = {
    flex: 1, minHeight: 0, position: 'relative', background: c.sheet,
};

/** The page box for a tab's viewport: the sheet at full width, the canvas
 *  around a letterboxed device. */
export function pageBoxStyleFor(viewportWidth: number | null): Style {
    return viewportWidth == null ? pageBoxStyle : { ...pageBoxStyle, background: c.canvas };
}

/** Where the page sits in the page box: the whole box for desktop, a column
 *  `viewportWidth` wide centred on the canvas for a phone or tablet (never
 *  wider than the box). The guest follows this element's bounds, so the
 *  letterbox needs nothing from the shell. */
export function letterboxStyle(viewportWidth: number | null): Style {
    if (viewportWidth == null) return { position: 'absolute', inset: 0 };
    return {
        position: 'absolute', top: 0, bottom: 0, left: 0, right: 0,
        width: viewportWidth, maxWidth: '100%', margin: '0 auto',
        background: c.sheet, borderLeft: `1px solid ${c.line}`, borderRight: `1px solid ${c.line}`,
        boxSizing: 'border-box',
    };
}

/** The device toggle: three segments in one bordered group. */
export const deviceGroupStyle: Style = {
    flexShrink: 0, height: 24, boxSizing: 'border-box', display: 'inline-flex',
    border: `1px solid ${c.line2}`, borderRadius: RADIUS_BUTTON, overflow: 'hidden',
};

/** One segment of the device toggle: the device the tab shows is filled on
 *  the sheet in ink, the others quiet. */
export function deviceSegmentStyle(
    active: boolean, state: { hover?: boolean; disabled?: boolean } = {},
): Style {
    const { hover = false, disabled = false } = state;
    const lit = hover && !disabled && !active;
    return {
        height: '100%', boxSizing: 'border-box', padding: '0 8px',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        fontFamily: tokens.font.ui, fontSize: 12, lineHeight: 1, whiteSpace: 'nowrap',
        border: 'none', borderRadius: 0,
        background: active ? c.sheet : lit ? c.sheet3 : 'transparent',
        color: active ? c.ink : lit ? c.ink : c.ink3,
        fontWeight: active ? 600 : 400,
        cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.45 : 1,
    };
}

/** A device toggle press: the `device` action, which the daemon answers by
 *  rewriting this tab's file with the device picked (the host stamps the
 *  action with this tab's file as its source). */
export function emitDevice(api: any, device: Device): Promise<boolean> {
    if (!api?.emitAction || !isDevice(device)) return Promise.resolve(false);
    return api.emitAction('device', { device });
}

/** The fallback frame: the site's page, in a box on the sheet. */
export const frameStyle: Style = {
    flex: 1, minHeight: 0, width: '100%', border: 'none', background: c.sheet,
};

/** The Enter page form: a card on the page box's sheet. */
export const enterCardStyle: Style = {
    width: '100%', maxWidth: 440, boxSizing: 'border-box',
    display: 'flex', flexDirection: 'column', gap: 12, padding: '18px 20px',
    background: c.sheet, color: c.ink,
    border: `1px solid ${c.line2}`, borderRadius: tokens.radius, boxShadow: tokens.shadow,
    fontFamily: tokens.font.ui,
};

/** The quiet centred panels: the waiting notice, the address notice, the
 *  crashed page. */
export const panelStyle: Style = {
    width: '100%', height: '100%', boxSizing: 'border-box',
    display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
    gap: 12, padding: 24, textAlign: 'center',
    background: c.sheet, color: c.ink3, fontFamily: tokens.font.ui, fontSize: 13,
};

/** A button that tracks its own hover (a style object has no :hover). */
function ChromeButton(props: {
    kind: 'tool' | 'quiet' | 'primary'; busy?: boolean; height?: number;
    type?: 'button' | 'submit'; title?: string; disabled?: boolean;
    onClick?: () => void; onMouseDown?: (e: React.MouseEvent) => void;
    children: React.ReactNode;
}) {
    const [hover, setHover] = useState(false);
    return (
        <button
            type={props.type ?? 'button'}
            title={props.title}
            aria-label={props.kind === 'tool' ? props.title : undefined}
            disabled={props.disabled}
            onClick={props.onClick}
            onMouseDown={props.onMouseDown}
            onMouseEnter={() => setHover(true)}
            onMouseLeave={() => setHover(false)}
            style={{
                ...buttonStyle(props.kind, { hover: hover && !props.disabled, busy: props.busy, height: props.height }),
                ...(props.disabled && !props.busy ? { opacity: 0.45, cursor: 'default' } : {}),
            }}
        >
            {props.children}
        </button>
    );
}

/** The renderer's empty / notice states: one centred panel, inline styles
 *  (shadow DOM: no stylesheet reaches in here). */
function Notice({ detail }: { detail?: string }) {
    return (
        <div style={panelStyle}>
            {detail && (
                <div style={{ fontSize: 12.5, lineHeight: 1.6, maxWidth: 440 }}>{detail}</div>
            )}
        </div>
    );
}

/** The action button's one behaviour: fire it at the generator and hold the
 *  button for a beat. The host stamps the action with this tab's file as its
 *  source, which is how the daemon knows which tab to act on. The generator
 *  answers by writing files into THIS cell — one `<site>-<timestamp>` folder
 *  per capture — not by mutating this file, so there is nothing to wait for
 *  beyond debouncing a double-click. */
function useActions(api: any) {
    const [busy, setBusy] = useState<string | null>(null);
    const fire = async (actionId: string) => {
        if (!api?.emitAction || busy) return;
        setBusy(actionId);
        try {
            await api.emitAction(actionId);
        } finally {
            setTimeout(() => setBusy(null), 1000);
        }
    };
    return { busy, fire };
}

/** The device toggle's state: the device the tab shows, or the one just
 *  picked while its action is on the way (the daemon's rewrite of the file
 *  is what settles it; a refused press drops the pick). */
function useDevicePick(api: any, device: Device): [Device, (next: Device) => void] {
    const [picked, setPicked] = useState<Device | null>(null);
    useEffect(() => { setPicked(null); }, [device]);
    const pick = (next: Device) => {
        setPicked(next);
        emitDevice(api, next).then(
            (ok) => { if (!ok) setPicked(null); },
            () => setPicked(null),
        );
    };
    return [picked ?? device, pick];
}

type ToolbarProps = {
    status: Status;
    urlInput: string;
    setUrlInput: (next: string) => void;
    onFocusUrl: () => void;
    onBlurUrl: () => void;
    onCommitUrl: () => void;
    onNavigate: (action: 'back' | 'forward' | 'reload') => void;
    /** Tooltip for the URL field (the page title). */
    hint?: string;
    buttons: ArtifactButton[];
    busyAction: string | null;
    onAction: (id: string) => void;
    /** The device the toggle shows as picked. */
    device: Device;
    onDevice: (device: Device) => void;
    /** The starting placeholder: every control shows, none answers. */
    disabled?: boolean;
};

/** One device segment that tracks its own hover. */
function DeviceSegment(props: {
    device: Device; active: boolean; disabled?: boolean; onPick: (device: Device) => void;
}) {
    const [hover, setHover] = useState(false);
    return (
        <button
            type="button"
            aria-pressed={props.active}
            disabled={props.disabled}
            onClick={() => { if (!props.active) props.onPick(props.device); }}
            onMouseEnter={() => setHover(true)}
            onMouseLeave={() => setHover(false)}
            style={deviceSegmentStyle(props.active, { hover, disabled: props.disabled })}
        >
            {DEVICE_LABELS[props.device]}
        </button>
    );
}

/** The Desktop / Phone / Tablet toggle: a press is the `device` action, and
 *  the tab switches when the daemon rewrites its file. */
function DeviceToggle(props: { device: Device; disabled?: boolean; onPick: (device: Device) => void }) {
    return (
        <div role="group" aria-label="Device" style={deviceGroupStyle}>
            {DEVICES.map((d) => (
                <DeviceSegment
                    key={d}
                    device={d}
                    active={d === props.device}
                    disabled={props.disabled}
                    onPick={props.onPick}
                />
            ))}
        </div>
    );
}

/** The chrome the session wears: status dot, history tools, address bar,
 *  the device toggle, then the generator's action buttons (Capture, New
 *  tab), quiet and unfilled: the address bar is the toolbar's one focal
 *  point. */
function Toolbar(props: ToolbarProps) {
    const { status, urlInput, setUrlInput, disabled } = props;
    const [focused, setFocused] = useState(false);
    return (
        <div style={toolbarStyle}>
            <span style={{
                width: 8, height: 8, borderRadius: '50%', margin: '0 4px',
                background: statusColorOf(status), flexShrink: 0,
            }} title={status.kind === 'closed' ? status.reason : status.kind} />
            <ChromeButton kind="tool" title="Back" disabled={disabled} onClick={() => props.onNavigate('back')}>←</ChromeButton>
            <ChromeButton kind="tool" title="Forward" disabled={disabled} onClick={() => props.onNavigate('forward')}>→</ChromeButton>
            <ChromeButton kind="tool" title="Reload" disabled={disabled} onClick={() => props.onNavigate('reload')}>⟳</ChromeButton>
            <input
                type="text"
                value={urlInput}
                disabled={disabled}
                spellCheck={false}
                placeholder={disabled ? 'Starting…' : 'https://…'}
                title={props.hint || undefined}
                onChange={(e) => setUrlInput(e.target.value)}
                onFocus={(e) => {
                    setFocused(true);
                    props.onFocusUrl();
                    e.target.select();
                }}
                onBlur={() => {
                    setFocused(false);
                    // Abandoned edit — snap back to the page's real URL.
                    props.onBlurUrl();
                }}
                onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                        props.onCommitUrl();
                        (e.target as HTMLInputElement).blur();
                    } else if (e.key === 'Escape') {
                        (e.target as HTMLInputElement).blur();
                    }
                    // Keep address-bar keystrokes out of the page's own
                    // key handling.
                    e.stopPropagation();
                }}
                style={addressStyle(focused)}
            />
            <DeviceToggle device={props.device} disabled={disabled} onPick={props.onDevice} />
            {props.buttons.map((act) => (
                <ChromeButton
                    key={act.id}
                    kind="quiet"
                    onClick={() => props.onAction(act.id)}
                    disabled={disabled || props.busyAction === act.id}
                    busy={props.busyAction === act.id}
                >
                    {props.busyAction === act.id ? '…' : act.label}
                </ChromeButton>
            ))}
        </div>
    );
}

/** The canvas while the tab is on no page: one centred form that IS the
 *  address bar in another place — it edits the same field and commits the
 *  same way — so a tab opened without a site has its way in where the page
 *  will be, not only in the chrome above. The Open button keeps the field's
 *  focus through its press: a blur first would snap the field back to the
 *  page's real URL, which is nothing, before the commit read it. */
function EnterPage(props: {
    urlInput: string;
    setUrlInput: (next: string) => void;
    onFocusUrl: () => void;
    onBlurUrl: () => void;
    onCommitUrl: () => void;
}) {
    const [focused, setFocused] = useState(false);
    return (
        <div style={{
            position: 'absolute', inset: 0, boxSizing: 'border-box',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            padding: 24, background: tokens.color.sheet,
        }}>
            <form
                onSubmit={(e) => {
                    e.preventDefault();
                    props.onCommitUrl();
                    e.currentTarget.querySelector('input')?.blur();
                }}
                style={enterCardStyle}
            >
                <div style={{ fontSize: 13.5, fontWeight: 600, color: tokens.color.ink }}>Enter page</div>
                <div style={{ display: 'flex', gap: 8 }}>
                    <input
                        type="text"
                        aria-label="Page"
                        autoFocus
                        value={props.urlInput}
                        spellCheck={false}
                        placeholder="https://…"
                        onChange={(e) => props.setUrlInput(e.target.value)}
                        onFocus={(e) => {
                            setFocused(true);
                            props.onFocusUrl();
                            e.target.select();
                        }}
                        onBlur={() => {
                            setFocused(false);
                            props.onBlurUrl();
                        }}
                        onKeyDown={(e) => {
                            if (e.key === 'Escape') (e.target as HTMLInputElement).blur();
                            // Enter submits the form. Every key stays out of
                            // the host's own key handling, as in the address bar.
                            e.stopPropagation();
                        }}
                        style={addressStyle(focused, 30)}
                    />
                    <ChromeButton
                        kind="primary"
                        type="submit"
                        height={30}
                        onMouseDown={(e) => e.preventDefault()}
                    >
                        Open
                    </ChromeButton>
                </div>
                <div style={{ fontSize: 12, lineHeight: 1.5, color: tokens.color.ink3 }}>
                    A site or a full URL, the same as the address bar above.
                </div>
            </form>
        </div>
    );
}

/** The control socket + RPC, shared by both session views: say hello as
 *  `client`, then answer every daemon RPC out of whatever guest `guest()`
 *  returns at answer time. `onOpen` runs after the hello. */
function useControlSocket(api: any, artifact: Artifact, opts: {
    client: 'electron' | 'browser';
    caps: Record<string, unknown> | null | undefined;
    guest: () => GuestHandle | null;
    onOpen?: (ws: WebSocket) => void;
}): { status: Status; sockRef: React.MutableRefObject<any> } {
    const sockRef = useRef<any>(null);  // SessionSocket from /api/sdk/session.js
    const [status, setStatus] = useState<Status>({ kind: 'connecting' });
    useEffect(() => {
        // The proxy rejects the handshake once the run has stopped, and there
        // is no close-code contract on this channel, so "the run is gone" is
        // repeated failure to OPEN rather than a 4404.
        const sock = openSessionSocket({
            url: sessionWsUrl(api, artifact.wsBase + CONTROL_WS_SUFFIX),
            transientRetryMs: 1500,
            maxFailedOpens: 5,
            onStatus: (st: string) => setStatus(statusFromSocket(st)),
            onOpen: (ws: WebSocket) => {
                ws.send(JSON.stringify(helloFrame(artifact.tab, opts.client, opts.caps)));
                opts.onOpen?.(ws);
            },
            onMessage: (ev: MessageEvent) => {
                let payload: any;
                try { payload = JSON.parse(ev.data); } catch { return; }
                // Anything that is not an RPC reads as null. The guest is read
                // at answer time, so an RPC that lands during a remount refuses
                // instead of hanging.
                answerRpc(payload, guestFromHandle(opts.guest())).then((reply) => {
                    if (reply) sockRef.current?.sendJson(reply);
                });
            },
        });
        sockRef.current = sock;
        return () => {
            sockRef.current = null;
            sock.close();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [artifact.wsBase, artifact.tab]);
    return { status, sockRef };
}

// ===========================================================================
// The session — the client's own native guest view IS the browser
// ===========================================================================

function SessionView({ artifact, api, desktop }: {
    artifact: Artifact; api: any; desktop: DesktopSurface;
}) {
    // Callback ref rather than useRef: the mount effect has to run the moment
    // the host node exists, and must not re-run on every render.
    const [hostEl, setHostEl] = useState<HTMLDivElement | null>(null);
    const handleRef = useRef<GuestHandle | null>(null);
    const [pageTitle, setPageTitle] = useState('');
    // Set from the guest's `crashed` event; replaces the view with an
    // in-place reload affordance (the guest process is gone, but the mount —
    // and its partition — is not).
    const [crash, setCrash] = useState<string | null>(null);
    const { busy: busyAction, fire } = useActions(api);
    // Address bar: `urlInput` is the local edit buffer; guest navigations only
    // overwrite it while the user isn't mid-edit in the field.
    const [urlInput, setUrlInput] = useState<string>(() => artifact.url);
    const editingUrlRef = useRef(false);
    const liveUrlRef = useRef(artifact.url);
    const rejectedUrlRef = useRef<string | null>(null);
    const navigationSeqRef = useRef(0);
    const [navigationError, setNavigationError] = useState('');
    // The guest's real URL as state too: the canvas holds the Enter page
    // form while the tab is blank, and a ref cannot re-render.
    const [liveUrl, setLiveUrl] = useState<string>(() => artifact.url);
    const blank = isBlankPage(liveUrl);
    const [shownDevice, pickDevice] = useDevicePick(api, artifact.device);
    // The device the mounted guest was last given: a mount takes the
    // artifact's current one, and a later rewrite of the file switches the
    // same guest in place (setDevice) instead of remounting it.
    const deviceRef = useRef<Device>(artifact.device);
    deviceRef.current = artifact.device;
    const appliedDeviceRef = useRef<Device>(artifact.device);

    const pushUrl = (u: string) => {
        liveUrlRef.current = u;
        setLiveUrl(u);
        if (!editingUrlRef.current && rejectedUrlRef.current === null) setUrlInput(displayUrl(u));
    };
    // The form leaves with the blank page. An edit in progress there is over
    // — the page is what the address bar shows now.
    useEffect(() => {
        if (blank) return;
        editingUrlRef.current = false;
        setUrlInput(displayUrl(liveUrlRef.current));
    }, [blank]);

    // ---- the control socket + RPC --------------------------------------
    // Declared before the guest mount, whose navigate handler reads sockRef.
    const { status, sockRef } = useControlSocket(api, artifact, {
        client: 'electron',
        caps: desktop.capabilities || {},
        guest: () => handleRef.current,
        onOpen: (ws) => {
            // The hello says we are the client that drives this tab; then
            // hand over the guest's URL, which may have moved while we were away.
            if (liveUrlRef.current) {
                ws.send(JSON.stringify({ kind: 'url', url: liveUrlRef.current }));
            }
        },
    });

    // ---- the guest -----------------------------------------------------
    useEffect(() => {
        if (!hostEl || !desktop.mountGuest) return;
        const handle = desktop.mountGuest(hostEl, {
            // No site named: the guest still mounts — the partition (and the
            // logins in it) is the point — it just opens on the blank page.
            // Its host is collapsed to nothing while it is blank, so what
            // shows is the Enter page form (or the address bar): commit
            // either and the guest navigates, reports its URL, and the host
            // opens up under it.
            src: artifact.url || BLANK_PAGE,
            // The artifact's partition is passed through VERBATIM. It already
            // carries the shell's `persist:collimate-` prefix (the daemon's
            // `_partition_for`), and the host's `partitionFor` is idempotent —
            // it strips a leading prefix before re-slugging — so a prefixed
            // name survives unchanged while a bare one still gets prefixed.
            // Stripping it here would produce the same string with one more
            // way to get it wrong.
            partition: artifact.partition || undefined,
            // The shell composes the user agent and mobile emulation from
            // the device name. Read off a ref: a device change is applied to
            // this same guest below, never by remounting it.
            device: deviceRef.current,
            // The daemon can capture this guest while the cell is parked
            // off-screen, and a reload would drop whatever login the user set
            // up in it — so it outlives parking AND this mount (destroy()
            // only releases the mount; the page, its history and its login
            // survive a remount and end with the app window).
            keepAlive: true,
            interactive: true,
            // Scopes the guest to THIS tab of THIS run (see guestSessionKeyOf): a
            // re-run or another tab opens a fresh page on its own URL instead
            // of reattaching a parked one (which would also drift the
            // daemon's state), while a remount of the same tab — closed and
            // reopened — still reattaches the live page.
            guestSessionKey: guestSessionKeyOf(artifact),
        });
        handleRef.current = handle;
        appliedDeviceRef.current = deviceRef.current;
        setCrash(null);
        const offs = [
            handle.on('navigate', (d) => {
                pushUrl(d.url);
                // The daemon's `state.current_url` follows the guest — it is
                // what a capture's ctx.url and the cell's status line read.
                sockRef.current?.sendJson({ kind: 'url', url: d.url });
            }),
            handle.on('title', (d) => setPageTitle(d.title || '')),
            handle.on('crashed', (d) => setCrash(d.reason || 'the page stopped responding')),
        ];
        // A keepAlive guest survives a remount of this renderer, and no
        // navigation event replays for it — so ask where it actually is
        // rather than showing the artifact's opening URL forever.
        handle.currentUrl().then((u) => { if (u) pushUrl(u); }).catch(() => { /* not attached yet */ });
        return () => {
            for (const off of offs) off();
            navigationSeqRef.current += 1;
            handleRef.current = null;
            handle.destroy();
        };
        // wsBase and tab are in here for the same reason they are the
        // session key: another run or tab is another session, and this mount
        // belongs to the old one.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [hostEl, artifact.url, artifact.partition, artifact.wsBase, artifact.tab]);

    // The toggle (here or in another window) rewrote the tab's file: switch
    // the mounted guest in place — same keepAlive guest, same session key, so
    // the login and history survive; the shell reloads the page.
    useEffect(() => {
        const handle = handleRef.current;
        if (!handle || appliedDeviceRef.current === artifact.device) return;
        appliedDeviceRef.current = artifact.device;
        handle.setDevice(artifact.device).catch(() => { /* a refused switch keeps the page */ });
    }, [artifact.device]);

    // ---- chrome --------------------------------------------------------
    // Every guest op rejects rather than throwing when the guest isn't there
    // (a refused mount, a crashed process) — swallow it: the buttons are
    // best-effort and the status dot already says whether we're attached.
    const quiet = (p: Promise<unknown> | undefined) => { p?.catch(() => {}); };
    const navigate = (action: 'back' | 'forward' | 'reload') => {
        const handle = handleRef.current;
        if (!handle) return;
        if (action === 'back') quiet(handle.goBack());
        else if (action === 'forward') quiet(handle.goForward());
        else quiet(handle.reload());
    };
    const editUrl = (value: string) => {
        navigationSeqRef.current += 1;
        rejectedUrlRef.current = null;
        setNavigationError('');
        setUrlInput(value);
    };
    const commitUrl = () => {
        const typed = urlInput.trim();
        if (!typed) return;
        const sequence = ++navigationSeqRef.current;
        const handle = handleRef.current;
        rejectedUrlRef.current = typed;
        editingUrlRef.current = false;
        setNavigationError('');
        Promise.resolve().then(() => {
            if (!handle) throw new Error('The browser view is not attached.');
            return handle.loadURL(withScheme(typed));
        }).then(() => {
            if (sequence !== navigationSeqRef.current || handleRef.current !== handle) return;
            rejectedUrlRef.current = null;
            setNavigationError('');
        }).catch(error => {
            if (sequence !== navigationSeqRef.current || handleRef.current !== handle) return;
            setUrlInput(typed);
            setNavigationError(String(error?.message || error || 'Navigation refused.'));
        });
    };
    const onFocusUrl = () => { editingUrlRef.current = true; };
    const onBlurUrl = () => {
        editingUrlRef.current = false;
        setUrlInput(rejectedUrlRef.current ?? displayUrl(liveUrlRef.current));
    };


    return (
        <div style={groundStyle}>
            <Toolbar
                status={status}
                urlInput={urlInput}
                setUrlInput={editUrl}
                onFocusUrl={onFocusUrl}
                onBlurUrl={onBlurUrl}
                onCommitUrl={commitUrl}
                onNavigate={navigate}
                hint={pageTitle}
                buttons={artifact.buttons}
                busyAction={busyAction}
                onAction={fire}
                device={shownDevice}
                onDevice={pickDevice}
            />
            {navigationError && <div role="alert" style={navigationErrorStyle}>{navigationError}</div>}
            <div style={pageBoxStyleFor(artifact.viewportWidth)}>
                {/* The guest is a real Chromium view; the shell's pool owns
                    its position and keeps it over this element — a host
                    with no size is a view with no size, which is how the
                    blank guest stays out of the form's way, and a host
                    letterboxed to the device's width is a guest that wide. */}
                <div
                    ref={setHostEl}
                    style={{ ...letterboxStyle(artifact.viewportWidth), display: blank ? 'none' : undefined }}
                />
                {blank && !crash && (
                    <EnterPage
                        urlInput={urlInput}
                        setUrlInput={editUrl}
                        onFocusUrl={onFocusUrl}
                        onBlurUrl={onBlurUrl}
                        onCommitUrl={commitUrl}
                    />
                )}
                {crash && (
                    <div style={{ ...panelStyle, position: 'absolute', inset: 0, zIndex: 10 }}>
                        <div style={{ color: tokens.color.ink, fontSize: 13.5, fontWeight: 600 }}>
                            The page crashed
                        </div>
                        <div style={{ fontFamily: tokens.font.mono, fontSize: 12 }}>
                            {crash}
                        </div>
                        <ChromeButton
                            kind="primary"
                            onClick={() => { setCrash(null); quiet(handleRef.current?.reload()); }}
                        >
                            Reload
                        </ChromeButton>
                    </div>
                )}
            </div>
        </div>
    );
}

// ===========================================================================
// The fallback — no native view, so an iframe does what an iframe can
// ===========================================================================

/** A client with no native guest view (a browser tab, above all). The page
 *  is framed best-effort: many sites refuse framing and logins don't
 *  persist, but an address bar that navigates beats a dead pane. The frame
 *  is sandboxed with no top-navigation token, so a page on another origin
 *  cannot replace the app's tab, and the URL it is given must be another
 *  origin's http(s) page (`fallbackPane`). A framed site that navigates its
 *  own frame onto the app's origin still runs as the app there (see
 *  `frameableUrl`). The control socket still connects and says honestly
 *  what this client is, so the daemon's capture verbs get an `rpc_error`
 *  (there is no guest to serve them from) instead of a hang. The toolbar
 *  still shows the run's Capture; every press of it fails that way, which
 *  the banner forewarns. */
function FallbackSessionView({ artifact, api, desktop }: {
    artifact: Artifact; api: any; desktop: DesktopSurface | undefined;
}) {
    // No handle exists in this mode: every RPC verb refuses, which the
    // daemon reports as the capture being unavailable.
    const { status, sockRef } = useControlSocket(api, artifact, {
        client: 'browser',
        caps: desktop?.capabilities,
        guest: () => null,
    });
    const { busy: busyAction, fire } = useActions(api);
    const [shownDevice, pickDevice] = useDevicePick(api, artifact.device);
    const [urlInput, setUrlInput] = useState<string>(() => artifact.url);
    // What the iframe is pointed at; the nonce remounts it for Reload
    // (an iframe's location is unreadable and unrewritable cross-origin).
    const [frameSrc, setFrameSrc] = useState<string>(() => artifact.url);
    // frameSrc as a ref too: the address bar's blur runs right after a
    // commit, in the same event, where the state is still the old value.
    const frameSrcRef = useRef<string>(artifact.url);
    const [frameNonce, setFrameNonce] = useState(0);

    const commitUrl = () => {
        const u = urlInput.trim();
        if (!u) return;
        const target = withScheme(u);
        frameSrcRef.current = target;
        setFrameSrc(target);
        setFrameNonce((n) => n + 1);
        sockRef.current?.sendJson({ kind: 'url', url: target });
    };
    const onFocusUrl = () => {};
    const onBlurUrl = () => setUrlInput(frameSrcRef.current);
    const pane = fallbackPane(frameSrc, [window.location.origin, apiOrigin(api)]);

    return (
        <div style={groundStyle}>
            <Toolbar
                status={status}
                urlInput={urlInput}
                setUrlInput={setUrlInput}
                onFocusUrl={onFocusUrl}
                onBlurUrl={onBlurUrl}
                onCommitUrl={commitUrl}
                onNavigate={(action) => {
                    // No history access across an iframe boundary — Reload
                    // remounts, back/forward have nothing to drive.
                    if (action === 'reload') setFrameNonce((n) => n + 1);
                }}
                buttons={artifact.buttons}
                busyAction={busyAction}
                onAction={fire}
                device={shownDevice}
                onDevice={pickDevice}
            />
            <div style={fallbackBannerStyle}>
                Browser fallback — some sites refuse to load in a frame, logins
                aren't persistent, capture needs the desktop app, and a phone
                or tablet is only its width here, not its user agent.
            </div>
            <div style={pageBoxStyleFor(artifact.viewportWidth)}>
                {pane === 'frame' ? (
                    <iframe
                        key={`${frameSrc}#${frameNonce}`}
                        src={frameSrc}
                        allow="fullscreen"
                        // No allow-top-navigation of any kind: a page on another
                        // origin may not replace the app's tab. Its popups leave
                        // the sandbox so sign-in and download flows still work.
                        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals"
                        style={{ ...frameStyle, ...letterboxStyle(artifact.viewportWidth), height: '100%' }}
                    />
                ) : pane === 'notice' ? (
                    <div style={{ position: 'absolute', inset: 0 }}>
                        <Notice detail={`${frameSrc} can't be shown in the browser fallback: it opens only http(s) sites on another origin. Open it in the desktop app.`} />
                    </div>
                ) : (
                    // A session that named no site has nothing to frame yet, and
                    // src="" would load the app's OWN origin into the pane — so
                    // there is no iframe at all until the address bar, or the
                    // Enter page form in its place, commits one.
                    <EnterPage
                        urlInput={urlInput}
                        setUrlInput={setUrlInput}
                        onFocusUrl={onFocusUrl}
                        onBlurUrl={onBlurUrl}
                        onCommitUrl={commitUrl}
                    />
                )}
            </div>
        </div>
    );
}

// ===========================================================================
// Starting — the host's placeholder before the daemon writes the tab
// ===========================================================================

/** The tab before its run's daemon exists: the chrome it will wear, every
 *  control disabled, and "Starting…" where the page will be. The daemon's
 *  first write replaces the file and the session mounts in its place. */
function StartingView({ label }: { label: string }) {
    return (
        <div style={groundStyle}>
            <Toolbar
                status={{ kind: 'starting' }}
                urlInput=""
                setUrlInput={() => {}}
                onFocusUrl={() => {}}
                onBlurUrl={() => {}}
                onCommitUrl={() => {}}
                onNavigate={() => {}}
                buttons={[]}
                busyAction={null}
                onAction={() => {}}
                device="desktop"
                onDevice={() => {}}
                disabled
            />
            <div style={pageBoxStyle}>
                <div style={{ ...panelStyle, position: 'absolute', inset: 0 }}>
                    <div style={{ color: tokens.color.ink, fontSize: 13.5, fontWeight: 600 }}>Starting…</div>
                    {label && <div style={{ fontSize: 12.5 }}>{label}</div>}
                </div>
            </div>
        </div>
    );
}

// ===========================================================================
// The renderer — parse, gate, mount
// ===========================================================================

function WebView() {
    const { content, api } = useCollimateFile();
    const artifact = useMemo<Artifact>(() => parseArtifact(content), [content]);
    const startingLabel = useMemo(() => startingLabelOf(content), [content]);
    const desktop = desktopOf(api);
    // Once per mount: the shadow root this renderer draws into is its own.
    useEffect(() => { api?.injectCss?.(CHROME_CSS); }, [api]);

    // The host's placeholder: the run is accepted and its daemon is on the
    // way. Show the tab now; the daemon's first write replaces it.
    if (startingLabel !== null) return <StartingView label={startingLabel} />;

    // A tab file names a live control socket, so an unreadable one (or one
    // from a generator out of step with this renderer) has no session: "no
    // session yet" comes first,
    // ahead of the gate, or a browser tab flashes the fallback at a session
    // that does not exist.
    if (!artifact.wsBase) {
        return <Notice detail="Waiting for the Web generator to publish a session…" />;
    }

    // The `requires` gate decides the MODE, not whether anything renders: a
    // client with the native surface hosts the page in its own guest view;
    // anything without it (a browser tab, above all — `api.desktop` is simply
    // absent there) gets the best-effort iframe fallback.
    const missing = unmetRequirements(artifact.requires, desktop?.capabilities);
    if (missing.length || typeof desktop?.mountGuest !== 'function') {
        return <FallbackSessionView artifact={artifact} api={api} desktop={desktop} />;
    }

    return <SessionView artifact={artifact} api={api} desktop={desktop!} />;
}

export default defineRenderer({ component: WebView });
