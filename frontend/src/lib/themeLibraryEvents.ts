// The server's theme library changed elsewhere (another window, another
// channel's pane): useChannelSSE hears `app_settings_changed` on the open
// channel's stream, or opens a stream that may have missed one, and calls
// reloadThemeLibrary; a theme section opening calls it too. ThemeProvider,
// which owns the library, re-reads it. A window event keeps the stream hook free
// of a context import.

export const THEME_LIBRARY_CHANGED_EVENT = 'collimate:app-settings-changed';

export function reloadThemeLibrary(): void {
    window.dispatchEvent(new Event(THEME_LIBRARY_CHANGED_EVENT));
}
