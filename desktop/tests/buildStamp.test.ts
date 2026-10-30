import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { buildSigned } from '../src/main/buildStamp.js';

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

  it('calls a build without the stamp unsigned: a dev run, a local package, a broken file', () => {
    expect(buildSigned(appDir(JSON.stringify({ name: 'collimate', version: '0.1.0' })))).toBe(false);
    expect(buildSigned(appDir(JSON.stringify({ collimate: { signed: 'true' } })))).toBe(false);
    expect(buildSigned(appDir(JSON.stringify({ collimate: 'signed' })))).toBe(false);
    expect(buildSigned(appDir('not json'))).toBe(false);
    expect(buildSigned(appDir(null))).toBe(false);
  });

  it('is what the repository package.json says today: unsigned', () => {
    expect(buildSigned(path.resolve('.'))).toBe(false);
  });
});
