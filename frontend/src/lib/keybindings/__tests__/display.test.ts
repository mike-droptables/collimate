import { describe, it, expect, afterEach, vi } from 'vitest';
import { displayCombo, displayComboString } from '../display';
import { parseCombo } from '../parser';

function setPlatform(value: string) {
    vi.stubGlobal('navigator', { platform: value, userAgent: '' });
}

afterEach(() => vi.unstubAllGlobals());

describe('displayCombo on Mac', () => {
    it('uses symbol glyphs for modifiers + named keys', () => {
        setPlatform('MacIntel');
        expect(displayCombo(parseCombo('Mod+K'))).toBe('⌘K');
        expect(displayCombo(parseCombo('Mod+Shift+D'))).toBe('⌘⇧D');
        expect(displayCombo(parseCombo('Mod+Enter'))).toBe('⌘↵');
        expect(displayCombo(parseCombo('Escape'))).toBe('Esc');
        expect(displayCombo(parseCombo('Shift+Enter'))).toBe('⇧↵');
        expect(displayCombo(parseCombo('Alt+ArrowLeft'))).toBe('⌥←');
    });

    it('concats without separators', () => {
        setPlatform('MacIntel');
        expect(displayCombo(parseCombo('Ctrl+Alt+Shift+k'))).toBe('⌃⌥⇧K');
    });
});

describe('displayCombo on non-Mac', () => {
    it('uses spelled-out modifiers with +', () => {
        setPlatform('Linux x86_64');
        expect(displayCombo(parseCombo('Mod+K'))).toBe('Ctrl+K');
        expect(displayCombo(parseCombo('Mod+Shift+D'))).toBe('Ctrl+Shift+D');
        expect(displayCombo(parseCombo('Mod+Enter'))).toBe('Ctrl+Enter');
        expect(displayCombo(parseCombo('Escape'))).toBe('Esc');
    });

    it('keeps arrow glyphs as arrows', () => {
        setPlatform('Linux x86_64');
        expect(displayCombo(parseCombo('Alt+ArrowLeft'))).toBe('Alt+←');
    });
});

describe('displayComboString', () => {
    it('round-trips via parser', () => {
        setPlatform('MacIntel');
        expect(displayComboString('Shift+Mod+D')).toBe('⌘⇧D');
    });

    it('returns the input unchanged when unparseable', () => {
        // Modifiers only, no key — parseCombo returns null.
        expect(displayComboString('Shift+Alt')).toBe('Shift+Alt');
        expect(displayComboString('')).toBe('');
    });
});

describe('displayCombo — empty / null', () => {
    it('returns empty string', () => {
        expect(displayCombo(null)).toBe('');
        expect(displayCombo({ key: '' })).toBe('');
    });
});
