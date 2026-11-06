// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import { blockNativeSelection } from '../nativeSelection';

const press = (target: EventTarget | null, button = 0) => ({ button, target, preventDefault: vi.fn() });

describe('blockNativeSelection', () => {
    it('cancels a primary press on plain chrome', () => {
        const div = document.createElement('div');
        const e = press(div);
        blockNativeSelection(e);
        expect(e.preventDefault).toHaveBeenCalled();
    });

    it('leaves other buttons alone', () => {
        const e = press(document.createElement('div'), 2);
        blockNativeSelection(e);
        expect(e.preventDefault).not.toHaveBeenCalled();
    });

    it('keeps caret and selection in editable targets', () => {
        const input = document.createElement('input');
        const inInput = press(input);
        blockNativeSelection(inInput);
        expect(inInput.preventDefault).not.toHaveBeenCalled();

        const editable = document.createElement('div');
        editable.setAttribute('contenteditable', 'true');
        const span = document.createElement('span');
        editable.appendChild(span);
        const inEditable = press(span);
        blockNativeSelection(inEditable);
        expect(inEditable.preventDefault).not.toHaveBeenCalled();
    });
});
