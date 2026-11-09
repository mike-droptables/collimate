import { afterEach, describe, expect, it, vi } from 'vitest';
import { bindingPermitted, comboFromKeydown } from '../record';

const down = (init: Partial<Pick<KeyboardEvent, 'key' | 'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>>) => ({
    key: '', code: '', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...init,
});
const onPlatform = (platform: string) => vi.stubGlobal('navigator', { platform, userAgent: '' });

afterEach(() => vi.unstubAllGlobals());

describe('comboFromKeydown', () => {
    it('ignores a bare modifier, dead or lock key (CP-2)', () => {
        onPlatform('Linux x86_64');
        for (const key of ['Shift', 'Control', 'Alt', 'Meta', 'Dead', 'Fn', 'CapsLock']) {
            expect(comboFromKeydown(down({ key, shiftKey: key === 'Shift' }))).toBeNull();
        }
    });

    it('records Ctrl as Mod and Meta literally off macOS (CP-2)', () => {
        onPlatform('Linux x86_64');
        expect(comboFromKeydown(down({ key: 'K', code: 'KeyK', ctrlKey: true, shiftKey: true }))).toEqual({ key: 'k', mod: true, shift: true });
        expect(comboFromKeydown(down({ key: 'k', code: 'KeyK', metaKey: true }))).toEqual({ key: 'k', meta: true });
        expect(comboFromKeydown(down({ key: 'Enter', code: 'Enter' }))).toEqual({ key: 'Enter' });
    });

    it('records ⌘ as Mod and Ctrl literally on macOS, and Option chords by the physical key (CP-2)', () => {
        onPlatform('MacIntel');
        expect(comboFromKeydown(down({ key: 'k', code: 'KeyK', metaKey: true }))).toEqual({ key: 'k', mod: true });
        expect(comboFromKeydown(down({ key: 'k', code: 'KeyK', ctrlKey: true }))).toEqual({ key: 'k', ctrl: true });
        expect(comboFromKeydown(down({ key: '®', code: 'KeyR', altKey: true }))).toEqual({ key: 'r', alt: true });
        expect(comboFromKeydown(down({ key: '–', code: 'Minus', altKey: true }))).toEqual({ key: '-', alt: true });
    });
});

describe('bindingPermitted', () => {
    it('the cell-move shortcuts need a modifier; other shortcuts take a bare key (CP-2)', () => {
        expect(bindingPermitted('nav.cellMoveLeft', { key: 'ArrowLeft' })).toBe(false);
        expect(bindingPermitted('nav.cellMoveRight', { key: 'ArrowRight', alt: true })).toBe(true);
        expect(bindingPermitted('scene.fullscreen', { key: 'f' })).toBe(true);
    });
});
