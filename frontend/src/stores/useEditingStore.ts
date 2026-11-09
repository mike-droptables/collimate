// The editing preferences (the channels page's Settings → This profile):
// spellcheck, and status_on_run (a run start opens its cell's status
// section, lib/runAttach).
//
// They are PER PROFILE: the desktop profile's settings.json, else this
// browser's localStorage for this server (lib/profilePrefs). The store
// starts from the synchronous read there (the shell's bootstrap value or
// localStorage), so the first paint carries them; the Editing group saves
// through `commitEditing`, and a change made elsewhere (the shell's push
// after a load, another tab's `storage` event) is adopted. No channel and no
// server is involved.
//
// The store applies spellcheck to <html> (lib/editingSnapshot): the
// HTML `spellcheck` attribute inherits to every editable that leaves it
// unset, the app's own fields and native renderers' (through the shadow
// host) alike, and the change event re-sends them to iframe renderers
// (SET_EDITING) and to native renderers' api.onEditingChange subscribers.
// Renderers see spellcheck only: status_on_run is the app's own and never
// reaches the snapshot. main.tsx imports this module at boot so the first paint carries them.

import { create } from 'zustand';
import {
    onEditingPreferencesChanged,
    readEditingPreferences,
    sanitizeEditing,
    writeEditingPreferences,
    type EditingPreferences,
} from '@/lib/profilePrefs';
import { applyEditingSnapshot } from '@/lib/editingSnapshot';

interface EditingState {
    editing: EditingPreferences;
    /** Adopt preferences without saving (a change made elsewhere, a rollback). */
    adoptEditing: (editing: EditingPreferences | null | undefined) => void;
    /** Apply + persist (Settings → This profile → Editing). Optimistic: the
     *  store and <html> update at once, then take what was saved; a failed
     *  save rejects so the caller can surface it and re-adopt the previous
     *  preferences. */
    commitEditing: (editing: EditingPreferences) => Promise<void>;
}

export const useEditingStore = create<EditingState>((set) => ({
    editing: readEditingPreferences(),
    adoptEditing: (editing) => set({ editing: sanitizeEditing(editing) }),
    commitEditing: async (editing) => {
        const next = sanitizeEditing(editing);
        set({ editing: next });
        set({ editing: await writeEditingPreferences(next) });
    },
}));

// A change made elsewhere in this profile: the shell's push, another tab.
onEditingPreferencesChanged((prefs) => useEditingStore.getState().adoptEditing(prefs));

// <html> and the renderer hosts follow the store's spellcheck from its first
// value on.
applyEditingSnapshot({ spellcheck: useEditingStore.getState().editing.spellcheck });
useEditingStore.subscribe((s) => applyEditingSnapshot({ spellcheck: s.editing.spellcheck }));
