import React, { useEffect, useMemo, useRef, useState } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, tokens, useCollimateFile } from '/api/sdk/react.js';
import { apiOrigin } from '/api/sdk/session.js';

type ButtonSpec = { id: string; label: string };

// The slice of the Electron shell a renderer is handed (`api.desktop`),
// declared structurally because the renderer cannot import app-host types. Undefined in a browser tab and in an
// iframe-embedded renderer — which IS the feature test.
type GuestHandle = {
    loadURL(url: string): Promise<void>;
    reload(): Promise<void>;
    on(kind: 'loading', cb: (d: { loading: boolean }) => void): () => void;
    destroy(): void;
};
type DesktopSurface = {
    capabilities: Record<string, boolean | undefined>;
    mountGuest?(hostEl: HTMLElement, opts: {
        src: string; partition?: string; device?: 'desktop' | 'phone' | 'tablet';
        keepAlive?: boolean; interactive?: boolean;
    }): GuestHandle;
};

// A `.website` pointer is always JSON — `{url, directUrl, loadingCover,
// buttons}`, built by collimate.pointers.website_pointer.
//
// `url` is the app-origin proxy path this browser iframes; `directUrl` is the
// daemon's loopback URL, which only a native (Electron) guest view can reach — so
// it drives the native path below and is ignored everywhere else.
//
// `loadingCover`: hold a panel on the skin's sheet over the iframe until it
// fires `load`, so an embedded app that boots on a white background (e.g.
// code-server's VS Code workbench) never flashes white before its own theme
// paints.
//
// Before the run's process exists the host writes its starting placeholder
// there instead, `{"starting": true, "label"}` (collimate.pointers
// .starting_pointer): the tab shows at once, its toolbar disabled with
// "Starting…", until the run's own pointer replaces it.
type Parsed = { url: string; directUrl: string; buttons: ButtonSpec[]; loadingCover: boolean };

const EMPTY: Parsed = { url: '', directUrl: '', buttons: [], loadingCover: false };

function parseContent(raw: string): Parsed {
    const s = (raw || '').trim();
    if (!s) return EMPTY;
    try {
        const j = JSON.parse(s);
        return {
            url: typeof j.url === 'string' ? j.url.trim() : '',
            directUrl: typeof j.directUrl === 'string' ? j.directUrl.trim() : '',
            buttons: Array.isArray(j.buttons)
                ? j.buttons
                      .filter((b: any) => b && typeof b.id === 'string' && typeof b.label === 'string')
                      .map((b: any) => ({ id: b.id, label: b.label }))
                : [],
            loadingCover: j.loadingCover === true,
        };
    } catch {
        return EMPTY;
    }
}

/** The host's starting placeholder (`{"starting": true, "label"}`) as the
 *  runner's name, or null for anything else — a real pointer included. */
export function startingLabelOf(raw: string): string | null {
    let j: any;
    try { j = JSON.parse((raw || '').trim()); } catch { return null; }
    if (!j || typeof j !== 'object' || Array.isArray(j) || j.starting !== true) return null;
    return typeof j.label === 'string' ? j.label.trim() : '';
}

// Host names that reach this machine's loopback; any of them on the app
// server's port is the app server (its origin guard treats them as one).
const LOOPBACK_NAMES = new Set(['localhost', '127.0.0.1', '[::1]']);

const portOf = (u: URL): string => u.port || (u.protocol === 'https:' ? '443' : '80');

/** Whether the frame showing `frameSrc` keeps its own origin
 *  (`allow-same-origin`). A page on another origin keeps it: the browser
 *  already holds it apart from the app, and it needs its own cookies and
 *  storage to work. On the app server, under any loopback name, only a
 *  `/proxy/` address keeps it: the server answers that path solely for a
 *  live run's own port, so the page a running generator serves works as an
 *  app, with the app's authority, because the user started that run and
 *  opened its page. Any other address on the app server is framed without
 *  it. `appOrigins` are the page's and the API's origins (they differ in a
 *  split-origin setup). */
export function frameKeepsOrigin(frameSrc: string, appOrigins: string[]): boolean {
    let u: URL;
    try {
        u = new URL(frameSrc);
    } catch {
        return false;
    }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
    const onAppServer = appOrigins.some((origin) => {
        let app: URL;
        try {
            app = new URL(origin);
        } catch {
            return false;
        }
        return u.origin === app.origin
            || (LOOPBACK_NAMES.has(u.hostname) && LOOPBACK_NAMES.has(app.hostname)
                && portOf(u) === portOf(app));
    });
    return !onAppServer || u.pathname.startsWith('/proxy/');
}

// ===========================================================================
// The chrome's look
// ===========================================================================
//
// Every colour, font and radius is a skin token (`tokens` from the SDK), so
// the toolbar, the cover and the empty state follow a skin change with no
// subscription. The page in the frame is the site's own and is never themed.
// A native renderer runs in a shadow root, so styles are inline objects;
// the few pseudo-class rules a style object cannot carry go in through
// api.injectCss (CHROME_CSS).

type Style = React.CSSProperties;

const c = tokens.color;
/** The mock's radii, stepped down from the skin's --radius. */
const RADIUS_BUTTON = `calc(${tokens.radius} - 3px)`;
const RADIUS_FIELD = `calc(${tokens.radius} - 2px)`;

/** Rules a style object cannot carry: the field's placeholder and the
 *  keyboard focus ring (teal, the on-state). */
export const CHROME_CSS = [
    `input::placeholder { color: ${c.ink3}; }`,
    `button:focus-visible { outline: 2px solid ${c.teal}; outline-offset: 2px; }`,
].join('\n');

/** The renderer's ground, and what shows behind a page that has not painted. */
export const groundStyle: Style = {
    width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
    background: c.sheet, color: c.ink, fontFamily: tokens.font.ui,
};

/** The address toolbar: a 36px raised bar under the cell's tabs. */
export const toolbarStyle: Style = {
    height: 36, boxSizing: 'border-box', flexShrink: 0,
    display: 'flex', alignItems: 'center', gap: 6, padding: '0 8px',
    background: c.sheet2, borderBottom: `1px solid ${c.line}`,
    fontFamily: tokens.font.ui, fontSize: 12.5, color: c.ink2,
};

/** The address field: a mono address on the sheet, its edge darkening
 *  while it has focus. */
export function addressStyle(focused: boolean): Style {
    return {
        flex: 1, minWidth: 60, height: 26, boxSizing: 'border-box', padding: '0 10px',
        background: c.sheet, color: c.ink,
        border: `1px solid ${focused ? c.line2 : c.line}`, borderRadius: RADIUS_FIELD,
        fontFamily: tokens.font.mono, fontSize: 12, outline: 'none',
    };
}

/** A toolbar button. `quiet` (Go, Open) is the bordered secondary button;
 *  `action` (a generator's button) is the bar's primary, on the sheet in
 *  ink. `busy` is the one that was pressed; `dimmed` the rest while it runs. */
export function buttonStyle(
    kind: 'quiet' | 'action',
    state: { hover?: boolean; busy?: boolean; dimmed?: boolean } = {},
): Style {
    const { hover = false, busy = false, dimmed = false } = state;
    const rest = kind === 'action' ? c.sheet : 'transparent';
    return {
        flexShrink: 0, height: 24, boxSizing: 'border-box', padding: kind === 'action' ? '0 10px' : '0 9px',
        display: 'inline-flex', alignItems: 'center', gap: 6,
        background: hover && !busy ? c.sheet3 : rest,
        color: kind === 'action' || hover ? c.ink : c.ink2,
        border: `1px solid ${c.line2}`, borderRadius: RADIUS_BUTTON,
        boxShadow: kind === 'action' ? `inset 0 0 0 0.5px ${c.line}` : 'none',
        fontFamily: tokens.font.ui, fontSize: 12.5, fontWeight: kind === 'action' ? 600 : 400,
        whiteSpace: 'nowrap', cursor: busy ? 'wait' : 'pointer',
        opacity: dimmed ? 0.45 : 1,
    };
}

/** The word after an action: quiet when it was sent, red when it failed. */
export function actionStatusStyle(ok: boolean): Style {
    return { color: ok ? c.ink2 : c.red, fontFamily: tokens.font.ui, fontSize: 12, whiteSpace: 'nowrap' };
}

/** The frame (or native guest host) box. Its ground is the sheet, which
 *  shows only while a page is blank, transparent or still loading. */
export const frameStyle: Style = {
    position: 'absolute', inset: 0, width: '100%', height: '100%',
    border: 'none', background: c.sheet,
};

/** The loading cover: opaque on the sheet while the page boots, then fading. */
export function coverStyle(covered: boolean): Style {
    return {
        position: 'absolute', inset: 0, background: c.sheet,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: c.ink3, fontFamily: tokens.font.ui, fontSize: 12.5,
        opacity: covered ? 1 : 0, transition: 'opacity 500ms ease',
        pointerEvents: covered ? 'auto' : 'none',
    };
}

/** The pane while the pointer names no address. */
export const emptyStyle: Style = {
    flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
    color: c.ink3, fontFamily: tokens.font.ui, fontSize: 13,
};

/** The pane under the disabled toolbar while the run is starting. */
export const startingStyle: Style = {
    flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
    gap: 4, color: c.ink3, fontFamily: tokens.font.ui, fontSize: 12.5,
};

/** A toolbar button that tracks its own hover (a style object has no
 *  :hover). */
function ChromeButton(props: {
    kind: 'quiet' | 'action'; busy?: boolean; dimmed?: boolean; disabled?: boolean;
    title?: string; onClick: () => void; children: React.ReactNode;
}) {
    const [hover, setHover] = useState(false);
    return (
        <button
            type="button"
            title={props.title}
            disabled={props.disabled}
            onClick={props.onClick}
            onMouseEnter={() => setHover(true)}
            onMouseLeave={() => setHover(false)}
            style={buttonStyle(props.kind, { hover: hover && !props.disabled, busy: props.busy, dimmed: props.dimmed })}
        >
            {props.children}
        </button>
    );
}

/** The host's placeholder: the run is accepted and its server is on the
 *  way. The toolbar shows, none of it answers; the run's pointer replaces
 *  this. */
function StartingView({ label }: { label: string }) {
    return (
        <div style={groundStyle}>
            <div style={toolbarStyle}>
                <input
                    type="text"
                    value=""
                    disabled
                    readOnly
                    aria-label="Address"
                    placeholder="Starting…"
                    style={addressStyle(false)}
                />
                <ChromeButton kind="quiet" disabled dimmed onClick={() => {}}>Open ↗</ChromeButton>
            </div>
            <div style={startingStyle}>
                <div style={{ color: c.ink, fontSize: 13.5, fontWeight: 600 }}>Starting…</div>
                {label && <div>{label}</div>}
            </div>
        </div>
    );
}

function Website() {
    const { content, api } = useCollimateFile();
    // Once per mount: the shadow root this renderer draws into is its own.
    useEffect(() => { api?.injectCss?.(CHROME_CSS); }, [api]);
    const startingLabel = useMemo(() => startingLabelOf(content), [content]);
    if (startingLabel !== null) return <StartingView label={startingLabel} />;
    return <WebsiteViewer />;
}

function WebsiteViewer() {
    const { content, saveContent, isReadOnly, api } = useCollimateFile();
    const parsed = useMemo<Parsed>(() => parseContent(content), [content]);
    const desktop = (api as unknown as { desktop?: DesktopSurface } | null)?.desktop;
    const nativePath = !!(desktop?.capabilities?.directLoopback
        && typeof desktop.mountGuest === 'function'
        && parsed.directUrl);
    const pointerUrl = nativePath ? parsed.directUrl : parsed.url;
    const [localUrl, setLocalUrl] = useState(pointerUrl);
    const [activeUrl, setActiveUrl] = useState(pointerUrl);
    const [pendingAction, setPendingAction] = useState<string | null>(null);
    const [actionStatus, setActionStatus] = useState<{ text: string; ok: boolean } | null>(null);
    const [addressFocused, setAddressFocused] = useState(false);
    // The cover shown over the iframe until it loads (see Parsed).
    const [covered, setCovered] = useState<boolean>(() => parsed.loadingCover && !!pointerUrl);
    // The pointer is authoritative: a generator that rewrites it (ui_file is
    // create-or-update) moves the address bar and the iframe, just as the
    // native path follows parsed.directUrl.
    useEffect(() => { setActiveUrl(pointerUrl); setLocalUrl(pointerUrl); }, [pointerUrl]);

    // Desktop shell surface — undefined in a browser, which is the feature
    // test. `directLoopback` is what makes `directUrl` usable: the daemon's
    // 127.0.0.1 URL is unreachable from a browser tab (page CSP) but not from
    // a native guest view, so on the desktop we mount the served port
    // DIRECTLY instead of routing every byte through the app-origin proxy.
    // Callback ref rather than useRef: the mount effect must run the moment
    // the host node exists (it doesn't on the first render when the pointer
    // has no url yet), and never re-run while it merely re-renders.
    const [hostEl, setHostEl] = useState<HTMLDivElement | null>(null);
    const handleRef = useRef<GuestHandle | null>(null);
    const uncoverTimer = useRef<number | null>(null);
    const clearUncover = () => {
        if (uncoverTimer.current !== null) {
            window.clearTimeout(uncoverTimer.current);
            uncoverTimer.current = null;
        }
    };

    // Re-cover whenever we point the iframe at a new URL (initial load + any
    // address-bar navigation), so the next page's boot is hidden too.
    useEffect(() => {
        clearUncover();
        if (parsed.loadingCover && activeUrl) setCovered(true);
    }, [activeUrl, parsed.loadingCover]);
    useEffect(() => clearUncover, []);

    const uncoverSoon = () => {
        if (!parsed.loadingCover) return;
        // `load` fires when the document + scripts arrive, but a heavy app
        // (code-server's workbench) still needs a moment to execute and paint
        // its dark theme. Hold the cover a beat past load, then fade it out
        // slowly so any residual repaint is masked by the transition.
        clearUncover();
        uncoverTimer.current = window.setTimeout(() => { uncoverTimer.current = null; setCovered(false); }, 300);
    };

    const onIframeLoad = () => uncoverSoon();

    // The native mount effect below captures its event handlers once; route
    // the cover release through a ref so a pointer whose content streamed in
    // after the mount still uncovers.
    const uncoverRef = useRef(uncoverSoon);
    useEffect(() => { uncoverRef.current = uncoverSoon; });

    // Native path: host the served port in a real Chromium guest. Mounted once
    // per directUrl — an address-bar commit navigates the guest it already has
    // (loadURL), mirroring the browser path's re-pointed iframe src rather
    // than tearing the view down. The guest's `loading` edge stands in for the
    // iframe's `load` event so the cover behaves identically.
    useEffect(() => {
        if (!nativePath || !hostEl || !desktop?.mountGuest) return;
        const handle = desktop.mountGuest(hostEl, {
            src: parsed.directUrl,
            interactive: true,
        });
        handleRef.current = handle;
        const offLoading = handle.on('loading', (d) => { if (!d.loading) uncoverRef.current(); });
        return () => {
            offLoading();
            handleRef.current = null;
            handle.destroy();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [nativePath, hostEl, parsed.directUrl]);

    const commit = () => {
        if (isReadOnly) return;
        const u = localUrl.trim();
        // Desktop browsing is local navigation. The pointer's url remains the
        // browser proxy address, while directUrl remains the next native mount.
        if (!nativePath) saveContent(JSON.stringify({ ...parsed, url: u }));
        setActiveUrl(u);
        // The native guest is already mounted, so a commit is a navigation.
        // Resolve a relative pointer path against the API origin first — the
        // shell refuses anything that isn't an absolute http(s) URL.
        // (A guest op rejects rather than throwing when the guest is gone.)
        if (nativePath && u) handleRef.current?.loadURL(resolveExternal(u))?.catch(() => {});
    };

    const resolveExternal = (u: string): string => {
        if (!u) return '';
        if (u.startsWith('http://') || u.startsWith('https://')) return u;
        try {
            // /proxy is served by the backend, which is not necessarily the
            // frontend origin (split-origin deployments).
            return new URL(u, apiOrigin(api)).toString();
        } catch {
            return u;
        }
    };

    const frameSrc = resolveExternal(activeUrl);
    const keepsOrigin = frameKeepsOrigin(frameSrc, [window.location.origin, apiOrigin(api)]);

    const openExternal = () => {
        const target = resolveExternal(localUrl.trim() || activeUrl);
        if (target) window.open(target, '_blank', 'noopener,noreferrer');
    };

    const clickAction = async (id: string, label: string) => {
        if (pendingAction) return;
        setPendingAction(id);
        setActionStatus(null);
        const ok = typeof api.emitAction === 'function' ? await api.emitAction(id) : false;
        setActionStatus({ text: ok ? `${label} sent` : `${label} failed`, ok });
        setPendingAction(null);
        setTimeout(() => setActionStatus(null), 2500);
    };

    return (
        <div style={groundStyle}>
            <div style={toolbarStyle}>
                <input
                    type="text"
                    value={localUrl}
                    onChange={e => setLocalUrl(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && commit()}
                    onFocus={() => setAddressFocused(true)}
                    onBlur={() => setAddressFocused(false)}
                    readOnly={isReadOnly}
                    spellCheck={false}
                    placeholder="https://..."
                    style={addressStyle(addressFocused)}
                />
                {!isReadOnly && <ChromeButton kind="quiet" onClick={commit}>Go</ChromeButton>}
                <ChromeButton kind="quiet" onClick={openExternal} title="Open in new browser tab">
                    Open ↗
                </ChromeButton>
                {parsed.buttons.map(b => {
                    const busy = pendingAction === b.id;
                    return (
                        <ChromeButton
                            key={b.id}
                            kind="action"
                            onClick={() => clickAction(b.id, b.label)}
                            disabled={busy || !!pendingAction}
                            busy={busy}
                            dimmed={!!pendingAction && !busy}
                        >
                            {busy ? '…' : b.label}
                        </ChromeButton>
                    );
                })}
                {actionStatus && <span style={actionStatusStyle(actionStatus.ok)}>{actionStatus.text}</span>}
            </div>

            {activeUrl ? (
                <div style={{ position: 'relative', flex: 1, minHeight: 0 }}>
                    {nativePath ? (
                        // The guest is a real Chromium view the shell's pool
                        // positions over this element: the same full-bleed
                        // box the iframe fills, on the same sheet while the
                        // page is blank.
                        <div ref={setHostEl} style={frameStyle} />
                    ) : keepsOrigin ? (
                        <iframe
                            src={frameSrc}
                            onLoad={onIframeLoad}
                            // A page paints its own background; the sheet
                            // shows only while it is blank, transparent or
                            // still loading.
                            style={frameStyle}
                            sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                        />
                    ) : (
                        <iframe
                            src={frameSrc}
                            onLoad={onIframeLoad}
                            style={frameStyle}
                            sandbox="allow-scripts allow-forms allow-popups"
                        />
                    )}
                    {parsed.loadingCover && (
                        // Opaque while the embedded app boots, then fades to
                        // reveal it, so code-server's white workbench shell
                        // never flashes through before its own theme paints.
                        <div style={coverStyle(covered)}>Loading…</div>
                    )}
                </div>
            ) : (
                <div style={emptyStyle}>Enter a URL above and press Enter</div>
            )}
        </div>
    );
}

export default defineRenderer({ component: Website });
