import { useState } from 'react';
import { SpellCheck } from 'lucide-react';
import type { ChannelSettingsEditing } from '@/lib/api';
import { useEditingStore } from '@/stores/useEditingStore';
import { ToggleRow } from './ToggleRow';

// ─── Editing section ─────────────────────────────────────────────────────────
// The editing preferences of THIS channel (the `editing` section of the
// channel settings doc), in the Commands pane's settings column under the
// section's head. Every editable follows them: the app's own fields and
// every renderer's editors, native and iframe alike.

export function EditingSection() {
    const editing = useEditingStore(s => s.editing);
    const [error, setError] = useState<string | null>(null);
    const [savedFlash, setSavedFlash] = useState(false);

    // Merge onto the LIVE store section (not a captured closure), so two
    // quick toggles compose.
    const commit = async (patch: Partial<ChannelSettingsEditing>) => {
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
        </div>
    );
}
