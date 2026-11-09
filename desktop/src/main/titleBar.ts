import { ipcMain, Menu, type BrowserWindow, type MenuItem } from 'electron';
import { IPC, type TitleBarMenu, type WindowTheme } from '../shared/bridge.js';

/**
 * The main-process half of the page-drawn title bar (see TitleBarOps in
 * shared/bridge.ts): the page renders the bar, this answers its verbs and
 * repaints the window chrome in the page's skin (`setWindowTheme`).
 * The application menu itself stays owned here — the page only gets
 * labels back and pops the real native submenus, so accelerators, roles,
 * and the switch-server/update items keep working unduplicated.
 */

/** Marks a rejection as deliberate: `collimate-titlebar/<reason>`. */
const REFUSAL_PREFIX = 'collimate-titlebar/';

function refuse(reason: string): Error {
  return new Error(`${REFUSAL_PREFIX}${reason}`);
}

/**
 * The top-level entries the page may draw: those with a submenu to pop.
 * One filter, used by both the listing and the popup, so the page's index
 * always addresses the same item it was shown.
 */
export function popupableItems(menu: Menu | null): MenuItem[] {
  if (!menu) return [];
  return menu.items.filter((item) => item.submenu !== undefined && item.visible !== false);
}

/**
 * A colour the page may hand the window: hex, a named colour, or an
 * rgb()/hsl() form — what `setBackgroundColor` and the overlay accept.
 */
const WINDOW_COLOR = /^(#[0-9a-f]{3,8}|[a-z]+|(rgb|hsl)a?\([0-9.,%\s/]+\))$/i;

function windowColor(value: unknown): string | null {
  return typeof value === 'string' && value.length <= 64 && WINDOW_COLOR.test(value.trim()) ? value.trim() : null;
}

/** A WindowTheme from the page, or null when any colour is not one. */
export function parseWindowTheme(payload: unknown): WindowTheme | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const { background, overlay, symbol } = payload as Record<string, unknown>;
  const theme = { background: windowColor(background), overlay: windowColor(overlay), symbol: windowColor(symbol) };
  if (theme.background === null || theme.overlay === null || theme.symbol === null) return null;
  return theme as WindowTheme;
}

/** Just the two BrowserWindow calls a skin change makes. */
export interface ThemeableWindow {
  setBackgroundColor(color: string): void;
  setTitleBarOverlay(options: { color: string; symbolColor: string }): void;
}

/**
 * Repaint a window in the page's skin: the background on every platform,
 * the caption overlay where there is one — Windows only (window.ts
 * titleBarWindowOptions; Linux is frameless and macOS keeps its native
 * bar). Returns false, touching nothing, when the payload is not a theme.
 */
export function applyWindowTheme(
  window: ThemeableWindow,
  payload: unknown,
  platform: NodeJS.Platform = process.platform,
): boolean {
  const theme = parseWindowTheme(payload);
  if (!theme) return false;
  window.setBackgroundColor(theme.background);
  if (platform === 'win32') window.setTitleBarOverlay({ color: theme.overlay, symbolColor: theme.symbol });
  return true;
}

export interface TitleBarControlOptions {
  /** The app window. Its webContents is the only sender any handler trusts. */
  window: BrowserWindow;
}

/**
 * Take ownership of one window's title-bar IPC.
 *
 * Returns a disposer, and calls it itself when the window closes — the
 * shell rebuilds the app window after a server crash, and a second install
 * over live `ipcMain.handle` registrations would throw. (Same shape as
 * installGuestViews.)
 */
export function installTitleBarControl(options: TitleBarControlOptions): () => void {
  const { window } = options;
  const host = window.webContents;
  let disposed = false;

  const assertSender = (event: Electron.IpcMainInvokeEvent): void => {
    if (event.sender !== host) throw refuse('not-app-window');
  };

  const handlers: Record<string, (event: Electron.IpcMainInvokeEvent, payload: unknown) => unknown> = {
    [IPC.titleBarMenus]: (event): TitleBarMenu[] => {
      assertSender(event);
      return popupableItems(Menu.getApplicationMenu()).map((item) => ({ label: item.label }));
    },

    [IPC.titleBarPopupMenu]: (event, payload): void => {
      assertSender(event);
      const { index, x, y } = (payload ?? {}) as Record<string, unknown>;
      const items = popupableItems(Menu.getApplicationMenu());
      if (typeof index !== 'number' || !Number.isInteger(index) || index < 0 || index >= items.length) {
        throw refuse('bad-menu-index');
      }
      if (typeof x !== 'number' || typeof y !== 'number' || !Number.isFinite(x) || !Number.isFinite(y)) {
        throw refuse('bad-position');
      }
      // The page reports CSS pixels; native menu coordinates are window DIPs.
      const zoom = host.getZoomFactor();
      items[index]!.submenu!.popup({ window, x: Math.round(x * zoom), y: Math.round(y * zoom) });
    },

    [IPC.titleBarToggleMaximize]: (event): boolean => {
      assertSender(event);
      if (window.isMaximized()) window.unmaximize();
      else window.maximize();
      return window.isMaximized();
    },

    [IPC.titleBarMinimize]: (event): void => {
      assertSender(event);
      window.minimize();
    },

    [IPC.titleBarClose]: (event): void => {
      assertSender(event);
      window.close();
    },

    [IPC.titleBarIsMaximized]: (event): boolean => {
      assertSender(event);
      return window.isMaximized();
    },
  };

  for (const [channel, handler] of Object.entries(handlers)) {
    ipcMain.handle(channel, handler);
  }

  // Fire-and-forget, so a listener rather than a handler: a stray sender
  // or a malformed theme is dropped, never answered.
  const onWindowTheme = (event: Electron.IpcMainEvent, payload: unknown): void => {
    if (event.sender !== host || window.isDestroyed()) return;
    applyWindowTheme(window, payload);
  };
  ipcMain.on(IPC.windowTheme, onWindowTheme);

  const dispose = (): void => {
    if (disposed) return;
    disposed = true;
    for (const channel of Object.keys(handlers)) ipcMain.removeHandler(channel);
    ipcMain.removeListener(IPC.windowTheme, onWindowTheme);
  };

  window.once('closed', dispose);
  return dispose;
}
