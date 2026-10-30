import type { MenuItemConstructorOptions } from 'electron';
import { afterEach, describe, expect, it } from 'vitest';
import { buildTemplate } from '../src/main/menu.js';

/**
 * The menu template, without an Electron runtime to build it in.
 *
 * Only one thing here is worth asserting and it isn't cosmetic: the
 * Check for Updates… item exists exactly when the install can actually
 * update (UpdateController.supported decides, index.ts passes or omits
 * the callback). A menu item that is present and does nothing is worse
 * than no menu item, because the user concludes the app is broken rather
 * than that this build updates through apt.
 */

const CHECK_LABEL = 'Check for Updates…';

const originalPlatform = Object.getOwnPropertyDescriptor(process, 'platform');

function setPlatform(platform: NodeJS.Platform): void {
  Object.defineProperty(process, 'platform', { value: platform, configurable: true });
}

afterEach(() => {
  if (originalPlatform) Object.defineProperty(process, 'platform', originalPlatform);
});

function submenuOf(item: MenuItemConstructorOptions | undefined): MenuItemConstructorOptions[] {
  const submenu = item?.submenu;
  return Array.isArray(submenu) ? submenu : [];
}

function labels(items: MenuItemConstructorOptions[]): (string | undefined)[] {
  return items.map((item) => item.label ?? item.role ?? item.type);
}

function helpMenu(template: MenuItemConstructorOptions[]): MenuItemConstructorOptions[] {
  return submenuOf(template.find((item) => item.role === 'help'));
}

describe('buildTemplate', () => {
  it('puts Check for Updates… under About in the macOS app menu', () => {
    setPlatform('darwin');
    const template = buildTemplate({ isDev: false, onCheckForUpdates: () => {} });

    const appMenu = submenuOf(template[0]);
    expect(labels(appMenu).slice(0, 3)).toEqual(['about', 'separator', CHECK_LABEL]);
    // Everything the appMenu role would have given us has to still be
    // there — Cmd-Q and Hide are not optional.
    expect(labels(appMenu)).toContain('quit');
    expect(labels(appMenu)).toContain('services');
    expect(labels(appMenu)).toContain('hide');
    expect(labels(helpMenu(template))).not.toContain(CHECK_LABEL);
  });

  it('puts it at the top of Help on Windows and Linux', () => {
    for (const platform of ['win32', 'linux'] as const) {
      setPlatform(platform);
      const template = buildTemplate({ isDev: false, onCheckForUpdates: () => {} });

      expect(labels(helpMenu(template)).slice(0, 2)).toEqual([CHECK_LABEL, 'separator']);
      // No app menu to hide it in off macOS.
      expect(template[0]?.label).toBe('File');
    }
  });

  it('omits the item entirely when the install has no update path', () => {
    for (const platform of ['darwin', 'win32', 'linux'] as const) {
      setPlatform(platform);
      const template = buildTemplate({ isDev: false });

      const everyLabel = template.flatMap((item) => labels(submenuOf(item)));
      expect(everyLabel).not.toContain(CHECK_LABEL);
      // And the menus it would have sat in are still intact.
      expect(labels(helpMenu(template))).toEqual(['Collimate Website']);
    }
  });

  it('wires the click through to the callback it was given', () => {
    setPlatform('linux');
    let clicks = 0;
    const template = buildTemplate({ isDev: false, onCheckForUpdates: () => (clicks += 1) });

    const item = helpMenu(template)[0];
    (item?.click as (() => void) | undefined)?.();

    expect(clicks).toBe(1);
  });
});

describe('File menu servers (MS-02)', () => {
  function fileMenu(template: MenuItemConstructorOptions[]): MenuItemConstructorOptions[] {
    return submenuOf(template.find((item) => item.label === 'File'));
  }

  it('leads with Servers…, then Quit, Keep Server Running, ahead of close/quit (MS-02)', () => {
    for (const [platform, tail] of [
      ['darwin', 'close'],
      ['linux', 'quit'],
    ] as const) {
      setPlatform(platform);
      const template = buildTemplate({ isDev: false, onOpenServers: () => {}, onQuitKeepServer: () => {} });

      expect(labels(fileMenu(template))).toEqual(['Servers…', 'separator', 'Quit, Keep Server Running', tail]);
      expect(fileMenu(template)[0]?.id).toBe('servers');
      expect(fileMenu(template)[2]?.id).toBe('quit-keep-server');
    }
  });

  it('has no Switch Server… any more (MS-02)', () => {
    setPlatform('linux');
    const template = buildTemplate({ isDev: false, onOpenServers: () => {}, onQuitKeepServer: () => {} });
    const everyLabel = template.flatMap((item) => labels(submenuOf(item)));
    expect(everyLabel).not.toContain('Switch Server…');
    expect(fileMenu(template).some((item) => item.id === 'switch-server')).toBe(false);
  });

  it('omits Quit, Keep Server Running when undefined — Windows has no local server (MS-04)', () => {
    setPlatform('win32');
    const template = buildTemplate({ isDev: false, onOpenServers: () => {} });

    expect(labels(fileMenu(template))).toEqual(['Servers…', 'separator', 'quit']);
  });

  it('starts Quit, Keep Server Running disabled unless told otherwise (MS-04)', () => {
    setPlatform('linux');
    const off = buildTemplate({ isDev: false, onQuitKeepServer: () => {} });
    expect(fileMenu(off)[0]?.enabled).toBe(false);
    const on = buildTemplate({ isDev: false, onQuitKeepServer: () => {}, quitKeepServerEnabled: true });
    expect(fileMenu(on)[0]?.enabled).toBe(true);
  });

  it('omits both when no callbacks are wired, keeping the bare File menu', () => {
    for (const [platform, tail] of [
      ['darwin', 'close'],
      ['linux', 'quit'],
    ] as const) {
      setPlatform(platform);
      const template = buildTemplate({ isDev: false });

      expect(labels(fileMenu(template))).toEqual([tail]);
    }
  });

  it('wires the clicks through to the callbacks', () => {
    setPlatform('darwin');
    const clicks: string[] = [];
    const template = buildTemplate({
      isDev: false,
      onOpenServers: () => clicks.push('servers'),
      onQuitKeepServer: () => clicks.push('keep'),
    });

    for (const item of fileMenu(template)) (item.click as (() => void) | undefined)?.();

    expect(clicks).toEqual(['servers', 'keep']);
  });
});

describe('View menu interface scale (RS-05)', () => {
  function viewMenu(template: MenuItemConstructorOptions[]): MenuItemConstructorOptions[] {
    return submenuOf(template.find((item) => item.label === 'View'));
  }

  it('replaces the zoom roles with Actual Size / Zoom In / Zoom Out that step the scale (RS-05)', () => {
    for (const isDev of [false, true]) {
      setPlatform('linux');
      const steps: string[] = [];
      const template = buildTemplate({ isDev, onInterfaceScale: (step) => steps.push(step) });
      const view = viewMenu(template);

      const roles = view.map((item) => item.role).filter(Boolean);
      for (const role of ['resetZoom', 'zoomIn', 'zoomOut']) expect(roles).not.toContain(role);
      const scaleItems = view.filter((item) => item.id?.startsWith('interface-scale-'));
      expect(scaleItems.map((item) => [item.id, item.label, item.accelerator, item.visible ?? true])).toEqual([
        ['interface-scale-reset', 'Actual Size', 'CmdOrCtrl+0', true],
        ['interface-scale-in', 'Zoom In', 'CmdOrCtrl+Plus', true],
        ['interface-scale-in-equals', 'Zoom In', 'CmdOrCtrl+=', false],
        ['interface-scale-out', 'Zoom Out', 'CmdOrCtrl+-', true],
      ]);
      expect(scaleItems[2]?.acceleratorWorksWhenHidden).toBe(true);
      for (const item of scaleItems) (item.click as (() => void) | undefined)?.();
      expect(steps).toEqual(['reset', 'in', 'in', 'out']);
      expect(view.at(-1)?.role).toBe('togglefullscreen');
      expect(roles.includes('reload')).toBe(isDev);
    }
  });

  it('omits the items when no callback is wired (RS-05)', () => {
    setPlatform('linux');
    expect(labels(viewMenu(buildTemplate({ isDev: false })))).toEqual(['togglefullscreen']);
  });
});
