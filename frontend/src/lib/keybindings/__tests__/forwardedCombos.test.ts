// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildForwardedCombos, onForwardedCombosChange } from '../forwardedCombos';
import { parseCombo } from '../parser';
import { useKeybindingsStore } from '../store';

afterEach(() => useKeybindingsStore.setState({ overrides: {}, commands: {} }));

const has = (key: string, alt: boolean) =>
    buildForwardedCombos().some(c => c.key === key && c.altKey === alt && !c.shiftKey);

describe('buildForwardedCombos', () => {
    it('forwards a command binding with a modifier out of a renderer frame, as a shortcut is (CP-2)', () => {
        expect(has('l', true)).toBe(false);
        useKeybindingsStore.getState().setCommandBinding('log', parseCombo('Alt+L'));
        expect(has('l', true)).toBe(true);
    });

    it('never forwards a bare printable command binding: it is the renderer\'s own typing (CP-2)', () => {
        useKeybindingsStore.getState().setCommandBinding('log', parseCombo('g'));
        expect(buildForwardedCombos().some(c => c.key === 'g')).toBe(false);
    });

    it('forwards every registry shortcut with a modifier and no bare key: the modifier rule', () => {
        const combos = buildForwardedCombos();
        expect(combos.length).toBeGreaterThan(0);
        expect(combos.every(c => c.altKey || c.ctrlKey || c.metaKey)).toBe(true);
        // The width keys, the full-width toggle, cell moves and stack
        // navigation, each a chord, reach the host from a renderer too.
        for (const key of ['-', '=', '0', 'f']) expect(has(key, true)).toBe(true);
        expect(combos.some(c => c.key === 'ArrowRight' && c.ctrlKey && c.shiftKey)).toBe(true);
        expect(combos.some(c => c.key === 'ArrowLeft' && c.ctrlKey && !c.shiftKey)).toBe(true);
        for (const bare of ['/', 'Escape', 'Enter', 'ArrowUp', 'ArrowDown']) {
            expect(combos.some(c => c.key === bare && !c.altKey && !c.ctrlKey && !c.metaKey)).toBe(false);
        }
    });

    it('follows a remap: a shortcut remapped to a bare named key stops being forwarded', () => {
        expect(has('r', true)).toBe(true);
        useKeybindingsStore.getState().setOverride('stack.run', parseCombo('F7'));
        expect(has('r', true)).toBe(false);
        expect(buildForwardedCombos().some(c => c.key === 'F7')).toBe(false);
    });

    it('tells the frames to re-push when a command is bound or a shortcut remapped, not on other changes (CP-2)', () => {
        const listener = vi.fn();
        const stop = onForwardedCombosChange(listener);
        useKeybindingsStore.getState().setCommandBinding('log', parseCombo('Alt+L'));
        expect(listener).toHaveBeenCalledTimes(1);
        useKeybindingsStore.getState().setCapturing('log');
        expect(listener).toHaveBeenCalledTimes(1);
        useKeybindingsStore.getState().setOverride('stack.run', parseCombo('Alt+Y'));
        expect(listener).toHaveBeenCalledTimes(2);
        stop();
        useKeybindingsStore.getState().setCapturing(null);
    });
});
