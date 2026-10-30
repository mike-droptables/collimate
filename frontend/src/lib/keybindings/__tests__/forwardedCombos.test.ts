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
