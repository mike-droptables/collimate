import { MemoryLog, type LogSink } from './log.js';
import { errorMessage } from './errors.js';

/**
 * Auto-update, and the decisions around it.
 *
 * Like ServerController, this file has no Electron imports: dialogs,
 * quitting, the clock, and electron-updater itself all arrive through the
 * `deps` bag, so the paths nobody can reproduce on demand — a download
 * finishing during a shutdown, a second dialog racing the first, a
 * quitAndInstall that never hands off — are asserted in
 * desktop/tests/updater.test.ts rather than hoped about.
 *
 * electron-updater is loaded with a dynamic import inside `start()`, not
 * at module scope. Two reasons: a .deb install and every dev run never
 * pay its module init at all (see `supported`), and nothing in the update
 * path can execute before the shell has decided it wants an update path.
 *
 * What this class deliberately does NOT do is install anything on its own
 * initiative. `autoInstallOnAppQuit` is turned off and the handoff happens
 * at the very end of runQuitSequence (main/quit.ts), *after* the server
 * has been asked to stop — an installer that swapped the app bundle out
 * from under a running uvicorn and its generator subprocess tree is how
 * you orphan Docker containers.
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
  /** desktop/package.json version — what "you're up to date" names. */
  appVersion: string;
  env?: NodeJS.ProcessEnv;
  log?: LogSink;
  deps: UpdateControllerDeps;
}

/**
 * How long after the window appears before the first check.
 *
 * A cold first launch is still downloading a Python interpreter and the
 * whole server wheel through `uvx` while this timer runs; racing an
 * update download against that turns a slow first start into a stalled
 * one on exactly the connections that can least afford it.
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
  private fromMenu = false;
  private menuDownload = false;
  private errorReported = false;
  private startupCheckArmed = false;
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
   * Whether this install has an update path at all.
   *
   * Every `false` here is a place where offering the user an update would
   * be a lie, so the menu item is omitted rather than shown-and-broken
   * (see menu.ts) and no check is ever scheduled.
   */
  get supported(): boolean {
    return this.unsupportedReason === null;
  }

  /** Why not, in a form fit for the log. `null` when updates are on. */
  get unsupportedReason(): string | null {
    if (!this.options.isPackaged) return 'running unpackaged';
    if (this.env['COLLIMATE_UPDATE_CHECK'] === '0') return 'COLLIMATE_UPDATE_CHECK=0';
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

  /** True once an update has been downloaded and someone has to quit. */
  get installPending(): boolean {
    return this.pending;
  }

  /**
   * Configure electron-updater and subscribe. Idempotent; safe to call
   * before the first check, which is what index.ts does so the feed
   * resolution and any config error land in the log at a legible moment.
   */
  async start(): Promise<void> {
    await this.ensureStarted();
  }

  /** The one automatic check. See STARTUP_CHECK_DELAY_MS for the delay. */
  checkOnStartup(): void {
    if (!this.supported || this.startupCheckArmed) return;
    this.startupCheckArmed = true;
    this.deps.setTimeoutImpl(() => {
      void this.runCheck();
    }, STARTUP_CHECK_DELAY_MS);
  }

  /**
   * "Check for Updates…". Coalesced: a second click while the first check
   * is still out is a no-op rather than a second request and a second
   * pair of dialogs.
   */
  checkFromMenu(): void {
    if (!this.supported) return;
    if (this.inFlight) {
      this.log.shell('update check already in progress');
      return;
    }
    this.fromMenu = true;
    void this.runCheck();
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
    this.log.shell(`installing update${this.pendingVersion ? ` ${this.pendingVersion}` : ''} (quitAndInstall)`);
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
    this.menuDownload = false;
    this.pending = true;
    this.shuttingDown = true;
    this.log.shell('restarting to install the update');
    this.deps.quit();
  }

  // --- plumbing ------------------------------------------------------------

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

    // Download in the background, but never install behind the user's
    // back. autoInstallOnAppQuit is electron-updater's "just do it on the
    // way out" hook and it cannot be trusted here: shutdownAndExit ends in
    // app.exit(), which skips the will-quit handler that hook rides on, so
    // it would silently do nothing on some paths and race the server
    // teardown on others. installIfPending() is the one install site.
    updater.autoDownload = true;
    updater.autoInstallOnAppQuit = false;
    // The version in the feed can only ever move forward. A downgrade
    // would also mean running `uvx --from collimate==<older>` against a
    // data dir a newer server has already migrated.
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
      if (!this.fromMenu || this.shuttingDown) return;
      this.menuDownload = true;
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
      this.fromMenu = false;
    });
    this.inFlight = attempt;
    return attempt;
  }

  private onNotAvailable(): void {
    this.log.shell(`no update available (running ${this.options.appVersion})`);
    if (!this.fromMenu || this.shuttingDown) return;
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
   * A quit already in flight suppresses it outright: the install is
   * happening anyway, and a dialog would arrive over a window that is
   * being torn down.
   */
  private async onDownloaded(payload: unknown): Promise<void> {
    const version = versionOf(payload);
    this.menuDownload = false;
    this.pending = true;
    this.pendingVersion = version;
    this.log.shell(`update downloaded: ${version ?? 'unknown version'}`);

    if (this.dialogOpen || this.shuttingDown) return;
    this.dialogOpen = true;
    try {
      const { response } = await this.deps.showMessageBox({
        type: 'info',
        title: 'Update ready',
        message: version
          ? `Collimate ${version} is ready to install.`
          : 'A new version of Collimate is ready to install.',
        detail: this.downloadedDetail(),
        buttons: ['Restart Now', 'Later'],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      });
      if (response === 0) this.requestInstallAndQuit();
      // "Later" leaves `pending` set: the install still happens, on
      // whatever quit comes next, without asking again.
    } catch (error) {
      this.log.shell(`update dialog failed: ${errorMessage(error)}`);
    } finally {
      this.dialogOpen = false;
    }
  }

  private downloadedDetail(): string {
    const detail =
      'Collimate will finish the work it has running, stop the local server, and restart into ' +
      'the new version. The first launch after an update fetches the matching Collimate server, ' +
      'so it can take a minute.';
    // Windows has no local server (see connect.ts bootTarget): the shell
    // doesn't own the server it talks to, so updating the app updates
    // only half the pair — and unlike the local server, where a bridge
    // mismatch is a warning in the log, a remote is refused when it
    // speaks a bridge protocol the shell doesn't implement.
    if (this.options.platform === 'win32') {
      return `${detail} You'll also want to upgrade the Collimate server you connect to, so the two stay in step.`;
    }
    return detail;
  }

  private reportError(error: unknown): void {
    const message = errorMessage(error);
    // Most failures reach us twice — electron-updater emits `error` *and*
    // rejects checkForUpdates — so within one check the first report wins.
    if ((this.inFlight || this.menuDownload) && this.errorReported) return;
    this.errorReported = true;
    this.log.shell(`update check failed: ${message}`);
    if ((!this.fromMenu && !this.menuDownload) || this.shuttingDown) return;
    this.menuDownload = false;
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
