// Boot cache for the settings the first paint needs — a localStorage
// snapshot of the last-applied channel sections (`theme`, the override;
// `keyboard`; `renderer_styles`) and of the server's theme library
// (`themeLibrary`), so the first paint (app boot, the channels page, any
// moment before the active channel's settings doc or the library arrives)
// matches what the user last saw instead of flashing defaults.
//
// The channel DB and the server's app settings are the source of truth;
// this is strictly a paint cache. Every local save AND every adopted server
// doc rewrites it, so it always reflects the most recently active channel.

import type { ChannelSettings, ThemeLibrary } from '@/lib/api';

const LS_KEY = 'channel-settings.cache.v1';

export interface SettingsBootCache {
    theme?: ChannelSettings['theme'];
    themeLibrary?: ThemeLibrary;
    keyboard?: ChannelSettings['keyboard'];
    renderer_styles?: ChannelSettings['renderer_styles'];
}

export function readBootCache(): SettingsBootCache {
    try {
        if (typeof localStorage === 'undefined') return {};
        const raw = localStorage.getItem(LS_KEY);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' ? parsed as SettingsBootCache : {};
    } catch { return {}; }
}

/** Merge several sections in one read + one write. */
export function writeBootCache(patch: Partial<SettingsBootCache>): void {
    try {
        if (typeof localStorage === 'undefined') return;
        const cache = readBootCache();
        Object.assign(cache, patch);
        localStorage.setItem(LS_KEY, JSON.stringify(cache));
    } catch { /* quota / SSR — cache is best-effort */ }
}

export function writeBootCacheSection<K extends keyof SettingsBootCache>(
    section: K, value: SettingsBootCache[K],
): void {
    writeBootCache({ [section]: value } as Partial<SettingsBootCache>);
}
