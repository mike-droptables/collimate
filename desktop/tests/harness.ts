import { EventEmitter } from 'node:events';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import type { ChildProcess, SpawnOptions } from 'node:child_process';
import { BRIDGE_PROTOCOL } from '../src/shared/bridge.js';
import { MemoryLog, type TailOptions } from '../src/main/log.js';
import type {
  FetchLike,
  FetchResponseLike,
  ServerControllerDeps,
  ServerControllerOptions,
} from '../src/main/server.js';
import type { AutoUpdaterEvent, AutoUpdaterLike } from '../src/main/updater.js';
import type { UvResolution } from '../src/main/uvProvision.js';

/**
 * Fakes for the ServerController's injection points.
 *
 * The controller takes every effect it has on the world through `deps`, so
 * this file is the whole test rig: one fake child process, one fake HTTP
 * server, one in-memory disk, one fake process log file (the child's
 * output lands in it, the controller follows it), and vitest's fake timers
 * as the single clock.
 * Nothing here spawns, connects, or sleeps for real — a test that "waits
 * 90 seconds" runs in a millisecond.
 *
 * FakeAutoUpdater at the bottom does the same job for UpdateController.
 */

/** Enough of a ChildProcess for the controller. Its output goes to the process log. */
export class FakeChild extends EventEmitter {
  exited = false;
  unrefs = 0;

  constructor(
    readonly pid: number | undefined,
    private readonly write: (line: string) => void,
  ) {
    super();
  }

  /** A line on the child's stdout: appended to the process log file. */
  say(line: string): void {
    this.write(line);
  }

  /** A line on stderr — the same file. */
  complain(line: string): void {
    this.write(line);
  }

  unref(): void {
    this.unrefs += 1;
  }

  exit(code: number | null, signal: NodeJS.Signals | null = null): void {
    if (this.exited) return;
    this.exited = true;
    this.emit('exit', code, signal);
  }

  asChildProcess(): ChildProcess {
    return this as unknown as ChildProcess;
  }
}

export interface SpawnCall {
  command: string;
  args: string[];
  options: SpawnOptions;
  child: FakeChild;
}

export interface KillCall {
  pid: number;
  signal: NodeJS.Signals;
}

/** How a route behaves: answers, refuses the connection, or never replies. */
export type RouteBehaviour = 'ok' | 'down' | 'hang' | 'invalid-json';

export const DEFAULT_PORT = 53219;
export const DEFAULT_PID = 4711;
/** The descriptor the fake process log hands out. */
export const PROCESS_LOG_FD = 99;
/** The /dev/null descriptor the fake hands out. */
export const NULL_FD = 98;
export const DEFAULT_UV: UvResolution = { uv: '/fake/bin/uv', source: 'path', version: 'uv 0.8.17' };

export interface HarnessOptions {
  env?: NodeJS.ProcessEnv;
  isPackaged?: boolean;
  appPath?: string;
  appVersion?: string;
  /** Overridden per-test for the repo-root walk; defaults to a tmp dir. */
  dataDir?: string;
}

export class Harness {
  readonly log = new MemoryLog();
  readonly tmpRoot: string;
  readonly options: ServerControllerOptions;

  // --- observed effects ---
  readonly spawns: SpawnCall[] = [];
  readonly kills: KillCall[] = [];
  readonly fetches: { url: string; method: string }[] = [];
  readonly files = new Map<string, string>();
  readonly removed: string[] = [];
  /** Ports the controller asked allocatePortImpl for first. */
  readonly preferredPorts: (number | undefined)[] = [];
  /** Process-log opens and closes, in order (`open` / `close:<fd>`). */
  readonly processLogCalls: string[] = [];
  /** Every follow of the process log, live or stopped. */
  readonly tails: { options: TailOptions; onLine: (line: string) => void; stopped: boolean }[] = [];
  /** Lines written to the process log file (stdout and stderr alike). */
  readonly processLines: string[] = [];

  // --- knobs ---
  /** Thrown by spawnImpl when set (ENOENT and friends). */
  spawnThrows: Error | null = null;
  /** Hand back a child with no pid, the "never started" case. */
  spawnPid: number | undefined = DEFAULT_PID;
  uv: UvResolution | Error = DEFAULT_UV;
  port = DEFAULT_PORT;
  health: RouteBehaviour = 'ok';
  ready: RouteBehaviour = 'ok';
  healthBody: unknown = { status: 'ok', version: '0.1.0', bridge: BRIDGE_PROTOCOL };
  /** GET /api/system/instance; `data_dir` defaults to this harness's data dir. */
  instanceBody: unknown = null;
  /** GET /api/system/instance answers or refuses. */
  instance: RouteBehaviour = 'ok';
  /** What gitHeadImpl reports for a checkout. */
  gitHead: string | null = 'abc123';
  /** Pids (not our child) that die when a shutdown POST is accepted. */
  readonly diesOnShutdown = new Set<number>();
  /** /ready answers 503 until this many ms after the harness was built. */
  readyAfterMs: number | null = null;
  /** POST /api/system/shutdown: rejects when true. */
  shutdownFails = false;
  /** How long after an accepted shutdown the child exits. null = never. */
  shutdownExitDelayMs: number | null = 0;
  /** Pids process.kill(pid, 0) reports as alive. */
  readonly alive = new Set<number>();
  /** Called for every real signal, so a test can make a pid die. */
  onSignal: ((pid: number, signal: NodeJS.Signals) => void) | null = null;
  /** When the fake machine booted: an hour before the harness was built. */
  bootTimeMs = Date.now() - 3_600_000;
  /**
   * A pid's command line (processArgsImpl). Defaults to a server of this
   * profile for every live pid: a recorded pid that is ours.
   */
  processArgs: (pid: number) => string | null = (pid) =>
    this.alive.has(pid) ? `uv run main.py --port 5001 --data-dir ${this.options.dataDir}` : null;

  /** Health/ready requests that were left hanging, and how many aborted. */
  hangs = 0;
  aborts = 0;

  private readonly startedAt = Date.now();

  constructor(harnessOptions: HarnessOptions = {}) {
    this.tmpRoot = mkdtempSync(path.join(os.tmpdir(), 'collimate-shell-test-'));
    this.options = {
      dataDir: harnessOptions.dataDir ?? path.join(this.tmpRoot, 'data'),
      runtimeDir: path.join(this.tmpRoot, 'runtime'),
      appVersion: harnessOptions.appVersion ?? '0.1.0',
      isPackaged: harnessOptions.isPackaged ?? false,
      appPath: harnessOptions.appPath ?? path.join(this.tmpRoot, 'app'),
      env: harnessOptions.env ?? {},
      log: this.log,
      deps: this.deps(),
    };
  }

  get recordPath(): string {
    return path.join(this.options.runtimeDir, 'server.json');
  }

  get url(): string {
    return `http://127.0.0.1:${this.port}`;
  }

  /** The child of the most recent spawn. */
  get child(): FakeChild {
    const last = this.spawns.at(-1);
    if (!last) throw new Error('nothing has been spawned');
    return last.child;
  }

  /** The parsed server.json the controller wrote, or null. */
  record(): Record<string, unknown> | null {
    const raw = this.files.get(this.recordPath);
    return raw === undefined ? null : (JSON.parse(raw) as Record<string, unknown>);
  }

  /** Seed a server.json as if a previous run had left one behind. */
  seedRecord(contents: unknown): void {
    this.files.set(this.recordPath, typeof contents === 'string' ? contents : JSON.stringify(contents));
  }

  cleanup(): void {
    rmSync(this.tmpRoot, { recursive: true, force: true });
  }

  private deps(): Partial<ServerControllerDeps> {
    return {
      spawnImpl: (command, args, options) => {
        if (this.spawnThrows) throw this.spawnThrows;
        const child = new FakeChild(this.spawnPid, (line) => this.writeProcessLine(line));
        this.spawns.push({ command, args, options, child });
        return child.asChildProcess();
      },
      fetchImpl: this.fetchImpl,
      now: () => Date.now(),
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      killImpl: (pid, signal) => {
        if (signal === 0) {
          // Number(): process.kill takes a numeric string as that pid, so a
          // record whose pid is '9182' reaches a live 9182 in the field too.
          if (!this.alive.has(Number(pid))) throw new Error(`ESRCH: no such process ${pid}`);
          return;
        }
        this.kills.push({ pid, signal });
        this.onSignal?.(pid, signal);
      },
      allocatePortImpl: (preferred) => {
        this.preferredPorts.push(preferred);
        return Promise.resolve(this.port);
      },
      processLog: {
        open: () => {
          this.processLogCalls.push('open');
          return PROCESS_LOG_FD;
        },
        close: (fd) => {
          this.processLogCalls.push(`close:${fd}`);
        },
        tail: (onLine, options) => {
          const tail = { options, onLine, stopped: false };
          this.tails.push(tail);
          return () => {
            tail.stopped = true;
          };
        },
      },
      gitHeadImpl: () => this.gitHead,
      openFdsImpl: () => [0, 1, 2, 5],
      openNullImpl: () => NULL_FD,
      ensureUvImpl: (onProgress) => {
        onProgress('Looking for uv…');
        if (this.uv instanceof Error) return Promise.reject(this.uv);
        return Promise.resolve(this.uv);
      },
      bootTimeMs: () => this.bootTimeMs,
      processArgsImpl: (pid) => this.processArgs(pid),
      fs: {
        readFileSync: (file) => this.files.get(file) ?? null,
        writeFileSync: (file, contents) => {
          this.files.set(file, contents);
        },
        rmSync: (file) => {
          this.removed.push(file);
          this.files.delete(file);
        },
      },
    };
  }

  /** The child wrote a line: into the file, and out to every live follower. */
  private writeProcessLine(line: string): void {
    this.processLines.push(line);
    for (const tail of this.tails) if (!tail.stopped) tail.onLine(line);
  }

  /** Every line still followed, i.e. what the controller's log receives. */
  get liveTails(): number {
    return this.tails.filter((tail) => !tail.stopped).length;
  }

  private readonly fetchImpl: FetchLike = (url, init) => {
    this.fetches.push({ url, method: init?.method ?? 'GET' });

    if (url.endsWith('/api/system/shutdown')) {
      if (this.shutdownFails) return Promise.reject(new Error('ECONNREFUSED'));
      if (this.shutdownExitDelayMs !== null) {
        const child = this.spawns.at(-1)?.child;
        setTimeout(() => {
          child?.exit(0, null);
          for (const pid of this.diesOnShutdown) this.alive.delete(pid);
        }, this.shutdownExitDelayMs);
      }
      return Promise.resolve(jsonResponse(200, { stopping: true }));
    }

    if (url.endsWith('/api/system/instance')) {
      if (this.instance === 'down') return Promise.reject(new Error('ECONNREFUSED'));
      return Promise.resolve(
        jsonResponse(
          200,
          this.instanceBody ?? {
            pid: 31337,
            data_dir: this.options.dataDir,
            instance: 'feedc0ffee12',
            version: '0.1.0',
            bridge: BRIDGE_PROTOCOL,
          },
        ),
      );
    }

    const isHealth = url.endsWith('/health');
    const behaviour = isHealth ? this.health : this.ready;
    if (behaviour === 'hang') return this.hang(init?.signal);
    if (behaviour === 'down') return Promise.reject(new Error('ECONNREFUSED'));
    if (
      !isHealth &&
      this.readyAfterMs !== null &&
      Date.now() - this.startedAt < this.readyAfterMs
    ) {
      return Promise.resolve(jsonResponse(503, { status: 'starting' }));
    }
    if (behaviour === 'invalid-json') {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.reject(new Error('not JSON')),
      });
    }
    return Promise.resolve(jsonResponse(200, isHealth ? this.healthBody : { status: 'ready' }));
  };

  /** A socket that accepted the connection and then said nothing. */
  private hang(signal: AbortSignal | undefined): Promise<FetchResponseLike> {
    this.hangs++;
    return new Promise<FetchResponseLike>((_resolve, reject) => {
      if (!signal) return;
      signal.addEventListener('abort', () => {
        this.aborts++;
        reject(new Error('The operation was aborted.'));
      });
    });
  }
}

function jsonResponse(status: number, body: unknown): FetchResponseLike {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
}

/** The command line the controller logged for a spawn call. */
export function commandLine(call: SpawnCall): string {
  return [call.command, ...call.args].join(' ');
}

// ---------------------------------------------------------------------------
// electron-updater
// ---------------------------------------------------------------------------

export interface UpdaterAssignment {
  key: 'autoDownload' | 'autoInstallOnAppQuit' | 'allowDowngrade' | 'logger';
  value: unknown;
}

/**
 * Stand-in for electron-updater's `autoUpdater`.
 *
 * Two things it does that the real one won't: it records every property
 * the controller *assigned* (not just the value that ended up there, so
 * "we pinned this" is distinguishable from "the default happened to
 * agree"), and it leaves `checkForUpdates()` pending until the test says
 * otherwise — which is what makes "second click while a check is in
 * flight" and "an error arrived during a menu check" expressible at all.
 *
 * The field defaults below are electron-updater 6's real defaults.
 */
export class FakeAutoUpdater implements AutoUpdaterLike {
  readonly assignments: UpdaterAssignment[] = [];
  readonly feedUrls: { provider: string; url: string }[] = [];
  readonly quitAndInstallCalls: {
    isSilent?: boolean | undefined;
    isForceRunAfter?: boolean | undefined;
  }[] = [];
  /** How many times checkForUpdates has been called. */
  checks = 0;
  /** Thrown by quitAndInstall when set (a Squirrel helper that won't launch). */
  quitAndInstallThrows: Error | null = null;

  private readonly handlers = new Map<AutoUpdaterEvent, Set<(payload: unknown) => void>>();
  private readonly outstanding: { resolve: (value: unknown) => void; reject: (error: unknown) => void }[] = [];

  private autoDownloadValue = true;
  private autoInstallOnAppQuitValue = true;
  private allowDowngradeValue = false;
  private loggerValue: unknown = null;

  get autoDownload(): boolean {
    return this.autoDownloadValue;
  }
  set autoDownload(value: boolean) {
    this.autoDownloadValue = value;
    this.assignments.push({ key: 'autoDownload', value });
  }

  get autoInstallOnAppQuit(): boolean {
    return this.autoInstallOnAppQuitValue;
  }
  set autoInstallOnAppQuit(value: boolean) {
    this.autoInstallOnAppQuitValue = value;
    this.assignments.push({ key: 'autoInstallOnAppQuit', value });
  }

  get allowDowngrade(): boolean {
    return this.allowDowngradeValue;
  }
  set allowDowngrade(value: boolean) {
    this.allowDowngradeValue = value;
    this.assignments.push({ key: 'allowDowngrade', value });
  }

  get logger(): unknown {
    return this.loggerValue;
  }
  set logger(value: unknown) {
    this.loggerValue = value;
    this.assignments.push({ key: 'logger', value });
  }

  on(event: AutoUpdaterEvent, handler: (payload: unknown) => void): void {
    const existing = this.handlers.get(event);
    if (existing) {
      existing.add(handler);
      return;
    }
    this.handlers.set(event, new Set([handler]));
  }

  setFeedURL(options: { provider: 'generic'; url: string }): void {
    this.feedUrls.push({ ...options });
  }

  checkForUpdates(): Promise<unknown> {
    this.checks += 1;
    return new Promise<unknown>((resolve, reject) => {
      this.outstanding.push({ resolve, reject });
    });
  }

  quitAndInstall(isSilent?: boolean, isForceRunAfter?: boolean): void {
    this.quitAndInstallCalls.push({ isSilent, isForceRunAfter });
    if (this.quitAndInstallThrows) throw this.quitAndInstallThrows;
  }

  // --- test controls ---

  /** Deliver an event to the controller, exactly as electron-updater would. */
  emit(event: AutoUpdaterEvent, payload?: unknown): void {
    for (const handler of [...(this.handlers.get(event) ?? [])]) handler(payload);
  }

  /** True while a checkForUpdates() call hasn't been answered. */
  get checkInFlight(): boolean {
    return this.outstanding.length > 0;
  }

  /** Answer the oldest outstanding checkForUpdates(). */
  settleCheck(result: unknown = { updateInfo: {} }): void {
    this.outstanding.shift()?.resolve(result);
  }

  /** Fail the oldest outstanding checkForUpdates(). */
  failCheck(error: Error): void {
    this.outstanding.shift()?.reject(error);
  }
}
