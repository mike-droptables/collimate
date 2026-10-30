import { BrowserWindow, shell } from 'electron';
import path from 'node:path';
import {
  ARG_BOOTSTRAP,
  ARG_SURFACE,
  isSameOrigin,
  shellCapabilities,
  usesCustomTitleBar,
  type RendererBootstrap,
  type ServerInfo,
  type WindowTheme,
} from '../shared/bridge.js';
import { isWebUrl } from './guestViews.js';

/** Roomy enough for the grid canvas plus a panel; still fits a 13" laptop. */
const DEFAULT_WIDTH = 1440;
const DEFAULT_HEIGHT = 900;
const MIN_WIDTH = 900;
const MIN_HEIGHT = 600;

/**
 * The page-drawn title bar's height. The overlay window controls are sized
 * to it here and the frontend's DesktopTitleBar reads the real value back
 * via `env(titlebar-area-height)`, so this is the one place it is set.
 */
export const TITLE_BAR_HEIGHT = 37;

/**
 * The night skin's window colours (frontend/src/lib/themes.ts), the ones a
 * window opens in before the page has applied its own skin. The page then
 * repaints them on every skin change through `setWindowTheme`.
 */
export const NIGHT_WINDOW_THEME: WindowTheme = {
  background: '#151311',
  overlay: '#1c1a17',
  symbol: '#e2ddd4',
};

/**
 * VS Code-style title bar on Windows/Linux: no native one — the page draws
 * the bar (icon, menus, drag region: frontend DesktopTitleBar, gated on
 * `capabilities.customTitleBar`). macOS keeps its native bar and menu.
 *
 * The two platforms drop the native bar differently, deliberately:
 * - Windows: `titleBarStyle: 'hidden'` + `titleBarOverlay` — the overlay
 *   min/max/close are the real native controls (snap layouts, hover
 *   states), painted in the skin's sheet and ink (see applyWindowTheme in
 *   main/titleBar.ts).
 * - Linux: `frame: false`, VS Code's approach, and the page draws the
 *   window controls too (`capabilities.customWindowControls`). The overlay
 *   exists on Linux but brings Chromium's CSD frame with it — an
 *   inset/border around the window that reads as a gap against the real
 *   window bounds — and a Wayland session may render no native frame at
 *   all anyway.
 */
export function titleBarWindowOptions(
  platform: NodeJS.Platform,
): Electron.BrowserWindowConstructorOptions {
  if (!usesCustomTitleBar(platform)) return {};
  if (platform === 'linux') return { frame: false };
  return {
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: NIGHT_WINDOW_THEME.overlay,
      symbolColor: NIGHT_WINDOW_THEME.symbol,
      height: TITLE_BAR_HEIGHT,
    },
    // The native menu bar would paint a second, redundant menu row under
    // the page-drawn one. Alt still summons it, as a fallback.
    autoHideMenuBar: true,
  };
}

/**
 * What a window is called when nothing says otherwise, and — because every
 * page this shell loads declares exactly this string as its document title
 * — the value that makes pinWindowTitle below a no-op.
 */
export const DEFAULT_WINDOW_TITLE = 'Collimate';

/**
 * Just enough of a BrowserWindow to pin a title. Structural rather than the
 * real type because no BrowserWindow can be constructed in a unit test, and
 * a seam this narrow is the only way "did the handler get installed?" is
 * assertable at all.
 */
export interface TitlePinnable {
  webContents: {
    on(
      event: 'page-title-updated',
      listener: (event: { preventDefault: () => void }) => void,
    ): unknown;
  };
}

/**
 * Keep a window's title from being replaced by the page it loads.
 *
 * Chromium renames a window to match its document title, and every page the
 * shell loads declares one: the served frontend sets 'Collimate', and
 * splash.html (the only file:// document there is) carries
 * <title>Collimate</title>, which Electron applies over the BrowserWindow
 * `title` constructor option as soon as the document loads. Either would
 * erase a profile title the moment the page arrives, leaving a dev run and
 * the installed app indistinguishable in the taskbar and the window
 * switcher — the exact confusion separate profiles exist to prevent (see
 * main/profile.ts). Preventing the default keeps the title the window was
 * constructed with.
 *
 * Installed only when the title actually differs from the one the pages
 * declare, so the stable app's windows behave byte-for-byte as they did
 * before profiles existed. The return value reports whether it was
 * installed, which is what makes that condition testable.
 */
export function pinWindowTitle(window: TitlePinnable, title: string): boolean {
  if (title === DEFAULT_WINDOW_TITLE) return false;
  window.webContents.on('page-title-updated', (event) => {
    event.preventDefault();
  });
  return true;
}

export interface MainWindowOptions {
  server: ServerInfo;
  shellVersion: string;
  /**
   * The window title, from the app profile (see main/profile.ts). Defaults
   * to DEFAULT_WINDOW_TITLE, which is what the stable app has always used.
   */
  title?: string;
  /** This device's interface scale (settings.json), carried in the bootstrap. */
  interfaceScale: number;
  /** Open the new channel page on this server after a local-server handoff. */
  newChannel?: boolean;
  /** Keep the page hidden until the New channel view is ready. */
  deferShow?: boolean;
  /**
   * Where the window goes: the previous window's normal bounds when the
   * window is recreated on another server, so a server change does not
   * jump it back to the default size and place.
   */
  bounds?: Electron.Rectangle;
  /** Maximize once shown (the previous window was maximized). */
  maximized?: boolean;
}

/**
 * The size and position options for a new main window: the carried
 * bounds when there are any, else the default size (centred by the OS).
 */
export function windowGeometry(bounds: Electron.Rectangle | undefined): Electron.BrowserWindowConstructorOptions {
  if (!bounds) return { width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT };
  return {
    x: bounds.x,
    y: bounds.y,
    width: Math.max(bounds.width, MIN_WIDTH),
    height: Math.max(bounds.height, MIN_HEIGHT),
  };
}

/**
 * The app window. Everything in it comes from the server over HTTP —
 * the shell ships no HTML, CSS, or JS for the UI, which is the property
 * that keeps the desktop app and the browser experience from drifting.
 */
export function createMainWindow(options: MainWindowOptions): BrowserWindow {
  const { server } = options;
  const title = options.title ?? DEFAULT_WINDOW_TITLE;
  const bootstrap: RendererBootstrap = {
    shellVersion: options.shellVersion,
    server,
    // Computed per window: a remote server's loopback ports aren't
    // reachable from this machine, so directLoopback must reflect the
    // server this window is actually bound to — the website renderer
    // falls back to the server's proxy path when it's false.
    capabilities: shellCapabilities(server.url),
    interfaceScale: options.interfaceScale,
  };

  const window = new BrowserWindow({
    ...windowGeometry(options.bounds),
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    // What Chromium paints wherever the page has not: before the first
    // frame, while a resize outruns the renderer, across a GPU process
    // restart. The page keeps it on its skin's canvas via setWindowTheme.
    backgroundColor: NIGHT_WINDOW_THEME.background,
    // Painting an empty window while the SPA boots looks like a hang.
    show: false,
    title,
    // Window/taskbar icon, à la VS Code's window icon. Linux takes it from
    // here in every mode; a dev run on Windows does too (a packaged one
    // prefers the exe resource); macOS ignores it in favour of the bundle.
    // build.mjs copies the png into dist/ beside this bundle.
    icon: path.join(__dirname, 'app-icon.png'),
    ...titleBarWindowOptions(process.platform),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      // No webviewTag: guests are main-process WebContentsViews now,
      // created and driven through installGuestViews (main/guestViews.ts).
      // The page cannot mint a guest by constructing a DOM element.
      additionalArguments: [
        `${ARG_SURFACE}main`,
        `${ARG_BOOTSTRAP}${Buffer.from(JSON.stringify(bootstrap), 'utf8').toString('base64')}`,
      ],
    },
  });

  window.once('ready-to-show', () => {
    if (options.maximized) window.maximize();
    if (!options.deferShow) window.show();
  });

  // The SPA sets `document.title` as soon as it boots; without this the
  // window takes that name and a dev run stops being distinguishable from
  // the installed app.
  pinWindowTitle(window, title);

  // --- navigation policy ---
  //
  // The window is pinned to the server's origin. A link to anywhere else
  // is a link to the web, and the web belongs in the user's browser: it
  // gets the user's extensions, profile, and password manager, and it
  // can't navigate the app away from itself.
  const origin = server.url;

  window.webContents.on('will-navigate', (event, url) => {
    if (isSameOrigin(url, origin)) return;
    event.preventDefault();
    void openExternally(url);
  });

  window.webContents.on('will-redirect', (details) => {
    if (!details.isMainFrame || isSameOrigin(details.url, origin)) return;
    details.preventDefault();
    void openExternally(details.url);
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    void openExternally(url);
    return { action: 'deny' };
  });

  // The whole point: the UI is the server's, fetched over loopback HTTP.
  // A failed load is reported and handled by the did-fail-load listener in
  // index.ts; the rejection itself is swallowed so it doesn't also surface
  // as an 'uncaught exception'.
  window.loadURL(options.newChannel ? `${server.url}/#new-channel` : server.url).catch(() => undefined);

  return window;
}

/**
 * Hand a URL to the OS browser. Only http(s) — a `file:`, `smb:`, or
 * custom-scheme URL from page content is an attempt to make the OS open
 * something, and we don't forward those.
 */
export async function openExternally(url: string): Promise<boolean> {
  if (!isWebUrl(url)) return false;
  try {
    await shell.openExternal(url);
    return true;
  } catch {
    return false;
  }
}
