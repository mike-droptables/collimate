import { useState } from 'react';
import { PanelTop, SpellCheck } from 'lucide-react';
import { useEditingStore } from '@/stores/useEditingStore';
import type { EditingPreferences } from '@/lib/profilePrefs';
import { ToggleRow } from './ToggleRow';

// ─── Editing section ─────────────────────────────────────────────────────────
// The editing preferences of THIS PROFILE (the desktop profile's
// settings.json, else this browser's localStorage: lib/profilePrefs), the
// Editing group of the channels page's Settings → This profile
// (ProfileSection). Every editable follows Spellcheck: the app's own fields
// and every renderer's editors, native and iframe alike. Open status when a
// run starts is the app's own (lib/runAttach).

export function EditingSection() {
    const editing = useEditingStore(s => s.editing);
    const [error, setError] = useState<string | null>(null);
    const [savedFlash, setSavedFlash] = useState(false);

    // Merge onto the LIVE store section (not a captured closure), so two
    // quick toggles compose.
    const commit = async (patch: Partial<EditingPreferences>) => {
        const prev = useEditingStore.getState().editing;
        try {
            await useEditingStore.getState().commitEditing({ ...prev, ...patch });
            setError(null);
            setSavedFlash(true);
            setTimeout(() => setSavedFlash(false), 1200);
        } catch (e) {
            useEditingStore.getState().adoptEditing(prev);  // roll back the optimistic flip
            setError(e instanceof Error ? e.message : 'Save failed');
        }
    };

    return (
        <div className="flex flex-col gap-2">
            {savedFlash && <p className="text-[0.71875rem] text-green">Saved</p>}
            {error && <p role="alert" className="text-[0.75rem] text-red">{error}</p>}
            <ToggleRow
                icon={<SpellCheck className="w-4 h-4" />}
                label="Spellcheck"
                desc="Underline misspelled words as you type; right-click one for suggestions."
                checked={editing.spellcheck}
                onChange={v => void commit({ spellcheck: v })}
            />
            <ToggleRow
                icon={<PanelTop className="w-4 h-4" />}
                label="Open status when a run starts"
                desc="Show the cell's status section, with the new run's row expanded, each time a run starts here."
                checked={editing.status_on_run}
                onChange={v => void commit({ status_on_run: v })}
            />
        </div>
    );
}
