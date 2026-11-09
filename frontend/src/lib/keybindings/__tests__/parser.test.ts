import { describe, it, expect } from 'vitest';
import { parseCombo, formatCombo } from '../parser';

describe('parseCombo', () => {
    it('returns null for empty / malformed', () => {
        expect(parseCombo('')).toBeNull();
        expect(parseCombo('+')).toBeNull();
        expect(parseCombo('K+L')).toBeNull(); // two non-modifier tokens
    });

    it('parses bare keys', () => {
        expect(parseCombo('Enter')).toEqual({ key: 'Enter' });
        expect(parseCombo('Escape')).toEqual({ key: 'Escape' });
        expect(parseCombo('k')).toEqual({ key: 'k' });
    });

    it('lowercases single-letter keys', () => {
        expect(parseCombo('K')).toEqual({ key: 'k' });
        expect(parseCombo('Mod+K')).toEqual({ mod: true, key: 'k' });
    });

    it('canonicalizes named-key aliases', () => {
        expect(parseCombo('esc')).toEqual({ key: 'Escape' });
        expect(parseCombo('return')).toEqual({ key: 'Enter' });
        expect(parseCombo('up')).toEqual({ key: 'ArrowUp' });
        expect(parseCombo('Up')).toEqual({ key: 'ArrowUp' });
        expect(parseCombo('Alt+left')).toEqual({ alt: true, key: 'ArrowLeft' });
    });

    it('parses all modifier forms', () => {
        expect(parseCombo('cmd+k')).toEqual({ meta: true, key: 'k' });
        expect(parseCombo('command+k')).toEqual({ meta: true, key: 'k' });
        expect(parseCombo('control+k')).toEqual({ ctrl: true, key: 'k' });
        expect(parseCombo('option+a')).toEqual({ alt: true, key: 'a' });
        expect(parseCombo('opt+a')).toEqual({ alt: true, key: 'a' });
    });

    it('parses combined modifiers regardless of order', () => {
        expect(parseCombo('Shift+Mod+D')).toEqual({ mod: true, shift: true, key: 'd' });
        expect(parseCombo('Mod+Shift+D')).toEqual({ mod: true, shift: true, key: 'd' });
    });

    it('preserves literal symbol keys', () => {
        expect(parseCombo('Mod+,')).toEqual({ mod: true, key: ',' });
        expect(parseCombo('\\')).toEqual({ key: '\\' });
    });
});

describe('formatCombo', () => {
    it('emits canonical order regardless of input order', () => {
        expect(formatCombo({ key: 'k', shift: true, mod: true })).toBe('Mod+Shift+k');
        expect(formatCombo({ key: 'k', shift: true, alt: true, ctrl: true }))
            .toBe('Ctrl+Alt+Shift+k');
    });

    it('renders bare key with no modifiers', () => {
        expect(formatCombo({ key: 'Escape' })).toBe('Escape');
    });

    it('displays space as Space', () => {
        expect(formatCombo({ mod: true, key: ' ' })).toBe('Mod+Space');
    });

    it('writes the + key as Plus so it round-trips', () => {
        expect(formatCombo({ shift: true, key: '+' })).toBe('Shift+Plus');
        expect(parseCombo('Shift+Plus')).toEqual({ shift: true, key: '+' });
        expect(parseCombo(formatCombo({ mod: true, key: '+' }))).toEqual({ mod: true, key: '+' });
    });
});
