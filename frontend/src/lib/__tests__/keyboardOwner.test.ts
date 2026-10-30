// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import {
    BARE_KEYS_OWNER_ATTR, BARE_KEYS_OWNER_PROPS, KEYBOARD_OWNER_ATTR, KEYBOARD_OWNER_PROPS, NAV_KEYS_OWNER_ATTR, NAV_KEYS_OWNER_PROPS,
    eventPathHasAttr, sceneKeyboardOwned,
} from '@/lib/keyboardOwner';

// The scene's capture-phase keyboard handler skips any keystroke whose
// composed path crosses a keyboard owner (the stack menu, the stack-name
// input) — including one inside an open shadow root.
describe('eventPathHasAttr', () => {
    const capture = (target: Element, attr: string) => {
        let hit: boolean | null = null;
        const onKey = (e: KeyboardEvent) => { hit = eventPathHasAttr(e, attr); };
        window.addEventListener('keydown', onKey, { capture: true });
        target.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, composed: true }));
        window.removeEventListener('keydown', onKey, { capture: true });
        return hit;
    };

    it('sees an owner ancestor, and nothing outside one', () => {
        const owner = document.createElement('div');
        owner.setAttribute(KEYBOARD_OWNER_ATTR, KEYBOARD_OWNER_PROPS[KEYBOARD_OWNER_ATTR]);
        const inner = document.createElement('button');
        owner.appendChild(inner);
        const outside = document.createElement('button');
        document.body.append(owner, outside);
        expect(capture(inner, KEYBOARD_OWNER_ATTR)).toBe(true);
        expect(capture(outside, KEYBOARD_OWNER_ATTR)).toBe(false);
        owner.remove(); outside.remove();
    });

    it('crosses an open shadow root', () => {
        const host = document.createElement('div');
        host.setAttribute('data-pick-surface', '');
        const input = document.createElement('input');
        host.attachShadow({ mode: 'open' }).appendChild(input);
        document.body.appendChild(host);
        expect(capture(input, 'data-pick-surface')).toBe(true);
        host.remove();
    });
});

// The status pane's status view owns only its navigation keys: the scene's other
// shortcuts keep working while focus sits in it.
describe('sceneKeyboardOwned and a nav-keys owner', () => {
    const owned = (target: Element, init: KeyboardEventInit) => {
        let hit: boolean | null = null;
        const onKey = (e: KeyboardEvent) => { hit = sceneKeyboardOwned(e); };
        window.addEventListener('keydown', onKey, { capture: true });
        target.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, composed: true, ...init }));
        window.removeEventListener('keydown', onKey, { capture: true });
        return hit;
    };

    it('holds back ↑ ↓ ↵ Esc and Ctrl+←/→, and lets every other key reach the scene (C16) (C20)', () => {
        const section = document.createElement('div');
        section.setAttribute(NAV_KEYS_OWNER_ATTR, NAV_KEYS_OWNER_PROPS[NAV_KEYS_OWNER_ATTR]);
        const row = document.createElement('button');
        section.appendChild(row);
        const outside = document.createElement('button');
        document.body.append(section, outside);
        for (const key of ['ArrowUp', 'ArrowDown', 'Enter', 'Escape']) expect(owned(row, { key })).toBe(true);
        expect(owned(row, { key: 'ArrowLeft', ctrlKey: true })).toBe(true);
        expect(owned(row, { key: 'ArrowRight', ctrlKey: true })).toBe(true);
        expect(owned(row, { key: 'v', altKey: true })).toBe(false);
        expect(owned(row, { key: 'k', ctrlKey: true })).toBe(false);
        expect(owned(row, { key: '/' })).toBe(false);
        expect(owned(row, { key: 'ArrowDown', altKey: true })).toBe(false);
        expect(owned(outside, { key: 'ArrowDown' })).toBe(false);
        section.remove(); outside.remove();
    });

    it('counts a focused element inside the section as its key too (C20)', () => {
        const section = document.createElement('div');
        section.setAttribute(NAV_KEYS_OWNER_ATTR, '');
        const row = document.createElement('button');
        section.appendChild(row);
        document.body.appendChild(section);
        row.focus();
        expect(owned(document.body, { key: 'Escape' })).toBe(true);
        expect(owned(document.body, { key: 'v', altKey: true })).toBe(false);
        section.remove();
    });
});

// The Capabilities manage card owns its unmodified keys only: Alt, Ctrl and
// Meta chords stay the scene's while focus is in it.
describe('sceneKeyboardOwned and a bare-keys owner', () => {
    const owned = (target: Element, init: KeyboardEventInit) => {
        let hit: boolean | null = null;
        const onKey = (e: KeyboardEvent) => { hit = sceneKeyboardOwned(e); };
        window.addEventListener('keydown', onKey, { capture: true });
        target.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, composed: true, ...init }));
        window.removeEventListener('keydown', onKey, { capture: true });
        return hit;
    };
    const card = () => {
        const root = document.createElement('div');
        root.setAttribute(BARE_KEYS_OWNER_ATTR, BARE_KEYS_OWNER_PROPS[BARE_KEYS_OWNER_ATTR]);
        const input = root.appendChild(document.createElement('input'));
        document.body.appendChild(root);
        return { root, input };
    };

    it('holds back keys with no Alt, Ctrl or Meta, Shift-only ones included', () => {
        const { root, input } = card();
        for (const init of [{ key: 'a' }, { key: '/' }, { key: 'ArrowDown' }, { key: 'ArrowLeft' }, { key: 'Enter' }, { key: 'Escape' }, { key: 'Backspace' }, { key: 'A', shiftKey: true }, { key: 'ArrowUp', shiftKey: true }]) {
            expect(owned(input, init)).toBe(true);
        }
        root.remove();
    });

    it('lets Alt, Ctrl and Meta chords reach the scene', () => {
        const { root, input } = card();
        for (const init of [
            { key: 'ArrowDown', altKey: true }, { key: 'ArrowUp', altKey: true }, { key: 'ArrowRight', ctrlKey: true }, { key: 'ArrowLeft', ctrlKey: true },
            { key: 'k', ctrlKey: true }, { key: 'k', metaKey: true }, { key: 'Backspace', altKey: true, shiftKey: true },
        ]) expect(owned(input, init)).toBe(false);
        root.remove();
    });

    it('counts a focused element inside the card as its key too', () => {
        const { root, input } = card();
        input.focus();
        expect(owned(document.body, { key: '/' })).toBe(true);
        expect(owned(document.body, { key: 'ArrowDown', altKey: true })).toBe(false);
        root.remove();
    });
});
