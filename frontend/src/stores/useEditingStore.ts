import { useChannelSettingsStore } from './useChannelSettingsStore';
// The editing preferences (Settings → Editing); today one flag, spellcheck.
//
// They are PER CHANNEL — the `editing` section of the channel settings doc.
// useChannelSettingsStore adopts the active channel's section on switch via
// `adoptEditing`; the Editing tab saves through `commitEditing`, which PUTs
// only the `editing` section. The boot cache paints the last-active
// channel's preferences before the first settings load.
//
// The store applies its preferences to <html> (lib/editingSnapshot): the
// HTML `spellcheck` attribute inherits to every editable that leaves it
// unset, the app's own fields and native renderers' (through the shadow
// host) alike, and the change event re-sends them to iframe renderers
// (SET_EDITING) and to native renderers' api.onEditingChange subscribers.
// main.tsx imports this module at boot so the first paint carries them.

import { create } from 'zustand';
import { putChannelSettings, type ChannelSettingsEditing } from '@/lib/api';
import { readBootCache, writeBootCacheSection } from '@/lib/settingsCache';
import { applyEditingSnapshot } from '@/lib/editingSnapshot';

export const DEFAULT_EDITING: Readonly<ChannelSettingsEditing> = Object.freeze({ spellcheck: true });

/** A section as the store holds it: a missing or non-boolean flag reads as
 *  its default (the server keeps only a real bool too). */
function sanitizeEditing(raw: unknown): ChannelSettingsEditing {
    const spellcheck = (raw as { spellcheck?: unknown } | null | undefined)?.spellcheck;
    return { spellcheck: typeof spellcheck === 'boolean' ? spellcheck : DEFAULT_EDITING.spellcheck };
}

interface EditingState {
    editing: ChannelSettingsEditing;
    /** Adopt the section of a loaded channel-settings doc — no persist. */
    adoptEditing: (editing: ChannelSettingsEditing | null | undefined) => void;
    /** Apply + persist (Settings → Editing). Optimistic: the store, <html>
     *  and the boot cache update at once; a failed PUT rejects so the tab can
     *  surface it and re-adopt the previous preferences. */
    commitEditing: (editing: ChannelSettingsEditing) => Promise<void>;
}

export const useEditingStore = create<EditingState>((set) => ({
    editing: sanitizeEditing(readBootCache().editing),
    adoptEditing: (editing) => set({ editing: sanitizeEditing(editing) }),
    commitEditing: async (editing) => {
        const channel = useChannelSettingsStore.getState().loadedFor;
        const next = sanitizeEditing(editing);
        set({ editing: next });
        writeBootCacheSection('editing', next);
        // Target the channel whose doc these preferences were adopted from —
        // not the live active channel (see keybindings/store.ts
        // persistOverrides).
        if (!channel) return; // no channel adopted (create view, boot, a switch still loading): cache only
        await putChannelSettings(channel, { editing: next });
    },
}));

// <html> and the renderer hosts follow the store from its first value on.
applyEditingSnapshot(useEditingStore.getState().editing);
useEditingStore.subscribe((s) => applyEditingSnapshot(s.editing));
