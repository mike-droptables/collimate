import { randomUUID } from 'node:crypto';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  lstatSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  rmSync,
  statSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { uvAttempts } from '../src/main/findUv.js';
import { chooseLocationOwned, type LocationOwnedDialog } from '../src/main/locationOwned.js';
import { BRIDGE_PROTOCOL } from '../src/shared/bridge.js';
import {
  BOOT_SLACK_MS,
  HEALTH_BUDGET_MS,
  HEALTH_POLL_MS,
  LIVENESS_HEALTH_EVERY,
  LIVENESS_HEALTH_FAILURES,
  LIVENESS_POLL_MS,
  SERVER_COPIES_DIR,
  SERVER_MANIFEST,
  SERVER_RESOURCE,
  SIGTERM_GRACE_MS,
  ServerController,
  ServerStartError,
  allocatePort,
  findRepoRoot,
  resolveCheckout,
  openFileDescriptors,
  processArgs,
  serverStdio,
  machineBootTimeMs,
  pruneDir,
  samePath,
  serverHash,
  stageServer,
  type ServerControllerOptions,
  type ServerState,
} from '../src/main/server.js';
import {
  Harness,
  DEFAULT_PID,
  DEFAULT_PORT,
  SERVER_HASH,
  NULL_FD,
  PROCESS_LOG_FD,
  RESOURCES_PATH,
  commandLine,
  type HarnessOptions,
} from './harness.js';

/**
 * The server lifecycle, driven through ServerController's injection points.
 *
 * Every scenario here is one the shell hits in the field and nobody can
 * reproduce on demand: a uv that isn't there, a server that accepts the
 * socket and never answers, a uvicorn that ignores SIGTERM, a crash with a
 * window open, an orphan left by a hard kill, a server kept running at quit
 * and adopted by the next launch. The controller takes spawn, fetch, kill,
 * the clock, the filesystem and the process log as dependencies precisely
 * so these can be asserted rather than hoped about.
 */

/** The spawn controller always owns a POSIX process group. */
const groupPid = -DEFAULT_PID;

const tempDirs: string[] = [];
let harness: Harness;

/** A tree that looks like the repo to findRepoRoot: <root>/api/collimate_server/__main__.py. */
function fakeRepo(): string {
  const root = mkdtempSync(path.join(os.tmpdir(), 'collimate-repo-'));
  tempDirs.push(root);
  mkdirSync(path.join(root, 'api', 'collimate_server'), { recursive: true });
  writeFileSync(path.join(root, 'api', 'collimate_server', '__main__.py'), '');
  mkdirSync(path.join(root, 'desktop'), { recursive: true });
  return root;
}

/** A nonexistent path whose ancestors cannot include the checkout, even
 *  when the required TMPDIR is inside it. No filesystem writes go here. */
function orphanAppPath(): string {
  return path.join(path.parse(process.cwd()).root, `collimate-norepo-${randomUUID()}`);
}

function build(options: HarnessOptions = {}): ServerController {
  harness = new Harness(options);
  return new ServerController(harness.options);
}

/**
 * build(), plus controller options the harness doesn't model, such as the
 * preferred port that comes from settings.json.
 */
function buildWith(
  extra: Partial<ServerControllerOptions>,
  options: HarnessOptions = {},
): ServerController {
  harness = new Harness(options);
  return new ServerController({ ...harness.options, ...extra });
}

/** Collect every state the controller passes through. */
function trace(controller: ServerController): ServerState[] {
  const states: ServerState[] = [];
  controller.events.on('state', (state) => states.push(state));
  return states;
}

/** Run a start that is expected to fail, advancing the clock past it. */
async function failedStart(controller: ServerController, advanceMs = HEALTH_BUDGET_MS + 5_000) {
  const caught = controller.start().then(
    () => null,
    (error: unknown) => error,
  );
  await vi.advanceTimersByTimeAsync(advanceMs);
  const error = await caught;
  expect(error).toBeInstanceOf(ServerStartError);
  return (error as ServerStartError).state;
}

beforeEach(() => {
  // One clock for everything: the controller's own budget (deps.now /
  // deps.sleep) and the real setTimeout ceilings it puts on each request,
  // so neither can be tested into irrelevance by the other.
  vi.useFakeTimers({ now: 1_700_000_000_000 });
});

afterEach(() => {
  vi.useRealTimers();
  harness?.cleanup();
  for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// happy path
// ---------------------------------------------------------------------------

describe('starting', () => {
  it('reaches ready and reports the server it brought up', async () => {
    const controller = build();
    const states = trace(controller);

    const info = await controller.start();

    expect(info).toEqual({
      id: 'local',
      kind: 'local',
      name: 'Local',
      url: `http://127.0.0.1:${DEFAULT_PORT}`,
      port: DEFAULT_PORT,
      dataDir: harness.options.dataDir,
      serverVersion: '0.1.0',
    });
    expect(controller.state).toEqual({ name: 'ready', info });
    expect(controller.info).toEqual(info);
    expect(states.map((state) => state.name)).toEqual([
      'findingUv',
      'spawning',
      'waitingHealth',
      'waitingHealth',
      'ready',
    ]);
    expect(states[0]).toEqual({ name: 'findingUv', detail: 'Looking for uv…' });
    expect(states[1]).toMatchObject({ name: 'spawning' });
    expect((states[1] as { command: string }).command).toContain(`--port ${DEFAULT_PORT}`);
    expect(harness.fetches.map((call) => call.url)).toEqual([
      `${harness.url}/health`,
      `${harness.url}/ready`,
    ]);
  });

  it('shares one attempt between concurrent callers, and is a no-op once ready', async () => {
    const controller = build();

    const [first, second] = await Promise.all([controller.start(), controller.start()]);

    expect(first).toEqual(second);
    expect(harness.spawns).toHaveLength(1);
    await controller.start();
    expect(harness.spawns).toHaveLength(1);
  });

  it('waits out a /ready that 503s while the server bootstraps', async () => {
    const controller = build();
    harness.readyAfterMs = 3_000;
    const statuses: { message: string; detail?: string }[] = [];
    controller.events.on('status', (status) => statuses.push(status));

    const started = controller.start();
    await vi.advanceTimersByTimeAsync(5_000);
    const info = await started;

    expect(info.port).toBe(DEFAULT_PORT);
    // Progress is reported roughly once a second, not once per 250ms poll.
    const elapsed = statuses.filter((status) => /^\d+s$/.test(status.detail ?? ''));
    expect(elapsed.length).toBeGreaterThanOrEqual(2);
    expect(statuses.some((status) => status.message === 'Opening the workbench…')).toBe(true);
  });

  it('accepts a /health body it cannot parse', async () => {
    const controller = build();
    harness.health = 'invalid-json';

    const info = await controller.start();

    // A 200 is the server saying it's up; the body is a nicety.
    expect(info.serverVersion).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// what we run
// ---------------------------------------------------------------------------

describe('the command', () => {
  it('runs the checkout with `uv run` in a dev build', async () => {
    const repo = fakeRepo();
    const controller = build({ appPath: path.join(repo, 'desktop'), isPackaged: false });

    await controller.start();

    const call = harness.spawns[0]!;
    expect(call.command).toBe('/fake/bin/uv');
    expect(call.args).toEqual([
      'run',
      '--project',
      repo,
      'python',
      '-m',
      'collimate_server',
      '--port',
      String(DEFAULT_PORT),
      '--data-dir',
      harness.options.dataDir,
    ]);
    expect(call.options.cwd).toBe(repo);
  });

  it('honours COLLIMATE_SERVER_SPEC=local:<path> even in a packaged build', async () => {
    const repo = fakeRepo();
    const controller = build({ isPackaged: true, env: { COLLIMATE_SERVER_SPEC: `local:${repo}` } });

    await controller.start();

    const call = harness.spawns[0]!;
    expect(call.command).toBe('/fake/bin/uv');
    expect(call.args.slice(0, 3)).toEqual(['run', '--project', repo]);
    expect(call.options.cwd).toBe(repo);
  });

  it.each(['collimate==9.9.9rc1', 'https://example.test/collimate.whl', 'local', 'path:/x'])(
    'refuses a COLLIMATE_SERVER_SPEC of %s, which is not local:<checkout>',
    async (spec) => {
      const controller = build({ env: { COLLIMATE_SERVER_SPEC: spec } });

      const state = await failedStart(controller, 0);

      expect(state.reason).toBe('spawnFailed');
      expect(state.message).toContain('local:<checkout>');
      expect(state.command).toBeNull();
      expect(harness.spawns).toEqual([]);
      expect(harness.uvLookups).toBe(0);
    },
  );

  it('treats an empty or blank environment spec as unset rather than as an override', async () => {
    // `COLLIMATE_SERVER_SPEC=` in a wrapper script means "never mind".
    for (const blank of ['', '  ']) {
      const controller = build({ env: { COLLIMATE_SERVER_SPEC: blank } });
      await controller.start();
      expect(harness.spawns[0]!.args.slice(0, 3)).toEqual(['run', '--project', harness.checkout]);
      harness.cleanup();
    }
    const controller = build({ isPackaged: true, bundledServer: {}, env: { COLLIMATE_SERVER_SPEC: '  ' } });
    await controller.start();
    expect(harness.spawns[0]!.command).toBe(path.join(RESOURCES_PATH, 'server', 'bin', 'python3'));
  });

  it('fails a dev build that is not in a checkout, with no fallback__P1F02', async () => {
    const controller = build({ appPath: orphanAppPath(), isPackaged: false, appVersion: '0.4.2' });

    const state = await failedStart(controller, 0);

    expect(state.reason).toBe('spawnFailed');
    expect(state.message).toContain('local:<checkout>');
    expect(state.command).toBeNull();
    expect(harness.spawns).toEqual([]);
    expect(harness.uvLookups).toBe(0);
  });

  it('runs a package without a server environment into bundleBroken, never into uv', async () => {
    const controller = build({ isPackaged: true });

    const state = await failedStart(controller, 0);

    expect(state.reason).toBe('bundleBroken');
    expect(harness.spawns).toEqual([]);
    expect(harness.uvLookups).toBe(0);
  });

  it('marks the child as an electron client and puts it in its own process group__F1031', async () => {
    const controller = build({ env: { PATH: '/usr/bin', HOME: '/home/tester' } });

    await controller.start();

    const { options } = harness.spawns[0]!;
    const env = options.env as NodeJS.ProcessEnv;
    expect(env['COLLIMATE_CLIENT']).toBe('electron');
    expect(env['PATH']).toBe('/usr/bin');
    expect(options.detached).toBe(true);
    expect(options.windowsHide).toBe(true);
  });

  it('points the child at the process log file, never at pipes (MS-04)', async () => {
    // A server kept running at quit would otherwise write into pipes whose
    // reader is gone; whether it will be kept is decided long after this.
    const controller = build();

    const started = controller.start();
    await vi.advanceTimersByTimeAsync(0);
    harness.child.say('INFO: Uvicorn running');
    harness.child.complain('WARNING: from stderr');
    await started;

    // Inherited descriptors (the fake process holds 5) are replaced with /dev/null.
    expect(harness.spawns[0]!.options.stdio).toEqual(['ignore', PROCESS_LOG_FD, PROCESS_LOG_FD, NULL_FD, NULL_FD, NULL_FD]);
    // The shell's copies are closed once the child holds its own.
    expect(harness.processLogCalls).toEqual(['open', `close:${PROCESS_LOG_FD}`, `close:${NULL_FD}`]);
    // Followed from the start of the freshly rotated file, into the run log.
    expect(harness.tails).toHaveLength(1);
    expect(harness.tails[0]!.options).toEqual({ fromEnd: false });
    expect(harness.log.lines).toContain('INFO: Uvicorn running');
    expect(harness.log.lines).toContain('WARNING: from stderr');
  });

  it('tries the last local port first (MS-04)', async () => {
    const controller = buildWith({ preferredPort: () => 5_555 });

    await controller.start();

    expect(harness.preferredPorts).toEqual([5_555]);
    // The harness hands out DEFAULT_PORT: the preferred one was taken.
    expect(harness.log.lines.some((line) => line.includes('port 5555 is taken'))).toBe(true);
  });

  it('a restart after a fallback prefers the fallback port, not the launch-time one (MS-04)', async () => {
    // index.ts saves the bound port to settings.localPort at ready and
    // hands the controller a getter over it.
    let localPort: number | undefined = 5_555;
    const controller = buildWith({ preferredPort: () => localPort });
    controller.events.on('state', (state) => {
      if (state.name === 'ready') localPort = state.info.port;
    });

    await controller.start();
    harness.diesOnShutdown.add(DEFAULT_PID);
    const restarted = controller.restart();
    await vi.advanceTimersByTimeAsync(1_000);
    await restarted;

    expect(harness.preferredPorts).toEqual([5_555, DEFAULT_PORT]);
  });

  it('walks up to the repo root and resolves a local: path either way round', () => {
    const repo = fakeRepo();
    expect(findRepoRoot(path.join(repo, 'desktop'))).toBe(repo);
    expect(findRepoRoot(repo)).toBe(repo);
    expect(findRepoRoot(path.parse(repo).root)).toBeNull();
    expect(resolveCheckout(repo)).toBe(repo);
    expect(resolveCheckout(path.join(repo, 'api'))).toBe(repo);
    expect(resolveCheckout(` ${path.join(repo, 'api')} `)).toBe(repo);
  });
});

// ---------------------------------------------------------------------------
// version skew
// ---------------------------------------------------------------------------

describe('the bridge handshake', () => {
  it('warns loudly but keeps going when the server speaks another bridge', async () => {
    const controller = build();
    harness.healthBody = { status: 'ok', version: '9.0.0', bridge: 99 };

    const info = await controller.start();

    expect(info.serverVersion).toBe('9.0.0');
    expect(controller.state.name).toBe('ready');
    const warning = harness.log.lines.find((line) => line.includes('WARNING: server speaks bridge'));
    expect(warning).toContain(`server speaks bridge 99, this shell implements ${BRIDGE_PROTOCOL}`);
    expect(warning).toContain('Some desktop features may be unavailable.');
  });

  it('says nothing when the bridge matches', async () => {
    const controller = build();
    await controller.start();
    expect(harness.log.lines.some((line) => line.includes('WARNING'))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// failure paths
// ---------------------------------------------------------------------------

describe('failing to start', () => {
  it('reports uvMissing with every place it looked', async () => {
    const controller = build();
    harness.uv = null;

    const state = await failedStart(controller, 0);

    expect(state.reason).toBe('uvMissing');
    expect(state.message).toContain(harness.checkout);
    expect(state.message).toContain('needs uv');
    expect(state.command).toBeNull();
    for (const attempt of uvAttempts({}))
      expect(harness.log.lines).toContain(`[shell] looked for uv at: ${attempt}`);
    expect(harness.log.lines).toContain('[shell] looked for uv at: uv on PATH');
    expect(harness.spawns).toHaveLength(0);
  });

  it('reports spawnFailed with the command line when the exec throws__P1F02', async () => {
    const controller = build();
    harness.spawnThrows = Object.assign(new Error('spawn uv ENOENT'), { code: 'ENOENT' });

    const state = await failedStart(controller, 0);

    expect(state.reason).toBe('spawnFailed');
    expect(state.message).toContain('spawn uv ENOENT');
    expect(state.command).toContain('python -m collimate_server');
  });

  it('reports spawnFailed when the process never gets a pid', async () => {
    const controller = build();
    harness.spawnPid = undefined;

    const state = await failedStart(controller, 0);

    expect(state.reason).toBe('spawnFailed');
    expect(state.message).toContain('the process never started');
  });

  it('gives up after the 90 second budget and keeps the log tail', async () => {
    const controller = build();
    harness.health = 'down';
    const startedStates = trace(controller);

    const pending = controller.start().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    harness.child.complain('Traceback (most recent call last):');
    harness.child.complain('ModuleNotFoundError: No module named "collimate"');

    // Still trying at 89s…
    await vi.advanceTimersByTimeAsync(HEALTH_BUDGET_MS - 1_000);
    expect(controller.state.name).toBe('waitingHealth');
    // …and given up shortly after 90.
    await vi.advanceTimersByTimeAsync(2_000);

    const error = await pending;
    expect(error).toBeInstanceOf(ServerStartError);
    const state = (error as ServerStartError).state;
    expect(state.reason).toBe('healthTimeout');
    expect(state.message).toContain('/health within 90 seconds');
    expect(state.tail).toContain('ModuleNotFoundError: No module named "collimate"');
    expect(startedStates.at(-1)).toMatchObject({ name: 'failed', reason: 'healthTimeout' });
  });

  it('reports healthTimeout differently when /health answers but /ready never does', async () => {
    const controller = build();
    harness.ready = 'down';

    const state = await failedStart(controller);

    expect(state.reason).toBe('healthTimeout');
    expect(state.message).toContain('never became ready');
  });

  it('reports earlyExit when the child dies during the health wait', async () => {
    const controller = build();
    harness.health = 'down';

    const pending = controller.start().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    harness.child.say('address already in use');
    harness.child.exit(1, null);
    await vi.advanceTimersByTimeAsync(1_000);

    const state = ((await pending) as ServerStartError).state;
    expect(state.reason).toBe('earlyExit');
    expect(state.message).toContain('exit code 1');
    expect(state.tail).toContain('address already in use');
  });

  it('reports earlyExit when the child is killed during the health wait', async () => {
    const controller = build();
    harness.health = 'down';

    const pending = controller.start().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    harness.child.exit(null, 'SIGKILL');
    await vi.advanceTimersByTimeAsync(1_000);

    const state = ((await pending) as ServerStartError).state;
    expect(state.reason).toBe('earlyExit');
    expect(state.message).toContain('killed by SIGKILL');
  });

  it('stops a timed-out child before the next start() rather than failing on its exit', async () => {
    const controller = build();
    harness.health = 'down';
    await failedStart(controller);
    const first = harness.child;
    expect(first.exited).toBe(false);

    harness.health = 'ok';
    harness.alive.add(DEFAULT_PID);
    harness.onSignal = (_pid, signal) => {
      if (signal !== 'SIGTERM') return;
      harness.alive.delete(DEFAULT_PID);
      first.exit(null, 'SIGTERM');
    };
    const started = controller.start();
    await vi.advanceTimersByTimeAsync(1_000);
    const info = await started;

    expect(controller.state).toEqual({ name: 'ready', info });
    expect(harness.kills).toEqual([{ pid: groupPid, signal: 'SIGTERM' }]);
    expect(harness.spawns).toHaveLength(2);
  });

  it('leaves no server running when a timed-out start is stopped', async () => {
    const controller = build();
    harness.health = 'down';
    await failedStart(controller);
    expect(harness.record()).not.toBeNull();
    harness.onSignal = (_pid, signal) => {
      if (signal === 'SIGTERM') harness.child.exit(null, 'SIGTERM');
    };

    const stopping = controller.stop();
    await vi.advanceTimersByTimeAsync(1_000);
    await stopping;

    expect(harness.kills).toEqual([{ pid: groupPid, signal: 'SIGTERM' }]);
    expect(controller.state).toEqual({ name: 'stopped' });
    expect(harness.record()).toBeNull();
  });

  it('caps a single probe so a wedged socket cannot park the budget', async () => {
    const controller = build();
    // Accepts the connection, then says nothing at all — the case a plain
    // `await fetch()` would sit on until the user force-quit.
    harness.health = 'hang';

    const state = await failedStart(controller);

    expect(state.reason).toBe('healthTimeout');
    // 90s of budget at a 5s ceiling plus a 250ms poll gap: ~17 attempts.
    // The point is that it is bounded and small, not 360 (the poll rate)
    // and not 1 (parked forever).
    expect(harness.aborts).toBe(harness.hangs);
    expect(harness.hangs).toBeGreaterThan(10);
    expect(harness.hangs).toBeLessThan(25);
  });
});

// ---------------------------------------------------------------------------
// stopping
// ---------------------------------------------------------------------------

describe('stopping', () => {
  it('asks over HTTP and sends no signals when the server goes quietly', async () => {
    const controller = build();
    await controller.start();
    harness.shutdownExitDelayMs = 200;

    const stopping = controller.stop();
    await vi.advanceTimersByTimeAsync(1_000);
    await stopping;

    expect(harness.fetches.at(-1)).toEqual({
      url: `${harness.url}/api/system/shutdown`,
      method: 'POST',
    });
    expect(harness.kills).toEqual([]);
    expect(controller.state).toEqual({ name: 'stopped' });
    expect(controller.info).toBeNull();
    expect(harness.record()).toBeNull();
  });

  it('escalates to SIGTERM on the process group when the polite ask times out', async () => {
    const controller = build();
    await controller.start();
    harness.shutdownExitDelayMs = null;
    // Dies to SIGTERM, so the ladder should stop there.
    harness.onSignal = (_pid, signal) => {
      if (signal === 'SIGTERM') harness.child.exit(null, 'SIGTERM');
    };

    const stopping = controller.stop();
    await vi.advanceTimersByTimeAsync(40_000);
    await stopping;

    expect(harness.kills).toEqual([{ pid: groupPid, signal: 'SIGTERM' }]);
    expect(controller.state).toEqual({ name: 'stopped' });
  });

  it('escalates SIGTERM then SIGKILL, both to the group, for a server that ignores both', async () => {
    const controller = build();
    await controller.start();
    harness.shutdownExitDelayMs = null;

    const stopping = controller.stop();
    const phases: string[] = [];
    controller.events.on('state', (state) => {
      if (state.name === 'quitting') phases.push(state.phase);
    });
    await vi.advanceTimersByTimeAsync(60_000);
    await stopping;

    expect(harness.kills).toEqual([
      { pid: groupPid, signal: 'SIGTERM' },
      { pid: groupPid, signal: 'SIGKILL' },
    ]);
    // Negative pid is the whole point: uv's uvicorn grandchild holds the
    // port, and only the group signal reaches it.
    expect(harness.kills.every((call) => call.pid === groupPid)).toBe(true);
    expect(phases).toEqual(['sigterm', 'sigkill']);
    expect(controller.state).toEqual({ name: 'stopped' });
  });

  it('respects COLLIMATE_APP_SHUTDOWN_TIMEOUT', async () => {
    const controller = build({ env: { COLLIMATE_APP_SHUTDOWN_TIMEOUT: '2' } });
    await controller.start();
    harness.shutdownExitDelayMs = null;

    const stopping = controller.stop();
    // The default is 30s; at 2.5s an unmodified controller is still waiting.
    await vi.advanceTimersByTimeAsync(2_500);
    expect(harness.kills).toEqual([{ pid: groupPid, signal: 'SIGTERM' }]);

    await vi.advanceTimersByTimeAsync(20_000);
    await stopping;
  });

  it('ignores a nonsense COLLIMATE_APP_SHUTDOWN_TIMEOUT and uses the default', async () => {
    const controller = build({ env: { COLLIMATE_APP_SHUTDOWN_TIMEOUT: 'soon' } });
    await controller.start();
    harness.shutdownExitDelayMs = null;

    const stopping = controller.stop();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(harness.kills).toEqual([]);

    await vi.advanceTimersByTimeAsync(40_000);
    await stopping;
    expect(harness.kills[0]).toEqual({ pid: groupPid, signal: 'SIGTERM' });
  });

  it('goes straight to signals when quitting before the server was ever ready', async () => {
    const controller = build();
    harness.health = 'down';
    const pending = controller.start().catch(() => undefined);
    await vi.advanceTimersByTimeAsync(0);

    const stopping = controller.stop();
    await vi.advanceTimersByTimeAsync(10_000);

    // No shutdown endpoint exists on a server that never came up, so we
    // don't sit out the graceful window waiting for one.
    expect(harness.fetches.some((call) => call.method === 'POST')).toBe(false);
    expect(harness.kills[0]).toEqual({ pid: groupPid, signal: 'SIGTERM' });
    await vi.advanceTimersByTimeAsync(90_000);
    await stopping;
    await pending;
  });

  it('still stops when the shutdown request itself fails', async () => {
    const controller = build();
    await controller.start();
    harness.shutdownFails = true;

    const stopping = controller.stop();
    await vi.advanceTimersByTimeAsync(60_000);
    await stopping;

    expect(harness.log.lines.some((line) => line.includes('graceful shutdown request failed'))).toBe(true);
    expect(harness.kills[0]).toEqual({ pid: groupPid, signal: 'SIGTERM' });
    expect(controller.state).toEqual({ name: 'stopped' });
  });

  it('is a no-op when there is nothing running', async () => {
    const controller = build();

    await controller.stop();

    expect(harness.kills).toEqual([]);
    expect(harness.fetches).toEqual([]);
    expect(controller.state).toEqual({ name: 'stopped' });
  });
});

// ---------------------------------------------------------------------------
// crash and restart
// ---------------------------------------------------------------------------

describe('crashing', () => {
  it('moves to crashed and hands out the tail when the server dies under a live window', async () => {
    const controller = build();
    const crashes: { code: number | null; signal: NodeJS.Signals | null; tail: string[] }[] = [];
    controller.events.on('crashed', (event) => crashes.push(event));
    await controller.start();

    harness.child.complain('sqlite3.OperationalError: database is locked');
    harness.child.exit(3, null);

    expect(controller.state).toMatchObject({ name: 'crashed', code: 3, signal: null });
    expect(controller.info).toBeNull();
    expect(crashes).toHaveLength(1);
    expect(crashes[0]!.tail).toContain('sqlite3.OperationalError: database is locked');
    // The record goes with it: there is no orphan to reap next launch.
    expect(harness.removed).toContain(harness.recordPath);
  });

  it('does not report a crash for a child that exits after a clean stop', async () => {
    const controller = build();
    const crashes: unknown[] = [];
    controller.events.on('crashed', (event) => crashes.push(event));
    await controller.start();
    harness.shutdownExitDelayMs = 100;

    const stopping = controller.stop();
    await vi.advanceTimersByTimeAsync(1_000);
    await stopping;

    expect(crashes).toEqual([]);
  });

  it('respawns on restart() after a crash', async () => {
    const controller = build();
    const first = await controller.start();
    harness.child.exit(1, null);
    expect(controller.state.name).toBe('crashed');

    harness.port = 53220;
    const second = await controller.restart();

    expect(harness.spawns).toHaveLength(2);
    expect(second.port).toBe(53220);
    expect(second.url).not.toBe(first.url);
    expect(controller.state).toEqual({ name: 'ready', info: second });
    // A crashed child is already gone; restarting must not signal it.
    expect(harness.kills).toEqual([]);
  });

  it('stops the old server before starting a new one on restart()', async () => {
    const controller = build();
    await controller.start();
    harness.shutdownExitDelayMs = 100;

    const restarting = controller.restart();
    await vi.advanceTimersByTimeAsync(1_000);
    await restarting;

    expect(harness.fetches.filter((call) => call.method === 'POST')).toHaveLength(1);
    expect(harness.spawns).toHaveLength(2);
  });

  it('ignores a late exit from a child that restart() already replaced', async () => {
    const controller = build();
    await controller.start();
    const first = harness.child;
    // Ignores the shutdown request, SIGTERM and SIGKILL alike.
    harness.shutdownExitDelayMs = null;

    const restarting = controller.restart();
    await vi.advanceTimersByTimeAsync(60_000);
    const info = await restarting;
    first.exit(null, 'SIGKILL');

    expect(harness.spawns).toHaveLength(2);
    expect(controller.state).toEqual({ name: 'ready', info });
  });
});


// ---------------------------------------------------------------------------
// the server record, adoption and keep-alive
// ---------------------------------------------------------------------------

describe('the server record', () => {
  /** A bare record a crashed shell left this boot: stamped a minute ago. */
  const orphanRecord = (startedAt = Date.now() - 60_000) => ({
    pid: 9182,
    port: 5001,
    url: 'http://127.0.0.1:5001',
    startedAt,
  });

  it('records the pid, port, url, version, shell version, spec and data dir (MS-04)', async () => {
    const repo = fakeRepo();
    const controller = build({ appPath: path.join(repo, 'desktop') });
    harness.readyAfterMs = 1_000;
    const started = controller.start();
    await vi.advanceTimersByTimeAsync(0);
    // Written at the spawn, before the server is ready to say which version it is.
    expect(harness.record()).toMatchObject({ pid: DEFAULT_PID, version: null });
    await vi.advanceTimersByTimeAsync(2_000);
    await started;

    const record = harness.record();
    expect(record).toEqual({
      pid: DEFAULT_PID,
      port: DEFAULT_PORT,
      url: `http://127.0.0.1:${DEFAULT_PORT}`,
      startedAt: expect.any(Number),
      version: '0.1.0',
      shellVersion: '0.1.0',
      // A checkout's spec carries its git HEAD: its code can change under
      // an unchanged command line and version.
      spec: `${commandLine(harness.spawns[0]!)} @ abc123`,
      dataDir: harness.options.dataDir,
    });
    // Written through the injected fs alone: nothing touched the real disk.
    expect(existsSync(harness.options.runtimeDir)).toBe(false);
  });

  it("records a checkout's spec with its git HEAD, or says there is none", async () => {
    await build().start();
    expect(harness.record()?.['spec']).toBe(`${commandLine(harness.spawns[0]!)} @ abc123`);
    harness.cleanup();

    const controller = build();
    harness.gitHead = null;
    await controller.start();
    expect(harness.record()?.['spec']).toBe(`${commandLine(harness.spawns[0]!)} @ no git HEAD`);
  });

  it("records a package's spec with the hash of the server environment it runs", async () => {
    await build({ isPackaged: true, bundledServer: {} }).start();
    expect(harness.record()?.['spec']).toBe(`${commandLine(harness.spawns[0]!)} @ server ${SERVER_HASH}`);
  });

  it('stops a non-adoptable server left behind by a previous run before spawning (MS-04)', async () => {
    const controller = build();
    harness.seedRecord(orphanRecord());
    harness.alive.add(9182);
    harness.instance = 'down';
    let spawnsAtSignal = -1;
    harness.onSignal = (_pid, signal) => {
      spawnsAtSignal = harness.spawns.length;
      if (signal === 'SIGTERM') harness.alive.delete(9182);
    };

    const started = controller.start();
    await vi.advanceTimersByTimeAsync(1_000);
    await started;

    // Silent, but its command line names this data dir: a wedged server
    // of ours, put down by signals only, with no shutdown POST.
    expect(harness.kills).toEqual([{ pid: -9182, signal: 'SIGTERM' }]);
    expect(harness.fetches.some((call) => call.method === 'POST')).toBe(false);
    // Gone before anything was spawned: the data dir's lock is free.
    expect(spawnsAtSignal).toBe(0);
    expect(harness.log.lines.some((line) => line.includes("found a server from a previous run (pid 9182) that can't be adopted"))).toBe(true);
    // Replaced by this run's own record, not left pointing at a dead pid.
    expect(harness.record()).toMatchObject({ pid: DEFAULT_PID });
  });

  it('escalates to SIGKILL for a recorded server that ignores SIGTERM, and waits for it (MS-04)', async () => {
    const controller = build();
    harness.seedRecord(orphanRecord());
    harness.alive.add(9182);
    harness.instance = 'down';
    harness.onSignal = (_pid, signal) => {
      if (signal === 'SIGKILL') setTimeout(() => harness.alive.delete(9182), 1_000);
    };

    const started = controller.start();
    await vi.advanceTimersByTimeAsync(SIGTERM_GRACE_MS + 3_000);
    await started;

    expect(harness.kills.map((call) => call.signal)).toEqual(['SIGTERM', 'SIGKILL']);
    expect(harness.log.lines.some((line) => line.includes('ignored SIGTERM; SIGKILL'))).toBe(true);
    expect(harness.spawns).toHaveLength(1);
  });

  it('drops a record whose pid is gone without signalling anything', async () => {
    const controller = build();
    harness.seedRecord(orphanRecord());
    // Not in `alive`: kill(pid, 0) throws, exactly like a dead pid.

    await controller.start();

    expect(harness.kills).toEqual([]);
    expect(harness.removed).toContain(harness.recordPath);
    expect(harness.record()).toMatchObject({ pid: DEFAULT_PID });
  });

  /**
   * Start with a bad record on disk, the clock run past a whole reap (the
   * SIGTERM grace and the SIGKILL wait), so a record that wrongly got
   * through fails on the signals it sent rather than on a test timeout.
   */
  async function startPastReap(controller: ServerController): Promise<void> {
    const started = controller.start();
    await vi.advanceTimersByTimeAsync(SIGTERM_GRACE_MS + 6_000 + 2 * HEALTH_POLL_MS);
    await started;
  }

  // Every pid-invalid record is stamped this boot and its pid is live, so
  // the pid guard alone is what drops it: -pid for pid 0 is the shell's own
  // group, and for pid 1 or a negative pid, kill() reaches far wider.
  const thisBoot = () => Date.now() - 60_000;
  it.each([
    ['not JSON at all', () => 'not json {'],
    ['JSON that is not an object', () => '"nope"'],
    ['a record with no pid', () => JSON.stringify({ port: 5001, startedAt: thisBoot() })],
    ['a record whose pid is a string', () => JSON.stringify({ pid: '9182', startedAt: thisBoot() })],
    ['a record whose pid is not an integer', () => JSON.stringify({ pid: 9182.5, startedAt: thisBoot() })],
    ['a record claiming pid 1', () => JSON.stringify({ pid: 1, startedAt: thisBoot() })],
    ['a record claiming pid 0', () => JSON.stringify({ pid: 0, startedAt: thisBoot() })],
    ['a record claiming a negative pid', () => JSON.stringify({ pid: -9182, startedAt: thisBoot() })],
  ])('drops %s', async (_label, contents) => {
    const controller = build();
    harness.seedRecord(contents());
    for (const pid of [0, 1, 9182, -9182]) harness.alive.add(pid);

    await startPastReap(controller);

    expect(harness.kills).toEqual([]);
    expect(harness.removed).toContain(harness.recordPath);
  });

  it('signals nothing for a live pid when the record predates this boot__F0984', async () => {
    const controller = build();
    // Written before the machine rebooted: the pid now belongs to whatever
    // the kernel handed it to — a shell, say, whose group -pid would reach.
    harness.seedRecord(orphanRecord(harness.bootTimeMs - BOOT_SLACK_MS - 1));
    harness.alive.add(9182);

    await startPastReap(controller);

    expect(harness.kills).toEqual([]);
    expect(harness.removed).toContain(harness.recordPath);
    expect(harness.log.lines.some((line) => line.includes('predates this boot'))).toBe(true);
    // Nothing was asked of whatever now answers on the recorded port.
    expect(harness.fetches.some((call) => call.url.startsWith('http://127.0.0.1:5001'))).toBe(false);
    expect(harness.record()).toMatchObject({ pid: DEFAULT_PID });
  });

  it('still settles a record stamped within the slack before the computed boot__F0984', async () => {
    const controller = build();
    harness.seedRecord(orphanRecord(harness.bootTimeMs - BOOT_SLACK_MS + 1_000));
    harness.alive.add(9182);
    harness.instance = 'down';
    harness.onSignal = (_pid, signal) => {
      if (signal === 'SIGTERM') harness.alive.delete(9182);
    };

    const started = controller.start();
    await vi.advanceTimersByTimeAsync(SIGTERM_GRACE_MS);
    await started;

    expect(harness.kills).toEqual([{ pid: -9182, signal: 'SIGTERM' }]);
  });

  it('signals nothing for a live pid when the record is stamped past now__F0984', async () => {
    const controller = build();
    // A clock that ran ahead before the reboot stamps a pre-boot record
    // after the computed boot time; this boot's records are never ahead of now.
    harness.seedRecord(orphanRecord(Date.now() + BOOT_SLACK_MS + 1));
    harness.alive.add(9182);

    await startPastReap(controller);

    expect(harness.kills).toEqual([]);
    expect(harness.removed).toContain(harness.recordPath);
    expect(harness.log.lines.some((line) => line.includes('stamped in the future'))).toBe(true);
    expect(harness.record()).toMatchObject({ pid: DEFAULT_PID });
  });

  it('still settles a record stamped within the slack after now__F0984', async () => {
    const controller = build();
    harness.seedRecord(orphanRecord(Date.now() + BOOT_SLACK_MS - 1_000));
    harness.alive.add(9182);
    harness.instance = 'down';
    harness.onSignal = (_pid, signal) => {
      if (signal === 'SIGTERM') harness.alive.delete(9182);
    };

    await startPastReap(controller);

    expect(harness.kills).toEqual([{ pid: -9182, signal: 'SIGTERM' }]);
  });

  it.each([
    ['missing', undefined],
    ['null', null],
    ['a string', '1700000000000'],
  ])('drops a record whose startedAt is %s without signalling its live pid__F0984', async (_label, startedAt) => {
    const controller = build();
    harness.seedRecord(JSON.stringify({ pid: 9182, port: 5001, url: 'http://127.0.0.1:5001', startedAt }));
    harness.alive.add(9182);

    await startPastReap(controller);

    expect(harness.kills).toEqual([]);
    expect(harness.removed).toContain(harness.recordPath);
  });

  it('computes the default boot time in milliseconds__F0984', () => {
    // A seconds/ms slip would put the boot at about now, and every orphan
    // older than BOOT_SLACK_MS would then be left running.
    vi.useRealTimers();
    const bootMs = machineBootTimeMs();
    expect(Math.abs(Date.now() - os.uptime() * 1_000 - bootMs)).toBeLessThan(5_000);
    expect(bootMs).toBeLessThan(Date.now());
    // Linux keeps the boot time on its own line: an independent source.
    let btime: number | null = null;
    try {
      const line = readFileSync('/proc/stat', 'utf8').match(/^btime (\d+)$/m);
      btime = line ? Number(line[1]) * 1_000 : null;
    } catch {
      // Not Linux.
    }
    if (btime !== null) expect(Math.abs(bootMs - btime)).toBeLessThan(BOOT_SLACK_MS);
  });

  it('clears the record on a clean stop', async () => {
    const controller = build();
    await controller.start();
    expect(harness.record()).not.toBeNull();
    harness.shutdownExitDelayMs = 0;

    const stopping = controller.stop();
    await vi.advanceTimersByTimeAsync(1_000);
    await stopping;

    expect(harness.record()).toBeNull();
    expect(harness.liveTails).toBe(0);
  });
});

describe('keep-alive: release and adoption (MS-04)', () => {
  /**
   * Launch one: start a server and release it at quit. Returns the record
   * it left. The released child stays "alive" for the next launch.
   */
  async function releasedServer(options: HarnessOptions = {}): Promise<Record<string, unknown>> {
    const first = build(options);
    await first.start();
    harness.alive.add(DEFAULT_PID);
    first.release();
    const record = harness.record();
    expect(record).not.toBeNull();
    return record!;
  }

  /** Launch two, against the same disk and processes. */
  function nextLaunch(extra: Partial<ServerControllerOptions> = {}): ServerController {
    return new ServerController({ ...harness.options, ...extra, env: { ...harness.options.env, ...extra.env } });
  }

  it('release keeps server.json, stops following the log and lets the process go (MS-04)', async () => {
    const controller = build();
    const crashes: unknown[] = [];
    controller.events.on('crashed', (event) => crashes.push(event));
    await controller.start();
    const child = harness.child;

    controller.release();

    expect(controller.state).toEqual({ name: 'released' });
    expect(controller.info).toBeNull();
    expect(harness.record()).toMatchObject({ pid: DEFAULT_PID, version: '0.1.0' });
    expect(harness.liveTails).toBe(0);
    expect(child.unrefs).toBe(1);
    // Nothing that follows may stop it: not a stop()…
    await controller.stop();
    expect(harness.fetches.some((call) => call.method === 'POST')).toBe(false);
    expect(harness.kills).toEqual([]);
    expect(harness.record()).not.toBeNull();
    // …and not its own later exit, which is no crash of ours.
    child.exit(0, null);
    expect(crashes).toEqual([]);
    expect(harness.record()).not.toBeNull();
  });

  it('release is a no-op for a server that is not running', async () => {
    const controller = build();
    controller.release();
    expect(controller.state).toEqual({ name: 'idle' });
  });

  it('the next launch adopts the same server: no spawn, same url, followed from the end (MS-04)', async () => {
    await releasedServer();
    const fetchesBefore = harness.fetches.length;
    const second = nextLaunch();

    const info = await second.start();

    expect(harness.spawns).toHaveLength(1);
    expect(info).toEqual({
      id: 'local',
      kind: 'local',
      name: 'Local',
      url: `http://127.0.0.1:${DEFAULT_PORT}`,
      port: DEFAULT_PORT,
      dataDir: harness.options.dataDir,
      serverVersion: '0.1.0',
    });
    expect(second.state).toEqual({ name: 'ready', info });
    expect(second.isAdopted).toBe(true);
    expect(harness.fetches.slice(fetchesBefore).map((call) => call.url)).toEqual([
      `${harness.url}/api/system/instance`,
      `${harness.url}/health`,
    ]);
    expect(harness.tails.at(-1)?.options).toEqual({ fromEnd: true });
    expect(harness.kills).toEqual([]);
    expect(harness.log.lines.some((line) => line.includes('adopted the server left running'))).toBe(true);
  });

  it.each([
    ['its /health version differs', () => {
      harness.healthBody = { status: 'ok', version: '0.2.0', bridge: BRIDGE_PROTOCOL };
    }, {}],
    ['its bridge differs', () => {
      harness.healthBody = { status: 'ok', version: '0.1.0', bridge: BRIDGE_PROTOCOL + 1 };
    }, {}],
    ['the shell version differs', () => {
      harness.seedRecord({ ...harness.record()!, shellVersion: '0.0.9' });
    }, {}],
    ['the spawn spec differs', () => {}, { env: { COLLIMATE_SERVER_SPEC: 'local:/elsewhere/checkout' } }],
    ['the recorded data dir differs', () => {
      harness.seedRecord({ ...harness.record()!, dataDir: '/elsewhere' });
    }, {}],
  ] as const)('restarts the server instead of adopting it when %s (MS-04)', async (_label, change, extra) => {
    await releasedServer();
    change();
    harness.diesOnShutdown.add(DEFAULT_PID);
    const second = nextLaunch(extra);

    const started = second.start();
    await vi.advanceTimersByTimeAsync(2_000);
    await started;

    // Ours (the instance check says so): asked over HTTP, no signals.
    const posts = harness.fetches.filter((call) => call.method === 'POST');
    expect(posts.map((call) => call.url)).toEqual([`${harness.url}/api/system/shutdown`]);
    expect(harness.kills).toEqual([]);
    expect(harness.spawns).toHaveLength(2);
    expect(second.isAdopted).toBe(false);
    expect(harness.log.lines.some((line) => line.includes("can't be adopted"))).toBe(true);
  });

  it('restarts a checkout whose git HEAD moved (MS-04)', async () => {
    const repo = fakeRepo();
    await releasedServer({ appPath: path.join(repo, 'desktop') });
    harness.gitHead = 'def456';
    harness.diesOnShutdown.add(DEFAULT_PID);

    const started = nextLaunch().start();
    await vi.advanceTimersByTimeAsync(2_000);
    await started;

    expect(harness.spawns).toHaveLength(2);
    expect(harness.log.lines.some((line) => line.includes('the server spec changed'))).toBe(true);
  });

  it('never adopts, asks to shut down or signals a pid when another data dir answers on its port (MS-04)', async () => {
    await releasedServer();
    // The port belongs to some other Collimate now: ours, alive, would
    // still hold it, so the recorded pid was recycled too.
    harness.instanceBody = { pid: 1, data_dir: '/home/someone/else', instance: 'x', version: '0.1.0', bridge: BRIDGE_PROTOCOL };

    const started = nextLaunch().start();
    await vi.advanceTimersByTimeAsync(2_000);
    await started;

    expect(harness.fetches.some((call) => call.method === 'POST')).toBe(false);
    expect(harness.kills).toEqual([]);
    expect(harness.log.lines.some((line) => line.includes("isn't ours") && line.includes('without signalling'))).toBe(true);
    expect(harness.spawns).toHaveLength(2);
    expect(harness.record()).toMatchObject({ pid: DEFAULT_PID });
  });

  it.each([
    ['names another data dir', (_pid: number) => 'uv run python -m collimate_server --port 5001 --data-dir /home/someone/else'],
    ['is some other program', (_pid: number) => '-bash'],
    ["can't be read", (_pid: number) => null],
  ])('signals nothing for a silent recorded port whose pid %s (MS-04)', async (_label, args) => {
    const controller = build();
    harness.seedRecord({ pid: 9182, port: 5001, url: 'http://127.0.0.1:5001', startedAt: Date.now() - 60_000 });
    harness.alive.add(9182);
    harness.instance = 'down';
    harness.processArgs = args;

    const started = controller.start();
    await vi.advanceTimersByTimeAsync(SIGTERM_GRACE_MS + 6_000);
    await started;

    expect(harness.kills).toEqual([]);
    expect(harness.removed).toContain(harness.recordPath);
    expect(harness.record()).toMatchObject({ pid: DEFAULT_PID });
  });

  it('waits for a replaced server to be gone before spawning the new one (MS-04)', async () => {
    await releasedServer();
    harness.seedRecord({ ...harness.record()!, shellVersion: '0.0.9' });
    // Accepts the shutdown but takes its time.
    harness.shutdownExitDelayMs = 3_000;
    harness.diesOnShutdown.add(DEFAULT_PID);

    const started = nextLaunch().start();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(harness.spawns).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(2_000);
    await started;

    expect(harness.spawns).toHaveLength(2);
  });

  it('stops an adopted server by polling its pid, with no child to wait on (MS-04)', async () => {
    await releasedServer();
    const second = nextLaunch();
    await second.start();
    harness.diesOnShutdown.add(DEFAULT_PID);
    harness.shutdownExitDelayMs = 500;

    const stopping = second.stop();
    await vi.advanceTimersByTimeAsync(1_000);
    await stopping;

    expect(harness.fetches.at(-1)).toEqual({ url: `${harness.url}/api/system/shutdown`, method: 'POST' });
    expect(harness.kills).toEqual([]);
    expect(second.state).toEqual({ name: 'stopped' });
    expect(harness.record()).toBeNull();
    expect(harness.liveTails).toBe(0);
  });

  it('escalates to signals for an adopted server that ignores the shutdown (MS-04)', async () => {
    await releasedServer({ env: { COLLIMATE_APP_SHUTDOWN_TIMEOUT: '1' } });
    const second = nextLaunch();
    await second.start();
    harness.onSignal = (_pid, signal) => {
      if (signal === 'SIGTERM') harness.alive.delete(DEFAULT_PID);
    };

    const stopping = second.stop();
    await vi.advanceTimersByTimeAsync(3_000);
    await stopping;

    expect(harness.kills).toEqual([{ pid: -DEFAULT_PID, signal: 'SIGTERM' }]);
    expect(second.state).toEqual({ name: 'stopped' });
  });

  it('the liveness watch reports an adopted server whose pid is gone as crashed (MS-04)', async () => {
    await releasedServer();
    const second = nextLaunch();
    const crashes: unknown[] = [];
    second.events.on('crashed', (event) => crashes.push(event));
    await second.start();

    harness.alive.delete(DEFAULT_PID);
    await vi.advanceTimersByTimeAsync(LIVENESS_POLL_MS);

    expect(crashes).toHaveLength(1);
    expect(second.state).toMatchObject({ name: 'crashed' });
    expect(second.info).toBeNull();
    expect(harness.record()).toBeNull();
  });

  it('the liveness watch reports a wedged adopted server and the next stop puts it down (MS-04)', async () => {
    await releasedServer();
    const second = nextLaunch();
    const crashes: unknown[] = [];
    second.events.on('crashed', (event) => crashes.push(event));
    await second.start();

    harness.health = 'down';
    await vi.advanceTimersByTimeAsync(LIVENESS_POLL_MS * LIVENESS_HEALTH_EVERY * LIVENESS_HEALTH_FAILURES + 1_000);

    expect(crashes).toHaveLength(1);
    // Still alive: its record stays so nothing else binds the data dir first.
    expect(harness.record()).not.toBeNull();
    harness.diesOnShutdown.add(DEFAULT_PID);
    const stopping = second.stop();
    await vi.advanceTimersByTimeAsync(1_000);
    await stopping;
    expect(harness.record()).toBeNull();
    expect(second.state).toEqual({ name: 'stopped' });
  });

  it('a stopped watch says nothing about a server it no longer owns', async () => {
    await releasedServer();
    const second = nextLaunch();
    const crashes: unknown[] = [];
    second.events.on('crashed', (event) => crashes.push(event));
    await second.start();
    second.release();

    harness.alive.delete(DEFAULT_PID);
    await vi.advanceTimersByTimeAsync(LIVENESS_POLL_MS * 3);

    expect(crashes).toEqual([]);
  });
});

describe('port allocation (MS-04)', () => {
  it('takes the preferred port when it is free, and an ephemeral one when it is taken (MS-04)', async () => {
    vi.useRealTimers();
    const free = await allocatePort();
    expect(await allocatePort(free)).toBe(free);

    const holder = net.createServer();
    await new Promise<void>((resolve) => holder.listen(0, '127.0.0.1', () => resolve()));
    try {
      const taken = (holder.address() as net.AddressInfo).port;
      const other = await allocatePort(taken);
      expect(other).not.toBe(taken);
      expect(other).toBeGreaterThan(0);
    } finally {
      await new Promise<void>((resolve) => holder.close(() => resolve()));
    }
  });

  it('points every inherited descriptor slot at /dev/null so a kept server holds none of the shell\'s (MS-04)', () => {
    expect(serverStdio(7, [0, 1, 2, 4, 9], 8)).toEqual(['ignore', 7, 7, 8, 8, 8, 8, 8, 8, 8]);
    expect(serverStdio(null, [0, 1, 2], 8)).toEqual(['ignore', 'ignore', 'ignore']);
    // Without /dev/null nothing can be replaced; stdio still goes to the file.
    expect(serverStdio(7, [0, 1, 2, 30], null)).toEqual(['ignore', 7, 7]);
    vi.useRealTimers();
    const fds = openFileDescriptors();
    expect(fds).toEqual(expect.arrayContaining([0, 1, 2]));
  });

  it("reads a live pid's command line, and nothing for a pid that is gone", () => {
    vi.useRealTimers();
    expect(processArgs(process.pid)).toContain(process.argv0.split('/').pop()!.slice(0, 4));
    expect(processArgs(2 ** 22 + 12_345)).toBeNull();
  });

  it('compares data dirs as paths, not strings', () => {
    expect(samePath('/a/b/', '/a/b')).toBe(true);
    expect(samePath('/a/b/../b', '/a/b')).toBe(true);
    expect(samePath('/a/b', '/a/c')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// a package: the server environment it carries
// ---------------------------------------------------------------------------

describe('a package runs the server environment it carries', () => {
  const hash16 = SERVER_HASH.slice(0, 16);
  const shipped = () => path.join(RESOURCES_PATH, SERVER_RESOURCE);
  const copiesDir = () => path.join(harness.options.runtimeDir, SERVER_COPIES_DIR);
  const copy = () => path.join(copiesDir(), hash16);
  const python = (root: string) => path.join(root, 'bin', 'python3');
  const packaged = (extra: HarnessOptions = {}): HarnessOptions => ({ isPackaged: true, bundledServer: {}, ...extra });

  it('runs its interpreter in place, isolated, in the data dir, naming the environment it runs from, without staging or a uv lookup', async () => {
    const controller = build(packaged());

    await controller.start();

    const call = harness.spawns[0]!;
    expect(call.command).toBe(python(shipped()));
    expect(call.args).toEqual([
      '-I',
      '-B',
      '-m',
      'collimate_server',
      '--port',
      String(DEFAULT_PORT),
      '--data-dir',
      harness.options.dataDir,
      '--server-env',
      shipped(),
    ]);
    expect(call.options.cwd).toBe(harness.options.dataDir);
    // The spec names the environment's hash: it is what a kept-alive server must match.
    expect(harness.record()?.['spec']).toBe(`${commandLine(call)} @ server ${SERVER_HASH}`);
    expect(harness.hashed).toEqual([shipped()]);
    expect(harness.stagedServers).toEqual([]);
    expect(harness.prunes).toEqual([]);
    expect(harness.uvLookups).toBe(0);
  });

  it('never looks for uv, and the splash never says it does', async () => {
    const controller = build(packaged());
    harness.uv = null;
    const states = trace(controller);

    await controller.start();

    expect(states.map((state) => state.name)).not.toContain('findingUv');
    expect(harness.uvLookups).toBe(0);
  });

  it.each([
    ['names another hash', 'f'.repeat(64), `is ${'f'.repeat(16)}, not the ${SERVER_HASH.slice(0, 16)}`],
    ['has no manifest', null, 'is missing or has no manifest'],
  ])('fails with bundleBroken, spawning nothing, when the shipped environment %s', async (_label, found, says) => {
    const controller = build(packaged());
    harness.shippedServerHash = found;

    const state = await failedStart(controller, 0);

    expect(state.reason).toBe('bundleBroken');
    expect(state.message).toContain(says);
    expect(state.message).toContain(shipped());
    expect(state.message).toContain('build/build_desktop.sh');
    expect(state.command).toBeNull();
    expect(harness.spawns).toEqual([]);
    expect(harness.record()).toBeNull();
    expect(harness.stagedServers).toEqual([]);
    expect(harness.prunes).toEqual([]);
  });

  it('still honours COLLIMATE_SERVER_SPEC=local:<checkout> over it, staging nothing', async () => {
    const repo = fakeRepo();
    await build(packaged({ env: { COLLIMATE_SERVER_SPEC: `local:${repo}` }, bundledServer: { stableCopy: true } })).start();

    expect(harness.spawns[0]!.args.slice(0, 3)).toEqual(['run', '--project', repo]);
    expect(harness.hashed).toEqual([]);
    expect(harness.stagedServers).toEqual([]);
    expect(harness.prunes).toEqual([]);
  });

  it('refuses another COLLIMATE_SERVER_SPEC rather than run the environment instead', async () => {
    const controller = build(packaged({ env: { COLLIMATE_SERVER_SPEC: 'collimate==9.9.9' } }));

    const state = await failedStart(controller, 0);

    expect(state.reason).toBe('spawnFailed');
    expect(state.message).toContain('local:');
    expect(harness.spawns).toEqual([]);
  });

  describe('from a stable copy under the runtime dir (an AppImage)', () => {
    const stable = (extra: HarnessOptions = {}) => packaged({ bundledServer: { stableCopy: true }, ...extra });

    it('runs the copy at server-environments/<hash16>, staged from the shipped tree before the spawn', async () => {
      const controller = build(stable());
      let spawnsWhenStaged = -1;
      harness.stageServer = () => {
        spawnsWhenStaged = harness.spawns.length;
        return Promise.resolve(true);
      };

      await controller.start();

      expect(harness.stagedServers).toEqual([{ source: shipped(), dest: copy(), hash: SERVER_HASH }]);
      expect(spawnsWhenStaged).toBe(0);
      expect(harness.spawns[0]!.command).toBe(python(copy()));
      // Its generator runs use the copy's uv and Python too: the mount's go with the shell.
      expect(harness.spawns[0]!.args.slice(-2)).toEqual(['--server-env', copy()]);
      expect(harness.spawns[0]!.options.cwd).toBe(harness.options.dataDir);
      expect(harness.record()?.['spec']).toBe(`${commandLine(harness.spawns[0]!)} @ server ${SERVER_HASH}`);
      expect(harness.log.lines).toContain(`[shell] server environment: ${copy()} (copied from ${shipped()})`);
    });

    it('says when the copy was already in place', async () => {
      const controller = build(stable());
      harness.stageServer = () => Promise.resolve(false);

      await controller.start();

      expect(harness.log.lines).toContain(`[shell] server environment: ${copy()} (already in place)`);
    });

    it('fails with bundleBroken, never falling back, when it cannot be copied', async () => {
      const controller = build(stable());
      harness.stageServer = () => Promise.reject(new Error('ENOSPC: no space left on device'));

      const state = await failedStart(controller, 0);

      expect(state.reason).toBe('bundleBroken');
      expect(state.message).toContain('ENOSPC');
      expect(state.message).toContain(copy());
      expect(harness.spawns).toEqual([]);
      expect(harness.record()).toBeNull();
      expect(harness.prunes).toEqual([]);
    });

    it("removes other builds' copies once the server is ready, and keeps its own", async () => {
      const controller = build(stable());
      harness.serverCopyDirs = [hash16, 'deadbeefdeadbeef'];
      let prunedBeforeReady = false;
      controller.events.on('state', (state) => {
        if (state.name !== 'ready' && harness.prunes.length) prunedBeforeReady = true;
      });

      await controller.start();

      expect(harness.prunes).toEqual([{ dir: copiesDir(), keep: [hash16] }]);
      expect(harness.serverCopyDirs).toEqual([hash16]);
      expect(prunedBeforeReady).toBe(false);
      expect(harness.log.lines).toContain("[shell] removed other builds' server environments: deadbeefdeadbeef");
    });

    it('keeps every copy when the server never comes up', async () => {
      const controller = build(stable());
      harness.serverCopyDirs = [hash16, 'deadbeefdeadbeef'];
      harness.health = 'down';

      await failedStart(controller);

      expect(harness.prunes).toEqual([]);
      expect(harness.serverCopyDirs).toEqual([hash16, 'deadbeefdeadbeef']);
    });

    it('checks the shipped environment before it copies anything', async () => {
      const controller = build(stable());
      harness.shippedServerHash = 'f'.repeat(64);

      const state = await failedStart(controller, 0);

      expect(state.reason).toBe('bundleBroken');
      expect(harness.stagedServers).toEqual([]);
    });
  });

  describe('and a server kept alive from an earlier launch', () => {
    async function releasedServer(options: HarnessOptions): Promise<void> {
      const first = build(options);
      await first.start();
      harness.alive.add(DEFAULT_PID);
      first.release();
      harness.diesOnShutdown.add(DEFAULT_PID);
    }

    /** The next launch: the same disk and processes, the package it now runs. */
    function nextLaunch(bundledServer: ServerControllerOptions['bundledServer']): ServerController {
      const { bundledServer: _previous, ...options } = harness.options;
      return new ServerController({
        ...options,
        isPackaged: bundledServer !== undefined,
        ...(bundledServer ? { bundledServer } : {}),
      });
    }

    async function launch(controller: ServerController): Promise<void> {
      const started = controller.start();
      await vi.advanceTimersByTimeAsync(2_000);
      await started;
    }

    it('is adopted when the spec, with the same hash, matches', async () => {
      await releasedServer(packaged());
      const second = nextLaunch({ resourcesPath: RESOURCES_PATH, hash: SERVER_HASH, stableCopy: false });

      await launch(second);

      expect(second.isAdopted).toBe(true);
      expect(harness.spawns).toHaveLength(1);
      expect(harness.kills).toEqual([]);
    });

    it('is adopted from another mount of the package when it runs from the stable copy', async () => {
      await releasedServer(packaged({ bundledServer: { stableCopy: true } }));
      const second = nextLaunch({ resourcesPath: '/tmp/.mount_CollimLTQTLM/resources', hash: SERVER_HASH, stableCopy: true });

      await launch(second);

      expect(second.isAdopted).toBe(true);
      expect(harness.spawns).toHaveLength(1);
    });

    it('is stopped and replaced when the hash differs', async () => {
      await releasedServer(packaged());
      const rebuilt = 'f'.repeat(64);
      harness.shippedServerHash = rebuilt;
      const second = nextLaunch({ resourcesPath: RESOURCES_PATH, hash: rebuilt, stableCopy: false });

      await launch(second);

      expect(second.isAdopted).toBe(false);
      expect(harness.fetches.filter((call) => call.method === 'POST').map((call) => call.url)).toEqual([
        `${harness.url}/api/system/shutdown`,
      ]);
      expect(harness.spawns).toHaveLength(2);
      expect(harness.log.lines.some((line) => line.includes('the server spec changed'))).toBe(true);
      expect(harness.record()?.['spec']).toContain(`@ server ${rebuilt}`);
    });

    it("is stopped and replaced when a checkout's server follows a package's, and the other way round", async () => {
      await releasedServer(packaged());
      await launch(nextLaunch(undefined));
      expect(harness.spawns).toHaveLength(2);
      expect(harness.log.lines.some((line) => line.includes('the server spec changed'))).toBe(true);
      harness.cleanup();

      await releasedServer({});
      await launch(nextLaunch({ resourcesPath: RESOURCES_PATH, hash: SERVER_HASH, stableCopy: false }));
      expect(harness.spawns).toHaveLength(2);
    });
  });
});

describe('stageServer, serverHash and pruneDir, on a real disk', () => {
  const HASH = 'ab'.repeat(32);
  const OTHER = 'cd'.repeat(32);

  function scratch(): string {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'collimate-server-env-'));
    tempDirs.push(dir);
    return dir;
  }

  /** A server environment: manifest, an interpreter, a library file and a symlink. */
  function environment(dir: string, hash: string = HASH): string {
    mkdirSync(path.join(dir, 'bin'), { recursive: true });
    mkdirSync(path.join(dir, 'lib'), { recursive: true });
    writeFileSync(path.join(dir, SERVER_MANIFEST), JSON.stringify({ hash }));
    writeFileSync(path.join(dir, 'bin', 'python3.12'), 'interpreter');
    symlinkSync('python3.12', path.join(dir, 'bin', 'python3'));
    writeFileSync(path.join(dir, 'lib', 'mod.py'), 'x = 1\n');
    return dir;
  }

  it('copies the tree, keeps a symlink a symlink and a file its mtime, and returns true', async () => {
    vi.useRealTimers();
    const dir = scratch();
    const source = environment(path.join(dir, 'resources', 'server'));
    const builtAt = new Date('2026-01-02T03:04:05Z');
    utimesSync(path.join(source, 'lib', 'mod.py'), builtAt, builtAt);
    const dest = path.join(dir, 'runtime', SERVER_COPIES_DIR, 'abababababababab');

    expect(await stageServer(source, dest, HASH)).toBe(true);

    expect(readFileSync(path.join(dest, 'bin', 'python3.12'), 'utf8')).toBe('interpreter');
    expect(lstatSync(path.join(dest, 'bin', 'python3')).isSymbolicLink()).toBe(true);
    expect(readlinkSync(path.join(dest, 'bin', 'python3'))).toBe('python3.12');
    expect(statSync(path.join(dest, 'lib', 'mod.py')).mtimeMs).toBe(builtAt.getTime());
    expect(await serverHash(dest)).toBe(HASH);
    // Nothing else beside it: no partial copy left behind.
    expect(readdirSync(path.dirname(dest))).toEqual(['abababababababab']);
  });

  it('returns false the next time, when dest holds the environment the hash names', async () => {
    vi.useRealTimers();
    const dir = scratch();
    const source = environment(path.join(dir, 'server'));
    const dest = path.join(dir, 'runtime', 'copy');

    expect(await stageServer(source, dest, HASH)).toBe(true);
    // Whatever is in place is left alone, even if the source changed since.
    writeFileSync(path.join(source, 'lib', 'mod.py'), 'x = 2\n');
    expect(await stageServer(source, dest, HASH)).toBe(false);
    expect(readFileSync(path.join(dest, 'lib', 'mod.py'), 'utf8')).toBe('x = 1\n');
  });

  it('replaces a dest that holds another environment', async () => {
    vi.useRealTimers();
    const dir = scratch();
    const source = environment(path.join(dir, 'server'));
    const dest = environment(path.join(dir, 'runtime', 'copy'), OTHER);

    expect(await stageServer(source, dest, HASH)).toBe(true);
    expect(await serverHash(dest)).toBe(HASH);
  });

  it('rejects, leaving nothing at dest nor a .partial sibling, when the source names another hash', async () => {
    vi.useRealTimers();
    const dir = scratch();
    const source = environment(path.join(dir, 'server'), OTHER);
    const dest = path.join(dir, 'runtime', 'copy');

    await expect(stageServer(source, dest, HASH)).rejects.toThrow(`not ${HASH}`);

    expect(existsSync(dest)).toBe(false);
    expect(readdirSync(path.dirname(dest))).toEqual([]);
  });

  it('rejects a source that is missing', async () => {
    vi.useRealTimers();
    const dir = scratch();
    const dest = path.join(dir, 'runtime', 'copy');

    await expect(stageServer(path.join(dir, 'absent'), dest, HASH)).rejects.toThrow();

    expect(existsSync(dest)).toBe(false);
    expect(readdirSync(path.dirname(dest))).toEqual([]);
  });

  it('serverHash reads a 64-hex hash from the manifest and nothing else', async () => {
    vi.useRealTimers();
    const dir = scratch();
    expect(await serverHash(path.join(dir, 'absent'))).toBeNull();
    expect(await serverHash(dir)).toBeNull();

    const manifest = path.join(dir, SERVER_MANIFEST);
    writeFileSync(manifest, JSON.stringify({ hash: HASH }));
    expect(await serverHash(dir)).toBe(HASH);

    for (const text of [
      'not json',
      '[]',
      'null',
      '{}',
      JSON.stringify({ hash: 7 }),
      JSON.stringify({ hash: 'a'.repeat(63) }),
      JSON.stringify({ hash: 'A'.repeat(64) }),
      JSON.stringify({ hash: 'z'.repeat(64) }),
    ]) {
      writeFileSync(manifest, text);
      expect(await serverHash(dir)).toBeNull();
    }
  });

  it('pruneDir removes every entry but the kept ones, and finds nothing in a missing dir', () => {
    vi.useRealTimers();
    const dir = scratch();
    for (const name of ['keep1', 'old', 'keep2']) mkdirSync(path.join(dir, name, 'nested'), { recursive: true });
    writeFileSync(path.join(dir, 'stray.partial'), '');

    expect(pruneDir(dir, ['keep1', 'keep2']).sort()).toEqual(['old', 'stray.partial']);
    expect(readdirSync(dir).sort()).toEqual(['keep1', 'keep2']);
    expect(pruneDir(path.join(dir, 'absent'), [])).toEqual([]);
  });
});

describe('location-owned refusal (plan 1105 phase 9)', () => {
  const REFUSAL = JSON.stringify({
    collimate_startup_refused: 'location_owned',
    path: '/home/me/Collimate/Channels',
    owner_data_dir: '/home/me/.config/Collimate',
    channels: 3,
  });

  /** A start whose child writes `lines` and exits with `code` during the health wait. */
  async function refusedStart(controller: ServerController, code: number, lines: string[]) {
    harness.health = 'down';
    const pending = controller.start().catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    for (const line of lines) harness.child.complain(line);
    harness.child.exit(code, null);
    await vi.advanceTimersByTimeAsync(1_000);
    const error = await pending;
    expect(error).toBeInstanceOf(ServerStartError);
    return (error as ServerStartError).state;
  }

  /** The user's answers, in order, and the dialogs they were asked. */
  function fakeUi(answers: number[], folders: (string | null)[] = []) {
    const asked: LocationOwnedDialog[] = [];
    const picks: unknown[] = [];
    return {
      asked,
      picks,
      ui: {
        ask: async (dialog: LocationOwnedDialog) => {
          asked.push(dialog);
          return answers.shift() ?? 2;
        },
        pickFolder: async (options: unknown) => {
          picks.push(options);
          return folders.shift() ?? null;
        },
      },
    };
  }

  /** The args after `--data-dir <dir>`: the channel-location flags. */
  function channelFlags(args: string[]): string[] {
    return args.slice(args.indexOf('--data-dir') + 2);
  }

  it('fails as locationOwned on exit 78 with the line, carrying what the line said', async () => {
    const controller = build();
    const state = await refusedStart(controller, 78, ['INFO: starting', REFUSAL, 'The channel location … belongs to another']);
    expect(state.reason).toBe('locationOwned');
    expect(state.locationOwned).toEqual({
      path: '/home/me/Collimate/Channels',
      ownerDataDir: '/home/me/.config/Collimate',
      channels: 3,
    });
    expect(state.message).toContain('/home/me/Collimate/Channels');
    expect(state.tail).toContain(REFUSAL);
  });

  it('is an ordinary earlyExit with the line but another code, or code 78 without the line', async () => {
    const withLine = await refusedStart(build(), 1, [REFUSAL]);
    expect(withLine.reason).toBe('earlyExit');
    expect(withLine).not.toHaveProperty('locationOwned');
    const withoutLine = await refusedStart(build(), 78, ['usage: collimate [-h] …']);
    expect(withoutLine.reason).toBe('earlyExit');
    const otherRefusal = await refusedStart(build(), 78, [
      JSON.stringify({ collimate_startup_refused: 'something_else', path: '/x', owner_data_dir: '/y', channels: 1 }),
    ]);
    expect(otherRefusal.reason).toBe('earlyExit');
  });

  it('a refusal line from an earlier spawn does not mark a later one', async () => {
    const controller = build();
    await refusedStart(controller, 78, [REFUSAL]);
    const later = await refusedStart(controller, 78, ['something else went wrong']);
    expect(later.reason).toBe('earlyExit');
  });

  it('Claim it asks with the location, owner and count, then respawns once with --claim-channels', async () => {
    const controller = build();
    const state = await refusedStart(controller, 78, [REFUSAL]);
    const { ui, asked } = fakeUi([0]);

    const override = await chooseLocationOwned(state.locationOwned!, ui);
    expect(asked).toEqual([
      {
        type: 'warning',
        title: 'Channel location in use',
        message:
          'The channel location /home/me/Collimate/Channels belongs to the Collimate installation at ' +
          '/home/me/.config/Collimate (3 channels).',
        detail: expect.stringContaining('Claim it') as unknown as string,
        buttons: ['Claim it', 'Choose another folder…', 'Quit'],
        defaultId: 0,
        cancelId: 2,
        noLink: true,
      },
    ]);
    expect(override).toEqual({ claimChannels: true });

    harness.health = 'ok';
    const started = controller.start(override!);
    await vi.advanceTimersByTimeAsync(0);
    await started;
    expect(harness.spawns).toHaveLength(2);
    expect(channelFlags(harness.spawns[0]!.args)).toEqual([]);
    expect(channelFlags(harness.spawns[1]!.args)).toEqual(['--claim-channels']);
    // The flag is this spawn's alone: the identity a later launch adopts
    // by leaves it out, and the next plain start passes nothing.
    expect(String(harness.record()?.['spec'])).not.toContain('--claim-channels');
    harness.child.exit(1, null);
    expect(controller.state.name).toBe('crashed');
    const again = controller.start();
    await vi.advanceTimersByTimeAsync(0);
    await again;
    expect(channelFlags(harness.spawns[2]!.args)).toEqual([]);
  });

  it('Choose another folder… respawns with --channels-dir <path>; a cancelled chooser asks again; Quit is null', async () => {
    const controller = build();
    const state = await refusedStart(controller, 78, [REFUSAL]);
    const { ui, asked, picks } = fakeUi([1, 1], [null, '/srv/channels']);

    const override = await chooseLocationOwned(state.locationOwned!, ui);
    expect(asked).toHaveLength(2);
    expect(picks).toHaveLength(2);
    expect(override).toEqual({ channelsDir: '/srv/channels' });

    harness.health = 'ok';
    const started = controller.start(override!);
    await vi.advanceTimersByTimeAsync(0);
    await started;
    expect(channelFlags(harness.spawns[1]!.args)).toEqual(['--channels-dir', '/srv/channels']);

    expect(await chooseLocationOwned(state.locationOwned!, fakeUi([2]).ui)).toBeNull();
  });

  it("passes the profile's channel location on every spawn; a chosen folder replaces it and a claim keeps it (D17)", async () => {
    const controller = buildWith({ channelsDir: '/home/me/Collimate/Channels-Dev' });
    const state = await refusedStart(controller, 78, [REFUSAL]);
    expect(channelFlags(harness.spawns[0]!.args)).toEqual(['--channels-dir', '/home/me/Collimate/Channels-Dev']);

    const claimed = controller.start({ claimChannels: true }).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(0);
    harness.child.exit(78, null);
    await vi.advanceTimersByTimeAsync(1_000);
    await claimed;
    expect(channelFlags(harness.spawns[1]!.args)).toEqual([
      '--channels-dir', '/home/me/Collimate/Channels-Dev', '--claim-channels',
    ]);

    harness.health = 'ok';
    const started = controller.start({ channelsDir: '/srv/channels' });
    await vi.advanceTimersByTimeAsync(0);
    await started;
    expect(channelFlags(harness.spawns[2]!.args)).toEqual(['--channels-dir', '/srv/channels']);
    // The recorded identity is the plain start's, the profile's location in it.
    expect(String(harness.record()?.['spec'])).toContain('--channels-dir /home/me/Collimate/Channels-Dev');
    expect(String(harness.record()?.['spec'])).not.toContain('/srv/channels');
    expect(state.reason).toBe('locationOwned');
  });
});
