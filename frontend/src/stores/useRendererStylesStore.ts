import { useChannelSettingsStore } from './useChannelSettingsStore';
// Each renderer's style choices (theme, font, text size), set from its
// style control and keyed by renderer id.
//
// They are PER CHANNEL — the `renderer_styles` section of the channel
// settings doc. useChannelSettingsStore adopts the active channel's section
// on switch via `adoptRendererStyles`; a renderer's api.setStyle commits
// through `commitStyle`, which PUTs the whole `renderer_styles` section (the
// section is replaced wholesale, so the client sends every id). The boot
// cache styles the last-active channel's renderers before the first
// settings load.
//
// The store applies its section to lib/rendererStyles, which tells the
// renderer hosts about every id whose record changed: NativeRenderer's
// api.onStyleChange subscribers and the SET_STYLE push into iframe
// renderers. main.tsx imports this module at boot so the first renderer
// mounts with the cached section.

import { create } from 'zustand';
import {
    putChannelSettings, type ChannelSettingsRendererStyles, type RendererStyleRecord,
} from '@/lib/api';
import { readBootCache, writeBootCacheSection } from '@/lib/settingsCache';
import {
    applyRendererStyles, isDefaultRendererStyle, isRendererStyleId, sanitizeRendererStyle, sanitizeRendererStyles,
} from '@/lib/rendererStyles';

interface RendererStylesState {
    styles: ChannelSettingsRendererStyles;
    /** Adopt the section of a loaded channel-settings doc — no persist. */
    adoptRendererStyles: (styles: ChannelSettingsRendererStyles | null | undefined) => void;
    /** Merge `patch` into `rendererId`'s record, apply and persist.
     *  Optimistic: the store, the renderers and the boot cache update at
     *  once; the whole section is then PUT to the loaded channel (cache only
     *  while none is loaded). A null field returns to the renderer's
     *  default, and a record left all null is dropped. An invalid renderer
     *  id is refused. */
    commitStyle: (rendererId: string, patch: Partial<RendererStyleRecord>) => Promise<void>;
}

export const useRendererStylesStore = create<RendererStylesState>((set, get) => ({
    styles: sanitizeRendererStyles(readBootCache().renderer_styles),
    adoptRendererStyles: (styles) => set({ styles: sanitizeRendererStyles(styles) }),
    commitStyle: async (rendererId, patch) => {
        if (!isRendererStyleId(rendererId)) return;
        const channel = useChannelSettingsStore.getState().loadedFor;
        const current = get().styles;
        const record = sanitizeRendererStyle({ ...current[rendererId], ...patch });
        const next: ChannelSettingsRendererStyles = { ...current };
        if (isDefaultRendererStyle(record)) delete next[rendererId];
        else next[rendererId] = record;
        const styles = sanitizeRendererStyles(next);
        set({ styles });
        writeBootCacheSection('renderer_styles', styles);
        // Target the channel whose doc this section was adopted from — not
        // the live active channel (see useEditingStore.commitEditing).
        if (!channel) return;
        await putChannelSettings(channel, { renderer_styles: styles });
    },
}));

// The renderer hosts follow the store from its first value on.
applyRendererStyles(useRendererStylesStore.getState().styles);
useRendererStylesStore.subscribe((s) => applyRendererStyles(s.styles));
