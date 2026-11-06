import { readFileSync } from 'node:fs';
import path from 'node:path';
import type { BuildInfo } from '../shared/bridge.js';

/**
 * What packaging stamped into the packaged package.json (electron-builder's
 * `extraMetadata`, written by build/desktop_packaging.py) under `collimate`:
 *
 * - `signed`: whether the build carries a code signature. The updater
 *   reads it (an unsigned macOS app cannot update itself).
 * - `build`: what every package was built from, a release's and a
 *   build/build_desktop.sh package's alike: the commit, whether the tree had
 *   uncommitted changes, when it was built, and `server`, the hash of the
 *   server environment it carries under resources/server
 *   (build/server_env.py's `collimate-server.json`), or null for a
 *   package without one (Windows, which is connect-only).
 *
 * A package.json without a stamp (a dev run from desktop/) is unsigned and
 * has no build.
 */

/** The `collimate.build` stamp. */
export interface BuildStamp {
  /** The full 40-hex git HEAD the package was built from. */
  commit: string;
  /** The tree that fed the build had uncommitted changes. */
  dirty: boolean;
  /** ISO-8601 UTC, when the build started. */
  builtAt: string;
  /** Lowercase hex sha256 naming the server environment, or null without one. */
  server: string | null;
}

/** `collimate` from `<appPath>/package.json`, or null when it is missing or garbled. */
function readStamp(appPath: string): Record<string, unknown> | null {
  let text: string;
  try {
    text = readFileSync(path.join(appPath, 'package.json'), 'utf8');
  } catch {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(text);
    if (!isObject(parsed)) return null;
    const stamp = parsed['collimate'];
    return isObject(stamp) ? stamp : null;
  } catch {
    return null;
  }
}

/** `collimate.signed`: false for a package without the stamp. */
export function buildSigned(appPath: string): boolean {
  return readStamp(appPath)?.['signed'] === true;
}

/**
 * What `collimate.build` says about this package: `absent` for a dev run
 * (or a package made without one), `valid` with the stamp, and `malformed`
 * when the key is there but does not parse: the server hash names the
 * environment the shell runs, so a half-understood stamp is no stamp.
 */
export type BuildStampRead =
  | { status: 'absent' }
  | { status: 'malformed' }
  | { status: 'valid'; stamp: BuildStamp };

/** The build stamp of the package at `appPath`. */
export function readBuildStamp(appPath: string): BuildStampRead {
  const stamp = readStamp(appPath);
  if (!stamp || !Object.hasOwn(stamp, 'build')) return { status: 'absent' };
  const build = parseBuildStamp(stamp['build']);
  return build ? { status: 'valid', stamp: build } : { status: 'malformed' };
}

/** {@link readBuildStamp}'s check on the already-parsed `collimate.build` value. */
export function parseBuildStamp(value: unknown): BuildStamp | null {
  if (!isObject(value)) return null;
  const { commit, dirty, builtAt, server } = value;
  if (typeof commit !== 'string' || !/^[0-9a-f]{40}$/.test(commit)) return null;
  if (typeof dirty !== 'boolean') return null;
  if (typeof builtAt !== 'string' || !builtAt || Number.isNaN(Date.parse(builtAt))) return null;
  if (server !== null && (typeof server !== 'string' || !/^[0-9a-f]{64}$/.test(server))) return null;
  return { commit, dirty, builtAt, server };
}

/** What the page's `buildInfo()` answers: the app version and the stamp, all null in a dev run. */
export function buildInfoOf(version: string, stamp: BuildStamp | null): BuildInfo {
  return {
    version,
    commit: stamp?.commit ?? null,
    dirty: stamp?.dirty ?? null,
    builtAt: stamp?.builtAt ?? null,
    serverEnvironmentHash: stamp?.server ?? null,
  };
}

/** One line for the launch log: `build <commit> (uncommitted changes), built <when>, server <hash>`. */
export function describeBuild(stamp: BuildStamp): string {
  const changes = stamp.dirty ? ' (uncommitted changes)' : '';
  const server = stamp.server ? `server ${stamp.server.slice(0, 16)}` : 'no server environment';
  return `build ${stamp.commit.slice(0, 10)}${changes}, built ${stamp.builtAt}, ${server}`;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
