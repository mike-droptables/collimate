import { EventEmitter } from 'node:events';
import { describe, expect, it, vi } from 'vitest';
import type { BrowserWindow } from 'electron';
import { waitForNewChannelPage } from '../src/main/newChannelHandoff.js';

function windowFixture() {
  const window = new EventEmitter();
  const webContents = Object.assign(new EventEmitter(), { executeJavaScript: vi.fn(async () => false) });
  return { window: Object.assign(window, { webContents }) as unknown as BrowserWindow, webContents };
}

describe('local-server New channel handoff', () => {
  it('requires a loaded page, its New channel view, and first paint', async () => {
    const { window, webContents } = windowFixture();
    const pending = waitForNewChannelPage(window, 1_000);
    webContents.emit('did-finish-load');
    await Promise.resolve();
    expect(webContents.executeJavaScript).toHaveBeenCalledWith(
      'Boolean(document.querySelector("[data-channel-create]") && document.documentElement.hasAttribute("data-shell-command-ready"))',
    );
    webContents.executeJavaScript.mockResolvedValue(true);
    window.emit('ready-to-show');
    await expect(pending).resolves.toBeUndefined();
  });

  it('rejects a failed main load or a page that never presents New channel', async () => {
    const failed = windowFixture();
    const failedPending = waitForNewChannelPage(failed.window, 1_000);
    failed.webContents.emit('did-fail-load', {}, -105, 'Name not resolved', 'https://box.test', true);
    await expect(failedPending).rejects.toThrow('failed to load: Name not resolved');

    const unready = windowFixture();
    const unreadyPending = waitForNewChannelPage(unready.window, 10);
    unready.webContents.emit('did-finish-load');
    unready.window.emit('ready-to-show');
    await expect(unreadyPending).rejects.toThrow('did not become ready');
  });

  it('cancels when the visible transition closes before the hidden page is ready', async () => {
    const { window, webContents } = windowFixture();
    const controller = new AbortController();
    const pending = waitForNewChannelPage(window, 1_000, controller.signal);
    webContents.emit('did-finish-load');
    controller.abort();
    await expect(pending).rejects.toThrow('handoff was cancelled');
  });
});
