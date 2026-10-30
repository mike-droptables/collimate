import { beforeEach, describe, expect, it, vi } from 'vitest';

const fake = vi.hoisted(() => {
  const listeners = new Map<string, (...args: unknown[]) => void>();
  const opened = vi.fn(async () => {});
  const menuPopup = vi.fn();
  const handlers = new Map<string, (event: unknown, payload: unknown) => unknown>();
  class BrowserWindow {
    webContents = {
      on: (name: string, handler: (...args: unknown[]) => void) => { listeners.set(name, handler); },
      setWindowOpenHandler: () => undefined,
      getZoomFactor: () => 1.25,
    };
    once(): void {}
    loadURL(): Promise<void> { return Promise.resolve(); }
    isDestroyed(): boolean { return false; }
  }
  return { listeners, opened, menuPopup, handlers, BrowserWindow };
});
vi.mock('electron', () => ({
  BrowserWindow: fake.BrowserWindow, shell: { openExternal: fake.opened },
  ipcMain: { handle: (key: string, handler: (event: unknown, payload: unknown) => unknown) => fake.handlers.set(key, handler), removeHandler: vi.fn(), on: vi.fn(), removeListener: vi.fn() },
  Menu: { getApplicationMenu: () => ({ items: [{ label: 'File', submenu: { popup: fake.menuPopup } }] }) },
}));
import { createMainWindow } from '../src/main/window.js';
import { installTitleBarControl } from '../src/main/titleBar.js';
import { IPC } from '../src/shared/bridge.js';

function windowFixture() {
  return createMainWindow({ server: { id: 'remote-1', kind: 'remote', name: 'App', url: 'https://app.test', port: 443, dataDir: null, serverVersion: 'test' }, shellVersion: 'test', interfaceScale: 1 });
}
beforeEach(() => { fake.listeners.clear(); fake.handlers.clear(); fake.opened.mockClear(); fake.menuPopup.mockClear(); });
describe('window origin and zoom', () => {
  it('refuses foreign main-frame redirects and retains same-origin/subframe redirects __F0995', () => {
    windowFixture();
    const redirect = fake.listeners.get('will-redirect');
    expect(redirect).toBeTypeOf('function');
    const event = { preventDefault: vi.fn(), url: 'https://foreign.test/secret', isMainFrame: true };
    redirect?.(event);
    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(fake.opened).toHaveBeenCalledWith(event.url);
    const same = { preventDefault: vi.fn(), url: 'https://app.test/login', isMainFrame: true };
    redirect?.(same);
    expect(same.preventDefault).not.toHaveBeenCalled();
    const subframe = { ...same, url: 'https://foreign.test/frame', isMainFrame: false };
    redirect?.(subframe);
    expect(subframe.preventDefault).not.toHaveBeenCalled();
    const unsafe = { preventDefault: vi.fn(), url: 'file:///etc/passwd', isMainFrame: true };
    redirect?.(unsafe);
    expect(unsafe.preventDefault).toHaveBeenCalledOnce();
    expect(fake.opened).toHaveBeenCalledTimes(1);
  });
  it('positions the title-bar menu using the current page zoom __F0985', () => {
    const window = windowFixture();
    const dispose = installTitleBarControl({ window });
    try {
      fake.handlers.get(IPC.titleBarPopupMenu)?.({ sender: window.webContents }, { index: 0, x: 100.4, y: 34.4 });
      expect(fake.menuPopup).toHaveBeenCalledWith({ window, x: 126, y: 43 });
    } finally { dispose(); }
  });
});
