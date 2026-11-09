import type { UpdateCheckStart } from '../shared/bridge.js';
import { MemoryLog, type LogSink } from './log.js';
import { errorMessage } from './errors.js';

/**
 * Auto-update, and the decisions around it.
 *
 * Like ServerController, this file has no Electron imports: dialogs,
 * quitting, the clock, the setting and electron-updater itself all arrive
 * through the `deps` bag, so the paths nobody can reproduce on demand — a
 * download finishing during a shutdown, a second dialog racing the first,
 * a quitAndInstall that never hands off, the setting flipped mid-check —
 * are asserted in desktop/tests/updater.test.ts rather than hoped about.
 *
 * electron-updater is loaded with a dynamic import, not at module scope:
 * an install without an update path, a dev run and a session with
 * automatic updates off never pay its module init, and nothing in the
 * update path can execute before the shell has decided it wants one.
 *
 * The user's setting, settings.json `automaticUpdates` (read live through
 * `deps.automaticUpdates`), decides everything automatic:
 *
 * - on (the default): one check STARTUP_CHECK_DELAY_MS after the window
 *   appears, a background download, the "update ready" dialog, and an
 *   install at the next quit when that dialog was answered "Later".
 * - off: no startup check, no timer, no download, no dialog of its own and
 *   no install at quit.
 *
 * "Check for Updates…" (the menu, and the page's per-device Settings
 * through the bridge, both `checkNow`) is the whole update in one gesture
 * in either state: it checks, downloads and asks "Restart Now" / "Later".
 * With automatic updates off, "Later" discards the downloaded update
 * rather than deferring it to the next quit.
 *
 * Nothing installs behind the server teardown: electron-updater's
 * `autoInstallOnAppQuit` is off and the one install site is
 * installIfPending(), at the very end of runQuitSequence (main/quit.ts),
 * *after* the server has been asked to stop — an installer that swapped
 * the app bundle out from under a running uvicorn and its generator
 * subprocess tree is how you orphan Docker containers.
 */

/** The events this shell subscribes to. */
export type AutoUpdaterEvent =
  | 'update-available'
  | 'update-not-available'
  | 'update-downloaded'
  | 'download-progress'
  | 'error';

/**
 * The slice of electron-updater's `autoUpdater` this shell uses.
 *
 * Structural, not `import type { AppUpdater }`: the tests hand in a fake,
 * and a fake that has to satisfy the real class's full surface is a fake
 * nobody maintains. Anything added here has to be added to
 * FakeAutoUpdater in tests/harness.ts too, which is the point.
 */
export interface AutoUpdaterLike {
  autoDownload: boolean;
  autoInstallOnAppQuit: boolean;
  allowDowngrade: boolean;
  logger: unknown;
  on(event: AutoUpdaterEvent, handler: (payload: unknown) => void): void;
  setFeedURL(options: { provider: 'generic'; url: string }): void;
  checkForUpdates(): Promise<unknown>;
  quitAndInstall(isSilent?: boolean, isForceRunAfter?: boolean): void;
}

/**
 * A message box, narrowed to what this file asks for.
 *
 * Structurally assignable to Electron's MessageBoxOptions /
 * MessageBoxReturnValue, so index.ts can wire `dialog.showMessageBox`
 * straight in without a cast.
 */
export interface UpdateDialogOptions {
  type: 'info' | 'error';
  title: string;
  message: string;
  detail: string;
  buttons: string[];
  defaultId: number;
  cancelId: number;
  noLink: true;
}

export interface UpdateControllerDeps {
  /** Resolves electron-updater's `autoUpdater`. Called at most once. */
  loadUpdater: () => Promise<AutoUpdaterLike>;
  /** settings.json `automaticUpdates`, read at every decision. */
  automaticUpdates: () => boolean;
  showMessageBox: (options: UpdateDialogOptions) => Promise<{ response: number }>;
  /**
   * `app.quit()` — never `app.exit()`. quit() runs `before-quit`, which is
   * what puts the server teardown and the install handoff on the path;
   * exit() would tear the process down mid-shutdown.
   */
  quit: () => void;
  setTimeoutImpl: (fn: () => void, ms: number) => void;
}

export interface UpdateControllerOptions {
  /** process.platform. */
  platform: NodeJS.Platform;
  isPackaged: boolean;
  /**
   * Whether this build carries a code signature: the packaged
   * package.json's `collimate.signed`, stamped by the release's packaging
   * config (build/desktop_packaging.py) from the presence of the signing
   * secrets. A build without the stamp is unsigned.
   */
  signed: boolean;
  /**
   * Whether the package carries electron-builder's `app-update.yml`, the
   * feed a `publish:` config writes into it. A package built with
   * `publish: null` (build/build_desktop.sh's) carries none, so it has no
   * feed unless COLLIMATE_UPDATE_FEED names one.
   */
  packagedFeed: boolean;
  /** The app version (SHELL_VERSION) — what "you're up to date" names. */
  appVersion: string;
  env?: NodeJS.ProcessEnv;
  log?: LogSink;
  deps: UpdateControllerDeps;
}

/**
 * How long after the window appears before the automatic check.
 *
 * A cold first launch is still starting its server while this timer runs;
 * racing an update download against that turns a slow first start into a
 * stalled one on exactly the connections that can least afford it.
 */
export const STARTUP_CHECK_DELAY_MS = 15_000;

export class UpdateController {
  private readonly env: NodeJS.ProcessEnv;
  private readonly log: LogSink;
  private readonly deps: UpdateControllerDeps;

  private loaded: AutoUpdaterLike | null = null;
  private starting: Promise<AutoUpdaterLike | null> | null = null;
  private inFlight: Promise<void> | null = null;
  /** True while a check the user asked for is out; drives dialog vs. log. */
  private userCheck = false;
  /** True from the user's check's `update-available` until its download lands or fails. */
  private userDownload = false;
  private errorReported = false;
  private windowShown = false;
  private startupCheckArmed = false;
  private startupCheckRan = false;
  private dialogOpen = false;
  private pending = false;
  private pendingVersion: string | null = null;
  private installStarted = false;
  private shuttingDown = false;
  private lastProgressBucket = -1;

  constructor(private readonly options: UpdateControllerOptions) {
    this.env = options.env ?? process.env;
    this.log = options.log ?? new MemoryLog();
    this.deps = options.deps;
  }

  /**
   * Whether this install has an update path at all: a feed, and a package
   * that can replace itself. The setting does not enter into it.
   *
   * Every `false` here is a place where offering the user an update would
   * be a lie, so the menu item is omitted rather than shown-and-broken
   * (see menu.ts), no check is ever scheduled, and the page's Check for
   * updates answers with the reason.
   */
  get supported(): boolean {
    return this.unsupportedReason === null;
  }

  /** Why not, in a form fit for the log and the page. `null` when there is a path. */
  get unsupportedReason(): string | null {
    if (!this.options.packagedFeed && !this.env['COLLIMATE_UPDATE_FEED']) {
      return 'no update feed (this package carries no app-update.yml and COLLIMATE_UPDATE_FEED is unset)';
    }
    // electron-updater skips every check in an unpackaged run.
    if (!this.options.isPackaged) return 'running unpackaged';
    // Linux ships an AppImage and a .deb from the same electron-builder
    // config, and app-update.yml is stamped into both — but only the
    // AppImage can rewrite itself. A .deb belongs to apt/dpkg, and a
    // process that swapped files under /opt behind the package manager's
    // back would leave the system lying about what's installed. The
    // AppImage runtime sets APPIMAGE to the path of the running image;
    // nothing else does.
    if (this.options.platform === 'linux' && !this.env['APPIMAGE']) {
      return 'installed from a .deb (updates belong to the package manager)';
    }
    // Squirrel.Mac validates the downloaded app's code signature against
    // the running app's before it swaps them, so an unsigned build would
    // download every update and install none. Windows is different: an
    // unsigned installer runs, and electron-updater verifies a publisher
    // only when the build wrote one into app-update.yml (a signed build).
    if (this.options.platform === 'darwin' && !this.options.signed) {
      return 'an unsigned build (a macOS app updates itself only when signed)';
    }
    return null;
  }

  /**
   * True once an update is staged to install at the next quit: "Restart
   * Now", or "Later" while automatic updates are on.
   */
  get installPending(): boolean {
    return this.pending;
  }

  /**
   * Configure electron-updater and subscribe. Idempotent; the automatic
   * check calls it when it is armed, so the feed resolution and any config
   * error land in the log at a legible moment.
   */
  async start(): Promise<void> {
    await this.ensureStarted();
  }

  /**
   * The window is up: the moment the session's one automatic check may be
   * armed (see STARTUP_CHECK_DELAY_MS). Nothing at all while automatic
   * updates are off; turning them on later arms it then
   * (automaticUpdatesChanged).
   */
  checkOnStartup(): void {
    this.windowShown = true;
    this.armStartupCheck();
  }

  /**
   * settings.json `automaticUpdates` changed. On: the session's automatic
   * check is armed when the window is up and it has not run yet. Off: an
   * armed check will not run, and an update an earlier "Later" staged is
   * no longer installed at quit.
   */
  automaticUpdatesChanged(): void {
    if (this.deps.automaticUpdates()) {
      this.log.shell('automatic updates on');
      this.armStartupCheck();
      return;
    }
    this.log.shell('automatic updates off');
    if (this.pending && !this.shuttingDown) {
      this.pending = false;
      this.log.shell(`the downloaded update${this.versionSuffix()} will not install at quit`);
    }
  }

  /**
   * "Check for Updates…", from the menu or the page. Coalesced: a second
   * request while the first check is still out is answered `busy` rather
   * than sending a second request and a second pair of dialogs. Every
   * later answer (up to date, downloading, ready, failed) is a dialog.
   */
  checkNow(): UpdateCheckStart {
    const reason = this.unsupportedReason;
    if (reason !== null) {
      this.log.shell(`update check unavailable: ${reason}`);
      return { status: 'unavailable', reason };
    }
    if (this.inFlight) {
      this.log.shell('update check already in progress');
      return { status: 'busy' };
    }
    this.userCheck = true;
    void this.runCheck();
    return { status: 'started' };
  }

  /** Tell the controller a quit is under way: no new dialogs from here. */
  noteShuttingDown(): void {
    this.shuttingDown = true;
  }

  /**
   * Hand off to the installer, if there is one waiting.
   *
   * Returns true only when quitAndInstall actually ran — the caller
   * (runQuitSequence) uses that to decide whether to exit the process
   * itself or leave the installer to drive the rest. Calling twice
   * returns false: quitAndInstall is not re-entrant, and a second call
   * during its own quit sequence relaunches the installer.
   */
  installIfPending(): boolean {
    if (!this.pending || this.installStarted) return false;
    const updater = this.loaded;
    if (!updater) {
      // Can't happen — `pending` is only set from an event the loaded
      // updater emitted — but returning false here means a bug shows up
      // as "didn't update" rather than "won't quit".
      this.log.shell('update pending but electron-updater was never loaded; quitting without installing');
      return false;
    }
    this.installStarted = true;
    this.log.shell(`installing update${this.versionSuffix()} (quitAndInstall)`);
    try {
      // (isSilent=false, isForceRunAfter=true): show the installer's own
      // progress on Windows rather than a silent gap where the app used
      // to be, and come back up afterwards — the user asked to restart.
      updater.quitAndInstall(false, true);
      return true;
    } catch (error) {
      this.log.shell(`quitAndInstall failed: ${errorMessage(error)}`);
      return false;
    }
  }

  /** Mark an install wanted and start the quit that performs it. */
  requestInstallAndQuit(): void {
    this.userDownload = false;
    this.pending = true;
    this.shuttingDown = true;
    this.log.shell('restarting to install the update');
    this.deps.quit();
  }

  // --- plumbing ------------------------------------------------------------

  private armStartupCheck(): void {
    if (!this.windowShown || this.startupCheckArmed || this.startupCheckRan) return;
    if (!this.supported || !this.deps.automaticUpdates()) return;
    this.startupCheckArmed = true;
    void this.ensureStarted();
    this.deps.setTimeoutImpl(() => {
      this.startupCheckArmed = false;
      // Turned off while the timer ran: no check, and on again re-arms.
      if (!this.deps.automaticUpdates()) return;
      this.startupCheckRan = true;
      void this.runCheck();
    }, STARTUP_CHECK_DELAY_MS);
  }

  private ensureStarted(): Promise<AutoUpdaterLike | null> {
    if (this.loaded) return Promise.resolve(this.loaded);
    if (this.starting) return this.starting;
    // No log line: `unsupportedReason` is public and index.ts reports it
    // once, at startup, where it is legible next to the version banner.
    if (!this.supported) return Promise.resolve(null);
    const attempt = this.load().catch((error: unknown) => {
      // A shell that can't load its updater is still a working shell.
      this.reportError(error);
      this.starting = null;
      return null;
    });
    this.starting = attempt;
    return attempt;
  }

  private async load(): Promise<AutoUpdaterLike> {
    const updater = await this.deps.loadUpdater();

    // Every check this controller makes is one the setting or the user
    // asked for, so its update downloads at once. It never installs behind
    // the user's back: autoInstallOnAppQuit is electron-updater's "just do
    // it on the way out" hook, and shutdownAndExit ends in app.exit(),
    // which skips the will-quit handler it rides on, so it would silently
    // do nothing on some paths and race the server teardown on others.
    // Off, Squirrel.Mac is not handed the download either, so an update
    // "Later" discarded is not applied at the next launch.
    // installIfPending() is the one install site.
    updater.autoDownload = true;
    updater.autoInstallOnAppQuit = false;
    // The version in the feed can only ever move forward: the app carries
    // its server, and an older server must never run over a data dir a
    // newer one has already migrated.
    updater.allowDowngrade = false;
    updater.logger = this.loggerAdapter();

    // The local-feed hook, mirroring COLLIMATE_SERVER_SPEC's role for the
    // server: point the shell at a directory served over HTTP holding
    // latest*.yml plus the artifacts, and a real packaged build will
    // update from it. Without this the only way to exercise the update
    // path is to cut a release.
    const feed = this.env['COLLIMATE_UPDATE_FEED'];
    if (feed) {
      this.log.shell(`update feed overridden: ${feed}`);
      updater.setFeedURL({ provider: 'generic', url: feed });
    }

    updater.on('update-available', (payload) => {
      const version = versionOf(payload);
      this.log.shell(`update available: ${version ?? 'unknown version'} (downloading)`);
      if (!this.userCheck || this.shuttingDown) return;
      this.userDownload = true;
      void this.deps.showMessageBox({
        type: 'info', title: 'Update available',
        message: version ? `Collimate ${version} is downloading.` : 'A new version of Collimate is downloading.',
        detail: 'You will be asked to restart when the download is ready.',
        buttons: ['OK'], defaultId: 0, cancelId: 0, noLink: true,
      }).catch((error: unknown) => this.log.shell(`update dialog failed: ${errorMessage(error)}`));
    });
    updater.on('update-not-available', () => this.onNotAvailable());
    updater.on('download-progress', (payload) => this.onProgress(payload));
    updater.on('update-downloaded', (payload) => {
      void this.onDownloaded(payload);
    });
    updater.on('error', (payload) => this.reportError(payload));

    this.loaded = updater;
    this.log.shell(`auto-update ready (current version ${this.options.appVersion})`);
    return updater;
  }

  /**
   * electron-updater wants {info,warn,error,debug}; the shell has a
   * LogSink. `debug` is dropped: it narrates every byte of the download
   * and the ring buffer's job is to still hold the *server's* last words
   * when something goes wrong.
   */
  private loggerAdapter(): Record<string, (message?: unknown) => void> {
    const line = (level: string, message: unknown): void => {
      this.log.shell(`updater ${level}: ${textOf(message)}`);
    };
    return {
      info: (message) => line('info', message),
      warn: (message) => line('warn', message),
      error: (message) => line('error', message),
      debug: () => {},
    };
  }

  private runCheck(): Promise<void> {
    if (this.inFlight) return this.inFlight;
    const attempt = (async () => {
      this.errorReported = false;
      const updater = await this.ensureStarted();
      if (!updater) return;
      this.log.shell('checking for updates…');
      try {
        await updater.checkForUpdates();
      } catch (error) {
        this.reportError(error);
      }
    })().finally(() => {
      this.inFlight = null;
      this.userCheck = false;
    });
    this.inFlight = attempt;
    return attempt;
  }

  private onNotAvailable(): void {
    this.log.shell(`no update available (running ${this.options.appVersion})`);
    if (!this.userCheck || this.shuttingDown) return;
    void this.deps.showMessageBox({
      type: 'info',
      title: 'No updates',
      message: "You're up to date.",
      detail: `Collimate ${this.options.appVersion} is the latest version.`,
      buttons: ['OK'],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
  }

  private onProgress(payload: unknown): void {
    const percent = numberField(payload, 'percent');
    if (percent === null) return;
    // Once per 10% rather than per chunk: this stream is hundreds of
    // events and the log is a 400-line ring the failure UI reads.
    const bucket = Math.floor(percent / 10);
    if (bucket === this.lastProgressBucket) return;
    this.lastProgressBucket = bucket;
    this.log.shell(`downloading update: ${Math.round(percent)}%`);
  }

  /**
   * The update is on disk. Ask once, and only once.
   *
   * electron-updater can re-emit this (a differential-download retry, a
   * second check that finds the same staged file), and two stacked modal
   * dialogs saying the same thing is how a user ends up quitting twice.
   * A quit already in flight suppresses the dialog outright: a dialog
   * would arrive over a window that is being torn down.
   *
   * With automatic updates on, the download is staged for the next quit
   * at once, whatever the answer. Off, only "Restart Now" stages it, and a
   * download no user check asked for (one an automatic check began before
   * the switch was turned off) is neither offered nor installed.
   */
  private async onDownloaded(payload: unknown): Promise<void> {
    const version = versionOf(payload);
    const asked = this.userDownload;
    this.userDownload = false;
    this.pendingVersion = version;
    this.log.shell(`update downloaded: ${version ?? 'unknown version'}`);
    const automatic = this.deps.automaticUpdates();
    if (automatic) this.pending = true;

    if (this.dialogOpen || this.shuttingDown) return;
    if (!automatic && !asked) {
      this.log.shell('automatic updates off: the downloaded update is not offered');
      return;
    }
    this.dialogOpen = true;
    try {
      const { response } = await this.deps.showMessageBox({
        type: 'info',
        title: 'Update ready',
        message: version
          ? `Collimate ${version} is ready to install.`
          : 'A new version of Collimate is ready to install.',
        detail: this.downloadedDetail(automatic),
        buttons: ['Restart Now', 'Later'],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      });
      if (response === 0) {
        this.requestInstallAndQuit();
      } else if (this.deps.automaticUpdates()) {
        // Later, automatic updates on: the install happens on whatever
        // quit comes next, without asking again.
        this.pending = true;
      } else {
        // Later, automatic updates off: nothing installs until the user
        // checks again, which finds the same download.
        this.pending = false;
        this.log.shell(`update${this.versionSuffix()} not installed: Later, with automatic updates off`);
      }
    } catch (error) {
      this.log.shell(`update dialog failed: ${errorMessage(error)}`);
    } finally {
      this.dialogOpen = false;
    }
  }

  private downloadedDetail(automatic: boolean): string {
    const restart =
      'Collimate will finish the work it has running, stop the local server, and restart into ' +
      'the new version, which carries its own server.';
    const later = automatic
      ? 'Later installs it the next time you quit.'
      : 'Later skips it for now; Check for Updates… offers it again.';
    // Windows has no local server (see connect.ts bootTarget): every server
    // it talks to is updated on its own machine, never from here.
    if (this.options.platform === 'win32') {
      return `${restart} ${later} The servers you connect to update on their own machines.`;
    }
    return `${restart} ${later}`;
  }

  private versionSuffix(): string {
    return this.pendingVersion ? ` ${this.pendingVersion}` : '';
  }

  private reportError(error: unknown): void {
    const message = errorMessage(error);
    // Most failures reach us twice — electron-updater emits `error` *and*
    // rejects checkForUpdates — so within one check the first report wins.
    if ((this.inFlight || this.userDownload) && this.errorReported) return;
    this.errorReported = true;
    this.log.shell(`update check failed: ${message}`);
    if ((!this.userCheck && !this.userDownload) || this.shuttingDown) return;
    this.userDownload = false;
    void this.deps.showMessageBox({
      type: 'error',
      title: 'Update check failed',
      message: "Couldn't check for updates.",
      detail: message,
      buttons: ['OK'],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
  }
}

// ---------------------------------------------------------------------------
// payload readers — everything electron-updater hands us is `unknown` here
// ---------------------------------------------------------------------------

function versionOf(payload: unknown): string | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const version = (payload as Record<string, unknown>)['version'];
  return typeof version === 'string' ? version : null;
}

function numberField(payload: unknown, key: string): number | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const value = (payload as Record<string, unknown>)[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function textOf(message: unknown): string {
  if (typeof message === 'string') return message;
  if (message instanceof Error) return message.message;
  return String(message);
}
