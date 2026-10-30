import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryLog } from '../src/main/log.js';
import {
  STARTUP_CHECK_DELAY_MS,
  UpdateController,
  type UpdateDialogOptions,
} from '../src/main/updater.js';
import { FakeAutoUpdater } from './harness.js';

/**
 * Auto-update, driven through UpdateController's injection points.
 *
 * The reason this file exists at all: every interesting moment in an
 * update is a moment you cannot arrange on a real machine. A download
 * finishing while the app is already quitting. A second click on Check
 * for Updates… while the first request is still out. A dialog that is
 * still open when electron-updater re-emits `update-downloaded`. An
 * install handoff that has to happen after the server teardown and only
 * once. Those are the tests; the rest is the supporting cast.
 */

/**
 * A message box that stays open until the test answers it — which is how
 * "a second event arrived while the dialog was up" becomes a thing you
 * can write down.
 */
class FakeDialog {
  readonly calls: UpdateDialogOptions[] = [];
  private readonly waiting: ((value: { response: number }) => void)[] = [];

  readonly show = (options: UpdateDialogOptions): Promise<{ response: number }> => {
    this.calls.push(options);
    return new Promise<{ response: number }>((resolve) => {
      this.waiting.push(resolve);
    });
  };

  /** Click a button on the oldest open dialog. */
  answer(response: number): void {
    this.waiting.shift()?.({ response });
  }

  get last(): UpdateDialogOptions | undefined {
    return this.calls.at(-1);
  }

  get open(): number {
    return this.waiting.length;
  }
}

interface BuildOptions {
  platform?: NodeJS.Platform;
  isPackaged?: boolean;
  signed?: boolean;
  env?: NodeJS.ProcessEnv;
  appVersion?: string;
}

function build(options: BuildOptions = {}) {
  const updater = new FakeAutoUpdater();
  const dialog = new FakeDialog();
  const log = new MemoryLog();
  const quits: number[] = [];

  const controller = new UpdateController({
    platform: options.platform ?? 'darwin',
    isPackaged: options.isPackaged ?? true,
    signed: options.signed ?? true,
    appVersion: options.appVersion ?? '0.1.0',
    env: options.env ?? {},
    log,
    deps: {
      loadUpdater: () => Promise.resolve(updater),
      showMessageBox: dialog.show,
      quit: () => quits.push(Date.now()),
      setTimeoutImpl: (fn, ms) => {
        setTimeout(fn, ms);
      },
    },
  });

  return { controller, updater, dialog, log, quits };
}

/** Let the controller's own promise chain settle under fake timers. */
async function flush(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

/** A started controller with its events wired. */
async function started(options: BuildOptions = {}) {
  const rig = build(options);
  await rig.controller.start();
  return rig;
}

beforeEach(() => {
  vi.useFakeTimers({ now: 1_700_000_000_000 });
});

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// where updates are, and aren't, offered
// ---------------------------------------------------------------------------

describe('supported', () => {
  it('is false unpackaged — dev runs must never install over themselves', () => {
    const { controller } = build({ isPackaged: false, platform: 'darwin' });
    expect(controller.supported).toBe(false);
  });

  it('is false on linux without APPIMAGE, true with it', () => {
    const deb = build({ platform: 'linux', env: {} });
    expect(deb.controller.supported).toBe(false);
    expect(deb.controller.unsupportedReason).toContain('.deb');

    const appImage = build({ platform: 'linux', env: { APPIMAGE: '/tmp/Collimate.AppImage' } });
    expect(appImage.controller.supported).toBe(true);
  });

  it('is true for a packaged mac or windows build', () => {
    expect(build({ platform: 'darwin' }).controller.supported).toBe(true);
    expect(build({ platform: 'win32' }).controller.supported).toBe(true);
  });

  it('is false for an unsigned mac build (Squirrel.Mac refuses an unsigned swap), true unsigned elsewhere', () => {
    const mac = build({ platform: 'darwin', signed: false });
    expect(mac.controller.supported).toBe(false);
    expect(mac.controller.unsupportedReason).toContain('unsigned');
    expect(build({ platform: 'win32', signed: false }).controller.supported).toBe(true);
    expect(build({ platform: 'linux', signed: false, env: { APPIMAGE: '/tmp/Collimate.AppImage' } })
      .controller.supported).toBe(true);
  });

  it('is false when COLLIMATE_UPDATE_CHECK=0', () => {
    const { controller } = build({ platform: 'darwin', env: { COLLIMATE_UPDATE_CHECK: '0' } });
    expect(controller.supported).toBe(false);
  });

  it('never loads electron-updater when unsupported', async () => {
    const { controller, updater } = build({ isPackaged: false });
    await controller.start();
    controller.checkOnStartup();
    controller.checkFromMenu();
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS * 2);
    expect(updater.checks).toBe(0);
    expect(updater.assignments).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// configuration
// ---------------------------------------------------------------------------

describe('start', () => {
  it('pins the three settings that decide when an install can happen', async () => {
    const { updater } = await started();

    expect(updater.autoDownload).toBe(true);
    expect(updater.autoInstallOnAppQuit).toBe(false);
    expect(updater.allowDowngrade).toBe(false);
    // Asserted as assignments, not just values: two of these agree with
    // electron-updater's defaults, and "we set it" is the property that
    // survives electron-updater changing its mind.
    const assigned = Object.fromEntries(updater.assignments.map(({ key, value }) => [key, value]));
    expect(assigned['autoDownload']).toBe(true);
    expect(assigned['autoInstallOnAppQuit']).toBe(false);
    expect(assigned['allowDowngrade']).toBe(false);
  });

  it('points at COLLIMATE_UPDATE_FEED when one is set', async () => {
    const { updater } = await started({ env: { COLLIMATE_UPDATE_FEED: 'http://127.0.0.1:8099/feed' } });
    expect(updater.feedUrls).toEqual([{ provider: 'generic', url: 'http://127.0.0.1:8099/feed' }]);
  });

  it('leaves the feed alone otherwise (app-update.yml is the source of truth)', async () => {
    const { updater } = await started();
    expect(updater.feedUrls).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// checking
// ---------------------------------------------------------------------------

describe('checkOnStartup', () => {
  it('checks exactly once, and not before the delay is up', async () => {
    const { controller, updater } = build();
    controller.checkOnStartup();

    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS - 1_000);
    expect(updater.checks).toBe(0);

    await vi.advanceTimersByTimeAsync(1_000);
    expect(updater.checks).toBe(1);

    // Called again (a second window shown) it must not re-arm.
    controller.checkOnStartup();
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS * 2);
    expect(updater.checks).toBe(1);
  });

  it('says nothing when there is no update', async () => {
    const { controller, updater, dialog } = build();
    controller.checkOnStartup();
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS);

    updater.emit('update-not-available', { version: '0.1.0' });
    await flush();

    expect(dialog.calls).toEqual([]);
  });
});

describe('checkFromMenu', () => {
  it('coalesces a second click while the first check is still out', async () => {
    const { controller, updater } = await started();

    controller.checkFromMenu();
    await flush();
    controller.checkFromMenu();
    await flush();

    expect(updater.checks).toBe(1);
    expect(updater.checkInFlight).toBe(true);
  });

  it('reports "up to date" rather than leaving the click unanswered', async () => {
    const { controller, updater, dialog } = await started();

    controller.checkFromMenu();
    await flush();
    updater.emit('update-not-available', { version: '0.1.0' });
    await flush();

    expect(dialog.calls).toHaveLength(1);
    expect(dialog.last?.message).toBe("You're up to date.");
    expect(dialog.last?.detail).toContain('0.1.0');
  });

  it('surfaces an error from a check the user asked for', async () => {
    const { controller, updater, dialog, log } = await started();

    controller.checkFromMenu();
    await flush();
    updater.emit('error', new Error('ENOTFOUND github.com'));
    await flush();

    expect(dialog.calls).toHaveLength(1);
    expect(dialog.last?.type).toBe('error');
    expect(dialog.last?.detail).toContain('ENOTFOUND');
    expect(log.lines.join('\n')).toContain('update check failed');
  });

  it('reports a rejected check once, not twice', async () => {
    const { controller, updater, dialog } = await started();

    controller.checkFromMenu();
    await flush();
    // electron-updater's usual behaviour: the event AND the rejection.
    updater.emit('error', new Error('offline'));
    updater.failCheck(new Error('offline'));
    await flush();

    expect(dialog.calls).toHaveLength(1);
  });
});

describe('a passive check', () => {
  it('keeps an error in the log and off the screen', async () => {
    const { updater, dialog, log } = await started();

    updater.emit('error', new Error('ETIMEDOUT'));
    await flush();

    expect(dialog.calls).toEqual([]);
    expect(log.lines.join('\n')).toContain('ETIMEDOUT');
  });
});

// ---------------------------------------------------------------------------
// the downloaded update
// ---------------------------------------------------------------------------

describe('update-downloaded', () => {
  it('asks once, and ignores a re-emit while the dialog is up', async () => {
    const { updater, dialog } = await started();

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();
    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(dialog.calls).toHaveLength(1);
    expect(dialog.open).toBe(1);
    expect(dialog.last?.message).toBe('Collimate 0.2.0 is ready to install.');
    expect(dialog.last?.buttons).toEqual(['Restart Now', 'Later']);
  });

  it('warns Windows users that the server they connect to is a separate upgrade', async () => {
    const { updater, dialog } = await started({ platform: 'win32' });

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(dialog.last?.detail).toContain('the Collimate server you connect to');
  });

  it('does not mention the server on macOS, where the shell owns it', async () => {
    const { updater, dialog } = await started({ platform: 'darwin' });

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(dialog.last?.detail).not.toContain('you connect to');
  });

  it('Restart Now quits — it does not install behind the server teardown', async () => {
    const { controller, updater, dialog, quits } = await started();

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();
    dialog.answer(0);
    await flush();

    expect(quits).toHaveLength(1);
    expect(controller.installPending).toBe(true);
    // The install happens at the end of the quit sequence (quit.ts), not
    // here. Calling quitAndInstall now would race the server's shutdown.
    expect(updater.quitAndInstallCalls).toEqual([]);
  });

  it('Later leaves the install staged for the next quit', async () => {
    const { controller, updater, dialog, quits } = await started();

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();
    dialog.answer(1);
    await flush();

    expect(quits).toEqual([]);
    expect(controller.installPending).toBe(true);
  });

  it('stays quiet once a quit is already under way', async () => {
    const { controller, updater, dialog } = await started();

    controller.noteShuttingDown();
    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(dialog.calls).toEqual([]);
    expect(controller.installPending).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// the handoff
// ---------------------------------------------------------------------------

describe('installIfPending', () => {
  it('is a no-op when nothing is waiting', async () => {
    const { controller, updater } = await started();

    expect(controller.installIfPending()).toBe(false);
    expect(updater.quitAndInstallCalls).toEqual([]);
  });

  it('hands off once, and only once', async () => {
    const { controller, updater, dialog } = await started();

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();
    dialog.answer(0);
    await flush();

    expect(controller.installIfPending()).toBe(true);
    expect(updater.quitAndInstallCalls).toEqual([{ isSilent: false, isForceRunAfter: true }]);

    // quitAndInstall triggers its own app.quit(), which comes back
    // through the same path. A second handoff would relaunch the
    // installer on top of itself.
    expect(controller.installIfPending()).toBe(false);
    expect(updater.quitAndInstallCalls).toHaveLength(1);
  });

  it('reports false when the handoff throws, so the caller still exits', async () => {
    const { controller, updater } = await started();
    updater.quitAndInstallThrows = new Error('ShipIt not found');

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(controller.installIfPending()).toBe(false);
  });
});

describe('menu automatic-download answer __F1007', () => {
  it('answers every menu retry when updater initialization keeps failing __F1007', async () => {
    const dialogs: UpdateDialogOptions[] = [];
    const controller = new UpdateController({ platform: 'darwin', isPackaged: true, signed: true, appVersion: 'test', env: {},
      deps: { loadUpdater: () => Promise.reject(new Error('updater module missing')),
        showMessageBox: async (options) => { dialogs.push(options); return { response: 0 }; },
        quit: () => undefined, setTimeoutImpl: () => undefined } });
    controller.checkFromMenu(); await flush();
    expect(dialogs).toHaveLength(1);
    expect(dialogs[0]?.detail).toContain('updater module missing');
    controller.checkFromMenu(); await flush();
    expect(dialogs).toHaveLength(2);
    expect(dialogs[1]?.detail).toContain('updater module missing');
  });

  it('answers update availability then reports a download error after the check resolves __F1007', async () => {
    const { controller, updater, dialog } = await started();
    controller.checkFromMenu(); await flush();
    updater.emit('update-available', { version: '0.2.0' }); await flush();
    expect(dialog.last?.title).toBe('Update available');
    dialog.answer(0);
    updater.settleCheck(); await flush();
    updater.emit('error', new Error('download interrupted')); await flush();
    expect(dialog.last?.type).toBe('error');
    expect(dialog.last?.detail).toContain('download interrupted');
  });
});
