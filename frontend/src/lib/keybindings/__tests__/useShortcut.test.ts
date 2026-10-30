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
