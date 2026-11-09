import { expect, it } from 'vitest';
import { SHORTCUT_IDS as IDs } from '@/lib/keybindings/ids';
import { resolveSceneKeyboardGroup as resolve, type SceneKeyboardSnapshot } from './sceneKeyboardCommands';

const snapshot = (ids: string[], overrides: Partial<SceneKeyboardSnapshot> = {}): SceneKeyboardSnapshot => ({
    matched: new Set(ids), isZoomedOut: false, sceneFocusArea: 'main', selectedWindowId: 'stack', fromCommandPane: false, ...overrides,
});

it('F0936 pure groups preserve width and action branch priority for overlapping remaps', () => {
    const sizing = snapshot([IDs.SCENE_FULLSCREEN, IDs.SCENE_WIDTH_DEC, IDs.SCENE_WIDTH_INC]);
    expect(resolve('sizing', sizing)).toBe(IDs.SCENE_WIDTH_INC);
    expect(resolve('sizing', snapshot([IDs.SCENE_FULLSCREEN], { isZoomedOut: true }))).toBeNull();
    expect(resolve('actions', snapshot([IDs.STACK_RUN, IDs.STACK_NEW_CELL_RUN, IDs.STACK_NEW_CELL]))).toBe(IDs.STACK_NEW_CELL);
    expect(resolve('actions', snapshot([IDs.STACK_ROTATE_NEXT, IDs.CELL_REMOVE]))).toBe(IDs.CELL_REMOVE);
});

it('F0936 creation needs no selection while selected actions retain main-view focus gates', () => {
    expect(resolve('actions', snapshot([IDs.STACK_NEW_CELL], { selectedWindowId: null }))).toBe(IDs.STACK_NEW_CELL);
    expect(resolve('actions', snapshot([IDs.STACK_RUN], { selectedWindowId: null }))).toBeNull();
    expect(resolve('actions', snapshot([IDs.STACK_NEW_CELL], { sceneFocusArea: 'nav' }))).toBeNull();
    expect(resolve('actions', snapshot([IDs.STACK_NEW_CELL], { isZoomedOut: true }))).toBeNull();
});

it('F0936 mode and navigation groups retain zoom-first and right-direction precedence', () => {
    expect(resolve('modes', snapshot([IDs.SCENE_FAN_OUT, IDs.GLOBAL_TOGGLE_ZOOM]))).toBe(IDs.GLOBAL_TOGGLE_ZOOM);
    expect(resolve('navigation', snapshot([IDs.NAV_STACK_LEFT, IDs.NAV_STACK_RIGHT]))).toBe(IDs.NAV_STACK_RIGHT);
    expect(resolve('movement', snapshot([IDs.NAV_CELL_MOVE_LEFT, IDs.NAV_CELL_MOVE_RIGHT]))).toBe(IDs.NAV_CELL_MOVE_RIGHT);
    expect(resolve('back', snapshot([IDs.GLOBAL_BACK], { isZoomedOut: true, selectedWindowId: null }))).toBe(IDs.GLOBAL_BACK);
});

it('keys typed in the Commands pane leave Alt-↑/↓ and Esc to the pane; Ctrl-←/→ and other chords still act (C19)', () => {
    const pane = { fromCommandPane: true };
    expect(resolve('actions', snapshot([IDs.STACK_ROTATE_PREV], pane))).toBeNull();
    expect(resolve('actions', snapshot([IDs.STACK_ROTATE_NEXT], pane))).toBeNull();
    expect(resolve('back', snapshot([IDs.GLOBAL_BACK], pane))).toBeNull();
    expect(resolve('navigation', snapshot([IDs.NAV_STACK_LEFT], pane))).toBe(IDs.NAV_STACK_LEFT);
    expect(resolve('actions', snapshot([IDs.STACK_RUN], pane))).toBe(IDs.STACK_RUN);
    expect(resolve('modes', snapshot([IDs.SCENE_FAN_OUT], pane))).toBe(IDs.SCENE_FAN_OUT);
    expect(resolve('actions', snapshot([IDs.STACK_ROTATE_PREV]))).toBe(IDs.STACK_ROTATE_PREV);
    expect(resolve('navigation', snapshot([IDs.NAV_STACK_RIGHT], { isZoomedOut: true }))).toBe(IDs.NAV_STACK_RIGHT);
});
