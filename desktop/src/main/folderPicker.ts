import type { OpenDialogOptions, OpenDialogReturnValue } from 'electron';
import type { PickFolderOptions } from '../shared/bridge.js';

/**
 * The main-process half of the bridge's `pickFolder`: what the page may ask
 * of the OS folder chooser, and what reaches it back. main/index.ts opens
 * the dialog itself, parented to the app window, so this stays free of
 * Electron's runtime and is tested with plain values.
 */

/** Long enough for any title the page writes; a longer one is not one. */
const MAX_TITLE = 200;

/** The page's {@link PickFolderOptions}, checked; throws on anything else. */
export function parsePickFolder(payload: unknown): PickFolderOptions {
  if (typeof payload !== 'object' || payload === null) throw new Error('pickFolder takes {title, defaultPath?}.');
  const { title, defaultPath } = payload as Record<string, unknown>;
  if (typeof title !== 'string' || title.trim() === '' || title.length > MAX_TITLE) {
    throw new Error('pickFolder needs a title.');
  }
  if (defaultPath !== undefined && (typeof defaultPath !== 'string' || defaultPath === '')) {
    throw new Error('pickFolder defaultPath must be a path.');
  }
  return defaultPath === undefined ? { title } : { title, defaultPath };
}

/**
 * The dialog for `options`: one folder, which the user may make on the spot
 * (`createDirectory` is macOS's New Folder button; the Linux and Windows
 * choosers always offer one). Files never show as choices.
 */
export function folderDialogOptions(options: PickFolderOptions): OpenDialogOptions {
  return {
    title: options.title,
    ...(options.defaultPath !== undefined ? { defaultPath: options.defaultPath } : {}),
    properties: ['openDirectory', 'createDirectory'],
  };
}

/** The one chosen folder, or null for a cancel (or a dialog that chose none). */
export function chosenFolder(result: OpenDialogReturnValue): string | null {
  if (result.canceled) return null;
  return result.filePaths[0] ?? null;
}
