import { errorMessage } from './errors.js';
import { app, BrowserWindow, crashReporter, dialog, ipcMain, Menu, net, session } from 'electron';
import { buildInfoOf, buildSigned, describeBuild, readBuildStamp, type BuildStampRead } from './buildStamp.js';
import { randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import path from 'node:path';
import type { ServerInfo, SplashAction, SplashView } from '../shared/bridge.js';
import {
  DEFAULT_INTERFACE_SCALE,
  IPC,
  LOCAL_SERVER_ID,
  SHELL_VERSION,
  stepInterfaceScale,
  type InterfaceScale,
  type ServersReply,
} from '../shared/bridge.js';
import {
  bootTarget,
  commitEditingPrefs,
  defaultSettings,
  editingPrefsOf,
  findServer,
  installEditingPrefsPush,
  readSettings,
  withEnvServer,
  writeSettings,
  type DesktopSettings,
} from './connect.js';
import { ServerAccess, shellRequestInit } from './serverAccess.js';
import { installContextMenu } from './contextMenu.js';
import { chosenFolder, folderDialogOptions, parsePickFolder } from './folderPicker.js';
import { chooseLocationOwned, type ChannelsOverride, type LocationOwned } from './locationOwned.js';
import { applyInterfaceScale, installInterfaceScale } from './interfaceScale.js';
import { guestWindowOf, installGuestViews } from './guestViews.js';
import { MemoryLog, RunLog } from './log.js';
import { installMenu } from './menu.js';
import { profileChannelsDir, resolveProfile, USER_DATA_ENV_VAR } from './profile.js';
import { runQuitSequence } from './quit.js';
import { ServerController, ServerStartError, type FailureReason, type FetchLike } from './server.js';
import {
  parseDraft,
  parseId,
  parseOpenChannel,
  parsePrefs,
  parseProbe,
  parseUpdate,
  RegistryError,
  ServerRegistry,
  serversForView,
  serverName,
  type WindowTarget,
} from './servers.js';
import { installTitleBarControl } from './titleBar.js';
import { resolveShellEnv } from './shellEnv.js';
import { Splash } from './splash.js';
import { UpdateController, type AutoUpdaterLike } from './updater.js';
import { createMainWindow, openExternally } from './window.js';
import { waitForNewChannelPage } from './newChannelHandoff.js';

/**
 * The Collimate desktop shell.
 *
 * It does one thing: make sure there is a Collimate server, then show it.
 * No UI assets ship here — the window loads the server's own frontend over
 * HTTP, so the desktop app and a browser tab are the same application by
 * construction, and a server upgrade upgrades the app.
 *
 * One window shows one server: the local server this shell starts (or
 * adopts) at every launch on POSIX, or a saved remote. Changing servers
 * recreates the window on the other origin; the local server keeps running
 * whatever the window shows. The registry (servers.ts) owns the list, the
 * page drives it over the bridge, and the splash's connect form is the
 * fallback when there is no window.
 */

// Which Collimate this is — the installed app, or a dev run out of a
// checkout. Resolved at module load because every identity below has to be
// set before the first getPath('userData') call, and `app.isPackaged` (the
// input) is one of the few things Electron answers before `ready`.
//
// The name is the load-bearing one: it decides the directory the shell
// keeps its logs, settings, channel data and single-instance lock in, so
// giving a dev run its own name is what lets it run beside the installed
// app instead of colliding with it. See main/profile.ts.
const profile = resolveProfile({ isPackaged: app.isPackaged, env: process.env });
app.setName(profile.name);
app.setAppUserModelId(profile.appUserModelId);
// A test harness names the shell's directory outright: on macOS Electron
// resolves Application Support from the account, not $HOME, so a HOME set
// for isolation would not move it (frontend/tests/electron/desktop.ts).
const userDataOverride = (process.env[USER_DATA_ENV_VAR] ?? '').trim();
if (userDataOverride) {
  app.setPath('userData', userDataOverride);
  app.setPath('sessionData', userDataOverride);
}

// Native Wayland when the session offers it (VS Code ships the same hint).
// Hosted in XWayland instead, Chromium pads a frameless window with
// invisible client-side shadow margins (_GTK_FRAME_EXTENTS), and Wayland
// compositors outline the whole X11 window — the app reads as inset from
// its own focus border. A native Wayland surface has no such margins.
// 'auto' still resolves to X11 on an X11 session.
if (process.platform === 'linux') {
  app.commandLine.appendSwitch('ozone-platform-hint', 'auto');
  // The Wayland app id (and the WM_CLASS-ish handle desktop environments
  // match icons and window rules on). Without it a dev run reports the
  // generic "electron", which compositor window rules can't target — and
  // with the installed app's id it would share that app's taskbar entry
  // and icon, which is the confusion the dev profile exists to avoid.
  app.setDesktopName(profile.desktopName);
}

// Local minidumps only, never uploaded. A native crash (Chromium CHECK,
// Wayland protocol error, GTK abort) kills the process with a bare signal —
// SIGTRAP in a terminal names nothing. With Crashpad armed, the stack lands
// in getPath('crashDumps') and the next run can say what actually died.
crashReporter.start({ uploadToServer: false });

const isDev = !app.isPackaged;
/** Windows has no local server: it is connect-only (no server environment, no POSIX process groups). */
const hasLocalServer = process.platform !== 'win32';
/**
 * What the packaged app's package.json says it was built from (every
 * package carries the stamp). A dev run reads none: desktop/package.json
 * carries no stamp, and its process.resourcesPath is Electron's own.
 */
const buildStampRead: BuildStampRead = app.isPackaged ? readBuildStamp(app.getAppPath()) : { status: 'absent' };
/** The build stamp, or null in a dev run (or a package whose stamp is missing or malformed). */
const buildStamp = buildStampRead.status === 'valid' ? buildStampRead.stamp : null;

let log: RunLog;
let settings: DesktopSettings = defaultSettings();
let userDataDir = '';
let controller: ServerController | null = null;
let registry: ServerRegistry | null = null;
let updater: UpdateController | null = null;
let splash: Splash | null = null;
let mainWindow: BrowserWindow | null = null;
/** What the window shows (or last showed, for macOS `activate`). */
let windowTarget: WindowTarget | null = null;
/** What Retry on the failure splash reopens. */
let retryServerId: string | null = null;
let quitting = false;
/** One window change at a time. */
let switchBusy = false;
/** A deliberate local stop: its exit is not a crash. */
let stoppingLocal = false;
/** The local server's startup progress goes to the splash (nothing else is on screen). */
let localInForeground = false;
/** Set by File → Quit, Keep Server Running for this one quit. */
let keepServerOnce = false;
let serverAccess: ServerAccess | null = null;
let shellEnvironment: Promise<void> | null = null;
/** The location-owned question while it is on screen, shared by every start that hit it. */
let locationOwnedPrompt: Promise<ChannelsOverride | null> | null = null;

// Electron's net stack, not Node's: requests then follow the system proxy
// and — the part that matters for TOFU — go through the default session's
// certificate verify proc, so a self-signed server can be pinned at probe
// time instead of failing before the user is ever asked. Marked as the
// shell's own, so the window's bearer injection never overrides (or adds)
// the credential a probe or poll chose.
const nodeFetch: FetchLike = (url, init) => net.fetch(url, shellRequestInit(init));

function main(): void {
  // A second launch must not spawn a second server against the same data
  // directory — two uvicorns writing one SQLite file is a corrupted
  // channel waiting to happen.
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }

  app.on('second-instance', () => {
    const win = liveMainWindow();
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
      return;
    }
    splash?.show();
  });

  app.on('window-all-closed', () => {
    // macOS convention: the app stays in the dock with no windows, and
    // `activate` brings the window back — with the server still running,
    // so it comes back instantly.
    if (process.platform === 'darwin' && !quitting) return;
    app.quit();
  });

  app.on('activate', () => {
    const win = liveMainWindow();
    if (win) {
      win.focus();
      return;
    }
    if (windowTarget && !switchBusy) {
      showMainWindow(windowTarget.info);
      return;
    }
    splash?.show();
  });

  app.on('before-quit', onBeforeQuit);

  // Chromium restarts a dead GPU process on its own, and the window survives
  // it — but every restart is one or more frames of bare window background
  // before the page is composited again, which a user reports as "the
  // background flickers". A line in the log is what turns that report into
  // a diagnosis (driver, compositor, out-of-VRAM) instead of a mystery.
  app.on('child-process-gone', (_event, details) => {
    if (details.type !== 'GPU') return;
    log?.shell(
      `GPU process gone: reason=${details.reason} exitCode=${String(details.exitCode)}` +
        (details.name ? ` name=${details.name}` : ''),
    );
  });

  process.on('uncaughtException', (error: Error) => {
    log?.shell(`uncaught exception: ${error.stack ?? error.message}`);
  });

  process.on('unhandledRejection', (error: unknown) => {
    const message = error instanceof Error ? (error.stack ?? error.message) : errorMessage(error);
    log?.shell(`unhandled rejection: ${message}`);
  });

  app.whenReady().then(bootstrap, (error: unknown) => {
    fatal('Collimate failed to start', error);
  });
}

async function bootstrap(): Promise<void> {
  userDataDir = app.getPath('userData');
  log = RunLog.inUserData(userDataDir);
  log.shell(
    `Collimate desktop ${SHELL_VERSION} — electron ${process.versions.electron}, ` +
      `node ${process.versions.node}, ${process.platform}/${process.arch}`,
  );
  log.shell(`profile: ${profile.id} (${profile.name}), packaged=${String(app.isPackaged)}`);
  if (buildStamp) log.shell(describeBuild(buildStamp));
  if (app.isPackaged && buildStampRead.status !== 'valid') {
    log.shell(`collimate.build stamp is ${buildStampRead.status}: the package's server environment cannot be named`);
  }
  log.shell(`userData: ${userDataDir}`);
  log.shell(`crash dumps: ${app.getPath('crashDumps')}`);

  // A window before the first wait, deliberately: the shell-environment
  // probe below runs the user's login shell with all of its rc files, which
  // is seconds on a busy machine and the full timeout in the worst case.
  // Until something is on screen, a launcher click has produced no feedback
  // whatsoever, and the second click it provokes starts nothing — the
  // single-instance lock hands it to this process, which is still waiting.
  // So the splash goes up first, saying what is being waited on.
  splashInstance().status('Starting Collimate…', 'reading your shell environment');

  // Start once now; opening a remote does not depend on shell tools. Only
  // the local server's start waits for it.
  shellEnvironment = applyShellEnv();

  // Built here, before the menu, and armed much later (see
  // beginUpdateChecks): the menu has to know at build time whether this
  // install can update at all, and constructing the controller costs
  // nothing — electron-updater isn't imported until a check is wanted.
  updater = new UpdateController({
    platform: process.platform,
    isPackaged: app.isPackaged,
    signed: buildSigned(app.getAppPath()),
    // Where electron-updater reads the feed of a packaged app.
    packagedFeed: app.isPackaged && existsSync(path.join(process.resourcesPath, 'app-update.yml')),
    appVersion: SHELL_VERSION,
    log,
    deps: {
      loadUpdater: async () => {
        const { autoUpdater } = await import('electron-updater');
        return autoUpdater as unknown as AutoUpdaterLike;
      },
      // Read at each decision; settings.json is loaded before the window
      // that arms the first one.
      automaticUpdates: () => settings.automaticUpdates,
      showMessageBox: (options) => dialog.showMessageBox(options),
      quit: () => app.quit(),
      setTimeoutImpl: (fn, ms) => {
        setTimeout(fn, ms);
      },
    },
  });
  if (!updater.supported) log.shell(`updates unavailable: ${updater.unsupportedReason}`);

  installMenu({
    isDev,
    appName: profile.name,
    // Omitted, not disabled, when this install has no update path: a
    // greyed-out or lying menu item reads as a broken app. Present whatever
    // the automatic-updates setting says. A thunk rather than a bound
    // method so the click reads `updater` at click time.
    ...(updater.supported ? { onCheckForUpdates: () => void updater?.checkNow() } : {}),
    onOpenServers: () => openServersSurface(),
    // No local server on Windows, so nothing to keep running there.
    ...(hasLocalServer
      ? {
          onQuitKeepServer: () => {
            keepServerOnce = true;
            quitApp();
          },
          quitKeepServerEnabled: false,
        }
      : {}),
    // The View menu's zoom items step the same per-device scale Settings → This
    // device sets, and persist it.
    onInterfaceScale: (step) => commitInterfaceScale(stepInterfaceScale(currentInterfaceScale(), step)),
  });

  ipcMain.handle(IPC.openExternal, async (_event, url: unknown) => {
    if (typeof url !== 'string') return false;
    return openExternally(url);
  });

  // Everything written to the log streams into the splash, so a stuck
  // start shows the resolver output that explains it rather than a
  // spinner.
  log.onLine((line) => splash?.pushLog(line));

  // The automation URL is saved (never its token) so it has an id to open.
  const loaded = readSettings(userDataDir);
  settings = withEnvServer(loaded, process.env, randomUUID);
  if (settings !== loaded) writeSettings(userDataDir, settings);
  log.shell(`automatic updates: ${settings.automaticUpdates ? 'on' : 'off'}`);

  registry = buildRegistry();
  installServersIpc(registry);
  installUpdatesIpc(updater);
  installProfileIpc();
  installDialogsIpc();
  registry.start();

  // The local server starts (or is adopted) at every launch on POSIX,
  // whatever the window opens on: its channels belong in the picker, and
  // a window opened on a remote must not wait for it.
  const localStart = hasLocalServer ? startLocalServer() : null;
  localStart?.catch(() => undefined);

  const { serverId } = bootTarget({ platform: process.platform, env: process.env, settings });
  const forcedByEnv = Boolean(process.env['COLLIMATE_CONNECT_URL']);
  log.shell(`opening ${serverId ?? 'the connect form'}${forcedByEnv ? ' (COLLIMATE_CONNECT_URL)' : ''}`);

  if (serverId === LOCAL_SERVER_ID) {
    await openLocalFromSplash(localStart);
    return;
  }
  if (serverId === null) {
    connectFormView({ url: process.env['COLLIMATE_CONNECT_URL'] ?? '' });
    return;
  }
  const opened = await openServerFromSplash(serverId, { formOnFailure: forcedByEnv || !hasLocalServer });
  // The server the window was last on is gone: the local server instead.
  // A target forced by automation is never second-guessed; its failure
  // stays on the connect form.
  if (!opened && !forcedByEnv && hasLocalServer && !quitting) await openLocalFromSplash(localStart);
}

/**
 * Give this process the environment the user's terminal would have.
 *
 * A launcher-started app inherits the desktop session's environment, not
 * the one the login shell builds — so PATH lacks everything nvm, fnm,
 * volta, pyenv or homebrew add from an rc file. The server we spawn
 * inherits our environment, so without this a feature that shells out
 * (the chat backend's node-based ACP adapter is the one that bit) works
 * when Collimate is started from a terminal and fails when it is started
 * from the app menu — with an error that names a toolchain version the
 * user does not believe they have.
 *
 * Assigned onto the live `process.env` rather than threaded through
 * options: ServerController and findUv both default to `process.env`
 * and read it at spawn time, so one assignment here reaches the uv
 * probes, the server child, and everything the server itself spawns.
 *
 * Non-fatal in every path, including an unexpected throw: the app has to
 * start on a machine with a broken rc file just as well as on a clean one,
 * and the worst case of skipping this is the behaviour we had before.
 */
async function applyShellEnv(): Promise<void> {
  try {
    const result = await resolveShellEnv({
      platform: process.platform,
      env: process.env,
      onSkip: (reason) => log.shell(`shell env: skipped (${reason})`),
    });
    if (!result) return;
    Object.assign(process.env, result.env);
    if (result.changed.length === 0) {
      log.shell('shell env: unchanged');
      return;
    }
    // The PATH additions are named, not counted: when this feature is the
    // suspect, the question is always "did it pick up the version manager"
    // and the answer is exactly this list.
    const added = result.pathAdded.length ? ` (PATH +${result.pathAdded.join(', +')})` : '';
    log.shell(`shell env: merged ${result.changed.length} vars from ${result.shell}${added}`);
  } catch (error) {
    log.shell(`shell env: failed (${errorMessage(error)})`);
  }
}

// ---------------------------------------------------------------------------
// the local server
// ---------------------------------------------------------------------------

/**
 * The one ServerController, built on first need and kept for the life of
 * the process. Two controllers over the same runtimeDir would fight over
 * server.json and reap each other's server, and the event subscriptions
 * here must exist exactly once.
 *
 * Built once means the options below are read once. That is fine for
 * every one of them — `localPort` only matters to a new spawn — but it is
 * why `settings` has to be loaded before anything calls this (bootstrap
 * does).
 */
function ensureController(): ServerController {
  if (controller) return controller;
  const channelsDir = profileChannelsDir(profile, app.getPath('home'));
  controller = new ServerController({
    // The data dir IS the server's cwd (see api/collimate_server/main.py cli_main), so
    // .collimate/ lands inside userData and survives an app reinstall.
    dataDir: userDataDir,
    runtimeDir: userDataDir,
    appVersion: SHELL_VERSION,
    isPackaged: app.isPackaged,
    appPath: app.getAppPath(),
    // A package runs the server environment it carries in its resources;
    // an AppImage from a stable copy, since its mount moves every launch.
    ...(buildStamp?.server
      ? {
          bundledServer: {
            resourcesPath: process.resourcesPath,
            hash: buildStamp.server,
            stableCopy: process.platform === 'linux' && Boolean(process.env['APPIMAGE']),
          },
        }
      : {}),
    // Read at every start: a restart then prefers the port the last
    // start saved (below), so the local origin holds within a session too.
    preferredPort: () => settings.localPort,
    // The profile's own visible channel location (~/Collimate/Channels,
    // the dev profile's Channels-Dev, D17), so neither refuses on the other's.
    channelsDir,
    log,
  });

  controller.events.on('status', ({ message, detail }) => {
    // Only while nothing else is on screen: a local start behind a window
    // on a remote server must not pop the splash over it.
    if (localInForeground || quitting) splashInstance().status(message, detail);
  });
  controller.events.on('state', (state) => {
    if (state.name === 'ready' && settings.localPort !== state.info.port) {
      saveSettings({ ...settings, localPort: state.info.port });
    }
    const item = Menu.getApplicationMenu()?.getMenuItemById('quit-keep-server');
    if (item) item.enabled = state.name === 'ready';
    registry?.notify();
  });
  controller.events.on('crashed', ({ tail }) => {
    void onServerCrashed(tail);
  });
  return controller;
}

/**
 * Start (or adopt) the local server once the shell environment is in.
 *
 * A start the server refuses because its channel location belongs to
 * another installation asks the user (askLocationOwned) and starts again
 * with the answer's flag, for that one spawn; Quit quits.
 */
async function startLocalServer(restart = false): Promise<ServerInfo> {
  await shellEnvironment;
  if (quitting) throw new RegistryError('Collimate is quitting.');
  const active = ensureController();
  let attempt = restart ? active.restart() : active.start();
  for (;;) {
    try {
      return await attempt;
    } catch (error) {
      const refusal = error instanceof ServerStartError ? error.state.locationOwned : undefined;
      if (!refusal || quitting) throw error;
      const override = await askLocationOwned(refusal);
      if (!override || quitting) {
        quitApp();
        throw error;
      }
      log.shell(`location owned: starting again with ${'claimChannels' in override ? '--claim-channels' : `--channels-dir ${override.channelsDir}`}`);
      attempt = active.start(override);
    }
  }
}

/**
 * The native question for a location-owned refusal (locationOwned.ts):
 * Claim it, Choose another folder…, or Quit. One question at a time, shared
 * by every start that is waiting on it.
 */
function askLocationOwned(refusal: LocationOwned): Promise<ChannelsOverride | null> {
  if (locationOwnedPrompt) return locationOwnedPrompt;
  const prompt = chooseLocationOwned(refusal, {
    ask: async (options) => {
      const win = liveMainWindow();
      const { response } = win ? await dialog.showMessageBox(win, options) : await dialog.showMessageBox(options);
      return response;
    },
    pickFolder: async (options) => {
      const dialogOptions = folderDialogOptions(options);
      const win = liveMainWindow();
      return chosenFolder(win ? await dialog.showOpenDialog(win, dialogOptions) : await dialog.showOpenDialog(dialogOptions));
    },
  }).finally(() => {
    locationOwnedPrompt = null;
  });
  locationOwnedPrompt = prompt;
  return prompt;
}

/**
 * Bring the window up on the local server with the splash showing the
 * start, or the failure. `pending` is a start already under way.
 */
async function openLocalFromSplash(pending: Promise<ServerInfo> | null = null, restart = false): Promise<void> {
  retryServerId = LOCAL_SERVER_ID;
  localInForeground = true;
  splashInstance().status(restart ? 'Restarting Collimate…' : 'Starting Collimate…');
  try {
    const info = await (pending && !restart ? pending : startLocalServer(restart));
    if (quitting) return;
    await openWindowOn({ info, credential: null });
  } catch (error) {
    showStartFailure(error);
  } finally {
    localInForeground = false;
  }
}

function showStartFailure(error: unknown): void {
  // Quitting mid-startup makes start() fail (we killed the child). That's
  // not news to show anyone.
  if (quitting) return;
  if (error instanceof ServerStartError) {
    const { reason, message, command, tail } = error.state;
    splashInstance().setView({
      kind: 'failure',
      title: failureTitle(reason),
      message,
      ...(command ? { command } : {}),
      tail,
    });
    return;
  }
  const message = errorMessage(error);
  log?.shell(`startup failed: ${message}`);
  splashInstance().setView({
    kind: 'failure',
    title: 'Collimate failed to start',
    message,
    tail: log?.tail() ?? [],
  });
}

function failureTitle(reason: FailureReason): string {
  switch (reason) {
    case 'uvMissing':
      return 'Collimate needs uv';
    case 'spawnFailed':
      return "The server command wouldn't run";
    case 'healthTimeout':
      return "The server didn't come up";
    case 'earlyExit':
      return 'The server stopped while starting';
    case 'bundleBroken':
      return "This package's server is missing or damaged";
    case 'locationOwned':
      return 'The channel location belongs to another installation';
  }
}

/**
 * The local server died. With the window on it that is not recoverable
 * silently — the window is pointed at a dead origin — so ask, with the log
 * tail visible, because the tail is usually the whole answer. With the
 * window on another server the Servers list says so (stopped) and nothing
 * interrupts the user.
 */
async function onServerCrashed(tail: string[]): Promise<void> {
  if (quitting || stoppingLocal) return;
  registry?.notify();
  if (windowTarget?.info.id !== LOCAL_SERVER_ID || !liveMainWindow()) {
    log.shell('the local server stopped while the window shows another server');
    return;
  }
  const detail = tail.length ? tail.join('\n') : 'No output was captured.';
  const { response } = await dialog.showMessageBox({
    type: 'error',
    title: 'Collimate server stopped',
    message: 'The Collimate server stopped unexpectedly.',
    detail,
    buttons: ['Restart Server', 'Quit'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  });
  if (response !== 0) {
    quitApp();
    return;
  }
  // Splash first: destroying the only window would fire window-all-closed
  // and quit.
  splashInstance().status('Restarting Collimate…');
  await destroyMainWindowAndWait();
  await openLocalFromSplash(null, true);
}

/** Stop the local server after the registry has confirmed a usable remote window. */
async function stopLocalServer(reportFailureToNewWindow = false): Promise<void> {
  if (quitting) throw new RegistryError('Collimate is quitting.');
  if (windowTarget?.info.id === LOCAL_SERVER_ID) {
    throw new RegistryError('Open another server before stopping local.');
  }
  const active = controller;
  if (!active) return;
  stoppingLocal = true;
  try {
    try {
      await active.stop();
    } catch (error) {
      const win = reportFailureToNewWindow ? liveMainWindow() : null;
      win?.webContents.send(IPC.shellCommand, {
        type: 'localStopFailed', message: 'The local server could not stop. Check its status in Servers.',
      });
      throw error;
    }
  } finally {
    stoppingLocal = false;
  }
}

/**
 * Stop the local server for a quit, and say so — teardown stops every
 * generator and Docker sandbox and can take tens of seconds. A no-op when
 * there is none or it is already down.
 */
async function stopCurrentServer(message: string, detail?: string): Promise<void> {
  const active = controller;
  if (!active) return;
  const state = active.state.name;
  if (state === 'stopped' || state === 'idle' || state === 'released') return;
  splashInstance().status(message, detail);
  await active.stop();
}

// ---------------------------------------------------------------------------
// servers and the window
// ---------------------------------------------------------------------------

/**
 * The one ServerAccess over the app's default session: the window's bearer
 * header and every saved https server's certificate pin. Guests live in
 * their own partitions and are never touched.
 */
function access(): ServerAccess {
  if (serverAccess) return serverAccess;
  serverAccess = new ServerAccess(session.defaultSession, {
    confirmPin: async (host, fingerprint) => {
      const { response } = await dialog.showMessageBox({
        type: 'warning',
        title: 'Unverified server certificate',
        message: `${host} presented a certificate that isn't from a trusted authority.`,
        detail:
          `Fingerprint:\n${fingerprint}\n\n` +
          'Trust it only if this is your own server and the fingerprint matches ' +
          'the one it printed at startup (or your reverse proxy reports). The choice ' +
          'is remembered for this server; a server with a new certificate has to be ' +
          'removed and added again.',
        buttons: ['Trust This Certificate', 'Cancel'],
        defaultId: 1,
        cancelId: 1,
        noLink: true,
      });
      return response === 0;
    },
    savePin: (serverId, fingerprint) => registry?.savePin(serverId, fingerprint),
    log: (line) => log?.shell(line),
  });
  return serverAccess;
}

/** Persist settings; every other key rides along in `next`. */
function saveSettings(next: DesktopSettings): void {
  settings = next;
  writeSettings(userDataDir, settings);
}

function buildRegistry(): ServerRegistry {
  const built = new ServerRegistry({
    fetchImpl: nodeFetch,
    settings: () => settings,
    saveSettings,
    env: process.env,
    mintId: randomUUID,
    setPins: (pins) => access().setPins(pins),
    pinPrompt: () => access().pendingPrompt(),
    isFocused: () => liveMainWindow()?.isFocused() ?? false,
    setTimeoutImpl: (fn, ms) => setTimeout(fn, ms),
    clearTimeoutImpl: (handle) => clearTimeout(handle as NodeJS.Timeout),
    log: (line) => log.shell(line),
    host: {
      local: () => (controller ? { state: controller.state, info: controller.info } : hasLocalServer ? { state: { name: 'idle' }, info: null } : null),
      windowServerId: () => (liveMainWindow() ? (windowTarget?.info.id ?? null) : null),
      openWindow: (target, options) => options?.newChannel ? handoffWindowOn(target) : openWindowOn(target),
      startLocal: async () => {
        const info = await startLocalServer();
        // Started from the page there is a window already; from nowhere
        // else can this run without one, but never leave the user with none.
        if (!liveMainWindow() && !quitting) await openWindowOn({ info, credential: null });
        return info;
      },
      stopLocal: (options) => stopLocalServer(options?.reportFailureToNewWindow),
      credentialChanged: (id) => {
        if (windowTarget?.info.id !== id) return;
        const credential = built.targetCredential(id);
        windowTarget = { ...windowTarget, credential };
        access().bindWindow(credential);
      },
      confirm: async ({ message, detail, confirmLabel }) => {
        const win = liveMainWindow();
        const options = {
          type: 'question' as const,
          message,
          ...(detail ? { detail } : {}),
          buttons: [confirmLabel, 'Cancel'],
          defaultId: 1,
          cancelId: 1,
          noLink: true,
        };
        const { response } = win ? await dialog.showMessageBox(win, options) : await dialog.showMessageBox(options);
        return response === 0;
      },
    },
  });
  built.events.on('changed', (entries) => {
    const win = liveMainWindow();
    if (win && !win.webContents.isDestroyed()) win.webContents.send(IPC.serversChanged, entries);
  });
  return built;
}

/**
 * One invoke answered only for the app window's own webContents, as
 * `{ value }` or `{ error }`: the bridge is exposed to whatever the window
 * loads, a remote server's frontend included, and to nothing else.
 * Registered once for the life of the process.
 */
function handleForAppWindow(channel: string, run: (payload: unknown) => unknown): void {
  ipcMain.handle(channel, async (event, payload: unknown): Promise<ServersReply<unknown>> => {
    const win = liveMainWindow();
    if (!win || event.sender !== win.webContents) return { error: 'collimate-servers/not-app-window' };
    try {
      const value = await run(payload);
      return { value: value ?? null };
    } catch (error) {
      return { error: errorMessage(error) };
    }
  });
}

/**
 * Settings → This profile's updates: the same controller call as the menu's
 * Check for Updates…, and the automatic-updates setting, saved and handed
 * to the controller.
 */
function installUpdatesIpc(controls: UpdateController): void {
  handleForAppWindow(IPC.updatesCheck, () => controls.checkNow());
  handleForAppWindow(IPC.automaticUpdatesGet, () => settings.automaticUpdates);
  handleForAppWindow(IPC.automaticUpdatesSet, (payload) => {
    if (typeof payload !== 'boolean') throw new RegistryError('automaticUpdates takes true or false.');
    if (payload !== settings.automaticUpdates) {
      saveSettings({ ...settings, automaticUpdates: payload });
      controls.automaticUpdatesChanged();
    }
    return settings.automaticUpdates;
  });
}

/**
 * This profile's editing preferences (Settings → This profile): a patch is
 * checked, saved to settings.json when it changes anything, and the result
 * pushed to the page as well as answered, so every listener (not only the
 * caller) sees it.
 */
function installProfileIpc(): void {
  handleForAppWindow(IPC.editingPrefsSet, (payload) =>
    commitEditingPrefs(settings, payload, {
      save: saveSettings,
      push: (prefs) => {
        const win = liveMainWindow();
        if (win && !win.webContents.isDestroyed()) win.webContents.send(IPC.editingPrefs, prefs);
      },
    }),
  );
}

/**
 * The native dialogs the page opens (see handleForAppWindow). The folder
 * chooser answers only a window on the local server: the path it returns
 * names this machine's disk, and a remote server's page has no use for it
 * (nor any business learning it).
 */
function installDialogsIpc(): void {
  handleForAppWindow(IPC.dialogPickFolder, async (payload) => {
    const options = folderDialogOptions(parsePickFolder(payload));
    if (windowTarget?.info.kind !== 'local') throw new Error('collimate-dialog/not-local-server');
    const win = liveMainWindow();
    if (!win) return null;
    return chosenFolder(await dialog.showOpenDialog(win, options));
  });
}

/** The servers/prefs IPC (see handleForAppWindow). */
function installServersIpc(servers: ServerRegistry): void {
  const handle = handleForAppWindow;
  handle(IPC.serversList, () => servers.list());
  handle(IPC.serversAdd, (payload) => servers.add(parseDraft(payload)));
  handle(IPC.serversUpdate, (payload) => servers.update(parseUpdate(payload)));
  handle(IPC.serversRemove, (payload) => servers.remove(parseId(payload)));
  handle(IPC.serversProbe, (payload) => servers.probe(parseProbe(payload)));
  handle(IPC.serversOpen, (payload) => servers.openServer(parseId(payload)));
  handle(IPC.serversOpenChannel, (payload) => {
    const { id, channelId } = parseOpenChannel(payload);
    return servers.openChannel(id, channelId);
  });
  handle(IPC.serversStartLocal, () => servers.startLocal());
  handle(IPC.serversStopLocal, () => servers.stopLocal());
  handle(IPC.shellPrefsGet, () => servers.getPrefs());
  handle(IPC.shellPrefsSet, (payload) => servers.setPrefs(parsePrefs(payload)));
}

/**
 * Recreate the window on `target`: the same server just reloads, any other
 * destroys the window and opens a new one on that origin with the old
 * window's bounds and maximized state. Never repointed in place, because
 * the preload bootstrap, the will-navigate origin pin and the
 * did-fail-load closure all freeze the server at window creation. The
 * local server is never stopped here.
 */
async function openWindowOn(
  target: WindowTarget,
  options: { newChannel?: boolean; waitForNewChannel?: boolean; bounds?: Electron.Rectangle; maximized?: boolean } = {},
): Promise<void> {
  if (quitting) throw new RegistryError('Collimate is quitting.');
  if (switchBusy) throw new RegistryError('Another server change is in progress.');
  switchBusy = true;
  const handoffAbort = options.waitForNewChannel ? new AbortController() : null;
  let handoffSplash: BrowserWindow | null = null;
  const onHandoffSplashClosed = (): void => {
    handoffAbort?.abort();
    quitApp();
  };
  try {
    const old = liveMainWindow();
    if (old && windowTarget?.info.id === target.info.id && windowTarget.info.url === target.info.url) {
      old.webContents.reload();
      old.focus();
      return;
    }
    // The normal bounds only stand in for a maximized or full-screen
    // window; otherwise the bounds it has are the ones to keep.
    const geometry = old
      ? {
          bounds: old.isMaximized() || old.isFullScreen() ? old.getNormalBounds() : old.getBounds(),
          maximized: old.isMaximized(),
        }
      : {
          ...(options.bounds ? { bounds: options.bounds } : {}),
          ...(options.maximized ? { maximized: true } : {}),
        };
    if (old) {
      // Splash first: destroying the only window would fire
      // window-all-closed and quit.
      const transition = splashInstance();
      transition.status(`Opening ${target.info.name}…`);
      if (handoffAbort) {
        handoffSplash = transition.show();
        handoffSplash.once('closed', onHandoffSplashClosed);
      }
      await destroyMainWindowAndWait();
    }
    if (quitting) throw new RegistryError('Collimate is quitting.');
    const where = geometry.bounds
      ? ` at ${geometry.bounds.width}x${geometry.bounds.height}+${geometry.bounds.x}+${geometry.bounds.y}${geometry.maximized ? ', maximized' : ''}`
      : '';
    log.shell(`window → ${target.info.name} (${target.info.url})${where}`);
    access().bindWindow(target.credential);
    windowTarget = target;
    if (settings.activeServer !== target.info.id) saveSettings({ ...settings, activeServer: target.info.id });
    const next = showMainWindow(target.info, geometry, options.newChannel ? { newChannel: true } : {});
    if (options.waitForNewChannel) {
      await waitForNewChannelPage(next, 20_000, handoffAbort?.signal);
      if (quitting || next.isDestroyed() || !splash?.isOpen) {
        throw new RegistryError('The New channel handoff was cancelled.');
      }
      handoffSplash?.removeListener('closed', onHandoffSplashClosed);
      next.show();
      closeSplash();
      beginUpdateChecks();
    }
  } finally {
    handoffSplash?.removeListener('closed', onHandoffSplashClosed);
    switchBusy = false;
  }
}

/** Open the remote's New channel page before stopping local. On failure,
 * recreate the local window while its server is still running. */
async function handoffWindowOn(target: WindowTarget): Promise<void> {
  const old = liveMainWindow();
  const previous = windowTarget;
  if (!old || previous?.info.id !== LOCAL_SERVER_ID) {
    throw new RegistryError('The local window is no longer available.');
  }
  const geometry = {
    bounds: old.isMaximized() || old.isFullScreen() ? old.getNormalBounds() : old.getBounds(),
    maximized: old.isMaximized(),
  };
  try {
    await openWindowOn(target, { newChannel: true, waitForNewChannel: true });
  } catch (error) {
    if (!quitting && (!liveMainWindow() || windowTarget?.info.id !== LOCAL_SERVER_ID)) {
      await openWindowOn(previous, geometry);
    }
    throw error;
  }
}

/**
 * Open saved server `id` from the splash (boot, Retry, the connect form's
 * saved servers). Resolves false when it could not be opened; with
 * `formOnFailure` the connect form then says why.
 */
async function openServerFromSplash(id: string, options: { formOnFailure: boolean }): Promise<boolean> {
  const servers = registry;
  if (!servers) return false;
  retryServerId = id;
  const saved = findServer(settings, id);
  const label = saved ? serverName(saved) : 'the server';
  splashInstance().setView({ kind: 'connect', url: saved?.url ?? '', busy: true, title: `Connecting to ${label}…` });
  try {
    await servers.openServer(id);
    return true;
  } catch (error) {
    const message = errorMessage(error);
    log.shell(`couldn't open ${label}: ${message}`);
    if (options.formOnFailure && !quitting) connectFormView({ url: saved?.url ?? '', error: message });
    return false;
  }
}

/** The connect form's typed URL: save it (probing first), then open it. */
async function addAndOpenFromSplash(url: string, token: string | undefined): Promise<void> {
  const servers = registry;
  if (!servers) return;
  splashInstance().setView({ kind: 'connect', url, busy: true });
  let id: string;
  try {
    id = (await servers.add({ url, ...(token ? { token } : {}) })).id;
  } catch (error) {
    const message = errorMessage(error);
    log.shell(`connect failed: ${message}`);
    connectFormView({ url, error: message });
    return;
  }
  await openServerFromSplash(id, { formOnFailure: true });
}

/** File → Servers…: the page's Servers tab, or the splash form without a window. */
function openServersSurface(): void {
  if (quitting) return;
  const win = liveMainWindow();
  if (win) {
    win.webContents.send(IPC.shellCommand, { type: 'openServers' });
    win.focus();
    return;
  }
  if (switchBusy) {
    splashInstance().show();
    return;
  }
  connectFormView({ url: '' });
}

/** The connect form, with the choices every variant offers. Tokens never travel. */
function connectFormView(extra: Partial<Extract<SplashView, { kind: 'connect' }>> & { url: string }): void {
  splashInstance().setView({
    kind: 'connect',
    busy: false,
    canUseLocal: hasLocalServer,
    servers: serversForView(settings),
    token: '',
    ...extra,
  });
}

/**
 * Tear down the main window and wait for `closed`, which is when the guest
 * control surface uninstalls its ipcMain handlers — the next window's
 * install would throw over live registrations otherwise.
 */
async function destroyMainWindowAndWait(): Promise<void> {
  const old = mainWindow;
  mainWindow = null;
  if (!old || old.isDestroyed()) return;
  await new Promise<void>((resolve) => {
    old.once('closed', () => resolve());
    old.destroy();
  });
}

// ---------------------------------------------------------------------------
// windows
// ---------------------------------------------------------------------------

/** This profile's interface scale (settings.json), the default when unset. */
function currentInterfaceScale(): InterfaceScale {
  return settings.interfaceScale ?? DEFAULT_INTERFACE_SCALE;
}

/** Persist an interface scale and apply it to the app window, if open. */
function commitInterfaceScale(scale: InterfaceScale): void {
  if (scale !== currentInterfaceScale()) saveSettings({ ...settings, interfaceScale: scale });
  const win = liveMainWindow();
  if (win) applyInterfaceScale(win.webContents, scale);
}

/** The main window, unless it is gone or already destroyed. */
function liveMainWindow(): BrowserWindow | null {
  return mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
}

function splashInstance(): Splash {
  if (splash && splash.isOpen) return splash;
  if (!splash) splash = new Splash({ onAction: onSplashAction, title: profile.title });
  splash.show();
  return splash;
}

function onSplashAction(action: SplashAction): void {
  switch (action.type) {
    case 'quit':
      quitApp();
      return;
    case 'retry':
      if (retryServerId === LOCAL_SERVER_ID) {
        // A server that still answers is reopened, not restarted: a
        // renderer crash is no reason to stop its running work.
        void openLocalFromSplash(null, controller?.state.name !== 'ready');
      } else if (retryServerId) {
        void openServerFromSplash(retryServerId, { formOnFailure: true });
      } else {
        connectFormView({ url: '' });
      }
      return;
    case 'connect':
      // A saved server by id: its token is resolved here, never sent by
      // the page. A typed URL is saved (probed first) and then opened.
      if (action.serverId) {
        void openServerFromSplash(action.serverId, { formOnFailure: true });
      } else {
        void addAndOpenFromSplash(action.url, action.token);
      }
      return;
    case 'spawn':
      if (hasLocalServer) void openLocalFromSplash();
      return;
  }
}

function showMainWindow(
  info: ServerInfo,
  geometry: { bounds?: Electron.Rectangle; maximized?: boolean } = {},
  options: { newChannel?: boolean } = {},
): BrowserWindow {
  const existing = liveMainWindow();
  if (existing) {
    existing.focus();
    closeSplash();
    return existing;
  }

  // First paint can arrive after the user opened another splash view (a
  // failure, the connect form). The splash may be the same object/window
  // while its presentation belongs to that newer flow, so both instance
  // and view revision must still match.
  const startupSplash = splash;
  const startupViewRevision = startupSplash?.viewRevision;
  const window = createMainWindow({
    server: info,
    shellVersion: SHELL_VERSION,
    title: profile.title,
    interfaceScale: currentInterfaceScale(),
    editingPrefs: editingPrefsOf(settings),
    buildInfo: buildInfoOf(SHELL_VERSION, buildStamp),
    profileName: profile.name,
    ...(options.newChannel ? { newChannel: true } : {}),
    ...(options.newChannel ? { deferShow: true } : {}),
    ...(geometry.bounds ? { bounds: geometry.bounds } : {}),
    ...(geometry.maximized ? { maximized: true } : {}),
  });
  mainWindow = window;

  // Before the page can load, let alone ask for a guest: this installs
  // the guest IPC surface (creation included), and it uninstalls itself
  // when the window closes.
  installGuestViews({ window: guestWindowOf(window), log, isDev });
  // Same lifecycle for the page-drawn title bar's verbs (menus/maximize)
  // and for the native right-click menu (spelling suggestions, edit verbs).
  installTitleBarControl({ window });
  installContextMenu({ window });
  // The interface scale: re-applied after every load (Chromium's zoom is
  // per origin, and the window's origin changes with its server), set by
  // the page.
  installInterfaceScale({ window, current: currentInterfaceScale, commit: commitInterfaceScale });
  // The editing preferences, likewise pushed after every load.
  installEditingPrefsPush(window, () => editingPrefsOf(settings));

  window.once('ready-to-show', () => {
    if (mainWindow !== window || quitting) return;
    if (options.newChannel) return;
    if (splash === startupSplash && splash?.viewRevision === startupViewRevision) closeSplash();
    beginUpdateChecks();
  });
  window.on('focus', () => registry?.focused());
  window.on('closed', () => {
    if (mainWindow === window) mainWindow = null;
  });

  window.webContents.on('render-process-gone', (_event, details) => {
    if (quitting || mainWindow !== window || details.reason === 'clean-exit') return;
    showRendererFailure(`renderer process gone: reason=${details.reason} exitCode=${String(details.exitCode)}`);
  });

  window.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    // -3 is ERR_ABORTED, which Chromium reports for ordinary in-app
    // navigations that get replaced; it isn't a failure.
    if (!isMainFrame || errorCode === -3) return;
    log.shell(`window failed to load ${validatedURL}: ${errorDescription} (${errorCode})`);
    retryServerId = info.id;
    // Splash first: destroying the only window would fire window-all-closed
    // and quit.
    splashInstance().setView({
      kind: 'failure',
      title: "Couldn't load the workbench",
      message: `${info.url} stopped responding: ${errorDescription}.`,
      tail: log.tail(),
    });
    window.destroy();
    mainWindow = null;
  });

  log.shell(`window open on ${info.url}`);
  return window;
}

function closeSplash(): void {
  splash?.close();
  splash = null;
}

// ---------------------------------------------------------------------------
// updates
// ---------------------------------------------------------------------------

/**
 * Arm the automatic check once there is a window to interrupt (nothing
 * while automatic updates are off).
 *
 * Not in bootstrap: on a first launch everything before this point is a
 * splash over the server's start, and an update dialog on top of that — or
 * an update download competing with it for the same connection — is the
 * worst possible moment for both.
 */
function beginUpdateChecks(): void {
  updater?.checkOnStartup();
}

// ---------------------------------------------------------------------------
// quitting
// ---------------------------------------------------------------------------

function quitApp(): void {
  app.quit();
}

function onBeforeQuit(event: Electron.Event): void {
  // The second `app.quit()` — the one quitAndInstall issues from inside
  // the install handoff — must be allowed through untouched. Cancelling
  // it would leave the update staged and the app open, which reads as
  // "the update silently did nothing". Hence: guard first, always.
  if (quitting) return;
  quitting = true;

  // Every quit goes through the async path: the update install happens at
  // the end of that sequence, and it has to happen on Windows too, where
  // there is no local server to stop.
  event.preventDefault();
  void shutdownAndExit();
}

async function shutdownAndExit(): Promise<void> {
  updater?.noteShuttingDown();
  registry?.dispose();
  await runQuitSequence({
    // A quit before whenReady (the second-instance path) has no RunLog
    // yet, and the sequence must still run.
    log: log ?? new MemoryLog(),
    stopServer: stopServerForQuit,
    releaseServer: () => controller?.release(),
    // Only a running server can be kept; one still starting is stopped.
    keepServer: () => (keepServerOnce || settings.keepLocalRunning) && controller?.state.name === 'ready',
    installPending: () => updater?.installPending ?? false,
    closeLog: () => log?.close(),
    installIfPending: () => updater?.installIfPending() ?? false,
    exit: (code) => app.exit(code),
    setTimeoutImpl: (fn, ms) => {
      setTimeout(fn, ms);
    },
  });
}

/**
 * Ask the local server to stop, and show that we're doing it.
 *
 * Teardown can take tens of seconds — the server stops every generator
 * and Docker sandbox on its way out — and an app that vanishes from the
 * screen while still holding the port looks broken and gets force-quit,
 * which is exactly what orphans containers.
 */
async function stopServerForQuit(): Promise<void> {
  await stopCurrentServer('Stopping the Collimate server…', 'finishing running work');

  // The install runs next, and it can take a while with no window on
  // screen (Squirrel unpacking a zip, NSIS asking for elevation). Say so,
  // in the one surface still alive at this point.
  if (updater?.installPending) {
    splashInstance().status('Installing the update…', 'Collimate will restart when it finishes.');
  }
}

function showRendererFailure(message: string): void {
  log.shell(message);
  if (quitting) return;
  retryServerId = windowTarget?.info.id ?? null;
  // Create the failure surface before destroying the dead workbench, so
  // window-all-closed cannot turn a recoverable renderer failure into a quit.
  splashInstance().setView({
    kind: 'failure', title: 'The workbench renderer stopped', message, tail: log.tail(),
  });
  const window = liveMainWindow();
  mainWindow = null;
  window?.destroy();
}

function fatal(title: string, error: unknown): void {
  const message = error instanceof Error ? (error.stack ?? error.message) : errorMessage(error);
  log?.shell(`${title}: ${message}`);
  dialog.showErrorBox(title, message);
  app.exit(1);
}

main();
