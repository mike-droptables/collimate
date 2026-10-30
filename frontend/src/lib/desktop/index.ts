// The Electron desktop shell's contextBridge surface, as the page sees it.
//
// Declared structurally here rather than imported from desktop/: the shell
// is a separate build that the browser bundle must never depend on, and
// this page has to compile (and run) with no shell present at all. The
// contract is therefore duplicated on purpose — desktop/src/preload owns
// the runtime side, this owns the page's view of it, and `bridge` is the
// version number that catches them drifting apart.
//
// Everything here degrades to "browser" when `window.collimateDesktop` is
// absent, which is both the real browser case and every test environment.

/** What the shell can do that a browser tab cannot. */
export interface DesktopCapabilities {
    /** Host a real Chromium view (a main-process WebContentsView) for a cell. */
    nativeGuests: boolean;
    /** Capture that view's pixels, visible or not. */
    guestCapture: boolean;
    /** Give a view its own cookie/storage partition that survives restart. */
    persistentPartitions: boolean;
    /** Reach 127.0.0.1 services directly, unblocked by page CSP. */
    directLoopback: boolean;
    /** Open a URL in the user's real browser. */
    externalOpen: boolean;
    /**
     * The window has no native title bar — this page is expected to render
     * the VS Code-style bar (icon, menus, drag region): see DesktopTitleBar.
     */
    customTitleBar: boolean;
    /**
     * The page must draw the min/max/close buttons too (Linux, where the
     * window is fully frameless). On Windows the shell's overlay draws
     * native controls over the page's top-right corner instead.
     */
    customWindowControls: boolean;
}

/**
 * What a guest did, pushed from the main process.
 *
 * Keyed by kind so `on('title', …)` gets a typed payload. Mirrors
 * `GuestEvent` in desktop/src/shared/bridge.ts. With no DOM element in
 * the page any more, this stream is the page's entire view of a guest:
 * `ready` is the pool's readiness signal (the old `dom-ready`), and
 * `focus` and `wheel` exist because the DOM cannot see a native view
 * taking input.
 */
export interface GuestEventData {
    download: { status: 'started' | 'completed' | 'refused' | 'failed'; fileName: string; reason?: string };
    navigate: { url: string; inPage: boolean };
    title: { title: string };
    loading: { loading: boolean };
    crashed: { reason: string; exitCode: number };
    focus: { focused: boolean };
    /** A wheel the native view took (and scrolled by), in DOM wheel terms:
     *  px, positive scrolls down/right. */
    wheel: { deltaX: number; deltaY: number; ctrlKey: boolean };
    ready: { url: string };
}

export type GuestEventKind = keyof GuestEventData;

export type GuestEvent = {
    [K in GuestEventKind]: { guestId: string; kind: K; data: GuestEventData[K] };
}[GuestEventKind];

/** The device a guest presents as: the shell composes its user agent and
 *  screen emulation from its own Chromium version. Mirrors `GuestDevice` in
 *  desktop/src/shared/bridge.ts. */
export type GuestDevice = 'desktop' | 'phone' | 'tablet';

/** What the page decides about a guest at creation time. */
export interface GuestCreateOptions {
    /** Renderer-minted id; every later call addresses the guest by it. */
    guestId: string;
    /** Must already carry the `persist:collimate-` prefix (see partitionFor). */
    partition: string;
    /** Initial URL. http(s) or about:blank — the shell refuses anything else. */
    url: string;
    /** The device the guest presents as; absent = desktop. */
    device?: GuestDevice;
    /** Mute the guest's audio while its native view is hidden. */
    muteWhenHidden?: boolean;
}

/** What `createGuest` resolves with. */
export interface GuestState {
    guestId: string;
    url: string;
}

/** One guest's target rectangle, in window CSS pixels, pre-snapped. */
export interface GuestLayoutRect {
    guestId: string;
    x: number;
    y: number;
    w: number;
    h: number;
    /** Uniform corner radius — a native view can't do per-corner rounding. */
    radius: number;
}

/** The layout hot path's payload. Fire-and-forget, batched per pass. */
export interface GuestLayoutBatch {
    guests: GuestLayoutRect[];
    /** The window zoom factor the rects were snapped against. */
    zoomFactor: number;
}

/**
 * Control of the guest views the page has created.
 *
 * `guestId` is the string the page minted and passed to `createGuest`.
 * The shell answers only for ids created through that call, and every
 * rejection carries a `collimate-guest/<reason>` marker.
 *
 * This is the ONLY way anything happens to a guest — there is no DOM
 * element to reach around it any more.
 */
export interface GuestOps {
    /** Create a guest view. Resolves once the view exists (not loaded). */
    createGuest(options: GuestCreateOptions): Promise<GuestState>;
    /** Remove the view and end its renderer process. Idempotent. */
    destroyGuest(guestId: string): Promise<void>;
    /** Show or hide the native view; the page keeps running either way. */
    setGuestVisible(guestId: string, visible: boolean): Promise<void>;
    /** Move the guest to the top of the window's native view z-order. */
    raiseGuest(guestId: string): Promise<void>;
    /** Position guests. Fire-and-forget — the layout hot path. */
    layoutGuests(batch: GuestLayoutBatch): void;
    /** Run `js` in the guest's main frame; resolves to its completion value. */
    evaluate(guestId: string, js: string): Promise<unknown>;
    /** A PNG of the guest's area, as raw bytes. Works on hidden guests. */
    capturePng(guestId: string): Promise<ArrayBuffer>;
    /** Private renderer-host JPEG85 for transient placeholders; never save or expose through SDK RPC. */
    capturePlaceholder(guestId: string): Promise<ArrayBuffer>;
    currentUrl(guestId: string): Promise<string>;
    /** Navigate the guest. http(s) or about:blank — anything else is refused. */
    navigate(guestId: string, url: string): Promise<void>;
    /** Resolves false when there was nowhere to go. */
    goBack(guestId: string): Promise<boolean>;
    goForward(guestId: string): Promise<boolean>;
    reload(guestId: string): Promise<void>;
    /** Present the guest as another device (user agent and screen
     *  emulation) and reload it in place, keeping its session. */
    setGuestDevice(guestId: string, device: GuestDevice): Promise<void>;
    /** Dev builds only; refused in a packaged app. */
    openDevTools(guestId: string): Promise<void>;
    /** Wipe a `persist:collimate-` partition's storage. */
    clearPartition(partition: string): Promise<void>;
}

/** One top-level application-menu entry, for the page-drawn menu bar. */
export interface TitleBarMenu {
    label: string;
}

/**
 * The page-drawn title bar's verbs (requires `capabilities.customTitleBar`).
 * The shell keeps owning the real application menu: this page draws the
 * top-level labels as buttons and pops the native submenu under whichever
 * one was clicked, so accelerators and platform items stay in one place.
 */
export interface TitleBarOps {
    /** Top-level application-menu entries, in order. */
    menus(): Promise<TitleBarMenu[]>;
    /** Pop the native submenu of entry `index` at page coordinates (x, y). */
    popupMenu(index: number, x: number, y: number): Promise<void>;
    /** Maximize, or restore when already maximized. Resolves to the new state. */
    toggleMaximize(): Promise<boolean>;
    // The page-drawn window controls (`capabilities.customWindowControls`).
    minimize(): Promise<void>;
    close(): Promise<void>;
    /** For the maximize/restore button's icon; poll on the page's resize. */
    isMaximized(): Promise<boolean>;
}

/** The window colours that follow the active skin. */
export interface WindowTheme {
    /** The window's own background (`BrowserWindow.setBackgroundColor`):
     *  the skin's canvas, so a resize or reload never flashes another colour. */
    background: string;
    /** The Windows caption-button overlay's fill: the skin's sheet. */
    overlay: string;
    /** The overlay's glyph colour: the skin's ink. */
    symbol: string;
}

/** Where a server runs: the shell's own (`local`, POSIX only) or a saved
 *  remote. Mirrors `ServerKind` in desktop/src/shared/bridge.ts. */
export type ServerKind = 'local' | 'remote';

/** The server this window is bound to. One window is one server origin:
 *  opening another server recreates the window on that server's origin.
 *  Mirrors `ServerInfo` in desktop/src/shared/bridge.ts. */
export interface ServerInfo {
    /** `'local'` for the shell's own server, else the saved server's id. */
    id: 'local' | string;
    kind: ServerKind;
    name: string;
    url: string;
    port: number;
    /** The server's data dir; null for a remote (that server isn't ours). */
    dataDir: string | null;
    /** `version` as reported by the server's /health. */
    serverVersion: string | null;
}

/** A registry entry's last known state, from the shell's polling. */
export type ServerStatus = 'starting' | 'running' | 'stopped' | 'unreachable' | 'unauthorized' | 'mismatch' | 'failed';

/** One channel in a server's light catalog (`GET /api/channels`). */
export interface ServerChannel {
    id: string;
    name: string;
}

/**
 * One server the shell knows: the local server plus every saved remote.
 * Mirrors `ServerEntry` in desktop/src/shared/bridge.ts. Tokens never come
 * back to the page; `hasToken` is all it learns.
 */
export interface ServerEntry {
    id: 'local' | string;
    kind: ServerKind;
    name: string;
    url: string;
    status: ServerStatus;
    serverVersion: string | null;
    hasToken: boolean;
    /** The server's channels, or null while it cannot be read. */
    channels: ServerChannel[] | null;
    activeChannelId: string | null;
    /** The server this window is on. */
    isWindowServer: boolean;
    /** Why the last poll failed, in the shell's words. */
    error?: string;
}

/** A remote server to add. The token is typed in the page and handed to
 *  the shell once; it is never read back. */
export interface ServerDraft {
    name?: string;
    url: string;
    token?: string;
}

/** Shell-wide preferences the page may show and change. */
export interface ShellPrefs {
    /** Leave the local server running when the app quits. */
    keepLocalRunning: boolean;
}

/** A shell menu item the page carries out (File → Servers… opens the
 *  Settings Servers tab). */
export type ShellCommand = { type: 'openServers' } | { type: 'localStopFailed'; message: string };

/** What `probe` resolves with: the server answered and admitted the token,
 *  or the reason it did not. */
export type ServerProbeResult = { ok: true; serverVersion: string } | { ok: false; message: string };

/**
 * The server registry the shell owns. Mirrors `ServersOps` in
 * desktop/src/shared/bridge.ts. Removing a server, changing a token and
 * stopping the local server are confirmed by a native dialog in the shell,
 * since remote-served pages reach this bridge too.
 */
export interface ServersOps {
    list(): Promise<ServerEntry[]>;
    /** Probes the server first and mints its id. */
    add(draft: ServerDraft): Promise<ServerEntry>;
    update(patch: { id: string; name?: string; token?: string }): Promise<ServerEntry>;
    remove(id: string): Promise<void>;
    probe(target: { url: string; token?: string }): Promise<ServerProbeResult>;
    /** Recreate the window on that server's origin. */
    openServer(id: string): Promise<void>;
    /** Select `channelId` on server `id`, then recreate the window on it. */
    openChannel(id: string, channelId: string): Promise<void>;
    startLocal(): Promise<void>;
    stopLocal(): Promise<void>;
    getPrefs(): Promise<ShellPrefs>;
    setPrefs(patch: Partial<ShellPrefs>): Promise<ShellPrefs>;
}

/** The object the preload script exposes on `window.collimateDesktop`. */
export interface CollimateDesktop {
    shellVersion: string;
    /** Shell↔server protocol number; matches /health's `bridge`. */
    bridge: number;
    /** node's process.platform — 'darwin' | 'win32' | 'linux' | … */
    platform: string;
    capabilities: DesktopCapabilities;
    /** The server this window is bound to. */
    server: ServerInfo;
    /** The server registry: the local server and the saved remotes. */
    servers: ServersOps;
    /** The registry's entries whenever they change (statuses from polling,
     *  adds, removes). Returns an unsubscribe. */
    onServersChanged(handler: (entries: ServerEntry[]) => void): () => void;
    /** Shell menu items the page carries out. Returns an unsubscribe. */
    onShellCommand(handler: (command: ShellCommand) => void): () => void;
    /** Hand a http(s) URL to the OS browser. Resolves false if refused. */
    openExternal(url: string): Promise<boolean>;
    /** The native folder dialog: the picked folder's absolute path, or
     *  null when cancelled. The shell answers only while the window is on
     *  the local server, whose filesystem that path names. */
    pickFolder?(): Promise<string | null>;
    /** Current Electron page zoom, including changes through menu roles. */
    getZoomFactor(): number;
    /** Delivers the current factor immediately and subsequent changes. */
    onZoomChanged(handler: (factor: number) => void): () => void;
    /** Repaint the window's background and caption overlay in the active
     *  skin. Fire-and-forget; ThemeContext calls it on every applied skin. */
    setWindowTheme?(theme: WindowTheme): void;
    /** This device's interface scale: the window's page zoom, one of
     *  INTERFACE_SCALES (lib/interfaceScale). The Themes tab's "Interface
     *  size" row shows only when the shell exposes both members. */
    getInterfaceScale?(): number;
    /** Apply and persist an interface scale (fire-and-forget; the shell
     *  ignores a factor off the list). */
    setInterfaceScale?(factor: number): void;
    /** Requires `capabilities.nativeGuests`. */
    guest: GuestOps;
    /** The verbs behind `capabilities.customTitleBar` — always exposed;
     *  the capability decides whether the page draws the bar. */
    titleBar: TitleBarOps;
    /**
     * Guest lifecycle, pushed rather than polled. Returns an unsubscribe.
     *
     * Events for every guest in the window arrive on the one callback;
     * filtering by `guestId` is the subscriber's job.
     */
    onGuestEvent(handler: (event: GuestEvent) => void): () => void;
}

declare global {
    interface Window {
        // Present only when the UI runs inside the Electron desktop shell.
        collimateDesktop?: CollimateDesktop;
    }
}

/** The bridge, or undefined in a browser (and in any non-DOM test env). */
export function desktopBridge(): CollimateDesktop | undefined {
    if (typeof window === 'undefined') return undefined;
    return window.collimateDesktop;
}

export function isDesktop(): boolean {
    return desktopBridge() !== undefined;
}

/** The shell's server registry, or undefined in a browser (multi-server is
 *  desktop-only). */
export function desktopServers(): ServersOps | undefined {
    return desktopBridge()?.servers;
}

/** Whether `id` names the server this window is bound to. Always false in
 *  a browser. */
export function isWindowServer(id: string): boolean {
    return desktopBridge()?.server.id === id;
}

/** Whether this window's server runs on another machine, so a path typed
 *  for it names a folder there. False in a browser, which cannot tell. */
export function serverIsRemote(): boolean {
    return desktopBridge()?.server.kind === 'remote';
}

/** The native folder dialog, where the shell has one and this window is on
 *  the local server (a path picked on this machine means nothing to a
 *  remote one); undefined elsewhere. */
export function localFolderPicker(): (() => Promise<string | null>) | undefined {
    const bridge = desktopBridge();
    const pick = bridge?.pickFolder;
    if (!pick || bridge.server.kind !== 'local') return undefined;
    return () => pick.call(bridge);
}

/**
 * The shell's capability set, or `{}` in a browser.
 *
 * Empty rather than an all-false object so callers can't accidentally read
 * a browser as "a desktop that happens to have everything switched off" —
 * `hasCap` treats both the same anyway, and the POST body wants `{}`.
 */
export function desktopCaps(): Partial<DesktopCapabilities> {
    return desktopBridge()?.capabilities ?? {};
}

/**
 * Is one capability available here? The only correct way to branch — the
 * shell reports every capability explicitly, a browser omits the whole
 * map, and both must read as "no" for anything not affirmatively true.
 */
export function hasCap(name: keyof DesktopCapabilities): boolean {
    return desktopCaps()[name] === true;
}

/** Open a URL in the user's real browser. No-op in a browser tab, where
 *  the caller should be rendering a plain link instead of calling this. */
export async function openExternal(url: string): Promise<void> {
    await desktopBridge()?.openExternal(url);
}

/**
 * `api.desktop` — the slice of the shell a renderer is handed.
 *
 * Not the bridge itself: renderers get capability facts and a narrow verb,
 * never `server.port` or anything else that would let them talk to the
 * shell behind the host's back. Mirrors `DesktopSurface` in
 * sdk/packages/renderer-hooks/src/types.ts, which is what
 * renderer authors compile against.
 */
export interface RendererDesktopSurface {
    capabilities: Partial<DesktopCapabilities>;
    openExternal(url: string): Promise<void>;
    /**
     * Mount a live web page inside `hostEl` and get a handle on it.
     *
     * Absent — not a function that fails — unless
     * `capabilities.nativeGuests` is true, so `api.desktop?.mountGuest`
     * is the whole feature test.
     */
    mountGuest?(hostEl: HTMLElement, options: RendererGuestOptions): GuestHandle;
}

/** What a renderer may decide about the guest it mounts. */
export interface RendererGuestOptions {
    /** Initial URL. http(s) or about:blank — the shell refuses anything else. */
    src: string;
    /**
     * Bare partition name, sanitised and prefixed before it reaches the
     * shell. Two mounts naming the same partition share cookies and
     * storage; the host picks a per-renderer default when this is absent.
     */
    partition?: string;
    /** The device the guest presents as (the shell composes the user agent
     *  and screen emulation); absent = desktop. */
    device?: GuestDevice;
    /**
     * Exempt the guest from the pool's LRU and keep its audio unmuted
     * while hidden. For guests whose value is that they stay loaded and
     * behaving — one being captured on a cell nobody is looking at.
     * Costs an OS process while it lives: `destroy()` releases
     * the mount (the guest parks, page state intact); the guest is
     * reclaimed only when the app window reloads or closes, when a mount
     * names a different `guestSessionKey`, or when the host removes the
     * `.webview` file that showed it (a run's tab file deleted at its end).
     */
    keepAlive?: boolean;
    /**
     * Whether the guest takes pointer input; defaults to true.
     *
     * A non-interactive guest is shown as a periodically-refreshed
     * screenshot that clicks fall straight through — a visible native
     * view cannot pass input through itself, so "live but click-through"
     * does not exist on this side of the migration.
     */
    interactive?: boolean;
    /**
     * Names the guest's logical session.
     *
     * A mount reattaches a parked guest by pool key alone, which is what
     * lets a cell be closed and reopened mid-session without losing the
     * page. This is the other half: a mount whose key differs from the
     * parked guest's DESTROYS that guest and creates a fresh one on
     * `src`, so a second session in the same cell is not silently handed
     * its predecessor's page. Omitted on both sides reattaches, as
     * before; name whatever is unique per session (a run id, a proxy
     * base) when the renderer has one.
     */
    guestSessionKey?: string;
}

/**
 * A mounted guest.
 *
 * Every operation goes through the shell's `guest` ops (see
 * {@link GuestOps}) against this mount's own guest id — a renderer never
 * handles a raw id and can't address anyone else's guest.
 */
export interface GuestHandle {
    /** Resolves with the guest's id once the shell has created it. */
    readonly guestId: Promise<string>;
    loadURL(url: string): Promise<void>;
    /** Resolves false when there was nowhere to go. */
    goBack(): Promise<boolean>;
    goForward(): Promise<boolean>;
    reload(): Promise<void>;
    /** Present the guest as another device and reload it in place (same
     *  guest, same session). */
    setDevice(device: GuestDevice): Promise<void>;
    evaluate(js: string): Promise<unknown>;
    capturePng(): Promise<ArrayBuffer>;
    currentUrl(): Promise<string>;
    /** Subscribe to one kind of guest event. Returns an unsubscribe. */
    on<K extends GuestEventKind>(kind: K, handler: (data: GuestEventData[K]) => void): () => void;
    /** Let the guest take pointer input, or show it as a click-through
     *  screenshot instead (what a preview cell wants). */
    setInteractive(interactive: boolean): void;
    /** Release the mount. Idempotent. */
    destroy(): void;
}

/** Body for the `POST /api/system/client` handshake. */
export interface ClientHandshake {
    client: 'electron' | 'browser';
    shellVersion?: string;
    bridge?: number;
    capabilities: Partial<DesktopCapabilities>;
}

/**
 * How this page should identify itself to the server. A browser tab posts
 * this too — the server may have been spawned by the shell with
 * COLLIMATE_CLIENT=electron, and only the page itself knows better.
 */
export function describeClient(): ClientHandshake {
    const shell = desktopBridge();
    if (!shell) return { client: 'browser', capabilities: {} };
    return {
        client: 'electron',
        shellVersion: shell.shellVersion,
        bridge: shell.bridge,
        capabilities: shell.capabilities,
    };
}
