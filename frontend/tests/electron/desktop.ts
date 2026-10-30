import { existsSync, chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { _electron as electron, type ElectronApplication, type Page } from "@playwright/test";

import { ShellTrace } from "./tracing";

/**
 * Paths and launch plumbing for the desktop shell, shared by the global
 * setup and the spec.
 *
 * The shell under test is the real one: `desktop/dist/main.cjs`, running
 * on the Electron binary from `desktop/node_modules`, supervising the real
 * `api/` server out of this checkout. Nothing here is stubbed — the point
 * of this suite is that the seams the unit tests fake (spawn, health,
 * shutdown, the guest-view bridge) hold when they are real.
 */

export const repoRoot = path.resolve(__dirname, "..", "..", "..");
export const desktopDir = path.join(repoRoot, "desktop");
export const mainEntry = path.join(desktopDir, "dist", "main.cjs");
export const apiProject = path.join(repoRoot, "api");

/**
 * The pinned Electron binary installed into desktop/node_modules.
 *
 * `path.txt` is written by electron's explicit installer and names the binary
 * inside `dist/` for this platform, so this works on macOS (where it is
 * inside an .app bundle) as well as Linux.
 */
export function electronBinary(): string | null {
  const root = path.join(desktopDir, "node_modules", "electron");
  const pointer = path.join(root, "path.txt");
  if (!existsSync(pointer)) return null;
  const relative = readFileSync(pointer, "utf8").trim();
  if (!relative) return null;
  const binary = path.join(root, "dist", relative);
  return existsSync(binary) ? binary : null;
}

/** Why this machine can't run the suite, or null if it can. */
export function missingPrerequisite(): string | null {
  if (!existsSync(path.join(desktopDir, "node_modules"))) {
    return "desktop/node_modules is missing — run `npm ci` in desktop/ first.";
  }
  if (electronBinary() === null) {
    return (
      "the Electron binary is not installed in desktop/node_modules/electron/dist — " +
      "run `npm ci` then `node node_modules/electron/install.js` in desktop/."
    );
  }
  if (!existsSync(mainEntry)) {
    return `${mainEntry} is missing — run \`npm run build\` in desktop/ first.`;
  }
  if (!existsSync(path.join(apiProject, "pyproject.toml"))) {
    return `${apiProject}/pyproject.toml is missing — the shell has no server to run.`;
  }
  return null;
}

export interface LaunchedShell {
  app: ElectronApplication;
  trace: ShellTrace;
  userDataRoot: string;
  runtimeDir: string;
}

export function testRoot(): string {
  const scratch = path.join(repoRoot, "tmp", "electron-e2e");
  mkdirSync(scratch, { recursive: true });
  return mkdtempSync(path.join(scratch, "run-"));
}

/** Carry tool/display plumbing, never a developer's account or login-shell state. */
export function isolatedEnvironment(root: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const name of [
    "PATH", "DISPLAY", "XAUTHORITY", "LD_LIBRARY_PATH", "LANG", "LC_ALL",
    "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "SystemRoot", "WINDIR",
  ]) {
    if (process.env[name] !== undefined) env[name] = process.env[name];
  }
  for (const [name, directory] of Object.entries({
    HOME: "home", USERPROFILE: "home", XDG_CONFIG_HOME: "config",
    XDG_CACHE_HOME: "cache", XDG_DATA_HOME: "data", XDG_STATE_HOME: "state",
    TMPDIR: "tmp", TMP: "tmp", TEMP: "tmp", APPDATA: "config", LOCALAPPDATA: "data",
  })) {
    env[name] = path.join(root, directory);
    mkdirSync(env[name]!, { recursive: true });
  }
  env.PYTHON_KEYRING_BACKEND = "keyring.backends.null.Keyring";
  if (process.platform === "linux") env.XDG_SESSION_TYPE = "x11";
  return env;
}

export interface LaunchOptions {
  connect?: { url: string; token: string };
  /**
   * Relaunch against an earlier launch's private root (its settings,
   * server.json and data dir) instead of a fresh one. The caller owns it.
   */
  userDataRoot?: string;
  prebootSentinel?: { pid: number; startedAt: number };
  /** Private fixed fixture; never inherits caller shell/rc environment. */
  shellFixture?: { delaySeconds: number };
  deviceScaleFactor?: number;
}

/** Source-paired real shell with private settings, accounts, home and cache. */
export async function launchShell(options: LaunchOptions = {}): Promise<LaunchedShell> {
  const binary = electronBinary();
  if (!binary) throw new Error(missingPrerequisite() ?? "no Electron binary");
  const scale = options.deviceScaleFactor;
  if (scale !== undefined && (!Number.isFinite(scale) || scale <= 0 || scale > 8)) throw new Error('invalid fixture device scale');
  if (options.shellFixture) {
    const delay = options.shellFixture.delaySeconds;
    if (!Number.isFinite(delay) || delay < 0 || delay > 8) throw new Error('invalid fixture shell delay');
  }
  const reused = options.userDataRoot !== undefined;
  const userDataRoot = options.userDataRoot ?? testRoot();
  let socketTempDir: string | undefined;
  let app: ElectronApplication | undefined;
  const removeSocketTemp = (): void => {
    if (socketTempDir) rmSync(socketTempDir, { recursive: true, force: true });
  };
  try {
    const env = isolatedEnvironment(userDataRoot);
    // Chromium binds SingletonSocket below TMPDIR. A checkout-relative root
    // exceeds sockaddr_un's path bound on CI; this exclusive directory is owned
    // by this launch, independent of caller TMPDIR and removed only after close.
    socketTempDir = mkdtempSync('/tmp/collimate-e2e-');
    chmodSync(socketTempDir, 0o700);
    env.TMPDIR = env.TMP = env.TEMP = socketTempDir;
    if (options.shellFixture) {
      const delay = options.shellFixture.delaySeconds;
      const script = path.join(userDataRoot, 'fixture-shell.sh');
      const quote = (value: string): string => "'" + value.replaceAll("'", "'\\''") + "'";
      writeFileSync(script, `#!/bin/sh\nprintf started > ${quote(path.join(userDataRoot, 'shell-probe-started'))}\nsleep ${delay}\nprintf completed > ${quote(path.join(userDataRoot, 'shell-probe-completed'))}\nexec /bin/sh "$@"\n`);
      chmodSync(script, 0o700);
      env.SHELL = script;
    }

    const runtimeDir = process.platform === "darwin"
      ? path.join(env.HOME!, "Library", "Application Support", "Collimate Dev")
      : path.join(env.XDG_CONFIG_HOME!, "Collimate Dev");
    mkdirSync(runtimeDir, { recursive: true });
    if (options.prebootSentinel) {
      writeFileSync(path.join(runtimeDir, "server.json"), JSON.stringify({
        ...options.prebootSentinel, port: 1, url: "http://127.0.0.1:1",
      }));
    }
    Object.assign(env, {
      COLLIMATE_SERVER_SPEC: `local:${repoRoot}`,
      COLLIMATE_APP_PROFILE: "dev",
      ELECTRON_DISABLE_SECURITY_WARNINGS: "1",
    });
    if (options.connect) {
      env.COLLIMATE_CONNECT_URL = options.connect.url;
      env.COLLIMATE_CONNECT_TOKEN = options.connect.token;
    }
    app = await electron.launch({
      executablePath: binary,
      args: [mainEntry, "--no-sandbox", ...(scale === undefined ? [] : [`--force-device-scale-factor=${scale}`])],
      cwd: desktopDir,
      env,
      timeout: 60_000,
    });
    app.once('close', removeSocketTemp);
    // Begin on the actual context before waiting for a splash or workbench.
    const trace = await ShellTrace.start(app);
    return { app, trace, userDataRoot, runtimeDir };
  } catch (error) {
    await app?.close().catch(() => undefined);
    removeSocketTemp();
    if (!reused) rmSync(userDataRoot, { recursive: true, force: true });
    throw error;
  }
}

/** A loopback URL the shell could have put a window on. */
const LOOPBACK = /^http:\/\/127\.0\.0\.1:\d+\//;

/**
 * The workbench window, once the server is up behind it.
 *
 * `firstWindow()` is the splash (a local file:// document): the shell
 * shows it while uv resolves the environment and the server boots, which
 * on a cold uv cache is most of a minute. The window this returns is the
 * one pointed at the server.
 */
export async function workbenchWindow(app: ElectronApplication, timeoutMs = 150_000, origin?: string): Promise<Page> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    for (const page of app.windows()) {
      if (LOOPBACK.test(page.url()) && (!origin || new URL(page.url()).origin === origin)) {
        await page.waitForLoadState("domcontentloaded");
        return page;
      }
    }
    if (Date.now() >= deadline) {
      const seen = app.windows().map((page) => page.url());
      throw new Error(
        `no window on the server after ${Math.round(timeoutMs / 1000)}s; windows: ${JSON.stringify(seen)}`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

/** True while something still answers `<url>/health`. */
export async function serverIsUp(url: string): Promise<boolean> {
  try {
    const response = await fetch(`${url}/health`, {
      signal: AbortSignal.timeout(2_000),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** The server.json the shell keeps in `runtimeDir`, or null. */
export function serverRecord(runtimeDir: string): Record<string, unknown> | null {
  const file = path.join(runtimeDir, "server.json");
  if (!existsSync(file)) return null;
  try {
    return JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** True while `pid` names a live process. */
export function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/**
 * Put down a server a test left running on purpose (keep-alive), so a
 * failed test cannot leak one: SIGTERM to its process group, then SIGKILL.
 */
export async function killRecordedServer(runtimeDir: string): Promise<void> {
  const pid = serverRecord(runtimeDir)?.["pid"];
  if (typeof pid !== "number" || pid <= 1 || !pidAlive(pid)) return;
  try {
    process.kill(-pid, "SIGTERM");
  } catch {
    return;
  }
  const deadline = Date.now() + 15_000;
  while (pidAlive(pid) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 200));
  if (pidAlive(pid)) {
    try {
      process.kill(-pid, "SIGKILL");
    } catch {
      // Gone between the check and the signal.
    }
  }
}
