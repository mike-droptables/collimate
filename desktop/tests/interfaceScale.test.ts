import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The interface scale's main-process leg (RS-05): the app window's page
 * zoom is re-applied after every main-frame load (Chromium keeps zoom per
 * origin, and a spawned server's port can change), and the page's
 * `IPC.interfaceScale` is taken from the app window only, on-list only.
 */

const fake = vi.hoisted(() => {
  const ipcListeners = new Map<string, (event: unknown, payload: unknown) => void>();
  const removed: string[] = [];
  return { ipcListeners, removed };
});

vi.mock('electron', () => ({
  ipcMain: {
    on: (channel: string, listener: (event: unknown, payload: unknown) => void) => fake.ipcListeners.set(channel, listener),
    removeListener: (channel: string) => {
      fake.removed.push(channel);
      fake.ipcListeners.delete(channel);
    },
  },
}));

import { applyInterfaceScale, installInterfaceScale } from '../src/main/interfaceScale.js';
import { IPC, type InterfaceScale } from '../src/shared/bridge.js';
import type { BrowserWindow } from 'electron';

function windowFixture() {
  const contentsListeners = new Map<string, () => void>();
  let onClosed: (() => void) | null = null;
  const webContents = {
    setZoomFactor: vi.fn(),
    send: vi.fn(),
    isDestroyed: () => false,
    on: (name: string, handler: () => void) => contentsListeners.set(name, handler),
    removeListener: (name: string) => contentsListeners.delete(name),
  };
  const window = {
    webContents,
    isDestroyed: () => false,
    once: (name: string, handler: () => void) => {
      if (name === 'closed') onClosed = handler;
    },
  };
  return { window, webContents, contentsListeners, close: () => onClosed?.() };
}

beforeEach(() => {
  fake.ipcListeners.clear();
  fake.removed.length = 0;
});

describe('installInterfaceScale (RS-05)', () => {
  it('applies the current scale after every load and tells the preload (RS-05)', () => {
    const { window, webContents, contentsListeners } = windowFixture();
    let scale: InterfaceScale = 1.1;
    installInterfaceScale({ window: window as unknown as BrowserWindow, current: () => scale, commit: vi.fn() });

    contentsListeners.get('did-finish-load')?.();
    expect(webContents.setZoomFactor).toHaveBeenLastCalledWith(1.1);
    expect(webContents.send).toHaveBeenLastCalledWith(IPC.interfaceScale, 1.1);
    // A reload on a new origin (a new spawn port) gets it again.
    scale = 1.25;
    contentsListeners.get('did-finish-load')?.();
    expect(webContents.setZoomFactor).toHaveBeenLastCalledWith(1.25);
    expect(webContents.setZoomFactor).toHaveBeenCalledTimes(2);
  });

  it('commits a listed scale from its own page only (RS-05)', () => {
    const { window, webContents } = windowFixture();
    const commit = vi.fn();
    installInterfaceScale({ window: window as unknown as BrowserWindow, current: () => 1, commit });
    const listener = fake.ipcListeners.get(IPC.interfaceScale)!;

    listener({ sender: webContents }, 0.9);
    expect(commit).toHaveBeenCalledExactlyOnceWith(0.9);
    listener({ sender: {} }, 1.25);
    for (const junk of [1.75, '1.1', null, Number.NaN]) listener({ sender: webContents }, junk);
    expect(commit).toHaveBeenCalledTimes(1);
  });

  it('uninstalls when the window closes (RS-05)', () => {
    const { window, contentsListeners, close } = windowFixture();
    installInterfaceScale({ window: window as unknown as BrowserWindow, current: () => 1, commit: vi.fn() });
    close();
    expect(fake.removed).toEqual([IPC.interfaceScale]);
    expect(contentsListeners.has('did-finish-load')).toBe(false);
  });

  it('applyInterfaceScale sets the page zoom and reports it (RS-05)', () => {
    const { webContents } = windowFixture();
    applyInterfaceScale(webContents as unknown as Electron.WebContents, 1.25);
    expect(webContents.setZoomFactor).toHaveBeenCalledWith(1.25);
    expect(webContents.send).toHaveBeenCalledWith(IPC.interfaceScale, 1.25);
  });

  it('maps the default 100% to neutral Electron page zoom (RS-05)', () => {
    const { webContents } = windowFixture();
    applyInterfaceScale(webContents as unknown as Electron.WebContents, 1);
    expect(webContents.setZoomFactor).toHaveBeenCalledWith(1);
    expect(webContents.send).toHaveBeenCalledWith(IPC.interfaceScale, 1);
  });
});
