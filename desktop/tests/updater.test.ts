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
 * still open when electron-updater re-emits `update-downloaded`. The
 * automatic-updates setting turned off while a check is armed. An install
 * handoff that has to happen after the server teardown and only once.
 * Those are the tests; the rest is the supporting cast.
 *
 * The setting gives three states worth naming: automatic updates on,
 * automatic updates off, and the user's own check in each.
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
  packagedFeed?: boolean;
  /** settings.json `automaticUpdates` at the start; `rig.setAutomatic` changes it. */
  automatic?: boolean;
  env?: NodeJS.ProcessEnv;
  appVersion?: string;
}

function build(options: BuildOptions = {}) {
  const updater = new FakeAutoUpdater();
  const dialog = new FakeDialog();
  const log = new MemoryLog();
  const quits: number[] = [];
  const timers: number[] = [];
  let automatic = options.automatic ?? true;
  let loads = 0;

  const controller = new UpdateController({
    platform: options.platform ?? 'darwin',
    isPackaged: options.isPackaged ?? true,
    signed: options.signed ?? true,
    packagedFeed: options.packagedFeed ?? true,
    appVersion: options.appVersion ?? '0.1.0',
    env: options.env ?? {},
    log,
    deps: {
      loadUpdater: () => {
        loads += 1;
        return Promise.resolve(updater);
      },
      automaticUpdates: () => automatic,
      showMessageBox: dialog.show,
      quit: () => quits.push(Date.now()),
      setTimeoutImpl: (fn, ms) => {
        timers.push(ms);
        setTimeout(fn, ms);
      },
    },
  });

  /** The user flips the switch: settings.json changes, then the controller hears of it. */
  const setAutomatic = (on: boolean): void => {
    automatic = on;
    controller.automaticUpdatesChanged();
  };

  return { controller, updater, dialog, log, quits, timers, setAutomatic, loads: () => loads };
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

/** A user check that found `version` and finished downloading it, its ready dialog open. */
async function userCheckDownloaded(rig: Awaited<ReturnType<typeof started>>, version = '0.2.0') {
  expect(rig.controller.checkNow()).toEqual({ status: 'started' });
  await flush();
  rig.updater.emit('update-available', { version });
  await flush();
  expect(rig.dialog.last?.title).toBe('Update available');
  rig.dialog.answer(0);
  rig.updater.settleCheck();
  await flush();
  rig.updater.emit('update-downloaded', { version });
  await flush();
  expect(rig.dialog.last?.title).toBe('Update ready');
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
  it('is false without a feed — a package built with publish: null, or a dev run — unless COLLIMATE_UPDATE_FEED names one', () => {
    const none = build({ packagedFeed: false });
    expect(none.controller.supported).toBe(false);
    expect(none.controller.unsupportedReason).toContain('no update feed');
    expect(build({ packagedFeed: false, env: { COLLIMATE_UPDATE_FEED: 'http://127.0.0.1:8099' } })
      .controller.supported).toBe(true);
    // A dev run has no packaged feed: the feed is its reason.
    expect(build({ isPackaged: false, packagedFeed: false }).controller.unsupportedReason).toContain('no update feed');
  });

  it('is false unpackaged — dev runs must never install over themselves', () => {
    const { controller } = build({ isPackaged: false, env: { COLLIMATE_UPDATE_FEED: 'http://127.0.0.1:8099' } });
    expect(controller.supported).toBe(false);
    expect(controller.unsupportedReason).toBe('running unpackaged');
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

  it('does not depend on the setting: an install with a path keeps it with automatic updates off', () => {
    expect(build({ automatic: false }).controller.supported).toBe(true);
  });

  it('never loads electron-updater when unsupported, and answers a check with the reason', async () => {
    const { controller, updater, loads } = build({ packagedFeed: false });
    await controller.start();
    controller.checkOnStartup();
    expect(controller.checkNow()).toEqual({ status: 'unavailable', reason: controller.unsupportedReason });
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS * 2);
    expect(loads()).toBe(0);
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
// state 1: automatic updates on (the default)
// ---------------------------------------------------------------------------

describe('automatic updates on', () => {
  it('checks exactly once, and not before the delay is up', async () => {
    const { controller, updater, loads } = build();
    controller.checkOnStartup();
    await flush();
    // Loaded when armed, so a config error is logged before the check.
    expect(loads()).toBe(1);

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

  it('keeps a passive error in the log and off the screen', async () => {
    const { updater, dialog, log } = await started();

    updater.emit('error', new Error('ETIMEDOUT'));
    await flush();

    expect(dialog.calls).toEqual([]);
    expect(log.lines.join('\n')).toContain('ETIMEDOUT');
  });

  it('asks once when the background download lands, and ignores a re-emit while the dialog is up', async () => {
    const { updater, dialog } = await started();

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();
    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(dialog.calls).toHaveLength(1);
    expect(dialog.open).toBe(1);
    expect(dialog.last?.message).toBe('Collimate 0.2.0 is ready to install.');
    expect(dialog.last?.buttons).toEqual(['Restart Now', 'Later']);
    expect(dialog.last?.detail).toContain('Later installs it the next time you quit.');
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

  it('stays quiet once a quit is already under way, and still installs at it', async () => {
    const { controller, updater, dialog } = await started();

    controller.noteShuttingDown();
    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(dialog.calls).toEqual([]);
    expect(controller.installPending).toBe(true);
  });

  it('turning it off drops an install staged by Later', async () => {
    const { controller, updater, dialog, setAutomatic, log } = await started();
    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();
    dialog.answer(1);
    await flush();
    expect(controller.installPending).toBe(true);

    setAutomatic(false);

    expect(controller.installPending).toBe(false);
    expect(controller.installIfPending()).toBe(false);
    expect(log.lines.join('\n')).toContain('will not install at quit');
  });

  it('turning it off while the startup check is armed: no check; on again: the check runs', async () => {
    const { controller, updater, setAutomatic } = build();
    controller.checkOnStartup();
    setAutomatic(false);
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS * 2);
    expect(updater.checks).toBe(0);

    setAutomatic(true);
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS);
    expect(updater.checks).toBe(1);
  });

  it('warns Windows users that the servers they connect to update on their own machines', async () => {
    const { updater, dialog } = await started({ platform: 'win32' });

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(dialog.last?.detail).toContain('The servers you connect to update on their own machines.');
  });

  it('does not mention other servers on macOS, where the shell carries its own', async () => {
    const { updater, dialog } = await started({ platform: 'darwin' });

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(dialog.last?.detail).not.toContain('you connect to');
  });
});

// ---------------------------------------------------------------------------
// state 2: automatic updates off
// ---------------------------------------------------------------------------

describe('automatic updates off', () => {
  it('no startup check, no timer, nothing loaded', async () => {
    const { controller, updater, timers, loads, dialog } = build({ automatic: false });
    controller.checkOnStartup();
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS * 4);

    expect(timers).toEqual([]);
    expect(loads()).toBe(0);
    expect(updater.checks).toBe(0);
    expect(updater.assignments).toEqual([]);
    expect(dialog.calls).toEqual([]);
    expect(controller.installPending).toBe(false);
  });

  it('a download no user check asked for is neither offered nor installed at quit', async () => {
    // An automatic check began before the switch was turned off.
    const { controller, updater, dialog, setAutomatic } = await started();
    setAutomatic(false);

    updater.emit('update-downloaded', { version: '0.2.0' });
    await flush();

    expect(dialog.calls).toEqual([]);
    expect(controller.installPending).toBe(false);
    expect(controller.installIfPending()).toBe(false);
    expect(updater.quitAndInstallCalls).toEqual([]);
  });

  it('turning it on once the window is up arms the session check then', async () => {
    const { controller, updater, setAutomatic } = build({ automatic: false });
    controller.checkOnStartup();
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS * 2);
    expect(updater.checks).toBe(0);

    setAutomatic(true);
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS - 1);
    expect(updater.checks).toBe(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(updater.checks).toBe(1);
  });

  it('turning it on before any window arms nothing until the window is up', async () => {
    const { controller, updater, setAutomatic, timers } = build({ automatic: false });
    setAutomatic(true);
    expect(timers).toEqual([]);
    controller.checkOnStartup();
    await vi.advanceTimersByTimeAsync(STARTUP_CHECK_DELAY_MS);
    expect(updater.checks).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// state 3: the user's own check (the menu, or the page's Check for updates)
// ---------------------------------------------------------------------------

describe.each([
  { automatic: true, later: 'Later installs it the next time you quit.' },
  { automatic: false, later: 'Later skips it for now; Check for Updates… offers it again.' },
])('checkNow with automatic updates $automatic', ({ automatic, later }) => {
  it('checks at once, whatever the setting, and loads the updater for it', async () => {
    const { controller, updater, loads } = build({ automatic });
    expect(controller.checkNow()).toEqual({ status: 'started' });
    await flush();
    expect(loads()).toBe(1);
    expect(updater.checks).toBe(1);
  });

  it('coalesces a second request while the first check is still out', async () => {
    const { controller, updater } = await started({ automatic });

    expect(controller.checkNow()).toEqual({ status: 'started' });
    await flush();
    expect(controller.checkNow()).toEqual({ status: 'busy' });
    await flush();

    expect(updater.checks).toBe(1);
    expect(updater.checkInFlight).toBe(true);
  });

  it('reports "up to date" rather than leaving the request unanswered', async () => {
    const { controller, updater, dialog } = await started({ automatic });

    controller.checkNow();
    await flush();
    updater.emit('update-not-available', { version: '0.1.0' });
    await flush();

    expect(dialog.calls).toHaveLength(1);
    expect(dialog.last?.message).toBe("You're up to date.");
    expect(dialog.last?.detail).toContain('0.1.0');
  });

  it('surfaces an error from the check', async () => {
    const { controller, updater, dialog, log } = await started({ automatic });

    controller.checkNow();
    await flush();
    updater.emit('error', new Error('ENOTFOUND github.com'));
    await flush();

    expect(dialog.calls).toHaveLength(1);
    expect(dialog.last?.type).toBe('error');
    expect(dialog.last?.detail).toContain('ENOTFOUND');
    expect(log.lines.join('\n')).toContain('update check failed');
  });

  it('reports a rejected check once, not twice', async () => {
    const { controller, updater, dialog } = await started({ automatic });

    controller.checkNow();
    await flush();
    // electron-updater's usual behaviour: the event AND the rejection.
    updater.emit('error', new Error('offline'));
    updater.failCheck(new Error('offline'));
    await flush();

    expect(dialog.calls).toHaveLength(1);
  });

  it('downloads and offers the install, saying what Later does', async () => {
    const rig = await started({ automatic });
    await userCheckDownloaded(rig);
    expect(rig.dialog.last?.buttons).toEqual(['Restart Now', 'Later']);
    expect(rig.dialog.last?.detail).toContain(later);
  });

  it('Restart Now quits — it does not install behind the server teardown', async () => {
    const rig = await started({ automatic });
    await userCheckDownloaded(rig);
    rig.dialog.answer(0);
    await flush();

    expect(rig.quits).toHaveLength(1);
    expect(rig.controller.installPending).toBe(true);
    // The install happens at the end of the quit sequence (quit.ts), not
    // here. Calling quitAndInstall now would race the server's shutdown.
    expect(rig.updater.quitAndInstallCalls).toEqual([]);
    expect(rig.controller.installIfPending()).toBe(true);
  });

  it(automatic ? 'Later defers the install to the next quit' : 'Later discards the install', async () => {
    const rig = await started({ automatic });
    await userCheckDownloaded(rig);
    rig.dialog.answer(1);
    await flush();

    expect(rig.quits).toEqual([]);
    expect(rig.controller.installPending).toBe(automatic);
    expect(rig.controller.installIfPending()).toBe(automatic);
  });

  it('answers a download error that arrives after the check resolved (F1007)', async () => {
    const { controller, updater, dialog } = await started({ automatic });
    controller.checkNow(); await flush();
    updater.emit('update-available', { version: '0.2.0' }); await flush();
    expect(dialog.last?.title).toBe('Update available');
    dialog.answer(0);
    updater.settleCheck(); await flush();
    updater.emit('error', new Error('download interrupted')); await flush();
    expect(dialog.last?.type).toBe('error');
    expect(dialog.last?.detail).toContain('download interrupted');
  });
});

describe('checkNow when electron-updater cannot load', () => {
  it('answers every retry with the load failure (F1007)', async () => {
    const dialogs: UpdateDialogOptions[] = [];
    const controller = new UpdateController({ platform: 'darwin', isPackaged: true, signed: true, packagedFeed: true, appVersion: 'test', env: {},
      deps: { loadUpdater: () => Promise.reject(new Error('updater module missing')), automaticUpdates: () => false,
        showMessageBox: async (options) => { dialogs.push(options); return { response: 0 }; },
        quit: () => undefined, setTimeoutImpl: () => undefined } });
    controller.checkNow(); await flush();
    expect(dialogs).toHaveLength(1);
    expect(dialogs[0]?.detail).toContain('updater module missing');
    controller.checkNow(); await flush();
    expect(dialogs).toHaveLength(2);
    expect(dialogs[1]?.detail).toContain('updater module missing');
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

  it('a Restart Now survives the switch being turned off during the quit', async () => {
    const rig = await started({ automatic: false });
    await userCheckDownloaded(rig);
    rig.dialog.answer(0);
    await flush();
    rig.setAutomatic(false);
    expect(rig.controller.installPending).toBe(true);
  });
});
