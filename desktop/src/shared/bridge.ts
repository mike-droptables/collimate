/**
 * The shell <-> renderer contract.
 *
 * Both sides of the preload boundary import these types, and the numbers
 * here are the same protocol numbers the server reports on `GET /health`
 * (`{"status","version","bridge"}`). The shell refuses a remote server
 * whose `bridge` it doesn't implement (main/connect.ts probeServer); for
 * the local server a mismatch is logged as a warning and the UI still
 * loads (main/server.ts awaitHealthy). Bump BRIDGE_PROTOCOL only on a
 * breaking change to that contract — see api/main.py BRIDGE_PROTOCOL.
 */

/**
 * Protocol version this shell implements. Must match the server's.
 *
 * 2: guests moved from `<webview>` tags to main-process WebContentsViews.
 * `guestId` became a renderer-minted string, guest creation became a
 * bridge call (`createGuest`), and the capability names lost their
 * webview spelling (`nativeGuests` / `guestCapture`).
 * 3: the page requires live zoom access/subscription for native guest geometry.
 * 4: the interface scale (`getInterfaceScale` / `setInterfaceScale` over
 * `IPC.interfaceScale`) joins the bridge.
 * 5: guests present as a device: `GuestCreateOptions.device` replaces the
 * raw `userAgent`, and `setGuestDevice` (`IPC.guestSetDevice`) switches a
 * live guest.
 * 6: the servers registry joins the bridge (`servers`, `onServersChanged`,
 * `onShellCommand`), and `ServerInfo` names its server by `id`, `kind` and
 * `name` in place of the spawn/connect mode.
 */
export const BRIDGE_PROTOCOL = 6;

/**
 * The interface scales the shell offers, smallest first: the app window's
 * relative size, per device (settings.json `interfaceScale`). The
 * Themes tab's "Interface size" row and the View menu's zoom items step
 * through exactly these, so the two never diverge. At 100%, the page uses
 * Electron zoom 1.
 */
export const INTERFACE_SCALES = [0.7, 0.8, 0.9, 1, 1.1, 1.25, 1.5] as const;
export type InterfaceScale = (typeof INTERFACE_SCALES)[number];
export const DEFAULT_INTERFACE_SCALE: InterfaceScale = 1;

/** True when `value` is one of {@link INTERFACE_SCALES}. */
export function isInterfaceScale(value: unknown): value is InterfaceScale {
  return typeof value === 'number' && (INTERFACE_SCALES as readonly number[]).includes(value);
}

/**
 * The scale one View-menu step away from `current`: `in` and `out` move to
 * the next larger or smaller scale (staying at the ends), `reset` returns
 * to {@link DEFAULT_INTERFACE_SCALE}. A `current` off the list counts as
 * the default.
 */
export function stepInterfaceScale(current: number, step: 'in' | 'out' | 'reset'): InterfaceScale {
  if (step === 'reset') return DEFAULT_INTERFACE_SCALE;
  const index = INTERFACE_SCALES.indexOf(isInterfaceScale(current) ? current : DEFAULT_INTERFACE_SCALE);
  const next = step === 'in' ? Math.min(index + 1, INTERFACE_SCALES.length - 1) : Math.max(index - 1, 0);
  return INTERFACE_SCALES[next]!;
}

/**
 * desktop/package.json's version, substituted by esbuild at build time.
 *
 * Not `app.getVersion()`: that reads whichever package.json Electron
 * decided was the app's, which in dev (`electron dist/main.cjs`) is none
 * of them and reports "0.0". This version pins the PyPI release the shell
 * runs in packaged builds, so it has to be the real one every time.
 */
declare const __SHELL_VERSION__: string | undefined;
export const SHELL_VERSION: string =
  typeof __SHELL_VERSION__ === 'string' ? __SHELL_VERSION__ : '0.0.0-dev';

/**
 * What this shell can do for the page, feature-detected rather than
 * sniffed. The frontend must treat a missing `window.collimateDesktop`
 * (plain browser) and a false capability identically — degrade, don't
 * break. Individual flags flip on as the main-process implementations
 * land; a flag is true only once the shell can actually do the thing.
 */
export interface DesktopCapabilities {
  /** The shell hosts native guest views (`createGuest` and friends). */
  nativeGuests: boolean;
  /** The shell can capture a still image of a guest, visible or not. */
  guestCapture: boolean;
  /** Named, disk-backed session partitions survive a restart. */
  persistentPartitions: boolean;
  /** Loopback URLs load directly, without a proxy hop through the server. */
  directLoopback: boolean;
  /** `openExternal` hands URLs to the OS browser. */
  externalOpen: boolean;
  /**
   * The window has no native title bar and the page is expected to render
   * the VS Code-style bar: app icon, menu buttons, drag region. True on
   * Windows/Linux, false on macOS (whose native bar stays).
   */
  customTitleBar: boolean;
  /**
   * The page must draw the min/max/close buttons too. Linux only: the
   * window is fully frameless there (`frame: false`, VS Code's approach —
   * Chromium's overlay controls come with a CSD inset/border on Linux
   * that reads as a gap around the window). On Windows the shell's
   * `titleBarOverlay` draws native controls instead, and on macOS the
   * native bar has them.
   */
  customWindowControls: boolean;
}

/** The id of the one local server, the server this shell starts itself. */
export const LOCAL_SERVER_ID = 'local';

/**
 * Where a server runs: `local` is the one server this shell starts (or
 * adopts) out of its own data dir, POSIX only; `remote` is a saved server
 * someone else runs.
 */
export type ServerKind = 'local' | 'remote';

/** The server this window is bound to. One window shows one server. */
export interface ServerInfo {
  /** {@link LOCAL_SERVER_ID}, or the id the shell minted when the remote was saved. */
  id: string;
  kind: ServerKind;
  /** What the Servers list calls it: the saved name, else the URL's host. */
  name: string;
  /** Origin the UI is served from, no trailing slash. e.g. `http://127.0.0.1:53219` */
  url: string;
  port: number;
  /**
   * Directory the server keeps state in (`.collimate/`).
   * `null` for a remote — that server isn't ours and its data dir is
   * none of our business.
   */
  dataDir: string | null;
  /** `version` as reported by the server's /health. */
  serverVersion: string | null;
}

// ---------------------------------------------------------------------------
// Servers registry
// ---------------------------------------------------------------------------

/**
 * One server's state as the registry last saw it.
 *
 * - `starting`: the local server is coming up, or a saved remote has not
 *   been checked yet (the registry polls every remote as it starts).
 * - `running`: /health answered with this shell's bridge and the channel
 *   catalog was readable.
 * - `stopped`: the local server is not running (stopped, crashed, or never
 *   started this session).
 * - `unreachable`: a remote did not answer /health.
 * - `unauthorized`: a remote answered but refused the saved token (or has
 *   one and none is saved).
 * - `mismatch`: a remote speaks another bridge protocol.
 * - `failed`: the local server failed to start (`error` says why).
 */
export type ServerStatus =
  | 'starting'
  | 'running'
  | 'stopped'
  | 'unreachable'
  | 'unauthorized'
  | 'mismatch'
  | 'failed';

/** A channel as the catalog lists it. */
export interface ServerChannel {
  id: string;
  name: string;
}

/**
 * A server in the registry, shaped for the page. Never carries a token:
 * `hasToken` says whether the shell holds one.
 */
export interface ServerEntry {
  id: string;
  kind: ServerKind;
  name: string;
  url: string;
  status: ServerStatus;
  serverVersion: string | null;
  hasToken: boolean;
  /** The server's channels, or null while they could not be read. */
  channels: ServerChannel[] | null;
  activeChannelId: string | null;
  /** True for the server this window shows. */
  isWindowServer: boolean;
  /** A sentence about the last failure, when there was one. */
  error?: string;
}

/** A remote server to add, as typed on the page. The token is sent once. */
export interface ServerDraft {
  name?: string;
  url: string;
  token?: string;
}

/** What a probe of a candidate server found. */
export type ServerProbeResult =
  | { ok: true; serverVersion: string }
  | { ok: false; message: string };

/** The shell's own preferences, set from the page's Servers tab. */
export interface ShellPrefs {
  /** Quitting the app releases the local server instead of stopping it. */
  keepLocalRunning: boolean;
}

/** Something the shell asks the page to do (main -> page). */
export type ShellCommand = { type: 'openServers' } | { type: 'localStopFailed'; message: string };

/**
 * The servers registry, owned by the shell. Every op is answered only for
 * the app window's own webContents; removing a server, changing a token
 * and stopping the local server are confirmed by a native dialog in the
 * shell, because remote-served pages reach this bridge too.
 */
export interface ServersOps {
  /** Every server with fresh statuses (the shell polls before answering). */
  list(): Promise<ServerEntry[]>;
  /** Probe, then save a remote; resolves with its entry. */
  add(draft: ServerDraft): Promise<ServerEntry>;
  /** Rename a remote or replace its token. */
  update(patch: { id: string; name?: string; token?: string }): Promise<ServerEntry>;
  /** Forget a saved remote. Not the window's own server. */
  remove(id: string): Promise<void>;
  /** Check a candidate server without saving anything. */
  probe(candidate: { url: string; token?: string }): Promise<ServerProbeResult>;
  /** Recreate the window on that server's origin. */
  openServer(id: string): Promise<void>;
  /** Select `channelId` on that server, then recreate the window on it. */
  openChannel(id: string, channelId: string): Promise<void>;
  startLocal(): Promise<void>;
  stopLocal(): Promise<void>;
  getPrefs(): Promise<ShellPrefs>;
  setPrefs(patch: Partial<ShellPrefs>): Promise<ShellPrefs>;
}

/**
 * What every servers/prefs invoke answers: the value, or a sentence the
 * preload rejects with (so the page sees the shell's message, not
 * Electron's "Error invoking remote method" wrapping).
 */
export type ServersReply<T> = { value: T } | { error: string };

// ---------------------------------------------------------------------------
// Guest views
// ---------------------------------------------------------------------------

/**
 * The only session partitions the shell will manage.
 *
 * Two things depend on this prefix. A `createGuest` whose `partition`
 * doesn't start with it is refused, which guarantees no guest ever lands
 * in the app's own session; and `clearPartition` will only wipe a
 * partition under it, which is what makes exposing storage clearing to
 * the page safe at all — the page can only ask us to erase storage it
 * could have created.
 *
 * `persist:` is part of the prefix on purpose: guest state is disk-backed
 * and survives a restart (that's `capabilities.persistentPartitions`). If
 * an in-memory partition is ever wanted, it needs its own prefix and its
 * own decision, not a hole in this one.
 */
export const PARTITION_PREFIX = 'persist:collimate-';

/**
 * The device a guest presents as. The shell composes the user agent from
 * its own Chromium version and the screen emulation (main/guestDevices.ts);
 * the page only names the device.
 */
export type GuestDevice = 'desktop' | 'phone' | 'tablet';

/**
 * What the page decides about a guest at creation time.
 *
 * `guestId` is minted by the renderer (it is the pool's key) and the
 * shell only checks it for shape and uniqueness — every later call
 * addresses the guest by it, so the page can never reach a webContents
 * it didn't create through this call.
 */
export interface GuestCreateOptions {
  /** Renderer-minted id. Non-empty string; must be unused in this window. */
  guestId: string;
  /** Must be under {@link PARTITION_PREFIX}; anything else is refused. */
  partition: string;
  /** Initial URL. http(s) or about:blank — anything else is refused. */
  url: string;
  /** The device the guest presents as; absent = desktop. */
  device?: GuestDevice;
  /**
   * Mute the guest's audio while it is hidden. The page decides —
   * a frozen-but-audible video is wrong for an ordinary page, but a
   * keepAlive session guest may depend on behaving exactly like a
   * visible one. Defaults to false (real behavior).
   */
  muteWhenHidden?: boolean;
}

/** What `createGuest` resolves with. */
export interface GuestState {
  guestId: string;
  /** The URL the guest was created on. */
  url: string;
}

/**
 * One guest's target rectangle, in window CSS pixels.
 *
 * The renderer pre-snaps every value with `v => Math.floor(v * z) / z`
 * (z = `zoomFactor`), so the shell's `Math.round(v * z)` is an identity
 * and the native view lands on exactly the pixels the placeholder
 * occupies. `radius` is a uniform corner radius — the native view cannot
 * express per-corner rounding, so the renderer sends the largest of the
 * placeholder's corners.
 */
export interface GuestLayoutRect {
  guestId: string;
  x: number;
  y: number;
  w: number;
  h: number;
  radius: number;
}

/**
 * The layout hot path: one fire-and-forget message per layout pass,
 * carrying every guest that moved. Never an invoke — bounds ride ~1–2
 * frames behind the DOM as it is, and a round-trip per frame would only
 * stretch that.
 */
export interface GuestLayoutBatch {
  guests: GuestLayoutRect[];
  /** The window's zoom factor the rects were snapped against. */
  zoomFactor: number;
}

/** Something a guest did, forwarded from the main process. */
export type GuestEvent =
  | { guestId: string; kind: 'download'; data: { status: 'started' | 'completed' | 'refused' | 'failed'; fileName: string; reason?: string } }
  | { guestId: string; kind: 'navigate'; data: { url: string; inPage: boolean } }
  | { guestId: string; kind: 'title'; data: { title: string } }
  | { guestId: string; kind: 'loading'; data: { loading: boolean } }
  | { guestId: string; kind: 'crashed'; data: { reason: string; exitCode: number } }
  /**
   * The guest's webContents gained or lost input focus. The page's DOM
   * cannot see this — a native view is not an element, and focusing it
   * blurs the page without telling it who won.
   */
  | { guestId: string; kind: 'focus'; data: { focused: boolean } }
  /**
   * The guest took a mouse wheel (and scrolls by it natively). The page's
   * DOM never sees it, so this is how a wheel over a live guest can still
   * select the guest's cell. DOM wheel terms: px, positive scrolls
   * down/right.
   */
  | { guestId: string; kind: 'wheel'; data: { deltaX: number; deltaY: number; ctrlKey: boolean } }
  /**
   * A main-frame load finished (`did-finish-load`). The first one is the
   * pool's readiness signal — the old `<webview>` `dom-ready` DOM event
   * with the main process as its one source of truth.
   */
  | { guestId: string; kind: 'ready'; data: { url: string } };

/**
 * Why the shell refused a guest operation.
 *
 * Every rejection out of the guest IPC surface carries one of these, so
 * "that guest is gone" and "the shell said no" are distinguishable
 * without matching on prose.
 */
export type GuestRefusal =
  | 'not-embedder'
  | 'unknown-guest'
  | 'duplicate-guest'
  | 'guest-gone'
  | 'bad-argument'
  | 'bad-url'
  | 'bad-partition'
  | 'devtools-unavailable';

/** Marks a rejection as a deliberate refusal: `collimate-guest/<reason>: …`. */
export const GUEST_REFUSAL_PREFIX = 'collimate-guest/';

/**
 * Control of the guest views the page has created.
 *
 * `guestId` is the renderer-minted string the page passed to
 * `createGuest`. The shell only answers for ids created through that
 * call on this window, so the page can't reach anything else with one;
 * see main/guestViews.ts.
 */
export interface GuestOps {
  /** Create a guest view. Resolves once the view exists (not loaded). */
  createGuest(options: GuestCreateOptions): Promise<GuestState>;
  /** Remove the view and end its renderer process. Idempotent. */
  destroyGuest(guestId: string): Promise<void>;
  /**
   * Show or hide the native view. Hiding never stops the page — its
   * webContents keeps running (that is what makes the frozen-placeholder
   * presentation possible at all). Showing is deferred until the guest
   * has received its first layout, so a never-laid-out view can't flash
   * at stale bounds.
   */
  setGuestVisible(guestId: string, visible: boolean): Promise<void>;
  /** Move the guest to the top of the window's view z-order. */
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
  /**
   * Present the guest as another device: its user agent and screen
   * emulation change and it reloads in place, keeping its session and
   * history. An unknown device is refused with `bad-argument`.
   */
  setGuestDevice(guestId: string, device: GuestDevice): Promise<void>;
  /** Dev builds only; refused in a packaged app. */
  openDevTools(guestId: string): Promise<void>;
  /** Wipe a `persist:collimate-` partition's storage. */
  clearPartition(partition: string): Promise<void>;
}

/** One top-level entry of the application menu, for the page's menu bar. */
export interface TitleBarMenu {
  label: string;
}

/**
 * The page-drawn title bar's verbs. Requires `capabilities.customTitleBar`
 * — on macOS the native bar is still there and none of this applies.
 *
 * The page renders the bar; the shell keeps owning the actual menu (the
 * same application menu macOS shows), so a menu click round-trips: the
 * page asks for the top-level labels, draws them as buttons, and pops the
 * real native submenu under whichever one was clicked.
 */
export interface TitleBarOps {
  /** Top-level application-menu entries, in order. */
  menus(): Promise<TitleBarMenu[]>;
  /** Pop the native submenu of entry `index` at page coordinates (x, y). */
  popupMenu(index: number, x: number, y: number): Promise<void>;
  /**
   * Maximize, or restore when already maximized — the double-click-on-the-
   * title-bar gesture the native bar would have provided. Resolves to the
   * new maximized state.
   */
  toggleMaximize(): Promise<boolean>;
  // The page-drawn window controls (`capabilities.customWindowControls`).
  minimize(): Promise<void>;
  close(): Promise<void>;
  /** For the maximize/restore button's icon; poll on the page's resize. */
  isMaximized(): Promise<boolean>;
}

/** The window colours that follow the page's active skin. */
export interface WindowTheme {
  /** `BrowserWindow.setBackgroundColor`: the skin's canvas. */
  background: string;
  /** The Windows caption-button overlay's fill: the skin's sheet. */
  overlay: string;
  /** The overlay's glyph colour: the skin's ink. */
  symbol: string;
}

/**
 * `window.collimateDesktop`: enough for the frontend to know it's in the
 * shell, which server it's talking to, how to open a link in the real
 * browser, and how to drive the native guests it creates. Everything
 * beyond the identity fields is capability-gated, because the same
 * frontend runs in a plain browser tab with none of it.
 */
export interface CollimateDesktop {
  /** desktop/package.json version. */
  shellVersion: string;
  /** BRIDGE_PROTOCOL, so the page can gate on it without importing us. */
  bridge: number;
  platform: NodeJS.Platform;
  capabilities: DesktopCapabilities;
  server: ServerInfo;
  /** Hand a http(s) URL to the OS browser. Resolves false if refused. */
  openExternal(url: string): Promise<boolean>;
  /**
   * The native folder dialog: the picked folder's absolute path, or null
   * when cancelled or refused. Answered only for the app window while it is
   * bound to the local server, whose filesystem that path names.
   */
  pickFolder(): Promise<string | null>;
  /** Current page zoom, before any geometry is sent to the native host. */
  getZoomFactor(): number;
  /** Current factor immediately, then changes; returns an unsubscribe. */
  onZoomChanged(handler: (factor: number) => void): () => void;
  /**
   * This device's interface scale, one of {@link INTERFACE_SCALES}: the
   * app window's relative size, kept in the shell's settings.json.
   */
  getInterfaceScale(): number;
  /**
   * Apply and persist an interface scale. Fire-and-forget; main ignores
   * anything not in {@link INTERFACE_SCALES}.
   */
  setInterfaceScale(factor: number): void;
  /**
   * Repaint the window's background and caption overlay in the page's
   * skin. Fire-and-forget; the page calls it on every skin it applies.
   */
  setWindowTheme(theme: WindowTheme): void;
  /** Requires `capabilities.nativeGuests`. */
  guest: GuestOps;
  /** Requires `capabilities.customTitleBar`. */
  titleBar: TitleBarOps;
  /**
   * Guest lifecycle, pushed rather than polled. Returns an unsubscribe.
   *
   * The main process is the only side that sees a guest at all now —
   * navigation, titles, load state, focus, and above all a renderer that
   * died all arrive here.
   */
  onGuestEvent(handler: (event: GuestEvent) => void): () => void;
  /** The servers registry (see {@link ServersOps}). */
  servers: ServersOps;
  /** The registry's entries whenever they change. Returns an unsubscribe. */
  onServersChanged(handler: (entries: ServerEntry[]) => void): () => void;
  /** Commands from the shell's menu (File → Servers…). Returns an unsubscribe. */
  onShellCommand(handler: (command: ShellCommand) => void): () => void;
}

/**
 * Injected into the renderer as a preload `additionalArguments` payload
 * rather than fetched over IPC, so `window.collimateDesktop` is fully
 * populated before the page's first script runs — no async hole where the
 * frontend can observe a half-built bridge.
 */
export interface RendererBootstrap {
  shellVersion: string;
  server: ServerInfo;
  capabilities: DesktopCapabilities;
  /** The interface scale when the window opened; main pushes changes. */
  interfaceScale: number;
}

/**
 * What this build of the shell reports.
 *
 * Four are static: main/guestViews.ts manages guest views — they create,
 * they capture, their partitions are `persist:`-backed. Three more are
 * computed per window via {@link shellCapabilities}, never hardcoded:
 * `directLoopback` depends on which server the window is bound to — only a
 * loopback server's ports are reachable from this machine. (Guests still
 * load their own target sites directly either way — this capability is
 * about the *server's* loopback ports, i.e. portals.) `customTitleBar` and
 * `customWindowControls` depend on the platform.
 */
export const SHELL_CAPABILITIES: Omit<
  DesktopCapabilities,
  'directLoopback' | 'customTitleBar' | 'customWindowControls'
> = {
  nativeGuests: true,
  guestCapture: true,
  persistentPartitions: true,
  externalOpen: true,
};

/**
 * Whether the shell hides the native title bar and expects the page to
 * draw one (VS Code-style). Windows and Linux: yes. Windows keeps the native
 * caption buttons through `titleBarOverlay`; Linux uses `frame: false` and
 * the page draws the window controls too (`customWindowControls`), since
 * the native frame is unreliable there (a Wayland session often renders the
 * window with no decorations at all). macOS keeps its native bar. See
 * main/window.ts titleBarWindowOptions.
 */
export function usesCustomTitleBar(platform: NodeJS.Platform): boolean {
  return platform !== 'darwin';
}

/** True if `url` is on `origin`. Anything unparseable is not. */
export function isSameOrigin(url: string, origin: string): boolean {
  try {
    return new URL(url).origin === new URL(origin).origin;
  } catch {
    return false;
  }
}

/** True when `url`'s host is this machine's loopback. */
export function isLoopbackUrl(url: string): boolean {
  try {
    const host = new URL(url).hostname;
    return (
      host === 'localhost' ||
      host === '127.0.0.1' ||
      host === '[::1]' ||
      host === '::1' ||
      host.endsWith('.localhost')
    );
  } catch {
    return false;
  }
}

/** The capability set for a window bound to `serverUrl`. */
export function shellCapabilities(
  serverUrl: string,
  platform: NodeJS.Platform = process.platform,
): DesktopCapabilities {
  return {
    ...SHELL_CAPABILITIES,
    directLoopback: isLoopbackUrl(serverUrl),
    customTitleBar: usesCustomTitleBar(platform),
    customWindowControls: platform === 'linux',
  };
}

// ---------------------------------------------------------------------------
// Splash surface
// ---------------------------------------------------------------------------

/** What the splash window is currently showing. */
export type SplashView =
  | { kind: 'status'; message: string; detail?: string }
  | {
      kind: 'failure';
      title: string;
      message: string;
      /** The exact command that failed, verbatim, when there was one. */
      command?: string;
      /** Tail of the server log, newest last. */
      tail: string[];
    }
  | {
      kind: 'connect';
      url: string;
      error?: string;
      busy: boolean;
      /** Overrides the default "Connect to a Collimate server" heading. */
      title?: string;
      /** Offer "Start local server" (POSIX only). */
      canUseLocal?: boolean;
      /** Clears the access-token field: the shell never sends a token to the page. */
      token?: '';
      /** Saved remote servers to offer as one-click choices. */
      servers?: { id: string; name?: string; url: string }[];
    };

/** main -> splash renderer. */
export type SplashEvent =
  | { type: 'view'; view: SplashView }
  | { type: 'log'; line: string };

/** splash renderer -> main. */
export type SplashAction =
  | { type: 'retry' }
  /** A typed URL (and token), or a saved server picked by `serverId`. */
  | { type: 'connect'; url: string; token?: string; serverId?: string }
  /** Start the local server and open the window on it. */
  | { type: 'spawn' }
  | { type: 'quit' };

/** What the preload exposes on the splash surface. */
export interface CollimateSplash {
  onEvent(handler: (event: SplashEvent) => void): void;
  send(action: SplashAction): void;
}

// ---------------------------------------------------------------------------
// IPC channel names + preload argv keys
// ---------------------------------------------------------------------------

export const IPC = {
  /** invoke(url) -> boolean */
  openExternal: 'collimate:open-external',
  /** invoke() -> string | null */
  pickFolder: 'collimate:pick-folder',
  /** main -> splash renderer */
  splashEvent: 'collimate:splash-event',
  /** splash renderer -> main */
  splashAction: 'collimate:splash-action',

  // Guest control. Every one of these takes a single object payload and is
  // answered only for the app window's own webContents — see
  // main/guestViews.ts. The four channels behind the SDK's session RPC
  // (evaluate, capture-png, current-url, navigate; the RPC's fifth verb,
  // html, is answered renderer-side via evaluate) are frozen — see
  // tests/bridgeSurface.test.ts.
  /** invoke(GuestCreateOptions) -> GuestState */
  guestCreate: 'collimate:guest-create',
  /** invoke({ guestId }) -> void */
  guestDestroy: 'collimate:guest-destroy',
  /** invoke({ guestId, visible }) -> void */
  guestSetVisible: 'collimate:guest-set-visible',
  /** invoke({ guestId }) -> void */
  guestRaise: 'collimate:guest-raise',
  /** send(GuestLayoutBatch) — fire-and-forget, the layout hot path */
  guestLayout: 'collimate:guest-layout',
  /** invoke({ guestId, js }) -> unknown */
  guestEvaluate: 'collimate:guest-evaluate',
  /** invoke({ guestId }) -> ArrayBuffer */
  guestCapturePng: 'collimate:guest-capture-png',
  guestCapturePlaceholder: 'collimate:guest-capture-placeholder',
  /** invoke({ guestId }) -> string */
  guestCurrentUrl: 'collimate:guest-current-url',
  /** invoke({ guestId, url }) -> void */
  guestNavigate: 'collimate:guest-navigate',
  /** invoke({ guestId }) -> boolean */
  guestGoBack: 'collimate:guest-go-back',
  /** invoke({ guestId }) -> boolean */
  guestGoForward: 'collimate:guest-go-forward',
  /** invoke({ guestId }) -> void */
  guestReload: 'collimate:guest-reload',
  /** invoke({ guestId, device }) -> void */
  guestSetDevice: 'collimate:guest-set-device',
  /** invoke({ guestId }) -> void, dev builds only */
  guestOpenDevTools: 'collimate:guest-open-devtools',
  /** invoke({ partition }) -> void */
  clearPartition: 'collimate:session-clear-partition',
  /** main -> app window renderer: GuestEvent */
  guestEvent: 'collimate:guest-event',

  // Page-drawn title bar (see TitleBarOps). Answered only for the app
  // window's own webContents — see main/titleBar.ts.
  /** invoke() -> TitleBarMenu[] */
  titleBarMenus: 'collimate:titlebar-menus',
  /** invoke({ index, x, y }) -> void */
  titleBarPopupMenu: 'collimate:titlebar-popup-menu',
  /** invoke() -> boolean (the new maximized state) */
  titleBarToggleMaximize: 'collimate:titlebar-toggle-maximize',
  /** invoke() -> void */
  titleBarMinimize: 'collimate:titlebar-minimize',
  /** invoke() -> void */
  titleBarClose: 'collimate:titlebar-close',
  /** invoke() -> boolean */
  titleBarIsMaximized: 'collimate:titlebar-is-maximized',
  /** send(WindowTheme) — fire-and-forget, on every skin the page applies */
  windowTheme: 'collimate:window-theme',
  /**
   * send(number) page -> main: apply and persist an interface scale.
   * main -> page: the scale now applied (after every load and change).
   */
  interfaceScale: 'collimate:interface-scale',

  // The servers registry (see ServersOps). Answered only for the app
  // window's own webContents — see main/index.ts installServersIpc.
  // Every invoke answers `{ value }` or `{ error }`.
  /** invoke() -> ServerEntry[] */
  serversList: 'collimate:servers-list',
  /** invoke(ServerDraft) -> ServerEntry */
  serversAdd: 'collimate:servers-add',
  /** invoke({ id, name?, token? }) -> ServerEntry */
  serversUpdate: 'collimate:servers-update',
  /** invoke({ id }) -> void, after a native confirmation */
  serversRemove: 'collimate:servers-remove',
  /** invoke({ url, token? }) -> ServerProbeResult */
  serversProbe: 'collimate:servers-probe',
  /** invoke({ id }) -> void */
  serversOpen: 'collimate:servers-open',
  /** invoke({ id, channelId }) -> void */
  serversOpenChannel: 'collimate:servers-open-channel',
  /** invoke() -> void */
  serversStartLocal: 'collimate:servers-start-local',
  /** invoke() -> void, after a native confirmation */
  serversStopLocal: 'collimate:servers-stop-local',
  /** invoke() -> ShellPrefs */
  shellPrefsGet: 'collimate:shell-prefs-get',
  /** invoke(Partial<ShellPrefs>) -> ShellPrefs */
  shellPrefsSet: 'collimate:shell-prefs-set',
  /** main -> app window renderer: ServerEntry[] */
  serversChanged: 'collimate:servers-changed',
  /** main -> app window renderer: ShellCommand */
  shellCommand: 'collimate:shell-command',
} as const;

/** `--collimate-surface=main|splash`, read by the preload from process.argv. */
export const ARG_SURFACE = '--collimate-surface=';
/** `--collimate-bootstrap=<base64 json RendererBootstrap>`. */
export const ARG_BOOTSTRAP = '--collimate-bootstrap=';

export type Surface = 'main' | 'splash';
