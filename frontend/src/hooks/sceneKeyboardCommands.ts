import { SHORTCUT_IDS } from '@/lib/keybindings/ids';

export interface SceneKeyboardSnapshot {
    matched: ReadonlySet<string>;
    isZoomedOut: boolean;
    sceneFocusArea: string;
    selectedWindowId: string | null;
    /** Typed in the Commands pane's query: the pane handles Alt-↑/↓ and Esc
     *  itself (the list keys never reach here as scene keys). */
    fromCommandPane: boolean;
}

const sizing = [SHORTCUT_IDS.SCENE_WIDTH_INC, SHORTCUT_IDS.SCENE_WIDTH_DEC, SHORTCUT_IDS.SCENE_WIDTH_RESET];
const create = [SHORTCUT_IDS.STACK_NEW_CELL, SHORTCUT_IDS.STACK_NEW_CELL_RUN];
const rotate: readonly string[] = [SHORTCUT_IDS.STACK_ROTATE_PREV, SHORTCUT_IDS.STACK_ROTATE_NEXT];
const selected = [SHORTCUT_IDS.STACK_RUN, SHORTCUT_IDS.STACK_DUPLICATE, SHORTCUT_IDS.STACK_DUPLICATE_RUN,
    SHORTCUT_IDS.STACK_DUPLICATE_STACK, SHORTCUT_IDS.STACK_CENTER, SHORTCUT_IDS.STACK_ALIGN_LEFT,
    SHORTCUT_IDS.STACK_ALIGN_RIGHT, SHORTCUT_IDS.STACK_REMOVE, SHORTCUT_IDS.CELL_REMOVE,
    SHORTCUT_IDS.STACK_ROTATE_PREV, SHORTCUT_IDS.STACK_ROTATE_NEXT];

/** Resolve each group against the event-time snapshot in the original branch order. */
export function resolveSceneKeyboardGroup(group: 'sizing' | 'modes' | 'actions' | 'navigation' | 'movement' | 'back', snapshot: SceneKeyboardSnapshot): string | null {
    const { matched, isZoomedOut, sceneFocusArea, selectedWindowId, fromCommandPane } = snapshot;
    if (group === 'sizing') {
        return sizing.find(id => matched.has(id))
            ?? (!isZoomedOut && matched.has(SHORTCUT_IDS.SCENE_FULLSCREEN) ? SHORTCUT_IDS.SCENE_FULLSCREEN : null);
    }
    if (group === 'modes') {
        return [SHORTCUT_IDS.GLOBAL_TOGGLE_ZOOM, SHORTCUT_IDS.SCENE_FAN_OUT].find(id => matched.has(id)) ?? null;
    }
    if (group === 'navigation' || group === 'movement') {
        const ids = group === 'navigation'
            ? [SHORTCUT_IDS.NAV_STACK_RIGHT, SHORTCUT_IDS.NAV_STACK_LEFT]
            : [SHORTCUT_IDS.NAV_CELL_MOVE_RIGHT, SHORTCUT_IDS.NAV_CELL_MOVE_LEFT];
        return ids.find(id => matched.has(id)) ?? null;
    }
    if (group === 'actions') {
        if (isZoomedOut || sceneFocusArea !== 'main') return null;
        return create.find(id => matched.has(id))
            ?? (selectedWindowId
                ? selected.find(id => matched.has(id) && !(fromCommandPane && rotate.includes(id))) ?? null
                : null);
    }
    return matched.has(SHORTCUT_IDS.GLOBAL_BACK) && !fromCommandPane ? SHORTCUT_IDS.GLOBAL_BACK : null;
}
