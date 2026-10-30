import { execFileSync, spawn, type ChildProcess, type SpawnOptions } from 'node:child_process';
import { existsSync, mkdirSync, openSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import net from 'node:net';
import { uptime } from 'node:os';
import path from 'node:path';
import { BRIDGE_PROTOCOL, LOCAL_SERVER_ID, type ServerInfo } from '../shared/bridge.js';
import {
  closeProcessLog,
  MemoryLog,
  openProcessLog,
  processLogPath,
  tailFile,
  type LogSink,
  type TailOptions,
} from './log.js';
import { errorMessage } from './errors.js';
import { ensureUv, uvxInvocation, UvUnavailableError, type UvResolution } from './uvProvision.js';

/**
 * The local server's lifecycle, as a state machine.
 *
 * Everything the shell knows about "is there a local Collimate server, and
 * can I point a window at it" lives here. The class is deliberately free
 * of Electron imports: every effect it has on the world (spawn, fetch,
 * kill, clock, port allocation, uv discovery, filesystem, the process log)
 * arrives through the `deps` bag with a production default, so the whole
 * machine — including the timeout, crash and adoption paths that are
 * miserable to reproduce for real — can be driven by a test with fakes.
 *
 * A server can outlive the shell ("Quit, Keep Server Running", or the
 * keepLocalRunning setting): `release()` leaves it running with its
 * server.json, and the next launch's `resolveRecord()` adopts it when it is
 * provably the same server this shell would start, or stops it first.
 */

/** What the Servers list calls the local server. */
export const LOCAL_SERVER_NAME = 'Local';

export type FailureReason =
  /** No uv, and we couldn't install one. */
  | 'uvMissing'
  /** The spawn itself failed (ENOENT, EACCES, …). */
  | 'spawnFailed'
  /** The child is alive but never answered /health + /ready in budget. */
  | 'healthTimeout'
  /** The child exited before it ever became healthy. */
  | 'earlyExit';

export type QuitPhase = 'graceful' | 'sigterm' | 'sigkill';

export type ServerState =
  | { name: 'idle' }
  | { name: 'provisioningUv'; detail: string }
  | { name: 'spawning'; command: string }
  | { name: 'waitingHealth'; command: string; elapsedMs: number }
  | { name: 'ready'; info: ServerInfo }
  | { name: 'crashed'; code: number | null; signal: NodeJS.Signals | null; tail: string[] }
  | { name: 'quitting'; phase: QuitPhase }
  | { name: 'stopped' }
  /** Left running on purpose at quit, its server.json kept for the next launch. */
  | { name: 'released' }
  | {
      name: 'failed';
      reason: FailureReason;
      message: string;
      /** The exact command line we ran, when we got that far. */
      command: string | null;
      tail: string[];
    };

/** A startup progress line for the splash. */
export interface StartupStatus {
  message: string;
  detail?: string;
}

type ServerEvents = {
  state: [ServerState];
  status: [StartupStatus];
  crashed: [{ code: number | null; signal: NodeJS.Signals | null; tail: string[] }];
};

type Handler<A extends unknown[]> = (...args: A) => void;

/**
 * Minimal typed event emitter — node's EventEmitter without the
 * stringly-typed channels, so a typo in an event name is a build error.
 */
export class Emitter<E extends Record<string, unknown[]>> {
  private readonly handlers: { [K in keyof E]?: Set<Handler<E[K]>> } = {};

  /** Returns an unsubscribe function. */
  on<K extends keyof E>(event: K, handler: Handler<E[K]>): () => void {
    const existing = this.handlers[event];
    const set = existing ?? new Set<Handler<E[K]>>();
    if (!existing) this.handlers[event] = set;
    set.add(handler);
    return () => this.off(event, handler);
  }

  off<K extends keyof E>(event: K, handler: Handler<E[K]>): void {
    this.handlers[event]?.delete(handler);
  }

  emit<K extends keyof E>(event: K, ...args: E[K]): void {
    const set = this.handlers[event];
    if (!set) return;
    for (const handler of [...set]) {
      try {
        handler(...args);
      } catch {
        // A listener throwing (a window that just closed) must not derail
        // a shutdown or a health poll.
      }
    }
  }
}

// --- injectable effects ----------------------------------------------------

export type SpawnLike = (command: string, args: string[], options: SpawnOptions) => ChildProcess;

export interface FetchResponseLike {
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
}

export type FetchLike = (
  url: string,
  init?: { method?: string; signal?: AbortSignal; headers?: Record<string, string> },
) => Promise<FetchResponseLike>;

export interface ServerFs {
  readFileSync(file: string): string | null;
  writeFileSync(file: string, contents: string): void;
  rmSync(file: string): void;
}

/**
 * The server process's output file (`<runtimeDir>/logs/server-process.log`):
 * the child's stdout and stderr go there, never through pipes, and the
 * shell follows it into the run log.
 */
export interface ProcessLog {
  /** Rotate and open the file for a new spawn; null when it can't be opened. */
  open(): number | null;
  /** Close the shell's copy of the descriptor once the child has its own. */
  close(fd: number): void;
  /** Follow the file into `onLine`; returns a stop that flushes what is left. */
  tail(onLine: (line: string) => void, options: TailOptions): () => void;
}

export interface ServerControllerDeps {
  spawnImpl: SpawnLike;
  fetchImpl: FetchLike;
  now: () => number;
  sleep: (ms: number) => Promise<void>;
  /** `signal === 0` is a liveness check, exactly like process.kill. */
  killImpl: (pid: number, signal: NodeJS.Signals | 0) => void;
  /** A free loopback port: `preferred` when it can be bound, else an ephemeral one. */
  allocatePortImpl: (preferred?: number) => Promise<number>;
  ensureUvImpl: (onProgress: (detail: string) => void) => Promise<UvResolution>;
  fs: ServerFs;
  processLog: ProcessLog;
  /** A checkout's git HEAD, or null outside a repository. */
  gitHeadImpl: (dir: string) => string | null;
  /** The descriptors this process has open (a child would inherit those without close-on-exec). */
  openFdsImpl: () => number[];
  /** A read-only descriptor on /dev/null, or null when it can't be opened. */
  openNullImpl: () => number | null;
  /** When this machine booted, wall-clock ms since the epoch (see resolveRecord). */
  bootTimeMs: () => number;
  /**
   * A live pid's command line, its arguments joined by spaces, or null when
   * it can't be read: how resolveRecord tells our silent server from a
   * recycled pid before signalling it.
   */
  processArgsImpl: (pid: number) => string | null;
}

export interface ServerControllerOptions {
  /** Passed to the server as --data-dir; the server chdirs into it. */
  dataDir: string;
  /** Where the shell keeps its own files (server.json, bin/, logs/). */
  runtimeDir: string;
  /**
   * desktop/package.json version — pins the PyPI release we run, and is the
   * `shellVersion` a kept-alive server's record must match to be adopted.
   */
  appVersion: string;
  isPackaged: boolean;
  /** app.getAppPath(); the walk-up to the repo root starts here. */
  appPath: string;
  /**
   * settings.json's `serverSpec`, if any: the persisted twin of
   * `COLLIMATE_SERVER_SPEC` (same grammar, see buildSpawnPlan). The
   * environment variable wins when both are set — a spec typed in front
   * of one launch has to beat one that was written to disk months ago.
   *
   * Read once, at construction: the controller is built once per process
   * (index.ts, ensureController), so changing this mid-session would only
   * take effect on the next launch anyway.
   */
  serverSpec?: string;
  /**
   * settings.json's `localPort`: the port the last local server had, tried
   * first so the local origin (and the page's per-origin storage) survives
   * a relaunch and a restart. Taken means an ephemeral port instead. Read
   * at every start, so a restart prefers the port the last start ended up
   * with rather than the one the shell launched with.
   */
  preferredPort?: () => number | undefined;
  env?: NodeJS.ProcessEnv;
  log?: LogSink;
  deps?: Partial<ServerControllerDeps>;
}

/** How long a first run gets: `uvx` may download an entire Python. */
export const HEALTH_BUDGET_MS = 90_000;
export const HEALTH_POLL_MS = 250;
/** After SIGTERM to the process group, before SIGKILL. */
export const SIGTERM_GRACE_MS = 5_000;
/** After SIGKILL, how long a pid gets to disappear before we stop waiting. */
export const SIGKILL_WAIT_MS = 5_000;
/**
 * How far a server.json's `startedAt` may sit before the computed boot time,
 * or after now, and still count as this boot's: the boot time is derived
 * from uptime and the wall clock, which can each be off by a little.
 */
export const BOOT_SLACK_MS = 60_000;
/** An adopted server's pid is checked this often… */
export const LIVENESS_POLL_MS = 2_000;
/** …and its /health every this many pid checks. */
export const LIVENESS_HEALTH_EVERY = 5;
/** Consecutive failed /health checks before an adopted server counts as gone. */
export const LIVENESS_HEALTH_FAILURES = 3;

/** When this machine booted, wall-clock ms since the epoch (os.uptime is in seconds). */
export function machineBootTimeMs(): number {
  return Date.now() - uptime() * 1_000;
}
const DEFAULT_SHUTDOWN_TIMEOUT_MS = 30_000;
/** Ceiling on a single /health or /ready request. */
const PROBE_TIMEOUT_MS = 5_000;
/** Ceiling on the POST that asks the server to shut itself down. */
const SHUTDOWN_REQUEST_TIMEOUT_MS = 10_000;

interface SpawnPlan {
  command: string;
  args: string[];
  cwd: string;
  /** Human-readable command line, verbatim, for logs and failure screens. */
  display: string;
  /** The checkout a `uv run` plan serves, whose git HEAD is part of its spec. */
  checkout?: string;
}

/**
 * `<runtimeDir>/server.json`: the server this shell spawned, written at the
 * spawn and again once it is ready, removed when it stops or crashes, and
 * kept when it is released to outlive the shell.
 */
export interface ServerRecord {
  /** The uv process-group leader (python runs as its child). */
  pid: number;
  port: number;
  url: string;
  /** Wall-clock ms; compared against the machine's boot time. */
  startedAt: number;
  /** `version` from the server's /health; null until it answered. */
  version: string | null;
  /** The shell that spawned it. */
  shellVersion: string;
  /** The spawn plan's command line, plus a checkout's git HEAD. */
  spec: string;
  dataDir: string;
}

export class ServerStartError extends Error {
  constructor(readonly state: Extract<ServerState, { name: 'failed' }>) {
    super(state.message);
    this.name = 'ServerStartError';
  }
}

export class ServerController {
  readonly events = new Emitter<ServerEvents>();
  private readonly deps: ServerControllerDeps;
  private readonly env: NodeJS.ProcessEnv;
  private readonly log: LogSink;
  private current: ServerState = { name: 'idle' };
  private child: ChildProcess | null = null;
  private childExit: { code: number | null; signal: NodeJS.Signals | null } | null = null;
  private exitWaiters = new Set<() => void>();
  /** A server from an earlier shell, running without a ChildProcess of ours. */
  private adopted: { pid: number; url: string } | null = null;
  /** Bumped to end the adopted server's liveness watch. */
  private watchGeneration = 0;
  private stopTail: (() => void) | null = null;
  private record: ServerRecord | null = null;
  private serverInfo: ServerInfo | null = null;
  private starting: Promise<ServerInfo> | null = null;

  constructor(private readonly options: ServerControllerOptions) {
    this.env = options.env ?? process.env;
    this.log = options.log ?? new MemoryLog();
    const processLogFile = processLogPath(options.runtimeDir);
    this.deps = {
      spawnImpl: spawn as SpawnLike,
      fetchImpl: ((url, init) => fetch(url, init)) as FetchLike,
      now: () => Date.now(),
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      killImpl: (pid, signal) => {
        process.kill(pid, signal === 0 ? 0 : signal);
      },
      allocatePortImpl: allocatePort,
      ensureUvImpl: (onProgress) =>
        ensureUv({ binDir: path.join(options.runtimeDir, 'bin'), env: this.env, onProgress }),
      fs: nodeServerFs,
      processLog: {
        open: () => {
          try {
            return openProcessLog(processLogFile);
          } catch (error) {
            this.log.shell(`couldn't open ${processLogFile}: ${errorMessage(error)}`);
            return null;
          }
        },
        close: closeProcessLog,
        tail: (onLine, tailOptions) => tailFile(processLogFile, onLine, tailOptions),
      },
      gitHeadImpl: gitHead,
      openFdsImpl: openFileDescriptors,
      openNullImpl: () => {
        try {
          return openSync('/dev/null', 'r');
        } catch {
          return null;
        }
      },
      bootTimeMs: machineBootTimeMs,
      processArgsImpl: processArgs,
      ...options.deps,
    };
  }

  get state(): ServerState {
    return this.current;
  }

  get info(): ServerInfo | null {
    return this.serverInfo;
  }

  /** True when the running server came from an earlier shell. */
  get isAdopted(): boolean {
    return this.adopted !== null && this.serverInfo !== null;
  }

  /**
   * Bring a server up — or adopt the one an earlier shell left running —
   * and return once it answers /health and /ready. Concurrent calls share
   * one attempt. Rejects with ServerStartError, whose `state` carries the
   * reason, the command, and the log tail.
   */
  start(): Promise<ServerInfo> {
    if (this.current.name === 'ready' && this.serverInfo) {
      return Promise.resolve(this.serverInfo);
    }
    if (this.starting) return this.starting;
    const attempt = this.runStart().finally(() => {
      this.starting = null;
    });
    this.starting = attempt;
    return attempt;
  }

  /** Stop the current server (if it's ours) and start a fresh one. */
  async restart(): Promise<ServerInfo> {
    await this.stop();
    this.setState({ name: 'idle' });
    return this.start();
  }

  private async runStart(): Promise<ServerInfo> {
    // A child left from a failed attempt (a healthTimeout leaves it
    // running), or a wedged adopted server, is ours: stop it first, or
    // resolveRecord would reap it and its exit would fail this attempt as
    // an early exit.
    if (this.child || this.adopted) await this.stop();
    this.childExit = null;
    this.serverInfo = null;

    // 1. uv — first, because the spawn plan it shapes is part of the
    // identity a kept-alive server must match to be adopted.
    let uv: UvResolution;
    this.setState({ name: 'provisioningUv', detail: 'Looking for uv…' });
    try {
      uv = await this.deps.ensureUvImpl((detail) => {
        this.setState({ name: 'provisioningUv', detail });
        this.emitStatus('Preparing the Python environment', detail);
        this.log.shell(detail);
      });
    } catch (error) {
      const attempts = error instanceof UvUnavailableError ? error.attempts : [];
      for (const attempt of attempts) this.log.shell(`looked for uv at: ${attempt}`);
      return this.fail('uvMissing', errorMessage(error), null);
    }
    this.log.shell(`using uv: ${uv.uv} (${uv.source}) — ${uv.version}`);

    // 2. A server an earlier shell recorded: adopt it when it is provably
    // the one we would start, otherwise stop it and wait until it is gone
    // (it holds the data dir's lock) before we bind a new one.
    const adopted = await this.resolveRecord((port) => this.specOf(this.buildSpawnPlan(uv, port)));
    if (adopted) return adopted;

    // 3. Port: the last local port first, so the origin stays the same;
    // otherwise ephemeral, so two Collimates, or an unrelated process on
    // 5001, can never collide.
    const preferred = this.options.preferredPort?.();
    const port = await this.deps.allocatePortImpl(preferred);
    if (preferred !== undefined && port !== preferred) {
      this.log.shell(`port ${preferred} is taken; using ${port} (the window's origin changes)`);
    }
    const url = `http://127.0.0.1:${port}`;

    // 4. Spawn, its output to a file (see ProcessLog) rather than pipes.
    const plan = this.buildSpawnPlan(uv, port);
    this.setState({ name: 'spawning', command: plan.display });
    this.emitStatus('Starting the Collimate server…');
    this.log.shell(`cwd: ${plan.cwd}`);
    this.log.shell(`exec: ${plan.display}`);

    const outputFd = this.deps.processLog.open();
    const nullFd = this.deps.openNullImpl();
    let child: ChildProcess;
    try {
      child = this.deps.spawnImpl(plan.command, plan.args, {
        cwd: plan.cwd,
        env: { ...this.env, COLLIMATE_CLIENT: 'electron' },
        stdio: serverStdio(outputFd, this.deps.openFdsImpl(), nullFd),
        // POSIX: put the child in its own process group (and session) so a
        // later kill(-pid) reaches uv AND the uvicorn grandchild it execs,
        // and so a released server is not hung up with the shell.
        detached: true,
        windowsHide: true,
      });
    } catch (error) {
      return this.fail('spawnFailed', `Couldn't run ${plan.display}: ${errorMessage(error)}`, plan.display);
    } finally {
      // The child holds its own duplicates; the shell reads the file by path.
      if (outputFd !== null) this.deps.processLog.close(outputFd);
      if (nullFd !== null) this.deps.processLog.close(nullFd);
    }
    if (!child.pid) {
      return this.fail('spawnFailed', `Couldn't run ${plan.display}: the process never started.`, plan.display);
    }

    this.child = child;
    this.stopTail = this.deps.processLog.tail((line) => this.log.server(line), { fromEnd: false });
    child.on('error', (error) => {
      this.log.shell(`child error: ${errorMessage(error)}`);
    });
    child.on('exit', (code, signal) => {
      if (this.child !== child) {
        this.log.shell(`ignoring exit of a replaced or released server process (${describeExit({ code, signal })})`);
        return;
      }
      this.onChildExit(code, signal);
    });
    this.writeRecord({
      pid: child.pid,
      port,
      url,
      startedAt: this.deps.now(),
      version: null,
      shellVersion: this.options.appVersion,
      spec: this.specOf(plan),
      dataDir: this.options.dataDir,
    });

    // 5. Health.
    return this.awaitHealthy(plan, port, url);
  }

  private async awaitHealthy(plan: SpawnPlan, port: number, url: string): Promise<ServerInfo> {
    const deadline = this.deps.now() + HEALTH_BUDGET_MS;
    const startedAt = this.deps.now();
    let lastStatusAt = 0;
    let serverVersion: string | null = null;
    let healthOk = false;

    const tick = (phase: string): void => {
      const elapsedMs = this.deps.now() - startedAt;
      this.setState({ name: 'waitingHealth', command: plan.display, elapsedMs });
      // Once a second is enough for a spinner; every 250ms would just
      // spam IPC at the splash.
      if (elapsedMs - lastStatusAt >= 1_000) {
        lastStatusAt = elapsedMs;
        this.emitStatus(phase, `${Math.round(elapsedMs / 1000)}s`);
      }
    };

    this.emitStatus('Waiting for the server…');

    while (this.deps.now() < deadline) {
      if (this.childExit) {
        return this.fail(
          'earlyExit',
          `The server exited before it was ready (${describeExit(this.childExit)}).`,
          plan.display,
        );
      }
      tick('Waiting for the server…');
      const health = await this.probe(`${url}/health`);
      if (health.ok) {
        const body = health.body;
        if (isRecord(body)) {
          const bridge = body['bridge'];
          if (typeof bridge === 'number' && bridge !== BRIDGE_PROTOCOL) {
            // Not fatal here: the shell still shows the UI, and a bridge
            // mismatch only matters for the capabilities the frontend
            // gates on. Loud in the log so a version skew is diagnosable.
            this.log.shell(
              `WARNING: server speaks bridge ${bridge}, this shell implements ${BRIDGE_PROTOCOL}. ` +
                'Some desktop features may be unavailable.',
            );
          }
          const version = body['version'];
          if (typeof version === 'string') serverVersion = version;
        }
        healthOk = true;
        break;
      }
      await this.deps.sleep(HEALTH_POLL_MS);
    }

    if (!healthOk) {
      return this.fail(
        'healthTimeout',
        `The server didn't answer ${url}/health within ${Math.round(HEALTH_BUDGET_MS / 1000)} seconds.`,
        plan.display,
      );
    }

    this.log.shell(`/health ok — server version ${serverVersion ?? 'unknown'}`);
    this.emitStatus('Opening the workbench…');

    // /ready is the real gate: DB reachable, blob dir writable. It can
    // 503 for a while after /health starts answering (first-launch
    // bootstrap), so it gets whatever remains of the same budget.
    let ready = false;
    while (this.deps.now() < deadline) {
      if (this.childExit) {
        return this.fail(
          'earlyExit',
          `The server exited during startup (${describeExit(this.childExit)}).`,
          plan.display,
        );
      }
      tick('Opening the workbench…');
      const probe = await this.probe(`${url}/ready`);
      if (probe.ok) {
        ready = true;
        break;
      }
      await this.deps.sleep(HEALTH_POLL_MS);
    }

    if (!ready) {
      return this.fail(
        'healthTimeout',
        `The server started but never became ready (${url}/ready kept failing).`,
        plan.display,
      );
    }

    // The version is part of what a later launch checks before adopting.
    if (this.record) this.writeRecord({ ...this.record, version: serverVersion });
    const info = localInfo(url, port, this.options.dataDir, serverVersion);
    this.serverInfo = info;
    this.setState({ name: 'ready', info });
    this.log.shell(`server ready at ${url}`);
    return info;
  }

  /**
   * Shut the server down: ask nicely over HTTP, then escalate.
   *
   * The polite path matters — POST /api/system/shutdown runs the server's
   * own teardown, which stops every generator subprocess and Docker
   * sandbox with its own grace window. Killing the process group first
   * would leave those containers running, since they live in their own
   * sessions and our signal never reaches them.
   */
  async stop(): Promise<void> {
    // A released server is meant to outlive us; nothing here may touch it.
    if (this.current.name === 'released') return;
    if (this.adopted) {
      await this.stopAdopted(this.adopted);
      return;
    }
    const child = this.child;
    if (!child || this.childExit) {
      this.child = null;
      this.endTail();
      this.clearRecord();
      this.setState({ name: 'stopped' });
      return;
    }

    const pid = child.pid;
    this.setState({ name: 'quitting', phase: 'graceful' });
    this.emitStatus('Stopping the Collimate server…', 'finishing running work');

    // Only a server that reached `ready` has a shutdown endpoint to ask.
    // Quitting mid-startup goes straight to signals rather than sitting
    // out the full graceful window for a request we never made.
    const asked = this.serverInfo ? await this.requestShutdown(this.serverInfo.url) : false;

    if (asked && (await this.waitForExit(this.shutdownTimeoutMs()))) {
      this.finishStop();
      return;
    }
    if (!asked && this.childExit) {
      this.finishStop();
      return;
    }

    if (pid) {
      this.setState({ name: 'quitting', phase: 'sigterm' });
      this.log.shell(`server still running after graceful timeout; SIGTERM to process group ${pid}`);
      this.signalGroup(pid, 'SIGTERM');
      if (await this.waitForExit(SIGTERM_GRACE_MS)) {
        this.finishStop();
        return;
      }
      this.setState({ name: 'quitting', phase: 'sigkill' });
      this.log.shell(`SIGKILL to process group ${pid}`);
      this.signalGroup(pid, 'SIGKILL');
      await this.waitForExit(2_000);
    }

    this.finishStop();
  }

  /**
   * Leave the running server up for the next launch to adopt: stop
   * following it (its output keeps going to the process log file), keep
   * server.json, and forget it. A server that is not running is left as
   * it is — there is nothing to keep.
   */
  release(): void {
    if (this.current.name !== 'ready') return;
    const pid = this.adopted?.pid ?? this.child?.pid;
    this.endWatch();
    this.endTail();
    // A late exit of the released child is ignored (`this.child` no longer
    // names it), and unref lets this process exit without waiting on it.
    this.child?.unref();
    this.child = null;
    this.adopted = null;
    this.serverInfo = null;
    this.setState({ name: 'released' });
    this.log.shell(`server left running for the next launch (pid ${pid ?? 'unknown'})`);
  }

  private finishStop(): void {
    this.child = null;
    this.adopted = null;
    this.serverInfo = null;
    this.endTail();
    this.clearRecord();
    this.setState({ name: 'stopped' });
    this.log.shell('server stopped');
  }

  /** POST /api/system/shutdown; true when the server accepted it. */
  private async requestShutdown(url: string): Promise<boolean> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), SHUTDOWN_REQUEST_TIMEOUT_MS);
    try {
      await this.deps.fetchImpl(`${url}/api/system/shutdown`, {
        method: 'POST',
        signal: controller.signal,
      });
      this.log.shell('POST /api/system/shutdown accepted');
      return true;
    } catch (error) {
      this.log.shell(`graceful shutdown request failed: ${errorMessage(error)}`);
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  // --- adopted servers -----------------------------------------------------

  private async stopAdopted(adopted: { pid: number; url: string }): Promise<void> {
    this.endWatch();
    this.setState({ name: 'quitting', phase: 'graceful' });
    this.emitStatus('Stopping the Collimate server…', 'finishing running work');
    await this.stopPid(adopted.pid, adopted.url, true);
    this.finishStop();
  }

  /**
   * Stop a server we hold no ChildProcess for — an adopted one, or one a
   * recorded pid names — and wait until its pid is gone: POST shutdown
   * (when `url` is known to be that server), then SIGTERM and SIGKILL to
   * its process group, polling kill(pid, 0) in between. Waiting matters:
   * the server holds the data dir's lock (.collimate/server.lock) until
   * it exits, and a new one started before then dies with LockHeld.
   */
  private async stopPid(pid: number, url: string | null, reportPhases: boolean): Promise<void> {
    if (url && (await this.requestShutdown(url)) && (await this.waitForPidGone(pid, this.shutdownTimeoutMs()))) {
      return;
    }
    if (!this.isAlive(pid)) return;
    if (reportPhases) this.setState({ name: 'quitting', phase: 'sigterm' });
    this.log.shell(`server (pid ${pid}) still running; SIGTERM to process group ${pid}`);
    this.signalGroup(pid, 'SIGTERM');
    if (await this.waitForPidGone(pid, SIGTERM_GRACE_MS)) return;
    if (reportPhases) this.setState({ name: 'quitting', phase: 'sigkill' });
    this.log.shell(`server (pid ${pid}) ignored SIGTERM; SIGKILL`);
    this.signalGroup(pid, 'SIGKILL');
    if (!(await this.waitForPidGone(pid, SIGKILL_WAIT_MS))) {
      this.log.shell(`server (pid ${pid}) is still alive after SIGKILL`);
    }
  }

  /** Resolves true once kill(pid, 0) says the pid is gone, false at `ms`. */
  private async waitForPidGone(pid: number, ms: number): Promise<boolean> {
    const deadline = this.deps.now() + ms;
    while (this.isAlive(pid)) {
      if (this.deps.now() >= deadline) return false;
      await this.deps.sleep(HEALTH_POLL_MS);
    }
    return true;
  }

  /**
   * Watch an adopted server, which sends us no exit event: its pid every
   * LIVENESS_POLL_MS, its /health every LIVENESS_HEALTH_EVERY polls. A gone
   * pid, or LIVENESS_HEALTH_FAILURES failed health checks in a row, is a
   * crash.
   */
  private startWatch(adopted: { pid: number; url: string }): void {
    const generation = ++this.watchGeneration;
    void (async () => {
      let polls = 0;
      let failures = 0;
      for (;;) {
        await this.deps.sleep(LIVENESS_POLL_MS);
        if (generation !== this.watchGeneration) return;
        if (!this.isAlive(adopted.pid)) {
          this.onAdoptedGone(adopted, false, 'the server process is gone');
          return;
        }
        polls += 1;
        if (polls % LIVENESS_HEALTH_EVERY !== 0) continue;
        const health = await this.probe(`${adopted.url}/health`);
        if (generation !== this.watchGeneration) return;
        failures = health.ok ? 0 : failures + 1;
        if (failures >= LIVENESS_HEALTH_FAILURES) {
          this.onAdoptedGone(adopted, true, 'the server stopped answering /health');
          return;
        }
      }
    })();
  }

  private endWatch(): void {
    this.watchGeneration += 1;
  }

  private onAdoptedGone(adopted: { pid: number; url: string }, stillAlive: boolean, why: string): void {
    this.endWatch();
    this.endTail();
    this.log.shell(`adopted server (pid ${adopted.pid}): ${why}`);
    const tail = this.log.tail();
    this.serverInfo = null;
    if (stillAlive) {
      // Wedged rather than gone: keep both the pid and its record, so the
      // next stop() or start() puts it down before anything else binds
      // the data dir.
      this.adopted = adopted;
    } else {
      this.adopted = null;
      this.clearRecord();
    }
    this.setState({ name: 'crashed', code: null, signal: null, tail });
    this.events.emit('crashed', { code: null, signal: null, tail });
  }

  // --- plumbing ------------------------------------------------------------

  private buildSpawnPlan(uv: UvResolution, port: number): SpawnPlan {
    const dataArgs = ['--port', String(port), '--data-dir', this.options.dataDir];
    // Which server to run, from two sources with one grammar.
    //
    // The environment variable is the per-launch override: type it in
    // front of the binary and this run uses it. `serverSpec` in
    // settings.json is the durable one, and it is what makes an
    // *installed* build usable at all before the matching release is on
    // PyPI — a launcher or a Dock icon starts the app from the desktop
    // session, which has no shell environment to carry a spec in, so the
    // answer has to already be on disk. build/build_desktop.sh writes
    // `local:<checkout>` there when it installs a locally built app.
    //
    // Env first, so the transient beats the persisted. Empty counts as
    // unset in both: `COLLIMATE_SERVER_SPEC=` in a wrapper script means
    // "never mind", not `--from ''`.
    const spec = firstNonEmpty(this.env['COLLIMATE_SERVER_SPEC'], this.options.serverSpec);

    // `local:<path>` runs a checkout instead of a PyPI release, in a
    // packaged build too — the escape hatch for testing a real .app
    // against local server changes.
    if (spec?.startsWith('local:')) {
      const project = resolveApiProject(spec.slice('local:'.length));
      return uvRunPlan(uv, project, dataArgs);
    }

    if (!this.options.isPackaged) {
      const repoRoot = findRepoRoot(this.options.appPath);
      if (repoRoot) return uvRunPlan(uv, path.join(repoRoot, 'api'), dataArgs);
      this.log.shell(
        `no api/pyproject.toml above ${this.options.appPath}; falling back to the released server`,
      );
    }

    const { command, args } = uvxInvocation(uv.uv);
    const from = spec ?? `collimate==${this.options.appVersion}`;
    const argv = [...args, '--from', from, 'collimate', ...dataArgs];
    return {
      command,
      args: argv,
      cwd: this.options.dataDir,
      display: display(command, argv),
    };
  }

  /**
   * The identity of what a plan runs, recorded in server.json and compared
   * before adopting: the command line, plus the checkout's git HEAD for a
   * `uv run` of a checkout — whose code can change under an unchanged
   * command line and version.
   */
  private specOf(plan: SpawnPlan): string {
    if (!plan.checkout) return plan.display;
    return `${plan.display} @ ${this.deps.gitHeadImpl(plan.checkout) ?? 'no git HEAD'}`;
  }

  /**
   * One health probe, with a hard ceiling.
   *
   * The timeout is not optional: a server that accepts the connection but
   * never answers (a wedged worker) would otherwise park the await
   * forever, and the 90s budget — which is only checked between polls —
   * would never be reconsidered. The splash would spin until the user
   * force-quit. Real timers here rather than deps.sleep so a fake clock
   * in a test can't disable the ceiling.
   */
  private async probe(url: string, timeoutMs: number = PROBE_TIMEOUT_MS): Promise<{ ok: boolean; body: unknown }> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await this.deps.fetchImpl(url, { signal: controller.signal });
      if (!response.ok) return { ok: false, body: null };
      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        // A 200 with an unparseable body still means the server is up.
      }
      return { ok: true, body };
    } catch {
      return { ok: false, body: null };
    } finally {
      clearTimeout(timer);
    }
  }

  private onChildExit(code: number | null, signal: NodeJS.Signals | null): void {
    this.childExit = { code, signal };
    for (const waiter of [...this.exitWaiters]) waiter();
    this.exitWaiters.clear();
    // The child is gone, so its file has everything it will ever write:
    // read it all now, so a crash or failure tail includes the last words.
    this.endTail();
    this.log.shell(`server process exited (${describeExit({ code, signal })})`);

    if (this.current.name === 'ready') {
      // Died under us with a window open. Someone has to decide what to
      // do about it; that someone is index.ts.
      const tail = this.log.tail();
      this.child = null;
      this.serverInfo = null;
      this.clearRecord();
      this.setState({ name: 'crashed', code, signal, tail });
      this.events.emit('crashed', { code, signal, tail });
    }
  }

  /** Resolves true if the child exited within `ms`. */
  private waitForExit(ms: number): Promise<boolean> {
    if (this.childExit) return Promise.resolve(true);
    return new Promise<boolean>((resolve) => {
      let settled = false;
      const done = (value: boolean): void => {
        if (settled) return;
        settled = true;
        this.exitWaiters.delete(waiter);
        resolve(value);
      };
      const waiter = (): void => done(true);
      this.exitWaiters.add(waiter);
      void this.deps.sleep(ms).then(() => done(this.childExit !== null));
    });
  }

  private signalGroup(pid: number, signal: NodeJS.Signals): void {
    try {
      // Negative pid = the whole process group, which is why we spawned
      // detached. This controller only runs on the POSIX spawn path.
      this.deps.killImpl(-pid, signal);
    } catch (error) {
      this.log.shell(`signal ${signal} to ${pid} failed: ${errorMessage(error)}`);
    }
  }

  private shutdownTimeoutMs(): number {
    const raw = this.env['COLLIMATE_APP_SHUTDOWN_TIMEOUT'];
    const seconds = raw ? Number(raw) : NaN;
    if (Number.isFinite(seconds) && seconds > 0) return seconds * 1_000;
    return DEFAULT_SHUTDOWN_TIMEOUT_MS;
  }

  private endTail(): void {
    const stop = this.stopTail;
    this.stopTail = null;
    stop?.();
  }

  // --- the server record -----------------------------------------------------

  private get recordPath(): string {
    return path.join(this.options.runtimeDir, 'server.json');
  }

  /** `startedAt` is wall-clock ms, the clock bootTimeMs answers in (resolveRecord). */
  private writeRecord(record: ServerRecord): void {
    this.record = record;
    try {
      this.deps.fs.writeFileSync(this.recordPath, JSON.stringify(record, null, 2));
    } catch (error) {
      this.log.shell(`couldn't record the server pid: ${errorMessage(error)}`);
    }
  }

  private clearRecord(): void {
    this.record = null;
    try {
      this.deps.fs.rmSync(this.recordPath);
    } catch {
      // Already gone is the expected case.
    }
  }

  /**
   * Settle the server a previous shell recorded in server.json, before
   * anything binds a port: adopt it, stop it, or drop a stale record.
   *
   * Only a record stamped since this machine booted is acted on at all.
   * server.json survives a crash and a reboot, and after a reboot its pid
   * names whatever the kernel handed that number to — often a shell or a
   * session leader, whose whole group -pid would reach. A server from
   * before the boot died with the machine, so such a record is dropped
   * without a signal, and so is one with no usable `startedAt` or one
   * stamped later than now: a clock that ran ahead before the reboot can
   * stamp a pre-boot record past the boot time, and a record from this
   * boot is never in the future. Each side gets BOOT_SLACK_MS.
   *
   * A live recorded server is ADOPTED only when it is provably the server
   * this shell would start: its /health answers with this shell's bridge
   * and the recorded version, its GET /api/system/instance reports this
   * profile's data dir (a recycled pid or port never passes that), and the
   * record's shellVersion, spec and dataDir equal the current ones (an
   * update, a new serverSpec or a checkout at another commit restarts it).
   * Anything else of ours is stopped — over HTTP when /api/system/instance
   * said it is ours, by signals to its group when only the pid's command
   * line names this data dir — and waited for. A pid nothing ties to this
   * profile (another data dir answers on the port, or a silent port and a
   * command line without our data dir) is dropped without a signal.
   *
   * `specFor(port)` is the spec this shell would record for a spawn on
   * `port`. Resolves with the adopted server's info, or null.
   */
  private async resolveRecord(specFor: (port: number) => string): Promise<ServerInfo | null> {
    const raw = this.deps.fs.readFileSync(this.recordPath);
    if (!raw) return null;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      this.clearRecord();
      return null;
    }
    if (!isRecord(parsed)) {
      this.clearRecord();
      return null;
    }
    const pid = parsed['pid'];
    const startedAt = parsed['startedAt'];
    if (
      typeof pid !== 'number' ||
      !Number.isInteger(pid) ||
      pid <= 1 ||
      typeof startedAt !== 'number' ||
      !Number.isFinite(startedAt)
    ) {
      this.clearRecord();
      return null;
    }

    if (startedAt < this.deps.bootTimeMs() - BOOT_SLACK_MS) {
      this.log.shell(`server.json (pid ${pid}) predates this boot; dropping it without signalling`);
      this.clearRecord();
      return null;
    }
    if (startedAt > this.deps.now() + BOOT_SLACK_MS) {
      this.log.shell(`server.json (pid ${pid}) is stamped in the future; dropping it without signalling`);
      this.clearRecord();
      return null;
    }

    if (!this.isAlive(pid)) {
      this.clearRecord();
      return null;
    }

    this.emitStatus('Checking the server from the last session…');
    const verdict = await this.inspectRecord(pid, parsed, specFor);
    if (verdict.adopt) {
      const adopted = { pid, url: verdict.url };
      this.adopted = adopted;
      this.record = { ...(parsed as unknown as ServerRecord) };
      const info = localInfo(verdict.url, verdict.port, this.options.dataDir, verdict.version);
      this.serverInfo = info;
      this.stopTail = this.deps.processLog.tail((line) => this.log.server(line), { fromEnd: true });
      this.startWatch(adopted);
      this.setState({ name: 'ready', info });
      this.log.shell(`adopted the server left running at ${verdict.url} (pid ${pid}, version ${verdict.version ?? 'unknown'})`);
      return info;
    }

    if (verdict.stop === 'none') {
      this.log.shell(`server.json (pid ${pid}) names a process that isn't ours: ${verdict.reason}; dropping it without signalling`);
      this.clearRecord();
      return null;
    }
    this.log.shell(`found a server from a previous run (pid ${pid}) that can't be adopted: ${verdict.reason}; stopping it`);
    this.emitStatus('Stopping the server from the last session…');
    await this.stopPid(pid, verdict.stop === 'http' ? verdict.url : null, false);
    this.clearRecord();
    return null;
  }

  /**
   * Whether a live, this-boot record is a server to adopt, and if not, how
   * it may be stopped: over HTTP when the server itself reported this data
   * dir, by signals when only its command line names this data dir, and
   * not at all when nothing says the pid is ours.
   */
  private async inspectRecord(
    pid: number,
    record: Record<string, unknown>,
    specFor: (port: number) => string,
  ): Promise<
    | { adopt: true; url: string; port: number; version: string | null }
    | { adopt: false; reason: string; stop: 'http' | 'signals' | 'none'; url: string | null }
  > {
    const port = record['port'];
    const rawUrl = record['url'];
    if (typeof port !== 'number' || !Number.isInteger(port) || typeof rawUrl !== 'string') {
      return this.silentRecord(pid, 'the record names no server address');
    }
    const url = rawUrl.replace(/\/+$/, '');
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(url)) {
      return this.silentRecord(pid, `the record names ${url}, not a loopback server`);
    }

    // Is it ours? Only the server's own answer counts: a pid or a port can
    // be recycled, a data dir cannot be shared (the server holds its lock).
    // Another data dir answering means our server is gone — alive, it
    // would still hold its port — so the pid is someone else's.
    const instance = await this.probe(`${url}/api/system/instance`);
    const instanceDir = instance.ok && isRecord(instance.body) ? instance.body['data_dir'] : undefined;
    if (typeof instanceDir === 'string' && !samePath(instanceDir, this.options.dataDir)) {
      return { adopt: false, reason: `${url} reports another data dir`, stop: 'none', url: null };
    }
    if (typeof instanceDir !== 'string') {
      return this.silentRecord(pid, `${url} does not report its data dir`);
    }

    const health = await this.probe(`${url}/health`);
    const body = health.ok && isRecord(health.body) ? health.body : null;
    const version = body && typeof body['version'] === 'string' ? body['version'] : null;
    const reasons: string[] = [];
    if (!body) reasons.push('/health did not answer');
    else if (body['bridge'] !== BRIDGE_PROTOCOL) reasons.push(`it speaks bridge ${String(body['bridge'])}`);
    if (body && version !== record['version']) {
      reasons.push(`its version is ${version ?? 'unknown'}, the record says ${String(record['version'])}`);
    }
    if (record['shellVersion'] !== this.options.appVersion) {
      reasons.push(`it was started by shell ${String(record['shellVersion'])}, this is ${this.options.appVersion}`);
    }
    if (record['spec'] !== specFor(port)) reasons.push('the server spec changed');
    if (typeof record['dataDir'] !== 'string' || !samePath(record['dataDir'], this.options.dataDir)) {
      reasons.push('it was recorded for another data dir');
    }
    if (reasons.length) return { adopt: false, reason: reasons.join('; '), stop: 'http', url };
    return { adopt: true, url, port, version };
  }

  /**
   * A recorded pid that gave no HTTP answer: signalled only when its own
   * command line passes this profile's data dir (a wedged server of ours),
   * never on the record's word alone — a kept-alive record lives for days,
   * long enough for its pid to be handed to a login shell or a daemon.
   */
  private silentRecord(
    pid: number,
    reason: string,
  ): { adopt: false; reason: string; stop: 'signals' | 'none'; url: null } {
    const args = this.deps.processArgsImpl(pid);
    const dataDir = this.options.dataDir;
    const ours = args !== null && (args.includes(`--data-dir ${dataDir}`) || args.includes(`--data-dir=${dataDir}`));
    return ours
      ? { adopt: false, reason, stop: 'signals', url: null }
      : { adopt: false, reason: `${reason}, and its command line does not name this data dir`, stop: 'none', url: null };
  }

  private isAlive(pid: number): boolean {
    try {
      this.deps.killImpl(pid, 0);
      return true;
    } catch {
      return false;
    }
  }

  // --- state ---------------------------------------------------------------

  private setState(state: ServerState): void {
    this.current = state;
    this.events.emit('state', state);
  }

  private emitStatus(message: string, detail?: string): void {
    this.events.emit('status', detail === undefined ? { message } : { message, detail });
  }

  private fail(reason: FailureReason, message: string, command: string | null): never {
    // A child that already exited has nothing more to say; read it all so
    // the failure screen shows its last lines.
    if (this.childExit) this.endTail();
    const state: Extract<ServerState, { name: 'failed' }> = {
      name: 'failed',
      reason,
      message,
      command,
      tail: this.log.tail(),
    };
    this.log.shell(`startup failed (${reason}): ${message}`);
    this.setState(state);
    throw new ServerStartError(state);
  }
}

// ---------------------------------------------------------------------------
// helpers (exported so tests can use them directly)
// ---------------------------------------------------------------------------

/** The ServerInfo of the local server at `url`. */
function localInfo(url: string, port: number, dataDir: string, serverVersion: string | null): ServerInfo {
  return { id: LOCAL_SERVER_ID, kind: 'local', name: LOCAL_SERVER_NAME, url, port, dataDir, serverVersion };
}

/** Two spellings of one directory: equal resolved, or equal once symlinks are followed. */
export function samePath(a: string, b: string): boolean {
  if (path.resolve(a) === path.resolve(b)) return true;
  try {
    return realpathSync.native(a) === realpathSync.native(b);
  } catch {
    return false;
  }
}

/** One loopback port: `port` itself, or null when it can't be bound. */
function tryListen(port: number): Promise<number | null> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(null));
    server.listen(port, '127.0.0.1', () => {
      const address = server.address();
      const bound = address !== null && typeof address !== 'string' ? address.port : null;
      server.close(() => resolve(bound));
    });
  });
}

/**
 * Pick the server's port: `preferred` when it is free (the last local
 * port, so the window's origin and its per-origin storage stay the same
 * across launches), else one the OS hands out.
 *
 * Classic TOCTOU: something else could take the port between our close
 * and the server's bind. In practice the window is microseconds on
 * loopback, and the alternative (a fixed port) collides with a second
 * Collimate, a stale server, or whatever else the user runs on 5001 —
 * which is a certainty rather than a race.
 */
export async function allocatePort(preferred?: number): Promise<number> {
  if (preferred !== undefined && Number.isInteger(preferred) && preferred > 0 && preferred < 65_536) {
    const bound = await tryListen(preferred);
    if (bound !== null) return bound;
  }
  const ephemeral = await tryListen(0);
  if (ephemeral === null) throw new Error('could not determine an ephemeral port');
  return ephemeral;
}

/**
 * The server child's stdio: stdout and stderr to the process log file, and
 * every other descriptor slot this process has open pointed at /dev/null.
 *
 * Electron's main process holds descriptors without close-on-exec
 * (Chromium's IPC sockets, pipes from whatever launched the app), and
 * libuv's spawn passes those to the child untouched. A server that
 * outlives the shell would keep them open: whatever waits for the app's
 * pipes to close — a test driver, `dev_desktop.sh | tee` — would then
 * wait for the server too. Mapping each slot to /dev/null replaces the
 * inherited descriptor in the child.
 */
export function serverStdio(
  outputFd: number | null,
  openFds: readonly number[],
  nullFd: number | null,
): SpawnOptions['stdio'] {
  const out = outputFd ?? 'ignore';
  const stdio: (number | 'ignore')[] = ['ignore', out, out];
  if (nullFd === null) return stdio;
  const highest = Math.max(2, ...openFds.filter((fd) => Number.isInteger(fd)));
  for (let fd = 3; fd <= highest; fd += 1) stdio.push(nullFd);
  return stdio;
}

/** This process's open descriptors, from /dev/fd (Linux and macOS alike). */
export function openFileDescriptors(): number[] {
  try {
    return readdirSync('/dev/fd')
      .map((entry) => Number(entry))
      .filter((fd) => Number.isInteger(fd) && fd >= 0);
  } catch {
    return [];
  }
}

/**
 * `pid`'s command line, arguments joined by spaces: /proc on Linux, `ps`
 * elsewhere (macOS). Null when it can't be read.
 */
export function processArgs(pid: number): string | null {
  try {
    if (process.platform === 'linux') {
      const raw = readFileSync(`/proc/${pid}/cmdline`, 'utf8');
      return raw.split('\0').filter(Boolean).join(' ') || null;
    }
    const args = execFileSync('ps', ['-ww', '-o', 'args=', '-p', String(pid)], {
      encoding: 'utf8',
      timeout: 3_000,
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    return args || null;
  } catch {
    return null;
  }
}

/** `git rev-parse HEAD` in `dir`, or null outside a repository (or without git). */
export function gitHead(dir: string): string | null {
  try {
    const head = execFileSync('git', ['-C', dir, 'rev-parse', 'HEAD'], {
      encoding: 'utf8',
      timeout: 3_000,
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    return head || null;
  } catch {
    return null;
  }
}

/** Walk up from `start` until a directory holding api/pyproject.toml. */
export function findRepoRoot(start: string): string | null {
  let dir = path.resolve(start);
  for (;;) {
    if (existsSync(path.join(dir, 'api', 'pyproject.toml'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** Accept either a repo root or the api/ dir itself for `local:<path>`. */
export function resolveApiProject(input: string): string {
  const resolved = path.resolve(input.trim());
  if (existsSync(path.join(resolved, 'api', 'pyproject.toml'))) return path.join(resolved, 'api');
  return resolved;
}

function uvRunPlan(uv: UvResolution, project: string, dataArgs: string[]): SpawnPlan {
  // `python main.py` rather than the `collimate` console script: the
  // checkout doesn't need to be installed as a tool, and an absolute
  // script path keeps this working regardless of cwd.
  const args = ['run', '--project', project, 'python', path.join(project, 'main.py'), ...dataArgs];
  return { command: uv.uv, args, cwd: project, display: display(uv.uv, args), checkout: project };
}

/**
 * The first value that carries something, trimmed; undefined if none do.
 *
 * Used for the server spec, where the sources are an environment variable
 * and a JSON field — both of which can arrive as an empty or padded
 * string, and neither of which means anything when they do.
 */
function firstNonEmpty(...values: (string | undefined)[]): string | undefined {
  for (const value of values) {
    const trimmed = value?.trim();
    if (trimmed) return trimmed;
  }
  return undefined;
}

function display(command: string, args: string[]): string {
  return [command, ...args].map((part) => (/\s/.test(part) ? JSON.stringify(part) : part)).join(' ');
}

function describeExit(exit: { code: number | null; signal: NodeJS.Signals | null }): string {
  if (exit.signal) return `killed by ${exit.signal}`;
  return `exit code ${exit.code ?? 'unknown'}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

const nodeServerFs: ServerFs = {
  readFileSync(file) {
    try {
      return readFileSync(file, 'utf8');
    } catch {
      return null;
    }
  },
  writeFileSync(file, contents) {
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, contents, 'utf8');
  },
  rmSync(file) {
    rmSync(file, { force: true });
  },
};
