// Boot cache for per-channel settings — a localStorage snapshot of the
// last-applied `theme`, `keyboard`, `editing` and `renderer_styles` sections so the first
// paint (app boot, channel-create view, any moment before the active
// channel's settings doc arrives) matches what the user last saw instead of
// flashing defaults.
//
// The channel DB is the source of truth; this is strictly a paint cache.
// Every local save AND every adopted server doc rewrites it, so it always
// reflects the most recently active channel.

import type { ChannelSettings } from '@/lib/api';

const LS_KEY = 'channel-settings.cache.v1';

export interface SettingsBootCache {
    theme?: ChannelSettings['theme'];
    keyboard?: ChannelSettings['keyboard'];
    editing?: ChannelSettings['editing'];
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
