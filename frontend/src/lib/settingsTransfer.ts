// The channel settings document as a `.json` file (the Commands pane's
// Settings → Export settings / Import settings, and the new-channel page's
// settings source). The callers do the fetching and the PUT; this names,
// writes, saves and parses the file. The file is the whole doc the server
// returned; an import sends the whole parsed doc back, which replaces every
// section it carries. Keys and accounts are never in the doc.

import type { ChannelSettings } from '@/lib/api/settings';

export const SETTINGS_FILE_KIND: ChannelSettings['kind'] = 'channel-settings';

/** `<channel name>-settings.json`, with the characters no OS allows in a
 *  file name (and control characters) replaced by `-`. */
export function settingsFileName(channelName: string | null | undefined): string {
    const base = (channelName ?? '')
        // eslint-disable-next-line no-control-regex
        .replace(/[/\\:*?"<>|\u0000-\u001f]/g, '-')
        .replace(/[. ]+$/, '')
        .trim();
    return `${base || 'channel'}-settings.json`;
}

/** The doc as the file's text: two-space JSON with a trailing newline. */
export function serializeSettings(doc: ChannelSettings): string {
    return `${JSON.stringify(doc, null, 2)}\n`;
}

export type SettingsImport =
    | { ok: true; doc: ChannelSettings }
    | { ok: false; error: string };

/** Parse an imported file's text. Refuses anything that is not a JSON object
 *  whose `kind` is the channel settings kind; the server validates the
 *  sections themselves on the PUT. */
export function parseSettingsImport(text: string): SettingsImport {
    let value: unknown;
    try {
        value = JSON.parse(text);
    } catch {
        return { ok: false, error: 'The file is not valid JSON.' };
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        return { ok: false, error: 'The file is not a settings document.' };
    }
    const kind = (value as { kind?: unknown }).kind;
    if (kind !== SETTINGS_FILE_KIND) {
        return { ok: false, error: `Not a channel settings file (kind ${JSON.stringify(kind ?? null)}).` };
    }
    return { ok: true, doc: value as ChannelSettings };
}

interface SaveFileHandle {
    createWritable(): Promise<{ write(data: Blob): Promise<void>; close(): Promise<void> }>;
}
interface SavePickerWindow {
    showSaveFilePicker?: (options: {
        suggestedName: string;
        types: { description: string; accept: Record<string, string[]> }[];
    }) => Promise<SaveFileHandle>;
}

/** Save `text` as `name`: the system save dialog where the File System
 *  Access API exists, else a download. False when the user cancelled. */
export async function saveJsonFile(name: string, text: string): Promise<boolean> {
    const blob = new Blob([text], { type: 'application/json' });
    const picker = window as unknown as SavePickerWindow;
    if (picker.showSaveFilePicker) {
        let handle: SaveFileHandle;
        try {
            handle = await picker.showSaveFilePicker({
                suggestedName: name,
                types: [{ description: 'Channel settings', accept: { 'application/json': ['.json'] } }],
            });
        } catch (e) {
            if (e instanceof DOMException && e.name === 'AbortError') return false;
            throw e;
        }
        const writable = await handle.createWritable();
        await writable.write(blob);
        await writable.close();
        return true;
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    return true;
}
