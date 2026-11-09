// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, renderHook } from '@testing-library/react';
import { useShortcut } from '../useShortcut';
import { SHORTCUT_IDS } from '../ids';

const typeIn = (el: HTMLElement, init: KeyboardEventInit) =>
    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init }));

afterEach(() => {
    cleanup();
    document.body.replaceChildren();
});

describe('useShortcut skipWhenTyping', () => {
    it('a modified chord fires from a text field; a bare key there is text (CP-2)', () => {
        const input = document.createElement('input');
        document.body.append(input);
        const settings = vi.fn();
        const commands = vi.fn();
        renderHook(() => {
            useShortcut(SHORTCUT_IDS.GLOBAL_OPEN_SETTINGS, settings);
            useShortcut(SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS, commands);
        });
        typeIn(input, { key: ',', ctrlKey: true });
        typeIn(input, { key: '/' });
        expect(settings).toHaveBeenCalledOnce();
        expect(commands).not.toHaveBeenCalled();
        typeIn(document.body, { key: '/' });
        expect(commands).toHaveBeenCalledOnce();
    });

    it('skipWhenTyping false fires a bare key from a text field (CP-2)', () => {
        const input = document.createElement('input');
        document.body.append(input);
        const commands = vi.fn();
        renderHook(() => useShortcut(SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS, commands, { skipWhenTyping: false }));
        typeIn(input, { key: '/' });
        expect(commands).toHaveBeenCalledOnce();
    });
});

describe('useShortcut on the window: the modifier rule', () => {
    /** A stand-in native renderer: a field inside an open shadow root, with
     *  its own keydown listener. */
    const renderer = () => {
        const host = document.createElement('div');
        document.body.append(host);
        const root = host.attachShadow({ mode: 'open' });
        const field = document.createElement('textarea');
        root.append(field);
        const own = vi.fn();
        field.addEventListener('keydown', own);
        return { field, own };
    };

    it('a chord fires ahead of a renderer field and is consumed when the handler takes it', () => {
        const { field, own } = renderer();
        const title = vi.fn((e: KeyboardEvent) => e.preventDefault());
        renderHook(() => useShortcut(SHORTCUT_IDS.CELL_EDIT_TITLE, title));
        typeIn(field, { key: 't', code: 'KeyT', altKey: true, composed: true });
        expect(title).toHaveBeenCalledOnce();
        expect(own).not.toHaveBeenCalled();
    });

    it('a chord its handler stands back from keeps travelling', () => {
        const { field, own } = renderer();
        const palette = vi.fn();
        renderHook(() => useShortcut(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE, palette));
        typeIn(field, { key: 'k', ctrlKey: true, metaKey: true, composed: true });
        typeIn(field, { key: 'k', ctrlKey: true, composed: true });
        expect(palette).toHaveBeenCalled();
        expect(own).toHaveBeenCalled();
    });

    it('a bare key listens in the bubble phase: whatever has focus can keep it', () => {
        const button = document.createElement('button');
        document.body.append(button);
        button.addEventListener('keydown', e => e.stopPropagation());
        const commands = vi.fn();
        renderHook(() => useShortcut(SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS, commands));
        typeIn(button, { key: '/' });
        expect(commands).not.toHaveBeenCalled();
        typeIn(document.body, { key: '/' });
        expect(commands).toHaveBeenCalledOnce();
    });

    it('hears a key a renderer frame forwarded onto the window', () => {
        const title = vi.fn();
        renderHook(() => useShortcut(SHORTCUT_IDS.CELL_EDIT_TITLE, title));
        window.dispatchEvent(new KeyboardEvent('keydown', { key: 't', code: 'KeyT', altKey: true, bubbles: true, cancelable: true }));
        expect(title).toHaveBeenCalledOnce();
    });
});
