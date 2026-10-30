import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { PROFILE_ENV_VAR, resolveProfile } from '../src/main/profile.js';

/**
 * Which app this process is.
 *
 * The stakes here are asymmetric, which is why every field is asserted and
 * not just the id. Getting the dev profile wrong costs a developer a
 * confusing launch. Getting the *stable* profile wrong — a packaged app
 * that renames itself — moves the user-data directory out from under an
 * installed app: settings.json, saved servers, and the .collimate/ channel
 * database all appear to have vanished, with no error anywhere, because
 * Electron happily creates the new empty directory instead.
 */

const STABLE = {
  id: 'stable',
  name: 'Collimate',
  desktopName: 'collimate.desktop',
  appUserModelId: 'com.collimate.app',
  title: 'Collimate',
};

const DEV = {
  id: 'dev',
  name: 'Collimate Dev',
  desktopName: 'collimate-dev.desktop',
  appUserModelId: 'com.collimate.dev',
  title: 'Collimate Dev',
};

describe('resolveProfile', () => {
  it('gives a packaged app the stable identity', () => {
    expect(resolveProfile({ isPackaged: true, env: {} })).toEqual(STABLE);
  });

  it('gives an unpackaged run the dev identity', () => {
    expect(resolveProfile({ isPackaged: false, env: {} })).toEqual(DEV);
  });

  it('keeps every identity distinct between the two profiles', () => {
    // The whole point of the split: two shells that share any one of these
    // still collide — on the data directory, the taskbar entry, or the
    // window the user is trying to tell apart.
    const stable = resolveProfile({ isPackaged: true, env: {} });
    const dev = resolveProfile({ isPackaged: false, env: {} });
    for (const key of ['name', 'desktopName', 'appUserModelId', 'title'] as const) {
      expect(stable[key]).not.toBe(dev[key]);
    }
  });

  it('lets the env var force stable onto an unpackaged run', () => {
    // The documented escape hatch: run a dev build against the installed
    // app's real data to reproduce a bug in it.
    expect(resolveProfile({ isPackaged: false, env: { [PROFILE_ENV_VAR]: 'stable' } })).toEqual(STABLE);
  });

  it('lets the env var force dev onto a packaged app', () => {
    expect(resolveProfile({ isPackaged: true, env: { [PROFILE_ENV_VAR]: 'dev' } })).toEqual(DEV);
  });

  it('accepts the value however it was typed', () => {
    for (const value of ['DEV', 'Dev', ' dev ', '\tdev\n']) {
      expect(resolveProfile({ isPackaged: true, env: { [PROFILE_ENV_VAR]: value } }).id).toBe('dev');
    }
  });

  it('ignores a value it does not recognise rather than refusing to start', () => {
    // This runs before there is a window, a log, or a dialog to complain
    // in, so a typo in a shell rc file has to fall back, not be fatal.
    for (const value of ['', ' ', 'stabel', 'production', '1', 'true']) {
      expect(resolveProfile({ isPackaged: true, env: { [PROFILE_ENV_VAR]: value } })).toEqual(STABLE);
      expect(resolveProfile({ isPackaged: false, env: { [PROFILE_ENV_VAR]: value } })).toEqual(DEV);
    }
  });

  it('names the packaged .desktop entry after the stable app id', () => {
    // electron-builder (linux.syncDesktopName) names the deb/AppImage entry
    // from package.json desktopName; Wayland compositors match it against
    // the app id index.ts sets, case-sensitively.
    const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
    expect(pkg.desktopName).toBe(resolveProfile({ isPackaged: true, env: {} }).desktopName);
  });
});
