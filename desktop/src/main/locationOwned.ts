/**
 * The local server's one startup refusal the shell answers itself: its
 * default channel location belongs to another Collimate installation.
 *
 * The server (api/collimate_server, plan 1105 S2) then writes one
 * machine-readable line to stderr,
 * `{"collimate_startup_refused":"location_owned","path":…,"owner_data_dir":…,"channels":N}`,
 * and exits with code 78 (EX_CONFIG). ServerController (server.ts) reads the
 * line out of the process log and fails the start as `locationOwned`;
 * main/index.ts then asks the user with {@link locationOwnedDialog} and, for
 * one spawn only, starts again with the flag the answer names. The shell
 * stores nothing: once the installation owns a location the server ignores
 * both flags.
 *
 * Pure, with the dialogs passed in, so the choice and the argv it leads to
 * are tested without Electron.
 */

/** The exit code that, with the line, means a refusal (sysexits EX_CONFIG). */
export const STARTUP_REFUSED_EXIT_CODE = 78;

/** What the refusal line says. */
export interface LocationOwned {
  /** The channel location the server would have used. */
  path: string;
  /** The data dir of the installation whose marker the location carries. */
  ownerDataDir: string;
  /** How many channels the location holds. */
  channels: number;
}

/** The one-spawn flag a start after a refusal adds. */
export type ChannelsOverride = { claimChannels: true } | { channelsDir: string };

/** The location-owned refusal on one line of server output, else null. */
export function parseStartupRefusal(line: string): LocationOwned | null {
  const text = line.trim();
  if (!text.startsWith('{') || !text.includes('"collimate_startup_refused"')) return null;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof value !== 'object' || value === null) return null;
  const record = value as Record<string, unknown>;
  const { path, owner_data_dir: ownerDataDir, channels } = record;
  if (record['collimate_startup_refused'] !== 'location_owned') return null;
  if (typeof path !== 'string' || typeof ownerDataDir !== 'string') return null;
  if (typeof channels !== 'number' || !Number.isInteger(channels) || channels < 0) return null;
  return { path, ownerDataDir, channels };
}

/**
 * The server's channel-location flags for one spawn: the profile's default
 * location (`--channels-dir`: `~/Collimate/Channels`, or the dev
 * profile's `~/Collimate/Channels-Dev`, D17) and the override a refusal's
 * answer chose. A chosen folder replaces the default; a claim
 * claims the location the server refused, which is that default.
 */
export function channelArgs(defaultDir: string | undefined, override?: ChannelsOverride): string[] {
  if (override && 'channelsDir' in override) return ['--channels-dir', override.channelsDir];
  const args = defaultDir === undefined ? [] : ['--channels-dir', defaultDir];
  return override ? [...args, '--claim-channels'] : args;
}

/** The buttons, in order; the index is the answer `showMessageBox` gives. */
export const LOCATION_OWNED_BUTTONS = ['Claim it', 'Choose another folder…', 'Quit'] as const;

/** Electron MessageBoxOptions, as far as this dialog uses them. */
export interface LocationOwnedDialog {
  type: 'warning';
  title: string;
  message: string;
  detail: string;
  buttons: string[];
  defaultId: number;
  cancelId: number;
  noLink: true;
}

export function locationOwnedDialog(refusal: LocationOwned): LocationOwnedDialog {
  const count = `${refusal.channels} channel${refusal.channels === 1 ? '' : 's'}`;
  return {
    type: 'warning',
    title: 'Channel location in use',
    message:
      `The channel location ${refusal.path} belongs to the Collimate installation at ` +
      `${refusal.ownerDataDir} (${count}).`,
    detail:
      'Claim it to make this installation its owner and bring its channels here; the other ' +
      'installation then shows the location as owned by another installation. Or choose another ' +
      'folder to keep this installation’s channels in a new location.',
    buttons: [...LOCATION_OWNED_BUTTONS],
    defaultId: 0,
    // Escape quits: starting the server either way is the user's call.
    cancelId: 2,
    noLink: true,
  };
}

/** The two native dialogs the choice needs. */
export interface LocationOwnedUi {
  /** Show the message box; resolves with the button's index. */
  ask(dialog: LocationOwnedDialog): Promise<number>;
  /** A folder chooser; resolves with the folder, or null on cancel. */
  pickFolder(options: { title: string; defaultPath?: string }): Promise<string | null>;
}

/**
 * Ask until the user decides: the override for the next spawn, or null to
 * quit. Cancelling the folder chooser asks again rather than quitting.
 */
export async function chooseLocationOwned(refusal: LocationOwned, ui: LocationOwnedUi): Promise<ChannelsOverride | null> {
  for (;;) {
    const answer = await ui.ask(locationOwnedDialog(refusal));
    if (answer === 0) return { claimChannels: true };
    if (answer !== 1) return null;
    const folder = await ui.pickFolder({ title: 'Choose a folder for this installation’s channels' });
    if (folder) return { channelsDir: folder };
  }
}
