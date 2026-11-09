import { execFileSync, spawn, type ChildProcess, type SpawnOptions } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { cp, mkdir, readFile, rename, rm } from 'node:fs/promises';
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
import { findUv, uvAttempts, type UvResolution } from './findUv.js';
import {
  channelArgs,
  parseStartupRefusal,
  STARTUP_REFUSED_EXIT_CODE,
  type ChannelsOverride,
  type LocationOwned,
} from './locationOwned.js';

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
  /** A checkout's server needs uv, and none was found. */
  | 'uvMissing'
  /** The spawn itself failed (ENOENT, EACCES, …). */
  | 'spawnFailed'
  /** The child is alive but never answered /health + /ready in budget. */
  | 'healthTimeout'
  /** The child exited before it ever became healthy. */
  | 'earlyExit'
  /** The package's server environment is missing, or not the one its build stamp names. */
  | 'bundleBroken'
  /**
   * The server refused to start: its default channel location belongs to
   * another installation (exit code 78 with the refusal line; see
   * locationOwned.ts). The failed state carries what the line said.
   */
  | 'locationOwned';

export type QuitPhase = 'graceful' | 'sigterm' | 'sigkill';

export type ServerState =
  | { name: 'idle' }
  | { name: 'findingUv'; detail: string }
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
      /** Present exactly when `reason` is `locationOwned`. */
      locationOwned?: LocationOwned;
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
  /** uv for a checkout's server, or null when the machine has none. */
  findUvImpl: () => UvResolution | null;
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
  /**
   * The `hash` a server environment's manifest names
   * (`<dir>/collimate-server.json`), or null when it has none.
   */
  serverHashImpl: (dir: string) => Promise<string | null>;
  /**
   * Copy the server environment at `source` to `dest`, unless `dest`
   * already holds the one named `hash`, and check the copy's manifest.
   * Resolves true when it copied (see {@link stageServer}).
   */
  stageServerImpl: (source: string, dest: string, hash: string) => Promise<boolean>;
  /** Remove every entry of `dir` not named in `keep`; returns the removed names. */
  pruneDirImpl: (dir: string, keep: readonly string[]) => string[];
}

export interface ServerControllerOptions {
  /** Passed to the server as --data-dir; the server chdirs into it. */
  dataDir: string;
  /** Where the shell keeps its own files (server.json, bin/, logs/). */
  runtimeDir: string;
  /**
   * The app version (SHELL_VERSION): the `shellVersion` a kept-alive server's
   * record must match to be adopted.
   */
  appVersion: string;
  isPackaged: boolean;
  /** app.getAppPath(); the walk-up to the repo root starts here. */
  appPath: string;
  /**
   * A package's server environment: the resources directory
   * (process.resourcesPath) whose `server/` holds it, the hash its build
   * stamp names, and whether it runs from a stable copy under the runtime
   * dir (an AppImage, whose mount moves every launch and goes when the
   * shell exits). Absent in a dev run, and in a package whose stamp names
   * no environment, which then cannot start a local server.
   */
  bundledServer?: { resourcesPath: string; hash: string; stableCopy: boolean };
  /**
   * settings.json's `localPort`: the port the last local server had, tried
   * first so the local origin (and the page's per-origin storage) survives
   * a relaunch and a restart. Taken means an ephemeral port instead. Read
   * at every start, so a restart prefers the port the last start ended up
   * with rather than the one the shell launched with.
   */
  preferredPort?: () => number | undefined;
  /**
   * The channel location this profile's server defaults to, passed as
   * `--channels-dir` on every spawn (`~/Collimate/Channels`, the dev
   * profile's `~/Collimate/Channels-Dev`, D17). The server uses it only
   * while the installation owns no location; after that it ignores the
   * flag. Absent: the server's own default, `<data dir>/Channels`.
   */
  channelsDir?: string;
  env?: NodeJS.ProcessEnv;
  log?: LogSink;
  deps?: Partial<ServerControllerDeps>;
}

/**
 * How long a start gets. A package's environment answers in a second or
 * two; the budget is for a checkout, whose first `uv run` syncs its
 * environment.
 */
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
  /** The server environment a package's plan runs, by the hash that is part of its spec. */
  server?: BundledServer;
}

/** Which server a start runs, decided before anything is looked up or spawned. */
type ServerChoice =
  | { kind: 'checkout'; checkout: string }
  | { kind: 'bundled'; server: BundledServer }
  | { kind: 'refused'; reason: FailureReason; message: string };

/** A package's server environment: where it is shipped, where it runs from, and its hash. */
interface BundledServer {
  /** `<resources>/server`, as the package carries it. */
  source: string;
  /** Where it runs: `source` itself, or its stable copy (see SERVER_COPIES_DIR). */
  root: string;
  hash: string;
  stableCopy: boolean;
}

/** The package's server environment, in its resources directory. */
export const SERVER_RESOURCE = 'server';
/** Its manifest (build/server_env.py), naming the environment's hash. */
export const SERVER_MANIFEST = 'collimate-server.json';
/**
 * `<runtimeDir>/server-environments/<hash[0:16]>/`: where an AppImage's
 * environment runs from. The AppImage mounts at a new /tmp path every
 * launch and unmounts when the shell exits, which a kept-alive server
 * would not survive and which would change the spawn spec every launch.
 * One directory per environment hash never holds different bytes, so it is
 * never stale, and a rebuild that changed the environment gets a new one.
 */
export const SERVER_COPIES_DIR = 'server-environments';

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
  /** The location-owned refusal the current spawn wrote, if it wrote one. */
  private startupRefusal: LocationOwned | null = null;

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
      findUvImpl: () => findUv({ env: this.env }),
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
      serverHashImpl: serverHash,
      stageServerImpl: stageServer,
      pruneDirImpl: pruneDir,
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
   *
   * `override` is the flag the user's answer to a `locationOwned` refusal
   * names, added to this spawn only (see locationOwned.ts channelArgs).
   */
  start(override?: ChannelsOverride): Promise<ServerInfo> {
    if (this.current.name === 'ready' && this.serverInfo) {
      return Promise.resolve(this.serverInfo);
    }
    if (this.starting) return this.starting;
    const attempt = this.runStart(override).finally(() => {
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

  private async runStart(override: ChannelsOverride | undefined): Promise<ServerInfo> {
    // A child left from a failed attempt (a healthTimeout leaves it
    // running), or a wedged adopted server, is ours: stop it first, or
    // resolveRecord would reap it and its exit would fail this attempt as
    // an early exit.
    if (this.child || this.adopted) await this.stop();
    this.childExit = null;
    this.serverInfo = null;

    // 1. Which server: a checkout's (with uv), or the environment the
    // package carries. Decided first, because the spawn plan is part of
    // the identity a kept-alive server must match to be adopted.
    const choice = this.chooseServer();
    if (choice.kind === 'refused') return this.fail(choice.reason, choice.message, null);
    let uv: UvResolution | null = null;
    if (choice.kind === 'checkout') {
      this.setState({ name: 'findingUv', detail: 'Looking for uv…' });
      uv = this.deps.findUvImpl();
      if (!uv) {
        const attempts = uvAttempts(this.env);
        for (const attempt of attempts) this.log.shell(`looked for uv at: ${attempt}`);
        return this.fail(
          'uvMissing',
          `Running the server of the checkout at ${choice.checkout} needs uv, and none was found ` +
            `(looked at ${attempts.join(', ')}). Install uv from https://docs.astral.sh/uv/ and start again.`,
          null,
        );
      }
      this.log.shell(`using uv: ${uv.uv} (${uv.source}) — ${uv.version}`);
    } else {
      const broken = await this.checkServer(choice.server);
      if (broken) return this.fail('bundleBroken', broken, null);
    }
    // The identity leaves a one-spawn override out: the server it starts is
    // the same one a plain start would make, and a later launch adopts it.
    const planFor = (port: number, oneSpawn?: ChannelsOverride): SpawnPlan =>
      this.buildSpawnPlan(choice, uv, port, oneSpawn);

    // 2. A server an earlier shell recorded: adopt it when it is provably
    // the one we would start, otherwise stop it and wait until it is gone
    // (it holds the data dir's lock) before we bind a new one.
    const adopted = await this.resolveRecord((port) => this.specOf(planFor(port)));
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
    const plan = planFor(port, override);
    this.setState({ name: 'spawning', command: plan.display });
    if (plan.server?.stableCopy) await this.stageServer(plan.server);
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
        // later kill(-pid) reaches the server and everything it spawned (for
        // a checkout, uv AND the python it execs),
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
    this.startupRefusal = null;
    this.stopTail = this.deps.processLog.tail((line) => {
      this.log.server(line);
      const refusal = parseStartupRefusal(line);
      if (refusal) this.startupRefusal = refusal;
    }, { fromEnd: false });
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
      spec: this.specOf(planFor(port)),
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
        return this.failExited(`The server exited before it was ready (${describeExit(this.childExit)}).`, plan);
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
        return this.failExited(`The server exited during startup (${describeExit(this.childExit)}).`, plan);
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
    if (plan.server?.stableCopy) this.pruneServers(plan.server);
    return info;
  }

  /**
   * Whether the package's environment is the one its build stamp names:
   * the message to fail with when it is missing or another, else null.
   * Running some other server than the one built is never a fallback.
   */
  private async checkServer(server: BundledServer): Promise<string | null> {
    const found = await this.deps.serverHashImpl(server.source).catch(() => null);
    if (found === server.hash) return null;
    return (
      `This package's server environment (${server.source}) ` +
      (found === null ? 'is missing or has no manifest' : `is ${found.slice(0, 16)}, not the ${server.hash.slice(0, 16)} its build stamp names`) +
      '. Install the app again, or package it again with build/build_desktop.sh.'
    );
  }

  /** Copy the environment to its stable path before the spawn (an AppImage). */
  private async stageServer(server: BundledServer): Promise<void> {
    this.emitStatus('Preparing the server this build carries…', 'copying it out of the AppImage, once per build');
    let copied: boolean;
    try {
      copied = await this.deps.stageServerImpl(server.source, server.root, server.hash);
    } catch (error) {
      return this.fail(
        'bundleBroken',
        `This package's server environment could not be copied to ${server.root}: ${errorMessage(error)}.`,
        null,
      );
    }
    this.log.shell(`server environment: ${server.root} (${copied ? `copied from ${server.source}` : 'already in place'})`);
  }

  /**
   * Once the server runs, drop the copies of other builds' environments:
   * nothing names their paths any more.
   */
  private pruneServers(server: BundledServer): void {
    const dir = path.join(this.options.runtimeDir, SERVER_COPIES_DIR);
    try {
      const removed = this.deps.pruneDirImpl(dir, [path.basename(server.root)]);
      if (removed.length) this.log.shell(`removed other builds' server environments: ${removed.join(', ')}`);
    } catch (error) {
      this.log.shell(`couldn't remove other builds' server environments: ${errorMessage(error)}`);
    }
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

  /**
   * Which server to run. `COLLIMATE_SERVER_SPEC=local:<checkout>`, typed in
   * front of the binary for one launch, runs that checkout in any build:
   * the developer's escape hatch for testing a real package against local
   * server changes. Empty counts as unset; any other value is refused, so
   * a stale spec never silently runs something else. Otherwise a dev run
   * serves the checkout it runs from, and a package runs the environment
   * it carries. A server that should outlive one launch is started on its
   * own and added in Settings → Servers instead.
   */
  private chooseServer(): ServerChoice {
    const spec = firstNonEmpty(this.env['COLLIMATE_SERVER_SPEC']);
    if (spec?.startsWith('local:')) {
      return { kind: 'checkout', checkout: resolveCheckout(spec.slice('local:'.length)) };
    }
    if (spec) {
      return {
        kind: 'refused',
        reason: 'spawnFailed',
        message: `COLLIMATE_SERVER_SPEC is ${JSON.stringify(spec)}; it takes one form, local:<checkout>.`,
      };
    }
    if (!this.options.isPackaged) {
      const checkout = findRepoRoot(this.options.appPath);
      if (checkout) return { kind: 'checkout', checkout };
      return {
        kind: 'refused',
        reason: 'spawnFailed',
        message:
          `An unpackaged shell runs the server of the checkout it is in, and there is no ${CHECKOUT_MARKER} ` +
          `above ${this.options.appPath}. Run it from a checkout, or set COLLIMATE_SERVER_SPEC=local:<checkout>.`,
      };
    }
    const bundled = this.options.bundledServer;
    if (!bundled) {
      return {
        kind: 'refused',
        reason: 'bundleBroken',
        message:
          'This package carries no server environment its build stamp names. ' +
          'Install the app again, or package it again with build/build_desktop.sh.',
      };
    }
    const source = path.join(bundled.resourcesPath, SERVER_RESOURCE);
    const root = bundled.stableCopy
      ? path.join(this.options.runtimeDir, SERVER_COPIES_DIR, bundled.hash.slice(0, 16))
      : source;
    return { kind: 'bundled', server: { source, root, hash: bundled.hash, stableCopy: bundled.stableCopy } };
  }

  private buildSpawnPlan(
    choice: Exclude<ServerChoice, { kind: 'refused' }>,
    uv: UvResolution | null,
    port: number,
    override?: ChannelsOverride,
  ): SpawnPlan {
    const dataArgs = [
      '--port',
      String(port),
      '--data-dir',
      this.options.dataDir,
      ...channelArgs(this.options.channelsDir, override),
    ];
    if (choice.kind === 'checkout') return uvRunPlan(uv!, choice.checkout, dataArgs);
    // The package's own interpreter, isolated (-I: no user site, no
    // PYTHON* variables, no cwd on sys.path) and writing no bytecode into
    // the environment (-B; it ships compiled), told the environment it runs
    // from (--server-env), whose uv starts its generator runs and whose
    // interpreter they run on where their requires-python allows. Nothing
    // is looked for and nothing downloaded to start it.
    const python = path.join(choice.server.root, 'bin', 'python3');
    const args = ['-I', '-B', '-m', 'collimate_server', ...dataArgs, '--server-env', choice.server.root];
    return { command: python, args, cwd: this.options.dataDir, display: display(python, args), server: choice.server };
  }

  /**
   * The identity of what a plan runs, recorded in server.json and compared
   * before adopting: the command line, plus what can change under an
   * unchanged command line: a checkout's git HEAD, or the environment's
   * hash for a package (an update installs a new environment at the same
   * path). A rebuild that changed the environment restarts a kept-alive
   * server; an identical one adopts it.
   */
  private specOf(plan: SpawnPlan): string {
    if (plan.server) return `${plan.display} @ server ${plan.server.hash}`;
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
   * update, a new spec or a checkout at another commit restarts it).
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

  /**
   * The child exited before it was ready: `locationOwned` when it exited
   * with code 78 and wrote the refusal line (both, never one alone), else
   * `earlyExit` with `message`.
   */
  private failExited(message: string, plan: SpawnPlan): never {
    // Its whole output first: the refusal line may be the last thing it wrote.
    this.endTail();
    const refusal = this.childExit?.code === STARTUP_REFUSED_EXIT_CODE ? this.startupRefusal : null;
    if (!refusal) return this.fail('earlyExit', message, plan.display);
    return this.fail(
      'locationOwned',
      `The channel location ${refusal.path} belongs to the Collimate installation at ${refusal.ownerDataDir}.`,
      plan.display,
      refusal,
    );
  }

  private fail(reason: FailureReason, message: string, command: string | null, locationOwned?: LocationOwned): never {
    // A child that already exited has nothing more to say; read it all so
    // the failure screen shows its last lines.
    if (this.childExit) this.endTail();
    const state: Extract<ServerState, { name: 'failed' }> = {
      name: 'failed',
      reason,
      message,
      command,
      tail: this.log.tail(),
      ...(locationOwned ? { locationOwned } : {}),
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
 *
 * The child's own descriptors are then numbered past this process's
 * highest, above 1024 as a rule: the server must never wait on them with
 * select(), which refuses such numbers (its relays and forwarders use
 * selectors; api/collimate_server/process_manager/environments.py says why).
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

/** What marks a checkout's root: the server package's `python -m` entry. */
export const CHECKOUT_MARKER = path.join('api', 'collimate_server', '__main__.py');

/** Walk up from `start` until a checkout's root (one holding {@link CHECKOUT_MARKER}). */
export function findRepoRoot(start: string): string | null {
  let dir = path.resolve(start);
  for (;;) {
    if (existsSync(path.join(dir, CHECKOUT_MARKER))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** `local:<path>`'s checkout: the root at or above `input`, else `input` as given. */
export function resolveCheckout(input: string): string {
  const resolved = path.resolve(input.trim());
  return findRepoRoot(resolved) ?? resolved;
}

/** The `hash` in `<dir>/collimate-server.json`, or null when there is none to read. */
export async function serverHash(dir: string): Promise<string | null> {
  try {
    const manifest: unknown = JSON.parse(await readFile(path.join(dir, SERVER_MANIFEST), 'utf8'));
    const hash = isRecord(manifest) ? manifest['hash'] : null;
    return typeof hash === 'string' && /^[0-9a-f]{64}$/.test(hash) ? hash : null;
  } catch {
    return null;
  }
}

/**
 * Copy the server environment at `source` to `dest`, unless `dest`
 * already holds the one named `hash`: a copy this function made earlier,
 * since it renames into place only after the copy is whole. Symlinks are
 * copied as links and timestamps kept. Resolves true when it copied;
 * rejects, leaving nothing at `dest`, when the copied manifest does not
 * name `hash`.
 */
export async function stageServer(source: string, dest: string, hash: string): Promise<boolean> {
  if ((await serverHash(dest)) === hash) return false;
  const partial = `${dest}.partial-${process.pid}`;
  await rm(partial, { recursive: true, force: true });
  await mkdir(path.dirname(dest), { recursive: true });
  try {
    await cp(source, partial, { recursive: true, verbatimSymlinks: true, preserveTimestamps: true });
    const copied = await serverHash(partial);
    if (copied !== hash) {
      throw new Error(`the copy names ${copied ?? 'no hash'}, not ${hash}`);
    }
    await rm(dest, { recursive: true, force: true });
    await rename(partial, dest);
  } finally {
    await rm(partial, { recursive: true, force: true });
  }
  return true;
}

/** Remove every entry of `dir` not named in `keep`; a missing `dir` has none. */
export function pruneDir(dir: string, keep: readonly string[]): string[] {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return [];
  }
  const removed: string[] = [];
  for (const entry of entries) {
    if (keep.includes(entry)) continue;
    rmSync(path.join(dir, entry), { recursive: true, force: true });
    removed.push(entry);
  }
  return removed;
}

function uvRunPlan(uv: UvResolution, checkout: string, dataArgs: string[]): SpawnPlan {
  // The checkout's own project environment, which `uv run` syncs: the
  // server package and the SDK from source, as the checkout holds them.
  const args = ['run', '--project', checkout, 'python', '-m', 'collimate_server', ...dataArgs];
  return { command: uv.uv, args, cwd: checkout, display: display(uv.uv, args), checkout };
}

/**
 * The first value that carries something, trimmed; undefined if none do.
 *
 * Used for the server spec, which can arrive from the environment as an
 * empty or padded string that means nothing.
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
