import { Menu, type MenuItemConstructorOptions } from 'electron';
import { openExternally } from './window.js';

export const WEBSITE_URL = 'https://collimate.dev';

/** Cosmetic on macOS — the OS substitutes the bundle name. */
export const APP_MENU_LABEL = 'Collimate';

export interface MenuOptions {
  isDev: boolean;
  /**
   * The app's name for the macOS app menu. Defaults to APP_MENU_LABEL so
   * every existing caller (and test) keeps the name it had; index.ts passes
   * the resolved profile's name, which is how a dev run says so in the one
   * place macOS puts an app's identity.
   */
  appName?: string;
  /**
   * Wired only when this install has an update path at all
   * (UpdateController.supported). Undefined omits the item entirely,
   * rather than shipping a control that can't do the thing it names —
   * a .deb has no way to update itself and saying so once, by absence,
   * beats saying so in a dialog every time someone clicks.
   */
  onCheckForUpdates?: () => void;
  /**
   * File → Servers…: the page's Settings → Servers tab, or the splash
   * connect form when there is no window. A thunk so the click reads live
   * state (windows) at click time, same as onCheckForUpdates. Undefined
   * omits the item.
   */
  onOpenServers?: () => void;
  /**
   * File → Quit, Keep Server Running: quit once and leave the local server
   * running for the next launch to adopt. Undefined omits the item — there
   * is no local server on Windows. Enabled only while the local server
   * runs (index.ts flips it by id).
   */
  onQuitKeepServer?: () => void;
  /** Whether Quit, Keep Server Running starts out enabled. */
  quitKeepServerEnabled?: boolean;
  /**
   * Steps the interface scale (INTERFACE_SCALES in shared/bridge.ts) and
   * persists it: the View menu's Actual Size / Zoom In / Zoom Out, so the
   * menu and the Themes tab's "Interface size" row never diverge. Undefined
   * omits the three items.
   */
  onInterfaceScale?: (step: InterfaceScaleStep) => void;
}

export type InterfaceScaleStep = 'reset' | 'in' | 'out';

/**
 * The View menu's zoom items, in place of Electron's resetZoom / zoomIn /
 * zoomOut roles: those change the page zoom freely, off the offered scales and
 * unpersisted. Ctrl/Cmd+= is the unshifted Zoom In key on most layouts, so
 * it rides a hidden twin of the item.
 */
function interfaceScaleItems(onStep: (step: InterfaceScaleStep) => void): MenuItemConstructorOptions[] {
  return [
    { id: 'interface-scale-reset', label: 'Actual Size', accelerator: 'CmdOrCtrl+0', click: () => onStep('reset') },
    { id: 'interface-scale-in', label: 'Zoom In', accelerator: 'CmdOrCtrl+Plus', click: () => onStep('in') },
    {
      id: 'interface-scale-in-equals',
      label: 'Zoom In',
      accelerator: 'CmdOrCtrl+=',
      visible: false,
      acceleratorWorksWhenHidden: true,
      click: () => onStep('in'),
    },
    { id: 'interface-scale-out', label: 'Zoom Out', accelerator: 'CmdOrCtrl+-', click: () => onStep('out') },
  ];
}

/**
 * The application menu.
 *
 * The Edit menu is not decoration on macOS: Cmd-C / Cmd-V only work in a
 * BrowserWindow if the roles exist in the menu, so a shell without one
 * ships an app where copy and paste are silently broken.
 */
export function installMenu(options: MenuOptions): void {
  Menu.setApplicationMenu(Menu.buildFromTemplate(buildTemplate(options)));
}

/**
 * The template, separately, so tests can assert what's in the menu and
 * where without an Electron runtime to build it in.
 */
export function buildTemplate(options: MenuOptions): MenuItemConstructorOptions[] {
  const isMac = process.platform === 'darwin';
  const checkForUpdates: MenuItemConstructorOptions | null = options.onCheckForUpdates
    ? { label: 'Check for Updates…', click: options.onCheckForUpdates }
    : null;

  const template: MenuItemConstructorOptions[] = [];

  if (isMac) {
    // `{role: 'appMenu'}` expanded by hand. The platform convention puts
    // Check for Updates… directly under About, and a role-built app menu
    // has no seam to insert into — so the standard template is spelled
    // out here instead. Order and separators match what Electron's own
    // appMenu role generates; the only addition is the one item.
    const appSubmenu: MenuItemConstructorOptions[] = [{ role: 'about' }];
    if (checkForUpdates) appSubmenu.push({ type: 'separator' }, checkForUpdates);
    appSubmenu.push(
      { type: 'separator' },
      { role: 'services' },
      { type: 'separator' },
      { role: 'hide' },
      { role: 'hideOthers' },
      { role: 'unhide' },
      { type: 'separator' },
      { role: 'quit' },
    );
    // Not `{role: 'appMenu', submenu}` — a role with a default submenu is
    // the thing being replaced here, and whether an explicit submenu wins
    // is an Electron implementation detail. macOS titles the first menu
    // with the bundle name regardless of this label.
    template.push({ label: options.appName ?? APP_MENU_LABEL, submenu: appSubmenu });
  }

  const fileSubmenu: MenuItemConstructorOptions[] = [];
  if (options.onOpenServers) {
    fileSubmenu.push({ id: 'servers', label: 'Servers…', click: options.onOpenServers }, { type: 'separator' });
  }
  if (options.onQuitKeepServer) {
    fileSubmenu.push({
      id: 'quit-keep-server',
      label: 'Quit, Keep Server Running',
      enabled: options.quitKeepServerEnabled ?? false,
      click: options.onQuitKeepServer,
    });
  }
  fileSubmenu.push(isMac ? { role: 'close' } : { role: 'quit' });
  template.push({ label: 'File', submenu: fileSubmenu });

  template.push({ role: 'editMenu' });

  // Reload and devtools stay out of release builds: reloading is not a
  // meaningful user action for a server-backed SPA, and a devtools item
  // in the menu bar invites bug reports about console noise.
  const viewSubmenu: MenuItemConstructorOptions[] = options.isDev
    ? [{ role: 'reload' }, { role: 'forceReload' }, { role: 'toggleDevTools' }, { type: 'separator' }]
    : [];
  if (options.onInterfaceScale) {
    viewSubmenu.push(...interfaceScaleItems(options.onInterfaceScale), { type: 'separator' });
  }
  viewSubmenu.push({ role: 'togglefullscreen' });
  template.push({ label: 'View', submenu: viewSubmenu });

  template.push({ role: 'windowMenu' });

  // Windows and Linux have no app menu; Help is where both platforms'
  // conventions put an update check.
  const helpSubmenu: MenuItemConstructorOptions[] = [];
  if (!isMac && checkForUpdates) helpSubmenu.push(checkForUpdates, { type: 'separator' });
  helpSubmenu.push({
    label: 'Collimate Website',
    click: () => {
      void openExternally(WEBSITE_URL);
    },
  });
  template.push({ role: 'help', submenu: helpSubmenu });

  return template;
}
