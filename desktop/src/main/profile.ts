/**
 * Which Collimate this process is: the installed app, or a dev run.
 *
 * Everything that separates two simultaneously running shells hangs off
 * one decision. Electron derives the user-data directory from the app
 * name (`~/.config/<name>` on Linux, `~/Library/Application Support/<name>`
 * on macOS), and that directory holds settings.json, the .collimate/
 * channel database the spawned server writes, the run log, and the
 * single-instance lock. Two shells with the same name therefore cannot
 * coexist: the second launch hits the lock and hands its argv to the
 * first, and if it somehow got past that, two uvicorns would be writing
 * one SQLite file. Giving the unpackaged shell a different name is what
 * buys the user a stable app and a test build side by side, each with its
 * own server, its own data, and its own lock.
 *
 * Pure, and separate from index.ts, because the decision is made at module
 * load — before `whenReady`, before anything can be injected — so the only
 * way to test it is to keep it out of the module that calls `app.setName`.
 */

export type ProfileId = 'stable' | 'dev';

export interface AppProfile {
  id: ProfileId;
  /** `app.setName` — and thus the user-data directory's name. */
  name: string;
  /**
   * `app.setDesktopName`: the Wayland app id / WM_CLASS handle a desktop
   * environment matches icons and window rules on. The stable app installs
   * collimate.desktop (build/build_desktop.sh), so a dev run must claim a
   * different id or the compositor groups the two in one taskbar entry and
   * gives both the installed app's icon.
   *
   * An id only helps if a desktop entry answers to it — a compositor reads
   * the window's icon and name out of the entry it finds — so the dev id's
   * file is written by build/dev_desktop.sh, as a hidden (NoDisplay=true)
   * ~/.local/share/applications/collimate-dev.desktop. A dev run started
   * any other way gets the generic placeholder icon, which is cosmetic and
   * not worth this module knowing anything about the filesystem.
   */
  desktopName: string;
  /** Windows taskbar identity; same grouping argument as desktopName. */
  appUserModelId: string;
  /** Window and splash title, so the two shells are told apart on screen. */
  title: string;
}

const STABLE: AppProfile = {
  id: 'stable',
  name: 'Collimate',
  desktopName: 'collimate.desktop',
  appUserModelId: 'com.collimate.app',
  title: 'Collimate',
};

const DEV: AppProfile = {
  id: 'dev',
  name: 'Collimate Dev',
  desktopName: 'collimate-dev.desktop',
  appUserModelId: 'com.collimate.dev',
  title: 'Collimate Dev',
};

/** The env var that forces a profile, for both directions of the escape hatch. */
export const PROFILE_ENV_VAR = 'COLLIMATE_APP_PROFILE';

export interface ResolveProfileOptions {
  /** `app.isPackaged` — false for `electron dist/main.cjs`, true in a build. */
  isPackaged: boolean;
  env: NodeJS.ProcessEnv;
}

/**
 * Packaged means stable, unpackaged means dev — with an env override.
 *
 * Packaging is the right default signal because it is exactly the line
 * between "the thing the user installed" and "the thing a developer is
 * running out of a checkout", and it needs no flag at the call site: both
 * `npx electron dist/main.cjs` and `npm run dev` are unpackaged, and
 * everything electron-builder produces is packaged.
 *
 * COLLIMATE_APP_PROFILE overrides it in either direction, which is the
 * documented way to point a dev build at the installed app's data (to
 * reproduce a bug against real channels: COLLIMATE_APP_PROFILE=stable) or
 * to run an installed build against throwaway data. An unrecognised value
 * is ignored rather than fatal: this runs before there is a window, a log,
 * or a dialog to complain in, and silently using the default beats failing
 * to start over a typo in a shell rc file.
 *
 * This is not the only reader of that variable: build/dev_desktop.sh reads
 * it too, to pick the user-data directory it wipes and reports, and the two
 * must agree on the grammar — trim, lowercase, anything else ignored —
 * because a value the script and the shell read differently points them at
 * two different profiles' data, and the script would then wipe or describe
 * a directory the app is not using.
 */
export function resolveProfile(options: ResolveProfileOptions): AppProfile {
  const requested = (options.env[PROFILE_ENV_VAR] ?? '').trim().toLowerCase();
  if (requested === 'stable') return STABLE;
  if (requested === 'dev') return DEV;
  return options.isPackaged ? STABLE : DEV;
}
