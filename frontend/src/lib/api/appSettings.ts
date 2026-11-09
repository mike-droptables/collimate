import type { UserTheme } from '@/lib/userThemes';
import { getApiUrl, requestJson } from './client';
import { postBody, type StoredBlob, type UploadOptions } from './transfer';

// ── The server's own settings (api/collimate_server/app_settings.py) ──
// One document per server (its data dir), shared by every channel on it:
// the theme library (the user themes) and the default theme, the look of
// the channels page and of every channel that does not override it. A
// channel's override is its own doc's `theme.override` (./settings). PUT is
// section-wholesale, like the channel doc; every save is announced on every
// channel's stream as `app_settings_changed`. Theme pictures live in the
// server's app store beside the document, never in a channel's.

/** The theme library: the user themes and the default theme's id (null
 *  for the built-in default). */
export interface ThemeLibrary {
    defaultThemeId: string | null;
    userThemes: UserTheme[];
}

export interface AppSettings {
    version: number;
    kind: 'app-settings';
    theme: ThemeLibrary;
}

export const fetchAppSettings = (): Promise<AppSettings> => requestJson('/app-settings', 'GET');

export const putAppSettings = (partial: Partial<Pick<AppSettings, 'theme'>>): Promise<AppSettings> =>
    requestJson('/app-settings', 'PUT', partial);

/** Stream a theme picture into the server's app store
 *  (POST /api/app-settings/images). */
export const uploadThemeImage = (body: Blob, options: UploadOptions = {}): Promise<StoredBlob> =>
    postBody<StoredBlob>('/app-settings/images', body, options);

/** Where a theme picture is served, immutable-cached by its hash. */
export const themeImageUrl = (hash: string): string => `${getApiUrl()}/app-settings/images/${hash}`;
