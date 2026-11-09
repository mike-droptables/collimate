import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { buildInfoOf, buildSigned, describeBuild, parseBuildStamp, readBuildStamp } from '../src/main/buildStamp.js';

const made: string[] = [];

function appDir(packageJson: string | null): string {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'collimate-stamp-'));
  made.push(dir);
  if (packageJson !== null) writeFileSync(path.join(dir, 'package.json'), packageJson);
  return dir;
}

afterEach(() => {
  for (const dir of made.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe('buildSigned', () => {
  it('reads the stamp the release packaging config writes', () => {
    expect(buildSigned(appDir(JSON.stringify({ name: 'collimate', collimate: { signed: true } })))).toBe(true);
    expect(buildSigned(appDir(JSON.stringify({ name: 'collimate', collimate: { signed: false } })))).toBe(false);
  });

  it('calls a build without the stamp unsigned: a dev run, a broken file', () => {
    expect(buildSigned(appDir(JSON.stringify({ name: 'collimate', version: '0.1.0' })))).toBe(false);
    expect(buildSigned(appDir(JSON.stringify({ collimate: { signed: 'true' } })))).toBe(false);
    expect(buildSigned(appDir(JSON.stringify({ collimate: 'signed' })))).toBe(false);
    expect(buildSigned(appDir('not json'))).toBe(false);
    expect(buildSigned(appDir(null))).toBe(false);
  });

  it('calls a stamped build without a signature unsigned', () => {
    expect(buildSigned(appDir(JSON.stringify({ collimate: { signed: false, build: STAMP } })))).toBe(false);
  });

  it('is what the repository package.json says today: unsigned', () => {
    expect(buildSigned(path.resolve('.'))).toBe(false);
  });
});

const STAMP = {
  commit: 'cfdf5158a1b2c3d4e5f60718293a4b5c6d7e8f90',
  dirty: true,
  builtAt: '2026-10-31T22:00:00Z',
  server: 'ab'.repeat(32),
};

/** A package.json whose build stamp is STAMP with `change` applied. */
function stamped(change: (stamp: Record<string, unknown>) => void = () => {}): string {
  const stamp = structuredClone(STAMP) as unknown as Record<string, unknown>;
  change(stamp);
  return appDir(JSON.stringify({ name: 'collimate', collimate: { signed: false, build: stamp } }));
}

describe('readBuildStamp', () => {
  it('reads the stamp every package carries', () => {
    expect(readBuildStamp(stamped())).toEqual({ status: 'valid', stamp: STAMP });
  });

  it('accepts a null server: a package without a server environment', () => {
    expect(
      readBuildStamp(
        stamped((s) => {
          s['server'] = null;
        }),
      ),
    ).toEqual({ status: 'valid', stamp: { ...STAMP, server: null } });
  });

  it('finds no stamp in a dev run and a broken file', () => {
    const absent = { status: 'absent' };
    expect(readBuildStamp(appDir(JSON.stringify({ collimate: { signed: true } })))).toEqual(absent);
    expect(readBuildStamp(appDir(JSON.stringify({ name: 'collimate' })))).toEqual(absent);
    expect(readBuildStamp(appDir(JSON.stringify({ collimate: 'build' })))).toEqual(absent);
    expect(readBuildStamp(appDir('not json'))).toEqual(absent);
    expect(readBuildStamp(appDir(null))).toEqual(absent);
    expect(readBuildStamp(path.resolve('.'))).toEqual(absent);
  });

  it('calls a stamp that is there but no object malformed, not absent', () => {
    const malformed = { status: 'malformed' };
    expect(readBuildStamp(appDir(JSON.stringify({ collimate: { build: 'yes' } })))).toEqual(malformed);
    expect(readBuildStamp(appDir(JSON.stringify({ collimate: { build: null } })))).toEqual(malformed);
    expect(readBuildStamp(appDir(JSON.stringify({ collimate: { build: {} } })))).toEqual(malformed);
    expect(readBuildStamp(appDir(JSON.stringify({ collimate: { build: [] } })))).toEqual(malformed);
  });

  it.each([
    ['a short commit', (s: Record<string, unknown>) => { s['commit'] = 'cfdf5158'; }],
    ['an uppercase commit', (s: Record<string, unknown>) => { s['commit'] = STAMP.commit.toUpperCase(); }],
    ['no commit', (s: Record<string, unknown>) => { delete s['commit']; }],
    ['a string dirty flag', (s: Record<string, unknown>) => { s['dirty'] = 'true'; }],
    ['no dirty flag', (s: Record<string, unknown>) => { delete s['dirty']; }],
    ['a numeric build time', (s: Record<string, unknown>) => { s['builtAt'] = 1_700_000_000; }],
    ['a build time that is no date', (s: Record<string, unknown>) => { s['builtAt'] = 'yesterday'; }],
    ['a short server hash', (s: Record<string, unknown>) => { s['server'] = 'a'.repeat(63); }],
    ['an uppercase server hash', (s: Record<string, unknown>) => { s['server'] = 'A'.repeat(64); }],
    ['a server hash that is no hex', (s: Record<string, unknown>) => { s['server'] = 'z'.repeat(64); }],
    ['a numeric server hash', (s: Record<string, unknown>) => { s['server'] = 7; }],
    ['no server key', (s: Record<string, unknown>) => { delete s['server']; }],
  ])('calls a stamp with %s malformed', (_label, change) => {
    expect(readBuildStamp(stamped(change))).toEqual({ status: 'malformed' });
  });
});

describe('parseBuildStamp', () => {
  it('parses the value of collimate.build', () => {
    expect(parseBuildStamp(STAMP)).toEqual(STAMP);
    expect(parseBuildStamp('nope')).toBeNull();
  });
});

describe('buildInfoOf', () => {
  it('carries the version and every stamp field', () => {
    expect(buildInfoOf('0.4.2', STAMP)).toEqual({
      version: '0.4.2',
      commit: STAMP.commit,
      dirty: true,
      builtAt: STAMP.builtAt,
      serverEnvironmentHash: STAMP.server,
    });
  });

  it('has a null server environment hash for a package without one', () => {
    expect(buildInfoOf('0.4.2', { ...STAMP, server: null }).serverEnvironmentHash).toBeNull();
  });

  it('is the version alone in a dev run', () => {
    expect(buildInfoOf('0.4.2', null)).toEqual({
      version: '0.4.2',
      commit: null,
      dirty: null,
      builtAt: null,
      serverEnvironmentHash: null,
    });
  });
});

describe('describeBuild', () => {
  it('names the commit, uncommitted changes, the build time and the server environment', () => {
    expect(describeBuild(STAMP)).toBe(
      `build cfdf5158a1 (uncommitted changes), built 2026-10-31T22:00:00Z, server ${'ab'.repeat(8)}`,
    );
  });

  it('says nothing of changes for a clean tree', () => {
    expect(describeBuild({ ...STAMP, dirty: false })).toBe(
      `build cfdf5158a1, built 2026-10-31T22:00:00Z, server ${'ab'.repeat(8)}`,
    );
  });

  it('says so when there is no server environment', () => {
    expect(describeBuild({ ...STAMP, dirty: false, server: null })).toBe(
      'build cfdf5158a1, built 2026-10-31T22:00:00Z, no server environment',
    );
  });
});
