import type { OpenDialogOptions } from 'electron';

/** `dialog.showOpenDialog`, bound to the window it is modal to. */
export type ShowOpenDialog = (options: OpenDialogOptions) => Promise<{ canceled: boolean; filePaths: string[] }>;

/**
 * The page's native folder dialog (`pickFolder` on the bridge): one folder,
 * its absolute path, or null when the user cancelled. The page sends the
 * path on to the local server, which reads the folder itself.
 */
export async function chooseFolder(show: ShowOpenDialog): Promise<string | null> {
  const { canceled, filePaths } = await show({ title: 'Choose a folder', properties: ['openDirectory'] });
  return canceled ? null : (filePaths[0] ?? null);
}
