import { errorMessage } from './errors.js';
import { execFile, spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

/**
 * Finding (or installing) `uv`.
 *
 * The shell never bundles a Python. It asks uv to materialise the server —
 * `uv run` from a source checkout in dev, `uvx --from collimate==<v>` in
 * release — which means a working `uv` is the shell's one hard external
 * dependency. This module makes that dependency effectively unconditional:
 * find one if the machine has it, otherwise download the official
 * standalone build into userData and use that.
 */

export type UvSource =
  | 'env' // $COLLIMATE_UV
  | 'path' // plain `uv` resolved through PATH
  | 'well-known' // one of the standard install locations
  | 'cache' // a copy we downloaded on an earlier run
  | 'downloaded'; // just fetched from GitHub

export interface UvResolution {
  /** Absolute path to the uv binary (or plain `uv` when found on PATH). */
  uv: string;
  source: UvSource;
  /** First line of `uv --version`. */
  version: string;
}

export class UvUnavailableError extends Error {
  constructor(
    message: string,
    /** Every location we looked, in order, for the failure UI. */
    readonly attempts: string[],
  ) {
    super(message);
    this.name = 'UvUnavailableError';
  }
}

export type ProgressFn = (detail: string) => void;

/**
 * The discovery ladder probes candidates without running a login shell.
 *
 * Same shape of seam as ServerControllerDeps: production defaults below,
 * overridable per call, so the ladder's *order* — which is the part with
 * a startup cost attached to getting it wrong — can be asserted without
 * a real uv, a real HOME, or the user's login shell.
 */
export interface UvProbes {
  /** `uv --version` on a candidate; null if it isn't there or won't run. */
  version: (candidate: string, env: NodeJS.ProcessEnv) => string | null;
  exists: (file: string) => boolean;
  homedir: () => string;
}

const nodeUvProbes: UvProbes = {
  version: probeUv,
  exists: existsSync,
  homedir: () => os.homedir(),
};

export interface EnsureUvOptions {
  /** `<userData>/bin` — where a downloaded uv is cached. */
  binDir: string;
  env?: NodeJS.ProcessEnv;
  onProgress?: ProgressFn;
  /**
   * Used for the release download only. Production passes Electron's
   * `net.fetch` so the download honours the system proxy configuration.
   */
  fetchImpl?: typeof fetch;
  probes?: Partial<UvProbes>;
  /** How long the download may go without receiving data; tests shorten it. */
  downloadStallMs?: number;
}

const PROBE_TIMEOUT_MS = 5_000;
const DOWNLOAD_STALL_MS = 30_000;

/** `uv --version` on a candidate; null if it isn't there or doesn't run. */
export function probeUv(candidate: string, env?: NodeJS.ProcessEnv): string | null {
  try {
    const result = spawnSync(candidate, ['--version'], {
      encoding: 'utf8',
      timeout: PROBE_TIMEOUT_MS,
      env: env ?? process.env,
    });
    if (result.status !== 0 || result.error) return null;
    const line = (result.stdout || '').trim().split('\n')[0];
    return line ? line.trim() : 'uv (version unknown)';
  } catch {
    return null;
  }
}

/** The standard places uv's own installers put it. */
function wellKnownPaths(home: string): string[] {
  return [
    path.join(home, '.local', 'bin', 'uv'), // astral installer default
    '/opt/homebrew/bin/uv', // homebrew, apple silicon
    '/usr/local/bin/uv', // homebrew intel / manual install
    path.join(home, '.cargo', 'bin', 'uv'), // cargo install uv
  ];
}

/**
 * Look for an existing uv. Returns null only if every probe failed.
 *
 * Order: explicit override, PATH, well-known locations, our own cached
 * download. The general shell-environment probe has already resolved
 * login-shell PATH before the local server's start calls this discovery ladder.
 */
export function resolveUv(options: {
  binDir: string;
  env?: NodeJS.ProcessEnv;
  probes?: Partial<UvProbes>;
}): UvResolution | null {
  const env = options.env ?? process.env;
  const { version: probeVersion, exists, homedir } = { ...nodeUvProbes, ...options.probes };
  const home = homedir();

  const override = env['COLLIMATE_UV'];
  if (override) {
    const version = probeVersion(override, env);
    // An explicit override that doesn't work is a user error worth
    // surfacing, but not worth blocking on — fall through and report it
    // in `attempts` if nothing else works either.
    if (version) return { uv: override, source: 'env', version };
  }

  const onPath = probeVersion('uv', env);
  if (onPath) return { uv: 'uv', source: 'path', version: onPath };

  for (const candidate of wellKnownPaths(home)) {
    if (!exists(candidate)) continue;
    const version = probeVersion(candidate, env);
    if (version) return { uv: candidate, source: 'well-known', version };
  }

  const cached = cachedUvPath(options.binDir);
  if (exists(cached)) {
    const version = probeVersion(cached, env);
    if (version) return { uv: cached, source: 'cache', version };
  }

  return null;
}

/** Everywhere resolveUv looked, for the "uv not found" failure screen. */
export function describeAttempts(binDir: string, env: NodeJS.ProcessEnv = process.env): string[] {
  const attempts: string[] = [];
  const override = env['COLLIMATE_UV'];
  if (override) attempts.push(`$COLLIMATE_UV (${override})`);
  attempts.push('uv on PATH');
  for (const candidate of wellKnownPaths(os.homedir())) attempts.push(candidate);
  attempts.push(cachedUvPath(binDir));
  return attempts;
}

export function cachedUvPath(binDir: string): string {
  return path.join(binDir, 'uv');
}

/**
 * The `uvx` that pairs with a given `uv`.
 *
 * uv ships both binaries side by side, so `<uv>x` is normally right. When
 * it isn't (a distro that packaged only `uv`, a PATH-resolved name with no
 * sibling to check), fall back to `uv tool run`, which is what `uvx` is a
 * shorthand for. Returns the command plus the argv prefix to put in front
 * of the tool's own arguments.
 */
export function uvxInvocation(uv: string): { command: string; args: string[] } {
  if (path.isAbsolute(uv)) {
    const uvx = `${uv}x`;
    if (existsSync(uvx)) return { command: uvx, args: [] };
    return { command: uv, args: ['tool', 'run'] };
  }
  return { command: uv, args: ['tool', 'run'] };
}

// ---------------------------------------------------------------------------
// Provisioning
// ---------------------------------------------------------------------------

/** uv's release asset naming — <arch>-<vendor>-<os>. */
function targetTriple(): string | null {
  const { platform, arch } = process;
  if (platform === 'darwin') {
    if (arch === 'arm64') return 'aarch64-apple-darwin';
    if (arch === 'x64') return 'x86_64-apple-darwin';
    return null;
  }
  if (platform === 'linux') {
    if (arch === 'x64') return 'x86_64-unknown-linux-gnu';
    if (arch === 'arm64') return 'aarch64-unknown-linux-gnu';
    return null;
  }
  return null;
}

function releaseUrl(triple: string): string {
  return `https://github.com/astral-sh/uv/releases/latest/download/uv-${triple}.tar.gz`;
}

/**
 * Download the official standalone uv into `binDir`.
 *
 * Windows is deliberately unsupported: the shell has no local server
 * there (see connect.ts bootTarget) and never spawns one, so it never
 * needs uv.
 */
export async function provisionUv(options: EnsureUvOptions): Promise<UvResolution> {
  const { binDir } = options;
  const env = options.env ?? process.env;
  const onProgress = options.onProgress ?? (() => {});
  const fetchImpl = options.fetchImpl ?? fetch;

  const triple = targetTriple();
  if (!triple) {
    throw new UvUnavailableError(
      `No uv on this machine, and no automatic install is available for ${process.platform}/${process.arch}. ` +
        'Install uv from https://docs.astral.sh/uv/getting-started/installation/ and start Collimate again.',
      describeAttempts(binDir, env),
    );
  }

  const url = releaseUrl(triple);
  mkdirSync(binDir, { recursive: true });
  const archive = path.join(binDir, `uv-${triple}.tar.gz`);

  onProgress(`Downloading uv for ${triple}…`);
  // An idle timeout, not a total one: a slow but moving download must be
  // allowed to finish, while a stalled one has to end in the failure view
  // (Retry / Quit), because the status splash has no way out of its own.
  const stallMs = options.downloadStallMs ?? DOWNLOAD_STALL_MS;
  const controller = new AbortController();
  let stallTimer: ReturnType<typeof setTimeout> | undefined;
  const arm = () => {
    clearTimeout(stallTimer);
    stallTimer = setTimeout(() => controller.abort(new Error(`no data for ${stallMs / 1000}s`)), stallMs);
  };
  let bytes: Buffer;
  try {
    arm();
    const response = await fetchImpl(url, { redirect: 'follow', signal: controller.signal });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} from ${url}`);
    }
    if (response.body) {
      const reader = response.body.getReader();
      const chunks: Uint8Array[] = [];
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        arm();
        chunks.push(value);
      }
      bytes = Buffer.concat(chunks);
    } else {
      bytes = Buffer.from(await response.arrayBuffer());
    }
  } catch (error) {
    throw new UvUnavailableError(
      `Couldn't download uv from ${url}: ${errorMessage(error)}. ` +
        'Check the network connection, or install uv yourself from ' +
        'https://docs.astral.sh/uv/getting-started/installation/ and start Collimate again.',
      describeAttempts(binDir, env),
    );
  } finally {
    clearTimeout(stallTimer);
  }

  writeFileSync(archive, bytes);
  onProgress(`Unpacking uv (${Math.round(bytes.byteLength / 1_000_000)} MB)…`);

  // tar ships with macOS and every Linux we target; shelling out beats
  // pulling a decompression library into the shell for one call a year.
  // --strip-components=1 flattens the archive's `uv-<triple>/` wrapper so
  // `uv` and `uvx` land directly in binDir.
  // Awaited, not spawnSync: this runs on the Electron main process, which
  // must keep delivering splash IPC while tar works.
  try {
    await execFileAsync('tar', ['-xzf', archive, '-C', binDir, '--strip-components=1'], {
      encoding: 'utf8',
      timeout: 120_000,
      env,
    });
  } catch (error) {
    const e = error as { code?: unknown; stderr?: string; killed?: boolean };
    throw new UvUnavailableError(
      `Couldn't unpack uv: tar ${e.killed ? 'timed out' : `exited ${String(e.code)}`}${e.stderr ? ` — ${e.stderr.trim()}` : ''}`,
      describeAttempts(binDir, env),
    );
  } finally {
    rmSync(archive, { force: true });
  }

  const uv = cachedUvPath(binDir);
  for (const binary of [uv, `${uv}x`]) {
    try {
      chmodSync(binary, 0o755);
    } catch {
      // uvx may legitimately be absent in some archives; uvxInvocation
      // falls back to `uv tool run` when it is.
    }
  }

  const version = probeUv(uv, env);
  if (!version) {
    throw new UvUnavailableError(
      `Downloaded uv to ${uv}, but it wouldn't run. The download may be corrupt — delete ${binDir} and try again.`,
      describeAttempts(binDir, env),
    );
  }

  onProgress(`Installed ${version}`);
  return { uv, source: 'downloaded', version };
}

/** Find uv, or install it. Throws UvUnavailableError if neither works. */
export async function ensureUv(options: EnsureUvOptions): Promise<UvResolution> {
  const onProgress = options.onProgress ?? (() => {});
  onProgress('Looking for uv…');
  const found = resolveUv(options);
  if (found) return found;
  onProgress('uv not found — installing it…');
  return provisionUv(options);
}
