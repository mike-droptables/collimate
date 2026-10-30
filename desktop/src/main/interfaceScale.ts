import { ipcMain, type BrowserWindow, type WebContents } from 'electron';
import { IPC, isInterfaceScale, type InterfaceScale } from '../shared/bridge.js';

/**
 * The interface scale: the app window's relative size, per device.
 *
 * Electron page zoom, not CSS zoom: it keeps every client rect the page
 * turns into px positions (menus, drag ghosts, native guest geometry)
 * consistent, and the page already follows it through `onZoomChanged`.
 * Chromium keeps zoom per origin, and a spawned server's port can change
 * between launches, so the scale is applied again after every main-frame
 * load rather than once.
 */

/** Apply `scale` to the window's page and tell the preload what it is. */
export function applyInterfaceScale(host: WebContents, scale: InterfaceScale): void {
  if (host.isDestroyed()) return;
  host.setZoomFactor(scale);
  host.send(IPC.interfaceScale, scale);
}

export interface InterfaceScaleOptions {
  window: BrowserWindow;
  /** The device's current scale (settings.json), read at every load. */
  current: () => InterfaceScale;
  /**
   * The page asked for `scale` (already validated): persist and apply it
   * (index.ts, which also serves the View menu's steps).
   */
  commit: (scale: InterfaceScale) => void;
}

/**
 * Wire the window: re-apply the scale after every main-frame load, and take
 * `IPC.interfaceScale` from this window's own page only, dropping any value
 * off INTERFACE_SCALES. Returns a disposer, and calls it itself when the
 * window closes (same shape as installTitleBarControl).
 */
export function installInterfaceScale(options: InterfaceScaleOptions): () => void {
  const { window, current, commit } = options;
  const host = window.webContents;
  let disposed = false;

  const onLoad = (): void => applyInterfaceScale(host, current());
  host.on('did-finish-load', onLoad);

  // Fire-and-forget, so a listener rather than a handler: a stray sender
  // or an off-list factor is dropped, never answered.
  const onScale = (event: Electron.IpcMainEvent, payload: unknown): void => {
    if (event.sender !== host || window.isDestroyed()) return;
    if (!isInterfaceScale(payload)) return;
    commit(payload);
  };
  ipcMain.on(IPC.interfaceScale, onScale);

  const dispose = (): void => {
    if (disposed) return;
    disposed = true;
    ipcMain.removeListener(IPC.interfaceScale, onScale);
    if (!host.isDestroyed()) host.removeListener('did-finish-load', onLoad);
  };
  window.once('closed', dispose);
  return dispose;
}
