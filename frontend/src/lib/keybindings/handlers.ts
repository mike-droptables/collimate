import { useKeybindingsStore } from './store';
import { matchCombo } from './match';

// `is(id)` — does the current event match the registered combo
// for `id`? Handles user overrides + platform Mod translation.
// Resolving per-call is O(1): the store does a dict lookup plus
// a parse of a short canonical string.
export const is = (e: KeyboardEvent, id: string): boolean => {
    const combo = useKeybindingsStore.getState().resolve(id);
    return combo ? matchCombo(e, combo) : false;
};
