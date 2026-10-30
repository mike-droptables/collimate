import { readFileSync } from 'node:fs';
import type { BrowserWindow, Menu } from 'electron';
import { describe, expect, it, vi } from 'vitest';
import { IPC } from '../src/shared/bridge.js';
import {
  applyWindowTheme,
  installTitleBarControl,
  parseWindowTheme,
  popupableItems,
  type ThemeableWindow,
} from '../src/main/titleBar.js';
import { NIGHT_WINDOW_THEME, TITLE_BAR_HEIGHT, titleBarWindowOptions } from '../src/main/window.js';

/** The ipcMain registrations installTitleBarControl makes, by channel. */
const ipc = vi.hoisted(() => ({
  listeners: new Map<string, (event: { sender: unknown }, payload: unknown) => void>(),
}));

vi.mock('electron', () => ({
  BrowserWindow: class {},
  shell: {},
  Menu: { getApplicationMenu: () => null },
  ipcMain: {
    handle: () => undefined,
    removeHandler: () => undefined,
    on: (channel: string, listener: (event: { sender: unknown }, payload: unknown) => void) => {
      ipc.listeners.set(channel, listener);
    },
    removeListener: (channel: string) => {
      ipc.listeners.delete(channel);
    },
  },
}));

/**
 * The page-drawn title bar's two pure seams.
 *
 * `popupableItems` is the one filter both IPC verbs share — the listing
 * and the popup must address the same items by the same index, so what's
 * worth freezing is that hidden and submenu-less entries drop out before
 * numbering, not after. The window options matter because the capability
 * flag (`shellCapabilities().customTitleBar`) promises the page a hidden
 * native bar: the two must flip on the same platforms or the app renders
 * two title bars (or none).
 */

function fakeMenu(items: Partial<Menu['items'][number]>[]): Menu {
  return { items } as unknown as Menu;
}

describe('popupableItems', () => {
  it('no application menu → nothing to draw', () => {
    expect(popupableItems(null)).toEqual([]);
  });

  it('keeps only visible entries that have a submenu to pop', () => {
    const submenu = {} as Menu;
    const menu = fakeMenu([
      { label: 'File', submenu, visible: true },
      { label: 'Bare', visible: true }, // no submenu — nothing to pop
      { label: 'Hidden', submenu, visible: false },
      { label: 'Help', submenu, visible: true },
    ]);
    expect(popupableItems(menu).map((item) => item.label)).toEqual(['File', 'Help']);
  });
});

describe('titleBarWindowOptions', () => {
  it('macOS keeps the native bar', () => {
    expect(titleBarWindowOptions('darwin')).toEqual({});
  });

  it('Linux is fully frameless — the page draws everything, no CSD inset', () => {
    expect(titleBarWindowOptions('linux')).toEqual({ frame: false });
  });

  it('Windows hides the bar and draws native overlay controls', () => {
    const options = titleBarWindowOptions('win32');
    expect(options.frame).toBeUndefined();
    expect(options.titleBarStyle).toBe('hidden');
    expect(options.autoHideMenuBar).toBe(true);
    const overlay = options.titleBarOverlay;
    expect(typeof overlay).toBe('object');
    expect((overlay as { height: number }).height).toBe(TITLE_BAR_HEIGHT);
  });

  it('Windows opens its overlay in the night skin\'s sheet and ink (S05)', () => {
    expect(titleBarWindowOptions('win32').titleBarOverlay).toEqual({
      color: NIGHT_WINDOW_THEME.overlay,
      symbolColor: NIGHT_WINDOW_THEME.symbol,
      height: TITLE_BAR_HEIGHT,
    });
  });
});

/**
 * The night skin's `vars` block in the frontend's themes.ts, read as text:
 * the shell cannot import the frontend, but its pre-first-frame colours
 * must be that skin's or the window flashes a stale colour.
 */
function nightSkinVars(): Record<string, string> {
  const source = readFileSync(new URL('../../frontend/src/lib/themes.ts', import.meta.url), 'utf8');
  const block = /id: 'night',[\s\S]*?vars: \{([\s\S]*?)\n {4}\},/.exec(source)?.[1];
  if (block === undefined) throw new Error("themes.ts has no id: 'night' vars block");
  const vars: Record<string, string> = {};
  for (const match of block.matchAll(/^\s*(\w+): '([^']*)',/gm)) vars[match[1]!] = match[2]!;
  return vars;
}

/** The splash's `:root` custom properties, keyed by their camelCased name. */
function splashRootVars(): Record<string, string> {
  const html = readFileSync(new URL('../src/splash/splash.html', import.meta.url), 'utf8');
  const root = /:root \{([\s\S]*?)\}/.exec(html)?.[1];
  if (root === undefined) throw new Error('splash.html has no :root block');
  const vars: Record<string, string> = {};
  for (const match of root.matchAll(/--([\w-]+):\s*([^;]+);/g)) {
    vars[match[1]!.replace(/-(\w)/g, (_, c: string) => c.toUpperCase())] = match[2]!.trim();
  }
  return vars;
}

/** Splash properties that are layout, not skin colour. */
const SPLASH_NON_SKIN = new Set(['radius', 'mono']);

describe('night skin drift', () => {
  it('the window opens in the night skin\'s canvas, sheet and ink (S05)', () => {
    const night = nightSkinVars();
    expect(NIGHT_WINDOW_THEME).toEqual({ background: night.canvas, overlay: night.sheet, symbol: night.ink });
  });

  it('every splash colour is the night skin\'s value (S05)', () => {
    const night = nightSkinVars();
    const splash = splashRootVars();
    const colours = Object.keys(splash).filter(key => !SPLASH_NON_SKIN.has(key));
    expect(colours).toEqual(expect.arrayContaining(['canvas', 'sheet', 'sheet2', 'line', 'line2', 'ink', 'ink2', 'ink3', 'brass', 'teal', 'red']));
    for (const key of colours) expect(splash[key], `splash ${key}`).toBe(night[key]);
  });
});

/** Records the BrowserWindow calls a skin change makes. */
function fakeThemeable(): ThemeableWindow & { calls: unknown[][] } {
  const calls: unknown[][] = [];
  return {
    calls,
    setBackgroundColor: (color) => calls.push(['background', color]),
    setTitleBarOverlay: (options) => calls.push(['overlay', options]),
  };
}

const PAPER = { background: '#e6e4de', overlay: '#f8f7f3', symbol: '#1e2227' };

describe('setWindowTheme', () => {
  it('on Windows the background and the overlay colours follow the op (S05)', () => {
    const window = fakeThemeable();
    expect(applyWindowTheme(window, NIGHT_WINDOW_THEME, 'win32')).toBe(true);
    expect(applyWindowTheme(window, PAPER, 'win32')).toBe(true);
    expect(window.calls).toEqual([
      ['background', '#151311'],
      ['overlay', { color: '#1c1a17', symbolColor: '#e2ddd4' }],
      ['background', '#e6e4de'],
      ['overlay', { color: '#f8f7f3', symbolColor: '#1e2227' }],
    ]);
  });

  it.each(['linux', 'darwin'] as const)('on %s only the background follows — there is no overlay (S05)', (platform) => {
    const window = fakeThemeable();
    applyWindowTheme(window, PAPER, platform);
    expect(window.calls).toEqual([['background', '#e6e4de']]);
  });

  it('accepts the colour forms a skin may hold (S05)', () => {
    expect(parseWindowTheme({ background: 'rgb(21, 19, 17)', overlay: 'hsla(30, 8%, 10%, 1)', symbol: 'white' })).toEqual({
      background: 'rgb(21, 19, 17)',
      overlay: 'hsla(30, 8%, 10%, 1)',
      symbol: 'white',
    });
  });

  it.each([
    [null],
    ['#151311'],
    [{ background: '#151311', overlay: '#1c1a17' }],
    [{ background: '#151311', overlay: '#1c1a17', symbol: 42 }],
    [{ background: 'url(x)', overlay: '#1c1a17', symbol: '#e2ddd4' }],
    [{ background: '#151311', overlay: 'var(--sheet)', symbol: '#e2ddd4' }],
  ])('drops a malformed theme without touching the window: %j (S05)', (payload) => {
    const window = fakeThemeable();
    expect(applyWindowTheme(window, payload, 'win32')).toBe(false);
    expect(window.calls).toEqual([]);
  });

  it('answers only the app window and stops listening on dispose (S05)', () => {
    const themed = fakeThemeable();
    const webContents = { getZoomFactor: () => 1 };
    const window = {
      ...themed,
      webContents,
      isDestroyed: () => false,
      once: () => undefined,
    } as unknown as BrowserWindow;
    const dispose = installTitleBarControl({ window });
    const listener = ipc.listeners.get(IPC.windowTheme);
    expect(listener).toBeDefined();

    listener!({ sender: {} }, PAPER);
    expect(themed.calls).toEqual([]);
    listener!({ sender: webContents }, PAPER);
    expect(themed.calls[0]).toEqual(['background', '#e6e4de']);

    dispose();
    expect(ipc.listeners.has(IPC.windowTheme)).toBe(false);
  });
});
