import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  UvUnavailableError,
  cachedUvPath,
  describeAttempts,
  ensureUv,
  resolveUv,
  uvxInvocation,
  type UvProbes,
} from '../src/main/uvProvision.js';

/**
 * Finding uv.
 *
 * uv is the shell's one hard external dependency, and the order of the
 * discovery ladder is load-bearing in both directions: get it too eager
 * and every launch pays for a login shell that costs seconds; get it too
 * lazy and a Mac launched from the Dock reports "uv not found" for a uv
 * the user's terminal finds instantly. So the assertions here are mostly
 * about order, and about what is *not* called.
 */

const HOME = '/home/tester';
const BIN = '/data/Collimate/bin';
const PATH_UV = 'uv';

const dirs: string[] = [];

afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

interface FakeMachine {
  probes: Partial<UvProbes> & { loginShell?: (env: NodeJS.ProcessEnv) => string | null };
  /** Every candidate we ran `uv --version` on, in order. */
  probed: string[];
  /** Whether the login shell was consulted at all. */
  askedShell: boolean;
}

/**
 * A machine with a known set of uv binaries on it.
 * `runnable` maps a path to the version it reports; anything in `present`
 * exists on disk but is broken (a stub, a bad download) unless it is also
 * runnable.
 */
function machine(options: {
  runnable?: Record<string, string>;
  present?: string[];
  loginShell?: string | null;
}): FakeMachine {
  const runnable = options.runnable ?? {};
  const present = new Set([...(options.present ?? []), ...Object.keys(runnable)]);
  const state: FakeMachine = { probes: {}, probed: [], askedShell: false };
  state.probes = {
    version: (candidate) => {
      state.probed.push(candidate);
      return runnable[candidate] ?? null;
    },
    exists: (file) => present.has(file),
    homedir: () => HOME,
    loginShell: () => {
      state.askedShell = true;
      return options.loginShell ?? null;
    },
  };
  return state;
}

const wellKnown = {
  astral: path.join(HOME, '.local', 'bin', 'uv'),
  homebrew: '/opt/homebrew/bin/uv',
  usrLocal: '/usr/local/bin/uv',
  cargo: path.join(HOME, '.cargo', 'bin', 'uv'),
};

// ---------------------------------------------------------------------------

describe('the discovery ladder', () => {
  it('F1031 keeps the spawn resolver free of retired Windows executable branches', () => {
    const source = readFileSync(new URL('../src/main/uvProvision.ts', import.meta.url), 'utf8');
    expect(source).not.toMatch(/uv\.exe|windowsHide|['"]win32['"]/);
  });

  it('prefers an explicit $COLLIMATE_UV over everything else', () => {
    const fake = machine({ runnable: { '/opt/mine/uv': 'uv 0.9.0', [PATH_UV]: 'uv 0.8.0' } });

    const found = resolveUv({ binDir: BIN, env: { COLLIMATE_UV: '/opt/mine/uv' }, probes: fake.probes });

    expect(found).toEqual({ uv: '/opt/mine/uv', source: 'env', version: 'uv 0.9.0' });
    expect(fake.probed).toEqual(['/opt/mine/uv']);
  });

  it('falls through an override that does not run rather than failing outright', () => {
    const fake = machine({ runnable: { [PATH_UV]: 'uv 0.8.0' } });

    const found = resolveUv({ binDir: BIN, env: { COLLIMATE_UV: '/opt/typo/uv' }, probes: fake.probes });

    expect(found).toEqual({ uv: PATH_UV, source: 'path', version: 'uv 0.8.0' });
    expect(fake.probed).toEqual(['/opt/typo/uv', PATH_UV]);
  });

  it('takes PATH before the well-known locations', () => {
    const fake = machine({ runnable: { [PATH_UV]: 'uv 0.8.0', [wellKnown.astral]: 'uv 0.7.0' } });

    const found = resolveUv({ binDir: BIN, env: {}, probes: fake.probes });

    expect(found?.source).toBe('path');
    expect(fake.probed).toEqual([PATH_UV]);
  });

  it('walks the well-known locations in install-popularity order', () => {
    const fake = machine({
      runnable: { [wellKnown.homebrew]: 'uv 0.8.1', [wellKnown.cargo]: 'uv 0.6.0' },
      // The astral default is there but broken — a half-finished install.
      present: [wellKnown.astral],
    });

    const found = resolveUv({ binDir: BIN, env: {}, probes: fake.probes });

    expect(found).toEqual({ uv: wellKnown.homebrew, source: 'well-known', version: 'uv 0.8.1' });
    // Probed in order, stopping at the first one that answers; the cargo
    // install further down the list was never touched.
    expect(fake.probed).toEqual([PATH_UV, wellKnown.astral, wellKnown.homebrew]);
  });

  it('uses its cached download without consulting a login shell __F1020_F0987', () => {
    const cached = cachedUvPath(BIN);
    const fake = machine({ runnable: { [cached]: 'uv 0.8.17' }, loginShell: '/opt/somewhere/uv' });

    const found = resolveUv({ binDir: BIN, env: { SHELL: '/bin/zsh' }, probes: fake.probes });

    expect(found).toEqual({ uv: cached, source: 'cache', version: 'uv 0.8.17' });
    // An extra injected legacy probe must remain unused, even with a shell configured.
    expect(fake.askedShell).toBe(false);
  });

  it('never invokes a second login shell after general environment discovery __F1020_F0987', () => {
    const fake = machine({ runnable: { '/opt/nix/bin/uv': 'uv 0.8.5' }, loginShell: '/opt/nix/bin/uv' });

    const found = resolveUv({ binDir: BIN, env: { SHELL: '/bin/zsh' }, probes: fake.probes });

    expect(found).toBeNull();
    expect(fake.askedShell).toBe(false);
    expect(fake.probed).not.toContain('/opt/nix/bin/uv');
  });

  it('ignores the retired injected login-shell probe __F1020_F0987', () => {
    const fake = machine({ loginShell: '/opt/stale/uv' });
    expect(resolveUv({ binDir: BIN, env: { SHELL: '/bin/zsh' }, probes: fake.probes })).toBeNull();
  });

  it('returns null when the machine has no uv anywhere', () => {
    const fake = machine({});

    expect(resolveUv({ binDir: BIN, env: {}, probes: fake.probes })).toBeNull();

    // Everything got its turn: PATH, four well-known paths (none present,
    // so unprobed), then the cache. No additional shell is started.
    expect(fake.probed).toEqual([PATH_UV]);
    expect(fake.askedShell).toBe(false);
  });
});

describe('describeAttempts', () => {
  it('lists every location in the order they were tried', () => {
    const attempts = describeAttempts(BIN, { COLLIMATE_UV: '/opt/mine/uv', SHELL: '/bin/zsh' });

    expect(attempts[0]).toBe('$COLLIMATE_UV (/opt/mine/uv)');
    expect(attempts[1]).toBe('uv on PATH');
    expect(attempts).toContain(cachedUvPath(BIN));
    expect(attempts.indexOf(cachedUvPath(BIN))).toBeGreaterThan(1);
    expect(attempts.at(-1)).toBe(cachedUvPath(BIN));
    expect(attempts.some((line) => line.includes('command -v uv'))).toBe(false);
  });

  it('leaves out the override and the shell when neither is set', () => {
    const attempts = describeAttempts(BIN, {});
    expect(attempts[0]).toBe('uv on PATH');
    expect(attempts.some((line) => line.includes('COLLIMATE_UV'))).toBe(false);
    expect(attempts.some((line) => line.includes('command -v uv'))).toBe(false);
  });
});

describe('uvxInvocation', () => {
  function binDir(files: string[]): string {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'collimate-uvx-'));
    dirs.push(dir);
    for (const file of files) writeFileSync(path.join(dir, file), '');
    return dir;
  }

  it('uses the uvx sitting next to uv', () => {
    const dir = binDir(['uv', 'uvx']);
    expect(uvxInvocation(path.join(dir, 'uv'))).toEqual({
      command: path.join(dir, 'uvx'),
      args: [],
    });
  });

  it('falls back to `uv tool run` when there is no sibling uvx', () => {
    const dir = binDir(['uv']);
    expect(uvxInvocation(path.join(dir, 'uv'))).toEqual({
      command: path.join(dir, 'uv'),
      args: ['tool', 'run'],
    });
  });

  it('falls back to `uv tool run` for a PATH-resolved name with nothing to check', () => {
    expect(uvxInvocation(PATH_UV)).toEqual({ command: PATH_UV, args: ['tool', 'run'] });
  });
});

describe('ensureUv', () => {
  it('reports what it found and installs nothing', async () => {
    const fake = machine({ runnable: { [PATH_UV]: 'uv 0.8.17' } });
    const progress: string[] = [];

    const found = await ensureUv({
      binDir: BIN,
      env: {},
      probes: fake.probes,
      onProgress: (detail) => progress.push(detail),
      // Reaching for the network would blow up rather than download.
      fetchImpl: (() => Promise.reject(new Error('the network must not be touched'))) as unknown as typeof fetch,
    });

    expect(found.source).toBe('path');
    expect(progress).toEqual(['Looking for uv…']);
  });

  it('explains a failed download in terms of what to do about it', async () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'collimate-uvbin-'));
    dirs.push(dir);
    const fake = machine({});
    const progress: string[] = [];

    const error = await ensureUv({
      binDir: dir,
      env: {},
      probes: fake.probes,
      onProgress: (detail) => progress.push(detail),
      fetchImpl: (() => Promise.resolve({ ok: false, status: 503 })) as unknown as typeof fetch,
    }).then(
      () => null,
      (reason: unknown) => reason,
    );

    expect(error).toBeInstanceOf(UvUnavailableError);
    const unavailable = error as UvUnavailableError;
    expect(progress).toEqual([
      'Looking for uv…',
      'uv not found — installing it…',
      expect.stringContaining('Downloading uv for'),
    ]);
    expect(unavailable.message).toContain('https://docs.astral.sh/uv/getting-started/installation/');
    // The failure UI lists everywhere we looked, so a user with uv in an
    // unusual place can see that we didn't look there.
    expect(unavailable.attempts).toContain('uv on PATH');
    expect(unavailable.attempts).toContain(cachedUvPath(dir));
  });

  it('gives up on a download that stops sending data', async () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'collimate-uvbin-'));
    dirs.push(dir);
    const fake = machine({});

    const error = await ensureUv({
      binDir: dir,
      env: {},
      probes: fake.probes,
      downloadStallMs: 50,
      // A connection that never answers: it settles only when aborted.
      fetchImpl: ((_url: string, init?: RequestInit) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(init.signal?.reason));
        })) as unknown as typeof fetch,
    }).then(
      () => null,
      (reason: unknown) => reason,
    );

    expect(error).toBeInstanceOf(UvUnavailableError);
    expect((error as UvUnavailableError).message).toContain('no data');
  });

  const canProvision =
    (process.platform === 'linux' || process.platform === 'darwin') &&
    (process.arch === 'x64' || process.arch === 'arm64');

  it.runIf(canProvision)('downloads, unpacks and runs the release archive', async () => {
    // A stand-in for the release tarball: `uv-<triple>/uv`, a script that
    // answers --version.
    const src = mkdtempSync(path.join(os.tmpdir(), 'collimate-uvsrc-'));
    const dir = mkdtempSync(path.join(os.tmpdir(), 'collimate-uvbin-'));
    dirs.push(src, dir);
    mkdirSync(path.join(src, 'uv-test'));
    writeFileSync(path.join(src, 'uv-test', 'uv'), "#!/bin/sh\necho 'uv 0.0.0-test'\n", { mode: 0o755 });
    const tarball = path.join(src, 'uv.tar.gz');
    expect(spawnSync('tar', ['-czf', tarball, '-C', src, 'uv-test']).status).toBe(0);
    const bytes = readFileSync(tarball);
    const progress: string[] = [];

    const found = await ensureUv({
      binDir: dir,
      env: { PATH: process.env['PATH'] },
      probes: machine({}).probes,
      onProgress: (detail) => progress.push(detail),
      // A real Response, so the streamed-body read (the production path) runs.
      fetchImpl: (() => Promise.resolve(new Response(new Uint8Array(bytes)))) as unknown as typeof fetch,
    });

    expect(found).toEqual({ uv: cachedUvPath(dir), source: 'downloaded', version: 'uv 0.0.0-test' });
    expect(progress.some((line) => line.startsWith('Unpacking uv'))).toBe(true);
    // Flattened into binDir, and the archive removed once unpacked.
    expect(readdirSync(dir)).toEqual(['uv']);
  });
});
