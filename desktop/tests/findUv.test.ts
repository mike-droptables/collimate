import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { findUv, uvAttempts, type UvProbes } from '../src/main/findUv.js';

/**
 * Finding uv: the order of the ladder is what matters (and what is not
 * probed), so a fake machine records every `uv --version` it was asked for.
 */

const HOME = '/home/tester';
const PATH_UV = 'uv';

const wellKnown = {
  astral: path.join(HOME, '.local', 'bin', 'uv'),
  homebrew: '/opt/homebrew/bin/uv',
  usrLocal: '/usr/local/bin/uv',
  cargo: path.join(HOME, '.cargo', 'bin', 'uv'),
};

function machine(options: { runnable?: Record<string, string>; present?: string[] }): {
  probes: UvProbes;
  probed: string[];
} {
  const runnable = options.runnable ?? {};
  const present = new Set([...(options.present ?? []), ...Object.keys(runnable)]);
  const probed: string[] = [];
  return {
    probed,
    probes: {
      version: (candidate) => {
        probed.push(candidate);
        return runnable[candidate] ?? null;
      },
      exists: (file) => present.has(file),
      homedir: () => HOME,
    },
  };
}

describe('findUv', () => {
  it('prefers an explicit $COLLIMATE_UV over everything else', () => {
    const fake = machine({ runnable: { '/opt/mine/uv': 'uv 0.9.0', [PATH_UV]: 'uv 0.8.0' } });
    const found = findUv({ env: { COLLIMATE_UV: '/opt/mine/uv' }, probes: fake.probes });
    expect(found).toEqual({ uv: '/opt/mine/uv', source: 'env', version: 'uv 0.9.0' });
    expect(fake.probed).toEqual(['/opt/mine/uv']);
  });

  it('falls through an override that does not run', () => {
    const fake = machine({ runnable: { [PATH_UV]: 'uv 0.8.0' } });
    const found = findUv({ env: { COLLIMATE_UV: '/opt/typo/uv' }, probes: fake.probes });
    expect(found).toEqual({ uv: PATH_UV, source: 'path', version: 'uv 0.8.0' });
    expect(fake.probed).toEqual(['/opt/typo/uv', PATH_UV]);
  });

  it('takes PATH before the well-known locations', () => {
    const fake = machine({ runnable: { [PATH_UV]: 'uv 0.8.0', [wellKnown.astral]: 'uv 0.7.0' } });
    expect(findUv({ env: {}, probes: fake.probes })?.source).toBe('path');
    expect(fake.probed).toEqual([PATH_UV]);
  });

  it('walks the well-known locations in order, skipping absent ones and stopping at the first that runs', () => {
    const fake = machine({
      runnable: { [wellKnown.homebrew]: 'uv 0.8.1', [wellKnown.cargo]: 'uv 0.6.0' },
      present: [wellKnown.astral], // there but broken
    });
    const found = findUv({ env: {}, probes: fake.probes });
    expect(found).toEqual({ uv: wellKnown.homebrew, source: 'well-known', version: 'uv 0.8.1' });
    expect(fake.probed).toEqual([PATH_UV, wellKnown.astral, wellKnown.homebrew]);
  });

  it('reaches the cargo location last', () => {
    const fake = machine({ runnable: { [wellKnown.cargo]: 'uv 0.6.0' } });
    expect(findUv({ env: {}, probes: fake.probes })?.uv).toBe(wellKnown.cargo);
    expect(fake.probed).toEqual([PATH_UV, wellKnown.cargo]);
  });

  it('returns null when nothing runs', () => {
    const fake = machine({ present: [wellKnown.usrLocal] });
    expect(findUv({ env: {}, probes: fake.probes })).toBeNull();
    expect(fake.probed).toEqual([PATH_UV, wellKnown.usrLocal]);
  });

  it('returns null on a machine with no uv anywhere', () => {
    const fake = machine({});
    expect(findUv({ env: {}, probes: fake.probes })).toBeNull();
    expect(fake.probed).toEqual([PATH_UV]);
  });
});

describe('uvAttempts', () => {
  it('lists every location in the order they are tried', () => {
    expect(uvAttempts({ COLLIMATE_UV: '/opt/mine/uv' }, HOME)).toEqual([
      '$COLLIMATE_UV (/opt/mine/uv)',
      'uv on PATH',
      wellKnown.astral,
      wellKnown.homebrew,
      wellKnown.usrLocal,
      wellKnown.cargo,
    ]);
  });

  it('leaves out the override when it is not set', () => {
    const attempts = uvAttempts({}, HOME);
    expect(attempts[0]).toBe('uv on PATH');
    expect(attempts.some((line) => line.includes('COLLIMATE_UV'))).toBe(false);
  });
});
