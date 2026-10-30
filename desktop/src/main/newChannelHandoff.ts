import type { BrowserWindow } from 'electron';

/** Wait for both Electron's first paint and the target app's New channel view.
 * A successful HTTP load alone can still leave the user on a broken page. */
export function waitForNewChannelPage(window: BrowserWindow, timeoutMs = 20_000, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    let painted = false;
    let loaded = false;
    let done = false;
    let poll: ReturnType<typeof setTimeout> | null = null;
    const timeout = setTimeout(() => finish(new Error('The new channel page did not become ready.')), timeoutMs);

    const finish = (error?: Error): void => {
      if (done) return;
      done = true;
      clearTimeout(timeout);
      if (poll) clearTimeout(poll);
      window.removeListener('ready-to-show', onPaint);
      window.removeListener('closed', onClose);
      window.webContents.removeListener('did-finish-load', onLoad);
      window.webContents.removeListener('did-fail-load', onFailure);
      signal?.removeEventListener('abort', onAbort);
      if (error) reject(error);
      else resolve();
    };
    const check = async (): Promise<void> => {
      if (done || !loaded) return;
      try {
        const ready = await window.webContents.executeJavaScript(
          'Boolean(document.querySelector("[data-channel-create]") && document.documentElement.hasAttribute("data-shell-command-ready"))',
        );
        if (done) return;
        if (ready === true && painted) {
          finish();
          return;
        }
      } catch {
        // The navigation can replace the document while this check runs.
      }
      if (!done) poll = setTimeout(() => void check(), 100);
    };
    const onPaint = (): void => {
      painted = true;
      void check();
    };
    const onLoad = (): void => {
      loaded = true;
      void check();
    };
    const onClose = (): void => finish(new Error('The new channel window closed before it was ready.'));
    const onAbort = (): void => finish(new Error('The New channel handoff was cancelled.'));
    const onFailure = (_event: unknown, code: number, description: string, _url: string, isMainFrame: boolean): void => {
      if (isMainFrame && code !== -3) finish(new Error(`The new channel page failed to load: ${description}.`));
    };

    window.on('ready-to-show', onPaint);
    window.on('closed', onClose);
    window.webContents.on('did-finish-load', onLoad);
    window.webContents.on('did-fail-load', onFailure);
    if (signal?.aborted) onAbort();
    else signal?.addEventListener('abort', onAbort, { once: true });
  });
}
