import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/**
 * Finding the `uv` a checkout's server runs with.
 *
 * A packaged app never looks for uv: it runs the server environment it
 * carries (resources/server, see server.ts) and names it to that server
 * (--server-env), which starts its generator runs with the uv and the
 * Python inside it. This is the uv used only
 * when the shell runs a checkout, `uv run --project <checkout> python -m
 * collimate_server`: a dev run from desktop/, or `COLLIMATE_SERVER_SPEC=
 * local:<checkout>` in front of any build. So it is found on the machine,
 * never downloaded; a checkout without uv is a developer setup to fix.
 */

export type UvSource =
  | 'env' // $COLLIMATE_UV
  | 'path' // plain `uv` resolved through PATH
  | 'well-known'; // one of the standard install locations

export interface UvResolution {
  /** Absolute path to the uv binary (or plain `uv` when found on PATH). */
  uv: string;
  source: UvSource;
  /** First line of `uv --version`. */
  version: string;
}

/**
 * The discovery ladder probes candidates without running a login shell.
 * Production defaults below, overridable per call, so the ladder's order
 * can be asserted without a real uv or a real HOME.
 */
export interface UvProbes {
  /** `uv --version` on a candidate; null if it isn't there or won't run. */
  version: (candidate: string, env: NodeJS.ProcessEnv) => string | null;
  exists: (file: string) => boolean;
  homedir: () => string;
}

const PROBE_TIMEOUT_MS = 5_000;

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

const nodeUvProbes: UvProbes = {
  version: probeUv,
  exists: existsSync,
  homedir: () => os.homedir(),
};

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
 * Look for uv: `$COLLIMATE_UV`, then PATH (the login shell's, which the
 * shell-environment probe has applied before a local start), then the
 * well-known install locations. Null when none of them runs.
 */
export function findUv(options: { env?: NodeJS.ProcessEnv; probes?: Partial<UvProbes> } = {}): UvResolution | null {
  const env = options.env ?? process.env;
  const { version: probeVersion, exists, homedir } = { ...nodeUvProbes, ...options.probes };

  const override = env['COLLIMATE_UV'];
  if (override) {
    // An override that doesn't run is reported in `uvAttempts` when nothing else works.
    const version = probeVersion(override, env);
    if (version) return { uv: override, source: 'env', version };
  }

  const onPath = probeVersion('uv', env);
  if (onPath) return { uv: 'uv', source: 'path', version: onPath };

  for (const candidate of wellKnownPaths(homedir())) {
    if (!exists(candidate)) continue;
    const version = probeVersion(candidate, env);
    if (version) return { uv: candidate, source: 'well-known', version };
  }
  return null;
}

/** Everywhere {@link findUv} looked, for the "uv not found" failure screen. */
export function uvAttempts(env: NodeJS.ProcessEnv = process.env, home: string = os.homedir()): string[] {
  const attempts: string[] = [];
  const override = env['COLLIMATE_UV'];
  if (override) attempts.push(`$COLLIMATE_UV (${override})`);
  attempts.push('uv on PATH');
  attempts.push(...wellKnownPaths(home));
  return attempts;
}
