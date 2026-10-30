import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';

/**
 * build/dev_desktop.sh --wipe next to a live server (F1074).
 *
 * The wipe deletes the dev profile's .collimate/ under Electron's user-data
 * dir, and a server still running out of that dir keeps writing into the
 * unlinked files. The script refuses — it never kills — when the shell's
 * server.json names a live pid (every OS) or, on Linux, when /proc shows a
 * main.py running from the dir that no record names.
 *
 * Every run is sandboxed: HOME and the XDG dirs point into a temp root, and
 * uv/npm/npx on PATH are stubs that exit 97, so a wipe that goes ahead stops
 * at the first build step instead of building and launching the app.
 */

const SCRIPT = fileURLToPath(new URL('../../build/dev_desktop.sh', import.meta.url));
const STUB_EXIT = 97;

interface Sandbox {
  root: string;
  home: string;
  config: string;
  bin: string;
  userData: string;
  appDb: string;
}

const roots: string[] = [];
const children: ChildProcess[] = [];

function sandbox(): Sandbox {
  const root = mkdtempSync(path.join(os.tmpdir(), 'collimate-dev-wipe-'));
  roots.push(root);
  const home = path.join(root, 'home');
  // XDG_CONFIG_HOME through a symlink, as a /home -> /var/home layout has
  // it: /proc/<pid>/cwd reads resolved, the script's USER_DATA does not.
  const config = path.join(root, 'config');
  mkdirSync(path.join(root, 'config-real'));
  symlinkSync(path.join(root, 'config-real'), config);
  const userData =
    process.platform === 'darwin'
      ? path.join(home, 'Library', 'Application Support', 'Collimate Dev')
      : path.join(config, 'Collimate Dev');
  mkdirSync(path.join(userData, '.collimate'), { recursive: true });
  const appDb = path.join(userData, '.collimate', 'app.db');
  writeFileSync(appDb, 'a live database');
  const bin = path.join(root, 'bin');
  mkdirSync(bin);
  for (const tool of ['uv', 'npm', 'npx']) {
    writeFileSync(path.join(bin, tool), `#!/bin/sh\nexit ${STUB_EXIT}\n`, { mode: 0o755 });
  }
  return { root, home, config, bin, userData, appDb };
}

/**
 * Run the wipe. PATH defaults to the stubs first, node (the record parser)
 * from the interpreter running this test, and coreutils from the system.
 */
function runWipe(box: Sandbox, PATH = [box.bin, path.dirname(process.execPath), '/usr/bin', '/bin'].join(':')) {
  return spawnSync('bash', [SCRIPT, '--skip', '--wipe'], {
    env: {
      PATH,
      HOME: box.home,
      XDG_CONFIG_HOME: box.config,
      XDG_DATA_HOME: path.join(box.root, 'data'),
    },
    encoding: 'utf8',
    timeout: 60_000,
  });
}

/** A process that stays up until the test kills it. */
function longLived(args: string[] = [], cwd?: string): ChildProcess {
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)', ...args], {
    stdio: 'ignore',
    ...(cwd ? { cwd } : {}),
  });
  children.push(child);
  return child;
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

afterEach(() => {
  for (const child of children.splice(0)) child.kill('SIGKILL');
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

// A bash start and, on Linux, a /proc scan per case: roomier than 5 s.
describe('dev_desktop.sh --wipe', { timeout: 30_000 }, () => {
  it('refuses while server.json names a live pid, on every OS, and signals nothing__F1074', () => {
    const box = sandbox();
    const server = longLived();
    const pid = server.pid!;
    writeFileSync(
      path.join(box.userData, 'server.json'),
      JSON.stringify({ pid, port: 5001, url: 'http://127.0.0.1:5001', startedAt: Date.now() }, null, 2),
    );

    const result = runWipe(box);

    expect(result.status).toBe(1);
    expect(result.stderr).toContain(`server pid ${pid}`);
    // Quitting no longer frees the dir when the server was kept running: say how to stop it (MS-04).
    expect(result.stderr).toContain('File → Servers → Stop');
    expect(existsSync(box.appDb)).toBe(true);
    expect(alive(pid)).toBe(true);
  });

  it.skipIf(!existsSync('/proc/self'))(
    'refuses for a server running from the dir that no record names (Linux /proc)__F1074',
    () => {
      const box = sandbox();
      const server = longLived(['main.py'], box.userData);
      const pid = server.pid!;

      const result = runWipe(box);

      expect(result.status).toBe(1);
      expect(result.stderr).toContain(`pid ${pid}`);
      expect(existsSync(box.appDb)).toBe(true);
      expect(alive(pid)).toBe(true);
    },
  );

  it('checks for node before the wipe, so a missing record parser cannot let it through__F1074', () => {
    const box = sandbox();
    const server = longLived();
    writeFileSync(
      path.join(box.userData, 'server.json'),
      JSON.stringify({ pid: server.pid!, port: 5001, url: 'http://127.0.0.1:5001', startedAt: Date.now() }),
    );
    // A PATH with the stubs and every tool the script reaches for up to and
    // through the wipe, but no node: live_server_pid could not read the record.
    const tools = path.join(box.root, 'tools');
    mkdirSync(tools);
    for (const tool of ['bash', 'dirname', 'tr', 'sed', 'uname', 'readlink', 'grep', 'rm', 'cat', 'cmp', 'mkdir']) {
      const found = ['/usr/bin', '/bin'].map((dir) => path.join(dir, tool)).find((file) => existsSync(file));
      if (found) symlinkSync(found, path.join(tools, tool));
    }
    expect(existsSync(path.join(tools, 'node'))).toBe(false);

    const result = runWipe(box, [box.bin, tools].join(':'));

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("'node' not found");
    expect(existsSync(box.appDb)).toBe(true);
  });

  it('wipes when the record names a dead pid (a crash leftover)__F1074', () => {
    const box = sandbox();
    // Above every platform's pid_max: kill -0 finds no such process.
    writeFileSync(
      path.join(box.userData, 'server.json'),
      JSON.stringify({ pid: 2_147_483_000, port: 5001, url: 'http://127.0.0.1:5001', startedAt: 1 }),
    );

    const result = runWipe(box);

    expect(result.stderr).not.toContain('is running');
    expect(existsSync(path.join(box.userData, '.collimate'))).toBe(false);
  });
});
